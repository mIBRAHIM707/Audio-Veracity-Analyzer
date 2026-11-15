# Passive tap (backlog 6.2 partial; PRD FR-T2-FR-T5, FR-I1; architecture §2.1, ADR-4)

The `tap-receiver` building blocks: an ARI application that taps each call's caller
direction and a per-call RTP receiver. AVA is never in the media path, and calls never
wait on it.

## Design decision: no Stasis hand-off (15 Nov 2026, review H1)

Architecture §2.1/§7 says the dialplan sends calls through a Stasis application. The
first version did that and continued each call at once, but an independent review
showed a call in Stasis still depends on a live tap: an app that dies between
`StasisStart` and `continue`, or is slow, leaves an unanswered call waiting. That
conflicts with FR-T4. The tap therefore keeps calls out of Stasis: its event WebSocket
subscribes to all channel events (`subscribeAll=true`), and once a call channel is
answered it creates a snoop channel on it from outside (verified on Asterisk 20.6:
snooping a channel that is not in Stasis works). The dialplan has no tap step. **The
architecture text should be updated to match** (open question for the PRD/architecture
owners).

## Modules

- `ari.py`: minimal ARI client (stdlib urllib REST, HTTP basic auth, a timeout on
  every call, 2 s for cleanup; every transport, HTTP, body-size or non-JSON fault is an
  `AriError` without credentials; `websockets` sync client for `/ari/events` with
  `subscribeAll`). IDs are checked so they cannot change the URL path.
- `rtp_receiver.py`: `parse_rtp` (RFC 3550 incl. CSRC, extension, padding; bounded) and
  `RtpSession` (one UDP socket per call; the negotiated G.711 payload type only; an
  optional allowed source address; locks onto the first sender's address and SSRC;
  in-order append with gap/late/duplicate/malformed counts; `take()` hands audio over
  and keeps none, at most 10 s pending; `seconds_since_packet`).
- `tap_app.py`: `TapApp` taps a channel when it is `Up`, named `PJSIP/...`
  (configurable), **marked by the dialplan as the inbound caller leg**
  (`Set(AVA_TAP=caller)` after `Answer()`; a plain variable, so a `Dial()`ed agent leg
  is never tapped as the caller; review H-A1) and, if `tap_extensions` is set, dialled
  one of them. It builds a mixing bridge named `ava-tap` first, then the snoop
  (`spy=in`) and ExternalMedia channels, each added at once, so crash leftovers are
  always in a tagged bridge. Failures release what was created and report `failed`; a
  call is tapped once (ended or refused IDs are remembered; a `monitor_lost` end is
  not, so the call can be tapped again). Ends: `bye` (hard hangup request or
  destruction of the call, or its snoop destroyed), `timeout` (no RTP for 30 s, FR-T2),
  `monitor_lost` (ExternalMedia lost, its socket failing, or `close()`); soft hangup
  requests are ignored (teardown follows on destroy); on end the socket is drained for
  up to 40 ms. `resume()` (after every (re)connect) runs `sweep()`, which removes
  unowned `ava-tap` bridges and hangs up only their snoop/ExternalMedia channels, then
  taps marked calls that were already answered (read with ARI's channel-variable call).
  One tap process per Asterisk and app name. Malformed events and sink faults are
  counted, never raised. Each call's RTP passes through `jitter.JitterBuffer` (60 ms,
  20 ms packets only; others counted `other_ptime`; restarted on an SSRC change) and a
  `LossMonitor`; the sink gets `audio(call_id, pcm16_8k, concealment)` with one
  `(samples, concealed_samples)` pair per 20 ms frame and, if it has one,
  `quality(call_id, 'degraded'|'recovered')` once per sustained-loss episode; `ended`
  carries the receiver and `jitter_` buffer counts.
