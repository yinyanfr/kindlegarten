"""Back up purchased comics using the Comipo Play web reader."""

import base64
import json
import os
import re
import shutil
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
from time import monotonic, time
from urllib.parse import parse_qsl, quote, unquote, urlencode, urljoin, urlsplit, urlunsplit
from zipfile import BadZipFile, ZipFile

from kindlegarten.browser import browser_context, playwright_session
from kindlegarten.i18n import tr
from kindlegarten.library import PLAY_TIMEZONE, library_cache_path, purchase_timestamp
from kindlegarten.network import browser_proxy
from kindlegarten.progress import DOWNLOAD, check_cancelled, download_directory, download_session, report_book
from kindlegarten.cli import (
    DownloadProgress, KindleError, SignInRequired, book_filename, hidden_input, publish_book, read_hidden_line,
    require_new_output,
)

PLAY = "https://play.comipo.app"
STORE = "https://www.comipo.app"
CDN = "https://dl.comipo.app"
SESSION_DOMAINS = {"comipo.app", "play.comipo.app", "www.comipo.app"}
SESSION_COOKIE_SCOPES = {
    "comipoplay_session": "play.comipo.app",
    "_comipo_SID": ".comipo.app",
    "XSRF-TOKEN": "play.comipo.app",
}
@dataclass(frozen=True)
class PlaySite:
    name: str
    play: str
    cdn: str
    api_prefix: str
    login_url: str
    domains: set[str]
    scopes: dict[str, str]


COMIPO = PlaySite("Comipo", PLAY, CDN, "/api/comipo/",
                  f"{STORE}/login?{urlencode({'to': PLAY + '/'})}",
                  SESSION_DOMAINS, SESSION_COOKIE_SCOPES)
DLSITE = PlaySite("DLsite", "https://play.dlsite.com", "https://play.dl.dlsite.com", "/api/",
                  "https://play.dlsite.com/login/",
                  {"dlsite.com", "play.dlsite.com", "www.dlsite.com"},
                  {"dlsite_session": "play.dlsite.com", "dlsiteplay_session": "play.dlsite.com",
                   "DLsite_SID": ".dlsite.com", "XSRF-TOKEN": "play.dlsite.com"})

LOGIN_ORIGINS = ("https://login.vivionid.com", "https://login.dlsite.com")
PASSWORD_FIELD = 'input[type="password"]'
LOGIN_ID_FIELD = ('input[name="login_id"], input[autocomplete="username"], input[type="email"], '
                  'input[placeholder="ログインID"]')
CODE_FIELD = ('input[autocomplete="one-time-code"], input[name="totp"], input[name="otp"], '
              'input[name="authentication_code"], input[name="verification_code"], '
              'input[name="two_factor_code"], input[name="one_time_password"], '
              'input[name="code"], input[placeholder="認証コード"]')
WORK_ID = re.compile(r"[BRV]J(?:[0-9]{6}|[0-9]{8})", re.IGNORECASE | re.ASCII)
STORE_SERIES_ID = re.compile(r"TITLE[0-9]+", re.IGNORECASE | re.ASCII)
SERIES_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}")
COMIC_TYPES = {"ebook_fixed", "ebook_webtoon", "ebook_voicecomic"}
CATEGORIES = {"B": "books", "R": "doujin", "V": "professional"}
DEFAULT_REVISION = "00000000-0000-7000-8000-000000000000"


def parse_target(value, source=None):
    if source == "amazon":
        return None
    kind, sep, identifier = value.partition(":")
    if sep and kind.lower() in {"work", "series"}:
        return checked_target(kind.lower(), identifier)
    if WORK_ID.fullmatch(value):
        return checked_target("work", value)
    if STORE_SERIES_ID.fullmatch(value):
        return checked_target("series", value)
    try:
        url = urlsplit(value)
        host = url.hostname
    except ValueError:
        host = None
    if host in {"play.comipo.app", "www.comipo.app", "comipo.app", "play.dlsite.com", "www.dlsite.com"}:
        if url.scheme in {"https", "http"}:
            if host in {"play.comipo.app", "play.dlsite.com"}:
                match = re.fullmatch(r"/(work|series)/([^/]+)(?:/(?:tree|viewer)(?:/[^/]+)*)?/?", url.path)
                if match:
                    return checked_target(match[1], unquote(match[2]))
            else:
                match = re.fullmatch(r"/(?:product/|[^/]+/work/=/product_id/)([A-Za-z0-9]+)(?:\.html)?/?", url.path)
                if match:
                    return checked_target("work", unquote(match[1]))
        raise KindleError(tr('Enter a supported Play work, series, or store link.'))
    if source in {"comipo", "dlsite"}:
        raise KindleError(tr('Enter work:ID, series:ID, or a supported Play link.'))
    return None


