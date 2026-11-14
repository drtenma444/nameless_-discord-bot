import asyncio
import logging
import os
import re
import socket
import time
import uuid
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import aiohttp

log = logging.getLogger("nameless")

API = "https://discord.com/api/v10"
MARKER = "🔒 lease"

LEASE_TIMEOUT = 75      # no heartbeat for this long = the other bot is considered gone
HEARTBEAT_EVERY = 20    # the running bot renews the lock this often
STANDBY_POLL = 15       # a waiting bot checks the lock this often


class LockError(RuntimeError):
    """The lock channel is set up wrong (bad id or missing permissions)."""


class InstanceLock:
    def __init__(self, token, channel_id):
        self.token = token
        self.channel_id = int(channel_id) if channel_id else None
        self.host = os.getenv("HOST_NAME") or socket.gethostname()
        self.me = f"{self.host}-{uuid.uuid4().hex[:6]}"
        self.bot_user_id = None
        self.message_id = None

    @property
    def enabled(self) -> bool:
        return self.channel_id is not None

    # ---------- talking to Discord ----------

    def _headers(self):
        return {"Authorization": f"Bot {self.token}", "User-Agent": "nameless-lock"}

    async def _request(self, session, method, path, **kwargs):
        for attempt in range(2):
            async with session.request(
                method, f"{API}{path}", headers=self._headers(), **kwargs
            ) as resp:
                if resp.status == 429 and attempt == 0:
                    data = await resp.json(content_type=None)
                    await asyncio.sleep(min(float(data.get("retry_after", 2)), 10))
                    continue
                if resp.status in (401, 403, 404):
                    raise LockError(
                        f"Discord answered {resp.status} for {path}. Check LOCK_CHANNEL_ID "
                        "and the bot's permissions in that channel."
                    )
                if resp.status >= 400:
                    raise aiohttp.ClientError(f"HTTP {resp.status}")
                data = await resp.json(content_type=None)
                return data, resp.headers.get("Date")
        raise aiohttp.ClientError("rate limited")

    async def _read(self, session):
        """Returns the current lease as a dict, or None if there isn't one yet."""
        if self.bot_user_id is None:
            me, _ = await self._request(session, "GET", "/users/@me")
            self.bot_user_id = me["id"]

        msgs, date_header = await self._request(
            session, "GET", f"/channels/{self.channel_id}/messages", params={"limit": 20}
        )
        for m in msgs:  # newest first
            if m["author"]["id"] != self.bot_user_id or not m["content"].startswith(MARKER):
                continue
            text = m["content"]
            holder = re.search(r"holder=(\S+)", text)
            host = re.search(r"host=(\S+)", text)
            state = re.search(r"state=(\w+)", text)
            stamp = datetime.fromisoformat(m.get("edited_timestamp") or m["timestamp"])
            try:
                server_now = parsedate_to_datetime(date_header)
            except Exception:
                server_now = datetime.now(timezone.utc)
            return {
                "id": m["id"],
                "holder": holder.group(1) if holder else "",
                "host": host.group(1) if host else "?",
                "state": state.group(1) if state else "active",
                "age": (server_now - stamp).total_seconds(),
            }
        return None

    async def _write(self, session, state="active"):
        content = (
            f"{MARKER} | holder={self.me} | host={self.host} | state={state} "
            f"| beat={int(time.time())}"
        )
        if self.message_id is None:
            data, _ = await self._request(
                session,
                "POST",
                f"/channels/{self.channel_id}/messages",
                json={"content": content, "allowed_mentions": {"parse": []}},
            )
            self.message_id = data["id"]
        else:
            await self._request(
                session,
                "PATCH",
                f"/channels/{self.channel_id}/messages/{self.message_id}",
                json={"content": content},
            )

    # ---------- the lock itself ----------

    async def acquire(self):
        """Waits (in standby) until this machine is the one that should run the bot."""
        timeout = aiohttp.ClientTimeout(total=15)
        waiting_for = None
        async with aiohttp.ClientSession(timeout=timeout) as session:
            while True:
                try:
                    lease = await self._read(session)
                    free = (
                        lease is None
                        or lease["holder"] == self.me
                        or lease["state"] == "released"
                        or lease["age"] > LEASE_TIMEOUT
                    )
                    if free:
                        self.message_id = lease["id"] if lease else None
                        await self._write(session, "active")
                        await asyncio.sleep(3)  # lets a competing claim land too
                        check = await self._read(session)
                        if check and check["holder"] == self.me:
                            self.message_id = check["id"]
                            log.info("Lock acquired by %s. Starting the bot.", self.host)
                            return
                        continue  # the other machine won the race

                    if waiting_for != lease["host"]:
                        log.info(
                            "Standby: %s is running the bot. Waiting until it stops or goes offline...",
                            lease["host"],
                        )
                        waiting_for = lease["host"]
                except LockError:
                    raise
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    log.warning("Couldn't reach Discord for the lock, retrying...")
                await asyncio.sleep(STANDBY_POLL)

    async def heartbeat(self, bot):
        """Runs while the bot is up: renews the lock, quits if someone else took it."""
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            while True:
                await asyncio.sleep(HEARTBEAT_EVERY)
                try:
                    lease = await self._read(session)
                    if lease is not None and lease["holder"] != self.me:
                        log.error(
                            "%s took over the lock. Shutting this copy down.", lease["host"]
                        )
                        await bot.close()
                        return
                    self.message_id = lease["id"] if lease else None
                    await self._write(session, "active")
                except LockError as e:
                    log.error("Lock problem (bot keeps running): %s", e)
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    log.warning("Lock heartbeat failed (internet?), will retry")

    async def release(self):
        """Called on a clean shutdown so the other machine can take over quickly."""
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                lease = await self._read(session)
                if lease and lease["holder"] == self.me:
                    self.message_id = lease["id"]
                    await self._write(session, "released")
                    log.info("Lock released.")
        except Exception:
            log.warning("Couldn't release the lock cleanly; the other machine will take over in about a minute.")
