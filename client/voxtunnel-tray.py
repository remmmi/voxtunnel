#!/usr/bin/env python3
#
# voxtunnel.py — petite app tray/fenetre pour piloter voxtunnel.sh
# vers les VPS decouverts dans ~/.ssh/config.
#
# - fenetre : un voyant + interrupteur maitre "Transmission", puis une ligne
#             par host avec un interrupteur on/off et un statut
# - tray    : icone dediee dans la barre KDE, clic droit = menu avec les memes
#             interrupteurs, clic gauche = afficher/masquer la fenetre
#
# Les interrupteurs par host expriment l'etat SOUHAITE ; l'interrupteur
# maitre "Transmission" coupe/retablit les liaisons sans changer cet etat :
# OFF -> voyant gris, tous les streams coupes, les interrupteurs restent tels
# quels ; ON -> voyant vert, les liaisons des hosts ON sont retablies.
#
# Decouverte : tous les blocs "Host" de ~/.ssh/config qui declarent un
# IdentityFile, hors patterns a jokers et hors liste d'exclusion
# (~/.config/voxtunnel/ignore, un host par ligne, github.com exclu d'office).
#
# Chaque stream est un process voxtunnel.sh lance dans son propre groupe ;
# l'arret envoie SIGTERM au groupe entier (arecord | ssh compris).
# Pas de relance automatique : un stream mort repasse son interrupteur a OFF
# et affiche l'erreur.

import os
import queue
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time

__version__ = "1.3.0"

# Linux est la plateforme de reference (testee sur du vrai materiel).
# macOS et Windows sont EXPERIMENTAUX : valides uniquement en CI,
# jamais sur une vraie machine. Voir client-macos/ et client-windows/.
IS_LINUX = sys.platform.startswith("linux")
IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform.startswith("win")

from PyQt5.QtCore import (
    QLockFile, QObject, QRectF, QSize, Qt, QTimer, QUrl, pyqtSignal,
)
from PyQt5.QtGui import QColor, QDesktopServices, QIcon, QPainter, QPixmap
from PyQt5.QtWidgets import (
    QApplication, QCheckBox, QFrame, QHBoxLayout, QLabel, QMenu, QSlider,
    QSystemTrayIcon, QToolButton, QVBoxLayout, QWidget,
)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import vox_buffer  # noqa: E402  (module voisin : sonde et tampon auto)
import vox_update  # noqa: E402  (module voisin : mises a jour)
import vox_codec   # noqa: E402  (module voisin : pcm ou opus)

VOICEPIPE = os.path.join(SCRIPT_DIR, "voxtunnel.sh")
ICON_DIR = os.path.join(SCRIPT_DIR, "icons")
LOG_DIR = os.path.join(os.path.expanduser("~"), ".cache", "voxtunnel")
IGNORE_FILE = os.path.join(os.path.expanduser("~"), ".config", "voxtunnel", "ignore")
DEFAULT_IGNORE = {"github.com"}

OFF, STARTING, ON, ERROR = "off", "starting", "on", "error"

GREEN, GRAY, ORANGE, RED = "#4caf50", "#9e9e9e", "#e6a23c", "#f44336"

# tampon ALSA reglable depuis la fenetre (voir l'en-tete de voxtunnel.sh),
# ou choisi par host en mode auto (voir vox_buffer.py)
BUFFER_MIN_MS = vox_buffer.BUFFER_MIN_MS
BUFFER_DEFAULT_MS = vox_buffer.BUFFER_DEFAULT_MS
BUFFER_MAX_MS = vox_buffer.BUFFER_MAX_MS

# en mode auto, une mesure du lien plus vieille que ca est refaite avant
# de relancer un stream ; en deca le stream repart sans attendre la sonde
PROBE_TTL_S = 600

# verification des mises a jour : peu apres le lancement, puis une fois par jour
UPDATE_FIRST_MS = 3000
UPDATE_EVERY_MS = 24 * 3600 * 1000

# verrou d'instance unique, relache avant de relancer l'app apres une
# mise a jour
INSTANCE_LOCK = None

# Etat de l'ecoute cote VPS. R = pret (carte Loopback en place),
# RL = pret et une capture est ouverte en ce moment (un substream pcm0c
# non "closed"), N = pas de Loopback. Host injoignable = ssh en erreur.
# Une ligne V=<version> suit quand le paquet voxtunnel-server est installe,
# et une ligne O quand opusdec est la (voir vox_update.parse_listen).
LISTEN_CMD = ("if [ -d /proc/asound/Loopback ]; then "
              "grep -L closed /proc/asound/Loopback/pcm0c/sub*/status "
              "2>/dev/null | grep -q . && echo RL || echo R; "
              "else echo N; fi; "
              "dpkg-query -W -f='V=${Version}\\n' voxtunnel-server "
              "2>/dev/null; command -v opusdec >/dev/null 2>&1 && echo O; "
              "true")

# micro pour les backends mac/windows : fichier optionnel avec le nom
# (dshow) ou l'index avfoundation (":1") du peripherique
MIC_FILE = os.path.join(os.path.expanduser("~"), ".config", "voxtunnel", "mic")

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def mic_device():
    try:
        with open(MIC_FILE) as f:
            return f.read().strip() or None
    except OSError:
        return None


