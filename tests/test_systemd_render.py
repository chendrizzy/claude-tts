"""Gate tests for the systemd --user unit renderer (daemon/platforms/service.py)."""
import sys
import configparser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from daemon.platforms.service import render_systemd_unit  # noqa: E402

# A new renderer must never inject personal data (same rule as the launchd one).
PII_FORBIDDEN = ("/Volumes/", "com.justinchen", "@gmail", "profile_id",
                 "/opt/anaconda", "anaconda3", "mlx_python")


def _parse(unit_text):
    # interpolation=None: systemd uses %t which BasicInterpolation would reject.
    # optionxform=str: preserve key case (Type, not type).
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.optionxform = str
    cp.read_string(unit_text)
    return cp


def test_render_systemd_unit_parses_and_has_sections():
    unit = render_systemd_unit(
        program_args=["/x/.venv/bin/python", "-m", "daemon.tts_daemon"],
        env={"PYTHONUNBUFFERED": "1"},
    )
    cp = _parse(unit)
    assert cp.has_section("Unit")
    assert cp.has_section("Service")
    assert cp.has_section("Install")
    assert cp["Service"]["Type"] == "simple"
    assert cp["Service"]["Restart"] == "always"
    assert cp["Service"]["RestartSec"] == "5"
    assert cp["Service"]["StandardOutput"] == "journal"
    assert cp["Service"]["StandardError"] == "journal"
    assert cp["Install"]["WantedBy"] == "default.target"


def test_render_systemd_unit_execstart_has_program_args():
    unit = render_systemd_unit(
        program_args=["/x/.venv/bin/python", "-m", "daemon.tts_daemon"], env={},
    )
    assert "ExecStart=/x/.venv/bin/python -m daemon.tts_daemon" in unit


def test_render_systemd_unit_quotes_args_with_spaces():
    unit = render_systemd_unit(
        program_args=["/home/a b/python", "-m", "daemon.tts_daemon"], env={},
    )
    assert 'ExecStart="/home/a b/python" -m daemon.tts_daemon' in unit


def test_render_systemd_unit_emits_env_and_xdg_runtime():
    unit = render_systemd_unit(
        program_args=["py"], env={"PYTHONUNBUFFERED": "1", "PATH": "/usr/bin"},
    )
    # Each Environment line is emitted verbatim; XDG_RUNTIME_DIR=%t is always added
    # so the daemon resolves ${XDG_RUNTIME_DIR}/claude-tts.sock under systemd --user.
    assert "Environment=PYTHONUNBUFFERED=1" in unit
    assert "Environment=PATH=/usr/bin" in unit
    assert "Environment=XDG_RUNTIME_DIR=%t" in unit


def test_render_systemd_unit_injects_no_pii():
    unit = render_systemd_unit(program_args=["PYTHON", "-m", "daemon.tts_daemon"], env={})
    for needle in PII_FORBIDDEN:
        assert needle not in unit, f"renderer injected PII: {needle}"
