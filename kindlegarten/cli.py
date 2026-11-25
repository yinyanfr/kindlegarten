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
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from urllib.parse import parse_qs, urlsplit
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile


SCRIPT = Path(__file__).resolve()
if str(SCRIPT.parent.parent) not in sys.path:
    sys.path.insert(0, str(SCRIPT.parent.parent))
if socks_site := os.environ.get("KINDLEGARTEN_SOCKS_SITE"):
    sys.path.append(socks_site)

from kindlegarten.i18n import tr
from kindlegarten.marketplaces import MARKETPLACES, REGIONS_BY_SITE
from kindlegarten.network import amazon_client, http_client, worker_environment
from kindlegarten.progress import PROGRESS, report, report_book
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
SOURCES = ("amazon", "comipo", "dlsite")


class KindleError(Exception):
    pass


class SignInRequired(KindleError):
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
    raise KindleError(tr('{binary} was not found. Install Calibre or set KINDLE_CALIBRE_BIN to its executable directory.', binary=binary))


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
            raise KindleError(tr('Another Kindlegarten process is busy. Try again when it has finished.')) from None
        yield


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def fetch_verified(source, destination):
    url, digest = source
    try:
        with http_client(redirects=True) as client, client.stream("GET", url) as response:
            response.raise_for_status()
            with destination.open("xb") as output:
                for block in response.iter_bytes():
                    output.write(block)
    except Exception:
        raise KindleError(tr("Could not download a setup dependency.")) from None
    if sha256(destination) != digest:
        raise KindleError(tr('The dependency checksum did not match: {url}', url=url))


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
    env = worker_environment()
    env["CALIBRE_CONFIG_DIRECTORY"] = str(config_dir)
    config_dir.mkdir()
    for plugin in PLUGINS:
        result = subprocess.run([customize, "--add-plugin", plugin_dir / plugin],
                                env=env, capture_output=True)
        if result.returncode:
            raise KindleError(tr('Calibre could not load the {plugin} plugin.', plugin=plugin))
    return env


def setup_ready():
    plugins = all((RUNTIME / filename).is_file() for filename in PLUGINS)
    return plugins and (getattr(sys, "frozen", False) or (RUNTIME / VENV_PYTHON).is_file())


def setup_worker_runtime():
    if getattr(sys, "frozen", False) or (RUNTIME / VENV_PYTHON).is_file():
        return
    base_python = os.environ.get("KINDLE_PYTHON", sys.executable)
    with TemporaryDirectory(prefix=".setup-", dir=DATA) as temporary:
        work = Path(temporary)
        archive = work / "kindl.tar.gz"
        fetch_verified(KINDL_SOURCE, archive)
        subprocess.run([base_python, "-m", "venv", work / "venv"], check=True, env=worker_environment())
        subprocess.run([work / VENV_PYTHON, "-m", "pip", "install", "--disable-pip-version-check",
                        "--no-cache-dir", "--constraint", SCRIPT.with_name("constraints.txt"), archive],
                       check=True, env=worker_environment())
        RUNTIME.mkdir(mode=0o700, exist_ok=True)
        (work / "venv").rename(RUNTIME / "venv")


def setup(_args):
    calibre_executable("calibre-debug")
    calibre_executable("calibre-customize")
    if setup_ready():
        print(tr('Dependencies are already installed.'))
        return

    setup_worker_runtime()
    with TemporaryDirectory(prefix=".setup-", dir=DATA) as temporary:
        staging = Path(temporary) / "plugins"
        staging.mkdir()
        print(tr('Preparing kindl, DeDRM, and KFX Input…'), flush=True)

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
        RUNTIME.mkdir(mode=0o700, exist_ok=True)
        for filename in PLUGINS:
            (staging / filename).replace(RUNTIME / filename)
    print(tr('Dependencies are ready.'))


def require_setup():
    if not setup_ready():
        raise KindleError(tr('Run {COMMAND} setup first.', COMMAND=COMMAND))


def account_path(region):
    return DATA / f"account-{region}.json"


