"""Run the pipeline from the terminal without Telegram:  python cli.py "что если" --top 10"""
import argparse
import asyncio
import os

import aiohttp
from dotenv import load_dotenv

from core import find_gaps, to_csv
from youtube import Store, YouTube


async def main() -> None:
    load_dotenv()
    p = argparse.ArgumentParser()
    p.add_argument("seed")
    p.add_argument("--top", type=int, default=int(os.getenv("TOP_N", "8")))
    a = p.parse_args()
    keys = [k.strip() for k in os.getenv("YT_API_KEYS", "").split(",") if k.strip()]

    async def progress(t: str) -> None:
        print(t)

    async with aiohttp.ClientSession(headers={"User-Agent": "Mozilla/5.0"}) as s:
        yt = YouTube(keys, s, Store(os.getenv("DB_PATH", "gapfinder.db")))
        results, warning = await find_gaps(a.seed, yt, s, a.top, progress)
    print()
    for r in results:
        print(f"{r['score']:>3}  {r['query']:<45} {', '.join(r['reasons'])}")
    if warning:
        print("\n⚠️", warning)
    with open("gaps.csv", "wb") as f:
        f.write(to_csv(results))
    print("\nСохранено в gaps.csv")


if __name__ == "__main__":
    asyncio.run(main())
