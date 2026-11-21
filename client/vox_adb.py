#!/usr/bin/env python3
#
# vox_adb.py — adb a travers le tunnel : le telephone branche ici, pilote
# par le client adb du serveur.
#
#   python3 vox_adb.py <host>    preflight, puis tunnel jusqu'a Ctrl-C
#
# Le serveur adb reste ICI (c'est lui qui parle au telephone en USB) ; un
# `ssh -R` pose son port 5037 sur le 127.0.0.1 du serveur, ou n'importe quel
# `adb ...` le trouve comme s'il etait local. Rien d'autre que le paquet adb
# n'est demande au serveur.
#
# Une connexion SSH a part de celle de la voix (ControlPath=none : jamais
# multiplexee, meme si ~/.ssh/config partage les connexions) : un `adb push`
# qui sature son tuyau ne retient pas l'audio, et l'inverse. IPQoS=throughput
# laisse la priorite reseau a la voix (lowdelay).
#
# Fiabilite, sur le modele de la voix :
#   - preflight nominatif : adb aux deux bouts, meme version de protocole
#     (un client adb d'une autre version TUE le serveur qu'il joint, donc
#     celui d'ici a travers le tunnel), et qui tient le port 5037 du serveur ;
#   - le dernier tunnel lance prend le port : un serveur adb demarre sur le
#     VPS pendant une coupure, ou le tunnel fantome d'un client parti sans
#     fermer, est arrete (un adb ou un sshd du meme utilisateur, rien
#     d'autre), comme SINK_GUARD pour la face du loopback ;
#   - battement de bout en bout : le serveur liste les telephones toutes les
#     5 s A TRAVERS le tunnel ; le tray affiche ce que le VPS voit vraiment ;
#   - ServerAlive : un lien mort fait sortir ssh en 30 s au lieu de rester
#     pendu. La relance (avec attente croissante) vit dans le tray, pas ici.
#
# Utilise par voxtunnel-tray.py ; aucun Qt ici (tests/test_adb.py).

import re
import shutil
import subprocess
import sys

PORT = 5037
BEAT_S = 5

OK, RETRY, FATAL = "ok", "retry", "fatal"

# attente avant la n-ieme relance d'un tunnel tombe, en secondes
BACKOFF_S = (2, 5, 10, 30)

SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]

FORWARD = "127.0.0.1:%d:127.0.0.1:%d" % (PORT, PORT)
# ce que ssh ecrit quand le serveur refuse le port (deja tenu)
FORWARD_FAILED = "remote port forwarding failed"

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Cote serveur : version d'adb, puis occupation du port. `adb version` ne
# demarre pas de serveur. ss ne donne le pid que pour nos propres process,
# et pas celui d'un sshd de session (process non lisible meme par son
# utilisateur) : sans pid, on cherche la boucle de battement d'un tunnel
# Voxtunnel a nous et on prend son sshd parent. Rien trouve : le port
# appartient a un autre utilisateur.
REMOTE_PREFLIGHT = r'''command -v adb >/dev/null 2>&1 || { echo NOADB; exit 0; }
adb version 2>/dev/null | sed -n "1s/.*version /V /p"
busy() { ss -Hltn "sport = :%(port)d" 2>/dev/null | grep -q .; }
busy || { echo free; exit 0; }
p=$(ss -Hltnp "sport = :%(port)d" 2>/dev/null | sed -n "s/.*pid=\([0-9]*\).*/\1/p" | head -n 1)
if [ -z "$p" ]; then
  g=$(pgrep -u "$(id -u)" -f "echo UP; while :; do if o=" 2>/dev/null | head -n 1)
  [ -n "$g" ] && p=$(ps -o ppid= -p "$g" 2>/dev/null | tr -d " ")
fi
[ -n "$p" ] || { echo denied; exit 0; }
c=$(ps -o comm= -p "$p" 2>/dev/null)
case "$c" in adb|sshd*) ;; *) echo "foreign $c"; exit 0 ;; esac
kill "$p" 2>/dev/null || { echo denied; exit 0; }
i=0
while [ $i -lt 30 ]; do
  busy || { echo "freed $c"; exit 0; }
  [ $i -eq 20 ] && kill -9 "$p" 2>/dev/null
  sleep 0.1; i=$((i+1))
done
echo stuck''' % {"port": PORT}

# Cote serveur, dans la session du tunnel : UP, puis toutes les 5 s le
# nombre de telephones vus par le adb du serveur (prets, non autorises), ou
# X quand le serveur adb ne repond pas au bout du tunnel.
REMOTE_BEAT = (
    "echo UP; while :; do "
    "if o=$(timeout 10 adb devices 2>/dev/null); then "
    "echo \"D $(printf '%%s\\n' \"$o\" | awk 'NR>1 && $2==\"device\"{d++} "
    "NR>1 && $2==\"unauthorized\"{u++} END{print d+0, u+0}')\"; "
    "else echo X; fi; sleep %d; done" % BEAT_S)


def have_local():
    return bool(shutil.which("adb"))


def version_of(text):
    """Version de protocole annoncee par `adb version` ('1.0.41'), ou ''."""
    m = re.search(r"version\s+(\d+(?:\.\d+)+)", text or "")
    return m.group(1) if m else ""


def preflight_cmd(host):
    return ["ssh"] + SSH_OPTS + [host, REMOTE_PREFLIGHT]


