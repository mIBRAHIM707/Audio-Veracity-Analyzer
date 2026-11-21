"""Model card for an exported AVA detector (backlog 10.2; PRD FR-M12, FR-E5, FR-O4,
FR-I4; architecture §2.3 "`model_card.json` alongside declares version, backbone, kept
layers, window size, sample rate, calibration temperature and licence restriction;
`inference` refuses to start if the card and config disagree, or if either file is
missing", §5 step 6).

- `build_card(onnx_path, ...)` makes the card dict: the fields above plus ECE, metrics,
  training data, known failure modes, the PRD §1.4 limitation text (fixed here, never
  optional), the I/O contract, and the ONNX file's name, size and SHA-256 (the card binds
  one exact file). Nothing is invented: a value that was not measured must be written as
  `NOT_MEASURED`, and an uncalibrated model has `calibration_temperature: null` with
  `calibration_status: "uncalibrated"`.
- `write_card` writes JSON (refuses to overwrite) and `render_markdown` a readable copy.
- `check_card(card_path, onnx_path, scoring_document=None)` is the serving start-up
  check: both files present, the schema valid, the ONNX hash equal to the card's, the
  window and rate equal to the serving contract, and (when given) the scoring config
  valid against the card's window (`services.scorer.config.parse_config`). Any problem
  raises `ModelCardError` listing all of them, so the service does not start.
"""
import hashlib
import json
import math
from pathlib import Path

CARD_SCHEMA = 'ava-model-card-v1'
WINDOW_SAMPLES = 64000
SAMPLE_RATE = 16000
NOT_MEASURED = 'not_measured'
EMBED_DIM = 256  # architecture §2.3 `embedding[B, 256]`; models/ava/sls.EMBED_DIM (torch module)
MAX_CARD_BYTES = 256 * 1024
# PRD v0.2 §1.4, verbatim (FR-M12, FR-U1).
LIMITATIONS = (
    'AVA flags likely synthetic speech. An alert is not proof of fraudulent intent, and the absence of an '
    'alert does not verify the caller.',
    'AVA does not detect human impersonators speaking in their own voice, which is the majority of the '
    'fraud counted in §1.1.',
    'AVA does not verify identity. It is not a voice biometric.',
    'Targets in §6 are provisional until the first baseline run and will be revised in v0.3 with measured '
    'values.',
)
from .onnx_export import BATCH_AXIS, INPUT_NAME, OUTPUT_NAMES, WINDOW_SAMPLES as EXPORT_WINDOW  # noqa: E402

assert EXPORT_WINDOW == WINDOW_SAMPLES
# The exported graph's names (onnx_export), not a separate copy.
CONTRACT = {'input': {'name': INPUT_NAME, 'dtype': 'float32', 'shape': [BATCH_AXIS, WINDOW_SAMPLES]},
            'outputs': [{'name': OUTPUT_NAMES[0], 'dtype': 'float32', 'shape': [BATCH_AXIS, 2],
                         'classes': ['synthetic', 'genuine']},
                        {'name': OUTPUT_NAMES[1], 'dtype': 'float32', 'shape': [BATCH_AXIS, EMBED_DIM]}]}
FIELDS = ('schema', 'version', 'backbone', 'kept_layers', 'window_s', 'window_samples', 'sample_rate',
          'calibration_temperature', 'calibration_status', 'ece', 'metrics', 'training_data', 'failure_modes',
          'licence_restriction', 'limitations', 'contract', 'onnx')
MAX_ONNX_BYTES = 4 * 1024 ** 3


class ModelCardError(ValueError):
    def __init__(self, problems):
        super().__init__('; '.join(problems))
        self.problems = list(problems)


def _sha256(path):
    digest, size = hashlib.sha256(), 0
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            size += len(block)
            if size > MAX_ONNX_BYTES:
                raise ModelCardError(['ONNX file is larger than 4 GiB'])
            digest.update(block)
    return digest.hexdigest(), size


def _measured(value):
    return value == NOT_MEASURED or (isinstance(value, (int, float)) and not isinstance(value, bool)
                                     and math.isfinite(value))


