"""Look up the public stable release for the running desktop build."""

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import platform
import re
import sys
from tempfile import NamedTemporaryFile
from urllib.parse import urljoin, urlsplit

from kindlegarten import __version__
from kindlegarten.cli import KindleError
from kindlegarten.i18n import tr
from kindlegarten.network import http_client
from kindlegarten.progress import DownloadCancelled

REPOSITORY = "https://github.com/yinyanfr/kindlegarten"
LATEST_RELEASE = "https://api.github.com/repos/yinyanfr/kindlegarten/releases/latest"


@dataclass(frozen=True)
class Release:
    tag: str
    page: str
    asset: str = ""
    size: int = 0
    digest: str = ""

    @property
    def download_url(self):
        return f"{REPOSITORY}/releases/download/{self.tag}/{self.asset}"


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
    page = f"{REPOSITORY}/releases/tag/{tag}"
    suffix = platform_suffix()
    if suffix:
        for asset in metadata.get("assets", []):
            name = f"kindlegarten-{tag.lstrip('v')}{suffix}"
            if asset.get("name") != name:
                continue
            digest, size = asset.get("digest"), asset.get("size")
            if type(size) is int and size > 0 and re.fullmatch(r"sha256:[a-f0-9]{64}", digest or ""):
                return Release(tag, page, name, size, digest[7:])
    return Release(tag, page)


def check_for_update():
    with http_client() as http:
        response = http.get(LATEST_RELEASE, timeout=15, headers={"Accept": "application/vnd.github+json"})
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return newer_release(response.json())


def download_update(release, directory, cancelled, progress):
    if not release.asset or not release.digest:
        raise KindleError(tr("A verifiable update package is not available."))
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination = directory / release.asset
    url = release.download_url
    with http_client() as http, NamedTemporaryFile(dir=directory, prefix=".download-", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            size = 0
            digest = sha256()
            for _ in range(6):
                host = urlsplit(url)
                if host.scheme != "https" or host.netloc not in {
                    "github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"
                }:
                    raise KindleError(tr("The update download was redirected to an unexpected address."))
                if cancelled.is_set():
                    raise DownloadCancelled
                with http.stream("GET", url, headers={"Accept-Encoding": "identity"}) as response:
                    if response.is_redirect:
                        url = urljoin(url, response.headers["Location"])
                        continue
                    response.raise_for_status()
                    for block in response.iter_bytes(256 * 1024):
                        if cancelled.is_set():
                            raise DownloadCancelled
                        size += len(block)
                        if size > release.size:
                            break
                        digest.update(block)
                        stream.write(block)
                        progress(min(99, size * 100 // release.size), "")
                    break
            if cancelled.is_set():
                raise DownloadCancelled
            if size != release.size or digest.hexdigest() != release.digest:
                raise KindleError(tr("The update package failed its size or SHA-256 check."))
            stream.close()
            temporary.replace(destination)
            progress(100, "")
            return destination
        finally:
            stream.close()
            temporary.unlink(missing_ok=True)
