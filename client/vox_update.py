#!/usr/bin/env python3
#
# vox_update.py — verification et installation des mises a jour.
#
# La source de verite est la derniere release GitHub du depot : son tag
# donne la version, ses assets les deux paquets Debian (client et serveur).
#
#   python3 vox_update.py    affiche la derniere release publiee
#
# Utilise par voxtunnel-tray.py :
#   fetch_latest()        derniere release, ou None (pas de reseau, pas de release)
#   is_newer()            compare deux versions
#   download()            telecharge un paquet et controle son SHA-256
#   install_cmd()         installation locale, via pkexec (mot de passe systeme)
#   server_*()            envoi et installation du paquet serveur par SSH
#   parse_listen()        sortie de la sonde d'ecoute, version serveur comprise
#
# L'empreinte SHA-256 vient de la meme API GitHub que le fichier : elle
# garantit un telechargement intact, pas l'identite de celui qui publie.

import collections
import hashlib
import json
import os
import shutil
import sys
import urllib.error
import urllib.request

REPO = "remmmi/voxtunnel"
API_URL = "https://api.github.com/repos/%s/releases/latest" % REPO
RAW_URL = "https://raw.githubusercontent.com/%s/v%%s/CHANGELOG.tsv" % REPO
DOWNLOAD_PREFIX = "https://github.com/%s/releases/download/" % REPO
TIMEOUT_S = 10

# repertoire du paquet Debian client : seul cas ou l'installation en un
# clic a un sens (clone git, macOS et Windows renvoient vers la release)
DEB_DIR = "/usr/share/voxtunnel"

PACKAGES = ("voxtunnel", "voxtunnel-server")

Asset = collections.namedtuple("Asset", "name url sha256")
Release = collections.namedtuple("Release", "version page_url assets notes")


class UpdateError(Exception):
    pass


def _numbers(version):
    try:
        return tuple(int(p) for p in (version or "").lstrip("v").split("."))
    except ValueError:
        return ()


def is_newer(candidate, current):
    """Vrai si `candidate` est une version lisible strictement superieure."""
    a, b = _numbers(candidate), _numbers(current)
    return bool(a and b and a > b)


def parse_release(data, notes=""):
    """Release decrite par une reponse de l'API, ou None si elle n'est pas
    exploitable (brouillon, pre-version, erreur de l'API)."""
    if not isinstance(data, dict) or data.get("draft") or data.get("prerelease"):
        return None
    version = str(data.get("tag_name") or "").lstrip("v")
    if not _numbers(version):
        return None
    assets = {}
    for item in data.get("assets") or []:
        name = str(item.get("name") or "")
        url = str(item.get("browser_download_url") or "")
        digest = str(item.get("digest") or "")
        for package in PACKAGES:
            if (name == "%s_%s_all.deb" % (package, version)
                    and url.startswith(DOWNLOAD_PREFIX)
                    and digest.startswith("sha256:")):
                assets[package] = Asset(name, url, digest[len("sha256:"):])
    return Release(version, str(data.get("html_url") or ""), assets, notes)


def changelog_line(tsv, version, column=3):
    """Texte de la ligne `version` de CHANGELOG.tsv (colonne 3 : francais)."""
    for line in tsv.splitlines():
        cells = line.split("\t")
        if cells[0] == version and len(cells) > column:
            return cells[column].strip()
    return ""


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "voxtunnel"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        return resp.read()


def fetch_latest():
    """Derniere release publiee, ou None si GitHub ne repond pas."""
    try:
        release = parse_release(json.loads(_get(API_URL).decode()))
    except (OSError, ValueError):
        return None
    if release is None:
        return None
    try:
        tsv = _get(RAW_URL % release.version).decode(errors="replace")
    except OSError:
        tsv = ""
    return release._replace(notes=changelog_line(tsv, release.version))


def verify(path, sha256):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            digest.update(chunk)
    if digest.hexdigest() != sha256:
        raise UpdateError("empreinte SHA-256 incorrecte : " + os.path.basename(path))


def download(asset, dest_dir):
    """Telecharge `asset` dans `dest_dir`, controle son empreinte, renvoie
    le chemin. Leve UpdateError si le fichier n'est pas celui annonce."""
    os.makedirs(dest_dir, exist_ok=True)
    path = os.path.join(dest_dir, asset.name)
    try:
        with open(path, "wb") as f:
            f.write(_get(asset.url))
        verify(path, asset.sha256)
    except (OSError, UpdateError) as e:
        try:
            os.remove(path)
        except OSError:
            pass
        if isinstance(e, UpdateError):
            raise
        raise UpdateError("telechargement impossible : %s" % e)
    return path


def is_deb_install(script_dir):
    return (os.path.realpath(script_dir) == DEB_DIR
            and bool(shutil.which("pkexec")) and bool(shutil.which("apt-get")))


def install_cmd(deb_path):
    return ["pkexec", "apt-get", "install", "-y", deb_path]


# --- serveur -----------------------------------------------------------------
# Le paquet est telecharge et controle ici, puis pousse par scp : le VPS n'a
# pas a joindre GitHub. L'installation passe par `sudo -n` : sans droits sudo
# sans mot de passe elle echoue proprement et le tray affiche la commande.

SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]


def server_copy_cmd(host, deb_path):
    return (["scp", "-q"] + SSH_OPTS
            + [deb_path, "%s:/tmp/%s" % (host, os.path.basename(deb_path))])


def server_install_cmd(host, deb_name):
    remote = "/tmp/" + deb_name
    return (["ssh"] + SSH_OPTS + [host,
            'if [ "$(id -u)" = 0 ]; then apt-get install -y %s; '
            "else sudo -n apt-get install -y %s; fi && rm -f %s"
            % (remote, remote, remote)])


def server_manual_cmd(deb_name):
    return "sudo apt-get install -y /tmp/" + deb_name


# Serveur deja a jour mais sans opus-tools (paquet installe avant que le
# Recommends existe, ou apt sans recommends) : meme mecanique que la mise a
# jour, sudo -n puis commande a coller.
def server_opus_cmd(host):
    return (["ssh"] + SSH_OPTS + [host,
            'if [ "$(id -u)" = 0 ]; then apt-get install -y opus-tools; '
            "else sudo -n apt-get install -y opus-tools; fi"])


def server_opus_manual_cmd():
    return "sudo apt-get install -y opus-tools"


def parse_listen(out):
    """Sortie de la sonde d'ecoute -> (pret, capture ouverte, version du
    paquet voxtunnel-server ou '' s'il n'est pas installe, decodeur opus
    present)."""
    lines = out.decode(errors="replace").split()
    state = lines[0] if lines else ""
    version = ""
    opus = False
    for line in lines[1:]:
        if line.startswith("V="):
            version = line[2:]
        elif line == "O":
            opus = True
    return state in ("R", "RL"), state == "RL", version, opus


def main():
    release = fetch_latest()
    if release is None:
        print("vox_update: GitHub ne repond pas ou aucune release", file=sys.stderr)
        return 1
    print("derniere release : v%s  %s" % (release.version, release.page_url))
    if release.notes:
        print("  " + release.notes)
    for package in PACKAGES:
        asset = release.assets.get(package)
        print("  %-17s %s" % (package, asset.name if asset else "absent"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
