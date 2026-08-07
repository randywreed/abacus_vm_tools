import importlib
import os
import tempfile
import unittest
from pathlib import Path


class DashboardTokenTests(unittest.TestCase):
    def test_uses_token_in_service_environment_without_legacy_token_file(self):
        with tempfile.TemporaryDirectory() as directory:
            missing_env_file = Path(directory) / "missing.env"
            attachment_dir = Path(directory) / "attachments"
            old_secret = os.environ.get("HERMES_CLASSROOM_SHARED_SECRET")
            old_token = os.environ.get("HERMES_DASHBOARD_SESSION_TOKEN")
            old_env_file = os.environ.get("HERMES_ENV_FILE")
            old_attachment_dir = os.environ.get("HERMES_ATTACHMENT_DIR")
            try:
                os.environ["HERMES_CLASSROOM_SHARED_SECRET"] = "a" * 64
                os.environ["HERMES_DASHBOARD_SESSION_TOKEN"] = "b" * 64
                os.environ["HERMES_ENV_FILE"] = str(missing_env_file)
                os.environ["HERMES_ATTACHMENT_DIR"] = str(attachment_dir)
                import hermes_classroom_connector
                module = importlib.reload(hermes_classroom_connector)
                self.assertEqual(module._dashboard_token(), "b" * 64)
            finally:
                for key, value in {
                    "HERMES_CLASSROOM_SHARED_SECRET": old_secret,
                    "HERMES_DASHBOARD_SESSION_TOKEN": old_token,
                    "HERMES_ENV_FILE": old_env_file,
                    "HERMES_ATTACHMENT_DIR": old_attachment_dir,
                }.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value


if __name__ == "__main__":
    unittest.main()
