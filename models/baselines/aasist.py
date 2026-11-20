# Derived from AASIST (https://github.com/clovaai/aasist), whose notice follows:
#
# Copyright (c) 2021-present NAVER Corp.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.  IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.
"""AASIST baseline (PRD FR-M4) adapted to AVA's window contract and training harness.

Written against the reference implementation, clovaai/aasist (MIT, NAVER Corp.),
commit a04c9863f63d44471dde8a6abcb3b082b07cd1d1, ``models/AASIST.py`` and
``config/AASIST.conf`` (Jung et al., "AASIST: Audio Anti-Spoofing using Integrated
Spectro-Temporal Graph Attention Networks", ICASSP 2022). The upstream source is not
copied into this repository; it is cloned outside it for the parity test
(``tests/model/test_aasist.py``, ``AVA_AASIST_UPSTREAM``).

Structure: fixed mel-spaced sinc filters (70 x 129 taps) over the waveform, |x| and a
3x3 max-pool over (filter, time), BatchNorm2d + SELU; six 2-D residual blocks
(``filts``), each pooling time by 3; spectral nodes (max over time, plus a learned
position embedding) and temporal nodes (max over frequency) each through a graph
attention layer and a graph pool; two heterogeneous spectro-temporal branches, each
with its own learned master node, a heterogeneous GAT, graph pools and a second
heterogeneous GAT added residually; element-wise max of the branches; readout
``[max|T|, mean T, max|S|, mean S, master]`` (5 x ``gat_dims[1]``); dropout 0.5; FC 2.

Behaviour kept from the reference, deliberately (checked by the parity or contract
tests; the last point by reading ``main.py``):

- a residual block's pre-activation (BN + SELU) is computed but ``conv1`` reads the
  block input, as upstream;
- the heterogeneous attention uses ``att_weight12`` for both cross-type blocks;
- ``pool_ratios[3]`` and ``temperatures[3]`` are accepted but unused (both
  second-branch pools use ``pool_ratios[2]``; every heterogeneous layer uses
  ``temperatures[2]``);
- the frequency-mask augmentation (``Freq_aug``) is not implemented: the reference
  configuration leaves it off (``main.py`` defaults ``freq_aug`` to False).

Sizes are trusted developer input, not runtime input: the attention tensors are
``[B, N, N, D]``, so memory grows with the square of the node count.

Adaptations, all explicit: input is AVA's ``float32[B, 64000]`` window (the reference
uses 64,600 samples; both give 29 temporal nodes); output is ``(logits[B, 2],
embedding[B, 5 * gat_dims[1]])``; the reference's class order (0 spoof, 1 bona fide)
already equals AVA's (synthetic, genuine). The embedding is the readout before the
0.5 dropout (upstream returns it after; identical in eval mode). The upstream
hard-coded 23 spectral position rows are derived as ``filts[0] // 3`` (23 for 70
filters), so other sizes work and the reference state dict still loads. Parameters
live under ``backbone.`` and ``head.`` for the shared harness;
``load_reference_state_dict`` maps upstream keys. No in-model normalization. Not
trained here; no accuracy is claimed.

``AASIST_TRAIN_CONFIG`` copies the reference values onto the shared harness (learning
rate 1e-4 everywhere, weight decay 1e-4, class weights 0.1 synthetic / 0.9 genuine,
nothing frozen). The decay value is copied, not its strength: the reference adds
1e-4 x weight to the gradient (Adam with coupled L2, on every parameter), while the
harness's AdamW shrinks weights by lr x 1e-4 = 1e-8 per step and skips biases and
norm weights. Other differences, recorded in ``REFERENCE_TRAINING``: a per-step cosine
schedule to 5e-6, batch 24, 100 epochs and SWA over the best epochs; the harness uses
constant learning rates.
"""
import math

import numpy as np
import torch
from torch.nn import functional as F

from models.ava.model import SAMPLE_RATE, WINDOW_SAMPLES
from models.ava.train import TrainConfig

