"""Regression tests for Starlette multipart upload selection."""

import asyncio
import unittest

from starlette.requests import Request

from connector.multipart_uploads import select_uploads


class MultipartUploadSelectionTests(unittest.TestCase):
    def test_repeated_files_parts_are_selected_in_order(self):
        boundary = "----hermes-repeated-files-boundary"
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="files"; filename="first.txt"\r\n'
            "Content-Type: text/plain\r\n\r\n"
            "first file\r\n"
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="files"; filename="second.txt"\r\n'
            "Content-Type: text/plain\r\n\r\n"
            "second file\r\n"
            f"--{boundary}--\r\n"
        ).encode()

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/hermes-classroom/v1/files",
                "headers": [
                    (b"content-type", f"multipart/form-data; boundary={boundary}".encode()),
                    (b"content-length", str(len(body)).encode()),
                ],
                "query_string": b"",
            },
            receive,
        )

        async def parse_and_select():
            form = await request.form()
            uploads = select_uploads(value for _, value in form.multi_items())
            try:
                self.assertEqual([upload.filename for upload in uploads], ["first.txt", "second.txt"])
            finally:
                for upload in uploads:
                    await upload.close()

        asyncio.run(parse_and_select())

    def test_real_starlette_multipart_file_is_selected(self):
        boundary = "----hermes-test-boundary"
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="notes.txt"\r\n'
            "Content-Type: text/plain\r\n\r\n"
            "hello from starlette\r\n"
            f"--{boundary}--\r\n"
        ).encode()

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/hermes-classroom/v1/files",
                "headers": [
                    (b"content-type", f"multipart/form-data; boundary={boundary}".encode()),
                    (b"content-length", str(len(body)).encode()),
                ],
                "query_string": b"",
            },
            receive,
        )

        async def parse_and_select():
            form = await request.form()
            uploads = select_uploads(form.values())
            try:
                self.assertEqual(len(uploads), 1)
                self.assertEqual(uploads[0].filename, "notes.txt")
            finally:
                for upload in uploads:
                    await upload.close()

        asyncio.run(parse_and_select())


if __name__ == "__main__":
    unittest.main()
