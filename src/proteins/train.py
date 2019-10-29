import os
import yaml
import pyaml
import random
import inspect
import textwrap
import itertools
import multiprocessing

from typing import Mapping
from pathlib import Path
from datetime import datetime
from operator import itemgetter

import numpy as np
import namesgenerator

import torch
import torch.utils.data
import torch.nn.functional as F
from tensorboardX import SummaryWriter

from ignite.engine import Engine, Events
from ignite.contrib.handlers import ProgressBar

from . import features
from .data import DecoyBatch
from .utils import round_timedelta, load_model
from .config import parse_args
from .saver import Saver
from .utils import git_info, cuda_info, set_seeds, import_, sort_dict
from .dataset import ProteinQualityTarget
from .metrics import customize_state, ProteinAverageLosses, LocalMetrics, GlobalMetrics, GpuMaxMemoryAllocated
from .my_hparams import make_session_start_summary, make_session_end_summary
from .ignite_commons import setup_training, setup_validation, update_metrics, handle_failure, flush_logger

# region Arguments parsing
ex = parse_args(config={
    # Experiment defaults
    'name': 'experiment',
    'fullname': '{tags}_{rand}',
    'tags': [],
    'data': {},
    'model': {},
    'optimizer': {},
    'loss': {
        'local_lddt': {},
        'global_gdtts': {},
    },
    'history': [],

    # Session defaults
    'session': {
        'max_epochs': 1,
        'batch_size': 1,
        'seed': random.randint(0, 9999),
        'cpus': multiprocessing.cpu_count() - 1,
        'device': 'cuda' if torch.cuda.is_available() else 'cpu',
        'data': {},
        # 'log': 1,
        'checkpoint': -1,
    }
})
session: dict = ex.pop('session')


# Experiment: checks and computed fields
def validate_history(history):
    if not isinstance(history, list):
        raise ValueError('History must be a list')
    for session in history:
        if session['status'] == 'FAILED':
            raise ValueError(f'Failed session in history:\n{session}')
        if session['status'] == 'COMPLETED':
            if 'samples' not in session:
                raise ValueError(f'Missing number of samples in completed session:\n{session}')
            if 'completed_epochs' not in session:
                raise ValueError(f'Missing number of epochs in completed session:\n{session}')


def validate_losses(losses):
    if not isinstance(losses, dict):
        raise ValueError('Losses must be a dict')
    for loss in losses.values():
        if 'name' not in loss:
            raise ValueError(f'Loss without a name {loss}')
        if 'weight' not in loss:
            raise ValueError(f'Loss without a weight {loss}')


if ex['name'] is None or len(ex['name']) == 0:
    raise ValueError(f'Experiment name is empty: {ex["name"]}')
if ex['tags'] is None:
    raise ValueError('Experiment tags is None')
if ex['model']['fn'] is None:
    raise ValueError('Model constructor function not defined')
if ex['optimizer']['fn'] is None:
    raise ValueError('Optimizer constructor function not defined')
validate_history(ex['history'])
validate_losses(ex['loss'])

ex['completed_epochs'] = sum((session['completed_epochs'] for session in ex['history']), 0)
ex['samples'] = sum((session['samples'] for session in ex['history']), 0)
ex['fullname'] = ex['fullname'].format(tags='_'.join(ex['tags']), rand=namesgenerator.get_random_name())

# These args are computed based on other stuff, the user should not provide a value,
# unless we are continuing training on top a previous session.
if ex['completed_epochs'] == 0:
    model_kwargs = {'enc_in_nodes', 'enc_in_edges'}
    if not set.isdisjoint(set(ex['model'].keys()), model_kwargs):
        raise ValueError(f'Model config dict can not have any of {model_kwargs} in its arguments, '
                         f'found: {", ".join(set.intersection(set(ex["model"].keys()), model_kwargs))}')
ex['model']['enc_in_nodes'] = sum((
    ex['model']['residue_emb_size'],
    23 if ex['data']['partial_entropy'] else 0,
    23 if ex['data']['self_information'] else 0,
    15 if ex['data']['dssp'] else 0,
))
ex['model']['enc_in_edges'] = sum((
    ex['model']['rbf_size'],
    7 if ex['model']['separation_enc'] else 0,
))

