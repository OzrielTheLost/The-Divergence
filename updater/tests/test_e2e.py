"""End-to-end tests: publish fake releases to a local store served over HTTP, then update installs with the tool.

Run: python -m pytest updater/tests -q
"""

import functools
import hashlib
import http.server
import json
import os
import shutil
import socketserver
import subprocess
import sys
import threading

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
UPD = os.path.dirname(HERE)
TOOL = os.path.join(UPD, "divergence_updater.py")
PUB = os.path.join(UPD, "publish.py")
STATE = ".divergence_update"


# ------------------------------------------------------------------ helpers

class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


@pytest.fixture
def env(tmp_path):
    store = tmp_path / "store"
    store.mkdir()
    handler = functools.partial(Quiet, directory=str(store))
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
    srv.daemon_threads = True
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    out = tmp_path / "out"
    out.mkdir()
    yield {"tmp": tmp_path, "store": store, "base": base, "out": out}
    srv.shutdown()


def write(root, rel, data):
    p = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data if isinstance(data, bytes) else data.encode())


def tree(root):
    out = {}
    for dp, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d != STATE]
        for fn in files:
            p = os.path.join(dp, fn)
            with open(p, "rb") as f:
                out[os.path.relpath(p, root).replace("\\", "/")] = hashlib.sha256(f.read()).hexdigest()
    return out


def make_v1(root):
    write(root, "The Divergence.me3", "profileVersion = 'v1'\n")
    write(root, "CHANGELOG.txt", "1.0 (2026-10-01)\n\n- first\n")
    write(root, "mod/regulation.bin", b"REG1" * 1000)
    write(root, "mod/chr/c0000.anibnd.dcx", b"ANI1" * 5000)
    write(root, "mod/chr/c4810.anibnd.dcx", b"OLDBOSS" * 100)    # removed in v2
    write(root, "mod/sfx/big.ffxbnd.dcx", os.urandom(300_000))    # split into parts by the test limits
    write(root, "mod/msg/dup_a.bin", b"SAME" * 10)
    write(root, "mod/msg/dup_b.bin", b"SAME" * 10)


def make_v2(src, dst):
    shutil.copytree(src, dst)
    write(dst, "CHANGELOG.txt", "1.1 (2026-11-01)\n\n- second\n\n1.0 (2026-10-01)\n\n- first\n")
    write(dst, "mod/regulation.bin", b"REG2" * 1200)
    os.remove(os.path.join(dst, "mod/chr/c4810.anibnd.dcx"))
    write(dst, "mod/chr/c9999.anibnd.dcx", b"NEWBOSS" * 300)
    write(dst, "mod/sfx/big.ffxbnd.dcx", os.urandom(310_000))
    write(dst, "The Divergence.me3", "profileVersion = 'v1'\n# v2\n")
    write(dst, "mod/msg/dup_c.bin", b"SAME" * 10)


def publish(e, release, version, extra=()):
    envv = dict(os.environ, DIVPUB_ASSET_LIMIT="100000", DIVPUB_PART_SIZE="65536")
    r = subprocess.run([sys.executable, PUB, str(release), "--version", version,
                        "--changelog", os.path.join(release, "CHANGELOG.txt"),
                        "--local-store", str(e["store"]), "--base-url", e["base"], "--out", str(e["out"]), *extra],
                       capture_output=True, text=True, env=envv)
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout


def run_tool(e, root, action="--update", ok=True):
    envv = dict(os.environ, DIVERGENCE_MANIFEST_URL=e["base"] + "/manifest.json")
    r = subprocess.run([sys.executable, TOOL, action, "--yes", "--root", str(root)],
                       capture_output=True, text=True, env=envv, encoding="utf-8")
    if ok:
        assert r.returncode == 0, r.stdout + r.stderr
    return r


@pytest.fixture
def releases(env):
    v1 = env["tmp"] / "v1"
    make_v1(str(v1))
    publish(env, v1, "1.0")
    v2 = env["tmp"] / "v2"
    make_v2(str(v1), str(v2))
    publish(env, v2, "1.1")
    return v1, v2


# ------------------------------------------------------------------ tests

def test_manifest_shape(env, releases):
    m = json.load(open(env["out"] / "manifest.json"))
    assert m["version"] == "1.1"
    assert "mod/chr/c4810.anibnd.dcx" in m["obsolete"]
    assert m["files"]["mod/sfx/big.ffxbnd.dcx"]["parts"]
    assert m["files"]["The Divergence.me3"]["user"] is True
    assert len(m["files"]["The Divergence.me3"]["known"]) == 2
    assert m["changelog"].startswith("1.1")


def test_old_version_updates_exactly(env, releases):
    v1, v2 = releases
    inst = env["tmp"] / "inst"
    shutil.copytree(v1, inst)
    r = run_tool(env, inst)
    assert tree(inst) == tree(v2), r.stdout
    assert "Updated to 1.1" in r.stdout
    # second run: nothing to do
    r = run_tool(env, inst)
    assert "up to date" in r.stdout


