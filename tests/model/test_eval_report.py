"""Evaluation report generation (backlog 9.3; PRD FR-M5, FR-M6, FR-M10).

Claude-authored test design, 12 November 2026. Stub models, hand-built scores and a
generated prepared root; report mechanics only, not model results.
"""
import array
import json
import math
from pathlib import Path
import tempfile
import unittest

import torch

from models.ava.model import WINDOW_SAMPLES, build_detector
from models.ava.windows import load_windows
from models.evaluation import metrics, report as rp
from tests.model.test_ava_sls import TINY
from tests.model import test_ava_windows as loader_fixture  # module import: its tests are not re-collected

COSTS = {'c_miss': 1, 'c_fa': 10, 'pi_spoof': 0.05}
SUMMARY = {'report_sha256': 'f' * 64, 'windows': 0}


def window(index, label, value, **provenance):
    samples = array.array('h', [value] * WINDOW_SAMPLES)
    return {'path': f'clips/{index}.wav', 'sha256': f'{index:064d}', 'label': label,
            'target': 0 if label == 'synthetic' else 1, 'speaker_id': f'spk{index % 4}',
            'codec': 'g711_ulaw', 'provenance': provenance, 'samples': samples}


class StubModel(torch.nn.Module):
    """Logits from the first sample: synthetic logit = 40 * x0, genuine logit = 1."""

    def __init__(self):
        super().__init__()
        self.weight = torch.nn.Parameter(torch.tensor(40.0))
        self.seen_training = []

    def forward(self, waveform):
        self.seen_training.append(self.training)
        first = waveform[:, 0]
        logits = torch.stack([self.weight * first, torch.ones_like(first)], dim=1)
        return logits, torch.zeros(waveform.shape[0], 256)


def scored(label, p, speaker='spk', temperature=1.0, **provenance):
    z = temperature * math.log(p / (1 - p))
    return {'label': label, 'p_synthetic': rp._sigmoid(z / temperature), 'logit': z,
            'temperature': temperature, 'speaker_id': speaker, 'codec': 'g711_ulaw', 'provenance': provenance}


def scored_rows(n=20):
    rows = []
    for i in range(n):
        for label, base in (('genuine', 0.1), ('synthetic', 0.9)):
            row = scored(label, base + (i - n / 2) * 0.004, f'{label}{i % 12}',
                         language='urd' if i % 3 else 'pnb', dialect='Majhi')
            row['codec'] = 'g711_ulaw' if i % 2 else 'g711_alaw'
            rows.append(row)
    return rows


class ScoreWindowsTests(unittest.TestCase):
    def test_scores_are_softmax_of_tempered_logits_in_window_order(self):
        windows = [window(i, 'synthetic' if i % 2 else 'genuine', v)
                   for i, v in enumerate((-3000, 0, 800, 3000, 16000))]
        model = StubModel().train()
        rows = rp.score_windows(model, windows, temperature=2.0, batch_size=2)
        self.assertEqual([r['path'] for r in rows], [w['path'] for w in windows])
        self.assertEqual(set(model.seen_training), {False})
        self.assertTrue(model.training, 'training state is restored after scoring')
        self.assertEqual({r['temperature'] for r in rows}, {2.0})
        for row, w in zip(rows, windows):
            x0 = w['samples'][0] / 32768
            logits = torch.tensor([40.0 * x0, 1.0], dtype=torch.float64)
            expected = torch.softmax(logits / 2.0, dim=0)[0].item()
            self.assertAlmostEqual(row['logit'], 40.0 * x0 - 1.0, places=5)
            self.assertAlmostEqual(row['p_synthetic'], expected, places=6)
            self.assertEqual((row['label'], row['codec']), (w['label'], 'g711_ulaw'))

    def test_extreme_logits_do_not_overflow(self):
        self.assertEqual(rp._sigmoid(-1000.0), 0.0)
        self.assertEqual(rp._sigmoid(1000.0), 1.0)
        self.assertAlmostEqual(rp._sigmoid(0.0), 0.5)

    def test_refusals(self):
        windows = [window(0, 'genuine', 5)]
        for temperature in (0, -1, float('nan'), True):
            with self.assertRaises(ValueError):
                rp.score_windows(StubModel(), windows, temperature=temperature)
        with self.assertRaises(ValueError):
            rp.score_windows(StubModel(), [])

        class Bad(torch.nn.Module):
            def __init__(self, out):
                super().__init__()
                self.out = out

            def forward(self, waveform):
                return self.out, None

        for out in (torch.zeros(1, 3), torch.tensor([[float('nan'), 0.0]]), torch.zeros(2)):
            with self.assertRaises(ValueError):
                rp.score_windows(Bad(out), windows)


