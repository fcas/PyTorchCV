#!/usr/bin/env python
# -*- coding:utf-8 -*-
# Author: Donny You(youansheng@gmail.com)
# Loss function for Image Classification.


from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import torch
import torch.nn as nn


class FCClsLoss(nn.Module):
    def __init__(self, configer):
        super(FCClsLoss, self).__init__()
        self.configer = configer
        self.ce_loss = CrossEntropyLoss(self.configer)

    def forward(self, inputs, *targets, **kwargs):

        return self.ce_loss(inputs, targets[0])


class CrossEntropyLoss(nn.Module):
    def __init__(self, configer=None):
        super(CrossEntropyLoss, self).__init__()
        self.configer = configer
        weight = None
        if not self.configer.is_empty('cross_entropy_loss', 'weight'):
            weight = self.configer.get('cross_entropy_loss', 'weight')
            weight = torch.FloatTensor(weight).cuda()

        reduction = 'elementwise_mean'
        if not self.configer.is_empty('cross_entropy_loss', 'reduction'):
            reduction = self.configer.get("cross_entropy_loss", "reduction")

        ignore_index = -100
        if not self.configer.is_empty('cross_entropy_loss', 'ignore_index'):
            ignore_index = self.configer.get('cross_entropy_loss', 'ignore_index')

        self.ce_loss = nn.CrossEntropyLoss(weight=weight, ignore_index=ignore_index, reduction=reduction)

    def forward(self, inputs, *targets, **kwargs):
        return self.ce_loss(inputs, targets[0])


class CenterLoss(nn.Module):
    def __init__(self, dim_hidden, num_classes, lambda_c=1.0, use_cuda=True):
        super(CenterLoss, self).__init__()
        self.dim_hidden = dim_hidden
        self.num_classes = num_classes
        self.lambda_c = lambda_c
        self.centers = nn.Parameter(torch.randn(num_classes, dim_hidden))
        self.use_cuda = use_cuda

    def forward(self, y, hidden):
        batch_size = hidden.size()[0]
        expanded_centers = self.centers.index_select(dim=0, index=y)
        intra_distances = hidden.dist(expanded_centers)
        loss = (self.lambda_c / 2.0 / batch_size) * intra_distances
        return loss

    def cuda(self, device_id=None):
        """Moves all model parameters and buffers to the GPU.
        Arguments:
            device_id (int, optional): if specified, all parameters will be
                copied to that device
        """
        self.use_cuda = True
        return self._apply(lambda t: t.cuda(device_id))


class ContrastiveCenterLoss(nn.Module):
    def __init__(self, dim_hidden, num_classes, lambda_c=1.0, use_cuda=True):
        super(ContrastiveCenterLoss, self).__init__()
        self.dim_hidden = dim_hidden
        self.num_classes = num_classes
        self.lambda_c = lambda_c
        self.centers = nn.Parameter(torch.randn(num_classes, dim_hidden))
        self.use_cuda = use_cuda

    # may not work due to flowing gradient. change center calculation to exp moving avg may work.
    def forward(self, y, hidden):
        batch_size = hidden.size()[0]
        expanded_centers = self.centers.expand(batch_size, -1, -1)
        expanded_hidden = hidden.expand(self.num_classes, -1, -1).transpose(1, 0)
        distance_centers = (expanded_hidden - expanded_centers).pow(2).sum(dim=-1)
        distances_same = distance_centers.gather(1, y.unsqueeze(1))
        intra_distances = distances_same.sum()
        inter_distances = distance_centers.sum().sub(intra_distances)
        epsilon = 1e-6
        loss = (self.lambda_c / 2.0 / batch_size) * intra_distances / \
               (inter_distances + epsilon) / 0.1
        return loss

    def cuda(self, device_id=None):
        """Moves all model parameters and buffers to the GPU.
        Arguments:
            device_id (int, optional): if specified, all parameters will be
                copied to that device
        """
        self.use_cuda = True
        return self._apply(lambda t: t.cuda(device_id))
