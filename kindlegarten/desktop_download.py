"""Run an individual desktop task against an isolated copy of its session."""

import shutil
from argparse import Namespace
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Condition, Event

from kindlegarten import cli
from kindlegarten.comipo import COMIPO, DLSITE, session_path
from kindlegarten.i18n import tr
from kindlegarten.library import Book, Series
from kindlegarten.progress import DOWNLOAD, PROGRESS, STATUS, DownloadCancelled, DownloadContext, check_cancelled

_volumes = set()
_volume_ready = Condition()


@contextmanager
def exporting_volume(key):
    with _volume_ready:
        while key in _volumes:
            check_cancelled()
            _volume_ready.wait(0.2)
        _volumes.add(key)
    try:
        check_cancelled()
        yield
    finally:
        with _volume_ready:
            _volumes.remove(key)
            _volume_ready.notify_all()


def series_directory(folder, series):
    return folder / cli.book_filename(series.title, series.id, "")


@dataclass(frozen=True)
class DownloadRequest:
    source: str
    region: str
    entry: Book | Series
    folder: Path
    format: str
    series: Series | None = None
    calibre_path: str = ""
    dedrm: bool = True

    @property
    def directory(self):
        series = self.entry if isinstance(self.entry, Series) else self.series
        return series_directory(self.folder, series) if series is not None else self.folder

    @property
    def key(self):
        return (self.source, self.region, type(self.entry), self.entry.id, self.directory.resolve(),
                self.format, self.dedrm)


def run_download(request, cancelled: Event, lock):
    """Keep the project lock around setup and session copying only."""
    workspace_root = cli.DATA / "downloads"
    workspace_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with cli.using_calibre(request.calibre_path), TemporaryDirectory(prefix="task-", dir=workspace_root) as temporary:
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
        if request.source == "amazon":
            if not request.dedrm:
                cli.setup_worker_runtime()
            elif not cli.setup_ready():
                cli.setup(None)
        shutil.copyfile(state, workspace / state.name)
        entries = request.entry.books if isinstance(request.entry, Series) else (request.entry,)
        if request.source == "dlsite" and any(book.legacy for book in entries):
            old = session_path(cli.DATA, COMIPO)
            if not old.is_file():
                raise cli.SignInRequired(tr("The saved Comipo session has expired. Sign in again."))
            shutil.copyfile(old, workspace / old.name)


def export_volumes(request):
    volumes = request.entry.books if isinstance(request.entry, Series) else (request.entry,)
    directory = request.directory
    resolved = directory.resolve()
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
            with exporting_volume((request.source, request.region, resolved, volume.id)):
                cli.download_selection(Namespace(
                    kind="work", identifier=volume.id, source=request.source,
                    region=request.region if request.source == "amazon" else None,
                    format=request.format, output=None, books=directory, resume=True,
                    dedrm=request.dedrm, title=volume.title, legacy_comipo=volume.legacy,
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
