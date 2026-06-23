"""
AUDIO-02: Single-spawner invariant regression test.

Verifies that there is exactly ONE callsite in daemon/ that spawns an afplay
subprocess.  This is enforced by deletion, not by a runtime lock.

A second afplay spawn site would mean:
  - Two independent audio paths with no shared lock semantics
  - Possible concurrent afplay instances (cross-session overlap)
  - Two places to maintain watchdog/cancel/FD-cleanup logic

The canonical site is PlaybackStage._play_audio_inner in
daemon/pipeline/playback_stage.py.  All legacy sites in tts_daemon.py
(osascript/afplay/say inside speak_text) were deleted in Phase 3 LEGACY-03/04.
"""
import ast
import os
import re
from pathlib import Path
from typing import List, Tuple


DAEMON_DIR = Path(__file__).parent.parent / "daemon"

# Pattern that matches afplay used as a subprocess command argument
# We check for string literals 'afplay' that appear as the first argument
# to asyncio.create_subprocess_exec or subprocess.Popen/run/call.
AFPLAY_SUBPROCESS_RE = re.compile(
    r"""(create_subprocess_exec|subprocess\.(Popen|run|call|check_call|check_output))\s*\(\s*['"]afplay['"]""",
    re.MULTILINE,
)

# Also check shell=True invocations that interpolate afplay
AFPLAY_SHELL_RE = re.compile(
    r"""(do shell script|shell\s*=\s*True).*afplay""",
    re.MULTILINE,
)


def collect_daemon_py_files() -> List[Path]:
    """Return all .py files under daemon/ excluding __pycache__."""
    files = []
    for root, dirs, fnames in os.walk(DAEMON_DIR):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for fname in fnames:
            if fname.endswith(".py"):
                files.append(Path(root) / fname)
    return sorted(files)


