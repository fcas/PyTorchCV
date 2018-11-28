#!/usr/bin/env python
# -*- coding:utf-8 -*-
# Author: Donny You (youansheng@gmail.com)
# Class Definition for Faster R-CNN.


from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import time
import torch
import torch.backends.cudnn as cudnn

from datasets.det_data_loader import DetDataLoader
from loss.loss_manager import LossManager
from methods.det.faster_rcnn_test import FastRCNNTest
from methods.tools.module_runner import ModuleRunner
from methods.tools.optim_scheduler import OptimScheduler
from models.det_model_manager import DetModelManager
from utils.layers.det.fr_priorbox_layer import FRPriorBoxLayer
from utils.tools.average_meter import AverageMeter
from utils.tools.logger import Logger as Log
from val.scripts.det.det_running_score import DetRunningScore
from vis.visualizer.det_visualizer import DetVisualizer


class FasterRCNN(object):
    """
      The class for Single Shot Detector. Include train, val, test & predict.
    """
    def __init__(self, configer):
        self.configer = configer
        self.batch_time = AverageMeter()
        self.data_time = AverageMeter()
        self.train_losses = AverageMeter()
        self.val_losses = AverageMeter()
        self.det_visualizer = DetVisualizer(configer)
        self.det_loss_manager = LossManager(configer)
        self.det_model_manager = DetModelManager(configer)
        self.det_data_loader = DetDataLoader(configer)
        self.fr_priorbox_layer = FRPriorBoxLayer(configer)
        self.det_running_score = DetRunningScore(configer)
        self.module_runner = ModuleRunner(configer)
        self.optim_scheduler = OptimScheduler(configer)

        self.det_net = None
        self.train_loader = None
        self.val_loader = None
        self.optimizer = None
        self.scheduler = None

        self._init_model()

    def _init_model(self):
        self.det_net = self.det_model_manager.object_detector()
        self.det_net = self.module_runner.load_net(self.det_net)

        self.optimizer, self.scheduler = self.optim_scheduler.init_optimizer(self._get_parameters())

        self.train_loader = self.det_data_loader.get_trainloader()
        self.val_loader = self.det_data_loader.get_valloader()

    def _get_parameters(self):
        lr_1 = []
        lr_2 = []
        params_dict = dict(self.det_net.named_parameters())
        for key, value in params_dict.items():
            if value.requires_grad:
                if 'bias' in key:
                    lr_2.append(value)
                else:
                    lr_1.append(value)

        params = [{'params': lr_1, 'lr': self.configer.get('lr', 'base_lr')},
                  {'params': lr_2, 'lr': self.configer.get('lr', 'base_lr') * 2., 'weight_decay': 0}]
        return params

    def __train(self):
        """
          Train function of every epoch during train phase.
        """
        self.det_net.train()
        start_time = time.time()
        # Adjust the learning rate after every epoch.
        self.configer.plus_one('epoch')
        self.scheduler.step(self.configer.get('epoch'))

        for i, data_dict in enumerate(self.train_loader):
            inputs = data_dict['img']
            img_scale = data_dict['imgscale']
            batch_gt_bboxes = self.module_runner.to_container(data_dict['bboxes'])
            batch_gt_labels = self.module_runner.to_container(data_dict['labels'])
            self.data_time.update(time.time() - start_time)
            # Change the data type.
            inputs = self.module_runner.to_device(inputs)
            # Forward pass.
            loss = self.det_net(inputs, batch_gt_bboxes, batch_gt_labels, img_scale)
            loss = loss.mean()
            self.train_losses.update(loss.item(), inputs.size(0))

            self.optimizer.zero_grad()
            loss.backward()
            self.module_runner.clip_grad(self.det_net, 10.)
            self.optimizer.step()

            # Update the vars of the train phase.
            self.batch_time.update(time.time() - start_time)
            start_time = time.time()
            self.configer.plus_one('iters')

            # Print the log info & reset the states.
            if self.configer.get('iters') % self.configer.get('solver', 'display_iter') == 0:
                Log.info('Train Epoch: {0}\tTrain Iteration: {1}\t'
                         'Time {batch_time.sum:.3f}s / {2}iters, ({batch_time.avg:.3f})\t'
                         'Data load {data_time.sum:.3f}s / {2}iters, ({data_time.avg:3f})\n'
                         'Learning rate = {3}\tLoss = {loss.val:.8f} (ave = {loss.avg:.8f})\n'.format(
                    self.configer.get('epoch'), self.configer.get('iters'),
                    self.configer.get('solver', 'display_iter'),
                    self.module_runner.get_lr(self.optimizer), batch_time=self.batch_time,
                    data_time=self.data_time, loss=self.train_losses))
                self.batch_time.reset()
                self.data_time.reset()
                self.train_losses.reset()

            # Check to val the current model.
            if self.val_loader is not None and \
               (self.configer.get('iters')) % self.configer.get('solver', 'test_interval') == 0:
                self.__val()

    def __val(self):
        """
          Validation function during the train phase.
        """
        self.det_net.eval()
        start_time = time.time()
        with torch.no_grad():
            for j, data_dict in enumerate(self.val_loader):
                inputs = data_dict['img']
                img_scale = data_dict['imgscale']
                batch_gt_bboxes = self.module_runner.to_container(data_dict['bboxes'])
                batch_gt_labels = self.module_runner.to_container(data_dict['labels'])
                # Forward pass.
                inputs = self.module_runner.to_device(inputs)
                loss, test_group = self.det_net(inputs, batch_gt_bboxes, batch_gt_labels, img_scale)
                # Compute the loss of the train batch & backward.
                loss = loss.mean()
                self.val_losses.update(loss.item(), inputs.size(0))
                test_indices_and_rois, test_roi_locs, test_roi_scores, test_rois_num = test_group
                batch_detections = FastRCNNTest.decode(test_roi_locs,
                                                       test_roi_scores,
                                                       test_indices_and_rois,
                                                       test_rois_num,
                                                       self.configer,
                                                       [inputs.size(3), inputs.size(2)])
                batch_pred_bboxes = self.__get_object_list(batch_detections)
                self.det_running_score.update(batch_pred_bboxes, batch_gt_bboxes, batch_gt_labels)

                # Update the vars of the val phase.
                self.batch_time.update(time.time() - start_time)
                start_time = time.time()

            self.module_runner.save_net(self.det_net, save_mode='iters')
            # Print the log info & reset the states.
            Log.info(
                'Test Time {batch_time.sum:.3f}s, ({batch_time.avg:.3f})\t'
                'Loss {loss.avg:.8f}\n'.format(
                    batch_time=self.batch_time, loss=self.val_losses))
            Log.info('Val mAP: {}\n'.format(self.det_running_score.get_mAP()))
            self.det_running_score.reset()
            self.batch_time.reset()
            self.val_losses.reset()
            self.det_net.train()

    def __get_object_list(self, batch_detections):
        batch_pred_bboxes = list()
        for idx, detections in enumerate(batch_detections):
            object_list = list()
            if detections is not None:
                for x1, y1, x2, y2, conf, cls_pred in detections:
                    xmin = x1.cpu().item()
                    ymin = y1.cpu().item()
                    xmax = x2.cpu().item()
                    ymax = y2.cpu().item()
                    cf = conf.cpu().item()
                    cls_pred = int(cls_pred.cpu().item()) - 1
                    object_list.append([xmin, ymin, xmax, ymax, cls_pred, float('%.2f' % cf)])

            batch_pred_bboxes.append(object_list)

        return batch_pred_bboxes

    def train(self):
        cudnn.benchmark = True
        if self.configer.get('network', 'resume') is not None and self.configer.get('network', 'resume_val'):
            self.__val()

        while self.configer.get('epoch') < self.configer.get('solver', 'max_epoch'):
            self.__train()
            if self.configer.get('epoch') == self.configer.get('solver', 'max_epoch'):
                break


if __name__ == "__main__":
    # Test class for pose estimator.
    pass
