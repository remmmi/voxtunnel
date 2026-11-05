#!/usr/bin/env bash
#
# voxtunnel.sh — streame le micro de la machine locale vers la carte son
# virtuelle (snd-aloop) d'un VPS distant, via SSH.
#
# A LANCER SUR LA MACHINE LOCALE (celle qui a le micro), pas sur le VPS.
#
#   ./voxtunnel.sh --check          verifie les deux bouts, ne streame pas
#   ./voxtunnel.sh                  streame jusqu'a Ctrl-C
#   ./voxtunnel.sh --tone           envoie un ton de test au lieu du micro
#
# Config par variables d'environnement :
#   VPS_HOST   user@host SSH               (obligatoire)
#   MIC        peripherique de capture     (defaut: default)
#   RATE       frequence d'echantillonnage (defaut: 48000)
#   BUFFER_US  tampon ALSA en us           (defaut: 80000)
#   PERIOD_US  periode ALSA en us          (defaut: 20000)
#   CODEC      auto | opus | pcm           (defaut: auto)
#              opus = Ogg Opus 24 kbit/s, trente fois moins de debit que le
#              PCM brut ; demande ffmpeg (libopus) ici et opusdec sur le VPS,
#              auto retombe en pcm s'il manque l'un des deux
#   SINK       face du loopback ou ecrire   (defaut: sonde, sinon
#              plughw:Loopback,1,0 ; les enregistreurs lisent default)
#
# Exemple :
#   VPS_HOST=user@my-vps ./voxtunnel.sh
#
# Si tu entends des micro-coupures (xruns), remonte les tampons :
#   BUFFER_US=200000 PERIOD_US=50000 VPS_HOST=... ./voxtunnel.sh
#
# Un seul flux par face du loopback : le preflight lit qui la tient (pid,
# depart, adresse du client SSH) ; --check le nomme, un stream l'arrete et
# prend la face. Le dernier flux lance garde le micro.
#
# Pas de relance automatique : si le pipe meurt, tu relances a la main.
# Une boucle de retry sans garde TTY dans un fichier de demarrage shell
# fabrique des shells orphelins qui respawnent en boucle.

set -euo pipefail

VPS_HOST="${VPS_HOST:-}"
MIC="${MIC:-default}"
RATE="${RATE:-48000}"

# Tampons ALSA, en microsecondes, appliques aux deux bouts.
# Les defauts d'ALSA (~500 ms) visent la lecture de musique : ils ajoutent
# une demi-seconde de latence a chaque extremite. 80/20 ms donne environ
# 150-200 ms bout en bout. Descendre plus bas provoque des xruns
# (micro-coupures), qui degradent la transcription bien plus qu'un delai.
BUFFER_US="${BUFFER_US:-80000}"
PERIOD_US="${PERIOD_US:-20000}"
CODEC="${CODEC:-auto}"

# Face playback du loopback cote VPS. Le prefixe plug: est obligatoire —
# snd-aloop ne resample pas tout seul, et le recorder distant ne demandera
# pas forcement la meme frequence que celle envoyee ici. snd-aloop croise
# ses deux devices : joue sur le 1, la voix ressort en capture sur le 0,
# qui est `default` sauf asoundrc personnalise. Sans SINK, le preflight
# ouvre default 0,3 s sur le VPS pour voir quel device le porte (meme
# sonde que vox_codec.py) et ecrit sur la face opposee.
REMOTE_SINK="${SINK:-}"
SINK_PROBE='arecord -D default -f S16_LE -c 1 -r 48000 -t raw -d 1 -q >/dev/null 2>&1 & p=$!; sleep 0.3; d=$(grep -l "owner_pid *: *$p\$" /proc/asound/Loopback/pcm[01]c/sub*/status 2>/dev/null | head -1); kill $p 2>/dev/null; case "$d" in *pcm0c*) echo C0;; *pcm1c*) echo C1;; *) echo "C?";; esac'

# Occupation de la face. snd-aloop n'accepte qu'un ecrivain par substream :
# tant qu'un autre flux la tient, aplay echoue en "Device or resource busy".
# Le noyau donne le proprietaire dans /proc/asound/Loopback/pcm<D>p/sub<S>/
# status (closed, ou owner_pid). Le preflight y lit le pid, dit qui c'est
# (commande, utilisateur, heure de depart, adresse du client SSH), et hors
# --check l'arrete pour prendre la face : le dernier flux lance garde le
# micro. Seul un aplay est arrete, c'est ce que lance tout client
# Voxtunnel ; un serveur de son qui tiendrait la face n'est pas touche.
# Variables attendues cote VPS : f = fichier status, k = 1 pour arreter.
SINK_GUARD='[ -r "$f" ] || exit 0
p=$(sed -n "s/^owner_pid *: *//p" "$f")
[ -n "$p" ] || { echo free; exit 0; }
c=$(ps -o comm= -p "$p" 2>/dev/null)
ip=$(tr "\0" "\n" 2>/dev/null <"/proc/$p/environ" | sed -n "s/^SSH_CONNECTION=\([^ ]*\).*/\1/p")
echo "busy pid $p ($c, $(ps -o user= -p "$p" 2>/dev/null)), depuis $(ps -o lstart= -p "$p" 2>/dev/null)${ip:+, venu de $ip}"
[ "$k" = 1 ] || exit 0
[ "$c" = aplay ] || { echo foreign; exit 0; }
kill "$p" 2>/dev/null || { echo denied; exit 0; }
i=0
while [ $i -lt 30 ]; do
  grep -q "^owner_pid *: *$p\$" "$f" 2>/dev/null || { echo freed; exit 0; }
  [ $i -eq 20 ] && kill -9 "$p" 2>/dev/null
  sleep 0.1; i=$((i+1))
