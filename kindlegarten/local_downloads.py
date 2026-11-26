"""Read the current download folder and match files to purchased books."""

import re
import unicodedata
from collections import defaultdict
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile

from kindlegarten import cli
from kindlegarten.desktop_download import series_directory
from kindlegarten.formats import MEDIA, already_saved, check_pdf

SUFFIXES = {".epub", ".pdf", ".cbz", ".zip", ".kfx-zip"}
ID_PATTERN = re.compile(r"(?:(amazon:[a-z]+|comipo|dlsite|urn:asin|asin):)?([A-Z0-9]{10}|[BRV]J(?:[0-9]{6}|[0-9]{8}))",
                        re.IGNORECASE | re.ASCII)


def file_name_key(name):
    return unicodedata.normalize("NFC", name.casefold())


def epub_identifiers(archive):
    container = archive.getinfo("META-INF/container.xml")
    if container.file_size > 1_000_000:
        raise ValueError
    rootfile = ET.fromstring(archive.read(container)).find(".//{*}rootfile")
    if rootfile is None:
        raise ValueError
    package = archive.getinfo(rootfile.attrib["full-path"])
    if package.file_size > 4_000_000:
        raise ValueError
    root = ET.fromstring(archive.read(package))
    if root.find(".//{*}spine/{*}itemref") is None:
        raise ValueError
    return [item.text.strip() for item in root.findall(".//{*}metadata/{*}identifier") if item.text]


def file_identifiers(path):
    """Return recognized ID metadata, an empty list for plausible exports, or None."""
    suffix = path.suffix.lower()
    try:
        if suffix == ".pdf":
            check_pdf(path)
            return []
        with ZipFile(path) as archive:
            if suffix == ".epub":
                return epub_identifiers(archive)
            if suffix == ".kfx-zip":
                return [] if any(item.file_size and Path(item.filename).suffix.lower() in {".azw8", ".azw9", ".kfx"}
                                 for item in archive.infolist()) else None
            if not any(item.file_size and Path(item.filename).suffix.lower() in MEDIA for item in archive.infolist()):
                return None
            comment = archive.comment.decode("ascii", errors="replace")
            if comment.startswith("Comipo work:"):
                return ["comipo:" + comment.removeprefix("Comipo work:")]
            if comment.startswith("DLsite work:"):
                return ["dlsite:" + comment.removeprefix("DLsite work:")]
            return []
    except (BadZipFile, OSError, ValueError, KeyError, RuntimeError, ET.ParseError, cli.KindleError):
        return None


def matched_ids(identifiers, source, region):
    """A recognized ID from a different source still prevents a title-only match."""
    recognized = False
    book_ids = set()
    for value in identifiers:
        match = ID_PATTERN.fullmatch(value)
        if match is None:
            continue
        provider, identifier = match.groups()
        provider = provider.lower() if provider else ""
        recognized = True
        match_source = provider in {"", source, f"{source}:{region}"} or (source == "dlsite" and provider == "comipo")
        if source == "amazon" and provider in {"asin", "urn:asin"}:
            match_source = True
        if match_source:
            book_ids.add(identifier.upper())
    return recognized, book_ids


def scan_folder(data, folder, books, source, region):
    try:
        if folder.is_symlink():
            return set()
        files = {path for path in folder.iterdir() if path.suffix.lower() in SUFFIXES
                 and not path.is_symlink() and path.is_file()}
    except OSError:
        return set()
    formats = {path.suffix.lower()[1:] for path in files}
    if not formats:
        return set()
    identity = f"amazon:{region}" if source == "amazon" else {"comipo": "Comipo", "dlsite": "DLsite"}[source]
    matched = set()
    remaining = files.copy()
    titles = defaultdict(set)
    known_ids = {book.id.upper(): book.id for book in books}
    for book in books:
        for suffix in formats:
            prefixes = (identity, "Comipo") if source == "dlsite" else (identity,)
            for prefix in prefixes:
                path = already_saved(data, folder, f"{prefix}:{book.id}", suffix)
                if path in files:
                    matched.add(book.id)
                    remaining.discard(path)
            name = file_name_key(cli.book_filename(book.title, book.id, "." + suffix))
            titles[name].add(book.id)
    for path in remaining:
        identifiers = file_identifiers(path)
        if identifiers is None:
            continue
        recognized, ids = matched_ids(identifiers, source, region)
        if recognized:
            candidates = {known_ids[key] for key in ids if key in known_ids}
        else:
            candidates = titles.get(file_name_key(path.name), set())
        if len(candidates) == 1:
            matched.update(candidates)
    return matched


def scan_downloads(data, folder, library, source, region):
    """Return keys in the form (series ID or None, book ID), and folders to watch."""
    folder = folder.resolve()
    groups = defaultdict(list)
    groups[folder].append((None, library.books))
    for series in library.series:
        groups[series_directory(folder, series)].append((series.id, series.books))
    matched = set()
    watch = set()
    for directory, sections in groups.items():
        if directory.is_dir() and not directory.is_symlink():
            watch.add(str(directory))
        downloaded = scan_folder(data, directory, [book for _, books in sections for book in books], source, region)
        matched.update((series_id, book.id) for series_id, books in sections
                       for book in books if book.id in downloaded)
    if str(folder) not in watch:
        parent = folder.parent
        while not parent.is_dir() and parent != parent.parent:
            parent = parent.parent
        watch.add(str(parent))
    return matched, watch


def folder_stamps(directories):
    stamps = {}
    for directory in directories:
        try:
            stamps[directory] = Path(directory).stat().st_mtime_ns
        except OSError:
            stamps[directory] = None
    return stamps