# Session computed fields
session['completed_epochs'] = 0
session['samples'] = 0
session['status'] = 'NEW'
session['datetime_started'] = None
session['datetime_completed'] = None
session['git'] = git_info()
session['cuda'] = cuda_info() if 'cuda' in session['device'] else None
session['metric'] = {}
session['misc'] = {}
# 0 = no checkpoint/log, 1 = every epoch, n = every n epochs, -1 = last epoch
session['checkpoint'] = range(0, session['max_epochs'] + 1)[session['checkpoint']]
# session['log'] = range(0, session['max_epochs'] + 1)[session['log']]
if session['cpus'] < 0:
    raise ValueError(f'Invalid number of cpus: {session["cpus"]}')
if session['seed'] is None:
    raise ValueError(f'Invalid seed: {session["seed"]}')
if session['data'].keys() == {'in_memory', 'trainval', 'split'}:
    if isinstance(session['data']['trainval'], str):
        session['data']['trainval'] = [session['data']['trainval']]
    if not isinstance(session['data']['split'], int) or session['data']['split'] <= 0:
        raise ValueError(f'Invalid data split {session["data"]["split"]}')
elif session['data'].keys() == {'in_memory', 'train', 'val'}:
    if isinstance(session['data']['train'], str):
        session['data']['train'] = session['data']['train'].split(':')
    if isinstance(session['data']['val'], str):
        session['data']['val'] = session['data']['val'].split(':')
else:
    raise ValueError(f'Invalid data specification: {session["data"]}')
ex['history'].append(session)

# Print config so far
sort_dict(ex, [
    'name', 'tags', 'fullname', 'comment', 'completed_epochs',
    'samples', 'data', 'model', 'optimizer', 'loss', 'history'
])
sort_dict(session, [
    'completed_epochs', 'samples', 'max_epochs', 'batch_size', 'seed', 'cpus', 'device', 'status',
    'datetime_started', 'datetime_completed', 'data', 'log', 'checkpoint', 'metric', 'misc', 'git', 'cuda'
])
pyaml.pprint(ex, safe=True, sort_dicts=False, force_embed=True, width=200)
# endregion

# region Building phase
# Random seeds (set them after the random run id is generated)
set_seeds(session['seed'])
saver = Saver(Path(os.environ.get('RUNS_FOLDER', './runs')).joinpath(ex['fullname']))
logger = SummaryWriter(saver.base_folder)


# Model and optimizer
def load_optimizer(config: Mapping, model: torch.nn.Module) -> torch.optim.Optimizer:
    special_keys = {'fn'}

    if 'fn' not in config:
        raise ValueError('Optimizer function not specified')

    function = import_(config['fn'])
    function_args = inspect.signature(function).parameters.keys()
    if not set.isdisjoint(special_keys, function_args):
        raise ValueError(f'Optimizer function can not have any of {special_keys} in its arguments, '
                         f'signature is {", ".join(function_args)}')

    kwargs = {k: v for k, v in config.items() if k not in special_keys}
    optimizer = function(model.parameters(), **kwargs)

    return optimizer


model = load_model(ex['model']).to(session['device'])
optimizer = load_optimizer(ex['optimizer'], model)
if ex['completed_epochs'] > 0:
    # Load latest weights and optimizer state
    model.load_state_dict(torch.load(saver.base_folder / 'model.latest.pt', map_location=session['device']))
    optimizer.load_state_dict(torch.load(saver.base_folder / 'optimizer.latest.pt', map_location=session['device']))
else:
    session['misc']['parameters'] = sum(p.numel() for p in model.parameters() if p.requires_grad)
    # A new experiment but using pretrained weights
    if 'state_dict' in ex['model']:
        model.load_state_dict(torch.load(Path(ex['model']['state_dict']).expanduser(), map_location=session['device']))


