"""Tiny CPU/fake-backend tests; never load the real 1.27 GB detector fixture."""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave

import torch
import safetensors.torch

from models.smoke import antideepfake as smoke


class TinyBackbone(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.scale = torch.nn.Parameter(torch.ones(1))
        self.observed = None

    def forward(self, waveform):
        self.observed = (waveform.clone(), self.training, torch.is_inference_mode_enabled())
        features = torch.zeros((1, 2, 1024))
        features[0, :, :2] = torch.tensor([[1., 3.], [5., 7.]])
        return features * self.scale, None


class SmokeLoadingTests(unittest.TestCase):
    def state(self):
        weight = torch.zeros((2, 1024))
        weight[:, :2] = torch.tensor([[2., -1.], [-1., 3.]])
        return {"m_ssl.model.scale": torch.tensor([2.]),
                "proj_fc.weight": weight, "proj_fc.bias": torch.tensor([1., 3.])}

    def load(self, state):
        backbone, head = TinyBackbone(), torch.nn.Linear(1024, 2)
        # This isolates strict-loading mechanics; it does not assert fairseq conversion parity.
        with patch("torchaudio.models.wav2vec2.utils.import_fairseq._convert_state_dict",
                   side_effect=lambda state: state):
            coverage = smoke.load_state_strict(backbone, head, state)
        return backbone, head, coverage

    def test_strict_load_retains_checkpoint_head_and_lists_only_allowed_omissions(self):
        state = self.state()
        state["m_ssl.model.mask_emb"] = torch.zeros(1)
        backbone, head, coverage = self.load(state)
        self.assertEqual(backbone.scale.tolist(), [2.])
        self.assertTrue(torch.equal(head.weight, state["proj_fc.weight"]))
        self.assertTrue(torch.equal(head.bias, state["proj_fc.bias"]))
        self.assertTrue(coverage["strict"])
        self.assertEqual(coverage["head_tensors_loaded"], 2)
        self.assertEqual(coverage["backbone_tensors_loaded"], 1)
        self.assertEqual(coverage["omitted_pretraining_keys"], ["m_ssl.model.mask_emb"])
        self.assertEqual(coverage["missing_keys"], [])

    def test_missing_extra_misshaped_or_nonfinite_weights_never_get_random_fallback(self):
        cases = []
        for missing in ("m_ssl.model.scale", "proj_fc.weight", "proj_fc.bias"):
            state = self.state()
            del state[missing]
            cases.append(state)
        cases += [{**self.state(), "surprise.weight": torch.ones(1)},
                  {**self.state(), "m_ssl.model.extra": torch.ones(1)},
                  {**self.state(), "proj_fc.weight": torch.zeros((2, 4))},
                  {**self.state(), "proj_fc.bias": torch.tensor([float("nan"), 0.])},
                  {**self.state(), "m_ssl.model.scale": torch.ones(1, dtype=torch.float64)}]
        for index, state in enumerate(cases):
            with self.subTest(case=index):
                with self.assertRaises((ValueError, RuntimeError)):
                    self.load(state)

    def test_detector_uses_verified_snapshot_nonzero_head_mean_pooling_and_inference(self):
        backbone = TinyBackbone()
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "fictional.safetensors"
            safetensors.torch.save_file(self.state(), str(checkpoint))
            original = checkpoint.read_bytes()
            real_load = safetensors.torch.load

            def mutate_path_after_snapshot(payload):
                self.assertIsInstance(payload, bytes)
                self.assertEqual(payload, original)
                checkpoint.write_bytes(b"changed after verified snapshot")
                return real_load(payload)

            with patch("torchaudio.models.wav2vec2_model", return_value=backbone), patch(
                    "torchaudio.models.wav2vec2.utils.import_fairseq._convert_state_dict",
                    side_effect=lambda state: state), patch.object(
                    smoke, "MODEL_SIZE", len(original)), patch.object(
                    smoke, "MODEL_SHA256", hashlib.sha256(original).hexdigest()), patch(
                    "safetensors.torch.load", side_effect=mutate_path_after_snapshot):
                detector = smoke.CpuDetector(checkpoint)
                result = detector([1., 2., 3.])
        # Mean features are [6,10]; W*[6,10]+[1,3] is [3,27].
        # Bias-only, first-frame, last-frame, and sum pooling produce other logits.
        self.assertEqual(result, [3., 27.])
        waveform, training, inference = backbone.observed
        self.assertFalse(training)
        self.assertTrue(inference)
        self.assertEqual(waveform.device.type, "cpu")
        self.assertAlmostEqual(float(waveform.mean()), 0, places=6)
        self.assertEqual(tuple(waveform.shape), (1, 3))

    def test_converter_dropped_keys_and_alias_collisions_are_rejected(self):
        target = "torchaudio.models.wav2vec2.utils.import_fairseq._convert_state_dict"
        for mode in ("drop", "collision"):
            with self.subTest(mode=mode):
                state = self.state()
                state["m_ssl.model.alias"] = torch.ones(1)
                converter = (lambda _: {}) if mode == "drop" else (lambda entry: {"scale": next(iter(entry.values()))})
                with patch(target, side_effect=converter):
                    with self.assertRaises(ValueError):
                        smoke.load_state_strict(TinyBackbone(), torch.nn.Linear(1024, 2), state)

    def test_legacy_position_weight_names_load_into_current_parametrizations(self):
        backbone = torch.nn.Module()
        current = backbone
        for component in ("encoder", "transformer", "pos_conv_embed"):
            nested = torch.nn.Module()
            setattr(current, component, nested)
            current = nested
        current.conv = torch.nn.utils.parametrizations.weight_norm(torch.nn.Conv1d(2, 2, 3))
        expected = {name: torch.full_like(value, .25) for name, value in backbone.state_dict().items()}
        state = {key: value for key, value in self.state().items() if key.startswith("proj_fc.")}
        for name, value in expected.items():
            old = name.replace("parametrizations.weight.original0", "weight_g").replace(
                "parametrizations.weight.original1", "weight_v")
            state["m_ssl.model." + old] = value
        with patch("torchaudio.models.wav2vec2.utils.import_fairseq._convert_state_dict", side_effect=lambda state: state):
            coverage = smoke.load_state_strict(backbone, torch.nn.Linear(1024, 2), state)
        self.assertEqual(coverage["backbone_tensors_loaded"], 3)
        for name, value in backbone.state_dict().items():
            self.assertTrue(torch.equal(value, expected[name]), name)


class SmokeNumericTests(unittest.TestCase):
    def test_waveform_normalization_is_whole_waveform_and_finite_for_constant(self):
        result = smoke.normalize_waveform([1., 2., 3.])
        self.assertEqual(tuple(result.shape), (1, 3))
        self.assertEqual(result.device.type, "cpu")
        self.assertAlmostEqual(float(result.mean()), 0, places=6)
        self.assertTrue(torch.allclose(result, torch.tensor([[-1.2247449, 0, 1.2247449]]), atol=2e-5))
        constant = smoke.normalize_waveform([7.] * 48000)
        self.assertTrue(torch.isfinite(constant).all())
        self.assertEqual(int(torch.count_nonzero(constant)), 0)

    def test_class_order_probability_and_nonfinite_output_rejection(self):
        fake = smoke.score_logits([2., -2.])
        real = smoke.score_logits([-2., 2.])
        self.assertEqual(fake["predicted_label"], "synthetic")
        self.assertEqual(real["predicted_label"], "genuine")
        self.assertGreater(fake["probabilities"][0], .98)
        self.assertGreater(real["probabilities"][1], .98)
        self.assertAlmostEqual(sum(fake["probabilities"]), 1, places=6)
        self.assertEqual(fake["logits"], [2., -2.])
        for invalid in ([], [1.], [1., 2., 3.], [float("nan"), 0], [0, float("inf")]):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    smoke.score_logits(invalid)


class FakeDetector:
    """Injected scores prove runner plumbing only, never actual model quality."""
    def __init__(self):
        self.loading_coverage = {"fixture_only": True, "description": "fictional fake backend"}
        self.calls = []

    def __call__(self, samples):
        self.calls.append(samples)
        return [3., -3.]


class SmokeFilesystemTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.models = self.root / "model"
        self.sources = self.root / "audio"
        self.models.mkdir()
        self.sources.mkdir()
        self.output = self.root / "result"
        self.manifest = self.root / "inputs.json"
        self.detector = FakeDetector()
        self.loads = []
        checkpoint = self.models / "model.safetensors"
        checkpoint.write_bytes(b"fictional bytes; fake backend never deserializes")
        # Pin the tiny fixture explicitly; this test does not validate the real artifact.
        fixture_pin = patch.object(smoke, "MODEL_SHA256", hashlib.sha256(checkpoint.read_bytes()).hexdigest())
        fixture_pin.start()
        self.addCleanup(fixture_pin.stop)
        fixture_size = patch.object(smoke, "MODEL_SIZE", checkpoint.stat().st_size)
        fixture_size.start()
        self.addCleanup(fixture_size.stop)
        card = self.models / "README.md"
        card.write_text("Fictional model card fixture", encoding="utf-8")
        self.metadata = {
            "repo": "nii-yamagishilab/mms-300m-anti-deepfake",
            "revision": "7928e119e09f616b2ce698892e00ca22dbaa40bf",
            "class_order": ["fake", "real"], "input_rate": 16000,
            "files": [{"path": path.name, "size": path.stat().st_size,
                       "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                      for path in (checkpoint, card)],
        }
        self.write_metadata()
        self.rows = [self.audio("genuine.wav", "genuine"), self.audio("synthetic.wav", "synthetic")]
        self.write_manifest()

    def audio(self, name, label, rate=16000, seconds=3, frames=None):
        path = self.sources / name
        count = rate * seconds if frames is None else frames
        with wave.open(str(path), "wb") as stream:
            stream.setparams((1, 2, rate, 0, "NONE", "not compressed"))
            stream.writeframes(b"\xe8\x03\x18\xfc" * (count // 2) + b"\xe8\x03" * (count % 2))
        return {"path": name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "label": label, "speaker_id": "fictional-speaker", "source_id": "fictional",
                "source_recording_id": "fictional-recording", "source_sha256": "a" * 64,
                "source_clip_id": "fictional-original", "benchmark_only": True}

    def write_metadata(self):
        (self.models / "model-source.json").write_text(json.dumps(self.metadata), encoding="utf-8")

    def write_manifest(self):
        self.manifest.write_text(json.dumps({"files": self.rows}), encoding="utf-8")

    def loader(self, checkpoint):
        self.loads.append(Path(checkpoint))
        self.assertEqual(Path(checkpoint), self.models / "model.safetensors")
        return self.detector

    def run_smoke(self, output=None):
        return smoke.run_smoke(self.models, self.manifest, self.sources, output or self.output,
                               threads=1, load_detector=self.loader)

    def test_fake_backend_preserves_manifest_order_lineage_and_separates_warmup(self):
        originals = {path: path.read_bytes() for path in self.sources.iterdir()}
        summary = self.run_smoke()
        report = json.loads((self.output / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(summary, report["summary"])
        self.assertEqual([trial["path"] for trial in report["trials"]], [row["path"] for row in self.rows])
        self.assertEqual(len(self.loads), 1)
        self.assertEqual(len(self.detector.calls), len(self.rows) + 1)
        self.assertIn("warmup", report)
        self.assertTrue(report["loading_coverage"]["fixture_only"])
        for trial, row in zip(report["trials"], self.rows):
            self.assertEqual(trial["sha256"], row["sha256"])
            self.assertEqual(trial["label"], row["label"])
            self.assertEqual(trial["source_clip_id"], row["source_clip_id"])
            self.assertEqual(trial["predicted_label"], "synthetic")
            self.assertGreaterEqual(trial["latency_seconds"], 0)
            self.assertAlmostEqual(sum(trial["probabilities"]), 1, places=6)
        self.assertIn("rss", json.dumps(report["memory"]).lower())
        self.assertGreater(report["memory"]["sampled_peak_rss_bytes"], 0)
        self.assertGreaterEqual(report["memory"]["samples"], 1)
        self.assertIn("sampled, not exact peak", report["memory"]["scope"])
        self.assertEqual(report["class_order"], ["fake", "real"])
        for path, before in originals.items():
            self.assertEqual(path.read_bytes(), before)
        self.assertFalse((self.output / "INCOMPLETE").exists())

    def test_repeated_fake_run_preserves_inputs_scores_but_not_timing_assumptions(self):
        self.run_smoke()
        self.run_smoke(self.root / "repeat")
        first = json.loads((self.output / "report.json").read_text(encoding="utf-8"))
        second = json.loads((self.root / "repeat/report.json").read_text(encoding="utf-8"))
        for a, b in zip(first["trials"], second["trials"]):
            for key in ("path", "sha256", "label", "logits", "probabilities", "predicted_label"):
                self.assertEqual(a[key], b[key])

    def test_bad_audio_hash_path_or_format_fails_all_before_backend_and_output(self):
        good = self.rows[0]
        bad_hash = {**self.rows[1], "sha256": "0" * 64}
        unsupported = self.audio("8khz.wav", "synthetic", rate=8000)
        short = self.audio("short.wav", "synthetic", seconds=2)
        long = self.audio("long.wav", "synthetic", seconds=7)
        truncated = self.audio("truncated.wav", "synthetic")
        path = self.sources / truncated["path"]
        path.write_bytes(path.read_bytes()[:-2])
        truncated["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        for index, bad in enumerate((bad_hash, {**good, "path": "../escape.wav"},
                                     {**good, "path": "/absolute.wav"}, unsupported, short, long, truncated)):
            with self.subTest(index=index):
                self.rows = [good, bad]
                self.write_manifest()
                output = self.root / f"invalid-{index}"
                with self.assertRaises((ValueError, OSError)):
                    self.run_smoke(output)
                self.assertEqual(self.loads, [])
                self.assertFalse(output.exists())

    def test_model_explicit_file_list_hashes_paths_and_class_order_are_enforced(self):
        original = json.loads(json.dumps(self.metadata))
        modifications = [lambda data: data["files"][0].update(sha256="0" * 64),
                         lambda data: data["files"][1].update(path="../README.md"),
                         lambda data: data["files"][1].update(size=data["files"][1]["size"] + 1),
                         lambda data: data.update(class_order=["real", "fake"]),
                         lambda data: data.update(input_rate=8000),
                         lambda data: data.pop("class_order"), lambda data: data.pop("input_rate")]
        for index, modify in enumerate(modifications):
            with self.subTest(index=index):
                self.metadata = json.loads(json.dumps(original))
                modify(self.metadata)
                self.write_metadata()
                output = self.root / f"bad-model-{index}"
                with self.assertRaises((ValueError, OSError)):
                    self.run_smoke(output)
                self.assertEqual(self.loads, [])
                self.assertFalse(output.exists())
        self.metadata = original
        self.write_metadata()
        card = self.models / "README.md"
        payload = card.read_bytes()
        card.write_bytes(bytes([payload[0] ^ 1]) + payload[1:])
        with self.assertRaises(ValueError):
            self.run_smoke()
        self.assertEqual(self.loads, [])
        self.assertFalse(self.output.exists())

    def test_exact_audio_frame_boundaries_accept_six_seconds_and_reject_one_outside(self):
        for frames in (47999, 96001):
            with self.subTest(frames=frames):
                self.rows = [self.audio(f"frames-{frames}.wav", "genuine", frames=frames)]
                self.write_manifest()
                destination = self.root / f"frames-{frames}"
                with self.assertRaises(ValueError):
                    self.run_smoke(destination)
                self.assertFalse(destination.exists())
                self.assertEqual(self.loads, [])
        self.rows = [self.audio("six-seconds.wav", "genuine", frames=96000)]
        self.write_manifest()
        summary = self.run_smoke()
        self.assertEqual(summary["trials"], 1)
        self.assertEqual(summary["audio_seconds"], 6)

    def test_nonfinite_manifest_and_model_metadata_fail_before_loader(self):
        for index, value in enumerate((float("nan"), float("inf"), float("-inf"))):
            for location in ("manifest", "model"):
                with self.subTest(value=value, location=location):
                    self.rows[0].pop("unexpected_numeric_metadata", None)
                    self.metadata.pop("unexpected_numeric_metadata", None)
                    (self.rows[0] if location == "manifest" else self.metadata)["unexpected_numeric_metadata"] = value
                    self.write_manifest()
                    self.write_metadata()
                    destination = self.root / f"nonfinite-{location}-{index}"
                    with self.assertRaises(ValueError):
                        self.run_smoke(destination)
                    self.assertEqual(self.loads, [])
                    self.assertFalse(destination.exists())
        self.rows[0].pop("unexpected_numeric_metadata", None)
        self.metadata.pop("unexpected_numeric_metadata", None)
        for index, literal in enumerate(("1e999", "-1e999")):
            for location in ("manifest", "model"):
                with self.subTest(literal=literal, location=location):
                    self.write_manifest()
                    self.write_metadata()
                    path = self.manifest if location == "manifest" else self.models / "model-source.json"
                    document = path.read_text(encoding="utf-8")
                    path.write_text(document[:-1] + ', "overflow_metadata": ' + literal + '}', encoding="utf-8")
                    destination = self.root / f"overflow-{location}-{index}"
                    with self.assertRaises(ValueError):
                        self.run_smoke(destination)
                    self.assertEqual(self.loads, [])
                    self.assertFalse(destination.exists())

    def test_checkpoint_replacement_after_preflight_fails_before_prediction(self):
        checkpoint = self.models / "model.safetensors"
        safetensors.torch.save_file(SmokeLoadingTests().state(), str(checkpoint))
        original = checkpoint.read_bytes()
        digest = hashlib.sha256(original).hexdigest()
        self.metadata["files"][0].update(size=len(original), sha256=digest)
        self.write_metadata()
        backbone = TinyBackbone()

        def replace_before_load(path):
            changed = original[:-1] + bytes([original[-1] ^ 1])
            Path(path).write_bytes(changed)
            return smoke.CpuDetector(path)

        with patch.object(smoke, "MODEL_SIZE", len(original)), patch.object(
                smoke, "MODEL_SHA256", digest), patch("torchaudio.models.wav2vec2_model", return_value=backbone):
            with self.assertRaises(ValueError):
                smoke.run_smoke(self.models, self.manifest, self.sources, self.output,
                                threads=1, load_detector=replace_before_load)
        self.assertIsNone(backbone.observed)
        self.assertFalse((self.output / "report.json").exists())
        self.assertTrue((self.output / "INCOMPLETE").exists())

    def test_report_write_failure_keeps_incomplete_marker(self):
        real_write_text = Path.write_text

        def fail_report(path, *args, **kwargs):
            if path == self.output / "report.json":
                raise OSError("fictional report disk failure")
            return real_write_text(path, *args, **kwargs)

        with patch.object(Path, "write_text", new=fail_report):
            with self.assertRaisesRegex(OSError, "fictional report disk failure"):
                self.run_smoke()
        self.assertTrue((self.output / "INCOMPLETE").exists())
        self.assertFalse((self.output / "report.json").exists())
        self.assertEqual(len(self.detector.calls), len(self.rows) + 1)

    def test_missing_checkpoint_and_malformed_manifest_fail_before_backend(self):
        self.manifest.write_text("not JSON", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.run_smoke()
        self.assertFalse(self.output.exists())
        self.write_manifest()
        (self.models / "model.safetensors").unlink()
        with self.assertRaises((ValueError, OSError)):
            self.run_smoke()
        self.assertEqual(self.loads, [])
        self.assertFalse(self.output.exists())

    def test_incomplete_input_or_model_marker_refuses_preflight(self):
        for index, root in enumerate((self.manifest.parent, self.models, self.sources)):
            with self.subTest(root=root):
                marker = root / "INCOMPLETE"
                marker.write_text("fictional incomplete publication", encoding="utf-8")
                try:
                    with self.assertRaises(ValueError):
                        self.run_smoke(self.root / f"incomplete-{index}")
                    self.assertFalse((self.root / f"incomplete-{index}").exists())
                    self.assertEqual(self.loads, [])
                finally:
                    marker.unlink()

    def test_manifest_count_and_byte_bounds_reject_before_backend(self):
        for index, contents in enumerate((json.dumps({"files": [self.rows[0]] * 101}), " " * 2_000_001)):
            with self.subTest(index=index):
                self.manifest.write_text(contents, encoding="utf-8")
                output = self.root / f"oversized-{index}"
                with self.assertRaises(ValueError):
                    self.run_smoke(output)
                self.assertFalse(output.exists())
                self.assertEqual(self.loads, [])

    def test_existing_output_is_refused_without_changing_sentinel(self):
        self.output.mkdir()
        sentinel = self.output / "keep.txt"
        sentinel.write_text("preserve", encoding="utf-8")
        for target in (self.output, self.sources / "nested", self.models / "nested", self.root):
            with self.subTest(target=target):
                with self.assertRaises((ValueError, OSError)):
                    self.run_smoke(target)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve")
        self.assertEqual(self.loads, [])

    def test_backend_failure_cannot_publish_a_complete_report(self):
        def failed_loader(_):
            raise ValueError("fictional checkpoint failure")

        with self.assertRaises(ValueError):
            smoke.run_smoke(self.models, self.manifest, self.sources, self.output,
                            threads=1, load_detector=failed_loader)
        if self.output.exists():
            self.assertTrue((self.output / "INCOMPLETE").exists())
        self.assertFalse((self.output / "report.json").exists())


if __name__ == "__main__":
    unittest.main()
