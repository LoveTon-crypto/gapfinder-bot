"""Stop-word filtering for autocomplete suggestions.

Three sources of stop words, all combined:
  1. DEFAULT_STOP below — common noise (games, true crime, music) that hijacks story/explainer niches;
  2. STOP_WORDS in .env — your own list, comma-separated;
  3. minus-words in the request itself:  "zodiac myths -killer -gun"
A single word matches a whole word only ("gun" won't drop "begun"); a phrase with spaces matches as a substring.
"""
from __future__ import annotations

import os
import re

DEFAULT_STOP = [
    # games
    "free fire", "fortnite", "minecraft", "roblox", "gta", "pubg", "valorant", "genshin", "total war",
    "master of orion", "pax", "gameplay", "walkthrough", "speedrun", "game", "games",
    # true crime / weapons
    "killer", "murder", "gun", "cipher",
    # music, edits, trending sounds
    "lyrics", "lyric", "remix", "song", "karaoke", "sped up", "slowed", "nightcore", "edit", "edits", "mv", "mvs",
    # games / fandoms that hijack "would you survive …" style queries
    "fnaf", "danganronpa", "left 4 dead", "katseye",
]

_WORD_RE = re.compile(r"[\w']+", re.UNICODE)


def _norm(s: str) -> str:
    return s.lower().replace("ё", "е").strip()


def split_request(text: str) -> tuple[str, list[str]]:
    """'zodiac myths -killer -gun' -> ('zodiac myths', ['killer', 'gun'])"""
    seed, minus = [], []
    for part in text.split():
        if part.startswith("-") and len(part) > 1:
            minus.append(_norm(part[1:]))
        else:
            seed.append(part)
    return " ".join(seed), minus


def stop_list(extra: list[str] | None = None) -> list[str]:
    env = [_norm(w) for w in os.getenv("STOP_WORDS", "").split(",") if w.strip()]
    if os.getenv("DEFAULT_STOP", "1") == "0":
        base = []
    else:
        base = DEFAULT_STOP
    return sorted({*(_norm(w) for w in base), *env, *(extra or [])})


def is_noise(query: str, stops: list[str], seed: str = "") -> bool:
    q = _norm(query)
    words = set(_WORD_RE.findall(q))
    seed_words = set(_WORD_RE.findall(_norm(seed)))
    for s in stops:
        if s in seed_words or (" " in s and s in _norm(seed)):
            continue  # user explicitly searches for it (e.g. seed "minecraft") — don't filter
        if (" " in s and s in q) or (" " not in s and s in words):
            return True
    return False
