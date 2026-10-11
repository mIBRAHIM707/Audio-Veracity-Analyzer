# Synthetic benchmark handoff

Completed4 October2026; todo5. No commits, pushes or PRs. New importer:
`data/collect/csalt_synthetic.py`; preparation now retains benchmark provenance
in both modes and rejects upstream incomplete publication.

From the pinned CSALT release:68immutable originals (34per family;2each per17
publisher folders),10.48979minutes. All publisher hashes/sizes and native WAV
decodes verified.34native16kHz and34native20kHz, allmonoPCM16.

Shared preparation produced50clips,237.8265seconds (9VITS,41Tacotron),10signalflags,
zeroquarantines.391.561seconds uncovered; outcomes22full,13partial,32withoutvalid
pause segments,1allquiet. Do not interpret flags as rejects, or this imbalance
as representative generator sampling. No outputs were individually listened to.

Every output retains benchmark_only=true, training_eligible=false, publisher
class/language, folder grouping, family and explicitly unknown generator version/
reference identity. Immutable hash/metadata/gain/coverage reconstruction passed.
This is not final same-consented-target AVA training data or a cloning permission.

QA:104Python tests discovered,102passed,twoexistingWindows symlink skips. All12
actualGPT-6Astra benchmark cases passed. Independent review found and verified
fixes for incomplete input manifests and provenance loss in default preparation.
During fixture integration an importlib import fallback issue was also fixed.
Foundation checks passed; no browser change requiring Playwright execution.

Run commands are in `data/collect/README.md`. External raw/manifests/processed
locations are in `.local/data-location.json`; output report and verification JSON
are preserved. Machine-local independent checks:
`.local/verify_synthetic_originals.py`, `.local/verify_synthetic_prepared.py`.
Next:todo6 CPU smoke test. No model quality or final split acceptance claimed.
