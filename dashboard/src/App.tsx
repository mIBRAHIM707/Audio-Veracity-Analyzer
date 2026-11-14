import { Component, useCallback, useEffect, useRef, useState } from 'react';
import type { DragEvent, ReactNode } from 'react';
import { ApiError, checkFile, pollJob, submitAnalysis } from './api';
import type { AnalysisResult, JobStatus } from './api';
import { Timeline } from './Timeline';

// Batch upload page (PRD FR-U4; backlog 2.3). Drag-and-drop or choose a file, poll the
// job when the service queues it, show the verdict and the per-window timeline.

const DECISION_THRESHOLD = 0.5; // services/inference/analysis.py, uncalibrated baseline
export const POLL = { intervalMs: 1000, maxConsecutiveErrors: 5, timeoutMs: 20 * 60 * 1000 };

/** Per-request time limit for status reads: 30 s, or `?requestTimeoutMs=` clamped to
 * 1-30 s (a diagnostics/test knob; it can only shorten the limit). */
function requestTimeoutMs(): number {
  const raw = Number(new URLSearchParams(window.location.search).get('requestTimeoutMs'));
  return Number.isFinite(raw) && raw >= 1000 && raw <= 30000 ? raw : 30000;
}

type View =
  | { phase: 'idle' }
  | { phase: 'uploading'; fileName: string }
  | { phase: 'polling'; fileName: string; jobId: string; status: JobStatus }
  | { phase: 'done'; fileName: string; result: AnalysisResult }
  | { phase: 'error'; fileName: string | null; message: string };

const VERDICT_TEXT: Record<AnalysisResult['verdict'], string> = {
  synthetic: 'Likely synthetic speech',
  genuine: 'No synthetic speech indicated',
  undetermined: 'Undetermined',
};

function Limitations() {
  // PRD §1.4, shown on the dashboard (FR-U1).
  return (
    <aside className="limits" aria-label="Limitations">
      <strong>Research prototype, advisory only.</strong>
      <ul>
        <li>AVA flags likely synthetic speech. An alert is not proof of fraudulent intent, and the absence of an alert does not verify the caller.</li>
        <li>AVA does not detect human impersonators speaking in their own voice.</li>
        <li>AVA does not verify identity. It is not a voice biometric.</li>
      </ul>
    </aside>
  );
}

/** Last line of defence: a result that still fails to render shows an error in place of
 * the result and leaves the page (limitations, upload) mounted. */
class ResultBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  render() {
    return this.state.failed
      ? <p role="alert" className="error" data-testid="error">The result could not be displayed.</p>
      : this.props.children;
  }
}

function Result({ fileName, result }: { fileName: string; result: AnalysisResult }) {
  const uncalibrated = result.calibration === 'uncalibrated_baseline';
  return (
    <section aria-labelledby="result-heading" data-testid="result">
      <h2 id="result-heading">Result for <span className="filename">{fileName}</span></h2>
      <p className={`verdict ${result.verdict}`} data-testid="verdict">{VERDICT_TEXT[result.verdict]}</p>
      <p className="badge" data-testid="calibration">
        {uncalibrated ? 'Uncalibrated baseline: scores are not probabilities you can rely on.' : `Calibration: ${result.calibration}`}
      </p>
      <dl className="facts">
        <dt>Mean p(synthetic)</dt><dd data-testid="score">{result.p_synthetic === null ? '—' : result.p_synthetic.toFixed(3)}</dd>
        <dt>Evidence windows</dt><dd>{result.evidence_windows}</dd>
        {result.reason && (<><dt>Why undetermined</dt><dd data-testid="reason">{result.reason}</dd></>)}
        <dt>Duration</dt><dd>{result.duration_seconds.toFixed(1)} s ({result.input.sample_rate} Hz, {result.input.channels} ch)</dd>
        <dt>Processing time</dt><dd>{result.processing_seconds.toFixed(2)} s</dd>
        <dt>Model</dt><dd data-testid="model">{result.model_id}</dd>
        <dt>Flags</dt><dd data-testid="flags">{result.flags.length ? result.flags.join(', ') : 'none'}</dd>
      </dl>
      <p className="muted" data-testid="scope">{result.scope}</p>
      <h3>Per-window timeline</h3>
      <Timeline windows={result.timeline} durationSeconds={result.duration_seconds} threshold={DECISION_THRESHOLD} />
    </section>
  );
}

