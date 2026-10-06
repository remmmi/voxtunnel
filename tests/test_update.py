# Tests unitaires de client/vox_update.py : comparaison de versions,
# lecture d'une reponse figee de l'API GitHub, controle du SHA-256, ligne
# de changelog, sortie de la sonde serveur. Aucun acces reseau.
# Lance par la CI : python tests/test_update.py
import hashlib
import os
import sys
import tempfile
import unittest

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(root, "client"))

import vox_update as vu  # noqa: E402

BASE = "https://github.com/remmmi/voxtunnel/releases/"

# reponse de /releases/latest, reduite aux champs lus
API = {
    "tag_name": "v1.3.0",
    "html_url": BASE + "tag/v1.3.0",
    "draft": False,
    "prerelease": False,
    "assets": [
        {"name": "voxtunnel-server_1.3.0_all.deb",
         "digest": "sha256:" + "a" * 64,
         "browser_download_url":
             BASE + "download/v1.3.0/voxtunnel-server_1.3.0_all.deb"},
        {"name": "voxtunnel_1.3.0_all.deb",
         "digest": "sha256:" + "b" * 64,
         "browser_download_url":
             BASE + "download/v1.3.0/voxtunnel_1.3.0_all.deb"},
    ],
}

TSV = ("1.3.0\t2026-10-04\tauto buffer\ttampon auto\n"
       "1.2.1\t2026-08-12\ticon\ticone\n")


class Versions(unittest.TestCase):
    def test_plus_recente(self):
        self.assertTrue(vu.is_newer("1.3.0", "1.2.1"))
        self.assertTrue(vu.is_newer("v1.10.0", "1.9.9"))
        self.assertTrue(vu.is_newer("2.0", "1.9.9"))

    def test_egale_ou_plus_vieille(self):
        self.assertFalse(vu.is_newer("1.2.1", "1.2.1"))
        self.assertFalse(vu.is_newer("1.2.0", "1.2.1"))

    def test_illisible(self):
        self.assertFalse(vu.is_newer("nightly", "1.2.1"))
        self.assertFalse(vu.is_newer("", "1.2.1"))
        self.assertFalse(vu.is_newer("1.3.0", None))


class ParseRelease(unittest.TestCase):
    def test_release_normale(self):
        rel = vu.parse_release(API)
        self.assertEqual(rel.version, "1.3.0")
        self.assertEqual(rel.page_url, BASE + "tag/v1.3.0")
        client = rel.assets["voxtunnel"]
        self.assertEqual(client.name, "voxtunnel_1.3.0_all.deb")
        self.assertEqual(client.sha256, "b" * 64)
        self.assertEqual(rel.assets["voxtunnel-server"].sha256, "a" * 64)

    def test_brouillon_et_preversion_ignores(self):
        self.assertIsNone(vu.parse_release(dict(API, draft=True)))
        self.assertIsNone(vu.parse_release(dict(API, prerelease=True)))

    def test_reponse_inattendue(self):
        self.assertIsNone(vu.parse_release({"message": "Not Found"}))
        self.assertIsNone(vu.parse_release([]))

    def test_asset_sans_empreinte_ecarte(self):
        data = dict(API, assets=[dict(API["assets"][1], digest=None)])
        self.assertNotIn("voxtunnel", vu.parse_release(data).assets)

    def test_asset_hors_du_depot_ecarte(self):
        bad = dict(API["assets"][1],
                   browser_download_url="https://example.com/voxtunnel_1.3.0_all.deb")
        self.assertNotIn("voxtunnel",
                         vu.parse_release(dict(API, assets=[bad])).assets)


class Changelog(unittest.TestCase):
    def test_ligne_de_la_version(self):
        self.assertEqual(vu.changelog_line(TSV, "1.3.0"), "tampon auto")
        self.assertEqual(vu.changelog_line(TSV, "1.2.1"), "icone")

    def test_version_absente(self):
        self.assertEqual(vu.changelog_line(TSV, "9.9.9"), "")
        self.assertEqual(vu.changelog_line("", "1.3.0"), "")


class Verify(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp()
        os.write(fd, b"paquet")
        os.close(fd)

    def tearDown(self):
        os.remove(self.path)

    def test_empreinte_juste(self):
        vu.verify(self.path, hashlib.sha256(b"paquet").hexdigest())

    def test_empreinte_fausse(self):
        with self.assertRaises(vu.UpdateError):
            vu.verify(self.path, "0" * 64)


class Commands(unittest.TestCase):
    def test_install_client(self):
        self.assertEqual(vu.install_cmd("/c/voxtunnel_1.3.0_all.deb"),
                         ["pkexec", "apt-get", "install", "-y",
                          "/c/voxtunnel_1.3.0_all.deb"])

    def test_install_serveur_sans_mot_de_passe(self):
        cmd = vu.server_install_cmd("hote", "voxtunnel-server_1.3.0_all.deb")
        opus = vu.server_opus_cmd("hote")
        self.assertEqual(opus[0], "ssh")
        self.assertIn("sudo -n apt-get install -y opus-tools", opus[-1])
        self.assertEqual(vu.server_opus_manual_cmd(),
                         "sudo apt-get install -y opus-tools")
        self.assertEqual(cmd[:3], ["ssh", "-o", "BatchMode=yes"])
        self.assertIn("hote", cmd)
        self.assertIn("sudo -n apt-get install -y "
                      "/tmp/voxtunnel-server_1.3.0_all.deb", cmd[-1])

    def test_commande_a_coller(self):
        self.assertEqual(
            vu.server_manual_cmd("voxtunnel-server_1.3.0_all.deb"),
            "sudo apt-get install -y /tmp/voxtunnel-server_1.3.0_all.deb")


class ParseListen(unittest.TestCase):
    def test_pret_avec_paquet(self):
        self.assertEqual(vu.parse_listen(b"RL\nV=1.2.1\n"),
                         (True, True, "1.2.1", False, ""))
        self.assertEqual(vu.parse_listen(b"R\nV=1.2.1\n"),
                         (True, False, "1.2.1", False, ""))

    def test_pret_hors_paquet(self):
        self.assertEqual(vu.parse_listen(b"R\n"), (True, False, "", False, ""))

    def test_decodeur_opus_present(self):
        self.assertEqual(vu.parse_listen(b"R\nV=1.4.0\nO\n"),
                         (True, False, "1.4.0", True, ""))
        self.assertEqual(vu.parse_listen(b"RL\nO\n"), (True, True, "", True, ""))

    def test_face_de_capture_de_default(self):
        self.assertEqual(vu.parse_listen(b"R\nO\nC0\n"),
                         (True, False, "", True, "C0"))
        self.assertEqual(vu.parse_listen(b"R\nC1\n"), (True, False, "", False, "C1"))

    def test_pas_de_loopback(self):
        self.assertEqual(vu.parse_listen(b"N\n"), (False, False, "", False, ""))
        self.assertEqual(vu.parse_listen(b""), (False, False, "", False, ""))


if __name__ == "__main__":
    unittest.main()
