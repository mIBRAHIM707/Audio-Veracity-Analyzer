# AVA original data collection plan

Prepared 4 October 2026 for todo 8. Operational plan only: it does not supply
approval, consent or any permission. It assumes the approval Tughral reported on
4 October 2026 and leaves every detail that approval has not yet supplied as an
explicit open field. Canonical requirements: PRD FR-D4 (sessions), FR-D5
(code-switching), FR-D11 (same-speaker rule), FR-E1 to FR-E6 (ethics, consent,
locality, release, licences, repository guard), D13 and D14.

## 1. What must be recorded before the first session

| Open field | Who supplies it | Where it is recorded |
|---|---|---|
| Approval reference, approving body, decision date, validity | Tughral (from the approval letter) | `approval-pack.md` required-fields table; datasheet (FR-E1) |
| Approved protocol and form versions, in English, Urdu and Shahmukhi Punjabi | Tughral with supervisor | `approval-pack.md`; controlled copy outside Git |
| Approved activities, separately: recording, cloning, listening study | Tughral | `approval-pack.md`; `source_register.json` `team_recordings` |
| Approved pilot size, adult-eligibility rule, recruitment method | Tughral | `approval-pack.md` |
| Storage location, access list, backup, deletion dates, withdrawal contact and response time | Tughral and Ibrahim | `approval-pack.md`; `.local` pointer on the storage machine |
| Compensation decision and funding approval (PRD §8.3, Rs 1,000 to 1,500 per session or volunteers) | Tughral with supervisor | `approval-pack.md`; PRD §8.3 once decided |
| Off-site processing: none, or the named in-country provider (D13) | Team decision | `approval-pack.md`; PRD D13 |
| Publication intent for dataset or paper (PRD Q12), because the consent form must say it | Team with supervisor | `approval-pack.md`; PRD Q12 |

Nothing below starts until the start gate in `approval-pack.md` is evidenced.
A session held before that is not usable and must be deleted under the approved
procedure.

## 2. Team allocation

Four people, all of whom code and all of whom collect. Allocations are working
defaults, not silos. Two members (Tughral, Ibrahim) have Claude Max and Codex
Pro access; two (Ilsa, Ayesha) do not. The plan compensates in two ways: the
AI-assisted members take the integration, review and cross-module work where
tooling saves the most time, and every coding task handed to Ilsa or Ayesha
comes with a written contract (issue, owned paths, acceptance tests) so that a
plain editor plus the repository's test commands is enough to finish it. Pairing
sessions, where one of the AI-assisted members drives review while the other
member implements, are the default for anything cross-cutting.

| Member | Collection role (first month) | Coding ownership | Tooling note |
|---|---|---|---|
| **Tughral** | Recruit and record 5 to 8 participants; owns the approval record and the datasheet entry | Model and research infrastructure: layer sweep, calibration, ONNX export, benchmark harness (WS-B); coordinator for shared context | Claude Max + Codex Pro; runs independent reviews for others' PRs |
| **Ibrahim** | Recruit and record 5 to 8 participants; validates the capture route (Asterisk lab call and WAV web recorder) with fictional material before any session | Backend, telephony, deployment: tap receiver, ingest, scorer, API, k3s (WS-C and WS-D backend) | Claude Max + Codex Pro; repository owner, merges PRs |
| **Ilsa** | Recruit and record 5 to 8 participants, leading outreach to women participants with Ayesha; owns the coded inventory and consent-use flags | Dataset engineering: intake validator extensions, manifest and split tooling, codec augmentation (WS-A code) | No paid AI tooling; tasks arrive as scoped issues with tests; pairs with Tughral for review |
| **Ayesha** | Recruit and record 5 to 8 participants, leading outreach to women participants with Ilsa; independent checker of consent gates and audio QA | Data QA and dashboard: listening-review page, quality reports, React dashboard components (WS-D frontend) | No paid AI tooling; tasks arrive as scoped issues with tests; pairs with Ibrahim for review |

Why women-led outreach for women participants: the corpus needs roughly equal
women and men (FR-D4), many prospective women participants will be more
comfortable being approached, consented and recorded by a woman, and the two
women on the team have the relevant networks. This is an outreach allocation,
not a restriction: Ilsa and Ayesha also record men, Tughral and Ibrahim also
record women where the participant is comfortable, and recording is a minority
of everyone's week. The target per person stays 5 to 8 participants in the
first month (20 to 32 total) so that nobody's coding time is displaced.

