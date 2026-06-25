"""Gate tests for the launchd plist renderer (daemon/platforms/service.py)."""
import sys
import plistlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from daemon.platforms.service import render_launchd_plist  # noqa: E402

# A new renderer must never inject personal data (spec section 6).
PII_FORBIDDEN = ("/Volumes/", "com.justinchen", "@gmail", "profile_id",
                 "/opt/anaconda", "anaconda3", "mlx_python")


def test_render_launchd_plist_round_trips():
    xml = render_launchd_plist(
        label="com.claude-tts.daemon",
        program_args=["/opt/x/.venv/bin/python", "-m", "daemon.tts_daemon"],
        env={"PATH": "/usr/bin", "PYTHONUNBUFFERED": "1"},
        stdout_path="/tmp/claude-tts-daemon.out.log",
        stderr_path="/tmp/claude-tts-daemon.err.log",
    )
    data = plistlib.loads(xml.encode("utf-8"))
    assert data["Label"] == "com.claude-tts.daemon"
    assert data["ProgramArguments"] == ["/opt/x/.venv/bin/python", "-m", "daemon.tts_daemon"]
    assert data["RunAtLoad"] is True
    assert data["KeepAlive"] is True
    assert data["ThrottleInterval"] == 10
    assert data["EnvironmentVariables"]["PYTHONUNBUFFERED"] == "1"
    assert "Sockets" not in data  # daemon binds its own unix socket


def test_render_launchd_plist_default_label_is_sanitized():
    xml = render_launchd_plist(
        program_args=["/x/python", "-m", "daemon.tts_daemon"],
        env={}, stdout_path="/tmp/o.log", stderr_path="/tmp/e.log",
    )
    assert plistlib.loads(xml.encode("utf-8"))["Label"] == "com.claude-tts.daemon"


def test_render_launchd_plist_injects_no_pii():
    # Caller passes only placeholder/portable values; the renderer must add none.
    xml = render_launchd_plist(
        program_args=["PYTHON", "-m", "daemon.tts_daemon"],
        env={}, stdout_path="OUT", stderr_path="ERR",
    )
    for needle in PII_FORBIDDEN:
        assert needle not in xml, f"renderer injected PII: {needle}"


from daemon.platforms import make_platform  # noqa: E402
from daemon.platforms.base import PlatformMacOS, PlatformLinux  # noqa: E402
import types
import daemon.platforms.base as base_mod  # noqa: E402
from daemon.platforms.base import PlatformWindows  # noqa: E402


def _fake_run(calls):
    """Replace subprocess.run: record the argv list, return rc 0."""
    def run(cmd, *a, **k):
        calls.append(cmd)
        return types.SimpleNamespace(returncode=0)
    return run


def test_macos_plist_path_uses_launchagents():
    p = PlatformMacOS()
    path = p.plist_path()
    assert str(path).endswith("/Library/LaunchAgents/com.claude-tts.daemon.plist")


def test_macos_render_service_round_trips():
    import plistlib
    p = PlatformMacOS()
    xml = p.render_service(program_args=["/x/python", "-m", "daemon.tts_daemon"],
                           env={"PYTHONUNBUFFERED": "1"})
    assert plistlib.loads(xml.encode())["Label"] == "com.claude-tts.daemon"


def test_linux_install_writes_unit_and_enables(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    calls = []
    monkeypatch.setattr(base_mod.shutil, "which", lambda n: f"/usr/bin/{n}")
    monkeypatch.setattr(base_mod.subprocess, "run", _fake_run(calls))
    monkeypatch.setattr(base_mod.getpass, "getuser", lambda: "tester")

    PlatformLinux().install_service(
        program_args=["/x/.venv/bin/python", "-m", "daemon.tts_daemon"],
        env={"PYTHONUNBUFFERED": "1"},
    )

    unit = (tmp_path / "systemd" / "user" / "claude-tts.service").read_text()
    assert "ExecStart=/x/.venv/bin/python -m daemon.tts_daemon" in unit
    assert "Type=simple" in unit and "Restart=always" in unit
    assert ["systemctl", "--user", "daemon-reload"] in calls
    assert ["systemctl", "--user", "enable", "--now", "claude-tts.service"] in calls
    assert ["loginctl", "enable-linger", "tester"] in calls


def test_linux_install_without_systemctl_raises(monkeypatch):
    monkeypatch.setattr(base_mod.shutil, "which", lambda n: None)
    try:
        PlatformLinux().install_service(program_args=["python"], env={})
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "systemctl" in str(e)


def test_linux_uninstall_disables_and_removes(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    unit_dir = tmp_path / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    unit_file = unit_dir / "claude-tts.service"
    unit_file.write_text("dummy")
    calls = []
    monkeypatch.setattr(base_mod.subprocess, "run", _fake_run(calls))

    PlatformLinux().uninstall_service()

    assert ["systemctl", "--user", "disable", "--now", "claude-tts.service"] in calls
    assert ["systemctl", "--user", "daemon-reload"] in calls
    assert not unit_file.exists()


def test_windows_install_service_not_supported():
    try:
        PlatformWindows().install_service(program_args=["python"], env={})
        assert False, "expected NotImplementedError"
    except NotImplementedError as e:
        assert "WSL2" in str(e) or "not supported" in str(e).lower()
