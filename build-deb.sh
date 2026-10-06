#!/usr/bin/env bash
# Builds the Voxtunnel Debian packages:
#   voxtunnel_<version>_all.deb         client (tray app + engine)
#   voxtunnel-server_<version>_all.deb  server (snd-aloop loopback setup)
# Output in the repository root. Version: __version__ in client/voxtunnel-tray.py.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$DIR/client/voxtunnel-tray.py")"
[ -n "$VERSION" ] || { echo "version not found in voxtunnel-tray.py" >&2; exit 1; }

"$DIR/update-changelog.sh"

BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT
ROOT="$BUILD/voxtunnel_${VERSION}_all"

mkdir -p "$ROOT/DEBIAN" \
         "$ROOT/usr/share/voxtunnel/icons" \
         "$ROOT/usr/bin" \
         "$ROOT/usr/share/applications" \
         "$ROOT/usr/share/icons/hicolor/scalable/apps" \
         "$ROOT/usr/share/doc/voxtunnel"

install -m 755 "$DIR/client/voxtunnel.sh" "$DIR/client/voxtunnel-tray.py" \
        "$DIR/client/vox_buffer.py" "$DIR/client/vox_update.py" \
        "$ROOT/usr/share/voxtunnel/"
install -m 644 "$DIR/client/icons/"*.svg "$ROOT/usr/share/voxtunnel/icons/"
install -m 644 "$DIR/client/icons/voxtunnel-on.svg" \
        "$ROOT/usr/share/icons/hicolor/scalable/apps/voxtunnel.svg"
install -m 644 "$DIR/README.md" "$ROOT/usr/share/doc/voxtunnel/"

cat > "$ROOT/usr/bin/voxtunnel" <<'EOF'
#!/bin/sh
exec /usr/share/voxtunnel/voxtunnel-tray.py "$@"
EOF
chmod 755 "$ROOT/usr/bin/voxtunnel"

sed 's|^Exec=.*|Exec=voxtunnel|' "$DIR/client/voxtunnel.desktop.in" \
    > "$ROOT/usr/share/applications/voxtunnel.desktop"
chmod 0644 "$ROOT/usr/share/applications/voxtunnel.desktop"

cat > "$ROOT/DEBIAN/control" <<EOF
Package: voxtunnel
Version: $VERSION
Section: sound
Priority: optional
Architecture: all
Depends: python3, python3-pyqt5, alsa-utils, openssh-client
Recommends: ffmpeg, sox
Conflicts: voicepipe
Replaces: voicepipe
Maintainer: remmmi <remmmi@users.noreply.github.com>
Homepage: https://github.com/remmmi/voxtunnel
Description: Stream the local microphone to a VPS over SSH
 Voxtunnel streams the local microphone to the ALSA loopback card of a
 remote server over SSH, so voice tools running there can record it as
 a regular capture device. Ships a tray app with one switch per SSH
 host (discovered from ~/.ssh/config) and a master transmission toggle.
 Audio travels as Ogg Opus (24 kbit/s) when ffmpeg is present here and
 opus-tools on the server, as raw PCM otherwise.
 The server side only needs alsa-utils and the snd-aloop module (see
 the server/ folder of the homepage repository).
EOF

# umask restrictif possible : dpkg-deb exige des repertoires 0755
find "$ROOT" -type d -exec chmod 0755 {} +

dpkg-deb --build --root-owner-group "$ROOT" "$DIR/voxtunnel_${VERSION}_all.deb"
echo "built: voxtunnel_${VERSION}_all.deb"

# --- paquet serveur -----------------------------------------------------------
SROOT="$BUILD/voxtunnel-server_${VERSION}_all"
mkdir -p "$SROOT/DEBIAN" \
         "$SROOT/etc/modules-load.d" \
         "$SROOT/usr/share/doc/voxtunnel-server"

echo snd-aloop > "$SROOT/etc/modules-load.d/voxtunnel-snd-aloop.conf"
chmod 0644 "$SROOT/etc/modules-load.d/voxtunnel-snd-aloop.conf"
install -m 644 "$DIR/README.md" "$SROOT/usr/share/doc/voxtunnel-server/"

cat > "$SROOT/DEBIAN/control" <<EOF
Package: voxtunnel-server
Version: $VERSION
Section: sound
Priority: optional
Architecture: all
Depends: alsa-utils, kmod
Recommends: opus-tools
Conflicts: voicepipe-server
Replaces: voicepipe-server
Maintainer: remmmi <remmmi@users.noreply.github.com>
Homepage: https://github.com/remmmi/voxtunnel
Description: Receive Voxtunnel audio on a server (ALSA loopback setup)
 Server side of Voxtunnel: loads the snd-aloop kernel module now and at
 every boot, so audio streamed over SSH into plughw:Loopback,1,0 can be
 recorded locally from plughw:Loopback,0,0 (the default device) like a regular microphone.
 Purely additive: no existing audio configuration is touched.
EOF

echo /etc/modules-load.d/voxtunnel-snd-aloop.conf > "$SROOT/DEBIAN/conffiles"

cat > "$SROOT/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = "configure" ]; then
    modprobe snd-aloop 2>/dev/null \
        || echo "voxtunnel-server: snd-aloop not loadable now, will load at boot"
    # Debian policy forbids interactive prompts here, so just say it:
    echo "voxtunnel-server: streaming as a non-root SSH user requires the"
    echo "voxtunnel-server: audio group:  usermod -aG audio <user>"
fi
exit 0
EOF
chmod 0755 "$SROOT/DEBIAN/postinst"

find "$SROOT" -type d -exec chmod 0755 {} +
find "$SROOT/DEBIAN" -type f -name conffiles -exec chmod 0644 {} +

dpkg-deb --build --root-owner-group "$SROOT" "$DIR/voxtunnel-server_${VERSION}_all.deb"
echo "built: voxtunnel-server_${VERSION}_all.deb"
