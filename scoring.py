"""Gap scoring: demand (from YouTube suggestions) vs. weakness of current supply (top-10 search results).

Pure stdlib, no network — easy to test and tune.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from statistics import median

SMALL_CHANNEL_SUBS = 10_000      # channel counts as "small" below this
OUTLIER_RATIO = 3.0              # views >= 3x subscribers => video outperformed its channel
OUTLIER_MAX_SUBS = 100_000       # only count outliers on small/mid channels
OLD_AGE_DAYS = 730               # median age of top results at which "freshness weakness" maxes out

_TOKEN_RE = re.compile(r"[\w']+", re.UNICODE)


def tokens(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall(text.lower().replace("ё", "е")) if len(t) > 2]


def title_matches(query: str, title: str) -> bool:
    """True if every meaningful word of the query (prefix match, to tolerate word endings) is in the title."""
    q = tokens(query)
    t = tokens(title)
    if not q:
        return True
    return all(any(w.startswith(qw[: max(4, len(qw) - 2)]) for w in t) for qw in q)


def _parse_date(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def analyze(query: str, videos: list[dict], suggest_demand: float, now: datetime | None = None) -> dict:
    """
    videos: [{title, published_at (ISO), views:int, subs:int|None}, ...] — the top search results.
    suggest_demand: 0..1, how strongly the query shows up in YouTube autocomplete.
    Returns a dict with score 0..100, metrics and human-readable reasons.
    """
    now = now or datetime.now(timezone.utc)

    if not videos:
        score = round(100 * suggest_demand)
        return {
            "query": query, "score": score, "demand": round(suggest_demand, 2), "supply_weakness": 1.0,
            "median_age_days": None, "median_views": None, "match_share": 0.0,
            "small_share": None, "outliers": 0, "reasons": ["в выдаче нет видео"],
        }

    ages = [max(0, (now - _parse_date(v["published_at"])).days) for v in videos]
    views = [v.get("views") or 0 for v in videos]
    match_share = sum(title_matches(query, v["title"]) for v in videos) / len(videos)
    known_subs = [v["subs"] for v in videos if v.get("subs") is not None]
    small_share = (sum(s < SMALL_CHANNEL_SUBS for s in known_subs) / len(known_subs)) if known_subs else 0.0
    outliers = sum(
        1 for v in videos
        if v.get("subs") and v["subs"] < OUTLIER_MAX_SUBS and (v.get("views") or 0) >= OUTLIER_RATIO * v["subs"]
    )

    med_age = median(ages)
    freshness_weak = min(1.0, med_age / OLD_AGE_DAYS)
    match_weak = 1.0 - match_share
    supply_weakness = 0.40 * freshness_weak + 0.35 * match_weak + 0.25 * small_share

    outlier_signal = min(1.0, outliers / 3)
    demand = 0.75 * suggest_demand + 0.25 * outlier_signal

    score = round(100 * math.sqrt(demand * supply_weakness))

    reasons = []
    if med_age >= 365:
        reasons.append(f"топ старый (~{med_age / 365:.1f} г.)")
    if match_share <= 0.3:
        reasons.append(f"точно по запросу лишь {round(match_share * 100)}% видео")
    if small_share >= 0.5:
        reasons.append(f"{round(small_share * 100)}% топа — малые каналы")
    if outliers:
        reasons.append(f"{outliers} видео выстрелили выше канала")
    if suggest_demand >= 0.7:
        reasons.append("высоко в подсказках")

    return {
        "query": query, "score": score, "demand": round(demand, 2), "supply_weakness": round(supply_weakness, 2),
        "median_age_days": int(med_age), "median_views": int(median(views)), "match_share": round(match_share, 2),
        "small_share": round(small_share, 2), "outliers": outliers, "reasons": reasons,
    }
