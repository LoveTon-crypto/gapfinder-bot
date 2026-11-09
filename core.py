"""Pipeline: seed → autocomplete expansion → top results per query → gap score."""
from __future__ import annotations

import csv
import io
import os
import re
from typing import Awaitable, Callable

import aiohttp

from filters import is_noise, split_request, stop_list
from scoring import MUSIC_DROP_SHARE, analyze
from suggest import detect_lang, expand
from youtube import QuotaExceeded, YouTube

UNITS_PER_QUERY = 102

Progress = Callable[[str], Awaitable[None]]

_FILLER = {"the", "a", "an", "of", "in", "on", "to", "and", "for", "is", "how", "what", "why",
           "и", "в", "на", "с", "о", "для", "как", "что", "почему"}


def _tokens(q: str) -> frozenset[str]:
    words = re.findall(r"[\w']+", q.lower())
    return frozenset(w[:-1] if len(w) > 3 and w.endswith("s") else w for w in words if w not in _FILLER)


def pick_diverse(candidates: list[tuple[str, float]], n: int) -> tuple[list[tuple[str, float]], int]:
    """Take the n strongest suggestions, skipping near-duplicates ("giant squid size" vs
    "giant squids size", "size of giant squid") — their YouTube results are almost the same,
    so paying 100 units for each is waste. Returns (picked, skipped_count)."""
    picked: list[tuple[str, float]] = []
    seen: list[frozenset[str]] = []
    skipped = 0
    for q, d in candidates:
        t = _tokens(q)
        if any(t == s or (len(t | s) and len(t & s) / len(t | s) >= 0.8) for s in seen):
            skipped += 1
            continue
        picked.append((q, d)); seen.append(t)
        if len(picked) >= n:
            break
    return picked, skipped


async def find_gaps(seed: str, yt: YouTube, session: aiohttp.ClientSession,
                    top_n: int = 15, progress: Progress | None = None) -> tuple[list[dict], str | None]:
    """Returns (results sorted by score desc, warning or None).
    seed may contain minus-words: "zodiac myths -killer -gun"."""
    seed, minus = split_request(seed)
    lang = detect_lang(seed)
    region = {"ru": os.getenv("REGION_RU", "RU"), "en": os.getenv("REGION_EN", "US")}[lang]
    if progress:
        await progress("🔎 Собираю подсказки YouTube…")
    demand = await expand(session, seed, lang)
    if not demand:
        return [], "Подсказок не нашлось — попробуй более общий запрос."

    stops = stop_list(minus)
    dropped = [q for q in demand if is_noise(q, stops, seed)]
    for q in dropped:
        del demand[q]
    if not demand:
        return [], f"Все подсказки отфильтрованы стоп-словами ({len(dropped)} шт.)."

    candidates = sorted(demand.items(), key=lambda kv: kv[1], reverse=True)
    candidates, dupes = pick_diverse(candidates, top_n)
    affordable = yt.units_left() // UNITS_PER_QUERY
    warning = None
    if affordable < len(candidates):
        warning = f"Квоты хватает только на {affordable} новых запросов (кэшированные бесплатны)."
    if dupes and progress:
        await progress(f"♻️ Пропущено почти одинаковых подсказок: {dupes} — квота на них не тратится")

    results, music = [], []
    for i, (q, d) in enumerate(candidates, 1):
        if progress and (i == 1 or i % 3 == 0):
            await progress(f"📊 Анализирую выдачу {i}/{len(candidates)}: «{q}»")
        try:
            videos = await yt.top_results(q, region, lang)
        except QuotaExceeded as e:
            warning = str(e)
            break
        r = analyze(q, videos, d)
        r["suggest_demand"] = d
        if r["music_share"] >= MUSIC_DROP_SHARE:
            music.append(q)  # top is mostly music videos: people want a song, not an explainer
            continue
        results.append(r)

    results.sort(key=lambda r: r["score"], reverse=True)
    if dropped and progress:
        await progress(f"🧹 Отфильтровано мусорных подсказок: {len(dropped)}")
    if music:
        note = f"🎵 Убрано музыкальных запросов (топ — клипы и песни): {len(music)}"
        warning = f"{warning}\n{note}" if warning else note
    if not results and music:
        return [], note
    return results, warning


def to_csv(results: list[dict]) -> bytes:
    buf = io.StringIO()
    cols = ["score", "query", "demand", "supply_weakness", "median_age_days", "median_views",
            "match_share", "small_share", "outliers", "music_share", "quote", "reasons"]
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for r in results:
        w.writerow({**r, "reasons": "; ".join(r["reasons"])})
    return buf.getvalue().encode("utf-8-sig")  # BOM so Excel opens Cyrillic correctly
