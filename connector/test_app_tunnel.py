"""Behavioral tests for the app tunnel registry and validation helpers.

The tunnel lets a student's Hermes agent publish a loopback web app (e.g.
running on 127.0.0.1:8767) at a public path under /hermes-classroom/apps/.
These tests cover the pure name/port validation and the in-memory registry;
the FastAPI routes are covered in test_app_tunnel_endpoint.py.
"""
import unittest

from app_tunnel import (
    AppTunnelRegistry,
    MAX_TUNNEL_APPS,
    RESERVED_TUNNEL_PORTS,
    validate_app_name,
    validate_tunnel_port,
)


class AppNameValidationTests(unittest.TestCase):
    def test_accepts_lowercase_alnum_and_hyphens(self):
        for name in ("game", "my-game", "a1", "game2", "a" * 63):
            with self.subTest(name=name):
                self.assertEqual(validate_app_name(name), name)

    def test_rejects_empty_and_whitespace(self):
        for name in ("", "  ", "game name", " game"):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    validate_app_name(name)

    def test_rejects_uppercase(self):
        for name in ("Game", "GAME", "myGame"):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    validate_app_name(name)

    def test_rejects_dots_slashes_and_path_separators(self):
        for name in ("game.name", "game/name", "game\\name", "..", "game..name", "a/b"):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    validate_app_name(name)

    def test_rejects_leading_or_trailing_hyphen(self):
        for name in ("-game", "game-"):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    validate_app_name(name)

    def test_rejects_underscores_and_other_symbols(self):
        for name in ("game_name", "game!name", "game@name", "g@me"):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    validate_app_name(name)

    def test_rejects_too_long_name(self):
        with self.assertRaises(ValueError):
            validate_app_name("a" * 64)


class PortValidationTests(unittest.TestCase):
    def test_accepts_high_ports(self):
        for port in (1024, 8080, 8767, 65535):
            with self.subTest(port=port):
                self.assertEqual(validate_tunnel_port(port), port)

    def test_rejects_privileged_and_out_of_range_ports(self):
        for port in (0, 1, 80, 443, 1023, 65536, -1):
            with self.subTest(port=port):
                with self.assertRaises(ValueError):
                    validate_tunnel_port(port)

    def test_rejects_reserved_infrastructure_ports(self):
        for port in sorted(RESERVED_TUNNEL_PORTS):
            with self.subTest(port=port):
                with self.assertRaises(ValueError):
                    validate_tunnel_port(port)

    def test_rejects_non_integer_types(self):
        for port in ("8080", None, 80.5, [8080]):
            with self.subTest(port=port):
                with self.assertRaises((ValueError, TypeError)):
                    validate_tunnel_port(port)


class TunnelRegistryTests(unittest.TestCase):
    def setUp(self):
        self.registry = AppTunnelRegistry()

    def test_register_and_get(self):
        self.registry.register("game", 8767)
        self.assertEqual(self.registry.get("game"), 8767)

    def test_register_replaces_existing_port(self):
        self.registry.register("game", 8767)
        self.registry.register("game", 8899)
        self.assertEqual(self.registry.get("game"), 8899)
        self.assertEqual(len(self.registry.list()), 1)

    def test_unregister_removes_entry(self):
        self.registry.register("game", 8767)
        self.assertTrue(self.registry.unregister("game"))
        self.assertIsNone(self.registry.get("game"))
        self.assertFalse(self.registry.unregister("game"))

    def test_list_returns_name_port_pairs(self):
        self.registry.register("game", 8767)
        self.registry.register("blog", 8080)
        entries = dict(self.registry.list())
        self.assertEqual(entries, {"game": 8767, "blog": 8080})

    def test_unknown_name_returns_none(self):
        self.assertIsNone(self.registry.get("nope"))

    def test_registry_caps_at_max_apps(self):
        registry = AppTunnelRegistry(max_apps=2)
        registry.register("a", 9001)
        registry.register("b", 9002)
        with self.assertRaises(ValueError):
            registry.register("c", 9003)
        # Replacing an existing name still works at the cap.
        registry.register("a", 9004)
        self.assertEqual(len(registry.list()), 2)

    def test_max_apps_constant_is_positive(self):
        self.assertGreaterEqual(MAX_TUNNEL_APPS, 1)

    def test_register_validates_name_and_port(self):
        with self.assertRaises(ValueError):
            self.registry.register("Bad Name", 8767)
        with self.assertRaises(ValueError):
            self.registry.register("game", 8642)


if __name__ == "__main__":
    unittest.main()
