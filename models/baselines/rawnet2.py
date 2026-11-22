"""RawNet2 baseline (PRD FR-M4) in the ASVspoof 2021 baseline layout, adapted to
AVA's window contract and training harness.

Structure (Tak et al., ICASSP 2021; ASVspoof 2021 LA/DF baseline code): fixed
mel-spaced sinc band-pass filters (20 x 1025 taps, Hamming window), |x|, max-pool 3,
batch norm, SELU; residual blocks [20,20] x2 and [20,128], [128,128] x3, each followed
by filter-wise feature-map scaling ``x * s + s`` with ``s = sigmoid(fc(avgpool(x)))``;
batch norm, SELU, 3-layer GRU (1024) over time, last step, FC 1024, FC 2.

Adaptations, all explicit: input is AVA's ``float32[B, 64000]`` window (the reference
used 64,600 samples); output is ``(logits[B, 2], embedding[B, 1024])`` in AVA class
order (synthetic, genuine) instead of log-softmax, so the shared cross-entropy and
evaluation code apply; there is no in-model normalization (the reference feeds raw
waveform). ``reference_quirks=True`` (default) keeps one behaviour of the published
code that two independent recollections agree on: a residual block's pre-activation
(BN + LeakyReLU) result is computed but unused (``conv1`` reads the block input). All
``out_channels`` sinc filters are built (the last spans the top mel band to Nyquist);
an earlier recollection that the reference leaves the last filter zero was disputed
and is not reproduced. No upstream source was available offline: verify both points
against the upstream commit before reporting baseline numbers. Not trained here.

``RAWNET2_TRAIN_CONFIG`` holds the reference training setup as recalled (Adam-family
at 1e-4 for every layer, weight decay 1e-4, class weights 0.1 synthetic / 0.9 genuine,
nothing frozen); the shared harness applies AdamW and exempts biases/norm weights
from decay, a small documented difference.
"""
import math

import numpy as np
import torch
from torch.nn import functional as F

from models.ava.model import SAMPLE_RATE, WINDOW_SAMPLES
from models.ava.train import TrainConfig

RAWNET2_TRAIN_CONFIG = TrainConfig(backbone_lr=1e-4, head_lr=1e-4, weight_decay=1e-4,
                                   freeze_feature_encoder_epochs=0, class_weights=(0.1, 0.9))

REFERENCE_CONFIG = dict(sinc_channels=20, sinc_kernel=1024, blocks=((20, 20), (20, 20), (20, 128),
                                                                    (128, 128), (128, 128), (128, 128)),
                        gru_hidden=1024, gru_layers=3, fc_hidden=1024)


def to_mel(hz):
    return 2595 * np.log10(1 + hz / 700)


def to_hz(mel):
    return 700 * (10 ** (mel / 2595) - 1)


def sinc_filterbank(out_channels, kernel_size, sample_rate=SAMPLE_RATE):
    """Mel-spaced Hamming-windowed sinc band-pass filters, ``[out, 1, kernel]``.

    Band edges: ``out + 1`` points evenly spaced in mel between the mel values of 0 Hz
    and Nyquist (on a 257-point grid). Filter ``i`` passes ``edges[i]..edges[i+1]``.
    Returns the filters and the ``out`` lower band edges.
    """
    if type(out_channels) is not int or out_channels < 2:
        raise ValueError('out_channels must be an integer >= 2')
    if type(kernel_size) is not int or kernel_size < 3:
        raise ValueError('kernel_size must be an integer >= 3')
    if kernel_size % 2 == 0:
        kernel_size += 1  # reference: force an odd, centred kernel
    grid = int(sample_rate / 2) * np.linspace(0, 1, 257)
    mel = to_mel(grid)
    edges = to_hz(np.linspace(mel.min(), mel.max(), out_channels + 1))
    support = np.arange(-(kernel_size - 1) / 2, (kernel_size - 1) / 2 + 1)
    window = np.hamming(kernel_size)
    bank = np.zeros((out_channels, kernel_size))
    for i in range(out_channels):
        low, high = edges[i], edges[i + 1]
        ideal = (2 * high / sample_rate) * np.sinc(2 * high * support / sample_rate) \
            - (2 * low / sample_rate) * np.sinc(2 * low * support / sample_rate)
        bank[i] = window * ideal
    return torch.tensor(bank, dtype=torch.float32).unsqueeze(1), edges[:out_channels]


class SincConv(torch.nn.Module):
    """Fixed (non-trainable) sinc filter bank; a buffer, not a parameter."""

    def __init__(self, out_channels, kernel_size):
        super().__init__()
        filters, self.freq = sinc_filterbank(out_channels, kernel_size)
        self.register_buffer('filters', filters)

    def forward(self, x):
        return F.conv1d(x, self.filters)


class ResidualBlock(torch.nn.Module):
    def __init__(self, in_channels, out_channels, first=False, reference_quirks=True):
        super().__init__()
        self.first, self.reference_quirks = first, reference_quirks
        if not first:
            self.bn1 = torch.nn.BatchNorm1d(in_channels)
        self.lrelu = torch.nn.LeakyReLU(negative_slope=0.3)
        self.conv1 = torch.nn.Conv1d(in_channels, out_channels, kernel_size=3, padding=1)
        self.bn2 = torch.nn.BatchNorm1d(out_channels)
        self.conv2 = torch.nn.Conv1d(out_channels, out_channels, kernel_size=3, padding=1)
        self.downsample = (torch.nn.Conv1d(in_channels, out_channels, kernel_size=1)
                           if in_channels != out_channels else None)
        self.pool = torch.nn.MaxPool1d(3)

    def forward(self, x):
        identity = x
        out = x if self.first else self.lrelu(self.bn1(x))
        out = self.conv1(x if self.reference_quirks else out)
        out = self.conv2(self.lrelu(self.bn2(out)))
        if self.downsample is not None:
            identity = self.downsample(identity)
        return self.pool(out + identity)


