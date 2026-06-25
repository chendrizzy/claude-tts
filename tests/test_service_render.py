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


def test_linux_install_service_not_implemented_yet():
    # Linux systemd install is Plan 4; the seam exists but raises until then.
    try:
        PlatformLinux().install_service(program_args=["python"], env={})
        assert False, "expected NotImplementedError"
    except NotImplementedError:
        pass
