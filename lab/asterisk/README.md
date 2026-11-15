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

Limits: calls are capped at 60 s by `Record` and SIPp's `-timeout 60s` although the
capture builder accepts up to 600 s; ACK and BYE use the original Request-URI rather
than the 200 OK Contact (fine on loopback).

## Not done (6.1 remaining, 6.x)

- 6.1: two SIP endpoints with a bridged call (FR-T1 "two or more SIP softphone
  endpoints"); codec forcing on the PBX side per endpoint (here the caller's
  single-codec offer forces it; the endpoint allows ulaw, alaw and gsm); a real
  genuine/synthetic WAV played through a call (the command exists, only synthetic
  signals were run); asserting the negotiated codec from the answer SDP; Docker/WSL
  preflight script; Compose file.
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
