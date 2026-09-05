"""
plugins/cocaster/summarizer.py — prompts and LLM backends for the co-caster.

Two jobs:
  * EAR: turn the last minute of chat into one spoken sentence for the
    streamer's headphones. Informative, terse, no persona. "Two people are
    asking your build, and Dyna wants a song."
  * LINE: a one-liner in the Hatmas persona reacting to a detector event
    (multikill, death). Text first; on-stream voice is a config switch.

Backends:
  * ClaudeBackend  - Anthropic API (default). Only chat text and match
    context ever leave the PC; never transcripts or recordings.
  * OllamaBackend  - fully local. Shares the GPU with Smite, so it is the
    privacy-maximal option rather than the default.

`clean_spoken` makes any model output safe to hand to text-to-speech:
no markdown, no quotes, no emoji, capped word count.
"""

from __future__ import annotations

import re
import time
from typing import Dict, List, Optional, Sequence

EAR_SYSTEM = (
    "You are a live producer whispering into a Twitch streamer's earpiece while he plays "
    "SMITE 2. You get the newest chat messages and the match state. Reply with ONE short "
    "spoken sentence, at most {max_words} words, plain text, no lists, no quotes, no emoji, "
    "no preamble. Priorities: direct questions to the streamer, requests (songs, gods), "
    "new people saying hi, anything he must react to. Name people by their chat names. "
    "Skip spam, bots, and command chatter. If nothing needs him, say exactly: "
    "Nothing new in chat."
)

LINE_SYSTEM = (
    "{persona}\n\n"
    "Write ONE spoken line, at most {max_words} words, plain text, no quotes, no emoji, "
    "no hashtags, reacting to the event you are given. Never invent facts that are not in "
    "the context: no made-up numbers, enemy names, item names, or market price moves "
    "(mention the market only if a price is given). Never start with a command prefix "
    "like ! or /."
)

_MD_RE = re.compile(r"[*_`#>\[\]()~|]+")
_EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F900-\U0001F9FF]+")
_WS_RE = re.compile(r"\s+")


def clean_spoken(text: str, max_words: int = 40) -> str:
    """Make model output safe for text-to-speech."""
    t = (text or "").strip()
    if not t:
        return ""
    t = t.split("\n")[0] if "\n" in t and len(t.split("\n")[0]) > 12 else t.replace("\n", " ")
    t = _MD_RE.sub("", t)
    t = _EMOJI_RE.sub("", t)
    t = t.strip().strip('"').strip("'").strip()
    t = _WS_RE.sub(" ", t)
    words = t.split(" ")
    if len(words) > max_words:
        t = " ".join(words[:max_words]).rstrip(",;:") + "."
    if t and t[0] in "!/.":
        t = t.lstrip("!/.").strip()
    return t


def format_context(ctx: Dict) -> str:
    """Match/stream state as short lines the model can lean on."""
    parts: List[str] = []
    if ctx.get("is_live") is not None:
        parts.append("Stream: " + ("live" if ctx.get("is_live") else "offline"))
    if ctx.get("god"):
        line = f"Playing {ctx['god']}"
        kda = ctx.get("kda")
        if kda and len(kda) >= 3:
            line += f", KDA {kda[0]}/{kda[1]}/{kda[2]}"
        if ctx.get("match_minutes") is not None:
            line += f", {int(ctx['match_minutes'])} min into the match"
        parts.append(line)
    else:
        parts.append("Not in a match right now")
    if ctx.get("viewers") is not None:
        parts.append(f"Viewers: {ctx['viewers']}")
    if ctx.get("queue_len") is not None:
        parts.append(f"God request queue: {ctx['queue_len']}")
    if ctx.get("song"):
        parts.append(f"Now playing: {ctx['song']}")
    return "\n".join(parts)