def find_afplay_spawn_sites(files: List[Path]) -> List[Tuple[Path, int, str]]:
    """Return (file, lineno, snippet) for every afplay spawn found.

    We look for 'afplay' as a bare string literal that appears as the first
    positional argument to a subprocess-creation call.  The argument may be
    on the same line as the call or on the next line (multi-line call style).

    Strategy: scan the full source text with a window of ±1 lines so we
    catch both single-line and multi-line call styles.
    """
    hits = []
    # Match 'afplay' as a standalone string literal on a non-comment line
    # that is adjacent to (within 3 lines of) a subprocess-creation call.
    spawn_call_re = re.compile(
        r"create_subprocess_exec|subprocess\.(Popen|run|call|check_call|check_output)"
    )
    afplay_arg_re = re.compile(r"""^\s*['"]afplay['"]\s*,?""")

    for path in files:
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        lines = source.splitlines()
        for lineno, line in enumerate(lines, start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue

            # Case 1: spawn call and 'afplay' on the same line
            if spawn_call_re.search(line) and "'afplay'" in line or '"afplay"' in line:
                if spawn_call_re.search(line):
                    hits.append((path, lineno, stripped))
                    continue

            # Case 2: 'afplay' is the first argument on the next line(s) after
            # a create_subprocess_exec call — look back up to 2 lines
            if afplay_arg_re.match(stripped):
                window_start = max(0, lineno - 3)
                preceding = "\n".join(lines[window_start : lineno - 1])
                if spawn_call_re.search(preceding):
                    # Find the create_subprocess_exec line number
                    for back_lineno, back_line in enumerate(
                        lines[window_start : lineno - 1],
                        start=window_start + 1,
                    ):
                        if spawn_call_re.search(back_line) and not back_line.strip().startswith("#"):
                            hits.append((path, back_lineno, back_line.strip()))
                            break

    # Deduplicate (same file + lineno may appear once)
    seen = set()
    unique_hits = []
    for h in hits:
        key = (h[0], h[1])
        if key not in seen:
            seen.add(key)
            unique_hits.append(h)
    return unique_hits


def test_exactly_one_afplay_spawn_site():
    """AUDIO-02: exactly one afplay spawn site in daemon/."""
    files = collect_daemon_py_files()
    assert files, "No Python files found in daemon/ — check DAEMON_DIR path"

    hits = find_afplay_spawn_sites(files)

    # Build human-readable report for assertion message
    report_lines = []
    for path, lineno, snippet in hits:
        rel = path.relative_to(DAEMON_DIR.parent)
        report_lines.append(f"  {rel}:{lineno}: {snippet}")
    report = "\n".join(report_lines)

    assert len(hits) == 1, (
        f"Expected exactly 1 afplay spawn site in daemon/, found {len(hits)}:\n"
        f"{report}\n\n"
        "The canonical site must be PlaybackStage._play_audio_inner in "
        "daemon/pipeline/playback_stage.py.  All legacy sites must be deleted."
    )

    # Assert the one hit is in the expected file
    expected_file = DAEMON_DIR / "pipeline" / "playback_stage.py"
    actual_file = hits[0][0]
    assert actual_file == expected_file, (
        f"Expected the single afplay spawn to be in {expected_file}, "
        f"but found it in {actual_file}:{hits[0][1]}"
    )


def test_no_speak_command_in_daemon():
    """LEGACY-03: the 'speak' command handler is gone from handle_client."""
    tts_daemon_py = DAEMON_DIR / "tts_daemon.py"
    assert tts_daemon_py.exists(), f"tts_daemon.py not found at {tts_daemon_py}"

    source = tts_daemon_py.read_text(encoding="utf-8")

    # grep for the dispatch pattern — must not exist outside comments
    hits = []
    for lineno, line in enumerate(source.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if re.search(r"""command\s*==\s*['"]speak['"]""", line):
            hits.append(f"  line {lineno}: {stripped}")

    assert not hits, (
        "Found 'speak' command dispatch in tts_daemon.py (LEGACY-03 violation):\n"
        + "\n".join(hits)
    )


def test_legacy_files_deleted():
    """LEGACY-05/06: deleted files must not exist in the daemon directory."""
    deleted = [
        DAEMON_DIR / "enhanced_hook_integration.py",
        DAEMON_DIR / "intelligent_tts_filter.py",
        DAEMON_DIR / "tts_output_filter.py",
    ]
    still_present = [str(p) for p in deleted if p.exists()]
    assert not still_present, (
        "Legacy files that should have been deleted still exist:\n"
        + "\n".join(f"  {p}" for p in still_present)
    )


def test_no_session_queue_class():
    """LEGACY-04: SessionQueue class must not be importable from tts_daemon."""
    tts_daemon_py = DAEMON_DIR / "tts_daemon.py"
    source = tts_daemon_py.read_text(encoding="utf-8")

    # Check that 'class SessionQueue' does not appear as live code
    for lineno, line in enumerate(source.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if re.match(r"class\s+SessionQueue\s*[:(]", stripped):
            raise AssertionError(
                f"SessionQueue class definition found at tts_daemon.py:{lineno} "
                "(LEGACY-04 violation)"
            )


def test_no_fcntl_import_in_playback_stage():
    """LEGACY-08: fcntl must not be imported in playback_stage.py."""
    ps_py = DAEMON_DIR / "pipeline" / "playback_stage.py"
    assert ps_py.exists(), f"playback_stage.py not found at {ps_py}"

    source = ps_py.read_text(encoding="utf-8")
    for lineno, line in enumerate(source.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if re.search(r"\bimport\s+fcntl\b", line):
            raise AssertionError(
                f"fcntl import found at playback_stage.py:{lineno} "
                "(LEGACY-08 violation — fcntl tier must be deleted)"
            )


def test_no_speak_text_method():
    """LEGACY-04: speak_text method must not exist in tts_daemon.py."""
    tts_daemon_py = DAEMON_DIR / "tts_daemon.py"
    source = tts_daemon_py.read_text(encoding="utf-8")

    for lineno, line in enumerate(source.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if re.match(r"def\s+speak_text\s*\(", stripped):
            raise AssertionError(
                f"speak_text method found at tts_daemon.py:{lineno} "
                "(LEGACY-04 violation)"
            )


if __name__ == "__main__":
    import sys

    tests = [
        test_exactly_one_afplay_spawn_site,
        test_no_speak_command_in_daemon,
        test_legacy_files_deleted,
        test_no_session_queue_class,
        test_no_fcntl_import_in_playback_stage,
        test_no_speak_text_method,
    ]
    failed = []
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            print(f"FAIL  {t.__name__}: {e}")
            failed.append(t.__name__)
        except Exception as e:
            print(f"ERROR {t.__name__}: {e}")
            failed.append(t.__name__)

    if failed:
        print(f"\n{len(failed)} test(s) failed: {failed}")
        sys.exit(1)
    else:
        print(f"\nAll {len(tests)} tests passed.")
