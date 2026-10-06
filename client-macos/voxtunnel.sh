#!/usr/bin/env bash
# voxtunnel.sh (macOS) — streams the local microphone to the snd-aloop
# loopback of a remote Linux server, over SSH.
#
# EXPERIMENTAL: CI-tested only, never validated on real Apple hardware.
#
#   VPS_HOST=user@vps ./voxtunnel.sh --check    verify the remote end
#   VPS_HOST=user@vps ./voxtunnel.sh            stream until Ctrl-C
#
# Config (environment variables):
#   VPS_HOST   user@host SSH             (required)
#   MIC        avfoundation input        (default ":0" = default mic)
#   RATE       sample rate               (default 48000)
#   BUFFER_US  remote ALSA buffer in us  (default 80000)
#   PERIOD_US  remote ALSA period in us  (default 20000)
#
# List capture devices:  ffmpeg -f avfoundation -list_devices true -i ""
set -euo pipefail

VPS_HOST="${VPS_HOST:-}"
MIC="${MIC:-:0}"
RATE="${RATE:-48000}"
BUFFER_US="${BUFFER_US:-80000}"
PERIOD_US="${PERIOD_US:-20000}"

die() { echo "voxtunnel: $*" >&2; exit 1; }
[ -n "$VPS_HOST" ] || die "VPS_HOST is not set. Ex: VPS_HOST=user@vps $0"
command -v ffmpeg >/dev/null || die "ffmpeg missing: brew install ffmpeg"

ssh_vps() { ssh -o BatchMode=yes -o ConnectTimeout=10 "$VPS_HOST" "$@"; }

if [ "${1:-}" = "--check" ]; then
  ssh_vps true || die "SSH to $VPS_HOST failed (key auth required)"
  ssh_vps 'command -v aplay >/dev/null' || die "aplay missing on the VPS"
  ssh_vps 'aplay -l 2>/dev/null | grep -q Loopback' \
    || die "no Loopback card on the VPS (see server/ in the repo)"
  echo "ok: ssh + aplay + Loopback"
  exit 0
fi

echo "voxtunnel — mic $MIC to $VPS_HOST (Ctrl-C to stop)"
ffmpeg -hide_banner -loglevel error -f avfoundation -i "$MIC" \
       -f s16le -ar "$RATE" -ac 1 - \
| ssh -o BatchMode=yes -o ConnectTimeout=10 -o Compression=no -o IPQoS=lowdelay \
      "$VPS_HOST" \
      "aplay -D plughw:Loopback,1,0 -f S16_LE -c 1 -r $RATE -t raw -q \
             --buffer-time=$BUFFER_US --period-time=$PERIOD_US"
