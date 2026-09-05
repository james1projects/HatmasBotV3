"""
faster-whisper wrapper for vodsearch.

One `Transcriber` holds one loaded model (large-v3 fp16 on CUDA runs
about 10x realtime on an RTX 5090, measured 2026-09-04; the batched
pipeline is faster still). Input is a float32 mono 16 kHz numpy array
straight from `audio.decode_tracks`, so no temp files.

Whisper's known failure mode on quiet mic tracks is hallucination:
looping the same sentence, or emitting "Thank you." / "Thanks for
watching." over silence. VAD filtering removes most of it; the
`clean_segments` pass catches the rest so junk never lands in the index.
"""

from __future__ import annotations

import inspect
import re
from dataclasses import dataclass
from typing import Iterable, List, Optional

import numpy as np

from .cuda import preload_cuda_dlls


@dataclass
class Word:
    start: float
    end: float
    word: str


@dataclass
class Segment:
    start: float
    end: float
    text: str
    no_speech_prob: float = 0.0
    avg_logprob: float = 0.0
    words: Optional[List[Word]] = None


SENTENCE_END = (".", "?", "!")
MAX_SENTENCE_S = 15.0      # a "moment" should be one thought, not a monologue
MAX_SENTENCE_CHARS = 220


def split_sentences(segments: Iterable[Segment], max_len_s: float = MAX_SENTENCE_S,
                    max_chars: int = MAX_SENTENCE_CHARS) -> List[Segment]:
    """Re-cut segments into sentence-sized pieces using word timestamps.
    faster-whisper's batched pipeline returns one segment per VAD chunk
    (measured up to 9 minutes long), which is useless as a search hit.
    Breaks after a word ending in . ? ! and whenever a piece would exceed
    max_len_s or max_chars. Segments without word data pass through."""
    out: List[Segment] = []
    for seg in segments:
        words = seg.words or []
        if not words:
            out.append(seg)
            continue
        cur: List[Word] = []
        chars = 0

        def flush():
            nonlocal cur, chars
            if cur:
                text = "".join(w.word for w in cur).strip()
                if text:
                    out.append(Segment(start=float(cur[0].start), end=float(cur[-1].end),
                                       text=text, no_speech_prob=seg.no_speech_prob,
                                       avg_logprob=seg.avg_logprob))
            cur, chars = [], 0

        for w in words:
            token = w.word or ""
            if cur and (chars + len(token) > max_chars or (w.end - cur[0].start) > max_len_s):
                flush()
            cur.append(w)
            chars += len(token)
            if token.rstrip().endswith(SENTENCE_END):
                flush()
        flush()
    return out


# Whole-segment texts Whisper emits over silence. Compared after
# lowercasing and stripping punctuation.
_JUNK = {
    "thank you", "thanks for watching", "thank you for watching",
    "you", "bye", "so", "the end", "subtitles by the amara org community",
    "thanks for listening", "please subscribe",
}
_NORM_RE = re.compile(r"[^a-z0-9 ]+")


def _norm(text: str) -> str:
    return _NORM_RE.sub("", text.lower()).strip()


def clean_segments(segments: Iterable[Segment],
                   max_no_speech: float = 0.85,
                   weak_logprob: float = -1.0,
                   max_repeat: int = 2) -> List[Segment]:
    """Drop empties, junk phrases, low-confidence non-speech, and
    collapse repetition loops (the same normalized text more than
    `max_repeat` times in a row)."""
    out: List[Segment] = []
    last_norm = ""
    run = 0
    for seg in segments:
        text = (seg.text or "").strip()
        if not text:
            continue
        norm = _norm(text)
        if not norm:
            continue
        if norm in _JUNK:
            continue
        if seg.no_speech_prob > max_no_speech and seg.avg_logprob < weak_logprob:
            continue
        if seg.end <= seg.start:
            continue
        if norm == last_norm:
            run += 1
            if run >= max_repeat:
                continue
        else:
            last_norm = norm
            run = 0
        out.append(Segment(start=float(seg.start), end=float(seg.end), text=text,
                           no_speech_prob=float(seg.no_speech_prob or 0.0),
                           avg_logprob=float(seg.avg_logprob or 0.0)))
    return out


class Transcriber:
    """Lazy-loads faster-whisper on first use so importing this module
    (and constructing the object in tests) costs nothing."""

    def __init__(self, model_name: str = "large-v3", device: str = "cuda",
                 compute_type: str = "float16", language: Optional[str] = "en",
                 beam_size: int = 5, batch_size: int = 16):
        self.model_name = model_name
        self.device = device
        self.compute_type = compute_type
        self.language = language
        self.beam_size = beam_size
        self.batch_size = batch_size
        self._model = None
        self._batched = None

    def load(self) -> None:
        if self._model is not None:
            return
        preload_cuda_dlls()
        from faster_whisper import WhisperModel  # noqa: WPS433 (lazy on purpose)
        self._model = WhisperModel(self.model_name, device=self.device,
                                   compute_type=self.compute_type)
        if self.batch_size and self.batch_size > 1:
            try:
                from faster_whisper import BatchedInferencePipeline
                self._batched = BatchedInferencePipeline(self._model)
            except Exception:
                self._batched = None

    @staticmethod
    def _filter_kwargs(fn, kwargs: dict) -> dict:
        try:
            params = inspect.signature(fn).parameters
        except (TypeError, ValueError):
            return kwargs
        if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
            return kwargs
        return {k: v for k, v in kwargs.items() if k in params}

    def transcribe(self, samples: np.ndarray) -> List[Segment]:
        """Transcribe one mono 16 kHz float32 track. Returns cleaned,
        chronologically ordered segments."""
        self.load()
        if samples.dtype != np.float32:
            samples = samples.astype(np.float32)
        kwargs = dict(language=self.language, beam_size=self.beam_size,
                      vad_filter=True, condition_on_previous_text=False,
                      word_timestamps=True)
        if self._batched is not None:
            fn = self._batched.transcribe
            kw = self._filter_kwargs(fn, dict(kwargs, batch_size=self.batch_size))
        else:
            fn = self._model.transcribe
            kw = self._filter_kwargs(fn, kwargs)
        raw, _info = fn(samples, **kw)
        segs = []
        for s in raw:
            words = [Word(start=float(w.start), end=float(w.end), word=str(w.word))
                     for w in (getattr(s, "words", None) or [])]
            segs.append(Segment(start=s.start, end=s.end, text=s.text,
                                no_speech_prob=getattr(s, "no_speech_prob", 0.0) or 0.0,
                                avg_logprob=getattr(s, "avg_logprob", 0.0) or 0.0,
                                words=words or None))
        segs.sort(key=lambda s: s.start)
        return clean_segments(split_sentences(segs))
