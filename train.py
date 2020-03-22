#!/usr/bin/env python

import argparse
import keras
from keras import backend as K
from keras.preprocessing import image
from keras.datasets import fashion_mnist
from keras_contrib.applications.wide_resnet import WideResidualNetwork
import mlflow
import mlflow.keras
import numpy as np
import tensorflow as tf
import horovod.keras as hvd
import os
import gzip

def load_data(data_path):
    """Loads the Fashion-MNIST dataset.
    # Returns
        Tuple of Numpy arrays: `(x_train, y_train), (x_test, y_test)`.
    """
    files = ['train-labels-idx1-ubyte.gz', 'train-images-idx3-ubyte.gz',
             't10k-labels-idx1-ubyte.gz', 't10k-images-idx3-ubyte.gz']
    paths = []
    for fname in files:
        paths.append(os.path.join(data_path, fname))
    with gzip.open(paths[0], 'rb') as lbpath:
        y_train = np.frombuffer(lbpath.read(), np.uint8, offset=8)
    with gzip.open(paths[1], 'rb') as imgpath:
        x_train = np.frombuffer(imgpath.read(), np.uint8, offset=16).reshape(len(y_train), 28, 28)
    with gzip.open(paths[2], 'rb') as lbpath:
        y_test = np.frombuffer(lbpath.read(), np.uint8, offset=8)
    with gzip.open(paths[3], 'rb') as imgpath:
        x_test = np.frombuffer(imgpath.read(), np.uint8, offset=16).reshape(len(y_test), 28, 28)
    return (x_train, y_train), (x_test, y_test)

parser = argparse.ArgumentParser(description='Keras Fashion MNIST Example',
                                 formatter_class=argparse.ArgumentDefaultsHelpFormatter)
parser.add_argument('--log-dir', default='./logs',
                    help='tensorboard log directory')
parser.add_argument('--batch-size', type=int, default=32,
                    help='input batch size for training')
parser.add_argument('--val-batch-size', type=int, default=32,
                    help='input batch size for validation')
parser.add_argument('--epochs', type=int, default=40,
                    help='number of epochs to train')
parser.add_argument('--base-lr', type=float, default=0.01,
                    help='learning rate for a single GPU')
parser.add_argument('--warmup-epochs', type=float, default=5,
                    help='number of warmup epochs')
parser.add_argument('--momentum', type=float, default=0.9,
                    help='SGD momentum')
parser.add_argument('--wd', type=float, default=0.000005,
                    help='weight decay')

# Added MMP/HaaS specific arguments
parser.add_argument('--dataset-path', help='Path to the training dataset.',
                    dest='dataset_path', type=str)
parser.add_argument('--output-path', help='Path to the trained model output.',
                    dest='output_path', type=str)
# parser.add_argument('--tracking-uri', help='MlFlow Tracking API URL.',
#                     dest='tracking_uri', type=str)
# parser.add_argument('--experiment-name', help='MlFlow Experiment Name.',
#                     dest='experiment_name', type=str)
parser.add_argument('--dataset', help='Training dataset Name.',
                    dest='dataset_type')

args = parser.parse_args()

# Checkpoints will be written in the log directory.
args.checkpoint_format = os.path.join(args.output_path, 'checkpoint-{epoch}.h5')

# Horovod: initialize Horovod.
hvd.init()

# Horovod: pin GPU to be used to process local rank (one GPU per process)
config = tf.ConfigProto()
config.gpu_options.allow_growth = True
config.gpu_options.visible_device_list = str(hvd.local_rank())
K.set_session(tf.Session(config=config))

# If set > 0, will resume training from a given checkpoint.
resume_from_epoch = 0
for try_epoch in range(args.epochs, 0, -1):
    if os.path.exists(args.checkpoint_format.format(epoch=try_epoch)):
        resume_from_epoch = try_epoch
        break

# Horovod: broadcast resume_from_epoch from rank 0 (which will have
# checkpoints) to other ranks.
resume_from_epoch = hvd.broadcast(resume_from_epoch, 0, name='resume_from_epoch')

# Horovod: print logs on the first worker.
verbose = 1 if hvd.rank() == 0 else 0

# Input image dimensions
img_rows, img_cols = 28, 28
num_classes = 10

# Load Fashion MNIST data.
(x_train, y_train), (x_test, y_test) = load_data(args.dataset_path)

if K.image_data_format() == 'channels_first':
    x_train = x_train.reshape(x_train.shape[0], 1, img_rows, img_cols)
    x_test = x_test.reshape(x_test.shape[0], 1, img_rows, img_cols)
    input_shape = (1, img_rows, img_cols)
else:
    x_train = x_train.reshape(x_train.shape[0], img_rows, img_cols, 1)
    x_test = x_test.reshape(x_test.shape[0], img_rows, img_cols, 1)
    input_shape = (img_rows, img_cols, 1)

# Convert class vectors to binary class matrices
y_train = keras.utils.to_categorical(y_train, num_classes)
y_test = keras.utils.to_categorical(y_test, num_classes)

# Training data iterator.
train_gen = image.ImageDataGenerator(featurewise_center=True, featurewise_std_normalization=True,
                                     horizontal_flip=True, width_shift_range=0.2, height_shift_range=0.2)
train_gen.fit(x_train)
train_iter = train_gen.flow(x_train, y_train, batch_size=args.batch_size)

