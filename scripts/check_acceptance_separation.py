"""Check the acceptance separation against the PRD and the backlog (backlog 11.QA).

Standard library only, so it runs in CI next to the unit suite. The separation
(`docs/qa/acceptance-separation.md`) classifies what still stands between the software
in this repository and the PRD's acceptance evidence. It has three tables:

    ## Objectives                   | ID | Status | Evidence | Remaining |
    ## Non-functional requirements  | Ref | PRD text | Status | Evidence | Remaining |
    ## Open backlog tasks           | Task | Held by | Remaining |

`separation_problems` returns every disagreement as a readable line: a PRD §7 objective
or §6 item without exactly one row, a row for something the PRD does not have, PRD
text that no longer matches, an open backlog box without a row (or a row for a closed
one), an unknown status, a cited path that does not exist, a `met` row without a test
or with something left, a row that is not `met` without a named remainder, or summary
counts that disagree with the rows. Whether a status is true is for independent review.

    python scripts/check_acceptance_separation.py
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
import re
import sys

try:
    from scripts.check_requirements_audit import (BACKTICK_RE, IMPLEMENTED_NOTE, NO_GAP, PRD_PATH,
                                                  REL_PATH_RE, ROOT, PathKind, _cells, _path_list,
                                                  repo_kind)
except ModuleNotFoundError:  # run as `python scripts/check_acceptance_separation.py`
    from check_requirements_audit import (BACKTICK_RE, IMPLEMENTED_NOTE, NO_GAP, PRD_PATH,  # type: ignore
                                          REL_PATH_RE, ROOT, PathKind, _cells, _path_list, repo_kind)

SEPARATION_PATH = ROOT / 'docs' / 'qa' / 'acceptance-separation.md'
BACKLOG_PATH = ROOT / 'docs' / 'context' / 'daily-backlog.md'

# In the order a row's status is chosen: the first that applies wins (see the document).
STATUSES = ('met', 'software-gap', 'decision', 'unmeasured', 'data-dependent', 'human')
HOLDS = ('software', 'decision', 'data', 'human', 'deferred')
# PRD §6 subsections whose items are acceptance criteria, and how each lists them. §6.4
# is the regulatory alignment table, accepted through BO-5 (supervisor review).
NFR_SECTIONS = {'6.1': 'table', '6.2': 'table', '6.3': 'bullets', '6.5': 'bullets', '6.6': 'bullets'}
OBJECTIVE_RE = re.compile(r'^(?:BO-\d+|RQ-[A-Z])$')
NFR_REF_RE = re.compile(r'^6\.\d\.\d+$')
TASK_RE = re.compile(r'^\d+\.(?:\d+|QA)$')
BACKLOG_BOX_RE = re.compile(r'^- \[( |x)\] (\d+\.(?:\d+|QA))\s')
BOX_LIKE_RE = re.compile(r'^\s*[-*+]\s*\[.?\]')
SUMMARY_ROW_RE = re.compile(r'^\|\s*([a-z-]+)\s*\|\s*(\d+)\s*\|\s*$')
MIN_PREFIX = 10
# A forgotten placeholder anywhere in a cell, not only at its start.
PLACEHOLDER_RE = re.compile(r'\b(?:placeholder|todo|tbd|fixme|xxx)\b', re.IGNORECASE)
# A `met` row's note may explain, but not admit that part of the clause is unmet.
UNMET_NOTE_RE = re.compile(r'\b(?:not|untested|missing|unbuilt|unmet|lacks?)\b', re.IGNORECASE)


class PrdParseError(ValueError):
    """The PRD sections this checker reads are missing or empty."""


@dataclass(frozen=True)
class Row:
    line: int
    key: str
    cells: tuple[str, ...]


def _normal(text: str) -> str:
    return ' '.join(text.replace('**', '').split())


def _sections(text: str) -> dict[str, list[str]]:
    """Heading number ('6.1', '7') -> the lines under it until the next heading of level
    1 to 3; deeper headings (####) stay inside their section."""
    found: dict[str, list[str]] = {}
    current = None
    for line in text.splitlines():
        match = re.match(r'^#{2,3} (\d+(?:\.\d+)?)\.? ', line)
        if match:
            current = match.group(1)
            found[current] = []
        elif re.match(r'^#{1,3} ', line):
            current = None
        elif current is not None:
            found[current].append(line)
    return found


def _table_first_cells(lines: list[str]) -> list[str]:
    """First cells of the body rows of the section's first table only."""
    rows: list[str] = []
    for line in lines:
        if line.startswith('|'):
            rows.append(line)
        elif rows:
            break
    # rows[0] is the header and rows[1] the |---| separator.
    return [_cells(line)[0] for line in rows[2:]]


def parse_prd(text: str) -> tuple[list[str], list[tuple[str, str]]]:
    """The §7 objective IDs and the §6 items as (ref, text), refs like '6.3.2'."""
    sections = _sections(text)
    if '7' not in sections:
        raise PrdParseError('PRD has no "## 7." acceptance section')
    objectives = [cell for cell in _table_first_cells(sections['7']) if OBJECTIVE_RE.match(cell)]
    if not objectives:
        raise PrdParseError('PRD §7 lists no BO-/RQ- objectives')
    items: list[tuple[str, str]] = []
    for number, kind in NFR_SECTIONS.items():
        lines = sections.get(number)
        if not lines:
            raise PrdParseError(f'PRD has no "### {number}" section')
        if kind == 'table':
            texts = _table_first_cells(lines)
        else:
            texts = [line[2:] for line in lines if line.startswith(('- ', '* '))]
        if not texts:
            raise PrdParseError(f'PRD §{number} lists no items')
        items += [(f'{number}.{index}', _normal(item)) for index, item in enumerate(texts, start=1)]
    return objectives, items


def parse_backlog(text: str) -> tuple[set[str], set[str], list[str]]:
    """(open, closed) task IDs from the backlog's check boxes, and every line that looks
    like a check box but cannot be read (indented, `[X]`, `*` bullet, odd task ID), so a
    box is never silently neither open nor closed."""
    open_, closed, unreadable = set(), set(), []
    for number, line in enumerate(text.splitlines(), start=1):
        match = BACKLOG_BOX_RE.match(line)
        if match:
            (open_ if match.group(1) == ' ' else closed).add(match.group(2))
        elif BOX_LIKE_RE.match(line):
            unreadable.append(f'backlog line {number}: unreadable check box {line.strip()[:40]!r}')
    return open_, closed, unreadable


TABLES = {'Objectives': (OBJECTIVE_RE, 4), 'Non-functional requirements': (NFR_REF_RE, 5),
          'Open backlog tasks': (TASK_RE, 3)}


def parse_separation(text: str) -> tuple[dict[str, list[Row]], dict[str, int], list[str]]:
    rows: dict[str, list[Row]] = {name: [] for name in TABLES}
    summary: dict[str, int] = {}
    problems: list[str] = []
    section = ''
    for number, line in enumerate(text.splitlines(), start=1):
        if line.startswith('## '):
            section = line[3:].strip()
            continue
        if not line.startswith('|'):
            continue
        cells = _cells(line)
        if section in TABLES:
            key_re, width = TABLES[section]
            if not key_re.match(cells[0]):
                continue
            if len(cells) != width:
                problems.append(f'line {number}: {cells[0]} has {len(cells)} cells, expected {width}')
                continue
            rows[section].append(Row(number, cells[0], tuple(cells[1:])))
        elif section == 'Summary':
            match = SUMMARY_ROW_RE.match(line)
            if match and match.group(1) in STATUSES:
                if match.group(1) in summary:
                    problems.append(f'line {number}: summary lists {match.group(1)} twice')
                summary[match.group(1)] = int(match.group(2))
    return rows, summary, problems


def _check_keys(table: str, rows: list[Row], expected: list[str] | set[str], what: str) -> list[str]:
    problems = []
    seen = Counter(row.key for row in rows)
    for key in sorted(set(expected) - set(seen)):
        problems.append(f'{table}: {key} is {what} but has no row')
    for key, count in sorted(seen.items()):
        if key not in expected:
            problems.append(f'{table}: row for {key}, which is not {what}')
        if count > 1:
            problems.append(f'{table}: {count} rows for {key}, expected 1')
    return problems


def _check_paths(where: str, remaining: str, kind: PathKind) -> list[str]:
    return [f'{where}: cited path does not exist: {token}' for token in BACKTICK_RE.findall(remaining)
            if REL_PATH_RE.match(token) and kind(token) is None]


def _check_criterion(where: str, status: str, evidence: str, remaining: str, kind: PathKind) -> list[str]:
    problems = []
    if status not in STATUSES:
        problems.append(f'{where}: unknown status {status!r}')
    paths = _path_list(evidence)
    if paths is None:
        problems.append(f'{where}: evidence cell must be "none" or backticked paths')
    for path in paths or []:
        found = kind(path)
        if found is None:
            problems.append(f'{where}: cited path does not exist: {path}')
        elif found != 'file':
            problems.append(f'{where}: evidence cells must name files: {path}')
    problems += _check_paths(where, remaining, kind)
    if status == 'met':
        if not any(path.startswith('tests/') for path in paths or []):
            problems.append(f'{where}: status met needs at least one test path')
        if not IMPLEMENTED_NOTE.match(remaining):
            problems.append(f'{where}: met rows keep notes as "none (...)"')
        elif UNMET_NOTE_RE.search(remaining):
            problems.append(f'{where}: a met row\'s note says something is unmet: '
                            f'{UNMET_NOTE_RE.search(remaining).group(0)!r}')
    elif NO_GAP.match(remaining.strip()):
        problems.append(f'{where}: status {status} needs a named remainder')
    return problems


def separation_problems(prd_text: str, backlog_text: str, separation_text: str, kind: PathKind) -> list[str]:
    objectives, items = parse_prd(prd_text)
    open_tasks, _closed, unreadable = parse_backlog(backlog_text)
    rows, summary, problems = parse_separation(separation_text)
    problems += unreadable
    prd_items = dict(items)
    for table, table_rows in rows.items():
        for row in table_rows:
            for cell in row.cells:
                if PLACEHOLDER_RE.search(cell):
                    problems.append(f'line {row.line} {row.key}: placeholder left in a cell: '
                                    f'{PLACEHOLDER_RE.search(cell).group(0)!r}')

    problems += _check_keys('objectives', rows['Objectives'], objectives, 'a PRD §7 objective')
    problems += _check_keys('non-functional', rows['Non-functional requirements'], list(prd_items),
                            'a PRD §6 item')
    problems += _check_keys('backlog', rows['Open backlog tasks'], open_tasks, 'an open backlog box')

    statuses = []
    for row in rows['Objectives']:
        status, evidence, remaining = row.cells
        statuses.append(status)
        problems += _check_criterion(f'line {row.line} {row.key}', status, evidence, remaining, kind)
    for row in rows['Non-functional requirements']:
        quoted, status, evidence, remaining = row.cells
        where = f'line {row.line} {row.key}'
        statuses.append(status)
        prefix = _normal(quoted).removesuffix('...').removesuffix('…').rstrip()
        if row.key in prd_items and (len(prefix) < MIN_PREFIX or not prd_items[row.key].startswith(prefix)):
            problems.append(f'{where}: PRD text {quoted!r} does not start PRD item '
                            f'{prd_items[row.key][:60]!r}')
        problems += _check_criterion(where, status, evidence, remaining, kind)
    for row in rows['Open backlog tasks']:
        held, remaining = row.cells
        where = f'line {row.line} {row.key}'
        reasons = [item.strip() for item in held.split(',')]
        unknown = [item for item in reasons if item not in HOLDS]
        if unknown:
            problems.append(f'{where}: unknown hold {", ".join(repr(item) for item in unknown)}')
        if len(set(reasons)) != len(reasons):
            problems.append(f'{where}: hold listed twice in {held!r}')
        if NO_GAP.match(remaining.strip()):
            problems.append(f'{where}: an open task needs a named remainder')
        problems += _check_paths(where, remaining, kind)

    counts = Counter(statuses)
    for status in STATUSES:
        if summary.get(status) != counts.get(status, 0):
            problems.append(f'summary: {status} says {summary.get(status)}, rows say {counts.get(status, 0)}')
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--prd', type=Path, default=PRD_PATH)
    parser.add_argument('--backlog', type=Path, default=BACKLOG_PATH)
    parser.add_argument('--separation', type=Path, default=SEPARATION_PATH)
    args = parser.parse_args(argv)
    try:
        problems = separation_problems(args.prd.read_text(encoding='utf-8'),
                                       args.backlog.read_text(encoding='utf-8'),
                                       args.separation.read_text(encoding='utf-8'), repo_kind(ROOT))
    except PrdParseError as error:
        print(f'FAIL: {error}')
        return 1
    for problem in problems:
        print(f'FAIL: {problem}')
    if problems:
        return 1
    print('PASS: every PRD §7 objective, §6 item and open backlog box has one consistent row')
    return 0


if __name__ == '__main__':
    sys.exit(main())
