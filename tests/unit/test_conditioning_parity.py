"""Offline/serving conditioning parity (backlog 3.1, PRD FR-D12/FR-D13, FR-I3).

Claude-authored test cases, 9 November 2026. The same native WAV goes through the
offline preparation path (`process_manifest(condition=True)`) and the analysis service
(`AnalysisService.analyze_wav`) with a recording fake detector. The canonical 16 kHz
PCM each path works on must be identical, sample for sample, and prepared clip
coordinates must address the same samples the service windows. Gain is a recorded
divergence: preparation peak-normalizes each clip; the service does not (see the
`services/inference/analysis.py` docstring). These are infrastructure tests only.
"""
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
import wave

import numpy as np

from data.prepare.conditioning import TARGET_PEAK, canonicalize, peak_normalize
from data.prepare.prepare_audio import process_manifest
from services.inference.analysis import AnalysisService, UnsupportedAudio
from tests.fixture_cleanup import cleanup_with_retry

FORMATS = [  # (native rate, channels, extra native frames, amplitude)
    (16000, 1, 0, 9000), (8000, 1, 0, 9000), (44100, 2, 0, 9000), (22050, 2, 0, 9000),
    (48000, 1, 0, 9000),
    (44100, 2, 7, 9000), (22050, 1, 1, 9000), (11025, 2, 3, 9000),  # fractional 16 kHz lengths
    (44100, 2, 0, 26000), (48000, 1, 5, 26000),  # near full scale, possible resampling overshoot
]


def native_signal(rate, channels, seconds=7.6, pause=(3.4, 3.8), amplitude=9000, extra=0):
    """Two partials with a silent pause; stereo channels differ so downmix matters.
    7.6 s leaves a 1.6 s tail, so the service's partial window covers every sample
    (a tail under 1.5 s is deliberately not scored)."""
    t = np.arange(round(rate * seconds) + extra) / rate
    left = amplitude * np.sin(2 * np.pi * 440 * t) + 0.3 * amplitude * np.sin(2 * np.pi * 1700 * t)
    left[round(pause[0] * rate):round(pause[1] * rate)] = 0
    if channels == 1:
        return np.clip(np.rint(left), -32768, 32767).astype('<i2')
    right = 0.5 * left + 0.2 * amplitude * np.sin(2 * np.pi * 900 * t)
    right[round(pause[0] * rate):round(pause[1] * rate)] = 0
    return np.clip(np.rint(np.column_stack([left, right])), -32768, 32767).astype('<i2')


def wav_bytes(frames, rate, channels, width=2):
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as stream:
        stream.setparams((channels, width, rate, 0, 'NONE', 'not compressed'))
        stream.writeframes(frames if isinstance(frames, bytes) else frames.tobytes())
    return buffer.getvalue()


class RecordingDetector:
    def __init__(self):
        self.pieces = []

    def __call__(self, piece):
        self.pieces.append(np.array(piece, copy=True))
        return [0.0, 0.0]


def service_canonical(payload):
    """Run the service and rebuild its canonical int16 samples from scored windows."""
    detector = RecordingDetector()
    result = AnalysisService(lambda: detector, model_id='fixture').analyze_wav(payload)
    total = round(result['duration_seconds'] * 16000)
    rebuilt = np.full(total, np.iinfo(np.int32).min, dtype=np.int64)
    assert len(detector.pieces) == len(result['timeline']), 'detector calls != timeline windows'
    for window, piece in zip(result['timeline'], detector.pieces):
        assert piece.dtype == np.float32 and piece.shape == (64000,), (piece.dtype, piece.shape)
        start, end = round(window['t_start'] * 16000), round(window['t_end'] * 16000)
        scaled = piece[:end - start].astype(np.float64) * 32768
        # Exact int16/32768 grid: any gain, dither or other scale would leave a remainder.
        np.testing.assert_array_equal(scaled, np.rint(scaled))
        values = scaled.astype(np.int64)
        seen = rebuilt[start:end] != np.iinfo(np.int32).min
        np.testing.assert_array_equal(rebuilt[start:end][seen], values[seen])  # overlaps agree
        rebuilt[start:end] = values
        np.testing.assert_array_equal(piece[end - start:], 0)  # partial tail zero-padded only
    return rebuilt, result, detector


class ConditioningParityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(cleanup_with_retry, temporary)
        self.root = Path(temporary.name).resolve()
        self.sources = self.root / 'sources'
        self.sources.mkdir()

    def prepare(self, payload, name):
        path = self.sources / f'{name}.wav'
        path.write_bytes(payload)
        row = {'path': path.name, 'sha256': hashlib.sha256(payload).hexdigest(),
               'speaker_id': 'fictional-speaker', 'source_id': 'fixture-source',
               'language': 'ur', 'label': 'genuine', 'split': 'unassigned'}
        manifest = self.root / f'{name}.json'
        manifest.write_text(json.dumps({'files': [row]}), encoding='utf-8')
        output = self.root / f'{name}-out'
        process_manifest(manifest, self.sources, output, condition=True)
        return output, json.loads((output / 'report.json').read_text(encoding='utf-8'))

    def test_both_paths_share_identical_canonical_samples_and_coordinates(self):
        for rate, channels, extra, amplitude in FORMATS:
            with self.subTest(rate=rate, channels=channels, extra=extra, amplitude=amplitude):
                payload = wav_bytes(native_signal(rate, channels, amplitude=amplitude, extra=extra),
                                    rate, channels)
                rebuilt, result, _ = service_canonical(payload)
                self.assertFalse((rebuilt == np.iinfo(np.int32).min).any(), 'windows left a gap')
                canonical = rebuilt.astype('<i2').tobytes()
                output, report = self.prepare(payload, f'f{rate}_{channels}_{extra}_{amplitude}')
                source = report['sources'][0]
                self.assertEqual(source['status'], 'processed', source.get('error'))
                self.assertEqual(source['sample_coordinate_rate'], 16000)
                self.assertEqual(source['conditioning']['canonical_pcm_sha256'],
                                 hashlib.sha256(canonical).hexdigest())
                self.assertEqual(source['sample_count'], len(rebuilt))
                self.assertEqual(result['input'], {'sample_rate': rate, 'channels': channels})
                self.assertEqual(('resampling_saturation' in result['flags']),
                                 bool(source['conditioning']['saturated_samples']))
                self.assertEqual(len(report['clips']), 2)  # split at the 3.4-3.8 s pause
                self.assertGreater(max(clip['start_sample'] for clip in report['clips']), 0)
                for clip in report['clips']:
                    start, end = clip['start_sample'], clip['end_sample']
                    expected, _ = peak_normalize(canonical[start * 2:end * 2])
                    with wave.open(str(output / clip['path']), 'rb') as stream:
                        self.assertEqual((stream.getframerate(), stream.getnchannels(),
                                          stream.getsampwidth()), (16000, 1, 2))
                        self.assertEqual(stream.readframes(stream.getnframes()), expected)

    def test_service_preserves_level_while_preparation_normalizes_each_clip(self):
        signals = {a: native_signal(22050, 2, amplitude=a) for a in (2000, 8000)}
        quiet, loud = (wav_bytes(signals[a], 22050, 2) for a in (2000, 8000))
        quiet_samples, _, _ = service_canonical(quiet)
        loud_samples, _, _ = service_canonical(loud)
        for amplitude, samples in ((2000, quiet_samples), (8000, loud_samples)):
            canonical, _ = canonicalize(signals[amplitude].tobytes(), 22050, 2)
            # No gain before the detector: its peak is the unnormalized canonical peak.
            self.assertEqual(int(np.max(np.abs(samples))),
                             int(np.max(np.abs(np.frombuffer(canonical, dtype='<i2').astype(np.int32)))))
        ratio = np.max(np.abs(loud_samples)) / np.max(np.abs(quiet_samples))
        self.assertAlmostEqual(ratio, 4.0, delta=0.01)
        peaks = []
        for name, payload in (('quiet', quiet), ('loud', loud)):
            output, report = self.prepare(payload, name)
            for clip in report['clips']:
                with wave.open(str(output / clip['path']), 'rb') as stream:
                    pcm = np.frombuffer(stream.readframes(stream.getnframes()), dtype='<i2')
                peaks.append(int(np.max(np.abs(pcm.astype(np.int32)))))
        self.assertEqual(len(peaks), 4)
        self.assertEqual(set(peaks), {TARGET_PEAK})  # every prepared clip lands at -3 dBFS

    def test_formats_outside_the_shared_contract_are_refused_by_both_paths(self):
        cases = {
            'pcm24': wav_bytes(b'\x00\x10\x00' * 16000, 16000, 1, width=3),
            'three_channel': wav_bytes(np.zeros(16000 * 3, dtype='<i2'), 16000, 3),
            'rate_6000': wav_bytes(np.zeros(16000, dtype='<i2'), 6000, 1),
            'rate_7999': wav_bytes(np.zeros(16000, dtype='<i2'), 7999, 1),
            'rate_192001': wav_bytes(np.zeros(16000, dtype='<i2'), 192001, 1),
        }
        for name, payload in cases.items():
            with self.subTest(case=name):
                detector = RecordingDetector()
                with self.assertRaises(UnsupportedAudio):
                    AnalysisService(lambda: detector, model_id='fixture').analyze_wav(payload)
                self.assertEqual(detector.pieces, [])
                _, report = self.prepare(payload, name)
                self.assertEqual(report['sources'][0]['status'], 'quarantined')
                self.assertEqual(report['clips'], [])


if __name__ == '__main__':
    unittest.main()
