import argparse
import errno
import getpass
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import unicodedata
import warnings
import webbrowser
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile


SCRIPT = Path(__file__).resolve()
COMMAND = "kindlegarten"
VENV_PYTHON = Path("venv") / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
KINDL_SOURCE = (
    "https://codeload.github.com/bGFiMTI2/kindl/tar.gz/a896621dde52fc052a960f4bab7af84036013e23",
    "defb9e1647096013c94067e8eaea43dd1d7289afa7c953029f18e0c010675a4e",
)
DEDRM_SOURCE = (
    "https://github.com/Satsuoni/DeDRM_tools/releases/download/v10.0.35/DeDRM_tools.zip",
    "5c85cce954b17836c2b421d6ae8b313f0aaeb190a9a3f81881636dbc80e25bc3",
)
KFX_SOURCE = (
    "https://plugins.calibre-ebook.com/291290.zip",
    "338809c18e5f9bb721dc3570a64cf6f7add4a1711c8183e6179e90fcbadf3c1d",
)
PLUGINS = ("KFX Input.zip", "DeDRM.zip")
REGIONS = {"amazon.co.jp": "jp", "amazon.com": "us"}


class KindleError(Exception):
    pass


def user_data_directory():
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library/Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    return base / "kindlegarten"


def calibre_executable(name):
    binary = name + ".exe" if os.name == "nt" else name
    override = os.environ.get("KINDLE_CALIBRE_BIN")
    if override:
        candidate = Path(override).expanduser() / binary
        if candidate.is_file():
            return candidate
    else:
        on_path = shutil.which(binary)
        if on_path:
            return Path(on_path)
        if os.name == "nt":
            directories = [Path(root) / "Calibre2" for key in ("ProgramFiles", "ProgramFiles(x86)")
                           if (root := os.environ.get(key))]
        elif sys.platform == "darwin":
            directories = [Path("/Applications/calibre.app/Contents/MacOS"),
                           Path.home() / "Applications/calibre.app/Contents/MacOS"]
        else:
            directories = [Path("/opt/calibre"), Path("/usr/bin"), Path("/usr/local/bin")]
        for directory in directories:
            candidate = directory / binary
            if candidate.is_file():
                return candidate
    raise KindleError(f"找不到 {binary}。请安装 Calibre，或用 KINDLE_CALIBRE_BIN 指定其可执行文件目录。")


@contextmanager
def project_lock():
    DATA.mkdir(mode=0o700, exist_ok=True)
    with (DATA / "lock").open("a+b") as lock:
        lock.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno not in {errno.EACCES, errno.EAGAIN}:
                raise
            raise KindleError("另一个下载器进程正在运行，请结束后再试。") from None
        yield


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def fetch_verified(source, destination):
    url, digest = source
    try:
        request = Request(url, headers={"User-Agent": "kindle-downloader/1.0"})
        with urlopen(request, timeout=60) as response, destination.open("xb") as output:
            shutil.copyfileobj(response, output)
    except (URLError, TimeoutError) as error:
        raise KindleError(f"无法下载依赖 {url}（{error}）。") from None
    if sha256(destination) != digest:
        raise KindleError(f"依赖包的 SHA-256 与预期不符：{url}")


def calibre_config_dir():
    override = os.environ.get("CALIBRE_CONFIG_DIRECTORY")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData/Roaming") / "calibre"
    if sys.platform == "darwin":
        return Path.home() / "Library/Preferences/calibre"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "calibre"


def configure_calibre(plugin_dir, config_dir):
    customize = calibre_executable("calibre-customize")
    env = dict(os.environ, CALIBRE_CONFIG_DIRECTORY=str(config_dir))
    config_dir.mkdir()
    for plugin in PLUGINS:
        result = subprocess.run([customize, "--add-plugin", plugin_dir / plugin],
                                env=env, capture_output=True)
        if result.returncode:
            raise KindleError(f"Calibre 插件 {plugin} 加载失败。")
    return env


def setup_ready():
    return all((RUNTIME / filename).is_file() for filename in (VENV_PYTHON, *PLUGINS))


