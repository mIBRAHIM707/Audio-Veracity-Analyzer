"""Local analysis API (backlog 2.2; PRD FR-A1/FR-A2 subset; FR-A3/A6 history and FR-U7
accounts from PostgreSQL when configured, see below).

POST /v1/analyze  multipart field `file` (PCM16 WAV). Files up to 60 s return 200 with
                  the analysis; longer files (up to 10 minutes) return 202 with a job ID.
GET  /v1/jobs/{id} queued | running | done (with result) | failed (with error).
GET  /healthz     liveness and whether the detector has loaded.
GET  /readyz      FR-A8 readiness: the first call runs one warm-up inference; 200 once a
                  validated inference has succeeded (sticky), else 503 with the reason
                  (busy, detector failure, stopping or job worker gone).
GET  /metrics     FR-A8 Prometheus text format (no client library): requests by route
                  and status class, job states, queue depth/capacity, uploads in flight,
                  detector loaded, ready, analysis seconds (sum/count).
GET  /v1/calls[?status=all|active|ended]  FR-A3 call summaries (state, score trend,
                  alert counts) from the in-memory registry (`services/calls`).
GET  /v1/calls/{id} FR-A3 detail: alerts and the timeline (30 min full resolution,
                  older points in 10 s buckets).
WS   /v1/events[?call_id=]  FR-A4 envelopes as JSON text; a `stream.gap` control
                  message reports envelopes dropped for a slow client, and events the
                  Redis relay lost upstream (re-read REST).
GET  /v1/calls/{id}/history  FR-A3 full stored history of a call from PostgreSQL: every
                  session with its alerts and per-window scores (19 Nov).
GET  /v1/history/calls[?status=&limit=&before=]  stored sessions, newest first, paged.
GET  /v1/alerts[?state=&since=&until=&min_confidence=&limit=&cursor=&format=json|csv]
                  FR-A6 stored alerts, newest raised first, filtered and paged; `csv`
                  downloads the page (next cursor in `X-AVA-Next-Cursor`) (19 Nov).
POST /v1/auth/login, POST /v1/auth/logout, GET /v1/auth/me  PRD FR-U7 local accounts
                  (19 Nov, attempt 7), only when `create_app(..., auth=)` is given: then
                  every other /v1 route and the WebSocket need a valid session cookie,
                  and a CSV export is written to the audit log (see "Authentication").
POST /v1/alerts/{id}/feedback, GET /v1/alerts/{id}/feedback  PRD FR-A7 (19 Nov, attempt
                  8): label a stored alert true_positive/false_positive, stored with a
                  copy of its window scores and its audit row in one transaction; list
                  its labels. Needs `feedback=` (and so `auth=`); otherwise 503.
The calls and events routes need an `EventHub` passed to `create_app`; otherwise
they answer 503 / close with 1011. With a `RedisRelay` on that hub the scorer's events
arrive from Redis pub/sub (16 Nov). The history routes need a `PostgresCallHistory`
(`services/store/history.py`); otherwise they answer 503. So does `/v1/alerts`.

Errors: 411 no Content-Length, 413 oversize/too long, 422 undecodable/unsupported,
503 busy, too many concurrent uploads or job queue full, 500 detector failure.
Jobs live in a bounded in-memory store served by one worker thread: they are lost
on restart (durable jobs are backlog 5.2). Multipart parsing keeps uploads over
1 MB in a temporary file that is deleted when the request ends; the bytes are then
held in memory only while queued or analysed (architecture §2.5 batch path).

Authentication (FR-U7, architecture §2.5, §6; 19 Nov). Without `auth` the API behaves
as before (no accounts; the command line keeps it on loopback). With an
`services.auth.service.Authenticator`: login sets an `ava_session` cookie (HttpOnly,
SameSite=Strict, Secure unless `cookie_secure=False` for a plain-HTTP lab, Path=/,
lifetime the session's absolute limit). Every /v1 route except login answers 401
without a valid session, and the WebSocket refuses the handshake (403) and closes a
socket whose session has ended (1008) within `SESSION_RECHECK_S`. A POST/PUT/PATCH/
DELETE or WebSocket with an `Origin` (or `Sec-Fetch-Site: cross-site`) from another
host is refused 403 (CSRF/cross-site WebSocket defence on top of SameSite); this
compares with the `Host` header, so a reverse proxy must pass it through unchanged.
Every mutating request with a session gets an audit row after it is answered
(architecture §2.5 audit middleware; `api.analyze` for uploads; a row that cannot be
written is counted in `ava_audit_write_failures_total`). Both roles may use every
route that exists today; `admin` is for the settings routes that are not built yet.
/healthz, /readyz, /metrics, /openapi.json and the dashboard's static files stay open
(no call data; /metrics belongs on the internal network). A store failure answers 503,
never a pass; at most `MAX_ACCOUNT_CALLS` session checks and audit writes run at once
(more are refused busy), so a stalled database cannot take every worker thread.
"""
import asyncio
from collections import OrderedDict
from contextlib import asynccontextmanager
import json
import math
from pathlib import Path
import threading
import time
import uuid

from urllib.parse import urlsplit

from fastapi import FastAPI, File, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from services.calls.registry import CallRegistry, RegistryFeed
from services.events.fanout import HubFull
from services.events.relay_loop import RelayLoop
from services.api.alert_csv import alerts_csv
from services.auth.store import AuthStoreError
from services.retention.playback import SnippetUnavailable, check_alert_id
from services.store.feedback import LABELS as FEEDBACK_LABELS, check_feedback
from services.store.history import (DEFAULT_LIMIT as DEFAULT_HISTORY_LIMIT, MAX_LIMIT as MAX_HISTORY_LIMIT,
                                    REASONS as HISTORY_REASONS, STATUSES as HISTORY_STATUSES, HistoryError,
                                    check_alert_filters, valid_call_id)
from services.inference.analysis import (AnalysisError, AnalyzerBusy, MAX_ASYNC_SECONDS, MAX_BYTES,
                                         NeedsAsyncJob, decode_wav)
# FR-O6 live-pipeline families, shared with the live service process (moved 16 Nov).
from services.scorer.metrics_text import (PIPELINE_COUNTERS, PIPELINE_GAUGES, PIPELINE_INTAKE,  # noqa: F401
                                          PIPELINE_RESOLVED, pipeline_lines as _pipeline_lines,
                                          snapshot_valid_lines)

LATENCY_BUCKETS_S = (0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0)
# FR-O6 WebSocket delivery (18 Nov): finer at the low end. 0.2 s is the architecture §3
# "WS to browser render" budget; 0.3 s is the polling floor of this span (the scorer's
# 0.2 s Redis forwarder poll plus the API's 0.1 s socket poll), so readings up to about
# 0.3 s are expected by design and this is not a direct check of the 0.2 s row.
WS_DELIVERY_BUCKETS_S = (0.005, 0.01, 0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)
ANALYSIS_STAGES = ('decode', 'condition', 'inference')
# Mutually exclusive fates of an envelope the registry received (they sum to the
# envelopes applied or refused); sub-counts and housekeeping are exported separately.
REGISTRY_OUTCOMES = ('applied', 'duplicate_or_stale', 'after_end', 'refused_active_limit', 'apply_errors',
                     'ignored_system_health')
REGISTRY_SEPARATE = (('ava_call_registry_lost_events_total', 'lost_events',
                      'Published events the registry feed dropped before applying them.'),
                     ('ava_calls_idle_expired_total', 'idle_expired',
                      'Active calls ended by the registry to free a slot after the idle timeout.'))
