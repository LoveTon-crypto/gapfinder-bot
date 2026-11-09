"""Telegram bot: send a topic → get YouTube gap queries ranked by gap score."""
from __future__ import annotations

import asyncio
import html
import os
from urllib.parse import quote_plus

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import BufferedInputFile, Message
from dotenv import load_dotenv

from core import UNITS_PER_QUERY, find_gaps, to_csv
from youtube import Store, YouTube

load_dotenv()
BOT_TOKEN = os.environ["BOT_TOKEN"]
YT_KEYS = [k.strip() for k in os.getenv("YT_API_KEYS", "").split(",") if k.strip()]
TOP_N = int(os.getenv("TOP_N", "8"))      # economical default: 8 diverse suggestions ≈ 816 units
DEEP_N = int(os.getenv("DEEP_N", "15"))   # /deep — wider scan when it is worth the quota
ALLOWED = {int(x) for x in os.getenv("ALLOWED_USERS", "").split(",") if x.strip()}  # empty = everyone

dp = Dispatcher()
busy: set[int] = set()
state: dict = {}

HELP = (
    "<b>Gap Finder</b> — ищу запросы на YouTube, где спрос есть, а хороших видео мало.\n\n"
    "Просто пришли тему, например: <code>что если</code> или <code>what if</code>\n"
    "Несколько тем — каждую с новой строки (до 5 за раз).\n"
    "/gap &lt;тема&gt; — то же самое (экономно, ~8 запросов)\n"
    "/deep &lt;тема&gt; — шире, ~15 запросов, дороже по квоте\n/quota — остаток квоты API\n\n"
    "Исключить слова: <code>zodiac myths -killer -tattoo</code>\n"
    "Игры, музыка и тру-крайм отсекаются автоматически (STOP_WORDS в .env — свой список). "
    "Запросы, где топ — клипы, выкидываются; похожие на строчки песен — понижаются.\n\n"
    "<b>Гэп-скор 0–100</b> = спрос (подсказки, видео выше своего канала) × слабость выдачи "
    "(старый топ, мало точных совпадений, малые каналы)."
)


def bar(score: int) -> str:
    n = round(score / 20)
    return "▰" * n + "▱" * (5 - n)


def format_results(seed: str, results: list[dict], limit: int = 12) -> str:
    lines = [f"🎯 <b>Гэпы по «{html.escape(seed)}»</b>\n"]
    for r in results[:limit]:
        url = f"https://www.youtube.com/results?search_query={quote_plus(r['query'])}"
        why = ", ".join(r["reasons"]) or "без явных сигналов"
        lines.append(f"<b>{r['score']}</b> {bar(r['score'])} <a href=\"{url}\">{html.escape(r['query'])}</a>\n"
                     f"<i>{html.escape(why)}</i>")
    return "\n\n".join(lines)


async def run(message: Message, seed: str, top_n: int = TOP_N) -> None:
    uid = message.from_user.id
    if ALLOWED and uid not in ALLOWED:
        return
    seed = seed.strip()
    if len(seed) < 2:
        await message.answer("Пришли тему хотя бы из пары букв.")
        return
    if uid in busy:
        await message.answer("⏳ Уже считаю предыдущий запрос, подожди.")
        return
    busy.add(uid)
    status = await message.answer("⏳ Начинаю…")

    async def progress(text: str) -> None:
        try:
            await status.edit_text(text)
        except Exception:
            pass

    try:
        yt = state["yt"]; yt.reset_spent()
        results, warning = await find_gaps(seed, yt, state["session"], top_n, progress)
        left = yt.units_left()
        spent_line = (f"💸 Потрачено {yt.spent} ед. · осталось ~{left} ≈ {left // UNITS_PER_QUERY} новых запросов"
                      if yt.spent else f"💸 Всё из кэша, квота не потрачена · осталось ~{left}")
        if not results:
            await status.edit_text(f"{html.escape(warning or 'Ничего не нашёл.')}\n\n{spent_line}")
            return
        text = format_results(seed, results)
        if warning:
            text += f"\n\n⚠️ {html.escape(warning)}"
        text += f"\n\n{spent_line}"
        await status.edit_text(text, disable_web_page_preview=True)
        await message.answer_document(BufferedInputFile(to_csv(results), filename="gaps.csv"),
                                      caption=f"Все {len(results)} запросов с метриками")
    except Exception as e:
        await status.edit_text(f"❌ Ошибка: {html.escape(str(e))}")
    finally:
        busy.discard(uid)


@dp.message(CommandStart())
async def start(message: Message) -> None:
    await message.answer(HELP)


@dp.message(Command("gap"))
async def gap(message: Message, command: CommandObject) -> None:
    await run(message, command.args or "")


@dp.message(Command("deep"))
async def deep(message: Message, command: CommandObject) -> None:
    await run(message, command.args or "", DEEP_N)


@dp.message(Command("quota"))
async def quota(message: Message) -> None:
    left = state["yt"].units_left()
    await message.answer(f"Осталось ~{left} единиц ≈ {left // UNITS_PER_QUERY} новых запросов сегодня.\n"
                         f"Повторы из кэша ({os.getenv('CACHE_DAYS', '7')} дн.) бесплатны. Сброс в полночь по Тихоокеанскому времени.")


MAX_SEEDS = 5


@dp.message(F.text & ~F.text.startswith("/"))
async def plain(message: Message) -> None:
    seeds = [s.strip() for s in message.text.splitlines() if s.strip()]
    if len(seeds) > MAX_SEEDS:
        await message.answer(f"Беру первые {MAX_SEEDS} тем из {len(seeds)} — квота не резиновая.")
    for seed in seeds[:MAX_SEEDS]:
        await run(message, seed)


async def main() -> None:
    async with aiohttp.ClientSession(headers={"User-Agent": "Mozilla/5.0"}) as session:
        state["session"] = session
        state["yt"] = YouTube(YT_KEYS, session, Store(os.getenv("DB_PATH", "gapfinder.db")))
        bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
        await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
