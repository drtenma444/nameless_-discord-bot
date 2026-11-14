import logging
import os
from contextlib import closing

import aiohttp

from database import connect, today_str

log = logging.getLogger("nameless")

TAVILY_URL = "https://api.tavily.com/search"

# ---- limits: change these numbers to taste ----
PER_USER_PER_DAY = 5       # free searches each person gets per day
GLOBAL_PER_DAY = 25        # free searches for the whole server per day
MONTHLY_HARD_LIMIT = 950   # stops EVERYONE (even the owner). Tavily's free plan is 1000/month


def enabled() -> bool:
    return bool(os.getenv("TAVILY_API_KEY"))


def month_used() -> int:
    """All searches this month: normal ones + owner/gifted ones."""
    prefix = today_str()[:7] + "-%"
    with closing(connect()) as conn:
        a = conn.execute(
            "SELECT COALESCE(SUM(count), 0) AS t FROM web_usage WHERE day LIKE ?", (prefix,)
        ).fetchone()["t"]
        b = conn.execute(
            "SELECT COALESCE(SUM(count), 0) AS t FROM web_extra WHERE day LIKE ?", (prefix,)
        ).fetchone()["t"]
    return a + b


def usage(user_id: int):
    """Returns (free searches this user has left today, server free searches left today, gifted balance)."""
    day = today_str()
    with closing(connect()) as conn:
        mine = conn.execute(
            "SELECT count FROM web_usage WHERE day = ? AND user_id = ?", (day, user_id)
        ).fetchone()
        total = conn.execute(
            "SELECT COALESCE(SUM(count), 0) AS total FROM web_usage WHERE day = ?", (day,)
        ).fetchone()
        bonus = conn.execute(
            "SELECT balance FROM search_bonus WHERE user_id = ?", (user_id,)
        ).fetchone()
    user_left = max(0, PER_USER_PER_DAY - (mine["count"] if mine else 0))
    global_left = max(0, GLOBAL_PER_DAY - total["total"])
    return user_left, global_left, (bonus["balance"] if bonus else 0)


def _record_extra() -> None:
    with closing(connect()) as conn, conn:
        conn.execute(
            """
            INSERT INTO web_extra (day, count) VALUES (?, 1)
            ON CONFLICT(day) DO UPDATE SET count = count + 1
            """,
            (today_str(),),
        )


def _take_bonus(user_id: int) -> bool:
    with closing(connect()) as conn, conn:
        cur = conn.execute(
            "UPDATE search_bonus SET balance = balance - 1 WHERE user_id = ? AND balance > 0",
            (user_id,),
        )
        return cur.rowcount > 0


def reserve(user_id: int, is_owner: bool = False):
    """Use up one search. Returns (True, kind) or (False, reason).
    kind is "owner", "allowance" or "bonus" (needed to refund correctly)."""
    if month_used() >= MONTHLY_HARD_LIMIT:
        return False, "the monthly web search budget is used up"

    if is_owner:
        _record_extra()
        return True, "owner"

    user_left, global_left, _ = usage(user_id)
    if user_left > 0 and global_left > 0:
        with closing(connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO web_usage (day, user_id, count) VALUES (?, ?, 1)
                ON CONFLICT(day, user_id) DO UPDATE SET count = count + 1
                """,
                (today_str(), user_id),
            )
        return True, "allowance"

    # free allowance is used up (or the server cap is hit): try gifted searches
    if _take_bonus(user_id):
        _record_extra()
        return True, "bonus"

    if user_left <= 0:
        return False, "this person already used all their web searches for today"
    return False, "the server's web search limit for today is used up"


def refund(user_id: int, kind: str) -> None:
    """Give the search back if it failed."""
    day = today_str()
    with closing(connect()) as conn, conn:
        if kind == "allowance":
            conn.execute(
                "UPDATE web_usage SET count = MAX(count - 1, 0) WHERE day = ? AND user_id = ?",
                (day, user_id),
            )
        else:
            conn.execute(
                "UPDATE web_extra SET count = MAX(count - 1, 0) WHERE day = ?", (day,)
            )
            if kind == "bonus":
                conn.execute(
                    """
                    INSERT INTO search_bonus (user_id, balance) VALUES (?, 1)
                    ON CONFLICT(user_id) DO UPDATE SET balance = balance + 1
                    """,
                    (user_id,),
                )


def add_bonus(user_id: int, amount: int) -> int:
    """Gift searches. Returns the person's new gifted balance."""
    with closing(connect()) as conn, conn:
        conn.execute(
            """
            INSERT INTO search_bonus (user_id, balance) VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET balance = balance + excluded.balance
            """,
            (user_id, amount),
        )
        row = conn.execute(
            "SELECT balance FROM search_bonus WHERE user_id = ?", (user_id,)
        ).fetchone()
    return row["balance"]


async def search(query: str):
    """Returns a list of {title, url, content}, or [] if anything went wrong."""
    key = os.getenv("TAVILY_API_KEY")
    if not key:
        return []

    payload = {
        "query": query[:300],
        "search_depth": "basic",   # basic = 1 credit
        "max_results": 3,
        "include_answer": False,
    }
    try:
        timeout = aiohttp.ClientTimeout(total=12)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                TAVILY_URL,
                json=payload,
                headers={"Authorization": f"Bearer {key}"},
            ) as resp:
                if resp.status != 200:
                    log.warning("Web search failed: HTTP %s", resp.status)
                    return []
                data = await resp.json()
    except Exception:
        log.exception("Web search request failed")
        return []

    results = []
    for r in data.get("results", [])[:3]:
        results.append(
            {
                "title": str(r.get("title", ""))[:120],
                "url": str(r.get("url", "")),
                "content": str(r.get("content", ""))[:500],
            }
        )
    return results