def detect_dshow_mic():
    """Premier peripherique audio DirectShow annonce par ffmpeg."""
    try:
        out = subprocess.run(
            ["ffmpeg", "-hide_banner", "-list_devices", "true",
             "-f", "dshow", "-i", "dummy"],
            capture_output=True, text=True, timeout=10,
            creationflags=NO_WINDOW).stderr
    except OSError:
        return None
    for line in out.splitlines():
        m = re.search(r'"([^"]+)"\s*\(audio\)', line)
        if m:
            return m.group(1)
    return None


def capture_cmd(codec="pcm"):
    """Capture micro -> stdout (mac/windows) : PCM s16le mono 48 kHz, ou
    Ogg Opus quand codec vaut "opus". Sur Linux c'est voxtunnel.sh qui
    s'en charge."""
    out = (vox_codec.opus_output_args(48000) if codec == "opus"
           else ["-f", "s16le", "-ar", "48000", "-ac", "1", "-"])
    if IS_MAC:
        return ["ffmpeg", "-hide_banner", "-loglevel", "error",
                "-f", "avfoundation", "-i", mic_device() or ":0",
                ] + out
    if IS_WIN:
        dev = mic_device() or detect_dshow_mic()
        if not dev:
            raise ValueError("aucun micro dshow trouve ; ecrire son nom "
                             "exact dans " + MIC_FILE)
        return ["ffmpeg", "-hide_banner", "-loglevel", "error",
                "-f", "dshow", "-audio_buffer_size", "20",
                "-i", "audio=" + dev,
                ] + out
    raise ValueError("pas de backend de capture pour cette plateforme")


def ssh_play_cmd(host, buffer_ms, codec="pcm"):
    """Lecture distante vers le loopback, memes reglages que voxtunnel.sh."""
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
            "-o", "Compression=no", "-o", "IPQoS=lowdelay", host,
            vox_codec.remote_cmd(codec, 48000, buffer_ms * 1000,
                                 vox_buffer.period_us(buffer_ms))]


def discover_hosts():
    """Hosts de ~/.ssh/config avec IdentityFile, hors jokers et exclusions."""
    ignore = set(DEFAULT_IGNORE)
    # l'ancien emplacement (~/.config/voicepipe/ignore) reste lu en repli
    legacy = os.path.join(os.path.expanduser("~"), ".config", "voicepipe", "ignore")
    for path in (IGNORE_FILE, legacy):
        try:
            with open(path) as f:
                ignore |= {l.strip() for l in f
                           if l.strip() and not l.startswith("#")}
        except OSError:
            pass

    hosts, current, has_key = [], [], False
    try:
        with open(os.path.expanduser("~/.ssh/config")) as f:
            lines = f.readlines()
    except OSError:
        return []

    def flush():
        if has_key:
            for h in current:
                if "*" not in h and "?" not in h and h not in ignore:
                    hosts.append(h)

    for line in lines:
        parts = line.split("#", 1)[0].split()
        if not parts:
            continue
        key = parts[0].lower()
        if key == "host":
            flush()
            current, has_key = parts[1:], False
        elif key == "identityfile":
            has_key = True
    flush()
    return hosts


