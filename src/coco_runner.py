import argparse
import datetime
import math
import sys
import time

import torch
from torch import distributed as dist
from torch import nn

from models import get_model, load_ckpt, save_ckpt
from myutils.common import file_util, yaml_util
from myutils.pytorch import func_util
from utils import data_util, main_util, misc_util


def get_argparser():
    argparser = argparse.ArgumentParser(description=__doc__)
    argparser.add_argument('--config', required=True, help='yaml config file')
    argparser.add_argument('--device', default='cuda', help='device')
    argparser.add_argument('--json', help='dictionary to overwrite config')
    argparser.add_argument('-train', action='store_true', help='train a model')
    # distributed training parameters
    argparser.add_argument('--world_size', default=1, type=int, help='number of distributed processes')
    argparser.add_argument('--dist_url', default='env://', help='url used to set up distributed training')
    return argparser


def train_model(model, optimizer, data_loader, device, epoch, log_freq):
    model.train()
    metric_logger = misc_util.MetricLogger(delimiter='  ')
    metric_logger.add_meter('lr', misc_util.SmoothedValue(window_size=1, fmt='{value:.6f}'))
    header = 'Epoch: [{}]'.format(epoch)
    lr_scheduler = None
    if epoch == 0:
        warmup_factor = 1.0 / 1000.0
        warmup_iters = min(1000, len(data_loader) - 1)
        lr_scheduler = main_util.warmup_lr_scheduler(optimizer, warmup_iters, warmup_factor)

    for images, targets in metric_logger.log_every(data_loader, log_freq, header):
        images = list(image.to(device) for image in images)

        targets = [{k: v.to(device) for k, v in t.items()} for t in targets]
        loss_dict = model(images, targets)
        losses = sum(loss for loss in loss_dict.values())

        # reduce losses over all GPUs for logging purposes
        loss_dict_reduced = misc_util.reduce_dict(loss_dict)
        losses_reduced = sum(loss for loss in loss_dict_reduced.values())
        loss_value = losses_reduced.item()

        if not math.isfinite(loss_value):
            print('Loss is {}, stopping training'.format(loss_value))
            print(loss_dict_reduced)
            sys.exit(1)

        optimizer.zero_grad()
        losses.backward()
        optimizer.step()
        if lr_scheduler is not None:
            lr_scheduler.step()

        metric_logger.update(loss=losses_reduced, **loss_dict_reduced)
        metric_logger.update(lr=optimizer.param_groups[0]['lr'])


def train(model, train_sampler, train_data_loader, val_data_loader, device, distributed, config, args, ckpt_file_path):
    train_config = config['train']
    optim_config = train_config['optimizer']
    optimizer = func_util.get_optimizer(model, optim_config['type'], optim_config['params'])
    scheduler_config = train_config['scheduler']
    lr_scheduler = func_util.get_scheduler(optimizer, scheduler_config['type'], scheduler_config['params'])
    best_val_map = 0.0
    if file_util.check_if_exists(ckpt_file_path):
        best_val_map, _, _ = load_ckpt(ckpt_file_path, optimizer=optimizer, lr_scheduler=lr_scheduler)

    num_epochs = train_config['num_epochs']
    log_freq = train_config['log_freq']
    start_time = time.time()
    for epoch in range(num_epochs):
        if distributed:
            train_sampler.set_epoch(epoch)

        train_model(model, optimizer, train_data_loader, device, epoch, log_freq)
        lr_scheduler.step()

        # evaluate after every epoch
        coco_evaluator = main_util.evaluate(model, val_data_loader, device=device)
        # Average Precision  (AP) @[ IoU=0.50:0.95 | area=   all | maxDets=100 ]
        val_map = coco_evaluator.coco_eval['bbox'].stats[0]
        if val_map > best_val_map:
            print('Updating ckpt (Best BBox mAP: {:.4f} -> {:.4f})'.format(best_val_map, val_map))
            best_val_map = val_map
            save_ckpt(model, optimizer, lr_scheduler, best_val_map, config, args, ckpt_file_path)
        lr_scheduler.step()

    dist.barrier()
    total_time = time.time() - start_time
    total_time_str = str(datetime.timedelta(seconds=int(total_time)))
    print('Training time {}'.format(total_time_str))


def main(args):
    distributed, device_ids = main_util.init_distributed_mode(args.world_size, args.dist_url)
    config = yaml_util.load_yaml_file(args.config)
    if args.json is not None:
        main_util.overwrite_config(config, args.json)

    device = torch.device(args.device)
    print(args)

    print('Loading data')
    train_config = config['train']
    train_sampler, train_data_loader, val_data_loader, test_data_loader =\
        data_util.get_coco_data_loaders(config['dataset'], train_config['batch_size'], distributed)

    print('Creating model')
    model_config = config['model']
    model = get_model(model_config, device)
    print('Model Created')

    if distributed:
        model = nn.parallel.DistributedDataParallel(model, device_ids=device_ids)

    if args.train:
        print('Start training')
        start_time = time.time()
        train(model, train_sampler, train_data_loader, val_data_loader, device, distributed,
              config, args, model_config['ckpt'])
        total_time = time.time() - start_time
        total_time_str = str(datetime.timedelta(seconds=int(total_time)))
        print('Training time {}'.format(total_time_str))
    main_util.evaluate(model, test_data_loader, device=device)


if __name__ == '__main__':
    parser = get_argparser()
    main(parser.parse_args())
