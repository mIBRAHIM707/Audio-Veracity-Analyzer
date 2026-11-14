// Claude-authored browser-test support (14 November 2026): start and stop the real
// analysis API (tests/browser/serve_dashboard_api.py) for a spec, and build PCM16 WAVs.
const { spawn, spawnSync } = require('node:child_process');
const net = require('node:net');
const path = require('node:path');

const ROOT = path.resolve(__dirname, '..', '..', '..');
const DIST = path.join(ROOT, 'dashboard', 'dist');

function freePort() {
  return new Promise((resolve, reject) => {
    const probe = net.createServer();
    probe.once('error', reject);
    probe.listen(0, '127.0.0.1', () => { const { port } = probe.address(); probe.close(() => resolve(port)); });
  });
}

async function waitHealthy(url, child, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (child.exitCode !== null) throw new Error(`test server exited with ${child.exitCode}`);
    try { if ((await fetch(`${url}/healthz`)).ok) return; } catch { /* not listening yet */ }
    await new Promise(r => setTimeout(r, 250));
  }
  throw new Error('test server did not become healthy');
}

/** Starts the API on a free local port; `uvArgs` are extra `uv run` arguments and
 * `serverArgs` extra server arguments. Returns { base, stop }. */
async function startApi({ uvArgs = [], serverArgs = [], timeoutMs = 150000 } = {}) {
  const port = await freePort();
  const base = `http://127.0.0.1:${port}`;
  const child = spawn('uv', ['run', '--no-project', '--python', '3.11', ...uvArgs,
    '--with-requirements', 'requirements-api.txt', 'python', '-m', 'tests.browser.serve_dashboard_api',
    '--port', String(port), '--dashboard', DIST, ...serverArgs],
  { cwd: ROOT, stdio: ['ignore', 'ignore', 'pipe'], windowsHide: true });
  child.stderr.on('data', chunk => process.stderr.write(chunk));
  const stop = async () => {
    if (child.exitCode !== null) return;
    const exited = new Promise(resolve => child.once('exit', resolve));
    if (process.platform === 'win32') {
      // Killing uv.exe alone may leave its Python child holding the port and the model:
      // end the whole process tree.
      spawnSync('taskkill', ['/PID', String(child.pid), '/T', '/F'], { stdio: 'ignore', windowsHide: true });
    } else {
      child.kill();
    }
    await Promise.race([exited, new Promise(r => setTimeout(r, 5000))]);
  };
  try {
    await waitHealthy(base, child, timeoutMs);
  } catch (error) {
    await stop();
    throw error;
  }
  return { base, stop };
}

/** Mono PCM16 WAV from Int16 sample arrays (or {seconds, hz} tone segments at `rate`). */
function wavFromPcm(chunks, rate) {
  const total = chunks.reduce((n, c) => n + c.length, 0);
  const buffer = Buffer.alloc(44 + 2 * total);
  buffer.write('RIFF'); buffer.writeUInt32LE(buffer.length - 8, 4); buffer.write('WAVEfmt ', 8);
  buffer.writeUInt32LE(16, 16); buffer.writeUInt16LE(1, 20); buffer.writeUInt16LE(1, 22);
  buffer.writeUInt32LE(rate, 24); buffer.writeUInt32LE(rate * 2, 28); buffer.writeUInt16LE(2, 32);
  buffer.writeUInt16LE(16, 34); buffer.write('data', 36); buffer.writeUInt32LE(2 * total, 40);
  let offset = 44;
  for (const chunk of chunks) for (const value of chunk) { buffer.writeInt16LE(value, offset); offset += 2; }
  return buffer;
}

function tones(segments, rate = 8000) {
  return wavFromPcm(segments.map(({ seconds, hz }) => {
    const n = Math.round(seconds * rate);
    return Int16Array.from({ length: n }, (_, k) => (hz ? Math.round(8000 * Math.sin(2 * Math.PI * hz * k / rate)) : 0));
  }), rate);
}

/** Reads a plain-PCM16 mono WAV: { rate, samples: Int16Array }. Refuses anything else. */
function readPcm16Mono(buffer) {
  if (buffer.toString('ascii', 0, 4) !== 'RIFF' || buffer.toString('ascii', 8, 12) !== 'WAVE') throw new Error('not a RIFF/WAVE file');
  let offset = 12, fmt = null;
  while (offset + 8 <= buffer.length) {
    const id = buffer.toString('ascii', offset, offset + 4), size = buffer.readUInt32LE(offset + 4);
    if (id === 'fmt ') fmt = { tag: buffer.readUInt16LE(offset + 8), channels: buffer.readUInt16LE(offset + 10), rate: buffer.readUInt32LE(offset + 12), bits: buffer.readUInt16LE(offset + 22) };
    if (id === 'data') {
      if (!fmt || fmt.tag !== 1 || fmt.channels !== 1 || fmt.bits !== 16) throw new Error('expected plain PCM16 mono');
      if (offset + 8 + size > buffer.length || size % 2) throw new Error('data chunk truncated or odd-sized');
      const samples = new Int16Array(size / 2);
      for (let i = 0; i < samples.length; i++) samples[i] = buffer.readInt16LE(offset + 8 + 2 * i);
      return { rate: fmt.rate, samples };
    }
    offset += 8 + size + (size & 1);
  }
  throw new Error('no data chunk');
}

module.exports = { DIST, ROOT, readPcm16Mono, startApi, tones, wavFromPcm };
