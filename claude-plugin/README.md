# Voxtunnel — Claude Code plugin

A skill for Claude Code (or any agent that reads `SKILL.md` files): install, verify, diagnose and
roll back Voxtunnel on the machine the agent runs on, server or client side.

The plugin ships **no code and installs nothing by itself**. Voxtunnel is a system application
(Debian packages, a kernel module on the server); every install goes through the skill, on request,
with the user's agreement. Application updates remain those of Voxtunnel itself (tray update check,
server package pushed from the client): the plugin only updates the skill.

Nothing in the Debian packages or the install scripts depends on this folder.

## Use it

Try it from a clone, without installing anything:

```
claude --plugin-dir /path/to/voxtunnel/claude-plugin
```

Or reference it from a plugin marketplace, as a sub-directory of this repository:

```json
{
  "name": "voxtunnel",
  "source": { "source": "git-subdir", "url": "https://github.com/remmmi/voxtunnel.git", "path": "claude-plugin", "ref": "main" }
}
```

## Version

`version` in `.claude-plugin/plugin.json` follows `__version__` in `client/voxtunnel-tray.py`;
`tests/test_plugin.py` (run in CI) fails when they differ.
