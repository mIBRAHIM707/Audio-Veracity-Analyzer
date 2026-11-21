"""Training step, evaluation and layer-sweep harness for tiny CPU smoke runs (backlog 9.1).

Implements the architecture §4.1/§4.2 training contract: AdamW with a small backbone
learning rate (2e-5) and a larger head learning rate (1e-3), the CNN feature encoder
frozen for the first epochs, and class-weighted cross-entropy. It does not load data,
choose a layer cut or establish accuracy; full training is out of the daily window.
Batches are ``(float32[B, 64000], int64[B])`` with labels in CLASS_ORDER indices.
"""
from dataclasses import dataclass
import math
import statistics
import time

import torch
from torch.nn import functional as F


TIMING_REPEATS = 5  # median of this many warm one-window forwards per cut


def _positive_finite(value):
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value) and value > 0)


@dataclass(frozen=True)
class TrainConfig:
    epochs: int = 1
    backbone_lr: float = 2e-5
    head_lr: float = 1e-3
    freeze_feature_encoder_epochs: int = 1
    class_weights: tuple = (1.0, 1.0)
    weight_decay: float = 0.0
    max_grad_norm: float | None = None
    seed: int = 0

    def __post_init__(self):
        problems = []
        if type(self.epochs) is not int or self.epochs < 1:
            problems.append('epochs must be an integer >= 1')
        for name in ('backbone_lr', 'head_lr'):
            if not _positive_finite(getattr(self, name)):
                problems.append(f'{name} must be a finite number > 0')
        if type(self.freeze_feature_encoder_epochs) is not int or self.freeze_feature_encoder_epochs < 0:
            problems.append('freeze_feature_encoder_epochs must be an integer >= 0')
        if (not isinstance(self.class_weights, tuple) or len(self.class_weights) != 2
                or not all(_positive_finite(value) for value in self.class_weights)):
            problems.append('class_weights must be a tuple of two finite numbers > 0')
        if (isinstance(self.weight_decay, bool) or not isinstance(self.weight_decay, (int, float))
                or not math.isfinite(self.weight_decay) or self.weight_decay < 0):
            problems.append('weight_decay must be a finite number >= 0')
        if self.max_grad_norm is not None and not _positive_finite(self.max_grad_norm):
            problems.append('max_grad_norm must be None or a finite number > 0')
        if type(self.seed) is not int or self.seed < 0:
            problems.append('seed must be an integer >= 0')
        if problems:
            raise ValueError('; '.join(problems))


def build_optimizer(model, config):
    """AdamW: backbone and head learning rates; biases, norm weights and SLS layer
    logits (every parameter with at most one dimension) are never weight-decayed,
    so decay cannot pull the layer weighting back to uniform."""
    backbone = list(model.backbone.parameters())
    head = list(model.head.parameters())
    if {id(p) for p in backbone} & {id(p) for p in head}:
        raise ValueError('Backbone and head share parameters')
    if len(backbone) + len(head) != len(list(model.parameters())):
        raise ValueError('Model has parameters outside the backbone and head')
    groups = []
    for name, params, lr in (('backbone', backbone, config.backbone_lr), ('head', head, config.head_lr)):
        groups.append({'params': [p for p in params if p.ndim > 1], 'lr': lr,
                       'weight_decay': config.weight_decay, 'name': name})
        groups.append({'params': [p for p in params if p.ndim <= 1], 'lr': lr,
                       'weight_decay': 0.0, 'name': name + '_no_decay'})
    return torch.optim.AdamW([group for group in groups if group['params']])


def set_feature_encoder_trainable(model, trainable):
    for parameter in model.backbone.feature_encoder.parameters():
        parameter.requires_grad_(trainable)


def _check_batch(waveform, labels):
    if (not isinstance(labels, torch.Tensor) or labels.dtype != torch.int64 or labels.ndim != 1
            or not isinstance(waveform, torch.Tensor) or waveform.ndim < 1
            or labels.shape[0] != waveform.shape[0]):
        raise ValueError('Expected int64 labels shaped [B] matching the waveform batch')
    if not bool(((labels == 0) | (labels == 1)).all()):
        raise ValueError('Labels must be 0 (synthetic) or 1 (genuine)')


def _loss(model, waveform, labels, weights):
    _check_batch(waveform, labels)
    logits, _ = model(waveform)
    loss = F.cross_entropy(logits, labels, weight=weights)
    if not torch.isfinite(loss):
        raise FloatingPointError('Non-finite training loss')
    return loss


