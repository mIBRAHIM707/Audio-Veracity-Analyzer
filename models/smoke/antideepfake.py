"""Strict local CPU execution smoke test of the official AntiDeepfake classifier."""
import argparse
import array
import hashlib
import importlib.metadata
import io
import json
import math
import platform
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import threading
import time
import wave

MODEL_REPO = 'nii-yamagishilab/mms-300m-anti-deepfake'
MODEL_REVISION = '7928e119e09f616b2ce698892e00ca22dbaa40bf'
MODEL_SHA256 = '9bd5d9785bf72f91dfca8508651354946c4b307921cb358505e1b94035102224'
MODEL_SIZE = 1_269_622_944
BACKBONE_CONFIG = dict(
    extractor_mode='layer_norm', extractor_conv_layer_config=[
        (512, 10, 5), (512, 3, 2), (512, 3, 2), (512, 3, 2),
        (512, 3, 2), (512, 2, 2), (512, 2, 2)],
    extractor_conv_bias=True, encoder_embed_dim=1024, encoder_projection_dropout=0.0,
    encoder_pos_conv_kernel=128, encoder_pos_conv_groups=16, encoder_num_layers=24,
    encoder_num_heads=16, encoder_attention_dropout=0.0, encoder_ff_interm_features=4096,
    encoder_ff_interm_dropout=0.0, encoder_dropout=0.0, encoder_layer_norm_first=True,
    encoder_layer_drop=0.0, aux_num_out=None,
)


def load_state_strict(backbone, head, state):
    """Reject all unrecognized tensors; load every inference parameter strictly."""
    import torch
    from torchaudio.models.wav2vec2.utils.import_fairseq import _convert_state_dict
    ssl, classifier, omitted = {}, {}, []
    for key, value in state.items():
        if key in ('proj_fc.weight', 'proj_fc.bias'):
            classifier[key.removeprefix('proj_fc.')] = value
        elif key.startswith('m_ssl.model.'):
            name = key.removeprefix('m_ssl.model.')
            if name == 'mask_emb' or name.startswith(('quantizer.', 'project_q.', 'final_proj.')):
                omitted.append(key)
            else:
                ssl[name] = value
        else:
            raise ValueError(f'Unexpected checkpoint tensor: {key}')
    if set(classifier) != {'weight', 'bias'}:
        raise ValueError('Original classifier weight and bias are required')
    if tuple(classifier['weight'].shape) != (2, 1024) or tuple(classifier['bias'].shape) != (2,):
        raise ValueError('Original classifier must have shape [2,1024] plus [2] bias')
    # Convert one key at a time to detect mapper omissions and alias collisions.
    converted = {}
    for key, value in ssl.items():
        mapped = _convert_state_dict({key: value})
        if len(mapped) != 1 or set(mapped) & set(converted):
            raise ValueError(f'Unmapped or colliding inference tensor: {key}')
        converted.update(mapped)
    expected = backbone.state_dict()
    # Torchaudio versions differ only in the spelling of weight-norm parameters.
    prefix = 'encoder.transformer.pos_conv_embed.conv.'
    for old, new in (('weight_g', 'parametrizations.weight.original0'),
                     ('weight_v', 'parametrizations.weight.original1')):
        if prefix + old in converted and prefix + new in expected and prefix + old not in expected:
            converted[prefix + new] = converted.pop(prefix + old)
    for key, value in list(converted.items()) + list(classifier.items()):
        if value.dtype != torch.float32 or not torch.isfinite(value).all().item():
            raise ValueError(f'Expected finite float32 inference tensor: {key}')
    backbone.load_state_dict(converted, strict=True, assign=True)
    head.load_state_dict(classifier, strict=True, assign=True)
    if any(parameter.is_meta for module in (backbone, head) for parameter in module.parameters()):
        raise ValueError('Unmaterialized inference parameters remain')
    return {'strict': True, 'backbone_tensors_loaded': len(converted),
            'head_tensors_loaded': len(classifier), 'checkpoint_tensors': len(state),
            'omitted_pretraining_keys': sorted(omitted), 'missing_keys': [], 'unexpected_keys': []}


