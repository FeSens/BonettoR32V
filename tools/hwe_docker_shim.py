#!/usr/bin/env python3
"""BonettoR32V: a minimal `docker` CLI for hwe-bench, which has no container engine.

hwe-bench has no docker, podman, apptainer or nix, but it has bubblewrap and unprivileged user
namespaces. This shim runs the pinned LibreLane image as an unpacked root filesystem under
bwrap, so tools/harden_tt.sh, tools/area.py and physical/tilefit.py run there unchanged.

It implements exactly the subset those tools (and tools/test_physical.py) use, nothing more:

  docker run [--rm] [-i] [--user UID[:GID]] [-v SRC:DST[:ro|:rw]]... [-e KEY[=VAL]]...
             [-w DIR] [--label KEY=VAL]... IMAGE@sha256:<digest> [CMD [ARG...]]
  docker ps [-q] [--no-trunc] [--filter label=KEY[=VAL]]...
  docker kill [-s SIGNAL] ID...
  docker pull IMAGE@sha256:<digest>     (same as `hwe_docker_shim.py pull`)
  docker info | docker version | docker --version

Anything else exits 125, docker's own status for CLI errors, so nothing is silently ignored.

Semantics, docker's where it matters:
- Only images pinned by digest run. `pull` fetches the host's platform (linux/amd64 here) from
  a multi-arch index through the registry API with an anonymous token, verifies the index,
  manifest, config and every layer blob against their sha256 digests and each uncompressed
  layer against the config's diff_ids, and unpacks the layers in order (OCI whiteouts and
  opaque directories handled, device nodes and setuid bits dropped, no entry may go through
  a symlink). Layout under $BONETTO_LL_HOME (default ~/bonetto-librelane):
  images/sha256-<digest>/{index,manifest,config}.json, rootfs/, meta.json.
- The container's root is an overlay: the image rootfs is the read-only lower layer, a
  per-container upper directory on disk takes writes, and it is deleted on exit (`--rm`
  semantics always; nothing is kept). /proc, /dev, a read-only /sys, the host's resolv.conf
  and hosts, and the -v binds (sorted by depth; a missing source directory is created, as
  docker does) are mounted on top.
- New user, pid, ipc and uts namespaces; the network is the host's. --user maps that uid and
  gid onto the caller (default 0:0, i.e. root inside, files owned by the caller outside).
- The environment is the image's Env, HOSTNAME, HOME (from the image's /etc/passwd, else /),
  then the -e values; nothing leaks in from the host. The working directory is -w, the image's
  WorkingDir, or /. The command is the image's Entrypoint plus CMD (or the image's Cmd).
- stdin is /dev/null unless -i. A command missing from the image's PATH exits 127.
- The exit status is the command's (128 + signal when it was killed).
- `--die-with-parent`: if this CLI process dies, everything in the container is SIGKILLed,
  so a killed `docker run` never leaves a container behind. SIGTERM, SIGINT, SIGHUP and SIGQUIT
  sent to this process are forwarded to the container's command (docker's --sig-proxy).
- `ps` and `kill` work from per-container records in $BONETTO_LL_HOME/containers/<id>/; a
  record whose process is gone (pid and start time checked) is removed when seen.
"""
import gzip
import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HOME = Path(os.environ.get("BONETTO_LL_HOME", str(Path.home() / "bonetto-librelane")))
IMAGES = HOME / "images"
BLOBS = HOME / "blobs" / "sha256"
CONTAINERS = HOME / "containers"
BWRAP = os.environ.get("BONETTO_BWRAP", "/usr/bin/bwrap")
DEFAULT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
REF_RE = re.compile(r"^([A-Za-z0-9.-]+(?::[0-9]+)?)/([a-z0-9._/-]+)@sha256:([0-9a-f]{64})$")

MT_OCI_INDEX = "application/vnd.oci.image.index.v1+json"
MT_DOCKER_LIST = "application/vnd.docker.distribution.manifest.list.v2+json"
MT_OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
MT_DOCKER_MANIFEST = "application/vnd.docker.distribution.manifest.v2+json"
GZIP_LAYERS = {"application/vnd.docker.image.rootfs.diff.tar.gzip",
               "application/vnd.oci.image.layer.v1.tar+gzip"}
PLAIN_LAYERS = {"application/vnd.oci.image.layer.v1.tar"}


