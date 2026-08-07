import asyncio
import os
import secrets
import tempfile
import unittest
from pathlib import Path

from connector.attachments import AttachmentRegistry, AttachmentRejected, attachment_purge_loop


class AttachmentRegistryTests(unittest.TestCase):
    def test_startup_removes_regular_opaque_orphans_but_preserves_symlinks_and_other_files(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root = Path(directory)
            orphan = root / secrets.token_urlsafe(32)
            orphan.write_bytes(b"orphan")
            unrelated = root / "keep-me.txt"
            unrelated.write_bytes(b"not an attachment")
            outside_file = Path(outside) / "outside.txt"
            outside_file.write_bytes(b"do not delete")
            link = root / secrets.token_urlsafe(32)
            link.symlink_to(outside_file)

            AttachmentRegistry(root, clock=lambda: 100.0)

            self.assertFalse(orphan.exists())
            self.assertTrue(unrelated.exists())
            self.assertTrue(link.is_symlink())
            self.assertTrue(outside_file.exists())

    def test_stores_private_opaque_files_and_resolves_once(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = AttachmentRegistry(Path(directory), clock=lambda: 100.0)
            item = registry.store("notes/report final.txt", b"hello")
            self.assertRegex(item["id"], r"^[A-Za-z0-9_-]{32,128}$")
            self.assertEqual(item["name"], "report final.txt")
            self.assertEqual(item["size"], 5)
            path = registry.resolve_and_consume(item["id"], item["name"])
            self.assertEqual(path.read_bytes(), b"hello")
            with self.assertRaises(AttachmentRejected):
                registry.resolve_and_consume(item["id"], item["name"])
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_rejects_traversal_empty_and_bad_names(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = AttachmentRegistry(Path(directory), clock=lambda: 100.0)
            for name in ("", "../secret.txt", "folder/../secret.txt", "bad*name.txt"):
                with self.subTest(name=name), self.assertRaises(AttachmentRejected):
                    registry.store(name, b"x")

    def test_expired_files_are_deleted_and_cannot_be_consumed(self):
        now = [100.0]
        with tempfile.TemporaryDirectory() as directory:
            registry = AttachmentRegistry(Path(directory), clock=lambda: now[0])
            item = registry.store("old.txt", b"x")
            now[0] = 100.0 + 30 * 60 + 1
            registry.purge_expired()
            with self.assertRaises(AttachmentRejected):
                registry.resolve_and_consume(item["id"], item["name"])

    def test_name_mismatch_does_not_consume_the_attachment(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = AttachmentRegistry(Path(directory), clock=lambda: 100.0)
            item = registry.store("report.txt", b"hello")
            with self.assertRaises(AttachmentRejected):
                registry.resolve_and_consume(item["id"], "wrong.txt")
            path = registry.resolve_and_consume(item["id"], item["name"])
            self.assertEqual(path.read_bytes(), b"hello")

    def test_cleanup_deletes_a_consumed_file(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = AttachmentRegistry(Path(directory), clock=lambda: 100.0)
            item = registry.store("report.txt", b"hello")
            path = registry.resolve_and_consume(item["id"], item["name"])
            self.assertTrue(path.exists())
            registry.cleanup(path)
            self.assertFalse(path.exists())

    def test_partial_batch_resolution_cleans_consumed_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = AttachmentRegistry(Path(directory), clock=lambda: 100.0)
            first = registry.store("first.txt", b"one")
            second = registry.store("second.txt", b"two")
            with self.assertRaises(AttachmentRejected):
                registry.resolve_and_consume_batch([
                    (first["id"], first["name"]),
                    ("missing-id", second["name"]),
                ])
            first_path = registry.resolve_and_consume(first["id"], first["name"])
            second_path = registry.resolve_and_consume(second["id"], second["name"])
            registry.cleanup(second_path)
            registry.cleanup(first_path)
            self.assertFalse(first_path.exists())
            self.assertFalse(second_path.exists())

    def test_limits_each_file_and_total_batch(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = AttachmentRegistry(Path(directory), clock=lambda: 100.0)
            with self.assertRaises(AttachmentRejected):
                registry.store("large.bin", b"x" * (5 * 1024 * 1024 + 1))
            with self.assertRaises(AttachmentRejected):
                registry.validate_batch([b"x" * (5 * 1024 * 1024)] * 3)


class AttachmentPurgeLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_loop_continues_after_purge_error_and_cancels_cleanly(self):
        class FakeRegistry:
            def __init__(self):
                self.calls = 0

            def purge_expired(self):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("simulated purge failure")

        registry = FakeRegistry()
        task = asyncio.create_task(attachment_purge_loop(registry, interval=0))
        for _ in range(100):
            if registry.calls >= 2:
                break
            await asyncio.sleep(0)
        self.assertGreaterEqual(registry.calls, 2)

        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task


if __name__ == "__main__":
    unittest.main()
