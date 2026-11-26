"""Normalized, purchased-only library metadata."""

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import quote, urlencode, urlsplit
from xml.etree import ElementTree as ET

PLAY_TIMEZONE = timezone(timedelta(hours=9))


@dataclass(frozen=True, slots=True)
class Book:
    id: str
    title: str
    cover: str = ""
    purchased_at: float | None = None
    store_url: str = ""
    legacy: bool = False


@dataclass(frozen=True, slots=True)
class Series:
    id: str
    title: str
    books: tuple[Book, ...]
    cover: str = ""
    store_url: str = ""

    @property
    def purchased_at(self):
        return max((book.purchased_at for book in self.books if book.purchased_at is not None), default=None)


@dataclass(frozen=True, slots=True)
class Library:
    series: tuple[Series, ...]
    books: tuple[Book, ...]
    complete: bool = True


def store_page(entry, source, region):
    from kindlegarten.marketplaces import MARKETPLACES

    if isinstance(entry, Book) and entry.legacy:
        source = "comipo"
    if source == "amazon":
        host = MARKETPLACES[region].site
    else:
        host = "comipo.app" if source == "comipo" else "dlsite.com"
    try:
        link = urlsplit(entry.store_url)
        if (link.scheme == "https" and link.netloc.lower() in {host, f"www.{host}"}
                and link.path not in {"", "/"}):
            return entry.store_url
    except (ValueError, TypeError):
        pass
    if isinstance(entry, Series):
        return store_page(entry.books[0], source, region) if entry.books else ""
    identifier = quote(entry.id, safe="")
    if source == "amazon":
        return f"https://www.{host}/dp/{identifier}"
    if source == "comipo":
        return f"https://www.{host}/product/{identifier}"
    section = {"B": "comic", "R": "home", "V": "pro"}.get(identifier[:1], "comic")
    return f"https://www.{host}/{section}/work/=/product_id/{identifier}.html"


def series_for_book(library, book_id):
    if library is not None:
        return min((group for group in library.series if any(book.id == book_id for book in group.books)),
                   key=lambda group: group.id, default=None)
    return None


def task_entries(lines, library, *, source=None):
    """Return the resolved items and the original lines that need attention."""
    def series_key(key):
        if source == "amazon" or re.fullmatch(r"TITLE[0-9]+", key, re.IGNORECASE):
            return key.upper()
        return key

    books = {book.id.upper(): book for book in library.books} if library else {}
    series = {series_key(group.id): group for group in library.series} if library else {}
    books.update((book.id.upper(), book) for group in series.values() for book in group.books)
    resolved, errors = [], []
    seen_lines, seen_entries = set(), set()
    for number, value in lines:
        if value in seen_lines:
            continue
        seen_lines.add(value)
        prefix, separator, identifier = value.partition(":")
        kind = prefix.casefold() if separator else ""
        key = identifier.strip() if separator else value
        if kind not in {"", "work", "series"} or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", key):
            errors.append((number, value, "Invalid book or series ID."))
            continue
        book = books.get(key.upper()) if kind != "series" else None
        group = series.get(series_key(key)) if kind != "work" else None
        if book and group:
            errors.append((number, value, "Use work:ID or series:ID to specify this item."))
        elif entry := book or group:
            identity = (type(entry), entry.id)
            if identity not in seen_entries:
                resolved.append(entry)
                seen_entries.add(identity)
        else:
            errors.append((number, value, "ID not found in your purchased library."))
    return resolved, errors


def purchase_timestamp(value, default_zone=timezone.utc):
    if not value:
        return None
    try:
        date = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return (date if date.tzinfo else date.replace(tzinfo=default_zone)).timestamp()


def library_cache_path(data, source, region):
    name = f"amazon-{region}" if source == "amazon" else source
    return data / "library" / f"{name}.json"


