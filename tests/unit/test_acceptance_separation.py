"""Acceptance separation checks (backlog 11.QA).

Claude-authored, 22 November 2026 (not Astra). The first cases check the real
separation against the real PRD, backlog and repository; the others feed the checker
small synthetic texts so each rule is shown to catch its own failure.
"""
from __future__ import annotations

from contextlib import redirect_stdout
import io
import pathlib
import tempfile
import unittest

from scripts import check_acceptance_separation as cas

PRD = """\
## 6. Non-functional requirements

### 6.1 Performance

Definitions paragraph.

| Metric | Target |
|---|---|
| Latency per window, p95 | 1 s |
| Memory per worker | 2 GB |

### 6.2 Detection quality

| Metric | Target |
|---|---|
| EER on the **full** test set | 5% |

### 6.3 Security and privacy

- No network egress from any service.
- TLS between dashboard and API.

### 6.4 Regulatory motivation

| Instrument | Response |
|---|---|
| A law | A response |

### 6.5 Reliability

- Restarts keep the call state intact.

### 6.6 Usability

- Renders at two sizes on the board.

---

## 7. Acceptance criteria by objective

| Objective | Acceptance evidence |
|---|---|
| BO-1 | A report. |
| RQ-A | A curve. |

## 8. Later
"""

BACKLOG = """\
- [x] 1.1 Done thing.
- [ ] 2.1 Open thing.
- [ ] 2.QA Open review.
- [x] 2.2 Another done thing.
"""

EXISTING = {'src/a.py', 'tests/test_a.py', 'docs/b.md'}
DIRECTORIES = {'src', 'tests', 'docs'}

OBJECTIVES = [
    '| BO-1 | met | `src/a.py`, `tests/test_a.py` | none |',
    '| RQ-A | data-dependent | none | Real scores; see `docs/b.md`. |',
]
NFR = [
    '| 6.1.1 | Latency per window | unmeasured | `src/a.py` | Box A1. |',
    '| 6.1.2 | Memory per worker | unmeasured | none | Box A1. |',
    '| 6.2.1 | EER on the full test set | data-dependent | none | Real scores. |',
    '| 6.3.1 | No network egress from any service | met | `tests/test_a.py` | none (lab) |',
    '| 6.3.2 | TLS between dashboard and API | software-gap | none | Not built. |',
    '| 6.5.1 | Restarts keep the call state | decision | none | Topology. |',
    '| 6.6.1 | Renders at two sizes... | human | none | A person. |',
]
TASKS = [
    '| 2.1 | software | A thing. |',
    '| 2.QA | data, human | Real data and a person. |',
]
COUNTS = {'met': 2, 'software-gap': 1, 'decision': 1, 'unmeasured': 2, 'data-dependent': 2, 'human': 1}


def separation(objectives=None, nfr=None, tasks=None, counts=None) -> str:
    counts = dict(COUNTS, **(counts or {}))
    summary = '\n'.join(f'| {status} | {counts[status]} |' for status in cas.STATUSES)
    return ('## Summary\n\n| Status | Rows |\n|---|---|\n' + summary + '\n\n'
            '## Objectives\n\n| ID | Status | Evidence | Remaining |\n|---|---|---|---|\n'
            + '\n'.join(OBJECTIVES if objectives is None else objectives) + '\n\n'
            '## Non-functional requirements\n\n| Ref | PRD text | Status | Evidence | Remaining |\n'
            '|---|---|---|---|---|\n' + '\n'.join(NFR if nfr is None else nfr) + '\n\n'
            '## Open backlog tasks\n\n| Task | Held by | Remaining |\n|---|---|---|\n'
            + '\n'.join(TASKS if tasks is None else tasks) + '\n')


def problems(text: str, prd: str = PRD, backlog: str = BACKLOG, existing=EXISTING) -> list[str]:
    def kind(path):
        if path in existing:
            return 'file'
        return 'dir' if path.rstrip('/') in DIRECTORIES else None
    return cas.separation_problems(prd, backlog, text, kind)


