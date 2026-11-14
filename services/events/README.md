# Events (backlog 5.1 partial; PRD FR-A4)

In-process, dependency-free building blocks for `WS /v1/events`.

## Envelope (`contract.py`)

`make_envelope(type, call_id, data, seq=, ts=)` accepts only the FR-A4 types
(`call.started`, `score.updated`, `alert.raised`, `alert.cleared`, `call.ended`,
`system.health`).

Document-backed fields: `call.ended` needs `final_state` and `reason` (FR-A4, §2.4;
reasons `bye`, `timeout`, `monitor_lost`) and `speech_seconds` (§2.4); `score.updated`
needs the degraded flag and the effective stride (FR-I6, §2.4); alerts need `alert_id`
(§2.4).

Choices made here, not in the PRD: the envelope shape (`type`, `call_id`, `seq`, `ts`,
`data`); requiring `score` (0-1 or None) and `state` on `score.updated`; the names
`effective_stride_s` (seconds, > 0) and `reason` on `system.health`; states limited to
the FR-U1 list (`monitoring`, `suspicious`, `alert`, `no_speech`, `unmonitored`);
`call_id` at most 128 characters; data as strict JSON (string keys at every level,
no NaN/Infinity, at most 32 levels deep) of at most 16 KiB of UTF-8. Urdu text is
stored as UTF-8, not `\u` escapes. Envelopes are immutable: data is held as canonical
JSON and `envelope.data` returns a fresh copy. Other fields are allowed.

Open spec question: whether the published `call.ended` should carry the scorer's
`verdict` (`undetermined`, FR-I5).

## Fan-out (`fanout.py`)

`EventHub.publish` validates and encodes outside the hub lock, then assigns one
gap-free hub-wide `seq` and copies the envelope to each matching subscriber's bounded
buffer (default 256, at most 4096). It never blocks: a full buffer drops its oldest
envelope. `Subscription.drain(limit)` returns `(envelopes, dropped_since_last_drain)`
read together; a nonzero count means re-read state over REST. Because `seq` is
hub-wide, a call-filtered subscriber normally sees gaps; order by `seq`, not `ts`
(wall clock). `system.health` without a call ID reaches every subscriber; a call-scoped
one reaches that call's and unfiltered subscribers. Subscribers are limited (default
64); extra subscriptions raise `HubFull`.

`publish` raises only `EventContractError` (invalid event) or `EventHubError` (the
hub's clock failed); either way nothing is delivered and no `seq` is used. Callers on
the scoring path must catch both, because publishing is advisory and must not stop
scoring.

Limits to know: worst-case buffered memory is subscribers x buffer x event size
(64 x 4096 x 16 KiB, about 4 GiB, if every subscriber has a full buffer of maximum-size
events); deployments should use smaller buffers or fewer subscribers. `seq` restarts at
1 with a new hub; there is no restart marker yet (FR-I11 work in 5.3).

## Not done

Mapping scorer domain events to these types (`services/scorer` produces per-direction
events; the published `call.ended` needs a call-level speech-seconds rule), Redis
pub/sub, and the PostgreSQL schema and migrations. The WebSocket endpoint and the
FR-A3 calls read model now consume this hub (`services/api`, `services/calls`, 14 Nov).
PostgreSQL 18 binaries exist on this machine (architecture names 16), but no Python
driver or Redis is pinned or installed, so migrations cannot be verified here without
a dependency decision.
