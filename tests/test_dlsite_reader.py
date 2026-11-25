import asyncio
import base64
import importlib.util
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from zipfile import ZipFile

import httpx

from kindlegarten.cli import check_epub
from kindlegarten.comipo import DLSITE
from kindlegarten.dlsite import reader_export


@unittest.skipUnless(importlib.util.find_spec("dlsite_async"), "DLsite optional dependency not installed")
class DLsiteReaderTest(unittest.TestCase):
    def test_authenticated_reflowable_reader_writes_a_complete_epub(self):
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding

        payloads = {
            "mimetype": b"application/epub+zip",
            "preprocess-settings.json": b'{"obfuscateText": false, "obfuscateImage": false}',
            "META-INF/container.xml": b"""<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
              <rootfiles><rootfile full-path="OPS/book.opf"/></rootfiles></container>""",
            "OPS/book.opf": b"""<package xmlns="http://www.idpf.org/2007/opf">
              <manifest><item id="page" href="page.xhtml" media-type="application/xhtml+xml"/></manifest>
              <spine><itemref idref="page"/></spine></package>""",
            "OPS/page.xhtml": b'<html xmlns="http://www.w3.org/1999/xhtml"><body>Example</body></html>',
        }
        key = b"fixture"
        def handler(request):
            if request.method == "POST":
                authority = json.loads(request.content)
                self.assertEqual(authority["workno"], "RJ123456")
                public = serialization.load_der_public_key(base64.b64decode(authority["public_key"]))
                digest = hashes.SHA256()
                encoded = public.encrypt(key.hex().encode(),
                                         padding.OAEP(mgf=padding.MGF1(digest), algorithm=digest, label=None))
                return httpx.Response(200, json={"values": {
                    "vt": "reader-fixture", "c": "unused", "base_url": DLSITE.cdn + "/example",
                    "account_id": "test", "customer_id": "test", "key": base64.b64encode(encoded).decode(),
                }})
            self.assertEqual(request.headers["authorization"], "Bearer reader-fixture")
            data = payloads[request.url.path.removeprefix("/example/")]
            return httpx.Response(200, content=bytes(value ^ key[i % len(key)] for i, value in enumerate(data)))

        client = Mock(site=DLSITE)
        client.request.storage_state.return_value = {"cookies": []}
        tree = {"hash": "fixture", "workno": "RJ123456", "playfile": {
            "file.epub": {"type": "epub_reflowable", "length": 0},
        }, "tree": []}
        with TemporaryDirectory() as directory, patch("httpx.AsyncHTTPTransport", return_value=httpx.MockTransport(handler)):
            path = asyncio.run(reader_export(client, Path(directory), "RJ123456", tree, "file.epub"))
            check_epub(path)
            with ZipFile(path) as archive:
                self.assertEqual(archive.read("OPS/page.xhtml"), payloads["OPS/page.xhtml"])


if __name__ == "__main__":
    unittest.main()
