"""Run an individual desktop task against an isolated copy of its session."""

import shutil
from argparse import Namespace
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event

from kindlegarten import cli
from kindlegarten.comipo import COMIPO, DLSITE, session_path
from kindlegarten.i18n import tr
from kindlegarten.library import Book, Series
from kindlegarten.progress import DOWNLOAD, PROGRESS, STATUS, DownloadCancelled, DownloadContext, check_cancelled


@dataclass(frozen=True)
class DownloadRequest:
    source: str
    region: str
    entry: Book | Series
    folder: Path
    format: str

    @property
    def directory(self):
        if isinstance(self.entry, Series):
            return self.folder / cli.book_filename(self.entry.title, self.entry.id, "")
        return self.folder


def run_download(request, cancelled: Event, lock):
    """Keep the project lock around setup and session copying only."""
    workspace_root = cli.DATA / "downloads"
    workspace_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="task-", dir=workspace_root) as temporary:
        workspace = Path(temporary)
        prepare_download(request, workspace, cancelled, lock)
        token = DOWNLOAD.set(DownloadContext(workspace, cancelled))
        try:
            check_cancelled()
            return export_volumes(request)
        finally:
            DOWNLOAD.reset(token)


def prepare_download(request, workspace, cancelled, lock):
    with lock, cli.project_lock():
        if cancelled.is_set():
            raise DownloadCancelled
        if request.source == "amazon":
            state = cli.account_path(request.region)
        else:
            site = COMIPO if request.source == "comipo" else DLSITE
            state = session_path(cli.DATA, site)
        if not state.is_file():
            raise cli.SignInRequired(tr("Sign in before downloading."))
        if request.source == "amazon" and not cli.setup_ready():
            cli.setup(None)
        shutil.copyfile(state, workspace / state.name)


def export_volumes(request):
    volumes = request.entry.books if isinstance(request.entry, Series) else (request.entry,)
    directory = request.directory
    progress = PROGRESS.get()
    status = STATUS.get()
    failures = []
    for index, volume in enumerate(volumes):
        check_cancelled()
        if status and len(volumes) > 1:
            status(tr("Volume {current}/{total} · {title}", current=index + 1, total=len(volumes), title=volume.title))

        def volume_progress(percent, speed):
            if progress:
                # Leave room for conversion and publishing until the volume actually finishes.
                completed = index + min(percent, 99) / 100
                progress(round(100 * completed / len(volumes)), speed)

        token = PROGRESS.set(volume_progress if progress else None)
        try:
            cli.download_selection(Namespace(
                kind="work", identifier=volume.id, source=request.source,
                region=request.region if request.source == "amazon" else None,
                format=request.format, output=None, books=directory, resume=True,
            ))
        except (cli.KindleError, OSError) as error:
            if len(volumes) == 1:
                raise
            failures.append(f"{volume.title}: {error}")
        finally:
            PROGRESS.reset(token)
        if progress:
            progress(round(100 * (index + 1) / len(volumes)), "")
    if failures:
        raise cli.KindleError("\n".join(failures))
    return directory
