"""Deliver download progress to a CLI terminal or a desktop task."""

import json
import os
import subprocess
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from threading import Event

PROGRESS = ContextVar("progress", default=None)
STATUS = ContextVar("status", default=None)
DOWNLOAD = ContextVar("download", default=None)


class DownloadCancelled(Exception):
    pass


@dataclass(frozen=True)
class DownloadContext:
    workspace: Path
    cancelled: Event


def check_cancelled():
    if (active := DOWNLOAD.get()) is not None and active.cancelled.is_set():
        raise DownloadCancelled


def download_directory(default):
    return active.workspace if (active := DOWNLOAD.get()) is not None else default


def download_session(path):
    return download_directory(path.parent) / path.name


def run_conversion(command, *, env):
    if DOWNLOAD.get() is None:
        return subprocess.run(command, env=env, capture_output=True)
    check_cancelled()
    with subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
        try:
            while True:
                check_cancelled()
                try:
                    stdout, stderr = process.communicate(timeout=0.2)
                except subprocess.TimeoutExpired:
                    continue
                check_cancelled()
                return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        except DownloadCancelled:
            process.kill()
            process.communicate()
            raise


def report(percent, speed):
    if callback := PROGRESS.get():
        callback(percent, speed)
        return True
    if os.environ.get("KINDLEGARTEN_PROGRESS") == "1":
        print(json.dumps({"percent": percent, "speed": speed}), flush=True)
        return True
    return False


def report_book(title):
    if callback := STATUS.get():
        callback(title)