def normalize_waveform(samples):
    import torch
    from torch.nn import functional as F
    waveform = torch.as_tensor(samples, dtype=torch.float32, device='cpu')
    if waveform.ndim != 1 or not waveform.numel() or not torch.isfinite(waveform).all().item():
        raise ValueError('Expected a finite nonempty mono waveform')
    return F.layer_norm(waveform, waveform.shape).unsqueeze(0)


def score_logits(logits):
    if hasattr(logits, 'detach'):
        logits = logits.detach().cpu().tolist()
    if not isinstance(logits, (tuple, list)) or len(logits) != 2:
        raise ValueError('Expected exactly two class logits')
    if any(isinstance(value, bool) or not isinstance(value, (int, float))
           or not math.isfinite(value) for value in logits):
        raise ValueError('Expected finite numeric logits')
    logits = [float(value) for value in logits]
    exponentials = [math.exp(value - max(logits)) for value in logits]
    total = sum(exponentials)
    probabilities = [value / total for value in exponentials]
    return {'logits': logits, 'probabilities': probabilities,
            'predicted_label': 'synthetic' if logits[0] >= logits[1] else 'genuine'}


class CpuDetector:
    def __init__(self, checkpoint_path):
        import torch
        import torchaudio
        from safetensors.torch import load
        checkpoint_path = Path(checkpoint_path)
        if checkpoint_path.suffix != '.safetensors':
            raise ValueError('Only local safetensors checkpoints are supported')
        # Hash and deserialize the same immutable snapshot, never reopen a verified path.
        payload = _bounded_read(checkpoint_path, MODEL_SIZE)
        if len(payload) != MODEL_SIZE or hashlib.sha256(payload).hexdigest() != MODEL_SHA256:
            raise ValueError('Checkpoint snapshot does not match pinned publisher size or SHA256')
        state = load(payload)
        del payload
        with torch.device('meta'):
            self.backbone = torchaudio.models.wav2vec2_model(**BACKBONE_CONFIG)
            self.head = torch.nn.Linear(1024, 2)
        self.loading_coverage = load_state_strict(self.backbone, self.head, state)
        self.backbone.eval()
        self.head.eval()

    def __call__(self, samples):
        import torch
        with torch.inference_mode():
            waveform = normalize_waveform(samples)
            features = self.backbone(waveform)[0]
            if features.ndim != 3 or features.shape[0] != 1 or features.shape[2] != 1024:
                raise ValueError('Unexpected backbone feature shape')
            logits = self.head(features.mean(dim=1))
            return logits[0].tolist()


def _bounded_read(path, limit):
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError('Expected a regular file')
    with path.open('rb') as stream:
        payload = stream.read(limit + 1)
    if len(payload) > limit:
        raise ValueError('File exceeds size limit')
    return payload


def _safe_path(root, name):
    if (not isinstance(name, str) or not name or len(name) > 1000
            or any(char in name for char in ('\\', ':', '\x00'))
            or PurePosixPath(name).is_absolute()
            or any(part in ('', '.', '..') or part != part.strip(' .') for part in name.split('/'))):
        raise ValueError('Unsafe relative path')
    path = root / name
    if not path.resolve().is_relative_to(root):
        raise ValueError('Path escapes source root')
    current = path
    while current != root:
        if current.is_symlink() or (hasattr(current, 'is_junction') and current.is_junction()):
            raise ValueError('Symlink or junction inputs unsupported')
        current = current.parent
    return path


def _valid_hash(digest):
    return isinstance(digest, str) and re.fullmatch('[0-9a-f]{64}', digest)


def _reject_json_constant(value):
    raise ValueError(f'Non-finite JSON constant is unsupported: {value}')


def _finite_json_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError('JSON number exceeds finite floating-point range')
    return number


