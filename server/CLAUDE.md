# Voxtunnel — server (VPS)

This folder is the REMOTE side of Voxtunnel. The server receives raw PCM
audio over SSH and plays it into an ALSA loopback card (`snd-aloop`), where
any local program (speech-to-text, a voice assistant, Claude Code voice
input, ...) can record it as if a real microphone were plugged in.

The client writes to `plughw:Loopback,1,0`; programs on the VPS record
from `plughw:Loopback,0,0`, which is the ALSA `default` device when the
Loopback is the only sound card, so recorders work without any setting.
If an asoundrc points `default` at the other device, the client notices (it
opens `default` for 0.3 s once per host) and writes to the other face.
Clients before 1.4 wrote to `1,0`: a recorder pointed at `plughw:Loopback,1,0`
by hand must move to `0,0` or `default`.

## Installing WITHOUT breaking the VPS

The whole setup is additive: one kernel module and one package. Follow
these rules strictly:

- Inspect before changing: `lsmod | grep snd`, `aplay -l`, and check
  whether an audio stack is already in use by another service.
- Preferred on Debian: install the `voxtunnel-server` package from the
  GitHub releases (`sudo apt install ./voxtunnel-server_*.deb`). It only
  pulls `alsa-utils` (and recommends `opus-tools`), loads `snd-aloop` and persists it via
  `/etc/modules-load.d/voxtunnel-snd-aloop.conf`; removal is a clean
  `apt purge voxtunnel-server`.
- Otherwise run `setup-vps.sh` (idempotent). It installs `alsa-utils` if
  missing, `opus-tools` when the package manager has it, loads `snd-aloop`,
  and with `--persist` writes a single line to
  `/etc/modules-load.d/voxtunnel-snd-aloop.conf`.
- NEVER edit `/etc/asound.conf`, `~/.asoundrc`, PulseAudio or PipeWire
  configuration, and never uninstall or reconfigure existing audio
  packages. Voxtunnel does not need any of that.
- No reboot is required. Do not reboot a production VPS for this.
- The SSH user needs permission to open audio devices: on most distros
  that means being in the `audio` group. `setup-vps.sh` detects this and
  asks before running `usermod -aG audio <user>` (answering no aborts
  with an explanation); the Debian package prints the command instead,
  as maintainer scripts must not prompt. Do not change anything else
  about the user.

`opus-tools` (`opusdec`) is optional: with it the client sends Ogg Opus at
24 kbit/s and the server runs `opusdec | aplay`; without it the client falls
back to raw PCM (768 kbit/s) on its own. Nothing else differs.

## Verify

```
aplay -l | grep Loopback                       # card is visible
arecord -D plughw:Loopback,0,0 -f S16_LE -c1 -r48000 -d 3 /tmp/test.wav
```

Then from the client: `VPS_HOST=user@this-vps ./voxtunnel.sh --check`
(and `--tone` to send a 5 s test tone while the arecord above runs).

## Rollback

```
rmmod snd-aloop                                # unload for this boot
rm -f /etc/modules-load.d/voxtunnel-snd-aloop.conf       # remove persistence
```

Servers prepared by an earlier `setup-vps.sh` have the same line in
`/etc/modules-load.d/snd-aloop.conf` instead: the script recognises it and
does not add a second file; remove that one for a rollback.

Nothing else was changed.
