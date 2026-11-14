"""SLS head and the FR-M8 mean-pool ablation head (architecture §4.2).

Input to both heads: hidden states of every retained layer, ``[L, B, T, D]``.
Output: ``(logits[B, 2], embedding[B, 256])`` in class order (synthetic, genuine),
the same order as the AntiDeepfake checkpoint (index 0 = fake).

SLS: a learned softmax weighting over the L retained layers sums them to
``[B, T, D]``; a linear projection to 256-d; attentive statistics pooling (attention-
weighted mean and standard deviation) to 512-d; a linear map to the 256-d embedding;
a 2-class linear output. The architecture names no further activation, so none is
added. Windows have a fixed length (64,000 samples), so no frame mask is applied.
"""
import torch
from torch.nn import functional as F

CLASS_ORDER = ('synthetic', 'genuine')
EMBED_DIM = 256


def _check_dims(**dims):
    for name, value in dims.items():
        if type(value) is not int or value < 1:
            raise ValueError(f'{name} must be a positive integer')


def _check_hidden(hidden, num_layers, feature_dim):
    if not isinstance(hidden, torch.Tensor) or hidden.ndim != 4:
        raise ValueError('Expected hidden states shaped [L, B, T, D]')
    if hidden.shape[0] != num_layers or hidden.shape[3] != feature_dim:
        raise ValueError(f'Expected {num_layers} layers of width {feature_dim}, got {tuple(hidden.shape)}')
    if hidden.shape[1] < 1 or hidden.shape[2] < 1:
        raise ValueError('Expected at least one batch row and one frame')


class AttentiveStatsPool(torch.nn.Module):
    """Attentive statistics pooling: ``[B, T, C] -> [B, 2C]`` (mean then std)."""

    def __init__(self, channels, attention_dim=128, eps=1e-5):
        super().__init__()
        self.attention = torch.nn.Linear(channels, attention_dim)
        self.score = torch.nn.Linear(attention_dim, 1)
        self.eps = eps

    def forward(self, frames):
        weights = torch.softmax(self.score(torch.tanh(self.attention(frames))), dim=1)
        mean = (weights * frames).sum(dim=1)
        # Centred form: E[x^2] - mean^2 cancels in float32 for large-offset channels.
        variance = (weights * (frames - mean.unsqueeze(1)) ** 2).sum(dim=1)
        std = torch.sqrt(variance.clamp(min=self.eps))
        return torch.cat((mean, std), dim=1)


class SLSHead(torch.nn.Module):
    def __init__(self, num_layers, feature_dim=1024, embed_dim=EMBED_DIM,
                 attention_dim=128, num_classes=len(CLASS_ORDER)):
        super().__init__()
        _check_dims(num_layers=num_layers, feature_dim=feature_dim, embed_dim=embed_dim,
                    attention_dim=attention_dim, num_classes=num_classes)
        self.num_layers, self.feature_dim = num_layers, feature_dim
        # Zero logits start from a uniform layer weighting.
        self.layer_logits = torch.nn.Parameter(torch.zeros(num_layers))
        self.projection = torch.nn.Linear(feature_dim, embed_dim)
        self.pooling = AttentiveStatsPool(embed_dim, attention_dim)
        self.embedding = torch.nn.Linear(2 * embed_dim, embed_dim)
        self.classifier = torch.nn.Linear(embed_dim, num_classes)

    def layer_weights(self):
        return torch.softmax(self.layer_logits, dim=0)

    def forward(self, hidden):
        _check_hidden(hidden, self.num_layers, self.feature_dim)
        mixed = torch.einsum('l,lbtd->btd', self.layer_weights(), hidden)
        embedding = self.embedding(self.pooling(self.projection(mixed)))
        return self.classifier(embedding), embedding


class MeanPoolHead(torch.nn.Module):
    """FR-M8 ablation: last retained layer, temporal mean, MLP to 256-d, 2-class output."""

    def __init__(self, num_layers, feature_dim=1024, embed_dim=EMBED_DIM,
                 num_classes=len(CLASS_ORDER)):
        super().__init__()
        _check_dims(num_layers=num_layers, feature_dim=feature_dim, embed_dim=embed_dim,
                    num_classes=num_classes)
        self.num_layers, self.feature_dim = num_layers, feature_dim
        self.hidden = torch.nn.Linear(feature_dim, embed_dim)
        self.classifier = torch.nn.Linear(embed_dim, num_classes)

    def forward(self, hidden):
        _check_hidden(hidden, self.num_layers, self.feature_dim)
        embedding = F.relu(self.hidden(hidden[-1].mean(dim=1)))
        return self.classifier(embedding), embedding
