"""Tests for the hwe-bench shims: tools/hwe_docker_shim.py (layer unpacking, run-option parsing,
image path resolution) and tools/hwe_lockf_shim.py (exclusive lock, heavy-lock slots).

The bwrap side (`docker run` itself) needs Linux, bubblewrap and the unpacked image, so it is
exercised on hwe-bench by tools/hwe_harden_setup.sh's smoke step and the hardens; these tests
cover the parts that decide what gets written to disk and what gets locked, on any host.
"""
import hashlib
import io
import os
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hwe_docker_shim as shim  # noqa: E402

LOCKF = [sys.executable, "-I", str(Path(__file__).resolve().parent / "hwe_lockf_shim.py")]


def tar_layer(path, entries):
    """entries: (name, kind, payload, mode); kind in file, dir, symlink, link, chr."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, kind, payload, mode in entries:
            ti = tarfile.TarInfo(name)
            ti.mode = mode
            data = None
            if kind == "file":
                data = payload.encode()
                ti.size = len(data)
            elif kind == "dir":
                ti.type = tarfile.DIRTYPE
            elif kind == "symlink":
                ti.type, ti.linkname = tarfile.SYMTYPE, payload
            elif kind == "link":
                ti.type, ti.linkname = tarfile.LNKTYPE, payload
            elif kind == "chr":
                ti.type = tarfile.CHRTYPE
            tf.addfile(ti, io.BytesIO(data) if data is not None else None)
    raw = buf.getvalue()
    path.write_bytes(raw)
    return (path, "application/vnd.oci.image.layer.v1.tar", hashlib.sha256(raw).hexdigest())


def test_unpack_layers_whiteouts_modes_links(tmp_path):
    l1 = tar_layer(tmp_path / "l1.tar", [
        ("a", "dir", None, 0o755), ("a/gone", "file", "x", 0o644),
        ("a/kept", "file", "k", 0o644), ("b", "dir", None, 0o755),
        ("b/old1", "file", "o", 0o644), ("b/sub", "dir", None, 0o555),
        ("b/sub/old2", "file", "o", 0o444),
        ("ro", "dir", None, 0o555), ("ro/f", "file", "r", 0o444),
        ("abs", "symlink", "/nix/store/x", 0o777), ("hard", "link", "a/kept", 0o644),
        ("suid", "file", "s", 0o4755), ("dev/null", "chr", None, 0o666),
        ("over", "file", "lower", 0o444),
    ])
    l2 = tar_layer(tmp_path / "l2.tar", [
        ("a/.wh.gone", "file", "", 0o644),
        ("b/.wh..wh..opq", "file", "", 0o644), ("b/new", "file", "n", 0o644),
        ("ro/g", "file", "g", 0o444), ("over", "file", "upper", 0o644),
    ])
    root = tmp_path / "root"
    shim.unpack_layers(root, [l1, l2])
    assert not (root / "a/gone").exists() and (root / "a/kept").read_text() == "k"
    assert sorted(p.name for p in (root / "b").iterdir()) == ["new"]   # opaque dir
    assert (root / "ro/g").read_text() == "g"                           # read-only lower dir
    assert (root / "ro").stat().st_mode & 0o777 == 0o555                # mode restored
    assert os.readlink(root / "abs") == "/nix/store/x"                  # kept verbatim
    assert (root / "hard").stat().st_ino == (root / "a/kept").stat().st_ino
    assert (root / "suid").stat().st_mode & 0o7000 == 0                 # setuid dropped
    assert not (root / "dev/null").exists()                             # no device nodes
    assert (root / "over").read_text() == "upper"                       # upper layer wins


@pytest.mark.parametrize("entries", [
    [("esc", "symlink", "/tmp", 0o777), ("esc/pwned", "file", "x", 0o644)],
    [("d", "dir", None, 0o755), ("d/l", "symlink", "../..", 0o777), ("d/l/x", "file", "", 0o644)],
    [("../up", "file", "x", 0o644)],
])
def test_unpack_refuses_escapes(tmp_path, entries):
    layer = tar_layer(tmp_path / "bad.tar", entries)
    with pytest.raises(shim.CliError):
        shim.unpack_layers(tmp_path / "root", [layer])
    assert not (Path("/tmp/pwned")).exists()


def test_unpack_checks_diff_id(tmp_path):
    path, mt, _ = tar_layer(tmp_path / "l.tar", [("f", "file", "x", 0o644)])
    with pytest.raises(shim.CliError, match="diff_id"):
        shim.unpack_layers(tmp_path / "root", [(path, mt, "0" * 64)])


def test_parse_run_subset():
    o, img, cmd = shim.parse_run([
        "--rm", "-i", "--user", "1000:1000", "-v", "/a:/a", "--volume=/b:/b:ro",
        "-e", "HOME=/tmp", "--env=X=1", "-w", "/a", "--label", "bonetto.harden.out=/o",
        "ghcr.io/r/i@sha256:" + "0" * 64, "librelane", "--flag", "-v"])
    assert o["interactive"] and o["user"] == "1000:1000" and o["workdir"] == "/a"
    assert o["vols"] == ["/a:/a", "/b:/b:ro"] and o["env"] == ["HOME=/tmp", "X=1"]
    assert o["labels"] == {"bonetto.harden.out": "/o"}
    assert img.endswith("0" * 64) and cmd == ["librelane", "--flag", "-v"]
    for bad in (["--privileged", "img"], ["-it", "img"], ["--network", "host", "img"], []):
        with pytest.raises(shim.CliError):
            shim.parse_run(bad)


def test_unpinned_image_refused():
    with pytest.raises(shim.CliError, match="pinned by digest"):
        shim.image_dir("ghcr.io/librelane/librelane:3.0.14")


def test_in_root_follows_absolute_symlinks_inside(tmp_path):
    (tmp_path / "nix/store/p/etc").mkdir(parents=True)
    (tmp_path / "nix/store/p/etc/passwd").write_text("root:x:0:0::/root:/bin/sh\n")
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc/passwd").symlink_to("/nix/store/p/etc/passwd")
    (tmp_path / "prof").symlink_to("nix/store/p")
    assert shim.in_root(tmp_path, "/etc/passwd") == tmp_path / "nix/store/p/etc/passwd"
    assert shim.in_root(tmp_path, "/prof/etc") == tmp_path / "nix/store/p/etc"
    assert shim.passwd_home(tmp_path, "0") == "/root"
    assert shim.passwd_home(tmp_path, "1000") == "/"


def lockf(*args, env=None, **kw):
    return subprocess.Popen([*LOCKF, *args], env={**os.environ, **(env or {})}, **kw)


def test_lockf_exclusive_and_status(tmp_path):
    lock = str(tmp_path / "l.lock")
    assert lockf("-k", lock, "sh", "-c", "exit 7").wait() == 7
    holder = lockf("-k", lock, "sleep", "20")
    try:
        time.sleep(1)
        assert lockf("-k", "-s", "-t", "0", lock, "true").wait() == 75
    finally:
        holder.kill()
        holder.wait()
    assert lockf("-k", "-t", "5", lock, "true").wait() == 0
    assert lockf("-n", str(tmp_path / "missing"), "true").wait() == 66
    assert lockf(lock, "true").wait() == 0 and not os.path.exists(lock)  # no -k: removed


def test_lockf_heavy_slots(tmp_path):
    heavy = str(tmp_path / "heavy.lock")
    env = {"BONETTO_HWE_HEAVY_LOCK_PATH": heavy, "BONETTO_HWE_HEAVY_SLOTS": "2",
           "BONETTO_HWE_MIN_FREE_GB": "0"}
    holders = [lockf("-k", heavy, "sleep", "20", env=env) for _ in range(2)]
    try:
        time.sleep(1.5)
        assert all(h.poll() is None for h in holders)               # both admitted at once
        assert lockf("-k", "-s", "-t", "0", heavy, "true", env=env).wait() == 75  # full
        other = str(tmp_path / "other.lock")                        # other files: exclusive
        h = lockf("-k", other, "sleep", "20", env=env)
        time.sleep(1)
        assert lockf("-k", "-s", "-t", "0", other, "true", env=env).wait() == 75
        h.kill()
        h.wait()
    finally:
        for h in holders:
            h.kill()
            h.wait()
    assert lockf("-k", "-t", "5", heavy, "true", env=env).wait() == 0


@pytest.mark.skipif(not Path("/proc/meminfo").exists(), reason="needs /proc/meminfo (Linux)")
def test_lockf_heavy_memory_gate(tmp_path):
    heavy = str(tmp_path / "heavy.lock")
    env = {"BONETTO_HWE_HEAVY_LOCK_PATH": heavy, "BONETTO_HWE_MIN_FREE_GB": "100000"}
    assert lockf("-k", "-s", "-t", "1", heavy, "true", env=env).wait() == 75
