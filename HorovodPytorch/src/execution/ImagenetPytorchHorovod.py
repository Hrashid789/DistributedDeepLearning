"""
Trains ResNet50 in Keras using Horovod.

It requires the following env variables
AZ_BATCHAI_INPUT_TRAIN
AZ_BATCHAI_INPUT_TEST
AZ_BATCHAI_OUTPUT_MODEL
AZ_BATCHAI_JOB_TEMP_DIR
"""
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

from timer import Timer

import os
from PIL import Image

import torch.optim as optim
from torchvision import transforms
import torch.utils.data.distributed
import torch.backends.cudnn as cudnn
import torchvision.models as models
from os import path
import pandas as pd
from torch.utils.data import Dataset
from torch.autograd import Variable
import torch.nn.functional as F

_WIDTH = 224
_HEIGHT = 224
_CHANNELS = 3
_LR = 0.001
_EPOCHS = 1
_BATCHSIZE = 64
_RGB_MEAN = [0.485, 0.456, 0.406]
_RGB_SD = [0.229, 0.224, 0.225]
_SEED=42

# Settings from https://arxiv.org/abs/1706.02677.
_WARMUP_EPOCHS = 5
_WEIGHT_DECAY = 0.00005

def _str_to_bool(in_str):
    if 't' in in_str.lower():
        return True
    else:
        return False

_DISTRIBUTED = _str_to_bool(os.getenv('DISTRIBUTED', 'False'))

if _DISTRIBUTED:
    import horovod.torch as hvd


def _append_path_to(data_path, data_series):
    return data_series.apply(lambda x: path.join(data_path, x))


def _load_training(data_dir):
    train_df = pd.read_csv(path.join(data_dir, 'train.csv'))
    return train_df.assign(filenames=_append_path_to(path.join(data_dir, 'train'),
                                                     train_df.filenames))

def _load_validation(data_dir):
    train_df = pd.read_csv(path.join(data_dir, 'validation.csv'))
    return train_df.assign(filenames=_append_path_to(path.join(data_dir, 'validation'),
                            train_df.filenames))


def _create_data_fn(train_path, test_path):
    logger.info('Reading training data info')
    train_df = _load_training(train_path)
    logger.info('Reading validation data info')
    validation_df = _load_validation(test_path)
    # File-path
    train_X = train_df['filenames'].values
    validation_X = validation_df['filenames'].values
    # One-hot encoded labels for torch
    train_labels = train_df[['num_id']].values.ravel()
    validation_labels = validation_df[['num_id']].values.ravel()
    # Index starts from 0
    train_labels -= 1
    validation_labels -= 1
    return train_X, train_labels, validation_X, validation_labels


class ImageNet(Dataset):
    def __init__(self, img_locs, img_labels, transform=None):
        self.img_locs, self.labels = img_locs, img_labels
        self.transform = transform
        logger.info("Loaded {} labels and {} images".format(len(self.labels), len(self.img_locs)))

    def __getitem__(self, idx):
        im_file = self.img_locs[idx]
        label = self.labels[idx]
        with open(im_file, 'rb') as f:
            im_rgb = Image.open(f)
            # Make sure 3-channel (RGB)
            im_rgb = im_rgb.convert('RGB')
            if self.transform is not None:
                im_rgb = self.transform(im_rgb)
            return im_rgb, label

    def __len__(self):
        return len(self.img_locs)


def _is_master(is_distributed=_DISTRIBUTED):
    if is_distributed:
        if hvd.rank() == 0:
            return True
        else:
            return False
    else:
        return True


def train(train_loader, model, criterion, optimizer, epoch):
    logger.info("Training ...")
    t=Timer()
    t.__enter__()
    for i, (data, target) in enumerate(train_loader):
        data, target = data.cuda(), target.cuda()
        data, target = Variable(data), Variable(target)
        # target = target.cuda(non_blocking=True)
        optimizer.zero_grad()
        # compute output
        output = model(data)
        loss = F.cross_entropy(output, target)
        # loss = criterion(output, target)
        # compute gradient and do SGD step
        loss.backward()
        optimizer.step()
        if i % 100 == 0:
            msg = 'Train Epoch: {}   duration({})  loss:{} total-samples: {}'
            logger.info(msg.format(epoch, t.elapsed, loss.data[0], i * len(data)))
            t.__enter__()


def main():
    if _DISTRIBUTED:
        # Horovod: initialize Horovod.
        logger.info("Runnin Distributed")
        hvd.init()
        torch.manual_seed(_SEED)
        # Horovod: pin GPU to local rank.
        torch.cuda.set_device(hvd.local_rank())
        torch.cuda.manual_seed(_SEED)

    logger.info("PyTorch version {}".format(torch.__version__))

    normalize = transforms.Normalize(_RGB_MEAN, _RGB_SD)

    train_X, train_y, valid_X, valid_y = _create_data_fn(os.getenv('AZ_BATCHAI_INPUT_TRAIN'), os.getenv('AZ_BATCHAI_INPUT_TEST'))

    logger.info("Setting up loaders")
    train_dataset = ImageNet(
        train_X,
        train_y,
        transforms.Compose([
            transforms.RandomResizedCrop(_WIDTH),
            transforms.RandomHorizontalFlip(),
            transforms.ToTensor(),
            normalize]))

    train_sampler = torch.utils.data.distributed.DistributedSampler(
        train_dataset, num_replicas=hvd.size(), rank=hvd.rank())

    kwargs = {'num_workers': 1, 'pin_memory': True}
    train_loader = torch.utils.data.DataLoader(
        train_dataset, batch_size=_BATCHSIZE, sampler=train_sampler, **kwargs)

    # Autotune
    cudnn.benchmark = True

    logger.info("Loading model")
    # Load symbol
    model = models.__dict__['resnet50'](pretrained=False)

    model.cuda()

    # Horovod: broadcast parameters.
    hvd.broadcast_parameters(model.state_dict(), root_rank=0)

    # Horovod: scale learning rate by the number of GPUs.
    optimizer = optim.SGD(model.parameters(), lr=_LR * hvd.size(),
                          momentum=0.9)

    # Horovod: wrap optimizer with DistributedOptimizer.
    optimizer = hvd.DistributedOptimizer(
        optimizer, named_parameters=model.named_parameters())

    criterion=None
    # Main training-loop
    for epoch in range(_EPOCHS):
        with Timer(output=logger.info, prefix="Training"):
            model.train()
            train_sampler.set_epoch(epoch)
            train(train_loader, model, criterion, optimizer, epoch)


if __name__ == '__main__':
    main()