class RealSeparationTest(unittest.TestCase):
    def test_real_separation_matches_prd_backlog_and_repository(self):
        prd_text = cas.PRD_PATH.read_text(encoding='utf-8')
        objectives, items = cas.parse_prd(prd_text)
        # Guard against a parser that silently finds little: the PRD has eight
        # objectives, three research questions and dozens of §6 items.
        self.assertEqual(objectives, [f'BO-{n}' for n in range(1, 9)] + ['RQ-A', 'RQ-B', 'RQ-C'])
        # Pinned per-section counts (PRD v0.2): a parser change that silently drops or
        # invents an item fails here; a PRD edit that adds one updates this line too.
        per_section = {section: sum(ref.startswith(section + '.') for ref, _ in items)
                       for section in cas.NFR_SECTIONS}
        self.assertEqual(per_section, {'6.1': 8, '6.2': 8, '6.3': 7, '6.5': 4, '6.6': 2})
        open_tasks, closed, unreadable = cas.parse_backlog(cas.BACKLOG_PATH.read_text(encoding='utf-8'))
        self.assertTrue(open_tasks and closed)
        self.assertEqual(unreadable, [])
        found = cas.separation_problems(prd_text, cas.BACKLOG_PATH.read_text(encoding='utf-8'),
                                        cas.SEPARATION_PATH.read_text(encoding='utf-8'),
                                        cas.repo_kind(cas.ROOT))
        self.assertEqual(found, [])

    def test_command_line_passes_on_the_repository(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = cas.main([])
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn('PASS', out.getvalue())


class ParserTest(unittest.TestCase):
    def test_prd_objectives_and_items_in_order_without_section_6_4(self):
        objectives, items = cas.parse_prd(PRD)
        self.assertEqual(objectives, ['BO-1', 'RQ-A'])
        self.assertEqual(items, [
            ('6.1.1', 'Latency per window, p95'), ('6.1.2', 'Memory per worker'),
            ('6.2.1', 'EER on the full test set'),
            ('6.3.1', 'No network egress from any service.'), ('6.3.2', 'TLS between dashboard and API.'),
            ('6.5.1', 'Restarts keep the call state intact.'), ('6.6.1', 'Renders at two sizes on the board.')])

    def test_missing_prd_section_is_an_error_not_an_empty_list(self):
        for drop, message in (('### 6.5 Reliability', '6.5'), ('## 7. Acceptance', '7')):
            with self.subTest(drop=drop):
                with self.assertRaisesRegex(cas.PrdParseError, message):
                    cas.parse_prd(PRD.replace(drop, '### Other'))

    def test_section_without_items_is_an_error(self):
        with self.assertRaisesRegex(cas.PrdParseError, '6.6 lists no items'):
            cas.parse_prd(PRD.replace('- Renders at two sizes on the board.', 'Prose only.'))

    def test_backlog_open_and_closed_boxes(self):
        self.assertEqual(cas.parse_backlog(BACKLOG), ({'2.1', '2.QA'}, {'1.1', '2.2'}, []))

    def test_unreadable_backlog_boxes_are_reported_not_dropped(self):
        for line in ('  - [ ] 3.1 Indented.', '- [X] 3.1 Capital.', '* [ ] 3.1 Star.', '- [ ] Three.1 Odd ID.',
                     '- [~] 3.1 Other mark.'):
            with self.subTest(line=line):
                open_, closed, unreadable = cas.parse_backlog(BACKLOG + line + '\n')
                self.assertEqual((open_, closed), ({'2.1', '2.QA'}, {'1.1', '2.2'}))
                self.assertEqual(len(unreadable), 1, unreadable)
                self.assertIn('backlog line 5: unreadable check box', unreadable[0])
        self.assertProblem(problems(separation(), backlog=BACKLOG + '- [X] 3.1 Capital.\n'),
                           'unreadable check box')

    def assertProblem(self, found, fragment):
        self.assertTrue(any(fragment in item for item in found), found)

    def test_star_bullets_and_deeper_headings_stay_in_their_section(self):
        prd = PRD.replace('- TLS between dashboard and API.',
                          '#### Detail\n\n* TLS between dashboard and API.')
        _objectives, items = cas.parse_prd(prd)
        self.assertIn(('6.3.2', 'TLS between dashboard and API.'), items)

    def test_only_the_first_table_of_a_section_counts(self):
        prd = PRD.replace('| Memory per worker | 2 GB |\n',
                          '| Memory per worker | 2 GB |\n\nNotes.\n\n| Other | Head |\n|---|---|\n| Extra | row |\n')
        _objectives, items = cas.parse_prd(prd)
        self.assertEqual([ref for ref, _ in items if ref.startswith('6.1.')], ['6.1.1', '6.1.2'])

    def test_consistent_separation_has_no_problems(self):
        self.assertEqual(problems(separation()), [])


class RuleTest(unittest.TestCase):
    def assertProblem(self, found, fragment):
        self.assertTrue(any(fragment in item for item in found), found)

    def test_missing_extra_and_duplicate_objective_rows(self):
        found = problems(separation(objectives=[OBJECTIVES[0], OBJECTIVES[0],
                                                '| BO-9 | human | none | A person. |'],
                                    counts={'met': 3, 'data-dependent': 1, 'human': 2}))
        self.assertProblem(found, 'objectives: RQ-A is a PRD §7 objective but has no row')
        self.assertProblem(found, 'objectives: row for BO-9, which is not a PRD §7 objective')
        self.assertProblem(found, 'objectives: 2 rows for BO-1, expected 1')

    def test_missing_and_extra_nfr_rows(self):
        nfr = NFR[1:] + ['| 6.4.1 | A law | human | none | Review. |']
        found = problems(separation(nfr=nfr, counts={'unmeasured': 1, 'human': 2}))
        self.assertProblem(found, 'non-functional: 6.1.1 is a PRD §6 item but has no row')
        self.assertProblem(found, 'non-functional: row for 6.4.1, which is not a PRD §6 item')

    def test_quoted_text_must_start_the_prd_item(self):
        for quoted in ('Latency per call', 'Latency', 'per window'):
            with self.subTest(quoted=quoted):
                nfr = [NFR[0].replace('Latency per window', quoted)] + NFR[1:]
                self.assertProblem(problems(separation(nfr=nfr)), "6.1.1: PRD text")

    def test_quoted_text_ignores_bold_markup_and_whitespace(self):
        nfr = list(NFR)
        nfr[2] = '| 6.2.1 | EER on  the full test | data-dependent | none | Real scores. |'
        self.assertEqual(problems(separation(nfr=nfr)), [])

    def test_a_reordered_prd_item_breaks_the_quote(self):
        # Inserting a PRD bullet ahead of an existing one renumbers it: the row for the
        # old number now quotes the wrong item, so the document must be revisited.
        prd = PRD.replace('- No network egress', '- Something new first.\n- No network egress')
        found = problems(separation(), prd=prd)
        self.assertProblem(found, '6.3.1: PRD text')
        self.assertProblem(found, 'non-functional: 6.3.3 is a PRD §6 item but has no row')

    def test_backlog_rows_follow_the_open_boxes(self):
        closed_now = BACKLOG.replace('- [ ] 2.QA', '- [x] 2.QA')
        self.assertProblem(problems(separation(), backlog=closed_now),
                           'backlog: row for 2.QA, which is not an open backlog box')
        opened = BACKLOG + '- [ ] 3.1 New thing.\n'
        self.assertProblem(problems(separation(), backlog=opened),
                           'backlog: 3.1 is an open backlog box but has no row')

    def test_unknown_or_repeated_hold(self):
        for held, fragment in (('later', "unknown hold 'later'"), ('', "unknown hold ''"),
                               ('data, data', 'hold listed twice')):
            with self.subTest(held=held):
                tasks = [TASKS[0], f'| 2.QA | {held} | Something. |']
                self.assertProblem(problems(separation(tasks=tasks)), fragment)

    def test_open_task_needs_a_remainder(self):
        tasks = [TASKS[0], '| 2.QA | data | none |']
        self.assertProblem(problems(separation(tasks=tasks)), '2.QA: an open task needs a named remainder')

    def test_unknown_status(self):
        nfr = [NFR[0].replace('unmeasured', 'done')] + NFR[1:]
        found = problems(separation(nfr=nfr, counts={'unmeasured': 1}))
        self.assertProblem(found, "unknown status 'done'")

    def test_met_row_needs_a_test_and_nothing_left(self):
        no_test = ['| BO-1 | met | `src/a.py` | none |', OBJECTIVES[1]]
        self.assertProblem(problems(separation(objectives=no_test)), 'status met needs at least one test path')
        left = ['| BO-1 | met | `tests/test_a.py` | Something is missing. |', OBJECTIVES[1]]
        self.assertProblem(problems(separation(objectives=left)), 'met rows keep notes as "none (...)"')

    def test_met_note_may_not_admit_an_unmet_part(self):
        for note in ('none (images are not scanned)', 'none (untested on Linux)', 'none (a part is missing)'):
            with self.subTest(note=note):
                objectives = [f'| BO-1 | met | `tests/test_a.py` | {note} |', OBJECTIVES[1]]
                self.assertProblem(problems(separation(objectives=objectives)), "a met row's note says")
        fine = ['| BO-1 | met | `tests/test_a.py` | none (cannot reach the internet; notes kept) |', OBJECTIVES[1]]
        self.assertEqual(problems(separation(objectives=fine)), [])

    def test_placeholder_anywhere_in_a_cell_is_reported(self):
        for text in ('Real scores, COVERAGE-PLACEHOLDER.', 'Box A1; TODO later.', 'Box A1 (tbd).'):
            with self.subTest(text=text):
                nfr = [NFR[0].replace('Box A1.', text)] + NFR[1:]
                self.assertProblem(problems(separation(nfr=nfr)), 'placeholder left in a cell')
        tasks = [TASKS[0], '| 2.QA | data | See XXX. |']
        self.assertProblem(problems(separation(tasks=tasks)), 'placeholder left in a cell')

    def test_other_statuses_need_a_named_remainder(self):
        for empty in ('none', 'n/a', '-', 'tbd'):
            with self.subTest(empty=empty):
                nfr = [NFR[0].replace('Box A1.', empty)] + NFR[1:]
                self.assertProblem(problems(separation(nfr=nfr)), 'status unmeasured needs a named remainder')

    def test_cited_paths_must_exist_in_evidence_and_remainder(self):
        for missing in ('src/a.py', 'tests/test_a.py', 'docs/b.md'):
            with self.subTest(missing=missing):
                self.assertProblem(problems(separation(), existing=EXISTING - {missing}),
                                   f'cited path does not exist: {missing}')
        tasks = [TASKS[0], '| 2.QA | data | See `docs/missing.md`. |']
        self.assertProblem(problems(separation(tasks=tasks)), 'cited path does not exist: docs/missing.md')

    def test_evidence_must_be_backticked_files(self):
        bare = ['| BO-1 | met | src/a.py, `tests/test_a.py` | none |', OBJECTIVES[1]]
        self.assertProblem(problems(separation(objectives=bare)), 'evidence cell must be "none" or backticked')
        folder = ['| BO-1 | met | `tests/`, `tests/test_a.py` | none |', OBJECTIVES[1]]
        self.assertProblem(problems(separation(objectives=folder)), 'evidence cells must name files: tests/')

    def test_routes_and_drives_in_a_remainder_are_not_paths(self):
        tasks = [TASKS[0], '| 2.QA | human | Needs `D:/AVA-Data` and `/openapi.json`. |']
        self.assertEqual(problems(separation(tasks=tasks)), [])

    def test_summary_counts_must_match(self):
        self.assertProblem(problems(separation(counts={'met': 3})), 'summary: met says 3, rows say 2')

    def test_summary_status_listed_twice(self):
        text = separation().replace('| human | 1 |', '| human | 1 |\n| human | 1 |')
        self.assertProblem(problems(text), 'summary lists human twice')

    def test_row_with_wrong_cell_count(self):
        objectives = [OBJECTIVES[0] + ' extra |', OBJECTIVES[1]]
        self.assertProblem(problems(separation(objectives=objectives, counts={'met': 1})),
                           'BO-1 has 5 cells, expected 4')

    def test_command_line_reports_a_broken_prd(self):
        with tempfile.TemporaryDirectory() as folder:
            prd = pathlib.Path(folder) / 'prd.md'
            prd.write_text('# Nothing here\n', encoding='utf-8')
            out = io.StringIO()
            with redirect_stdout(out):
                code = cas.main(['--prd', str(prd)])
        self.assertEqual(code, 1)
        self.assertIn('FAIL', out.getvalue())


if __name__ == '__main__':
    unittest.main()
