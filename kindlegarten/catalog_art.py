"""Retrieve public cover artwork without forwarding account credentials."""

from hashlib import sha256
from html.parser import HTMLParser
from tempfile import NamedTemporaryFile
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from kindlegarten.marketplaces import MARKETPLACES
from kindlegarten.network import http_client


class ProductCover(HTMLParser):
    url = ""

    def handle_starttag(self, tag, attrs):
        fields = dict(attrs)
        if tag == "meta" and fields.get("property") in {"og:image", "og:image:secure_url"}:
            self.url = fields.get("content") or self.url
        if tag == "img" and fields.get("id") in {"landingImage", "imgBlkFront"} and not self.url:
            self.url = fields.get("data-old-hires") or fields.get("src") or ""


def artwork_allowed(address):
    parts = urlsplit(address)
    allowed = ("media-amazon.com", "ssl-images-amazon.com", "images-amazon.com", "comipo.app", "dlsite.com")
    return (parts.scheme == "https" and parts.netloc == parts.hostname
            and any(parts.hostname == root or parts.hostname.endswith("." + root) for root in allowed))


def fetch_image(http, address):
    from io import BytesIO
    from PIL import Image

    if address.startswith("//"):
        address = "https:" + address
    for _ in range(3):
        if not address or not artwork_allowed(address):
            return b""
        with http.stream("GET", address, timeout=10) as response:
            if response.is_redirect:
                address = urljoin(address, response.headers.get("location", ""))
                continue
            if not response.is_success or not response.headers.get("content-type", "").startswith("image/"):
                return b""
            payload = bytearray()
            for block in response.iter_bytes():
                payload.extend(block)
                if len(payload) > 3_000_000:
                    return b""
            try:
                with Image.open(BytesIO(payload)) as image:
                    if min(image.size) < 48:
                        return b""
                    image.verify()
            except (OSError, ValueError):
                return b""
            return bytes(payload)
    return b""


def cover_path(data, source, region, item):
    name = sha256(f"{source}:{region}:{item.id}:{item.cover}".encode()).hexdigest()
    return data / "covers" / name


def cached_cover(data, source, region, item):
    try:
        return cover_path(data, source, region, item).read_bytes()
    except OSError:
        return b""


def cover(data, source, region, item):
    import re
    import httpx

    if payload := cached_cover(data, source, region, item):
        return payload
    path = cover_path(data, source, region, item)
    directory = path.parent
    with http_client() as http:
        address = item.cover
        payload = fetch_image(http, address) if address else b""
        if not payload and source == "amazon" and re.fullmatch(r"[A-Z0-9]{10}", item.id):
            endpoint = f"https://m.media-amazon.com/images/P/{item.id}.01.LZZZZZZZ.jpg"
            try:
                payload = fetch_image(http, endpoint)
            except httpx.HTTPError:
                pass
            if not payload:
                marketplace = f"www.{MARKETPLACES[region].site}"
                product = f"https://{marketplace}/dp/{item.id}"
                response = http.get(product, timeout=8)
                response.raise_for_status()
                parser = ProductCover()
                parser.feed(response.text)
                payload = fetch_image(http, parser.url)
        if not payload:
            return b""
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=directory, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(payload)
            stream.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    return payload
