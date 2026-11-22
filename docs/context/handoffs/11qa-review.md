# 11.QA independent review: acceptance separation

Reviewer: Claude (ava-reviewer role), 22 November 2026, read-only. Scope:
`docs/qa/acceptance-separation.md`, `scripts/check_acceptance_separation.py`,
`tests/unit/test_acceptance_separation.py` (uncommitted, on adaaf2f). Row 6.5.2's
coverage cell was ignored as instructed.

## Verdict

**Conditional pass.** The structure, checker and tests are sound and the open-box set
is complete (25 boxes, 25 rows). Six status errors (two `met` overclaims, and four rows
whose status breaks the document's own precedence rule and hides data-free software
work) and some stale holds need fixing first. After the fixes, recompute the summary
and the "What this means" prose.

## Evidence and commands

- `uv run ... python -m unittest tests.unit.test_acceptance_separation` and
  `python scripts/check_acceptance_separation.py`: **not run**. The Bash tool failed to
  start (`bash.exe not found`). The review was done by reading the files. The author's
  PASS output was not verified independently.
- Cited paths: 36 evidence paths spot-checked with Glob. All exist.
- Open boxes from `daily-backlog.md`: 3.1, 3.4, 3.QA, 5.1-5.3, 5.QA, 6.1-6.3, 6.QA,
  7.3, 7.QA, 8.1, 8.2, 8.QA, 9.2, 9.3, 9.QA, 10.1, 10.2, 10.QA, 11.1, 11.2, 11.QA. These
  match the table. 9.QA has no progress note, so "not started, data-free" is correct.
- Summary counts (5/9/2/6/14/4 = 40) match the rows as written.

## Findings

### Medium: status errors

**M1. Row 6.3.5 should be `software-gap`, not `decision`.** The PRD clause already
settles the open question: "...if FR-U7 is delivered; otherwise settings are file-only
and the audit log records `actor=system`." Setting changes must be audited in both
cases, and the file-reload case is fully specified. Confirmed: nothing in
`services/live/config_reload.py` writes `audit_log`. The note at
`services/live/README.md:551` calls this open for a PRD decision, but the PRD does not
leave it open. The FR-U7 admin settings page is also software (P1). Consequence: the
runner can do this work now, but the row hides it. Fix: mark it `software-gap`, name
"reload outcomes to `audit_log` as `actor=system`" and the settings page as the
remainder, and tell the coordinator the README note is stale.

**M2. Rows 6.2.3 and 6.2.4 (and the gap part of BO-6) hide software work that needs no
data.** (a) PRD §6.2 says "Gap targets are met when the interval of the gap includes the
target". `models/evaluation/metrics.py` has only a single-set `bootstrap_ci`, and
`report.py` has no interval for a difference (Urdu minus Punjabi, subset minus full
set). (b) FR-D5 is `not-started` in the audit, yet the PRD defines the `code_switched`
rule exactly (at least one English content word in the transcript). Row 6.2.3 admits
this ("not built either"). Under the precedence rule both rows are `software-gap`.
Fix: change both statuses, add "gap-interval bootstrap" and "FR-D5 field and rule" to
the software list, and give BO-6 a gap note. Low, same root: 6.2.6 has no
campaign harness that counts alerts per codec over N calls. That plumbing could be
built now with the untrained model, so name it or explain why not.

**M3. Row 6.1.4 `met` overclaims.** §6.1 is headed "reference box A1". On A1 the
cadence target only holds if inference keeps up at the concurrent load. The evidence
is a design constant (`HOP_SAMPLES = 2 * RATE`) and a unit test of the windower, not a
measured cadence. Fix: `unmeasured`, with the note "the 2 s speech-window rule is
built". Alternatively, record a scope note that §6.1 cadence counts as a design rule,
if the coordinator accepts that reading.

**M4. Row 6.3.3 `met` overclaims.** (a) The row's own note says "image layers are not
scanned for credentials", so half of the clause ("no credentials in images") has no
test. `.dockerignore` is a good allowlist (`*` first), but the only test of it checks
`!services/**/*.py` (`tests/unit/test_compose_demo_profile.py:116`). (b) The FR-O5
snippet AES key is a secret, but it is generated into a PVC by an init container
(`deploy/k8s/base/live.yaml:63-82,181`), not held in a Kubernetes Secret. (c)
`tests/unit/test_k8s_manifests.py` mostly skips in CI: `foundation.yml` installs only
`requirements-audio.txt`, not PyYAML. Fix: make it `software-gap`, and add a static
test that the build context admits no key, `.env` or secret patterns (or a layer scan
in the lab). Record whether the PVC-held key is an accepted deviation; if it needs a
decision, say so.

