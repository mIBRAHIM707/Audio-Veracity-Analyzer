# AVA telephony lab image (backlog 6.1; PRD FR-T1): Asterisk 20 (PJSIP) and SIPp 3.7 from
# Ubuntu 24.04 packages, pinned by base digest and exact package versions. Local lab
# only: no registry push, no credentials, SIP/RTP bound inside the container.
FROM ubuntu:24.04@sha256:2198e4c65076e9cb0b3d695219df667c0e3b0072da64ca3c2648aca17dcecaf5

ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      asterisk=1:20.6.0~dfsg+~cs6.13.40431414-2build5 \
      sip-tester=1:3.7.2-1build3 \
 && rm -rf /var/lib/apt/lists/*

# Replace the packaged sample configuration with the lab's minimal set.
RUN mkdir -p /etc/asterisk.dist && mv /etc/asterisk/* /etc/asterisk.dist/
COPY config/ /etc/asterisk/
COPY sipp/ /lab/sipp/
COPY entrypoint.sh /lab/entrypoint.sh
RUN mkdir -p /lab/io && chown asterisk:asterisk /lab/io && chmod 0755 /lab/entrypoint.sh

# Foreground, console logging to stdout; the test harness owns the container lifetime.
# ARI/HTTP only when the run passes AVA_ARI_PASSWORD (see entrypoint.sh).
CMD ["/lab/entrypoint.sh"]