def book_target(value):
    if re.fullmatch(r"[A-Z0-9]{10}", value, re.IGNORECASE | re.ASCII):
        return value.upper(), None
    try:
        url = urlsplit(value)
        host = re.sub(r"^(www|smile)\.", "", url.hostname or "")
    except ValueError:
        raise KindleError(tr('Enter a 10-character ASIN or an Amazon product link.')) from None
    if url.scheme not in {"https", "http"} or host not in REGIONS_BY_SITE:
        raise KindleError(tr('Enter a 10-character ASIN or a product link from a supported Amazon store.'))
    match = re.search(r"/(?:dp|gp/product|gp/aw/d)/([A-Z0-9]{10})(?:/|$)",
                      url.path, re.IGNORECASE | re.ASCII)
    if not match:
        raise KindleError(tr('The product link does not contain an ASIN.'))
    return match[1].upper(), REGIONS_BY_SITE[host]


def book_filename(title, asin, extension):
    invalid = r'[<>:"/\\|?*\x00-\x1f\x7f-\x9f\ud800-\udfff]+'
    fallback = re.sub(invalid, "_", str(asin)).strip(" ._") or "book"
    stem = re.sub(invalid, "_", unicodedata.normalize("NFC", title))
    stem = re.sub(r"\s+", " ", stem).strip(" .")
    if not stem.strip(" _."):
        stem = fallback

    device = stem.split(".", 1)[0].rstrip().upper()
    if re.fullmatch(r"CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|(?:COM|LPT)[1-9¹²³]", device):
        stem = f"_{stem}"

    max_bytes = 255 - len(extension.encode("utf-8"))
    stem = stem.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore").rstrip(" .")
    return f"{stem or fallback}{extension}"


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
    if args.source != "amazon":
        from kindlegarten.comipo import COMIPO, DLSITE, import_session, login

        site = COMIPO if args.source == "comipo" else DLSITE
        if args.session_file is not None and args.method != "cookie":
            raise KindleError(tr("--session-file requires --method cookie with Comipo or DLsite."))
        if args.method == "callback":
            raise KindleError(tr("Use --method cookie or browser for this service."))
        if args.method == "credentials" and site is DLSITE:
            raise KindleError(tr("Use --method cookie or browser for this service."))
        if args.method == "cookie":
            if site is COMIPO:
                import_session(DATA, COMMAND, args.session_file)
            else:
                import_session(DATA, COMMAND, args.session_file, site=site)
        elif site is COMIPO:
            login(DATA, COMMAND, method=args.method or "browser")
        else:
            login(DATA, COMMAND, method="browser", site=site)
        return
    if args.session_file is not None:
        raise KindleError(tr("--session-file requires --method cookie with Comipo or DLsite."))
    if args.method in {"credentials", "cookie"}:
        raise KindleError(tr('Use --method browser or --method callback for Amazon.'))
    require_setup()
    state = account_path(args.region)
    run_worker("_login", args.region, state, args.method or "browser", sys.executable)


def logout(args):
    logout_source(args.source, args.region)


def logout_source(source, region):
    from kindlegarten.library import library_cache_path

    library_cache_path(DATA, source, region).unlink(missing_ok=True)
    if source != "amazon":
        from kindlegarten.comipo import COMIPO, DLSITE, session_path

        site = COMIPO if source == "comipo" else DLSITE
        session_path(DATA, site).unlink(missing_ok=True)
        shutil.rmtree(DATA / "browser" / source, ignore_errors=True)
        print(tr("Saved session cleared for {site}.", site=site.name))
        return
    state = account_path(region)
    state.unlink(missing_ok=True)
    state.with_suffix(".json.tmp").unlink(missing_ok=True)
    shutil.rmtree(DATA / "browser" / f"amazon-{region}", ignore_errors=True)
    print(tr("Saved Amazon session cleared for {region}.", region=region))