def setup(_args):
    calibre_executable("calibre-debug")
    calibre_executable("calibre-customize")
    if setup_ready():
        print("依赖已安装。")
        return

    base_python = os.environ.get("KINDLE_PYTHON", sys.executable)
    with TemporaryDirectory(prefix=".setup-", dir=DATA) as temporary:
        staging = Path(temporary) / "runtime"
        staging.mkdir()
        print("正在安装 kindl、DeDRM 和 KFX Input…", flush=True)
        archive = staging / "kindl.tar.gz"
        fetch_verified(KINDL_SOURCE, archive)
        subprocess.run([base_python, "-m", "venv", staging / "venv"], check=True)
        subprocess.run([staging / VENV_PYTHON, "-m", "pip", "install",
                        "--disable-pip-version-check", "--no-cache-dir", "--constraint",
                        SCRIPT.with_name("constraints.txt"), archive], check=True)
        archive.unlink()

        dedrm_archive = staging / "DeDRM_tools.zip"
        fetch_verified(DEDRM_SOURCE, dedrm_archive)
        with ZipFile(dedrm_archive) as package:
            (staging / "DeDRM.zip").write_bytes(package.read("DeDRM_plugin.zip"))
        dedrm_archive.unlink()

        kfx_archive = staging / "KFX Input.zip"
        local_kfx = calibre_config_dir() / "plugins/KFX Input.zip"
        if local_kfx.is_file() and sha256(local_kfx) == KFX_SOURCE[1]:
            shutil.copyfile(local_kfx, kfx_archive)
        else:
            fetch_verified(KFX_SOURCE, kfx_archive)

        configure_calibre(staging, Path(temporary) / "calibre")
        if RUNTIME.exists():
            shutil.rmtree(RUNTIME)
        staging.rename(RUNTIME)
    print("依赖安装完成。")


def require_setup():
    if not setup_ready():
        raise KindleError(f"请先运行 {COMMAND} setup。")


def account_path(region):
    return DATA / f"account-{region}.json"


def book_target(value):
    if re.fullmatch(r"[A-Z0-9]{10}", value, re.IGNORECASE | re.ASCII):
        return value.upper(), None
    try:
        url = urlsplit(value)
        host = re.sub(r"^(www|smile)\.", "", url.hostname or "")
    except ValueError:
        raise KindleError("请输入 10 位 ASIN 或 Amazon 商品链接。") from None
    if url.scheme not in {"https", "http"} or host not in REGIONS:
        raise KindleError("请输入 10 位 ASIN 或 amazon.co.jp / amazon.com 的完整商品链接。")
    match = re.search(r"/(?:dp|gp/product|gp/aw/d)/([A-Z0-9]{10})(?:/|$)",
                      url.path, re.IGNORECASE | re.ASCII)
    if not match:
        raise KindleError("商品链接里找不到 ASIN。")
    return match[1].upper(), REGIONS[host]


def book_filename(title, asin, extension):
    stem = unicodedata.normalize("NFC", title)
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f-\x9f\ud800-\udfff]+', "_", stem)
    stem = re.sub(r"\s+", " ", stem).strip(" .")
    if not stem.strip(" _."):
        stem = asin

    device = stem.split(".", 1)[0].rstrip().upper()
    if re.fullmatch(r"CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|(?:COM|LPT)[1-9¹²³]", device):
        stem = f"_{stem}"

    max_bytes = 255 - len(extension.encode("utf-8"))
    stem = stem.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore").rstrip(" .")
    return f"{stem or asin}{extension}"


def publish_book(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".kindle-", dir=destination.parent) as temporary:
        staged = Path(temporary) / source.name
        shutil.copyfile(source, staged)
        if os.name == "nt":
            staged.rename(destination)
        else:
            os.link(staged, destination)


def start_login(args):
    if args.session_file is not None and (args.source, args.method) != ("comipo", "cookie"):
        raise KindleError("--session-file 和 --source comipo --method cookie 一起使用。")
    if args.source == "comipo":
        from kindlegarten.comipo import import_session, login

        if args.method == "callback":
            raise KindleError("Comipo Play 的登录状态保存在 Cookie 中。请使用 --method cookie、browser 或 credentials。")
        if args.method == "cookie":
            import_session(DATA, COMMAND, args.session_file)
        else:
            login(DATA, COMMAND, method=args.method or "browser")
        return
    if args.method in {"credentials", "cookie"}:
        raise KindleError("Amazon 请使用 --method browser 或 --method callback。")
    require_setup()
    state = account_path(args.region)
    if state.exists():
        print(f"{args.region} 已有登录状态。更换账户前请先运行 {COMMAND} logout --region {args.region}。")
        return
    run_worker("_login", args.region, state, args.method or "default", sys.executable)


