"""Redis pub/sub bridge for FR-A4 events between processes (backlog 5.1/5.2; PRD
FR-A4; architecture §2.4 "publishes events to a Redis pub/sub channel consumed by
`api`", §2.5 "WebSocket /v1/events fans out Redis pub/sub events", §2.7 Redis 7).

Scorer side: `RedisForwarder(subscription, client).pump()` drains an `EventHub`
subscription and PUBLISHes each envelope's JSON on one channel. Publishing is
advisory: a Redis failure is counted, those envelopes are lost (pub/sub has no
backlog anyway), and scoring is never held up or raised into. The hub's own overflow
drops are counted too.

API side: `RedisRelay(connect, hub).pump(timeout)` reads the channel, treats every
message as untrusted (size bound, strict JSON, the FR-A4 envelope contract re-checked
by `make_envelope`) and republishes valid events into the local hub with their
original `ts`; the local hub assigns its own gap-free `seq`. The source `seq` is used
only to detect loss: a jump counts the missing events (`gaps`, `missing`), a step back
is a source restart (`restarts`, its `seq` restarted at 1). Redis pub/sub is
at-most-once: events published while the relay is disconnected are lost, which the
next message's sequence reveals; consumers should then re-read state over REST. After
a connection error the relay drops its connection and resubscribes on the next pump.

Clients are injected (`redis.Redis` from `requirements-redis.txt`, or a fake in tests);
this module does not import redis itself.
"""
import json

from .contract import EventContractError, make_envelope

CHANNEL = 'ava:events:v1'
MAX_MESSAGE_BYTES = 64 * 1024  # envelope data is at most 16 KiB; leaves room for the wrapper
ENVELOPE_KEYS = {'type', 'call_id', 'seq', 'ts', 'data'}


MAX_SEQ = 2 ** 63 - 1


class RedisForwarder:
    """Owned by one thread (the pump loop), never the scoring thread: a Redis call can
    take up to the client's socket timeouts."""

    def __init__(self, subscription, client, *, channel=CHANNEL, batch=256):
        if type(batch) is not int or batch < 1:
            raise ValueError('batch must be a positive integer')
        if getattr(subscription, 'call_id', None) is not None:
            raise ValueError('forward an unfiltered subscription: the relay reads seq gaps as loss')
        self._subscription, self._client, self.channel, self._batch = subscription, client, channel, batch
        self.stats = {'forwarded': 0, 'publish_failures': 0, 'hub_dropped': 0, 'no_receivers': 0}

    def pump(self):
        """Forward what the subscription holds now (at most `batch`); returns the number
        forwarded. Never raises for Redis faults. After the first failed publish the
        rest of the batch is counted as failed without further attempts, so an
        unreachable Redis costs one client timeout per pump, not one per envelope."""
        envelopes, dropped = self._subscription.drain(self._batch)
        self.stats['hub_dropped'] += dropped
        sent = 0
        for index, envelope in enumerate(envelopes):
            try:
                receivers = self._client.publish(self.channel, envelope.to_json())
            except Exception:  # any client or connection fault: advisory, the batch is lost
                self.stats['publish_failures'] += len(envelopes) - index
                break
            sent += 1
            if receivers == 0:
                self.stats['no_receivers'] += 1  # delivered to Redis, but nobody subscribed
        self.stats['forwarded'] += sent
        return sent


def parse_message(payload):
    """Untrusted pub/sub payload (bytes or str) -> validated Envelope with the source
    seq and ts. Raises EventContractError."""
    if isinstance(payload, str):
        payload = payload.encode('utf-8')
    if not isinstance(payload, bytes) or len(payload) > MAX_MESSAGE_BYTES:
        raise EventContractError('message is not bytes or exceeds the size bound')
    try:
        value = json.loads(payload.decode('utf-8'), parse_constant=_no_constants, object_pairs_hook=_unique_keys)
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise EventContractError(f'message is not strict UTF-8 JSON: {type(error).__name__}') from None
    if not isinstance(value, dict) or set(value) != ENVELOPE_KEYS:
        raise EventContractError(f'message must be an object with exactly {sorted(ENVELOPE_KEYS)}')
    if not isinstance(value['type'], str):
        raise EventContractError('message type must be a string')
    if type(value['seq']) is int and value['seq'] > MAX_SEQ:
        raise EventContractError('message seq is out of range')
    return make_envelope(value['type'], value['call_id'], value['data'], seq=value['seq'], ts=value['ts'])


def _no_constants(name):
    raise ValueError(f'{name} is not allowed')


def _unique_keys(pairs):
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError('duplicate object key')
    return dict(pairs)


class RedisRelay:
    """Owned by one thread (the pump loop), never the API event loop. `connect()`
    returns a client the caller owns (closing it is the caller's job); configure it with
    `socket_timeout`, `socket_connect_timeout` and `health_check_interval`, otherwise a
    silently dropped connection is never noticed and a reconnect can exceed `timeout`."""

    def __init__(self, connect, hub, *, channel=CHANNEL):
        self._connect, self._hub, self.channel = connect, hub, channel
        self._pubsub = None
        self._last_seq = None
        self.stats = {'relayed': 0, 'invalid': 0, 'hub_errors': 0, 'gaps': 0, 'missing': 0, 'restarts': 0,
                      'connection_errors': 0}

    @property
    def subscribed(self):
        return self._pubsub is not None

    def close(self):
        pubsub, self._pubsub = self._pubsub, None
        if pubsub is not None:
            try:
                pubsub.close()
            except Exception:  # closing a broken connection may fail too; nothing to keep
                pass

    def pump(self, timeout=0.0, limit=256):
        """Relay up to `limit` messages, waiting at most `timeout` seconds for the first.
        Returns the number relayed. Never raises for Redis or message faults."""
        if type(limit) is not int or limit < 1:
            raise ValueError('limit must be a positive integer')
        relayed = 0
        try:
            if self._pubsub is None:
                self._pubsub = self._connect().pubsub(ignore_subscribe_messages=True)
                self._pubsub.subscribe(self.channel)  # on failure, close() releases it below
            wait = timeout
            for _ in range(limit):
                message = self._pubsub.get_message(timeout=wait)
                wait = 0.0
                if message is None:
                    break
                if message.get('type') != 'message':
                    continue
                relayed += self._relay(message.get('data'))
        except Exception:  # connection loss or client fault: resubscribe next time
            self.stats['connection_errors'] += 1
            self.close()
        return relayed

    def _relay(self, payload):
        try:
            envelope = parse_message(payload)
        except Exception:  # contract violations, and any parser fault: never a connection error
            self.stats['invalid'] += 1
            return 0
        if self._last_seq is not None:
            if envelope.seq > self._last_seq + 1:
                self.stats['gaps'] += 1
                self.stats['missing'] += envelope.seq - self._last_seq - 1
            elif envelope.seq <= self._last_seq:
                # The source hub restarted (its seq starts at 1): events before this one
                # since the restart are missing. Loss just before the restart is not
                # visible (no restart marker).
                self.stats['restarts'] += 1
                if envelope.seq > 1:
                    self.stats['gaps'] += 1
                    self.stats['missing'] += envelope.seq - 1
        self._last_seq = envelope.seq
        try:
            self._hub.publish(envelope.type, envelope.call_id, envelope.data, ts=envelope.ts)
        except Exception:  # EventContractError/EventHubError: counted, the relay keeps going
            self.stats['hub_errors'] += 1
            return 0
        self.stats['relayed'] += 1
        return 1
