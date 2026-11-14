"""Local analysis API (backlog 2.2; PRD FR-A1/FR-A2 subset). No auth, no persistence yet.

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
                  message reports envelopes dropped for a slow client (re-read REST).
The calls and events routes need an `EventHub` passed to `create_app`; otherwise
they answer 503 / close with 1011. Redis pub/sub and PostgreSQL history are backlog 5.1.

Errors: 411 no Content-Length, 413 oversize/too long, 422 undecodable/unsupported,
503 busy, too many concurrent uploads or job queue full, 500 detector failure.
Jobs live in a bounded in-memory store served by one worker thread: they are lost
on restart (durable jobs are backlog 5.2). Multipart parsing keeps uploads over
1 MB in a temporary file that is deleted when the request ends; the bytes are then
held in memory only while queued or analysed (architecture §2.5 batch path).
"""
import asyncio
from collections import OrderedDict
from contextlib import asynccontextmanager
import json
import threading
import uuid

from fastapi import FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, PlainTextResponse

from services.calls.registry import CallRegistry, RegistryFeed
from services.events.fanout import HubFull
from services.inference.analysis import (AnalysisError, AnalyzerBusy, MAX_ASYNC_SECONDS, MAX_BYTES,
                                         NeedsAsyncJob, decode_wav)

PUMP_INTERVAL_S = 0.2
WS_BUFFER, WS_BATCH, WS_POLL_S, WS_SEND_TIMEOUT_S = 256, 64, 0.1, 10.0
MAX_QUEUED_JOBS, MAX_KEPT_JOBS = 4, 64
CHUNK = 1 << 20
MULTIPART_OVERHEAD = 64 * 1024
MAX_CONCURRENT_UPLOADS = 2
JOB_STATES = ('queued', 'running', 'done', 'failed')
ROUTES = ('/healthz', '/readyz', '/metrics', '/v1/analyze', '/v1/jobs/:id', '/v1/calls',
          '/v1/calls/:id')
STATUS_CLASSES = ('2xx', '3xx', '4xx', '5xx')


def route_label(path):
    """Fixed route labels keep metric cardinality bounded (job/call IDs are never labels)."""
    if path.startswith('/v1/jobs/'):
        return '/v1/jobs/:id'
    if path.startswith('/v1/calls/'):
        return '/v1/calls/:id'
    return path if path in ROUTES else 'other'


class Metrics:
    """Thread-safe counters rendered in Prometheus text exposition format 0.0.4."""

    def __init__(self):
        self._lock = threading.Lock()
        self.requests = {(route, cls): 0 for route in ROUTES + ('other',) for cls in STATUS_CLASSES}
        self.analysis_seconds_sum, self.analysis_count = 0.0, 0

    def count_request(self, route, status):
        cls = f'{status // 100}xx'
        with self._lock:
            if (route, cls) in self.requests:
                self.requests[(route, cls)] += 1

    def observe_analysis(self, seconds):
        if isinstance(seconds, (int, float)) and seconds >= 0:
            with self._lock:
                self.analysis_seconds_sum += seconds
                self.analysis_count += 1

    def render(self, jobs, service, uploads_in_flight):
        stats = jobs.stats()
        with self._lock:
            requests = dict(self.requests)
            seconds, count = self.analysis_seconds_sum, self.analysis_count
        lines = ['# HELP ava_api_requests_total HTTP responses by route and status class.',
                 '# TYPE ava_api_requests_total counter']
        lines += [f'ava_api_requests_total{{route="{route}",status="{cls}"}} {value}'
                  for (route, cls), value in sorted(requests.items())]
        lines += ['# HELP ava_jobs Jobs currently held in memory by state.', '# TYPE ava_jobs gauge']
        lines += [f'ava_jobs{{state="{state}"}} {stats["by_status"][state]}' for state in JOB_STATES]
        gauges = (('ava_job_queue_depth', 'Jobs waiting for the worker.', stats['queued']),
                  ('ava_job_queue_capacity', 'Maximum queued jobs.', stats['capacity']),
                  ('ava_uploads_in_flight', 'Uploads being received or analysed.', uploads_in_flight),
                  ('ava_detector_loaded', '1 when the detector is loaded.', int(service.detector_loaded)),
                  ('ava_ready', '1 after a successful warm-up inference.', int(service.ready)))
        for name, text, value in gauges:
            lines += [f'# HELP {name} {text}', f'# TYPE {name} gauge', f'{name} {value}']
        lines += ['# HELP ava_analysis_seconds Processing time of completed analyses (excludes model load).',
                  '# TYPE ava_analysis_seconds summary',
                  f'ava_analysis_seconds_sum {seconds!r}', f'ava_analysis_seconds_count {count}']
        return '\n'.join(lines) + '\n'