def run_worker(command, *args, capture=False, input_text=None):
    if getattr(sys, "frozen", False):
        # In a frozen app the Kindle dependency is bundled. Calibre still runs separately.
        if not capture and input_text is None:
            return worker(command, args)
        if command == "_library":
            from kindlegarten.library import amazon_library_json

            return amazon_library_json(Path(args[0]))
        if command == "_prepare_login":
            from kindlegarten.amazon import prepare_login

            return json.dumps(prepare_login(args[0]))
        if command == "_validate_login":
            from kindlegarten.amazon import saved_session_valid

            return json.dumps(saved_session_valid(Path(args[0])))
        if command == "_account":
            from kindlegarten.amazon import saved_account

            return json.dumps(saved_account(Path(args[0])))
        if command == "_register_login":
            from kindlegarten.amazon import register

            payload = json.loads(input_text)
            return register(Path(args[0]), payload["authorization"], payload["callback"])

    env = worker_environment()
    import importlib.util
    socks = importlib.util.find_spec("socksio")
    if socks and socks.origin:
        env["KINDLEGARTEN_SOCKS_SITE"] = str(Path(socks.origin).parent.parent)
    invocation = [RUNTIME / VENV_PYTHON, SCRIPT, command, *args]
    if command == "_download" and PROGRESS.get():
        env["KINDLEGARTEN_PROGRESS"] = "1"
        with subprocess.Popen(invocation, env=env, stdout=subprocess.PIPE, text=True, encoding="utf-8") as process:
            assert process.stdout is not None
            for line in process.stdout:
                try:
                    event = json.loads(line)
                    report(event["percent"], event["speed"])
                except (ValueError, KeyError, TypeError):
                    pass
            if process.wait():
                raise KindleError(tr("The Kindle download worker failed."))
        return None
    result = subprocess.run(invocation, env=env, input=input_text, text=True, encoding="utf-8",
                            stdout=subprocess.PIPE if capture else None)
    if result.returncode:
        raise KindleError(tr("The Kindle worker failed. Check the previous error message."))
    return result.stdout


def read_library(source, region="jp"):
    from kindlegarten.comipo import COMIPO, DLSITE, session
    from kindlegarten.library import by_purchase, from_json

    if source != "amazon":
        site = COMIPO if source == "comipo" else DLSITE
        with session(DATA, COMMAND, site) as client:
            return by_purchase(client, dlsite=site is DLSITE)
    account = account_path(region)
    if not account.is_file():
        raise SignInRequired(tr("Sign in to Amazon ({region}) before accessing the library.", region=region))
    if not getattr(sys, "frozen", False) and not (RUNTIME / VENV_PYTHON).is_file():
        raise KindleError(tr('Run {COMMAND} setup first.', COMMAND=COMMAND))
    payload = json.loads(run_worker("_library", account, capture=True))
    if payload is None:
        raise SignInRequired(tr("Sign in to Amazon ({region}) before accessing the library.", region=region))
    return from_json(payload)


def print_library(args):
    if args.region and args.source != "amazon":
        raise KindleError(tr('--region is only available for Amazon.'))
    snapshot = read_library(args.source, args.region or "jp")
    for series in snapshot.series:
        print(tr("{title} [{id}] · {count} purchased books", title=series.title, id=series.id, count=len(series.books)))
        for book in series.books:
            print(f"  {book.title} [{book.id}]")
    if snapshot.books:
        print(tr("Standalone books"))
        for book in snapshot.books:
            print(f"  {book.title} [{book.id}]")
    if not snapshot.books and not snapshot.series:
        print(tr("No purchased books found."))
    if not snapshot.complete:
        print(tr("The service did not return the complete library."), file=sys.stderr)