**M5. Row 6.5.2 omits half of the PRD item.** The criterion continues: "an end-to-end
test that plays a known synthetic file through Asterisk and asserts an alert, and a
genuine file and asserts none". That needs the trained model. Fix: whatever the
coverage result, the Remaining cell must name this (data-dependent) part too.

**M6. Row 6.1.7 `unmeasured` contradicts the status definition** ("the software
exists"). Its evidence is `none`, and `models/export/benchmark_onnx.py` says it does
not record RSS. The only serving-process memory reading is the lab's `/proc` sampler
in `tests/lab/test_compose_stack_lab.py`. Fix: either cite that sampler as the
instrument and say how it will run on A1, or mark the row `software-gap` (add a
serving-worker RSS measurement; no data needed).

### Low: backlog rows

**L1. 3.4.** Held by omits `decision`, although the Remaining cell itself calls
handset versus speakerphone labelling a PRD question. "G.729 has no free encoder" is
not remaining work: FR-D6 says "Opus ... or G.729", and Opus was built on 15 Nov.

**L2. 11.1 versus 5.3.** Row 5.3 calls snippet-key rotation an operator decision. Row
11.1 lists key rotation but is held by `software` only. Fix: add `decision` to 11.1, or
reconcile the two rows.

**L3. 10.2 and 6.3.2 (TLS).** The backlog (19 Nov) says "Ingress/TLS for the dashboard
(auth decision)", and `deploy/k8s/README.md:17` still says "the API has no
authentication". FR-U7 login was added on 19 Nov, so both are stale. The separation
treats TLS as plain software without saying why. Fix: state that auth now exists (so
the hold is software), or keep a `decision` hold. Report the stale README to the
coordinator.

**L4. Issue #28 is missing.** The suspected AMR augmentation defect (about 3 dB SNR;
`docs/context/current-state.md:95`) is open, data-free investigation work that appears
nowhere in the document, because its box (3.3) is closed. Add it to the software list.

**L5. Row 6.3.6 evidence is thin for "any component".** The claim is true for the
dashboard (RC03a scans built files and the CSP; RC03b aborts every other origin, with
controls). Also cite `tests/unit/test_ort_runtime.py` (ONNX Runtime telemetry forced
off) and `tests/unit/test_monitoring_dashboard.py` (Grafana call-home settings off).
Note that the dashboard Playwright project is not in CI.

### Low: checker and tests

**L6. Any "none (...)" note satisfies a `met` row.** `IMPLEMENTED_NOTE` accepts any
note, so a `met` row can carry an unmet clause, as 6.3.3 does. Fix: a review rule, or
flag notes on `met` rows that contain "not".

**L7. Prefix-only matching.** A PRD change to the tail of an item, or to its target or
stretch columns, never forces a revisit (6.5.2 shows this). The Objectives table has no
PRD-text column at all, so if BO-4 dropped "in CI" nothing would flag it. Fix: quote
the full normalised item, or store a short SHA-256 of it per row, objectives included.

**L8. Parsing can drop items silently.**
- A backlog box written `[X]` or indented is neither open nor closed. Fix: flag every
  `- [` line that fails `BACKLOG_BOX_RE`.
- A `####` heading inside a §6.x section ends the section, and later bullets vanish.
- A second table in §6.1 or §6.2 would turn its header and `---` rows into items.
- `* ` bullets are skipped.

The real-PRD test pins the objective list exactly but checks only `len(items) >= 25`.
Fix: pin the per-section counts (8, 8, 7, 4, 2) and add a synthetic test for each case
above.

**L9. Placeholders pass.** `COVERAGE-PLACEHOLDER`, or "TODO" anywhere after the first
word, passes, because `NO_GAP` only checks the start of the cell. Fix: reject
`PLACEHOLDER`, `TODO` and `TBD` tokens anywhere, so a forgotten cell cannot reach the
PR.

**L10. Linux CI.** No blocker found: the checker is stdlib only, reads UTF-8, and the
`scripts.` import with fallback matches `test_requirements_audit.py`. One caveat: on
Windows `repo_kind` matches paths case-insensitively, so a mis-cased path passes locally
and fails only on Ubuntu CI. That failure is loud, not silent.

## Not assessed or noted

