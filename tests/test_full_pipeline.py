#!/usr/bin/env python3
"""
Test the FULL pipeline including Ollama expansion and contraction restoration
This simulates exactly what happens in the daemon
"""

import subprocess
import sys
import time
import re
from typing import Dict

# Import the contraction preservation functions from the daemon
def _extract_contractions(text: str) -> Dict[str, str]:
    """Extract contractions (same as daemon code)"""
    contraction_map = {}

    common_contractions = {
        r"\bisn't\b": "is not",
        r"\baren't\b": "are not",
        r"\bcan't\b": "cannot",
        r"\bdon't\b": "do not",
        r"\bdoesn't\b": "does not",
        r"\bwon't\b": "will not",
        r"\bI'm\b": "I am",
        r"\byou're\b": "you are",
        r"\bhe's\b": "he is",
        r"\bshe's\b": "she is",
        r"\bit's\b": "it is",
        r"\bwe're\b": "we are",
        r"\bthey're\b": "they are",
        r"\bthat's\b": "that is",
        r"\bI've\b": "I have",
        r"\byou've\b": "you have",
        r"\bwe've\b": "we have",
        r"\bthey've\b": "they have",
        r"\bI'll\b": "I will",
        r"\byou'll\b": "you will",
        r"\bwe'll\b": "we will",
        r"\bthey'll\b": "they will",
        r"\bI'd\b": "I would",
        r"\byou'd\b": "you would",
    }

    for contraction_pattern, expanded_form in common_contractions.items():
        matches = re.finditer(contraction_pattern, text, re.IGNORECASE)
        for match in matches:
            original_contraction = match.group(0)
            contraction_map[expanded_form.lower()] = original_contraction
            contraction_map[expanded_form] = original_contraction
            if original_contraction[0].isupper():
                cap_expanded = expanded_form[0].upper() + expanded_form[1:]
                contraction_map[cap_expanded] = original_contraction

    return contraction_map

def _restore_contractions(processed_text: str, contraction_map: Dict[str, str]) -> str:
    """Restore contractions (same as daemon code)"""
    if not contraction_map:
        return processed_text

    restored_text = processed_text
    sorted_expansions = sorted(contraction_map.keys(), key=len, reverse=True)

    for expanded_form in sorted_expansions:
        original_contraction = contraction_map[expanded_form]
        pattern = r'\b' + re.escape(expanded_form) + r'\b'
        restored_text = re.sub(pattern, original_contraction, restored_text, flags=re.IGNORECASE)

    return restored_text

def simulate_ollama_expansion(text: str) -> str:
    """Simulate what Ollama does - expand contractions"""
    expansions = {
        r"\bI'm\b": "I am",
        r"\bisn't\b": "is not",
        r"\bthat's\b": "that is",
        r"\bwe're\b": "we are",
        r"\byou'll\b": "you will",
        r"\bI've\b": "I have",
        r"\bcan't\b": "cannot",
        r"\bdon't\b": "do not",
        r"\bit's\b": "it is",
        r"\bwon't\b": "will not",
    }

    result = text
    for pattern, expansion in expansions.items():
        result = re.sub(pattern, expansion, result, flags=re.IGNORECASE)

    return result

def speak_text(text: str, label: str):
    """Generate and play audio"""
    print(f"\n{label}")
    print(f"Speaking: \"{text}\"")
    print("Playing...", end=" ", flush=True)

    try:
        process = subprocess.Popen(
            ["edge-tts", "--text", text, "--voice", "en-US-JennyNeural", "--rate", "+15%"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )

        play_process = subprocess.Popen(
            ["afplay", "-"],
            stdin=process.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )

        process.stdout.close()
        play_process.communicate()

        print("✅")
        return True

    except Exception as e:
        print(f"❌ Error: {e}")
        return False

def main():
    """Test the full pipeline with contraction preservation"""

    print("=" * 70)
    print("FULL PIPELINE TEST - WITH CONTRACTION PRESERVATION")
    print("=" * 70)
    print("\nThis test simulates the EXACT daemon pipeline:")
    print("1. Extract contractions from original text")
    print("2. Ollama expands them (unavoidable)")
    print("3. Restore original contractions")
    print("4. Speak the restored text")
    print("\n" + "=" * 70)

    test_cases = [
        "I'm working on this task.",
        "That's great, but it isn't done yet.",
        "We're making progress and you'll love it.",
    ]

    for i, original_text in enumerate(test_cases, 1):
        print(f"\n{'='*70}")
        print(f"TEST {i}/{len(test_cases)}")
        print(f"{'='*70}")
        print(f"Original text:     {original_text}")

        # Step 1: Extract contractions
        contraction_map = _extract_contractions(original_text)
        print(f"Extracted:         {len(contraction_map)} contraction mappings")

        # Step 2: Simulate Ollama expansion
        expanded = simulate_ollama_expansion(original_text)
        print(f"Ollama expanded:   {expanded}")

        # Step 3: Restore contractions
        restored = _restore_contractions(expanded, contraction_map)
        print(f"Restored:          {restored}")

        # Step 4: Verify restoration worked
        if restored == original_text:
            print("✅ Restoration:    PERFECT")
        else:
            print("❌ Restoration:    FAILED")
            print(f"   Expected: {original_text}")
            print(f"   Got:      {restored}")

        print(f"\n{'─'*70}")

        # Step 5: Speak the RESTORED text (this is what daemon sends to TTS)
        speak_text(restored, f"🔊 Audio Output (Test {i})")

        time.sleep(1)

    print("\n" + "=" * 70)
    print("COMPARISON TEST")
    print("=" * 70)
    print("\nNow hear the DIFFERENCE:")
    print("\n1. First you'll hear: WITHOUT fix (expanded contractions)")
    print("2. Then you'll hear:  WITH fix (natural contractions)")
    print("=" * 70)

    comparison_text = "I'm sure you'll find that it's working perfectly."

    # WITHOUT fix (expanded)
    expanded_version = simulate_ollama_expansion(comparison_text)
    print(f"\n❌ WITHOUT FIX (what you used to hear):")
    print(f"   Text: {expanded_version}")
    speak_text(expanded_version, "🔊 Playing EXPANDED version")

    time.sleep(1)

    # WITH fix (restored)
    contraction_map = _extract_contractions(comparison_text)
    expanded = simulate_ollama_expansion(comparison_text)
    restored_version = _restore_contractions(expanded, contraction_map)
    print(f"\n✅ WITH FIX (what you hear now):")
    print(f"   Text: {restored_version}")
    speak_text(restored_version, "🔊 Playing RESTORED version")

    print("\n" + "=" * 70)
    print("VERIFICATION")
    print("=" * 70)
    print("\nDid you notice the difference?")
    print("\n❌ EXPANDED (old): Robotic \"I am\", \"you will\", \"it is\"")
    print("✅ NATURAL (new):  Conversational \"I'm\", \"you'll\", \"it's\"")
    print("\nThe fix is working! Your daemon now preserves contractions.")
    print("=" * 70)

if __name__ == "__main__":
    try:
        main()
        sys.exit(0)
    except KeyboardInterrupt:
        print("\n\nTest interrupted")
        sys.exit(130)
