# PyTorch StudioGAN: https://github.com/POSTECH-CVLab/PyTorch-StudioGAN
# The MIT License (MIT)
# See license file or visit https://github.com/POSTECH-CVLab/PyTorch-StudioGAN for details

# train.py


from data_utils.load_dataset import *
from metrics.inception_network import InceptionV3
from metrics.prepare_inception_moments_eval_dataset import prepare_inception_moments_eval_dataset
from models.linear_classifier import linear_classifier
from utils.log import make_run_name, make_logger, make_checkpoint_dir
from utils.losses import *
from utils.load_checkpoint import load_checkpoint
from utils.utils import *
from utils.biggan_utils import ema_
from sync_batchnorm.replicate import patch_replication_callback
from trainer import Trainer

import glob
import os
import PIL
from os.path import dirname, abspath, exists, join
import random
import warnings

import torch
from torchvision import transforms
from torch.utils.data import DataLoader
from torch.backends import cudnn
from torch.nn import DataParallel
from torch.utils.tensorboard import SummaryWriter



RUN_NAME_FORMAT = (
    "{framework}-"
    "{phase}-"
    "{timestamp}"
)

def train_framework(seed, num_workers, config_path, reduce_train_dataset, load_current, type4eval_dataset, dataset_name, num_classes, img_size, data_path,
                    architecture, conditional_strategy, hypersphere_dim, nonlinear_embed, normalize_embed, g_spectral_norm, d_spectral_norm, activation_fn,
                    attention, attention_after_nth_gen_block, attention_after_nth_dis_block, z_dim, shared_dim, g_conv_dim, d_conv_dim, G_depth, D_depth,
                    optimizer, batch_size, d_lr, g_lr, momentum, nesterov, alpha, beta1, beta2, total_step, adv_loss, consistency_reg, g_init, d_init,
                    random_flip_preprocessing, prior, truncated_factor, latent_op, ema, ema_decay, ema_start, synchronized_bn, hdf5_path_train, train_config,
                    model_config, **_):
    fix_all_seed(seed)
    cudnn.benchmark = False # Not good Generator for undetermined input size
    cudnn.deterministic = True
    n_gpus = torch.cuda.device_count()
    default_device = torch.cuda.current_device()
    second_device = default_device if n_gpus == 1 or conditional_strategy == "Proxy_NCA_GAN" else default_device+1
    assert batch_size % n_gpus == 0, "batch_size should be divided by the number of gpus "

    if n_gpus == 1:
        warnings.warn('You have chosen a specific GPU. This will completely '
                      'disable data parallelism.')

    step, best_step, best_fid, best_fid_checkpoint_path = 0, 0, None, None
    run_name = make_run_name(RUN_NAME_FORMAT,
                             framework=config_path.split('/')[3][:-5],
                             phase='train')

    logger = make_logger(run_name, None)
    writer = SummaryWriter(log_dir=join('./logs', run_name))
    logger.info('Run name : {run_name}'.format(run_name=run_name))
    logger.info(train_config)
    logger.info(model_config)

    logger.info('Loading train datasets...')
    train_dataset = LoadDataset(dataset_name, data_path, train=True, download=True, resize_size=img_size, conditional_strategy=conditional_strategy,
                                hdf5_path=hdf5_path_train, consistency_reg=consistency_reg, random_flip=random_flip_preprocessing)
    if reduce_train_dataset < 1.0:
        num_train = int(reduce_train_dataset*len(train_dataset))
        train_dataset, _ = torch.utils.data.random_split(train_dataset, [num_train, len(train_dataset) - num_train])
    logger.info('Train dataset size : {dataset_size}'.format(dataset_size=len(train_dataset)))

    logger.info('Loading {mode} datasets...'.format(mode=type4eval_dataset))
    eval_mode = True if type4eval_dataset == 'train' else False
    eval_dataset = LoadDataset(dataset_name, data_path, train=eval_mode, download=True, resize_size=img_size, conditional_strategy="no",
                               hdf5_path=None, consistency_reg=False, random_flip=False)
    logger.info('Eval dataset size : {dataset_size}'.format(dataset_size=len(eval_dataset)))

    logger.info('Building model...')
    if architecture == "dcgan":
        assert img_size == 32, "Sry, StudioGAN does not support dcgan models for generation of images larger than 32 resolution."
    module = __import__('models.{architecture}'.format(architecture=architecture),fromlist=['something'])
    logger.info('Modules are located on models.{architecture}'.format(architecture=architecture))
    Gen = module.Generator(z_dim, shared_dim, img_size, g_conv_dim, g_spectral_norm, attention, attention_after_nth_gen_block, activation_fn,
                           conditional_strategy, num_classes, synchronized_bn, g_init, G_depth).to(default_device)

    Dis = module.Discriminator(img_size, d_conv_dim, d_spectral_norm, attention, attention_after_nth_dis_block, activation_fn, conditional_strategy,
                               hypersphere_dim, num_classes, nonlinear_embed, normalize_embed, synchronized_bn, d_init, D_depth).to(default_device)

    if ema:
        print('Preparing EMA for G with decay of {}'.format(ema_decay))
        Gen_copy = module.Generator(z_dim, shared_dim, img_size, g_conv_dim, g_spectral_norm, attention, attention_after_nth_gen_block, activation_fn,
                                    conditional_strategy, num_classes, synchronized_bn=False, initialize=False, G_depth=G_depth).to(default_device)
        Gen_ema = ema_(Gen, Gen_copy, ema_decay, ema_start)
    else:
        Gen_copy, Gen_ema = None, None

    if n_gpus > 1:
        Gen = DataParallel(Gen, output_device=second_device)
        Dis = DataParallel(Dis, output_device=second_device)
        if ema:
            Gen_copy = DataParallel(Gen_copy, output_device=second_device)
        if synchronized_bn:
            patch_replication_callback(Gen)
            patch_replication_callback(Dis)

    logger.info(count_parameters(Gen))
    logger.info(Gen)

    logger.info(count_parameters(Dis))
    logger.info(Dis)

    train_dataloader = DataLoader(train_dataset, batch_size=batch_size, shuffle=True, pin_memory=True, num_workers=num_workers, drop_last=True)
    eval_dataloader = DataLoader(eval_dataset, batch_size=batch_size, shuffle=True, pin_memory=True, num_workers=num_workers, drop_last=False)

    G_loss = {'vanilla': loss_dcgan_gen, 'hinge': loss_hinge_gen, 'wasserstein': loss_wgan_gen}
    D_loss = {'vanilla': loss_dcgan_dis, 'hinge': loss_hinge_dis, 'wasserstein': loss_wgan_dis}

    if optimizer == "SGD":
        G_optimizer = torch.optim.SGD(filter(lambda p: p.requires_grad, Gen.parameters()), g_lr, momentum=momentum, nesterov=nesterov)
        D_optimizer = torch.optim.SGD(filter(lambda p: p.requires_grad, Dis.parameters()), d_lr, momentum=momentum, nesterov=nesterov)
    elif optimizer == "RMSprop":
        G_optimizer = torch.optim.RMSprop(filter(lambda p: p.requires_grad, Gen.parameters()), g_lr, momentum=momentum, alpha=alpha)
        D_optimizer = torch.optim.RMSprop(filter(lambda p: p.requires_grad, Dis.parameters()), d_lr, momentum=momentum, alpha=alpha)
    elif optimizer == "Adam":
        G_optimizer = torch.optim.Adam(filter(lambda p: p.requires_grad, Gen.parameters()), g_lr, [beta1, beta2], eps=1e-6)
        D_optimizer = torch.optim.Adam(filter(lambda p: p.requires_grad, Dis.parameters()), d_lr, [beta1, beta2], eps=1e-6)
    else:
        raise NotImplementedError

    if train_config['checkpoint_folder'] is not None:
        when = "current" if load_current is True else "best"
        if not exists(abspath(train_config['checkpoint_folder'])):
            raise NotADirectoryError
        checkpoint_dir = make_checkpoint_dir(train_config['checkpoint_folder'], run_name)
        g_checkpoint_dir = glob.glob(join(checkpoint_dir,"model=G-{when}-weights-step*.pth".format(when=when)))[0]
        d_checkpoint_dir = glob.glob(join(checkpoint_dir,"model=D-{when}-weights-step*.pth".format(when=when)))[0]
        Gen, G_optimizer, trained_seed, run_name, step, best_step = load_checkpoint(Gen, G_optimizer, g_checkpoint_dir)
        Dis, D_optimizer, trained_seed, run_name, step, best_step, best_fid, best_fid_checkpoint_path = load_checkpoint(Dis, D_optimizer, d_checkpoint_dir, metric=True)
        logger = make_logger(run_name, None)
        if ema:
            g_ema_checkpoint_dir = glob.glob(join(checkpoint_dir, "model=G_ema-{when}-weights-step*.pth".format(when=when)))[0]
            Gen_copy = load_checkpoint(Gen_copy, None, g_ema_checkpoint_dir, ema=True)
            Gen_ema.source, Gen_ema.target = Gen, Gen_copy

        writer = SummaryWriter(log_dir=join('./logs', run_name))
        assert seed == trained_seed, "seed for sampling random numbers should be same!"
        logger.info('Generator checkpoint is {}'.format(g_checkpoint_dir))
        logger.info('Discriminator checkpoint is {}'.format(d_checkpoint_dir))
    else:
        checkpoint_dir = make_checkpoint_dir(train_config['checkpoint_folder'], run_name)

    if train_config['eval']:
        inception_model = InceptionV3().to(default_device)
        inception_model = DataParallel(inception_model, output_device=second_device)
        mu, sigma, is_score, is_std = prepare_inception_moments_eval_dataset(dataloader=eval_dataloader,
                                                                             generator=Gen,
                                                                             eval_mode=type4eval_dataset,
                                                                             inception_model=inception_model,
                                                                             splits=10,
                                                                             run_name=run_name,
                                                                             logger=logger,
                                                                             device=second_device)
    else:
        mu, sigma, inception_model = None, None, None

    if train_config['linear_evaluation']:
        in_channels = hypersphere_dim if conditional_strategy == "ContraGAN" else Dis.out_dims[-1]
        linear_model = linear_classifier(in_channels=in_channels, num_classes=num_classes).to(default_device)
        if n_gpus > 1:
            linear_model = DataParallel(linear_model, output_device=second_device)
            if synchronized_bn:
                patch_replication_callback(linear_model)

        if optimizer == "SGD":
            L_optimizer = torch.optim.SGD(filter(lambda p: p.requires_grad, linear_model.parameters()), g_lr, momentum=momentum, nesterov=nesterov)
        elif optimizer == "RMSprop":
            L_optimizer = torch.optim.RMSprop(filter(lambda p: p.requires_grad, linear_model.parameters()), g_lr, momentum=momentum, alpha=alpha)
        elif optimizer == "Adam":
            L_optimizer = torch.optim.Adam(filter(lambda p: p.requires_grad, linear_model.parameters()), g_lr, [beta1, beta2], eps=1e-6)
        else:
            raise NotImplementedError
    else:
        linear_model = None
        L_optimizer = None


    trainer = Trainer(
        run_name=run_name,
        best_step=best_step,
        dataset_name=dataset_name,
        type4eval_dataset=type4eval_dataset,
        logger=logger,
        writer=writer,
        n_gpus=n_gpus,
        gen_model=Gen,
        dis_model=Dis,
        inception_model=inception_model,
        Gen_copy=Gen_copy,
        linear_model=linear_model,
        Gen_ema=Gen_ema,
        train_dataloader=train_dataloader,
        eval_dataloader=eval_dataloader,
        conditional_strategy=conditional_strategy,
        pos_collected_numerator=model_config['model']['pos_collected_numerator'],
        z_dim=z_dim,
        num_classes=num_classes,
        hypersphere_dim=hypersphere_dim,
        d_spectral_norm=d_spectral_norm,
        g_spectral_norm=g_spectral_norm,
        G_optimizer=G_optimizer,
        D_optimizer=D_optimizer,
        L_optimizer=L_optimizer,
        batch_size=batch_size,
        g_steps_per_iter=model_config['optimization']['g_steps_per_iter'],
        d_steps_per_iter=model_config['optimization']['d_steps_per_iter'],
        accumulation_steps=model_config['optimization']['accumulation_steps'],
        total_step = total_step,
        G_loss=G_loss[adv_loss],
        D_loss=D_loss[adv_loss],
        contrastive_lambda=model_config['loss_function']['contrastive_lambda'],
        margin=model_config['loss_function']['margin'],
        tempering_type=model_config['loss_function']['tempering_type'],
        tempering_step=model_config['loss_function']['tempering_step'],
        start_temperature=model_config['loss_function']['start_temperature'],
        end_temperature=model_config['loss_function']['end_temperature'],
        gradient_penalty_for_dis=model_config['loss_function']['gradient_penalty_for_dis'],
        gradient_penelty_lambda=model_config['loss_function']['gradient_penelty_lambda'],
        weight_clipping_for_dis=model_config['loss_function']['weight_clipping_for_dis'],
        weight_clipping_bound=model_config['loss_function']['weight_clipping_bound'],
        consistency_reg=consistency_reg,
        consistency_lambda=model_config['loss_function']['consistency_lambda'],
        diff_aug=model_config['training_and_sampling_setting']['diff_aug'],
        prior=prior,
        truncated_factor=truncated_factor,
        ema=ema,
        latent_op=latent_op,
        latent_op_rate=model_config['training_and_sampling_setting']['latent_op_rate'],
        latent_op_step=model_config['training_and_sampling_setting']['latent_op_step'],
        latent_op_step4eval=model_config['training_and_sampling_setting']['latent_op_step4eval'],
        latent_op_alpha=model_config['training_and_sampling_setting']['latent_op_alpha'],
        latent_op_beta=model_config['training_and_sampling_setting']['latent_op_beta'],
        latent_norm_reg_weight=model_config['training_and_sampling_setting']['latent_norm_reg_weight'],
        default_device=default_device,
        second_device=second_device,
        print_every=train_config['print_every'],
        save_every=train_config['save_every'],
        checkpoint_dir=checkpoint_dir,
        evaluate=train_config['eval'],
        mu=mu,
        sigma=sigma,
        best_fid=best_fid,
        best_fid_checkpoint_path=best_fid_checkpoint_path,
        train_config=train_config,
        model_config=model_config,
    )

    if conditional_strategy in ['ContraGAN', "Proxy_NCA_GAN", "XT_Xent_GAN"] and train_config['train']:
        step = trainer.run_ours(current_step=step, total_step=total_step)
    elif train_config['train']:
        step = trainer.run(current_step=step, total_step=total_step)

    if train_config['eval']:
        is_save = trainer.evaluation(step=step)

    if train_config['k_nearest_neighbor']:
        trainer.Nearest_Neighbor(nrow=train_config['nrow'], ncol=train_config['ncol'])

    if train_config['interpolation']:
        trainer.linear_interpolation(nrow=train_config['nrow'], ncol=train_config['ncol'], fix_z=True, fix_y=False)
        trainer.linear_interpolation(nrow=train_config['nrow'], ncol=train_config['ncol'], fix_z=False, fix_y=True)

    if train_config['linear_evaluation']:
        trainer.linear_classification(train_config['step_linear_eval'])