- The checker run and the full unit suite (shell unavailable).
- `tests/browser/dashboard-viewport.spec.cjs` appeared during the review, apparently
  still in progress (`SIZES` holds 480x400). If it lands, row 6.6.2 must change in the
  same change.
- Wording: no accuracy or detection claims. Every figure from the untrained model or
  the laptop (0.43 s, 151 ms, 2.772 GB) is labelled as not §6 evidence.

## Proposed next action

The author fixes M1-M6 and L1-L5, recomputes the summary and "What this means", and
reruns the checker and its tests. L6-L9 can be a follow-up hardening step. The
coordinator corrects the stale notes in `services/live/README.md:551` and
`deploy/k8s/README.md:17`.

## Re-review (22 November 2026)

Read-only re-check of the applied fixes. Commands were again **not run**: the shell is
unavailable, so the reported PASS results (checker, unit tests, Playwright 7 passed,
coverage) are taken from the author and were not verified independently.

**Verdict: pass.** No blocking or Medium findings remain. The Low items below are
follow-ups.

Verified fixed:
- **M1.** Row 6.3.5 is now `software-gap` and quotes the PRD's `actor=system` case.
  `services/live/README.md:551-554` is corrected.
- **M2.** BO-6, 6.2.3 and 6.2.4 are now `software-gap` and name the gap-interval
  bootstrap and FR-D5.
- **M3.** Row 6.1.4 is now `unmeasured`.
- **M4.** Row 6.3.3 is now `software-gap` and names the image credential check, the
  key held on a volume claim and the PyYAML skips.
- **M5.** Row 6.5.2 now names the end-to-end alert test. `coverage==7.10.7` is pinned
  in `requirements-test.txt` and marked as a measurement-only tool.
- **M6.** Row 6.1.7 is now `software-gap`.
- **L1.** Row 3.4 now holds `human, software, decision` and carries the Opus note.
- **L2.** Row 11.1 adds `decision`.
- **L3.** The TLS rows say authentication exists. `deploy/k8s/README.md:17` is
  corrected.
- **L4.** Issue #28 is in the summary.
- **L5.** Row 6.3.6 has the extra citations.
- **Summary.** I recounted the rows: 4/13/1/6/12/4 = 40, which matches.
- **Backlog.** The 11.QA box is checked with evidence, and its row is gone. The other
  24 open boxes each still have one row.
- **Checker.**
  - `####` headings now stay inside their section.
  - `*` bullets are read.
  - Unreadable boxes (indented, `[X]`, `*`, odd ID, other mark) are reported.
  - Placeholders are caught anywhere in a cell.
  - A `met` note that says something is unmet is rejected.
  - The per-section counts (8, 8, 7, 4, 2) are pinned against the real PRD.
  - Each rule has its own synthetic test.
  - The three `met` notes contain no unmet-word match ("cannot" is not matched by
    `\bnot\b`).
- **L7.** The limitation is documented in the document's introduction.
- **Row 6.6.2 `met`.** The spec has a VP00 self-check that proves the layout check
  catches sideways scroll, overflow and clipping. VP01-VP03 run at 1366x768 and
  1920x1080 and assert the window size, eight cards with an alert on the first screen,
  a batch result with its timeline, and the history page. This is acceptable as
  infrastructure evidence for the scoped reading of "renders correctly".

Remaining Low findings:
- **R1. Row 6.2.6 contradicts the `data-dependent` definition.** The definition says
  "the software that can be built without data is built", but the row's own note says
  the campaign driver "could be built with the untrained model". Fix: either mark the
  row `software-gap`, or add a carve-out to the definition for harnesses whose output
  is meaningless without the data. Wording only; the counts change by one if the
  status changes.
- **R2. The 95% coverage figure has no recorded command or output.** It appears only in
  the separation and backlog text: nothing in `current-state.md` or the 22 Nov
  daily-run log. Record the exact command, the package set, the skipped count and the
  per-module report there, so the number can be reproduced. Also state whether
  "ingestion" covers only `services/tap`, or also `services/live` and
  `services/events`.
- **R3. Row 6.6.2's scope is wider in wording than in tests.** The note says "the
  history page", but VP03 checks it only without a store (error or empty state). Stored
  rows, the sign-in page and the feedback and snippet panels are not checked at these
  sizes. Fix: say "history page without a store" in the note, or add a case on
  PostgreSQL. The dashboard project is still outside CI, as the note already states.
- **R4. "First table only" is a new way to drop items silently.** A second §6.1 or
  §6.2 table is now ignored. The pinned real-PRD counts make the test fail if items
  change, so this risk is accepted; no change is required.
