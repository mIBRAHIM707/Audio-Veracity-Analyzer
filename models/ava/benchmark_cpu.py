"""CPU timing of the AVA window model per kept-layer cut (PRD §6.1, task 2.10 proxy;
architecture §4.1 sweep; backlog 8.QA).

Builds the real-size truncated MMS-300M + SLS architecture with **random weights**
(compute cost depends mainly on the architecture, not the weight values), and runs
``rounds`` rounds; each round visits the cuts in a seeded random order (interleaving
spreads host-load drift across cuts), builds the model, makes ``warmup`` untimed
calls, then times ``repeats`` forwards of ``float32[batch, 64000]`` noise windows in
eval and inference mode with ``threads`` intra-op threads. Per cut it reports the
median, the per-round medians (run-to-run spread), the maximum and raw timings; p95
only when at least 20 timings exist; PRD RTF (seconds per second of audio, so
per-window latency = RTF x 4 s) only at batch 1, otherwise seconds per audio second
labelled as throughput; RSS before the build and the sampled peak.

What this is not: PRD §6.1 targets are for the INT8 ONNX model on reference box A1.
These are FP32 PyTorch timings on whatever machine runs them; they are indicative,
not a bound on INT8 latency and not a reliable ranking of close cuts on a noisy host.
Single process, no concurrency.
"""
import argparse
import datetime
import gc
import json
import platform
import random
import statistics
import sys
import time

from models.ava.backbone import SWEEP_LAYERS
from models.ava.model import SAMPLE_RATE, WINDOW_SAMPLES, build_detector

PRD_TARGETS = {'p95_latency_seconds': 1.5, 'rtf': 0.375,
               'conditions': 'INT8 ONNX, 4 threads, batch 1, reference box A1 (PRD §6.1)'}
MIN_TIMINGS_FOR_P95 = 20


def _percentile(values, fraction):
    ordered = sorted(values)
    position = fraction * (len(ordered) - 1)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def benchmark(kept_layers=SWEEP_LAYERS, *, threads=4, batch=1, warmup=2, repeats=10, rounds=1,
              seed=0, base=None, clock=time.perf_counter):
    """Return the benchmark report dictionary (see module docstring)."""
    import psutil
    import torch
    if (not isinstance(kept_layers, (list, tuple)) or not kept_layers
            or any(type(k) is not int or k < 1 for k in kept_layers) or len(set(kept_layers)) != len(kept_layers)):
        raise ValueError('kept_layers must be distinct positive integers')
    for name, value, low in (('threads', threads, 1), ('batch', batch, 1), ('warmup', warmup, 0),
                             ('repeats', repeats, 2), ('rounds', rounds, 1), ('seed', seed, 0)):
        if type(value) is not int or value < low:
            raise ValueError(f'{name} must be an integer >= {low}')
    previous_threads, rng_state = torch.get_num_threads(), torch.random.get_rng_state()
    torch.set_num_threads(threads)
    process = psutil.Process()
    audio_seconds = batch * WINDOW_SAMPLES / SAMPLE_RATE
    timings = {kept: [] for kept in kept_layers}
    round_medians = {kept: [] for kept in kept_layers}
    rss_before, rss_peak, parameters, orders = {}, {}, {}, []
    try:
        for round_index in range(rounds):
            order = list(kept_layers)
            random.Random(seed * 1000 + round_index).shuffle(order)
            orders.append(order)
            for kept in order:
                gc.collect()
                rss_before.setdefault(kept, process.memory_info().rss)
                torch.manual_seed(seed)
                model = build_detector(kept, base).eval()
                parameters[kept] = sum(p.numel() for p in model.parameters())
                window = torch.randn(batch, WINDOW_SAMPLES, generator=torch.Generator().manual_seed(seed)) * 0.1
                peak, these = process.memory_info().rss, []
                with torch.inference_mode():
                    for _ in range(warmup):
                        model(window)
                    for _ in range(repeats):
                        start = clock()
                        model(window)
                        these.append(clock() - start)
                        peak = max(peak, process.memory_info().rss)
                if any(t <= 0 for t in these):
                    raise RuntimeError('Clock returned a non-positive duration')
                timings[kept].extend(these)
                round_medians[kept].append(statistics.median(these))
                rss_peak[kept] = max(rss_peak.get(kept, 0), peak)
                del model
    finally:
        torch.set_num_threads(previous_threads)
        torch.random.set_rng_state(rng_state)
    rows = []
    for kept in kept_layers:
        values = timings[kept]
        median = statistics.median(values)
        row = {'kept_layers': kept, 'parameters': parameters[kept], 'timings': len(values),
               'median_seconds_per_call': median, 'round_medians_seconds': round_medians[kept],
               'max_seconds_per_call': max(values), 'timings_seconds': values,
               'rss_before_build_bytes': rss_before[kept], 'sampled_peak_rss_bytes': rss_peak[kept]}
        if len(values) >= MIN_TIMINGS_FOR_P95:
            row['p95_seconds_per_call'] = _percentile(values, 0.95)
        else:
            row['p95_status'] = f'not reported: fewer than {MIN_TIMINGS_FOR_P95} timings'
        if batch == 1:  # PRD RTF is defined at batch 1 only
            row['median_rtf'] = median / audio_seconds
            if 'p95_seconds_per_call' in row:
                row['p95_rtf'] = row['p95_seconds_per_call'] / audio_seconds
        else:
            row['median_throughput_seconds_per_audio_second'] = median / audio_seconds
        rows.append(row)
    return {'schema_version': 2, 'rows': rows, 'round_orders': orders,
            'settings': {'threads': threads, 'batch': batch, 'warmup': warmup, 'repeats': repeats,
                         'rounds': rounds, 'seed': seed, 'window_samples': WINDOW_SAMPLES,
                         'precision': 'float32', 'runtime': 'PyTorch eager, random weights'},
            'environment': {'measured_at_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                            'torch': torch.__version__, 'python': sys.version.split()[0],
                            'platform': platform.platform(), 'processor': platform.processor(),
                            'logical_cpus': psutil.cpu_count(logical=True),
                            'physical_cpus': psutil.cpu_count(logical=False)},
            'prd_targets_for_context': PRD_TARGETS,
            'limitations': 'FP32 PyTorch eager with random weights on this machine, not INT8 ONNX on box A1: '
                           'no PRD target is met or missed by these numbers, and they do not bound INT8 '
                           'latency. Close cuts may swap order on a loaded host (see round medians). '
                           'Random weights do not reproduce trained-weight value effects such as '
                           'denormals. RSS is sampled process memory including the Python runtime; '
                           'later cuts in the same process may carry earlier allocations. The processor '
                           'string is a family/model identifier, not a brand name.'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--kept-layers', type=int, nargs='+', default=list(SWEEP_LAYERS))
    parser.add_argument('--threads', type=int, default=4)
    parser.add_argument('--batch', type=int, default=1)
    parser.add_argument('--warmup', type=int, default=2)
    parser.add_argument('--repeats', type=int, default=10)
    parser.add_argument('--rounds', type=int, default=1)
    args = parser.parse_args(argv)
    try:
        report = benchmark(args.kept_layers, threads=args.threads, batch=args.batch,
                           warmup=args.warmup, repeats=args.repeats, rounds=args.rounds)
    except (ValueError, RuntimeError) as error:
        print(f'CPU benchmark failed: {error}', file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
