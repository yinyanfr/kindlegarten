"""Back up purchased comics using the Comipo Play web reader."""

import base64
import json
import os
import re
import sys
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
from time import monotonic, time
from urllib.parse import parse_qsl, quote, unquote, urlencode, urljoin, urlsplit, urlunsplit
from zipfile import BadZipFile, ZipFile

from kindlegarten.browser import launch_browser, playwright_session
from kindlegarten.cli import (
    DownloadProgress, KindleError, book_filename, hidden_input, publish_book, read_hidden_line, require_new_output,
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
    if host in {"play.comipo.app", "www.comipo.app", "comipo.app"}:
        if url.scheme in {"https", "http"}:
            if host == "play.comipo.app":
                match = re.fullmatch(r"/(work|series)/([^/]+)(?:/(?:tree|viewer)(?:/[^/]+)*)?/?", url.path)
                if match:
                    return checked_target(match[1], unquote(match[2]))
            else:
                match = re.fullmatch(r"/product/([^/]+)/?", url.path)
                if match:
                    return checked_target("work", unquote(match[1]))
        raise KindleError("请输入 Comipo Play 的 work 或 series 链接，或 Comipo 商品链接。")
    if source == "comipo":
        raise KindleError("请指定 work:ID、series:ID 或 Comipo 链接。")
    return None


def checked_target(kind, identifier):
    pattern = WORK_ID if kind == "work" else SERIES_ID
    if not pattern.fullmatch(identifier):
        raise KindleError(f"无效的 Comipo {kind} ID。")
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


def session_path(data):
    return data / "comipo-session.json"


def save_session(request, state):
    cookies = [
        cookie for cookie in request.storage_state()["cookies"]
        if cookie["domain"].lstrip(".") in SESSION_DOMAINS
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


def imported_storage(text):
    text = text.strip()
    if text.startswith("{"):
        try:
            cookies = json.loads(text)["cookies"]
            if not isinstance(cookies, list):
                raise TypeError
            cookies = [cookie for cookie in cookies if cookie["domain"].lstrip(".") in SESSION_DOMAINS]
        except (ValueError, KeyError, TypeError, AttributeError):
            raise KindleError("会话文件格式无效。请使用 kindlegarten 保存的 comipo-session.json。") from None
    else:
        if text.startswith(("http://", "https://")):
            raise KindleError("请复制 Comipo Play 请求的 Cookie 标头值。登录后的地址栏链接无法导入。")
        if text.lower().startswith("cookie:"):
            text = text[7:].strip()
        if "\r" in text or "\n" in text:
            raise KindleError("请粘贴单行 Cookie 标头值。")
        try:
            header = SimpleCookie(text)
        except CookieError:
            raise KindleError("Cookie 格式无效。请复制 Comipo Play 请求的完整 Cookie 标头值。") from None
        cookies = [
            {
                "name": name, "value": header[name].value, "domain": domain,
                "path": "/", "expires": -1, "httpOnly": name != "XSRF-TOKEN",
                "secure": True, "sameSite": "Lax",
            }
            for name, domain in SESSION_COOKIE_SCOPES.items()
            if name in header and header[name].value
        ]

    if not any(cookie.get("name") in {"comipoplay_session", "_comipo_SID"} and cookie.get("value")
               for cookie in cookies):
        raise KindleError("没有找到 Comipo 登录 Cookie。请先在 Comipo Play 完成登录。")
    return {"cookies": cookies, "origins": []}


def import_session(data, command, session_file):
    if session_file is None:
        if not sys.stdin.isatty():
            raise KindleError("请在交互式终端粘贴 Cookie，或使用 --session-file 导入会话文件。")
        text = read_hidden_line("粘贴 Comipo Play 请求的 Cookie 标头值并按回车（输入隐藏）：")
    else:
        try:
            text = session_file.expanduser().read_text(encoding="utf-8")
        except UnicodeError:
            raise KindleError("会话文件必须是 UTF-8 格式的 comipo-session.json。") from None
        if not text.lstrip().startswith("{"):
            raise KindleError("--session-file 需要 kindlegarten 保存的 comipo-session.json。")
    storage = imported_storage(text)
    with playwright_session(command, extra="comipo") as (playwright, _):
        request = playwright.request.new_context(storage_state=storage)
        try:
            if not ComipoClient(request).authorized():
                raise KindleError("Comipo Play 未接受这个会话。请重新登录并复制最新的 Cookie。")
            save_session(request, session_path(data))
        finally:
            request.dispose()
    print("已验证并保存 Comipo 登录状态。")


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
        raise KindleError("viviON ID 未返回预期的登录表单或跳转。请检查凭据，或使用 --method browser。") from None
    if state == "challenge":
        raise KindleError("viviON ID 要求人工完成 Cloudflare 验证。请使用 --method browser。")
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
        raise KindleError("viviON ID 表单指向了未知的登录地址。请使用 --method browser。")
    return form, submit


def sign_in_with_credentials(page):
    from playwright.sync_api import TimeoutError

    wait_login_page(page, PASSWORD_FIELD, returning=False)
    form, submit = vivion_form(page, PASSWORD_FIELD)
    login_id = input("viviON 登录 ID：").strip()
    if not login_id:
        raise KindleError("请输入 viviON 登录 ID。")
    form.locator(f":is({LOGIN_ID_FIELD}):visible").fill(login_id)
    password = hidden_input("viviON 密码（输入隐藏）：")
    if not password:
        raise KindleError("请输入 viviON 密码。")
    form.locator(f"{PASSWORD_FIELD}:visible").fill(password)
    submit.click()

    if wait_login_page(page, CODE_FIELD, returning=True) == "form":
        form, submit = vivion_form(page, CODE_FIELD)
        code = hidden_input("viviON 两步验证码（输入隐藏）：").strip()
        if not re.fullmatch(r"[0-9]{6}", code):
            raise KindleError("请输入认证应用显示的六位验证码。")
        form.locator(f":is({CODE_FIELD}):visible").fill(code)
        submit.click()
        try:
            page.wait_for_url(f"{PLAY}/**", wait_until="domcontentloaded", timeout=25_000)
        except TimeoutError:
            raise KindleError("viviON ID 未接受验证码或还需要人工验证。请检查后重试。") from None


def login(data, command, *, method="browser"):
    if not sys.stdin.isatty():
        raise KindleError("请在交互式终端中运行 Comipo 登录命令。")
    with playwright_session(command, extra="comipo") as (playwright, error_type):
        with launch_browser(playwright, error_type, headless=method == "credentials") as browser:
            context = browser.new_context()
            page = context.new_page()
            page.goto(f"{STORE}/login?{urlencode({'to': PLAY + '/'})}", wait_until="domcontentloaded")
            client = ComipoClient(context.request)
            if method == "credentials":
                sign_in_with_credentials(page)
                page.wait_for_load_state("domcontentloaded")
                if not client.authorized():
                    raise KindleError("Comipo Play 尚未建立登录状态。请使用 --method browser 完成登录。")
            else:
                print("请在打开的浏览器里登录 viviON ID。返回 Comipo Play 后，回到终端按回车。",
                      flush=True)
                while True:
                    input()
                    if client.authorized():
                        break
                    print("浏览器尚未登录 Comipo Play。请完成登录，再按回车。", flush=True)
            save_session(context.request, session_path(data))
    print("已保存 Comipo 登录状态。")


class ComipoClient:
    def __init__(self, request):
        self.request = request

    def fetch(self, url, *, payload=None, as_json=True):
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.netloc not in {"play.comipo.app", "dl.comipo.app"}:
            raise KindleError("Comipo 返回了未知的资源域名。")
        headers = {"Referer": PLAY + "/"}
        if parts.netloc == "dl.comipo.app" or payload is not None:
            headers["Origin"] = PLAY
        if parts.netloc == "play.comipo.app" and payload is not None:
            for cookie in self.request.storage_state()["cookies"]:
                if (cookie["name"] == "XSRF-TOKEN"
                        and cookie["domain"].lstrip(".") in {"comipo.app", "play.comipo.app"}):
                    headers["X-XSRF-TOKEN"] = unquote(cookie["value"])
                    break
        options = {} if payload is None else {"method": "POST", "data": payload}
        response = self.request.fetch(url, headers=headers, max_redirects=0, timeout=60000, **options)
        try:
            if not response.ok:
                if response.status in {401, 403}:
                    raise KindleError(f"Comipo 拒绝访问（HTTP {response.status}）。请检查登录及购买状态。")
                raise KindleError(f"Comipo 请求失败（HTTP {response.status}）。")
            if not as_json:
                return response.body()
            try:
                return response.json()
            except ValueError:
                raise KindleError("Comipo 返回了无法读取的 JSON。") from None
        finally:
            response.dispose()

    def api(self, endpoint, *, payload=None, params=None):
        query = "?" + urlencode(params) if params else ""
        return self.fetch(f"{PLAY}/api/comipo/{endpoint}{query}", payload=payload)

    def authorized(self):
        return bool(self.api("authorize"))

    def purchases(self, series_id):
        sales = self.api("v3/content/sales", params={"last": 0})
        ids = list(dict.fromkeys(sale["workno"] for sale in sales))
        members = []
        name = None
        for index in range(0, len(ids), 100):
            batch = self.api("v3/content/works", payload=ids[index:index + 100])
            for series in batch["series"]:
                if str(series["title_id"]) == series_id:
                    name = series.get("name") or name
            members.extend(
                work for work in batch["works"]
                if str((work.get("series") or {}).get("title_id")) == series_id
            )
        if not members:
            raise KindleError(f"Comipo 书库里没有系列 {series_id} 的已购漫画。")
        if not isinstance(name, str) or not name.strip():
            raise KindleError(f"Comipo 没有返回系列 {series_id} 的名称。")
        members.sort(key=lambda work: (int(work["series"].get("volume_number") or 0), work["workno"]))
        return name, members

    def ziptree(self, work_id):
        self.fetch(f"{CDN}/api/comipo/v3/download/sign/cookie?{urlencode({'workno': work_id})}")
        number = work_id[2:]
        bucket = (int(number) + 999) // 1000 * 1000
        directory = f"{work_id[:2]}{bucket:0{len(number)}d}"
        minute = int(time()) // 60 * 60
        return self.fetch(
            f"{CDN}/content/work/{CATEGORIES[work_id[0]]}/{directory}/{work_id}/ziptree.json?v={minute}"
        )


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
        raise KindleError("Comipo 返回了无效的资源路径。")
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
        raise KindleError("请安装 kindlegarten[comipo] 以启用 Comipo 下载。") from None

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
        raise KindleError(f"无法读取 {work_id} 的 Comipo 阅读器密钥。") from None

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
            raise KindleError("Comipo 页面的图片加密格式暂不支持。")
        try:
            key = bytes.fromhex(crypto["key"])
        except ValueError:
            raise KindleError("Comipo 页面返回了无效的图片密钥。") from None
    if not key:
        raise KindleError("Comipo 页面未提供有效的图片密钥。")
    mask = (key * ((len(image) + len(key) - 1) // len(key)))[:len(image)]
    image = (int.from_bytes(image, "little") ^ int.from_bytes(mask, "little")).to_bytes(len(image), "little")
    extension = image_extension(image)
    if not extension:
        raise KindleError("Comipo 返回的页面无法识别为图片。")
    return image, extension


def completed_work(path, work_id):
    if not path.is_file() or path.is_symlink():
        return False
    try:
        with ZipFile(path) as archive:
            return archive.comment == f"Comipo work:{work_id}".encode("ascii") and bool(archive.namelist())
    except BadZipFile:
        return False


def series_destination(directory, title, work_id):
    path = directory / book_filename(title, work_id, ".cbz")
    if (path.exists() or path.is_symlink()) and not completed_work(path, work_id):
        path = directory / book_filename(title, work_id, f" [{work_id}].cbz")
    return path


def write_comic(client, work_id, destination, data):
    require_new_output(destination)
    ziptree = client.ziptree(work_id)
    comics = list(dict.fromkeys(comic_hashes(ziptree["tree"], ziptree["playfile"])))
    if not comics:
        raise KindleError(f"{work_id} 没有 Comipo 阅读器支持的图片漫画。")
    sections = []
    for hashname, play_type in comics:
        manifest, prefix, params, key = encrypted_manifest(
            client, work_id, hashname, play_type, ziptree.get("revision")
        )
        sections.append((hashname, manifest["pages"], prefix, params, key))

    total = sum(len(pages) for _, pages, _, _, _ in sections)
    if not total:
        raise KindleError(f"{work_id} 的 Comipo 清单没有漫画页面。")
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
                archive.comment = f"Comipo work:{work_id}".encode("ascii")
            require_new_output(destination)
            publish_book(staged, destination)
        finished = True
    finally:
        progress.show(monotonic(), finished)
    print(f"已保存 {destination}", flush=True)


def download(target, output, data, books, command):
    kind, identifier = target
    state = session_path(data)
    if output:
        output = output.expanduser().absolute()
        if kind == "work" and output.suffix.lower() != ".cbz":
            raise KindleError("Comipo work 的 --output 需要指定 .cbz 文件。")
    if not state.is_file():
        raise KindleError(f"请先运行 {command} login --source comipo。")
    try:
        storage = json.loads(state.read_text(encoding="utf-8"))
    except ValueError:
        raise KindleError(f"Comipo 登录状态无法读取，请重新运行 {command} login --source comipo。") from None
    with playwright_session(command, extra="comipo") as (playwright, _):
        request = playwright.request.new_context(storage_state=storage)
        try:
            client = ComipoClient(request)
            if not client.authorized():
                raise KindleError(f"Comipo 登录已失效，请重新运行 {command} login --source comipo。")
            save_session(request, state)
            if kind == "work":
                work = client.api(f"v3/work/{identifier}")["work"]
                name = work_title(work, identifier)
                destination = output or books / book_filename(name, identifier, ".cbz")
                write_comic(client, identifier, destination, data)
                return

            name, works = client.purchases(identifier)
            directory = (output or books) / book_filename(name, identifier, "")
            print(f"{name}：{len(works)} 本已购漫画", flush=True)
            failures = []
            for work in works:
                _, work_id = checked_target("work", work["workno"])
                destination = series_destination(directory, work_title(work, work_id), work_id)
                if completed_work(destination, work_id):
                    print(f"已存在 {destination}", flush=True)
                    continue
                try:
                    write_comic(client, work_id, destination, data)
                except (KindleError, OSError) as error:
                    print(f"{work_id}：{error}", file=sys.stderr)
                    failures.append(work_id)
                except (KeyError, TypeError, ValueError):
                    print(f"{work_id}：Comipo 返回的数据格式无法识别。", file=sys.stderr)
                    failures.append(work_id)
            if failures:
                raise KindleError(f"以下漫画未能保存：{', '.join(failures)}。")
        except (KeyError, TypeError, ValueError) as error:
            raise KindleError("Comipo 返回的数据格式无法识别。") from error
        finally:
            request.dispose()