class BuildReportTests(unittest.TestCase):
    def build(self, rows=None, **kwargs):
        options = dict(costs=COSTS, costs_status='unconfirmed test constants', temperature_status='uncalibrated',
                       model_id='stub', windows_summary=SUMMARY, n_resamples=100)
        options.update(kwargs)
        return rp.build_report(scored_rows() if rows is None else rows, **options)

    def test_subset_table_and_calibration_use_the_metric_functions(self):
        rows = scored_rows()
        report = self.build(rows)
        full = report['subsets'][0]
        self.assertEqual((full['field'], full['genuine'], full['synthetic']), (None, 20, 20))
        genuine = [r['p_synthetic'] for r in rows if r['label'] == 'genuine']
        synthetic = [r['p_synthetic'] for r in rows if r['label'] == 'synthetic']
        self.assertEqual(full['eer'], metrics.eer(genuine, synthetic))
        self.assertEqual(full['eer'], 0.0)
        by = {(r['field'], r['value']): r for r in report['subsets']}
        self.assertEqual(by[('codec', 'g711_alaw')]['genuine'], 10)
        self.assertEqual(by[('language', 'urd')]['status'], 'ok')
        self.assertEqual(by[('language', 'pnb')]['status'], 'insufficient data')
        self.assertEqual(by[('generator_family', None)]['status'], 'no values')
        self.assertEqual(by[('dialect', 'Majhi')]['genuine'], 20)
        self.assertEqual((report['calibration']['temperature'], report['calibration']['temperature_status']),
                         (1.0, 'uncalibrated'))
        for subset in ('(e) code-switched', '(g) real phone-call set'):
            self.assertIn(subset, report['limitations'])
        p = [r['p_synthetic'] for r in rows]
        y = [r['label'] == 'synthetic' for r in rows]
        self.assertEqual(report['calibration']['ece'], metrics.ece(p, y))
        self.assertEqual(report['calibration']['reliability'], metrics.reliability_bins(p, y))
        self.assertEqual((report['costs'], report['costs_status']), (COSTS, 'unconfirmed test constants'))
        self.assertIn('Clip-level', report['limitations'])
        self.assertEqual(report['windows_summary'], SUMMARY)

    def test_single_class_skips_calibration(self):
        rows = [r for r in scored_rows() if r['label'] == 'genuine']
        report = self.build(rows)
        self.assertEqual(report['calibration']['status'], 'single class: ECE not reported')
        self.assertEqual(report['subsets'][0]['status'], 'insufficient data')

    def test_dialect_far_uses_genuine_listed_dialects_at_the_threshold(self):
        rows = scored_rows()
        rows[0]['provenance'] = {'dialect': 'majhi'}
        rows[2]['provenance'] = {}
        rows[4]['speaker_id'] = None
        report = self.build(rows, dialects=['Majhi', 'Pothwari'], threshold=0.5)
        dialect = report['dialect_far']
        self.assertEqual(dialect['unlisted_genuine_by_dialect'], {'majhi': 1, '(missing)': 1})
        self.assertEqual(dialect['listed_genuine_without_speaker'], 1)
        self.assertEqual([r['dialect'] for r in dialect['rows']], ['Majhi', 'Pothwari'])
        self.assertEqual(dialect['rows'][0]['genuine_clips'], 17)
        self.assertEqual(dialect['rows'][0]['status'], 'insufficient data')
        with self.assertRaises(ValueError):
            self.build(rows, dialects=['Majhi'])
        with self.assertRaises(ValueError):
            self.build(rows, threshold=0.5)

    def test_reportable_dialect_far_counts_scores_at_the_threshold_as_flagged(self):
        rows = [scored('genuine', 0.5 if i < 50 else 0.2, f'spk{i % 10}', dialect='Majhi') for i in range(500)]
        rows += [scored('synthetic', 0.9, 'syn')] * 10
        row = self.build(rows, dialects=['Majhi'], threshold=0.5)['dialect_far']['rows'][0]
        self.assertEqual((row['status'], row['speakers'], row['genuine_clips']), ('ok', 10, 500))
        self.assertAlmostEqual(row['far'], 50 / 500)

    def test_temperature_comes_from_consistent_rows(self):
        rows = [scored('genuine', 0.2, temperature=2.0), scored('synthetic', 0.8, temperature=2.0)]
        self.assertEqual(self.build(rows)['calibration']['temperature'], 2.0)
        mixed = [scored('genuine', 0.2, temperature=2.0), scored('synthetic', 0.8, temperature=1.0)]
        with self.assertRaisesRegex(ValueError, 'disagree'):
            self.build(mixed)
        tampered = scored_rows()
        tampered[3]['p_synthetic'] = 0.42
        with self.assertRaisesRegex(ValueError, 'does not match'):
            self.build(tampered)
        missing = scored_rows()
        del missing[0]['temperature']
        with self.assertRaisesRegex(ValueError, 'disagree'):
            self.build(missing)

    def test_refusals(self):
        for kwargs in ({'costs_status': ''}, {'model_id': ''}, {'windows_summary': {}},
                       {'temperature_status': ''}, {'costs': {'c_miss': 1}}):
            with self.assertRaises(ValueError):
                self.build(**kwargs)
        with self.assertRaises(ValueError):
            self.build([])
        with self.assertRaises(ValueError):
            self.build([dict(scored_rows()[0], label='bonafide')])


