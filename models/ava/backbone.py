"""Layer-truncated wav2vec 2.0 backbone in the AntiDeepfake MMS-300M layout (FR-M1).

Truncation keeps the CNN feature encoder, feature projection, positional
convolution and the first ``kept_layers`` transformer layers (architecture §4.1).
The hidden states of every retained layer are returned for the SLS head.
"""
import re

import torch

from models.smoke.antideepfake import BACKBONE_CONFIG

SWEEP_LAYERS = (4, 6, 7, 8, 10, 12)  # PRD task 2.4 / architecture §4.1
_LAYER_KEY = re.compile(r'^encoder\.transformer\.layers\.(\d+)\.')


def _check_layers(kept_layers, total_layers):
    if type(total_layers) is not int or total_layers < 1:
        raise ValueError('total_layers must be a positive integer')
    if type(kept_layers) is not int or not 1 <= kept_layers <= total_layers:
        raise ValueError(f'kept_layers must be an integer in 1..{total_layers}')


def backbone_config(kept_layers, base=BACKBONE_CONFIG):
    """Return ``base`` with ``encoder_num_layers`` cut to ``kept_layers``."""
    _check_layers(kept_layers, base['encoder_num_layers'])
    return dict(base, encoder_num_layers=kept_layers)


def truncate_state(state, kept_layers, total_layers):
    """Drop transformer layers ``>= kept_layers`` from a converted torchaudio state.

    Refuses a state whose layer indices are not exactly ``0..total_layers-1``, so
    a partial or differently sized checkpoint is never silently truncated.
    Returns ``(truncated_state, dropped_keys)``.
    """
    _check_layers(kept_layers, total_layers)
    present = set()
    for key in state:
        match = _LAYER_KEY.match(key)
        if match:
            present.add(int(match.group(1)))
    if present != set(range(total_layers)):
        raise ValueError(f'State has transformer layers {sorted(present)}, expected 0..{total_layers - 1}')
    kept, dropped = {}, []
    for key, value in state.items():
        match = _LAYER_KEY.match(key)
        if match and int(match.group(1)) >= kept_layers:
            dropped.append(key)
        else:
            kept[key] = value
    return kept, sorted(dropped)


class TruncatedBackbone(torch.nn.Module):
    """Wrap a torchaudio ``Wav2Vec2Model``; ``forward`` returns ``[L, B, T, D]``."""

    def __init__(self, model):
        super().__init__()
        self.model = model
        self.kept_layers = len(model.encoder.transformer.layers)
        self.feature_dim = model.encoder.feature_projection.projection.out_features

    @property
    def feature_encoder(self):
        """The CNN feature encoder, frozen for the first epochs (architecture §4.1)."""
        return self.model.feature_extractor

    def forward(self, waveform):
        outputs, _ = self.model.extract_features(waveform)
        if len(outputs) != self.kept_layers:
            raise RuntimeError('Backbone returned an unexpected number of layers')
        return torch.stack(outputs, dim=0)


def build_backbone(kept_layers, base=BACKBONE_CONFIG):
    """Build a randomly initialized truncated backbone (tests and shape checks only)."""
    import torchaudio
    return TruncatedBackbone(torchaudio.models.wav2vec2_model(**backbone_config(kept_layers, base)))


def truncate_backbone(full_model, kept_layers, base=BACKBONE_CONFIG):
    """Return a new truncated backbone strictly loaded from a full ``Wav2Vec2Model``.

    ``full_model`` is typically the strictly loaded AntiDeepfake backbone from
    ``models.smoke.antideepfake.CpuDetector``; ``base`` must describe it.
    """
    total = base['encoder_num_layers']
    if len(full_model.encoder.transformer.layers) != total:
        raise ValueError('Full model layer count does not match its configuration')
    state, dropped = truncate_state(full_model.state_dict(), kept_layers, total)
    backbone = build_backbone(kept_layers, base)
    backbone.model.load_state_dict(state, strict=True)
    backbone.dropped_keys = dropped
    return backbone
