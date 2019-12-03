import torch
import torch.nn as nn
import torch.optim as optim
from utils.options import args
import utils.common as utils

import os
import time
from data import cifar10, imagenet
from importlib import import_module

device = torch.device(f"cuda:{args.gpus[0]}") if torch.cuda.is_available() else 'cpu'
checkpoint = utils.checkpoint(args)
logger = utils.get_logger(os.path.join(args.job_dir + 'logger.log'))
loss_func = nn.CrossEntropyLoss()

# Data
print('==> Preparing data..')
if args.data_set == 'cifar10':
    loader = cifar10.Data(args)
elif args.data_set == 'imagenet':
    loader = imagenet.Data(args)

def weight_norm(weight, weight_norm_method=None, filter_norm=False):

    if weight_norm_method == 'max':
        norm_func = lambda x: torch.max(torch.abs(x))
    elif weight_norm_method == 'sum':
        norm_func = lambda x: torch.sum(torch.abs(weight))
    elif weight_norm_method == 'l2':
        norm_func = lambda x: torch.sqrt(torch.sum(x.pow(2)))
    elif weight_norm_method == 'l1':
        norm_func = lambda x: torch.sqrt(torch.sum(torch.abs(x)))
    elif weight_norm_method == 'l2_2':
        norm_func = lambda x: torch.sum(weight.pow(2))
    elif weight_norm_method == '2max':
        norm_func = lambda x: (2 * torch.max(torch.abs(x)))
    else:
        norm_func = lambda x: x

    if filter_norm:
        for i in range(weight.size(0)):
            weight[i] /= norm_func(weight[i])
    else:
        weight /= norm_func(weight)

    return weight

def sketch_matrix(weight, l, dim,
                  bn_weight, bn_bias=None, sketch_bn=False,
                  weight_norm_method=None, filter_norm=False):
    # if l % 2 != 0:
    #     raise ('l should be an even number...')
    A = weight.clone()
    if weight.dim() == 4:  #Convolution layer
        A = A.view(A.size(dim), -1)
        if sketch_bn:
            bn_weight = bn_weight.view(bn_weight.size(0), -1)
            A = torch.cat((A, bn_weight), 1)
            bn_bias = bn_bias.view(bn_bias.size(0), -1)
            A = torch.cat((A, bn_bias), 1)

    B = torch.zeros(l, A.size(1))
    ind = int(l / 2)
    [n, _] = A.size()  # n: number of samples m: dimension
    numNonzeroRows = torch.nonzero(torch.sum(B.mul(B), 1) > 0).size(0) # number of non - zero rows

    for i in range(n):
        if numNonzeroRows < l:
            B[numNonzeroRows, :] = A[i, :]
        else:

            if n - i < l // 2:
                break

            u, sigma, _ = torch.svd(B.t())

            sigmaSquare = sigma.mul(sigma)
            sigmaSquareDiag = torch.diag(sigmaSquare)
            theta = sigmaSquareDiag[ind]
            sigmaSquare = sigmaSquare - torch.eye(l) * theta
            sigmaHat = torch.sqrt(torch.where(sigmaSquare > 0,
                                              sigmaSquare, torch.zeros(sigmaSquare.size())))
            B = sigmaHat.mm(u.t())

            numNonzeroRows = ind
            B[numNonzeroRows, :] = A[i, :]

        numNonzeroRows = numNonzeroRows + 1

    if dim == 0:
        if sketch_bn:
            split_size = weight.size(1) * weight.size(2) * weight.size(3)
            B, bn_para = torch.split(B, split_size, dim=1)
            return weight_norm(B.view(l, weight.size(1), weight.size(2), weight.size(3)), weight_norm_method, filter_norm), \
                   torch.unsqueeze(bn_para[:, 0], 0).view(-1), \
                   torch.unsqueeze(bn_para[:, 1], 0).view(-1),
        else:
            return weight_norm(B.view(l, weight.size(1), weight.size(2), weight.size(3)), weight_norm_method, filter_norm)
    elif dim == 1:
        return weight_norm(B.view(weight.size(0), l, weight.size(2), weight.size(3)), weight_norm_method, filter_norm)

