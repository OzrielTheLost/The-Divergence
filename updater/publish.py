"""Publish a Divergence release for the update tool.

Hashes the release folder, uploads only files whose contents are not stored yet, then writes manifest.json and
history.json. Run from a clone of the The-Divergence repo (the manifest is committed there).

    python updater/publish.py "D:/Games/MMV + Convergence Weapons/The Divergence" --version 1.1.3 \\
        --changelog "D:/Games/MMV + Convergence Weapons/The Divergence/CHANGELOG.txt" --push
    python updater/publish.py <release dir> --version 1.1.3 --dry          # show what would upload
    python updater/publish.py <release dir> --version 1.1.3 --updater-exe "dist/The Divergence Update Tool.exe"

Storage backends:
    default        GitHub Releases on --repo, via the `gh` CLI (must be logged in: `gh auth login`)
    --local-store  copy blobs into a folder served at --base-url (tests, or any static host)

Blobs are named by SHA-256, so an unchanged file is uploaded once and reused by every later manifest. The manifest is
written last, so players never see a manifest that points at blobs that aren't uploaded yet.
"""

import argparse
import fnmatch
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

REPO = "OzrielTheLost/The-Divergence"
STORE_PREFIX = "store-"
STORE_MAX_ASSETS = 950          # GitHub allows 1000 assets per release; keep headroom
# files over the 2 GiB asset limit are split into 1 GiB parts (env overrides exist for tests)
PART_SIZE = int(os.environ.get("DIVPUB_PART_SIZE", 1024 ** 3))
ASSET_LIMIT = int(os.environ.get("DIVPUB_ASSET_LIMIT", 2 * 1024 ** 3 - 1))
BLOCK = 4 * 1024 * 1024

IGNORE = ["*.bak", "*.prev", "*.log", "desktop.ini", "Thumbs.db", ".divergence_update/*", "*/.divergence_update/*",
          ".smithbox/*", "*/.smithbox/*", "*-dcx/*", "_divergence_tool_swap.bat",
          "mod/project.json",  # Smithbox project file: not used by the game, contains a local path
          "The Divergence Update Tool.exe", "The Divergence Update Tool.exe.new"]
# Files players may edit: only replaced if unchanged from a shipped version, otherwise saved as <name>.new
USER_FILES = ["*.me3", "*.ini", "*.toml", "*.cfg"]


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(BLOCK)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def matches(path, patterns):
    name = path.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(path, p) or fnmatch.fnmatch(name, p) for p in patterns)


def scan_release(root):
    out = {}
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in (".git", ".divergence_update", ".smithbox")]
        for fn in files:
            full = os.path.join(dirpath, fn)
            relp = os.path.relpath(full, root).replace("\\", "/")
            if matches(relp, IGNORE):
                continue
            out[relp] = full
    return out


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, indent=1, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


# ------------------------------------------------------------------ storage backends

class LocalStore:
    def __init__(self, folder, base_url):
        self.folder = folder
        self.base_url = base_url.rstrip("/")
        os.makedirs(folder, exist_ok=True)

    def upload(self, items):
        """items: list of (sha, path). Returns {sha: {"url": ...}}."""
        out = {}
        for sha, path in items:
            dst = os.path.join(self.folder, sha)
            if not os.path.exists(dst):
                shutil.copyfile(path, dst)
            out[sha] = {"url": f"{self.base_url}/{sha}"}
        return out

    def publish_manifest(self, manifest_path):
        shutil.copyfile(manifest_path, os.path.join(self.folder, "manifest.json"))


