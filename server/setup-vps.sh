#!/usr/bin/env bash
# Prepares a VPS to receive Voxtunnel audio: ALSA loopback card (snd-aloop)
# plus alsa-utils. Idempotent and non-destructive: it only ADDS the loopback
# card, it never touches existing audio configuration.
#
#   ./setup-vps.sh            check + load snd-aloop for this boot
#   ./setup-vps.sh --persist  also load it automatically at every boot
#
# Run as root (or with sudo).
set -euo pipefail

say() { echo "setup-vps: $*"; }

[ "$(id -u)" -eq 0 ] || { say "run as root (sudo $0)"; exit 1; }

# --- alsa-utils (aplay/arecord) ---------------------------------------------
if command -v aplay >/dev/null 2>&1; then
  say "alsa-utils: already installed"
elif command -v apt-get >/dev/null 2>&1; then
  say "installing alsa-utils (apt)"
  DEBIAN_FRONTEND=noninteractive apt-get install -y alsa-utils
elif command -v dnf >/dev/null 2>&1; then
  say "installing alsa-utils (dnf)"
  dnf install -y alsa-utils
elif command -v pacman >/dev/null 2>&1; then
  say "installing alsa-utils (pacman)"
  pacman -S --noconfirm --needed alsa-utils
else
  say "no known package manager; install alsa-utils manually"; exit 1
fi

# --- snd-aloop module ---------------------------------------------------------
if lsmod | grep -q '^snd_aloop'; then
  say "snd-aloop: already loaded"
else
  say "loading snd-aloop"
  modprobe snd-aloop
fi

aplay -l | grep -q Loopback || { say "Loopback card not visible after modprobe"; exit 1; }
say "Loopback card: ok"

# --- persistence (optional) ---------------------------------------------------
if [ "${1:-}" = "--persist" ]; then
  # Same file as the voxtunnel-server package. Any file already loading the
  # module counts (snd-aloop.conf written by earlier versions of this script,
  # or a line added by hand): never a second one.
  CONF=/etc/modules-load.d/voxtunnel-snd-aloop.conf
  FOUND="$(grep -lsxE 'snd[-_]aloop' /etc/modules-load.d/*.conf | head -n 1 || true)"
  if [ -n "$FOUND" ]; then
    say "persistence: already configured ($FOUND)"
  else
    echo snd-aloop > "$CONF"
    chmod 0644 "$CONF"
    say "persistence: snd-aloop added to $CONF"
  fi
fi

# --- audio group for the SSH user (Y/n) ---------------------------------------
# aplay needs access to /dev/snd; root has it, other users need the group.
TARGET="${SUDO_USER:-}"
if [ -n "$TARGET" ] && [ "$TARGET" != "root" ] \
   && ! id -nG "$TARGET" 2>/dev/null | grep -qw audio; then
  if [ -t 0 ]; then
    printf 'add %s to the audio group (needed to stream as this user)? [Y/n] ' "$TARGET"
    read -r a
    case "$a" in
      n|N|no|non)
        say "aborted: without the audio group, $TARGET cannot open /dev/snd"
        say "and every stream toward this VPS as $TARGET will fail with a"
        say "permission error. Rerun this script to retry, or do it by hand:"
        say "  usermod -aG audio $TARGET"
        exit 1 ;;
      *) usermod -aG audio "$TARGET"
         say "$TARGET added to the audio group (takes effect on next SSH login)" ;;
    esac
  else
    say "note: to stream as $TARGET, run: usermod -aG audio $TARGET"
  fi
fi

say "done. Recording side on this VPS reads from: plughw:Loopback,0,0 (default)"