class CliError(Exception):
    """A usage or setup error: message on stderr, exit 125 (docker's CLI error status)."""


def err(msg):
    print(f"docker (bonetto bwrap shim): {msg}", file=sys.stderr)


def host_arch():
    m = platform.machine()
    return {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(m, m)


def image_dir(ref):
    m = REF_RE.match(ref)
    if not m:
        raise CliError(f"image {ref!r}: only references pinned by digest (REPO@sha256:...) "
                       f"are supported")
    return IMAGES / f"sha256-{m.group(3)}"


def load_image(ref):
    d = image_dir(ref)
    meta = d / "meta.json"
    if not (meta.is_file() and (d / "rootfs").is_dir()):
        raise CliError(f"Unable to find image {ref!r} locally. Run tools/hwe_harden_setup.sh "
                       f"(or `docker pull {ref}`)")
    cfg = json.loads((d / "config.json").read_text())
    return d / "rootfs", cfg.get("config") or {}, json.loads(meta.read_text())


# ------------------------------------------------------------------------------ pull
class Registry:
    def __init__(self, host, repo):
        self.host, self.repo, self.token = host, repo, None

    def _open(self, url, accept=None):
        req = urllib.request.Request(url)
        if accept:
            req.add_header("Accept", accept)
        if self.token:
            # not forwarded to the blob CDN a registry redirects to
            req.add_unredirected_header("Authorization", f"Bearer {self.token}")
        return urllib.request.urlopen(req, timeout=120)

    def _auth(self, www):
        m = re.match(r"Bearer\s+(.*)", www or "", re.I)
        if not m:
            raise CliError(f"registry {self.host}: unsupported auth challenge {www!r}")
        params = dict(re.findall(r'(\w+)="([^"]*)"', m.group(1)))
        q = {"scope": f"repository:{self.repo}:pull"}
        if "service" in params:
            q["service"] = params["service"]
        url = params["realm"] + "?" + urllib.parse.urlencode(q)
        with urllib.request.urlopen(url, timeout=60) as r:
            j = json.load(r)
        self.token = j.get("token") or j.get("access_token")

    def get(self, path, accept=None):
        url = f"https://{self.host}/v2/{self.repo}/{path}"
        try:
            return self._open(url, accept)
        except urllib.error.HTTPError as e:
            if e.code != 401 or self.token:
                raise
            self._auth(e.headers.get("WWW-Authenticate"))
            return self._open(url, accept)

    def manifest(self, digest):
        accept = ", ".join([MT_OCI_INDEX, MT_DOCKER_LIST, MT_OCI_MANIFEST, MT_DOCKER_MANIFEST])
        with self.get(f"manifests/sha256:{digest}", accept) as r:
            body = r.read()
        got = hashlib.sha256(body).hexdigest()
        if got != digest:
            raise CliError(f"manifest digest mismatch: wanted {digest}, got {got}")
        return body

    def blob(self, digest, size=None, to=None):
        """Small blobs are returned; with `to`, streamed to that file, verified, then renamed."""
        if to is None:
            with self.get(f"blobs/sha256:{digest}") as r:
                body = r.read()
            if hashlib.sha256(body).hexdigest() != digest:
                raise CliError(f"blob {digest}: digest mismatch")
            return body
        part = to.with_name(to.name + ".partial")
        h, n, last = hashlib.sha256(), 0, 0
        with self.get(f"blobs/sha256:{digest}") as r, open(part, "wb") as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                h.update(chunk)
                f.write(chunk)
                n += len(chunk)
                if n - last >= 200 << 20:
                    last = n
                    print(f"  {n >> 20} MiB" + (f" of {size >> 20}" if size else ""),
                          file=sys.stderr, flush=True)
        if h.hexdigest() != digest or (size is not None and n != size):
            part.unlink()
            raise CliError(f"blob {digest}: digest or size mismatch ({n} bytes)")
        part.rename(to)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class HashReader:
    """File-like wrapper hashing everything read (for diff_id checks)."""

    def __init__(self, f):
        self.f, self.h = f, hashlib.sha256()

    def read(self, n=-1):
        b = self.f.read(n)
        self.h.update(b)
        return b

    def drain(self):
        while self.read(1 << 20):
            pass
        return self.h.hexdigest()


def rel_name(name):
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise CliError(f"layer entry {name!r} escapes the root")
    return "/".join(parts)


class Unpacker:
    def __init__(self, root):
        self.root = root
        self.safe_dirs = {""}
        self.dir_modes = {}

    def check_parent(self, rel):
        """No existing component of rel's parent may be a symlink (it could point outside)."""
        parent = rel.rpartition("/")[0]
        if parent in self.safe_dirs:
            return
        acc = ""
        for p in parent.split("/"):
            acc = f"{acc}/{p}" if acc else p
            if acc in self.safe_dirs:
                continue
            path = self.root / acc
            if path.is_symlink():
                raise CliError(f"layer entry {rel!r} goes through the symlink {acc!r}")
            if path.is_dir():
                self.safe_dirs.add(acc)

    def remove(self, path):
        if path.is_symlink() or not path.is_dir():
            if os.path.lexists(path):
                path.unlink()
            return
        for d, _, _ in os.walk(path):  # lower-layer dirs may be read-only (nix store)
            os.chmod(d, 0o700)
        shutil.rmtree(path)

    def whiteouts(self, members):
        for name in members:
            rel = rel_name(name)
            base = rel.rpartition("/")[2]
            if not base.startswith(".wh."):
                continue
            self.check_parent(rel)
            parent = self.root / rel.rpartition("/")[0]
            if base == ".wh..wh..opq":
                if parent.is_dir() and not parent.is_symlink():
                    for child in parent.iterdir():
                        self.remove(child)
            else:
                self.remove(parent / base[len(".wh."):])

    def filter(self, m, _dest):
        rel = rel_name(m.name)
        base = rel.rpartition("/")[2]
        if not rel or base.startswith(".wh."):
            return None
        if m.ischr() or m.isblk() or m.isfifo():
            return None
        self.check_parent(rel)
        path = self.root / rel
        if os.path.lexists(path):
            if not (m.isdir() and path.is_dir() and not path.is_symlink()):
                self.remove(path)
        mode = m.mode & 0o1777  # no setuid / setgid
        if m.isdir():  # writable while unpacking; the image's mode is restored in finish()
            self.dir_modes[rel] = mode
            mode |= 0o700
        linkname = m.linkname
        if m.islnk():
            linkname = rel_name(m.linkname)
            self.check_parent(linkname)
        return m.replace(name=rel, mode=mode, linkname=linkname, uid=0, gid=0,
                         uname="", gname="", deep=False)

    def finish(self):
        for rel in sorted(self.dir_modes, key=lambda r: r.count("/"), reverse=True):
            p = self.root / rel
            if p.is_dir() and not p.is_symlink():
                os.chmod(p, self.dir_modes[rel])


def layer_stream(path, media_type):
    if media_type in GZIP_LAYERS:
        return gzip.open(path, "rb")
    if media_type in PLAIN_LAYERS:
        return open(path, "rb")
    raise CliError(f"unsupported layer media type {media_type}")


def unpack_layers(root, layers):
    """Unpack [(blob path, media type, diff_id hex)], lowest layer first, into a fresh root."""
    if os.path.lexists(root):
        Unpacker(root).remove(root)
    root.mkdir(parents=True)
    up = Unpacker(root)
    for i, (blob, mt, diff_id) in enumerate(layers):
        print(f"layer {i + 1}/{len(layers)} {blob.name[:12]}: unpacking", file=sys.stderr,
              flush=True)
        if i > 0:  # whiteouts hide lower layers only: apply them before this layer's entries
            with layer_stream(blob, mt) as f, tarfile.open(fileobj=f, mode="r|") as tf:
                up.whiteouts(x.name for x in tf)
        with layer_stream(blob, mt) as f:
            hr = HashReader(f)
            with tarfile.open(fileobj=hr, mode="r|") as tf:
                for member in tf:
                    tf.extract(member, root, filter=up.filter)
            got = hr.drain()
        if got != diff_id:
            raise CliError(f"layer {blob.name}: uncompressed digest {got} != diff_id {diff_id}")
    up.finish()


def pull(ref, keep_blobs=False):
    m = REF_RE.match(ref)
    if not m:
        raise CliError(f"pull {ref!r}: only references pinned by digest are supported")
    host, repo, digest = m.groups()
    d = image_dir(ref)
    if (d / "meta.json").is_file() and (d / "rootfs").is_dir():
        print(f"{ref}: already unpacked in {d}", file=sys.stderr)
        return 0
    reg = Registry(host, repo)
    d.mkdir(parents=True, exist_ok=True)
    body = reg.manifest(digest)
    top = json.loads(body)
    arch = host_arch()
    if top.get("mediaType") in (MT_OCI_INDEX, MT_DOCKER_LIST) or "manifests" in top:
        (d / "index.json").write_bytes(body)
        cands = [x for x in top["manifests"]
                 if x.get("platform", {}).get("os") == "linux"
                 and x.get("platform", {}).get("architecture") == arch]
        if len(cands) != 1:
            raise CliError(f"{ref}: {len(cands)} linux/{arch} manifests in the index")
        mdigest = cands[0]["digest"].split(":", 1)[1]
        body = reg.manifest(mdigest)
        man = json.loads(body)
    else:
        mdigest, man = digest, top
    (d / "manifest.json").write_bytes(body)
    cdigest = man["config"]["digest"].split(":", 1)[1]
    cbody = reg.blob(cdigest)
    (d / "config.json").write_bytes(cbody)
    cfg = json.loads(cbody)
    if cfg.get("architecture") != arch or cfg.get("os") != "linux":
        raise CliError(f"{ref}: config says {cfg.get('os')}/{cfg.get('architecture')}")
    diff_ids = [x.split(":", 1)[1] for x in cfg["rootfs"]["diff_ids"]]
    layers = man["layers"]
    if len(diff_ids) != len(layers):
        raise CliError(f"{ref}: {len(layers)} layers but {len(diff_ids)} diff_ids")
    BLOBS.mkdir(parents=True, exist_ok=True)
    for i, layer in enumerate(layers):
        ld = layer["digest"].split(":", 1)[1]
        blob = BLOBS / ld
        if blob.is_file() and sha256_file(blob) == ld:
            print(f"layer {i + 1}/{len(layers)} {ld[:12]}: cached", file=sys.stderr)
            continue
        print(f"layer {i + 1}/{len(layers)} {ld[:12]}: downloading {layer['size'] >> 20} MiB",
              file=sys.stderr, flush=True)
        reg.blob(ld, layer["size"], blob)

    part = d / "rootfs.partial"
    unpack_layers(part, [(BLOBS / x["digest"].split(":", 1)[1], x["mediaType"], diff_ids[i])
                         for i, x in enumerate(layers)])
    part.rename(d / "rootfs")
    meta = {"ref": ref, "platform": f"linux/{arch}", "manifest": f"sha256:{mdigest}",
            "config": f"sha256:{cdigest}", "layers": [x["digest"] for x in layers],
            "unpacked": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    tmp = d / "meta.json.tmp"
    tmp.write_text(json.dumps(meta, indent=2) + "\n")
    tmp.rename(d / "meta.json")
    if not keep_blobs:
        for layer in layers:
            (BLOBS / layer["digest"].split(":", 1)[1]).unlink(missing_ok=True)
    print(f"{ref}: linux/{arch} manifest sha256:{mdigest} unpacked in {d / 'rootfs'}",
          file=sys.stderr)
    return 0


# ------------------------------------------------------------------------------ records
def proc_start(pid):
    try:
        s = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    return s.rpartition(")")[2].split()[19]  # field 22: start time in clock ticks


def live_records():
    out = []
    if not CONTAINERS.is_dir():
        return out
    for d in CONTAINERS.iterdir():
        st = d / "state.json"
        try:
            rec = json.loads(st.read_text())
        except (OSError, ValueError):
            # a record being created, or a leftover without state: drop it once it is old
            try:
                if time.time() - d.stat().st_mtime > 600:
                    rmtree_force(d)
            except OSError:
                pass
            continue
        pid = rec.get("pid")
        if pid and proc_start(pid) == rec.get("pid_start"):
            out.append(rec)
        else:
            rmtree_force(d)
    return out


def rmtree_force(path):
    def onexc(_fn, p, _e):
        try:
            os.chmod(os.path.dirname(p), 0o700)
            os.chmod(p, 0o700)
        except OSError:
            pass
        if os.path.isdir(p) and not os.path.islink(p):
            shutil.rmtree(p, ignore_errors=True)
        else:
            try:
                os.unlink(p)
            except OSError:
                pass
    if os.path.lexists(path):
        shutil.rmtree(path, onexc=onexc)


def command_pids(bwrap_pid):
    """The container's command: the children of bwrap's init (bwrap -> init -> command)."""
    try:
        init = [int(x) for x in Path(f"/proc/{bwrap_pid}/task/{bwrap_pid}/children")
                .read_text().split()]
    except OSError:
        return []
    out = []
    for i in init:
        try:
            out += [int(x) for x in Path(f"/proc/{i}/task/{i}/children").read_text().split()]
        except OSError:
            pass
    return out


# ------------------------------------------------------------------------------ run
def take_value(args, i, opt):
    a = args[i]
    if a.startswith(opt + "="):
        return a[len(opt) + 1:], i + 1
    if i + 1 >= len(args):
        raise CliError(f"flag needs an argument: {opt}")
    return args[i + 1], i + 2


def parse_run(args):
    o = {"interactive": False, "user": None, "vols": [], "env": [], "workdir": None,
         "labels": {}}
    value_opts = {"--user": "user", "-u": "user", "--volume": "vols", "-v": "vols",
                  "--env": "env", "-e": "env", "--workdir": "workdir", "-w": "workdir",
                  "--label": "labels", "-l": "labels"}
    i = 0
    while i < len(args):
        a = args[i]
        if not a.startswith("-") or a == "-":
            break
        if a == "--":
            i += 1
            break
        if a == "--rm":
            i += 1
            continue
        if a in ("-i", "--interactive"):
            o["interactive"] = True
            i += 1
            continue
        opt = a.split("=", 1)[0] if a.startswith("--") else a
        if opt not in value_opts:
            raise CliError(f"unsupported docker run option {a!r}")
        v, i = take_value(args, i, opt)
        k = value_opts[opt]
        if k == "vols" or k == "env":
            o[k].append(v)
        elif k == "labels":
            key, _, val = v.partition("=")
            o["labels"][key] = val
        else:
            o[k] = v
    if i >= len(args):
        raise CliError("docker run requires an image")
    return o, args[i], args[i + 1:]


def in_root(rootfs, path, depth=0):
    """Host path of the image path `path`, following symlinks as if rootfs were /."""
    if depth > 40:
        return None
    cur = ""
    parts = [p for p in path.split("/") if p not in ("", ".")]
    for i, p in enumerate(parts):
        if p == "..":
            cur = cur.rpartition("/")[0]
            continue
        nxt = f"{cur}/{p}"
        host = rootfs / nxt.lstrip("/")
        if host.is_symlink():
            target = os.readlink(host)
            base = target if target.startswith("/") else f"{cur}/{target}"
            rest = "/".join(parts[i + 1:])
            return in_root(rootfs, f"{base}/{rest}" if rest else base, depth + 1)
        cur = nxt
    return rootfs / cur.lstrip("/")


def passwd_home(rootfs, uid):
    path = in_root(rootfs, "/etc/passwd") or rootfs / "etc" / "passwd"
    try:
        for line in path.read_text().splitlines():
            f = line.split(":")
            if len(f) >= 6 and f[2] == str(uid):
                return f[5]
    except OSError:
        pass
    return "/"


def find_in_image(rootfs, argv0, path_var, binds):
    if "/" in argv0:
        cands = [argv0]
    else:
        cands = [f"{d}/{argv0}" for d in path_var.split(":") if d]
    for c in cands:
        if not c.startswith("/"):
            return True  # relative to the workdir: let bwrap decide
        d, _, base = c.rpartition("/")
        host_dir = in_root(rootfs, d)
        if host_dir is not None and os.path.lexists(host_dir / base):
            return True
        for src, dst in binds:
            if c == dst or c.startswith(dst.rstrip("/") + "/"):
                if os.path.lexists(src + c[len(dst.rstrip("/")):]):
                    return True
    return False


def cmd_run(args):
    o, ref, cmd = parse_run(args)
    rootfs, cfg, _meta = load_image(ref)
    if not os.access(BWRAP, os.X_OK):
        raise CliError(f"{BWRAP} not found")

    user = o["user"] if o["user"] is not None else (cfg.get("User") or "0")
    if user == "root":
        user = "0"
    uid, _, gid = user.partition(":")
    if not uid.isdigit() or (gid and not gid.isdigit()):
        raise CliError(f"--user {user!r}: only numeric UID[:GID] is supported")
    gid = gid or "0"

    env = {}
    for kv in cfg.get("Env") or []:
        k, _, v = kv.partition("=")
        env[k] = v
    env.setdefault("PATH", DEFAULT_PATH)
    cid = secrets.token_hex(32)
    env["HOSTNAME"] = cid[:12]
    env.setdefault("HOME", passwd_home(rootfs, uid))
    for kv in o["env"]:
        k, eq, v = kv.partition("=")
        if eq:
            env[k] = v
        elif k in os.environ:
            env[k] = os.environ[k]

    binds = []
    for v in o["vols"]:
        parts = v.split(":")
        if len(parts) == 2:
            src, dst, mode = parts[0], parts[1], "rw"
        elif len(parts) == 3:
            src, dst, mode = parts
        else:
            raise CliError(f"-v {v!r}: expected SRC:DST[:ro|:rw]")
        if mode not in ("ro", "rw"):
            raise CliError(f"-v {v!r}: unsupported mode {mode!r}")
        if not (src.startswith("/") and dst.startswith("/")):
            raise CliError(f"-v {v!r}: only absolute host paths (no named volumes)")
        os.makedirs(src, exist_ok=True)
        binds.append((os.path.normpath(src), os.path.normpath(dst), mode))
    binds.sort(key=lambda b: len([p for p in b[1].split("/") if p]))

    argv = list(cfg.get("Entrypoint") or []) + (cmd if cmd else list(cfg.get("Cmd") or []))
    if not argv:
        raise CliError("no command given and the image has no Cmd")
    if not find_in_image(rootfs, argv[0], env["PATH"], [(s, d) for s, d, _ in binds]):
        err(f"exec: {argv[0]!r}: executable file not found in $PATH")
        return 127
    workdir = o["workdir"] or cfg.get("WorkingDir") or "/"

    cdir = CONTAINERS / cid
    upper, work = cdir / "upper", cdir / "work"
    upper.mkdir(parents=True)
    work.mkdir()
    bw = [BWRAP, "--unshare-user", "--uid", uid, "--gid", gid,
          "--unshare-pid", "--unshare-ipc", "--unshare-uts", "--unshare-cgroup-try",
          "--hostname", cid[:12], "--die-with-parent",
          "--overlay-src", str(rootfs), "--overlay", str(upper), str(work), "/",
          "--proc", "/proc", "--dev", "/dev", "--ro-bind", "/sys", "/sys"]
    for f in ("/etc/resolv.conf", "/etc/hosts"):
        if os.path.exists(f):
            bw += ["--ro-bind", os.path.realpath(f), f]
    for src, dst, mode in binds:
        bw += ["--ro-bind" if mode == "ro" else "--bind", src, dst]
    bw += ["--chdir", workdir, "--clearenv"]
    for k, v in env.items():
        bw += ["--setenv", k, v]
    bw += ["--"] + argv

    # Forward the usual termination signals to the container's command (docker --sig-proxy).
    proc = None

    def forward(signum, _frame):
        if proc is None:
            return
        pids = command_pids(proc.pid) or [proc.pid]
        for p in pids:
            try:
                os.kill(p, signum)
            except OSError:
                pass

    for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT):
        signal.signal(s, forward)
    try:
        proc = subprocess.Popen(bw, stdin=None if o["interactive"] else subprocess.DEVNULL)
        rec = {"id": cid, "image": ref, "labels": o["labels"], "command": argv,
               "created": time.time(), "pid": proc.pid, "pid_start": proc_start(proc.pid)}
        tmp = cdir / "state.json.tmp"
        tmp.write_text(json.dumps(rec) + "\n")
        tmp.rename(cdir / "state.json")
        while True:
            try:
                rc = proc.wait()
                break
            except InterruptedError:
                continue
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait()
        rmtree_force(cdir)
    return 128 - rc if rc < 0 else rc


# ------------------------------------------------------------------------------ ps / kill
def label_match(rec, filters):
    for f in filters:
        if not f.startswith("label="):
            raise CliError(f"unsupported filter {f!r} (only label=KEY[=VAL])")
        key, eq, val = f[len("label="):].partition("=")
        labels = rec.get("labels") or {}
        if key not in labels or (eq and labels[key] != val):
            return False
    return True


def cmd_ps(args):
    quiet, notrunc, filters, i = False, False, [], 0
    while i < len(args):
        a = args[i]
        if a in ("-q", "--quiet"):
            quiet, i = True, i + 1
        elif a == "--no-trunc":
            notrunc, i = True, i + 1
        elif a.split("=", 1)[0] in ("--filter", "-f"):
            v, i = take_value(args, i, a.split("=", 1)[0])
            filters.append(v)
        elif a in ("-qa", "-aq", "-a", "--all"):  # nothing is kept after exit: same list
            quiet = quiet or "q" in a
            i += 1
        else:
            raise CliError(f"unsupported docker ps option {a!r}")
    recs = [r for r in live_records() if label_match(r, filters)]
    if not quiet:
        print(f"{'CONTAINER ID':<14}{'IMAGE':<24}{'CREATED':<22}COMMAND")
    for r in sorted(recs, key=lambda r: r["created"]):
        cid = r["id"] if notrunc else r["id"][:12]
        if quiet:
            print(cid)
        else:
            created = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["created"]))
            img = r["image"].rpartition("@")[0][-22:]
            print(f"{cid:<14}{img:<24}{created:<22}{' '.join(r['command'])[:60]}")
    return 0