class GitHubStore:
    def __init__(self, repo, history):
        self.repo = repo
        self.history = history
        if not shutil.which("gh"):
            sys.exit("The GitHub CLI `gh` is required (https://cli.github.com), logged in with `gh auth login`.")

    def gh(self, *args, capture=True):
        r = subprocess.run(["gh", *args], capture_output=capture, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"gh {' '.join(args)} failed:\n{r.stderr}")
        return r.stdout

    def release_assets(self, tag):
        try:
            data = json.loads(self.gh("api", f"repos/{self.repo}/releases/tags/{tag}"))
        except RuntimeError:
            return None
        out = {}
        tsv = self.gh("api", "--paginate", f"repos/{self.repo}/releases/{data['id']}/assets?per_page=100",
                      "--jq", ".[] | [.name, .browser_download_url, .url] | @tsv")
        for line in tsv.splitlines():
            if line.strip():
                name, url, api = line.split("\t")
                out[name] = {"url": url, "api": api}
        return out

    def ensure_release(self, tag, title, notes):
        if self.release_assets(tag) is None:
            self.gh("release", "create", tag, "--repo", self.repo, "--title", title, "--notes", notes)

    def upload(self, items):
        out = {}
        stores = self.history.setdefault("stores", {})
        pending = list(items)
        tmpdir = tempfile.mkdtemp(prefix="divpub_")
        try:
            while pending:
                tag = self.current_store(stores)
                room = STORE_MAX_ASSETS - stores[tag]
                batch, pending = pending[:room], pending[room:]
                for i, (sha, path) in enumerate(batch, 1):
                    link = os.path.join(tmpdir, sha)
                    if not os.path.exists(link):
                        try:
                            os.link(path, link)
                        except OSError:
                            shutil.copyfile(path, link)
                    print(f"  [{i}/{len(batch)}] uploading {sha[:12]} ({os.path.getsize(path) / 1048576:.1f} MB)"
                          f" -> {tag}")
                    for attempt in range(4):
                        try:
                            self.gh("release", "upload", tag, link, "--repo", self.repo, "--clobber")
                            break
                        except RuntimeError as e:
                            if attempt == 3:
                                raise
                            print(f"    retrying: {str(e).strip()[:200]}")
                            time.sleep(2 ** (attempt + 1))
                    os.remove(link)
                    stores[tag] += 1
                assets = self.release_assets(tag)
                for sha, _ in batch:
                    if sha not in assets:
                        raise RuntimeError(f"{sha} missing from {tag} after upload")
                    out[sha] = dict(assets[sha], release=tag)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        return out

    def current_store(self, stores):
        tags = sorted(stores)
        if tags and stores[tags[-1]] < STORE_MAX_ASSETS:
            return tags[-1]
        tag = f"{STORE_PREFIX}{len(tags) + 1:03d}"
        self.ensure_release(tag, f"File store {len(tags) + 1}",
                            "Storage for The Divergence Update Tool. Files are named by their SHA-256. "
                            "Don't download from here by hand; use the update tool or the full release.")
        stores[tag] = len(self.release_assets(tag) or {})
        return tag

    def publish_manifest(self, manifest_path):
        self.ensure_release("manifest", "Update manifest",
                            "Fallback copy of manifest.json for The Divergence Update Tool.")
        self.gh("release", "upload", "manifest", manifest_path, "--repo", self.repo, "--clobber")


