# Voxtunnel — client (local machine)

This folder is the LOCAL side of Voxtunnel: it captures the microphone and
streams it over SSH to the ALSA loopback card of a remote server. See the
repository README for the full picture.

## Files

- `voxtunnel.sh` — the streaming engine (bash). One process per stream,
  configured entirely through environment variables (`VPS_HOST`, `MIC`,
  `RATE`, `BUFFER_US`, `PERIOD_US`). Run `./voxtunnel.sh --help`.
- `voxtunnel-tray.py` — PyQt5 tray/window app. Discovers hosts from
  `~/.ssh/config` (Host blocks that declare an IdentityFile), shows one
  toggle per host plus a master "Transmission" toggle, and runs one
  `voxtunnel.sh` process per active host.
- `vox_buffer.py` — link test and automatic buffer sizing, imported by the
  tray (no Qt inside, covered by `tests/test_buffer.py`). Before a stream it
  probes the link through `ssh <host> cat` and picks a starting buffer per
  host; during the stream it counts the remote `aplay` underruns in the host
  log and asks the tray to restart that stream with a larger buffer. Nothing
  is installed on the server. `python3 vox_buffer.py <host>` runs the probe
  alone and prints the recommended buffer; it never opens the microphone.
- `vox_codec.py` — codec choice and the encode/decode commands, imported
  by the tray (no Qt inside, covered by `tests/test_codec.py`). Two modes:
  `pcm` (raw s16, 768 kbit/s, no dependency) and `opus` (Ogg Opus
  24 kbit/s through `ffmpeg -c:a libopus` here and `opusdec | aplay` on
  the server). The listener probe reports whether `opusdec` exists on
  each host; the tray picks `opus` only when both ends can, otherwise
  `pcm`, and shows the mode in the host row (`actif (opus) - 300 ms`).
  `voxtunnel.sh` carries the same ffmpeg options in bash (`CODEC=auto`
  by default): change both together.
- `vox_update.py` — update check and install, imported by the tray (no Qt
  inside, covered by `tests/test_update.py`). The latest GitHub release of
  the repository is the reference: the tray queries it shortly after launch
  and once a day, announces a newer version in the window footer and the
  tray menu, and on click downloads the client `.deb`, checks its SHA-256
  against the release metadata and installs it through `pkexec apt-get`
  (Debian package installs only; any other install opens the release page).
  It never installs while a stream is running. Each host row also shows the
  `voxtunnel-server` package version read over the existing listener probe;
  a click pushes the server `.deb` by `scp` and installs it with `sudo -n`,
  falling back to a command to paste when sudo needs a password.
  `python3 vox_update.py` prints the latest release.
- `install.sh` — user-level install of the desktop launcher (no root).
- `voxtunnel.desktop.in` — launcher template; `@DIR@` is replaced by
  the absolute path of this folder at install time.
- `icons/` — SVG tray icons: the VOX wordmark, outlines vectorised from
  Orbitron 900 (the site's display font), so no font needs to be installed.
  Site colours = transmitting, grey = idle, red with a diagonal cut = off.

## Installing without breaking the machine

Everything is user-level. Do NOT install anything system-wide beyond the
distribution packages listed below, and do not modify any audio
configuration: the client only READS the microphone through the existing
ALSA/PulseAudio/PipeWire stack.

1. Check dependencies (install with the distro package manager if missing):
   - `python3` and PyQt5 (`python3-pyqt5` on Debian/Ubuntu,
     `python3-qt5` on Fedora, `python-pyqt5` on Arch)
   - `alsa-utils` (for `arecord`) or `ffmpeg` as fallback recorder
   - optional: `ffmpeg` with `libopus` (Debian's has it) for the Opus mode
   - `openssh-client`
   - optional: `sox` (only for `voxtunnel.sh --tone`)
2. `./install.sh` installs the menu launcher for the current user only
   (`~/.local/share/applications`). `./install.sh --autostart` also copies
   it to `~/.config/autostart`. Nothing else is written outside
   `~/.cache/voxtunnel/` (logs) and `~/.config/voxtunnel/` (optional
   `ignore` file).
3. Run `python3 voxtunnel-tray.py` (or the menu entry). A second launch exits
   immediately (single-instance lock in /tmp).

## Rules for agents

- Never commit or print the user's `~/.ssh/config` contents, host names or
  key paths: they are private. The app reads them at runtime only.
- Do not start a stream toward a host without the user asking: it sends
  live microphone audio.
- Never make the tray install anything by itself, locally or on a server:
  every install is one explicit click.
- SSH access must use keys (`BatchMode=yes`); never store passwords.
- Keep `voxtunnel.sh` free of automatic retry loops (see its header
  comment for why).
