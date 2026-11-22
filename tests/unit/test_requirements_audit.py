"""Requirements and evidence audit checks (backlog 11.2).

Claude-authored, 22 November 2026 (not Astra). The first case checks the real audit
against the real PRD and repository; the others feed the checker small synthetic
PRD and audit texts so each rule is shown to catch its own failure.
"""
from __future__ import annotations

from contextlib import redirect_stdout
import io
import pathlib
import tempfile
import unittest

from scripts import check_requirements_audit as cra

PRD = """\
| ID | Requirement | Priority |
|---|---|---|
| FR-X1 | First thing. | P0 |
| FR-X2 | Second thing, with a | pipe inside. | P1 |

| Requirements | Phase |
|---|---|
| FR-X1 to FR-X2 | 3 |
| FR-X1 | 4 (later part) |
"""

HEADER = """\
## Summary

| Status | Rows |
|---|---|
| implemented | {implemented} |
| partial | {partial} |
| data-dependent | 0 |
| not-started | 0 |
| human | 0 |
| deferred | 0 |

## Rows

| ID | P | Status | Code and documents | Tests | Gap |
|---|---|---|---|---|---|
"""

GOOD_X1 = '| FR-X1 | P0 | implemented | `src/a.py` | `tests/test_a.py` | none |'
GOOD_X2 = '| FR-X2 | P1 | partial | `src/b.py`, `src/c.py` | `tests/test_b.py` | The rest; see `docs/b.md`. |'
EXISTING = {'src/a.py', 'src/b.py', 'src/c.py', 'tests/test_a.py', 'tests/test_b.py', 'docs/b.md'}
DIRECTORIES = {'src', 'tests', 'docs'}


def audit(*rows: str, implemented: int = 1, partial: int = 1) -> str:
    return HEADER.format(implemented=implemented, partial=partial) + '\n'.join(rows) + '\n'


def problems(text: str, existing=EXISTING) -> list[str]:
    def kind(path):
        if path in existing:
            return 'file'
        return 'dir' if path.rstrip('/') in DIRECTORIES else None
    return cra.audit_problems(cra.parse_prd(PRD), text, kind)


