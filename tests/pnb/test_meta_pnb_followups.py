"""Claude-authored tests (9 November 2026) for Punjabi importer review follow-ups, backlog 1.2.

Authored by Claude (Claude Code), not by GPT-6 Astra; the Astra catalogue in
test_meta_pnb_pilot.py is unchanged. Covers 7b review items L3 (signal inspection
without a vacuous publisher-hash claim; review_flags as a list), L4 (rows longer
than the 10 MB WAV bound are excluded before ranking), L7 (CLI exit codes, 48 kHz
and stereo fixtures) and L8 (archive root outside the repository). Fictional
Parquet/FLAC only; no real archive, speech or network.
"""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import soundfile as sf

from data.collect import csalt_pilot, meta_pnb_pilot as pnb
from scripts import verify_pnb_pilot as verifier
from tests.fixture_cleanup import cleanup_with_retry

REVISION = pnb.REVISION
SHARD = "data/pnb_Arab/train-00000-of-00002.parquet"


def flac(rate=20000, channels=1, seconds=4, period=40):
    mono = np.where(np.arange(rate * seconds) % period < period // 2, 2000, -2000).astype(np.int16)
    values = mono if channels == 1 else np.stack([mono, (mono // 2).astype(np.int16)], axis=1)
    buffer = io.BytesIO()
    sf.write(buffer, values, rate, format="FLAC", subtype="PCM_16")
    return buffer.getvalue(), values


class PnbFollowupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(cleanup_with_retry, temporary)
        self.root = Path(temporary.name).resolve()
        self.archives = self.root / "archives"
        self.archives.mkdir()
        self.raw, self.output, self.manifest = self.root / "raw", self.root / "output", self.root / "source.json"

    def rows(self, per_speaker=2, rate=20000, channels=1, durations=None):
        rows = []
        for speaker in (1, 2):
            for index in range(per_speaker):
                payload, _ = flac(rate, channels, period=40 + 2 * len(rows))
                rows.append({"language": "pnb_Arab", "speaker_id": f"fictional-speaker-{speaker}",
                             "prompt_id": "reused-prompt", "prompt": "Fictional prompt",
                             "segment_id": "s01", "raw_text": "Fictional unaligned transcript",
                             "iso_639_3": "pnb", "glottocode": "west2386", "iso_15924": "Arab",
                             "duration": (durations or {}).get(len(rows), 4.),
                             "audio": {"bytes": payload, "path": f"fictional-{speaker}-{index}.flac"}})
        return rows

    def write_archive(self, rows, archives=None):
        archives = archives or self.archives
        archive = archives / Path(SHARD).name
        pq.write_table(pa.Table.from_pylist(rows), archive, row_group_size=2)
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        self.manifest.write_text(json.dumps({
            "repo": pnb.REPO, "revision": REVISION, "language": "pnb_Arab", "licence": "CC-BY-4.0",
            "files": [{"path": SHARD, "local_name": archive.name, "size": archive.stat().st_size,
                       "sha256": digest, "publisher_lfs_sha256": digest}]}), encoding="utf-8")
        return digest

    def load(self, name):
        return json.loads((self.output / name).read_text(encoding="utf-8"))

    # L3 ------------------------------------------------------------------------
    def test_review_flags_are_lists_and_inspection_makes_no_publisher_claim(self):
        self.write_archive(self.rows())
        pnb.run_pilot(self.manifest, self.archives, self.raw, self.output)
        for name in ("inventory.json", "pilot.json"):
            for row in self.load(name)["files"]:
                self.assertIsInstance(row["review_flags"], list)
                self.assertIn("requires_format_processing", row["review_flags"])
        wav_bytes = next((self.raw / "wav").iterdir()).read_bytes()
        info = csalt_pilot.inspect_pcm(wav_bytes)
        self.assertEqual(info["sha256"], hashlib.sha256(wav_bytes).hexdigest())
        self.assertIsInstance(info["review_flags"], list)

    def test_csalt_publisher_inspection_keeps_string_flags_and_still_rejects_mismatch(self):
        self.write_archive(self.rows())
        pnb.run_pilot(self.manifest, self.archives, self.raw, self.output)
        wav_bytes = next((self.raw / "wav").iterdir()).read_bytes()
        entry = {"size": len(wav_bytes), "lfs": {"oid": hashlib.sha256(wav_bytes).hexdigest()}}
        info = csalt_pilot.inspect_audio(wav_bytes, entry)
        self.assertEqual(json.loads(info["review_flags"]), csalt_pilot.inspect_pcm(wav_bytes)["review_flags"])
        for wrong in ({**entry, "size": entry["size"] + 1}, {**entry, "lfs": {"oid": "0" * 64}}):
            with self.subTest(wrong=wrong), self.assertRaisesRegex(ValueError, "Publisher"):
                csalt_pilot.inspect_audio(wav_bytes, wrong)

    # L4 ------------------------------------------------------------------------
    def test_row_longer_than_wav_bound_is_excluded_before_ranking_and_verifier_agrees(self):
        self.assertAlmostEqual(pnb.MAX_SELECTABLE_SECONDS, verifier.MAX_SELECTABLE_SECONDS)
        # The long row is the one the hash ranking would select first for speaker 1.
        top = min(range(3), key=lambda i: hashlib.sha256(f"{REVISION}/{SHARD}#{i}".encode()).hexdigest())
        rows = self.rows(per_speaker=3, durations={top: 200.})
        digest = self.write_archive(rows)
        summary = pnb.run_pilot(self.manifest, self.archives, self.raw, self.output)
        self.assertTrue(summary["pilot_complete"], summary)
        self.assertEqual(summary["excluded_over_duration_bound"], 1)
        self.assertEqual(summary["metadata_rows"], 6)
        selected = {row["source_row_index"] for row in self.load("pilot.json")["files"]}
        self.assertNotIn(top, selected)
        self.assertEqual(selected & {0, 1, 2}, {0, 1, 2} - {top})
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = verifier.main(["--source-manifest", str(self.manifest), "--archive-root", str(self.archives),
                                  "--raw-root", str(self.raw), "--manifest-root", str(self.output),
                                  "--expected-archive-sha256", digest, "--expected-speakers", "2"])
        self.assertEqual(code, 0, output.getvalue())

    def test_bound_is_the_publisher_rate_limit_not_the_old_625_seconds(self):
        self.assertLess(pnb.MAX_SELECTABLE_SECONDS, 105)
        self.assertGreater(pnb.MAX_SELECTABLE_SECONDS, 104)
        # Even with the 0.1 s declared/decoded tolerance the 48 kHz mono WAV fits.
        self.assertLessEqual((pnb.MAX_SELECTABLE_SECONDS + .1) * 48000 * 2 + 44, pnb.MAX_AUDIO_BYTES)

    def test_long_rows_never_silently_drop_a_speaker_or_its_allocation(self):
        cases = (("all_rows_long", {0: 200., 1: 300.}, "no rows within"),
                 ("too_few_left", {0: 200.}, "fewer than requested"))
        for name, durations, message in cases:
            with self.subTest(name=name):
                self.write_archive(self.rows(per_speaker=2, durations=durations))
                with self.assertRaisesRegex(ValueError, message):
                    pnb.run_pilot(self.manifest, self.archives, self.raw, self.output, per_speaker=2)
                self.assertFalse(self.raw.exists())
                self.assertFalse(self.output.exists())

    # L7 ------------------------------------------------------------------------
    def test_48khz_and_stereo_sources_keep_native_samples(self):
        for rate, channels in ((48000, 1), (20000, 2)):
            with self.subTest(rate=rate, channels=channels):
                base = self.root / f"case-{rate}-{channels}"
                self.archives, self.raw, self.output = base / "archives", base / "raw", base / "output"
                self.archives.mkdir(parents=True)
                rows = self.rows(rate=rate, channels=channels)
                self.write_archive(rows)
                summary = pnb.run_pilot(self.manifest, self.archives, self.raw, self.output)
                self.assertTrue(summary["pilot_complete"], summary)
                for row in self.load("pilot.json")["files"]:
                    self.assertEqual((row["sample_rate"], row["channels"], row["pcm_bits"]), (rate, channels, 16))
                    with wave.open(str(self.raw / row["path"]), "rb") as audio:
                        stored = np.frombuffer(audio.readframes(audio.getnframes()), dtype="<i2")
                    original = sf.read(io.BytesIO(rows[row["source_row_index"]]["audio"]["bytes"]), dtype="int16")[0]
                    np.testing.assert_array_equal(stored, original.reshape(-1))

    def run_main(self, *extra):
        argv = ["meta_pnb_pilot.py", "--source-manifest", str(self.manifest), "--archive-root", str(self.archives),
                "--raw-root", str(self.raw), "--output-root", str(self.output), *extra]
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", argv), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = pnb.main()
        return code, stdout.getvalue(), stderr.getvalue()

    def test_cli_exit_codes_complete_incomplete_and_preflight_failure(self):
        self.write_archive(self.rows())
        code, stdout, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(stdout)["pilot_complete"])
        code, _, stderr = self.run_main()
        self.assertEqual(code, 1, "existing destinations must fail")
        self.assertIn("Punjabi intake failed", stderr)
        self.assertNotIn("Traceback", stderr)
        rows = self.rows()
        rows[0]["audio"]["bytes"] = b"not a FLAC payload"
        self.raw, self.output = self.root / "raw-corrupt", self.root / "output-corrupt"
        self.write_archive(rows)
        code, stdout, _ = self.run_main()
        self.assertEqual(code, 1, "an incomplete pilot must not exit 0")
        self.assertFalse(json.loads(stdout)["pilot_complete"])
        self.assertTrue((self.output / "INCOMPLETE").exists())

    # L8 ------------------------------------------------------------------------
    def test_archive_root_inside_repository_is_refused_before_outputs(self):
        fake_repo = self.root / "repo"
        archives = fake_repo / "archives"
        archives.mkdir(parents=True)
        self.write_archive(self.rows(), archives)
        with patch.object(pnb, "__file__", str(fake_repo / "data" / "collect" / "meta_pnb_pilot.py")):
            with self.assertRaisesRegex(ValueError, "outside repository"):
                pnb.run_pilot(self.manifest, archives, self.raw, self.output)
        self.assertFalse(self.raw.exists())
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
