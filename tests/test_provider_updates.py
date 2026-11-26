import contextlib
from hashlib import sha256
import json
from pathlib import Path
import stat
from tempfile import TemporaryDirectory
from threading import Event
from unittest import TestCase
from unittest.mock import Mock, patch
from zipfile import ZipFile, ZipInfo

import httpx

from kindlegarten import cli, comipo, play_library, update_install, updates
from kindlegarten.library import Book, Library, Series
from kindlegarten.network import proxy_address, using_proxy
from kindlegarten.progress import DownloadCancelled


class UnifiedProviderTest(TestCase):
    def test_aliases_and_old_product_links_route_to_dlsite(self):
        with TemporaryDirectory() as temporary, patch.dict("os.environ", {"KINDLEGARTEN_DATA_DIR": temporary}):
            commands = (
                (["download", "https://www.comipo.app/product/BJ123456", "--source", "comipo"], ("work", "BJ123456")),
                (["w", "BJ123456"], ("work", "BJ123456")),
                (["s", "TITLE1234", "--source", "comipo"], ("series", "TITLE1234")),
            )
            for args, target in commands:
                with self.subTest(args=args), patch("sys.argv", ["kindlegarten", *args]), patch.object(comipo, "download") as download:
                    self.assertEqual(cli.entrypoint(), 0)
                    self.assertEqual(download.call_args.args[0], target)
                    self.assertIs(download.call_args.kwargs["site"], comipo.DLSITE)

    def test_play_merge_uses_primary_ids_and_refuses_other_accounts(self):
        primary = Library((Series("TITLE1", "Main", (Book("BJ123456", "Main title"),)),), ())
        old = Library((Series("TITLE1", "Old", (Book("BJ123456", "Old title"), Book("BJ123457", "Missing"))),), ())
        with patch.object(play_library, "session", side_effect=lambda *args: contextlib.nullcontext(args[-1])),                 patch.object(play_library, "by_purchase", side_effect=lambda site, **_: primary if site is comipo.DLSITE else old),                 patch.object(play_library, "saved_account", side_effect=[{"login": "reader@example.test"}] * 2):
            merged = play_library.read_play_library(Path("profile"), "kindlegarten", legacy=True)
            self.assertEqual([(book.title, book.legacy) for book in merged.series[0].books],
                             [("Main title", False), ("Missing", True)])
        with patch.object(play_library, "session", return_value=contextlib.nullcontext(comipo.DLSITE)) as open_session,                 patch.object(play_library, "by_purchase", return_value=primary),                 patch.object(play_library, "saved_account", side_effect=[{"login": "first"}, {"login": "second"}]):
            with self.assertRaises(cli.KindleError):
                play_library.read_play_library(Path("profile"), "kindlegarten", legacy=True)
            self.assertEqual(open_session.call_count, 1)

    def test_old_cookie_is_never_imported_into_dlsite(self):
        with self.assertRaisesRegex(cli.KindleError, "old Comipo Cookie"):
            comipo.imported_storage("comipoplay_session=old; XSRF-TOKEN=x", comipo.DLSITE)

    def test_dlsite_logout_clears_both_routes(self):
        with TemporaryDirectory() as temporary, patch.object(cli, "DATA", Path(temporary), create=True):
            for site in (comipo.COMIPO, comipo.DLSITE):
                comipo.session_path(cli.DATA, site).write_text("{}")
            with contextlib.redirect_stdout(None):
                cli.logout_source("comipo", "jp")
            for site in (comipo.COMIPO, comipo.DLSITE):
                self.assertFalse(comipo.session_path(cli.DATA, site).exists())


