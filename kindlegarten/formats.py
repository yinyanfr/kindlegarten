"""Format selection and conversion shared by the command line and desktop app."""

import json
import shutil
import sys
from hashlib import sha256
from html import escape
from pathlib import Path
from tempfile import NamedTemporaryFile
from zipfile import ZIP_DEFLATED, ZipFile

from kindlegarten.cli import KindleError, book_filename, check_epub, publish_book
from kindlegarten.i18n import tr
from kindlegarten.network import worker_environment
from kindlegarten.progress import check_cancelled, run_conversion

FORMATS = ("auto", "epub", "pdf", "cbz", "zip")
MEDIA = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif",
         ".webp": "image/webp", ".bmp": "image/bmp", ".avif": "image/avif"}


def selected_format(requested, destination=None):
    requested = requested or "auto"
    if requested not in FORMATS:
        raise KindleError(tr("Unsupported format: {format}", format=requested))
    if destination is None:
        return requested
    extension = destination.suffix.lower().removeprefix(".")
    if extension not in FORMATS[1:]:
        raise KindleError(tr("The output filename needs an EPUB, PDF, CBZ, or ZIP extension."))
    if requested not in {"auto", extension}:
        raise KindleError(tr("The output filename extension does not match --format {format}.", format=requested))
    return extension


def unique_destination(directory, title, identifier, suffix):
    path = directory / book_filename(title, identifier, suffix)
    if not path.exists() and not path.is_symlink():
        return path
    label = book_filename(str(identifier)[:60], "id", "")
    for number in range(1, 10_001):
        collision = label if number == 1 else f"{label} {number}"
        candidate = directory / book_filename(title, identifier, f" [{collision}]{suffix}")
        if not candidate.exists() and not candidate.is_symlink():
            return candidate
    raise KindleError(tr("No free output filename for {title}.", title=title))


def output_record(data, directory, identity, requested):
    key = "\n".join((str(directory.resolve()), identity, requested))
    digest = sha256(key.encode("utf-8")).hexdigest()
    return data / "exports" / f"{digest}.json"


def already_saved(data, directory, identity, requested):
    try:
        info = json.loads(output_record(data, directory, identity, requested).read_text(encoding="utf-8"))
        path = directory / Path(info["file"]).name
        if not path.is_symlink() and path.is_file():
            stat = path.stat()
            if stat.st_size == info["size"] and stat.st_mtime_ns == info.get("mtime"):
                return path
    except (ValueError, KeyError, OSError):
        pass
    return None


def save_export(source, destination, data, identity, requested):
    publish_book(source, destination)
    stat = destination.stat()
    try:
        for key in {requested, destination.suffix.lower().removeprefix(".")}:
            record = output_record(data, destination.parent, identity, key)
            record.parent.mkdir(mode=0o700, exist_ok=True)
            with NamedTemporaryFile(mode="w", encoding="utf-8", dir=record.parent, delete=False) as stream:
                temporary = Path(stream.name)
                try:
                    json.dump({"file": destination.name, "size": stat.st_size, "mtime": stat.st_mtime_ns}, stream)
                    stream.close()
                    temporary.replace(record)
                finally:
                    temporary.unlink(missing_ok=True)
    except OSError:
        print(tr("The file was saved, but download history could not be recorded."), file=sys.stderr)
    return destination


def publish_export(source, destination, directory, title, identifier, data, identity, requested):
    if destination is not None:
        return save_export(source, destination, data, identity, requested)
    while True:
        target = unique_destination(directory, title, identifier, source.suffix)
        try:
            return save_export(source, target, data, identity, requested)
        except FileExistsError:
            # A concurrent task may have just published a different title at this path.
            if not (target.exists() or target.is_symlink()):
                raise


def image_entries(archive):
    names = [item.filename for item in archive.infolist()
             if not item.is_dir() and Path(item.filename).suffix.lower() in MEDIA]
    if not names or len(set(names)) != len(names) or archive.testzip() is not None:
        raise KindleError(tr("The comic archive is incomplete or invalid."))
    return names


