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
read together; a nonzero count means re-read state over REST. `Subscription.buffered()`
(16 Nov) is the number waiting now (used to report what a stopping pump left). Because `seq` is
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
1 with a new hub, and every hub has its own random `epoch` (32 hex characters,
`EventHub(epoch=)` to fix one in tests; 17 Nov), which the Redis bridge uses as a
restart marker.

Upstream loss (16 Nov): `EventHub.report_upstream_loss(missing=, interruptions=)` tells
every current subscriber (filtered ones too: the lost events' call IDs are unknown)
that a relay lost events before they reached this hub; it uses no `seq`.
`Subscription.drain_batch(limit)` returns `Drained(envelopes, dropped,
upstream_missing, upstream_interruptions)` read together under the lock; `drain()` keeps
its `(envelopes, dropped)` result and leaves upstream loss for `drain_batch`. Loss is
reported at the start of the batch in which it became known, so a few envelopes in that
batch may predate it; any nonzero count means re-read REST.

`publish(..., ts=)` (15 Nov) keeps an event's original time when it is relayed from
another process; the hub still assigns its own `seq`. An invalid `ts` is a contract
error and uses no `seq`.

## Redis bridge (`redis_bridge.py`, 15 Nov)

Architecture §2.4/§2.5: the scorer publishes to Redis pub/sub and `api` fans out.
`RedisForwarder(subscription, client).pump()` drains an unfiltered hub subscription
and PUBLISHes each envelope's JSON on `ava:events:v1`; Redis faults are counted
(`publish_failures`), never raised into scoring, and those envelopes are lost.
`RedisRelay(connect, hub).pump(timeout)` subscribes, treats each message as untrusted
(64 KiB bound, strict UTF-8 JSON without NaN/Infinity, exactly the five envelope keys
plus, since 17 Nov, an optional `source`; the FR-A4 contract re-checked) and republishes valid events into the local hub with
the source `ts`. The source `seq` only detects loss: a jump counts `gaps` and
`missing`, a step back counts `restarts` for messages without `source` (see "Source
epoch" below for the rules with it). Pub/sub is
at-most-once: events published while the relay is disconnected are lost and show up as
a gap at the next message; after a connection error the relay resubscribes on the next
pump. One forwarder per channel, on an unfiltered subscription (several scorer
processes on one channel would interleave their sequences; a filtered one is refused);
a pump may return before `timeout` (subscribe confirmations), so call it in a loop.
After the first failed publish a forwarder pump counts the rest of its batch as failed
without trying, so an unreachable Redis costs one client timeout per pump. A source
restart (seq back to n) counts n - 1 missing; loss just before a restart is invisible.

Source epoch (17 Nov; closes the 16 Nov review L2). The wire form is the five envelope
keys plus `source`, the forwarding hub's `epoch` (`encode_message`); the relay accepts
messages with or without it (`parse_message_with_source`; a `source` that is not 32
lowercase hex characters is invalid). Relay rules:

| Message | Counted as | Reported to subscribers |
|---|---|---|
| same `source`, `seq` = last + 1 | relayed | nothing |
| same `source`, `seq` > last + 1 | gap, `missing` = the jump | `missing` |
| new `source` (any `seq`) | `restarts`, `missing` = seq - 1 | `missing` and 1 interruption |
| same `source`, `seq` <= last | `out_of_order`, dropped (last seq kept) | 1 interruption per run of them |
| no `source`, `seq` <= last | `restarts` (the earlier rule) | `missing` and 1 interruption |
| `source` appears or disappears | `restarts` | `missing` and 1 interruption |

So a restart is found even when the new hub's first events were lost (its first seen
`seq` may be above the old one, or exactly the next one). A message within one epoch
that does not advance is a duplicate, a fault or a restarted hub that reused its
epoch; it is not delivered, and subscribers get one interruption at the start of each
such run (the next relayed message ends it). The relay cannot tell a harmless duplicate
from a reused epoch, so a duplicate also produces that "re-read REST" hint with nothing
lost. After a reused-epoch run, the message that ends it is compared with the old last
`seq`, so the new hub's own skipped events are not counted in `missing` (the
interruption already covers them). Epochs must be unique per hub:
`EventHub(epoch=)` is for tests. The wire `seq` must be at least 1 (hubs number from
1), so a restart never counts a negative loss.

