#!/usr/bin/env python3
# Tests de client/vox_adb.py : lecture du preflight et des battements,
# commande du tunnel, et le script de preflight execute pour de vrai avec
# de faux `adb` et `ss` (aucun ssh, aucun telephone). L'occupant du port
# est un vrai processus, nomme adb, sshd ou autrement.
# Lance par la CI : python tests/test_adb.py
import os
import stat
import subprocess
import sys
import tempfile
import unittest

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(root, "client"))

import vox_adb as va  # noqa: E402

FAKE_ADB = '#!/bin/sh\necho "Android Debug Bridge version 1.0.41"\necho "Version 34.0.5"\n'
# ss factice : le port est tenu tant que le process note dans $HOLDER vit ;
# $HIDE_PID = 1 imite un port tenu par un autre utilisateur (pas de pid)
FAKE_SS = '''#!/bin/sh
p=$(cat "$HOLDER" 2>/dev/null)
# un occupant tue reste zombie tant que le test ne l'a pas ramasse
case "$(ps -o stat= -p "${p:-0}" 2>/dev/null)" in ""|Z*) exit 0 ;; esac
case "$1" in
  *p*) [ "$HIDE_PID" = 1 ] && echo "LISTEN 0 128 127.0.0.1:5037 0.0.0.0:*" \\
       || echo "LISTEN 0 128 127.0.0.1:5037 0.0.0.0:* users:((\\"x\\",pid=$p,fd=9))" ;;
  *) echo "LISTEN 0 128 127.0.0.1:5037 0.0.0.0:*" ;;
esac
'''
OCCUPANT = "#!/bin/sh\nread x\n"


def script(path, body):
    with open(path, "w") as f:
        f.write(body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)


class Lecture(unittest.TestCase):
    def test_version(self):
        self.assertEqual(va.version_of(
            "Android Debug Bridge version 1.0.41\nVersion 34.0.5-debian"), "1.0.41")
        self.assertEqual(va.version_of("command not found"), "")

    def test_preflight_libre(self):
        self.assertEqual(va.parse_preflight("V 1.0.41\nfree\n", "1.0.41"), (va.OK, ""))

    def test_preflight_port_repris(self):
        status, message = va.parse_preflight("V 1.0.41\nfreed adb\n", "1.0.41")
        self.assertEqual(status, va.OK)
        self.assertIn("adb", message)

    def test_preflight_sans_adb(self):
        status, message = va.parse_preflight("NOADB\n", "1.0.41")
        self.assertEqual(status, va.FATAL)
        self.assertIn(va.install_hint(), message)

    def test_preflight_versions_differentes(self):
        # un client d'une autre version tuerait le serveur adb local
        status, message = va.parse_preflight("V 1.0.39\nfree\n", "1.0.41")
        self.assertEqual(status, va.FATAL)
        self.assertIn("1.0.39", message)

    def test_preflight_port_etranger(self):
        self.assertEqual(
            va.parse_preflight("V 1.0.41\nforeign nginx\n", "1.0.41")[0], va.FATAL)
        self.assertEqual(
            va.parse_preflight("V 1.0.41\ndenied\n", "1.0.41")[0], va.FATAL)

    def test_preflight_a_retenter(self):
        self.assertEqual(
            va.parse_preflight("V 1.0.41\nstuck\n", "1.0.41")[0], va.RETRY)

    def test_battements(self):
        self.assertEqual(va.parse_beat("UP\n"), ("up",))
        self.assertEqual(va.parse_beat("D 1 0"), ("devices", 1, 0))
        self.assertEqual(va.parse_beat("X"), ("mute",))
        self.assertEqual(va.parse_beat(
            "Warning: remote port forwarding failed for listen port 5037"),
            ("refused",))
        self.assertIsNone(va.parse_beat("autre chose"))

    def test_description(self):
        self.assertEqual(va.describe(("devices", 1, 0))[1], True)
        self.assertEqual(va.describe(("devices", 0, 1))[1], False)
        self.assertIn("non autorisé", va.describe(("devices", 0, 1))[0])
        self.assertEqual(va.describe(("devices", 0, 0))[1], False)

    def test_attente_croissante(self):
        waits = [va.backoff(n) for n in range(6)]
        self.assertEqual(waits, sorted(waits))
        self.assertEqual(waits[-1], va.BACKOFF_S[-1])

    def test_tunnel_a_part_de_la_voix(self):
        cmd = va.tunnel_cmd("alpha")
        # jamais multiplexe avec une autre connexion, port sur 127.0.0.1 seul
        self.assertIn("ControlPath=none", cmd)
        self.assertIn("127.0.0.1:5037:127.0.0.1:5037", cmd)
        self.assertEqual(cmd[-2], "alpha")
        self.assertIn("ServerAliveInterval=10", cmd)

    def test_preflight_sans_adb_local(self):
        real = va.have_local
        va.have_local = lambda: False
        try:
            self.assertEqual(va.preflight("alpha")[0], va.FATAL)
        finally:
            va.have_local = real