def checked_target(kind, identifier):
    pattern = WORK_ID if kind == "work" else SERIES_ID
    if not pattern.fullmatch(identifier):
        raise KindleError(tr('Invalid Play {kind} ID.', kind=kind))
    if kind == "work" or STORE_SERIES_ID.fullmatch(identifier):
        identifier = identifier.upper()
    return kind, identifier


def work_title(work, work_id):
    name = work.get("name")
    if isinstance(name, dict):
        names = (name.get("ja_JP"), *name.values())
    else:
        names = (name,)
    return next((value for value in names if isinstance(value, str) and value.strip()), work_id)


def session_path(data, site=COMIPO):
    return data / f"{site.name.lower()}-session.json"


def save_session(request, state, site=COMIPO):
    cookies = [
        cookie for cookie in request.storage_state()["cookies"]
        if cookie["domain"].lstrip(".") in site.domains
    ]
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=state.parent,
                            prefix=".comipo-", delete=False) as file:
        temporary = Path(file.name)
        try:
            json.dump({"cookies": cookies, "origins": []}, file)
            file.close()
            os.replace(temporary, state)
        finally:
            temporary.unlink(missing_ok=True)


def imported_storage(text, site=COMIPO):
    text = text.strip()
    if text.startswith("{"):
        try:
            cookies = json.loads(text)["cookies"]
            if not isinstance(cookies, list):
                raise TypeError
            cookies = [cookie for cookie in cookies if cookie["domain"].lstrip(".") in site.domains]
        except (ValueError, KeyError, TypeError, AttributeError):
            raise KindleError(tr('Invalid session file. Use the JSON session saved by Kindlegarten.')) from None
    else:
        if text.startswith(("http://", "https://")):
            raise KindleError(tr('Copy the Cookie request header from Play. A sign-in URL cannot be imported.'))
        if text.lower().startswith("cookie:"):
            text = text[7:].strip()
        if "\r" in text or "\n" in text:
            raise KindleError(tr('Paste the Cookie header value on a single line.'))
        try:
            header = SimpleCookie(text)
        except CookieError:
            raise KindleError(tr('Invalid Cookie format. Copy the full Cookie header from Play.')) from None
        if site is DLSITE and {"comipoplay_session", "_comipo_SID"} & header.keys():
            raise KindleError(tr("Use the Cookie from DLsite Play. An old Comipo Cookie cannot sign in to DLsite."))
        scopes = site.scopes if site is COMIPO else {
            name: site.scopes.get(name, "play.dlsite.com") for name in header
        }
        cookies = [
            {
                "name": name, "value": header[name].value, "domain": domain,
                "path": "/", "expires": -1, "httpOnly": name != "XSRF-TOKEN",
                "secure": True, "sameSite": "Lax",
            }
            for name, domain in scopes.items()
            if name in header and header[name].value
        ]

    if not any(cookie.get("value") and (
        site is DLSITE or cookie.get("name") in {"comipoplay_session", "_comipo_SID"}
    ) for cookie in cookies):
        raise KindleError(tr('No sign-in cookie was found. Sign in to Play first.'))
    return {"cookies": cookies, "origins": []}