def logout(args):
    if args.source == "comipo":
        from kindlegarten.comipo import session_path

        session_path(DATA).unlink(missing_ok=True)
        print("已清除 Comipo 的本地登录状态。")
        return
    state = account_path(args.region)
    state.unlink(missing_ok=True)
    state.with_suffix(".json.tmp").unlink(missing_ok=True)
    print(f"已清除 {args.region} 的本地登录状态。")


def run_worker(command, *args):
    result = subprocess.run([RUNTIME / VENV_PYTHON, SCRIPT, command, *args])
    if result.returncode:
        raise SystemExit(result.returncode)


def download_comipo(args):
    from kindlegarten.comipo import checked_target, download

    target = checked_target(args.kind, args.identifier)
    download(target, args.output, DATA, BOOKS, COMMAND)


def download_book(args):
    from kindlegarten.comipo import download, parse_target

    target = parse_target(args.book, args.source)
    if target:
        if args.region:
            raise KindleError("--region 只适用于 Amazon。")
        download(target, args.output, DATA, BOOKS, COMMAND)
        return

    asin, site_region = book_target(args.book)
    if site_region and args.region and site_region != args.region:
        raise KindleError("商品链接站点与 --region 不一致。")
    region = args.region or site_region or "jp"
    destination = args.output.expanduser().absolute() if args.output else None
    requested_format = destination.suffix.lower()[1:] if destination else "auto"
    if requested_format not in {"epub", "cbz"} and destination:
        raise KindleError("--output 需要指定 .epub 或 .cbz 文件。")
    if destination:
        require_new_output(destination)
    require_setup()
    account = account_path(region)
    if not account.is_file():
        raise KindleError(f"请先运行 {COMMAND} login --region {region}。")
    debug = calibre_executable("calibre-debug")

    with TemporaryDirectory(prefix=".job-", dir=DATA) as temporary:
        staging = Path(temporary)
        env = configure_calibre(RUNTIME, staging / "calibre")
        encrypted = staging / "download" / f"{asin}.kfx-zip"
        decrypted = staging / "decrypted.kfx-zip"
        print(f"正在请求 {region} 站 iPad 版 {asin}…", flush=True)
        run_worker("_download", account, asin, encrypted)
        print("正在去除 DRM 并转换文件…", flush=True)
        result = subprocess.run([debug, "-e", SCRIPT, "--", "_decrypt", account, encrypted, decrypted],
                                env=env, capture_output=True)
        if result.returncode or not decrypted.is_file():
            raise KindleError("DeDRM 无法解密该书。请确认账户有访问权限，或检查该加密版本是否受支持。")
        conversion = subprocess.run([debug, "-e", SCRIPT, "--", "_convert", decrypted, staging, requested_format],
                                    env=env, capture_output=True)
        messages = conversion.stdout + conversion.stderr
        if conversion.returncode:
            if b"CBZ_REQUIRES_IMAGE_PAGES" in messages:
                raise KindleError("这本书含文字或使用了无法安全导出为 CBZ 的版式。请指定 .epub 输出。")
            raise KindleError("KFX Input 无法转换这份 KFX。")
        converted = [staging / name for name in ("book.epub", "book.cbz") if (staging / name).is_file()]
        if len(converted) != 1:
            raise KindleError("KFX Input 没有生成一个有效的输出文件。")
        source = converted[0]
        if destination is None:
            title = json.loads((staging / "title.json").read_text(encoding="utf-8"))
            destination = BOOKS / book_filename(title, asin, source.suffix)
        require_new_output(destination)
        publish_book(source, destination)
        if b"Missing font family names" in messages:
            print("还有出版方字体无法映射，已保留 EPUB 的字体回退设置。", file=sys.stderr)
    print(f"已保存 {destination}")


def require_new_output(path):
    if path.exists() or path.is_symlink():
        raise KindleError(f"输出文件已存在：{path}。请用 --output 指定新文件。")


