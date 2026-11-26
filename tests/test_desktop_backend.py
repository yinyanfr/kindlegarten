import io
import os
import unittest
from contextlib import redirect_stdout
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Event, Lock
from unittest.mock import patch
from zipfile import ZipFile

from PIL import Image
from pikepdf import Pdf

from kindlegarten import cli, comipo, updates
from kindlegarten.desktop_download import DownloadRequest, run_download, series_directory
from kindlegarten.formats import already_saved, convert, publish_export, save_export, selected_format
from kindlegarten.i18n import LANGUAGE
from kindlegarten.library import (
    Book, Library, Series, amazon_library, by_purchase, cache_library, cached_library, purchase_timestamp,
    series_for_book, store_page, task_entries,
)
from kindlegarten.local_downloads import scan_downloads
from kindlegarten.network import browser_proxy, using_proxy
from kindlegarten.progress import DownloadCancelled, download_session


class AmazonLibraryTest(unittest.TestCase):
    def test_real_cursor_fields_and_purchased_series_members(self):
        def book(identifier, kind, origin):
            return f"""<meta_data><ASIN>{identifier}</ASIN><title>{identifier}</title>
              <purchase_date>2024-01-0{identifier[-1]}T10:00:00+0000</purchase_date>
              <cde_contenttype>{kind}</cde_contenttype><origins><origin>
              <type>{origin}</type></origin></origins></meta_data>"""

        first = f"""<response><has_more>true</has_more><sync_time>cursor-1</sync_time>
          <add_update_list>{book("B01", "EBOK", "Purchase")}{book("B03", "EBOK", "Loan")}</add_update_list>
          <series_add_update_list><series_meta_data><seriesId>s-1</seriesId><displayName>A series</displayName>
          <seriesItems>
          <seriesItem><itemAsin>B03</itemAsin><position>3</position></seriesItem>
          <seriesItem><itemAsin>B02</itemAsin><position>2</position></seriesItem>
          <seriesItem><itemAsin>B01</itemAsin><position>1</position></seriesItem>
          </seriesItems></series_meta_data></series_add_update_list></response>"""
        last = f"""<response><has_more>false</has_more><add_update_list>
          {book("B02", "EBOK", "Purchase")}{book("B04", "PDOC", "Purchase")}
          {book("B05", "EBOK", "Purchase")}</add_update_list></response>"""
        snapshot = amazon_library({"": first, "cursor-1": last}.__getitem__)
        self.assertTrue(snapshot.complete)
        self.assertEqual(snapshot.series[0].id, "s-1")
        self.assertEqual([item.id for item in snapshot.series[0].books], ["B01", "B02"])
        self.assertEqual(snapshot.series[0].purchased_at, purchase_timestamp("2024-01-02T10:00:00Z"))
        self.assertEqual([item.id for item in snapshot.books], ["B05"])

    def test_stationary_cursor_marks_results_as_partial(self):
        document = "<response><has_more>true</has_more><sync_time>same</sync_time></response>"
        self.assertFalse(amazon_library(lambda _: document).complete)


class LibraryCacheTest(unittest.TestCase):
    def test_play_uses_the_purchase_record_instead_of_work_metadata(self):
        work = {"workno": "BJ123456", "work_type": "MNG", "name": "Book"}
        purchases = [
            {"workno": work["workno"], "sales_date": "2024-11-02T14:30:00Z"},
            {"workno": work["workno"], "sales_date": "2023-10-02T14:30:00Z"},
        ]
        for site in (comipo.COMIPO, comipo.DLSITE):
            client = comipo.ComipoClient(None, site)
            with patch.object(client, "api", side_effect=[purchases, {"works": [work]}]):
                snapshot = by_purchase(client, dlsite=site is comipo.DLSITE)
            self.assertEqual(snapshot.books[0].purchased_at, purchase_timestamp(purchases[0]["sales_date"]))

    def test_logout_only_removes_the_matching_market_cache(self):
        with TemporaryDirectory() as temporary, patch.object(cli, "DATA", Path(temporary), create=True):
            older = Book("B01", "An earlier book")
            newer = Book("B02", "A later book", purchased_at=123.0)
            snapshot = Library((Series("S01", "Series", (older, newer)),), (Book("B03", "Standalone"),))
            for region in ("de", "uk"):
                cache_library(cli.DATA, "amazon", region, snapshot)
            self.assertEqual(cached_library(cli.DATA, "amazon", "de"), snapshot)
            with redirect_stdout(io.StringIO()):
                cli.logout_source("amazon", "de")
            self.assertIsNone(cached_library(cli.DATA, "amazon", "de"))
            self.assertEqual(cached_library(cli.DATA, "amazon", "uk"), snapshot)


