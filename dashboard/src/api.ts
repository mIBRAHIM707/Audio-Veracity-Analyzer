// Typed client for the local analysis API (services/api/app.py; PRD FR-A1/FR-A2).
// Same origin only (architecture 2.6): relative URLs, no CORS, no external host.

export const MAX_UPLOAD_BYTES = 100 * 1024 * 1024; // FR-A1; the server enforces it too

export interface WindowScore {
  index: number;
  t_start: number;
  t_end: number;
  partial: boolean;
  p_synthetic: number;
}

export interface AnalysisResult {
  verdict: 'synthetic' | 'genuine' | 'undetermined';
  p_synthetic: number | null;
  margin: number | null;
  evidence_windows: number;
  reason: string | null;
  calibration: string;
  model_id: string;
  scope: string;
  duration_seconds: number;
  input: { sample_rate: number; channels: number };
  flags: string[];
  timeline: WindowScore[];
  aggregation: string;
  processing_seconds: number;
}

export type JobStatus = 'queued' | 'running' | 'done' | 'failed';

export interface JobRecord {
  job_id: string;
  status: JobStatus;
  result?: AnalysisResult;
  error?: { status: number; detail: string };
}

export type Submission =
  | { kind: 'result'; result: AnalysisResult }
  | { kind: 'job'; jobId: string };

/** An error the page can show as is: HTTP status (0 = network/protocol) and a message. */
export class ApiError extends Error {
  constructor(readonly status: number, message: string) {
    super(message);
    this.name = 'ApiError';
  }
}

const STATUS_TEXT: Record<number, string> = {
  404: 'The job is no longer known to the service (it may have restarted).',
  411: 'The upload had no Content-Length.',
  413: 'The file is too large (over 100 MB) or too long (over 10 minutes).',
  422: 'The file could not be decoded. Only PCM16 WAV is supported for now.',
  500: 'The detector failed while analysing this file.',
  503: 'The service is busy or its job queue is full. Try again shortly.',
};

/** Pure: the message shown for a failed response. Server detail is shown as text, never HTML. */
export function describeError(status: number, detail: unknown): string {
  const base = STATUS_TEXT[status] ?? `The service answered with HTTP ${status}.`;
  return typeof detail === 'string' && detail.trim() ? `${base} (${detail.trim()})` : base;
}

/** Pure: reject files the server would refuse before sending them. Null means acceptable. */
export function checkFile(file: { name: string; size: number }): string | null {
  if (file.size === 0) return 'The file is empty.';
  if (file.size > MAX_UPLOAD_BYTES) return STATUS_TEXT[413];
  return null;
}

/** Body as JSON, or undefined when it is not JSON. An abort (caller or time limit)
 * while reading is rethrown so it is reported as such, not as an unreadable body. */
async function readJson(response: Response, signal: AbortSignal): Promise<unknown> {
  try {
    return await response.json();
  } catch (error) {
    if (signal.aborted) throw error;
    return undefined;
  }
}