def hidden_input(prompt):
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return getpass.getpass(prompt)
        except getpass.GetPassWarning:
            raise KindleError("无法关闭终端回显。请通过交互式终端登录。") from None


def read_hidden_line(prompt):
    if os.name == "nt":
        return hidden_input(prompt).strip()

    # macOS canonical mode may truncate long redirects and cookie headers.
    import termios

    fd = sys.stdin.fileno()
    original = termios.tcgetattr(fd)
    settings = termios.tcgetattr(fd)
    settings[3] &= ~termios.ICANON
    settings[6][termios.VMIN] = 1
    settings[6][termios.VTIME] = 0
    termios.tcsetattr(fd, termios.TCSADRAIN, settings)
    try:
        return hidden_input(prompt).strip()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, original)


def is_amazon_callback(url, site):
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == "https"
                and parsed.hostname in {site, f"www.{site}"}
                and bool(parse_qs(parsed.query).get("openid.oa2.authorization_code")))
    except ValueError:
        return False


def login(region, state, method="default", cli_python=None):
    from kindl.auth import Account, AccountStore, new_device_identity, pkce_pair
    from kindl.client import AmazonDeviceClient
    from kindl.regions import get_region

    if method != "browser" and not sys.stdin.isatty():
        raise KindleError("请在交互式终端中运行登录命令。")
    site = get_region(region).site
    serial, pid = new_device_identity()
    verifier, challenge = pkce_pair()
    language = "ja_JP" if region == "jp" else "en_US"
    url = AmazonDeviceClient.login_url(serial, challenge, language=language, region=region)

    if method == "browser":
        result = subprocess.run(
            [cli_python, "-m", "kindlegarten.cli", "_login_browser", site],
            cwd=SCRIPT.parent.parent, input=url, stdout=subprocess.PIPE, text=True,
        )
        if result.returncode:
            raise SystemExit(result.returncode)
        callback = result.stdout.strip()
        if not is_amazon_callback(callback, site):
            raise KindleError("未收到有效的 Amazon 登录跳转。请重试或使用 --method callback。")
    else:
        if method == "callback" or not webbrowser.open(url):
            print(f"请在浏览器打开此登录地址：\n{url}", flush=True)
        print("登录完成后，复制浏览器地址栏里的完整跳转网址。", flush=True)
        prompt = "粘贴登录后的完整网址并按回车（输入隐藏）："
        callback = read_hidden_line(prompt)
        while not is_amazon_callback(callback, site):
            print(f"请重新复制 {site} 登录后地址栏里的完整网址。")
            callback = read_hidden_line(prompt)

    with AmazonDeviceClient(region=region) as client:
        credentials = client.register(callback, verifier, serial, pid)
    store = AccountStore()
    store.add(Account("ipad", credentials))
    store.save(state)
    print(f"已为 {site} 保存本机登录状态。")


def download(state, asin, destination):
    import httpx
    from kindl.auth import AccountStore
    from kindl.bundle import package_kfx_zip
    from kindl.client import AmazonDeviceClient

    store = AccountStore.load(state)
    account = store.get("ipad")
    if account is None:
        raise KindleError("本地登录状态不完整，请重新登录。")

    folder = destination.parent
    folder.mkdir(parents=True, exist_ok=True)
    with AmazonDeviceClient(account.credentials) as client:
        client.refresh_device_credentials()
        store.save(state)
        if not account.credentials.account_secrets:
            try:
                if client.restore_account_secrets():
                    store.save(state)
            except (httpx.HTTPError, OSError, RuntimeError):
                # Some vouchers can be decrypted with the device serial alone.
                pass
        with track_download(client):
            client.download(asin, folder, include_optional=True, destination=folder)
    package_kfx_zip(folder, destination)


def byte_label(amount):
    for unit in ("B", "KiB", "MiB", "GiB"):
        if amount < 1024 or unit == "GiB":
            return f"{amount:.1f} {unit}"
        amount /= 1024