def import_session(data, command, session_file, *, site=COMIPO, pasted=None):
    if pasted is not None:
        text = pasted
    elif session_file is None:
        if not sys.stdin.isatty():
            raise KindleError(tr('Paste a Cookie in an interactive terminal or import a file with --session-file.'))
        text = read_hidden_line(tr('Paste the Play Cookie header and press Enter (input hidden): '))
    else:
        try:
            text = session_file.expanduser().read_text(encoding="utf-8")
        except UnicodeError:
            raise KindleError(tr('The session file must be UTF-8 JSON.')) from None
        if not text.lstrip().startswith("{"):
            raise KindleError(tr('--session-file requires a JSON session saved by Kindlegarten.'))
    storage = imported_storage(text, site)
    with playwright_session(command, extra="comipo") as (playwright, _):
        request = playwright.request.new_context(storage_state=storage, **browser_proxy())
        try:
            if not make_client(request, site).authorized():
                raise KindleError(tr("The service rejected this session. Sign in and copy fresh cookies."))
            library_cache_path(data, site.name.lower(), None).unlink(missing_ok=True)
            save_session(request, session_path(data, site), site)
        finally:
            request.dispose()
    print(tr('Verified and saved the Comipo session.').replace("Comipo", site.name))


def wait_login_page(page, field, *, returning):
    from playwright.sync_api import TimeoutError

    try:
        state = page.wait_for_function(
            """({ loginOrigins, play, field, returning }) => {
                if (returning && location.origin === play)
                    return "play";
                if (loginOrigins.includes(location.origin) &&
                    Array.from(document.querySelectorAll(field)).some(input => input.getClientRects().length))
                    return "form";
                if (document.querySelector('#challenge-form, #challenge-running, [name="cf-turnstile-response"]'))
                    return "challenge";
                return false;
            }""",
            arg={"loginOrigins": LOGIN_ORIGINS, "play": PLAY, "field": field, "returning": returning},
            timeout=25_000,
        ).json_value()
    except TimeoutError:
        raise KindleError(tr('viviON ID did not return the expected login form. Check your credentials or use --method browser.')) from None
    if state == "challenge":
        raise KindleError(tr('viviON ID requires interactive Cloudflare verification. Use --method browser.'))
    return state


def vivion_form(page, field):
    locator = page.locator(f":is({field}):visible")
    form = page.locator("form").filter(has=locator)
    submit = form.locator(
        ':is(button[type="submit"], input[type="submit"], button:not([type])):visible'
    ).first
    action = submit.get_attribute("formaction") or form.get_attribute("action") or page.url
    origin = urlsplit(page.url)
    destination = urlsplit(urljoin(page.url, action))
    if (f"{origin.scheme}://{origin.netloc}" not in LOGIN_ORIGINS
            or f"{destination.scheme}://{destination.netloc}" not in LOGIN_ORIGINS):
        raise KindleError(tr('The viviON ID form points to an unknown sign-in address. Use --method browser.'))
    return form, submit


def sign_in_with_credentials(page):
    from playwright.sync_api import TimeoutError

    wait_login_page(page, PASSWORD_FIELD, returning=False)
    form, submit = vivion_form(page, PASSWORD_FIELD)
    login_id = input(tr('viviON login ID: ')).strip()
    if not login_id:
        raise KindleError(tr('Enter your viviON login ID.'))
    form.locator(f":is({LOGIN_ID_FIELD}):visible").fill(login_id)
    password = hidden_input(tr('viviON password (input hidden): '))
    if not password:
        raise KindleError(tr('Enter your viviON password.'))
    form.locator(f"{PASSWORD_FIELD}:visible").fill(password)
    submit.click()

    if wait_login_page(page, CODE_FIELD, returning=True) == "form":
        form, submit = vivion_form(page, CODE_FIELD)
        code = hidden_input(tr('viviON verification code (input hidden): ')).strip()
        if not re.fullmatch(r"[0-9]{6}", code):
            raise KindleError(tr('Enter the six-digit code from your authenticator.'))
        form.locator(f":is({CODE_FIELD}):visible").fill(code)
        submit.click()
        try:
            page.wait_for_url(f"{PLAY}/**", wait_until="domcontentloaded", timeout=25_000)
        except TimeoutError:
            raise KindleError(tr('viviON ID rejected the code or needs additional verification. Check it and try again.')) from None