def _verify_model(model_root):
    if (model_root / 'INCOMPLETE').exists():
        raise ValueError('Model publication is incomplete')
    metadata_payload = _bounded_read(_safe_path(model_root, 'model-source.json'), 2_000_000)
    metadata = json.loads(metadata_payload, parse_constant=_reject_json_constant,
                          parse_float=_finite_json_float)
    if (not isinstance(metadata, dict) or metadata.get('repo') != MODEL_REPO
            or metadata.get('revision') != MODEL_REVISION):
        raise ValueError('Expected pinned official model source')
    if metadata.get('class_order') != ['fake', 'real'] or type(metadata.get('input_rate')) is not int or metadata['input_rate'] != 16000:
        raise ValueError('Unexpected model class order or input sample rate')
    files = metadata.get('files')
    if not isinstance(files, list) or not 1 <= len(files) <= 20:
        raise ValueError('Model source requires explicit file hashes')
    seen = set()
    for entry in files:
        if not isinstance(entry, dict):
            raise ValueError('Invalid model file entry')
        path = _safe_path(model_root, entry.get('path'))
        if entry['path'].casefold() in seen:
            raise ValueError('Duplicate model path')
        seen.add(entry['path'].casefold())
        size = entry.get('size')
        if type(size) is not int or not 0 < size <= 2_000_000_000 or not _valid_hash(entry.get('sha256')):
            raise ValueError('Invalid model file size or hash')
        if entry['path'] == 'model.safetensors' and entry['sha256'] != MODEL_SHA256:
            raise ValueError('Checkpoint does not match pinned publisher SHA256')
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size != size:
            raise ValueError('Model file size mismatch')
        digest, count = hashlib.sha256(), 0
        with path.open('rb') as stream:
            while chunk := stream.read(min(1_048_576, size + 1 - count)):
                count += len(chunk)
                digest.update(chunk)
                if count > size:
                    raise ValueError('Model file grew while verifying')
        if count != size or digest.hexdigest() != entry['sha256']:
            raise ValueError('Model file hash mismatch')
    if 'model.safetensors' not in {entry['path'] for entry in files}:
        raise ValueError('Missing explicit model.safetensors provenance')
    return metadata, hashlib.sha256(metadata_payload).hexdigest()


def _read_inputs(manifest_path, source_root):
    if (manifest_path.parent / 'INCOMPLETE').exists() or (source_root / 'INCOMPLETE').exists():
        raise ValueError('Audio input publication is incomplete')
    payload = _bounded_read(manifest_path, 2_000_000)
    manifest = json.loads(payload, parse_constant=_reject_json_constant,
                          parse_float=_finite_json_float)
    rows = manifest.get('files') if isinstance(manifest, dict) else None
    if not isinstance(rows, list) or not 1 <= len(rows) <= 100:
        raise ValueError('Smoke manifest requires 1–100 files')
    inputs, seen = [], set()
    for row in rows:
        if not isinstance(row, dict) or row.get('label') not in ('genuine', 'synthetic'):
            raise ValueError('Each input requires a genuine or synthetic publisher label')
        path = _safe_path(source_root, row.get('path'))
        current = path.parent
        while current != source_root:
            if (current / 'INCOMPLETE').exists():
                raise ValueError('Audio input publication is incomplete')
            current = current.parent
        if row['path'].casefold() in seen or not _valid_hash(row.get('sha256')):
            raise ValueError('Duplicate input path or invalid SHA256')
        seen.add(row['path'].casefold())
        audio_payload = _bounded_read(path, 10_000_000)
        if hashlib.sha256(audio_payload).hexdigest() != row['sha256']:
            raise ValueError('Audio hash mismatch')
        with wave.open(io.BytesIO(audio_payload), 'rb') as audio:
            if (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) != (16000, 1, 2):
                raise ValueError('Expected 16 kHz mono PCM16 WAV')
            count = audio.getnframes()
            if not 48000 <= count <= 96000:
                raise ValueError('Expected 3–6 seconds of audio')
            raw = audio.readframes(count)
        if len(raw) != count * 2:
            raise ValueError('Truncated WAV')
        samples = array.array('h', raw)
        if sys.byteorder != 'little':
            samples.byteswap()
        inputs.append((dict(row, duration_seconds=count / 16000), [value / 32768 for value in samples]))
    return inputs, hashlib.sha256(payload).hexdigest()


