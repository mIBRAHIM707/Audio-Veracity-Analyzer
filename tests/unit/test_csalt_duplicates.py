"""Claude-authored tests (9 November 2026) for CSALT duplicate-original annotation, todo 9d.

Authored by Claude (Claude Code), not by GPT-6 Astra. Fictional rows only.
"""
import unittest

from data.collect import csalt_pilot


def inventory_row(path, digest, status="verified"):
    row = {field: "" for field in csalt_pilot.FIELDS}
    row.update(path=path, speaker_id=path.split("/")[1], sha256=digest, validation_status=status)
    return row


class DuplicateOriginalTests(unittest.TestCase):
    def test_cross_speaker_and_same_speaker_duplicates_are_annotated(self):
        rows = [inventory_row("Bonafide/Speaker_01/a.wav", "1" * 64),
                inventory_row("Bonafide/Speaker_02/b.wav", "1" * 64),
                inventory_row("Bonafide/Speaker_03/c.wav", "2" * 64),
                inventory_row("Bonafide/Speaker_03/d.wav", "2" * 64),
                inventory_row("Bonafide/Speaker_04/e.wav", "3" * 64)]
        csalt_pilot.annotate_duplicates(rows)
        self.assertEqual([(r["duplicate_original_rows"], r["duplicate_original_speakers"]) for r in rows],
                         [("2", "2"), ("2", "2"), ("2", ""), ("2", ""), ("", "")])

    def test_failed_rows_are_neither_counted_nor_annotated(self):
        rows = [inventory_row("Bonafide/Speaker_01/a.wav", "1" * 64),
                inventory_row("Bonafide/Speaker_02/b.wav", "1" * 64, status="failed")]
        csalt_pilot.annotate_duplicates(rows)
        self.assertEqual([(r["duplicate_original_rows"], r["duplicate_original_speakers"]) for r in rows],
                         [("", ""), ("", "")])

    def test_columns_are_part_of_the_inventory_schema(self):
        for field in ("duplicate_original_rows", "duplicate_original_speakers"):
            self.assertIn(field, csalt_pilot.FIELDS)
        self.assertEqual(csalt_pilot.FIELDS[-1], "error")


if __name__ == "__main__":
    unittest.main()