- `jitter.py`: the jitter buffer and loss monitor (backlog 6.3; see its docstring). Only
  holes closed by a later packet are concealed; a stream that stops or pauses produces
  no made-up audio and no loss (a resumed stream starts a new talkspurt). Sink: `started(call)` (call ID, caller number, dialled extension,
  creation time; `codec` is the tap's format, the call's codec is not in ARI's channel
  object), `audio(call_id, pcm16_8k)`, `ended(call_id, reason, receiver_stats)`,
  `failed(call_id, error)`.

`requirements-tap.txt`: websockets 17.4 (BSD-3-Clause).

## Evidence (15 Nov 2026)

Unit (`tests/unit/test_tap.py`, Claude-authored, runs in CI, 17 tests; also: unmarked
agent legs, `Local/`/`SIP/` channels, resume after reconnect, snoop loss, socket
failure, SSRC change from the same sender, sweep never hanging up a call channel): RTP parsing and
8 malformed cases; a real UDP session on 127.0.0.1 (order, gaps, late/duplicate, other
payload, SSRC and address lock, allowed source, sequence wrap, pending bound, silence
clock); ARI URL/ID rules, an unreachable server without leaking the password, and a
stub HTTP server returning non-JSON, deeply nested, 500 and 404 replies (all
`AriError`); TapApp with a fake ARI (taps only answered PJSIP calls of the chosen
extensions, never touches the call, own channels ignored, soft hangup ignored, no
re-tap after the end, failure at each of four steps releases everything, capacity,
audio delivered once, in-flight packets at hangup delivered, timeout, ExternalMedia
loss, close, sweep of unowned bridges only, malformed events).

Lab (`tests/lab/test_ari_tap.py`, Docker, opt-in, Claude-authored, 6 tests, all pass
together with the 6.1 lab tests): Asterisk 20.6 with ARI enabled by a random per-run
password; ExternalMedia sends RTP to this host. The lab cases run through the 60 ms
jitter buffer: the main case also asserts zero concealed samples and no late packets
over the whole call (including 1.5 s of silence before the BYE) and no quality report.
- Extension 701 dials an agent (a SIPp UAS, checked to be answered): exactly one tap,
  on the caller leg, and its audio matches the caller.
- The tap stops reading events for 8 s mid-call while staying connected: the call
  completes unharmed and the tap catches up and ends it.
- A 4 s mu-law call to extension 700 is tapped with metadata (caller token, dialled
  `700`), ends `bye` less than 5 s after the caller's BYE completed, leaves no tap
  bridge or channel in Asterisk, the receiver saw no gaps, late, malformed or foreign
  packets, and after a start-up of at most 200 ms (a few frames after the answer)
  **every tapped sample equals the injected audio**.
- No tap running: the call completes and records normally.
- Tap stopped cleanly mid-call (after it started on the call): the call completes,
  fully recorded; the tap reports `monitor_lost` and leaves nothing in Asterisk.
- Tap crashed mid-call (WebSocket dropped, no cleanup): the call completes, fully
  recorded; one `ava-tap` bridge and its ExternalMedia channel remain, and a restarted
  tap's `resume()` sweep removes them.

Not covered: masquerades/attended transfers; overlapping tap processes (the sweep would
remove the other's taps); `rtp_source` is optional (set it to the PBX address outside
the lab); the lab password is visible in the container's config and `docker exec`
environments (`unset` affects only Asterisk).

This answers the Phase 0 tap spike (task 0.9 / FR-T7) for this lab: snoop plus
ExternalMedia delivers caller RTP, so the FR-T7 fallbacks are not needed here.
Infrastructure evidence only; no detector claim.

## Not done

Feeding tapped audio into `services/scorer/speech_feed.SpeechFeed` and the live
pipeline (8 kHz -> 16 kHz streaming, concealment mask through the windower for the
FR-I2 >10 % window gate); the agent direction
(`spy=out`, FR-I8); a service process with reconnect; audio between answer and tap
start (about 100 ms) is not tapped; calls bridged to a second endpoint; ARI over TLS
and an allowed RTP source in deployment; the tap's ARI port is reachable from other
containers on the lab bridge (password-protected).
