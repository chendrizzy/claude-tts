"""Prompt-echo guard for the TTS summarizer.

The summarizer model (a tiny instruct LLM, e.g. qwen2.5-coder:1.5b) sometimes
ECHOES its own summarize-prompt rule block instead of summarizing the content —
and that echo was being spoken aloud verbatim. _looks_like_prompt_echo() detects
it so _call_ollama falls back to the deterministic rule-based summary. Pure
string logic, no Ollama needed, so it runs in the make verify gate.
"""
import daemon.ollama_summarizer as s


def test_detects_the_reported_spoken_echo():
    # The exact text the daemon was caught speaking.
    echo = ('No "Here\'s", no "The output shows", no preamble. '
            'First person ("I found"), never "we"')
    assert s._looks_like_prompt_echo(echo) is True


def test_detects_full_rule_block_echo():
    block = ('Speak the substance directly. No "Here is", no preamble.\n'
             'First person, never "we". Three sentences max. Understated tone.')
    assert s._looks_like_prompt_echo(block) is True


def test_detects_scaffolding_echo():
    assert s._looks_like_prompt_echo("CATEGORY: status\nCONTENT: the tests passed") is True


def test_real_summaries_are_not_flagged():
    for ok in [
        "I found 3 errors in the auth module.",
        "the router tests: 23 passed, 4 failed.",
        "Disk guard refuses synthesis below 200 megabytes.",
        "The cache cleanup loop runs every 10 minutes now.",
    ]:
        assert s._looks_like_prompt_echo(ok) is False, ok


def test_single_signature_is_not_enough():
    # One stray phrase must not trip it — needs >= 2 distinct signatures.
    assert s._looks_like_prompt_echo("I'll summarize this in first person.") is False
    assert s._looks_like_prompt_echo("") is False


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
