# Asterisk/SIPp telephony lab (backlog 6.1 partial; PRD FR-T1)

Local lab image with Asterisk 20 (PJSIP) and SIPp, used to place calls that carry
known audio. Lab only: nothing here is a deployment, no ports are published, no
credentials exist, and the image is never pushed. The 6.1 tests run the container
with `--network none` (SIPp and Asterisk talk over its loopback), so the
unauthenticated `anonymous` endpoint is unreachable from other containers too; any
later use on a shared network must restrict it.

## Contents

- `Dockerfile`: `ubuntu:24.04@sha256:2198e4c6...cecaf5` with exact package versions
  `asterisk=1:20.6.0~dfsg+~cs6.13.40431414-2build5` (GPL-2.0) and
  `sip-tester=1:3.7.2-1build3` (SIPp 3.7.2, GPL-2.0), both from Ubuntu noble; the
  packaged sample configuration is moved aside and replaced by `config/`.
- `config/`: UDP 5060 transport and one unauthenticated `anonymous` endpoint allowing
  ulaw, alaw and gsm (the caller's single-codec SDP offer forces the codec);
  extension `600` answers and records what the caller sends to
  `/lab/io/rec-<caller number>.wav` (8 kHz signed linear, receive direction only);
  RTP ports 10000-10200; noisy unused modules not loaded.
- `sipp/uac_inject.xml`: one call to `[service]` offering one codec
  (`-key pt <payload type> -key codec <PCMU|PCMA|GSM> -key caller <token>`), plays
  `/lab/io/inject.pcap` after the ACK, pauses for `-d` ms, then BYE.
- `rtp_pcap.py`: 8 kHz PCM16 -> 20 ms RTP payloads (G.711 by the project's encoder,
  GSM 06.10 by the pinned ffmpeg as RFC 3551 33-byte frames) -> libpcap file
  (Ethernet/IPv4/UDP/RTP, valid IP checksums, marker on the first packet) for SIPp's
  `play_pcap_audio`; `reference_decode` gives what an ideal receiver reconstructs.
  Command line for a real file: `uv run --no-project --python 3.11
  --with-requirements requirements-audio.txt python -m lab.asterisk.rtp_pcap --wav
  <file.wav> --codec ulaw|alaw|gsm --out <lab io folder>/inject.pcap` (plain PCM16
  WAV, mono or stereo, any rate 8-192 kHz; mean downmix and the shared soxr HQ step to
  8 kHz, no gain change). Captures are call audio: `.pcap` and raw telephony
  extensions are blocked by `.gitignore` and `scripts/repository_guard.py`.
- `harness.py`: disposable test container (unique name, label `ava-lab-test=1`,
  `--pull never`, temporary I/O folder made writable for the container's asterisk
  user, boot wait, removal even when start-up fails if `stop` is registered first).
  Find leftovers with `docker ps -a --filter label=ava-lab-test=1`.

## Build and test

Check the host first (17 Nov 2026, `lab/preflight.py`; read-only: it never pulls,
builds, starts or removes anything):

```text
uv run --no-project --python 3.11 python -m lab.preflight [--json] [--image TAG] [--min-free-gib 10]
```

It reads the environment the lab tests get: `.local/lab-test.env` under the shell's
variables. That shell variables win is assumed from the dotenv convention, not checked
against uv. No value is printed except the image tag. It reports
`pass`/`warn`/`fail`/`skip` for eight checks, the same eight on every run:
- the docker CLI;
- a running Linux-containers engine;
- the WSL 2 backend and engine CPU/memory (warn below 2 CPUs or 4 GiB);
- the lab image: `--image`, else `AVA_LAB_IMAGE`, else `ava-lab-asterisk:dev`. It fails
  if the image is not built, and prints the build command below as the fix;
- leftover `ava-lab-test=1` containers;
- free GiB on the repository drive and the temporary folder;
- `AVA_TEST_DOCKER_LAB=1`;
- the ffmpeg the GSM cases use, run with `-version` and held to the exact pin, and
  listing every encoder and decoder `data/prepare/ffmpeg_codecs.Ffmpeg` requires.

It exits 1 on any failure and 2 on bad arguments. Only selected fields are printed (no
`docker info` proxies, registries or labels).

On this host on 17 Nov all eight checks passed: Docker Desktop 29.9.3, WSL 2 kernel
6.18.33.2, 8 CPUs, 7.6 GiB, 182.0/67.2 GiB free, ffmpeg 9.0.2. The unit tests with a fake
docker are in `tests/unit/test_lab_preflight.py` (Claude-authored; part of the unit
suite CI runs).

```text
docker build -t ava-lab-asterisk:dev lab/asterisk
uv run --no-project --python 3.11 --env-file .local/lab-test.env --with-requirements requirements-audio.txt python -m unittest tests.lab.test_asterisk_lab -v
```

`.local/lab-test.env` (gitignored) holds `AVA_TEST_DOCKER_LAB=1`. Each test class
starts its own container through `harness.LabContainer`, waits for
`core waitfullybooted`, runs SIPp inside it and removes the container afterwards.
Docker Desktop 4.99 / engine 29.9.3 on WSL 2 (Windows 11) was used on 15 Nov 2026; the
build took about 3 minutes (apt downloads), a test run about 23 s. Only Windows was
tested; the GSM case needs the pinned ffmpeg (found through `AVA_FFMPEG` or the winget
install) and skips without it.

Results (15 Nov 2026, Claude-authored tests `tests/lab/test_asterisk_lab.py`): a
4 s mu-law call and a 3 s A-law call are recorded by Asterisk **sample-identical** to
the decode of the injected packets over at least 90 % of the call (the recording starts
just after the answer); a 4 s GSM 06.10 call correlates above 0.99 with ffmpeg's libgsm
decode of the same frames, and better than with the original signal; an offer with
only G.722 (not allowed) is refused with 488 and nothing is recorded. Synthetic
band-limited signals only; this is lab plumbing evidence, not detector evidence, and
the lab tests never run in CI (the pure capture tests in
`tests/unit/test_lab_rtp_pcap.py` do).

Limits: calls are capped at 60 s by `Record` (or `Dial`'s `L(60000)` for 810/811)
and SIPp's `-timeout 60s` although the
capture builder accepts up to 600 s; ACK and BYE use the original Request-URI rather
than the 200 OK Contact (fine on loopback).

## Bridged calls with PBX-forced codecs (17 Nov 2026; FR-T1)

Two more endpoints, `agent_alaw` (A-law only, SIPp UAS on 127.0.0.1:5071) and
`agent_gsm` (GSM 06.10 only, 5072). They are SIPp processes, not softphones. Extension
810 or 811 bridges the caller to one of them (`Dial` with `L(60000)`, so calls are
capped at 60 s like `Record`), and the PBX transcodes between a mu-law caller and the
agent's forced codec. `sipp/uas_agent.xml` answers with one codec (`-key pt`,
`-key codec`) and, run with `-rtp_echo`, sends the audio it receives back. Recordings
in `/lab/io`:
- `bridge-<caller>-to-agent.wav` and `bridge-<caller>-from-agent.wav`: MixMonitor on
  the agent leg, started by a pre-dial handler (what the PBX sent the agent, and the
  echo).
- `bridge-<caller>-to-caller.wav`: MixMonitor on the caller leg (what the PBX sends
  back towards the caller).
- `bridge-<caller>-format.txt`: the agent leg's `CHANNEL(audionativeformat)`, written
  by a `U()` hook on answer.
- `bridge-<caller>-dialstatus.txt`: `DIALSTATUS`, written only when `Dial` returns
  with the caller still up.

Results (Claude-authored `tests/lab/test_bridged_call_lab.py`, 17 Nov, 3 cases):
- **Codec forcing.** The INVITE to the agent offers only the forced audio codec (PCMA
  pt 8, or GSM pt 3), plus telephone-event 101 (DTMF, allowed at most once). The SDP is
  read from that INVITE only. The leg's native format is `(alaw)` / `(gsm)`.
  Mutation check: allowing `ulaw,alaw` on `agent_alaw` fails the case.
- **A-law path.** Asterisk 20.6 translates mu-law to A-law directly (`codec_a_mu`), so
  the agent-leg recording is A-law quantised: sample-identical to the project encoder's
  A-law round trip of the caller's mu-law decode, and different from the mu-law audio
  itself. The echo equals it exactly. The caller leg gets the mu-law round
  trip of the echo, also sample-identical. Which format a recording shows depends on
  Asterisk's translation path, so another version may need these checks updated.
- **GSM path.** The agent leg sees the mu-law decode exactly (signed linear) before the
  encoder. The echo is sample-identical to a libgsm encode (pinned ffmpeg) of exactly
  what the encoder received, so Asterisk's `codec_gsm` and libgsm made the same frames.
  The caller leg gets that GSM decode exactly (no direct GSM to mu-law translator).
- **Echo timing.** Each echo recording starts with one silent 20 ms block and then
  carries every echoed frame, starting with the first frame the agent leg sent (offset
  asserted). No sample is skipped in any comparison.
- **Agent not running.** Over UDP nothing reports the missing agent, so `Dial` rings it
  out for its 30 s timeout. `DIALSTATUS` is `NOANSWER`, the caller is never answered
  (it fails after 25-45 s), and the answer hook never runs.
- **Cleanup.** Each case first checks that no agent is bound to its port. It waits
  until the agent is listening, and after the call until the agent has exited. It
  kills the agent at cleanup (`pkill`, present in the image).

Existing lab cases (`test_asterisk_lab` 4, `test_ari_tap` 6) still pass with the new
configuration. Synthetic signals only.

Security: like `agent`, the two agent endpoints have no `identify` section and no
authentication. On Docker's default bridge network (used only by the ARI tap tests),
another container could therefore call 810/811 through `anonymous`, or match an agent
endpoint by its From user. Each such call records up to 60 s into the mounted I/O
folder. The bridged-call tests themselves run with `--network none`.

## Not done (6.1 remaining, 6.x)

- 6.1: a real genuine/synthetic WAV played through a call (the command exists, only
  synthetic signals were run here; the 6.QA tap tests play public CSALT clips through a
  call); Compose file. A softphone
  (rather than SIPp) as an endpoint was not tried.
- 6.3 RTP jitter/loss handling and AMR-NB replay; 6.QA end to end with AVA.

## ARI tap (6.2, 15 Nov 2026)

`entrypoint.sh` enables the HTTP server and ARI (user `ava`) only when the run passes
`AVA_ARI_PASSWORD` (letters and digits, at least 16 characters; tests generate one per
container, passed through the harness's environment rather than its command line), so
no credential is in the image or in Git. Extension `700` answers and records like 600;
it has no tap step: the tap spies on it from outside Stasis, so the call never depends
on the tap. The tap itself and its results are in `services/tap/README.md`; its lab
tests (`tests/lab/test_ari_tap.py`) publish ARI on 127.0.0.1 only and run on Docker's
default bridge (ExternalMedia must reach this host), so the anonymous SIP endpoint and
the password-protected ARI port are reachable from other containers on that bridge
during those tests (no SIP or RTP port is published).
- The built image's digest depends on the apt mirror state at build time (the two
  packages are pinned, their dependencies are not).
