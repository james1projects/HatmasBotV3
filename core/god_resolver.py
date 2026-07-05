"""
core/god_resolver.py — tiered god-name resolution for chat input.

Chat types "zues", "kuku", or "morri" and expects the bot to know who
they mean. Before this module, godrequest / god_pool / economy each
had their own exact -> prefix -> contains matcher and a typo simply
failed. This resolver gives every call site the same tiers:

    1. exact     — normalized (and space-squashed) equality
    2. alias     — community nicknames from core/god_aliases.py
    3. prefix    — unique prefix, 3+ chars
    4. contains  — unique substring, 3+ chars
    5. fuzzy     — difflib ratio with a clear-winner margin (stdlib)
    6. llm       — local Ollama, strict timeout, OFF the hot path

The LLM tier exists for inputs like "the fox god" that no string
metric can resolve. It is last for a reason: Ollama shares the GPU
with Smite and OBS, so the call carries a hard timeout and any
failure (down, cold model, nonsense reply) silently falls back to
"unresolved" — a slow miss must never delay chat handling.

Call sites pass their own candidate list (canonical names in the
casing they want back), so the economy can resolve against DB gods
while godrequest resolves against SMITE2_GODS:

    from core.god_resolver import resolve_sync, resolve
    hit = resolve_sync("kuku", SMITE2_GODS)      # tiers 1-5
    hit = await resolve("the fox god", gods)     # tiers 1-6
    # hit is (candidate_as_given, tier) or None
"""

from __future__ import annotations

import difflib
import re

try:
    import aiohttp
except ImportError:  # resolver stays importable without the bot's deps
    aiohttp = None

from core.god_aliases import ALIASES

TIER_EXACT = "exact"
TIER_ALIAS = "alias"
TIER_PREFIX = "prefix"
TIER_CONTAINS = "contains"
TIER_FUZZY = "fuzzy"
TIER_LLM = "llm"

# Fuzzy tier: SequenceMatcher ratio floor, and how clearly the best
# match must beat the runner-up. 0.75 admits the classic transposition
# "zues" -> zeus (0.75) while junk like "kratos" stays below the floor
# (~0.67 vs its closest god). Scores are also taken over phonetically
# folded forms (c->k, y->i, oo->u, ph->f) so "skilla" -> scylla lands.
FUZZY_CUTOFF = 0.75
FUZZY_MARGIN = 0.03

# Minimum input length for the partial-match tiers, so "a" doesn't
# prefix-match anything and "ra" still exact-matches Ra.
MIN_PARTIAL_LEN = 3

_PUNCT_RE = re.compile(r"[’'\-.!?,:;]")
_WS_RE = re.compile(r"\s+")


def _normalize(text: str) -> str:
    """Lowercase, strip punctuation that varies in chat (apostrophes,
    hyphens, periods), collapse whitespace."""
    text = _PUNCT_RE.sub("", text.lower().strip())
    return _WS_RE.sub(" ", text)


def _squash(text: str) -> str:
    """Normalized form with spaces removed ("guan yu" -> "guanyu")."""
    return _normalize(text).replace(" ", "")


def _phonetic(text: str) -> str:
    """Crude phonetic fold for the fuzzy tier only: collapse the
    substitutions chat actually makes when spelling by ear."""
    return (text.replace("ph", "f").replace("oo", "u")
            .replace("c", "k").replace("y", "i"))


