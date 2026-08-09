"""Boundary tests for the top-level installer's isolated/staging mode.

These tests exercise install.sh's `--stage-root` test-mode guard only.  They
never run the production path, never touch real system paths, and never invoke
sudo/systemctl/nginx/network/pip.
"""

import os
import select
import shutil
import stat
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO_ROOT / "install.sh"
STAGE_GUARD = "HERMES_CLASSROOM_STAGE_TEST"
STAGE_MARKER = ".hermes-classroom-stage-root"
SAFE_REGISTER_RETRY = (
    "/opt/hermes-classroom-connector/register.sh https://YOUR-PORTAL-HOST"
)

RUNTIME_FILES = [
    "hermes_classroom_connector.py",
    "app_tunnel.py",
    "abacus_usage.py",
    "telemetry.py",
    "idempotency.py",
    "session_payloads.py",
    "streaming_sse.py",
    "clarify_state.py",
    "attachments.py",
    "multipart_uploads.py",
    "patch_nginx_default.py",
    "nginx-hermes-classroom.conf",
]

STAGING_ENV = "\n".join([
    "HERMES_CLASSROOM_SHARED_SECRET=" + "a" * 64,
    "HERMES_DASHBOARD_SESSION_TOKEN=" + "b" * 64,
    "HERMES_CLASSROOM_PORT=8765",
    "HERMES_LOCAL_URL=http://127.0.0.1:8642",
]) + "\n"


def run_install(stage_root, *, guard=True, extra_env=None, extra_args=None):
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if guard:
        env[STAGE_GUARD] = "1"
    if extra_env:
        env.update(extra_env)
    args = ["bash", str(INSTALL_SH)]
    if stage_root is not None:
        args += ["--stage-root", str(stage_root)]
    if extra_args:
        args += extra_args
    return subprocess.run(
        args,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.fixture
def stage_root(tmp_path):
    return tmp_path / "stage"


def combined(proc):
    return (proc.stdout or "") + (proc.stderr or "")


def assert_mode(path, mode):
    current = stat.S_IMODE(path.stat().st_mode)
    assert current == mode, f"{path}: expected {mode:o}, got {current:o}"


def run_registration_tail(*, guard=True, extra_env=None):
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if guard:
        env[STAGE_GUARD] = "1"
    else:
        env.pop(STAGE_GUARD, None)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(INSTALL_SH), "--test-registration-tail"],
        env=env,
        capture_output=True,
        text=True,
        timeout=5,
    )


def run_registration_tail_pty(answer, *, extra_env):
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env[STAGE_GUARD] = "1"
    env.update(extra_env)
    master, slave = os.openpty()
    proc = subprocess.Popen(
        ["bash", str(INSTALL_SH), "--test-registration-tail"],
        env=env,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        close_fds=True,
    )
    os.close(slave)
    output = bytearray()
    sent = False
    deadline = time.monotonic() + 5
    try:
        while time.monotonic() < deadline:
            readable, _, _ = select.select([master], [], [], 0.1)
            if master in readable:
                try:
                    chunk = os.read(master, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                output.extend(chunk)
                if not sent and b"Register this VM with the course portal now? [Y/n]" in output:
                    os.write(master, answer.encode("utf-8") + b"\n")
                    sent = True
            if proc.poll() is not None:
                break
        assert sent, output.decode("utf-8", errors="replace")
        returncode = proc.wait(timeout=1)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        os.close(master)
    return returncode, output.decode("utf-8", errors="replace")


class TestStageModeGuard:
    def test_stage_root_refused_without_explicit_test_mode_guard(self, stage_root):
        proc = run_install(stage_root, guard=False)
        assert proc.returncode != 0
        assert STAGE_GUARD in combined(proc)

    def test_stage_root_refuses_root_directory(self, tmp_path):
        proc = run_install(Path("/"))
        assert proc.returncode != 0
        assert "Refusing" in combined(proc)

    def test_stage_root_refuses_relative_path(self, tmp_path):
        proc = run_install("relative/stage")
        assert proc.returncode != 0
        assert "absolute" in combined(proc)

    def test_stage_root_refuses_existing_unmarked_directory(self, tmp_path):
        existing = tmp_path / "already-here"
        existing.mkdir()
        proc = run_install(existing)
        assert proc.returncode != 0
        assert "not created by a staging install" in combined(proc)

    def test_stage_root_refuses_symlink_root(self, tmp_path):
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real)
        proc = run_install(link)
        assert proc.returncode != 0
        assert "symlink" in combined(proc)

    def test_stage_root_refuses_missing_parent(self, tmp_path):
        proc = run_install(tmp_path / "no-such-parent" / "stage")
        assert proc.returncode != 0
        assert "parent" in combined(proc)