RELAY_OUTCOMES = ('relayed', 'invalid', 'hub_errors', 'out_of_order')
RELAY_COUNTERS = (('ava_event_relay_missing_events_total', 'missing',
                   'Events the relay counted lost from gaps in the scorer sequence.'),
                  ('ava_event_relay_gaps_total', 'gaps', 'Sequence gaps the relay saw.'),
                  ('ava_event_relay_source_restarts_total', 'restarts', 'Scorer sequence restarts the relay saw.'),
                  ('ava_event_relay_connection_errors_total', 'connection_errors',
                   'Redis connection or subscription failures.'),
                  ('ava_event_relay_interruptions_total', 'interruptions',
                   'Redis outages (each one loses an unknown number of events).'),
                  ('ava_event_relay_loss_report_errors_total', 'loss_report_errors',
                   'Losses the relay could not report to WebSocket clients.'))
PUMP_INTERVAL_S = 0.2
WS_BUFFER, WS_BATCH, WS_POLL_S, WS_SEND_TIMEOUT_S = 256, 64, 0.1, 10.0
MAX_QUEUED_JOBS, MAX_KEPT_JOBS = 4, 64
CHUNK = 1 << 20
MULTIPART_OVERHEAD = 64 * 1024
MAX_CONCURRENT_UPLOADS = 2
JOB_STATES = ('queued', 'running', 'done', 'failed')
ROUTES = ('/healthz', '/readyz', '/metrics', '/v1/analyze', '/v1/jobs/:id', '/v1/calls',
          '/v1/calls/:id', '/v1/calls/:id/history', '/v1/history/calls', '/v1/alerts', '/v1/auth/login',
          '/v1/auth/logout', '/v1/auth/me', '/v1/alerts/:id/feedback', '/v1/alerts/:id/snippet')
# FR-U7 local accounts (19 Nov, attempt 7).
SESSION_COOKIE = 'ava_session'
LOGIN_OUTCOMES = ('ok', 'invalid', 'locked', 'busy', 'unavailable')  # as services.auth.service
AUTH_REFUSALS = ('no_session', 'cross_origin', 'store_unavailable')
UNSAFE_METHODS = frozenset({'POST', 'PUT', 'PATCH', 'DELETE'})
MAX_LOGIN_BYTES = 4096
SESSION_RECHECK_S = 60.0  # an open WebSocket re-checks its session this often
MAX_ACCOUNT_CALLS = 8  # session checks and audit writes in worker threads at once (review M1)
# Audit action per mutating route (architecture §2.5); any other mutating route is 'api.request'.
AUDITED_ROUTES = {'/v1/analyze': 'api.analyze', '/v1/alerts/:id/feedback': 'alerts.feedback'}
# Routes whose success writes its own audit row in the same transaction as the change
# (with what was changed): the middleware then records only their refusals and failures.
SELF_AUDITED_ROUTES = frozenset({'/v1/alerts/:id/feedback'})
# FR-A7 alert feedback (19 Nov, attempt 8): outcomes of POST /v1/alerts/{id}/feedback.
FEEDBACK_OUTCOMES = ('ok', 'invalid', 'not_found', 'conflict') + HISTORY_REASONS
MAX_FEEDBACK_BYTES = 1024
AUDIT_SLOT_WAIT_S = 2.0  # an audit write waits this long for an account slot before it is skipped/refused
# FR-U5 snippet playback (19 Nov, attempt 10): mutually exclusive outcomes of
# GET /v1/alerts/{id}/snippet; `unaudited`: decrypted but not sent (no audit row).
SNIPPET_OUTCOMES = ('ok', 'invalid', 'missing', 'expired', 'unreadable', 'error', 'busy', 'unaudited')
MAX_SNIPPET_READS = 2  # decrypting and encoding up to 8 MB each; more at once are refused busy
SNIPPET_STATUS = {'missing': (404, 'No snippet is kept for this alert'),
                  'expired': (410, 'The snippet for this alert has expired'),
                  'unreadable': (500, 'The stored snippet could not be authenticated or read'),
                  'error': (503, 'The snippet store could not be read (error)')}


class LoginBusy:  # stands in for an Authenticator result when no account slot is free
    outcome, token, principal = 'busy', None, None
STATUS_CLASSES = ('2xx', '3xx', '4xx', '5xx')
HTTP_METHODS = frozenset({'GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'})  # others logged as 'other'
# Mutually exclusive outcomes of a PostgreSQL history read (19 Nov, FR-A3).
HISTORY_OUTCOMES = ('ok', 'not_found') + HISTORY_REASONS
ALERT_READ_OUTCOMES = ('ok',) + HISTORY_REASONS  # an alert search always has a page, maybe empty
MAX_HISTORY_REQUESTS = 4  # well below the 40 worker threads Starlette's thread pool has
# FR-A6 alert search page sizes (19 Nov, attempt 5); the store allows up to 10,000.
DEFAULT_ALERT_LIMIT, MAX_ALERT_JSON_LIMIT = 100, 1_000
DEFAULT_ALERT_CSV_LIMIT, MAX_ALERT_CSV_LIMIT = 1_000, 10_000


def route_label(path):
    """Fixed route labels keep metric cardinality bounded (job/call IDs are never labels)."""
    if path.startswith('/v1/jobs/'):
        return '/v1/jobs/:id'
    if path.startswith('/v1/calls/'):
        rest = path[len('/v1/calls/'):]
        return '/v1/calls/:id/history' if rest.endswith('/history') else '/v1/calls/:id'
    for tail in ('/feedback', '/snippet'):
        if path.startswith('/v1/alerts/') and path.endswith(tail):
            alert_id = path[len('/v1/alerts/'):-len(tail)]
            if alert_id and '/' not in alert_id:  # what the route matches (review N1)
                return f'/v1/alerts/:id{tail}'
    return path if path in ROUTES else 'other'


class Histogram:
    """Cumulative Prometheus histogram with fixed buckets; one series per label value
    from a fixed list (bounded cardinality). Not locked: `Metrics` holds its lock."""

    def __init__(self, labels=(None,), buckets=LATENCY_BUCKETS_S):
        self.bounds = tuple(buckets)
        self.series = {label: {'buckets': [0] * len(self.bounds), 'sum': 0.0, 'count': 0}
                       for label in labels}

    def observe(self, value, label=None):
        if label not in self.series or isinstance(value, bool) or not isinstance(value, (int, float)) \
                or not math.isfinite(value) or value < 0:
            return False
        series = self.series[label]
        for i, bound in enumerate(self.bounds):
            if value <= bound:
                series['buckets'][i] += 1
        series['sum'] += value
        series['count'] += 1
        return True

    def render(self, name, label_name=None):
        lines = []
        for label, series in self.series.items():
            base = '' if label is None else f'{label_name}="{label}",'
            plain = '' if label is None else f'{{{label_name}="{label}"}}'
            lines += [f'{name}_bucket{{{base}le="{bound!r}"}} {n}'
                      for bound, n in zip(self.bounds, series['buckets'])]
            lines += [f'{name}_bucket{{{base}le="+Inf"}} {series["count"]}',
                      f'{name}_sum{plain} {series["sum"]!r}', f'{name}_count{plain} {series["count"]}']
        return lines




