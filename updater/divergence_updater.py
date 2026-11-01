"""The Divergence Update Tool.

Drop the exe into any install of The Divergence (any version, even a hand-edited one) and run it. It hashes every
file, compares against the latest manifest, downloads only what differs, and moves files that no longer belong into a
backup folder. It never trusts version numbers or leftover markers: the end state is always byte-identical to the
latest release.

Run without arguments for the menu. Command line (used by tests and power users):
    --root DIR       install folder (default: auto-detect next to the exe)
    --update         update, then exit
    --check          show what would change, download nothing
    --repair         update and also move out every file under mod/ the release doesn't ship
    --restore        undo the last update
    --install        first-time setup (update, find game, install Mod Engine 3, shortcuts)
    --yes            answer yes to every question
Environment:
    DIVERGENCE_MANIFEST_URL   use this manifest instead of the published one (testing)
    DIVERGENCE_GITHUB_TOKEN   token for a private repo (testing only; the shipped exe never contains one)
"""

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time

import requests

from art import ASCII_CONTENT, COLOR_MAP_JSON

TOOL_VERSION = "1.0.0"
TOOL_TITLE = "The Divergence Update Tool"
REPO = "OzrielTheLost/The-Divergence"
MANIFEST_URLS = [
    f"https://raw.githubusercontent.com/{REPO}/main/manifest.json",
    f"https://github.com/{REPO}/releases/download/manifest/manifest.json",
]
ME3_REPO = "garyttierney/me3"
ME3_RELEASES_PAGE = f"https://github.com/{ME3_REPO}/releases/latest"

STATE_DIR = ".divergence_update"
KEEP_BACKUPS = 2
WORKERS = 4
BLOCK = 4 * 1024 * 1024
USER_AGENT = f"DivergenceUpdateTool/{TOOL_VERSION}"

ASSUME_YES = False


class UpdateError(Exception):
    pass


# ==========================================
# UI (header reused from the NIGHTREMIX Update Tool)
# ==========================================

def hex_to_rgb(hex_str):
    hex_str = hex_str.lstrip('#')
    if len(hex_str) != 6:
        return (255, 255, 255)
    return tuple(int(hex_str[i:i + 2], 16) for i in (0, 2, 4))


def clear_screen():
    os.system('cls' if os.name == 'nt' else 'clear')


def print_header():
    clear_screen()
    if os.name == 'nt':
        os.system('')
    try:
        color_map = json.loads(COLOR_MAP_JSON)
    except Exception:
        color_map = {}
    print("\n")
    for y, line in enumerate(ASCII_CONTENT):
        colored_line = ""
        for x, char in enumerate(line):
            coord = f"{x},{y}"
            if coord in color_map:
                r, g, b = hex_to_rgb(color_map[coord])
                colored_line += f"\033[38;2;{r};{g};{b}m{char}\033[0m"
            else:
                colored_line += char
        print("  " + colored_line)
    print("\n" + "=" * 54)
    print(f"\033[1;36m{TOOL_TITLE:^54}\033[0m")
    print(f"{'v' + TOOL_VERSION:^54}")
    print("=" * 54 + "\n")


def ask(question, default=True):
    if ASSUME_YES:
        print(f"{question} [auto: yes]")
        return True
    hint = "[Y/n]" if default else "[y/N]"
    while True:
        try:
            ans = input(f"{question} {hint} ").strip().lower()
        except EOFError:
            return default
        if not ans:
            return default
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False


def pause(msg="\nPress Enter to continue..."):
    if ASSUME_YES:
        return
    try:
        input(msg)
    except EOFError:
        pass


def fmt_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


# ==========================================
# NETWORK
# ==========================================

_session_local = threading.local()


def http():
    s = getattr(_session_local, "s", None)
    if s is None:
        s = requests.Session()
        s.headers["User-Agent"] = USER_AGENT
        _session_local.s = s
    return s


def token():
    return os.environ.get("DIVERGENCE_GITHUB_TOKEN") or None