def format_messages(messages: Sequence[Dict], max_chars: int = 3000) -> str:
    lines: List[str] = []
    total = 0
    for m in messages:
        who = m.get("display") or m.get("user") or "someone"
        text = (m.get("text") or "").strip().replace("\n", " ")
        if not text:
            continue
        line = f"{who}: {text[:240]}"
        total += len(line) + 1
        if total > max_chars:
            break
        lines.append(line)
    return "\n".join(lines)


def build_ear_prompt(messages: Sequence[Dict], ctx: Dict, previous: Optional[str] = None,
                     max_words: int = 35) -> Dict[str, str]:
    """-> {"system", "prompt"} for the earpiece summary."""
    system = EAR_SYSTEM.format(max_words=max_words)
    body = format_context(ctx) + "\n\nNew chat messages (oldest first):\n" + (
        format_messages(messages) or "(none)")
    if previous:
        body += f"\n\nYou last told him: {previous}\nDo not repeat that."
    body += "\n\nYour one sentence:"
    return {"system": system, "prompt": body}


def build_line_prompt(event: Dict, ctx: Dict, persona: str, max_words: int = 22,
                      recent_chat: Optional[Sequence[Dict]] = None) -> Dict[str, str]:
    """-> {"system", "prompt"} for an on-stream caster line."""
    system = LINE_SYSTEM.format(persona=persona.strip(), max_words=max_words)
    kind = event.get("kind", "event")
    detail = event.get("detail") or ""
    body = format_context(ctx) + f"\n\nEvent: {kind}" + (f" ({detail})" if detail else "")
    if event.get("count") is not None:
        body += f"\nCount this session: {event['count']}"
    if recent_chat:
        body += "\n\nWhat chat just said:\n" + format_messages(recent_chat, max_chars=600)
    body += "\n\nYour line:"
    return {"system": system, "prompt": body}


class ClaudeBackend:
    """Anthropic Messages API. Async; one short completion per call."""

    name = "claude"

    def __init__(self, api_key: str, model: str = "claude-opus-5", effort: str = "low",
                 timeout_s: float = 25.0):
        import anthropic  # local import keeps tests free of the SDK
        self.model = model
        self.effort = effort
        self.client = anthropic.AsyncAnthropic(api_key=api_key, timeout=timeout_s, max_retries=1)

    async def complete(self, system: str, prompt: str, max_tokens: int = 200) -> str:
        kwargs = dict(model=self.model, max_tokens=max_tokens, system=system,
                      messages=[{"role": "user", "content": prompt}])
        if self.effort:
            kwargs["output_config"] = {"effort": self.effort}
        resp = await self.client.messages.create(**kwargs)
        if getattr(resp, "stop_reason", "") == "refusal":
            return ""
        return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()


class OllamaBackend:
    """Local Ollama /api/generate. think=False because a thinking model
    burns seconds before the first word (see GOD_RESOLVER notes)."""

    name = "ollama"

    def __init__(self, host: str = "http://localhost:11434", model: str = "qwen3.6:27b",
                 timeout_s: float = 25.0):
        self.host = host.rstrip("/")
        self.model = model
        self.timeout_s = timeout_s

    async def complete(self, system: str, prompt: str, max_tokens: int = 200) -> str:
        import aiohttp
        payload = {"model": self.model, "system": system, "prompt": prompt, "stream": False,
                   "think": False, "options": {"num_predict": max_tokens, "temperature": 0.7}}
        timeout = aiohttp.ClientTimeout(total=self.timeout_s)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(self.host + "/api/generate", json=payload) as r:
                if r.status != 200:
                    raise RuntimeError(f"ollama {r.status}: {(await r.text())[:200]}")
                data = await r.json()
        return str(data.get("response") or "").strip()


class RateLimiter:
    """Minimum spacing between spoken lines, per channel."""

    def __init__(self, cooldown_s: float):
        self.cooldown_s = float(cooldown_s)
        self.last_at = 0.0

    def allow(self, now: Optional[float] = None) -> bool:
        now = time.time() if now is None else now
        if now - self.last_at < self.cooldown_s:
            return False
        self.last_at = now
        return True
