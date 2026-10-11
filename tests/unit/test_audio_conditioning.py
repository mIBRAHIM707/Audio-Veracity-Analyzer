"""Synthetic numeric/WAV regressions; no corpus audio, GPU or network."""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import wave

import numpy as np

from data.prepare.conditioning import canonicalize, peak_normalize
from data.prepare.prepare_audio import process_manifest


def pcm(values):
    return np.asarray(values, dtype="<i2").tobytes()


def samples(raw):
    return np.frombuffer(raw, dtype="<i2").astype(np.float64)


def tone(rate, seconds=1, frequency=1000, amplitude=10000):
    t = np.arange(round(rate * seconds), dtype=np.float64) / rate
    return np.rint(amplitude * np.sin(2 * np.pi * frequency * t)).astype("<i2")


class ConditioningNumericTests(unittest.TestCase):
    def test_same_rate_mono_is_exact_bypass_with_hash_and_counts(self):
        original = pcm([-32768, -1000, -1, 0, 1, 1000, 32767])
        converted, info = canonicalize(original, 16000, 1)
        self.assertEqual(converted, original)
        self.assertEqual(info["resampler"], "bypass")
        self.assertEqual(info["input_rate"], 16000)
        self.assertEqual(info["input_channels"], 1)
        self.assertEqual(info["input_sample_count"], 7)
        self.assertEqual(info["output_sample_count"], 7)
        self.assertEqual(info["canonical_pcm_sha256"], hashlib.sha256(original).hexdigest())

    def test_tone_resampling_preserves_duration_frequency_and_level(self):
        for rate in (8000, 20000, 48000, 192000):
            with self.subTest(rate=rate):
                raw = tone(rate).tobytes()
                converted, info = canonicalize(raw, rate, 1)
                signal = samples(converted)
                self.assertLessEqual(abs(len(signal) - 16000), 1)
                self.assertEqual(info["input_sample_count"], rate)
                self.assertEqual(info["output_sample_count"], len(signal))
                self.assertEqual(info["resampler"], "soxr_hq")
                spectrum = np.abs(np.fft.rfft(signal))
                peak_hz = np.argmax(spectrum) * 16000 / len(signal)
                self.assertLessEqual(abs(peak_hz - 1000), 1)
                rms = np.sqrt(np.mean(signal[320:-320] ** 2))
                self.assertAlmostEqual(rms / (10000 / np.sqrt(2)), 1, delta=.02)
                self.assertTrue(np.all(np.isfinite(signal)))
                self.assertLessEqual(float(np.max(np.abs(signal))), 32768)

    def test_downsampling_attenuates_out_of_band_instead_of_aliasing(self):
        inband, _ = canonicalize(tone(20000, frequency=1000).tobytes(), 20000, 1)
        outband, _ = canonicalize(tone(20000, frequency=9000).tobytes(), 20000, 1)
        in_rms = np.sqrt(np.mean(samples(inband)[320:-320] ** 2))
        out_rms = np.sqrt(np.mean(samples(outband)[320:-320] ** 2))
        self.assertLess(out_rms / in_rms, .01, "Require at least 40 dB interior attenuation")

    def test_stereo_downmix_cancels_opposites_and_avoids_integer_overflow(self):
        opposite = np.column_stack((tone(16000), -tone(16000))).astype("<i2")
        converted, info = canonicalize(opposite.tobytes(), 16000, 2)
        self.assertEqual(np.count_nonzero(samples(converted)), 0)
        self.assertEqual(info["input_sample_count"], 16000)
        self.assertEqual(info["output_sample_count"], 16000)
        extremes = pcm([[32767, 32767], [-32768, -32768], [20000, 10000]])
        converted, _ = canonicalize(extremes, 16000, 2)
        self.assertEqual(samples(converted).tolist(), [32767, -32768, 15000])

    def test_fractional_resampling_length_is_bounded_and_repeatable(self):
        raw = tone(20000).tobytes()[:10001 * 2]
        first, first_info = canonicalize(raw, 20000, 1)
        second, second_info = canonicalize(raw, 20000, 1)
        self.assertLessEqual(abs(len(first) // 2 - 10001 * 16000 / 20000), 1)
        self.assertEqual(first, second)
        self.assertEqual(first_info, second_info)
        self.assertEqual(first_info["canonical_pcm_sha256"], hashlib.sha256(first).hexdigest())

    def test_resampling_overshoot_is_saturated_without_wraparound_and_reported(self):
        native = np.where(np.arange(48000) % 48 < 24, 32767, -32768).astype("<i2")
        converted, info = canonicalize(native.tobytes(), 48000, 1)
        signal = samples(converted)
        self.assertTrue(np.isfinite(signal).all())
        self.assertGreater(info["saturated_samples"], 0)
        self.assertEqual(float(np.max(signal)), 32767)
        self.assertEqual(float(np.min(signal)), -32768)
        self.assertEqual(info["canonical_pcm_sha256"], hashlib.sha256(converted).hexdigest())

    def test_invalid_pcm_rate_and_channels_are_rejected(self):
        for raw, rate, channels in ((b"", 16000, 1), (b"x", 16000, 1),
                                    (b"\0" * 6, 16000, 2), (b"\0\0", 7999, 1),
                                    (b"\0\0", 192001, 1), (b"\0\0", 16000, 3),
                                    (b"\0\0", 16000, True)):
            with self.subTest(rate=rate, channels=channels, length=len(raw)):
                with self.assertRaises(ValueError):
                    canonicalize(raw, rate, channels)

    def test_normalization_targets_peak_preserves_polarity_and_gain_sign(self):
        for values, sign in (([-1000, 0, 1000], 1), ([-32768, 0, 32767], -1)):
            with self.subTest(values=values):
                normalized, info = peak_normalize(pcm(values))
                signal = samples(normalized)
                self.assertLessEqual(abs(np.max(np.abs(signal)) - 23198), 1)
                self.assertLess(signal[0], 0)
                self.assertEqual(signal[1], 0)
                self.assertGreater(signal[2], 0)
                self.assertGreater(info["gain_db"] * sign, 0)
                self.assertEqual(info["target_peak_dbfs"], -3)
                self.assertAlmostEqual(info["output_peak_dbfs"], -3, delta=.001)
                repeated, repeat_info = peak_normalize(normalized)
                self.assertLessEqual(np.max(np.abs(samples(repeated) - signal)), 1)
                self.assertLess(abs(repeat_info["gain_db"]), .001)

    def test_silence_is_unchanged_with_json_safe_peak_metadata(self):
        raw = b"\0\0" * 100
        converted, info = peak_normalize(raw)
        self.assertEqual(converted, raw)
        self.assertEqual(info["gain_db"], 0)
        self.assertIsNone(info["input_peak_dbfs"])
        self.assertIsNone(info["output_peak_dbfs"])
        json.dumps(info, allow_nan=False)
        for invalid in (b"", b"x"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    peak_normalize(invalid)


class ConditionedPreparationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source_root = self.root / "sources"
        self.source_root.mkdir()
        self.manifest = self.root / "manifest.json"

    def source(self, rate=20000, seconds=7.2, amplitude=10000, pause=(3.4, 3.8)):
        signal = tone(rate, seconds, amplitude=amplitude)
        if pause:
            signal[round(pause[0] * rate):round(pause[1] * rate)] = 0
        path = self.source_root / "fictional.wav"
        with wave.open(str(path), "wb") as stream:
            stream.setparams((1, 2, rate, 0, "NONE", "not compressed"))
            stream.writeframes(signal.tobytes())
        row = {"path": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
               "clip_id": "fictional-original-clip",
               "speaker_id": "fictional-speaker", "source_id": "fixture-source",
               "source_version": "a" * 40, "source_recording_id": "fictional-recording",
               "language": "ur", "label": "genuine", "label_provenance": "publisher",
               "licence_ref": "fictional-research-review", "use_status": "approved",
               "use_scope": "noncommercial_detection_research", "cloning_status": "not_authorized",
               "split": "unassigned", "listening_status": "not_individually_reviewed"}
        self.manifest.write_text(json.dumps({"files": [row]}), encoding="utf-8")
        return path, signal.tobytes(), row

    def run_preparation(self, condition=True, name="output"):
        output = self.root / name
        if condition:
            summary = process_manifest(self.manifest, self.source_root, output, condition=True)
        else:
            summary = process_manifest(self.manifest, self.source_root, output)
        report = json.loads((output / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["summary"], summary)
        return output, report

    def test_conditioned_lineage_source_immutability_and_exact_post_segment_gain(self):
        source, native_raw, row = self.source()
        original = source.read_bytes()
        output, report = self.run_preparation()
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(report["summary"]["errors"], 0)
        self.assertEqual(len(report["clips"]), 2)
        record = report["sources"][0]
        self.assertEqual(record["sample_coordinate_rate"], 16000)
        self.assertEqual(record["conditioning"]["input_rate"], 20000)
        self.assertEqual(record["conditioning"]["input_sample_count"], 144000)
        canonical, _ = canonicalize(native_raw, 20000, 1)
        ranges = [(gap["start_sample"], gap["end_sample"]) for gap in record["uncovered_ranges"]]
        inherited = set(row) - {"path", "sha256", "speaker_id", "clip_id"}
        self.assertEqual(record["source_clip_id"], row["clip_id"])
        for clip in report["clips"]:
            start, end = clip["start_sample"], clip["end_sample"]
            self.assertTrue(48000 <= end - start <= 96000)
            for boundary in (start, end):
                if boundary not in (0, len(canonical) // 2):
                    self.assertTrue(3.4 * 16000 <= boundary <= 3.8 * 16000)
            ranges.append((start, end))
            self.assertEqual(clip["sample_coordinate_rate"], 16000)
            self.assertEqual(clip["source_sha256"], row["sha256"])
            self.assertEqual(clip["source_clip_id"], row["clip_id"])
            self.assertEqual(clip["speaker_id"], row["speaker_id"])
            self.assertEqual(clip["review_status"], "unreviewed")
            for key in inherited:
                self.assertEqual(record[key], row[key])
                self.assertEqual(clip[key], row[key])
            expected, normalization = peak_normalize(canonical[start * 2:end * 2])
            self.assertEqual(clip["normalization"], normalization)
            with wave.open(str(output / clip["path"]), "rb") as stream:
                self.assertEqual((stream.getframerate(), stream.getnchannels(), stream.getsampwidth()), (16000, 1, 2))
                self.assertEqual(stream.readframes(stream.getnframes()), expected)
            self.assertEqual(hashlib.sha256((output / clip["path"]).read_bytes()).hexdigest(), clip["sha256"])
        cursor = 0
        for start, end in sorted(ranges):
            self.assertEqual(start, cursor)
            cursor = end
        self.assertEqual(cursor, len(canonical) // 2)
        self.assertEqual(record["sample_count"], cursor)

    def test_very_quiet_source_is_not_amplified_before_pause_detection(self):
        self.source(seconds=4, amplitude=30, pause=None)
        _, report = self.run_preparation()
        self.assertEqual(report["clips"], [])
        record = report["sources"][0]
        self.assertEqual(record["segmentation_outcome"], "all_quiet")
        self.assertEqual(record["uncovered_ranges"], [{"start_sample": 0, "end_sample": 64000}])

    def test_continuous_long_signal_has_no_forced_cuts_after_resampling(self):
        self.source(seconds=7, pause=None)
        _, report = self.run_preparation()
        self.assertEqual(report["clips"], [])
        self.assertEqual(report["sources"][0]["uncovered_ranges"],
                         [{"start_sample": 0, "end_sample": 112000}])

    def test_legacy_default_remains_exact_and_rejects_native_rate(self):
        self.source(seconds=4, pause=None)
        _, unsupported = self.run_preparation(condition=False, name="unsupported")
        self.assertEqual(unsupported["summary"]["errors"], 1)
        _, raw, _ = self.source(rate=16000, seconds=4, pause=None, amplitude=1000)
        output, legacy = self.run_preparation(condition=False, name="legacy")
        self.assertEqual(len(legacy["clips"]), 1)
        with wave.open(str(output / legacy["clips"][0]["path"]), "rb") as stream:
            self.assertEqual(stream.readframes(stream.getnframes()), raw)

    def test_conditioned_preflight_and_existing_output_remain_safe(self):
        self.source(seconds=4, pause=None)
        self.manifest.write_text('{"files": [{}]}', encoding="utf-8")
        with self.assertRaises(ValueError):
            process_manifest(self.manifest, self.source_root, self.root / "bad", condition=True)
        self.assertFalse((self.root / "bad").exists())
        self.source(seconds=4, pause=None)
        output, _ = self.run_preparation()
        before = (output / "report.json").read_bytes()
        with self.assertRaises(ValueError):
            process_manifest(self.manifest, self.source_root, output, condition=True)
        self.assertEqual((output / "report.json").read_bytes(), before)

    def test_native_clipping_survives_stereo_downmix_in_source_and_clip_flags(self):
        source, _, row = self.source(rate=16000, seconds=4, pause=None)
        left = np.where(np.arange(64000) % 40 < 20, 32767, -32768)
        raw = np.column_stack((left, np.zeros(64000))).astype("<i2").tobytes()
        canonical, info = canonicalize(raw, 16000, 2)
        self.assertGreater(info["native_near_full_scale_fraction"], .001)
        self.assertLess(float(np.max(np.abs(samples(canonical)))), 20000)
        with wave.open(str(source), "wb") as stream:
            stream.setparams((2, 2, 16000, 0, "NONE", "not compressed"))
            stream.writeframes(raw)
        original = source.read_bytes()
        row["sha256"] = hashlib.sha256(original).hexdigest()
        self.manifest.write_text(json.dumps({"files": [row]}), encoding="utf-8")
        _, report = self.run_preparation()
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(report["summary"]["errors"], 0)
        self.assertEqual(len(report["clips"]), 1)
        record = report["sources"][0]
        self.assertGreater(record["conditioning"]["native_near_full_scale_fraction"], .001)
        self.assertIn("native_possible_digital_clipping", record["metrics"]["review_flags"])
        self.assertIn("native_possible_digital_clipping", report["clips"][0]["review_flags"])
        self.assertLessEqual(report["clips"][0]["normalization"]["output_peak_dbfs"], -2.999)

    def test_conditioned_mixed_batch_quarantines_bad_inputs_and_keeps_valid_sibling(self):
        valid, _, row = self.source(rate=20000, seconds=4, pause=None)
        records = [row]
        wrong_hash = self.source_root / "wrong-hash.wav"
        wrong_hash.write_bytes(valid.read_bytes())
        records.append({**row, "path": wrong_hash.name, "sha256": "0" * 64,
                        "clip_id": "fictional-wrong-hash"})
        stereo = self.source_root / "truncated-stereo.wav"
        with wave.open(str(stereo), "wb") as stream:
            stream.setparams((2, 2, 20000, 0, "NONE", "not compressed"))
            stream.writeframes(np.column_stack((tone(20000), tone(20000))).astype("<i2").tobytes())
        stereo.write_bytes(stereo.read_bytes()[:-4])
        records.append({**row, "path": stereo.name, "sha256": hashlib.sha256(stereo.read_bytes()).hexdigest(),
                        "clip_id": "fictional-truncated"})
        pcm8 = self.source_root / "unsupported-8bit.wav"
        with wave.open(str(pcm8), "wb") as stream:
            stream.setparams((1, 1, 16000, 0, "NONE", "not compressed"))
            stream.writeframes(b"\x80" * 64000)
        records.append({**row, "path": pcm8.name, "sha256": hashlib.sha256(pcm8.read_bytes()).hexdigest(),
                        "clip_id": "fictional-pcm8"})
        originals = {path: path.read_bytes() for path in (valid, wrong_hash, stereo, pcm8)}
        self.manifest.write_text(json.dumps({"files": records}), encoding="utf-8")
        _, report = self.run_preparation()
        self.assertEqual(report["summary"]["sources"], 4)
        self.assertEqual(report["summary"]["errors"], 3)
        self.assertEqual(report["summary"]["clips"], 1)
        states = {source["path"]: source for source in report["sources"]}
        self.assertEqual(states[valid.name]["status"], "processed")
        for bad in (wrong_hash, stereo, pcm8):
            self.assertEqual(states[bad.name]["status"], "quarantined")
            self.assertTrue(states[bad.name]["error"])
        self.assertEqual(report["clips"][0]["source_path"], valid.name)
        self.assertEqual(report["clips"][0]["source_sha256"], row["sha256"])
        self.assertEqual(report["clips"][0]["source_clip_id"], row["clip_id"])
        for path, before in originals.items():
            self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