GitHub access: the repository currently has two collaborators. Ilsa's and
Ayesha's GitHub usernames need to be added with write access before their first
issue (owner action, Ibrahim or Tughral).

## 3. Recruitment and scheduling

- Sources: families and friends of the two Punjabi-speaking members, GIKI and
  Lahore student societies, one well-connected community contact (brief §6.3).
  Use only the approved recruitment method and wording; no participant contact
  before the start gate.
- Mix: mostly Majhi (Lahore region) plus some Pothwari, Saraiki and other
  regions; about half women, half men; adults only under the approved rule.
- Scheduling: one 25-minute slot per participant (10 minutes of explanation,
  questions and signed choices; 10 to 12 minutes recording; 3 minutes transfer
  and verification). Never compress the consent step to protect the recording
  window.
- Each collector keeps their own appointment list outside Git. Participants get
  a coded ID (P001 onward, allocated from one shared counter kept by Ilsa) and
  a copy of the approved information sheet.

## 4. Session procedure

Follow `session-script.md`. Summary: Part A read speech (20 to 30 sentences in
Shahmukhi Punjabi and Urdu, some with English words, 3 minutes), Part B free
speech on safe everyday prompts that invite natural code-switching (5 minutes),
Part C a fictional blocked-bank-card call (3 minutes). The participant may skip,
pause or stop at any point. Approved prompt text replaces the placeholders in
the script before use; native-speaker review of the prompts is Tughral's,
Ilsa's and Ayesha's task as the Urdu and Punjabi speakers on the team.

Capture: the Asterisk lab call (gives true telephone-path audio) or a WAV-saving
web recorder, as approved. No WhatsApp voice notes, no personal cloud sync.
Record device, sample rate, channels, bit depth and codec on the demographic
form. Keep the native capture format; do not resample at capture time.

## 5. Intake flow for every accepted session

1. Save the original recording under the coded ID only, for example
   `P017_A.wav`, `P017_B.wav`, `P017_C.wav`, plus the demographic form as
   `P017_form.json`. No names in filenames, metadata or logs.
2. Compute SHA-256 of each original and transfer originals plus hashes to the
   approved controlled storage and its one backup. Verify hashes after transfer.
   Clear recorder copies under the approved policy.
3. Add one row per original to the external intake CSV (columns from
   `data/collect/intake_template.csv`: clip ID, source `team_recordings`,
   session date as version, coded speaker ID, recording ID, language, label
   `genuine`, hash, duration, licence reference pointing to the consent record,
   `use_status` reflecting the signed choices, `split` `unassigned`).
4. Run `python data/collect/validate_intake.py <external>/intake.csv` and the
   repository guard. Fix structural errors before the next session.
5. Record the signed choices (recording, cloning, internal access, examiner
   sharing, listening stimulus, off-site processing) in the restricted
   permissions inventory, separately from audio, linked by coded ID.
6. Preparation into 3 to 6 second candidates uses the shared `--condition` path
   later, exactly as for the public pilots; originals stay immutable.

## 6. QA and review cadence

- Same day: the collector listens to each part once for private details,
  interruptions and level problems; a take containing a real name, number or
  third-party detail is handled under the approved deletion and re-record step.
- Weekly: Ayesha samples 5% of new originals and all flagged candidates for
  targeted listening; Ilsa reconciles the intake CSV, permissions inventory and
  storage listing; a short note goes to `docs/context/current-state.md` with
  counts only.
- Independent check: a second team member verifies each participant's signed
  choices against the inventory before any derivative is produced.

## 7. Weekly rhythm for the team

One 45-minute meeting: last week's counts (participants, minutes, flags), this
week's slots, blockers, and issue assignments. Each member keeps at least three
coding days per week. Recording, consent and transfer should take under four
hours per member per week at the first-month pace.

## 8. Explicit limits

- No cloning of any participant until the approved protocol covers it and that
  participant ticked the separate cloning box; recording-only participants go
  to the test split only (FR-D11).
- No listening study until its own protocol, listener consent and stimulus
  permissions exist (FR-S1).
- No public release of audio, speaker-level manifests or trained models (D14).
- No audio, consent forms or personal manifests in Git (FR-E6); the repository
  guard is run before every handoff.