UPSTREAM = 'clovaai/aasist@a04c9863f63d44471dde8a6abcb3b082b07cd1d1 (MIT)'

AASIST_TRAIN_CONFIG = TrainConfig(backbone_lr=1e-4, head_lr=1e-4, weight_decay=1e-4,
                                  freeze_feature_encoder_epochs=0, class_weights=(0.1, 0.9))

REFERENCE_TRAINING = dict(optimizer='adam', base_lr=1e-4, lr_min=5e-6, betas=(0.9, 0.999),
                          weight_decay=1e-4, weight_decay_mode='coupled L2, every parameter',
                          scheduler='cosine per step', batch_size=24,
                          num_epochs=100, class_weights=(0.1, 0.9), swa=True, freq_aug=False)

# config/AASIST.conf "model_config" (nb_samp dropped: AVA windows are 64,000 samples).
REFERENCE_CONFIG = dict(first_conv=128, filts=(70, (1, 32), (32, 32), (32, 64), (64, 64)),
                        gat_dims=(64, 32), pool_ratios=(0.5, 0.7, 0.5, 0.5),
                        temperatures=(2.0, 2.0, 100.0, 100.0))
# config/AASIST-L.conf: the light variant (the upstream README states 85,306 parameters).
LIGHT_CONFIG = dict(first_conv=128, filts=(70, (1, 32), (32, 32), (32, 24), (24, 24)),
                    gat_dims=(24, 32), pool_ratios=(0.4, 0.5, 0.7, 0.5),
                    temperatures=(2.0, 2.0, 100.0, 100.0))


def to_mel(hz):
    return 2595 * np.log10(1 + hz / 700)


def to_hz(mel):
    return 700 * (10 ** (mel / 2595) - 1)


def sinc_filterbank(out_channels, kernel_size, sample_rate=SAMPLE_RATE):
    """``[out, kernel']`` mel-spaced Hamming-windowed sinc band-pass filters, kernel
    forced odd, as the reference ``CONV`` builds them (float32, from float64 taps)."""
    if type(out_channels) is not int or out_channels < 2:
        raise ValueError('out_channels must be an integer >= 2')
    if type(kernel_size) is not int or kernel_size < 2:
        raise ValueError('kernel_size must be an integer >= 2')
    if kernel_size % 2 == 0:
        kernel_size += 1
    grid = int(sample_rate / 2) * np.linspace(0, 1, 257)
    mel = to_mel(grid)
    edges = to_hz(np.linspace(np.min(mel), np.max(mel), out_channels + 1))
    # The same mixed torch/NumPy expression as the reference, so the float32 taps match
    # it exactly: the support is a float32 tensor, the sinc is evaluated by NumPy.
    support = torch.arange(-(kernel_size - 1) / 2, (kernel_size - 1) / 2 + 1)
    window = torch.tensor(np.hamming(kernel_size), dtype=torch.float32)
    bank = torch.zeros(out_channels, kernel_size)
    for i in range(out_channels):
        low, high = edges[i], edges[i + 1]
        ideal = ((2 * high / sample_rate) * np.sinc(2 * high * support / sample_rate)
                 - (2 * low / sample_rate) * np.sinc(2 * low * support / sample_rate))
        bank[i] = window * torch.tensor(np.asarray(ideal), dtype=torch.float32)
    return bank, edges


class SincConv(torch.nn.Module):
    """Fixed sinc filters; a non-persistent buffer (the reference keeps them out of
    the state dict too), so the feature encoder has no parameters."""

    def __init__(self, out_channels, kernel_size):
        super().__init__()
        bank, self.edges = sinc_filterbank(out_channels, kernel_size)
        self.register_buffer('filters', bank.unsqueeze(1), persistent=False)

    def forward(self, x):
        return F.conv1d(x, self.filters)