def test_fresh_folder_full_download(env, releases):
    _, v2 = releases
    inst = env["tmp"] / "fresh"
    inst.mkdir()
    write(str(inst), "mod/.keep_dir_marker_ignored", b"")  # just a mod folder, nothing else
    os.remove(os.path.join(inst, "mod/.keep_dir_marker_ignored"))
    run_tool(env, inst)
    assert tree(inst) == tree(v2)


def test_mixed_and_unknown_files(env, releases):
    v1, v2 = releases
    inst = env["tmp"] / "mixed"
    shutil.copytree(v1, inst)
    shutil.copy(os.path.join(v2, "mod/regulation.bin"), os.path.join(inst, "mod/regulation.bin"))
    write(str(inst), "mod/chr/c1234_from_prebeta.dcx", b"STALE")         # unknown -> first-run sweep
    write(str(inst), "mod/chr/c0000.anibnd.dcx", b"CORRUPT")            # damaged -> replaced
    write(str(inst), "notes_by_player.txt", b"keep me")                 # outside mod/ -> untouched
    r = run_tool(env, inst)
    t = tree(inst)
    assert t.pop("notes_by_player.txt")
    assert t == tree(v2), r.stdout
    assert "c1234_from_prebeta.dcx" in r.stdout


def test_edited_user_file_kept(env, releases):
    v1, v2 = releases
    inst = env["tmp"] / "edited"
    shutil.copytree(v1, inst)
    write(str(inst), "The Divergence.me3", "my own edits\n")
    run_tool(env, inst)
    with open(os.path.join(inst, "The Divergence.me3")) as f:
        assert f.read() == "my own edits\n"
    assert open(os.path.join(inst, "The Divergence.me3.new")).read() == open(os.path.join(v2, "The Divergence.me3")).read()


def test_restore(env, releases):
    v1, _ = releases
    inst = env["tmp"] / "restore"
    shutil.copytree(v1, inst)
    write(str(inst), "mod/chr/stray.dcx", b"X")
    before = tree(inst)
    run_tool(env, inst)
    assert tree(inst) != before
    run_tool(env, inst, "--restore")
    assert tree(inst) == before


def test_check_only_changes_nothing(env, releases):
    v1, _ = releases
    inst = env["tmp"] / "check"
    shutil.copytree(v1, inst)
    before = tree(inst)
    r = run_tool(env, inst, "--check")
    assert tree(inst) == before
    assert "changed file" in r.stdout
    # the first-run sweep is still offered on the real update afterwards
    write(str(inst), "mod/stray2.bin", b"Y")
    r = run_tool(env, inst)
    assert "stray2.bin" in r.stdout


def test_missing_blob_leaves_install_untouched(env, releases):
    v1, _ = releases
    m = json.load(open(env["out"] / "manifest.json"))
    os.remove(os.path.join(env["store"], m["files"]["mod/regulation.bin"]["sha256"]))
    inst = env["tmp"] / "broken"
    shutil.copytree(v1, inst)
    before = tree(inst)
    r = run_tool(env, inst, ok=False)
    assert r.returncode != 0
    assert tree(inst) == before


def test_exe_dropped_inside_mod_finds_root(env, releases):
    sys.path.insert(0, UPD)
    import divergence_updater as du
    v1, _ = releases
    inst = env["tmp"] / "nested"
    shutil.copytree(v1, inst)
    assert du.find_install_root(str(inst / "mod" / "chr")) == str(inst)
    assert du.find_install_root(str(inst / "mod")) == str(inst)
    assert du.find_install_root(str(inst)) == str(inst)
    outer = env["tmp"] / "outer"
    outer.mkdir()
    shutil.move(str(inst), str(outer / "The Divergence"))
    assert du.find_install_root(str(outer)) == str(outer / "The Divergence")


def test_crash_mid_apply_recovers(env, releases, monkeypatch):
    sys.path.insert(0, UPD)
    import divergence_updater as du
    v1, v2 = releases
    inst = env["tmp"] / "crash"
    shutil.copytree(v1, inst)
    monkeypatch.setenv("DIVERGENCE_MANIFEST_URL", env["base"] + "/manifest.json")
    monkeypatch.setattr(du, "ASSUME_YES", True)
    real = du.move_into
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] == 4:
            raise KeyboardInterrupt  # power cut mid-apply
        return real(src, dst)

    monkeypatch.setattr(du, "move_into", flaky)
    with pytest.raises(KeyboardInterrupt):
        du.do_update(str(inst))
    assert os.path.exists(inst / STATE / "journal.json")
    monkeypatch.setattr(du, "move_into", real)
    du.do_update(str(inst))
    assert tree(inst) == tree(v2)