# Validation data iterator.
test_gen = image.ImageDataGenerator(featurewise_center=True, featurewise_std_normalization=True)
test_gen.mean = train_gen.mean
test_gen.std = train_gen.std
test_iter = test_gen.flow(x_test, y_test, batch_size=args.val_batch_size)

# Restore from a previous checkpoint, if initial_epoch is specified.
# Horovod: restore on the first worker which will broadcast both model and optimizer weights
# to other workers.
if resume_from_epoch > 0 and hvd.rank() == 0:
    model = hvd.load_model(args.checkpoint_format.format(epoch=resume_from_epoch))
else:
    # Set up standard WideResNet-16-10 model.
    model = WideResidualNetwork(depth=16, width=10, weights=None, input_shape=input_shape,
                                classes=num_classes, dropout_rate=0.01)

    # WideResNet model that is included with Keras is optimized for inference.
    # Add L2 weight decay & adjust BN settings.
    model_config = model.get_config()
    for layer, layer_config in zip(model.layers, model_config['layers']):
        if hasattr(layer, 'kernel_regularizer'):
            regularizer = keras.regularizers.l2(args.wd)
            layer_config['config']['kernel_regularizer'] = \
                {'class_name': regularizer.__class__.__name__,
                 'config': regularizer.get_config()}
        if type(layer) == keras.layers.BatchNormalization:
            layer_config['config']['momentum'] = 0.9
            layer_config['config']['epsilon'] = 1e-5

    model = keras.models.Model.from_config(model_config)

    # Horovod: adjust learning rate based on number of GPUs.
    opt = keras.optimizers.SGD(lr=args.base_lr * hvd.size(),
                               momentum=args.momentum)

    # Horovod: add Horovod Distributed Optimizer.
    opt = hvd.DistributedOptimizer(opt)

    model.compile(loss=keras.losses.categorical_crossentropy,
                  optimizer=opt,
                  metrics=['accuracy'])


callbacks = [
    # Horovod: broadcast initial variable states from rank 0 to all other processes.
    # This is necessary to ensure consistent initialization of all workers when
    # training is started with random weights or restored from a checkpoint.
    hvd.callbacks.BroadcastGlobalVariablesCallback(0),

    # Horovod: average metrics among workers at the end of every epoch.
    #
    # Note: This callback must be in the list before the ReduceLROnPlateau,
    # TensorBoard, or other metrics-based callbacks.
    hvd.callbacks.MetricAverageCallback(),

    # Horovod: using `lr = 1.0 * hvd.size()` from the very beginning leads to worse final
    # accuracy. Scale the learning rate `lr = 1.0` ---> `lr = 1.0 * hvd.size()` during
    # the first five epochs. See https://arxiv.org/abs/1706.02677 for details.
    hvd.callbacks.LearningRateWarmupCallback(warmup_epochs=args.warmup_epochs, verbose=verbose),

    # Horovod: after the warmup reduce learning rate by 10 on the 15th, 25th and 35th epochs.
    hvd.callbacks.LearningRateScheduleCallback(start_epoch=args.warmup_epochs, end_epoch=15, multiplier=1.),
    hvd.callbacks.LearningRateScheduleCallback(start_epoch=15, end_epoch=25, multiplier=1e-1),
    hvd.callbacks.LearningRateScheduleCallback(start_epoch=25, end_epoch=35, multiplier=1e-2),
    hvd.callbacks.LearningRateScheduleCallback(start_epoch=35, multiplier=1e-3),
]

# Horovod: save checkpoints only on the first worker to prevent other workers from corrupting them.
if hvd.rank() == 0:
    callbacks.append(keras.callbacks.ModelCheckpoint(args.checkpoint_format))
    callbacks.append(keras.callbacks.TensorBoard(args.output_path))

# Train the model. The training will randomly sample 1 / N batches of training data and
# 3 / N batches of validation data on every worker, where N is the number of workers.
# Over-sampling of validation data, which helps to increase the probability that every
# validation example will be evaluated.
model.fit_generator(train_iter,
                    steps_per_epoch=len(train_iter) // hvd.size(),
                    callbacks=callbacks,
                    epochs=args.epochs,
                    verbose=verbose,
                    workers=8,
                    initial_epoch=resume_from_epoch,
                    validation_data=test_iter,
                    validation_steps=3 * len(test_iter) // hvd.size())

# Evaluate the model on the full data set.
score = model.evaluate_generator(test_iter, len(test_iter), workers=8)
if verbose:
    print('Test loss:', score[0])
    print('Test accuracy:', score[1])

# if hvd.rank() == 0:
#     mlflow.tracking.set_tracking_uri(args.tracking_uri)
#     experiment_id = mlflow.create_experiment(args.experiment_name)
#     with mlflow.start_run(experiment_id=experiment_id) as run:
#         mlflow.log_param('epochs', args.epochs)
#         mlflow.log_param('batch_size', args.batch_size)
#         mlflow.log_param('momentum', args.momentum)
#         mlflow.log_param('weight_decay', args.wd)
#         mlflow.log_param('learning_rate', args.base_lr)
#         mlflow.log_metric('loss', score[0])
#         mlflow.log_metric('accuracy', score[1])
#         mlflow.log_artifacts(args.output_path, 'output')