def validate(card):
    """List of problems (empty when the card is well formed)."""
    if not isinstance(card, dict):
        return ['card must be a JSON object']
    problems = [f'missing or unknown fields: {sorted(set(FIELDS) ^ set(card))}'] if set(card) != set(FIELDS) else []
    if problems:
        return problems
    if card['schema'] != CARD_SCHEMA:
        problems.append(f'schema must be {CARD_SCHEMA!r}')
    for key in ('version', 'backbone', 'licence_restriction'):
        if not isinstance(card[key], str) or not card[key].strip() or len(card[key]) > 500:
            problems.append(f'{key} must be a non-empty string of at most 500 characters')
    if type(card['kept_layers']) is not int or not 1 <= card['kept_layers'] <= 48:
        problems.append('kept_layers must be an integer from 1 to 48')
    if card['window_samples'] != WINDOW_SAMPLES or card['sample_rate'] != SAMPLE_RATE \
            or card['window_s'] != WINDOW_SAMPLES / SAMPLE_RATE:
        problems.append(f'window must be {WINDOW_SAMPLES} samples at {SAMPLE_RATE} Hz (the serving contract)')
    temperature, status = card['calibration_temperature'], card['calibration_status']
    if status == 'uncalibrated':
        if temperature is not None:
            problems.append('an uncalibrated card has calibration_temperature null')
    elif status == 'calibrated':
        if isinstance(temperature, bool) or not isinstance(temperature, (int, float)) \
                or not math.isfinite(temperature) or temperature <= 0:
            problems.append('a calibrated card needs a positive calibration_temperature')
        if card['ece'] == NOT_MEASURED:
            problems.append('a calibrated card needs a measured ECE')
    else:
        problems.append('calibration_status must be "calibrated" or "uncalibrated"')
    if not _measured(card['ece']):
        problems.append(f'ece must be a number or {NOT_MEASURED!r}')
    if not isinstance(card['metrics'], dict) or not card['metrics'] \
            or not all(isinstance(k, str) and _measured(v) for k, v in card['metrics'].items()):
        problems.append(f'metrics must name at least one value, each a number or {NOT_MEASURED!r}')
    if not isinstance(card['training_data'], dict) or not card['training_data'] or not all(
            isinstance(k, str) and (isinstance(v, str) or _measured(v)) for k, v in card['training_data'].items()):
        problems.append('training_data must describe the data (an object of strings or finite numbers)')
    if not isinstance(card['failure_modes'], list) or not card['failure_modes'] \
            or not all(isinstance(f, str) and f.strip() for f in card['failure_modes']):
        problems.append('failure_modes must list at least one known failure mode')
    if card['limitations'] != list(LIMITATIONS):
        problems.append('limitations must be the PRD §1.4 text, unchanged')
    if card['contract'] != CONTRACT:
        problems.append('contract must be the serving I/O contract')
    onnx = card['onnx']
    if not isinstance(onnx, dict) or set(onnx) != {'file', 'bytes', 'sha256'} \
            or not isinstance(onnx.get('file'), str) or not onnx['file'].endswith('.onnx') \
            or type(onnx.get('bytes')) is not int or onnx['bytes'] <= 0 \
            or not isinstance(onnx.get('sha256'), str) or len(onnx['sha256']) != 64 \
            or any(c not in '0123456789abcdef' for c in onnx['sha256']):
        problems.append('onnx must give the .onnx file name, its size in bytes and a lowercase SHA-256')
    return problems


def build_card(onnx_path, *, version, backbone, kept_layers, metrics, training_data, failure_modes,
               licence_restriction, calibration_temperature=None, ece=NOT_MEASURED):
    path = Path(onnx_path)
    if not path.is_file():
        raise ModelCardError([f'ONNX file not found: {path.name}'])
    sha256, size = _sha256(path)
    card = {'schema': CARD_SCHEMA, 'version': version, 'backbone': backbone, 'kept_layers': kept_layers,
            'window_s': WINDOW_SAMPLES / SAMPLE_RATE, 'window_samples': WINDOW_SAMPLES, 'sample_rate': SAMPLE_RATE,
            'calibration_temperature': calibration_temperature,
            'calibration_status': 'uncalibrated' if calibration_temperature is None else 'calibrated',
            'ece': ece, 'metrics': dict(metrics), 'training_data': dict(training_data),
            'failure_modes': list(failure_modes), 'licence_restriction': licence_restriction,
            'limitations': list(LIMITATIONS), 'contract': json.loads(json.dumps(CONTRACT)),
            'onnx': {'file': path.name, 'bytes': size, 'sha256': sha256}}
    problems = validate(card)
    if problems:
        raise ModelCardError(problems)
    return card


