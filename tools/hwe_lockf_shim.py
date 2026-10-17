#!/usr/bin/env python3
"""BonettoR32V: lockf(1) for hwe-bench (Linux has flock(1) but no lockf(1)).

BSD syntax, as macOS's lockf: lockf [-kns] [-t SECONDS] FILE COMMAND [ARG...]
  -k  keep FILE afterwards (without it, FILE is removed once COMMAND exits)
  -n  do not create FILE: exit 66 if it is missing
  -s  silent: no message when the lock can't be had in time
  -t  wait at most SECONDS (0: don't wait), then exit 75 (EX_TEMPFAIL)
A plain exclusive flock(2) on FILE, held while COMMAND runs; the exit status is COMMAND's.

One exception, so several heavy jobs (hardens, tilefit runs) can share hwe-bench: the heavy
lock /tmp/bonetto-heavy.lock is a counting semaphore on this host. Up to
$BONETTO_HWE_HEAVY_SLOTS holders (default 3) run at once (slot files /tmp/bonetto-heavy.lock,
/tmp/bonetto-heavy.lock.slot1, ...), and a holder is admitted only while /proc/meminfo's
MemAvailable is at least $BONETTO_HWE_MIN_FREE_GB (default 8): formal jobs here use ~40 GB.
Every other FILE (tests use private lock files) stays strictly exclusive.
"""
import fcntl
import os
import sys
import time

# the heavy lock (BONETTO_HWE_HEAVY_LOCK_PATH only exists for tools/test_hwe_shims.py)
HEAVY = os.environ.get("BONETTO_HWE_HEAVY_LOCK_PATH", "/tmp/bonetto-heavy.lock")
EX_USAGE, EX_NOINPUT, EX_OSERR, EX_CANTCREAT, EX_TEMPFAIL = 64, 66, 71, 73, 75


def usage():
    print("usage: lockf [-kns] [-t seconds] file command [arguments]", file=sys.stderr)
    sys.exit(EX_USAGE)


def mem_available_gb():
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / (1 << 20)
    except OSError:
        pass
    return float("inf")


def open_lock(path, create):
    flags = os.O_RDWR | (os.O_CREAT if create else 0)
    try:
        return os.open(path, flags, 0o666)
    except FileNotFoundError:
        if not create:
            sys.exit(EX_NOINPUT)
        raise
    except OSError as e:
        print(f"lockf: cannot open {path}: {e}", file=sys.stderr)
        sys.exit(EX_CANTCREAT)


def try_lock(fd):
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def main(argv):
    keep = nocreate = silent = False
    timeout = None
    i = 0
    while i < len(argv) and argv[i].startswith("-") and argv[i] != "-":
        a = argv[i]
        if a == "--":
            i += 1
            break
        if a == "-t" or a.startswith("-t"):
            v = a[2:] if len(a) > 2 else (argv[i + 1] if i + 1 < len(argv) else None)
            if v is None or not v.isdigit():
                usage()
            timeout = int(v)
            i += 1 if len(a) > 2 else 2
            continue
        for c in a[1:]:
            if c == "k":
                keep = True
            elif c == "n":
                nocreate = True
            elif c == "s":
                silent = True
            elif c == "w":
                pass  # open for writing: flock needs no particular mode
            else:
                usage()
        i += 1
    if len(argv) - i < 2:
        usage()
    path, cmd = argv[i], argv[i + 1:]

    deadline = None if timeout is None else time.monotonic() + timeout
    heavy = os.path.abspath(path) == os.path.abspath(HEAVY)
    slots = [path]
    min_gb = 0.0
    if heavy:
        n = max(1, int(os.environ.get("BONETTO_HWE_HEAVY_SLOTS", "3")))
        slots = [path] + [f"{path}.slot{k}" for k in range(1, n)]
        min_gb = float(os.environ.get("BONETTO_HWE_MIN_FREE_GB", "8"))
    fds = [open_lock(p, not nocreate or p != path) for p in slots]

    held, told = None, False
    while held is None:
        free = mem_available_gb()
        if free >= min_gb:
            for k, fd in enumerate(fds):
                if try_lock(fd):
                    held = k
                    break
        if held is not None:
            break
        if not heavy and deadline is None:
            fcntl.flock(fds[0], fcntl.LOCK_EX)  # plain lock: block in the kernel
            held = 0
            break
        if deadline is not None and time.monotonic() >= deadline:
            if not silent:
                print(f"lockf: {path}: already locked", file=sys.stderr)
            sys.exit(EX_TEMPFAIL)
        if heavy and not told and not silent:
            why = (f"MemAvailable {free:.1f} GB < {min_gb:g} GB" if free < min_gb
                   else f"all {len(fds)} heavy slots busy")
            print(f"lockf: waiting for the heavy lock on hwe-bench ({why})", file=sys.stderr)
            told = True
        time.sleep(0.2 if not heavy else 5)

    for k, fd in enumerate(fds):
        if k != held:
            os.close(fd)
    fd = fds[held]
    if keep or heavy:
        # exec with the locked descriptor inherited: the lock lives as long as COMMAND
        os.set_inheritable(fd, True)
        try:
            os.execvp(cmd[0], cmd)
        except OSError as e:
            print(f"lockf: {cmd[0]}: {e}", file=sys.stderr)
            sys.exit(EX_OSERR)
    pid = os.fork()
    if pid == 0:
        os.close(fd)
        try:
            os.execvp(cmd[0], cmd)
        except OSError as e:
            print(f"lockf: {cmd[0]}: {e}", file=sys.stderr)
            os._exit(EX_OSERR)
    while True:
        try:
            _, status = os.waitpid(pid, 0)
            break
        except InterruptedError:
            continue
    try:
        os.unlink(path)
    except OSError:
        pass
    sys.exit(os.waitstatus_to_exitcode(status) if os.WIFEXITED(status)
             else 128 + os.WTERMSIG(status))


if __name__ == "__main__":
    main(sys.argv[1:])