def tunnel_cmd(host):
    return (["ssh"] + SSH_OPTS + [
        "-o", "ControlMaster=no", "-o", "ControlPath=none",
        "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3",
        "-o", "IPQoS=throughput",
        "-R", FORWARD, host, REMOTE_BEAT])


def parse_preflight(out, local_version):
    """Sortie de REMOTE_PREFLIGHT -> (OK | RETRY | FATAL, message)."""
    lines = [l.strip() for l in (out or "").splitlines() if l.strip()]
    if "NOADB" in lines:
        return FATAL, "adb absent du serveur. Sur le serveur : " + install_hint()
    remote = ""
    for line in lines:
        if line.startswith("V "):
            remote = line[2:].strip()
    if not remote:
        return FATAL, "version d'adb illisible sur le serveur"
    if remote != local_version:
        return FATAL, ("adb %s ici, %s sur le serveur : versions de protocole "
                       "différentes, le client du serveur tuerait le serveur "
                       "adb d'ici. Aligner les deux paquets adb."
                       % (local_version or "?", remote))
    last = lines[-1]
    if last == "free":
        return OK, ""
    if last.startswith("freed"):
        return OK, "port %d repris à %s" % (PORT, last[6:] or "?")
    if last.startswith("foreign"):
        return FATAL, ("port %d du serveur tenu par %s : ni adb ni sshd, "
                       "je n'y touche pas" % (PORT, last[8:] or "?"))
    if last == "denied":
        return FATAL, ("port %d du serveur tenu par un autre utilisateur. "
                       "Sur le serveur : sudo ss -ltnp 'sport = :%d'"
                       % (PORT, PORT))
    if last == "stuck":
        return RETRY, "port %d du serveur toujours occupé 3 s après l'arrêt" % PORT
    return RETRY, "réponse du serveur illisible"


def install_hint():
    return "sudo apt-get install -y adb"


def preflight(host, run=subprocess.run):
    """Verifie les deux bouts et libere le port du serveur. Bloquant
    (quelques secondes) : a lancer hors du fil de l'interface."""
    if not have_local():
        return FATAL, "adb absent de cette machine (paquet adb)"
    kw = dict(capture_output=True, text=True, stdin=subprocess.DEVNULL,
              creationflags=NO_WINDOW)
    try:
        local = version_of(run(["adb", "version"], timeout=10, **kw).stdout)
        # le serveur adb d'ici doit tourner avant que le serveur le cherche
        started = run(["adb", "start-server"], timeout=30, **kw)
    except (OSError, subprocess.SubprocessError) as e:
        return RETRY, "adb local : %s" % e
    if started.returncode != 0:
        lines = (started.stderr or "").strip().splitlines()
        return RETRY, "adb start-server : " + (lines[-1] if lines else "échec")
    try:
        remote = run(preflight_cmd(host), timeout=40, **kw)
    except (OSError, subprocess.SubprocessError) as e:
        return RETRY, "ssh vers %s : %s" % (host, e)
    if remote.returncode != 0:
        lines = (remote.stderr or "").strip().splitlines()
        return RETRY, lines[-1] if lines else "ssh vers %s impossible" % host
    return parse_preflight(remote.stdout, local)


def parse_beat(line):
    """Ligne du tunnel -> ('up',), ('devices', prets, non autorises),
    ('mute',) quand le serveur adb ne repond pas au bout du tunnel,
    ('refused',) quand le serveur a refuse le port, ou None."""
    line = line.strip()
    if line == "UP":
        return ("up",)
    if line == "X":
        return ("mute",)
    if FORWARD_FAILED in line:
        return ("refused",)
    m = re.match(r"D (\d+) (\d+)$", line)
    if m:
        return ("devices", int(m.group(1)), int(m.group(2)))
    return None


def backoff(attempt):
    """Attente avant la relance numero `attempt` (0 = la premiere)."""
    return BACKOFF_S[min(max(attempt, 0), len(BACKOFF_S) - 1)]


def describe(beat):
    """Battement -> (texte court pour l'interface, tout va bien)."""
    if beat[0] == "devices":
        ready, unauthorized = beat[1], beat[2]
        if ready:
            text = "%d téléphone%s visible%s du serveur" % (
                ready, "s" if ready > 1 else "", "s" if ready > 1 else "")
            return text, True
        if unauthorized:
            return "téléphone non autorisé : accepter le débogage USB sur l'écran", False
        return "tunnel ouvert, aucun téléphone branché ici", False
    if beat[0] == "mute":
        return "serveur adb local muet, relance", False
    return "tunnel ouvert", False


def main():
    if len(sys.argv) != 2 or sys.argv[1].startswith("-"):
        print("usage: vox_adb.py <host>", file=sys.stderr)
        return 2
    host = sys.argv[1]
    status, message = preflight(host)
    if status != OK:
        print("vox_adb: " + message, file=sys.stderr)
        return 1
    if message:
        print("vox_adb: " + message)
    print("vox_adb: tunnel adb vers %s (port %d). Ctrl-C pour couper." % (host, PORT))
    proc = subprocess.Popen(tunnel_cmd(host), stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True)
    last = None
    try:
        for line in proc.stdout:
            beat = parse_beat(line)
            if beat is None:
                print("  " + line.rstrip())
            elif beat[0] == "refused":
                print("vox_adb: le serveur a refusé le port %d" % PORT,
                      file=sys.stderr)
                proc.terminate()
            elif beat != last:
                last = beat
                print("  " + describe(beat)[0])
    except KeyboardInterrupt:
        proc.terminate()
    return proc.wait()


if __name__ == "__main__":
    sys.exit(main())
