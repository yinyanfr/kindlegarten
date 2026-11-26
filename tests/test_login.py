import io
import sys
import unittest
from contextlib import nullcontext, redirect_stderr
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from kindlegarten import cli, comipo


class LoginOptionsTest(unittest.TestCase):
    def test_comipo_login_options(self):
        with TemporaryDirectory() as temporary:
            data = Path(temporary).resolve()
            for args, method in (
                ([], "browser"),
                (["--method", "browser"], "browser"),
                (["--method", "credentials"], "credentials"),
            ):
                with (
                    self.subTest(method=method, args=args),
                    patch.dict("os.environ", {"KINDLEGARTEN_DATA_DIR": temporary}),
                    patch("sys.argv", ["kindlegarten", "login", "--source", "comipo", *args]),
                    patch.object(comipo, "login") as login,
                ):
                    self.assertEqual(cli.entrypoint(), 0)
                    login.assert_called_once_with(data, "kindlegarten", method=method, site=comipo.DLSITE)

    def test_amazon_browser_dispatches_through_the_kindle_worker(self):
        for options in ([], ["--method", "browser"]):
            with (
                self.subTest(options=options),
                TemporaryDirectory() as temporary,
                patch.dict("os.environ", {"KINDLEGARTEN_DATA_DIR": temporary}),
                patch("sys.argv", ["kindlegarten", "login", "--region", "us", *options]),
                patch.object(cli, "setup_worker_runtime"),
                patch.object(cli, "run_worker") as worker,
            ):
                self.assertEqual(cli.entrypoint(), 0)
                worker.assert_called_once_with(
                    "_login", "us", Path(temporary).resolve() / "account-us.json", "browser", sys.executable
                )

    def test_method_mismatch(self):
        for source, method, message in (
            ("amazon", "credentials", "--method browser"),
            ("amazon", "cookie", "--method browser"),
            ("comipo", "callback", "--method cookie"),
        ):
            with (
                self.subTest(source=source),
                patch("sys.argv", ["kindlegarten", "login", "--source", source, "--method", method]),
                redirect_stderr(io.StringIO()) as stderr,
                TemporaryDirectory() as temporary,
                patch.dict("os.environ", {"KINDLEGARTEN_DATA_DIR": temporary}),
            ):
                self.assertEqual(cli.entrypoint(), 1)
                self.assertIn(message, stderr.getvalue())

    def test_comipo_session_file_dispatch(self):
        with (
            TemporaryDirectory() as temporary,
            patch.dict("os.environ", {"KINDLEGARTEN_DATA_DIR": temporary}),
            patch("sys.argv", ["kindlegarten", "login", "--source", "comipo", "--method", "cookie",
                               "--session-file", "my-session.json"]),
            patch.object(comipo, "import_session") as import_session,
        ):
            self.assertEqual(cli.entrypoint(), 0)
            import_session.assert_called_once_with(Path(temporary).resolve(), "kindlegarten", Path("my-session.json"),
                                                   site=comipo.DLSITE)

    def test_callback_origin_and_code(self):
        query = "?openid.oa2.authorization_code=test-code"
        self.assertTrue(cli.is_amazon_callback("https://www.amazon.co.jp/" + query, "amazon.co.jp"))
        self.assertFalse(cli.is_amazon_callback("https://amazon.co.jp.example.org/" + query, "amazon.co.jp"))
        self.assertFalse(cli.is_amazon_callback("http://amazon.co.jp/" + query, "amazon.co.jp"))
        self.assertFalse(cli.is_amazon_callback("https://amazon.co.jp/", "amazon.co.jp"))


class ComipoCookieTest(unittest.TestCase):
    def test_header_scopes_session_cookies(self):
        storage = comipo.imported_storage(
            "Cookie: comipoplay_session=test=value; _comipo_SID=shared; XSRF-TOKEN=token; unrelated=ignore"
        )
        self.assertEqual(
            [(item["name"], item["value"], item["domain"]) for item in storage["cookies"]],
            [
                ("comipoplay_session", "test=value", "play.comipo.app"),
                ("_comipo_SID", "shared", ".comipo.app"),
                ("XSRF-TOKEN", "token", "play.comipo.app"),
            ],
        )

    def test_url_or_unrelated_cookie_is_rejected(self):
        for value in ("https://play.comipo.app/", "another_service=secret"):
            with self.subTest(value=value), self.assertRaises(comipo.KindleError):
                comipo.imported_storage(value)

    def test_expired_session_preserves_existing_session(self):
        request = Mock()
        playwright = Mock()
        playwright.request.new_context.return_value = request
        with (
            TemporaryDirectory() as temporary,
            patch.object(comipo.sys.stdin, "isatty", return_value=True),
            patch.object(comipo, "read_hidden_line", return_value="comipoplay_session=expired"),
            patch.object(comipo, "playwright_session", return_value=nullcontext((playwright, None))),
            patch.object(comipo.ComipoClient, "authorized", return_value=False),
        ):
            data = Path(temporary)
            state = comipo.session_path(data)
            state.write_text("existing", encoding="utf-8")
            with self.assertRaises(comipo.KindleError):
                comipo.import_session(data, "kindlegarten", None)
            self.assertEqual(state.read_text(encoding="utf-8"), "existing")
            request.dispose.assert_called_once()


if __name__ == "__main__":
    unittest.main()
