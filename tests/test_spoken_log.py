"""Tests for daemon/spoken_log.py — best-effort, bounded per-session spoken log.

All sync (the module does plain file I/O) so this runs in the all-sync
`make verify` gate.
"""
import daemon.spoken_log as sl


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(sl, "SPOKEN_DIR", tmp_path / "spoken")


def test_append_then_read_recent_newest_first(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    assert sl.append("first", session_id="s1", category="status")
    assert sl.append("second", session_id="s1", category="final_answer")
    recent = sl.read_recent("s1", limit=10)
    assert [r["text"] for r in recent] == ["second", "first"]  # newest first
    assert recent[0]["category"] == "final_answer"
    assert isinstance(recent[0]["ts"], float)


def test_append_empty_is_noop(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    assert sl.append("", session_id="s1") is False
    assert sl.append("   ", session_id="s1") is False
    assert sl.read_recent("s1") == []


def test_trim_caps_at_max_lines(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(sl, "MAX_LINES", 5)
    for i in range(20):
        sl.append(f"line-{i}", session_id="s1")
    recent = sl.read_recent("s1", limit=100)
    assert len(recent) == 5
    assert [r["text"] for r in recent] == [f"line-{i}" for i in (19, 18, 17, 16, 15)]


def test_append_never_raises_on_bad_dir(tmp_path, monkeypatch):
    # SPOKEN_DIR under a regular FILE → mkdir fails → swallowed, returns False.
    bad = tmp_path / "afile"
    bad.write_text("x")
    monkeypatch.setattr(sl, "SPOKEN_DIR", bad / "sub")
    assert sl.append("hi", session_id="s1") is False


def test_read_recent_skips_corrupt_lines(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    sl.SPOKEN_DIR.mkdir(parents=True, exist_ok=True)
    p = sl.session_path("s1")
    p.write_text(
        '{"ts":1,"text":"ok","category":null}\n'
        "NOT JSON\n"
        '{"ts":2,"text":"ok2","category":null}\n'
    )
    recent = sl.read_recent("s1", limit=10)
    assert [r["text"] for r in recent] == ["ok2", "ok"]  # corrupt line skipped


def test_read_latest_uses_most_recent_session(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    assert sl.latest_session_file() is None
    sl.append("hello", session_id="solo")
    assert sl.latest_session_file().stem == "solo"
    assert [r["text"] for r in sl.read_latest()] == ["hello"]
