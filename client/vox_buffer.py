#!/usr/bin/env python3
#
# vox_buffer.py — test du lien et auto-adaptation du tampon ALSA.
#
# Rien ne tourne cote VPS en dehors de `cat` (sonde) et du `aplay` du
# stream : tout est decide ici, cote client.
#
#   python3 vox_buffer.py <host>    sonde le lien, affiche le tampon conseille
#
# Trois briques, utilisees par voxtunnel-tray.py :
#   probe()      avant le stream : mesure la gigue du lien dans `ssh host cat`
#   recommend()  gigue mesuree -> tampon de depart
#   Watcher      pendant le stream : compte les underruns du aplay distant
#                et demande un tampon plus grand quand ils s'accumulent
#
# Le tampon ne descend jamais en cours de stream : chaque changement relance
# le aplay distant, donc coupe l'audio. La baisse passe par la sonde du
# demarrage suivant.

import math
import re
import subprocess
import sys
import threading
import time

BUFFER_MIN_MS, BUFFER_DEFAULT_MS, BUFFER_MAX_MS = 40, 80, 1500

# Periode ALSA : un quart du tampon, plafonnee. Au-dela, arecord livrerait
# l'audio par gros paquets et fabriquerait lui-meme des a-coups.
PERIOD_MAX_MS = 50

# --- sonde -------------------------------------------------------------------
# La sonde imite le debit du vrai stream (PCM s16 mono 48 kHz) : un bloc de
# 20 ms d'audio toutes les 20 ms, renvoye par `cat`. Des paquets plus petits
# seraient retenus par l'algorithme de Nagle et fausseraient la mesure.
PROBE_S = 2.0
PROBE_INTERVAL_S = 0.02
PROBE_BLOCK = 1920
MIN_SAMPLES = 20          # en dessous, la mesure ne vaut rien
MARGIN = 1.5              # tampon = p99 de la gigue x marge

# --- surveillance ------------------------------------------------------------
# Underruns comptes : assez longs pour s'entendre, assez courts pour qu'un
# tampon les absorbe. Au-dela du plafond c'est un arret du flux.
COUNT_MIN_MS = 20.0
TRIGGER_COUNT = 3
WINDOW_S = 60.0
STEP = 1.5

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# aplay traduit son message et sa virgule decimale selon la locale du VPS
UNDERRUN_RE = re.compile(r"!!!\D*([0-9]+(?:[.,][0-9]+)?)\s*ms")


def period_us(buffer_ms):
    return int(min(buffer_ms / 4.0, PERIOD_MAX_MS) * 1000)


def _clamp(ms):
    ms = int(math.ceil(ms / 10.0)) * 10
    return max(BUFFER_MIN_MS, min(BUFFER_MAX_MS, ms))


def recommend(rtts_ms, previous=None):
    """Tampon de depart (ms) pour une serie d'allers-retours, ou None si
    la serie est trop courte. La gigue est l'ecart au plus rapide ;
    l'aller-retour la surestime, le resultat est donc prudent.
    `previous` est le dernier tampon du host : on n'en redescend que d'un
    palier a la fois, car il a pu etre atteint sur de vrais underruns que
    deux secondes de sonde ne revoient pas forcement."""
    if len(rtts_ms) < MIN_SAMPLES:
        return None
    base = min(rtts_ms)
    jitter = sorted(r - base for r in rtts_ms)
    p99 = jitter[int(math.ceil(0.99 * len(jitter))) - 1]
    ms = _clamp(p99 * MARGIN)
    if previous:
        ms = max(ms, _clamp(previous / STEP))
    return ms


def step_up(buffer_ms):
    """Palier suivant, ou None si le tampon est deja au plafond."""
    if buffer_ms >= BUFFER_MAX_MS:
        return None
    return _clamp(buffer_ms * STEP)


def parse_underruns(text):
    """Durees (ms) des underruns presents dans un morceau de log."""
    return [float(m.replace(",", ".")) for m in UNDERRUN_RE.findall(text)]


