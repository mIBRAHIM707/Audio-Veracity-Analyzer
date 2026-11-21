"""Claude-authored tests (9 November 2026) for services/inference/analysis.py, backlog 2.1.

Authored by Claude (Claude Code), not by GPT-6 Astra. Fictional synthetic PCM and
an injected fake detector: these test orchestration, bounds and failure handling,
not model accuracy. The real-checkpoint smoke is recorded separately.
"""
import io
import threading
import unittest
import wave

import numpy as np

from services.inference import analysis

RATE = 16000


def wav(seconds, rate=RATE, channels=1, amplitude=3000):
    t = np.arange(round(seconds * rate)) / rate
    mono = np.rint(amplitude * np.sin(2 * np.pi * 300 * t)).astype("<i2")
    data = mono if channels == 1 else np.stack([mono, mono], axis=1).reshape(-1)
    stream = io.BytesIO()
    with wave.open(stream, "wb") as audio:
        audio.setnchannels(channels)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(data.astype("<i2").tobytes())
    return stream.getvalue()


class FakeDetector:
    def __init__(self, logits=(2.0, 0.0)):
        self.logits, self.calls = logits, []

    def __call__(self, samples):
        self.calls.append(np.array(samples, copy=True))
        return list(self.logits)


def service(detector=None, factory=None):
    detector = detector or FakeDetector()
    return analysis.AnalysisService(factory or (lambda: detector), model_id="fx-model"), detector


class WindowPlanTests(unittest.TestCase):
    def test_full_windows_every_two_seconds_and_partial_tail(self):
        cases = {1.0: [], 1.5: [(0, 1.5, True)], 4.0: [(0, 4, False)],
                 5.0: [(0, 4, False)], 5.5: [(0, 4, False), (2, 5.5, True)],
                 8.0: [(0, 4, False), (2, 6, False), (4, 8, False)],
                 9.6: [(0, 4, False), (2, 6, False), (4, 8, False), (6, 9.6, True)]}
        for seconds, expected in cases.items():
            with self.subTest(seconds=seconds):
                plan = analysis.plan_windows(round(seconds * RATE))
                self.assertEqual([(w.start_sample / RATE, w.end_sample / RATE, w.partial) for w in plan],
                                 [(float(s), float(e), p) for s, e, p in expected])
                self.assertEqual([w.index for w in plan], list(range(len(plan))))


class AggregationTests(unittest.TestCase):
    def test_minimum_evidence_and_partial_exclusion(self):
        one = [{"p_synthetic": 0.9, "partial": False}]
        self.assertEqual(analysis.aggregate(one)["verdict"], "undetermined")
        self.assertEqual(analysis.aggregate([])["verdict"], "undetermined")
        mixed = [{"p_synthetic": 0.2, "partial": False}, {"p_synthetic": 0.4, "partial": False},
                 {"p_synthetic": 1.0, "partial": True}]
        result = analysis.aggregate(mixed)
        self.assertEqual((result["verdict"], result["evidence_windows"]), ("genuine", 2))
        self.assertAlmostEqual(result["p_synthetic"], 0.3)
        self.assertAlmostEqual(result["margin"], 0.4)
        tie = analysis.aggregate([{"p_synthetic": 0.5, "partial": False}] * 2)
        self.assertEqual(tie["verdict"], "synthetic", "threshold is inclusive")

    def test_p_synthetic_uses_fake_first_class_order_and_rejects_bad_logits(self):
        self.assertAlmostEqual(analysis.p_synthetic([0.0, 0.0]), 0.5)
        self.assertGreater(analysis.p_synthetic([3.0, -1.0]), 0.98)
        self.assertAlmostEqual(analysis.p_synthetic([1000.0, 0.0]), 1.0)
        for bad in ([1.0], [float("nan"), 0.0], [True, 0.0], ["1", 0.0], None):
            with self.subTest(bad=bad), self.assertRaises(analysis.DetectorFailure):
                analysis.p_synthetic(bad)