def _xavier_parameter(*size):
    parameter = torch.nn.Parameter(torch.empty(*size))
    torch.nn.init.xavier_normal_(parameter)
    return parameter


def _batch_norm_nodes(bn, x):
    shape = x.shape
    return bn(x.reshape(-1, shape[-1])).reshape(shape)


def _pairwise(x):
    nodes = x.shape[1]
    x = x.unsqueeze(2).expand(-1, -1, nodes, -1)
    return x * x.transpose(1, 2)


class GraphAttentionLayer(torch.nn.Module):
    def __init__(self, in_dim, out_dim, temperature=1.0):
        super().__init__()
        self.att_proj = torch.nn.Linear(in_dim, out_dim)
        self.att_weight = _xavier_parameter(out_dim, 1)
        self.proj_with_att = torch.nn.Linear(in_dim, out_dim)
        self.proj_without_att = torch.nn.Linear(in_dim, out_dim)
        self.bn = torch.nn.BatchNorm1d(out_dim)
        self.input_drop = torch.nn.Dropout(p=0.2)
        self.temp = temperature

    def forward(self, x):
        x = self.input_drop(x)
        att = torch.matmul(torch.tanh(self.att_proj(_pairwise(x))), self.att_weight) / self.temp
        att = F.softmax(att, dim=-2)
        x = self.proj_with_att(torch.matmul(att.squeeze(-1), x)) + self.proj_without_att(x)
        return F.selu(_batch_norm_nodes(self.bn, x))


class HtrgGraphAttentionLayer(torch.nn.Module):
    """Heterogeneous GAT over type-1 (temporal) and type-2 (spectral) nodes plus a
    master node updated by its own attention."""

    def __init__(self, in_dim, out_dim, temperature=1.0):
        super().__init__()
        self.proj_type1 = torch.nn.Linear(in_dim, in_dim)
        self.proj_type2 = torch.nn.Linear(in_dim, in_dim)
        self.att_proj = torch.nn.Linear(in_dim, out_dim)
        self.att_projM = torch.nn.Linear(in_dim, out_dim)
        self.att_weight11 = _xavier_parameter(out_dim, 1)
        self.att_weight22 = _xavier_parameter(out_dim, 1)
        self.att_weight12 = _xavier_parameter(out_dim, 1)
        self.att_weightM = _xavier_parameter(out_dim, 1)
        self.proj_with_att = torch.nn.Linear(in_dim, out_dim)
        self.proj_without_att = torch.nn.Linear(in_dim, out_dim)
        self.proj_with_attM = torch.nn.Linear(in_dim, out_dim)
        self.proj_without_attM = torch.nn.Linear(in_dim, out_dim)
        self.bn = torch.nn.BatchNorm1d(out_dim)
        self.input_drop = torch.nn.Dropout(p=0.2)
        self.temp = temperature

    def forward(self, x1, x2, master):
        n1, n2 = x1.shape[1], x2.shape[1]
        x = torch.cat([self.proj_type1(x1), self.proj_type2(x2)], dim=1)
        x = self.input_drop(x)
        projected = torch.tanh(self.att_proj(_pairwise(x)))
        att = torch.cat([
            torch.cat([projected[:, :n1, :n1] @ self.att_weight11,
                       projected[:, :n1, n1:] @ self.att_weight12], dim=2),
            torch.cat([projected[:, n1:, :n1] @ self.att_weight12,  # reference: 12, not a 21
                       projected[:, n1:, n1:] @ self.att_weight22], dim=2)], dim=1)
        att = F.softmax(att / self.temp, dim=-2)
        master_att = torch.matmul(torch.tanh(self.att_projM(x * master)), self.att_weightM) / self.temp
        master_att = F.softmax(master_att, dim=-2)
        master = (self.proj_with_attM(torch.matmul(master_att.squeeze(-1).unsqueeze(1), x))
                  + self.proj_without_attM(master))
        x = self.proj_with_att(torch.matmul(att.squeeze(-1), x)) + self.proj_without_att(x)
        x = F.selu(_batch_norm_nodes(self.bn, x))
        return x.narrow(1, 0, n1), x.narrow(1, n1, n2), master