class TestStagedRuntimeLayout:
    def test_full_runtime_layout_with_no_test_or_source_strays(self, stage_root):
        proc = run_install(stage_root)
        assert proc.returncode == 0, combined(proc)

        install_root = stage_root / "opt" / "hermes-classroom-connector"
        config_root = stage_root / "etc" / "hermes-classroom-connector"
        systemd_dir = stage_root / "etc" / "systemd" / "system"
        data_dir = stage_root / "var" / "lib" / "hermes-classroom-connector"

        for name in RUNTIME_FILES:
            target = install_root / name
            assert target.is_file(), f"missing staged runtime file: {target}"

        for name in ("attachments.py", "multipart_uploads.py", "app_tunnel.py"):
            assert_mode(install_root / name, 0o644)
        skill = (
            stage_root
            / "home"
            / "ubuntu"
            / ".hermes"
            / "skills"
            / "devops"
            / "abacus-vm-web-deployment"
            / "SKILL.md"
        )
        assert skill.is_file(), "missing installed web-deployment skill"
        assert skill.read_bytes() == (
            REPO_ROOT / "skills" / "devops" / "abacus-vm-web-deployment" / "SKILL.md"
        ).read_bytes()
        assert_mode(skill, 0o644)
        assert_mode(install_root / "patch_nginx_default.py", 0o755)
        assert_mode(install_root / "nginx-hermes-classroom.conf", 0o644)

        for name in ("hermes-classroom-connector.service", "hermes-classroom-serve.service"):
            target = systemd_dir / name
            assert target.is_file(), f"missing staged service unit: {target}"
            assert_mode(target, 0o644)

        attachments = data_dir / "attachments"
        assert attachments.is_dir(), "missing protected attachments directory"
        assert_mode(attachments, 0o700)

        env_file = config_root / "connector.env"
        assert env_file.is_file(), "missing staged connector.env"
        assert_mode(env_file, 0o640)

        assert (stage_root / STAGE_MARKER).is_file()

        staged = [p for p in stage_root.rglob("*") if p.is_file()]
        bad_names = [
            p for p in staged
            if p.name.startswith("test_")
            or "__pycache__" in p.parts
            or p.name.endswith(".pyc")
            or p.name.endswith(".pyo")
        ]
        assert bad_names == [], f"staged files leaked into layout: {bad_names}"

        assert not (install_root / "README.md").exists()
        assert not (install_root / "install.sh").exists()

    def test_register_command_is_installed_byte_identical_and_executable(self, stage_root):
        proc = run_install(stage_root)
        assert proc.returncode == 0, combined(proc)

        source = REPO_ROOT / "register.sh"
        target = stage_root / "opt" / "hermes-classroom-connector" / "register.sh"
        assert target.is_file()
        assert target.read_bytes() == source.read_bytes()
        assert_mode(target, 0o755)

    def test_reviewed_modules_copied_byte_for_byte_from_source(self, stage_root):
        proc = run_install(stage_root)
        assert proc.returncode == 0, combined(proc)
        install_root = stage_root / "opt" / "hermes-classroom-connector"
        for name in ("attachments.py", "multipart_uploads.py"):
            source = REPO_ROOT / "connector" / name
            target = install_root / name
            assert target.read_bytes() == source.read_bytes(), f"{name} copy diverged from source"

    def test_staged_connector_env_uses_deterministic_placeholders(self, stage_root):
        proc = run_install(stage_root)
        assert proc.returncode == 0, combined(proc)
        env_file = stage_root / "etc" / "hermes-classroom-connector" / "connector.env"
        assert env_file.read_text() == STAGING_ENV


