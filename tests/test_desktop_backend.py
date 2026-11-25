import io
import os
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from zipfile import ZipFile

from PIL import Image
from pikepdf import Pdf

from kindlegarten import cli, comipo, updates
from kindlegarten.formats import already_saved, convert, save_export, selected_format
from kindlegarten.i18n import LANGUAGE
from kindlegarten.library import Book, Library, Series, amazon_library, by_purchase, cache_library, cached_library, purchase_timestamp
from kindlegarten.network import browser_proxy, using_proxy


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


class CommandsTest(unittest.TestCase):
    def test_download_infers_an_additional_store_from_product_url(self):
        with (
            TemporaryDirectory() as temporary,
            patch.dict(os.environ, {"KINDLEGARTEN_DATA_DIR": temporary}),
            patch("sys.argv", ["kindlegarten", "download", "https://www.amazon.co.uk/dp/B012345678"]),
            patch.object(cli, "download_amazon") as download,
        ):
            self.assertEqual(cli.entrypoint(), 0)
            download.assert_called_once_with("B012345678", "uk", None, "auto")

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
            metadata = {"tag_name": "v1.2.0", "assets": [{"name": "kindlegarten-1.2.0-macos-arm64.zip"}]}
            newer = updates.newer_release(metadata, installed="1.1.9")
            self.assertEqual(newer[0], "v1.2.0")
            self.assertTrue(newer[1].startswith(updates.REPOSITORY + "/releases/download/"))
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
