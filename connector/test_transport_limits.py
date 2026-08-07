"""Regression tests for the multipart transport framing ceiling.

The connector transport ceiling must admit 10 MiB of attachment content plus a
small bounded multipart framing allowance, while the attachment content policy
in attachments.py (5 MiB per file, 10 MiB total) stays unchanged.

The full ``hermes_classroom_connector`` module is not imported here because the
test environment does not provide its runtime dependencies; the constants are
read from the connector source with ``ast`` instead, matching the static-source
style used elsewhere in this suite.
"""

import ast
import tempfile
import unittest
from pathlib import Path

from connector.attachments import AttachmentRegistry, AttachmentRejected, MAX_FILE_BYTES, MAX_TOTAL_BYTES

CONNECTOR_SOURCE = Path(__file__).parent / "hermes_classroom_connector.py"


class _SafeArithmeticEvaluator(ast.NodeVisitor):
    def __init__(self, bindings):
        self.bindings = dict(bindings)

    def visit_Constant(self, node):
        if isinstance(node.value, int):
            return node.value
        raise ValueError(f"unsupported literal {node.value!r}")

    def visit_Name(self, node):
        if node.id in self.bindings:
            return self.bindings[node.id]
        raise ValueError(f"unknown identifier {node.id!r}")

    def visit_BinOp(self, node):
        left = self.visit(node.left)
        right = self.visit(node.right)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.FloorDiv):
            return left // right
        raise ValueError(f"unsupported operator {type(node.op).__name__}")

    def visit_UnaryOp(self, node):
        operand = self.visit(node.operand)
        if isinstance(node.op, ast.USub):
            return -operand
        if isinstance(node.op, ast.UAdd):
            return operand
        raise ValueError(f"unsupported unary operator {type(node.op).__name__}")

    def visit(self, node):
        return super().visit(node)


def connector_constants():
    tree = ast.parse(CONNECTOR_SOURCE.read_text(encoding="utf-8"))
    # Imported content policy from attachments.py; attachments.py itself is
    # asserted unchanged below, so these seeds are the source of truth here.
    bindings = {
        "MAX_TOTAL_BYTES": MAX_TOTAL_BYTES,
        "MAX_FILE_BYTES": MAX_FILE_BYTES,
        "MAX_FILES": 3,
    }
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            try:
                bindings[node.targets[0].id] = _SafeArithmeticEvaluator(bindings).visit(node.value)
            except ValueError:
                pass
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            try:
                bindings[node.target.id] = _SafeArithmeticEvaluator(bindings).visit(node.value)
            except ValueError:
                pass
    return bindings


class MultipartFramingCeilingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.constants = connector_constants()

    def test_framing_allowance_is_small_and_bounded(self):
        self.assertEqual(self.constants["MULTIPART_FRAMING_ALLOWANCE"], 64 * 1024)
        self.assertLessEqual(self.constants["MULTIPART_FRAMING_ALLOWANCE"], 64 * 1024)

    def test_connector_ceiling_is_content_policy_plus_framing_allowance(self):
        self.assertEqual(self.constants["MAX_TOTAL_BYTES"], MAX_TOTAL_BYTES)
        self.assertEqual(
            self.constants["MAX_FILE_REQUEST_BODY"],
            MAX_TOTAL_BYTES + self.constants["MULTIPART_FRAMING_ALLOWANCE"],
        )
        # The transport ceiling must stay bounded and never grow unboundedly.
        self.assertLess(
            self.constants["MAX_FILE_REQUEST_BODY"],
            11 * 1024 * 1024,
            "connector transport ceiling must stay below the framework ceiling",
        )

    def test_non_file_ceiling_is_unchanged(self):
        self.assertEqual(self.constants["MAX_BODY"], 1024 * 1024)

    def test_connector_authenticates_before_parsing_multipart(self):
        source = CONNECTOR_SOURCE.read_text(encoding="utf-8")
        authenticate = source.index(
            "await _authenticate(request.method, request.url.path, request.headers, body, MAX_FILE_REQUEST_BODY)"
        )
        parse = source.index("await request.form()")
        self.assertGreater(parse, authenticate, "HMAC-before-parse order must be preserved")


class AttachmentContentPolicyTests(unittest.TestCase):
    def test_attachment_content_policy_is_unchanged(self):
        self.assertEqual(MAX_FILE_BYTES, 5 * 1024 * 1024)
        self.assertEqual(MAX_TOTAL_BYTES, 10 * 1024 * 1024)

    def test_validate_batch_still_rejects_overlimit_files_and_totals(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = AttachmentRegistry(Path(directory), clock=lambda: 100.0)
            with self.assertRaises(AttachmentRejected):
                registry.validate_batch([b"x" * (MAX_FILE_BYTES + 1)])
            with self.assertRaises(AttachmentRejected):
                registry.validate_batch([b"x" * MAX_TOTAL_BYTES])
            with self.assertRaises(AttachmentRejected):
                registry.validate_batch([b"x" * MAX_TOTAL_BYTES, b"y"])
            # Exactly at the content ceiling (two 5 MiB files) is still accepted.
            registry.validate_batch([b"x" * MAX_FILE_BYTES, b"x" * MAX_FILE_BYTES])

    def test_registry_rejects_file_over_max_file_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = AttachmentRegistry(Path(directory), clock=lambda: 100.0)
            with self.assertRaises(AttachmentRejected):
                registry.store("over.bin", b"x" * (MAX_FILE_BYTES + 1))

    def test_portal_route_keeps_controlled_413_for_oversized_uploads(self):
        route_source_path = Path(__file__).parent.parent / "app/api/hermes/[...path]/route.ts"
        if not route_source_path.exists():
            self.skipTest("portal source is not part of this VM repo")
        route_source = route_source_path.read_text(encoding="utf-8")
        self.assertIn("MAX_FILE_REQUEST_BYTES", route_source)
        self.assertIn("status: 413", route_source)
        self.assertIn("Attachments must be 10 MiB or smaller.", route_source)


if __name__ == "__main__":
    unittest.main()