class Watcher:
    """Suit les underruns d'un stream. feed() recoit les nouveaux octets du
    log et renvoie le tampon a appliquer quand il faut monter, sinon None."""

    def __init__(self, buffer_ms):
        self.buffer_ms = buffer_ms
        self.unstable = False     # au plafond et toujours des underruns
        self._events = []         # horodatages des underruns comptes
        self._rest = ""           # ligne incomplete de la lecture precedente

    def feed(self, text, now):
        text = self._rest + text
        cut = text.rfind("\n") + 1
        text, self._rest = text[:cut], text[cut:]
        for ms in parse_underruns(text):
            if COUNT_MIN_MS <= ms <= BUFFER_MAX_MS:
                self._events.append(now)
        self._events = [t for t in self._events if now - t <= WINDOW_S]
        if len(self._events) < TRIGGER_COUNT:
            return None
        self._events = []
        nxt = step_up(self.buffer_ms)
        if nxt is None:
            self.unstable = True
        return nxt


def probe_cmd(host):
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
            "-o", "Compression=no", "-o", "IPQoS=lowdelay", host, "cat"]


def probe(host, duration=PROBE_S, cmd=None, timeout=15.0):
    """Allers-retours (ms) de blocs envoyes dans `ssh host cat` pendant
    `duration` secondes. Liste vide si le lien ne repond pas."""
    try:
        proc = subprocess.Popen(
            cmd or probe_cmd(host), stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=NO_WINDOW)
    except OSError:
        return []

    received = {}                 # numero de bloc -> instant de retour
    ready = threading.Event()

    def reader():
        for line in proc.stdout:
            try:
                received[int(line.split(b" ", 1)[0])] = time.monotonic()
            except ValueError:
                continue
            ready.set()

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()

    def send(seq):
        head = b"%d " % seq
        proc.stdin.write(head + b"0" * (PROBE_BLOCK - len(head) - 1) + b"\n")
        proc.stdin.flush()

    sent = {}
    try:
        # le bloc 0 paie l'ouverture de la connexion : il n'est pas mesure
        send(0)
        limit = time.monotonic() + timeout
        while (not ready.wait(0.05) and proc.poll() is None
               and time.monotonic() < limit):
            pass
        if ready.is_set():
            seq, end = 0, time.monotonic() + duration
            while time.monotonic() < end:
                seq += 1
                sent[seq] = time.monotonic()
                send(seq)
                time.sleep(PROBE_INTERVAL_S)
            # laisse rentrer les derniers echos
            limit = time.monotonic() + 2.0
            while seq not in received and time.monotonic() < limit:
                time.sleep(0.02)
    except (OSError, ValueError):
        pass
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(2)
        except subprocess.TimeoutExpired:
            proc.kill()
        thread.join(1)
        proc.stdout.close()
    return [(received[s] - t) * 1000.0 for s, t in sorted(sent.items())
            if s in received]


def main(argv):
    if len(argv) != 2 or argv[1] in ("-h", "--help"):
        print("usage: vox_buffer.py <host>   (host SSH, comme VPS_HOST)",
              file=sys.stderr)
        return 2
    print("vox_buffer — sonde de %.0f s (aucun micro ouvert)" % PROBE_S)
    rtts = probe(argv[1])
    ms = recommend(rtts)
    if ms is None:
        print("  lien injoignable ou mesure trop courte (%d blocs)" % len(rtts),
              file=sys.stderr)
        return 1
    srt = sorted(rtts)
    print("  allers-retours : %d blocs   min %.0f ms   mediane %.0f ms   "
          "p99 %.0f ms   max %.0f ms"
          % (len(srt), srt[0], srt[len(srt) // 2],
             srt[int(math.ceil(0.99 * len(srt))) - 1], srt[-1]))
    print("  tampon conseille : %d ms (periode %d ms)"
          % (ms, period_us(ms) // 1000))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
