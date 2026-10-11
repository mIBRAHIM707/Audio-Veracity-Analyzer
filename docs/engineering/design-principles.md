# Construction principles for AVA

These choices reduce defect risk; they do not guarantee a bug-free system. Apply
each when the relevant component exists. Current intake code does not need a
microservice framework or class hierarchy to validate a CSV.

## Boundaries and contracts

Keep domain rules independent of HTTP, audio decoders, storage, model runtimes and
the browser. Functions receive explicit inputs and return structured results.
At real boundaries use validated data contracts; internal code must not repeatedly
reinterpret sample rates, units, timestamps, label order or model versions.

Separate intake, conditioning, inference, scoring and delivery. Validate malformed
input at entry, encode domain invariants once, and test adapters against contracts.
Use interfaces/protocols only for real interchangeable dependencies. Examples are
a model scorer, audio source, clock, and persistence store. Inject those collaborators
for deterministic tests rather than monkeypatching global state throughout the system.

## Patterns with a concrete purpose

| Need | Suitable design | Evidence required |
|---|---|---|
| File and RTP inputs share processing | Adapters into one conditioning pipeline | Equivalent conditioned samples for equivalent input |
| CPU runtime or model changes | Strategy behind a small scoring contract | Shape, class-order and numerical parity checks |
| Monitoring, suspicious and alert transitions | Explicit finite state machine | Transition table, threshold boundaries, re-alert and end-of-call tests |
| Database independent of scoring | Repository boundary only where persistence is used | Restart, idempotency and transaction tests |
| Browser receives call events | Typed events with IDs and lifecycle ordering | Reconnection, duplicates and late-event behavior |
| Dataset processing can restart | Idempotent stages with lineage and atomic output writes | Interrupted job resumes without duplicate accepted records |
| Multiple calls compete for CPU | Bounded queues and explicit backpressure | Capacity refusal, scheduling fairness and bounded memory |

Prefer composition over inheritance. Follow single responsibility and explicit
dependency direction, but avoid a universal base class, service locator, generic
repository framework, or plugin system without a real need. Remove duplication
of domain rules; do not merge unrelated code merely because it looks similar.

## Correctness details that are easy to miss

- Name seconds, samples, channels and rates in contracts; never infer them from filenames.
- Treat VAD speech-time and wall-clock time as different axes. Timeline mapping
  must preserve original timestamps when silence is removed.
- Audio paths, manifests and model metadata are untrusted inputs. Limit size and
  duration; validate path boundaries, missing fields and non-finite numbers.
- Use context managers/finally for sockets, files, temp data and sessions. Define
  behavior on cancellation, timeout, duplicate events and completion after hangup.
- Never derive rights or cloning consent from an extension, source name or public URL.
- Split source speakers, reference voices, recordings and derived clips coherently.
  Content hashes catch exact duplicates, not all near-duplicates or reused speakers.
- Calibrate and tune on development data. Freeze final test data and report uncertainty.
- Model loading fails explicitly on metadata mismatch. Local model loading is not
  evidence of offline operation until a blocked-egress test passes.
- Logging uses IDs and aggregate diagnostics, not private audio or identifying metadata.

## Test layers

Fast unit tests check pure rules and numeric boundaries. Integration tests cover
decoders, storage, model adapters and service contracts. Playwright checks real
browser flows against an actual app; local fixture tests validate only the browser
harness. Separate ML quality, latency/concurrency and PBX call-survival campaigns
provide evidence that UI tests cannot. Avoid excessive mocks and tests that merely
repeat the implementation. Every regression test should fail for the actual defect.

An ADR is warranted when changing a boundary, persistence contract, model strategy,
source-of-truth location, or irreversible data transformation. Small internal
refactors need a clear PR explanation rather than ceremonial architecture documents.