export function App() {
  const [view, setView] = useState<View>({ phase: 'idle' });
  const [dragging, setDragging] = useState(false);
  const controller = useRef<AbortController | null>(null);
  const busy = view.phase === 'uploading' || view.phase === 'polling';

  useEffect(() => () => controller.current?.abort(), []);

  const analyse = useCallback(async (file: File) => {
    const refusal = checkFile(file);
    if (refusal) {
      setView({ phase: 'error', fileName: file.name, message: refusal });
      return;
    }
    controller.current?.abort();
    const own = new AbortController();
    controller.current = own;
    setView({ phase: 'uploading', fileName: file.name });
    try {
      const submission = await submitAnalysis(file, own.signal);
      if (submission.kind === 'result') {
        setView({ phase: 'done', fileName: file.name, result: submission.result });
        return;
      }
      setView({ phase: 'polling', fileName: file.name, jobId: submission.jobId, status: 'queued' });
      const result = await pollJob(submission.jobId, {
        ...POLL, requestTimeoutMs: requestTimeoutMs(), signal: own.signal,
        onStatus: status => {
          if (!own.signal.aborted) setView({ phase: 'polling', fileName: file.name, jobId: submission.jobId, status });
        },
      });
      if (!own.signal.aborted) setView({ phase: 'done', fileName: file.name, result });
    } catch (error) {
      if (own.signal.aborted) return; // superseded by a newer upload or a cancel
      const message = error instanceof ApiError ? error.message : 'Unexpected error while analysing the file.';
      setView({ phase: 'error', fileName: file.name, message });
    }
  }, []);

  const cancel = () => {
    controller.current?.abort();
    setView({ phase: 'idle' });
  };

  const onDrop = (event: DragEvent<HTMLElement>) => {
    event.preventDefault();
    setDragging(false);
    if (busy) return;
    const files = event.dataTransfer.files;
    if (files.length !== 1) {
      setView({ phase: 'error', fileName: null, message: 'Drop exactly one file.' });
      return;
    }
    void analyse(files[0]);
  };

  return (
    <main>
      <header>
        <h1>AVA batch analysis</h1>
        <p className="muted">CPU-only analysis of one audio file. The file is sent only to this AVA server.</p>
      </header>
      <Limitations />
      <section
        className={`drop ${dragging ? 'dragging' : ''}`} data-testid="dropzone" aria-label="Upload"
        onDragOver={event => { event.preventDefault(); if (!busy) setDragging(true); }}
        onDragLeave={() => setDragging(false)} onDrop={onDrop}>
        <p>Drop a PCM16 WAV file here (up to 100 MB and 10 minutes), or</p>
        <label className="button">
          Choose a file
          <input type="file" accept=".wav,audio/wav,audio/x-wav" disabled={busy}
                 onChange={event => {
                   const file = event.currentTarget.files?.[0];
                   event.currentTarget.value = '';
                   if (file) void analyse(file);
                 }} />
        </label>
        <p className="muted">Files up to 60 s are analysed at once; longer files are queued as a job and polled.</p>
      </section>
      <div role="status" aria-live="polite" data-testid="status">
        {view.phase === 'uploading' && <p>Uploading and analysing {view.fileName}…</p>}
        {view.phase === 'polling' && <p>Job {view.status}: {view.fileName}</p>}
      </div>
      {/* Only while polling: aborting an upload would not stop the server's analysis
          (no cancellation in the API), it would only hide its outcome. */}
      {view.phase === 'polling' && <button type="button" onClick={cancel}>Stop waiting</button>}
      {view.phase === 'error' && (
        <p role="alert" className="error" data-testid="error">
          {view.fileName ? <><span className="filename">{view.fileName}</span>: </> : null}{view.message}
        </p>
      )}
      {view.phase === 'done' && (
        <ResultBoundary>
          <Result fileName={view.fileName} result={view.result} />
        </ResultBoundary>
      )}
    </main>
  );
}
