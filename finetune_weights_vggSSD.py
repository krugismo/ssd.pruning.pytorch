'''
    Use absolute weights-based criterion for filter pruning on vggSSD
    Execute: python3 finetune_weights_vggSSD.py --prune --trained_model weights/_your_trained_model_.pth
'''
import torch
from torch.autograd import Variable
#from torchvision import models
import cv2
import sys
import numpy as np
import torchvision
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
#import dataset
from pruning.prune_vgg import *
import argparse
from operator import itemgetter
from heapq import nsmallest #heap queue algorithm
import time

from data import *
import torch.utils.data as data
from utils.augmentations import SSDAugmentation
from layers.modules import MultiBoxLoss
from models.SSD_vggres import build_ssd

def str2bool(v):
    return v.lower() in ("yes", "true", "t", "1")

parser = argparse.ArgumentParser()
parser.add_argument("--train", dest="train", action="store_true")
parser.add_argument("--prune", dest="prune", action="store_true")
parser.add_argument("--prune_folder", default = "prunes/")
parser.add_argument("--trained_model", default = "prunes/vggSSD_trained.pth")
parser.add_argument('--dataset_root', default=VOC_ROOT)
parser.add_argument('--cuda', default=True, type=str2bool, help='Use cuda to train model')
parser.set_defaults(train=False)
parser.set_defaults(prune=False)
args = parser.parse_args()

dataset_mean = (104, 117, 123)
cfg = voc

# only prune base net
# store the functions for ranking and deciding which filters to be pruned
class FilterPrunner:
    def __init__(self, model):
        self.model = model
        self.reset()

    def reset(self):
        # self.activations = []
        # self.gradients = []
        # self.grad_index = 0
        # self.activation_to_layer = {}
        self.filter_ranks = {}
    '''
    Do one-time forward propagation, prepare useful info. for
    1. compute_rank()
    2. backprop
    3. training and param update
    @return: output of forward
    '''
    def rank(self):
        self.weights = [] # store the absolute weights for filter
        self.weight_to_layer = {} # dict matching weight index to layer index

        weight_index = 0
        # the layer excluded from pruning due to existence of forking
        fork_indices = [21, 33] # len(self.model.base)-1] = 34 ReLU
        # layer: index number, (name, module): item in _modules
        # _modules is an embedded attribute in Module class, with type of OrderDict(), name is key, module is content
        for layer, (name, module) in enumerate(self.model.base._modules.items()):
            if isinstance(module, torch.nn.modules.conv.Conv2d) and (layer not in fork_indices):

                if module.weight.data.size(0) <= 1:
                    continue # skip the layer with only one filter left

                abs_wgt = torch.abs(module.weight.data) # batch_size x out_channels x 3 x 3?
                self.weights.append(abs_wgt)
                self.weight_to_layer[weight_index] = layer

                # compute the rank and store into self.filter_ranks
                # size(1) represents the num of filter/individual feature map
                values = \
                    torch.sum(abs_wgt, dim = 1, keepdim = True).\
                        sum(dim=2, keepdim = True).sum(dim=3, keepdim = True)[:, 0, 0, 0]#.data

                # Normalize the sum of weight by the batch_size
                values = values / (abs_wgt.size(1) * abs_wgt.size(2) * abs_wgt.size(3)) # (filter_number for this layer, 1)

                if weight_index not in self.filter_ranks:
                    self.filter_ranks[weight_index] = \
                        torch.FloatTensor(abs_wgt.size(0)).zero_().cuda()

                self.filter_ranks[weight_index] += values # filter_ranks are 0 initially, size = (num_filter, 1)

                weight_index += 1

        return True # output

    def lowest_ranking_filters(self, num):
        data = []
        for i in sorted(self.filter_ranks.keys()): # for one layer
            for j in range(self.filter_ranks[i].size(0)): # num_filter for this layer
                data.append((self.weight_to_layer[i], j, self.filter_ranks[i][j]))

        return nsmallest(num, data, itemgetter(2)) # (l, f, _)

    def get_prunning_plan(self, num_filters_to_prune):
        filters_to_prune = self.lowest_ranking_filters(num_filters_to_prune)

        filters_to_prune_per_layer = {}
        for (l, f, _) in filters_to_prune:
            if l not in filters_to_prune_per_layer:
                filters_to_prune_per_layer[l] = []
            filters_to_prune_per_layer[l].append(f)

        for l in filters_to_prune_per_layer:
            filters_to_prune_per_layer[l] = sorted(filters_to_prune_per_layer[l])
            # After each of the k filters are prunned,
            # the filter index of the next filters change since the model is smaller.
            for i in range(len(filters_to_prune_per_layer[l])):
                filters_to_prune_per_layer[l][i] = filters_to_prune_per_layer[l][i] - i

        filters_to_prune = []
        for l in filters_to_prune_per_layer:
            for i in filters_to_prune_per_layer[l]:
                filters_to_prune.append((l, i))

        return filters_to_prune

