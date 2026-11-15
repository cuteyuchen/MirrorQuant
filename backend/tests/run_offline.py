"""Run pytest in temporary storage with Linux seccomp network blocking.

Usage: backend/.venv/bin/python backend/tests/run_offline.py [test paths]
No application imports occur before isolation. Fails if seccomp is unavailable.
"""
from __future__ import annotations

import base64
import ctypes
import ctypes.util
import errno
import os
from pathlib import Path
import socket
import stat
import sys
import tempfile


def block_network() -> None:
    for descriptor in Path("/proc/self/fd").iterdir():
        try:
            fd = int(descriptor.name)
            if stat.S_ISSOCK(os.fstat(fd).st_mode):
                with socket.socket(fileno=os.dup(fd)) as inherited:
                    if inherited.family != socket.AF_UNIX:
                        raise RuntimeError("Offline runner inherited a network socket")
        except OSError:
            continue
    lib = ctypes.CDLL(ctypes.util.find_library("seccomp") or "libseccomp.so.2", use_errno=True)
    lib.seccomp_init.argtypes = [ctypes.c_uint32]
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_syscall_resolve_name.restype = ctypes.c_int
    lib.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    lib.seccomp_load.argtypes = [ctypes.c_void_p]
    lib.seccomp_release.argtypes = [ctypes.c_void_p]

    class Compare(ctypes.Structure):
        _fields_ = [("arg", ctypes.c_uint), ("op", ctypes.c_int),
                    ("datum_a", ctypes.c_uint64), ("datum_b", ctypes.c_uint64)]

    context = lib.seccomp_init(0x7FFF0000)  # SCMP_ACT_ALLOW
    if not context:
        raise RuntimeError("Cannot initialize offline network isolation")
    try:
        deny = 0x00050000 | errno.EPERM  # SCMP_ACT_ERRNO
        syscall = lib.seccomp_syscall_resolve_name(b"socket")
        # Permit only local socket pairs used by asyncio; every other family fails.
        comparison = Compare(0, 1, socket.AF_UNIX, 0)  # SCMP_CMP_NE
        if syscall < 0 or lib.seccomp_rule_add(context, deny, syscall, 1, comparison) != 0:
            raise RuntimeError("Cannot block network sockets")
        # Local asyncio socket pairs must retain send/recv for worker wakeups.
        # Network sockets cannot be created or inherited; connection is blocked too.
        for name in (b"connect", b"io_uring_setup"):
            syscall = lib.seccomp_syscall_resolve_name(name)
            if syscall >= 0 and lib.seccomp_rule_add(context, deny, syscall, 0) != 0:
                raise RuntimeError("Cannot block network syscall")
        if lib.seccomp_load(context) != 0:
            raise RuntimeError("Cannot enforce offline network isolation")
    finally:
        lib.seccomp_release(context)
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            with socket.socket(family, socket.SOCK_STREAM):
                pass
        except PermissionError:
            continue
        raise RuntimeError("Offline network isolation self-check failed")


def guard_files(root: Path, backend: Path) -> None:
    def inside(value) -> bool:
        if isinstance(value, int):
            return True  # Writable descriptors can only originate from guarded opens.
        return Path(os.fsdecode(value)).resolve().is_relative_to(root)

    def audit(event, args):
        if event == "open":
            path, mode, flags = args
            if not isinstance(path, int):
                resolved = Path(os.fsdecode(path)).resolve()
                if (resolved.name in (".master_key", ".jwt_secret") or resolved.name.startswith(".env") or
                        resolved.is_relative_to(backend / "data") or
                        resolved.is_relative_to(Path("/srv/apps/mirror-quant-data"))) and not inside(path):
                    raise PermissionError("Offline tests cannot read production configuration/data")
            writes = flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
            if writes and not inside(path):
                raise PermissionError("Offline tests cannot write outside temporary storage")
        elif event in ("os.mkdir", "os.remove", "os.rmdir", "os.chmod", "os.chown", "os.utime", "os.truncate"):
            if not inside(args[0]):
                raise PermissionError("Offline tests cannot mutate outside temporary storage")
        elif event in ("os.rename", "os.link", "os.symlink"):
            if not inside(args[0]) or not inside(args[1]):
                raise PermissionError("Offline tests cannot move files outside temporary storage")
        elif event == "sqlite3.connect":
            if args[0] != ":memory:" and not inside(args[0]):
                raise PermissionError("Offline tests cannot open a non-temporary database")
        elif event in ("subprocess.Popen", "os.system", "os.exec", "os.posix_spawn"):
            raise PermissionError("Offline tests cannot launch unguarded subprocesses")
    sys.addaudithook(audit)


def main() -> int:
    backend = Path(__file__).resolve().parents[1]
    paths = [str((Path.cwd() / p).resolve()) for p in sys.argv[1:]] or [str(backend / "tests")]
    sys.dont_write_bytecode = True
    block_network()
    with tempfile.TemporaryDirectory(prefix="mirrorquant-offline-") as directory:
        root = Path(directory).resolve()
        os.chdir(root)
        # Discard inherited application/proxy settings and use synthetic secrets.
        retained = {key: os.environ[key] for key in ("PATH", "LANG") if key in os.environ}
        os.environ.clear()
        os.environ.update(retained)
        os.environ.update(MQ_ENV="test", MQ_ENABLE_BACKGROUND_ENGINE="false",
            MQ_DATA_DIR=str(root / "data"), MQ_DATABASE_URL="sqlite+aiosqlite:///:memory:",
            MQ_MASTER_KEY=base64.urlsafe_b64encode(b"0" * 32).decode(),
            MQ_JWT_SECRET="OfflineSyntheticJWTSecretForTestsOnly32!",
            MQ_BOOTSTRAP_TOKEN="OfflineSyntheticBootstrapForTestsOnly32!",
            PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", TMPDIR=str(root))
        tempfile.tempdir = str(root)
        guard_files(root, backend)
        sys.path.insert(0, str(backend))
        from app.core.config import Settings
        Settings.model_config["env_file"] = None
        import pytest
        print("Offline isolation verified: network syscalls blocked; writes confined to temporary storage.")
        return pytest.main(["-c", str(backend / "pyproject.toml"), "-p", "pytest_asyncio.plugin",
            "-o", f"cache_dir={root / 'pytest-cache'}", "--basetemp", str(root / "pytest-tmp"),
            "-o", f"log_file={root / 'pytest.log'}",
            "--tb=short", "-q", *paths])


if __name__ == "__main__":
    raise SystemExit(main())