def load_vgg_sketch_model(model):

    if args.sketch_model is None or not os.path.exists(args.sketch_model):
        raise ('Sketch model path should be exist!')
    ckpt = torch.load(args.sketch_model, map_location=device)
    origin_model = import_module(f'model.{args.arch}').VGG().to(device)
    origin_model.load_state_dict(ckpt['state_dict'])
    logger.info('==>Before Sketch')
    test(origin_model, loader.testLoader)
    oristate_dict = origin_model.state_dict()

    state_dict = model.state_dict()
    is_preserve = False
    for name, module in origin_model.named_modules():
        if isinstance(module, nn.Conv2d):
            oriweight = module.weight.data
            layer = int(name.split('.')[1]) + 1  # the index of BN in state_dict
            l = int(oriweight.size(0) * args.sketch_rate)

            if not args.sketch_lastconv and name == 'features.40':
                sketch_channel = sketch_matrix(oriweight, l, dim=1, bn_weight=None, sketch_bn=False,
                                               weight_norm_method=args.weight_norm_method,
                                               filter_norm=args.filter_norm)
                state_dict[name + '.weight'] = sketch_channel
                continue

            if l < oriweight.size(1) * oriweight.size(2) * oriweight.size(3):
                if args.sketch_bn:
                    sketch_filter, state_dict['features.' + str(layer) + '.weight'], \
                        state_dict['features.' + str(layer) + '.bias'] = sketch_matrix(oriweight, l, dim=0,
                                                       bn_weight=oristate_dict['features.' + str(layer) + '.weight'],
                                                       bn_bias=oristate_dict['features.' + str(layer) + '.bias'], sketch_bn=True)
                else:
                    sketch_filter = sketch_matrix(oriweight, l, dim=0,
                                                       bn_weight=oristate_dict['features.' + str(layer) + '.weight'],
                                                       bn_bias=oristate_dict['features.' + str(layer) + '.bias'], sketch_bn=False,
                                                        weight_norm_method=args.weight_norm_method,
                                                        filter_norm=args.filter_norm)
                if is_preserve: #If the previous layer is reserved, there is no need to sketch the channel
                    state_dict[name + '.weight'] = sketch_filter
                else:
                    l = int(oriweight.size(1) * args.sketch_rate)
                    sketch_channel = sketch_matrix(sketch_filter, l, dim=1, bn_weight=None, sketch_bn=False,
                                                   weight_norm_method=args.weight_norm_method,
                                                   filter_norm=args.filter_norm)
                    state_dict[name + '.weight'] = sketch_channel
                is_preserve = False
            else:
                state_dict[name + '.weight'] = oriweight
                is_preserve = True
        elif isinstance(module, nn.Linear) and not args.sketch_lastconv:

            state_dict[name + '.weight'] = module.weight.data
            state_dict[name + '.bias'] = module.bias.data

    model.load_state_dict(state_dict)
    logger.info('==>After Sketch')
    test(model, loader.testLoader)