class FeatureMapScaling(torch.nn.Module):
    """Filter-wise FMS: ``x * s + s`` with ``s = sigmoid(fc(mean_t(x)))``."""

    def __init__(self, channels):
        super().__init__()
        self.fc = torch.nn.Linear(channels, channels)

    def forward(self, x):
        scale = torch.sigmoid(self.fc(x.mean(dim=2))).unsqueeze(2)
        return x * scale + scale


class RawNet2Body(torch.nn.Module):
    """Sinc front end through GRU last step: ``[B, 64000] -> [B, gru_hidden]``.

    ``feature_encoder`` (the fixed sinc filters) exists for harness compatibility;
    it has no parameters, so freezing it is a no-op.
    """

    def __init__(self, sinc_channels, sinc_kernel, blocks, gru_hidden, gru_layers, reference_quirks=True):
        super().__init__()
        if (not blocks or any(len(pair) != 2 or any(type(c) is not int or c < 1 for c in pair)
                              for pair in blocks)):
            raise ValueError('Residual blocks must be (in, out) pairs of positive integers')
        if blocks[0][0] != sinc_channels or any(a[1] != b[0] for a, b in zip(blocks, blocks[1:])):
            raise ValueError('Residual block channels must chain from the sinc channel count')
        self.sinc = SincConv(sinc_channels, sinc_kernel)
        self.first_bn = torch.nn.BatchNorm1d(sinc_channels)
        self.blocks = torch.nn.ModuleList(
            ResidualBlock(i, o, first=index == 0, reference_quirks=reference_quirks)
            for index, (i, o) in enumerate(blocks))
        self.scalings = torch.nn.ModuleList(FeatureMapScaling(o) for _, o in blocks)
        self.bn_before_gru = torch.nn.BatchNorm1d(blocks[-1][1])
        self.gru = torch.nn.GRU(blocks[-1][1], gru_hidden, num_layers=gru_layers, batch_first=True)
        self.output_dim = gru_hidden

    @property
    def feature_encoder(self):
        return self.sinc

    def forward(self, waveform):
        x = self.sinc(waveform.unsqueeze(1))
        x = F.selu(self.first_bn(F.max_pool1d(torch.abs(x), 3)))
        for block, scaling in zip(self.blocks, self.scalings):
            x = scaling(block(x))
        x = F.selu(self.bn_before_gru(x)).permute(0, 2, 1)
        if x.shape[1] < 1:
            raise ValueError('Input too short for the RawNet2 pooling stack')
        output, _ = self.gru(x)
        return output[:, -1, :]


class RawNet2Head(torch.nn.Module):
    def __init__(self, in_features, fc_hidden, num_classes=2):
        super().__init__()
        self.fc1 = torch.nn.Linear(in_features, fc_hidden)
        self.fc2 = torch.nn.Linear(fc_hidden, num_classes)

    def forward(self, features):
        embedding = self.fc1(features)
        return self.fc2(embedding), embedding


class RawNet2(torch.nn.Module):
    """``float32[B, 64000] -> (logits[B, 2], embedding[B, fc_hidden])`` (synthetic, genuine)."""

    def __init__(self, config=None, reference_quirks=True):
        super().__init__()
        config = dict(REFERENCE_CONFIG, **(config or {}))
        if set(config) != set(REFERENCE_CONFIG):
            raise ValueError(f'Unknown RawNet2 configuration keys: {sorted(set(config) - set(REFERENCE_CONFIG))}')
        for key in ('sinc_channels', 'sinc_kernel', 'gru_hidden', 'gru_layers', 'fc_hidden'):
            if type(config[key]) is not int or config[key] < 1:
                raise ValueError(f'{key} must be a positive integer')
        if not isinstance(config['blocks'], (list, tuple)):
            raise ValueError('blocks must be a sequence of (in, out) pairs')
        if config['sinc_kernel'] >= WINDOW_SAMPLES or frames_after_pooling(config) < 1:
            raise ValueError('Configuration leaves no GRU frames for a 64,000-sample window')
        self.config = config
        self.backbone = RawNet2Body(config['sinc_channels'], config['sinc_kernel'], tuple(config['blocks']),
                                    config['gru_hidden'], config['gru_layers'], reference_quirks)
        self.head = RawNet2Head(config['gru_hidden'], config['fc_hidden'])

    def forward(self, waveform):
        if not isinstance(waveform, torch.Tensor) or waveform.dtype != torch.float32:
            raise ValueError('Expected a float32 tensor')
        if waveform.ndim != 2 or waveform.shape[0] < 1 or waveform.shape[1] != WINDOW_SAMPLES:
            raise ValueError(f'Expected shape [B, {WINDOW_SAMPLES}], got {tuple(waveform.shape)}')
        if not torch.isfinite(waveform).all():
            raise ValueError('Waveform contains non-finite samples')
        return self.head(self.backbone(waveform))


def frames_after_pooling(config=None, samples=WINDOW_SAMPLES):
    """GRU sequence length for a window: sinc 'valid' conv, then max-pool 3 per stage."""
    config = dict(REFERENCE_CONFIG, **(config or {}))
    kernel = config['sinc_kernel'] + (1 - config['sinc_kernel'] % 2)
    length = samples - kernel + 1
    for _ in range(1 + len(config['blocks'])):
        length = math.floor(length / 3)
    return length