class Metrics:
    """Thread-safe counters rendered in Prometheus text exposition format 0.0.4."""

    def __init__(self):
        self._lock = threading.Lock()
        self.requests = {(route, cls): 0 for route in ROUTES + ('other',) for cls in STATUS_CLASSES}
        self.analysis = Histogram()
        self.stages = Histogram(ANALYSIS_STAGES)
        self.ws_delivery = Histogram(buckets=WS_DELIVERY_BUCKETS_S)
        self.ws_unmeasured = 0
        self.ws_slow_closes = 0
        self.history = {outcome: 0 for outcome in HISTORY_OUTCOMES}
        self.alert_reads = {outcome: 0 for outcome in ALERT_READ_OUTCOMES}
        self.logins = {outcome: 0 for outcome in LOGIN_OUTCOMES}
        self.auth_refusals = {reason: 0 for reason in AUTH_REFUSALS}
        self.audit_failures = 0
        self.feedback = {outcome: 0 for outcome in FEEDBACK_OUTCOMES}
        self.snippets = {outcome: 0 for outcome in SNIPPET_OUTCOMES}

    def count_snippet(self, outcome):
        with self._lock:
            if outcome in self.snippets:
                self.snippets[outcome] += 1

    def count_feedback(self, outcome):
        with self._lock:
            if outcome in self.feedback:
                self.feedback[outcome] += 1

    def count_audit_failure(self):
        with self._lock:
            self.audit_failures += 1

    def count_login(self, outcome):
        with self._lock:
            if outcome in self.logins:
                self.logins[outcome] += 1

    def count_auth_refusal(self, reason):
        with self._lock:
            if reason in self.auth_refusals:
                self.auth_refusals[reason] += 1

    def count_history(self, outcome, kind='history'):
        counts = self.alert_reads if kind == 'alerts' else self.history
        with self._lock:
            if outcome in counts:
                counts[outcome] += 1

    def observe_ws_delivery(self, sent_at, event_ts):
        """One FR-A4 envelope finished sending on a WebSocket at wall-clock `sent_at`
        (None if the clock failed); `event_ts` is its `ts`. A negative, non-finite or
        missing difference (another process's clock ahead of ours, a clock fault) is
        counted as unmeasured, never observed."""
        delay = None
        if all(type(v) in (int, float) for v in (sent_at, event_ts)):
            delay = sent_at - event_ts
        with self._lock:
            if not self.ws_delivery.observe(delay):
                self.ws_unmeasured += 1

    def count_ws_slow_close(self):
        with self._lock:
            self.ws_slow_closes += 1

    def count_request(self, route, status):
        cls = f'{status // 100}xx'
        with self._lock:
            if (route, cls) in self.requests:
                self.requests[(route, cls)] += 1

    def observe_analysis(self, result):
        """Record a completed analysis: total processing time and FR-O6 stage times."""
        if not isinstance(result, dict):
            return
        stages = result.get('stage_seconds')
        with self._lock:
            self.analysis.observe(result.get('processing_seconds'))
            if isinstance(stages, dict):
                for stage in ANALYSIS_STAGES:
                    self.stages.observe(stages.get(stage), stage)

    def render(self, jobs, service, uploads_in_flight, live=None, pipeline=None, relay=None, history=False,
               auth=False, snippets=False):
        stats = jobs.stats()
        lines = ['# HELP ava_api_requests_total HTTP responses by route and status class.',
                 '# TYPE ava_api_requests_total counter']
        with self._lock:
            history_reads = dict(self.history)
            alert_reads = dict(self.alert_reads)
            logins, auth_refusals, audit_failures = dict(self.logins), dict(self.auth_refusals), self.audit_failures
            feedback = dict(self.feedback)
            snippet_reads = dict(self.snippets)
            requests = dict(self.requests)
            analysis = self.analysis.render('ava_analysis_seconds')
            stages = self.stages.render('ava_analysis_stage_seconds', 'stage')
            ws_delivery = self.ws_delivery.render('ava_ws_delivery_seconds')
            ws_unmeasured, ws_slow_closes = self.ws_unmeasured, self.ws_slow_closes
        lines += [f'ava_api_requests_total{{route="{route}",status="{cls}"}} {value}'
                  for (route, cls), value in sorted(requests.items())]
        lines += ['# HELP ava_jobs Jobs currently held in memory by state.', '# TYPE ava_jobs gauge']
        lines += [f'ava_jobs{{state="{state}"}} {stats["by_status"][state]}' for state in JOB_STATES]
        gauges = [('ava_job_queue_depth', 'Jobs waiting for the worker.', stats['queued']),
                  ('ava_job_queue_capacity', 'Maximum queued jobs.', stats['capacity']),
                  ('ava_uploads_in_flight', 'Uploads being received or analysed.', uploads_in_flight),
                  ('ava_detector_loaded', '1 when the detector is loaded.', int(service.detector_loaded)),
                  ('ava_ready', '1 after a successful warm-up inference.', int(service.ready))]
        if live is not None:
            gauges += [('ava_calls_active', 'Calls the call registry holds as active.', live['active']),
                       ('ava_calls_stale', 'Active calls with no event for the idle timeout.', live['stale']),
                       ('ava_alerts_open', 'Open alerts on active calls.', live['alerts_open']),
                       ('ava_event_subscribers', 'Event hub subscribers (registry feed and WebSockets).',
                        live['subscribers'])]
        for name, text, value in gauges:
            lines += [f'# HELP {name} {text}', f'# TYPE {name} gauge', f'{name} {value}']
        if live is not None:
            lines += ['# HELP ava_call_registry_events_total Envelopes received by the call registry, by '
                      'mutually exclusive outcome.', '# TYPE ava_call_registry_events_total counter']
            lines += [f'ava_call_registry_events_total{{outcome="{name}"}} {live["counters"][name]}'
                      for name in REGISTRY_OUTCOMES]
            for name, key, text in REGISTRY_SEPARATE:
                lines += [f'# HELP {name} {text}', f'# TYPE {name} counter', f'{name} {live["counters"][key]}']
            lines += ['# HELP ava_alerts_raised_total Alerts raised on calls seen by the registry.',
                      '# TYPE ava_alerts_raised_total counter',
                      f'ava_alerts_raised_total {live["counters"]["alerts_raised"]}']
            lines += ['# HELP ava_ws_delivery_seconds Time from an event\'s ts (its publish time in the scorer, '
                      'kept across the Redis relay) until this process handed it to the WebSocket transport (FR-O6). '
                      'Includes the scorer\'s 0.2 s Redis forwarder poll and the API\'s 0.1 s socket poll. '
                      'One observation per socket send (an event sent to N sockets counts N times). Excludes the '
                      'network and browser render; compares two processes\' clocks, so valid on one host only.',
                      '# TYPE ava_ws_delivery_seconds histogram', *ws_delivery,
                      '# HELP ava_ws_delivery_unmeasured_total Events sent on a WebSocket whose delivery time '
                      'could not be measured (sender clock ahead of this one, or a clock fault).',
                      '# TYPE ava_ws_delivery_unmeasured_total counter',
                      f'ava_ws_delivery_unmeasured_total {ws_unmeasured}',
                      f'# HELP ava_ws_slow_client_closes_total WebSocket clients closed because a send did not '
                      f'complete within {WS_SEND_TIMEOUT_S:g} s.',
                      '# TYPE ava_ws_slow_client_closes_total counter',
                      f'ava_ws_slow_client_closes_total {ws_slow_closes}']
        if relay is not None:
            lines += relay_lines(relay)
        if history:
            lines += ['# HELP ava_call_history_reads_total PostgreSQL call history reads by mutually exclusive '
                      'outcome (unavailable: no or lost connection; schema: database not migrated).',
                      '# TYPE ava_call_history_reads_total counter']
            lines += [f'ava_call_history_reads_total{{outcome="{outcome}"}} {history_reads[outcome]}'
                      for outcome in HISTORY_OUTCOMES]
            lines += ['# HELP ava_alert_search_reads_total PostgreSQL alert searches (GET /v1/alerts, FR-A6) by '
                      'mutually exclusive outcome; they share the history connection.',
                      '# TYPE ava_alert_search_reads_total counter']
            lines += [f'ava_alert_search_reads_total{{outcome="{outcome}"}} {alert_reads[outcome]}'
                      for outcome in ALERT_READ_OUTCOMES]
        if auth:
            lines += ['# HELP ava_auth_logins_total Login attempts by mutually exclusive outcome (FR-U7; invalid '
                      'covers unknown names, wrong passwords and disabled users alike).',
                      '# TYPE ava_auth_logins_total counter']
            lines += [f'ava_auth_logins_total{{outcome="{outcome}"}} {logins[outcome]}' for outcome in LOGIN_OUTCOMES]
            lines += ['# HELP ava_auth_refused_requests_total Requests and WebSocket handshakes refused before a '
                      'route ran, by reason.', '# TYPE ava_auth_refused_requests_total counter']
            lines += [f'ava_auth_refused_requests_total{{reason="{reason}"}} {auth_refusals[reason]}'
                      for reason in AUTH_REFUSALS]
            lines += ['# HELP ava_audit_write_failures_total Mutating requests answered whose audit row could not '
                      'be written (the account store failed or was busy).',
                      '# TYPE ava_audit_write_failures_total counter', f'ava_audit_write_failures_total {audit_failures}']
            lines += ['# HELP ava_alert_feedback_writes_total Alert feedback submissions (POST /v1/alerts/{id}/feedback, '
                      'FR-A7) by mutually exclusive outcome; ok rows are stored with their audit row.',
                      '# TYPE ava_alert_feedback_writes_total counter']
            lines += [f'ava_alert_feedback_writes_total{{outcome="{outcome}"}} {feedback[outcome]}'
                      for outcome in FEEDBACK_OUTCOMES]
        if snippets:
            lines += ['# HELP ava_snippet_reads_total Retained alert snippet playbacks (GET /v1/alerts/{id}/snippet, '
                      'FR-U5) by mutually exclusive outcome; ok ones are sent after their audit row.',
                      '# TYPE ava_snippet_reads_total counter']
            lines += [f'ava_snippet_reads_total{{outcome="{outcome}"}} {snippet_reads[outcome]}'
                      for outcome in SNIPPET_OUTCOMES]
        if pipeline is not None:
            try:
                snapshot, age = pipeline.read()
                extra = [] if snapshot is None else _pipeline_lines(snapshot, age)
            except Exception:  # a bad snapshot or reader must not break /metrics
                extra = []
            lines += extra + snapshot_valid_lines(extra)
        lines += ['# HELP ava_analysis_seconds Processing time of completed analyses (excludes model load).',
                  '# TYPE ava_analysis_seconds histogram', *analysis,
                  '# HELP ava_analysis_stage_seconds Per-stage time of completed analyses (FR-O6).',
                  '# TYPE ava_analysis_stage_seconds histogram', *stages]
        return '\n'.join(lines) + '\n'