def cmd_kill(args):
    sig, ids, i = signal.SIGKILL, [], 0
    while i < len(args):
        a = args[i]
        if a.split("=", 1)[0] in ("-s", "--signal"):
            v, i = take_value(args, i, a.split("=", 1)[0])
            name = v.upper()
            name = name if name.startswith("SIG") or name.isdigit() else "SIG" + name
            sig = signal.Signals(int(name)) if name.isdigit() else signal.Signals[name]
        elif a.startswith("-"):
            raise CliError(f"unsupported docker kill option {a!r}")
        else:
            ids.append(a)
            i += 1
    if not ids:
        raise CliError("docker kill requires at least one container id")
    recs, rc = live_records(), 0
    for want in ids:
        hits = [r for r in recs if r["id"].startswith(want)]
        if len(hits) != 1:
            print(f"Error response from daemon: No such container: {want}", file=sys.stderr)
            rc = 1
            continue
        pid = hits[0]["pid"]
        # SIGKILL on bwrap takes the whole pid namespace down (--die-with-parent); other
        # signals go to the container's command, as docker sends them to its PID 1.
        targets = [pid] if sig == signal.SIGKILL else (command_pids(pid) or [pid])
        for p in targets:
            try:
                os.kill(p, sig)
            except OSError:
                pass
        print(want)
    return rc


