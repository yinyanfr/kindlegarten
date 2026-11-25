import io
import unittest
from contextlib import nullcontext, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from kindlegarten import comipo


class ComipoTitlesTest(unittest.TestCase):
    def test_download_uses_localized_work_name(self):
        work_id = "BJ123456"
        series_id = "TITLE12345678"
        work = {"workno": work_id, "name": {"en_US": "English", "ja_JP": "連載作品（1）"}}
        client = Mock()
        client.authorized.return_value = True
        client.api.return_value = {"work": work}
        client.purchases.return_value = ("連載:作品", [work])
        playwright = Mock()

        with TemporaryDirectory() as temporary:
            data = Path(temporary)
            books = data / "books"
            comipo.session_path(data).write_text("{}", encoding="utf-8")
            with (
                patch.object(comipo, "playwright_session", return_value=nullcontext((playwright, None))),
                patch.object(comipo, "ComipoClient", return_value=client),
                patch.object(comipo, "save_session"),
                patch.object(comipo, "write_comic") as write_comic,
                redirect_stdout(io.StringIO()),
            ):
                comipo.download(("series", series_id), None, data, books, "kindlegarten")
                write_comic.assert_called_once_with(
                    client, work_id, books / "連載_作品" / "連載作品（1）.cbz", data
                )
                write_comic.reset_mock()
                comipo.download(("work", work_id), None, data, books, "kindlegarten")
                write_comic.assert_called_once_with(client, work_id, books / "連載作品（1）.cbz", data)

    def test_work_title_fallbacks(self):
        self.assertEqual(comipo.work_title({"name": "Title"}, "BJ123456"), "Title")
        self.assertEqual(comipo.work_title({"name": {"en_US": "Title"}}, "BJ123456"), "Title")
        self.assertEqual(comipo.work_title({"name": {"ja_JP": ""}}, "BJ123456"), "BJ123456")


if __name__ == "__main__":
    unittest.main()
