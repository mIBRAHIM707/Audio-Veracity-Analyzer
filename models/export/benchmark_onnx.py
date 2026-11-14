"""CPU timing of exported ONNX models through onnxruntime (backlog 8.QA; PRD §6.1).

Times ``session.run`` on one seeded ``float32[batch, 64000]`` noise window per model,
using the same method as ``models/ava/benchmark_cpu.py``: ``rounds`` rounds visit the
models in a seeded random order (interleaving spreads host-load drift), each round
opens a fresh CPU-only session with ``threads`` intra-op threads (hash-pinned), makes
``warmup`` untimed calls and times ``repeats`` calls. Per model: median, per-round
medians, maximum and raw timings; p95 only with at least 20 timings; PRD RTF (seconds
per audio second) only at batch 1, otherwise labelled throughput.

What this is not: PRD §6.1 targets are for the INT8 model on reference box A1 with the
trained head; these timings are for whatever models and machine are given. Session
creation time is reported separately and excluded from the call timings. Single
process, no concurrency; noise input (graph cost does not depend on the content, but
dynamic INT8 activation scaling does run on it).
"""
import argparse
import datetime
import json
import os
import platform
import random
import statistics
import sys
import time
from pathlib import Path

import numpy as np

from models.export import onnx_export as ox

SAMPLE_RATE = 16_000
MIN_TIMINGS_FOR_P95 = 20
PRD_TARGETS = {'p95_latency_seconds': 1.5, 'rtf': 0.375,
               'conditions': 'INT8 ONNX, 4 threads, batch 1, reference box A1 (PRD §6.1)'}


def _percentile(values, fraction):
    ordered = sorted(values)
    position = fraction * (len(ordered) - 1)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def benchmark(models, *, threads=4, batch=1, warmup=2, repeats=20, rounds=1, seed=0,
              clock=time.perf_counter, open_session=ox.open_session):
    """``models`` maps a label to ``{'path', 'sha256'}``; returns the report dictionary."""
    if not isinstance(models, dict) or not models:
        raise ValueError('models must be a non-empty {label: {path, sha256}} mapping')
    for label, spec in models.items():
        if not isinstance(label, str) or not label:
            raise ValueError('model labels must be non-empty strings')
        if not isinstance(spec, dict) or set(spec) != {'path', 'sha256'}:
            raise ValueError(f'model {label!r} needs exactly path and sha256')
    for name, value, low in (('threads', threads, 1), ('batch', batch, 1), ('warmup', warmup, 0),
                             ('repeats', repeats, 2), ('rounds', rounds, 1), ('seed', seed, 0)):
        if type(value) is not int or value < low:
            raise ValueError(f'{name} must be an integer >= {low}')
    if batch > ox.PARITY_MAX_BATCH:
        raise ValueError(f'batch must be at most {ox.PARITY_MAX_BATCH}')
    rng = np.random.default_rng(seed)
    window = (rng.standard_normal((batch, ox.WINDOW_SAMPLES)) * 0.1).astype(np.float32)
    feeds = {ox.INPUT_NAME: window}
    labels = list(models)
    timings = {label: [] for label in labels}
    round_medians = {label: [] for label in labels}
    open_seconds = {label: [] for label in labels}
    orders = []
    for round_index in range(rounds):
        order = list(labels)
        random.Random(seed * 1000 + round_index).shuffle(order)
        orders.append(order)
        for label in order:
            spec = models[label]
            start = clock()
            session = open_session(spec['path'], threads=threads, expected_sha256=spec['sha256'])
            open_seconds[label].append(clock() - start)
            problems = ox.check_session(session)
            if problems:
                raise ValueError(f'model {label!r} breaks the I/O contract: {problems}')
            for _ in range(warmup):
                session.run(None, feeds)
            these = []
            for _ in range(repeats):
                start = clock()
                session.run(None, feeds)
                these.append(clock() - start)
            del session
            if any(not t > 0 for t in these):
                raise RuntimeError('Clock returned a non-positive duration')
            timings[label].extend(these)
            round_medians[label].append(statistics.median(these))
    audio_seconds = batch * ox.WINDOW_SAMPLES / SAMPLE_RATE
    rows = []
    for label in labels:
        values = timings[label]
        median = statistics.median(values)
        row = {'label': label, 'sha256': models[label]['sha256'], 'timings': len(values),
               'median_seconds_per_call': median, 'round_medians_seconds': round_medians[label],
               'max_seconds_per_call': max(values), 'timings_seconds': values,
               'session_open_seconds': open_seconds[label]}
        if len(values) >= MIN_TIMINGS_FOR_P95:
            row['p95_seconds_per_call'] = _percentile(values, 0.95)
        else:
            row['p95_status'] = f'not reported: fewer than {MIN_TIMINGS_FOR_P95} timings'
        if batch == 1:
            row['median_rtf'] = median / audio_seconds
            if 'p95_seconds_per_call' in row:
                row['p95_rtf'] = row['p95_seconds_per_call'] / audio_seconds
        else:
            row['median_throughput_seconds_per_audio_second'] = median / audio_seconds
        rows.append(row)
    try:
        import onnxruntime
        runtime_version = onnxruntime.__version__
    except ImportError:  # injected sessions in tests
        runtime_version = None
    return {'schema_version': 1, 'rows': rows, 'round_orders': orders,
            'settings': {'threads': threads, 'batch': batch, 'warmup': warmup, 'repeats': repeats,
                         'rounds': rounds, 'seed': seed, 'window_samples': ox.WINDOW_SAMPLES,
                         'runtime': 'onnxruntime CPUExecutionProvider', 'input': 'seeded noise x 0.1'},
            'environment': {'measured_at_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                            'onnxruntime': runtime_version, 'python': sys.version.split()[0],
                            'platform': platform.platform(), 'processor': platform.processor(),
                            'logical_cpus': os.cpu_count()},
            'prd_targets_for_context': PRD_TARGETS,
            'limitations': 'Timings of the given models on this machine, single process, no '
                           'concurrency. Not box A1 and not the trained head unless the models say so: '
                           'no PRD target is met or missed by these numbers alone. Close results may swap '
                           'order on a loaded host (see round medians). Process memory (RSS) is not '
                           'measured here (models/ava/benchmark_cpu.py does for PyTorch).'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--model', nargs=3, action='append', metavar=('LABEL', 'PATH', 'SHA256'),
                        required=True)
    parser.add_argument('--threads', type=int, default=4)
    parser.add_argument('--batch', type=int, default=1)
    parser.add_argument('--warmup', type=int, default=2)
    parser.add_argument('--repeats', type=int, default=20)
    parser.add_argument('--rounds', type=int, default=1)
    parser.add_argument('--output', help='new JSON file outside the repository')
    args = parser.parse_args(argv)
    models = {}
    for label, path, digest in args.model:
        if label in models:
            parser.error(f'duplicate label {label!r}')
        models[label] = {'path': path, 'sha256': digest}
    try:
        report = benchmark(models, threads=args.threads, batch=args.batch, warmup=args.warmup,
                           repeats=args.repeats, rounds=args.rounds)
    except (ValueError, RuntimeError) as error:
        print(f'ONNX benchmark failed: {error}', file=sys.stderr)
        return 1
    text = json.dumps(report, indent=2)
    if args.output:
        output = Path(args.output).resolve()
        if output.is_relative_to(Path(__file__).resolve().parents[2]):
            print('ONNX benchmark failed: output must be outside the repository', file=sys.stderr)
            return 1
        with output.open('x', encoding='utf-8') as stream:
            stream.write(text)
    print(text)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