def download_selection(args):
    from kindlegarten.comipo import COMIPO, DLSITE, checked_target, download as play_download

    books = getattr(args, "books", BOOKS)
    if args.source == "amazon":
        if args.kind == "series":
            return download_amazon_series(args.identifier, args.region or "jp", args.output, args.format)
        asin, region = book_target(args.identifier)
        if args.region and region and args.region != region:
            raise KindleError(tr('The product link and --region specify different Amazon stores.'))
        region = args.region or region or "jp"
        if getattr(args, "resume", False):
            from kindlegarten.formats import already_saved

            if already_saved(DATA, books, f"amazon:{region}:{asin}", args.format):
                return None
        return download_amazon(asin, region, args.output, args.format, directory=books)
    if args.region:
        raise KindleError(tr('--region is only available for Amazon.'))
    site = DLSITE if args.source == "dlsite" else COMIPO
    target = checked_target(args.kind, args.identifier)
    return play_download(target, args.output, DATA, books, COMMAND, args.format, site=site,
                         resume=getattr(args, "resume", False))


def download_amazon_series(identifier, region, output, requested):
    from kindlegarten.formats import already_saved

    snapshot = read_library("amazon", region)
    series = next((entry for entry in snapshot.series if entry.id == identifier), None)
    if series is None:
        raise KindleError(tr("No purchased volumes were found for series {id}.", id=identifier))
    directory = (output or BOOKS).expanduser().absolute() / book_filename(series.title, identifier, "")
    failures = []
    for book in series.books:
        report_book(book.title)
        identity = f"amazon:{region}:{book.id}"
        if path := already_saved(DATA, directory, identity, requested):
            print(tr("Already saved: {path}", path=path), flush=True)
            continue
        try:
            download_amazon(book.id, region, None, requested, directory=directory)
        except (KindleError, OSError) as error:
            failures.append(book.id)
            print(f"{book.id}: {error}", file=sys.stderr)
    if failures:
        raise KindleError(tr("The following books could not be saved: {ids}.", ids=", ".join(failures)))


def download_book(args):
    from kindlegarten.comipo import COMIPO, DLSITE, download as play_download, parse_target

    target = parse_target(args.book, args.source)
    if target:
        if args.region:
            raise KindleError(tr('--region is only available for Amazon.'))
        host = urlsplit(args.book).hostname if args.book.startswith(("https://", "http://")) else None
        site = DLSITE if args.source == "dlsite" or host in {"play.dlsite.com", "www.dlsite.com"} else COMIPO
        return play_download(target, args.output, DATA, BOOKS, COMMAND, args.format, site=site)
    asin, site_region = book_target(args.book)
    if site_region and args.region and site_region != args.region:
        raise KindleError(tr('The product link and --region specify different Amazon stores.'))
    return download_amazon(asin, args.region or site_region or "jp", args.output, args.format)


def download_amazon(asin, region, output, requested="auto", *, directory=None):
    from kindlegarten.formats import convert, save_export, selected_format, unique_destination

    destination = output.expanduser().absolute() if output else None
    requested = selected_format(requested, destination)
    requested_format = {"zip": "cbz", "pdf": "auto"}.get(requested, requested)
    if destination:
        require_new_output(destination)
    require_setup()
    account = account_path(region)
    if not account.is_file():
        raise KindleError(tr('Run {COMMAND} login --region {region} first.', COMMAND=COMMAND, region=region))
    debug = calibre_executable("calibre-debug")
    script = (Path(sys._MEIPASS) / "kindlegarten" / "cli.py") if getattr(sys, "frozen", False) else SCRIPT

    with TemporaryDirectory(prefix=".job-", dir=DATA) as temporary:
        staging = Path(temporary)
        env = configure_calibre(RUNTIME, staging / "calibre")
        encrypted = staging / "download" / f"{asin}.kfx-zip"
        decrypted = staging / "decrypted.kfx-zip"
        print(tr('Requesting the {region} Kindle for iPad edition of {asin}…', region=region, asin=asin), flush=True)
        run_worker("_download", account, asin, encrypted)
        print(tr('Preparing the Kindle file for conversion…'), flush=True)
        result = subprocess.run([debug, "-e", script, "--", "_decrypt", account, encrypted, decrypted],
                                env=env, capture_output=True)
        if result.returncode or not decrypted.is_file():
            raise KindleError(tr('DeDRM could not process this book. Check access and format compatibility.'))
        conversion = subprocess.run([debug, "-e", script, "--", "_convert", decrypted, staging, requested_format],
                                    env=env, capture_output=True)
        messages = conversion.stdout + conversion.stderr
        if conversion.returncode:
            if b"CBZ_REQUIRES_IMAGE_PAGES" in messages:
                raise KindleError(tr('This book needs a text format. Select EPUB or PDF instead.'))
            raise KindleError(tr('KFX Input could not convert this file.'))
        converted = [staging / name for name in ("book.epub", "book.cbz") if (staging / name).is_file()]
        if len(converted) != 1:
            raise KindleError(tr('KFX Input did not produce a valid output file.'))
        title = json.loads((staging / "title.json").read_text(encoding="utf-8"))
        identity = f"amazon:{region}:{asin}"
        artifact = convert(converted[0], requested, title or asin, identity)
        if destination is None:
            destination = unique_destination(directory or BOOKS, title, asin, artifact.suffix)
        require_new_output(destination)
        save_export(artifact, destination, DATA, identity, requested)
        if b"Missing font family names" in messages:
            print(tr('Some publisher fonts could not be mapped. EPUB fallback fonts were retained.'), file=sys.stderr)
    print(tr('Saved {destination}', destination=destination))