# ------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser(description="Publish a Divergence release for the update tool")
    ap.add_argument("release_dir", help="folder laid out exactly like the release zip (mod/, *.me3, ...)")
    ap.add_argument("--version", required=True)
    ap.add_argument("--changelog", help="text file; its first section (up to the first blank-line + version) is shown")
    ap.add_argument("--repo", default=REPO)
    ap.add_argument("--out", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    help="where manifest.json and history.json live (default: the repo root)")
    ap.add_argument("--updater-exe", help="built update tool exe to advertise for self-update")
    ap.add_argument("--updater-version", help="version of --updater-exe (default: read from divergence_updater.py)")
    ap.add_argument("--local-store", help="copy blobs into this folder instead of GitHub Releases")
    ap.add_argument("--base-url", help="URL where --local-store is served")
    ap.add_argument("--exclude", action="append", default=[], metavar="PATTERN",
                    help="extra path pattern to leave out (repeatable), e.g. --exclude \"mod/notes/*\"")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--push", action="store_true", help="git commit + push manifest.json/history.json in --out")
    args = ap.parse_args()

    root = os.path.abspath(args.release_dir)
    if not os.path.isdir(os.path.join(root, "mod")):
        sys.exit(f"{root} has no mod/ folder; point at the folder laid out like the release zip.")
    manifest_path = os.path.join(args.out, "manifest.json")
    history_path = os.path.join(args.out, "history.json")
    history = load_json(history_path, {"paths": {}, "blobs": {}, "stores": {}, "versions": []})

    print(f"[1/4] Hashing {root} ...")
    IGNORE.extend(p.replace("\\", "/") for p in args.exclude)
    files = scan_release(root)
    entries = {}
    t0 = time.time()
    for i, (relp, full) in enumerate(sorted(files.items()), 1):
        entries[relp] = {"size": os.path.getsize(full), "sha256": sha256_file(full)}
        if i % 100 == 0:
            print(f"      {i}/{len(files)}")
    print(f"      {len(entries)} files, {sum(e['size'] for e in entries.values()) / 1073741824:.2f} GB "
          f"in {time.time() - t0:.0f}s")

    # big files -> parts
    to_upload = {}   # sha -> path
    part_files = {}  # relp -> [(sha, size, tmp path)]
    tmp_parts = tempfile.mkdtemp(prefix="divparts_")
    try:
        for relp, e in entries.items():
            if e["size"] > ASSET_LIMIT:
                parts = []
                with open(files[relp], "rb") as f:
                    n = 0
                    while True:
                        data = f.read(PART_SIZE)
                        if not data:
                            break
                        n += 1
                        sha = hashlib.sha256(data).hexdigest()
                        pp = os.path.join(tmp_parts, sha)
                        with open(pp, "wb") as o:
                            o.write(data)
                        parts.append((sha, len(data), pp))
                part_files[relp] = parts
                for sha, _, pp in parts:
                    if sha not in history["blobs"]:
                        to_upload[sha] = pp
            elif e["sha256"] not in history["blobs"]:
                to_upload[e["sha256"]] = files[relp]

        updater = None
        if args.updater_exe:
            usha = sha256_file(args.updater_exe)
            uver = args.updater_version or read_tool_version()
            updater = {"version": uver, "sha256": usha, "size": os.path.getsize(args.updater_exe)}
            if usha not in history["blobs"]:
                to_upload[usha] = args.updater_exe

        up_bytes = sum(os.path.getsize(p) for p in to_upload.values())
        print(f"[2/4] {len(to_upload)} new blob(s) to upload ({up_bytes / 1048576:.1f} MB); "
              f"{len(entries) - len(to_upload)} file(s) already stored.")
        if args.dry:
            for sha, p in sorted(to_upload.items(), key=lambda kv: kv[1]):
                print(f"      {os.path.relpath(p, root) if p.startswith(root) else p}")
            print("Dry run: nothing uploaded or written.")
            return

        if args.local_store:
            if not args.base_url:
                sys.exit("--local-store needs --base-url")
            store = LocalStore(args.local_store, args.base_url)
        else:
            store = GitHubStore(args.repo, history)

        if to_upload:
            uploaded = store.upload(sorted(to_upload.items(), key=lambda kv: os.path.getsize(kv[1])))
            history["blobs"].update(uploaded)
            save_json(history_path, history)  # record progress even if a later step fails
    finally:
        shutil.rmtree(tmp_parts, ignore_errors=True)

    print("[3/4] Writing manifest...")
    m_files = {}
    for relp, e in sorted(entries.items()):
        entry = dict(e)
        if relp in part_files:
            entry["parts"] = [dict(size=size, sha256=sha, **history["blobs"][sha]) for sha, size, _ in part_files[relp]]
        else:
            entry.update(history["blobs"][e["sha256"]])
        entry.pop("release", None)
        for p in entry.get("parts", []):
            p.pop("release", None)
        if matches(relp, USER_FILES):
            entry["user"] = True
            entry["known"] = sorted(set(history["paths"].get(relp, [])) | {e["sha256"]})
        m_files[relp] = entry

    # history: every path ever shipped, with every hash it had
    for relp, e in entries.items():
        known = history["paths"].setdefault(relp, [])
        if e["sha256"] not in known:
            known.append(e["sha256"])
    obsolete = {}
    for relp, known in sorted(history["paths"].items()):
        if relp not in entries:
            obsolete[relp] = {"known": known, "user": matches(relp, USER_FILES)}

    manifest = {
        "schema": 1,
        "mod": "The Divergence",
        "version": args.version,
        "released": time.strftime("%Y-%m-%d"),
        "changelog": read_changelog(args.changelog),
        "files": m_files,
        "obsolete": obsolete,
    }
    if updater:
        updater.update({k: v for k, v in history["blobs"][updater["sha256"]].items() if k != "release"})
        manifest["updater"] = updater
    if args.version not in history["versions"]:
        history["versions"].append(args.version)

    save_json(history_path, history)
    save_json(manifest_path, manifest)
    print(f"      {manifest_path}")

    print("[4/4] Publishing manifest...")
    store.publish_manifest(manifest_path)
    if args.push:
        git = ["git", "-C", args.out]
        subprocess.run(git + ["add", "manifest.json", "history.json"], check=True)
        subprocess.run(git + ["commit", "-m", f"Publish {args.version}"], check=True)
        subprocess.run(git + ["push"], check=True)
    else:
        print("      Commit and push manifest.json + history.json to make it live (or rerun with --push).")
    print(f"Done: {args.version} published.")


def read_tool_version():
    here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "divergence_updater.py")
    with open(here, "r", encoding="utf-8") as f:
        for line in f:
            if line.startswith("TOOL_VERSION"):
                return line.split("=")[1].strip().strip('"\'')
    return "0"


def read_changelog(path, max_lines=40):
    """First version section of a changelog, e.g. '1.1.2 (2026-10-31)' up to the next version heading."""
    if not path:
        return ""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()
    import re
    head = re.compile(r"^\s*#*\s*v?\d+\.\d+(\.\d+)?[a-z]?\b")
    start = next((i for i, ln in enumerate(lines) if head.match(ln)), None)
    if start is None:
        return "\n".join(lines[:max_lines]).strip()
    end = next((i for i in range(start + 1, len(lines)) if head.match(lines[i])), len(lines))
    return "\n".join(lines[start:end][:max_lines]).strip()


if __name__ == "__main__":
    main()