class StreamManager(QObject):
    """Un process voxtunnel.sh par host actif, surveille par un timer."""

    state_changed = pyqtSignal(str, str, str)  # host, etat, message
    buffer_wanted = pyqtSignal(str, int)       # host, tampon a appliquer (ms)

    def __init__(self):
        super().__init__()
        self.procs = {}      # host -> liste de Popen (pipeline complet)
        self.started = {}    # host -> horodatage du lancement
        self.stopping = set()
        self.buffer_ms = BUFFER_DEFAULT_MS   # tampon manuel (slider)
        self.auto = True     # tampon choisi et surveille par host
        self.buffers = {}    # host -> tampon du stream en cours (ms)
        self.codecs = {}     # host -> codec du stream en cours
        self.watchers = {}   # host -> vox_buffer.Watcher
        self.log_pos = {}    # host -> octets du log deja lus
        os.makedirs(LOG_DIR, exist_ok=True)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll)
        self.timer.start(1000)

    def log_path(self, host):
        return os.path.join(LOG_DIR, host + ".log")

    def is_running(self, host):
        return host in self.procs

    def is_unstable(self, host):
        watcher = self.watchers.get(host)
        return bool(watcher and watcher.unstable)

    def start(self, host, buffer_ms=None, codec="pcm"):
        if host in self.procs:
            return
        ms = buffer_ms or self.buffer_ms
        log = open(self.log_path(host), "wb", buffering=0)
        try:
            if IS_LINUX:
                # chemin de reference : le script fait tout, dans son
                # propre groupe de process
                env = dict(os.environ, VPS_HOST=host, CODEC=codec,
                           BUFFER_US=str(ms * 1000),
                           PERIOD_US=str(vox_buffer.period_us(ms)))
                procs = [subprocess.Popen(
                    [VOICEPIPE], env=env, stdout=log,
                    stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                    start_new_session=True)]
            else:
                # mac/windows : pipeline capture | ssh monte ici meme
                flags = {"creationflags": NO_WINDOW} if IS_WIN else {}
                cap = subprocess.Popen(
                    capture_cmd(codec), stdout=subprocess.PIPE, stderr=log,
                    stdin=subprocess.DEVNULL, **flags)
                play = subprocess.Popen(
                    ssh_play_cmd(host, ms, codec), stdin=cap.stdout,
                    stdout=log, stderr=subprocess.STDOUT, **flags)
                cap.stdout.close()
                procs = [cap, play]
        except (OSError, ValueError) as e:
            log.close()
            self.state_changed.emit(host, ERROR, str(e))
            return
        log.close()
        self.procs[host] = procs
        self.started[host] = time.monotonic()
        self.buffers[host] = ms
        self.codecs[host] = codec
        self.watchers[host] = vox_buffer.Watcher(ms)
        self.log_pos[host] = 0
        self.state_changed.emit(host, STARTING, "")

    def _terminate(self, proc):
        try:
            if IS_LINUX:
                os.killpg(proc.pid, signal.SIGTERM)
            else:
                proc.terminate()
        except (ProcessLookupError, PermissionError, OSError):
            pass

    def stop(self, host):
        procs = self.procs.get(host)
        if not procs:
            return
        self.stopping.add(host)
        for proc in procs:
            self._terminate(proc)

    def stop_all(self):
        for host in list(self.procs):
            self.stop(host)

    def _last_log_line(self, host):
        try:
            with open(self.log_path(host), errors="replace") as f:
                lines = [l.strip() for l in f if l.strip()]
            return lines[-1] if lines else "arret sans message"
        except OSError:
            return "pas de log"

    def _watch(self, host):
        """Donne au Watcher du host les lignes du log arrivees depuis la
        derniere lecture ; signale le tampon a appliquer s'il faut monter."""
        try:
            with open(self.log_path(host), "rb") as f:
                f.seek(self.log_pos[host])
                data = f.read()
        except OSError:
            return
        self.log_pos[host] += len(data)
        if not self.auto:
            return
        ms = self.watchers[host].feed(data.decode(errors="replace"),
                                      time.monotonic())
        if ms:
            self.buffer_wanted.emit(host, ms)

    def _poll(self):
        for host, procs in list(self.procs.items()):
            # le dernier maillon (ssh sur mac/win, le script sur linux)
            # fait foi pour la vie du stream
            if procs[-1].poll() is not None:
                for proc in procs:
                    if proc.poll() is None:
                        self._terminate(proc)
                del self.procs[host]
                self.started.pop(host, None)
                self.buffers.pop(host, None)
                self.watchers.pop(host, None)
                self.log_pos.pop(host, None)
                if host in self.stopping:
                    self.stopping.discard(host)
                    self.state_changed.emit(host, OFF, "")
                else:
                    self.state_changed.emit(host, ERROR, self._last_log_line(host))
                continue
            self._watch(host)
            if host in self.stopping:
                continue
            if IS_LINUX:
                # passe de "connexion" a "actif" quand le preflight est franchi
                try:
                    with open(self.log_path(host), errors="replace") as f:
                        if "Streaming" in f.read():
                            self.state_changed.emit(host, ON, "")
                except OSError:
                    pass
            else:
                # pas de preflight sur mac/win : actif = pipeline toujours
                # vivant apres quelques secondes
                if time.monotonic() - self.started.get(host, 0) > 2.5:
                    self.state_changed.emit(host, ON, "")


class ListenerChecker(QObject):
    """Verifie periodiquement en SSH si une ecoute est ouverte sur le
    loopback de chaque host (un process qui enregistre Loopback,0,0).
    Tout est non bloquant : un ssh par host, ramasse par un timer."""

    # host, pret, capture ouverte, version du paquet serveur ('' = aucun),
    # decodeur opus present
    result = pyqtSignal(str, bool, bool, str, bool)

    SLOW_MS = 30000   # tous les hosts
    FAST_MS = 3000    # hosts avec un stream actif : l'ecoute distante peut
                      # s'ouvrir et se fermer vite (mode appui par exemple)

    def __init__(self, hosts):
        super().__init__()
        self.hosts = hosts
        self.active = set()  # hosts a surveiller de pres (streams en cours)
        self.pending = {}  # host -> Popen
        self.collect_timer = QTimer(self)
        self.collect_timer.timeout.connect(self._collect)
        self.collect_timer.start(500)
        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self.refresh)
        self.refresh_timer.start(self.SLOW_MS)
        self.fast_timer = QTimer(self)
        self.fast_timer.timeout.connect(lambda: self._launch(self.active))
        self.fast_timer.start(self.FAST_MS)
        self.refresh()

    def refresh(self):
        self._launch(self.hosts)

    def _launch(self, hosts):
        for host in hosts:
            if host in self.pending:
                continue
            try:
                self.pending[host] = subprocess.Popen(
                    ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                     host, LISTEN_CMD],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL)
            except OSError:
                self.result.emit(host, False, False, "", False)

    def _collect(self):
        for host, proc in list(self.pending.items()):
            if proc.poll() is None:
                continue
            del self.pending[host]
            out = proc.stdout.read() if proc.stdout else b""
            ready, capture_open, version, opus = vox_update.parse_listen(out)
            self.result.emit(host, proc.returncode == 0 and ready,
                             capture_open, version, opus)


class ToggleSwitch(QCheckBox):
    """QCheckBox peinte en forme d'interrupteur."""

    def __init__(self):
        super().__init__()
        self.setCursor(Qt.PointingHandCursor)

    def sizeHint(self):
        return QSize(44, 24)

    def hitButton(self, pos):
        return self.contentsRect().contains(pos)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        track = QColor(GREEN) if self.isChecked() else QColor(GRAY)
        if not self.isEnabled():
            track = QColor("#c0c0c0")
        w, h = 44, 24
        p.setBrush(track)
        p.drawRoundedRect(QRectF(0, 0, w, h), h / 2, h / 2)
        p.setBrush(QColor("#ffffff"))
        x = w - h + 2 if self.isChecked() else 2
        p.drawEllipse(QRectF(x, 2, h - 4, h - 4))


