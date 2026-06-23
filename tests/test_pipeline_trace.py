"""
DIAGNOSTIC: Full Pipeline Text Trace Test

This test captures text at EVERY stage of the TTS pipeline to identify
exactly WHERE contraction expansion occurs (if at all).

PURPOSE: Answer the critical question - "Where do contractions get expanded?"

Stages traced:
1. Original input (what Claude outputs)
2. After hook processing (enhanced_hook_integration.py)
3. After daemon text cleaning (tts_daemon.py)
4. After pipeline processing (process_stage.py)
5. Final text sent to TTS engine

HYPOTHESIS: Claude's original output may already contain expanded forms.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import re
import json
from datetime import datetime
from typing import Dict, List, Tuple

# Import all processing components
from daemon.pipeline import ProcessStage, SyncTextProcessor, IngestMessage
import time


class PipelineTracer:
    """Traces text through ALL processing stages with detailed logging."""

    # Contractions we're monitoring
    CONTRACTIONS = {
        "I'm": "I am",
        "I've": "I have",
        "I'll": "I will",
        "I'd": "I would",
        "you're": "you are",
        "you've": "you have",
        "you'll": "you will",
        "you'd": "you would",
        "we're": "we are",
        "we've": "we have",
        "we'll": "we will",
        "we'd": "we would",
        "they're": "they are",
        "they've": "they have",
        "they'll": "they will",
        "they'd": "they would",
        "it's": "it is",
        "that's": "that is",
        "what's": "what is",
        "who's": "who is",
        "where's": "where is",
        "there's": "there is",
        "here's": "here is",
        "don't": "do not",
        "doesn't": "does not",
        "didn't": "did not",
        "won't": "will not",
        "wouldn't": "would not",
        "couldn't": "could not",
        "shouldn't": "should not",
        "can't": "cannot",
        "isn't": "is not",
        "aren't": "are not",
        "wasn't": "was not",
        "weren't": "were not",
        "hasn't": "has not",
        "haven't": "have not",
        "hadn't": "had not",
        "let's": "let us",
    }

    def __init__(self):
        self.traces: List[Dict] = []
        self.process_stage = ProcessStage(chunk_size=150)
        self.sync_processor = SyncTextProcessor()

    def _analyze_text(self, text: str, stage: str) -> Dict:
        """Analyze text for contractions and expansions."""
        result = {
            "stage": stage,
            "text": text,
            "text_length": len(text),
            "contractions_found": [],
            "expansions_found": [],
            "analysis": ""
        }

        # Check for contractions (GOOD)
        for contraction, expanded in self.CONTRACTIONS.items():
            pattern = re.compile(re.escape(contraction), re.IGNORECASE)
            matches = pattern.findall(text)
            if matches:
                result["contractions_found"].extend(matches)

        # Check for expanded forms (BAD)
        for contraction, expanded in self.CONTRACTIONS.items():
            # Create word-boundary pattern for expanded form
            pattern = re.compile(r'\b' + re.escape(expanded) + r'\b', re.IGNORECASE)
            matches = pattern.findall(text)
            if matches:
                result["expansions_found"].extend(matches)

        # Analyze
        if result["expansions_found"] and not result["contractions_found"]:
            result["analysis"] = "❌ ONLY EXPANSIONS - text has expanded forms only"
        elif result["expansions_found"] and result["contractions_found"]:
            result["analysis"] = "⚠️ MIXED - both contractions and expansions present"
        elif result["contractions_found"]:
            result["analysis"] = "✅ GOOD - contractions preserved"
        else:
            result["analysis"] = "ℹ️ NONE - no contractions or expansions detected"

        return result

    def trace_pipeline(self, input_text: str) -> List[Dict]:
        """Trace text through all pipeline stages."""
        traces = []

        # Stage 0: Original Input
        traces.append(self._analyze_text(input_text, "0. ORIGINAL INPUT"))

        # Stage 1: SyncTextProcessor.clean_text
        sync_cleaned = self.sync_processor.clean_text(input_text)
        traces.append(self._analyze_text(sync_cleaned, "1. SyncTextProcessor.clean_text()"))

        # Stage 2: SyncTextProcessor.restore_contractions (isolated)
        restored = self.sync_processor.restore_contractions(input_text)
        traces.append(self._analyze_text(restored, "2. SyncTextProcessor.restore_contractions()"))

        # Stage 3: Full sync process
        cleaned, chunks = self.sync_processor.process_full(input_text)
        traces.append(self._analyze_text(cleaned, "3. SyncTextProcessor.process_full() - cleaned"))
        traces.append(self._analyze_text(" ".join(chunks), "4. SyncTextProcessor.process_full() - chunks joined"))

        return traces

    def print_trace_report(self, traces: List[Dict], title: str = "Pipeline Trace Report"):
        """Print formatted trace report."""
        print("\n" + "=" * 80)
        print(f"  {title}")
        print("=" * 80)

        for trace in traces:
            print(f"\n{'─' * 60}")
            print(f"STAGE: {trace['stage']}")
            print(f"{'─' * 60}")
            print(f"Text ({trace['text_length']} chars): {trace['text'][:200]}{'...' if len(trace['text']) > 200 else ''}")
            print(f"\nContractions found: {trace['contractions_found'] or 'None'}")
            print(f"Expansions found: {trace['expansions_found'] or 'None'}")
            print(f"\nAnalysis: {trace['analysis']}")

        print("\n" + "=" * 80)


def test_with_contractions():
    """Test: Input WITH contractions - they must NEVER be expanded."""
    tracer = PipelineTracer()

    input_text = (
        "I'm going to show you what's possible. We've been working on this, "
        "and they'll love it. Don't worry about the details - it isn't hard. "
        "That's exactly what I've been saying. Let's do this!"
    )

    traces = tracer.trace_pipeline(input_text)
    tracer.print_trace_report(traces, "TEST: Contractions → Must Stay Contracted")

    # CRITICAL ASSERTIONS
    for trace in traces:
        if trace["expansions_found"]:
            print(f"\n🚨 FAILURE at stage '{trace['stage']}': Contractions were EXPANDED!")
            print(f"   Expansions found: {trace['expansions_found']}")
            return False

    print("\n✅ SUCCESS: All contractions preserved through pipeline")
    return True


def test_with_expansions():
    """Test: Input WITH expansions - they must be CONTRACTED."""
    tracer = PipelineTracer()

    input_text = (
        "I am going to show you what is possible. We have been working on this, "
        "and they will love it. Do not worry about the details - it is not hard. "
        "That is exactly what I have been saying. Let us do this!"
    )

    traces = tracer.trace_pipeline(input_text)
    tracer.print_trace_report(traces, "TEST: Expansions → Must Be Contracted")

    # Check final stage
    final_trace = traces[-1]
    if final_trace["expansions_found"] and not final_trace["contractions_found"]:
        print(f"\n🚨 FAILURE: Expansions were NOT contracted!")
        return False
    elif final_trace["contractions_found"]:
        print("\n✅ SUCCESS: Expansions were contracted to natural speech")
        return True
    else:
        print("\n⚠️ UNCLEAR: Could not determine contraction status")
        return None


def test_mixed_input():
    """Test: Mixed input with both contractions and expansions."""
    tracer = PipelineTracer()

    input_text = (
        "I'm happy to help, but I do not understand what you mean. "
        "It's confusing because we have never seen this before. "
        "Don't worry though - that is what we are here for!"
    )

    traces = tracer.trace_pipeline(input_text)
    tracer.print_trace_report(traces, "TEST: Mixed Input")

    # Final stage should have contractions and NO expansions
    final_trace = traces[-1]
    if final_trace["expansions_found"]:
        print(f"\n🚨 FAILURE: Expansions remain in final output!")
        print(f"   Remaining expansions: {final_trace['expansions_found']}")
        return False

    print("\n✅ SUCCESS: All forms contracted in final output")
    return True


def test_real_claude_output_simulation():
    """
    Test: Simulate typical Claude assistant output.

    NOTE: This simulates what Claude MIGHT output. The actual Claude output
    needs to be captured from LIVE sessions to verify the hypothesis.
    """
    tracer = PipelineTracer()

    # Typical Claude response patterns (with expanded forms - common in AI output)
    input_text = (
        "I would be happy to help you with that. Here is what you need to know: "
        "First, I am going to explain the concept. It is actually quite simple once "
        "you understand the basics. We have covered similar topics before. "
        "Do not hesitate to ask if you are not sure about anything. "
        "I am confident you will find this helpful!"
    )

    traces = tracer.trace_pipeline(input_text)
    tracer.print_trace_report(traces, "TEST: Simulated Claude Output (Expanded Forms)")

    # This tests the restoration pipeline
    final_trace = traces[-1]
    contracted_forms = ["I'd", "I'm", "it's", "We've", "Don't", "you're", "I'm"]

    found_contractions = sum(1 for c in final_trace["contractions_found"] if c)
    remaining_expansions = len(final_trace["expansions_found"])

    print(f"\nResults: {found_contractions} contractions, {remaining_expansions} expansions remaining")

    if remaining_expansions > 0:
        print(f"🚨 FAILURE: {remaining_expansions} expanded forms not contracted!")
        return False

    print("✅ SUCCESS: All AI output expanded forms were contracted")
    return True


def run_all_tests():
    """Run all diagnostic tests."""
    print("\n" + "█" * 80)
    print("  PIPELINE CONTRACTION TRACE DIAGNOSTIC")
    print("  Testing where contractions get expanded (if at all)")
    print("█" * 80)

    results = []

    print("\n\n[1/4] Testing input WITH contractions...")
    results.append(("Contractions preserved", test_with_contractions()))

    print("\n\n[2/4] Testing input WITH expansions...")
    results.append(("Expansions contracted", test_with_expansions()))

    print("\n\n[3/4] Testing mixed input...")
    results.append(("Mixed input handled", test_mixed_input()))

    print("\n\n[4/4] Testing simulated Claude output...")
    results.append(("Claude output contracted", test_real_claude_output_simulation()))

    # Summary
    print("\n" + "█" * 80)
    print("  SUMMARY")
    print("█" * 80)

    for name, passed in results:
        status = "✅ PASS" if passed else "❌ FAIL" if passed is False else "⚠️ UNCLEAR"
        print(f"  {status}: {name}")

    all_passed = all(r for _, r in results if r is not None)

    print("\n" + "█" * 80)
    if all_passed:
        print("  CONCLUSION: Pipeline correctly handles contractions")
        print("  If contractions are still expanded in production, the source")
        print("  is BEFORE this pipeline (i.e., Claude's original output)")
    else:
        print("  CONCLUSION: Pipeline has issues - see failed tests above")
    print("█" * 80 + "\n")

    return all_passed


if __name__ == "__main__":
    run_all_tests()