class DownloadProgress:
    def __init__(self):
        self.tty = sys.stderr.isatty()
        self.started = None
        self.received = self.completed = self.width = 0
        self.current_size = self.current_received = 0

    def start(self, sizes):
        self.files = len(sizes)
        self.total = (sum(sizes) or None) if all(type(size) is int and size > 0 for size in sizes) else None
        self.started = self.updated = monotonic()
        if self.tty:
            self.show(self.started)

    def begin_resource(self, length):
        self.current_size = int(length) if length.isascii() and length.isdecimal() else 0

    def finish_resource(self):
        self.completed += 1
        self.current_size = self.current_received = 0

    def advance(self, size):
        self.received += size
        self.current_received += size
        now = monotonic()
        if self.tty and now - self.updated >= 0.2:
            self.show(now)

    def show(self, now, finished=None):
        self.updated = now
        speed = byte_label(self.received / max(now - self.started, 0.001))
        downloaded = byte_label(self.received)
        if self.total is not None:
            ratio = self.received / self.total
            downloaded += f"/{byte_label(self.received if finished else self.total)}"
        else:
            fraction = min(1, self.current_received / self.current_size) if self.current_size else 0
            ratio = (self.completed + fraction) / max(self.files, 1)
        percent = 100 if finished else min(99, int(ratio * 100))
        approximate = "~" if self.total is None and not finished else ""
        filled = percent * 16 // 100
        bar = "#" * filled + "-" * (16 - filled)
        status = "  完成" if finished else "  中断" if finished is False else ""
        line = f"下载 [{bar}] {approximate}{percent:3d}%  {downloaded}  {speed}/s{status}"
        if self.tty:
            print("\r" + line.ljust(self.width), end="\n" if finished is not None else "",
                  file=sys.stderr, flush=True)
            self.width = len(line)
        else:
            print(line, file=sys.stderr, flush=True)


@contextmanager
def track_download(client):
    from httpx import SyncByteStream

    progress = DownloadProgress()
    manifest = client.manifest
    stream = client.http.stream

    def prepare_progress(asin):
        payload, correlation = manifest(asin)
        sizes = [resource.get("size") for resource in payload.get("resources", [])
                 if (resource.get("endpoint") or {}).get("url") and resource.get("type") != "KINDLE_USER_ANOT"]
        progress.start(sizes)
        return payload, correlation

    class MeasuredStream(SyncByteStream):
        def __init__(self, wrapped):
            self.wrapped = wrapped

        def __iter__(self):
            for chunk in self.wrapped:
                progress.advance(len(chunk))
                yield chunk

        def close(self):
            self.wrapped.close()

    @contextmanager
    def measure(*args, **kwargs):
        with stream(*args, **kwargs) as response:
            if not response.is_success:
                yield response
                return
            progress.begin_resource(response.headers.get("Content-Length", ""))
            response.stream = MeasuredStream(response.stream)
            yield response
            progress.finish_resource()

    client.manifest = prepare_progress
    client.http.stream = measure
    finished = False
    try:
        yield
        finished = True
    finally:
        client.manifest = manifest
        client.http.stream = stream
        if progress.started is not None:
            progress.show(monotonic(), finished)


def decrypt(state, source, destination):
    from calibre.customize.ui import find_plugin
    from calibre_plugins.dedrm import ion
    from calibre_plugins.dedrm.prefs import DeDRM_Prefs

    account = next(item for item in json.loads(state.read_text())["accounts"] if item["name"] == "ipad")
    credentials = account["credentials"]
    serial = credentials["serial"]
    if not ion.is_serial_id(serial):
        raise KindleError("本地登录状态里的设备序列号无效，请重新登录。")
    tokens = list(dict.fromkeys(
        value
        for token in credentials.get("account_secrets", [])
        for value in (token, *ion.unwrap_secret_token(token, allow_plain=True))
    ))
    preferences = DeDRM_Prefs()
    preferences.set("serials", [serial])
    preferences.set("pids", [*tokens, *(serial + token for token in tokens)])
    preferences.writeprefs()

    plugin = find_plugin("DeDRM")
    if plugin is None:
        raise KindleError("临时 Calibre 配置未加载 DeDRM。")
    decrypted = Path(plugin.run(str(source)))
    shutil.copyfile(decrypted, destination)