def cached_library(data, source, region):
    try:
        return from_json(json.loads(library_cache_path(data, source, region).read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None


def cache_library(data, source, region, snapshot):
    path = library_cache_path(data, source, region)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        staging = Path(stream.name)
        try:
            json.dump(asdict(snapshot), stream, ensure_ascii=False)
            stream.close()
            staging.replace(path)
        finally:
            staging.unlink(missing_ok=True)


def assemble(books, groups, complete=True):
    series = []
    grouped = set()
    for identifier, title, entries, cover, link in groups:
        ids = list(dict.fromkeys(entry for entry in entries if entry in books))
        if ids:
            series.append(Series(identifier, title, tuple(books[entry] for entry in ids), cover, link))
            grouped.update(ids)
    return Library(tuple(sorted(series, key=lambda item: item.title.casefold())),
                   tuple(sorted((book for key, book in books.items() if key not in grouped),
                                key=lambda book: book.title.casefold())), complete)


def amazon_library(fetch):
    books = {}
    groups = {}
    seen = set()
    removed_books = set()
    removed_series = set()
    cursor = ""
    for _ in range(128):
        root = ET.fromstring(fetch(cursor))
        for node in root.iter():
            node.tag = node.tag.rsplit("}", 1)[-1]

        for item in root.findall("./add_update_list/meta_data"):
            identifier = item.findtext("ASIN")
            title = item.findtext("title")
            purchase = any(
                (origin.findtext("type") or "").strip().casefold() == "purchase"
                and not origin.findall("./restrictions/restriction")
                for origin in item.findall("./origins/origin")
            )
            if (identifier and title and purchase and identifier not in removed_books
                    and (item.findtext("cde_contenttype") or "").strip().upper() == "EBOK"):
                books.setdefault(identifier, Book(identifier, title,
                                                   purchased_at=purchase_timestamp(item.findtext("purchase_date"))))

        for node in root.findall("./series_add_update_list/series_meta_data"):
            identifier = node.findtext("seriesId")
            title = node.findtext("displayName")
            if not identifier or not title or identifier in removed_series:
                continue
            members = node.findall("./seriesItems/seriesItem")
            previous = groups.get(identifier)
            positions = previous[2] if previous else {}
            for item in members:
                if asin := item.findtext("itemAsin"):
                    try:
                        position = float(item.findtext("position", "inf"))
                    except ValueError:
                        position = float("inf")
                    positions.setdefault(asin, position)
            if positions:
                art = node.findtext("image") or (previous[3] if previous else "")
                link = node.findtext("detailPageUrl") or node.findtext("seriesUrl") or (previous[4] if previous else "")
                groups[identifier] = (identifier, title, positions, art, link)

        for item in root.findall("./removal_list/*"):
            identifier = item.findtext("ASIN") or item.findtext("asin")
            if identifier and identifier not in books:
                removed_books.add(identifier)
        for item in root.findall("./series_removal_list/series_meta_data"):
            identifier = item.findtext("seriesId")
            if identifier and identifier not in groups:
                removed_series.add(identifier)

        if (root.findtext("has_more") or "").strip().lower() not in {"true", "1"}:
            return assemble_amazon(books, groups, complete=True)
        if not any(root.findall(f"./{key}/*") for key in (
            "add_update_list", "series_add_update_list", "removal_list", "series_removal_list"
        )):
            break
        cursor = (root.findtext("sync_time") or "").strip()
        if not cursor or cursor in seen:
            break
        seen.add(cursor)
    return assemble_amazon(books, groups, complete=False)


def assemble_amazon(books, groups, *, complete):
    ordered = ((identifier, title, sorted(positions, key=positions.get), image, link)
               for identifier, title, positions, image, link in groups.values())
    return assemble(books, ordered, complete)


def read_amazon(state):
    from kindl.auth import AccountStore
    from kindl.client import LIBRARY_URL
    from kindlegarten.cli import SignInRequired
    from kindlegarten.network import amazon_client
    from kindlegarten.i18n import tr

    try:
        account = AccountStore.load(state).get("ipad")
    except (KeyError, RuntimeError, TypeError, ValueError):
        account = None
    if account is None:
        raise SignInRequired(tr('The saved session is incomplete. Sign in again.'))
    with amazon_client(account.credentials) as client:
        def fetch(cursor):
            params = {"item_count": 1000}
            if cursor:
                params["last_sync_time"] = cursor
            url = f"{LIBRARY_URL}?{urlencode(params)}"
            response = client.http.get(url, headers=client._signed_headers("GET", url, reason="Registration"))
            if response.status_code in {401, 403}:
                raise SignInRequired(tr('The saved session is incomplete. Sign in again.'))
            response.raise_for_status()
            return response.content

        return amazon_library(fetch)


def amazon_library_json(state):
    from kindlegarten.cli import SignInRequired

    try:
        return json.dumps(asdict(read_amazon(state)))
    except SignInRequired:
        return "null"


def by_purchase(client, *, dlsite=False):
    from kindlegarten.comipo import work_title

    works, metadata = client.catalog()
    books = {}
    groups = {}
    for item in works:
        if dlsite and item.get("work_type") not in {"MNG", "NRE", "COM", "NOV", "SCM", "PBC"}:
            continue
        key = item["workno"]
        files = item.get("work_files") or {}
        date = item.get("purchased_at")
        if date is None:
            date = purchase_timestamp(item.get("sales_date"), PLAY_TIMEZONE)
        link = item.get("product_url")
        books[key] = Book(key, work_title(item, key), files.get("main") or item.get("work_image") or "",
                          date, link if isinstance(link, str) else "")
        series = item.get("series") or {}
        if not (identifier := series.get("title_id")):
            continue
        groups.setdefault(str(identifier), []).append((int(series.get("volume_number") or 0), key))
    normalized = []
    for identifier, entries in groups.items():
        entry = metadata.get(identifier, {})
        if title := entry.get("name"):
            link = entry.get("series_url")
            normalized.append((identifier, work_title({"name": title}, identifier),
                               [key for _, key in sorted(entries)], "", link if isinstance(link, str) else ""))
    return assemble(books, normalized)


def from_json(payload):
    return Library(
        tuple(Series(item["id"], item["title"], tuple(Book(**book) for book in item["books"]),
                     item.get("cover", ""), item.get("store_url", "")) for item in payload["series"]),
        tuple(Book(**item) for item in payload["books"]), payload.get("complete", True),
    )