def fetch_manifest():
    urls = [os.environ["DIVERGENCE_MANIFEST_URL"]] if os.environ.get("DIVERGENCE_MANIFEST_URL") else list(MANIFEST_URLS)
    tok = token()
    errors = []
    for url in urls:
        headers = {"Cache-Control": "no-cache"}
        if tok and ("githubusercontent.com" in url or "github.com" in url):
            headers["Authorization"] = f"token {tok}"
        try:
            r = http().get(url, headers=headers, timeout=30)
            if r.status_code == 200:
                m = r.json()
                if m.get("schema") != 1 or "files" not in m:
                    raise UpdateError("unknown manifest format (this tool may be too old; get the latest from Nexus)")
                return m
            errors.append(f"{url}: HTTP {r.status_code}")
        except (requests.RequestException, ValueError) as e:
            errors.append(f"{url}: {e}")
    raise UpdateError("Could not download the update manifest.\n    " + "\n    ".join(errors))


def blob_request_args(entry):
    """URL + headers for one blob. With a token and an API url, use the API (needed for private repos)."""
    tok = token()
    if tok and entry.get("api"):
        return entry["api"], {"Authorization": f"token {tok}", "Accept": "application/octet-stream"}
    return entry["url"], {}


def download_blob(entry, dest, progress):
    """Download one blob to dest with resume; verify sha256. Raises UpdateError on failure."""
    want_sha, want_size = entry["sha256"], entry["size"]
    part = dest + ".part"
    last_err = None
    for attempt in range(3):
        try:
            have = os.path.getsize(part) if os.path.exists(part) else 0
            if have > want_size:
                os.remove(part)
                have = 0
            url, headers = blob_request_args(entry)
            headers = dict(headers)
            if have:
                headers["Range"] = f"bytes={have}-"
            with http().get(url, headers=headers, stream=True, timeout=60) as r:
                if r.status_code == 416:
                    os.remove(part)
                    raise UpdateError("bad resume range")
                if r.status_code not in (200, 206):
                    raise UpdateError(f"HTTP {r.status_code}")
                if have and r.status_code == 200:
                    have = 0  # server ignored Range; start over
                progress.set(part, have)
                with open(part, "ab" if have else "wb") as f:
                    for chunk in r.iter_content(chunk_size=1024 * 256):
                        if chunk:
                            f.write(chunk)
                            progress.add(part, len(chunk))
            if sha256_file(part) != want_sha:
                os.remove(part)
                progress.set(part, 0)
                raise UpdateError("checksum mismatch")
            os.replace(part, dest)
            return
        except (requests.RequestException, UpdateError, OSError) as e:
            last_err = e
            time.sleep(1 + attempt * 2)
    raise UpdateError(f"failed to download {entry['sha256'][:12]}: {last_err}")


class Progress:
    def __init__(self, total):
        self.total = max(total, 1)
        self.done = 0
        self.per_part = {}
        self.lock = threading.Lock()
        self.last = 0

    def set(self, key, n):
        with self.lock:
            self.done += n - self.per_part.get(key, 0)
            self.per_part[key] = n
            self.show()

    def add(self, key, n):
        with self.lock:
            self.per_part[key] = self.per_part.get(key, 0) + n
            self.done += n
            self.show()

    def show(self, force=False):
        now = time.time()
        if not force and now - self.last < 0.2:
            return
        self.last = now
        pct = min(100.0, self.done * 100.0 / self.total)
        bar = "#" * int(pct / 4)
        sys.stdout.write(f"\r  [{bar:<25}] {pct:5.1f}%  {fmt_size(self.done)} / {fmt_size(self.total)}   ")
        sys.stdout.flush()


# ==========================================
# FILES AND HASHING
# ==========================================

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(BLOCK)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def rel(root, path):
    return os.path.relpath(path, root).replace("\\", "/")


def absp(root, relpath):
    return os.path.join(root, *relpath.split("/"))


