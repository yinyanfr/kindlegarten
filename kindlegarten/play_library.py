"""DLsite's purchased catalogue and its opt-in Comipo compatibility route."""

from dataclasses import replace

from kindlegarten.cli import KindleError, book_filename
from kindlegarten.comipo import COMIPO, DLSITE, download, saved_account, session
from kindlegarten.i18n import tr
from kindlegarten.library import Library, by_purchase, cached_library
from kindlegarten.progress import check_cancelled


def volumes(library):
    return {book.id: book for book in (*library.books, *(book for group in library.series for book in group.books))}


def missing_legacy_books(data, primary):
    old = cached_library(data, "comipo", None)
    return set(volumes(old)) - set(volumes(primary)) if old and primary.complete else set()


def merge_libraries(primary, old):
    missing = set(volumes(old)) - set(volumes(primary))
    series = {group.id: group for group in primary.series}
    claimed = set()
    for group in old.series:
        additions = tuple(replace(book, legacy=True) for book in group.books if book.id in missing - claimed)
        if additions:
            current = series.get(group.id)
            series[group.id] = replace(current, books=current.books + additions) if current else replace(group, books=additions)
            claimed.update(book.id for book in additions)
    books = primary.books + tuple(replace(book, legacy=True) for book in old.books if book.id in missing - claimed)
    return Library(tuple(series.values()), books, primary.complete)


def read_play_library(data, command, *, legacy=False):
    with session(data, command, DLSITE) as client:
        primary = by_purchase(client, dlsite=True)
    if not legacy or not primary.complete:
        return primary
    names = [(saved_account(data, command, site) or {}).get("login", "") for site in (DLSITE, COMIPO)]
    if not all(names) or names[0].casefold() != names[1].casefold():
        raise KindleError(tr("The DLsite and Comipo sessions must verify the same viviON ID for compatibility."))
    with session(data, command, COMIPO) as client:
        return merge_libraries(primary, by_purchase(client))


def download_with_legacy(target, output, data, books, command, requested, *, resume=False):
    kind, identifier = target
    library = read_play_library(data, command, legacy=True)
    if kind == "work":
        book = volumes(library).get(identifier)
        if book is None:
            raise KindleError(tr("This book is not in the purchased library."))
        return download(target, output, data, books, command, requested,
                        site=COMIPO if book.legacy else DLSITE, resume=resume)
    group = next((group for group in library.series if group.id == identifier), None)
    if not group:
        raise KindleError(tr("No purchased volumes were found for series {id}.", id=identifier))
    directory = (output or books).expanduser().absolute() / book_filename(group.title, group.id, "")
    failures = []
    for book in group.books:
        check_cancelled()
        try:
            download(("work", book.id), None, data, directory, command, requested,
                     site=COMIPO if book.legacy else DLSITE, resume=True)
        except (KindleError, OSError) as error:
            failures.append(f"{book.id}: {error}")
    if failures:
        raise KindleError("\n".join(failures))