class Voyant(QLabel):
    """Petit rond de couleur (vert = transmission, gris = coupee)."""

    def __init__(self):
        super().__init__()
        self.setFixedSize(14, 14)
        self.set_on(True)

    def set_on(self, on):
        color = GREEN if on else GRAY
        self.setStyleSheet("border-radius: 7px; background: %s;" % color)


class StatusDot(QLabel):
    """Petit ordinateur devant le nom du VPS : vert = ecoute en etat de
    marche (host joignable, carte Loopback en place), rouge = injoignable
    ou sans Loopback, gris = pas encore verifie."""

    def __init__(self):
        super().__init__()
        self.setFixedSize(15, 15)
        self.set_state(None, False)

    @staticmethod
    def _computer_pixmap(color):
        # silhouette de moniteur : ecran, pied, socle
        pix = QPixmap(15, 15)
        pix.fill(Qt.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(color))
        p.drawRoundedRect(QRectF(1, 1.5, 13, 8.5), 1.5, 1.5)
        p.drawRect(QRectF(6.5, 10, 2, 2))
        p.drawRoundedRect(QRectF(4, 12, 7, 1.8), 0.9, 0.9)
        p.end()
        return pix

    def set_state(self, ready, capture_open):
        color = GRAY if ready is None else (GREEN if ready else RED)
        self.setPixmap(self._computer_pixmap(color))
        if ready is None:
            tip = "écoute distante : vérification..."
        elif not ready:
            tip = "écoute distante : injoignable ou sans carte Loopback"
        elif capture_open:
            tip = "écoute distante : prête, capture ouverte en ce moment"
        else:
            tip = "écoute distante : prête"
        self.setToolTip(tip)


class HostRow(QWidget):
    def __init__(self, host, toggle_cb, server_cb):
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 2, 8, 2)
        self.dot = StatusDot()
        layout.addWidget(self.dot)
        self.name = QLabel(host)
        self.switch = ToggleSwitch()
        self.status = QLabel("inactif")
        self.status.setStyleSheet("color: %s;" % GRAY)
        # erreurs copiables : selection a la souris, clic droit > Copier
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.name)
        layout.addWidget(self.switch)
        layout.addWidget(self.status, 1)
        # visible seulement sur une erreur
        self.copy = QToolButton()
        self.copy.setText("copier")
        self.copy.setToolTip("Copier le message d'erreur")
        self.copy.clicked.connect(
            lambda: QApplication.clipboard().setText(self.status.text()))
        self.copy.hide()
        layout.addWidget(self.copy)
        # version du paquet serveur, avec un lien quand une release la depasse
        self.server = QLabel()
        self.server.setStyleSheet("font-size: 10px; color: %s;" % GRAY)
        self.server.setTextInteractionFlags(
            Qt.TextSelectableByMouse | Qt.LinksAccessibleByMouse)
        self.server.linkActivated.connect(lambda href: server_cb(host, href))
        layout.addWidget(self.server)
        self.switch.toggled.connect(lambda on: toggle_cb(host, on))

    def set_checked(self, checked):
        self.switch.blockSignals(True)
        self.switch.setChecked(checked)
        self.switch.blockSignals(False)

    def set_status(self, text, color):
        self.status.setText(text)
        self.status.setStyleSheet("color: %s;" % color)
        self.copy.setVisible(color == RED)