class WriteReportTests(unittest.TestCase):
    def test_writes_new_json_outside_the_repository_only(self):
        import numpy as np
        report = {'value': np.float32(0.25), 'n': np.int64(3), 'bins': np.array([1, 2])}
        with tempfile.TemporaryDirectory() as temp:
            path = rp.write_report(Path(temp) / 'eval' / 'report.json', report)
            self.assertEqual(json.loads(path.read_text(encoding='utf-8')), {'value': 0.25, 'n': 3, 'bins': [1, 2]})
            with self.assertRaisesRegex(ValueError, 'not JSON-serializable'):
                rp.write_report(Path(temp) / 'object.json', {'x': object()})
            with self.assertRaisesRegex(ValueError, 'never overwritten'):
                rp.write_report(path, report)
            with self.assertRaises(ValueError):
                rp.write_report(Path(temp) / 'nan.json', {'x': math.nan})
        with self.assertRaisesRegex(ValueError, 'outside the repository'):
            rp.write_report(Path(__file__).resolve().parent / 'report.json', report)


class EndToEndTests(unittest.TestCase):
    """Loaded prepared windows -> tiny detector scores -> report (structure only)."""

    SOURCES = loader_fixture.WindowLoaderTests.SOURCES
    setUp = loader_fixture.WindowLoaderTests.setUp  # same generated prepared root

    def test_prepared_windows_score_into_a_report(self):
        loaded = load_windows(self.prepared, 'train', 'evaluate')
        torch.manual_seed(0)
        rows = rp.score_windows(build_detector(2, TINY), loaded['windows'], batch_size=2)
        report = rp.build_report(rows, costs=COSTS, costs_status='unconfirmed', temperature_status='uncalibrated',
                                 model_id='tiny-random', windows_summary=loaded['summary'],
                                 min_clips_per_class=1, n_resamples=100)
        self.assertEqual(report['windows'], 3)
        by = {(r['field'], r['value']): r for r in report['subsets']}
        self.assertEqual((by[('codec', 'none')]['genuine'], by[('codec', 'none')]['synthetic']), (1, 2))
        self.assertIn(('language', 'pnb'), by)
        self.assertTrue(all(0 <= r['p_synthetic'] <= 1 for r in rows))


if __name__ == '__main__':
    unittest.main()
