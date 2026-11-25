"""DLsite Play ebook delivery using the signed-in reader session."""

import asyncio
from pathlib import Path
from urllib.parse import quote, urlsplit
from zipfile import ZipFile

from kindlegarten.cli import KindleError
from kindlegarten.i18n import tr
from kindlegarten.network import proxy_address

class ReaderConnection:
    _DL_TIMEOUT = 60
    _DL_CHUNK_SIZE = 64 * 1024

    def __init__(self, http):
        self.http = http

    async def get(self, url, *, stream=False, raise_for_status=True, **kwargs):
        self.validate(url)
        response = await self.http.get(url, **kwargs)
        if raise_for_status:
            response.raise_for_status()
        return response if not stream else ReaderStream(response)

    async def post(self, url, **kwargs):
        self.validate(url)
        response = await self.http.post(url, **kwargs)
        response.raise_for_status()
        return response

    @staticmethod
    def validate(url):
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.netloc not in {"play.dlsite.com", "play.dl.dlsite.com"}:
            raise KindleError(tr("The provider returned an unrecognized reader address."))


class ReaderStream:
    def __init__(self, response):
        self.headers = response.headers
        self.aiter_content = response.aiter_bytes


def entries(tree):
    for item in tree:
        if item["type"] == "folder":
            yield from entries(item["children"])
        elif item["type"] == "file":
            yield item


async def reader_export(client, directory, work_id, tree, key):
    import httpx
    from dlsite_async.play.epub import EpubFixedSession, EpubReflowableSession
    from dlsite_async.play.models import ZipTree

    cookies = httpx.Cookies()
    for cookie in client.request.storage_state()["cookies"]:
        if cookie["domain"].lstrip(".") in client.site.domains:
            cookies.set(cookie["name"], cookie["value"], domain=cookie["domain"], path=cookie["path"])
    transport = httpx.AsyncHTTPTransport(proxy=proxy_address(), trust_env=False, retries=2)
    headers = {"Referer": client.site.play + "/", "Origin": client.site.play}
    from urllib.parse import unquote
    for item in cookies.jar:
        if item.name == "XSRF-TOKEN" and item.domain.lstrip(".") in client.site.domains:
            headers["X-XSRF-TOKEN"] = unquote(item.value)
            if item.domain == "play.dlsite.com":
                break
    class SafeFixed(EpubFixedSession):
        async def _download_page_info(self, index):
            import re

            info = await super()._download_page_info(index)
            if any(not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", part.part_no) for part in info.parts):
                raise KindleError(tr("The ebook contains an invalid resource path."))
            return info

    class SafeReflowable(EpubReflowableSession):
        async def _download_epub_entry(self, root, entry):
            from urllib.parse import unquote

            parts = urlsplit(entry)
            target = (root / entry).resolve()
            if (parts.scheme or parts.netloc or parts.query or parts.fragment or chr(92) in entry
                    or not target.is_relative_to(root.resolve())
                    or not (root / unquote(entry)).resolve().is_relative_to(root.resolve())):
                raise KindleError(tr("The ebook contains an invalid resource path."))
            return await super()._download_epub_entry(root, entry)

    async with httpx.AsyncClient(transport=transport, trust_env=False, timeout=60,
                                 cookies=cookies, headers=headers) as http:
        reader = ReaderConnection(http)
        catalog = ZipTree.from_json(tree)
        playfile = catalog.playfile[key]
        if playfile.is_epub_reflowable:
            async with SafeReflowable(reader, catalog, playfile, workno=work_id) as book:
                return await book.download_epub(directory)

        destination = directory / "book.cbz"
        async with SafeFixed(reader, catalog, playfile, workno=work_id) as book:
            with ZipFile(destination, "w") as archive:
                for number in range(book.page_count):
                    for index, path in enumerate(await book.download_page(number, directory, descramble=True)):
                        archive.write(path, f"{number + 1:05d}-{index + 1:02d}{path.suffix.lower()}")
        return destination


def download_book(client, work_id, directory):
    from kindlegarten.comipo import comic_hashes, write_comic

    tree = client.ziptree(work_id)
    if tuple(comic_hashes(tree["tree"], tree["playfile"])):
        destination = directory / "book.cbz"
        write_comic(client, work_id, destination, directory, announce=False)
        return destination

    contents = [(item, tree["playfile"].get(item["hashname"], {})) for item in entries(tree["tree"])]
    for item, resource in contents:
        kind = resource.get("type", "")
        if kind in {"epub_reflowable", "epub"}:
            return asyncio.run(reader_export(client, directory, work_id, tree, item["hashname"]))
    from kindlegarten.comipo import image_extension

    resources = []
    for item, resource in contents:
        kind = resource.get("type")
        file = resource.get(kind, {}).get("optimized") or {}
        if name := file.get("name"):
            if kind == "image" or Path(name).suffix.lower() in {".pdf", ".epub"}:
                resources.append((item, resource, kind, file, name))

    if resources:
        signed = client.fetch(f"{client.site.cdn}/api/v3/download/sign/cookie?workno={quote(work_id)}")
        base = signed["url"].rstrip("/") + "/optimized/"
        for _, _, kind, _, name in resources:
            extension = Path(name).suffix.lower()
            if kind != "image" and extension in {".pdf", ".epub"}:
                result = directory / f"book{extension}"
                result.write_bytes(client.fetch(optimized_address(base, name), as_json=False))
                return result
        images = [row for row in resources if row[2] == "image"]
        if images:
            from kindlegarten.cli import DownloadProgress
            from time import monotonic

            progress = DownloadProgress()
            progress.start([item[3].get("length") for item in images])
            result = directory / "book.cbz"
            success = False
            try:
                with ZipFile(result, "w") as archive:
                    for index, (item, resource, _, optimized, name) in enumerate(images, 1):
                        image = client.fetch(optimized_address(base, name), as_json=False)
                        ext = image_extension(image)
                        if ext is None:
                            raise KindleError(tr("The page was not a recognizable image."))
                        if optimized.get("crypt"):
                            from dlsite_async.play.models import PlayFile
                            from dlsite_async.play.scramble import descramble

                            path = directory / f"page-{index}{ext}"
                            path.write_bytes(image)
                            descramble(path, PlayFile.from_json(resource, hashname=item["hashname"]))
                            image = path.read_bytes()
                            path.unlink()
                        archive.writestr(f"{index:05d}{ext}", image)
                        progress.finish_resource()
                        progress.advance(len(image))
                success = True
                return result
            finally:
                progress.show(monotonic(), success)
    raise KindleError(tr("This DLsite work has no supported ebook rendition."))


def optimized_address(base, name):
    from urllib.parse import unquote

    segments = unquote(name).split("/")
    if any(segment in {"", ".", ".."} or chr(92) in segment for segment in segments):
        raise KindleError(tr("The reader returned an invalid resource path."))
    return base + "/".join(quote(segment, safe="") for segment in segments)