class PrunningFineTuner_vggSSD:
    def __init__(self, train_loader, testset, criterion, model):
        self.train_data_loader = train_loader
        self.testset = testset

        self.model = model
        self.criterion = criterion
        self.prunner = FilterPrunner(self.model)
        self.model.train()

    def rank(self):
        self.prunner.rank()

    def test(self):
        self.model.eval()
        # evaluation
        # test_net('prunes/vggssd_test', self.model, detector, priors, args.cuda, testset,
        #        BaseTransform(self.model.size, cfg['dataset_mean']),
        #        300, thresh=0.01)

        self.model.train()

    # epoches: fine tuning for this epoches
    def train(self, optimizer = None, epoches = 5):
        if optimizer is None:
            optimizer = \
                optim.SGD(self.model.parameters(),
                    lr=0.0001, momentum=0.9, weight_decay=5e-4)

        for i in range(epoches):
            print("FineTune... Epoch: ", i)
            self.train_epoch(optimizer) # no need for rank_filters
            self.test()
        print("Finished fine tuning.")

    # batch: images, label: targets
    def train_batch(self, optimizer, batch, label):
        # set gradients of all model parameters to zero
        self.model.zero_grad() # same as optimizer.zero_grad() when SGD() get model.parameters
        # input = Variable(batch)
        input = batch

        loss_l, loss_c = self.criterion(self.model(input), label)
        loss = loss_l + loss_c
        loss.backward()
        optimizer.step() # update params

    # train for one epoch, so the data_loader will not pop StopIteration error
    def train_epoch(self, optimizer = None):
        for batch, label in self.train_data_loader:
            batch = Variable(batch.cuda())
            label = [Variable(ann.cuda(), volatile=True) for ann in label]
            self.train_batch(optimizer, batch, label)

    def get_candidates_to_prune(self, num_filters_to_prune):
        self.prunner.reset()
        self.rank()
        # self.train_epoch(rank_filters = True) # need to rank_filters by backprop

        # self.prunner.normalize_ranks_per_layer()

        return self.prunner.get_prunning_plan(num_filters_to_prune)

    def total_num_filters(self):
        filters = 0
        fork_indices = [21, 33]# len(self.model.base)-1]
        for layer, (name, module) in enumerate(self.model.base._modules.items()):
            if isinstance(module, torch.nn.modules.conv.Conv2d) and (layer not in fork_indices):
                filters = filters + module.out_channels
        return filters

    def prune(self):
        #Get the accuracy before prunning
        self.test()

        self.model.train()

        #Make sure all the layers are trainable
        for param in self.model.base.parameters():
            param.requires_grad = True

        number_of_filters = self.total_num_filters()
        num_filters_to_prune_per_iteration = 512
        iterations = int(float(number_of_filters) / num_filters_to_prune_per_iteration) # the total iterations for cutting 100% all

        #iterations = int(iterations * 2.0 / 3)
        iterations = int(iterations * 2.0 / 10)

        #print "Number of prunning iterations to reduce 67% filters", iterations
        print("Number of prunning iterations to reduce 20% filters", iterations)

        for iteration in range(iterations):
            print("Ranking filters.. ")
            prune_targets = self.get_candidates_to_prune(num_filters_to_prune_per_iteration)
            layers_prunned = {}
            for layer_index, filter_index in prune_targets:
                if layer_index not in layers_prunned:
                    layers_prunned[layer_index] = 0
                layers_prunned[layer_index] = layers_prunned[layer_index] + 1

            print("Layers that will be prunned", layers_prunned)
            print("Prunning filters.. ")
            model = self.model.cpu()
            for layer_index, filter_index in prune_targets:
                model = prune_vggbase_conv_layer(model, layer_index, filter_index)

            self.model = model.cuda()

            message = str(100*float(self.total_num_filters()) / number_of_filters) + "%"
            print("Filters prunned", str(message))
            self.test()
            print("Fine tuning to recover from prunning iteration.")

            optimizer = optim.SGD(self.model.parameters(), lr=0.001, momentum=0.9)

            self.train(optimizer, epoches = 10)


        print("Finished. Going to fine tune the model a bit more")
        self.train(optimizer, epoches = 15)
        print('Saving pruned model...')
        torch.save(self.model, 'prunes/vggSSD_prunned.pth')
        #torch.save(model, "model_prunned")

if __name__ == '__main__':
    if not args.cuda:
        print("this file only supports cuda version now!")

    # store pruning models
    if not os.path.exists(args.prune_folder):
        os.mkdir(args.prune_folder)

    if args.train:
        model = build_ssd('train', cfg['min_dim'], cfg['num_classes'], base='vgg').cuda()
    elif args.prune:
        #model = torch.load("model").cuda()
        model = build_ssd('train', cfg['min_dim'], cfg['num_classes'], base='vgg').cuda()
        state_dict = torch.load(args.trained_model)
        from collections import OrderedDict
        new_state_dict = OrderedDict()
        for k, v in state_dict.items():
            head = k[:7]
            if head == 'module.':
                name = k[7:] # remove `module.` because you store the model without DataParallel
            else:
                name = k
            new_state_dict[name] = v
        model.load_state_dict(new_state_dict)
        #model.load_state_dict(torch.load(args.trained_model))

    dataset = VOCDetection(root=args.dataset_root,
                           transform=SSDAugmentation(cfg['min_dim'],
                                                     cfg['dataset_mean']))
    testset = VOCDetection(root=args.dataset_root, image_sets=[('2007', 'test')],
                                transform=BaseTransform(cfg['min_dim'], cfg['testset_mean']))
    data_loader = data.DataLoader(dataset, 32, num_workers=4,
                                  shuffle=True, collate_fn=detection_collate,
                                  pin_memory=True)

    criterion = MultiBoxLoss(cfg['num_classes'], 0.5, True, 0, True, 3, 0.5, False, args.cuda)

    fine_tuner = PrunningFineTuner_vggSSD(data_loader, testset, criterion, model)

    if args.train:
        fine_tuner.train(epoches = 20)
        print('Saving trained model...')
        torch.save(self.model.state_dict(), 'prunes/vgg_trained.pth')
        #torch.save(model, "model")

    elif args.prune:
        fine_tuner.prune()
