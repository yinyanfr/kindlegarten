"""Prepare and replace desktop packages from an independent updater process."""

from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import plistlib
import shutil
import stat
import subprocess
import sys
from tempfile import mkdtemp
from time import monotonic, sleep
from zipfile import ZipFile

BUNDLE_ID = "io.github.yinyanfr.kindlegarten"
PORTABLE_MARKER = ".kindlegarten-portable"
READY_VARIABLE = "KINDLEGARTEN_UPDATE_READY"
RESULT_VARIABLE = "KINDLEGARTEN_UPDATE_RESULT"
HELPER_NAME = "KindlegartenUpdater.exe" if os.name == "nt" else "KindlegartenUpdater"


class ManualInstall(Exception):
    pass


@dataclass(frozen=True)
class Installation:
    kind: str
    target: Path

    @property
    def executable(self):
        if self.kind == "macos":
            return self.target / "Contents" / "MacOS" / "Kindlegarten"
        if self.kind == "windows":
            return self.target / "Kindlegarten.exe"
        return self.target


def appimage(path):
    if not path.is_file() or path.is_symlink():
        return False
    with path.open("rb") as stream:
        header = stream.read(11)
    return header.startswith(b"\x7fELF") and header[8:11] == b"AI\x02"


def bundle_id(path):
    try:
        with (path / "Contents" / "Info.plist").open("rb") as stream:
            return plistlib.load(stream).get("CFBundleIdentifier")
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None


def installed_package():
    if not getattr(sys, "frozen", False):
        return None
    executable = Path(sys.executable)
    if sys.platform == "darwin":
        bundle = executable.parents[2]
        if bundle.suffix == ".app" and bundle_id(bundle) == BUNDLE_ID:
            return Installation("macos", bundle)
    if sys.platform == "win32" and (executable.parent / PORTABLE_MARKER).is_file():
        return Installation("windows", executable.parent)
    if sys.platform.startswith("linux") and (name := os.environ.get("APPIMAGE")):
        image = Path(name)
        if image.is_absolute() and appimage(image):
            return Installation("linux", image)
    return None


def updater_executable():
    base = getattr(sys, "_MEIPASS", "")
    return Path(base) / "updater" / HELPER_NAME if base else None


def can_install(installation):
    helper = updater_executable()
    if not (installation and helper and helper.is_file() and not installation.target.is_symlink()
            and os.access(installation.target.parent, os.W_OK) and os.access(installation.target, os.W_OK)):
        return False
    if installation.kind == "windows":
        return all(path.name in {"Kindlegarten.exe", "_internal", PORTABLE_MARKER}
                   for path in installation.target.iterdir())
    return True


def safe_unpack(archive, destination, root, *, symlinks=False):
    """Accept only the package's expected tree. Write symlinks after regular files."""
    links = []
    names = set()
    with ZipFile(archive) as package:
        for item in package.infolist():
            path = PurePosixPath(item.filename)
            if path.parts and path.parts[0] == "__MACOSX" and symlinks:
                continue
            if (not path.parts or path.parts[0] != root or ".." in path.parts
                    or "\\" in item.filename or ":" in item.filename):
                raise ValueError("Unsafe update archive path")
            # Both packaged ZIP targets commonly use case-insensitive filesystems.
            key = tuple(part.casefold() for part in path.parts)
            if key in names:
                raise ValueError("Duplicate update archive entry")
            names.add(key)
            target = destination.joinpath(*path.parts)
            mode = item.external_attr >> 16
            if stat.S_ISLNK(mode):
                if not symlinks or item.file_size > 4096:
                    raise ValueError("Unexpected update archive symlink")
                reference = package.read(item).decode()
                if not reference or reference.startswith("/") or "\\" in reference:
                    raise ValueError("Unsafe update archive symlink")
                links.append((target, reference))
            elif item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with package.open(item) as src, target.open("xb") as dst:
                    shutil.copyfileobj(src, dst)
                if mode & 0o777:
                    target.chmod(mode & 0o777)
    package_root = (destination / root).resolve()
    for link, reference in links:
        if (not link.parent.resolve().is_relative_to(package_root)
                or not (link.parent / reference).resolve().is_relative_to(package_root)):
            raise ValueError("Update archive symlink points outside the package")
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(reference)
    for link, _ in links:
        if not link.resolve(strict=True).is_relative_to(package_root):
            raise ValueError("Update archive symlink points outside the package")


