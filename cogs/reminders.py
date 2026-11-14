import logging
import re
import time
from contextlib import closing

import discord
from discord import app_commands
from discord.ext import commands, tasks

from database import connect

log = logging.getLogger("nameless")

MAX_ACTIVE = 10            # reminders one person can have at the same time
MIN_SECONDS = 60           # shortest reminder: 1 minute
MAX_SECONDS = 30 * 86400   # longest reminder: 30 days
UNITS = {"d": 86400, "h": 3600, "m": 60}


def parse_duration(text: str):
    """'1h30m' -> 5400 seconds. Returns None if it can't be understood."""
    text = text.strip().lower().replace(" ", "")
    if not re.fullmatch(r"(?:\d+[dhm])+", text):
        return None
    return sum(int(n) * UNITS[u] for n, u in re.findall(r"(\d+)([dhm])", text))


class Reminders(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        self.check_loop.start()

    async def cog_unload(self):
        self.check_loop.cancel()

    remind = app_commands.Group(name="remind", description="Set reminders for yourself.")

    # ---------- commands ----------

    @remind.command(name="add", description="Set a reminder.")
    @app_commands.describe(
        when="When, like 10m, 2h, 1d or 1h30m",
        message="What should I remind you about?",
        where="Where to remind you",
    )
    @app_commands.choices(
        where=[
            app_commands.Choice(name="here (ping me in this channel)", value="here"),
            app_commands.Choice(name="dm (message me privately)", value="dm"),
        ]
    )
    async def add(
        self,
        interaction: discord.Interaction,
        when: str,
        message: app_commands.Range[str, 1, 300],
        where: app_commands.Choice[str] = None,
    ):
        seconds = parse_duration(when)
        if seconds is None:
            await interaction.response.send_message(
                "i couldn't read that time. try something like `10m`, `2h`, `1d` or `1h30m`",
                ephemeral=True,
            )
            return
        if seconds < MIN_SECONDS or seconds > MAX_SECONDS:
            await interaction.response.send_message(
                "pick a time between 1 minute and 30 days", ephemeral=True
            )
            return

        with closing(connect()) as conn:
            active = conn.execute(
                "SELECT COUNT(*) AS n FROM reminders WHERE user_id = ?",
                (interaction.user.id,),
            ).fetchone()["n"]
        if active >= MAX_ACTIVE:
            await interaction.response.send_message(
                f"you already have {MAX_ACTIVE} reminders. cancel one with `/remind cancel` first",
                ephemeral=True,
            )
            return

        due = int(time.time()) + seconds
        via_dm = 1 if (where and where.value == "dm") else 0
        with closing(connect()) as conn, conn:
            cur = conn.execute(
                """
                INSERT INTO reminders (user_id, channel_id, message, due_ts, via_dm)
                VALUES (?, ?, ?, ?, ?)
                """,
                (interaction.user.id, interaction.channel_id, message, due, via_dm),
            )
            rid = cur.lastrowid

        await interaction.response.send_message(
            f"got it, i'll remind you <t:{due}:R> (<t:{due}:f>). reminder id: **{rid}**",
            ephemeral=True,
        )

    @remind.command(name="list", description="See your pending reminders.")
    async def list_reminders(self, interaction: discord.Interaction):
        with closing(connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM reminders WHERE user_id = ? ORDER BY due_ts",
                (interaction.user.id,),
            ).fetchall()

        if not rows:
            await interaction.response.send_message(
                "you have no reminders right now", ephemeral=True
            )
            return

        lines = [
            f"**#{r['id']}** · <t:{r['due_ts']}:R> · {discord.utils.escape_markdown(r['message'][:60])}"
            for r in rows
        ]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @remind.command(name="cancel", description="Delete one of your reminders.")
    @app_commands.describe(id="The reminder id (see /remind list)")
    async def cancel(self, interaction: discord.Interaction, id: int):
        with closing(connect()) as conn, conn:
            cur = conn.execute(
                "DELETE FROM reminders WHERE id = ? AND user_id = ?",
                (id, interaction.user.id),
            )
        if cur.rowcount:
            await interaction.response.send_message(
                f"reminder #{id} deleted", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "i couldn't find a reminder of yours with that id", ephemeral=True
            )

    # ---------- sending reminders ----------

    @tasks.loop(seconds=30)
    async def check_loop(self):
        try:
            now = int(time.time())
            with closing(connect()) as conn:
                due = conn.execute(
                    "SELECT * FROM reminders WHERE due_ts <= ? ORDER BY due_ts LIMIT 20",
                    (now,),
                ).fetchall()

            for r in due:
                # delete first, so a reminder can never be sent twice
                with closing(connect()) as conn, conn:
                    cur = conn.execute("DELETE FROM reminders WHERE id = ?", (r["id"],))
                if cur.rowcount == 0:
                    continue
                await self.deliver(r, now)
        except Exception:
            log.exception("Reminder check failed")

    @check_loop.before_loop
    async def before_check(self):
        await self.bot.wait_until_ready()

    async def deliver(self, r, now: int):
        late = (
            "\n-# sorry, this one was late, i was offline"
            if now - r["due_ts"] > 120
            else ""
        )

        if r["via_dm"]:
            try:
                user = await self.bot.fetch_user(r["user_id"])
                await user.send(f"⏰ reminder: {r['message']}{late}"[:1900])
                return
            except discord.HTTPException:
                pass  # DMs closed, fall back to the channel

        channel = self.bot.get_channel(r["channel_id"]) if r["channel_id"] else None
        if channel is None and r["channel_id"]:
            try:
                channel = await self.bot.fetch_channel(r["channel_id"])
            except discord.HTTPException:
                return
        if channel is None:
            return

        try:
            await channel.send(
                f"⏰ <@{r['user_id']}> reminder: {r['message']}{late}"[:1900],
                allowed_mentions=discord.AllowedMentions(
                    users=[discord.Object(id=r["user_id"])]
                ),
            )
        except discord.HTTPException:
            log.warning("Couldn't deliver reminder %s", r["id"])

    # ---------- errors ----------

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ):
        log.exception("Reminder command error", exc_info=error)
        msg = "Something went wrong on my end. Try again in a moment."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Reminders(bot))
