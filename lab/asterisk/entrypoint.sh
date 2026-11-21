#!/bin/sh
# AVA lab entrypoint (backlog 6.2). ARI (and the HTTP server it needs) is enabled only
# when the run supplies AVA_ARI_PASSWORD, so no credential lives in the image or in Git;
# tests generate a random one per container. Without it the HTTP server stays off.
set -eu

# Credential files are written with umask 027 (0640, group asterisk). The image's own
# umask is restored before Asterisk starts, so recordings in /lab/io keep their usual
# modes (20 Nov review M1: a umask left in place made them unreadable to the test user
# on a Linux host).
ORIGINAL_UMASK=$(umask)
umask 027

if [ -n "${AVA_ARI_PASSWORD:-}" ]; then
  case "$AVA_ARI_PASSWORD" in
    *[!A-Za-z0-9]*) echo "AVA_ARI_PASSWORD must be letters and digits only" >&2; exit 64 ;;
  esac
  if [ "${#AVA_ARI_PASSWORD}" -lt 16 ]; then
    echo "AVA_ARI_PASSWORD must be at least 16 characters" >&2; exit 64
  fi
  cat > /etc/asterisk/http.conf <<EOF
[general]
enabled = yes
bindaddr = 0.0.0.0
bindport = 8088
EOF
  cat > /etc/asterisk/ari.conf <<EOF
[general]
enabled = yes

[ava]
type = user
read_only = no
password = ${AVA_ARI_PASSWORD}
EOF
  chown root:asterisk /etc/asterisk/http.conf /etc/asterisk/ari.conf
  unset AVA_ARI_PASSWORD
fi

# Optional authenticated softphone endpoint (backlog 6.1; PRD FR-T1 "SIP softphone
# endpoints"): user `softphone`, digest authentication with the run's
# AVA_SOFTPHONE_PASSWORD (same rules as the ARI password). Without it the endpoint does
# not exist: pjsip.conf includes an empty file.
if [ -n "${AVA_SOFTPHONE_PASSWORD:-}" ]; then
  case "$AVA_SOFTPHONE_PASSWORD" in
    *[!A-Za-z0-9]*) echo "AVA_SOFTPHONE_PASSWORD must be letters and digits only" >&2; exit 64 ;;
  esac
  if [ "${#AVA_SOFTPHONE_PASSWORD}" -lt 16 ]; then
    echo "AVA_SOFTPHONE_PASSWORD must be at least 16 characters" >&2; exit 64
  fi
  cat > /etc/asterisk/pjsip_softphone.conf <<EOF
[softphone]
type=auth
auth_type=userpass
username=softphone
password=${AVA_SOFTPHONE_PASSWORD}

[softphone]
type=aor
max_contacts=1
remove_existing=yes
maximum_expiration=600

[softphone]
type=endpoint
context=from-softphone
disallow=all
allow=ulaw,alaw,gsm
auth=softphone
aors=softphone
direct_media=no
rtp_symmetric=yes
force_rport=yes
rewrite_contact=yes
EOF
  unset AVA_SOFTPHONE_PASSWORD
else
  : > /etc/asterisk/pjsip_softphone.conf
fi
chown root:asterisk /etc/asterisk/pjsip_softphone.conf

umask "$ORIGINAL_UMASK"
exec asterisk -f -U asterisk -G asterisk -vvv
