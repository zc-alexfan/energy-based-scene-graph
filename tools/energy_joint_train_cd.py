# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT
from maskrcnn_benchmark.utils.env import setup_environment  # noqa F401 isort:skip

import argparse
import os
import time
import datetime
from timeit import default_timer as timer

import torch
from torch.nn.utils import clip_grad_norm_
import wandb

from maskrcnn_benchmark.config import cfg
from maskrcnn_benchmark.data import make_data_loader, get_dataset_statistics
from maskrcnn_benchmark.solver import make_lr_scheduler
from maskrcnn_benchmark.solver import make_optimizer
from maskrcnn_benchmark.engine.trainer import reduce_loss_dict
from maskrcnn_benchmark.engine.inference import inference, energy_inference
from maskrcnn_benchmark.modeling.detector import build_detection_model
from maskrcnn_benchmark.modeling.energy_head import build_energy_model
from maskrcnn_benchmark.modeling.energy_head import detection2graph, gt2graph
from maskrcnn_benchmark.modeling.energy_head import build_loss_function, build_sampler
from maskrcnn_benchmark.utils.checkpoint import EBMCheckpointer
from maskrcnn_benchmark.utils.checkpoint import clip_grad_norm
from maskrcnn_benchmark.utils.collect_env import collect_env_info
from maskrcnn_benchmark.utils.comm import synchronize, get_rank, all_gather
from maskrcnn_benchmark.utils.imports import import_file
from maskrcnn_benchmark.utils.logger import setup_logger, debug_print
from maskrcnn_benchmark.utils.miscellaneous import mkdir, save_config, get_mode
from maskrcnn_benchmark.utils.metric_logger import MetricLogger

# See if we can use apex.DistributedDataParallel instead of the torch default,
# and enable mixed-precision via apex.amp
try:
    from apex import amp
except ImportError:
    raise ImportError('Use APEX for multi-precision via apex.amp')