class TestStagingIsolation:
    FORBIDDEN = ("sudo", "systemctl", "nginx", "curl", "pip", "pip3", "openssl")

    def test_staging_never_calls_sudo_systemctl_nginx_network_or_pip(self, stage_root, tmp_path):
        fakebin = tmp_path / "fakebin"
        fakebin.mkdir()
        marker = tmp_path / "forbidden-called"
        for name in self.FORBIDDEN:
            shim = fakebin / name
            shim.write_text(
                "#!/usr/bin/env bash\n"
                f'echo "{name}" >> "{marker}"\n'
                "exit 99\n"
            )
            shim.chmod(0o755)

        env = {"PATH": f"{fakebin}:{os.environ['PATH']}"}
        proc = run_install(stage_root, extra_env=env)
        assert proc.returncode == 0, combined(proc)
        assert not marker.exists(), f"forbidden command was invoked: {marker.read_text()!r}"


class TestStagedConfigIdempotency:
    def test_existing_connector_env_is_byte_identical_after_reinstall(self, stage_root):
        first = run_install(stage_root)
        assert first.returncode == 0, combined(first)

        env_file = stage_root / "etc" / "hermes-classroom-connector" / "connector.env"
        original = env_file.read_bytes()

        second = run_install(stage_root)
        assert second.returncode == 0, combined(second)
        assert env_file.read_bytes() == original, "connector.env changed across isolated reinstall"

    def test_unrelated_config_keys_survive_reinstall(self, stage_root):
        first = run_install(stage_root)
        assert first.returncode == 0, combined(first)

        env_file = stage_root / "etc" / "hermes-classroom-connector" / "connector.env"
        env_file.write_text(
            env_file.read_text() + "HERMES_CUSTOM_EXTRA=keep-me\n",
            encoding="utf-8",
        )
        original = env_file.read_bytes()

        second = run_install(stage_root)
        assert second.returncode == 0, combined(second)
        assert env_file.read_bytes() == original
        assert b"HERMES_CUSTOM_EXTRA=keep-me" in env_file.read_bytes()

    def test_missing_dashboard_token_fails_before_services_without_leaking_values(self, stage_root):
        first = run_install(stage_root)
        assert first.returncode == 0, combined(first)

        env_file = stage_root / "etc" / "hermes-classroom-connector" / "connector.env"
        marker_secret = "1" * 64
        env_file.write_text(
            "HERMES_CLASSROOM_SHARED_SECRET=" + marker_secret + "\n"
            "HERMES_CLASSROOM_PORT=8765\n"
            "HERMES_LOCAL_URL=http://127.0.0.1:8642\n",
            encoding="utf-8",
        )

        proc = run_install(stage_root)
        assert proc.returncode != 0
        assert "DASHBOARD_SESSION_TOKEN" in combined(proc)
        assert marker_secret not in combined(proc), "secret value leaked into installer output"
        assert "0" * 64 not in combined(proc)


class TestInstallerDependencyMap:
    def test_multipart_module_maps_to_python_multipart_package(self):
        source = INSTALL_SH.read_text(encoding="utf-8")
        assert "python-multipart" in source
        assert re_search_for_multipart_module(source)

    def test_other_module_package_mappings_remain_explicit(self):
        source = INSTALL_SH.read_text(encoding="utf-8")
        for package in ("fastapi", "uvicorn", "httpx", "websockets"):
            assert f"{package}]={package}" in source


class TestRegistrationHandoffWiring:
    def test_guarded_tail_and_production_use_the_single_handoff_function(self):
        source = INSTALL_SH.read_text(encoding="utf-8")
        assert source.count("completion_and_registration_handoff() {") == 1
        assert source.count("  completion_and_registration_handoff\n") == 1
        assert source.rstrip().endswith("completion_and_registration_handoff")


