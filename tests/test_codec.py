#!/usr/bin/env python3
# Tests de vox_codec.py : choix du codec et commandes d'encodage/decodage.
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "client"))
import vox_codec as vc  # noqa: E402


class Choose(unittest.TestCase):
    def test_opus_quand_les_deux_bouts_savent(self):
        self.assertEqual(vc.choose(server_opus=True, local_opus=True), "opus")

    def test_repli_pcm_sans_decodeur_distant(self):
        self.assertEqual(vc.choose(server_opus=False, local_opus=True), "pcm")

    def test_repli_pcm_sans_encodeur_local(self):
        self.assertEqual(vc.choose(server_opus=True, local_opus=False), "pcm")


class Commands(unittest.TestCase):
    def test_encodeur_sans_tampon_et_bloc_d_une_periode(self):
        cmd = vc.encode_cmd(48000, 20000)
        self.assertEqual(cmd[0], "ffmpeg")
        # 20 ms a 48 kHz mono s16 = 1920 octets : ffmpeg ne doit pas lire le
        # pipe par blocs de 32 Ko, sinon il retient 340 ms d'audio
        self.assertIn("1920", cmd[cmd.index("-blocksize") + 1])
        self.assertIn("nobuffer", cmd)
        self.assertIn("libopus", cmd)
        self.assertEqual(cmd[-1], "pipe:1")

    def test_sortie_opus_pour_une_capture_ffmpeg(self):
        args = vc.opus_output_args(48000)
        self.assertIn("libopus", args)
        self.assertIn("ogg", args)
        self.assertEqual(args[-1], "pipe:1")

    def test_commande_distante_pcm(self):
        cmd = vc.remote_cmd("pcm", 48000, 80000, 20000)
        self.assertTrue(cmd.startswith("aplay -D plughw:Loopback,1,0 "))
        self.assertIn("-t raw", cmd)
        self.assertIn("--buffer-time=80000", cmd)
        self.assertNotIn("opusdec", cmd)

    def test_commande_distante_opus(self):
        cmd = vc.remote_cmd("opus", 48000, 80000, 20000)
        self.assertTrue(cmd.startswith("opusdec "))
        self.assertIn("--rate 48000", cmd)
        self.assertIn("| aplay -D plughw:Loopback,1,0 ", cmd)
        self.assertIn("-t wav", cmd)
        self.assertIn("--period-time=20000", cmd)

    def test_codec_inconnu(self):
        with self.assertRaises(ValueError):
            vc.remote_cmd("mp3", 48000, 80000, 20000)


if __name__ == "__main__":
    unittest.main()
