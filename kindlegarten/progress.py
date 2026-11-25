"""Deliver download progress to a CLI terminal or a desktop task."""

import json
import os
from contextvars import ContextVar

PROGRESS = ContextVar("progress", default=None)
STATUS = ContextVar("status", default=None)


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
