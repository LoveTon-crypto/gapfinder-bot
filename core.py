"""Pipeline: seed → autocomplete expansion → top results per query → gap score."""
from __future__ import annotations

import csv
import io
import os
from typing import Awaitable, Callable

import aiohttp

from filters import is_noise, split_request, stop_list
from scoring import MUSIC_DROP_SHARE, analyze
from suggest import detect_lang, expand
from youtube import QuotaExceeded, YouTube

UNITS_PER_QUERY = 102

Progress = Callable[[str], Awaitable[None]]


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
    affordable = yt.units_left() // UNITS_PER_QUERY
    warning = None
    if affordable < top_n:
        warning = f"Квоты хватает только на {affordable} запросов (кэшированные бесплатны)."
    candidates = candidates[:top_n]

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