def load_resnet_sketch_model(model):
    cfg = {'resnet56': [9, 9, 9],
           'resnet110': [18, 18, 18],
           }

    if args.sketch_model is None or not os.path.exists(args.sketch_model):
        raise ('Sketch model path should be exist!')
    ckpt = torch.load(args.sketch_model, map_location=device)
    origin_model = import_module(f'model.{args.arch}').resnet(args.cfg).to(device)
    origin_model.load_state_dict(ckpt['state_dict'])
    logger.info('==>Before Sketch')
    test(origin_model, loader.testLoader)

    oristate_dict = origin_model.state_dict()

    state_dict = model.state_dict()
    is_preserve = False #Whether the previous layer retains the original weight dimension, no sketch

    current_cfg = cfg[args.cfg]

    all_sketch_conv_weight = []
    all_sketch_bn_weight = []

    for layer, num in enumerate(current_cfg):
        layer_name = 'layer' + str(layer + 1) + '.'
        for i in range(num):
            for j in range(2):
                #Block the first convolution layer, only sketching the first dimension
                #Block the last convolution layer, only Skitch on the channel dimension
                conv_name = layer_name + str(i) + '.conv' + str(j + 1)
                conv_weight_name = conv_name + '.weight'
                all_sketch_conv_weight.append(conv_weight_name) #Record the weight of the sketch
                oriweight = oristate_dict[conv_weight_name]
                l = int(oriweight.size(0) * args.sketch_rate)

                if l < oriweight.size(1) * oriweight.size(2) * oriweight.size(3) and j == 0:
                    bn_weight_name = layer_name + str(i) + '.bn' + str(j + 1) + '.weight'
                    bn_bias_name = layer_name + str(i) + '.bn' + str(j + 1) + '.bias'
                    all_sketch_bn_weight.append(bn_weight_name)
                    if args.sketch_bn:
                        bn_weight = oristate_dict[bn_weight_name]
                        bn_bias = oristate_dict[bn_bias_name]
                        sketch_filter, state_dict[bn_weight_name], \
                            state_dict[bn_bias_name] = sketch_matrix(oriweight, l, dim=0,
                                                           bn_weight=bn_weight,
                                                           bn_bias=bn_bias, sketch_bn=True)
                        if args.weight_norm:
                            sketch_filter /= torch.sum(sketch_filter)
                            state_dict[bn_weight_name] /= \
                                torch.sum(state_dict[bn_weight_name])
                            state_dict[bn_bias_name] /= \
                                torch.sum(state_dict[bn_bias_name])
                    else:
                        sketch_filter = sketch_matrix(oriweight, l, dim=0,
                                                    bn_weight=None,
                                                    bn_bias=None, sketch_bn=False,
                                                      weight_norm_method=args.weight_norm_method,
                                                      filter_norm=args.filter_norm
                                                      )
                    if is_preserve or j == 0:
                        state_dict[conv_weight_name] = sketch_filter
                    else:
                        l = int(oriweight.size(1) * args.sketch_rate)
                        sketch_channel = sketch_matrix(sketch_filter, l, dim=1, bn_weight=None, sketch_bn=False,
                                                       weight_norm_method=args.weight_norm_method,
                                                       filter_norm=args.filter_norm
                                                       )
                        state_dict[conv_weight_name] = sketch_channel
                    is_preserve = False
                else:
                    if j == 1: #Block the last volume layer only sketch the channel dimension
                        l = int(oriweight.size(1) * args.sketch_rate)
                        sketch_channel = sketch_matrix(oriweight, l, dim=1, bn_weight=None, sketch_bn=False,
                                                       weight_norm_method=args.weight_norm_method,
                                                       filter_norm=args.filter_norm
                                                       )
                        state_dict[conv_weight_name] = sketch_channel
                    else:
                        state_dict[conv_weight_name] = oriweight
                        is_preserve = True

    # print(all_sketch_bn_weight)
    for name, module in model.named_modules():
        if isinstance(module, nn.Conv2d):
            conv_name = name + '.weight'
            if conv_name not in all_sketch_conv_weight:
                state_dict[conv_name] = oristate_dict[conv_name]

        elif isinstance(module, nn.BatchNorm2d):
            bn_weight_name = name + '.weight'
            bn_bias_name = name + '.bias'
            bn_mean_name = name + '.running_mean'
            bn_var_name = name + '.running_var'
            if bn_weight_name not in all_sketch_bn_weight:
                state_dict[bn_weight_name] = oristate_dict[bn_weight_name]
                state_dict[bn_bias_name] = oristate_dict[bn_bias_name]
                state_dict[bn_mean_name] = oristate_dict[bn_mean_name]
                state_dict[bn_var_name] = oristate_dict[bn_var_name]

        elif isinstance(module, nn.Linear):
            state_dict[name + '.weight'] = oristate_dict[name + '.weight']
            state_dict[name + '.bias'] = oristate_dict[name + '.bias']

    model.load_state_dict(state_dict)
    logger.info('==>After Sketch')
    test(model, loader.testLoader)

