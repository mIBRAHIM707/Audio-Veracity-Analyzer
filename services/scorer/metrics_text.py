"""Prometheus text lines for the live-pipeline snapshot (FR-O6, FR-A8), shared by the
analysis API (`services/api/app.py`) and the live service process
(`services/live/health.py`). Pure: no web framework, no call IDs, no window data.
Moved unchanged from `services/api/app.py` on 16 Nov 2026 so the live process can
render the same families without FastAPI.
"""
import math

# FR-O6 live-pipeline families (services/scorer/pipeline.py LivePipeline.metrics_snapshot).
# Intake outcomes are exclusive per offered window; resolution outcomes are exclusive per
# accepted window (accepted = resolved + waiting + in batcher).
PIPELINE_INTAKE = (('accepted', 'accepted'), ('stride_skipped', 'stride_skipped'),
                   ('call_inactive', 'offered_inactive'))
PIPELINE_RESOLVED = (('scored', 'scored'), ('dropped_oldest', 'dropped_pending'),
                     ('cancelled', 'cancelled_pending'), ('skipped_ended', 'skipped_ended'),
                     ('detector_failure', 'windows_lost_to_detector'), ('result_error', 'result_errors'),
                     ('not_applied', 'result_not_applied'))
PIPELINE_COUNTERS = (('ava_live_batches_total', 'batches', 'Live batches sent to the detector.'),
                     ('ava_live_detector_failures_total', 'detector_failures',
                      'Live batches whose detector call failed (their windows are lost).'),
                     ('ava_live_unmonitored_entries_total', 'unmonitored_entered',
                      'Times a call direction entered unmonitored at the stride cap (FR-I10).'),
                     ('ava_live_metrics_publish_failures_total', 'metrics_publish_failures',
                      'Failed hand-overs of the pipeline snapshot to the metrics reader.'),
                     ('ava_live_status_publish_failures_total', 'status_publish_failures',
                      'Failed unmonitored status publications (the window was kept).'),
                     ('ava_live_inference_timer_failures_total', 'timer_failures',
                      'Detector calls not timed because the timer failed or went backwards.'),
                     ('ava_live_file_batches_not_run_total', 'file_batches_not_run',
                      'File batches handed to the live loop and not run (file jobs are not driven there).'))
PIPELINE_GAUGES = (('ava_live_directions', 'directions', 'Call directions with a window queue.'),
                   ('ava_live_windows_waiting', 'waiting', 'Windows waiting in per-direction queues.'),
                   ('ava_live_windows_in_batcher', 'in_batcher', 'Live windows in the micro-batcher.'),
                   ('ava_live_directions_degraded', 'degraded', 'Directions scoring at a grown stride.'),
                   ('ava_live_directions_unmonitored', 'unmonitored', 'Directions at the stride cap.'),
                   ('ava_live_max_effective_stride_seconds', 'max_effective_stride_s',
                    'Largest effective stride across directions (0 with none).'))


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError('pipeline metric values must be finite non-negative numbers')
    return value


def inference_histogram(histogram):
    """`ava_live_inference_seconds{outcome}` from the pipeline's cumulative-ready bucket
    counts; the bounds travel with the snapshot and are checked, not trusted."""
    bounds, series = histogram['bounds'], histogram['series']
    if not bounds or any(not isinstance(b, (int, float)) or isinstance(b, bool) or not math.isfinite(b)
                         for b in bounds) or list(bounds) != sorted(set(bounds)) \
            or set(series) != {'ok', 'failed'}:
        raise ValueError('inference histogram bounds/series are malformed')
    lines = ['# HELP ava_live_inference_seconds Live detector call time per batch (FR-O6), by outcome.',
             '# TYPE ava_live_inference_seconds histogram']
    for outcome in ('ok', 'failed'):
        counts, count = series[outcome]['buckets'], number(series[outcome]['count'])
        if len(counts) != len(bounds) or any(number(n) > count for n in counts) or list(counts) != sorted(counts):
            raise ValueError('inference histogram buckets are malformed')
        lines += [f'ava_live_inference_seconds_bucket{{outcome="{outcome}",le="{float(bound)!r}"}} {n}'
                  for bound, n in zip(bounds, counts)]
        lines += [f'ava_live_inference_seconds_bucket{{outcome="{outcome}",le="+Inf"}} {count}',
                  f'ava_live_inference_seconds_sum{{outcome="{outcome}"}} {float(number(series[outcome]["sum"]))!r}',
                  f'ava_live_inference_seconds_count{{outcome="{outcome}"}} {count}']
    return lines


def snapshot_valid_lines(valid):
    """The validity gauge both processes export next to the pipeline families."""
    return ['# HELP ava_live_pipeline_snapshot_valid 1 when the last live-pipeline '
            'snapshot rendered (0 before the first publish or when malformed).',
            '# TYPE ava_live_pipeline_snapshot_valid gauge',
            f'ava_live_pipeline_snapshot_valid {1 if valid else 0}']


def pipeline_lines(snapshot, age_seconds):
    """Prometheus lines for the latest live-pipeline snapshot; no call IDs ever."""
    counts = snapshot['counts']
    lines = ['# HELP ava_live_windows_offered_total Windows offered to the live pipeline, by exclusive '
             'intake outcome.', '# TYPE ava_live_windows_offered_total counter']
    lines += [f'ava_live_windows_offered_total{{outcome="{label}"}} {number(counts[key])}'
              for label, key in PIPELINE_INTAKE]
    lines += ['# HELP ava_live_windows_resolved_total Accepted live windows by exclusive final outcome.',
              '# TYPE ava_live_windows_resolved_total counter']
    lines += [f'ava_live_windows_resolved_total{{outcome="{label}"}} {number(counts[key])}'
              for label, key in PIPELINE_RESOLVED]
    for name, key, text in PIPELINE_COUNTERS:
        lines += [f'# HELP {name} {text}', f'# TYPE {name} counter', f'{name} {number(counts[key])}']
    for name, key, text in PIPELINE_GAUGES:
        lines += [f'# HELP {name} {text}', f'# TYPE {name} gauge', f'{name} {number(snapshot[key])}']
    lines += inference_histogram(snapshot['inference_seconds'])
    lines += ['# HELP ava_live_pipeline_snapshot_age_seconds Seconds since the live loop last '
              'published its snapshot (grows when the loop stalls).',
              '# TYPE ava_live_pipeline_snapshot_age_seconds gauge',
              f'ava_live_pipeline_snapshot_age_seconds {number(age_seconds)!r}']
    return lines
