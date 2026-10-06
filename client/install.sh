#!/usr/bin/env bash
# Installs the Voxtunnel launcher for the current user (XDG desktops).
# With --autostart, also adds it to session startup.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"

ask() { # question avec defaut oui ; sans TTY, repond non
  [ -t 0 ] || return 1
  read -r -p "$1 [Y/n] " a
  case "$a" in n|N|no|non) return 1 ;; *) return 0 ;; esac
}

# --- dependances : proposees en Y/n, sudo demande son mot de passe lui-meme --
MISSING=""
python3 -c "import PyQt5" 2>/dev/null || MISSING="$MISSING python3-pyqt5"
command -v arecord >/dev/null || command -v ffmpeg >/dev/null || MISSING="$MISSING alsa-utils"
command -v ssh >/dev/null || MISSING="$MISSING openssh-client"
if [ -n "$MISSING" ]; then
  echo "Missing packages:$MISSING"
  if command -v apt-get >/dev/null && ask "Install them now with sudo apt-get?"; then
    sudo apt-get install -y $MISSING
  else
    echo ""
    echo "Install aborted: Voxtunnel cannot run without these packages."
    echo "Install them, then rerun this script:"
    echo "  sudo apt-get install$MISSING && ./install.sh"
    exit 1
  fi
fi
# ffmpeg is optional: without it the stream is raw PCM (768 kbit/s)
command -v ffmpeg >/dev/null \
  || echo "note: Opus (24 kbit/s instead of 768) needs ffmpeg here and opus-tools on the server: sudo apt-get install ffmpeg"

chmod +x "$DIR/voxtunnel-tray.py" "$DIR/voxtunnel.sh"

APPS="$HOME/.local/share/applications"
mkdir -p "$APPS"
sed "s|@DIR@|$DIR|g" "$DIR/voxtunnel.desktop.in" > "$APPS/voxtunnel.desktop"

# named icon so the launcher shows up properly in the Multimedia menu
ICONS="$HOME/.local/share/icons/hicolor/scalable/apps"
mkdir -p "$ICONS"
cp "$DIR/icons/voxtunnel-on.svg" "$ICONS/voxtunnel.svg"

# clean leftovers from the old VoicePipe name
rm -f "$APPS/voicepipe-tray.desktop" "$ICONS/voicepipe.svg" \
      "$HOME/.config/autostart/voicepipe-tray.desktop"

command -v update-desktop-database >/dev/null && update-desktop-database "$APPS" || true
command -v kbuildsycoca5 >/dev/null && kbuildsycoca5 --noincremental >/dev/null 2>&1 || true
echo "Launcher and icon installed for the current user"

if [ "${1:-}" = "--autostart" ]; then
  mkdir -p "$HOME/.config/autostart"
  cp "$APPS/voxtunnel.desktop" "$HOME/.config/autostart/"
  echo "Added to session autostart"
fi