def load_resnet_imagenet_sketch_model(model):
    cfg = {'resnet18': [2, 2, 2, 2],
           'resnet34': [3, 4, 6, 3],
           'resnet50': [3, 4, 6, 3],
           'resnet101': [3, 4, 23, 3],
           'resnet152': [3, 8, 36, 3]}

    if args.sketch_model is None or not os.path.exists(args.sketch_model):
        raise ('Sketch model path should be exist!')
    ckpt = torch.load(args.sketch_model, map_location=device)
    origin_model = import_module(f'model.{args.arch}_imagenet').resnet(args.cfg).to(device)
    origin_model.load_state_dict(ckpt)
    logger.info('==>Before Sketch')
    test(origin_model, loader.testLoader, topk=(1, 5))

    oristate_dict = origin_model.state_dict()

    state_dict = model.state_dict()
    is_preserve = False  # Whether the previous layer retains the original weight dimension, no sketch

    current_cfg = cfg[args.cfg]

    all_sketch_conv_weight = []
    all_sketch_bn_weight = []

    for layer, num in enumerate(current_cfg):
        layer_name = 'layer' + str(layer + 1) + '.'
        for i in range(num):
            if args.cfg == 'resnet18' or args.cfg == 'resnet34':
                iter = 2  # the number of convolution layers in a block, except for shortcut
            else:
                iter = 3
            for j in range(iter):
                # Block the first convolution layer, only sketching the first dimension
                # Block the last convolution layer, only Skitch on the channel dimension
                conv_name = layer_name + str(i) + '.conv' + str(j + 1)
                conv_weight_name = conv_name + '.weight'
                all_sketch_conv_weight.append(conv_weight_name)  # Record the weight of the sketch
                oriweight = oristate_dict[conv_weight_name]
                l = int(oriweight.size(0) * args.sketch_rate)

                if l < oriweight.size(1) * oriweight.size(2) * oriweight.size(3) and j != iter - 1:
                    bn_weight_name = layer_name + str(i) + '.bn' + str(j + 1) + '.weight'
                    bn_bias_name = layer_name + str(i) + '.bn' + str(j + 1) + '.bias'
                    all_sketch_bn_weight.append(bn_weight_name)
                    if args.sketch_bn:
                        bn_weight = oristate_dict[bn_weight_name]
                        bn_bias = oristate_dict[bn_bias_name]
                        sketch_filter, state_dict[bn_weight_name], \
                        state_dict[bn_bias_name] = sketch_matrix(oriweight, l, dim=0,
                                                                 bn_weight=bn_weight,
                                                                 bn_bias=bn_bias, sketch_bn=True,
                                                                 weight_norm_method = args.weight_norm_method,
                                                                 filter_norm = args.filter_norm
                        )
                    else:
                        sketch_filter = sketch_matrix(oriweight, l, dim=0,
                                                      bn_weight=None,
                                                      bn_bias=None, sketch_bn=False,
                                                      weight_norm_method=args.weight_norm_method,
                                                      filter_norm=args.filter_norm
                                                      )
                    if is_preserve or j == 0:
                        state_dict[conv_weight_name] = sketch_filter
                    else:
                        l = int(oriweight.size(1) * args.sketch_rate)
                        sketch_channel = sketch_matrix(sketch_filter, l, dim=1, bn_weight=None, sketch_bn=False,
                                                       weight_norm_method=args.weight_norm_method,
                                                       filter_norm=args.filter_norm
                                                       )
                        state_dict[conv_weight_name] = sketch_channel
                    is_preserve = False
                else:
                    if j == iter - 1:  # Block the last volume layer only sketch the channel dimension
                        l = int(oriweight.size(1) * args.sketch_rate)
                        sketch_channel = sketch_matrix(oriweight, l, dim=1, bn_weight=None, sketch_bn=False,
                                                       weight_norm_method=args.weight_norm_method,
                                                       filter_norm=args.filter_norm
                                                       )
                        state_dict[conv_weight_name] = sketch_channel
                    else:
                        state_dict[conv_weight_name] = oriweight
                        is_preserve = True

    for name, module in model.named_modules():
        if isinstance(module, nn.Conv2d):
            conv_name = name + '.weight'
            if conv_name not in all_sketch_conv_weight:
                state_dict[conv_name] = oristate_dict[conv_name]

        elif isinstance(module, nn.BatchNorm2d):
            bn_weight_name = name + '.weight'
            bn_bias_name = name + '.bias'
            bn_mean_name = name + '.running_mean'
            bn_var_name = name + '.running_var'
            if bn_weight_name not in all_sketch_bn_weight:
                state_dict[bn_weight_name] = oristate_dict[bn_weight_name]
                state_dict[bn_bias_name] = oristate_dict[bn_bias_name]
                # state_dict[bn_mean_name] = oristate_dict[bn_mean_name]
                # state_dict[bn_var_name] = oristate_dict[bn_var_name]

        elif isinstance(module, nn.Linear):
            state_dict[name + '.weight'] = oristate_dict[name + '.weight']
            state_dict[name + '.bias'] = oristate_dict[name + '.bias']

    model.load_state_dict(state_dict)
    logger.info('==>After Sketch')
    test(model, loader.testLoader, topk=(1, 5))

