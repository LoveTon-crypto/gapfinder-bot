"""YouTube autocomplete expansion (unofficial endpoint, no API quota)."""
from __future__ import annotations

import asyncio
import json

import aiohttp

SUGGEST_URL = "https://suggestqueries.google.com/complete/search"
LETTERS = {
    "ru": "абвгдежзийклмнопрстуфхцчшэюя",
    "en": "abcdefghijklmnopqrstuvwxyz",
}


def detect_lang(text: str) -> str:
    return "ru" if any("а" <= ch.lower() <= "я" or ch.lower() == "ё" for ch in text) else "en"


def parse_response(raw: bytes) -> list[str]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("cp1251", errors="replace")
    data = json.loads(text)
    return [s for s in data[1] if isinstance(s, str)]


async def fetch(session: aiohttp.ClientSession, q: str, hl: str) -> list[str]:
    params = {"client": "firefox", "ds": "yt", "q": q, "hl": hl, "ie": "utf-8", "oe": "utf-8"}
    async with session.get(SUGGEST_URL, params=params, timeout=aiohttp.ClientTimeout(total=10)) as r:
        r.raise_for_status()
        return parse_response(await r.read())


async def expand(session: aiohttp.ClientSession, seed: str, hl: str | None = None,
                 concurrency: int = 4) -> dict[str, float]:
    """
    Returns {suggestion: demand 0..1}.
    Demand = position in the suggestion list (1st = 1.0, 10th = 0.1), with a bonus if the phrase
    shows up already for the bare seed and for repeated appearances.
    """
    seed = seed.strip()
    hl = hl or detect_lang(seed)
    prefixes = [seed] + [f"{seed} {ch}" for ch in LETTERS[hl]]
    sem = asyncio.Semaphore(concurrency)

    async def one(prefix: str) -> tuple[str, list[str]]:
        async with sem:
            try:
                res = await fetch(session, prefix, hl)
            except Exception:
                res = []
            await asyncio.sleep(0.15)  # be polite, avoid getting rate-limited
            return prefix, res

    results = await asyncio.gather(*(one(p) for p in prefixes))

    best: dict[str, float] = {}
    hits: dict[str, int] = {}
    for prefix, suggestions in results:
        bare = prefix == seed
        for i, s in enumerate(suggestions[:10]):
            s = s.strip().lower()
            if not s or s == seed.lower():
                continue
            pos_score = 1.0 - i / 10
            if bare:
                pos_score = min(1.0, pos_score + 0.3)
            best[s] = max(best.get(s, 0.0), pos_score)
            hits[s] = hits.get(s, 0) + 1

    return {s: round(min(1.0, d + 0.05 * (hits[s] - 1)), 3) for s, d in best.items()}