def picture_pages(book):
    if not book.is_image_based_fixed_layout:
        return []
    resources, _, _ = book.get_ordered_image_resources()
    if book.has_pdf_resource:
        from calibre_plugins.kfx_input.kfxlib.resources import get_pdf_reader
        from calibre_plugins.kfx_input.kfxlib.yj_to_image_book import KFX_IMAGE_BOOK

        image_book = KFX_IMAGE_BOOK(book)
        try:
            for resource in resources:
                image = image_book.get_resource_image(resource)
                if image is None:
                    return []
                if image.format == "$565":
                    pdf = get_pdf_reader(image.raw_media, book.pdf_cache)
                    for number in image.page_nums:
                        page = pdf.pages[number - 1]
                        if len(page.images) != 1 or page.extract_text().strip():
                            return []
        except Exception:
            # Retain EPUB if the PDF page content cannot be verified as images alone.
            return []
    return resources


def convert_book(source, output_dir, requested_format):
    from calibre_plugins.kfx_input import get_symbol_catalog_filename
    from calibre_plugins.kfx_input.config import config_desaturate_notebooks
    from calibre_plugins.kfx_input.kfxlib import YJ_Book
    from calibre_plugins.kfx_input.kfxlib.yj_to_epub import KFX_EPUB

    class JapaneseEPUB(KFX_EPUB):
        def process_document_data(self):
            # process_fonts() has registered the book's embedded families by now.
            self.font_name_replacements.setdefault("ja", "serif")
            super().process_document_data()

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    book = YJ_Book(str(source), symbol_catalog_filename=get_symbol_catalog_filename())
    book.decode_book(retain_yj_locals=True)
    title = str(book.get_metadata_value("title") or "")
    (output_dir / "title.json").write_text(json.dumps(title), encoding="utf-8")

    pages = picture_pages(book) if requested_format != "epub" else []
    if requested_format == "cbz" and not pages:
        raise KindleError("CBZ_REQUIRES_IMAGE_PAGES")
    if pages:
        output = output_dir / "book.cbz"
        output.write_bytes(book.convert_to_cbz())
        with ZipFile(output) as archive:
            images = [name for name in archive.namelist()
                      if Path(name).suffix.lower() in {".jpg", ".jpeg", ".png", ".gif"}]
            if len(images) != len(pages) or archive.testzip() is not None:
                raise KindleError("CBZ 的图片页数与原书不符，或存档校验失败。")
        return

    output = output_dir / "book.epub"
    language = str(book.get_metadata_value("language") or "").lower().replace("_", "-").split("-")[0]
    if language == "ja":
        epub = JapaneseEPUB(book, desaturate_notebooks=config_desaturate_notebooks()).decompile_to_epub()
        book.final_actions()
    else:
        epub = book.convert_to_epub(desaturate_notebooks=config_desaturate_notebooks())
    output.write_bytes(epub)
    check_epub(output)


def check_epub(path):
    try:
        with ZipFile(path) as epub:
            if epub.read("mimetype") != b"application/epub+zip" or epub.testzip() is not None:
                raise KindleError("EPUB 校验失败。")
            container = ElementTree.fromstring(epub.read("META-INF/container.xml"))
            rootfile = container.find(".//{*}rootfile")
            if rootfile is None:
                raise KindleError("EPUB 缺少目录清单。")
            package = ElementTree.fromstring(epub.read(rootfile.attrib["full-path"]))
            if not package.findall(".//{*}spine/{*}itemref"):
                raise KindleError("EPUB 缺少正文阅读顺序。")
    except (BadZipFile, KeyError, ElementTree.ParseError):
        raise KindleError("生成的 EPUB 文件结构不完整。") from None


def worker(command, args):
    if command in {"_login", "_download"}:
        for key in list(os.environ):
            if key.startswith("KINDL_"):
                del os.environ[key]
        os.environ["KINDL_PROFILE"] = "ios-ipad"
    try:
        if command == "_login":
            region, state, *options = args
            login(region, Path(state), *options)
        elif command == "_login_browser":
            from kindlegarten.browser import open_amazon_login

            print(open_amazon_login(sys.stdin.read(), args[0], COMMAND))
        elif command == "_download":
            state, asin, destination = args
            download(Path(state), asin, Path(destination))
        elif command == "_decrypt":
            decrypt(*map(Path, args))
        else:
            source, output_dir, requested_format = args
            convert_book(Path(source), Path(output_dir), requested_format)
    except (KindleError, KeyboardInterrupt, EOFError):
        raise
    except Exception as error:
        response = getattr(error, "response", None)
        code = getattr(response, "status_code", None)
        detail = f"HTTP {code}" if code is not None else type(error).__name__
        raise KindleError(f"操作失败（{detail}）。请检查登录状态、网络及该书在对应站点的访问权限。") from None


