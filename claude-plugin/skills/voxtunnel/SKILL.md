---
name: voxtunnel
description: >
  Install, verify, diagnose or roll back Voxtunnel: the microphone of a local machine streamed over
  SSH to the ALSA loopback card (snd-aloop) of a server, where speech-to-text or Claude Code voice
  input records it like a real microphone. Use when the user mentions voxtunnel, voice input on a
  VPS or remote server, "no microphone on the server", snd-aloop, the Loopback card, or asks to
  install, update or check either side.
---

# Voxtunnel

```
local machine (client)             server (VPS)
mic -> arecord ---- ssh (raw PCM) ---> aplay -> snd-aloop loopback
                                                  ^ recorders read default (plughw:Loopback,0,0)
```

Two sides, two packages, one repository: https://github.com/remmmi/voxtunnel. This skill never
installs anything on its own initiative: every step that changes the machine needs the user's
explicit agreement, and most need `sudo`.

## Rules (both sides)

- Inspect before changing anything. Never edit `/etc/asound.conf`, `~/.asoundrc`, PulseAudio or
  PipeWire configuration; never uninstall or reconfigure existing audio packages. Voxtunnel only
  **adds** a loopback card on the server and only **reads** the microphone on the client.
- No reboot. Do not reboot a server for this.
- Never print or commit the user's `~/.ssh/config`, host names or key paths.
- Never start a stream toward a host unless the user asks: it sends live microphone audio.
- SSH access uses keys only (`BatchMode=yes`); never store a password.
- If `sudo` asks for a password, do not try to work around it: give the user the exact command to
  run themselves.

## 1. Which side is this machine, and what is already there?

```bash
dpkg-query -W -f='${Package} ${Version} ${Status}\n' voxtunnel voxtunnel-server 2>/dev/null
lsmod | grep '^snd_aloop'                       # loopback module loaded?
aplay -l 2>/dev/null | grep Loopback            # loopback card visible?
ls /etc/modules-load.d/ | grep -i aloop         # loaded at boot?
[ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] && echo "graphical session"
```

| What you find | Meaning | What to do |
|---|---|---|
| headless machine reached over SSH, the user wants voice input here | **server** | §2 |
| desktop with a microphone, the user wants to talk to their servers | **client** | §3 |
| `voxtunnel-server` package installed, Loopback card visible | server done | §4 to verify |
| no package, but `snd_aloop` loaded, Loopback card visible and a file in `/etc/modules-load.d/` | server **working, outside the package** (set up by `setup-vps.sh` or by hand) | it works: say so. Offer the package (clean updates and removal), do not impose it |
| `snd_aloop` loaded but nothing in `/etc/modules-load.d/` | works until the next reboot | offer persistence (§2) |
| nothing | not installed | §2 or §3 |

A machine can be both (a desktop that also receives audio): treat each side separately.

## 2. Server side

Preferred on Debian and derivatives: the `voxtunnel-server` package from the latest release. It pulls
`alsa-utils`, loads `snd-aloop` now and at every boot
(`/etc/modules-load.d/voxtunnel-snd-aloop.conf`), and is removed cleanly by `apt purge`.

```bash
cd "$(mktemp -d)"
curl -fsSL https://api.github.com/repos/remmmi/voxtunnel/releases/latest \
  | python3 -c 'import json,sys
for a in json.load(sys.stdin)["assets"]:
    if a["name"].startswith("voxtunnel-server_"): print(a["browser_download_url"], a.get("digest",""))'
# download the URL printed above, then compare with the printed sha256 digest:
curl -fsSLO <url> && sha256sum voxtunnel-server_*.deb
sudo apt install ./voxtunnel-server_*.deb
```

Other distributions, or no package wanted: `server/setup-vps.sh --persist` from a clone of the
repository, as root. It is idempotent and writes one line to the same file as the package, unless a
file in `/etc/modules-load.d/` already loads the module (earlier versions of the script wrote
`snd-aloop.conf`).