def login(data, command, *, method="browser", site=COMIPO, interactive=True, force=False):
    if interactive and not sys.stdin.isatty():
        raise KindleError(tr('Run the Play login command in an interactive terminal.'))
    with playwright_session(command, extra="comipo") as (playwright, error_type):
        browser_profile = data / "browser" / site.name.lower()
        profile = None if method == "credentials" or force else browser_profile
        with browser_context(playwright, error_type, profile=profile, headless=method == "credentials") as context:
            page = context.new_page()
            page.goto(site.login_url, wait_until="domcontentloaded")
            client = make_client(context.request, site)
            if method == "credentials":
                sign_in_with_credentials(page)
                page.wait_for_load_state("domcontentloaded")
            elif not wait_for_authorization(context, client, page):
                raise KindleError(tr("Browser sign-in did not complete."))
            if not client.authorized():
                raise KindleError(tr("The service did not establish a session."))
            library_cache_path(data, site.name.lower(), None).unlink(missing_ok=True)
            save_session(context.request, session_path(data, site), site)
        if force:
            shutil.rmtree(browser_profile, ignore_errors=True)
    print(tr('Saved the Comipo session.').replace("Comipo", site.name))


def wait_for_authorization(context, client, page):
    finish = monotonic() + 600
    while monotonic() < finish:
        pages = [item for item in context.pages if not item.is_closed()]
        if not pages:
            return False
        if any(urlsplit(item.url).netloc == urlsplit(client.site.play).netloc for item in pages):
            if client.authorized():
                return True
        pages[0].wait_for_timeout(1000)
    return False