class MainWindow(QWidget):
    def __init__(self, hosts, toggle_cb, master_cb, buffer_cb, auto_cb,
                 update_cb, server_cb):
        super().__init__()
        self.buffer_cb = buffer_cb
        self.auto_cb = auto_cb
        self.setWindowTitle("Voxtunnel")
        layout = QVBoxLayout(self)

        master = QHBoxLayout()
        master.setContentsMargins(8, 2, 8, 6)
        self.voyant = Voyant()
        label = QLabel("Transmission")
        label.setStyleSheet("font-weight: bold;")
        self.master_switch = ToggleSwitch()
        self.master_switch.setChecked(True)
        self.master_switch.toggled.connect(master_cb)
        master.addWidget(self.voyant)
        master.addWidget(label)
        master.addStretch(1)
        master.addWidget(self.master_switch)
        layout.addLayout(master)

        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Sunken)
        layout.addWidget(line)

        self.rows = {}
        if not hosts:
            layout.addWidget(QLabel("Aucun host avec clé trouvé dans ~/.ssh/config"))
        for host in hosts:
            row = HostRow(host, toggle_cb, server_cb)
            self.rows[host] = row
            layout.addWidget(row)
        if hosts:
            # colonne des noms calee sur le plus long : les interrupteurs
            # restent alignes quelle que soit la longueur des noms
            fm = self.fontMetrics()
            name_w = max(160, max(fm.boundingRect(h).width() for h in hosts) + 12)
            for row in self.rows.values():
                row.name.setFixedWidth(name_w)
        layout.addStretch(1)

        # tampon ALSA, en bas. Auto : mesure par host au demarrage, puis
        # montee sur underruns. Sinon le slider : plus haut = moins de
        # coupures, plus de latence ; applique aux streams en cours et suivants
        buf = QHBoxLayout()
        buf.setContentsMargins(8, 6, 8, 2)
        self.buffer_label = QLabel()
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(BUFFER_MIN_MS, BUFFER_MAX_MS)
        self.slider.setValue(BUFFER_DEFAULT_MS)
        self.slider.setSingleStep(10)
        self.slider.setPageStep(50)
        self.slider.setEnabled(False)
        self.slider.setToolTip(
            "Buffer audio : plus haut = moins de micro-coupures,\n"
            "plus de latence. Les streams en cours sont relancés.")
        self.slider.valueChanged.connect(self._on_slider_changed)
        self.slider.sliderReleased.connect(
            lambda: self.buffer_cb(self.slider.value()))
        self.auto_box = QCheckBox("Auto")
        self.auto_box.setChecked(True)
        self.auto_box.setToolTip(
            "Auto : le buffer de chaque host est mesuré au démarrage,\n"
            "puis monte tout seul si le lien décroche.")
        self.auto_box.toggled.connect(self._on_auto_toggled)
        buf.addWidget(QLabel("Buffer"))
        buf.addWidget(self.auto_box)
        buf.addWidget(self.slider, 1)
        buf.addWidget(self.buffer_label)
        layout.addLayout(buf)
        self._on_slider_changed(self.slider.value())

        # annonce d'une release plus recente, vide tant qu'il n'y en a pas
        self.update_label = QLabel()
        self.update_label.setWordWrap(True)
        self.update_label.setStyleSheet("font-size: 10px; margin: 0 8px;")
        self.update_label.linkActivated.connect(update_cb)
        self.update_label.hide()
        layout.addWidget(self.update_label)

        link = QLabel('v%s — <a href="https://github.com/remmmi/voxtunnel" '
                      'style="color: %s;">voxtunnel</a>'
                      % (__version__, GRAY))
        link.setOpenExternalLinks(True)
        link.setAlignment(Qt.AlignRight)
        link.setStyleSheet("font-size: 10px; margin-right: 8px;")
        layout.addWidget(link)

    def _on_auto_toggled(self, on):
        self.slider.setEnabled(not on)
        self._on_slider_changed(self.slider.value())
        self.auto_cb(on)

    def _on_slider_changed(self, value):
        self.buffer_label.setText(
            "auto" if self.auto_box.isChecked() else "%d ms" % value)
        # au clavier il n'y a pas de sliderReleased : applique directement
        if not self.slider.isSliderDown():
            self.buffer_cb(value)

    def set_update(self, html):
        self.update_label.setText(html)
        self.update_label.setVisible(bool(html))

    def set_voyant(self, on):
        self.voyant.set_on(on)
        self.master_switch.blockSignals(True)
        self.master_switch.setChecked(on)
        self.master_switch.blockSignals(False)

    def closeEvent(self, event):
        # la fenetre se cache, l'app vit dans le tray
        event.ignore()
        self.hide()