# Datasets and dataloaders
def get_dataloaders(ex, session):
    node_features = [k for k in ('partial_entropy', 'self_information', 'dssp') if ex['data'][k]]
    cutoff = ex['data']['cutoff']

    max_sequence_length = 0
    if session['data'].keys() == {'in_memory', 'trainval', 'split'}:
        targets = []
        for folder in session['data']['trainval']:
            folder = Path(folder).expanduser().resolve()
            if not folder.is_dir():
                raise ValueError(f'Not a directory: {folder}')
            with open(folder / 'dataset_stats.yaml') as f:
                max_sequence_length = max(max_sequence_length, yaml.safe_load(f)['max_length'])
            for target in sorted(folder.glob('*.npz')):
                targets.append(ProteinQualityTarget(target, node_features=node_features, cutoff=cutoff))

        # If this session is continuing a previous session, make sure to always use the same data split
        # Doing np.random.permutation(targets) takes forever, maybe related to
        # the fact that ProteinQualityTarget defines __len__, but.... ???
        targets_split = np.random.RandomState(ex['history'][0]['seed']).permutation(len(targets))
        targets_train = [targets[i] for i in targets_split[session['data']['split']:]]
        targets_val = [targets[i] for i in targets_split[:session['data']['split']]]
    elif session['data'].keys() == {'in_memory', 'train', 'val'}:
        targets_train = []
        for folder in session['data']['train']:
            folder = Path(folder).expanduser().resolve()
            if not folder.is_dir():
                raise ValueError(f'Not a directory: {folder}')
            with open(folder / 'dataset_stats.yaml') as f:
                max_sequence_length = max(max_sequence_length, yaml.safe_load(f)['max_length'])
            for target in folder.glob('*.npz'):
                targets_train.append(ProteinQualityTarget(target, node_features=node_features, cutoff=cutoff))

        targets_val = []
        for folder in session['data']['val']:
            folder = Path(folder).expanduser().resolve()
            if not folder.is_dir():
                raise ValueError(f'Not a directory: {folder}')
            with open(folder / 'dataset_stats.yaml') as f:
                max_sequence_length = max(max_sequence_length, yaml.safe_load(f)['max_length'])
            for target in folder.glob('*.npz'):
                targets_val.append(ProteinQualityTarget(target, node_features=node_features, cutoff=cutoff))
    else:
        raise ValueError(f'Invalid data specification: {session["data"]}')

    if 'QUICK_RUN' in os.environ:
        print('QUICK RUN: limiting the train and val datasets to 5 targets each')
        targets_train = targets_train[:5]
        targets_val = targets_val[:5]

    if not set.isdisjoint(set(targets_train), set(targets_val)):
        raise ValueError("Found duplicate targets in train and val splits:",
                         *list(set.intersection(set(targets_train), set(targets_val))))

    if session['data']['in_memory']:
        for target in itertools.chain(targets_train, targets_val):
            target.load_eager()

    dataset_train = torch.utils.data.ConcatDataset(targets_train)
    dataset_val = torch.utils.data.ConcatDataset(targets_val)

    dataloader_kwargs = dict(
        num_workers=session['cpus'],
        pin_memory='cuda' in session['device'],
        worker_init_fn=lambda _: np.random.seed(int(torch.initial_seed()) % (2 ** 32 - 1)),
        batch_size=session['batch_size'],
        collate_fn=DecoyBatch.collate,
    )

    # TODO implement target-based sampler
    dataloader_train = torch.utils.data.DataLoader(dataset_train, shuffle=True, **dataloader_kwargs)
    dataloader_val = torch.utils.data.DataLoader(dataset_val, shuffle=False, **dataloader_kwargs)

    return dataloader_train, dataloader_val


dataloader_train, dataloader_val = get_dataloaders(ex, session)
session['misc']['samples'] = {'train': len(dataloader_train.dataset), 'val': len(dataloader_val.dataset)}

if ex['completed_epochs'] == 0:
    saver.save_experiment(ex, epoch=ex['completed_epochs'], samples=ex['samples'])
    logger.add_text('Experiment',
                    textwrap.indent(pyaml.dump(ex, safe=True, sort_dicts=False, force_embed=True), '    '),
                    global_step=ex['samples'])
# endregion


# region Training
def training_function(trainer, batch):
    batch = batch.to(session['device'])
    results = model(batch)

    loss_local_lddt = torch.tensor(0., device=session['device'])
    loss_global_gdtts = torch.tensor(0., device=session['device'])

    if ex['loss']['local_lddt']['weight'] > 0:
        node_mask = torch.isfinite(batch.lddt)
        loss_local_lddt = F.mse_loss(
            results.node_features[node_mask, features.Output.Node.LOCAL_LDDT],
            batch.lddt[node_mask], reduction='none'
        )
        loss_local_lddt = loss_local_lddt.mean()
        assert torch.isfinite(loss_local_lddt).item()

    if ex['loss']['global_gdtts']['weight'] > 0:
        loss_global_gdtts = F.mse_loss(
            results.global_features[:, features.Output.Global.GLOBAL_GDTTS],
            batch.gdtts, reduction='none'
        )
        loss_global_gdtts = loss_global_gdtts.mean()
        assert torch.isfinite(loss_global_gdtts).item()

    loss_total = (
        ex['loss']['local_lddt']['weight'] * loss_local_lddt +
        ex['loss']['global_gdtts']['weight'] * loss_global_gdtts
    )

    optimizer.zero_grad()
    loss_total.backward()
    optimizer.step(closure=None)

    return {
        'num_samples': len(batch),
        'results': results,
        'loss': {
            'total': loss_total.item(),
            'local_lddt': loss_local_lddt.item(),
            'global_gdtts': loss_global_gdtts.item(),
        },
    }


