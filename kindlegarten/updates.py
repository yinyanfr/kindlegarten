"""Look up the public stable release for the running desktop build."""

import platform
import re
import sys

from kindlegarten import __version__
from kindlegarten.network import http_client

REPOSITORY = "https://github.com/yinyanfr/kindlegarten"
LATEST_RELEASE = "https://api.github.com/repos/yinyanfr/kindlegarten/releases/latest"


def version_tuple(value):
    if not re.fullmatch(r"v?\d+\.\d+\.\d+", value):
        raise ValueError("Invalid stable release version")
    return tuple(int(component) for component in value.lstrip("v").split("."))


def platform_suffix():
    if sys.platform == "win32" and platform.machine().lower() in {"amd64", "x86_64"}:
        return "-windows-x64.zip"
    if sys.platform == "darwin" and platform.machine() == "arm64":
        return "-macos-arm64.zip"
    if sys.platform.startswith("linux") and platform.machine() == "x86_64":
        return "-linux-x86_64.AppImage"
    return ""


def newer_release(metadata, *, installed=__version__):
    tag = metadata["tag_name"]
    if metadata.get("draft") or metadata.get("prerelease") or version_tuple(tag) <= version_tuple(installed):
        return None
    url = f"{REPOSITORY}/releases/tag/{tag}"
    suffix = platform_suffix()
    if suffix:
        for asset in metadata.get("assets", []):
            name = asset.get("name", "")
            if name.endswith(suffix) and re.fullmatch(r"kindlegarten-[A-Za-z0-9._-]+", name):
                url = f"{REPOSITORY}/releases/download/{tag}/{name}"
                break
    return tag, url


def check_for_update():
    with http_client() as http:
        response = http.get(LATEST_RELEASE, timeout=15, headers={"Accept": "application/vnd.github+json"})
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return newer_release(response.json())