done
echo stuck'

MODE="stream"
case "${1:-}" in
  --check) MODE="check" ;;
  --tone)  MODE="tone" ;;
  --help|-h)
    sed -n '2,37p' "$0" | sed 's/^# \{0,1\}//'
    exit 0 ;;
  "") ;;
  *) echo "argument inconnu: $1 (voir --help)" >&2; exit 2 ;;
esac

die() { echo "voxtunnel: $*" >&2; exit 1; }

[ -n "$VPS_HOST" ] || die "VPS_HOST n'est pas defini. Ex: VPS_HOST=user@ip $0"

# --- Choix de l'enregistreur local -----------------------------------------
# Les deux sortent du PCM brut signe 16 bits little-endian, mono.
if command -v arecord >/dev/null 2>&1; then
  RECORDER="arecord"
  record_cmd() {
    arecord -D "$MIC" -f S16_LE -c 1 -r "$RATE" -t raw -q \
            --buffer-time="$BUFFER_US" --period-time="$PERIOD_US"
  }
elif command -v ffmpeg >/dev/null 2>&1; then
  RECORDER="ffmpeg (pulse)"
  # fragment_size est en octets : RATE * periode * 2 octets par echantillon.
  FRAG=$(( RATE * PERIOD_US / 1000000 * 2 ))
  record_cmd() {
    ffmpeg -hide_banner -loglevel error \
           -f pulse -fragment_size "$FRAG" -i "$MIC" \
           -f s16le -ar "$RATE" -ac 1 - ;
  }
else
  die "aucun enregistreur trouve. Installe alsa-utils (arecord) ou ffmpeg."
fi

ssh_vps() { ssh -o BatchMode=yes -o ConnectTimeout=10 "$VPS_HOST" "$@"; }

# --- Encodeur Opus ----------------------------------------------------------
# Memes options que vox_codec.py (le tray) : a modifier ensemble. Sans
# nobuffer/probesize/blocksize, ffmpeg lit le pipe par blocs de 32 Ko et
# retient 340 ms d'audio avant la premiere trame.
# grep sans -q : avec pipefail, un grep -q qui s'arrete au premier match
# fait mourir ffmpeg en SIGPIPE et le pipeline compte comme un echec.
local_opus() {
  command -v ffmpeg >/dev/null 2>&1 \
    && ffmpeg -hide_banner -encoders 2>/dev/null | grep ' libopus ' >/dev/null
}
encode_cmd() {
  ffmpeg -hide_banner -loglevel error \
         -fflags nobuffer -probesize 32 -analyzeduration 0 \
         -blocksize $(( RATE * PERIOD_US / 1000000 * 2 )) \
         -f s16le -ar "$RATE" -ac 1 -i pipe:0 \
         -c:a libopus -b:a 24k -application voip -frame_duration 20 \
         -f ogg -page_duration 20000 -flush_packets 1 pipe:1
}

# Lecture distante vers la face playback du loopback. Compression SSH
# desactivee : le PCM brut ne se comprime pas et Opus l'est deja, gzip ne
# ferait qu'ajouter du delai. En opus, opusdec sort un WAV sans longueur
# qu'aplay lit quand meme (un avertissement "Cannot seek", sans effet).
remote_play() {
  local cmd
  if [ "$CODEC" = opus ]; then
    cmd="opusdec --quiet --force-wav --rate $RATE - - \
         | aplay -D $REMOTE_SINK -t wav -q \
                 --buffer-time=$BUFFER_US --period-time=$PERIOD_US"
  else
    cmd="aplay -D $REMOTE_SINK -f S16_LE -c 1 -r $RATE -t raw -q \
               --buffer-time=$BUFFER_US --period-time=$PERIOD_US"
  fi
  ssh -o BatchMode=yes -o ConnectTimeout=10 -o Compression=no -o IPQoS=lowdelay \
      "$VPS_HOST" "$cmd"
}

# Chaine complete : micro (ou stdin) -> [encodeur] -> ssh -> loopback.
send() {
  if [ "$CODEC" = opus ]; then
    encode_cmd | remote_play
  else
    remote_play
  fi
}

