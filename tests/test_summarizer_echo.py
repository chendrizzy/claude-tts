"""Prompt-echo guard for the TTS summarizer.

The summarizer model (a tiny instruct LLM, e.g. qwen2.5-coder:1.5b) sometimes
ECHOES its own summarize-prompt rule block instead of summarizing the content —
and that echo was being spoken aloud verbatim. _looks_like_prompt_echo() detects
it so _call_ollama falls back to the deterministic rule-based summary. Pure
string logic, no Ollama needed, so it runs in the make verify gate.
"""
import asyncio

import daemon.ollama_summarizer as s
from daemon.ollama_summarizer import OllamaSummarizer
from daemon.tts_types import Category


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


# --------------------------------------------------------------------------- #
# Mid-sentence cutoff guard (num_predict cap on the summarizer)               #
#                                                                             #
# The summarizer hard-stops generation at _SUMMARIZE_HARD_TOKENS output      #
# tokens; a longer summary comes back cut mid-sentence with no terminal       #
# punctuation, and was being spoken/logged as a dangling fragment ("...so     #
# I'd"). The fix trims to the last complete sentence, or falls back if        #
# nothing survived.                                                           #
# --------------------------------------------------------------------------- #


class _FakeClient:
    """Minimal OllamaClient stand-in: returns one programmed string and records
    the num_predict (max_tokens) budget each call was made with."""

    def __init__(self, text):
        self._text = text
        self.calls = []

    def generate_response(self, prompt, model=None, max_tokens=120,
                          temperature=0.3, keep_alive=None):
        self.calls.append({"max_tokens": max_tokens})
        return self._text


def _run(coro):
    return asyncio.run(coro)


def test_complete_sentence_unchanged():
    txt = "I fixed the auth module. The tests pass now."
    assert s._last_complete_sentence(txt) == txt


def test_question_and_bang_count_as_complete():
    assert s._last_complete_sentence("Did the build pass?") == "Did the build pass?"
    assert s._last_complete_sentence("The release shipped!") == "The release shipped!"


def test_short_complete_summary_preserved():
    # A short but COMPLETE summary keeps its full text (stub guard is trim-only).
    assert s._last_complete_sentence("Done.") == "Done."


def test_dangling_tail_trimmed_to_last_boundary():
    capped = "I fixed the auth module. The tests pass now. Then I started refacto"
    assert (s._last_complete_sentence(capped)
            == "I fixed the auth module. The tests pass now.")


def test_no_complete_sentence_returns_empty():
    # The exact mid-clause fragment from a field-captured spoken log (Record A).
    frag = ("Insight: The fixes above are uncommitted and ready when you want "
            "them (note pnpm-workspace dot yaml is tangled with your in-progress "
            "pnpm migration, so I'd")
    assert s._last_complete_sentence(frag) == ""


def test_short_stub_is_rejected():
    # Sub-40-char first sentence + dangling tail -> fall back, don't speak a stub.
    assert s._last_complete_sentence("Ok. Then I started doing the thing that got cut") == ""


def test_summarize_falls_back_when_capped_midclause():
    # No terminal punctuation anywhere -> the raw fragment must NOT be spoken;
    # summarize() returns the deterministic rule-based summary of the content.
    frag = "Insight: The fixes are uncommitted and ready, so I'd"
    content = ("I made several uncommitted fixes; the pnpm-workspace file is "
               "tangled with the in-progress migration.")
    summ = OllamaSummarizer(_FakeClient(frag), model="qwen2.5-coder:1.5b", timeout_s=2.0)
    out = _run(summ.summarize(content, Category.INSIGHT))
    assert out and not out.endswith("so I'd")
    assert out == s.rule_based_summary(content)


def test_summarize_trims_dangling_tail():
    capped = "I fixed the auth module. The tests pass now. Then I started refacto"
    summ = OllamaSummarizer(_FakeClient(capped), timeout_s=2.0)
    out = _run(summ.summarize("auth work", Category.STATUS))
    assert out == "I fixed the auth module. The tests pass now."


def test_judge_token_is_never_trimmed():
    # Regression guard: the binary judge (allow_fallback=False) returns a bare
    # SPEAK/SKIP token with no punctuation. The completeness trim must NOT touch
    # it — trimming "SPEAK" to "" would make the daemon go mute.
    summ = OllamaSummarizer(_FakeClient("SPEAK"), timeout_s=2.0)
    out = _run(summ.summarize("anything", Category.STATUS, allow_fallback=False))
    assert out == "SPEAK"


# --- soft/slack/hard output-token budget ---

def test_summary_uses_soft_plus_slack_budget():
    # The summary path requests num_predict == soft + slack (the hard cutoff),
    # giving the model room to finish its thought instead of cutting at 120.
    c = _FakeClient("All good. The tests pass.")
    summ = OllamaSummarizer(c, soft_tokens=200, slack_tokens=96, timeout_s=2.0)
    _run(summ.summarize("some content to summarize", Category.STATUS))
    assert c.calls[0]["max_tokens"] == 296


def test_budget_is_configurable():
    # soft/slack are knobs: the hard cutoff tracks them.
    c = _FakeClient("Done here. Shipped it.")
    summ = OllamaSummarizer(c, soft_tokens=120, slack_tokens=40, timeout_s=2.0)
    _run(summ.summarize("content", Category.STATUS))
    assert c.calls[0]["max_tokens"] == 160
    assert summ._hard_tokens == 160


def test_judge_uses_small_token_budget():
    # The judge must stay cheap — a tiny budget, not the full summary budget.
    c = _FakeClient("SPEAK")
    summ = OllamaSummarizer(c, soft_tokens=200, slack_tokens=96, timeout_s=2.0)
    _run(summ.summarize("x", Category.STATUS, allow_fallback=False))
    assert c.calls[0]["max_tokens"] == s._JUDGE_MAX_TOKENS
    assert s._JUDGE_MAX_TOKENS < 296


def test_default_budget_matches_module_constants():
    c = _FakeClient("Ok. Fine.")
    summ = OllamaSummarizer(c, timeout_s=2.0)
    _run(summ.summarize("content", Category.STATUS))
    assert c.calls[0]["max_tokens"] == s._SUMMARIZE_HARD_TOKENS
    assert s._SUMMARIZE_HARD_TOKENS == s._SUMMARIZE_SOFT_TOKENS + s._SUMMARIZE_SLACK_TOKENS


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