def stage_update(asset, installation, release):
    if not can_install(installation):
        raise ManualInstall()
    staging = Path(mkdtemp(prefix=".kindlegarten-update-", dir=installation.target.parent))
    try:
        # Verify the bytes under a private directory immediately before extraction.
        copy = staging / asset.name
        shutil.copyfile(asset, copy)
        with copy.open("rb") as stream:
            digest = sha256()
            for block in iter(lambda: stream.read(256 * 1024), b""):
                digest.update(block)
        if copy.stat().st_size != release.size or digest.hexdigest() != release.digest:
            raise ValueError("Update package changed after download")
        if installation.kind == "linux":
            if not appimage(copy):
                raise ValueError("Invalid AppImage")
            replacement = copy
            replacement.chmod(0o755)
        else:
            root = "Kindlegarten.app" if installation.kind == "macos" else "Kindlegarten"
            safe_unpack(copy, staging, root, symlinks=installation.kind == "macos")
            replacement = staging / root
            candidate = Installation(installation.kind, replacement)
            if not candidate.executable.is_file() or candidate.executable.is_symlink():
                raise ValueError("Update executable is missing")
            if candidate.kind == "macos":
                if bundle_id(replacement) != BUNDLE_ID:
                    raise ValueError("Unexpected macOS application identifier")
                for argv in (["/usr/bin/codesign", "--verify", "--deep", "--strict"],
                             ["/usr/sbin/spctl", "--assess", "--type", "execute"]):
                    if subprocess.run([*argv, replacement], capture_output=True, timeout=30).returncode:
                        raise ManualInstall()
            elif not (replacement / PORTABLE_MARKER).is_file():
                raise ValueError("Update package is not a Kindlegarten portable installation")
            copy.unlink()
        helper = staging / HELPER_NAME
        shutil.copy2(updater_executable(), helper)
        job = staging / "job.json"
        job.write_text(json.dumps({"kind": installation.kind, "target": str(installation.target),
                                   "replacement": replacement.name, "pid": os.getpid(),
                                   "version": release.tag.lstrip("v")}), encoding="utf-8")
        return job
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def standalone_environment():
    env = {key: value for key, value in os.environ.items()
           if key not in {"APPIMAGE", "APPDIR", "OWD", "ARGV0", READY_VARIABLE, RESULT_VARIABLE}
           and not key.startswith("_PYI_")}
    original = env.pop("LD_LIBRARY_PATH_ORIG", None)
    if original is not None:
        env["LD_LIBRARY_PATH"] = original
    elif getattr(sys, "frozen", False):
        env.pop("LD_LIBRARY_PATH", None)
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return env


def start_installer(job):
    command = [job.with_name(HELPER_NAME), job]
    kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
    subprocess.Popen(command, env=standalone_environment(), cwd=job.parent, **kwargs)


def wait_for_exit(pid):
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        api.OpenProcess.restype = wintypes.HANDLE
        api.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        api.WaitForSingleObject.restype = wintypes.DWORD
        api.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = api.OpenProcess(0x00100000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 87
        try:
            return api.WaitForSingleObject(handle, 60_000) == 0
        finally:
            api.CloseHandle(handle)
    deadline = monotonic() + 60
    while monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        sleep(0.25)
    return False


def replace_and_restart(installation, replacement, version, staging):
    target = installation.target
    backup = staging / "previous"
    ready = staging / "ready"
    target.rename(backup)
    placed = False
    launched = None
    env = standalone_environment()
    try:
        replacement.rename(target)
        placed = True
        launched = subprocess.Popen([installation.executable], cwd=target.parent,
                                    env=dict(env, **{READY_VARIABLE: str(ready)}))
        deadline = monotonic() + 45
        while monotonic() < deadline:
            if ready.exists() and ready.read_text(encoding="utf-8") == version:
                return True
            if launched.poll() is not None:
                break
            sleep(0.25)
    except (OSError, UnicodeError):
        pass
    if launched and launched.poll() is None:
        launched.terminate()
        try:
            launched.wait(timeout=5)
        except subprocess.TimeoutExpired:
            launched.kill()
            launched.wait(timeout=5)
    if placed:
        target.rename(staging / "failed")
    backup.rename(target)
    subprocess.Popen([installation.executable], cwd=target.parent,
                     env=dict(env, **{RESULT_VARIABLE: "restored"}))
    return False


def apply_job(data, staging, installation):
    if not wait_for_exit(data["pid"]):
        shutil.rmtree(staging, ignore_errors=True)
        return 1
    try:
        success = replace_and_restart(installation, staging / data["replacement"], data["version"], staging)
    except OSError:
        if installation.executable.is_file():
            subprocess.Popen([installation.executable], cwd=installation.target.parent,
                             env=dict(standalone_environment(), **{RESULT_VARIABLE: "failed"}))
        return 1
    shutil.rmtree(staging, ignore_errors=True)
    return 0 if success else 1


def main(job):
    job = Path(job).absolute()
    data = json.loads(job.read_text(encoding="utf-8"))
    installation = Installation(data["kind"], Path(data["target"]))
    with (installation.target.parent / ".kindlegarten-update.lock").open("a+b") as lock:
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            shutil.rmtree(job.parent, ignore_errors=True)
            return 1
        return apply_job(data, job.parent, installation)