def images_as_epub(source, target, title, identifier, direction):
    from PIL import Image

    with ZipFile(source) as incoming, ZipFile(target, "w", compression=ZIP_DEFLATED) as ebook:
        ebook.writestr("mimetype", b"application/epub+zip", compress_type=0)
        ebook.writestr("META-INF/container.xml", """<?xml version="1.0"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
<rootfiles><rootfile full-path="book.opf" media-type="application/oebps-package+xml"/></rootfiles></container>""")
        manifest, spine, navigation = [], [], []
        for index, name in enumerate(image_entries(incoming), 1):
            check_cancelled()
            import io
            payload = incoming.read(name)
            extension = Path(name).suffix.lower()
            with Image.open(io.BytesIO(payload)) as image:
                width, height = image.size
                if extension not in {".jpg", ".jpeg", ".png", ".gif"}:
                    buffer = io.BytesIO()
                    image.save(buffer, format="PNG")
                    payload, extension = buffer.getvalue(), ".png"
            image_path = f"images/{index}{extension}"
            page_path = f"page-{index}.xhtml"
            ebook.writestr(image_path, payload)
            ebook.writestr(page_path, f"""<html xmlns="http://www.w3.org/1999/xhtml"><head><title>{index}</title>
<meta name="viewport" content="width={width},height={height}"/></head>
<body style="margin:0"><img src="{image_path}" width="{width}" height="{height}" alt="{index}"/></body></html>""")
            manifest.extend((f'<item id="image-{index}" href="{image_path}" media-type="{MEDIA[extension]}"/>',
                             f'<item id="page-{index}" href="{page_path}" media-type="application/xhtml+xml"/>'))
            spine.append(f'<itemref idref="page-{index}"/>')
            navigation.append(f'<li><a href="{page_path}">{index}</a></li>')
        ebook.writestr("nav.xhtml", f"""<html xmlns="http://www.w3.org/1999/xhtml"
xmlns:epub="http://www.idpf.org/2007/ops"><head><title>{escape(title)}</title></head>
<body><nav epub:type="toc"><ol>{"".join(navigation)}</ol></nav></body></html>""")
        ebook.writestr("book.opf", f"""<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="id">{escape(identifier)}</dc:identifier>
<dc:title>{escape(title)}</dc:title><dc:language>und</dc:language>
<meta property="rendition:layout">pre-paginated</meta>
<meta property="dcterms:modified">1980-01-01T00:00:00Z</meta></metadata>
<manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>{"".join(manifest)}</manifest>
<spine page-progression-direction="{direction}">{"".join(spine)}</spine></package>""")
    check_epub(target)


def images_as_pdf(source, target):
    import img2pdf
    from PIL import Image

    with ZipFile(source) as comic:
        paths = []
        for index, name in enumerate(image_entries(comic)):
            check_cancelled()
            extension = Path(name).suffix.lower()
            path = target.parent / f"pdf-page-{index}{extension}"
            with comic.open(name) as image, path.open("wb") as output:
                shutil.copyfileobj(image, output)
            paths.append(path)
            if extension not in {".png", ".jpg", ".jpeg"}:
                with Image.open(path) as image:
                    normalized = path.with_suffix(".png")
                    image.convert("RGBA" if "A" in image.mode else "RGB").save(normalized)
                paths[-1] = normalized
        with target.open("wb") as output:
            img2pdf.convert(*(str(path) for path in paths), outputstream=output)


def check_pdf(path):
    with path.open("rb") as stream:
        if stream.read(5) != b"%PDF-":
            raise KindleError(tr("The PDF could not be validated."))
        stream.seek(max(0, path.stat().st_size - 4096))
        if b"%%EOF" not in stream.read():
            raise KindleError(tr("The PDF could not be validated."))


def convert(source, requested, title, identifier, *, direction="rtl"):
    from kindlegarten.cli import calibre_executable

    check_cancelled()
    native = source.suffix.removeprefix(".")
    target = source.with_name(f"export.{requested}")
    if requested in {"auto", native}:
        if native == "pdf":
            check_pdf(source)
        elif native == "epub":
            check_epub(source)
        elif native == "cbz":
            with ZipFile(source) as archive:
                image_entries(archive)
        return source
    if native == "cbz":
        if requested == "zip":
            with ZipFile(source) as archive:
                image_entries(archive)
            return source.rename(target)
        if requested == "epub":
            images_as_epub(source, target, title, identifier, direction)
            return target
        if requested == "pdf":
            images_as_pdf(source, target)
            check_pdf(target)
            return target
    if native == "epub" and requested == "pdf":
        result = run_conversion([calibre_executable("ebook-convert"), source, target], env=worker_environment())
        if result.returncode or not target.is_file():
            raise KindleError(tr("Calibre could not convert this ebook to PDF."))
        check_pdf(target)
        return target
    raise KindleError(tr("This book does not support {format} export.", format=requested.upper()))