class GraphPool(torch.nn.Module):
    """Keeps the top ``max(int(n * k), 1)`` nodes by sigmoid score, scaled by it."""

    def __init__(self, k, in_dim, p=0.3):
        super().__init__()
        self.k = k
        self.proj = torch.nn.Linear(in_dim, 1)
        self.drop = torch.nn.Dropout(p=p) if p > 0 else torch.nn.Identity()

    def forward(self, h):
        scores = torch.sigmoid(self.proj(self.drop(h)))
        keep = max(int(h.shape[1] * self.k), 1)
        _, index = torch.topk(scores, keep, dim=1)
        return torch.gather(h * scores, 1, index.expand(-1, -1, h.shape[2]))


class ResidualBlock(torch.nn.Module):
    """2-D residual block over (filter, time); pools time by 3."""

    def __init__(self, in_channels, out_channels, first=False):
        super().__init__()
        self.first = first
        if not first:
            self.bn1 = torch.nn.BatchNorm2d(in_channels)
        self.conv1 = torch.nn.Conv2d(in_channels, out_channels, kernel_size=(2, 3), padding=(1, 1))
        self.bn2 = torch.nn.BatchNorm2d(out_channels)
        self.conv2 = torch.nn.Conv2d(out_channels, out_channels, kernel_size=(2, 3), padding=(0, 1))
        self.downsample = in_channels != out_channels
        if self.downsample:
            self.conv_downsample = torch.nn.Conv2d(in_channels, out_channels, kernel_size=(1, 3), padding=(0, 1))
        self.mp = torch.nn.MaxPool2d((1, 3))

    def forward(self, x):
        if not self.first and self.training:
            # Reference quirk: the pre-activation is computed and discarded (conv1 reads
            # x). Only its BatchNorm running statistics change, so it runs only in training.
            self.bn1(x)
        out = self.conv2(F.selu(self.bn2(self.conv1(x))))
        identity = self.conv_downsample(x) if self.downsample else x
        return self.mp(out + identity)


def _check_config(config):
    filts, gat_dims = config['filts'], config['gat_dims']
    if type(config['first_conv']) is not int or config['first_conv'] < 2:
        raise ValueError('first_conv must be an integer >= 2')
    if (not isinstance(filts, (list, tuple)) or len(filts) != 5 or type(filts[0]) is not int
            or filts[0] < 3):
        raise ValueError('filts must be (sinc filters >= 3, four (in, out) pairs)')
    pairs = filts[1:]
    if any(not isinstance(p, (list, tuple)) or len(p) != 2 or any(type(c) is not int or c < 1 for c in p)
           for p in pairs):
        raise ValueError('filts pairs must be (in, out) positive integers')
    if pairs[0][0] != 1 or any(a[1] != b[0] for a, b in zip(pairs, pairs[1:])) or pairs[3][0] != pairs[3][1]:
        raise ValueError('filts pairs must chain from 1 channel and end with an (n, n) pair')
    if (not isinstance(gat_dims, (list, tuple)) or len(gat_dims) != 2
            or any(type(d) is not int or d < 1 for d in gat_dims)):
        raise ValueError('gat_dims must be two positive integers')
    for key in ('pool_ratios', 'temperatures'):
        values = config[key]
        if (not isinstance(values, (list, tuple)) or len(values) != 4
                or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0
                       for v in values)):
            raise ValueError(f'{key} must be four finite numbers > 0')
    if any(v > 1 for v in config['pool_ratios']):
        raise ValueError('pool_ratios must be in (0, 1]')
    if temporal_nodes(config) < 1:
        raise ValueError('Configuration leaves no temporal nodes for a 64,000-sample window')