Upgrade order: an API relay from before 17 Nov accepts exactly the five envelope keys
and so refuses every message with `source` (all `invalid`, a blank live board).
Update or restart the API (`python -m services.api`) before the scorer
(`python -m services.live`); the new relay accepts both forms.
`out_of_order` is a fourth message outcome on the API's
`ava_event_relay_messages_total`. One forwarder per hub: a forwarder made on a
subscription without `hub_epoch` (a test fake) uses its own random epoch.

Conditions for wiring (not enforced by the module): each pump runs on its own thread,
never the scoring thread or the API event loop; clients set `socket_timeout`,
`socket_connect_timeout` and `health_check_interval` (otherwise a silently dropped
connection blocks or is never noticed); the caller owns and closes the clients.

Loss reports (16 Nov): the relay tells its hub (`report_upstream_loss`) about a sequence
gap (`missing`, before the event after the gap is published), a source restart (an
interruption, plus `missing` = seq - 1 when known), a valid event the hub refused
(`missing` 1), and a Redis outage (one interruption at its start, until a subscription
succeeds again; the next message's seq may add a counted gap). Counters `interruptions`
and `loss_report_errors` (a hub without loss reports) join the relay stats. Invalid
messages show up as a gap at the next valid one.

`relay_loop.py` (16 Nov): `RelayLoop(relay)` runs the relay on one owned daemon thread
(`ava-redis-relay`): `pump(timeout=0.5)` while subscribed with a 20 ms pause after an
empty turn; while unsubscribed a doubling backoff from 0.5 s to 10 s, reset after a
subscription; a stop wakes any wait. `stop(join_timeout_s)` (default wait + 1 s) closes
the relay's subscription only after the thread ended; a thread stuck in a client call is
reported `stuck` and left behind as a daemon. Pump exceptions are counted (`faults`,
`last_fault` type name). `open_relay_client(url)` builds the redis-py client: 2 s socket
and connect timeouts (above the 0.5 s pump wait), TCP keepalive, a 10 s pub/sub health
check and no client retries. The analysis API runs it (`create_app(..., relay=)`).
Messages are not authenticated: any client that can PUBLISH on the channel can inject
valid-looking events, so Redis must be on the private lab/cluster network with an ACL
(a deployment decision, 10.x). The 64 KiB message bound is checked after redis-py has
read the message; Redis's `client-output-buffer-limit pubsub` bounds memory. The
real-Redis tests (which rely on redis-py 8.1.0 not resubscribing by itself after
`CLIENT KILL`) do not run in CI. Done since: the forwarder thread in the live service
(16 Nov), the relay thread in the API and relay loss as `stream.gap` (16 Nov). Not done:
an API command line that opens Redis, Redis session state (hash per call), AOF.

`requirements-redis.txt`: redis-py 8.1.0 (MIT). Tests `tests/unit/test_redis_bridge.py`
(Claude-authored, 15 Nov) and `tests/unit/test_event_relay_api.py` (Claude-authored,
16 Nov; hub loss reports, relay loss reports, the relay thread, the API wiring, and one
real-Redis case: hub -> forwarder -> Redis -> relay thread -> WebSocket, with
`CLIENT KILL` becoming `stream.gap`): pure tests with a fake client always run; real-Redis cases
need `AVA_TEST_REDIS_URL` (skipped in CI). Local run, with `.local/redis-test.env`
holding `AVA_TEST_REDIS_URL=redis://127.0.0.1:56379/0` and `AVA_REQUIRE_REDIS=1`:

```text
docker run -d --rm --name ava-redis-test -p 127.0.0.1:56379:6379 redis:7-alpine
uv run --no-project --python 3.11 --env-file .local/redis-test.env --with-requirements requirements-redis.txt python -m unittest tests.unit.test_redis_bridge
docker stop ava-redis-test
```

Tested on `redis:7-alpine` (Redis 7.4.12, digest
`sha256:7f7d30b7fdd09467bac58198a1f166dd69789d348d4fe859cccb20e0cf287fbe`).

## Not done

Scorer events are mapped to these types by `services/scorer/publish.py`
(14 Nov; call-level speech seconds are the sum over scored directions, a choice to
confirm). The WebSocket endpoint and the FR-A3 calls read model now consume this hub
(`services/api`, `services/calls`, 14 Nov). The PostgreSQL schema, migrations and a
per-envelope durable store are in `services/store` (15 Nov, psycopg 3, tested on a
PostgreSQL 16 container); the pump from the hub into the store is not wired yet.