def validation_function(validator, batch):
    batch = batch.to(session['device'])
    results = model(batch)

    loss_local_lddt = torch.tensor(0.)
    loss_global_gdtts = torch.tensor(0.)

    if ex['loss']['local_lddt']['weight'] > 0:
        node_mask = torch.isfinite(batch.lddt)
        loss_local_lddt = F.mse_loss(
            results.node_features[node_mask, features.Output.Node.LOCAL_LDDT],
            batch.lddt[node_mask], reduction='none'
        )
        loss_local_lddt = loss_local_lddt.mean()

    if ex['loss']['global_gdtts']['weight'] > 0:
        loss_global_gdtts = F.mse_loss(
            results.global_features[:, features.Output.Global.GLOBAL_GDTTS],
            batch.gdtts, reduction='none'
        )
        loss_global_gdtts = loss_global_gdtts.mean()

    loss_total = (
        ex['loss']['local_lddt']['weight'] * loss_local_lddt +
        ex['loss']['global_gdtts']['weight'] * loss_global_gdtts
    )

    return {
        'num_samples': len(batch),
        'results': results,
        'loss': {
            'total': loss_total.item(),
            'local_lddt': loss_local_lddt.item(),
            'global_gdtts': loss_global_gdtts.item(),
        },
    }


def session_start(trainer: Engine, session: dict):
    session['status'] = 'RUNNING'
    session['datetime_started'] = datetime.utcnow()

    session_start_summary = make_session_start_summary(hparam_values={
        **{f'data/{k}': v for k, v in ex['data'].items()},
        **{f'optimizer/{k}': v for k, v in ex['optimizer'].items()},
        **{f'model/{k}': v for k, v in ex['model'].items()},
        **{f'loss/local_lddt/{k}': v for k, v in ex['loss']['local_lddt'].items()},
        **{f'loss/global_gdtts/{k}': v for k, v in ex['loss']['global_gdtts'].items()},
    })
    logger.file_writer.add_summary(session_start_summary)


def update_samples(trainer: Engine, ex, session):
    num_samples = trainer.state.output['num_samples']
    ex['samples'] += num_samples
    session['samples'] += num_samples


def update_completed_epochs(trainer, ex, session):
    ex['completed_epochs'] += 1
    session['completed_epochs'] += 1
    trainer.state.misc['epochs'] = ex['completed_epochs']


def save_model(trainer, model, ex, optimizer, session):
    if session['checkpoint'] > 0:
        if session['completed_epochs'] % session['checkpoint'] == 0 or \
                session['completed_epochs'] == session['max_epochs']:
            saver.save(model, ex, optimizer, epoch=ex['completed_epochs'], samples=ex['samples'])


trainer = Engine(training_function)
validator = Engine(validation_function)

losses_avg = ProteinAverageLosses(
    lambda o: (o['loss']['local_lddt'], o['loss']['global_gdtts'], o['num_samples']))
losses_avg.attach(validator)

ot = itemgetter('results')
local_lddt_metrics = LocalMetrics(features.Output.Node.LOCAL_LDDT, title='Local LDDT', output_transform=ot)
local_lddt_metrics.attach(trainer, 'local_lddt')
local_lddt_metrics.attach(validator, 'local_lddt')

global_gdtts_metrics = GlobalMetrics(features.Output.Global.GLOBAL_GDTTS, title='Global GDT_TS', output_transform=ot,
                                     figures=('hist', 'recall_at_k'))
global_gdtts_metrics.attach(trainer, 'global_gdtts')
global_gdtts_metrics.attach(validator, 'global_gdtts')

gpu_max_memory_allocated = GpuMaxMemoryAllocated()
gpu_max_memory_allocated.attach(trainer, 'gpu')
gpu_max_memory_allocated.attach(validator, 'gpu')

# During training and validation, the progress bar shows the average loss over all batches processed so far
pbar_train = ProgressBar(desc='Train')
pbar_train.attach(trainer, output_transform=lambda o: o['loss'])
pbar_val = ProgressBar(desc='Val')
pbar_val.attach(validator, output_transform=lambda o: o['loss'])


def log_losses_batch(engine, tag):
    """Log the losses for the current batch, to be called at every iteration"""
    for name, value in engine.state.output['loss'].items():
        if name == 'total' or ex['loss'][name]['weight'] > 0:
            logger.add_scalar(f'{tag}/loss/{name}', engine.state.output['loss'][name], global_step=ex['samples'])


