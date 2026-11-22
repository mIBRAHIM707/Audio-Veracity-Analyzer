# AVA lab demo: instructions and script

Backlog 11.2 ("reproducible demo instructions"); PRD roadmap item 5.9 ("Demo script and
rehearsal: live tapped call, batch upload, timeline, egress proof"), BO-4 (a call placed
through Asterisk, tapped audio received, live verdict and timeline on the dashboard) and
BO-8 (live board, timeline, alert feed and batch upload). Written 22 November 2026.

Steps 1-6 were run by the automated rehearsal on this host on 22 Nov 2026, with its own
project name, port and passwords (see "Rehearsal evidence", which lists what it did NOT
run). `tests/unit/test_demo_guide.py` checks in CI that the files, `lab.demo` commands,
Compose profiles and demo port named here still exist.

## 1. What the demo shows, and what it does not

It shows the software working end to end on one machine:
- a SIP call into the lab Asterisk PBX, tapped passively over ARI, scored by the live
  service, stored in PostgreSQL and pushed to the dashboard through Redis and a
  WebSocket;
- the live board, the call's timeline, the alert feed and the stored history;
- a batch upload analysed by the AntiDeepfake baseline;
- that the AVA services cannot reach the internet.

It does NOT show detection accuracy. Say this in the demo:
- the live scorer runs the 14 Nov static-INT8 export with an UNTRAINED head until the
  trained AVA model exists, so live scores carry no meaning;
- the batch upload uses the pretrained AntiDeepfake baseline, which is uncalibrated and
  was not trained on Urdu or Punjabi telephone audio. Its verdict is an observation;
- the PBX is the lab Asterisk inside the stack, not an operator network;
- the dashboard has no sign-in in this stack and is reachable on 127.0.0.1 only. The API
  answers only requests addressed to `127.0.0.1`, `localhost` or `api`, so a web page
  cannot reach it through DNS rebinding; open the dashboard by one of the first two.

## 2. Prerequisites

- Windows with Docker Desktop on WSL 2 (or Linux with Docker), Node 22, `uv` and
  Python 3.11. Check the host with
  `uv run --no-project --python 3.11 python -m lab.preflight`.
- The three images, built from the repository root:

  ```text
  docker build -f deploy/live/Dockerfile -t ava-live:dev .
  docker build -f deploy/api/Dockerfile -t ava-api:dev .
  docker build -t ava-lab-asterisk:dev lab/asterisk
  ```

- Model files outside the repository (never in Git): an exported ONNX detector with its
  model card (`models/export/model_card.py`), the Silero VAD 6.2.3 model, and the
  AntiDeepfake MMS-300M checkpoint folder.
- Public audio for the call and the upload: plain PCM16 WAV files. The rehearsal used
  public CSALT Urdu clips (CC BY-NC 4.0, noncommercial use only). One call holds at most
  55 s of audio. Never use participant recordings in a demo.
- An env file outside the repository, copied from `deploy/compose/stack.env.example`,
  with every password replaced and the model folders filled in. Add `AVA_DEMO_PORT` if
  port 8080 is taken on this machine.

## 3. Start the stack

```text
docker compose --env-file D:/AVA-Data/deploy/demo.env -f deploy/compose/compose.yaml --profile lab --profile demo up -d --wait
uv run --no-project --python 3.11 --with-requirements requirements-audio.txt python -m lab.demo status --env-file D:/AVA-Data/deploy/demo.env
```

The first start takes a few minutes: the API verifies, loads and warms up the
checkpoint (its health check allows 180 s). `status` exits 0 only when every service is
running and healthy and the dashboard answers; its last line gives the URL to open. If
you set `AVA_DEMO_PORT`, pass the same number as `--demo-port`. Add
`--profile monitoring` to the `up` command and `--monitoring` to the `lab.demo` commands
to include Prometheus and Grafana.

## 4. Demo script (about 10 minutes)

### Step 1: the live board (1 min)

Open `http://127.0.0.1:8080/#live` (or your `AVA_DEMO_PORT`). The connection line reads
"Live". Explain: the board shows active calls, recently ended calls and the alert feed;
it is fed by the WebSocket and re-reads the call list after any gap.

### Step 2: a live tapped call (3 min)

Place a call with public audio (at most 55 s in total):

```text
uv run --no-project --python 3.11 --with-requirements requirements-audio.txt python -m lab.demo call --env-file D:/AVA-Data/deploy/demo.env --wav D:/AVA-Data/demo/clip1.wav --wav D:/AVA-Data/demo/clip2.wav
```

Within a few seconds a card appears under "Active calls" with the state and the smoothed
score marked uncalibrated. Click "Show timeline": scored windows appear as the call runs.
When the command prints `call_completed`, the card moves to "Recently ended" with end
reason `bye`. Point out that AVA never answers or holds the call: the tap is passive, and
if AVA fails the call still completes (PRD constraint; tested in
`tests/lab/test_compose_stack_lab.py`). `--codec alaw` places an A-law call instead.

### Step 3: the timeline (1 min)