class JobStore:
    """Bounded in-memory jobs processed in order by one daemon worker thread."""

    def __init__(self, service, max_queued=MAX_QUEUED_JOBS, max_kept=MAX_KEPT_JOBS, on_result=None, log=None):
        """`log` (optional `ApiLog`) gets job_queued/job_done/job_failed with the job ID
        and, for failures, the HTTP status only; its faults never reach a job."""
        self._service, self._max_queued, self._max_kept = service, max_queued, max_kept
        self._on_result = on_result or (lambda result: None)
        self._log = log
        self._jobs, self._pending, self._closed = OrderedDict(), [], False
        self._condition = threading.Condition()
        self._worker = threading.Thread(target=self._run, name='ava-analysis-jobs', daemon=True)
        self._worker.start()

    def close(self, timeout=5):
        """Stop accepting work, drop queued uploads and let the worker exit after its current job."""
        with self._condition:
            self._closed = True
            dropped = [job_id for job_id, _ in self._pending]
            for job_id in dropped:
                self._jobs[job_id].update(status='failed', error={'status': 503, 'detail': 'Service stopped'})
            self._pending.clear()
            self._condition.notify_all()
        for job_id in dropped:
            self._emit('job_failed', 'warning', job_id=job_id, status=503)
        self._worker.join(timeout)

    def submit(self, payload):
        with self._condition:
            if self._closed:
                raise HTTPException(503, 'Service is stopping')
            if len(self._pending) >= self._max_queued:
                raise HTTPException(503, 'Job queue is full; retry later')
            job_id = uuid.uuid4().hex
            self._jobs[job_id] = {'job_id': job_id, 'status': 'queued'}
            self._pending.append((job_id, payload))
            while len(self._jobs) > self._max_kept:  # forget the oldest finished jobs
                oldest = next((k for k, v in self._jobs.items() if v['status'] in ('done', 'failed')), None)
                if oldest is None:
                    break
                del self._jobs[oldest]
            # Under the lock (re-review 18 Nov, low 3), so the worker's job_done cannot be
            # queued first; `emit` never blocks and takes no lock of ours.
            self._emit('job_queued', 'info', job_id=job_id)
            self._condition.notify()
        return job_id

    def _emit(self, event, level, **fields):
        if self._log is None:
            return
        try:
            self._log.emit(event, level, **fields)
        except Exception:  # advisory
            pass

    def get(self, job_id):
        with self._condition:
            job = self._jobs.get(job_id)
            return dict(job) if job else None

    def stats(self):
        """Counts by status, queue depth/capacity and whether the worker can still run jobs."""
        with self._condition:
            counts = {status: 0 for status in JOB_STATES}
            for job in self._jobs.values():
                counts[job['status']] += 1
            return {'by_status': counts, 'queued': len(self._pending), 'capacity': self._max_queued,
                    'accepting': not self._closed and self._worker.is_alive()}

    def _run(self):
        while True:
            with self._condition:
                while not self._pending and not self._closed:
                    self._condition.wait()
                if self._closed:
                    return
                job_id, payload = self._pending.pop(0)
                self._jobs[job_id]['status'] = 'running'
            while True:
                try:
                    result = self._service.analyze_wav(payload, max_seconds=MAX_ASYNC_SECONDS)
                    update = {'status': 'done', 'result': result}
                    self._on_result(result)
                except AnalysisError as error:
                    if error.status == 503 and not self._closed:  # a sync request holds the analyser
                        threading.Event().wait(0.2)
                        continue
                    update = {'status': 'failed', 'error': {'status': error.status, 'detail': str(error)}}
                except Exception as error:  # the worker must survive any fault
                    update = {'status': 'failed', 'error': {'status': 500, 'detail': type(error).__name__}}
                break
            payload = None
            with self._condition:
                self._jobs[job_id].update(update)
            if update['status'] == 'done':
                self._emit('job_done', 'info', job_id=job_id)
            else:
                self._emit('job_failed', 'warning', job_id=job_id, status=update['error']['status'])


def gap_message(batch):
    """`stream.gap` control text for a `Drained` batch, or None. `dropped` is always
    present (this client's own buffer overflowed); `upstream_missing` (events counted
    lost before they reached this process) and `upstream_interruptions` (losses of
    unknown size: the Redis relay lost its connection or the scorer restarted) only
    when nonzero."""
    if not (batch.dropped or batch.upstream_missing or batch.upstream_interruptions):
        return None
    message = {'type': 'stream.gap', 'dropped': batch.dropped}
    if batch.upstream_missing:
        message['upstream_missing'] = batch.upstream_missing
    if batch.upstream_interruptions:
        message['upstream_interruptions'] = batch.upstream_interruptions
    return json.dumps(message)


def relay_lines(snapshot):
    """FR-O6 lines for the Redis relay thread (`RelayLoop.snapshot()` plus the relay's
    counters). No channel names, URLs or call IDs."""
    stats = snapshot['relay']
    lines = ['# HELP ava_event_relay_messages_total Redis pub/sub messages the relay handled, by outcome.',
             '# TYPE ava_event_relay_messages_total counter']
    lines += [f'ava_event_relay_messages_total{{outcome="{outcome}"}} {stats.get(outcome, 0)}'
              for outcome in RELAY_OUTCOMES]
    for name, key, text in RELAY_COUNTERS:
        lines += [f'# HELP {name} {text}', f'# TYPE {name} counter', f'{name} {stats.get(key, 0)}']
    for name, value, text in (('ava_event_relay_subscribed', int(snapshot['subscribed']),
                               '1 while the relay holds a Redis subscription.'),
                              ('ava_event_relay_running', int(snapshot['alive']),
                               '1 while the relay thread runs.')):
        lines += [f'# HELP {name} {text}', f'# TYPE {name} gauge', f'{name} {value}']
    lines += ['# HELP ava_event_relay_thread_faults_total Exceptions raised out of a relay pump.',
              '# TYPE ava_event_relay_thread_faults_total counter',
              f'ava_event_relay_thread_faults_total {snapshot["faults"]}']
    return lines


