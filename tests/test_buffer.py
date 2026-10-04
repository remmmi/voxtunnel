# Tests unitaires de client/vox_buffer.py : recommandation de tampon,
# lecture des underruns, politique de montee, sonde sur un echo local
# (aucun ssh reel). Lance par la CI : python tests/test_buffer.py
import os
import sys
import unittest

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(root, "client"))

import vox_buffer as vb  # noqa: E402

# durees (ms) des 55 underruns d'une vraie session a 190 ms de tampon
REAL_SESSION = [
    51.209, 50.096, 7.362, 105.347, 0.016, 0.883, 5.098, 93.529, 28.210,
    10.246, 36.392, 32.062, 12.240, 16.223, 35.197, 49.307, 96.338, 53.323,
    0.221, 30.884, 8.306, 0.014, 24.063, 0.015, 31.429, 52.064, 0.014,
    6.341, 5.057, 0.013, 127.281, 3.757, 19.182, 13.096, 6396.641, 839.176,
    0.014, 34.482, 8.312, 0.013, 7.053, 85.146, 8.299, 0.170, 3.054, 0.017,
    0.012, 16.353, 0.013, 7.125, 9.201, 2.042, 18538.138, 2896.555, 462.497,
]


def log_lines(values):
    return "".join("underrun!!! (at least %.3f ms long)\n" % v for v in values)


class Recommend(unittest.TestCase):
    def test_lien_calme_donne_le_plancher(self):
        self.assertEqual(vb.recommend([30.0 + i % 3 for i in range(100)]),
                         vb.BUFFER_MIN_MS)

    def test_p99_de_la_gigue_fois_marge(self):
        # 100 mesures : base 30 ms, deux pointes a +200 ms => p99 = 200
        rtts = [30.0] * 98 + [230.0, 230.0]
        self.assertEqual(vb.recommend(rtts), 300)

    def test_une_pointe_isolee_ne_compte_pas(self):
        rtts = [30.0] * 199 + [900.0]
        self.assertEqual(vb.recommend(rtts), vb.BUFFER_MIN_MS)

    def test_plafond(self):
        rtts = [30.0] * 50 + [5030.0] * 50
        self.assertEqual(vb.recommend(rtts), vb.BUFFER_MAX_MS)

    def test_redescend_d_un_palier_au_plus(self):
        calme = [30.0] * 100
        self.assertEqual(vb.recommend(calme, previous=290), 200)
        self.assertEqual(vb.recommend(calme, previous=40), vb.BUFFER_MIN_MS)

    def test_trop_peu_de_mesures(self):
        self.assertIsNone(vb.recommend([30.0] * 5))
        self.assertIsNone(vb.recommend([]))


class Period(unittest.TestCase):
    def test_ratio_4_1_puis_plafond(self):
        self.assertEqual(vb.period_us(80), 20000)
        self.assertEqual(vb.period_us(200), 50000)
        self.assertEqual(vb.period_us(1500), 50000)


class ParseUnderruns(unittest.TestCase):
    def test_session_reelle(self):
        self.assertEqual(vb.parse_underruns(log_lines(REAL_SESSION)),
                         REAL_SESSION)

    def test_ignore_le_reste_du_log(self):
        text = ("voxtunnel — micro local vers host\n"
                "Streaming (tampon 190000us, periode 47500us).\n"
                "underrun!!! (at least 12.500 ms long)\n")
        self.assertEqual(vb.parse_underruns(text), [12.5])

    def test_aplay_localise(self):
        # aplay traduit le message et la virgule decimale selon la locale
        text = "sous-charge !!! (au moins 43,643 ms)\n"
        self.assertEqual(vb.parse_underruns(text), [43.643])


class WatcherPolicy(unittest.TestCase):
    def test_session_reelle_fait_monter(self):
        w = vb.Watcher(190)
        self.assertEqual(w.feed(log_lines(REAL_SESSION), now=0.0), 290)

    def test_sous_le_seuil_rien(self):
        w = vb.Watcher(80)
        self.assertIsNone(w.feed(log_lines([50.0, 60.0]), now=0.0))

    def test_micro_trous_et_arrets_ignores(self):
        w = vb.Watcher(80)
        text = log_lines([0.014, 5.0, 19.9, 6396.6, 18538.1, 2896.5])
        self.assertIsNone(w.feed(text, now=0.0))

    def test_fenetre_glissante(self):
        w = vb.Watcher(80)
        self.assertIsNone(w.feed(log_lines([50.0]), now=0.0))
        self.assertIsNone(w.feed(log_lines([50.0]), now=30.0))
        # le premier est sorti de la fenetre de 60 s
        self.assertIsNone(w.feed(log_lines([50.0]), now=70.0))
        self.assertEqual(w.feed(log_lines([50.0]), now=80.0), 120)

    def test_ligne_coupee_entre_deux_lectures(self):
        w = vb.Watcher(80)
        text = log_lines([50.0, 50.0, 50.0])
        self.assertIsNone(w.feed(text[:-20], now=0.0))
        self.assertEqual(w.feed(text[-20:], now=1.0), 120)

    def test_au_plafond_instable_sans_relance(self):
        w = vb.Watcher(vb.BUFFER_MAX_MS)
        self.assertIsNone(w.feed(log_lines([50.0] * 3), now=0.0))
        self.assertTrue(w.unstable)

    def test_monte_jusqu_au_plafond(self):
        self.assertEqual(vb.step_up(1200), vb.BUFFER_MAX_MS)
        self.assertIsNone(vb.step_up(vb.BUFFER_MAX_MS))


class Probe(unittest.TestCase):
    def test_echo_local(self):
        echo = [sys.executable, "-u", "-c",
                "import sys\n"
                "for l in sys.stdin.buffer:\n"
                "    sys.stdout.buffer.write(l); sys.stdout.buffer.flush()\n"]
        # pas de seuil sur le nombre de blocs : les machines de CI
        # cadencent mal les envois toutes les 20 ms
        rtts = vb.probe("inutile", duration=1.0, cmd=echo)
        self.assertGreaterEqual(len(rtts), 3)
        self.assertGreaterEqual(min(rtts), 0.0)

    def test_commande_morte(self):
        dead = [sys.executable, "-c", "import sys; sys.exit(3)"]
        self.assertEqual(vb.probe("inutile", duration=0.3, cmd=dead), [])


if __name__ == "__main__":
    unittest.main()