class VoiceTrayApp:
    def __init__(self, app):
        self.app = app
        self.hosts = discover_hosts()
        self.manager = StreamManager()
        self.desired = set()       # hosts dont l'interrupteur est ON
        self.transmitting = True   # etat du voyant / interrupteur maitre
        self.restart_pending = set()  # a relancer des que leur arret est acte
        self.host_buffer = {}      # host -> (tampon auto en ms, instant
                                   #          mesure, codec de la mesure)
        self.host_opus = {}        # host -> opusdec present (sonde d'ecoute)
        self.probing = set()       # hosts dont la sonde est en vol
        self.probe_results = queue.Queue()  # (host, allers-retours en ms)
        self.probe_timer = QTimer()
        self.probe_timer.timeout.connect(self._collect_probes)
        self.probe_timer.start(200)

        self.release = None        # derniere release GitHub connue
        self.server_versions = {}  # host -> version du paquet serveur
        self.updating = set()      # "client" ou host : installation en vol
        self.installed = False     # client mis a jour, relance attendue
        self.update_results = queue.Queue()
        self.probe_timer.timeout.connect(self._collect_updates)
        self.update_timer = QTimer()
        self.update_timer.timeout.connect(self._check_update)
        self.update_timer.start(UPDATE_EVERY_MS)
        QTimer.singleShot(UPDATE_FIRST_MS, self._check_update)

        self.window = MainWindow(self.hosts, self.toggle_host,
                                 self.set_transmitting, self.set_buffer,
                                 self.set_auto, self._on_update_link,
                                 self.update_server)

        self.checker = ListenerChecker(self.hosts)
        self.checker.result.connect(self._on_listener_result)

        self.icon_on = self._load_icon("voxtunnel-on.svg")
        self.icon_idle = self._load_icon("voxtunnel-idle.svg")
        self.icon_off = self._load_icon("voxtunnel-off.svg")
        self.window.setWindowIcon(self.icon_on)

        self.tray = QSystemTrayIcon(self.icon_idle)
        self.menu = QMenu()
        self.master_action = self.menu.addAction("Transmission")
        self.master_action.setCheckable(True)
        self.master_action.setChecked(True)
        self.master_action.toggled.connect(self.set_transmitting)
        self.menu.addSeparator()
        self.actions = {}
        for host in self.hosts:
            act = self.menu.addAction(host)
            act.setCheckable(True)
            act.toggled.connect(lambda on, h=host: self.toggle_host(h, on))
            self.actions[host] = act
        self.menu.addSeparator()
        self.update_action = self.menu.addAction("", self.update_client)
        self.update_action.setVisible(False)
        self.menu.addAction("Ouvrir la fenêtre", self._show_window)
        self.menu.addAction("Quitter", self._quit)
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

        self.manager.state_changed.connect(self._on_state_changed)
        self.manager.buffer_wanted.connect(self._on_buffer_wanted)
        self._update_tray()

    @staticmethod
    def _load_icon(filename):
        # pre-rend le SVG en pixmaps : une QIcon purement SVG arrive
        # transparente dans le tray Plasma (transfert DBus sans tailles)
        src = QIcon(os.path.join(ICON_DIR, filename))
        icon = QIcon()
        for size in (16, 22, 24, 32, 48, 64):
            icon.addPixmap(src.pixmap(size, size))
        return icon

    # --- logique d'etat -----------------------------------------------------

    def toggle_host(self, host, on):
        if on:
            self.desired.add(host)
            if self.transmitting:
                self._start_stream(host)
            else:
                self._set_host_ui(host, True, "suspendu", ORANGE)
        else:
            self.desired.discard(host)
            if self.manager.is_running(host):
                self.manager.stop(host)
            else:
                self._set_host_ui(host, False, "inactif", GRAY)
        self._update_tray()

    def _start_stream(self, host):
        """Lance le stream ; en mode auto, sonde d'abord le lien si la
        derniere mesure du host est absente ou trop vieille."""
        if not self.manager.auto:
            self.manager.start(host, codec=self._codec(host))
            return
        codec = self._codec(host)
        known = self.host_buffer.get(host)
        if known and known[2] != codec:
            # un tampon mesure en pcm ne dit rien du lien en opus (ni
            # l'inverse) : on repart de zero, sans palier de descente
            del self.host_buffer[host]
            known = None
        if known and time.monotonic() - known[1] < PROBE_TTL_S:
            self.manager.start(host, known[0], codec)
            return
        self._set_host_ui(host, True, "mesure du lien...", ORANGE)
        if host in self.probing:
            return
        self.probing.add(host)
        threading.Thread(
            target=lambda: self.probe_results.put(
                (host, codec, vox_buffer.probe(
                    host, block=vox_buffer.probe_block(codec)))),
            daemon=True).start()

    def _collect_probes(self):
        while True:
            try:
                host, codec, rtts = self.probe_results.get_nowait()
            except queue.Empty:
                return
            self.probing.discard(host)
            previous = self.host_buffer.get(host, (None,))[0]
            ms = vox_buffer.recommend(rtts, previous)
            if ms:
                self.host_buffer[host] = (ms, time.monotonic(), codec)
            # sonde en echec : le stream part quand meme, son preflight
            # affichera la vraie erreur
            if self.transmitting and host in self.desired:
                self.manager.start(host, ms if self.manager.auto else None,
                                   codec)
                self._update_tray()

    def _restart(self, host):
        self.restart_pending.add(host)
        self.manager.stop(host)

    def _on_buffer_wanted(self, host, ms):
        # trop d'underruns : le stream repart avec un tampon plus grand
        self.host_buffer[host] = (ms, time.monotonic(),
                                  self.manager.codecs.get(host, "pcm"))
        self._restart(host)

    def set_auto(self, on):
        self.manager.auto = on
        if on:
            # les streams en cours gardent leur tampon, la surveillance reprend
            return
        for host, ms in list(self.manager.buffers.items()):
            if ms != self.manager.buffer_ms:
                self._restart(host)

    def set_buffer(self, ms):
        if ms == self.manager.buffer_ms:
            return
        self.manager.buffer_ms = ms
        if self.manager.auto:
            return
        # relance les streams en cours pour appliquer le nouveau tampon
        for host in list(self.manager.procs):
            self._restart(host)

    def _codec(self, host):
        """Opus si la sonde d'ecoute a vu opusdec et que ffmpeg encode ici,
        sinon le PCM brut d'origine (un stream deja lance garde son codec
        jusqu'a son prochain lancement)."""
        return vox_codec.choose(self.host_opus.get(host, False),
                                vox_codec.have_local_opus())

    def _on_listener_result(self, host, ready, capture_open, version, opus):
        row = self.window.rows.get(host)
        if row:
            row.dot.set_state(ready, capture_open)
        if ready:
            self.host_opus[host] = opus
            self.server_versions[host] = version
            self._refresh_server_ui(host)

    # --- mises a jour --------------------------------------------------------

    def _check_update(self):
        threading.Thread(
            target=lambda: self.update_results.put(
                ("release", vox_update.fetch_latest())),
            daemon=True).start()

    def _collect_updates(self):
        while True:
            try:
                result = self.update_results.get_nowait()
            except queue.Empty:
                return
            if result[0] == "release":
                if result[1] is not None:
                    self.release = result[1]
                    self._refresh_client_ui()
                    for host in self.server_versions:
                        self._refresh_server_ui(host)
            elif result[0] == "client":
                self._on_client_updated(*result[1:])
            else:
                self._on_server_updated(*result[1:])

    def _client_update(self):
        """Release a proposer pour ce client, ou None."""
        rel = self.release
        if rel and vox_update.is_newer(rel.version, __version__):
            return rel
        return None

    def _refresh_client_ui(self):
        rel = self._client_update()
        if not rel or self.installed or "client" in self.updating:
            return
        one_click = (vox_update.is_deb_install(SCRIPT_DIR)
                     and "voxtunnel" in rel.assets)
        action = "installer" if one_click else "voir la release"
        notes = " : " + rel.notes if rel.notes else ""
        self.window.set_update(
            'v%s disponible%s - <a href="#install" style="color: %s;">%s</a>'
            % (rel.version, notes, GREEN, action))
        self.update_action.setText("%s v%s" % (action.capitalize(), rel.version))
        self.update_action.setVisible(True)

    def _on_update_link(self, href):
        if href == "#restart":
            self._relaunch()
        else:
            self.update_client()

    def update_client(self):
        rel = self._client_update()
        if not rel or "client" in self.updating:
            return
        asset = rel.assets.get("voxtunnel")
        if not asset or not vox_update.is_deb_install(SCRIPT_DIR):
            QDesktopServices.openUrl(QUrl(rel.page_url))
            return
        if self.manager.procs:
            # l'installation remplace les fichiers du stream en cours
            self.tray.showMessage(
                "Voxtunnel", "Coupe la Transmission avant d'installer v%s."
                % rel.version, QSystemTrayIcon.Information, 6000)
            return
        self.updating.add("client")
        self.update_action.setVisible(False)
        self.window.set_update("installation de v%s..." % rel.version)

        def job():
            try:
                deb = vox_update.download(asset, LOG_DIR)
                run = subprocess.run(vox_update.install_cmd(deb),
                                     capture_output=True, text=True)
                if run.returncode in (126, 127):
                    message = "installation annulée (mot de passe refusé)"
                else:
                    lines = (run.stderr or run.stdout).strip().splitlines()
                    message = lines[-1] if lines else ""
                self.update_results.put(
                    ("client", run.returncode == 0, rel.version, message))
            except (vox_update.UpdateError, OSError) as e:
                self.update_results.put(("client", False, rel.version, str(e)))

        threading.Thread(target=job, daemon=True).start()

    def _on_client_updated(self, ok, version, message):
        self.updating.discard("client")
        if ok:
            self.installed = True
            self.window.set_update(
                'v%s installée - <a href="#restart" style="color: %s;">'
                'relancer</a>' % (version, GREEN))
            self.update_action.setText("Relancer en v%s" % version)
            self.update_action.triggered.disconnect()
            self.update_action.triggered.connect(self._relaunch)
            self.update_action.setVisible(True)
        else:
            self._refresh_client_ui()
            self.tray.showMessage("Voxtunnel — mise à jour", message,
                                  QSystemTrayIcon.Warning, 8000)

    def _relaunch(self):
        self.manager.stop_all()

        def restart():
            if INSTANCE_LOCK is not None:
                INSTANCE_LOCK.unlock()
            os.execv(sys.executable, [sys.executable] + sys.argv)

        # laisse une seconde aux SIGTERM avant de repartir
        QTimer.singleShot(1000, restart)

    def _server_update(self, host):
        """Release a proposer pour le paquet serveur de `host`, ou None."""
        rel, version = self.release, self.server_versions.get(host)
        if (rel and version and "voxtunnel-server" in rel.assets
                and vox_update.is_newer(rel.version, version)):
            return rel
        return None

    def _refresh_server_ui(self, host):
        row = self.window.rows.get(host)
        if not row or host in self.updating:
            return
        version = self.server_versions.get(host)
        opus = self.host_opus.get(host, True)
        row.server.setToolTip(
            "" if opus else
            "Flux pcm (768 kbit/s) : opus-tools absent sur le serveur.\n"
            "Opus = 24 kbit/s, tient sur un lien lent ; coute le paquet "
            "opus-tools.")
        if not version:
            row.server.setText("srv hors paquet")
        elif self._server_update(host):
            row.server.setText(
                'srv %s - <a href="#server" style="color: %s;">passer en %s</a>'
                % (version, GREEN, self.release.version))
            if not opus:
                row.server.setToolTip("La mise à jour installe aussi "
                                      "opus-tools (Opus 24 kbit/s au lieu "
                                      "de 768).")
        elif not opus:
            # serveur a jour mais sans decodeur : un clic l'installe
            row.server.setText(
                'srv %s - <a href="#opus" style="color: %s;">opus ?</a>'
                % (version, ORANGE))
        else:
            row.server.setText("srv " + version)

    def install_opus(self, host):
        """Installe opus-tools sur un serveur deja a jour (lien "opus ?")."""
        row = self.window.rows.get(host)
        if not row or host in self.updating:
            return
        self.updating.add(host)
        row.server.setText("srv : installation d'opus-tools...")

        def job():
            try:
                run = subprocess.run(
                    vox_update.server_opus_cmd(host),
                    capture_output=True, text=True, stdin=subprocess.DEVNULL)
                self.update_results.put(
                    ("server", host, run.returncode == 0,
                     vox_update.server_opus_manual_cmd()))
            except OSError as e:
                self.update_results.put(("server", host, False, str(e)))

        threading.Thread(target=job, daemon=True).start()

    def update_server(self, host, href="#server"):
        if href == "#opus":
            self.install_opus(host)
            return
        rel = self._server_update(host)
        row = self.window.rows.get(host)
        if not rel or not row or host in self.updating:
            return
        asset = rel.assets["voxtunnel-server"]
        self.updating.add(host)
        row.server.setText("srv : installation de %s..." % rel.version)

        def job():
            try:
                deb = vox_update.download(asset, LOG_DIR)
                copy = subprocess.run(vox_update.server_copy_cmd(host, deb),
                                      capture_output=True, text=True,
                                      stdin=subprocess.DEVNULL)
                if copy.returncode != 0:
                    raise vox_update.UpdateError(
                        "envoi du paquet impossible : " + copy.stderr.strip())
                run = subprocess.run(
                    vox_update.server_install_cmd(host, asset.name),
                    capture_output=True, text=True, stdin=subprocess.DEVNULL)
                # echec ici = sudo veut un mot de passe : le paquet est deja
                # sur le VPS, il reste une commande a coller
                self.update_results.put(
                    ("server", host, run.returncode == 0,
                     vox_update.server_manual_cmd(asset.name)))
            except (vox_update.UpdateError, OSError) as e:
                self.update_results.put(("server", host, False, str(e)))

        threading.Thread(target=job, daemon=True).start()

    def _on_server_updated(self, host, ok, message):
        self.updating.discard(host)
        if ok:
            # la sonde d'ecoute relira la version installee
            self.server_versions.pop(host, None)
            self.checker._launch([host])
            row = self.window.rows.get(host)
            if row:
                row.server.setText("srv : mis à jour")
            return
        self._refresh_server_ui(host)
        if message.startswith("sudo "):
            self.app.clipboard().setText(message)
            message = ("sudo demande un mot de passe sur %s. Commande à coller "
                       "sur le VPS (copiée dans le presse-papiers) :\n%s"
                       % (host, message))
        self.tray.showMessage("Voxtunnel — " + host, message,
                              QSystemTrayIcon.Warning, 12000)

    def set_transmitting(self, on):
        self.transmitting = on
        self.window.set_voyant(on)
        self.master_action.blockSignals(True)
        self.master_action.setChecked(on)
        self.master_action.blockSignals(False)
        if on:
            for host in sorted(self.desired):
                self._start_stream(host)
        else:
            self.manager.stop_all()
            # les interrupteurs restent tels quels, seuls les streams tombent
            for host in sorted(self.desired):
                if not self.manager.is_running(host):
                    self._set_host_ui(host, True, "suspendu", ORANGE)
        self._update_tray()

    def _on_state_changed(self, host, state, message):
        if state == STARTING:
            self._set_host_ui(host, True, "connexion...", ORANGE)
        elif state == ON:
            text, color = ("actif (%s) - %d ms" % (self.manager.codecs[host],
                                                   self.manager.buffers[host]),
                           GREEN)
            if self.manager.is_unstable(host):
                text, color = text + " - lien instable", ORANGE
            self._set_host_ui(host, True, text, color)
        elif state == ERROR:
            self.desired.discard(host)
            self.restart_pending.discard(host)
            self._set_host_ui(host, False, "erreur : " + message, RED)
            self.tray.showMessage("Voxtunnel — " + host, message,
                                  QSystemTrayIcon.Warning, 8000)
        else:  # OFF
            if host in self.restart_pending:
                self.restart_pending.discard(host)
                if self.transmitting and host in self.desired:
                    # arret demande pour changer de tampon : repart aussitot
                    self._start_stream(host)
                    self._update_tray()
                    return
            if host in self.desired and not self.transmitting:
                self._set_host_ui(host, True, "suspendu", ORANGE)
            else:
                self._set_host_ui(host, host in self.desired, "inactif", GRAY)
        self._update_tray()

    # --- helpers UI ----------------------------------------------------------

    def _set_host_ui(self, host, checked, text, color):
        row = self.window.rows.get(host)
        if row:
            row.set_checked(checked)
            row.set_status(text, color)
        act = self.actions.get(host)
        if act:
            act.blockSignals(True)
            act.setChecked(checked)
            act.blockSignals(False)

    def _update_tray(self):
        self.checker.active = set(self.manager.procs)
        # couleurs forcees : vert = transmission active, rouge = coupee
        if not self.transmitting:
            self.tray.setIcon(self.icon_off)
            self.tray.setToolTip("Voxtunnel — transmission coupée")
        else:
            self.tray.setIcon(self.icon_on)
            if self.manager.procs:
                self.tray.setToolTip(
                    "Voxtunnel — actif : " + ", ".join(sorted(self.manager.procs)))
            else:
                self.tray.setToolTip("Voxtunnel — transmission prête, aucun stream")

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.Trigger:
            if self.window.isVisible():
                self.window.hide()
            else:
                self._show_window()

    def _show_window(self):
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()

    def _quit(self):
        self.manager.stop_all()
        # laisse une seconde aux SIGTERM avant de sortir
        QTimer.singleShot(1000, self.app.quit)