class AASISTBody(torch.nn.Module):
    """Waveform to the readout ``[B, 5 * gat_dims[1]]``; the upstream names minus ``out_layer``."""

    def __init__(self, config):
        super().__init__()
        filts, gat_dims = config['filts'], config['gat_dims']
        pool, temps = config['pool_ratios'], config['temperatures']
        channels = filts[-1][-1]
        self.conv_time = SincConv(filts[0], config['first_conv'])
        self.first_bn = torch.nn.BatchNorm2d(1)
        self.encoder = torch.nn.Sequential(*(
            torch.nn.Sequential(ResidualBlock(*pair, first=index == 0))
            for index, pair in enumerate((filts[1], filts[2], filts[3], filts[4], filts[4], filts[4]))))
        self.pos_S = torch.nn.Parameter(torch.randn(1, filts[0] // 3, channels))
        self.master1 = torch.nn.Parameter(torch.randn(1, 1, gat_dims[0]))
        self.master2 = torch.nn.Parameter(torch.randn(1, 1, gat_dims[0]))
        self.GAT_layer_S = GraphAttentionLayer(channels, gat_dims[0], temperature=temps[0])
        self.GAT_layer_T = GraphAttentionLayer(channels, gat_dims[0], temperature=temps[1])
        self.HtrgGAT_layer_ST11 = HtrgGraphAttentionLayer(gat_dims[0], gat_dims[1], temperature=temps[2])
        self.HtrgGAT_layer_ST12 = HtrgGraphAttentionLayer(gat_dims[1], gat_dims[1], temperature=temps[2])
        self.HtrgGAT_layer_ST21 = HtrgGraphAttentionLayer(gat_dims[0], gat_dims[1], temperature=temps[2])
        self.HtrgGAT_layer_ST22 = HtrgGraphAttentionLayer(gat_dims[1], gat_dims[1], temperature=temps[2])
        self.pool_S = GraphPool(pool[0], gat_dims[0])
        self.pool_T = GraphPool(pool[1], gat_dims[0])
        self.pool_hS1 = GraphPool(pool[2], gat_dims[1])
        self.pool_hT1 = GraphPool(pool[2], gat_dims[1])
        self.pool_hS2 = GraphPool(pool[2], gat_dims[1])  # reference: pool_ratios[2], [3] unused
        self.pool_hT2 = GraphPool(pool[2], gat_dims[1])
        self.drop_way = torch.nn.Dropout(0.2)
        self.output_dim = 5 * gat_dims[1]

    @property
    def feature_encoder(self):
        return self.conv_time

    def _branch(self, first, second, out_T, out_S, master, pool_S, pool_T):
        out_T1, out_S1, master1 = first(out_T, out_S, master)
        out_S1, out_T1 = pool_S(out_S1), pool_T(out_T1)
        aug_T, aug_S, aug_master = second(out_T1, out_S1, master1)
        return out_T1 + aug_T, out_S1 + aug_S, master1 + aug_master

    def forward(self, waveform):
        x = self.conv_time(waveform.unsqueeze(1)).unsqueeze(1)
        x = F.selu(self.first_bn(F.max_pool2d(torch.abs(x), (3, 3))))
        e = self.encoder(x)  # [B, C, spectral, temporal]
        e_S = torch.max(torch.abs(e), dim=3)[0].transpose(1, 2) + self.pos_S
        out_S = self.pool_S(self.GAT_layer_S(e_S))
        e_T = torch.max(torch.abs(e), dim=2)[0].transpose(1, 2)
        out_T = self.pool_T(self.GAT_layer_T(e_T))
        T1, S1, M1 = self._branch(self.HtrgGAT_layer_ST11, self.HtrgGAT_layer_ST12, out_T, out_S,
                                  self.master1, self.pool_hS1, self.pool_hT1)
        T2, S2, M2 = self._branch(self.HtrgGAT_layer_ST21, self.HtrgGAT_layer_ST22, out_T, out_S,
                                  self.master2, self.pool_hS2, self.pool_hT2)
        T1, T2, S1, S2, M1, M2 = (self.drop_way(t) for t in (T1, T2, S1, S2, M1, M2))
        out_T, out_S, master = torch.max(T1, T2), torch.max(S1, S2), torch.max(M1, M2)
        return torch.cat([torch.max(torch.abs(out_T), dim=1)[0], torch.mean(out_T, dim=1),
                          torch.max(torch.abs(out_S), dim=1)[0], torch.mean(out_S, dim=1),
                          master.squeeze(1)], dim=1)


class AASISTHead(torch.nn.Module):
    def __init__(self, in_features, num_classes=2):
        super().__init__()
        self.drop = torch.nn.Dropout(0.5)
        self.out_layer = torch.nn.Linear(in_features, num_classes)

    def forward(self, features):
        return self.out_layer(self.drop(features)), features


class AASIST(torch.nn.Module):
    """``float32[B, 64000] -> (logits[B, 2], embedding[B, 5 * gat_dims[1]])`` (synthetic, genuine)."""

    def __init__(self, config=None):
        super().__init__()
        if config is not None and (not isinstance(config, dict) or not all(isinstance(k, str) for k in config)):
            raise ValueError('config must be a dict of AASIST configuration keys')
        config = dict(REFERENCE_CONFIG, **(config or {}))
        if set(config) != set(REFERENCE_CONFIG):
            raise ValueError(f'Unknown AASIST configuration keys: {sorted(set(config) - set(REFERENCE_CONFIG))}')
        _check_config(config)
        self.config = config
        self.backbone = AASISTBody(config)
        self.head = AASISTHead(self.backbone.output_dim)

    def forward(self, waveform):
        if not isinstance(waveform, torch.Tensor) or waveform.dtype != torch.float32:
            raise ValueError('Expected a float32 tensor')
        if waveform.ndim != 2 or waveform.shape[0] < 1 or waveform.shape[1] != WINDOW_SAMPLES:
            raise ValueError(f'Expected shape [B, {WINDOW_SAMPLES}], got {tuple(waveform.shape)}')
        if not torch.isfinite(waveform).all():
            raise ValueError('Waveform contains non-finite samples')
        return self.head(self.backbone(waveform))


def temporal_nodes(config=None, samples=WINDOW_SAMPLES):
    """Temporal graph nodes for a window: sinc 'valid' conv, 3x3 pool, six /3 pools."""
    config = dict(REFERENCE_CONFIG, **(config or {}))
    kernel = config['first_conv'] + (1 - config['first_conv'] % 2)
    length = samples - kernel + 1
    for _ in range(7):
        length = math.floor(length / 3)
    return length


def reference_key(name):
    """AVA parameter name to the upstream ``Model`` name."""
    if name.startswith('head.'):
        return name[len('head.'):]
    if name.startswith('backbone.'):
        return name[len('backbone.'):]
    raise ValueError(f'Not an AASIST parameter name: {name}')


def load_reference_state_dict(model, reference_state):
    """Load an upstream ``Model`` state dict strictly (every key present on both sides,
    shapes equal). Raises ValueError otherwise; nothing is loaded on refusal."""
    ours = model.state_dict()
    mapped = {name: reference_key(name) for name in ours}
    missing = sorted(set(mapped.values()) - set(reference_state))
    unexpected = sorted(set(reference_state) - set(mapped.values()))
    if missing or unexpected:
        raise ValueError(f'Reference state dict mismatch: missing {missing[:5]}, unexpected {unexpected[:5]}')
    for name, key in mapped.items():
        if tuple(reference_state[key].shape) != tuple(ours[name].shape):
            raise ValueError(f'Shape mismatch for {key}: {tuple(reference_state[key].shape)} '
                             f'vs {tuple(ours[name].shape)}')
    model.load_state_dict({name: reference_state[key] for name, key in mapped.items()}, strict=True)
    return model