class PlayHttpError(KindleError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class ComipoClient:
    def __init__(self, request, site=COMIPO):
        self.request = request
        self.site = site

    def fetch(self, url, *, payload=None, as_json=True):
        check_cancelled()
        parts = urlsplit(url)
        if parts.scheme != "https" or f"https://{parts.netloc}" not in {self.site.play, self.site.cdn}:
            raise KindleError(tr('The reader returned an unknown resource host.'))
        headers = {"Referer": self.site.play + "/"}
        if f"https://{parts.netloc}" == self.site.cdn or payload is not None:
            headers["Origin"] = self.site.play
        if f"https://{parts.netloc}" == self.site.play and payload is not None:
            for cookie in self.request.storage_state()["cookies"]:
                if (cookie["name"] == "XSRF-TOKEN"
                        and cookie["domain"].lstrip(".") in self.site.domains):
                    headers["X-XSRF-TOKEN"] = unquote(cookie["value"])
                    break
        options = {} if payload is None else {"method": "POST", "data": payload}
        response = self.request.fetch(url, headers=headers, max_redirects=0, timeout=60000, **options)
        try:
            check_cancelled()
            if not response.ok:
                if response.status in {401, 403}:
                    raise PlayHttpError(response.status, tr("{site} refused access (HTTP {status}). Check your session and purchases.",
                                         site=self.site.name, status=response.status))
                raise PlayHttpError(response.status, tr("{site} request failed (HTTP {status}).", site=self.site.name, status=response.status))
            if not as_json:
                return response.body()
            try:
                return response.json()
            except ValueError:
                raise KindleError(tr('The reader returned invalid JSON.')) from None
        finally:
            response.dispose()

    def api(self, endpoint, *, payload=None, params=None):
        query = "?" + urlencode(params) if params else ""
        return self.fetch(f"{self.site.play}{self.site.api_prefix}{endpoint}{query}", payload=payload)

    def authorized(self):
        return bool(self.authorization())

    def authorization(self):
        try:
            return self.api("authorize") or None
        except PlayHttpError as error:
            if error.status in {401, 403}:
                return None
            raise

    def catalog(self):
        sales = self.api("v3/content/sales", params={"last": 0})
        dates = {}
        for sale in sales:
            identifier = sale["workno"]
            date = purchase_timestamp(sale.get("sales_date"), PLAY_TIMEZONE)
            previous = dates.setdefault(identifier, None)
            if date is not None and (previous is None or date > previous):
                dates[identifier] = date
        ids = list(dates)
        works = []
        series = {}
        for index in range(0, len(ids), 100):
            included = set(ids[index:index + 100])
            batch = self.api("v3/content/works", payload=ids[index:index + 100])
            works.extend(dict(item, purchased_at=dates[item["workno"]])
                         for item in batch["works"] if item["workno"] in included)
            series.update((str(item["title_id"]), item) for item in batch.get("series", []))
        return works, series

    def purchases(self, series_id):
        works, series = self.catalog()
        members = [item for item in works if str((item.get("series") or {}).get("title_id")) == series_id]
        if not members:
            raise KindleError(tr("No purchased volumes were found for series {id}.", id=series_id))
        name = work_title(series.get(series_id, {}), series_id)
        members.sort(key=lambda work: (int(work["series"].get("volume_number") or 0), work["workno"]))
        return name, members

    def ziptree(self, work_id):
        cdn = self.site.cdn
        signed = self.fetch(f"{cdn}{self.site.api_prefix}v3/download/sign/cookie?{urlencode({'workno': work_id})}")
        minute = int(time()) // 60 * 60
        if self.site is DLSITE:
            return self.fetch(f"{signed['url'].rstrip('/')}/ziptree.json?v={minute}")
        number = work_id[2:]
        bucket = (int(number) + 999) // 1000 * 1000
        directory = f"{work_id[:2]}{bucket:0{len(number)}d}"
        return self.fetch(f"{cdn}/content/work/{CATEGORIES[work_id[0]]}/{directory}/{work_id}/ziptree.json?v={minute}")


def make_client(request, site):
    return ComipoClient(request) if site is COMIPO else ComipoClient(request, site)


@contextmanager
def play_client(storage, command, site):
    with playwright_session(command, extra="comipo") as (playwright, _):
        request = playwright.request.new_context(storage_state=storage, **browser_proxy())
        try:
            yield request, make_client(request, site)
        finally:
            request.dispose()


def saved_account(data, command, site):
    try:
        storage = json.loads(download_session(session_path(data, site)).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None
    with play_client(storage, command, site) as (_, client):
        authorization = client.authorization()
    if authorization is None:
        return None
    profile = {}
    if isinstance(authorization, dict):
        login_id = authorization.get("login_id")
        account_id = authorization.get("account_id")
        if isinstance(login_id, str) and login_id.strip():
            profile["login"] = login_id.strip()[:256]
        if type(account_id) is int:
            profile["id"] = str(account_id)
    return profile


@contextmanager
def session(data, command, site=COMIPO):
    state = download_session(session_path(data, site))
    if not state.is_file():
        if site is DLSITE and session_path(data, COMIPO).is_file():
            raise SignInRequired(tr("Sign in to DLsite with the same viviON ID as your saved Comipo account."))
        raise SignInRequired(tr("Sign in to {site} before accessing the library.", site=site.name))
    try:
        storage = json.loads(state.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        raise SignInRequired(tr("The saved {site} session is invalid. Sign in again.", site=site.name)) from None
    with play_client(storage, command, site) as (request, client):
        if not client.authorized():
            raise SignInRequired(tr("The saved {site} session has expired. Sign in again.", site=site.name))
        if site is COMIPO:
            save_session(request, state)
        else:
            save_session(request, state, site)
        yield client


def comic_hashes(tree, files):
    for node in tree:
        if node["type"] == "folder":
            yield from comic_hashes(node["children"], files)
        elif node["type"] == "file":
            hashname = node["hashname"]
            play_type = files.get(hashname, {}).get("type")
            if play_type in COMIC_TYPES:
                yield hashname, play_type


def resource_url(prefix, hashname, resource, parameters):
    parts = urlsplit(prefix)
    segments = [unquote(segment) for segment in (hashname, *resource.split("/"))]
    if any(segment in {"", ".", ".."} or "/" in segment or "\\" in segment for segment in segments):
        raise KindleError(tr('The reader returned an invalid resource path.'))
    suffix = "/".join(quote(segment, safe="") for segment in segments)
    path = f"{parts.path.rstrip('/')}/{suffix}"
    params = dict(parse_qsl(parts.query))
    params.update(parameters)
    return urlunsplit((parts.scheme, parts.netloc, path, urlencode(params), ""))


def encrypted_manifest(client, work_id, hashname, play_type, revision):
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding, rsa
    except ImportError:
        raise KindleError(tr('Install kindlegarten[comipo] to download Play books.')) from None

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=4096)
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    token = client.api(f"v3/viewer/token/{work_id}", payload={
        "play_type": play_type,
        "revision": revision or DEFAULT_REVISION,
        "public_key": base64.b64encode(public_key).decode("ascii"),
    })
    try:
        sha = hashes.SHA256()
        clear_key = private_key.decrypt(
            base64.b64decode(token["key"], validate=True),
            padding.OAEP(mgf=padding.MGF1(sha), algorithm=sha, label=None),
        )
        key = bytes.fromhex(clear_key.decode("ascii"))
        if not key:
            raise ValueError("empty viewer key")
    except ValueError:
        raise KindleError(tr('The reader key for {work_id} could not be read.', work_id=work_id)) from None

    parameters = dict(token.get("parameters") or {})
    if revision:
        parameters["v"] = revision
    prefix = token["prefix"]
    manifest_url = resource_url(prefix, hashname, "viewer-meta.json", parameters)
    return client.fetch(manifest_url), prefix, parameters, key


def image_extension(image):
    if image.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if image.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if image.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if image[:4] == b"RIFF" and image[8:12] == b"WEBP":
        return ".webp"
    if image.startswith(b"BM"):
        return ".bmp"
    if image[4:12] in {b"ftypavif", b"ftypavis"}:
        return ".avif"
    return None


def read_page(client, url, key, crypto):
    image = client.fetch(url, as_json=False)
    extension = image_extension(image)
    if extension:
        return image, extension
    if crypto:
        if crypto.get("method") != "xor":
            raise KindleError(tr('This reader image format is unsupported.'))
        try:
            key = bytes.fromhex(crypto["key"])
        except ValueError:
            raise KindleError(tr('The reader returned an invalid image key.')) from None
    if not key:
        raise KindleError(tr('The reader did not supply a usable image key.'))
    mask = (key * ((len(image) + len(key) - 1) // len(key)))[:len(image)]
    image = (int.from_bytes(image, "little") ^ int.from_bytes(mask, "little")).to_bytes(len(image), "little")
    extension = image_extension(image)
    if not extension:
        raise KindleError(tr('The page was not a recognizable image.'))
    return image, extension


def completed_work(path, work_id):
    if not path.is_file() or path.is_symlink():
        return False
    try:
        with ZipFile(path) as archive:
            return (archive.comment == f"Comipo work:{work_id}".encode("ascii")
                    and bool(archive.namelist()) and archive.testzip() is None)
    except BadZipFile:
        return False


def series_destination(directory, title, work_id):
    path = directory / book_filename(title, work_id, ".cbz")
    if (path.exists() or path.is_symlink()) and not completed_work(path, work_id):
        path = directory / book_filename(title, work_id, f" [{work_id}].cbz")
    return path


def write_comic(client, work_id, destination, data, *, announce=True):
    require_new_output(destination)
    ziptree = client.ziptree(work_id)
    comics = list(dict.fromkeys(comic_hashes(ziptree["tree"], ziptree["playfile"])))
    if not comics:
        raise KindleError(tr('No supported image comic is available for {work_id}.', work_id=work_id))
    sections = []
    for hashname, play_type in comics:
        manifest, prefix, params, key = encrypted_manifest(
            client, work_id, hashname, play_type, ziptree.get("revision")
        )
        sections.append((hashname, manifest["pages"], prefix, params, key))

    total = sum(len(pages) for _, pages, _, _, _ in sections)
    if not total:
        raise KindleError(tr('The manifest for {work_id} contains no comic pages.', work_id=work_id))
    progress = DownloadProgress()
    progress.start([None] * total)
    finished = False
    width = max(4, len(str(total)))
    try:
        with TemporaryDirectory(prefix=".job-", dir=data) as temporary:
            staged = Path(temporary) / "comipo.cbz"
            with ZipFile(staged, "w") as archive:
                page_number = 0
                for hashname, pages, prefix, params, key in sections:
                    for page in pages:
                        url = resource_url(prefix, hashname, page["src"], params)
                        image, extension = read_page(client, url, key, page.get("crypto"))
                        page_number += 1
                        archive.writestr(f"{page_number:0{width}d}{extension}", image)
                        progress.finish_resource()
                        progress.advance(len(image))
                archive.comment = f"{client.site.name} work:{work_id}".encode("ascii")
            require_new_output(destination)
            publish_book(staged, destination)
        finished = True
    finally:
        progress.show(monotonic(), finished)
    if announce:
        print(tr('Saved {destination}', destination=destination), flush=True)


def download(target, output, data, books, command, requested="auto", *, site=COMIPO, resume=False):
    from kindlegarten.formats import already_saved, convert, publish_export, selected_format, unique_destination
    from kindlegarten.library import by_purchase

    kind, identifier = target
    output = output.expanduser().absolute() if output else None
    destination = output if kind == "work" else None
    requested = selected_format(requested, destination)
    if destination is not None:
        require_new_output(destination)
    with session(data, command, site) as client:
        if site is DLSITE:
            library = by_purchase(client, dlsite=True)
            all_books = {book.id: book for book in library.books}
            all_books.update((book.id, book) for series in library.series for book in series.books)
            if kind == "work":
                book = all_books.get(identifier)
                if book is None:
                    raise KindleError(tr("This book is not in the purchased library."))
                entries = [(book.id, book.title)]
                directory = books
            else:
                series = next((item for item in library.series if item.id == identifier), None)
                if series is None:
                    raise KindleError(tr("No purchased volumes were found for series {id}.", id=identifier))
                directory = (output or books) / book_filename(series.title, identifier, "")
                entries = [(book.id, book.title) for book in series.books]
        elif kind == "work":
            work = client.api(f"v3/work/{identifier}")["work"]
            entries = [(identifier, work_title(work, identifier))]
            directory = books
        else:
            name, works = client.purchases(identifier)
            directory = (output or books) / book_filename(name, identifier, "")
            entries = [(work["workno"], work_title(work, work["workno"])) for work in works]
            print(tr("{title}: {count} purchased volumes", title=name, count=len(entries)), flush=True)

        failures = []
        for work_id, title in entries:
            report_book(title)
            _, work_id = checked_target("work", work_id)
            identity = f"{site.name}:{work_id}"
            try:
                identities = (identity, f"Comipo:{work_id}") if site is DLSITE else (identity,)
                if (kind == "series" or resume) and (existing := next(
                    (path for key in identities if (path := already_saved(data, directory, key, requested))), None
                )):
                    print(tr("Already saved: {path}", path=existing), flush=True)
                    continue
                if site is COMIPO and requested in {"auto", "cbz"}:
                    path = destination or series_destination(directory, title, work_id)
                    if (kind == "series" or resume) and completed_work(path, work_id):
                        print(tr("Already saved: {path}", path=path), flush=True)
                        continue
                    if DOWNLOAD.get() is None:
                        if destination is None and path.exists():
                            path = unique_destination(directory, title, work_id, ".cbz")
                        write_comic(client, work_id, path, data)
                        continue
                workspace = download_directory(data)
                with TemporaryDirectory(prefix=".job-", dir=workspace) as folder:
                    staging = Path(folder)
                    if site is DLSITE:
                        from kindlegarten.dlsite import download_book

                        source = download_book(client, work_id, staging)
                    else:
                        source = staging / "book.cbz"
                        write_comic(client, work_id, source, workspace, announce=False)
                    artifact = convert(source, requested, title, identity)
                    path = publish_export(artifact, destination, directory, title, work_id, data, identity, requested)
                    print(tr('Saved {destination}', destination=path), flush=True)
            except (KindleError, OSError, KeyError, TypeError, ValueError, BadZipFile) as error:
                if kind == "work":
                    if isinstance(error, (KindleError, OSError)):
                        raise
                    raise KindleError(tr("The service returned an unrecognized book response.")) from None
                detail = str(error) if isinstance(error, (KindleError, OSError)) else tr(
                    "The service returned an unrecognized book response."
                )
                print(f"{work_id}: {detail}", file=sys.stderr)
                failures.append(work_id)
        if failures:
            raise KindleError(tr("The following books could not be saved: {ids}.", ids=", ".join(failures)))
