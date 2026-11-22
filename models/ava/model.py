"""AVA window model contract (architecture §2.3): ``float32[B, 64000] -> logits, embedding``."""
import torch
from torch.nn import functional as F

WINDOW_SAMPLES = 64_000  # 4 s at 16 kHz
SAMPLE_RATE = 16_000


class AvaDetector(torch.nn.Module):
    """Per-window layer normalization (AntiDeepfake card), backbone, then a head.

    The normalization is inside the model so offline evaluation, the ONNX graph and
    serving share it. Softmax outputs are uncalibrated (temperature comes later, FR-M6).
    """

    def __init__(self, backbone, head):
        super().__init__()
        if backbone.kept_layers != head.num_layers or backbone.feature_dim != head.feature_dim:
            raise ValueError('Head must match the retained layer count and feature width')
        self.backbone, self.head = backbone, head

    def forward(self, waveform):
        if not isinstance(waveform, torch.Tensor) or waveform.dtype != torch.float32:
            raise ValueError('Expected a float32 tensor')
        if waveform.ndim != 2 or waveform.shape[0] < 1 or waveform.shape[1] != WINDOW_SAMPLES:
            raise ValueError(f'Expected shape [B, {WINDOW_SAMPLES}], got {tuple(waveform.shape)}')
        if not torch.isfinite(waveform).all():
            raise ValueError('Waveform contains non-finite samples')
        normalized = F.layer_norm(waveform, (WINDOW_SAMPLES,))
        return self.head(self.backbone(normalized))


def build_detector(kept_layers, base=None, head='sls'):
    """Randomly initialized detector for smoke runs; ``head`` is 'sls' or 'mean_pool'."""
    from models.ava.backbone import BACKBONE_CONFIG, build_backbone
    from models.ava.sls import MeanPoolHead, SLSHead
    heads = {'sls': SLSHead, 'mean_pool': MeanPoolHead}
    if head not in heads:
        raise ValueError(f'head must be one of {sorted(heads)}')
    backbone = build_backbone(kept_layers, BACKBONE_CONFIG if base is None else base)
    return AvaDetector(backbone, heads[head](backbone.kept_layers, backbone.feature_dim))