def same_origin(headers):
    """False for a browser request from another site: `Sec-Fetch-Site: cross-site`, or an
    `Origin` whose host:port is not this request's `Host` (scheme ignored, so a TLS proxy
    in front does not matter). A request with no `Origin` (not a browser, or a browser
    GET) passes: SameSite=Strict keeps the cookie off cross-site requests anyway."""
    if headers.get('sec-fetch-site', '').lower() == 'cross-site':
        return False
    origin = headers.get('origin')
    if origin is None:
        return True
    host = headers.get('host')
    if not host or origin == 'null':
        return False
    try:
        netloc = urlsplit(origin).netloc
    except ValueError:
        return False
    return bool(netloc) and netloc.lower() == host.lower()


async def read_small_body(request, limit):
    """The request body, or None if it is larger than `limit` bytes (chunked bodies too)."""
    length = request.headers.get('content-length')
    if length is not None and (not length.isdigit() or int(length) > limit):
        return None
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            return None
    return bytes(body)


class _SlowClient(Exception):
    """A WebSocket send did not complete within `WS_SEND_TIMEOUT_S`."""


async def _send_or_slow(websocket, text):
    """Send `text`; a client that stops reading must not hold the server, so give up
    after the timeout with `_SlowClient`. Only this timeout is a slow client: a
    transport error from the send (even `OSError(ETIMEDOUT)`, a `TimeoutError` on
    Python 3.11) is re-raised as itself (review L1, 18 Nov)."""
    send = asyncio.ensure_future(websocket.send_text(text))
    try:
        done, _ = await asyncio.wait({send}, timeout=WS_SEND_TIMEOUT_S)
    except asyncio.CancelledError:  # the handler itself was cancelled: do not orphan the send
        send.cancel()
        raise
    if not done:
        send.cancel()
        await asyncio.gather(send, return_exceptions=True)
        raise _SlowClient
    send.result()


async def _close_quietly(websocket, code, reason):
    """Close without waiting on a stuck client for longer than the send timeout."""
    try:
        await asyncio.wait_for(websocket.close(code=code, reason=reason), WS_SEND_TIMEOUT_S)
    except (asyncio.TimeoutError, WebSocketDisconnect, OSError, RuntimeError):  # already closing
        pass


async def read_bounded(upload):
    data = bytearray()
    while chunk := await upload.read(CHUNK):
        data.extend(chunk)
        if len(data) > MAX_BYTES:
            raise HTTPException(413, 'File exceeds 100 MB')
    return bytes(data)