The SSH user that receives the stream must be allowed to open audio devices: on most distributions,
membership of the `audio` group. Check with `id -nG <user>`; if missing, propose
`sudo usermod -aG audio <user>` (effective at that user's next login) and change nothing else about
the user.

## 3. Client side

Debian package (tray app, engine, menu entry), then the user launches `voxtunnel`:

```bash
# same release lookup as §2, with the asset name starting with "voxtunnel_"
sudo apt install ./voxtunnel_*.deb
```

Without root, from a clone: check the dependencies (`python3`, PyQt5, `alsa-utils` or `ffmpeg`,
`openssh-client`; optional `sox`), then `client/install.sh` (menu launcher for the current user;
`--autostart` for session start) and `python3 client/voxtunnel-tray.py`. Nothing is written outside
`~/.local/share/applications`, `~/.config/autostart`, `~/.cache/voxtunnel/` and `~/.config/voxtunnel/`.

The tray builds one switch per `Host` of `~/.ssh/config` that declares an `IdentityFile`. Exclusions:
`~/.config/voxtunnel/ignore`, one host per line.

## 4. Verify

Server, alone:

```bash
aplay -l | grep Loopback
arecord -D plughw:Loopback,1,0 -f S16_LE -c1 -r48000 -d 3 /tmp/voxtunnel-test.wav   # silence is fine here
```

Both ends, from the client (ask the user first: `--tone` sends a 5 s test tone, the plain command
would send the microphone):

```bash
VPS_HOST=user@server ./voxtunnel.sh --check     # verifies both ends, sends nothing
VPS_HOST=user@server ./voxtunnel.sh --tone      # while the arecord above runs on the server
```

## 5. Diagnose

| Symptom | Look at | Usual cause |
|---|---|---|
| no Loopback card on the server | `lsmod \| grep snd_aloop`, `sudo modprobe snd-aloop` | module not loaded; kernel without `snd-aloop` (some minimal cloud kernels: install the distribution's `linux-modules-extra` / full kernel image, with agreement) |
| card gone after a reboot | `/etc/modules-load.d/` | no persistence file |
| `aplay: audio open error: Permission denied` in the host log | `id -nG <ssh user>` | user not in the `audio` group, or not logged in again since being added |
| switch flips back to OFF in the tray | `~/.cache/voxtunnel/<host>.log` on the client | the last lines give the reason (SSH refused, `aplay` missing, device busy) |
| recording on the server is silent | read from `default` or `plughw:Loopback,0,0` (the client writes to `Loopback,1,0`) | wrong device, or master Transmission toggle off |
| dropouts (xruns) | buffer slider in the window; `BUFFER_US=200000 PERIOD_US=50000` for the bare engine | link jitter |
| host missing from the tray | its `Host` block in `~/.ssh/config` | no `IdentityFile`, wildcard host, or listed in the ignore file |

No automatic reconnect, by design: a dead stream turns its switch off and shows the error. Do not add
a retry loop around the engine.

## 6. Updates

The application updates itself, not through this plugin: the tray checks the latest release shortly
after launch and once a day, and installs on one explicit click (never while a stream runs); each
host row shows the server package version and can push the server `.deb`. A server set up outside
the package has nothing to update: the kernel module comes with the kernel. This plugin only keeps
the skill current.

## 7. Roll back

Server, package: `sudo apt purge voxtunnel-server`. Server, script or by hand:

```bash
sudo rmmod snd-aloop                                   # unload for this boot (fails if a stream is running)
sudo rm -f /etc/modules-load.d/voxtunnel-snd-aloop.conf /etc/modules-load.d/snd-aloop.conf
```

Remove a persistence file only if Voxtunnel put it there: ask when another tool on the machine may
rely on the loopback card. Client, package: `sudo apt purge voxtunnel`. Client, from source: delete
the launcher in `~/.local/share/applications` and `~/.config/autostart`. Nothing else was changed.