class DownloadQueueBackendTest(unittest.TestCase):
    def test_parallel_tasks_copy_account_and_publish_distinct_paths(self):
        with TemporaryDirectory() as temporary, patch.object(cli, "DATA", Path(temporary), create=True):
            source = cli.account_path("jp")
            source.write_text("original")
            folder = cli.DATA / "books"
            ready = Barrier(2)
            gate = Lock()
            entries = (Book("B01", "Same title"), Book("B02", "Same title"))

            def transfer(args):
                isolated = download_session(source)
                self.assertNotEqual(isolated, source)
                self.assertEqual(isolated.read_text(), "original")
                isolated.write_text(args.identifier)
                ready.wait(timeout=10)
                artifact = isolated.with_name("book.epub")
                artifact.write_text(args.identifier)
                publish_export(artifact, None, args.books, "Same title", args.identifier, cli.DATA,
                               args.identifier, "epub")

            with (
                patch.object(cli, "setup_ready", return_value=True),
                patch.object(cli, "download_selection", side_effect=transfer),
                ThreadPoolExecutor(max_workers=2) as pool,
            ):
                jobs = [pool.submit(run_download, DownloadRequest("amazon", "jp", book, folder, "epub"),
                                    Event(), gate) for book in entries]
                self.assertEqual([job.result(timeout=12) for job in jobs], [folder, folder])
            self.assertEqual(source.read_text(), "original")
            self.assertEqual({file.read_text() for file in folder.glob("*.epub")}, {"B01", "B02"})
            self.assertEqual(list((cli.DATA / "downloads").iterdir()), [])

    def test_cancelled_task_removes_staging_and_skips_publication(self):
        with TemporaryDirectory() as temporary, patch.object(cli, "DATA", Path(temporary), create=True):
            comipo.session_path(cli.DATA).write_text("{}")
            request = DownloadRequest("comipo", "jp", Book("BJ123456", "Book"), cli.DATA / "books", "epub")
            cancelled = Event()

            def transfer(args):
                source = download_session(comipo.session_path(cli.DATA)).with_name("staged.epub")
                source.write_bytes(b"partial")
                cancelled.set()
                cli.publish_book(source, args.books / "Book.epub")

            with patch.object(cli, "download_selection", side_effect=transfer), self.assertRaises(DownloadCancelled):
                run_download(request, cancelled, Lock())
            self.assertFalse(request.folder.exists())
            self.assertEqual(list((cli.DATA / "downloads").iterdir()), [])