def require_new_output(path):
    if path.exists() or path.is_symlink():
        raise KindleError(tr('The output already exists at {path}. Choose another file with --output.', path=path))


def hidden_input(prompt):
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return getpass.getpass(prompt)
        except getpass.GetPassWarning:
            raise KindleError(tr('Terminal echo could not be disabled. Sign in from an interactive terminal.')) from None


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


def login(region, state, method="browser", cli_python=None):
    from kindlegarten.amazon import prepare_login, register, saved_session_valid

    if state.is_file() and saved_session_valid(state):
        print(tr('Amazon {region} is already signed in. Run {COMMAND} logout --region {region} to switch accounts.', region=region, COMMAND=COMMAND))
        return
    if method != "browser" and not sys.stdin.isatty():
        raise KindleError(tr('Run the login command in an interactive terminal.'))
    authorization = prepare_login(region)
    site, url = authorization["site"], authorization["url"]

    if method == "browser":
        profile = state.parent / "browser" / f"amazon-{region}"
        result = subprocess.run(
            [cli_python, "-m", "kindlegarten.cli", "_login_browser", site, profile],
            cwd=SCRIPT.parent.parent, input=url, stdout=subprocess.PIPE, text=True,
        )
        if result.returncode:
            raise SystemExit(result.returncode)
        callback = result.stdout.strip()
        if not is_amazon_callback(callback, site):
            raise KindleError(tr('A valid Amazon redirect was not received. Try again or use --method callback.'))
    else:
        print(tr('Open this sign-in URL in a browser:\n{url}', url=url), flush=True)
        print(tr('After signing in, copy the full final URL from the browser address bar.'), flush=True)
        prompt = tr('Paste the final URL and press Enter (input hidden): ')
        callback = read_hidden_line(prompt)
        while not is_amazon_callback(callback, site):
            print(tr('Copy the full final URL from {site} again.', site=site))
            callback = read_hidden_line(prompt)

    register(state, authorization, callback)
    print(tr('Saved the local sign-in session for {site}.', site=site))