def create_app(service, *, hub=None, registry=None, dashboard_dir=None, pipeline_stats=None, relay=None,
               oplog=None, wall_clock=time.time, history=None, auth=None, cookie_secure=True, feedback=None,
               snippets=None):
    """`hub` is the in-process `services.events.EventHub` carrying FR-A4 events; with it
    the app serves `/v1/calls` from a `CallRegistry` (a new one unless given) and
    `WS /v1/events`. Without it those routes answer 503: no live source is connected.
    `relay` is a `services.events.redis_bridge.RedisRelay` on that same hub: while the
    app runs (lifespan) a `RelayLoop` thread moves the scorer's events from Redis into
    the hub, and the relay's loss reports reach WebSocket clients as `stream.gap`.
    `pipeline_stats` is the `services.scorer.pipeline.PipelineStats` the live loop
    publishes to; `/metrics` then adds the `ava_live_*` window/queue/stride families.
    `dashboard_dir` is the built dashboard (`dashboard/dist`, architecture §2.6): served
    at `/` on the same origin, after every API route so it never shadows one.
    `oplog` (optional, `services/api/oplog.ApiLog`, FR-O6) gets request, job,
    detector-ready and relay-loss lines; its faults never reach a request.
    `wall_clock` (seconds since the epoch, the clock of envelope `ts`) times each event's
    WebSocket delivery for `ava_ws_delivery_seconds`; a fault in it is counted, never raised.
    `history` (optional, `services.store.history.PostgresCallHistory`, FR-A3 "full history
    in PostgreSQL") serves `GET /v1/calls/{id}/history` and `GET /v1/history/calls`;
    without it they answer 503. It works without a hub: history needs no live source.
    The app does not close it (its owner does).
    `auth` (optional, `services.auth.service.Authenticator`, FR-U7) turns on accounts:
    see "Authentication" in the module docstring. `cookie_secure=False` drops the
    cookie's Secure flag (plain HTTP on a non-loopback lab address only).
    `feedback` (optional, `services.store.feedback.AlertFeedbackStore`, FR-A7) serves
    `POST`/`GET /v1/alerts/{id}/feedback`; it needs `auth` (every label names its
    analyst). Without it those routes answer 503.
    `snippets` (optional, `services.retention.playback.SnippetReader`, FR-U5) serves
    `GET /v1/alerts/{id}/snippet`, the alert's retained audio as WAV; it needs `auth`
    (every playback is an audited audio export). Without it the route answers 503."""
    if feedback is not None and auth is None:
        raise ValueError('alert feedback needs local accounts: every label names who gave it')
    if snippets is not None and auth is None:
        raise ValueError('snippet playback needs local accounts: every playback is audited with its listener')
    if registry is not None and hub is None:
        raise ValueError('a call registry needs the event hub that feeds it')
    if relay is not None and (hub is None or getattr(relay, 'hub', None) is not hub):
        raise ValueError('an event relay must publish into the app\'s event hub')
    if dashboard_dir is not None and not (Path(dashboard_dir) / 'index.html').is_file():
        raise ValueError('dashboard_dir must be a built dashboard containing index.html')
    metrics = Metrics()
    jobs = JobStore(service, on_result=metrics.observe_analysis, log=oplog)
    in_flight = [0]
    registry = None if hub is None else (registry or CallRegistry())
    feeds = []  # the live feed; subscribed only while the app runs (lifespan)
    relay_loops = []  # likewise the Redis relay thread
    pump_errors = [0]

    def pump_once():
        try:
            feeds[0].pump()
        except Exception:  # the read model must not take the API down; counted, retried
            pump_errors[0] += 1
        if oplog is not None and relay is not None:
            try:
                oplog.relay_stats(dict(relay.stats))
            except Exception:
                pass

    async def pump_forever():
        while True:
            await run_in_threadpool(pump_once)  # registry work stays off the event loop
            await asyncio.sleep(PUMP_INTERVAL_S)

    @asynccontextmanager
    async def lifespan(_app):
        pump_task = None
        if hub is not None:
            feeds.append(RegistryFeed(registry, hub))
            pump_task = asyncio.create_task(pump_forever())
        try:
            if relay_loops and not relay_loops[-1].alive:
                relay_loops.pop()  # a loop left stuck by an earlier run has since ended
            # After the registry subscribed, so it sees the first relayed event. The relay
            # has one owner: never a second thread while an earlier one is stuck in it.
            if relay is not None and not relay_loops:
                loop = RelayLoop(relay)
                relay_loops.append(loop)
                loop.start()
            yield
        finally:
            # Off the event loop; the join waits for one pump at most. A stuck loop stays
            # listed (and visible in /metrics) so a later run does not start another.
            if relay_loops and await run_in_threadpool(relay_loops[-1].stop):
                relay_loops.pop()
            if pump_task is not None:
                pump_task.cancel()
                try:
                    await pump_task
                except asyncio.CancelledError:
                    pass
                feeds.pop().close()
            await run_in_threadpool(jobs.close)

    # Swagger/ReDoc pages load CDN assets, so they stay off; /openapi.json is served locally.
    app = FastAPI(title='AVA local analysis API', version='0.1.0', docs_url=None, redoc_url=None,
                  lifespan=lifespan)
    uploads = asyncio.Semaphore(MAX_CONCURRENT_UPLOADS)
    app.state.jobs = jobs

    app.state.metrics = metrics
    app.state.calls_registry = registry

    @app.get('/healthz')
    def health():
        return {'status': 'ok', 'detector_loaded': service.detector_loaded, 'model_id': service.model_id}

    @app.get('/readyz')
    async def ready():
        if not jobs.stats()['accepting']:
            return JSONResponse({'status': 'not_ready', 'reason': 'job worker stopped'}, status_code=503)
        try:
            seconds = await run_in_threadpool(service.warm_up)
        except AnalysisError as error:
            reason = 'busy' if isinstance(error, AnalyzerBusy) else str(error)
            return JSONResponse({'status': 'not_ready', 'reason': reason}, status_code=503)
        if oplog is not None:
            try:
                oplog.detector_ready(service.model_id, seconds)
            except Exception:
                pass
        return {'status': 'ready', 'model_id': service.model_id, 'warmup_seconds': seconds}

    @app.get('/metrics')
    def prometheus():
        live = None
        if feeds:
            pump_once()
            live = {**registry.live_stats(), 'subscribers': hub.subscriber_count}
        relay_snapshot = None
        if relay_loops:
            relay_snapshot = {**relay_loops[-1].snapshot(), 'relay': dict(relay.stats)}
        return PlainTextResponse(metrics.render(jobs, service, in_flight[0], live, pipeline_stats, relay_snapshot,
                                                history=history is not None, auth=auth is not None,
                                                snippets=snippets is not None),
                                 media_type='text/plain; version=0.0.4; charset=utf-8')

    @app.middleware('http')
    async def reject_oversize(request, call_next):
        # Refuse before multipart parsing spools the body; chunked bodies are bounded on read.
        if request.url.path == '/v1/analyze' and request.method == 'POST':
            length = request.headers.get('content-length')
            if length is None:
                return JSONResponse({'detail': 'Content-Length is required'}, status_code=411)
            if not length.isdigit() or int(length) > MAX_BYTES + MULTIPART_OVERHEAD:
                return JSONResponse({'detail': 'File exceeds 100 MB'}, status_code=413)
            if uploads.locked():
                return JSONResponse({'detail': 'Too many concurrent uploads; retry later'}, status_code=503)
            async with uploads:
                in_flight[0] += 1
                try:
                    return await call_next(request)
                finally:
                    in_flight[0] -= 1
        return await call_next(request)

    # At most this many session checks and audit writes use worker threads at once
    # (review M1): the rest are refused busy at once, so a stalled database cannot fill
    # the thread pool that /healthz, /readyz and /metrics need.
    account_slots = asyncio.Semaphore(MAX_ACCOUNT_CALLS)

    async def account_call(function, *args, wait_s=0.0):
        """Run one account-store call off the event loop. AuthStoreError('busy') when every
        slot is taken (after waiting up to `wait_s` for one: audit writes wait, so a
        moment of dashboard load does not skip a row; re-review L)."""
        if account_slots.locked() and wait_s <= 0:
            raise AuthStoreError('too many account store calls', reason='busy')
        try:
            await asyncio.wait_for(account_slots.acquire(), wait_s if wait_s > 0 else None)
        except asyncio.TimeoutError:
            raise AuthStoreError('too many account store calls', reason='busy') from None
        try:
            return await run_in_threadpool(function, *args)
        finally:
            account_slots.release()

    async def resolve_session(token):
        """(Principal or None, None) or (None, response) when the account store failed."""
        try:
            return await account_call(auth.store.resolve, token), None
        except AuthStoreError as error:
            metrics.count_auth_refusal('store_unavailable')
            return None, JSONResponse({'detail': f'Account store unavailable ({error.reason})'}, status_code=503)

    async def audit_request(request, principal, status):
        """Architecture §2.5 "audit middleware writes every mutating request": one row after
        the route answered (actor, route, method, status; never a body). Login, logout and
        exports write their own rows. A row that cannot be written is counted
        (`ava_audit_write_failures_total`); the answer already stands."""
        route = route_label(request.url.path)
        outcome = 'ok' if status < 400 else 'refused' if status < 500 else 'failed'
        detail = {'route': route, 'method': request.method, 'status': status}
        try:
            await account_call(auth.store.audit, principal.username, AUDITED_ROUTES.get(route, 'api.request'),
                               outcome, detail, wait_s=AUDIT_SLOT_WAIT_S)
        except (AuthStoreError, ValueError):
            metrics.count_audit_failure()

    if auth is not None:
        # FR-U7: outside reject_oversize, so an upload without a session is refused before
        # its body is read; inside count_responses, so refusals are counted and logged.
        # Registered only with accounts on: without them the middleware stack is unchanged.
        @app.middleware('http')
        async def require_session(request, call_next):
            if not request.url.path.startswith('/v1/'):
                return await call_next(request)
            if request.method in UNSAFE_METHODS and not same_origin(request.headers):
                metrics.count_auth_refusal('cross_origin')
                return JSONResponse({'detail': 'Cross-origin request refused'}, status_code=403)
            if request.url.path == '/v1/auth/login':
                return await call_next(request)
            principal, failed = await resolve_session(request.cookies.get(SESSION_COOKIE))
            if failed is not None:
                return failed
            if principal is None:
                metrics.count_auth_refusal('no_session')
                return JSONResponse({'detail': 'Login required'}, status_code=401)
            request.state.principal = principal
            response = await call_next(request)
            if request.method in UNSAFE_METHODS and request.url.path != '/v1/auth/logout' \
                    and not (route_label(request.url.path) in SELF_AUDITED_ROUTES and response.status_code < 400):
                await audit_request(request, principal, response.status_code)
            return response

    # Registered last, so it is the outermost middleware and also counts early refusals.
    @app.middleware('http')
    async def count_responses(request, call_next):
        started = time.perf_counter()
        route = route_label(request.url.path)
        try:
            response = await call_next(request)
        except Exception:  # unhandled: Starlette's outer error handler answers 500
            metrics.count_request(route, 500)
            log_request(route, request.method, 500, started)
            raise
        metrics.count_request(route, response.status_code)
        log_request(route, request.method, response.status_code, started)
        return response

    def log_request(route, method, status, started):
        if oplog is None:
            return
        try:  # the operations log is advisory: never fails a request
            # A streamed body (the dashboard's static files) may still be sending; the
            # duration is to the response start.
            oplog.request(route, method if method in HTTP_METHODS else 'other', status,
                          time.perf_counter() - started)
        except Exception:
            pass

    @app.post('/v1/analyze')
    async def analyze(file: UploadFile = File(...)):
        payload = await read_bounded(file)
        try:
            # Route by length before touching the analyser, so long files queue even while busy.
            try:
                await run_in_threadpool(decode_wav, payload)
            except NeedsAsyncJob:
                await run_in_threadpool(decode_wav, payload, MAX_ASYNC_SECONDS)  # 413/422 now, not as a failed job
                job_id = jobs.submit(payload)
                return JSONResponse({'job_id': job_id, 'status': 'queued', 'poll': f'/v1/jobs/{job_id}'},
                                    status_code=202)
            result = await run_in_threadpool(service.analyze_wav, payload)
            metrics.observe_analysis(result)
            return result
        except AnalysisError as error:
            raise HTTPException(error.status, str(error)) from None

    @app.get('/v1/jobs/{job_id}')
    def job(job_id: str):
        record = jobs.get(job_id)
        if record is None:
            raise HTTPException(404, 'Unknown job')
        return record

    if auth is not None:
        @app.post('/v1/auth/login')
        async def login(request: Request):
            body = await read_small_body(request, MAX_LOGIN_BYTES)
            if body is None:
                raise HTTPException(413, f'Login body exceeds {MAX_LOGIN_BYTES} bytes')
            try:
                fields = json.loads(body)
            except (ValueError, UnicodeDecodeError):
                fields = None
            if not isinstance(fields, dict) or set(fields) != {'username', 'password'} \
                    or not all(isinstance(fields[k], str) for k in fields):
                raise HTTPException(422, 'Send a JSON object with text fields username and password only')
            try:  # within the account slots too (re-review L): it reads and writes the store
                result = await account_call(auth.login, fields['username'], fields['password'])
            except AuthStoreError:
                result = LoginBusy
            metrics.count_login(result.outcome)
            if result.outcome == 'ok':
                principal = result.principal
                response = JSONResponse({'username': principal.username, 'role': principal.role,
                                         'expires_ts': principal.expires_ts, 'idle_s': auth.store.idle_s,
                                         'now_ts': wall_clock()})
                response.set_cookie(SESSION_COOKIE, result.token, max_age=auth.store.absolute_s, path='/',
                                    secure=cookie_secure, httponly=True, samesite='strict')
                return response
            if result.outcome == 'invalid':
                return JSONResponse({'detail': 'Invalid username or password'}, status_code=401)
            if result.outcome == 'locked':
                return JSONResponse({'detail': 'Too many failed attempts; try again later'}, status_code=429)
            reason = 'busy' if result.outcome == 'busy' else 'unavailable'
            return JSONResponse({'detail': f'Login unavailable ({reason}); retry later'}, status_code=503,
                                headers={'Retry-After': '1'})

        @app.post('/v1/auth/logout')
        async def logout(request: Request):
            try:
                await account_call(auth.store.close_session, request.cookies.get(SESSION_COOKIE),
                                   request.state.principal)
            except AuthStoreError as error:  # the cookie stays: the session is still open
                raise HTTPException(503, f'Account store unavailable ({error.reason})') from None
            response = Response(status_code=204)
            response.delete_cookie(SESSION_COOKIE, path='/', secure=cookie_secure, httponly=True,
                                   samesite='strict')
            return response

        @app.get('/v1/auth/me')
        def me(request: Request):
            # `idle_s`: the server's idle limit, so the dashboard can re-check just after it
            # without its own checks keeping an idle session alive. `now_ts`: this server's
            # clock, so the dashboard times the absolute end without trusting its own clock.
            principal = request.state.principal
            return {'username': principal.username, 'role': principal.role, 'expires_ts': principal.expires_ts,
                    'idle_s': auth.store.idle_s, 'now_ts': wall_clock()}

    def current_registry():
        if not feeds:
            raise HTTPException(503, 'No live event source is connected')
        pump_once()  # serve what has been published so far, not only the last background pump
        return registry

    @app.get('/v1/calls')
    def calls(status: str = 'all'):
        if status not in ('all', 'active', 'ended'):
            raise HTTPException(422, "status must be 'all', 'active' or 'ended'")
        registry_now = current_registry()
        return {'calls': registry_now.list_calls(status),
                'registry': {**registry_now.stats(), 'pump_errors': pump_errors[0]}}

    @app.get('/v1/calls/{call_id}')
    def call_detail(call_id: str):
        record = current_registry().get_call(call_id)
        if record is None:
            raise HTTPException(404, 'Unknown call')
        return record

    def encode_json(result):
        return Response(json.dumps(result, allow_nan=False, separators=(',', ':')), media_type='application/json')

    def read_and_encode(read, encode=encode_json):
        result = read()
        if result is None:
            return None
        # Encoded here, in the worker thread: a long history must not hold the event loop
        # (review M1). Non-finite numbers are already None, so this is strict JSON.
        try:
            return encode(result)
        except (TypeError, ValueError, KeyError, ArithmeticError, OSError) as error:  # e.g. a column type
            raise HistoryError(f'call history could not be encoded: {type(error).__name__}', reason='error') \
                from error

    # At most this many history requests use worker threads at once (re-review L2): the
    # rest are refused as busy instead of waiting, so sync probes keep their threads.
    history_slots = asyncio.Semaphore(MAX_HISTORY_REQUESTS)

    async def read_history(read, encode=encode_json, kind='history'):
        """Run one history read and its encoding off the event loop; map failures to 503
        with a fixed reason (never the driver's message) and count every outcome."""
        if history is None:
            raise HTTPException(503, 'No call history store is configured')
        try:
            if history_slots.locked():
                raise HistoryError('too many call history requests', reason='busy')
            async with history_slots:
                response = await run_in_threadpool(read_and_encode, read, encode)
        except HistoryError as error:
            metrics.count_history(error.reason, kind)
            raise HTTPException(503, f'Call history store unavailable ({error.reason})') from None
        metrics.count_history('ok' if response is not None else 'not_found', kind)
        return response

    @app.get('/v1/calls/{call_id}/history')
    async def call_history(call_id: str):
        response = await read_history(lambda: history.call(call_id))
        if response is None:
            raise HTTPException(404, 'No stored history for this call')
        return response

    @app.get('/v1/history/calls')
    async def history_calls(status: str = 'all', limit: int = DEFAULT_HISTORY_LIMIT, before: int | None = None):
        if status not in HISTORY_STATUSES:
            raise HTTPException(422, "status must be 'all', 'active' or 'ended'")
        if not 1 <= limit <= MAX_HISTORY_LIMIT:
            raise HTTPException(422, f'limit must be from 1 to {MAX_HISTORY_LIMIT}')
        if before is not None and not 1 <= before < 2 ** 63:
            raise HTTPException(422, 'before must be a positive session number')
        return await read_history(lambda: history.recent(status, limit, before))

    def encode_alert_csv(page):
        headers = {'Content-Disposition': 'attachment; filename="ava-alerts.csv"'}
        if page['next_cursor'] is not None:  # base64url: always a valid header value
            headers['X-AVA-Next-Cursor'] = page['next_cursor']
        return Response(alerts_csv(page['alerts']), media_type='text/csv; charset=utf-8', headers=headers)

    @app.get('/v1/alerts')
    async def alerts(request: Request, state: str = 'all', since: float | None = None, until: float | None = None,
                     min_confidence: float | None = None, limit: int | None = None, cursor: str | None = None,
                     format: str = 'json'):
        """PRD FR-A6: stored alerts newest first, filtered; `format=csv` exports the page."""
        if format not in ('json', 'csv'):
            raise HTTPException(422, "format must be 'json' or 'csv'")
        if limit is None:
            limit = DEFAULT_ALERT_CSV_LIMIT if format == 'csv' else DEFAULT_ALERT_LIMIT
        highest = MAX_ALERT_CSV_LIMIT if format == 'csv' else MAX_ALERT_JSON_LIMIT
        if not 1 <= limit <= highest:
            raise HTTPException(422, f'limit must be from 1 to {highest}')
        try:  # every other check (finite times, order, range, cursor) is the store's own
            check_alert_filters(state, since, until, min_confidence, cursor)
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        rows = []

        def encode_csv(page):
            rows.append(len(page['alerts']))
            return encode_alert_csv(page)
        response = await read_history(lambda: history.alerts(state, since, until, min_confidence, limit, cursor),
                                      encode_csv if format == 'csv' else encode_json, kind='alerts')
        if format == 'csv' and auth is not None:
            # PRD §6.3: exports are audited. Recorded before the file is sent: an export
            # that cannot be recorded is not sent (503).
            detail = {'state': state, 'since': since, 'until': until, 'min_confidence': min_confidence,
                      'limit': limit, 'continued': cursor is not None, 'rows': rows[0] if rows else None}
            try:
                await account_call(auth.store.audit, request.state.principal.username, 'alerts.export', 'ok', detail,
                                   wait_s=AUDIT_SLOT_WAIT_S)
            except AuthStoreError as error:
                raise HTTPException(503, f'Export not recorded in the audit log ({error.reason})') from None
        return response

    def feedback_store():
        if feedback is None:
            raise HTTPException(503, 'Alert feedback needs local accounts and the call store')
        return feedback

    @app.post('/v1/alerts/{alert_id}/feedback')
    async def give_feedback(alert_id: str, request: Request):
        """PRD FR-A7: label a stored alert true or false positive (201 with the record).
        The label, a copy of the alert's window scores and the audit row are one transaction."""
        store = feedback_store()
        body = await read_small_body(request, MAX_FEEDBACK_BYTES)
        if body is None:
            metrics.count_feedback('invalid')
            raise HTTPException(413, f'Feedback body exceeds {MAX_FEEDBACK_BYTES} bytes')
        try:
            fields = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            fields = None
        try:
            if not isinstance(fields, dict) or set(fields) != {'label'}:
                raise ValueError(f'send a JSON object with one field, label: one of {FEEDBACK_LABELS}')
            check_feedback(alert_id, fields['label'])
        except ValueError as error:
            metrics.count_feedback('invalid')
            raise HTTPException(422, str(error)) from None
        try:
            record = await account_call(store.record, request.state.principal, alert_id, fields['label'],
                                        wait_s=AUDIT_SLOT_WAIT_S)
        except AuthStoreError as error:
            metrics.count_feedback(error.reason)
            if error.reason == 'not_found':
                raise HTTPException(404, 'No stored alert with this ID') from None
            if error.reason == 'conflict':
                raise HTTPException(409, 'This alert has the most labels allowed') from None
            raise HTTPException(503, f'Feedback not stored ({error.reason})') from None
        metrics.count_feedback('ok')
        return JSONResponse(record, status_code=201)

    @app.get('/v1/alerts/{alert_id}/feedback')
    async def read_feedback(alert_id: str):
        """The alert's labels, newest first; `current` is the newest label."""
        store = feedback_store()
        if not valid_call_id(alert_id):  # alert IDs have the call ID's 1-128 character rule
            raise HTTPException(422, 'alert ID must be 1-128 characters without NUL')
        try:
            return await account_call(store.list, alert_id)
        except AuthStoreError as error:
            if error.reason == 'not_found':
                raise HTTPException(404, 'No stored alert with this ID') from None
            raise HTTPException(503, f'Feedback store unavailable ({error.reason})') from None

    snippet_slots = asyncio.Semaphore(MAX_SNIPPET_READS)

    @app.get('/v1/alerts/{alert_id}/snippet')
    async def play_snippet(alert_id: str, request: Request):
        """PRD FR-U5: the alert's retained snippet as a PCM16 mono WAV, decrypted in memory.
        Sent only after its `alerts.snippet` audit row is written (an audio export, PRD
        §6.3); 404 none kept, 410 expired, 500 unreadable, 503 not configured/busy/not
        audited. Never cached by the browser."""
        if snippets is None:
            raise HTTPException(503, 'Snippet playback is not configured on this server')
        try:
            check_alert_id(alert_id)
        except ValueError as error:
            metrics.count_snippet('invalid')
            raise HTTPException(422, str(error)) from None
        if snippet_slots.locked():
            metrics.count_snippet('busy')
            raise HTTPException(503, 'Too many snippet playbacks at once (busy); retry later')
        # The slot is held until the audit row is written (review M1): a decrypted WAV
        # waiting for an account slot still counts, so at most 2 are in memory at once.
        async with snippet_slots:
            try:
                snippet = await run_in_threadpool(snippets.read, alert_id)
            except SnippetUnavailable as error:
                metrics.count_snippet(error.reason)
                status, detail = SNIPPET_STATUS[error.reason]
                raise HTTPException(status, detail) from None
            except Exception:  # an unexpected reader fault: counted, fixed text, nothing sent
                metrics.count_snippet('error')
                raise HTTPException(*SNIPPET_STATUS['error']) from None
            detail = {'alert_id': alert_id, 'seconds': round(snippet['seconds'], 3), 'direction': snippet['direction']}
            try:
                await account_call(auth.store.audit, request.state.principal.username, 'alerts.snippet', 'ok', detail,
                                   wait_s=AUDIT_SLOT_WAIT_S)
            except (AuthStoreError, ValueError) as error:
                metrics.count_snippet('unaudited')
                reason = error.reason if isinstance(error, AuthStoreError) else 'invalid'
                raise HTTPException(503, f'Playback not recorded in the audit log ({reason})') from None
        metrics.count_snippet('ok')
        return Response(snippet['wav'], media_type='audio/wav',
                        headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
                                 'Content-Disposition': 'inline; filename="ava-alert-snippet.wav"',
                                 'X-AVA-Snippet-Seconds': f'{snippet["seconds"]:.3f}'})

    def read_wall_clock():
        try:
            return wall_clock()
        except Exception:  # timing is advisory: the event was sent; counted as unmeasured
            return None

    @app.websocket('/v1/events')
    async def events(websocket: WebSocket, call_id: str | None = None):
        token = None
        if auth is not None:  # before anything else: a refused handshake is HTTP 403
            if not same_origin(websocket.headers):
                metrics.count_auth_refusal('cross_origin')
                await websocket.close(code=1008)
                return
            token = websocket.cookies.get(SESSION_COOKIE)
            principal, failed = await resolve_session(token)
            if principal is None:
                if failed is None:
                    metrics.count_auth_refusal('no_session')
                await websocket.close(code=1008)
                return
        if hub is None:
            await websocket.accept()
            await websocket.close(code=1011, reason='No live event source is connected')
            return
        # Subscribe before accepting: a client that reads the REST snapshot as soon as the
        # socket opens cannot miss an event published between that read and the subscription.
        try:
            subscription = hub.subscribe(call_id=call_id or None, buffer=WS_BUFFER)
        except HubFull:
            await websocket.accept()
            await websocket.close(code=1013, reason='Too many event subscribers; retry later')
            return
        receiver = None
        try:
            await websocket.accept()
            receiver = asyncio.create_task(websocket.receive())  # watches for the client leaving
            checked = time.monotonic()
            while True:
                if auth is not None and time.monotonic() - checked >= SESSION_RECHECK_S:
                    principal, _ = await resolve_session(token)
                    if principal is None:  # logged out, revoked, expired, disabled, or the store failed
                        await _close_quietly(websocket, 1008, 'Session ended; log in again')
                        break
                    checked = time.monotonic()
                if receiver.done():
                    if receiver.result()['type'] == 'websocket.disconnect':
                        break
                    receiver = asyncio.create_task(websocket.receive())  # client text is ignored
                batch = subscription.drain_batch(WS_BATCH)
                envelopes = batch.envelopes
                messages = [(envelope.to_json(), envelope.ts) for envelope in envelopes]
                gap = gap_message(batch)
                if gap is not None:  # control message, not an FR-A4 event: the client should re-read REST
                    messages.insert(0, (gap, None))
                for text, event_ts in messages:
                    await _send_or_slow(websocket, text)
                    if event_ts is not None:  # FR-O6: publish -> handed to the server's transport
                        metrics.observe_ws_delivery(read_wall_clock(), event_ts)
                if not envelopes:
                    await asyncio.wait({receiver}, timeout=WS_POLL_S)
        except _SlowClient:
            metrics.count_ws_slow_close()
            await _close_quietly(websocket, 1008, 'Client is not reading events')
        except (WebSocketDisconnect, OSError):
            pass  # client went away mid-send (Starlette or the server's transport)
        finally:
            if receiver is not None:
                receiver.cancel()
            subscription.close()

    if dashboard_dir is not None:  # mounted last: API routes above always win
        app.mount('/', StaticFiles(directory=str(dashboard_dir), html=True), name='dashboard')
    return app