class ServiceTests(unittest.TestCase):
    def test_eight_second_file_yields_three_scored_windows_with_scope_labels(self):
        svc, detector = service()
        result = svc.analyze_wav(wav(8.0))
        self.assertEqual(len(detector.calls), 3)
        self.assertTrue(all(call.shape == (64000,) and call.dtype == np.float32 for call in detector.calls))
        self.assertEqual(result["verdict"], "synthetic")
        self.assertEqual(result["calibration"], "uncalibrated_baseline")
        self.assertIn("uncalibrated", result["scope"])
        self.assertEqual(result["model_id"], "fx-model")
        self.assertEqual([w["t_start"] for w in result["timeline"]], [0.0, 2.0, 4.0])
        self.assertEqual(result["flags"], ["out_of_domain:wideband_source"], "16 kHz is wideband (FR-A1)")
        self.assertGreaterEqual(result["processing_seconds"], 0)

    def test_partial_tail_is_zero_padded(self):
        svc, detector = service()
        result = svc.analyze_wav(wav(5.5))
        self.assertTrue(result["timeline"][-1]["partial"])
        tail = detector.calls[-1]
        self.assertTrue(np.all(tail[int(3.5 * RATE):] == 0))
        self.assertTrue(np.any(tail[:int(3.5 * RATE)] != 0))
        self.assertEqual(result["evidence_windows"], 1)
        self.assertEqual(result["verdict"], "undetermined")

    def test_stereo_48k_is_downmixed_resampled_and_flagged_wideband(self):
        svc, detector = service()
        result = svc.analyze_wav(wav(6.0, rate=48000, channels=2))
        self.assertEqual(result["input"], {"sample_rate": 48000, "channels": 2})
        self.assertAlmostEqual(result["duration_seconds"], 6.0)
        self.assertIn("out_of_domain:wideband_source", result["flags"])
        self.assertEqual(len(detector.calls), 2)

    def test_narrowband_8k_is_not_flagged(self):
        svc, _ = service()
        self.assertEqual(svc.analyze_wav(wav(4.0, rate=8000))["flags"], [])

    def test_short_file_does_not_load_detector_and_is_undetermined(self):
        loads = []
        svc, _ = service(factory=lambda: loads.append(1) or FakeDetector())
        result = svc.analyze_wav(wav(1.0))
        self.assertEqual((result["verdict"], loads, result["timeline"]), ("undetermined", [], []))

    def test_detector_loads_once_across_requests(self):
        loads = []
        detector = FakeDetector()
        svc, _ = service(factory=lambda: loads.append(1) or detector)
        svc.analyze_wav(wav(4.0))
        svc.analyze_wav(wav(4.0))
        self.assertEqual(len(loads), 1)

    def test_malformed_and_unsupported_inputs_raise_422(self):
        eight_bit = io.BytesIO()
        with wave.open(eight_bit, "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(1)
            audio.setframerate(RATE)
            audio.writeframes(bytes(RATE))
        truncated = wav(4.0)[:-101]
        for payload in (b"not a wav", b"", eight_bit.getvalue(), wav(4.0, rate=7000), "text", truncated):
            with self.subTest(payload=str(payload)[:20]):
                with self.assertRaises(analysis.UnsupportedAudio) as caught:
                    service()[0].analyze_wav(payload)
                self.assertEqual(caught.exception.status, 422)

    def test_duration_boundaries_for_sync_job_and_413(self):
        frame = 1 / 8000
        svc, _ = service()
        self.assertIn(svc.analyze_wav(wav(60.0, rate=8000))["verdict"], ("synthetic", "genuine"))
        with self.assertRaises(analysis.NeedsAsyncJob) as caught:
            svc.analyze_wav(wav(60.0 + frame, rate=8000))
        self.assertEqual(caught.exception.status, 202)
        self.assertNotIsInstance(caught.exception, analysis.AnalysisError, "a job is not a failure")
        long = svc.analyze_wav(wav(61.0, rate=8000), max_seconds=analysis.MAX_ASYNC_SECONDS)
        self.assertEqual(long["evidence_windows"], 29)
        with self.assertRaises(analysis.InputTooLarge) as caught:
            svc.analyze_wav(wav(600.0 + frame, rate=8000), max_seconds=analysis.MAX_ASYNC_SECONDS)
        self.assertEqual(caught.exception.status, 413)
        with self.assertRaises(ValueError):
            svc.analyze_wav(wav(4.0), max_seconds=601)

    def test_window_contents_are_exact_slices_in_unit_range(self):
        ramp = (np.arange(int(8.5 * RATE)) % 32768 - 16384).astype("<i2")
        stream = io.BytesIO()
        with wave.open(stream, "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(RATE)
            audio.writeframes(ramp.tobytes())
        svc, detector = service()
        result = svc.analyze_wav(stream.getvalue())
        expected = ramp.astype(np.float32) / 32768
        plan = analysis.plan_windows(len(ramp))
        self.assertEqual(len(detector.calls), len(plan))
        for call, window in zip(detector.calls, plan):
            piece = expected[window.start_sample:window.end_sample]
            np.testing.assert_array_equal(call[:len(piece)], piece)
            self.assertTrue(np.all(call[len(piece):] == 0))
            self.assertTrue(-1 <= call.min() and call.max() < 1)
        self.assertEqual(set(result) >= {"verdict", "p_synthetic", "margin", "evidence_windows", "reason",
                                         "aggregation", "model_load_seconds"}, True)

    def test_result_keys_are_identical_for_decided_and_undetermined(self):
        svc, _ = service()
        decided, undetermined = svc.analyze_wav(wav(8.0)), svc.analyze_wav(wav(4.0))
        self.assertEqual(set(decided), set(undetermined))
        self.assertIsNone(undetermined["margin"])
        self.assertEqual(undetermined["reason"], "fewer than 2 full windows")
        self.assertEqual(svc.analyze_wav(wav(2.0))["reason"], "only a partial window")

    def test_failed_load_is_retried_and_load_time_reported_once(self):
        attempts = []

        def flaky():
            attempts.append(1)
            if len(attempts) == 1:
                raise OSError("fictional transient load fault")
            return FakeDetector()
        svc, _ = service(factory=flaky)
        with self.assertRaises(analysis.DetectorFailure):
            svc.analyze_wav(wav(4.0))
        first = svc.analyze_wav(wav(4.0))
        second = svc.analyze_wav(wav(4.0))
        self.assertEqual(len(attempts), 2)
        self.assertGreaterEqual(first["model_load_seconds"], 0)
        self.assertEqual(second["model_load_seconds"], 0.0)

    def test_saturation_flag_and_unexpected_conditioning_fault(self):
        loud = io.BytesIO()
        t = np.arange(4 * 48000) / 48000
        square = np.where(np.sin(2 * np.pi * 1000 * t) >= 0, 32767, -32768).astype("<i2")
        with wave.open(loud, "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(48000)
            audio.writeframes(square.tobytes())
        self.assertIn("resampling_saturation", service()[0].analyze_wav(loud.getvalue())["flags"])
        original = analysis.canonicalize
        analysis.canonicalize = lambda *args: (_ for _ in ()).throw(MemoryError())
        try:
            with self.assertRaises(analysis.AnalysisError) as caught:
                service()[0].analyze_wav(wav(4.0))
            self.assertEqual(caught.exception.status, 500)
        finally:
            analysis.canonicalize = original
        original = analysis.MAX_BYTES
        analysis.MAX_BYTES = 1000
        try:
            with self.assertRaises(analysis.InputTooLarge):
                service()[0].analyze_wav(wav(4.0))
        finally:
            analysis.MAX_BYTES = original

    def test_detector_faults_become_typed_failures(self):
        class Broken:
            def __call__(self, samples):
                raise RuntimeError("fictional CPU fault")
        cases = (Broken(), FakeDetector(logits=(float("inf"), 0.0)), lambda samples: None)
        for detector in cases:
            with self.subTest(detector=type(detector).__name__):
                with self.assertRaises(analysis.DetectorFailure) as caught:
                    service(detector)[0].analyze_wav(wav(4.0))
                self.assertEqual(caught.exception.status, 500)
        def failing_factory():
            raise OSError("fictional missing checkpoint")
        with self.assertRaises(analysis.DetectorFailure):
            service(factory=failing_factory)[0].analyze_wav(wav(4.0))

    def test_concurrent_request_is_refused_not_queued(self):
        entered, release = threading.Event(), threading.Event()

        class Slow(FakeDetector):
            def __call__(self, samples):
                entered.set()
                release.wait(5)
                return super().__call__(samples)
        svc, _ = service(Slow())
        worker = threading.Thread(target=svc.analyze_wav, args=(wav(4.0),))
        worker.start()
        self.assertTrue(entered.wait(5))
        try:
            with self.assertRaises(analysis.AnalyzerBusy) as caught:
                svc.analyze_wav(wav(4.0))
            self.assertEqual(caught.exception.status, 503)
        finally:
            release.set()
            worker.join(5)
        self.assertEqual(svc.analyze_wav(wav(4.0))["evidence_windows"], 1, "lock released after use")


class EntryPointTests(unittest.TestCase):
    def test_cli_never_prints_the_input_path(self):
        import contextlib
        from services.inference import cli
        secret = "fx-private-speaker-name/missing.wav"
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--model-root", "fx-model-root", "--wav", secret])
        self.assertEqual(code, 1)
        self.assertNotIn("fx-private-speaker-name", out.getvalue() + err.getvalue())

    def test_baseline_import_is_lazy_and_threads_are_bounded(self):
        import subprocess
        import sys
        probe = ("import sys; import services.inference.baseline as b; "
                 "print('torch' in sys.modules)")
        result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.stdout.strip(), "False", result.stderr)
        from services.inference import baseline
        for threads in (0, 65, 2.0):
            with self.subTest(threads=threads), self.assertRaises(ValueError):
                baseline.baseline_factory("fx-root", threads)


if __name__ == "__main__":
    unittest.main()
