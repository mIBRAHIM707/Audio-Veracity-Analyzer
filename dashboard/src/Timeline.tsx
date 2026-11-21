import type { WindowScore } from './api';

// Per-window score timeline (PRD FR-U3; architecture 2.6: lightweight SVG, no chart
// library). Shared later by the live and batch views. Each window is a bar spanning
// its seconds; bars at or above the threshold are marked as scored synthetic.

interface Props {
  windows: WindowScore[];
  durationSeconds: number;
  threshold: number;
  /** Replaces the batch description of the bars (the live view's bars differ). */
  caption?: string;
  /** Table shows only the newest rows (the chart still draws every window). */
  maxRows?: number;
  /** Heading of the first column (the live view numbers events, not windows). */
  indexHeading?: string;
  /** Score column heading; the history view's scores are uncalibrated, not probabilities. */
  scoreHeading?: string;
  /** Note for a window at or above the threshold. */
  markText?: string;
  /** Chart description for assistive technology (the span in seconds is appended). */
  ariaLabel?: string;
}

const WIDTH = 640;
const HEIGHT = 120;

export function Timeline({ windows, durationSeconds, threshold, caption, maxRows, indexHeading = 'Window',
                          scoreHeading = 'p(synthetic)', markText = 'scored synthetic',
                          ariaLabel = 'Per-window probability of synthetic speech' }: Props) {
  if (windows.length === 0) {
    return <p className="muted" data-testid="timeline-empty">No window was scored.</p>;
  }
  const rows = maxRows !== undefined && windows.length > maxRows ? windows.slice(-maxRows) : windows;
  const span = Math.max(durationSeconds, ...windows.map(w => w.t_end), 1e-6);
  const x = (t: number) => (t / span) * WIDTH;
  const y = (p: number) => HEIGHT - Math.min(Math.max(p, 0), 1) * HEIGHT;
  return (
    <figure className="timeline">
      <svg viewBox={`0 0 ${WIDTH} ${HEIGHT}`} role="img" preserveAspectRatio="none"
           aria-label={`${ariaLabel} over ${span.toFixed(1)} seconds`}>
        <rect className="plot" x={0} y={0} width={WIDTH} height={HEIGHT} />
        {windows.map(w => (
          <rect key={w.index} data-testid="timeline-bar"
                className={w.p_synthetic >= threshold ? 'bar synthetic' : 'bar'}
                x={x(w.t_start)} width={Math.max(x(w.t_end) - x(w.t_start), 1)}
                y={y(w.p_synthetic)} height={HEIGHT - y(w.p_synthetic)} opacity={w.partial ? 0.5 : 0.8}>
            <title>{`${w.t_start.toFixed(1)}–${w.t_end.toFixed(1)} s: ${w.p_synthetic.toFixed(3)}${w.partial ? ' (partial)' : ''}`}</title>
          </rect>
        ))}
        <line className="threshold" x1={0} x2={WIDTH} y1={y(threshold)} y2={y(threshold)} />
      </svg>
      <figcaption className="muted">
        {caption ?? 'Bars: windows (4 s, 2 s hop; overlapping). Faded bars are partial windows.'} Line: decision threshold {threshold}.
      </figcaption>
      {rows.length < windows.length && (
        <p className="muted" data-testid="timeline-rows-capped">Table: newest {rows.length} of {windows.length} windows.</p>
      )}
      <table>
        <thead>
          <tr><th scope="col">{indexHeading}</th><th scope="col">Seconds</th><th scope="col">{scoreHeading}</th><th scope="col">Note</th></tr>
        </thead>
        <tbody>
          {rows.map(w => (
            <tr key={w.index} data-testid="timeline-row" className={w.p_synthetic >= threshold ? 'synthetic' : undefined}>
              <td>{w.index}</td>
              <td>{w.t_start.toFixed(1)}–{w.t_end.toFixed(1)}</td>
              <td>{w.p_synthetic.toFixed(3)}</td>
              <td>{[w.p_synthetic >= threshold ? markText : '', w.partial ? 'partial' : '', w.note ?? ''].filter(Boolean).join(', ')}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </figure>
  );
}
