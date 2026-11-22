"""Check the requirements and evidence audit against the canonical PRD (backlog 11.2).

Standard library only, so it runs in CI next to the unit suite. The audit
(`docs/qa/requirements-evidence-audit.md`) has one Markdown table row per PRD
functional requirement:

    | ID | P | Status | Code and documents | Tests | Gap |

`audit_problems` returns every disagreement as a readable line; an empty list means
the audit is consistent with the PRD and the repository. It does not judge whether a
status is true: that is what independent review of the evidence is for.

    python scripts/check_requirements_audit.py
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
import re
import sys
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
PRD_PATH = ROOT / '_bmad-output' / 'planning-artifacts' / 'prd.md'
AUDIT_PATH = ROOT / 'docs' / 'qa' / 'requirements-evidence-audit.md'

STATUSES = ('implemented', 'partial', 'data-dependent', 'not-started', 'human', 'deferred')
ID_RE = re.compile(r'FR-[A-Z]+\d+')
# A PRD requirement row: "| FR-D1 | text ... | P0 |". The phase table ("| FR-D1 to
# FR-D15, ... |") does not match because the ID cell must hold one ID only.
PRD_ROW_RE = re.compile(r'^\|\s*(FR-[A-Z]+\d+)\s*\|.*\|\s*(P[012])\s*\|\s*$')
SUMMARY_ROW_RE = re.compile(r'^\|\s*([a-z-]+)\s*\|\s*(\d+)\s*\|\s*$')
BACKTICK_RE = re.compile(r'`([^`]+)`')
# A repository-relative path inside a gap note: at least one slash, no drive letter,
# no leading slash (that would be a URL route such as `/openapi.json`).
REL_PATH_RE = re.compile(r'^\.?[A-Za-z0-9_][A-Za-z0-9_.-]*(/[A-Za-z0-9_.-]+)+/?$')
COLUMNS = 6


@dataclass(frozen=True)
class AuditRow:
    line: int
    req_id: str
    priority: str
    status: str
    code: str
    tests: str
    gap: str


class PrdParseError(ValueError):
    """A PRD requirement row that cannot be read, or an ID defined twice."""


# Any table row whose first cell is a single requirement ID, whatever its last cell.
PRD_ID_ROW_RE = re.compile(r'^\|\s*(FR-[A-Z]+\d+)\s*\|')
REQUIREMENT_HEADER_RE = re.compile(r'^\|\s*ID\s*\|\s*Requirement\s*\|\s*Priority\s*\|\s*$')


def parse_prd(text: str) -> dict[str, str]:
    """Requirement ID -> priority, from the PRD's requirement tables.

    A requirement table is one whose header is `| ID | Requirement | Priority |`; other
    tables (the phase map, for example) are ignored. Inside one, every row that starts
    with a single ID must end in a plain P0/P1/P2 cell, and no ID may be defined twice;
    otherwise PrdParseError, so a row is never skipped silently.
    """
    found: dict[str, str] = {}
    in_table = False
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.startswith('|'):
            in_table = False
            continue
        if REQUIREMENT_HEADER_RE.match(line):
            in_table = True
            continue
        if not in_table:
            continue
        id_match = PRD_ID_ROW_RE.match(line)
        if not id_match:
            continue
        match = PRD_ROW_RE.match(line)
        if not match:
            raise PrdParseError(f'PRD line {number}: {id_match.group(1)} row has no plain '
                                f'P0/P1/P2 priority cell')
        if match.group(1) in found:
            raise PrdParseError(f'PRD line {number}: {match.group(1)} is defined twice')
        found[match.group(1)] = match.group(2)
    return found


def _cells(line: str) -> list[str]:
    inner = line.strip()
    if inner.startswith('|'):
        inner = inner[1:]
    if inner.endswith('|'):
        inner = inner[:-1]
    return [cell.strip() for cell in inner.split('|')]


def parse_audit(text: str) -> tuple[list[AuditRow], dict[str, int], list[str]]:
    """Requirement rows, the summary table and malformed-row problems."""
    rows: list[AuditRow] = []
    summary: dict[str, int] = {}
    problems: list[str] = []
    section = ''
    for number, line in enumerate(text.splitlines(), start=1):
        if line.startswith('## '):
            section = line[3:].strip()
            continue
        if not line.startswith('|'):
            continue
        first = _cells(line)[0]
        if ID_RE.fullmatch(first):
            cells = _cells(line)
            if len(cells) != COLUMNS:
                problems.append(f'line {number}: {first} has {len(cells)} cells, expected {COLUMNS}')
                continue
            rows.append(AuditRow(number, *cells))
            continue
        summary_match = SUMMARY_ROW_RE.match(line)
        if section == 'Summary' and summary_match and summary_match.group(1) in STATUSES:
            if summary_match.group(1) in summary:
                problems.append(f'line {number}: summary lists {summary_match.group(1)} twice')
            summary[summary_match.group(1)] = int(summary_match.group(2))
    return rows, summary, problems


def _path_list(cell: str) -> list[str] | None:
    """The paths in a code/tests cell, [] for "none", None if the cell is malformed."""
    if cell == 'none':
        return []
    items = [item.strip() for item in cell.split(',')]
    paths = []
    for item in items:
        match = re.fullmatch(r'`([^`]+)`', item)
        if not match:
            return None
        paths.append(match.group(1))
    return paths


PathKind = Callable[[str], 'str | None']
# A gap cell that names nothing: empty, "none (...)", "n/a", "tbd" or a dash.
NO_GAP = re.compile(r'^(?:$|(?:none|n/?a|tbd|todo)\b|[-–—?])', re.IGNORECASE)
# An implemented row's gap cell: exactly "none", or "none (a note)".
IMPLEMENTED_NOTE = re.compile(r'^none(?: \(.+\))?$')


def audit_problems(prd: dict[str, str], audit_text: str, kind: PathKind) -> list[str]:
    """Every disagreement between the audit, the PRD IDs and the repository.

    `kind(path)` returns 'file', 'dir' or None (missing). Code and test cells must name
    files; a path inside a gap note may also be a directory.
    """
    rows, summary, problems = parse_audit(audit_text)
    seen = Counter(row.req_id for row in rows)
    for req_id in sorted(set(prd) - set(seen)):
        problems.append(f'{req_id}: in the PRD but has no audit row')
    for req_id, count in sorted(seen.items()):
        if req_id not in prd:
            problems.append(f'{req_id}: audit row for an ID that is not in the PRD')
        if count > 1:
            problems.append(f'{req_id}: {count} audit rows, expected 1')
    for row in rows:
        where = f'line {row.line} {row.req_id}'
        if row.req_id in prd and row.priority != prd[row.req_id]:
            problems.append(f'{where}: priority {row.priority}, PRD says {prd[row.req_id]}')
        if row.status not in STATUSES:
            problems.append(f'{where}: unknown status {row.status!r}')
        code = _path_list(row.code)
        tests = _path_list(row.tests)
        for label, paths in (('code', code), ('tests', tests)):
            if paths is None:
                problems.append(f'{where}: {label} cell must be "none" or backticked paths')
        for path in (code or []) + (tests or []):
            found = kind(path)
            if found is None:
                problems.append(f'{where}: cited path does not exist: {path}')
            elif found != 'file':
                problems.append(f'{where}: code and test cells must name files: {path}')
        for token in BACKTICK_RE.findall(row.gap):
            if REL_PATH_RE.match(token) and kind(token) is None:
                problems.append(f'{where}: cited path does not exist: {token}')
        if row.status != 'implemented' and NO_GAP.match(row.gap.strip()):
            problems.append(f'{where}: status {row.status} needs a named gap')
        if row.status in ('implemented', 'partial'):
            if not any(path.startswith('tests/') for path in tests or []):
                problems.append(f'{where}: status {row.status} needs at least one test path')
            if not code:
                problems.append(f'{where}: status {row.status} needs at least one code path')
        if row.status == 'implemented' and not IMPLEMENTED_NOTE.match(row.gap):
            problems.append(f'{where}: implemented rows keep notes as "none (...)"')
    counts = Counter(row.status for row in rows)
    for status in STATUSES:
        if summary.get(status) != counts.get(status, 0):
            problems.append(f'summary: {status} says {summary.get(status)}, '
                            f'rows say {counts.get(status, 0)}')
    return problems


def repo_kind(root: Path) -> PathKind:
    """'file', 'dir' or None for a path relative to `root`; paths leaving it are None."""
    def kind(path: str) -> str | None:
        target = (root / path).resolve()
        if not target.is_relative_to(root):
            return None
        if target.is_file():
            return 'file'
        return 'dir' if target.is_dir() else None
    return kind


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--prd', type=Path, default=PRD_PATH)
    parser.add_argument('--audit', type=Path, default=AUDIT_PATH)
    args = parser.parse_args(argv)
    try:
        prd = parse_prd(args.prd.read_text(encoding='utf-8'))
    except PrdParseError as error:
        print(f'FAIL: {error}')
        return 1
    if not prd:
        print(f'FAIL: no requirement rows found in {args.prd}')
        return 1
    problems = audit_problems(prd, args.audit.read_text(encoding='utf-8'), repo_kind(ROOT))
    for problem in problems:
        print(f'FAIL: {problem}')
    if problems:
        return 1
    print(f'PASS: {len(prd)} PRD requirements, one audit row each, all cited paths exist')
    return 0


if __name__ == '__main__':
    sys.exit(main())