def train(cfg, local_rank, distributed, logger):

    ###################################################################################################
    #Build Models, Sampler and loss function
    debug_print(logger, 'prepare training')
    base_model = build_detection_model(cfg) 
    debug_print(logger, 'end base model construction')
    
    energy_model = build_energy_model(cfg, base_model.roi_heads.relation.box_feature_extractor.out_channels)
    debug_print(logger, 'End energy Model Constructin')

    mode = get_mode(cfg)
    sampler = build_sampler(cfg)
    loss_function = build_loss_function(cfg)
    ###################################################################################################
    ###################################################################################################

    # modules that should be always set in eval mode
    # their eval() method should be called after model.train() is called
    eval_modules = (base_model.backbone, base_model.rpn, base_model.roi_heads.box,)
    fix_eval_modules(eval_modules)

    # NOTE, we slow down the LR of the layers start with the names in slow_heads
    if cfg.MODEL.ROI_RELATION_HEAD.PREDICTOR == "IMPPredictor":
        slow_heads = ["roi_heads.relation.box_feature_extractor",
                      "roi_heads.relation.union_feature_extractor.feature_extractor",]
    else:
        slow_heads = []
    ###################################################################################################
    ###################################################################################################
    # load pretrain layers to new layers
    load_mapping = {"roi_heads.relation.box_feature_extractor" : "roi_heads.box.feature_extractor",
                    "roi_heads.relation.union_feature_extractor.feature_extractor" : "roi_heads.box.feature_extractor"}
    
    if cfg.MODEL.ATTRIBUTE_ON:
        load_mapping["roi_heads.relation.att_feature_extractor"] = "roi_heads.attribute.feature_extractor"
        load_mapping["roi_heads.relation.union_feature_extractor.att_feature_extractor"] = "roi_heads.attribute.feature_extractor"
    ###################################################################################################
    ###################################################################################################
    #GPU setup
    device = torch.device(cfg.MODEL.DEVICE)
    base_model.to(device)
    energy_model.to(device)

    num_gpus = int(os.environ["WORLD_SIZE"]) if "WORLD_SIZE" in os.environ else 1
    num_batch = cfg.SOLVER.IMS_PER_BATCH
    ###################################################################################################
    ###################################################################################################
    #Build Optimier
    base_optimizer = make_optimizer(cfg, base_model, logger, slow_heads=slow_heads, slow_ratio=10.0, rl_factor=float(num_batch))
    energy_optimizer = make_optimizer(cfg, energy_model, logger, slow_heads=[], slow_ratio=10.0, rl_factor=float(num_batch))

    #Build scheduler
    base_scheduler = make_lr_scheduler(cfg, base_optimizer, logger)
    energy_scheduler = make_lr_scheduler(cfg, energy_optimizer, logger)
    debug_print(logger, 'end optimizer and scheduler')
    ###################################################################################################
    ###################################################################################################
    #Mixed precision
    # Initialize mixed-precision training
    use_mixed_precision = cfg.DTYPE == "float16"
    amp_opt_level = 'O1' if use_mixed_precision else 'O0'
    [base_model, energy_model] , [base_optimizer, energy_optimizer] = amp.initialize([
        base_model, energy_model], [base_optimizer, energy_optimizer], opt_level=amp_opt_level, num_losses=2)

    ###################################################################################################
    ###################################################################################################
    #Distributed Setup
    if distributed:
        base_model = torch.nn.parallel.DistributedDataParallel(
            base_model, device_ids=[local_rank], output_device=local_rank,
            # this should be removed if we update BatchNorm stats
            broadcast_buffers=False,
            find_unused_parameters=True,
        )
        energy_model = torch.nn.parallel.DistributedDataParallel(
            energy_model, device_ids=[local_rank], output_device=local_rank,
            # this should be removed if we update BatchNorm stats
            broadcast_buffers=False,
            find_unused_parameters=True,
        )
        base_model_module = base_model.module
    else:
        base_model_module = base_model

    debug_print(logger, 'end distributed')
    ###################################################################################################
    ###################################################################################################
    #Checkpoint
    arguments = {}
    arguments["iteration"] = 0

    output_dir = cfg.OUTPUT_DIR

    save_to_disk = get_rank() == 0

    checkpointer = EBMCheckpointer(
        cfg=cfg, base_model=base_model, energy_model=energy_model, 
        base_optimizer=None, energy_optimizer=energy_optimizer, 
        base_scheduler=None, energy_scheduler=energy_scheduler, 
        save_dir=output_dir, save_to_disk=save_to_disk, custom_scheduler=True
    )

    if checkpointer.has_checkpoint():
        extra_checkpoint_data = checkpointer.load(cfg.MODEL.PRETRAINED_DETECTOR_CKPT, 
                                       update_schedule=cfg.SOLVER.UPDATE_SCHEDULE_DURING_LOAD)
        arguments.update(extra_checkpoint_data)
    else:
        #Load the detector
        checkpointer.load(cfg.MODEL.PRETRAINED_DETECTOR_CKPT, with_optim=False, load_mapping=load_mapping, only_base=True)
    
    checkpoint_period = cfg.SOLVER.CHECKPOINT_PERIOD
    debug_print(logger, 'end load checkpointer')
    ###################################################################################################
    ###################################################################################################
    #DataLoader
    train_data_loader = make_data_loader(
        cfg,
        mode='train',
        is_distributed=distributed,
        start_iter=arguments["iteration"],
    )
    val_data_loaders = make_data_loader(
        cfg,
        mode='val',
        is_distributed=distributed,
    )
    debug_print(logger, 'end dataloader')
    
    ###################################################################################################
    ###################################################################################################
    if cfg.SOLVER.PRE_VAL:
        logger.info("Validate base model before training")
        # run_val(cfg, base_model, val_data_loaders, distributed, logger)
        run_energy_val(cfg, base_model, energy_model, sampler, val_data_loaders, distributed, logger)
    ###################################################################################################
    ###################################################################################################
    logger.info("Start training")
    meters = MetricLogger(delimiter="  ")
    max_iter = len(train_data_loader)
    start_iter = arguments["iteration"]
    start_training_time = time.time()
    end = time.time()

    print_first_grad = True
    
    for iteration, (images, targets, _) in enumerate(train_data_loader, start_iter):

        if any(len(target) < 1 for target in targets):
            logger.error(f"Iteration={iteration + 1} || Image Ids used for training {_} || targets Length={[len(target) for target in targets]}" )
        data_time = time.time() - end
        iteration = iteration + 1
        arguments["iteration"] = iteration
        ########################################################################
        #Train Mode
        base_model.train()
        fix_eval_modules(eval_modules)
        energy_model.train()
        ########################################################################
        ########################################################################
        #FOrward
        images = images.to(device)
        targets = [target.to(device) for target in targets]

        task_loss_dict, detections, roi_features = base_model(images,targets)
        
        if mode != 'sgdet':
            gt_node_states = roi_features
            pred_node_states = roi_features
        else:
            gt_node_states = None
            pred_node_states = roi_features

        gt_im_graph, gt_scene_graph, gt_bbox = gt2graph(gt_node_states, images, targets, base_model_module, 
                                                        cfg.DATASETS.NUM_OBJ_CLASSES, cfg.DATASETS.NUM_REL_CLASSES, 
                                                        cfg.ENERGY_MODEL.DATA_NOISE_VAR)

        pred_im_graph, pred_scene_graph, pred_bbox = detection2graph(pred_node_states, images, detections, base_model_module, 
                                                                    cfg.DATASETS.NUM_OBJ_CLASSES, mode, 
                                                                    cfg.ENERGY_MODEL.DATA_NOISE_VAR)
        # end_time = timer()
        # print("pred-graph time {}".format(end_time - start_time))
        
        #MCMC Step for Contrastive Loss
        # start_time = timer()
        pred_scene_graph = sampler.sample(energy_model, pred_im_graph, pred_scene_graph, pred_bbox.detach(), mode, set_grad=False)
        # end_time = timer()
        # print("sampler time {}".format(end_time - start_time))
        
        ########################################################################
        ########################################################################
        #Loss Computation
        positive_energy = energy_model(gt_im_graph, gt_scene_graph, gt_bbox)
        negative_energy = energy_model(pred_im_graph, pred_scene_graph, pred_bbox)

        energy_loss_dict = loss_function(cfg, positive_energy, negative_energy)
        #If the iteration is training ebm then only use ebm loss else add ebm loss to the task loss

        task_losses = sum(loss for loss in task_loss_dict.values())
        energy_losses = sum(loss for loss in energy_loss_dict.values())
        total_losses = task_losses + energy_losses
        loss_dict = {**task_loss_dict, **energy_loss_dict}
        
        if get_rank() == 0:
            log_dict = {k: v.item() for k, v in loss_dict.items()}
            wandb.log(log_dict)
            wandb.log({'Positive Energy': positive_energy.mean().item(), 'Negative Energy': negative_energy.mean().item()})
        
        # reduce losses over all GPUs for logging purposes
        loss_dict_reduced = reduce_loss_dict(loss_dict)
        losses_reduced = sum(loss for loss in loss_dict_reduced.values())
        meters.update(loss=losses_reduced, **loss_dict_reduced)
        # print("Loss >> {}".format(loss_dict))
        ########################################################################
        ########################################################################
        #Optimizer Step
        base_optimizer.zero_grad()
        energy_optimizer.zero_grad()

        # Note: If mixed precision is not used, this ends up doing nothing
        # Otherwise apply loss scaling for mixed-precision recipe
        # start_time = timer()
        with amp.scale_loss(total_losses, [base_optimizer, energy_optimizer]) as scaled_task_losses:
            scaled_task_losses.backward()
        # end_time = timer()
        # print("loss backward time {}".format(end_time - start_time))
        
        # with amp.scale_loss(task_losses, base_optimizer, loss_id=1) as scaled_task_losses:
        #     scaled_task_losses.backward()
        # base_optimizer.step()

        # with amp.scale_loss(energy_losses, energy_optimizer, loss_id=0) as scaled_energy_losses:
        #     scaled_energy_losses.backward()
        # energy_optimizer.step()
         # add clip_grad_norm from MOTIFS, tracking gradient, used for debug
        verbose = (iteration % cfg.SOLVER.PRINT_GRAD_FREQ) == 0 or print_first_grad # print grad or not
        print_first_grad = False
        clip_grad_norm([(n, p) for n, p in energy_model.named_parameters() if p.requires_grad], max_norm=cfg.SOLVER.GRAD_NORM_CLIP, logger=logger, verbose=verbose, clip=True)
        clip_grad_norm([(n, p) for n, p in base_model.named_parameters() if p.requires_grad], max_norm=cfg.SOLVER.GRAD_NORM_CLIP, logger=logger, verbose=verbose, clip=True)

        # start_time = timer()
        base_optimizer.step()
        energy_optimizer.step()
        
        ########################################################################
        ########################################################################
        batch_time = time.time() - end
        end = time.time()
        meters.update(time=batch_time, data=data_time)
        # import ipdb; ipdb.set_trace()
        eta_seconds = meters.time.global_avg * (max_iter - iteration)
        eta_string = str(datetime.timedelta(seconds=int(eta_seconds)))

        if iteration % 200 == 0 or iteration == max_iter:
            logger.info(
                meters.delimiter.join(
                    [
                        "eta: {eta}",
                        "iter: {iter}",
                        "{meters}",
                        "energy lr: {lr:.6f}",
                        "base lr : {lrb:.6f}",
                        "max mem: {memory:.0f}",
                    ]
                ).format(
                    eta=eta_string,
                    iter=iteration,
                    meters=str(meters),
                    lr=energy_optimizer.param_groups[-1]["lr"],
                    lrb=base_optimizer.param_groups[-1]["lr"],
                    memory=torch.cuda.max_memory_allocated() / 1024.0 / 1024.0,
                )
            )

        if iteration % checkpoint_period == 0:
            checkpointer.save("model_{:07d}".format(iteration), **arguments)
        if iteration == max_iter:
            checkpointer.save("model_final", **arguments)

        val_result = None # used for scheduler updating
        if cfg.SOLVER.TO_VAL and iteration % cfg.SOLVER.VAL_PERIOD == 0:
            logger.info("Start validating")
            val_result = run_energy_val(cfg, base_model, energy_model, sampler, val_data_loaders,
                                        distributed, logger)

            logger.info("Validation Result: %.4f" % val_result)
        ########################################################################
        ########################################################################

        # scheduler should be called after optimizer.step() in pytorch>=1.1.0
        # https://pytorch.org/docs/stable/optim.html#how-to-adjust-learning-rate
        if cfg.SOLVER.SCHEDULE.TYPE == "WarmupReduceLROnPlateau":
            
            energy_scheduler.step(val_result, epoch=iteration)
            base_scheduler.step(val_result, epoch=iteration)
            

            if energy_scheduler.stage_count >= cfg.SOLVER.SCHEDULE.MAX_DECAY_STEP or base_scheduler.stage_count >= cfg.SOLVER.SCHEDULE.MAX_DECAY_STEP :
                logger.info("Trigger MAX_DECAY_STEP at iteration {}.".format(iteration))
                break
        else:
            energy_scheduler.step()
            base_scheduler.step()

        if cfg.MODEL.DEV_RUN and iteration == 10:
            break
    
    total_training_time = time.time() - start_training_time
    total_time_str = str(datetime.timedelta(seconds=total_training_time))
    logger.info(
        "Total training time: {} ({:.4f} s / it)".format(
            total_time_str, total_training_time / (max_iter)
        )
    )
    return base_model, energy_model, sampler

