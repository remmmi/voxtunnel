# Garde-fou du plugin Claude Code (claude-plugin/) : sa version suit
# __version__ du client, son manifeste et son skill sont lisibles, et rien
# de propre a une machine n'y traine. Aucun acces reseau.
# Lance par la CI : python tests/test_plugin.py
import json
import os
import re
import unittest

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
plugin = os.path.join(root, "claude-plugin")


def read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as fh:
        return fh.read()


class Plugin(unittest.TestCase):
    def test_version_follows_client(self):
        tray = read(root, "client", "voxtunnel-tray.py")
        version = re.search(r'^__version__ = "(.*)"', tray, re.M).group(1)
        manifest = json.loads(read(plugin, ".claude-plugin", "plugin.json"))
        self.assertEqual(manifest["version"], version)
        self.assertEqual(manifest["name"], "voxtunnel")

    def test_skill_has_frontmatter(self):
        skill = read(plugin, "skills", "voxtunnel", "SKILL.md")
        head = re.match(r"---\n(.*?)\n---\n", skill, re.S)
        self.assertIsNotNone(head)
        self.assertIn("name: voxtunnel", head.group(1))
        self.assertIn("description:", head.group(1))

    def test_nothing_machine_specific(self):
        # ni chemin de compte, ni adresse IP : le depot est public
        for folder, _, files in os.walk(plugin):
            for name in files:
                text = read(folder, name)
                self.assertIsNone(re.search(r"/home/[a-z0-9_-]+/", text), name)
                self.assertIsNone(
                    re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", text), name)

    def test_not_shipped_in_packages(self):
        # les paquets Debian ne doivent pas embarquer ce dossier
        self.assertNotIn("claude-plugin", read(root, "build-deb.sh"))


if __name__ == "__main__":
    unittest.main()