def load_googlenet_sketch_model(model):
    if args.sketch_model is None or not os.path.exists(args.sketch_model):
        raise ('Sketch model path should be exist!')
    ckpt = torch.load(args.sketch_model, map_location=device)
    origin_model = import_module(f'model.{args.arch}').GoogLeNet().to(device)
    origin_model.load_state_dict(ckpt['state_dict'])
    oristate_dict = origin_model.state_dict()

    state_dict = model.state_dict()

    for name, module in origin_model.named_modules():
        if isinstance(module, Inception):
            conv_weight = []
            bn_weight = []
            bn_bias = []

            cfg = [1, 2, 3, 1]

            sketch_filter_channel_index = [4]  # the index of sketch filter and channel weight
            sketch_channel_index = [2, 5]  # the index of sketch channel weight
            sketch_filter_index = [1, 3]  # the index of sketch filter weight

            for i in range(4): #each inception has four module

                for j in range(cfg[i]):

                    conv_index = 3 * j
                    bn_index = 3 * j + 1

                    if i == 3:
                        conv_index = 1
                        bn_index = 2

                    conv_weight_name = name + '.b' + str(i+1) + '.' + str(conv_index) + '.weight'
                    conv_weight.append(conv_weight_name)

                    bn_weight_name = name + '.b' + str(i+1) + '.' + str(bn_index) + '.weight'
                    bn_weight.append(bn_weight_name)

                    bn_bias_name = name + '.b' + str(i+1) + '.' + str(bn_index) + '.bias'
                    bn_bias.append(bn_bias_name)

            for i in range(len(conv_weight)):
                conv_weight_name = conv_weight[i]
                oriweight = oristate_dict[conv_weight_name]

                l = int(oriweight.size(0) * args.sketch_rate)

                if l < oriweight.size(1) * oriweight.size(2) * oriweight.size(3) and (i in sketch_filter_channel_index
                                                                                      or i in sketch_filter_index):
                    if args.sketch_bn:
                        bn_weight_name = bn_weight[i]
                        bn_bias_name = bn_bias[i]
                        bn_weight = oristate_dict[bn_weight_name]
                        bn_bias = oristate_dict[bn_bias_name]
                        sketch_filter, state_dict[bn_weight_name], \
                            state_dict[bn_bias_name] = sketch_matrix(oriweight, l, dim=0,
                                                           bn_weight=bn_weight,
                                                           bn_bias=bn_bias, sketch_bn=True)
                        if args.weight_norm:
                            sketch_filter /= torch.sum(sketch_filter)
                            state_dict[bn_weight_name] /= \
                                torch.sum(state_dict[bn_weight_name])
                            state_dict[bn_bias_name] /= \
                                torch.sum(state_dict[bn_bias_name])
                    else:
                        sketch_filter = sketch_matrix(oriweight, l, dim=0,
                                                           bn_weight=None,
                                                           bn_bias=None, sketch_bn=False,
                                                            weight_norm_method=args.weight_norm_method,
                                                            filter_norm=args.filter_norm
                                                      )
                    if i in sketch_filter_index or (i in [0, 1, 3, 6] and name == 'a3'):
                        state_dict[conv_weight_name] = sketch_filter
                    else:
                        l = int(oriweight.size(1) * args.sketch_rate)
                        sketch_channel = sketch_matrix(sketch_filter, l, dim=1, bn_weight=None, sketch_bn=False,
                                                       weight_norm_method=args.weight_norm_method,
                                                       filter_norm=args.filter_norm
                                                       )
                        state_dict[conv_weight_name] = sketch_channel
                    is_preserve = False
                else:
                    if i in sketch_channel_index:
                        l = int(oriweight.size(1) * args.sketch_rate)
                        sketch_channel = sketch_matrix(oriweight, l, dim=1, bn_weight=None, sketch_bn=False,
                                                       weight_norm_method=args.weight_norm_method,
                                                       filter_norm=args.filter_norm
                                                       )
                        state_dict[conv_weight_name] = sketch_channel
                    else:
                        state_dict[conv_weight_name] = oriweight
                        is_preserve = True

    for name, module in model.named_modules(): #Reassign non sketch weights to the new network
        if isinstance(module, Inception):
            state_dict[name + '.b1.0.weight'] = oristate_dict[name + '.b1.0.weight']
            state_dict[name + '.b1.1.weight'] = oristate_dict[name + '.b1.1.weight']
            state_dict[name + '.b1.1.bias'] = oristate_dict[name + '.b1.1.bias']

            state_dict[name + '.b4.1.weight'] = oristate_dict[name + '.b4.1.weight']
            state_dict[name + '.b4.2.weight'] = oristate_dict[name + '.b4.2.weight']
            state_dict[name + '.b4.2.bias'] = oristate_dict[name + '.b4.2.bias']

        # elif isinstance(module, nn.Linear):
        #     state_dict[name + '.weight'] = oristate_dict[name + '.weight']
        #     state_dict[name + '.bias'] = oristate_dict[name + '.bias']

    state_dict['pre_layers.0.weight'] = oristate_dict['pre_layers.0.weight']
    state_dict['pre_layers.0.bias'] = oristate_dict['pre_layers.0.bias']
    state_dict['pre_layers.1.weight'] = oristate_dict['pre_layers.1.weight']
    state_dict['pre_layers.1.bias'] = oristate_dict['pre_layers.1.bias']

    model.load_state_dict(state_dict)