class JobStore:
    """Bounded in-memory jobs processed in order by one daemon worker thread."""

    def __init__(self, service, max_queued=MAX_QUEUED_JOBS, max_kept=MAX_KEPT_JOBS, on_result=None):
        self._service, self._max_queued, self._max_kept = service, max_queued, max_kept
        self._on_result = on_result or (lambda result: None)
        self._jobs, self._pending, self._closed = OrderedDict(), [], False
        self._condition = threading.Condition()
        self._worker = threading.Thread(target=self._run, name='ava-analysis-jobs', daemon=True)
        self._worker.start()

    def close(self, timeout=5):
        """Stop accepting work, drop queued uploads and let the worker exit after its current job."""
        with self._condition:
            self._closed = True
            for job_id, _ in self._pending:
                self._jobs[job_id].update(status='failed', error={'status': 503, 'detail': 'Service stopped'})
            self._pending.clear()
            self._condition.notify_all()
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
            self._condition.notify()
            return job_id

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


def create_app(service, *, hub=None, registry=None):
    """`hub` is the in-process `services.events.EventHub` carrying FR-A4 events; with it
    the app serves `/v1/calls` from a `CallRegistry` (a new one unless given) and
    `WS /v1/events`. Without it those routes answer 503: no live source is connected."""
    if registry is not None and hub is None:
        raise ValueError('a call registry needs the event hub that feeds it')
    metrics = Metrics()
    jobs = JobStore(service, on_result=lambda result: metrics.observe_analysis(result.get('processing_seconds')))
    in_flight = [0]
    registry = None if hub is None else (registry or CallRegistry())
    feeds = []  # the live feed; subscribed only while the app runs (lifespan)
    pump_errors = [0]

    def pump_once():
        try:
            feeds[0].pump()
        except Exception:  # the read model must not take the API down; counted, retried
            pump_errors[0] += 1

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
            yield
        finally:
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
        return {'status': 'ready', 'model_id': service.model_id, 'warmup_seconds': seconds}

    @app.get('/metrics')
    def prometheus():
        return PlainTextResponse(metrics.render(jobs, service, in_flight[0]),
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

    # Registered last, so it is the outermost middleware and also counts early refusals.
    @app.middleware('http')
    async def count_responses(request, call_next):
        try:
            response = await call_next(request)
        except Exception:  # unhandled: Starlette's outer error handler answers 500
            metrics.count_request(route_label(request.url.path), 500)
            raise
        metrics.count_request(route_label(request.url.path), response.status_code)
        return response

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
            metrics.observe_analysis(result.get('processing_seconds'))
            return result
        except AnalysisError as error:
            raise HTTPException(error.status, str(error)) from None

    @app.get('/v1/jobs/{job_id}')
    def job(job_id: str):
        record = jobs.get(job_id)
        if record is None:
            raise HTTPException(404, 'Unknown job')
        return record

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

    @app.websocket('/v1/events')
    async def events(websocket: WebSocket, call_id: str | None = None):
        await websocket.accept()
        if hub is None:
            await websocket.close(code=1011, reason='No live event source is connected')
            return
        try:
            subscription = hub.subscribe(call_id=call_id or None, buffer=WS_BUFFER)
        except HubFull:
            await websocket.close(code=1013, reason='Too many event subscribers; retry later')
            return
        receiver = asyncio.create_task(websocket.receive())  # watches for the client leaving
        try:
            while True:
                if receiver.done():
                    if receiver.result()['type'] == 'websocket.disconnect':
                        break
                    receiver = asyncio.create_task(websocket.receive())  # client text is ignored
                envelopes, dropped = subscription.drain(WS_BATCH)
                texts = [envelope.to_json() for envelope in envelopes]
                if dropped:  # control message, not an FR-A4 event: the client should re-read REST
                    texts.insert(0, json.dumps({'type': 'stream.gap', 'dropped': dropped}))
                for text in texts:
                    # A client that stops reading must not hold the server: give up after a timeout.
                    await asyncio.wait_for(websocket.send_text(text), WS_SEND_TIMEOUT_S)
                if not envelopes:
                    await asyncio.wait({receiver}, timeout=WS_POLL_S)
        except asyncio.TimeoutError:
            await _close_quietly(websocket, 1008, 'Client is not reading events')
        except (WebSocketDisconnect, OSError):
            pass  # client went away mid-send (Starlette or the server's transport)
        finally:
            receiver.cancel()
            subscription.close()

    return app