@unittest.skipUnless(sys.platform.startswith("linux"), "script serveur")
class PortDuServeur(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        script(os.path.join(self.bin, "adb"), FAKE_ADB)
        script(os.path.join(self.bin, "ss"), FAKE_SS)
        self.holder = os.path.join(self.tmp, "holder")
        self.occupant = None

    def tearDown(self):
        if self.occupant:
            if self.occupant.poll() is None:
                self.occupant.kill()
            self.occupant.stdin.close()
            self.occupant.wait()

    def occupy(self, name):
        path = os.path.join(self.tmp, name)
        script(path, OCCUPANT)
        self.occupant = subprocess.Popen([path], stdin=subprocess.PIPE)
        with open(self.holder, "w") as f:
            f.write(str(self.occupant.pid))

    def run_preflight(self, **env):
        env = dict(os.environ, HOLDER=self.holder,
                   PATH=self.bin + os.pathsep + os.environ["PATH"], **env)
        return subprocess.run(["sh", "-c", va.REMOTE_PREFLIGHT], env=env,
                              capture_output=True, text=True,
                              timeout=20).stdout

    def test_port_libre(self):
        out = self.run_preflight()
        self.assertEqual(out.split("\n")[:2], ["V 1.0.41", "free"])
        self.assertEqual(va.parse_preflight(out, "1.0.41"), (va.OK, ""))

    def test_serveur_adb_local_arrete(self):
        # un adb demarre sur le VPS pendant une coupure tient le port
        self.occupy("adb")
        out = self.run_preflight()
        self.assertIn("freed adb", out)
        self.assertIsNotNone(self.occupant.wait(timeout=5))

    def test_tunnel_fantome_arrete(self):
        self.occupy("sshd-session")
        out = self.run_preflight()
        self.assertIn("freed sshd-session", out)
        self.assertIsNotNone(self.occupant.wait(timeout=5))

    def test_occupant_etranger_epargne(self):
        self.occupy("nginx")
        out = self.run_preflight()
        self.assertIn("foreign nginx", out)
        self.assertIsNone(self.occupant.poll())

    def test_autre_utilisateur(self):
        self.occupy("adb")
        out = self.run_preflight(HIDE_PID="1")
        self.assertEqual(va.parse_preflight(out, "1.0.41")[0], va.FATAL)
        self.assertIsNone(self.occupant.poll())

    def test_adb_absent(self):
        os.remove(os.path.join(self.bin, "adb"))
        env = dict(os.environ, HOLDER=self.holder, PATH=self.bin)
        out = subprocess.run(["/bin/sh", "-c", va.REMOTE_PREFLIGHT], env=env,
                             capture_output=True, text=True).stdout
        self.assertEqual(out.strip(), "NOADB")


if __name__ == "__main__":
    unittest.main()
