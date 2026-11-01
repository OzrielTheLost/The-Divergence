# The Divergence

Update host and update tool for **The Divergence** (Convergence for Elden Ring Nightreign).

- `manifest.json` / `history.json`: what the latest release contains, written by the publisher. The tool reads
  `manifest.json` from this repo.
- Releases named `store-001`, `store-002`, ...: the mod files, one asset per file, named by SHA-256.
  Don't download from them by hand.
- `updater/`: the update tool and the publisher.

## For players

Download `The Divergence Update Tool.exe`, drop it into your The Divergence folder (the one with `mod` and
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

## For Kiwi: publishing a release

Needs Python 3.10+, `pip install requests`, and the GitHub CLI logged in (`gh auth login`).

```
git clone https://github.com/OzrielTheLost/The-Divergence
cd The-Divergence
python updater/publish.py "D:/Games/MMV + Convergence Weapons/The Divergence" --version 1.1.3 ^
    --changelog "D:/Games/MMV + Convergence Weapons/The Divergence/CHANGELOG.txt" --dry      # preview
python updater/publish.py "D:/Games/MMV + Convergence Weapons/The Divergence" --version 1.1.3 ^
    --changelog "D:/Games/MMV + Convergence Weapons/The Divergence/CHANGELOG.txt" --push     # publish
```

Point it at the folder laid out exactly like the release zip. The first publish uploads every file (~1,400 files, a
few GB, so it takes a while); later ones upload only changed files. The manifest is pushed last, so players never
see a half-published release.

To advertise a new tool version for self-update, add `--updater-exe "path/to/The Divergence Update Tool.exe"`
(bump `TOOL_VERSION` in `updater/divergence_updater.py` first).

**The repo must be public before players can use the tool.** While it's private, test with
`set DIVERGENCE_GITHUB_TOKEN=<token with repo read access>` before running the exe.

## Building the exe

GitHub Actions builds it on every change under `updater/` (Actions tab, artifact *The-Divergence-Update-Tool*). Push
a tag like `updater-v1.0.0` to attach the exe to a release. Locally:

```
pip install -r updater/requirements-build.txt
cd updater
pyinstaller --onefile --console --name "The Divergence Update Tool" --icon assets/icon.ico divergence_updater.py
```

Tests: `python -m pytest updater/tests -q` (publishes fake releases to a local server and updates fake installs).