class UpdateDownloadTest(TestCase):
    def test_check_and_download_use_the_same_proxy_policy(self):
        binary = b"fixture package"
        digest = sha256(binary).hexdigest()
        name = "kindlegarten-9.0.0-linux-x86_64.AppImage"
        addresses = []

        def response(request):
            if request.url.host == "api.github.com":
                return httpx.Response(200, json={"tag_name": "v9.0.0", "assets": [
                    {"name": name, "size": len(binary), "digest": "sha256:" + digest}]})
            if request.url.host == "github.com":
                return httpx.Response(302, headers={"Location": "https://release-assets.githubusercontent.com/file"})
            return httpx.Response(200, content=binary)

        def client():
            addresses.append(proxy_address())
            return httpx.Client(transport=httpx.MockTransport(response))

        with TemporaryDirectory() as temporary, using_proxy("http://127.0.0.1:8099"),                 patch.object(updates, "http_client", side_effect=client),                 patch.object(updates, "platform_suffix", return_value="-linux-x86_64.AppImage"):
            release = updates.check_for_update()
            saved = updates.download_update(release, Path(temporary), Event(), Mock())
            self.assertEqual(saved.name, name)
            self.assertEqual(saved.read_bytes(), binary)
        self.assertEqual(addresses, ["http://127.0.0.1:8099"] * 2)

    def test_bad_payload_cancellation_and_foreign_redirect_leave_no_file(self):
        release = updates.Release("v9.0.0", "https://github.com", "kindlegarten-9.0.0-linux-x86_64.AppImage",
                                  10, "0" * 64)
        scenarios = (
            (httpx.Response(200, content=b"wrong data"), False, cli.KindleError),
            (httpx.Response(200, content=b"wrong data"), True, DownloadCancelled),
            (httpx.Response(302, headers={"Location": "https://example.invalid/a"}), False, cli.KindleError),
        )
        for response, cancel, error in scenarios:
            with self.subTest(response=response.status_code, cancel=cancel), TemporaryDirectory() as temporary:
                root = Path(temporary)
                stop = Event()
                if cancel:
                    stop.set()
                client = httpx.Client(transport=httpx.MockTransport(lambda _: response))
                with patch.object(updates, "http_client", return_value=client), self.assertRaises(error):
                    updates.download_update(release, root, stop, Mock())
                self.assertEqual(list(root.iterdir()), [])


class InstallationTest(TestCase):
    def test_rejects_paths_and_symlinks_outside_zip_root(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "update.zip"
            for path in ("App/../../outside", "App/escape"):
                with ZipFile(archive, "w") as package:
                    item = ZipInfo(path)
                    if "escape" in path:
                        item.create_system = 3
                        item.external_attr = (stat.S_IFLNK | 0o777) << 16
                    package.writestr(item, "../../outside")
                with self.assertRaises(ValueError):
                    update_install.safe_unpack(archive, root / "contents", "App", symlinks=True)
                self.assertFalse((root / "outside").exists())

    def test_installer_replaces_or_restores_after_launch_failure(self):
        for boots in (True, False):
            with self.subTest(boots=boots), TemporaryDirectory() as temporary:
                root = Path(temporary)
                old = root / "Kindlegarten.AppImage"
                old.write_text("previous")
                stage = root / "stage"
                stage.mkdir()
                new = stage / "update"
                new.write_text("next")
                notifications = []

                def launch(arguments, *, cwd, env):
                    notifications.append(env)
                    child = Mock()
                    child.poll.return_value = 1
                    if boots and update_install.READY_VARIABLE in env:
                        Path(env[update_install.READY_VARIABLE]).write_text("9.0.0")
                    return child

                with patch.object(update_install.subprocess, "Popen", side_effect=launch):
                    result = update_install.replace_and_restart(update_install.Installation("linux", old), new, "9.0.0", stage)
                self.assertEqual(result, boots)
                self.assertEqual(old.read_text(), "next" if boots else "previous")
                self.assertEqual(len(notifications), 1 if boots else 2)
                if not boots:
                    self.assertEqual(notifications[-1][update_install.RESULT_VARIABLE], "restored")

    def test_portable_zip_is_staged_and_protected_when_not_writable(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = root / "Portable"
            folder.mkdir()
            (folder / update_install.PORTABLE_MARKER).touch()
            (folder / "Kindlegarten.exe").touch()
            (folder / "_internal").mkdir()
            helper = root / "helper"
            helper.write_bytes(b"helper")
            bundle = root / "update.zip"
            with ZipFile(bundle, "w") as package:
                package.writestr("Kindlegarten/Kindlegarten.exe", "updated")
                package.writestr("Kindlegarten/" + update_install.PORTABLE_MARKER, "")
            release = updates.Release("v9.0.0", "", "update.zip", bundle.stat().st_size, sha256(bundle.read_bytes()).hexdigest())
            installed = update_install.Installation("windows", folder)
            with patch.object(update_install, "updater_executable", return_value=helper):
                job = update_install.stage_update(bundle, installed, release)
                self.assertEqual(json.loads(job.read_text())["target"], str(folder))
                self.assertTrue((job.parent / "Kindlegarten" / "Kindlegarten.exe").is_file())
                with patch.object(update_install.os, "access", return_value=False), self.assertRaises(update_install.ManualInstall):
                    update_install.stage_update(bundle, installed, release)
