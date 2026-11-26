"""Build the desktop release on its target platform."""

import argparse
import errno
import importlib.util
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from importlib.metadata import distribution
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kindlegarten import __version__
from kindlegarten.cli import KINDL_SOURCE, fetch_verified
from kindlegarten.update_install import HELPER_NAME, PORTABLE_MARKER


def install_kindl():
    if importlib.util.find_spec("kindl") is not None:
        return
    with TemporaryDirectory() as temporary:
        archive = Path(temporary) / "kindl.tar.gz"
        fetch_verified(KINDL_SOURCE, archive)
        subprocess.run([sys.executable, "-m", "pip", "install", archive], check=True)


def license_notices(target):
    target.mkdir()
    names = ("kindl", "PySide6-Essentials", "shiboken6", "playwright", "cryptography", "dlsite-async",
             "Pillow", "img2pdf", "pikepdf", "httpx")
    for name in names:
        metadata = distribution(name)
        destination = target / name
        destination.mkdir()
        for entry in metadata.files or ():
            if entry.parent.name == "licenses" or entry.name.upper().startswith(("LICENSE", "COPYING", "NOTICE")):
                path = metadata.locate_file(entry)
                if path.is_file():
                    shutil.copyfile(path, destination / entry.name)
        homepage = metadata.metadata.get("Home-page", "")
        links = metadata.metadata.get_all("Project-URL") or []
        (destination / "distribution.txt").write_text(
            "\n".join((f"{name} {metadata.version}", homepage, *links)) + "\n", encoding="utf-8"
        )


def unpack_appimage(image, target):
    payload = image.read_bytes()
    offset = payload.find(b"hsqs")
    while offset >= 0:
        arguments = ["unsquashfs", "-offset", str(offset)]
        checked = subprocess.run([*arguments, "-s", image], capture_output=True)
        if checked.returncode == 0:
            subprocess.run([*arguments, "-d", target, image], check=True)
            return target / "AppRun"
        offset = payload.find(b"hsqs", offset + 4)
    raise RuntimeError(f"No SquashFS filesystem found in {image}")


def invoke_appimage(image, *arguments, env, fallback, timeout=None):
    try:
        subprocess.run([image, *arguments], env=env, check=True, timeout=timeout)
    except OSError as error:
        if error.errno != errno.ENOEXEC or not shutil.which("unsquashfs"):
            raise
        # Some amd64 emulators cannot execute the statically linked AppImage runtime.
        executable = unpack_appimage(image, fallback)
        subprocess.run([executable, *arguments], env=dict(env, APPDIR=str(fallback)),
                       check=True, timeout=timeout)


def build(args):
    machine = platform.machine().lower()
    platform_id = (
        "macos-arm64" if sys.platform == "darwin" and machine == "arm64" else
        "windows-x64" if sys.platform == "win32" and machine in {"amd64", "x86_64"} else
        "linux-x86_64" if sys.platform.startswith("linux") and machine == "x86_64" else ""
    )
    if not platform_id:
        raise SystemExit("Use macOS arm64, Windows x64, or Linux x86_64 for this release.")
    install_kindl()
    output = args.output.expanduser().absolute()
    output.mkdir(parents=True, exist_ok=True)
    package = output / f"kindlegarten-{__version__}-{platform_id}"
    with TemporaryDirectory(prefix="kindlegarten-build-") as temporary:
        staging = Path(temporary)
        resources = ROOT / "kindlegarten"
        art = resources / "app-icon.png"
        notices = staging / "Third-party notices"
        license_notices(notices)
        shutil.copyfile(ROOT / "LICENSE", notices / "Kindlegarten.txt")
        helpers = staging / "helpers"
        updater = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile",
                   "--name", "KindlegartenUpdater", "--distpath", str(helpers),
                   "--workpath", str(staging / "updater-work"), "--specpath", str(staging),
                   "--paths", str(ROOT)]
        if sys.platform == "win32":
            updater.append("--noconsole")
        subprocess.run([*updater, str(ROOT / "scripts" / "update_entry.py")], check=True, cwd=ROOT)
        command = [
            sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir", "--windowed",
            "--name", "Kindlegarten", "--distpath", str(staging / "dist"), "--workpath", str(staging / "work"),
            "--specpath", str(staging), "--paths", str(ROOT), "--icon", str(art),
            "--hidden-import", "kindl", "--collect-submodules", "kindl", "--collect-all", "playwright",
            "--add-data", f"{resources}:kindlegarten", "--add-data", f"{notices}:Third-party notices",
            "--add-binary", f"{helpers / HELPER_NAME}:updater",
        ]
        if sys.platform == "darwin":
            command.extend(["--osx-bundle-identifier", "io.github.yinyanfr.kindlegarten"])
        subprocess.run([*command, str(ROOT / "scripts" / "desktop_entry.py")], check=True, cwd=ROOT)
        dist = staging / "dist"
        if sys.platform == "darwin":
            bundle = dist / "Kindlegarten.app"
            smoke = bundle / "Contents" / "MacOS" / "Kindlegarten"
            subprocess.run(["ditto", "-c", "-k", "--keepParent", bundle, f"{package}.zip"], check=True)
        elif sys.platform == "win32":
            smoke = dist / "Kindlegarten" / "Kindlegarten.exe"
            (smoke.parent / PORTABLE_MARKER).touch()
            shutil.make_archive(str(package), "zip", root_dir=dist, base_dir="Kindlegarten")
        else:
            appdir = staging / "Kindlegarten.AppDir"
            appdir.mkdir()
            shutil.move(dist / "Kindlegarten", appdir / "Kindlegarten")
            smoke = appdir / "Kindlegarten" / "Kindlegarten"
            (appdir / "AppRun").symlink_to("Kindlegarten/Kindlegarten")
            shutil.copyfile(art, appdir / "kindlegarten.png")
            (appdir / "kindlegarten.desktop").write_text(
                "[Desktop Entry]\nType=Application\nName=Kindlegarten\nExec=Kindlegarten\n"
                "Icon=kindlegarten\nCategories=Utility;\n", encoding="utf-8"
            )
            if not args.appimagetool:
                raise SystemExit("Set --appimagetool to the local appimagetool executable.")
            path = args.appimagetool.expanduser().absolute()
            env = dict(os.environ, ARCH="x86_64", APPIMAGE_EXTRACT_AND_RUN="1")
            smoke = Path(f"{package}.AppImage")
            invoke_appimage(path, appdir, smoke, env=env, fallback=staging / "packager")
        env = dict(os.environ, QT_QPA_PLATFORM="offscreen", KINDLEGARTEN_DATA_DIR=str(staging / "smoke"),
                   KINDLEGARTEN_SMOKE_FILE=str(staging / "smoke-ok"))
        if sys.platform.startswith("linux"):
            env["APPIMAGE_EXTRACT_AND_RUN"] = "1"
            invoke_appimage(smoke, "--smoke-test", env=env, fallback=staging / "appimage-smoke", timeout=60)
        else:
            subprocess.run([smoke, "--smoke-test"], check=True, env=env, timeout=60)
        if not (staging / "smoke-ok").is_file():
            raise SystemExit("The packaged app did not complete its smoke test.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist-desktop")
    parser.add_argument("--appimagetool", type=Path)
    build(parser.parse_args())