def fix_eval_modules(eval_modules):
    for module in eval_modules:
        for _, param in module.named_parameters():
            param.requires_grad = False
        # DO NOT use module.eval(), otherwise the module will be in the test mode, i.e., all self.training condition is set to False

def run_energy_val(cfg, base_model, energy_model, sampler, val_data_loaders, distributed, logger):
    if distributed:
        base_model = base_model.module
        energy_model = energy_model.module

    torch.cuda.empty_cache()
    iou_types = ("bbox",)
    if cfg.MODEL.MASK_ON:
        iou_types = iou_types + ("segm",)
    if cfg.MODEL.KEYPOINT_ON:
        iou_types = iou_types + ("keypoints",)
    if cfg.MODEL.RELATION_ON:
        iou_types = iou_types + ("relations", )
    if cfg.MODEL.ATTRIBUTE_ON:
        iou_types = iou_types + ("attributes", )

    dataset_names = cfg.DATASETS.VAL
    val_result = []
    for dataset_name, val_data_loader in zip(dataset_names, val_data_loaders):
        
        # dataset_result = energy_inference(
        #                     cfg,
        #                     base_model,
        #                     energy_model,
        #                     sampler,
        #                     val_data_loader,
        #                     dataset_name=dataset_name,
        #                     with_sample=True,
        #                     iou_types=iou_types,
        #                     box_only=False if cfg.MODEL.RETINANET_ON else cfg.MODEL.RPN_ONLY,
        #                     device=cfg.MODEL.DEVICE,
        #                     expected_results=cfg.TEST.EXPECTED_RESULTS,
        #                     expected_results_sigma_tol=cfg.TEST.EXPECTED_RESULTS_SIGMA_TOL,
        #                     output_folder=None,
        #                     logger=logger,
        #                 )
        dataset_result = energy_inference(
                        cfg,
                        base_model,
                        energy_model,
                        sampler,
                        val_data_loader,
                        dataset_name=dataset_name,
                        with_sample=False,
                        iou_types=iou_types,
                        box_only=False if cfg.MODEL.RETINANET_ON else cfg.MODEL.RPN_ONLY,
                        device=cfg.MODEL.DEVICE,
                        expected_results=cfg.TEST.EXPECTED_RESULTS,
                        expected_results_sigma_tol=cfg.TEST.EXPECTED_RESULTS_SIGMA_TOL,
                        output_folder=None,
                        logger=logger,
                    )
        synchronize()
        val_result.append(dataset_result)
    # support for multi gpu distributed testing
    gathered_result = all_gather(torch.tensor(dataset_result).cpu())
    gathered_result = [t.view(-1) for t in gathered_result]
    gathered_result = torch.cat(gathered_result, dim=-1).view(-1)
    valid_result = gathered_result[gathered_result>=0]
    val_result = float(valid_result.mean())
    del gathered_result, valid_result
    torch.cuda.empty_cache()
    return val_result

