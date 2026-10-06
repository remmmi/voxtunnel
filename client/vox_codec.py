#!/usr/bin/env python3
#
# vox_codec.py — choix du codec et commandes d'encodage/decodage.
#
# Deux modes pour le flux dans le tunnel SSH :
#   pcm   s16 mono brut, 768 kbit/s a 48 kHz : aucune dependance, le
#         fonctionnement d'origine
#   opus  Ogg Opus 24 kbit/s (mode voip, trames de 20 ms) : trente fois
#         moins de debit, pour les liens ou le PCM brut sature la montee
#
# Opus demande ffmpeg avec libopus en local et opusdec (paquet opus-tools)
# sur le serveur. Il manque l'un des deux : repli pcm, decide par choose().
# Les options ffmpeg sont reprises a l'identique dans voxtunnel.sh, qui
# doit rester autonome ; modifier les deux ensemble.

import shutil
import subprocess

OPUS_KBPS = 24
FRAME_MS = 20

# Face du loopback ou le client ecrit. snd-aloop croise ses deux devices :
# ce qui est joue sur le 1 ressort en capture sur le 0, et inversement. Les
# enregistreurs lisent `default`, qui est la capture du device 0 sauf
# asoundrc personnalise : on joue donc sur le 1, ou sur la face opposee a
# celle que la sonde ci-dessous a vue derriere `default`.
SINK = "plughw:Loopback,1,0"
SINKS = {"C0": "plughw:Loopback,1,0", "C1": "plughw:Loopback,0,0"}

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_local_opus = None


def choose(server_opus, local_opus):
    """'opus' quand les deux bouts savent encoder et decoder, sinon 'pcm'."""
    return "opus" if server_opus and local_opus else "pcm"


def have_local_opus():
    """ffmpeg present avec l'encodeur libopus (mesure une fois)."""
    global _local_opus
    if _local_opus is None:
        _local_opus = False
        if shutil.which("ffmpeg"):
            try:
                out = subprocess.run(
                    ["ffmpeg", "-hide_banner", "-encoders"],
                    capture_output=True, text=True, timeout=10,
                    creationflags=NO_WINDOW).stdout
                _local_opus = " libopus " in out
            except (OSError, subprocess.SubprocessError):
                pass
    return _local_opus


def sink_probe_cmd():
    """Commande shell cote serveur : ouvre `default` en capture 0,3 s et
    dit quel device du loopback le porte (C0 ou C1), sans rien jouer.
    C? quand default n'est pas le loopback ou qu'il est occupe."""
    return ("arecord -D default -f S16_LE -c 1 -r 48000 -t raw -d 1 -q "
            ">/dev/null 2>&1 & p=$!; sleep 0.3; "
            "d=$(grep -l \"owner_pid *: *$p\\$\" "
            "/proc/asound/Loopback/pcm[01]c/sub*/status 2>/dev/null | head -1); "
            "kill $p 2>/dev/null; "
            "case \"$d\" in *pcm0c*) echo C0;; *pcm1c*) echo C1;; *) echo 'C?';; esac")


def sink_for(capture):
    """Face ou jouer d'apres la reponse de la sonde ('' ou C? = defaut)."""
    return SINKS.get(capture, SINK)


def opus_output_args(rate):
    """Arguments de sortie ffmpeg : Ogg Opus sur stdout, une page Ogg par
    trame et vidage immediat, sinon le conteneur retient l'audio."""
    return ["-c:a", "libopus", "-b:a", "%dk" % OPUS_KBPS,
            "-application", "voip", "-frame_duration", str(FRAME_MS),
            "-f", "ogg", "-page_duration", str(FRAME_MS * 1000),
            "-flush_packets", "1", "pipe:1"]


def encode_cmd(rate, period_us):
    """PCM s16 mono sur stdin -> Ogg Opus sur stdout. Sans ces options
    ffmpeg lit le pipe par blocs de 32 Ko et sonde l'entree : 340 ms
    retenus avant la premiere trame, mesure a 2,8 s de bout en bout."""
    block = rate * period_us // 1000000 * 2
    return ["ffmpeg", "-hide_banner", "-loglevel", "error",
            "-fflags", "nobuffer", "-probesize", "32",
            "-analyzeduration", "0", "-blocksize", str(block),
            "-f", "s16le", "-ar", str(rate), "-ac", "1", "-i", "pipe:0",
            ] + opus_output_args(rate)


def remote_cmd(codec, rate, buffer_us, period_us, sink=SINK):
    """Commande shell cote serveur : lecture du flux recu vers le loopback.
    En opus, opusdec sort un WAV sans longueur ; aplay le lit quand meme
    et avertit une fois ("Cannot seek on wav file output")."""
    if codec == "pcm":
        return ("aplay -D %s -f S16_LE -c 1 -r %d -t raw -q "
                "--buffer-time=%d --period-time=%d"
                % (sink, rate, buffer_us, period_us))
    if codec == "opus":
        return ("opusdec --quiet --force-wav --rate %d - - "
                "| aplay -D %s -t wav -q --buffer-time=%d --period-time=%d"
                % (rate, sink, buffer_us, period_us))
    raise ValueError("codec inconnu: %r" % codec)