def main():
    os.umask(0o077)
    if len(sys.argv) > 1 and sys.argv[1] in {"_login", "_login_browser", "_download", "_decrypt", "_convert"}:
        worker(sys.argv[1], sys.argv[2:])
        return
    if sys.version_info < (3, 11):
        raise KindleError("请使用 Python 3.11 或更新版。")

    from kindlegarten import __version__

    parser = argparse.ArgumentParser(prog=COMMAND, description="下载 Kindle 和 Comipo 书库中的作品，导出 EPUB 或 CBZ。")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(title="命令")
    commands.add_parser("setup", help="安装独立依赖").set_defaults(action=setup)
    for name, action, description in (("login", start_login, "登录 Amazon 或 Comipo"),
                                      ("logout", logout, "清除本地登录状态")):
        subparser = commands.add_parser(name, help=description)
        subparser.add_argument("--source", choices=("amazon", "comipo"), default="amazon", help="服务，默认 amazon")
        subparser.add_argument("--region", choices=("jp", "us"), default="jp", help="账户站点，默认 jp")
        if name == "login":
            subparser.add_argument(
                "--method", choices=("browser", "callback", "credentials", "cookie"),
                help="browser 打开浏览器，callback 粘贴 Amazon 跳转网址，"
                     "credentials 输入 Comipo 凭据，cookie 导入 Comipo 会话",
            )
            subparser.add_argument(
                "--session-file", type=Path, metavar="PATH", help="使用 Comipo cookie 模式导入会话 JSON 文件",
            )
        subparser.set_defaults(action=action)

    get = commands.add_parser("download", aliases=["d"], help="下载一本书或一个系列")
    get.add_argument("book", metavar="ID_OR_URL", help="Amazon ASIN、Comipo work:ID、series:ID 或相应链接")
    get.add_argument("--source", choices=("amazon", "comipo"), help="指定服务，省略时从 ID 或链接识别")
    get.add_argument("--region", choices=("jp", "us"), help="账户站点，纯 ASIN 默认 jp")
    get.add_argument("-o", "--output", type=Path, metavar="PATH",
                     help="单本指定 .epub 或 .cbz，Comipo 系列指定父目录，默认 books/")
    get.set_defaults(action=download_book)

    for kind, alias, description, output_help in (
        ("work", "w", "下载一本 Comipo 漫画", "指定 CBZ 文件，默认保存到 books/"),
        ("series", "s", "下载 Comipo 系列的已购漫画", "指定系列文件夹的上级目录，默认 books/"),
    ):
        subparser = commands.add_parser(kind, aliases=[alias], help=description)
        subparser.add_argument("identifier", metavar="ID", help=f"Comipo {kind} ID")
        subparser.add_argument("-o", "--output", type=Path, metavar="PATH", help=output_help)
        subparser.set_defaults(kind=kind, action=download_comipo)

    args = parser.parse_args()
    if not hasattr(args, "action"):
        parser.print_help()
        return

    with project_lock():
        for entry in DATA.iterdir():
            temporary_job = re.fullmatch(r"\.(?:setup|job)-[A-Za-z0-9_]{8}", entry.name)
            if temporary_job and entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
        args.action(args)


def entrypoint(checkout=None):
    global BOOKS, COMMAND, DATA, RUNTIME

    COMMAND = "python kindle.py" if checkout else "kindlegarten"
    data_override = os.environ.get("KINDLEGARTEN_DATA_DIR")
    default_data = checkout / ".kindle-data" if checkout else user_data_directory()
    DATA = Path(data_override).expanduser().resolve() if data_override else default_data
    RUNTIME = DATA / "runtime"
    BOOKS = (checkout or Path.cwd()) / "books"
    try:
        main()
    except KindleError as error:
        print(error, file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("已取消。", file=sys.stderr)
        return 130
    except subprocess.CalledProcessError:
        print("安装 Python 依赖失败，请检查 Python 安装及网络。", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"操作失败：{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.modules.setdefault("kindlegarten.cli", sys.modules[__name__])
    sys.exit(entrypoint())