def resolve_sync(text, candidates, *, fuzzy=True):
    """Resolve `text` against `candidates` using tiers 1-5.

    candidates: iterable of canonical god names, any casing; the hit
    returns the candidate string exactly as given.
    Returns (candidate, tier) or None.
    """
    query = _normalize(text)
    if not query:
        return None
    squashed = query.replace(" ", "")

    cands = list(candidates)
    by_norm = {}
    by_squash = {}
    for c in cands:
        by_norm.setdefault(_normalize(c), c)
        by_squash.setdefault(_squash(c), c)

    # 1. exact (normalized, then space-squashed: "guanyu" == "guan yu")
    if query in by_norm:
        return by_norm[query], TIER_EXACT
    if squashed in by_squash:
        return by_squash[squashed], TIER_EXACT

    # 2. alias — table maps to canonical lowercase names; only accept
    # aliases whose target is actually in this call's candidate list.
    target = ALIASES.get(query) or ALIASES.get(squashed)
    if target is not None:
        hit = by_norm.get(_normalize(target))
        if hit is not None:
            return hit, TIER_ALIAS

    if len(squashed) >= MIN_PARTIAL_LEN:
        # 3. prefix — must be unique across candidates
        pref = [c for n, c in by_norm.items()
                if n.startswith(query) or n.replace(" ", "").startswith(squashed)]
        if len(set(pref)) == 1:
            return pref[0], TIER_PREFIX

        # 4. contains — must be unique across candidates
        cont = [c for n, c in by_norm.items()
                if query in n or squashed in n.replace(" ", "")]
        if len(set(cont)) == 1:
            return cont[0], TIER_CONTAINS

    # 5. fuzzy — clear winner over both normalized and squashed forms
    if fuzzy and len(squashed) >= MIN_PARTIAL_LEN:
        q_phon = _phonetic(squashed)
        scored = []
        for n, c in by_norm.items():
            n_squash = n.replace(" ", "")
            score = max(
                difflib.SequenceMatcher(None, query, n).ratio(),
                difflib.SequenceMatcher(None, squashed, n_squash).ratio(),
                difflib.SequenceMatcher(None, q_phon, _phonetic(n_squash)).ratio(),
            )
            scored.append((score, c))
        scored.sort(key=lambda s: s[0], reverse=True)
        if scored and scored[0][0] >= FUZZY_CUTOFF:
            best_score, best = scored[0]
            runner_up = next((s for s, c in scored[1:] if c != best), 0.0)
            if best_score - runner_up >= FUZZY_MARGIN:
                return best, TIER_FUZZY

    return None


async def resolve(text, candidates, *, allow_llm=True):
    """Tiers 1-5, then the Ollama LLM tier for still-unresolved input.

    Never raises: any LLM failure (disabled, unreachable, timeout,
    unparseable or non-candidate answer) returns None.
    """
    hit = resolve_sync(text, candidates)
    if hit is not None or not allow_llm:
        return hit
    name = await _llm_resolve(text, list(candidates))
    if name is not None:
        return name, TIER_LLM
    return None


async def _llm_resolve(text, cands):
    from core import config
    if aiohttp is None or not getattr(config, "GOD_RESOLVER_LLM_ENABLED", True):
        return None

    # Default must be a NON-thinking model: thinking models (qwen3.6)
    # spend the whole num_predict budget on the "thinking" field and
    # return an empty "response". qwen3-coder is a fast non-thinking MoE.
    model = getattr(config, "GOD_RESOLVER_LLM_MODEL", "qwen3-coder:30b")
    timeout = float(getattr(config, "GOD_RESOLVER_LLM_TIMEOUT", 2.5))
    prompt = (
        "You match Twitch chat input to a SMITE 2 god name.\n"
        f"Input: {text.strip()[:80]!r}\n"
        f"Gods: {', '.join(sorted(_normalize(c) for c in cands))}\n"
        "Reply with exactly one god name from the list, or NONE if the "
        "input does not refer to any of them. No other text."
    )
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout)
        ) as session:
            async with session.post(
                "http://127.0.0.1:11434/api/generate",
                json={"model": model, "prompt": prompt, "stream": False,
                      "think": False,
                      "options": {"temperature": 0, "num_predict": 12}},
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
    except Exception:
        return None

    answer = _normalize(str(data.get("response", "")).strip().strip('"'))
    if not answer or answer == "none":
        return None
    for c in cands:
        if _normalize(c) == answer:
            return c
    return None