class ExportTest(unittest.TestCase):
    def test_formats_preserve_comic_archive_and_page_order(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "book.cbz"
            with ZipFile(source, "w") as archive:
                for number, color in enumerate(("red", "blue")):
                    buffer = io.BytesIO()
                    Image.new("RGB", (16, 24 + number), color).save(buffer, "JPEG")
                    archive.writestr(f"{number}.jpg", buffer.getvalue())
            original = source.read_bytes()
            epub = convert(source, "epub", "Example", "example")
            with ZipFile(epub) as ebook, ZipFile(source) as comic:
                self.assertEqual(ebook.read("images/1.jpg"), comic.read("0.jpg"))
                self.assertEqual(ebook.read("images/2.jpg"), comic.read("1.jpg"))
            pdf = convert(source, "pdf", "Example", "example")
            with Pdf.open(pdf) as document:
                self.assertEqual(len(document.pages), 2)
            zip_file = convert(source, "zip", "Example", "example")
            self.assertEqual(zip_file.read_bytes(), original)
            with self.assertRaises(cli.KindleError):
                convert(epub, "zip", "Example", "example")

    def test_extension_conflict_and_resume_record(self):
        self.assertEqual(selected_format("auto", Path("book.pdf")), "pdf")
        with self.assertRaises(cli.KindleError):
            selected_format("cbz", Path("book.zip"))
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = root / "verified.pdf"
            payload.write_bytes(b"validated content")
            folder = root / "books"
            output = folder / "title.pdf"
            save_export(payload, output, root, "source:book", "pdf")
            self.assertEqual(already_saved(root, folder, "source:book", "pdf"), output)
            self.assertIsNone(already_saved(root, folder, "source:book", "epub"))
            output.unlink()
            self.assertIsNone(already_saved(root, folder, "source:book", "pdf"))


class ShelfDownloadsTest(unittest.TestCase):
    def test_badges_require_series_files_and_unique_names(self):
        one = Book("BJ123456", "One")
        two = Book("BJ123457", "Two")
        series = Series("TITLE1", "Name / Series", (one, two))
        solo = Book("BJ123458", "Standalone")
        same_a = Book("BJ123459", "Same")
        same_b = Book("BJ123460", "Same")
        library = Library((series,), (solo, same_a, same_b))
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = root / "books"
            directory = series_directory(folder, series)
            directory.mkdir(parents=True)
            self.assertEqual(DownloadRequest("comipo", "jp", one, folder, "auto", series=series).directory, directory)
            self.assertEqual(DownloadRequest("comipo", "jp", series, folder, "auto").directory, directory)

            def keys():
                return scan_downloads(root, folder, library, "comipo", "jp")[0]

            pdf = b"%PDF-1.4\n%%EOF\n"
            (folder / "One.pdf").write_bytes(pdf)
            (folder / "Standalone.pdf").write_bytes(pdf)
            (folder / "Same.pdf").write_bytes(pdf)
            (directory / "One.pdf").write_bytes(b"incomplete")
            self.assertEqual(keys(), {(None, solo.id)})
            with ZipFile(directory / "Moved.cbz", "w") as archive:
                archive.comment = b"Comipo work:BJ123456"
                archive.writestr("page.jpg", b"page")
            self.assertEqual(keys(), {(None, solo.id), (series.id, one.id)})
            (directory / "One.pdf").write_bytes(pdf)
            (directory / "Two.pdf").write_bytes(pdf)
            self.assertEqual(keys(), {(None, solo.id), (series.id, one.id), (series.id, two.id)})
            (directory / "Two.pdf").unlink()
            self.assertEqual(keys(), {(None, solo.id), (series.id, one.id)})

    def test_manual_id_order_and_ambiguous_targets(self):
        first = Book("B012345678", "A title")
        group = Series(first.id, "Same ID", (first,))
        another = Series("A012345678", "Another series", (first,), store_url="https://www.amazon.co.jp/dp/A012345678")
        library = Library((group, another), ())
        drafts = ["B012345678", "work:B012345678", "work:b012345678", "series:B012345678", "B000000000",
                  "https://evil.test"]
        entries, errors = task_entries(list(enumerate(drafts, 1)), library, source="amazon")
        self.assertEqual(entries, [first, group])
        self.assertEqual([number for number, _, _ in errors], [1, 5, 6])
        self.assertEqual(series_for_book(library, first.id), another)
        self.assertEqual(store_page(group, "amazon", "jp"), "https://www.amazon.co.jp/dp/B012345678")
        self.assertEqual(store_page(another, "amazon", "jp"), another.store_url)
        self.assertEqual(store_page(another, "amazon", "uk"), "https://www.amazon.co.uk/dp/B012345678")


class CommandsTest(unittest.TestCase):
    def test_download_infers_an_additional_store_from_product_url(self):
        with (
            TemporaryDirectory() as temporary,
            patch.dict(os.environ, {"KINDLEGARTEN_DATA_DIR": temporary}),
            patch("sys.argv", ["kindlegarten", "download", "https://www.amazon.co.uk/dp/B012345678"]),
            patch.object(cli, "download_amazon") as download,
        ):
            self.assertEqual(cli.entrypoint(), 0)
            download.assert_called_once_with("B012345678", "uk", None, "auto", dedrm=True)

    def test_no_dedrm_saves_the_original_archive_without_calibre(self):
        for command in ("d", "w", "s"):
            with self.subTest(command=command), TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                target = root / ("books" if command == "s" else "backup.kfx-zip")
                volume = Book("B012345678", "A title")
                series = Series("Group", "A series", (volume,))
                payload = b"original encrypted bytes"

                def raw_worker(action, account, asin, archive, mode):
                    self.assertEqual((action, account, asin, mode),
                                     ("_download", root / "account-jp.json", volume.id, "original"))
                    with ZipFile(archive, "w") as bundle:
                        bundle.writestr("book.azw8", payload)

                (root / "account-jp.json").touch()
                identifier = series.id if command == "s" else volume.id
                args = [command, identifier, "--source", "amazon", "--no-dedrm", "--output", str(target)]
                with (
                    patch.dict(os.environ, {"KINDLEGARTEN_DATA_DIR": temporary}),
                    patch("sys.argv", ["kindlegarten", *args]),
                    patch.object(cli, "setup_worker_runtime"),
                    patch.object(cli, "read_library", return_value=Library((series,), ())),
                    patch.object(cli, "calibre_executable", side_effect=AssertionError("Calibre must be skipped")),
                    patch.object(cli, "run_worker", side_effect=raw_worker),
                    redirect_stdout(io.StringIO()),
                ):
                    self.assertEqual(cli.entrypoint(), 0)
                output = target / series.title / "A title.kfx-zip" if command == "s" else target
                with ZipFile(output) as bundle:
                    self.assertEqual(bundle.read("book.azw8"), payload)
                self.assertEqual(already_saved(root, output.parent, f"amazon:jp:{volume.id}", "kfx-zip"), output)
                self.assertIsNone(already_saved(root, output.parent, f"amazon:jp:{volume.id}", "auto"))

    def test_calibre_override_precedes_environment_and_is_scoped(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            binary = "calibre-debug.exe" if os.name == "nt" else "calibre-debug"
            path = root / "calibre.app" / "Contents" / "MacOS" / binary
            path.parent.mkdir(parents=True)
            path.touch()
            path.chmod(0o755)
            with patch.dict(os.environ, {"KINDLE_CALIBRE_BIN": str(root / "missing")}):
                for value in (path, path.parent, root / "calibre.app"):
                    with cli.using_calibre(value):
                        self.assertEqual(cli.calibre_executable("calibre-debug"), path)
                with self.assertRaises(cli.KindleError):
                    cli.calibre_executable("calibre-debug")

    def test_library_accepts_an_additional_store(self):
        with (
            TemporaryDirectory() as temporary,
            patch.dict(os.environ, {"KINDLEGARTEN_DATA_DIR": temporary}),
            patch("sys.argv", ["kindlegarten", "library", "--region", "de"]),
            patch.object(cli, "read_library", return_value=Library((), ())) as query,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(cli.entrypoint(), 0)
            query.assert_called_once_with("amazon", "de")

    def test_help_matches_the_download_alias(self):
        token = LANGUAGE.set("ja")
        try:
            with patch("sys.argv", ["kindlegarten", "help", "d"]), redirect_stdout(io.StringIO()) as output:
                self.assertEqual(cli.entrypoint(), 0)
                self.assertIn("使用法:", output.getvalue())
                self.assertIn("--format", output.getvalue())
                self.assertIn("--no-dedrm", output.getvalue())
                self.assertIn("--calibre-path", output.getvalue())
        finally:
            LANGUAGE.reset(token)

    def test_library_lists_series_and_book_ids_for_default_region(self):
        library = Library((Series("S-1", "Series", (Book("B01", "First"),)),), (Book("B02", "Second"),))
        with TemporaryDirectory() as temporary, patch.dict(os.environ, {"KINDLEGARTEN_DATA_DIR": temporary}):
            with (
                patch("sys.argv", ["kindlegarten", "library"]),
                patch.object(cli, "read_library", return_value=library) as query,
                redirect_stdout(io.StringIO()) as output,
            ):
                self.assertEqual(cli.entrypoint(), 0)
                query.assert_called_once_with("amazon", "jp")
                self.assertIn("Series [S-1]", output.getvalue())
                self.assertIn("First [B01]", output.getvalue())
                self.assertIn("Second [B02]", output.getvalue())


class DesktopServicesTest(unittest.TestCase):
    def test_release_uses_only_the_project_download_address(self):
        with patch.object(updates, "platform_suffix", return_value="-macos-arm64.zip"):
            metadata = {"tag_name": "v1.2.0", "assets": [{"name": "kindlegarten-1.2.0-macos-arm64.zip",
                                                       "size": 100, "digest": "sha256:" + "0" * 64}]}
            newer = updates.newer_release(metadata, installed="1.1.9")
            self.assertEqual(newer.tag, "v1.2.0")
            self.assertTrue(newer.download_url.startswith(updates.REPOSITORY + "/releases/download/"))
            self.assertEqual(updates.newer_release({"tag_name": "v1.2.0", "assets": []}, installed="1.1.9").asset, "")
            self.assertIsNone(updates.newer_release(metadata, installed="1.2.0"))
            self.assertIsNone(updates.newer_release({**metadata, "prerelease": True}, installed="1.1.9"))

    def test_proxy_authentication_separates_server_and_password(self):
        with using_proxy("http://name:p%40ss@proxy.example:8080"):
            config = browser_proxy()["proxy"]
            self.assertEqual(config["server"], "http://proxy.example:8080")
            self.assertEqual(config["password"], "p@ss")
        with using_proxy("direct"):
            self.assertEqual(browser_proxy(), {})
        with self.assertRaises(ValueError), using_proxy("socks5://name:password@proxy.example:1080"):
            pass


if __name__ == "__main__":
    unittest.main()