def reap_orphans():
    """Tue les pipelines survivants d'une instance precedente (app tuee
    sans passer par Quitter : les streams continuent d'emettre).
    POSIX uniquement ; sur Windows les process du pipeline meurent avec
    la console ou restent visibles dans le gestionnaire de taches."""
    if os.name != "posix":
        return
    for pattern in ("voxtunnel.sh", "plughw:Loopback,1,0"):
        try:
            out = subprocess.run(["pgrep", "-f", pattern],
                                 capture_output=True, text=True).stdout
        except OSError:
            return
        for token in out.split():
            try:
                pid = int(token)
                if pid == os.getpid():
                    continue
                try:
                    os.killpg(os.getpgid(pid), signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    os.kill(pid, signal.SIGTERM)
            except (ValueError, ProcessLookupError, PermissionError):
                pass


def main():
    uid = os.getuid() if hasattr(os, "getuid") else 0
    lock = QLockFile(os.path.join(tempfile.gettempdir(), "voxtunnel-%d.lock" % uid))
    lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        print("voxtunnel: deja lance", file=sys.stderr)
        return 1
    global INSTANCE_LOCK
    INSTANCE_LOCK = lock

    # le verrou garantit qu'aucune autre instance ne tourne : tout
    # voxtunnel.sh restant est un orphelin a purger
    reap_orphans()

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    if not QSystemTrayIcon.isSystemTrayAvailable():
        print("voxtunnel: pas de zone de notification disponible", file=sys.stderr)

    vt = VoiceTrayApp(app)
    vt._show_window()
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