def download(state, asin, destination):
    import httpx
    from kindl.auth import AccountStore
    from kindl.bundle import package_kfx_zip
    store = AccountStore.load(state)
    account = store.get("ipad")
    if account is None:
        raise KindleError(tr('The saved session is incomplete. Sign in again.'))

    folder = destination.parent
    folder.mkdir(parents=True, exist_ok=True)
    with amazon_client(account.credentials) as client:
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
        if (self.tty or PROGRESS.get() or os.environ.get("KINDLEGARTEN_PROGRESS")) and now - self.updated >= 0.2:
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
        if report(percent, speed + "/s"):
            return
        approximate = "~" if self.total is None and not finished else ""
        filled = percent * 16 // 100
        bar = "#" * filled + "-" * (16 - filled)
        status = tr('  Done') if finished else tr('  Interrupted') if finished is False else ""
        line = tr('Downloading [{bar}] {approximate}{percent:3d}%  {downloaded}  {speed}/s{status}', bar=bar, approximate=approximate, percent=percent, downloaded=downloaded, speed=speed, status=status)
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
        raise KindleError(tr('The saved device serial is invalid. Sign in again.'))
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
        raise KindleError(tr('DeDRM was not loaded in the temporary Calibre profile.'))
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
                raise KindleError(tr('The CBZ page count did not match or the archive was invalid.'))
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
                raise KindleError(tr('The EPUB could not be validated.'))
            container = ElementTree.fromstring(epub.read("META-INF/container.xml"))
            rootfile = container.find(".//{*}rootfile")
            if rootfile is None:
                raise KindleError(tr('The EPUB package is missing.'))
            package = ElementTree.fromstring(epub.read(rootfile.attrib["full-path"]))
            if not package.findall(".//{*}spine/{*}itemref"):
                raise KindleError(tr('The EPUB spine is missing.'))
    except (BadZipFile, KeyError, ElementTree.ParseError):
        raise KindleError(tr('The generated EPUB is incomplete.')) from None


def worker(command, args):
    if command in {"_login", "_download", "_library", "_prepare_login", "_register_login", "_validate_login", "_account"}:
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

            site, profile = args
            print(open_amazon_login(sys.stdin.read(), site, COMMAND, profile=Path(profile)))
        elif command == "_download":
            state, asin, destination = args
            download(Path(state), asin, Path(destination))
        elif command == "_library":
            from kindlegarten.library import amazon_library_json

            print(amazon_library_json(Path(args[0])))
        elif command == "_prepare_login":
            from kindlegarten.amazon import prepare_login

            print(json.dumps(prepare_login(args[0])))
        elif command == "_validate_login":
            from kindlegarten.amazon import saved_session_valid

            print(json.dumps(saved_session_valid(Path(args[0]))))
        elif command == "_account":
            from kindlegarten.amazon import saved_account

            print(json.dumps(saved_account(Path(args[0]))))
        elif command == "_register_login":
            from kindlegarten.amazon import register

            payload = json.load(sys.stdin)
            register(Path(args[0]), payload["authorization"], payload["callback"])
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
        raise KindleError(tr('The operation failed ({detail}). Check your session, connection, and access to the book.', detail=detail)) from None


