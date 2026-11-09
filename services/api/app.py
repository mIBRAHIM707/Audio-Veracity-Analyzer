"""Local analysis API (backlog 2.2; PRD FR-A1/FR-A2 subset). No auth, no persistence yet.

POST /v1/analyze  multipart field `file` (PCM16 WAV). Files up to 60 s return 200 with
                  the analysis; longer files (up to 10 minutes) return 202 with a job ID.
GET  /v1/jobs/{id} queued | running | done (with result) | failed (with error).
GET  /healthz     liveness and whether the detector has loaded.

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
import threading
import uuid

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from services.inference.analysis import (AnalysisError, MAX_ASYNC_SECONDS, MAX_BYTES, NeedsAsyncJob,
                                         decode_wav)

MAX_QUEUED_JOBS, MAX_KEPT_JOBS = 4, 64
CHUNK = 1 << 20
MULTIPART_OVERHEAD = 64 * 1024
MAX_CONCURRENT_UPLOADS = 2


class JobStore:
    """Bounded in-memory jobs processed in order by one daemon worker thread."""

    def __init__(self, service, max_queued=MAX_QUEUED_JOBS, max_kept=MAX_KEPT_JOBS):
        self._service, self._max_queued, self._max_kept = service, max_queued, max_kept
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


async def read_bounded(upload):
    data = bytearray()
    while chunk := await upload.read(CHUNK):
        data.extend(chunk)
        if len(data) > MAX_BYTES:
            raise HTTPException(413, 'File exceeds 100 MB')
    return bytes(data)


def create_app(service):
    jobs = JobStore(service)

    @asynccontextmanager
    async def lifespan(_app):
        yield
        await run_in_threadpool(jobs.close)

    # Swagger/ReDoc pages load CDN assets, so they stay off; /openapi.json is served locally.
    app = FastAPI(title='AVA local analysis API', version='0.1.0', docs_url=None, redoc_url=None,
                  lifespan=lifespan)
    uploads = asyncio.Semaphore(MAX_CONCURRENT_UPLOADS)
    app.state.jobs = jobs

    @app.get('/healthz')
    def health():
        return {'status': 'ok', 'detector_loaded': service.detector_loaded, 'model_id': service.model_id}

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
                return await call_next(request)
        return await call_next(request)

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
            return await run_in_threadpool(service.analyze_wav, payload)
        except AnalysisError as error:
            raise HTTPException(error.status, str(error)) from None

    @app.get('/v1/jobs/{job_id}')
    def job(job_id: str):
        record = jobs.get(job_id)
        if record is None:
            raise HTTPException(404, 'Unknown job')
        return record

    return app