class HashCache:
    """(path, size, mtime) -> sha256. Only a speed-up: any mismatch means rehash."""

    def __init__(self, root):
        self.path = os.path.join(root, STATE_DIR, "hashcache.json")
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self.data = json.load(f)
        except Exception:
            self.data = {}

    def sha(self, root, relpath):
        p = absp(root, relpath)
        try:
            st = os.stat(p)
        except OSError:
            return None
        key = [st.st_size, st.st_mtime_ns]
        hit = self.data.get(relpath)
        if hit and hit[:2] == key:
            return hit[2]
        digest = sha256_file(p)
        self.data[relpath] = key + [digest]
        return digest

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f)
        os.replace(tmp, self.path)


def own_exe_names():
    names = {os.path.basename(sys.executable).lower()} if getattr(sys, "frozen", False) else set()
    names.add(os.path.basename(os.path.abspath(sys.argv[0])).lower())
    return names


def walk_files(root, sub="mod"):
    base = absp(root, sub)
    out = []
    if not os.path.isdir(base):
        return out
    for dirpath, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d != STATE_DIR]
        for fn in files:
            out.append(rel(root, os.path.join(dirpath, fn)))
    return out


def is_game_running():
    if os.name != "nt":
        return False
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq nightreign.exe", "/NH"],
                             capture_output=True, text=True, timeout=15).stdout
        return "nightreign.exe" in out.lower()
    except Exception:
        return False


# ==========================================
# FINDING THE INSTALL
# ==========================================

def exe_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(sys.argv[0]))


def is_install_root(d):
    if not os.path.isdir(os.path.join(d, "mod")):
        return False
    if os.path.isfile(os.path.join(d, "mod", "regulation.bin")):
        return True
    try:
        return any(fn.lower().endswith(".me3") for fn in os.listdir(d))
    except OSError:
        return False


def find_install_root(start):
    start = os.path.abspath(start)
    candidates = [start, os.path.dirname(start)]
    # exe dropped inside mod/ or a subfolder of mod/
    p = start
    for _ in range(4):
        if os.path.basename(p).lower() == "mod":
            candidates.insert(0, os.path.dirname(p))
            break
        p = os.path.dirname(p)
    try:
        for name in sorted(os.listdir(start)):
            child = os.path.join(start, name)
            if os.path.isdir(child) and name != STATE_DIR:
                candidates.append(child)
    except OSError:
        pass
    for c in candidates:
        if is_install_root(c):
            return c
    return None


def pick_folder(title):
    if ASSUME_YES:
        return None
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        d = filedialog.askdirectory(title=title)
        root.destroy()
        return d or None
    except Exception:
        try:
            d = input(f"{title}\nPaste the folder path (empty to cancel): ").strip().strip('"')
        except EOFError:
            return None
        return d or None


def resolve_root(arg_root, for_install=False):
    if arg_root:
        return os.path.abspath(arg_root)
    found = find_install_root(exe_dir())
    if found:
        return found
    if for_install:
        return exe_dir()
    print("[!] Could not find The Divergence next to this tool.")
    print("    Put the tool in the folder that has 'mod' and 'The Divergence.me3', or pick that folder now.")
    d = pick_folder("Select your The Divergence folder (the one containing 'mod')")
    if d and is_install_root(d):
        return os.path.abspath(d)
    if d:
        print(f"[!] {d} doesn't look like a Divergence install (no 'mod' folder).")
    return None


# ==========================================
# PLANNING
# ==========================================

