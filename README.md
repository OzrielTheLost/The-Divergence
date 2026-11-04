# The Divergence

Update host and update tool for **The Divergence** (Convergence for Elden Ring Nightreign).

- `manifest.json` / `history.json`: what the latest release contains, written by the publisher. The tool reads
  `manifest.json` from this repo.
- Releases named `store-001`, `store-002`, ...: the mod files, one asset per file, named by SHA-256.
  Don't download from them by hand.
- `updater/`: the update tool and the publisher.

## For players

Download [The-Divergence-Update-Tool.exe](https://github.com/OzrielTheLost/The-Divergence/releases/download/updater-latest/The-Divergence-Update-Tool.exe), drop it into your The Divergence folder (the one with `mod` and
`The Divergence.me3`; any older version works) and run it.

| Option | What it does |
|---|---|
| 1. Install | Downloads the whole mod into the folder the tool is in (or updates it), finds Nightreign, installs Mod Engine 3 if it's missing, offers desktop shortcuts |
| 2. Update | Checks every file and downloads only what changed. Files that no longer belong are moved to a backup |
| 3. Check only | Shows what would change, changes nothing |
| 4. Repair (strict) | Update, and also move out any file under `mod/` that the release doesn't ship |
| 5. Restore last backup | Undo the last update |

The tool never trusts version numbers: it compares file contents, so any old or hand-edited install ends up exactly
like the latest release. Edited `.me3` profiles are kept; the new one is saved next to it as `.new`. Close the game
before updating.

## Building the exe

GitHub Actions builds it on every change under `updater/` (Actions tab, artifact *The-Divergence-Update-Tool*). Push
a tag like `updater-v1.0.0` to attach the exe to a release. Locally:

```
pip install -r updater/requirements-build.txt
cd updater
pyinstaller --onefile --console --name "The Divergence Update Tool" --icon assets/icon.ico divergence_updater.py
```

Tests: `python -m pytest updater/tests -q` (publishes fake releases to a local server and updates fake installs).