With the timeline open, explain the bars: each is a scored window placed at the time its
score was published, covering the scoring stride. Point at the decision threshold line;
the table's note column names the scored direction. The alert feed lists any alert raised during the call; with the
untrained head, whether one appears means nothing.

### Step 4: batch upload (2 min)

Open `http://127.0.0.1:8080/` and drop a WAV file on "Choose a file". Files up to 60 s
are answered directly; longer ones become a job that the page polls. The result shows the
verdict, "Uncalibrated baseline", the model ID and a per-window timeline.

### Step 5: stored history (1 min)

Open `http://127.0.0.1:8080/#history`. The call from step 2 is listed with end `bye`;
click it to see its stored session, the codec detected (`ulaw`) and its windows. The API
reads this as the read-only `ava_reader` login.

### Step 6: egress proof (1 min)

```text
uv run --no-project --python 3.11 --with-requirements requirements-audio.txt python -m lab.demo egress --env-file D:/AVA-Data/deploy/demo.env
```

Both lines must show `"blocked": true`. For each of `live` and `api` the tool checks two
things inside the container: it has no default route (its networks are internal), and
a connection to 1.1.1.1:443 fails. A failed connection alone does not count, since an
offline laptop would also fail it; such a result reads "not proven". Mention the
exception: the demo forwarder is on a non-internal network (Docker needs one to publish
a port), so it has a route out, but it only ever connects to the API
(`deploy/compose/README.md`, "Demo profile").

### Optional: monitoring

Not part of the rehearsal. With `--profile monitoring`, Grafana is not published either.
Show its health through
`docker compose ... exec grafana curl -s http://127.0.0.1:3000/api/health`, or the
Prometheus targets through
`docker compose ... exec prometheus wget -qO- http://127.0.0.1:9090/api/v1/targets`.

## 5. Stop

```text
docker compose --env-file D:/AVA-Data/deploy/demo.env -f deploy/compose/compose.yaml --profile lab --profile demo down
```

Add `-v` to delete the stored calls as well. Stop the stack after the demo: while
`demo-proxy` runs, any local user or program can reach the unauthenticated API.

## 6. Recording the demo (BO-4)

BO-4 asks for a recorded live demo. Record the screen with any local tool while running
steps 1-6. The recording shows public clips only; keep it outside the repository (the
repository guard blocks audio and video files).

## 7. Rehearsal evidence

`tests/lab/test_compose_demo_lab.py` (Claude-authored, 22 Nov 2026) runs this script
automatically. It starts a disposable project with random passwords and a free port,
runs `lab.demo status`, then the Playwright project `compose-demo`
(`tests/browser/compose-demo-rehearsal.spec.cjs`):
- DR01: a real call with five public clips appears on the live board with its
  timeline, then ends `bye`;
- DR02: a batch upload shows the verdict, the calibration label and every window;
- DR03: the call is in the stored history with end `bye` and codec `ulaw`.
It then checks that a request or WebSocket naming another Host is refused through the
forwarder, runs `lab.demo egress`, and stops the forwarder. No page request went to another origin.

Not rehearsed: the default port 8080 and project name `ava` (the test picks its own),
`--codec alaw` (the A-law tap path is covered by the lab tests in
`tests/lab/test_tap_end_to_end.py`), an upload over 60 s through the forwarder (the job
path is covered by `tests/browser/real-model-dashboard.spec.cjs` without it), the
monitoring commands, and the board reconnecting after a restart (covered by
`tests/browser/dashboard-recovery.spec.cjs`, not on this stack).

```text
uv run --no-project --python 3.11 --env-file .local/lab-test.env --with-requirements requirements-audio.txt --with-requirements requirements-vad.txt --with-requirements requirements-export.txt python -m unittest tests.lab.test_compose_demo_lab -v
```

On 22 Nov the final run took 124 s with the images built: 3 browser cases passed in
54 s. The call showed 1 timeline row while active and 10 after its end, and the upload
11 windows for 24.1 s of audio. The forwarder accepted 26 connections and refused none;
the rebinding WebSocket got `403 Forbidden` and the rebinding request `421`.
These are plumbing observations, not accuracy results.

## 8. Troubleshooting

- `up` fails with "port is already allocated": set another `AVA_DEMO_PORT` in the env
  file.
- `status` shows `api` starting: the checkpoint warm-up is still running; wait up to
  3 minutes.
- `live` keeps restarting: it refused the model files. `docker compose ... logs live`
  shows one `refused` line per attempt with the reason (`deploy/compose/README.md`,
  "Model change and rollback").
- No card appears for a call: check `docker compose ... logs live` for `call_started`.
  The tapped extension is `AVA_TAP_EXTENSION` (default 700), and `lab.demo call` dials
  700 unless given `--extension`. Place one call at a time: every call plays the same
  capture file in the PBX.
- The page answers "Host not allowed": open it as `127.0.0.1` or `localhost`, not by the
  machine's name or address.
- The board says "disconnected": the API restarted or the forwarder stopped. The board
  reconnects by itself and re-reads the call list.