def run_test(cfg, base_model, energy_model, sampler, distributed, logger):
    
    if distributed:
        base_model = base_model.module
        energy_model = energy_model.module

    torch.cuda.empty_cache()
    iou_types = ("bbox",)
    if cfg.MODEL.MASK_ON:
        iou_types = iou_types + ("segm",)
    if cfg.MODEL.KEYPOINT_ON:
        iou_types = iou_types + ("keypoints",)
    if cfg.MODEL.RELATION_ON:
        iou_types = iou_types + ("relations", )
    if cfg.MODEL.ATTRIBUTE_ON:
        iou_types = iou_types + ("attributes", )
    
    output_folders = [None] * len(cfg.DATASETS.TEST)
    dataset_names = cfg.DATASETS.TEST
    
    if cfg.OUTPUT_DIR:
        for idx, dataset_name in enumerate(dataset_names):
            output_folder = os.path.join(cfg.OUTPUT_DIR, "inference", dataset_name)
            mkdir(output_folder)
            output_folders[idx] = output_folder
    
    data_loaders_val = make_data_loader(cfg, mode='test', is_distributed=distributed)
    
    for output_folder, dataset_name, data_loader_val in zip(output_folders, dataset_names, data_loaders_val):
        # logger.info(">>>>>>>>>>Testing with Sampling")
        # energy_inference(
        #     cfg,
        #     base_model,
        #     energy_model,
        #     sampler,
        #     data_loader_val,
        #     dataset_name=dataset_name,
        #     with_sample = True,
        #     iou_types=iou_types,
        #     box_only=False if cfg.MODEL.RETINANET_ON else cfg.MODEL.RPN_ONLY,
        #     device=cfg.MODEL.DEVICE,
        #     expected_results=cfg.TEST.EXPECTED_RESULTS,
        #     expected_results_sigma_tol=cfg.TEST.EXPECTED_RESULTS_SIGMA_TOL,
        #     output_folder=output_folder,
        #     logger=logger,
        #     # is_distributed=distributed
        # )
        logger.info(">>>>>>>>>>Testing without Sampling")
        energy_inference(
            cfg,
            base_model,
            energy_model,
            sampler,
            data_loader_val,
            dataset_name=dataset_name,
            with_sample = False,
            iou_types=iou_types,
            box_only=False if cfg.MODEL.RETINANET_ON else cfg.MODEL.RPN_ONLY,
            device=cfg.MODEL.DEVICE,
            expected_results=cfg.TEST.EXPECTED_RESULTS,
            expected_results_sigma_tol=cfg.TEST.EXPECTED_RESULTS_SIGMA_TOL,
            output_folder=output_folder,
            logger=logger,
            # is_distributed=distributed
        )
        synchronize()