def load_densenet_sketch_model(model):

    if args.sketch_model is None or not os.path.exists(args.sketch_model):
        raise ('Sketch model path should be exist!')

    cfgs = {
        'densenet121': [6, 12, 24, 16],
        'densenet161': [6, 12, 36, 24],
        'densenet169': [6, 12, 32, 32],
        'densenet201': [6, 12, 48, 32],
        'densenet_cifar': [6, 12, 24, 16],
    }
    current_cfg = cfgs[args.cfg]
    ckpt = torch.load(args.sketch_model, map_location=device)
    origin_model = import_module(f'model.{args.arch}').densenet_cifar().to(device)
    origin_model.load_state_dict(ckpt['state_dict'])
    oristate_dict = origin_model.state_dict()

    state_dict = model.state_dict()

    #the first convolution layer sketch filter and the second sketch channel
    conv_weight = []
    bn_weight = []
    bn_bias = []
    for i in range(4):

        for j in range(current_cfg[i]):

            conv1_weight_name = 'dense%d.%d.conv1.weight' % (i + 1, j)
            conv2_weight_name = 'dense%d.%d.conv2.weight' % (i + 1, j)
            conv_weight.append(conv1_weight_name)
            conv_weight.append(conv2_weight_name)

            bn_weight_name = 'dense%d.%d.bn2.weight' % (i + 1, j)
            bn_weight.append(bn_weight_name)

            bn_bias_name = 'dense%d.%d.bn2.bias' % (i + 1, j)
            bn_bias.append(bn_bias_name)

    #     print(k, v.size())
    # print(conv_weight)
    # print(bn_weight)
    # print(bn_bias)

    for i in range(len(conv_weight) // 2):
        conv_weight_name = conv_weight[2 * i]
        oriweight = oristate_dict[conv_weight_name]

        l = int(oriweight.size(0) * args.sketch_rate)

        if l < oriweight.size(1) * oriweight.size(2) * oriweight.size(3):
            if args.sketch_bn:
                bn_weight_name = bn_weight[i]
                bn_bias_name = bn_bias[i]
                bn_weight = oristate_dict[bn_weight_name]
                bn_bias = oristate_dict[bn_bias_name]
                sketch_filter, state_dict[bn_weight_name], \
                state_dict[bn_bias_name] = sketch_matrix(oriweight, l, dim=0,
                                                         bn_weight=bn_weight,
                                                         bn_bias=bn_bias, sketch_bn=True)
                if args.weight_norm:
                    sketch_filter /= torch.sum(sketch_filter)
                    state_dict[bn_weight_name] /= \
                        torch.sum(state_dict[bn_weight_name])
                    state_dict[bn_bias_name] /= \
                        torch.sum(state_dict[bn_bias_name])
            else:
                sketch_filter = sketch_matrix(oriweight, l, dim=0,
                                              bn_weight=None,
                                              bn_bias=None, sketch_bn=False,
                                              weight_norm_method=args.weight_norm_method,
                                              filter_norm=args.filter_norm
                                              )
            state_dict[conv_weight_name] = sketch_filter

            conv_weight_name = conv_weight[2 * i + 1]
            oriweight = oristate_dict[conv_weight_name]
            l = int(oriweight.size(1) * args.sketch_rate)
            sketch_channel = sketch_matrix(oriweight, l, dim=1, bn_weight=None, sketch_bn=False,
                                           weight_norm_method=args.weight_norm_method,
                                           filter_norm=args.filter_norm
                                           )
            state_dict[conv_weight_name] = sketch_channel

    for name, module in model.named_modules():
        if isinstance(module, nn.Conv2d):
            conv_name = name + '.weight'
            if conv_name not in conv_weight:
                state_dict[conv_name] = oristate_dict[conv_name]

        elif isinstance(module, nn.BatchNorm2d):
            bn_weight_name = name + '.weight'
            bn_bias_name = name + '.bias'
            if bn_weight_name not in bn_weight and bn_bias_name not in bn_bias:
                state_dict[bn_weight_name] = oristate_dict[bn_weight_name]
                state_dict[bn_bias_name] = oristate_dict[bn_bias_name]

        elif isinstance(module, nn.Linear):
            state_dict[name + '.weight'] = oristate_dict[name + '.weight']
            state_dict[name + '.bias'] = oristate_dict[name + '.bias']

    model.load_state_dict(state_dict)

# Training
def train(model, optimizer, trainLoader, args, epoch):

    model.train()
    losses = utils.AverageMeter()
    accurary = utils.AverageMeter()
    print_freq = len(trainLoader.dataset) // args.train_batch_size // 10
    start_time = time.time()
    for batch, (inputs, targets) in enumerate(trainLoader):

        inputs, targets = inputs.to(device), targets.to(device)
        optimizer.zero_grad()
        output = model(inputs)
        loss = loss_func(output, targets)
        loss.backward()
        losses.update(loss.item(), inputs.size(0))
        optimizer.step()

        prec1 = utils.accuracy(output, targets)
        accurary.update(prec1[0], inputs.size(0))

        if batch % print_freq == 0 and batch != 0:
            current_time = time.time()
            cost_time = current_time - start_time
            logger.info(
                'Epoch[{}] ({}/{}):\t'
                'Loss {:.4f}\t'
                'Accuracy {:.2f}%\t\t'
                'Time {:.2f}s'.format(
                    epoch, batch * args.train_batch_size, len(trainLoader.dataset),
                    float(losses.avg), float(accurary.avg), cost_time
                )
            )
            start_time = current_time

def test(model, testLoader, topk=(1,)):
    model.eval()

    losses = utils.AverageMeter()
    accuracy = utils.AverageMeter()
    top5_accuracy = utils.AverageMeter()

    start_time = time.time()
    with torch.no_grad():
        for batch_idx, (inputs, targets) in enumerate(testLoader):
            inputs, targets = inputs.to(device), targets.to(device)
            outputs = model(inputs)
            loss = loss_func(outputs, targets)

            losses.update(loss.item(), inputs.size(0))
            predicted = utils.accuracy(outputs, targets, topk=topk)
            accuracy.update(predicted[0], inputs.size(0))
            if len(topk) == 2:
                top5_accuracy.update(predicted[1], inputs.size(0))

        current_time = time.time()
        if len(topk) == 1:
            logger.info(
                'Test Loss {:.4f}\tAccuracy {:.2f}%\t\tTime {:.2f}s\n'
                .format(float(losses.avg), float(accuracy.avg), (current_time - start_time))
            )
        else:
            logger.info(
                'Test Loss {:.4f}\tTop1 {:.2f}%\tTop5 {:.2f}%\tTime {:.2f}s\n'
                    .format(float(losses.avg), float(accuracy.avg), float(top5_accuracy.avg), (current_time - start_time))
            )
    return accuracy.avg

def main():
    start_epoch = 0
    best_acc = 0.0

    # Model
    print('==> Building model..')
    if args.arch == 'vgg':
        model = import_module(f'model.{args.arch}').SketchVGG(args.sketch_rate).to(device)
        load_vgg_sketch_model(model)
    elif args.arch == 'resnet':
        if args.data_set == 'imagenet':
            model = import_module(f'model.{args.arch}_imagenet').resnet(args.cfg, sketch_rate=args.sketch_rate).to(device)
            load_resnet_imagenet_sketch_model(model)
        else:
            model = import_module(f'model.{args.arch}').resnet(args.cfg, sketch_rate=args.sketch_rate).to(device)
            load_resnet_sketch_model(model)
    elif args.arch == 'googlenet':
        model = import_module(f'model.{args.arch}').GoogLeNet(args.sketch_rate).to(device)
        load_googlenet_sketch_model(model)
    elif args.arch == 'densenet':
        model = import_module(f'model.{args.arch}').densenet_cifar(args.sketch_rate).to(device)
        load_densenet_sketch_model(model)

    print('Sketch Done!')

    if len(args.gpus) != 1:
        model = nn.DataParallel(model, device_ids=args.gpus)

    optimizer = optim.SGD(model.parameters(), lr=args.lr, momentum=args.momentum, weight_decay=args.weight_decay)
    scheduler = optim.lr_scheduler.MultiStepLR(optimizer, milestones=args.lr_decay_step, gamma=0.1)

    for epoch in range(start_epoch, args.num_epochs):
        train(model, optimizer, loader.trainLoader, args, epoch)
        scheduler.step()
        test_acc = test(model, loader.testLoader, topk=(1, 5) if args.data_set == 'imagenet' else (1, ))

        is_best = best_acc < test_acc
        best_acc = max(best_acc, test_acc)

        model_state_dict = model.module.state_dict() if len(args.gpus) > 1 else model.state_dict()

        state = {
            'state_dict': model_state_dict,
            'best_acc': best_acc,
            'optimizer': optimizer.state_dict(),
            'scheduler': scheduler.state_dict(),
            'epoch': epoch + 1
        }
        checkpoint.save_model(state, epoch + 1, is_best)

    logger.info('Best accuracy: {:.3f}'.format(float(best_acc)))