def main():
    os.umask(0o077)
    if len(sys.argv) > 1 and sys.argv[1] in {"_login", "_login_browser", "_download", "_decrypt", "_convert", "_library", "_prepare_login", "_register_login", "_validate_login", "_account"}:
        worker(sys.argv[1], sys.argv[2:])
        return
    if sys.version_info < (3, 11):
        raise KindleError(tr('Use Python 3.11 or later.'))

    from kindlegarten import __version__
    from kindlegarten.formats import FORMATS

    argparse._ = tr
    argparse.ngettext = lambda one, many, count: tr(one if count == 1 else many)
    parser = argparse.ArgumentParser(prog=COMMAND, description=tr('Back up purchased Kindle, Comipo, and DLsite books.'))
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(title=tr('Commands'))
    commands.add_parser("setup", help=tr('Install Kindle export dependencies.')).set_defaults(action=setup)
    for name, action, description in (("login", start_login, tr('Sign in to Amazon, Comipo, or DLsite.')),
                                      ("logout", logout, tr('Clear a saved local session.'))):
        subparser = commands.add_parser(name, help=description)
        subparser.add_argument("--source", choices=SOURCES, default="amazon", help=tr('Source (default: amazon).'))
        subparser.add_argument("--region", choices=MARKETPLACES, default="jp", help=tr('Amazon region (default: jp).'))
        if name == "login":
            subparser.add_argument(
                "--method", choices=("browser", "callback", "credentials", "cookie"),
                help=tr('browser opens a browser, callback pastes an Amazon redirect, credentials signs in to Comipo, and cookie imports a Play session.'),
            )
            subparser.add_argument(
                "--session-file", type=Path, metavar="PATH", help=tr('Import a Play session JSON file with cookie mode.'),
            )
        subparser.set_defaults(action=action)

    get = commands.add_parser("download", aliases=["d"], help=tr('Download a book or series.'))
    get.add_argument("book", metavar="ID_OR_URL", help=tr('Amazon ASIN, Play work:ID or series:ID, or a supported link.'))
    get.add_argument("--source", choices=SOURCES, help=tr('Source (auto-detected from the ID or link when omitted).'))
    get.add_argument("--region", choices=MARKETPLACES, help=tr('Amazon region (bare ASINs default to jp).'))
    get.add_argument("-o", "--output", type=Path, metavar="PATH",
                     help=tr('Output file for a single book or parent directory for a series.'))
    get.add_argument("--format", choices=FORMATS, default="auto", help=tr("Select the output format."))
    get.set_defaults(action=download_book)

    for kind, alias, description, output_help in (
        ("work", "w", tr('Download an individual book.'), tr('Output filename (default directory: books/).')),
        ("series", "s", tr('Download the purchased volumes of a series.'), tr('Parent directory for the series (default: books/).')),
    ):
        subparser = commands.add_parser(kind, aliases=[alias], help=description)
        subparser.add_argument("identifier", metavar="ID",
                               help=tr("Series ID.") if kind == "series" else tr("Book ID or ASIN."))
        subparser.add_argument("-o", "--output", type=Path, metavar="PATH", help=output_help)
        subparser.add_argument("--source", choices=SOURCES, default="comipo", help=tr("Select the book source."))
        subparser.add_argument("--region", choices=MARKETPLACES, help=tr("Select the Amazon region."))
        subparser.add_argument("--format", choices=FORMATS, default="auto", help=tr("Select the output format."))
        subparser.set_defaults(kind=kind, action=download_selection)

    library = commands.add_parser("library", help=tr("List purchased books and series."))
    library.add_argument("--source", choices=SOURCES, default="amazon", help=tr("Select the book source."))
    library.add_argument("--region", choices=MARKETPLACES, help=tr("Select the Amazon region."))
    library.set_defaults(action=print_library)

    help_parser = commands.add_parser("help", help=tr("Show command help."))
    help_parser.add_argument("command", nargs="?", choices=tuple(commands.choices), help=tr("Command or alias."))
    args = parser.parse_args()
    if "command" in vars(args):
        (commands.choices[args.command] if args.command else parser).print_help()
        return
    if not hasattr(args, "action"):
        parser.print_help()
        return

    with project_lock():
        for entry in DATA.iterdir():
            temporary_job = re.fullmatch(r"\.(?:setup|job)-[A-Za-z0-9_]{8}", entry.name)
            if temporary_job and entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
        args.action(args)


def initialize(checkout=None):
    global BOOKS, COMMAND, DATA, RUNTIME

    COMMAND = "python kindle.py" if checkout else "kindlegarten"
    data_override = os.environ.get("KINDLEGARTEN_DATA_DIR")
    default_data = checkout / ".kindle-data" if checkout else user_data_directory()
    DATA = Path(data_override).expanduser().resolve() if data_override else default_data
    RUNTIME = DATA / "runtime"
    BOOKS = (checkout or Path.cwd()) / "books"


def entrypoint(checkout=None):
    initialize(checkout)
    try:
        main()
    except KindleError as error:
        print(error, file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print(tr('Cancelled.'), file=sys.stderr)
        return 130
    except subprocess.CalledProcessError:
        print(tr('Python dependencies could not be installed. Check Python and the connection.'), file=sys.stderr)
        return 1
    except OSError as error:
        print(tr('The operation failed: {error}', error=error), file=sys.stderr)
        return 1
    except ValueError:
        print(tr("The service returned an unrecognized book response."), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.modules.setdefault("kindlegarten.cli", sys.modules[__name__])
    sys.exit(entrypoint())
