"""Gate tests for config assembly + write (daemon/config_io.py)."""
import sys
import json
import stat
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from daemon.config_io import render_config, write_config  # noqa: E402

PII_FORBIDDEN = ("/Volumes/", "com.justinchen", "@gmail", "profile_id",
                 "/opt/anaconda", "anaconda3", "mlx_python")


def test_render_config_ollama_shape():
    cfg = render_config(engine="edge-tts", voice_name="en-US-AvaNeural",
                        backend="ollama", model="qwen2.5-coder:1.5b")
    assert cfg["voice"]["engine"] == "edge-tts"
    assert cfg["voice"]["name"] == "en-US-AvaNeural"
    assert cfg["llm_provider"]["type"] == "ollama"
    assert cfg["summarizer"]["model"] == "qwen2.5-coder:1.5b"


def test_render_config_openai_shape():
    cfg = render_config(engine="edge-tts", voice_name="v", backend="openai",
                        model="gpt-4o-mini", base_url="http://localhost:1234/v1",
                        api_key="sk-test")
    assert cfg["llm_provider"] == {
        "type": "openai", "base_url": "http://localhost:1234/v1",
        "model": "gpt-4o-mini", "api_key": "sk-test",
    }


def test_render_config_null_is_deterministic_mode():
    cfg = render_config(engine="say", voice_name="Alex", backend="null")
    assert cfg["llm_provider"] == {"type": "null"}
    assert "summarizer" not in cfg  # no model to configure


def test_render_config_injects_no_pii():
    cfg = render_config(engine="edge-tts", voice_name="v", backend="ollama", model="m")
    text = json.dumps(cfg)
    for needle in PII_FORBIDDEN:
        assert needle not in text


def test_write_config_round_trips_and_is_private(tmp_path):
    cfg = render_config(engine="edge-tts", voice_name="v", backend="null")
    dest = tmp_path / "sub" / "config.json"
    write_config(cfg, dest)
    assert json.loads(dest.read_text()) == cfg
    # Config may carry an API key -> file is owner-only.
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600
