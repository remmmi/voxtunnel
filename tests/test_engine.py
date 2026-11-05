#!/usr/bin/env python3
# Tests de client/voxtunnel.sh : occupation de la face du loopback.
#
# Aucun ssh, aucun micro, aucune carte son : `ssh` est remplace par un
# script qui execute la commande ici, en deplacant /proc/asound vers un
# dossier temporaire ; `arecord` et `aplay` sont des faux. L'occupant de
# la face est un vrai processus, nomme aplay ou autrement.
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENGINE = os.path.join(ROOT, "client", "voxtunnel.sh")

FAKE_SSH = """#!/bin/sh
while [ $# -gt 0 ]; do
  case "$1" in -o) shift 2 ;; -*) shift ;; *) break ;; esac
done
shift
cmd=$(printf '%s' "$*" | sed "s#/proc/asound#$FAKE_ASOUND#g")
exec sh -c "$cmd"
"""
FAKE_APLAY = """#!/bin/sh
case "$1" in -l) echo "card 1: Loopback [Loopback]" ;; *) cat >/dev/null ;; esac
"""
FAKE_ARECORD = "#!/bin/sh\nexit 0\n"
OCCUPANT = "#!/bin/sh\nread x\n"


def script(path, body):
    with open(path, "w") as f:
        f.write(body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)


@unittest.skipUnless(sys.platform.startswith("linux"), "moteur Linux")
class FaceDuLoopback(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        script(os.path.join(self.bin, "ssh"), FAKE_SSH)
        script(os.path.join(self.bin, "aplay"), FAKE_APLAY)
        script(os.path.join(self.bin, "arecord"), FAKE_ARECORD)
        self.asound = os.path.join(self.tmp, "asound")
        sub = os.path.join(self.asound, "Loopback", "pcm1p", "sub0")
        os.makedirs(sub)
        self.status = os.path.join(sub, "status")
        self.set_status("closed\n")
        self.occupant = None

    def tearDown(self):
        if self.occupant:
            if self.occupant.poll() is None:
                self.occupant.kill()
            self.occupant.stdin.close()
            self.occupant.wait()

    def set_status(self, text):
        with open(self.status, "w") as f:
            f.write(text)

    def occupy(self, name):
        """Un processus nomme `name` tient la face, comme le noyau le
        montre dans status ; a sa mort la face redevient closed."""
        folder = os.path.join(self.tmp, "occ")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, name)
        script(path, OCCUPANT)
        env = dict(os.environ,
                   SSH_CONNECTION="203.0.113.7 5000 198.51.100.1 22")
        self.occupant = subprocess.Popen([path], stdin=subprocess.PIPE,
                                         env=env)
        self.set_status("state: RUNNING\nowner_pid   : %d\n"
                        "trigger_time: 1.0\n" % self.occupant.pid)

        def release():
            self.occupant.wait()
            self.set_status("closed\n")
        threading.Thread(target=release, daemon=True).start()
        return self.occupant

    def engine(self, *args, **extra):
        env = dict(os.environ, VPS_HOST="alpha", CODEC="pcm",
                   SINK="plughw:Loopback,1,0", FAKE_ASOUND=self.asound,
                   PATH=self.bin + os.pathsep + os.environ["PATH"])
        env.update(extra)
        return subprocess.run(["bash", ENGINE] + list(args), env=env,
                              capture_output=True, text=True, timeout=30)

    def test_face_libre(self):
        r = self.engine("--check")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("libre", r.stdout)

    def test_check_nomme_l_occupant_sans_y_toucher(self):
        occ = self.occupy("aplay")
        r = self.engine("--check")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("pid %d" % occ.pid, r.stdout)
        self.assertIn("aplay", r.stdout)
        self.assertIn("203.0.113.7", r.stdout)
        self.assertIsNone(occ.poll(), "--check ne doit rien arreter")

    def test_stream_arrete_l_occupant_et_prend_la_face(self):
        occ = self.occupy("aplay")
        r = self.engine()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("pid %d" % occ.pid, r.stdout)
        self.assertIn("arrete", r.stdout)
        self.assertIsNotNone(occ.wait(timeout=5))
        self.assertIn("Streaming", r.stdout)

    def test_occupant_qui_n_est_pas_aplay_reste_en_vie(self):
        occ = self.occupy("pipewire")
        r = self.engine()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("pid %d" % occ.pid, r.stderr)
        self.assertIn("pipewire", r.stderr)
        self.assertNotIn("Streaming", r.stdout)
        self.assertIsNone(occ.poll(), "seul un aplay peut etre arrete")

    def test_face_hors_loopback_non_controlee(self):
        self.occupy("aplay")
        r = self.engine("--check", SINK="plug:autre")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("pid", r.stdout)


if __name__ == "__main__":
    unittest.main()