def log_losses_avg(engine, tag):
    """Log the avg of the losses for the current epoch, to be called at the end of an epoch"""
    for name, value in engine.state.losses.items():
        if ex['loss'][name]['weight'] > 0:
            logger.add_scalar(f'{tag}/loss/{name}', value, global_step=ex['samples'])


def log_misc(engine, trainval_tag):
    for name, value in engine.state.misc.items():
        logger.add_scalar(f'misc/{name}/{trainval_tag}', value, global_step=ex['samples'])


def log_metrics(engine, trainval_tag):
    for name, value in engine.state.metrics.items():
        logger.add_scalar(f'{trainval_tag}/metric/{name}', value, global_step=ex['samples'])


def log_figures(engine, trainval_tag):
    for name, fig in engine.state.figures.items():
        logger.add_figure(f'{trainval_tag}/{name}', fig, global_step=ex['samples'], close=True)


def run_validation(trainer, validator, dataloader_val):
    validator.run(dataloader_val)


def session_end(trainer, session):
    session['status'] = 'COMPLETED'
    session['datetime_completed'] = datetime.utcnow()
    elapsed = session["datetime_completed"] - session["datetime_started"]

    print(f'Completed {session["completed_epochs"]} epochs in {round_timedelta(elapsed)} '
          f'({round_timedelta(elapsed / session["completed_epochs"])} per epoch)')

    if 'cuda' in session['device']:
        for device_id, device_info in session['cuda']['devices'].items():
            device_info.update({
                'memory_used_max': f'{torch.cuda.max_memory_allocated(device_id) // (10**6)} MiB',
                'memory_cached_max': f'{torch.cuda.max_memory_cached(device_id) // (10**6)} MiB',
            })
        print(pyaml.dump(session['cuda']['devices'], safe=True, sort_dicts=False), sep='\n')

    print(pyaml.dump(session['metric']))

    # Need to save again because we updated session and gpu info
    saver.save_experiment(ex, epoch=ex['completed_epochs'], samples=ex['samples'])

    # Log session end to tensorboard
    logger.add_text('Experiment',
                    textwrap.indent(pyaml.dump(ex, safe=True, sort_dicts=False, force_embed=True), '    '),
                    ex['samples'])
    session_end_summary = make_session_end_summary('SUCCESS')
    logger.file_writer.add_summary(session_end_summary)


trainer.add_event_handler(Events.STARTED, session_start, session)
trainer.add_event_handler(Events.STARTED, customize_state)
trainer.add_event_handler(Events.EPOCH_STARTED, setup_training, model)
trainer.add_event_handler(Events.EXCEPTION_RAISED, handle_failure, 'training', ex, session)

trainer.add_event_handler(Events.ITERATION_COMPLETED, update_samples, ex, session)
trainer.add_event_handler(Events.ITERATION_COMPLETED, log_losses_batch, 'train')

trainer.add_event_handler(Events.EPOCH_COMPLETED, update_completed_epochs, ex, session)
trainer.add_event_handler(Events.EPOCH_COMPLETED, log_misc, 'train')
trainer.add_event_handler(Events.EPOCH_COMPLETED, log_metrics, 'train')
trainer.add_event_handler(Events.EPOCH_COMPLETED, log_figures, 'train')
trainer.add_event_handler(Events.EPOCH_COMPLETED, update_metrics, session)
trainer.add_event_handler(Events.EPOCH_COMPLETED, flush_logger, logger)
trainer.add_event_handler(Events.EPOCH_COMPLETED, run_validation, validator, dataloader_val)

trainer.add_event_handler(Events.COMPLETED, session_end, session)

validator.add_event_handler(Events.STARTED, customize_state)
validator.add_event_handler(Events.EPOCH_STARTED, setup_validation, model)
trainer.add_event_handler(Events.EXCEPTION_RAISED, handle_failure, 'validation', ex, session, saver, logger)

validator.add_event_handler(Events.EPOCH_COMPLETED, log_losses_avg, 'val')
validator.add_event_handler(Events.EPOCH_COMPLETED, log_misc, 'val')
validator.add_event_handler(Events.EPOCH_COMPLETED, log_metrics, 'val')
validator.add_event_handler(Events.EPOCH_COMPLETED, log_figures, 'val')
validator.add_event_handler(Events.EPOCH_COMPLETED, flush_logger, logger)
validator.add_event_handler(Events.EPOCH_COMPLETED, update_metrics, session)
validator.add_event_handler(Events.EPOCH_COMPLETED, save_model, model, ex, optimizer, session)

trainer.run(dataloader_train, max_epochs=session['max_epochs'])
logger.close()