def run_smoke(model_root, manifest_path, source_root, output_root, threads=4, load_detector=CpuDetector):
    if type(threads) is not int or not 1 <= threads <= 64:
        raise ValueError('threads must be an integer in 1..64')
    model_root, source_root, output_root = (Path(path).resolve() for path in (model_root, source_root, output_root))
    manifest_path = Path(manifest_path).resolve()
    repo = Path(__file__).resolve().parents[2]
    if (output_root.exists() or output_root.is_relative_to(repo)
            or manifest_path.is_relative_to(output_root)
            or any(output_root.is_relative_to(root) or root.is_relative_to(output_root)
                   for root in (model_root, source_root))):
        raise ValueError('Output must be new, external, and disjoint from inputs')
    metadata, metadata_hash = _verify_model(model_root)
    inputs, manifest_hash = _read_inputs(manifest_path, source_root)
    import torch
    import psutil
    torch.set_num_threads(threads)
    output_root.mkdir(parents=True)
    marker = output_root / 'INCOMPLETE'
    marker.write_text('Smoke report incomplete; do not consume.', encoding='utf-8')
    process = psutil.Process()
    samples_rss = [process.memory_info().rss]
    stop = threading.Event()

    def sample_memory():
        while not stop.wait(0.05):
            samples_rss.append(process.memory_info().rss)

    sampler = threading.Thread(target=sample_memory, daemon=True)
    sampler.start()
    try:
        start = time.perf_counter()
        detector = load_detector(model_root / 'model.safetensors')
        loading_seconds = time.perf_counter() - start
        start = time.perf_counter()
        warmup_score = score_logits(detector(inputs[0][1]))
        warmup_seconds = time.perf_counter() - start
        trials = []
        for row, samples in inputs:
            start = time.perf_counter()
            score = score_logits(detector(samples))
            latency = time.perf_counter() - start
            trials.append(dict(row, **score, latency_seconds=latency,
                               real_time_factor=latency / row['duration_seconds']))
        samples_rss.append(process.memory_info().rss)
    finally:
        stop.set()
        sampler.join()
    summary = {'trials': len(trials), 'loading_seconds': loading_seconds,
               'warmup_seconds': warmup_seconds,
               'inference_seconds': sum(row['latency_seconds'] for row in trials),
               'audio_seconds': sum(row['duration_seconds'] for row in trials)}
    report = {
        'schema_version': 1, 'summary': summary, 'trials': trials,
        'warmup': dict(path=inputs[0][0]['path'], latency_seconds=warmup_seconds, **warmup_score),
        'loading_coverage': detector.loading_coverage, 'model_source': metadata,
        'model_source_sha256': metadata_hash, 'manifest_sha256': manifest_hash,
        'class_order': ['fake', 'real'], 'device': 'cpu', 'threads': torch.get_num_threads(),
        'versions': {name: importlib.metadata.version(name) for name in ('torch', 'torchaudio', 'safetensors', 'psutil')},
        'platform': platform.platform(), 'processor': platform.processor(),
        'memory': {'sampled_peak_rss_bytes': max(samples_rss), 'samples': len(samples_rss),
                   'sampling_interval_seconds': 0.05, 'scope': 'process RSS during load, warmup and measured trials; sampled, not exact peak'},
        'limitations': 'CPU execution smoke only. No fairseq numerical parity, AVA SLS, calibration, telephone performance, or accuracy acceptance established.',
    }
    (output_root / 'report.json').write_text(json.dumps(report, indent=2, allow_nan=False), encoding='utf-8')
    marker.unlink()
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('model-root', 'manifest', 'source-root', 'output-root'):
        parser.add_argument('--' + name, required=True, type=Path)
    parser.add_argument('--threads', type=int, default=4)
    args = parser.parse_args()
    try:
        summary = run_smoke(args.model_root, args.manifest, args.source_root, args.output_root, args.threads)
    except (OSError, ValueError, RuntimeError, wave.Error, EOFError) as error:
        print(f'CPU smoke failed: {error}', file=sys.stderr)
        return 1
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