def plan_update(root, manifest, strict=False):
    cache = HashCache(root)
    files = manifest["files"]
    obsolete = manifest.get("obsolete", {})
    first_run = not os.path.exists(state_path(root, "swept"))
    skip = own_exe_names()

    downloads = []   # (relpath, entry, kind) kind: add | replace | new_beside
    removals = []    # relpath
    extras = []      # files under mod/ the release doesn't ship and history doesn't know

    total = len(files)
    for i, (path, entry) in enumerate(sorted(files.items()), 1):
        if i % 50 == 0 or i == total:
            sys.stdout.write(f"\r  Checking files... {i}/{total}")
            sys.stdout.flush()
        p = absp(root, path)
        if not os.path.isfile(p):
            downloads.append((path, entry, "add"))
            continue
        local = cache.sha(root, path)
        if local == entry["sha256"]:
            continue
        if entry.get("user") and local not in set(entry.get("known", [])):
            downloads.append((path, entry, "new_beside"))
        else:
            downloads.append((path, entry, "replace"))
    print()

    for path, known in obsolete.items():
        p = absp(root, path)
        if os.path.isfile(p):
            if isinstance(known, dict) and known.get("user"):
                if cache.sha(root, path) not in set(known.get("known", [])):
                    continue  # player edited it; leave it
            removals.append(path)

    if strict or first_run:
        listed = set(files) | set(removals)
        for path in walk_files(root, "mod"):
            if path not in listed and path not in obsolete and os.path.basename(path).lower() not in skip:
                extras.append(path)

    cache.save()
    return {"downloads": downloads, "removals": removals, "extras": extras, "first_run": first_run}


# ==========================================
# APPLYING (journaled, resumable)
# ==========================================