def cmd_info(_args):
    imgs = sorted(p.parent.name for p in IMAGES.glob("*/meta.json")) if IMAGES.is_dir() else []
    ok = os.access(BWRAP, os.X_OK) and bool(imgs)
    print("Server Version: bonetto-bwrap-shim (no daemon; bubblewrap over unpacked images)")
    print(f"Runtime: {BWRAP}")
    print(f"Images: {len(imgs)} in {IMAGES}")
    for i in imgs:
        print(f"  {i}")
    print(f"Running: {len(live_records())}")
    return 0 if ok else 1


def main(argv):
    if not argv:
        print(__doc__)
        return 125
    sub, args = argv[0], argv[1:]
    try:
        if sub == "run":
            return cmd_run(args)
        if sub == "ps":
            return cmd_ps(args)
        if sub == "kill":
            return cmd_kill(args)
        if sub == "pull":
            keep = "--keep-blobs" in args
            refs = [a for a in args if a != "--keep-blobs"]
            if len(refs) != 1:
                raise CliError("usage: pull [--keep-blobs] REPO@sha256:<digest>")
            return pull(refs[0], keep)
        if sub == "info":
            return cmd_info(args)
        if sub in ("version", "--version", "-v"):
            print("Docker version 0.0.0-bonetto-bwrap-shim (bubblewrap "
                  f"{subprocess.run([BWRAP, '--version'], capture_output=True, text=True).stdout.split()[-1]})")
            return 0
        raise CliError(f"unsupported docker command {sub!r}")
    except CliError as e:
        err(str(e))
        return 125


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