def write_card(card, path):
    problems = validate(card)
    if problems:
        raise ModelCardError(problems)
    text = json.dumps(card, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False)  # before any file
    path = Path(path)
    with open(path, 'x', encoding='utf-8') as handle:  # never overwrite a published card
        handle.write(text)
    return path


def render_markdown(card):
    problems = validate(card)
    if problems:
        raise ModelCardError(problems)
    calibration = (f"temperature {card['calibration_temperature']}" if card['calibration_status'] == 'calibrated'
                   else 'UNCALIBRATED: scores are not probabilities')
    lines = [f"# AVA model card — {card['version']}", '',
             f"- Backbone: {card['backbone']}, {card['kept_layers']} layers kept",
             f"- Input: {card['window_s']} s windows at {card['sample_rate']} Hz",
             f"- Calibration: {calibration}; ECE: {card['ece']}",
             f"- ONNX: `{card['onnx']['file']}`, {card['onnx']['bytes']} bytes, SHA-256 `{card['onnx']['sha256']}`",
             f"- Licence restriction: {card['licence_restriction']}", '', '## Metrics', '']
    lines += [f'- {name}: {value}' for name, value in sorted(card['metrics'].items())]
    lines += ['', '## Training data', '']
    lines += [f'- {name}: {value}' for name, value in sorted(card['training_data'].items())]
    lines += ['', '## Known failure modes', ''] + [f'- {item}' for item in card['failure_modes']]
    lines += ['', '## Limitations (PRD §1.4)', ''] + [f'- {item}' for item in card['limitations']]
    return '\n'.join(lines) + '\n'


def _strict_object(pairs):
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError('duplicate key')
    return dict(pairs)


def _no_constant(name):
    raise ValueError(f'{name} is not allowed')


def check_card(card_path, onnx_path, scoring_document):
    """Serving start-up check (FR-O4): returns the card, or raises ModelCardError with
    every problem found. The scoring config (a parsed mapping) is required: the card
    and config must agree (`parse_config` with the card's window). The served file's
    name must be the card's, and its size and SHA-256 must match."""
    card_path, onnx_path = Path(card_path), Path(onnx_path)
    problems = [f'{label} not found: {p.name}' for label, p in (('model card', card_path), ('ONNX model', onnx_path))
                if not p.is_file()]
    if problems:
        raise ModelCardError(problems)
    try:
        raw = card_path.read_bytes()
        if len(raw) > MAX_CARD_BYTES:
            raise ModelCardError([f'model card is larger than {MAX_CARD_BYTES} bytes'])
        card = json.loads(raw.decode('utf-8'), parse_constant=_no_constant, object_pairs_hook=_strict_object)
    except ModelCardError:
        raise
    except (OSError, UnicodeDecodeError, ValueError, RecursionError) as error:
        raise ModelCardError([f'model card is not readable strict JSON: {type(error).__name__}']) from None
    problems = validate(card)
    if not problems:
        if onnx_path.name != card['onnx']['file']:
            problems.append('the served ONNX file name is not the one the card names')
        try:
            sha256, size = _sha256(onnx_path)
        except OSError as error:
            problems.append(f'ONNX file unreadable: {type(error).__name__}')
        else:
            if (sha256, size) != (card['onnx']['sha256'], card['onnx']['bytes']):
                problems.append('ONNX file does not match the card (hash or size)')
        from services.scorer.config import ConfigError, parse_config
        try:
            parse_config(scoring_document, model_window_s=card['window_s'])
        except ConfigError as error:
            problems.append(f'scoring config disagrees with the card: {error}')
    if problems:
        raise ModelCardError(problems)
    return card