def state_path(root, *parts):
    return os.path.join(root, STATE_DIR, *parts)


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def move_into(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        os.replace(src, dst)
    except OSError:
        shutil.move(src, dst)


def run_journal(root, journal):
    """Idempotent: safe to rerun after a crash at any point."""
    staging = state_path(root, "staging")
    backup = journal["backup"]
    # how many pending ops still need each staged blob
    remaining = {}
    for op in journal["ops"]:
        if op.get("sha"):
            remaining[op["sha"]] = remaining.get(op["sha"], 0) + 1

    for op in journal["ops"]:
        target = absp(root, op["target"])
        bak = absp(backup, op["target"])
        if op["action"] == "remove":
            if os.path.exists(target):
                move_into(target, bak)
            continue
        sha = op["sha"]
        remaining[sha] -= 1
        staged = os.path.join(staging, sha)
        if not os.path.exists(staged):
            continue  # already placed by an earlier run
        if op["action"] == "replace" and os.path.exists(target) and not os.path.exists(bak):
            move_into(target, bak)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if remaining[sha] > 0:
            tmp = target + ".divtmp"
            shutil.copyfile(staged, tmp)
            os.replace(tmp, target)
        else:
            move_into(staged, target)


def prune_backups(root):
    bdir = state_path(root, "backups")
    if not os.path.isdir(bdir):
        return
    olds = sorted(d for d in os.listdir(bdir) if os.path.isdir(os.path.join(bdir, d)))
    for d in olds[:-KEEP_BACKUPS]:
        shutil.rmtree(os.path.join(bdir, d), ignore_errors=True)


def finish_journal(root, journal):
    write_json(os.path.join(journal["backup"], "restore.json"),
               {"ops": journal["ops"], "from_version": journal.get("from_version"),
                "to_version": journal.get("to_version")})
    os.remove(state_path(root, "journal.json"))
    shutil.rmtree(state_path(root, "staging"), ignore_errors=True)
    prune_backups(root)


def recover_interrupted(root):
    jp = state_path(root, "journal.json")
    if not os.path.exists(jp):
        return
    print("[Updater] A previous update was interrupted. Finishing it now...")
    with open(jp, "r", encoding="utf-8") as f:
        journal = json.load(f)
    run_journal(root, journal)
    finish_journal(root, journal)
    print("[✓] Previous update finished.")


def read_local_version(root):
    try:
        with open(state_path(root, "version.txt"), "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return None


def do_update(root, check_only=False, strict=False):
    if is_game_running():
        raise UpdateError("Nightreign is running. Close the game and run the tool again.")

    recover_interrupted(root)
    print("[Updater] Downloading the update list...")
    manifest = fetch_manifest()
    maybe_self_update(manifest)
    local_v = read_local_version(root)
    print(f"[Updater] Latest version: {manifest.get('version', '?')}"
          + (f"   (last updated by this tool: {local_v})" if local_v else ""))
    print(f"[Updater] Install folder: {root}")
    print("[Updater] Checking your files (the first run takes a little longer)...")
    plan = plan_update(root, manifest, strict=strict)

    downloads, removals, extras = plan["downloads"], plan["removals"], plan["extras"]
    dl_bytes = sum(e["size"] for _, e, _ in downloads)

    if extras:
        why = ("This looks like the first time the tool runs in this folder." if plan["first_run"] and not strict
               else "Repair mode.")
        print(f"\n[?] {why} {len(extras)} file(s) under 'mod' are not part of the latest release:")
        for p in extras[:15]:
            print(f"      {p}")
        if len(extras) > 15:
            print(f"      ... and {len(extras) - 15} more")
        if check_only:
            print("    (Check only: nothing moved.)")
            extras = []
        elif ask("    Move them to the backup folder? (Restore puts them back.)", default=True):
            removals = removals + extras
        extras = []
    if not check_only:
        write_json_text(state_path(root, "swept"), "first-run cleanup offered\n")

    if not downloads and not removals:
        write_json_text(state_path(root, "version.txt"), manifest.get("version", ""))
        print("\n[✓] Everything is up to date.")
        return True

    adds = sum(1 for _, _, k in downloads if k == "add")
    reps = sum(1 for _, _, k in downloads if k == "replace")
    news = [p for p, _, k in downloads if k == "new_beside"]
    print(f"\n  {adds} new file(s), {reps} changed file(s), {len(removals)} to remove.")
    print(f"  Download size: {fmt_size(dl_bytes)}")
    if news:
        print("  You edited these, so the new version will be saved next to them as <name>.new:")
        for p in news:
            print(f"      {p}")
    if check_only:
        for p, _, k in downloads[:40]:
            print(f"    [{k}] {p}")
        if len(downloads) > 40:
            print(f"    ... and {len(downloads) - 40} more")
        for p in removals[:40]:
            print(f"    [remove] {p}")
        return True
    if not ask("\nApply the update?", default=True):
        print("Cancelled. Nothing was changed.")
        return False

    needed = dl_bytes + 64 * 1024 * 1024
    free = shutil.disk_usage(root).free
    if free < needed:
        raise UpdateError(f"Not enough disk space: need about {fmt_size(needed)}, have {fmt_size(free)}.")

    # 1. download every blob to staging
    staging = state_path(root, "staging")
    os.makedirs(staging, exist_ok=True)
    blobs = {}
    for _, entry, _ in downloads:
        blobs[entry["sha256"]] = entry
    todo = [e for s, e in blobs.items() if not os.path.exists(os.path.join(staging, s))]
    total_bytes = sum(e["size"] for e in todo)
    if todo:
        print(f"\n[Updater] Downloading {len(todo)} file(s)...")
        prog = Progress(total_bytes)
        with concurrent.futures.ThreadPoolExecutor(WORKERS) as ex:
            futs = [ex.submit(fetch_blob, e, staging, prog) for e in todo]
            for fut in concurrent.futures.as_completed(futs):
                fut.result()  # raises on failure -> install untouched
        prog.show(force=True)
        print()

    # 2. apply with a journal
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = state_path(root, "backups", stamp)
    ops = []
    for path, entry, kind in downloads:
        target = path + ".new" if kind == "new_beside" else path
        ops.append({"target": target, "sha": entry["sha256"],
                    "action": "replace" if os.path.exists(absp(root, target)) else "add"})
    for path in removals:
        ops.append({"target": path, "action": "remove"})
    journal = {"backup": backup, "ops": ops, "from_version": read_local_version(root),
               "to_version": manifest.get("version")}
    write_json(state_path(root, "journal.json"), journal)
    print("[Updater] Applying...")
    run_journal(root, journal)
    finish_journal(root, journal)
    write_json_text(state_path(root, "version.txt"), manifest.get("version", ""))
    # refresh hash cache for touched files on next run
    print(f"\n[✓] Updated to {manifest.get('version', 'the latest version')}.")
    print(f"    Replaced/removed files were moved to {os.path.relpath(backup, root)} (menu: Restore).")
    if manifest.get("changelog"):
        print("\nWhat's new:\n" + manifest["changelog"].strip())
    return True


def write_json_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def fetch_blob(entry, staging, prog):
    dest = os.path.join(staging, entry["sha256"])
    if os.path.exists(dest):
        return
    parts = entry.get("parts")
    if not parts:
        download_blob(entry, dest, prog)
        return
    for part in parts:
        pdest = os.path.join(staging, part["sha256"])
        if not os.path.exists(pdest):
            download_blob(part, pdest, prog)
    tmp = dest + ".join"
    with open(tmp, "wb") as out:
        for part in parts:
            with open(os.path.join(staging, part["sha256"]), "rb") as f:
                shutil.copyfileobj(f, out, BLOCK)
    if sha256_file(tmp) != entry["sha256"]:
        os.remove(tmp)
        raise UpdateError(f"joined file checksum mismatch ({entry['sha256'][:12]})")
    os.replace(tmp, dest)
    for part in parts:
        try:
            os.remove(os.path.join(staging, part["sha256"]))
        except OSError:
            pass


def do_restore(root):
    recover_interrupted(root)
    bdir = state_path(root, "backups")
    stamps = sorted(d for d in os.listdir(bdir)) if os.path.isdir(bdir) else []
    stamps = [s for s in stamps if os.path.isfile(os.path.join(bdir, s, "restore.json"))]
    if not stamps:
        print("[!] No backup to restore.")
        return False
    stamp = stamps[-1]
    backup = os.path.join(bdir, stamp)
    with open(os.path.join(backup, "restore.json"), "r", encoding="utf-8") as f:
        info = json.load(f)
    print(f"[Restore] Backup from {stamp} (update {info.get('from_version') or '?'} -> {info.get('to_version')}).")
    if not ask("Put the files back the way they were before that update?", default=True):
        return False
    for op in reversed(info["ops"]):
        target = absp(root, op["target"])
        bak = absp(backup, op["target"])
        if op["action"] == "add":
            if os.path.exists(target):
                os.remove(target)
        elif os.path.exists(bak):
            move_into(bak, target)
    shutil.rmtree(backup, ignore_errors=True)
    if info.get("from_version"):
        write_json_text(state_path(root, "version.txt"), info["from_version"])
    else:
        try:
            os.remove(state_path(root, "version.txt"))
        except OSError:
            pass
    print("[✓] Restored.")
    return True


# ==========================================
# SELF UPDATE
# ==========================================

def version_tuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v or "0")[:4])