def train_epoch(model, optimizer, batches, config, epoch):
    """One pass over ``batches``; the feature encoder is frozen while epoch < freeze epochs."""
    trainable = epoch >= config.freeze_feature_encoder_epochs
    set_feature_encoder_trainable(model, trainable)
    model.train()
    weights = torch.tensor(config.class_weights, dtype=torch.float32)
    losses = []
    for waveform, labels in batches:
        optimizer.zero_grad(set_to_none=True)
        loss = _loss(model, waveform, labels, weights)
        loss.backward()
        if config.max_grad_norm is not None:
            torch.nn.utils.clip_grad_norm_(model.parameters(), config.max_grad_norm)
        optimizer.step()
        losses.append(loss.item())
    if not losses:
        raise ValueError('Epoch received no batches')
    return {'epoch': epoch, 'steps': len(losses), 'mean_loss': sum(losses) / len(losses),
            'step_losses': losses, 'feature_encoder_trainable': trainable}


def evaluate(model, batches, config):
    """Class-weighted cross-entropy over the whole evaluation set (not a mean of
    per-batch means), in eval and inference mode."""
    model.eval()
    weights = torch.tensor(config.class_weights, dtype=torch.float32)
    loss_sum, weight_sum, windows = 0.0, 0.0, 0
    with torch.inference_mode():
        for waveform, labels in batches:
            _check_batch(waveform, labels)
            logits, _ = model(waveform)
            loss_sum += F.cross_entropy(logits, labels, weight=weights, reduction='sum').item()
            weight_sum += weights[labels].sum().item()
            windows += labels.shape[0]
    if not windows:
        raise ValueError('Evaluation received no batches')
    loss = loss_sum / weight_sum
    if not math.isfinite(loss):
        raise FloatingPointError('Non-finite evaluation loss')
    return {'mean_loss': loss, 'windows': windows}


def run_training(model, config, epoch_batches):
    """Train for ``config.epochs``; ``epoch_batches(epoch)`` returns that epoch's batches.

    The feature encoder is made trainable again on return, so a freeze period
    longer than the run never leaves the model silently frozen.
    """
    torch.manual_seed(config.seed)
    optimizer = build_optimizer(model, config)
    try:
        return [train_epoch(model, optimizer, epoch_batches(epoch), config, epoch)
                for epoch in range(config.epochs)]
    finally:
        set_feature_encoder_trainable(model, True)


def layer_sweep(kept_layers, make_model, config, epoch_batches, eval_batches):
    """Train and evaluate one model per cut (PRD task 2.4 harness).

    ``make_model(kept)`` builds a fresh detector; each cut starts from the same seed.
    Timings are single-process CPU wall-clock for this run's model, not capacity
    evidence; losses on smoke data say nothing about detection accuracy.
    """
    if (not isinstance(kept_layers, (list, tuple)) or not kept_layers
            or any(type(k) is not int or k < 1 for k in kept_layers)
            or len(set(kept_layers)) != len(kept_layers)):
        raise ValueError('kept_layers must be a non-empty list of distinct positive integers')
    rows = []
    for kept in kept_layers:
        torch.manual_seed(config.seed)
        model = make_model(kept)
        if model.backbone.kept_layers != kept:
            raise ValueError(f'make_model({kept}) returned {model.backbone.kept_layers} layers')
        history = run_training(model, config, epoch_batches)
        evaluation = evaluate(model, eval_batches(), config)
        window = next(iter(eval_batches()))[0][:1]
        timings = []
        with torch.inference_mode():
            model(window)  # warm-up call, not timed
            for _ in range(TIMING_REPEATS):
                start = time.perf_counter()
                model(window)
                timings.append(time.perf_counter() - start)
        seconds = statistics.median(timings)
        weights = model.head.layer_weights().tolist() if hasattr(model.head, 'layer_weights') else None
        rows.append({'kept_layers': kept,
                     'parameters': sum(p.numel() for p in model.parameters()),
                     'history': [{k: v for k, v in epoch.items() if k != 'step_losses'} for epoch in history],
                     'eval': evaluation, 'forward_seconds_one_window': seconds,
                     'layer_weights': weights})
    return {'rows': rows, 'config': config.__dict__ | {'class_weights': list(config.class_weights)},
            'limitations': 'Smoke harness only: no layer cut chosen, no accuracy or CPU capacity evidence.'}
