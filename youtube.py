"""YouTube Data API v3 client with key rotation, a SQLite cache and a daily quota counter.

Cost per analysed query: search.list 100 + videos.list 1 + channels.list 1 = 102 units.
Default quota is 10,000 units/day per Google Cloud PROJECT (keys in one project share it) → ~98 queries/day/key. Cached queries cost 0.
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp

API = "https://www.googleapis.com/youtube/v3"
CACHE_TTL = 24 * 3600


def quota_day() -> str:
    """YouTube quota resets at midnight Pacific Time."""
    return datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()


class QuotaExceeded(Exception):
    pass


class Store:
    def __init__(self, path: str = "gapfinder.db"):
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS cache (k TEXT PRIMARY KEY, ts INTEGER, v TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS quota (day TEXT, key_idx INTEGER, units INTEGER, "
                        "PRIMARY KEY (day, key_idx))")
        self.db.commit()

    def get(self, k: str):
        row = self.db.execute("SELECT ts, v FROM cache WHERE k=?", (k,)).fetchone()
        if row and time.time() - row[0] < CACHE_TTL:
            return json.loads(row[1])
        return None

    def put(self, k: str, v) -> None:
        self.db.execute("REPLACE INTO cache VALUES (?,?,?)", (k, int(time.time()), json.dumps(v, ensure_ascii=False)))
        self.db.commit()

    def add_units(self, key_idx: int, units: int) -> None:
        self.db.execute("INSERT INTO quota VALUES (?,?,?) ON CONFLICT(day, key_idx) DO UPDATE SET units=units+?",
                        (quota_day(), key_idx, units, units))
        self.db.commit()

    def used_today(self) -> dict[int, int]:
        rows = self.db.execute("SELECT key_idx, units FROM quota WHERE day=?", (quota_day(),))
        return dict(rows.fetchall())


class YouTube:
    def __init__(self, keys: list[str], session: aiohttp.ClientSession, store: Store, daily_limit: int = 10_000):
        if not keys:
            raise ValueError("Нужен хотя бы один YouTube API ключ (YT_API_KEYS)")
        self.keys, self.session, self.store, self.daily_limit = keys, session, store, daily_limit
        self.dead: set[int] = set()

    def units_left(self) -> int:
        used = self.store.used_today()
        return sum(max(0, self.daily_limit - used.get(i, 0)) for i in range(len(self.keys)) if i not in self.dead)

    async def _get(self, endpoint: str, params: dict, cost: int) -> dict:
        used = self.store.used_today()
        for idx, key in enumerate(self.keys):
            if idx in self.dead or used.get(idx, 0) + cost > self.daily_limit:
                continue
            async with self.session.get(f"{API}/{endpoint}", params={**params, "key": key},
                                        timeout=aiohttp.ClientTimeout(total=15)) as r:
                data = await r.json()
            self.store.add_units(idx, cost)
            if r.status == 403 and any(e.get("reason") in ("quotaExceeded", "dailyLimitExceeded")
                                       for e in data.get("error", {}).get("errors", [])):
                self.dead.add(idx)
                continue
            if r.status != 200:
                raise RuntimeError(f"YouTube API {r.status}: {data.get('error', {}).get('message')}")
            return data
        raise QuotaExceeded("Квота YouTube API на сегодня закончилась на всех ключах")

    async def top_results(self, query: str, region: str, lang: str, n: int = 10) -> list[dict]:
        ck = f"top2:{region}:{lang}:{n}:{query.lower()}"
        cached = self.store.get(ck)
        if cached is not None:
            return cached

        s = await self._get("search", {"part": "snippet", "q": query, "type": "video", "maxResults": n,
                                       "regionCode": region, "relevanceLanguage": lang}, cost=100)
        ids = [it["id"]["videoId"] for it in s.get("items", []) if it.get("id", {}).get("videoId")]
        if not ids:
            self.store.put(ck, [])
            return []

        v = await self._get("videos", {"part": "snippet,statistics", "id": ",".join(ids)}, cost=1)
        ch_ids = sorted({it["snippet"]["channelId"] for it in v.get("items", [])})
        c = await self._get("channels", {"part": "statistics", "id": ",".join(ch_ids)}, cost=1)
        subs = {}
        for it in c.get("items", []):
            st = it.get("statistics", {})
            subs[it["id"]] = None if st.get("hiddenSubscriberCount") else int(st.get("subscriberCount", 0))

        out = []
        for it in v.get("items", []):
            sn, st = it["snippet"], it.get("statistics", {})
            out.append({
                "video_id": it["id"], "title": sn["title"], "channel": sn["channelTitle"],
                "published_at": sn["publishedAt"], "views": int(st.get("viewCount", 0)),
                "subs": subs.get(sn["channelId"]), "category": sn.get("categoryId"),
            })
        self.store.put(ck, out)
        return out