function detailOf(body: unknown): unknown {
  return body && typeof body === 'object' ? (body as { detail?: unknown }).detail : undefined;
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

const VERDICTS = new Set(['synthetic', 'genuine', 'undetermined']);
const JOB_STATES = new Set(['queued', 'running', 'done', 'failed']);

const finite = (v: unknown): boolean => typeof v === 'number' && Number.isFinite(v);

/** Validates every field the page renders; a malformed body becomes an ApiError, not a crash. */
export function parseResult(value: unknown): AnalysisResult {
  if (!isObject(value) || !VERDICTS.has(value.verdict as string) || !Array.isArray(value.timeline)
      || typeof value.calibration !== 'string' || typeof value.model_id !== 'string'
      || typeof value.scope !== 'string' || !Array.isArray(value.flags)
      || !value.flags.every(flag => typeof flag === 'string')
      || !(value.p_synthetic === null || finite(value.p_synthetic))
      || !finite(value.evidence_windows) || !(value.reason === null || typeof value.reason === 'string')
      || !finite(value.duration_seconds) || !finite(value.processing_seconds)
      || !isObject(value.input) || !finite(value.input.sample_rate) || !finite(value.input.channels)) {
    throw new ApiError(0, 'The service returned a result this page cannot read.');
  }
  for (const w of value.timeline) {
    if (!isObject(w) || !finite(w.index) || !finite(w.t_start) || !finite(w.t_end)
        || !finite(w.p_synthetic) || typeof w.partial !== 'boolean') {
      throw new ApiError(0, 'The service returned a timeline this page cannot read.');
    }
  }
  return value as unknown as AnalysisResult;
}

export function parseJob(value: unknown): JobRecord {
  if (!isObject(value) || typeof value.job_id !== 'string' || !JOB_STATES.has(value.status as string)) {
    throw new ApiError(0, 'The service returned a job record this page cannot read.');
  }
  const job = value as unknown as JobRecord;
  if (job.status === 'done') parseResult(job.result);
  return job;
}

/** Upload plus up to 60 s of audio analysed synchronously on a CPU. */
export const SUBMIT_TIMEOUT_MS = 5 * 60 * 1000;
/** One job-status read; the API answers these from memory. */
export const POLL_REQUEST_TIMEOUT_MS = 30 * 1000;

/** fetch bounded by a time limit as well as the caller's signal; a time-out or network
 * failure becomes ApiError(0), a caller abort is rethrown unchanged. The body is read
 * under the same limit. */
async function boundedFetch(url: string, init: RequestInit, signal: AbortSignal | undefined,
                            timeoutMs: number): Promise<{ response: Response; body: unknown }> {
  const limit = AbortSignal.timeout(Math.max(timeoutMs, 1));
  const combined = signal ? AbortSignal.any([signal, limit]) : limit;
  try {
    const response = await fetch(url, { ...init, signal: combined });
    return { response, body: await readJson(response, combined) };
  } catch (error) {
    if (signal?.aborted) throw error;
    if (limit.aborted) throw new ApiError(0, 'The analysis service did not answer in time.');
    throw new ApiError(0, 'The analysis service could not be reached.');
  }
}

export async function submitAnalysis(file: File, signal?: AbortSignal,
                                     timeoutMs = SUBMIT_TIMEOUT_MS): Promise<Submission> {
  const form = new FormData();
  form.append('file', file, file.name);
  const { response, body } = await boundedFetch('v1/analyze', { method: 'POST', body: form }, signal, timeoutMs);
  if (response.status === 200) return { kind: 'result', result: parseResult(body) };
  if (response.status === 202) {
    if (!isObject(body) || typeof body.job_id !== 'string' || !body.job_id) {
      throw new ApiError(0, 'The service accepted the file but returned no job ID.');
    }
    return { kind: 'job', jobId: body.job_id };
  }
  throw new ApiError(response.status, describeError(response.status, detailOf(body)));
}

export async function getJob(jobId: string, signal?: AbortSignal,
                             timeoutMs = POLL_REQUEST_TIMEOUT_MS): Promise<JobRecord> {
  const { response, body } = await boundedFetch(`v1/jobs/${encodeURIComponent(jobId)}`, {}, signal, timeoutMs);
  if (!response.ok) throw new ApiError(response.status, describeError(response.status, detailOf(body)));
  return parseJob(body);
}

export interface PollOptions {
  intervalMs: number;
  /** Consecutive network/5xx failures tolerated before giving up (a 404 ends at once). */
  maxConsecutiveErrors: number;
  /** Overall bound: a 10-minute file on a busy CPU should finish well inside this. */
  timeoutMs: number;
  /** Bound on one status read (default POLL_REQUEST_TIMEOUT_MS), never past the deadline. */
  requestTimeoutMs?: number;
  onStatus: (status: JobStatus) => void;
  signal: AbortSignal;
  sleep?: (ms: number, signal: AbortSignal) => Promise<void>;
  now?: () => number;
}

function abortableSleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) return reject(signal.reason);
    const timer = setTimeout(() => { signal.removeEventListener('abort', onAbort); resolve(); }, ms);
    const onAbort = () => { clearTimeout(timer); reject(signal.reason); };
    signal.addEventListener('abort', onAbort, { once: true });
  });
}

/** Polls GET /v1/jobs/{id} until done (result) or failed (ApiError with the job's error). */
export async function pollJob(jobId: string, options: PollOptions): Promise<AnalysisResult> {
  const sleep = options.sleep ?? abortableSleep;
  const now = options.now ?? (() => Date.now());
  const deadline = now() + options.timeoutMs;
  let errors = 0;
  for (;;) {
    let job: JobRecord | null = null;
    try {
      // Each read is bounded so a stalled request cannot outlive the overall deadline.
      const remaining = Math.max(deadline - now(), 1);
      job = await getJob(jobId, options.signal, Math.min(options.requestTimeoutMs ?? POLL_REQUEST_TIMEOUT_MS, remaining));
      errors = 0;
    } catch (error) {
      if (options.signal.aborted) throw error;
      const retryable = error instanceof ApiError && (error.status === 0 || error.status >= 500);
      errors += 1;
      if (!retryable || errors > options.maxConsecutiveErrors) throw error;
    }
    if (job) {
      options.onStatus(job.status);
      if (job.status === 'done') return job.result as AnalysisResult;
      if (job.status === 'failed') {
        // The job's own detail is the authoritative reason (e.g. 'Service stopped' is a
        // shutdown, not a busy queue), so it leads and the status text is not guessed.
        const status = finite(job.error?.status) ? (job.error as { status: number }).status : 500;
        const detail = typeof job.error?.detail === 'string' && job.error.detail.trim()
          ? job.error.detail.trim() : 'no reason given';
        throw new ApiError(status, `The job failed (HTTP ${status}): ${detail}`);
      }
    }
    if (now() >= deadline) {
      throw new ApiError(0, 'The job did not finish in time; it may still be running on the server.');
    }
    await sleep(options.intervalMs, options.signal);
  }
}
