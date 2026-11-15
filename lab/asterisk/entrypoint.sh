#!/bin/sh
# AVA lab entrypoint (backlog 6.2). ARI (and the HTTP server it needs) is enabled only
# when the run supplies AVA_ARI_PASSWORD, so no credential lives in the image or in Git;
# tests generate a random one per container. Without it the HTTP server stays off.
set -eu

if [ -n "${AVA_ARI_PASSWORD:-}" ]; then
  case "$AVA_ARI_PASSWORD" in
    *[!A-Za-z0-9]*) echo "AVA_ARI_PASSWORD must be letters and digits only" >&2; exit 64 ;;
  esac
  if [ "${#AVA_ARI_PASSWORD}" -lt 16 ]; then
    echo "AVA_ARI_PASSWORD must be at least 16 characters" >&2; exit 64
  fi
  umask 027
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

exec asterisk -f -U asterisk -G asterisk -vvv