def main():
    ###################################################################################################
    parser = argparse.ArgumentParser(description="PyTorch Relation Detection Training")
    parser.add_argument(
        "--config-file",
        default="",
        metavar="FILE",
        help="path to config file",
        type=str,
    )
    parser.add_argument("--local_rank", type=int, default=0)
    parser.add_argument(
        "--skip-test",
        dest="skip_test",
        help="Do not test the final model",
        action="store_true",
    )
    parser.add_argument(
        "opts",
        help="Modify config options using the command-line",
        default=None,
        nargs=argparse.REMAINDER,
    )
    parser.add_argument(
            "--slurm_id", dest="slurm_id", type=int, help="slurm job id")
    args = parser.parse_args()
    ###################################################################################################
    ###################################################################################################
    #Distributed Setup
    num_gpus = int(os.environ["WORLD_SIZE"]) if "WORLD_SIZE" in os.environ else 1
    args.distributed = num_gpus > 1

    if args.distributed:
        torch.cuda.set_device(args.local_rank)
        torch.distributed.init_process_group(
            backend="nccl", init_method="env://"
        )
        synchronize()
    ###################################################################################################
    ###################################################################################################
    cfg.merge_from_file(args.config_file)
    cfg.merge_from_list(args.opts)

    output_dir = cfg.OUTPUT_DIR
    if output_dir:
        mkdir(output_dir)
        
    cfg.DATASETS.NUM_OBJ_CLASSES = cfg.MODEL.ROI_BOX_HEAD.NUM_CLASSES
    cfg.DATASETS.NUM_REL_CLASSES = cfg.MODEL.ROI_RELATION_HEAD.NUM_CLASSES

    cfg.freeze()
    ###################################################################################################
    ###################################################################################################
    #Wandb Setup
    if get_rank() == 0:
        if cfg.MODEL.DEV_RUN or cfg.WANDB.MUTE:
            os.environ['WANDB_MODE'] = 'dryrun'

        wandb.init(project="sgebm", id=str(args.slurm_id))
    ###################################################################################################
    ###################################################################################################
    logger = setup_logger("maskrcnn_benchmark", output_dir, get_rank())
    logger.info("Using {} GPUs".format(num_gpus))
    logger.info(args)

    logger.info("Collecting env info (might take some time)")
    logger.info("\n" + collect_env_info())

    logger.info("Loaded configuration file {}".format(args.config_file))
    with open(args.config_file, "r") as cf:
        config_str = "\n" + cf.read()
        logger.info(config_str)
    logger.info("Running with config:\n{}".format(cfg))

    output_config_path = os.path.join(cfg.OUTPUT_DIR, 'config.yml')
    logger.info("Saving config into: {}".format(output_config_path))
    # save overloaded model config in the output directory
    save_config(cfg, output_config_path)
    ###################################################################################################

    base_model, energy_model, sampler = train(cfg, args.local_rank, args.distributed, logger)
    
    if not args.skip_test:
        run_test(cfg, base_model, energy_model, sampler, args.distributed, logger)


if __name__ == "__main__":
    main()
