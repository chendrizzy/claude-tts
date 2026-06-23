"""
Process Stage - Async text preprocessing pipeline with chunking for streaming TTS.

Runs CPU-bound text cleaning in thread pool to avoid blocking the event loop.
Includes contraction RESTORATION (not expansion!) for natural speech output.

CRITICAL: This stage CONTRACTS expanded forms → natural contractions
  "I am" → "I'm" (CORRECT)
  NOT "I'm" → "I am" (WRONG - never expand contractions!)
"""

import asyncio
import re
from functools import lru_cache
from typing import AsyncIterator, Optional
from dataclasses import dataclass
import time
import logging

from .ingest_stage import IngestMessage

logger = logging.getLogger(__name__)


@dataclass
class ProcessedMessage:
    """Message after text preprocessing."""
    original: str
    cleaned: str
    chunks: list[str]
    session_id: str
    priority: int
    request_id: str
    processing_time_ms: float


class ProcessStage:
    """
    Async text preprocessing pipeline with chunking for streaming TTS.

    Key features:
    - CPU-bound ops run in thread pool via run_in_executor
    - Contraction RESTORATION for natural speech (NOT expansion!)
    - Text chunking for streaming TTS generation
    - Precompiled regex patterns for performance
    """

    # Precompiled patterns for performance
    _CODE_BLOCK_PATTERN = re.compile(r'```[\s\S]*?```', re.MULTILINE)
    _INLINE_CODE_PATTERN = re.compile(r'`[^`]+`')
    _EMOJI_PATTERN = re.compile(
        "["
        "\U0001F1E0-\U0001F1FF"  # flags
        "\U0001F300-\U0001F5FF"  # symbols
        "\U0001F600-\U0001F64F"  # emoticons
        "\U0001F680-\U0001F6FF"  # transport
        "\U0001F700-\U0001F77F"  # alchemical
        "\U0001F780-\U0001F7FF"  # geometric
        "\U0001F800-\U0001F8FF"  # arrows
        "\U0001F900-\U0001F9FF"  # supplemental
        "\U0001FA00-\U0001FA6F"  # chess
        "\U0001FA70-\U0001FAFF"  # symbols
        "\U00002702-\U000027B0"  # dingbats
        "]+",
        flags=re.UNICODE
    )

    # CONTRACTION RESTORATION: Map expanded forms → natural contractions
    # CRITICAL: We CONTRACT expanded forms, NOT expand contractions!
    _EXPANDED_TO_CONTRACTED = {
        # Negative contractions (highest priority - most common in AI output)
        r"\bdo not\b": "don't",
        r"\bdoes not\b": "doesn't",
        r"\bdid not\b": "didn't",
        r"\bwill not\b": "won't",
        r"\bwould not\b": "wouldn't",
        r"\bcould not\b": "couldn't",
        r"\bshould not\b": "shouldn't",
        r"\bmight not\b": "mightn't",
        r"\bmust not\b": "mustn't",
        r"\bneed not\b": "needn't",
        r"\bcannot\b": "can't",
        r"\bcan not\b": "can't",
        r"\bis not\b": "isn't",
        r"\bare not\b": "aren't",
        r"\bwas not\b": "wasn't",
        r"\bwere not\b": "weren't",
        r"\bhas not\b": "hasn't",
        r"\bhave not\b": "haven't",
        r"\bhad not\b": "hadn't",

        # Pronoun + be contractions
        r"\bI am\b": "I'm",
        r"\byou are\b": "you're",
        r"\bhe is\b": "he's",
        r"\bshe is\b": "she's",
        r"\bit is\b": "it's",
        r"\bwe are\b": "we're",
        r"\bthey are\b": "they're",
        r"\bthat is\b": "that's",
        r"\bwhat is\b": "what's",
        r"\bwho is\b": "who's",
        r"\bwhere is\b": "where's",
        r"\bwhen is\b": "when's",
        r"\bwhy is\b": "why's",
        r"\bhow is\b": "how's",
        r"\bthere is\b": "there's",
        r"\bhere is\b": "here's",

        # Pronoun + will contractions
        r"\bI will\b": "I'll",
        r"\byou will\b": "you'll",
        r"\bhe will\b": "he'll",
        r"\bshe will\b": "she'll",
        r"\bit will\b": "it'll",
        r"\bwe will\b": "we'll",
        r"\bthey will\b": "they'll",
        r"\bthat will\b": "that'll",
        r"\bwho will\b": "who'll",

        # Pronoun + would contractions
        r"\bI would\b": "I'd",
        r"\byou would\b": "you'd",
        r"\bhe would\b": "he'd",
        r"\bshe would\b": "she'd",
        r"\bit would\b": "it'd",
        r"\bwe would\b": "we'd",
        r"\bthey would\b": "they'd",
        r"\bthat would\b": "that'd",

        # Pronoun + had contractions (same as would but context-dependent)
        r"\bI had\b": "I'd",
        r"\byou had\b": "you'd",
        r"\bhe had\b": "he'd",
        r"\bshe had\b": "she'd",
        r"\bwe had\b": "we'd",
        r"\bthey had\b": "they'd",

        # Pronoun + have contractions
        r"\bI have\b": "I've",
        r"\byou have\b": "you've",
        r"\bwe have\b": "we've",
        r"\bthey have\b": "they've",
        r"\bcould have\b": "could've",
        r"\bwould have\b": "would've",
        r"\bshould have\b": "should've",
        r"\bmight have\b": "might've",
        r"\bmust have\b": "must've",

        # Let us
        r"\blet us\b": "let's",
    }

    def __init__(self, chunk_size: int = 150):
        self.chunk_size = chunk_size
        self._stats = {
            'messages_processed': 0,
            'total_processing_time_ms': 0.0,
            'chunks_created': 0,
        }

        # Pre-compile contraction patterns (longest first for correct matching)
        self._compiled_patterns = [
            (re.compile(pattern, re.IGNORECASE), contraction)
            for pattern, contraction in sorted(
                self._EXPANDED_TO_CONTRACTED.items(),
                key=lambda x: len(x[0]), reverse=True
            )
        ]

    async def process(self, message: IngestMessage) -> ProcessedMessage:
        """
        Process message through async pipeline.

        Runs CPU-bound text cleaning in thread pool to avoid blocking.
        """
        start_time = time.time()

        # Run CPU-bound operations in thread pool
        loop = asyncio.get_event_loop()
        cleaned = await loop.run_in_executor(
            None, self._clean_text_sync, message.content
        )

        # Chunk for streaming TTS
        chunks = await loop.run_in_executor(
            None, self._chunk_text, cleaned
        )

        processing_time_ms = (time.time() - start_time) * 1000

        # Update stats
        self._stats['messages_processed'] += 1
        self._stats['total_processing_time_ms'] += processing_time_ms
        self._stats['chunks_created'] += len(chunks)

        logger.debug(
            f"Processed message {message.request_id} in {processing_time_ms:.1f}ms "
            f"({len(chunks)} chunks)"
        )

        return ProcessedMessage(
            original=message.content,
            cleaned=cleaned,
            chunks=chunks,
            session_id=message.session_id,
            priority=message.priority,
            request_id=message.request_id,
            processing_time_ms=processing_time_ms
        )

    def _clean_text_sync(self, text: str) -> str:
        """
        Synchronous text cleaning (runs in thread pool).

        Pipeline order:
        1. Markdown -> speech: drop fenced code, KEEP inline-code contents,
           destructure headers/emphasis/lists/tables/links/rules/box-drawing/
           diffstats/HTML-entities (the single shared, idempotent pass that
           every TTS-bound path funnels through). See text_utils.normalize_for_speech.
        2. Remove emojis (not speakable)
        3. Humanize filesystem paths
        4. RESTORE contractions (contract expanded → natural)
        5. Normalize whitespace
        """
        if not text:
            return ""

        # Lazy import (matches the established pattern below) so the daemon's
        # sys.path is guaranteed ready at call time. Degrade gracefully if the
        # import ever fails, but normalize_for_speech is load-bearing for audio
        # quality, so this should effectively never fall back.
        try:
            from daemon.text_utils import normalize_for_speech, humanize_paths, is_speakable
        except Exception:  # pragma: no cover
            def normalize_for_speech(text: str) -> str:  # type: ignore
                return text or ""

            def humanize_paths(text: str) -> str:  # type: ignore
                return text

            def is_speakable(text: str) -> bool:  # type: ignore
                return bool(text and text.strip())

        # 1. Markdown -> speech (fenced code dropped, inline code CONTENTS kept,
        #    all structural markup destructured). Idempotent.
        text = normalize_for_speech(text)

        # 2. Remove emojis
        text = self._EMOJI_PATTERN.sub('', text)

        # 3. Humanize filesystem paths: "/Volumes/DISK/.../example"
        # → "example" so TTS doesn't spell out every "slash X slash Y".
        text = humanize_paths(text)

        # 4. RESTORE contractions (CONTRACT expanded forms for natural speech)
        # CRITICAL: "I am" → "I'm", NOT "I'm" → "I am"!
        text = self._restore_contractions(text)

        # 5. Normalize whitespace
        text = ' '.join(text.split())
        text = text.strip()

        # 6. Speakability backstop: if normalization left only non-lexical
        # residue (a bare SHA reduced to nothing, a ps/env dump dominated by
        # numbers/symbols), drop it. Empty cleaned text is an already-handled
        # contract (returns "" above for empty input), so downstream produces
        # no chunks and no audio — the utterance is silently skipped.
        if not is_speakable(text):
            return ""

        return text

    def _restore_contractions(self, text: str) -> str:
        """
        CONTRACTION RESTORATION: Convert expanded forms to natural contractions.

        CRITICAL BEHAVIOR:
        - "I am going" → "I'm going" (CORRECT - natural speech)
        - "I'm going" → "I'm going" (PRESERVED - already contracted)

        This ensures contractions like "I'm", "what's", "we've" are NEVER
        expanded to "I am", "what is", "we have" - they stay natural.
        """
        if not text:
            return text or ""

        result = text
        for pattern, contraction in self._compiled_patterns:
            def make_replacer(contr):
                def replacer(match):
                    matched = match.group(0)
                    # Preserve capitalization
                    if matched.isupper():
                        return contr.upper()
                    elif matched[0].isupper():
                        return contr[0].upper() + contr[1:]
                    return contr
                return replacer
            result = pattern.sub(make_replacer(contraction), result)

        return result

    def _chunk_text(self, text: str) -> list[str]:
        """
        Split text into speakable chunks for streaming TTS.

        Preserves sentence boundaries for natural speech pauses.
        """
        if not text:
            return []

        if len(text) <= self.chunk_size:
            return [text]

        chunks = []
        current_chunk = []
        current_length = 0

        # Split by sentence boundaries
        sentences = re.split(r'(?<=[.!?])\s+', text)

        for sentence in sentences:
            sentence_length = len(sentence)

            if current_length + sentence_length <= self.chunk_size:
                current_chunk.append(sentence)
                current_length += sentence_length + 1
            else:
                if current_chunk:
                    chunks.append(' '.join(current_chunk))
                current_chunk = [sentence]
                current_length = sentence_length

        if current_chunk:
            chunks.append(' '.join(current_chunk))

        return chunks

    async def process_stream(self, message: IngestMessage) -> AsyncIterator[str]:
        """
        Stream processed chunks as they're ready.

        Enables TTS generation to start before full text is processed.
        """
        processed = await self.process(message)
        for chunk in processed.chunks:
            yield chunk

    def get_stats(self) -> dict:
        """Get processing statistics."""
        avg_time = 0.0
        if self._stats['messages_processed'] > 0:
            avg_time = (
                self._stats['total_processing_time_ms'] /
                self._stats['messages_processed']
            )
        return {
            **self._stats,
            'avg_processing_time_ms': avg_time,
        }