def maybe_self_update(manifest):
    info = manifest.get("updater")
    if not info or version_tuple(info.get("version")) <= version_tuple(TOOL_VERSION):
        return
    print(f"[Updater] A newer version of this tool is available ({info['version']}).")
    if not getattr(sys, "frozen", False) or os.name != "nt":
        print("    (Running from source: skipping self-update.)")
        return
    if not ask("    Update the tool first?", default=True):
        return
    exe = os.path.abspath(sys.executable)
    new = exe + ".new"
    entry = {"url": info["url"], "api": info.get("api"), "sha256": info["sha256"], "size": info["size"]}
    download_blob(entry, new, Progress(info["size"]))
    bat = os.path.join(os.path.dirname(exe), "_divergence_tool_swap.bat")
    with open(bat, "w", encoding="utf-8") as f:
        f.write("@echo off\r\n"
                ":wait\r\n"
                "timeout /t 1 /nobreak >nul\r\n"
                f'move /y "{new}" "{exe}" >nul 2>&1 || goto wait\r\n'
                f'start "" "{exe}"\r\n'
                '(goto) 2>nul & del "%~f0"\r\n')
    subprocess.Popen(["cmd", "/c", bat], creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
    print("    Restarting the tool...")
    sys.exit(0)


# ==========================================
# INSTALL (Steam finder reused from the NIGHTREMIX Update Tool)
# ==========================================

def find_steam_libraries():
    libraries = []
    if sys.platform == "win32":
        import winreg
        for hive, key in ((winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam"),
                          (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Valve\Steam")):
            try:
                k = winreg.OpenKey(hive, key)
                for name in ("InstallPath", "SteamPath"):
                    try:
                        libraries.append(winreg.QueryValueEx(k, name)[0])
                    except OSError:
                        pass
            except OSError:
                pass
        libraries.append(r"C:\Program Files (x86)\Steam")
    else:
        libraries.append(os.path.expanduser("~/.steam/steam"))
        libraries.append(os.path.expanduser("~/.local/share/Steam"))

    all_libraries = set()
    for p in libraries:
        vdf_path = os.path.join(p, "steamapps", "libraryfolders.vdf")
        if os.path.exists(vdf_path):
            all_libraries.add(os.path.normpath(p))
            try:
                with open(vdf_path, 'r', encoding='utf-8', errors='ignore') as f:
                    for line in f:
                        match = re.search(r'"path"\s+"([^"]+)"', line, re.IGNORECASE)
                        if match:
                            all_libraries.add(os.path.normpath(match.group(1).replace('\\\\', '\\')))
            except Exception:
                pass
    return list(all_libraries)


def auto_find_game_dir():
    for lib in find_steam_libraries():
        target = os.path.join(lib, "steamapps", "common", "ELDEN RING NIGHTREIGN", "Game")
        if os.path.exists(os.path.join(target, "nightreign.exe")):
            return target
    return None


def find_me3():
    exe = shutil.which("me3")
    if exe:
        return exe
    local = os.environ.get("LOCALAPPDATA", "")
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    for c in (os.path.join(local, "Programs", "me3", "bin", "me3.exe"),
              os.path.join(local, "garyttierney", "me3", "bin", "me3.exe"),
              os.path.join(pf, "me3", "bin", "me3.exe"),
              os.path.join(pf, "garyttierney", "me3", "bin", "me3.exe")):
        if os.path.isfile(c):
            return c
    if sys.platform == "win32":
        import winreg
        try:
            winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, ".me3")
            return "(registered for .me3 files)"
        except OSError:
            pass
    return None


def install_me3():
    print("[Installer] Mod Engine 3 (me3) is not installed. Downloading its latest installer...")
    try:
        r = http().get(f"https://api.github.com/repos/{ME3_REPO}/releases/latest", timeout=30,
                       headers={"Accept": "application/vnd.github+json"})
        r.raise_for_status()
        rel_info = r.json()
        assets = rel_info.get("assets", [])
        pick = next((a for a in assets if a["name"].lower().endswith(".exe") and "installer" in a["name"].lower()),
                    None) or next((a for a in assets if a["name"].lower().endswith(".msi")), None)
        if not pick:
            raise UpdateError("no Windows installer in the latest me3 release")
        dest = os.path.join(os.environ.get("TEMP", exe_dir()), pick["name"])
        with http().get(pick["browser_download_url"], stream=True, timeout=60) as d:
            d.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in d.iter_content(1024 * 256):
                    f.write(chunk)
        print(f"[Installer] Running {pick['name']} ({rel_info.get('tag_name', '')}). Finish it, then come back here.")
        if dest.lower().endswith(".msi"):
            subprocess.run(["msiexec", "/i", dest])
        else:
            subprocess.run([dest])
        return True
    except Exception as e:
        print(f"[!] Could not download the me3 installer automatically: {e}")
        print(f"    Opening the me3 download page: {ME3_RELEASES_PAGE}")
        try:
            import webbrowser
            webbrowser.open(ME3_RELEASES_PAGE)
        except Exception:
            pass
        pause("    Install me3 from that page, then press Enter...")
        return False


def create_shortcuts(root):
    if os.name != "nt":
        return
    desktop = os.path.join(os.environ.get("USERPROFILE", ""), "Desktop")
    profiles = [fn for fn in os.listdir(root) if fn.lower().endswith(".me3")]
    for fn in profiles:
        lnk = os.path.join(desktop, os.path.splitext(fn)[0] + ".lnk")
        target = os.path.join(root, fn)
        ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{0}');"
              "$s.TargetPath='{1}';$s.WorkingDirectory='{2}';$s.Save()").format(
            lnk.replace("'", "''"), target.replace("'", "''"), root.replace("'", "''"))
        try:
            subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, timeout=30)
            print(f"  [+] Desktop shortcut: {os.path.basename(lnk)}")
        except Exception as e:
            print(f"  [!] Could not create shortcut for {fn}: {e}")


def do_install(root):
    print(f"[Installer] Install folder: {root}")
    if not is_install_root(root):
        others = [n for n in os.listdir(root) if n.lower() not in own_exe_names() and n != STATE_DIR]
        if others and not ask("    This folder has other files in it. Install The Divergence here anyway?", True):
            return False
    print("\n[Installer] Step 1/3: download or update the mod files")
    if not do_update(root):
        return False

    print("\n[Installer] Step 2/3: find Elden Ring Nightreign")
    game = auto_find_game_dir()
    if game:
        print(f"[✓] Found the game at:\n    {game}")
    else:
        print("[!] Could not find Nightreign in your Steam libraries. me3 will ask Steam for it when you launch,")
        print("    so this is only a problem if the game isn't installed through Steam.")

    print("\n[Installer] Step 3/3: Mod Engine 3")
    me3 = find_me3()
    if me3:
        print(f"[✓] me3 is installed: {me3}")
    else:
        install_me3()
        me3 = find_me3()
        print(f"[✓] me3 is installed: {me3}" if me3 else
              "[!] Still can't find me3. Install it before launching (see the mod page).")

    if ask("\nCreate desktop shortcuts for the launch profiles?", default=True):
        create_shortcuts(root)
    print("\n[✓] Done. Launch the game by opening 'The Divergence.me3' (or its desktop shortcut).")
    return True


# ==========================================
# MAIN
# ==========================================

def run_action(action, root_arg):
    try:
        if action == "install":
            root = resolve_root(root_arg, for_install=True)
            return do_install(root)
        root = resolve_root(root_arg)
        if not root:
            return False
        if action == "update":
            return do_update(root)
        if action == "check":
            return do_update(root, check_only=True)
        if action == "repair":
            return do_update(root, strict=True)
        if action == "restore":
            return do_restore(root)
    except UpdateError as e:
        print(f"\n[!] {e}")
        print("    Your install was not changed by this run (anything already downloaded is kept for next time).")
        return False
    except KeyboardInterrupt:
        print("\n[!] Stopped. Run the tool again to continue where it left off.")
        return False


def safe_console():
    """Never crash on a character the console/pipe encoding lacks (e.g. cp1252 when output is redirected)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass


def main():
    global ASSUME_YES
    safe_console()
    ap = argparse.ArgumentParser(description=TOOL_TITLE)
    ap.add_argument("--root")
    g = ap.add_mutually_exclusive_group()
    for a in ("update", "check", "repair", "restore", "install"):
        g.add_argument(f"--{a}", action="store_true")
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()
    ASSUME_YES = args.yes

    for a in ("update", "check", "repair", "restore", "install"):
        if getattr(args, a):
            ok = run_action(a, args.root)
            sys.exit(0 if ok else 1)

    menu = [("1", "Install", "install"), ("2", "Update", "update"), ("3", "Check only", "check"),
            ("4", "Repair (strict)", "repair"), ("5", "Restore last backup", "restore"), ("6", "Exit", None)]
    while True:
        print_header()
        print("Please select an option:")
        for key, label, _ in menu:
            print(f"  {key}. {label}")
        try:
            choice = input("\n>> ").strip() or "2"
        except EOFError:
            return
        hit = next((m for m in menu if m[0] == choice), None)
        if not hit:
            print("Invalid choice.")
            pause()
            continue
        if hit[2] is None:
            return
        print()
        run_action(hit[2], args.root)
        pause("\nPress Enter to return to the menu...")


if __name__ == "__main__":
    main()