class TestRegistrationHandoffTail:
    DUMMY_SECRET = "a" * 64
    DUMMY_TOKEN = "b" * 64

    def test_tail_mode_refused_without_explicit_test_mode_guard(self):
        proc = run_registration_tail(guard=False)
        assert proc.returncode != 0
        assert STAGE_GUARD in combined(proc)

    def test_non_tty_returns_without_invoking_register_and_prints_safe_retry(self, tmp_path):
        marker = tmp_path / "register-called"
        fake_register = self.make_fake_register(tmp_path, marker)
        proc = run_registration_tail(
            extra_env={"HERMES_CLASSROOM_REGISTER_TEST_EXECUTABLE": str(fake_register)}
        )
        output = combined(proc)
        assert proc.returncode == 0, output
        assert not marker.exists()
        assert SAFE_REGISTER_RETRY in output
        assert self.DUMMY_SECRET not in output
        assert self.DUMMY_TOKEN not in output

    def test_non_tty_tail_has_no_system_network_package_or_config_access(self, tmp_path):
        fakebin = tmp_path / "fakebin"
        fakebin.mkdir()
        forbidden_marker = tmp_path / "forbidden-called"
        register_marker = tmp_path / "register-called"
        fake_register = self.make_fake_register(tmp_path, register_marker)
        for name in ("sudo", "systemctl", "nginx", "curl", "pip", "pip3", "openssl"):
            shim = fakebin / name
            shim.write_text(
                "#!/usr/bin/env bash\n"
                f"printf '%s\\n' {name} >> {forbidden_marker!s}\n"
                "exit 99\n",
                encoding="utf-8",
            )
            shim.chmod(0o755)

        proc = run_registration_tail(extra_env={
            "PATH": f"{fakebin}:{os.environ['PATH']}",
            "HERMES_CLASSROOM_REGISTER_TEST_EXECUTABLE": str(fake_register),
        })
        assert proc.returncode == 0, combined(proc)
        assert not forbidden_marker.exists()
        assert not register_marker.exists()

    @pytest.mark.parametrize("answer", ["", "y"])
    def test_pty_default_or_yes_invokes_register_once_with_zero_args(self, tmp_path, answer):
        marker = tmp_path / "register-called"
        fake_register = self.make_fake_register(tmp_path, marker)
        returncode, output = run_registration_tail_pty(
            answer,
            extra_env={"HERMES_CLASSROOM_REGISTER_TEST_EXECUTABLE": str(fake_register)},
        )
        assert returncode == 0, output
        assert "Register this VM with the course portal now? [Y/n]" in output
        assert marker.read_text(encoding="utf-8") == "0\n"
        assert self.DUMMY_SECRET not in output
        assert self.DUMMY_TOKEN not in output

    def test_pty_decline_does_not_invoke_register_and_prints_safe_retry(self, tmp_path):
        marker = tmp_path / "register-called"
        fake_register = self.make_fake_register(tmp_path, marker)
        returncode, output = run_registration_tail_pty(
            "n",
            extra_env={"HERMES_CLASSROOM_REGISTER_TEST_EXECUTABLE": str(fake_register)},
        )
        assert returncode == 0, output
        assert "Register this VM with the course portal now? [Y/n]" in output
        assert not marker.exists()
        assert SAFE_REGISTER_RETRY in output
        assert self.DUMMY_SECRET not in output
        assert self.DUMMY_TOKEN not in output

    def test_pty_registration_failure_is_visible_and_returns_nonzero(self, tmp_path):
        marker = tmp_path / "register-called"
        fake_register = self.make_fake_register(tmp_path, marker, exit_code=7)
        returncode, output = run_registration_tail_pty(
            "",
            extra_env={"HERMES_CLASSROOM_REGISTER_TEST_EXECUTABLE": str(fake_register)},
        )
        assert returncode != 0
        assert marker.read_text(encoding="utf-8") == "0\n"
        assert "Registration failed" in output
        assert "installation remains complete" in output
        assert SAFE_REGISTER_RETRY in output

    @staticmethod
    def make_fake_register(tmp_path, marker, *, exit_code=0):
        fake_register = tmp_path / "fake-register"
        fake_register.write_text(
            "#!/usr/bin/env bash\n"
            f"printf '%s\\n' \"$#\" >> {marker!s}\n"
            f"exit {exit_code}\n",
            encoding="utf-8",
        )
        fake_register.chmod(0o755)
        return fake_register


def re_search_for_multipart_module(source):
    if "multipart" not in source:
        return False
    return "[multipart]=python-multipart" in source