class RealAuditTest(unittest.TestCase):
    def test_real_audit_matches_prd_and_repository(self):
        prd = cra.parse_prd(cra.PRD_PATH.read_text(encoding='utf-8'))
        # Guard against a parser that silently finds nothing: the PRD has dozens of IDs
        # across every group.
        self.assertGreater(len(prd), 80)
        for group in ('FR-D', 'FR-E', 'FR-M', 'FR-I', 'FR-T', 'FR-A', 'FR-U', 'FR-S', 'FR-O', 'FR-W'):
            self.assertTrue(any(req.startswith(group) for req in prd), group)
        # No requirement table with a different header is missed: every row anywhere in
        # the PRD whose first cell is a single ID names a parsed requirement.
        prd_text = cra.PRD_PATH.read_text(encoding='utf-8')
        first_cells = {match.group(1) for line in prd_text.splitlines()
                       if (match := cra.PRD_ID_ROW_RE.match(line))}
        self.assertLessEqual(first_cells, set(prd))
        text = cra.AUDIT_PATH.read_text(encoding='utf-8')
        self.assertEqual(cra.audit_problems(prd, text, cra.repo_kind(cra.ROOT)), [])
        # The stated total follows the PRD.
        self.assertIn(f'{len(prd)} PRD requirements in total', text)

    def test_command_line_passes_on_the_repository(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = cra.main([])
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn('PASS', out.getvalue())


class ParserTest(unittest.TestCase):
    def test_prd_rows_and_priorities_ignore_the_phase_table(self):
        self.assertEqual(cra.parse_prd(PRD), {'FR-X1': 'P0', 'FR-X2': 'P1'})

    def test_consistent_audit_has_no_problems(self):
        self.assertEqual(problems(audit(GOOD_X1, GOOD_X2)), [])

    @staticmethod
    def with_row(row):
        """The test PRD with `row` added inside its requirement table."""
        anchor = 'pipe inside. | P1 |\n'
        return PRD.replace(anchor, anchor + row + '\n')

    def test_prd_row_without_a_plain_priority_is_an_error_not_a_skip(self):
        for bad in ('| FR-X3 | Third. | **P0** |', '| FR-X3 | Third. | P0 (was P1) |'):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(cra.PrdParseError, 'FR-X3 row has no plain'):
                    cra.parse_prd(self.with_row(bad))

    def test_prd_id_defined_twice_is_an_error(self):
        with self.assertRaisesRegex(cra.PrdParseError, 'FR-X1 is defined twice'):
            cra.parse_prd(self.with_row('| FR-X1 | Again. | P2 |'))

    def test_rows_outside_requirement_tables_are_not_requirements(self):
        self.assertNotIn('FR-X9', cra.parse_prd(PRD + '\n| ID | Other |\n|---|---|\n| FR-X9 | P0 |\n'))

    def test_command_line_reports_an_unreadable_prd(self):
        with tempfile.TemporaryDirectory() as folder:
            prd = pathlib.Path(folder) / 'prd.md'
            prd.write_text('| ID | Requirement | Priority |\n|---|---|---|\n| FR-X1 | Thing. | High |\n',
                           encoding='utf-8')
            out = io.StringIO()
            with redirect_stdout(out):
                code = cra.main(['--prd', str(prd)])
        self.assertEqual(code, 1)
        self.assertIn('FR-X1 row has no plain', out.getvalue())

    def test_summary_rows_outside_the_summary_section_are_ignored(self):
        stray = '\n## Notes\n\n| implemented | 9 |\n'
        self.assertEqual(problems(audit(GOOD_X1, GOOD_X2) + stray), [])

    def test_status_listed_twice_in_the_summary_is_reported(self):
        text = audit(GOOD_X1, GOOD_X2).replace('| partial | 1 |', '| partial | 1 |\n| partial | 1 |')
        self.assertTrue(any('summary lists partial twice' in item for item in problems(text)))


class RuleTest(unittest.TestCase):
    def assertProblem(self, found, fragment):
        self.assertTrue(any(fragment in item for item in found), found)

    def test_missing_requirement_is_reported(self):
        found = problems(audit(GOOD_X1, implemented=1, partial=0))
        self.assertProblem(found, 'FR-X2: in the PRD but has no audit row')

    def test_unknown_and_duplicate_ids_are_reported(self):
        extra = '| FR-X9 | P0 | implemented | `src/a.py` | `tests/test_a.py` | none |'
        found = problems(audit(GOOD_X1, GOOD_X1, GOOD_X2, extra, implemented=3))
        self.assertProblem(found, 'FR-X9: audit row for an ID that is not in the PRD')
        self.assertProblem(found, 'FR-X1: 2 audit rows, expected 1')

    def test_priority_must_match_the_prd(self):
        found = problems(audit(GOOD_X1, GOOD_X2.replace('| P1 |', '| P0 |')))
        self.assertProblem(found, 'priority P0, PRD says P1')

    def test_unknown_status_is_reported(self):
        found = problems(audit(GOOD_X1.replace('implemented', 'done'), GOOD_X2, implemented=0))
        self.assertProblem(found, "unknown status 'done'")

    def test_missing_cited_path_is_reported_in_every_column(self):
        for missing in ('src/a.py', 'tests/test_b.py', 'docs/b.md'):
            with self.subTest(missing=missing):
                found = problems(audit(GOOD_X1, GOOD_X2), EXISTING - {missing})
                self.assertProblem(found, f'cited path does not exist: {missing}')

    def test_routes_drives_and_identifiers_in_gaps_are_not_paths(self):
        row = GOOD_X2.replace('see `docs/b.md`', '`/openapi.json`, `D:/AVA-Data`, `score.updated`')
        self.assertEqual(problems(audit(GOOD_X1, row)), [])

    def test_repo_kind_separates_files_directories_and_escapes(self):
        kind = cra.repo_kind(cra.ROOT)
        self.assertEqual(kind('scripts/check_requirements_audit.py'), 'file')
        self.assertEqual(kind('tests/unit'), 'dir')
        self.assertIsNone(kind('../outside.txt'))
        self.assertIsNone(kind('scripts/no_such_file.py'))

    def test_directory_in_a_code_or_test_cell_is_reported(self):
        found = problems(audit(GOOD_X1.replace('`tests/test_a.py`', '`tests/`'), GOOD_X2))
        self.assertProblem(found, 'code and test cells must name files: tests/')

    def test_directory_in_a_gap_note_is_accepted(self):
        row = GOOD_X2.replace('see `docs/b.md`', 'see `docs/`')
        self.assertEqual(problems(audit(GOOD_X1, row)), [])

    def test_malformed_path_cell_is_reported(self):
        found = problems(audit(GOOD_X1.replace('`src/a.py`', 'src/a.py'), GOOD_X2))
        self.assertProblem(found, 'code cell must be "none" or backticked paths')

    def test_non_implemented_row_needs_a_gap(self):
        for empty in ('none', 'None (later)', 'n/a', 'NA', 'tbd', 'TODO', '-', '--', '—', '?'):
            with self.subTest(gap=empty):
                found = problems(audit(GOOD_X1, GOOD_X2.replace('The rest; see `docs/b.md`.', empty)))
                self.assertProblem(found, 'status partial needs a named gap')

    def test_gap_that_merely_starts_with_a_none_like_word_is_accepted(self):
        row = GOOD_X2.replace('The rest; see `docs/b.md`.', 'Nonexistent tests for the rest.')
        self.assertEqual(problems(audit(GOOD_X1, row)), [])

    def test_implemented_or_partial_rows_need_tests_and_code(self):
        no_test = GOOD_X1.replace('`tests/test_a.py`', '`src/c.py`')
        found = problems(audit(no_test, GOOD_X2))
        self.assertProblem(found, 'status implemented needs at least one test path')
        no_code = GOOD_X2.replace('`src/b.py`, `src/c.py`', 'none')
        found = problems(audit(GOOD_X1, no_code))
        self.assertProblem(found, 'status partial needs at least one code path')

    def test_implemented_row_with_a_gap_is_reported(self):
        for gap in ('Missing a part.', 'Nonexistent tests for X.', 'none, but X is missing'):
            with self.subTest(gap=gap):
                found = problems(audit(GOOD_X1.replace('| none |', f'| {gap} |'), GOOD_X2))
                self.assertProblem(found, 'implemented rows keep notes as "none (...)"')

    def test_implemented_row_may_carry_a_bracketed_note(self):
        row = GOOD_X1.replace('| none |', '| none (a recorded interpretation) |')
        self.assertEqual(problems(audit(row, GOOD_X2)), [])

    def test_summary_counts_must_match_rows(self):
        found = problems(audit(GOOD_X1, GOOD_X2, implemented=2, partial=1))
        self.assertProblem(found, 'summary: implemented says 2, rows say 1')

    def test_row_with_wrong_cell_count_is_reported(self):
        found = problems(audit(GOOD_X1 + ' extra |', GOOD_X2, implemented=0))
        self.assertProblem(found, 'has 7 cells, expected 6')

    def test_data_dependent_row_may_cite_nothing(self):
        row = '| FR-X2 | P1 | data-dependent | none | none | Needs the dataset. |'
        text = audit(GOOD_X1, row, partial=0).replace('| data-dependent | 0 |', '| data-dependent | 1 |')
        self.assertEqual(problems(text), [])


if __name__ == '__main__':
    unittest.main()