# --- Occupation de la face ---------------------------------------------------
# Sans effet quand SINK ne nomme pas un substream du loopback (D,S).
free_sink() {
  [[ "$REMOTE_SINK" =~ Loopback,([0-9]+),([0-9]+)$ ]] || return 0
  local take=1 out who
  [ "$MODE" = check ] && take=0
  out=$(ssh_vps "f=/proc/asound/Loopback/pcm${BASH_REMATCH[1]}p/sub${BASH_REMATCH[2]}/status; k=$take; $SINK_GUARD" 2>/dev/null) || true
  who=$(sed -n '1s/^busy //p' <<<"$out")
  case "$(tail -n 1 <<<"$out")" in
    free)    echo "  occupation         : libre" ;;
    freed)   echo "  occupation         : $who -> arrete, face libre" ;;
    busy*)   echo "  occupation         : $who"
             echo "  (un stream arretera ce processus pour prendre la face)" ;;
    foreign) die "face $REMOTE_SINK tenue par $who : ce n'est pas un aplay, je n'y touche pas." ;;
    denied)  die "face $REMOTE_SINK tenue par $who : arret refuse (autre utilisateur). Sur le VPS : sudo kill ${who#pid }" ;;
    stuck)   die "face $REMOTE_SINK tenue par $who : toujours occupee 3 s apres l'arret." ;;
  esac
}

# --- Preflight --------------------------------------------------------------
preflight() {
  echo "  enregistreur local : $RECORDER   (micro: $MIC, ${RATE} Hz)"

  ssh_vps true 2>/dev/null \
    || die "SSH vers $VPS_HOST impossible (BatchMode: il faut une cle, pas un mot de passe)."
  echo "  ssh $VPS_HOST      : ok"

  ssh_vps 'command -v aplay >/dev/null' \
    || die "aplay absent du VPS. Sur le VPS : sudo apt install alsa-utils"
  echo "  aplay distant      : ok"

  ssh_vps 'aplay -l 2>/dev/null | grep -q Loopback' \
    || die "carte Loopback absente du VPS. Sur le VPS : sudo modprobe snd-aloop"
  echo "  carte Loopback     : ok"

  if [ -z "$REMOTE_SINK" ]; then
    case "$(ssh_vps "$SINK_PROBE" 2>/dev/null)" in
      C1) REMOTE_SINK="plughw:Loopback,0,0" ;;
      *)  REMOTE_SINK="plughw:Loopback,1,0" ;;
    esac
  fi
  echo "  face loopback      : $REMOTE_SINK"
  free_sink

  case "$CODEC" in
    auto)
      CODEC=pcm
      if ! local_opus; then
        echo "  (opus : ffmpeg avec libopus absent en local -> pcm brut)"
      elif ssh_vps 'command -v opusdec >/dev/null'; then
        CODEC=opus
      else
        echo "  (opus : 24 kbit/s au lieu de 768, sur le VPS : sudo apt install opus-tools)"
      fi ;;
    opus)
      local_opus || die "ffmpeg avec libopus absent en local (CODEC=opus)."
      ssh_vps 'command -v opusdec >/dev/null' \
        || die "opusdec absent du VPS. Sur le VPS : sudo apt install opus-tools" ;;
    pcm) ;;
    *) die "CODEC=$CODEC inconnu (auto, opus ou pcm)" ;;
  esac
  if [ "$CODEC" = opus ]; then
    echo "  codec              : opus 24 kbit/s"
  else
    echo "  codec              : pcm brut ($(( RATE * 16 / 1000 )) kbit/s)"
  fi
}

case "$MODE" in
  check)
    echo "voxtunnel — verification"
    preflight
    echo
    echo "Les deux bouts repondent. Lance sans --check pour streamer."
    ;;

  tone)
    echo "voxtunnel — ton de test (1 kHz, 5 s) vers $VPS_HOST"
    preflight
    echo
    echo "Sur le VPS, en parallele :"
    echo "  arecord -D default -f S16_LE -c1 -r$RATE -d 5 /tmp/loop.wav"
    echo
    command -v sox >/dev/null 2>&1 \
      || die "sox absent en local (necessaire pour --tone). Utilise --check a la place."
    sox -n -t raw -r "$RATE" -e signed -b 16 -c 1 - synth 5 sine 1000 vol 0.3 \
      | send
    echo "Ton envoye."
    ;;

  stream)
    echo "voxtunnel — micro local vers $VPS_HOST"
    preflight
    echo
    echo "Streaming $CODEC (tampon ${BUFFER_US}us, periode ${PERIOD_US}us). Ctrl-C pour couper."
    echo "Marque un temps apres avoir declenche /voice : le tuyau contient"
    echo "deja ~0,2 s d'audio, sinon tu perds ta premiere syllabe."
    record_cmd | send
    ;;
esac
