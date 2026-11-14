import logging
import time
from collections import Counter
from contextlib import closing

import discord
from discord import app_commands
from discord.ext import commands, tasks

from database import connect, days_ago_str, today_str

log = logging.getLogger("nameless")

ACTIVE_WINDOW = 15 * 60   # "chatting right now" = sent a message in the last 15 minutes
KEEP_DAYS = 90            # older counts are deleted


class Activity(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.pending = Counter()   # (guild_id, user_id, day) -> messages not saved yet
        self.recent = {}           # guild_id -> {user_id: time of last message}

    async def cog_load(self):
        with closing(connect()) as conn, conn:
            conn.execute("DELETE FROM activity WHERE day < ?", (days_ago_str(KEEP_DAYS),))
        self.flush_loop.start()

    async def cog_unload(self):
        self.flush_loop.cancel()
        self.flush()

    # ---------- counting (message text is never stored) ----------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return
        self.pending[(message.guild.id, message.author.id, today_str())] += 1
        self.recent.setdefault(message.guild.id, {})[message.author.id] = time.monotonic()

    def flush(self):
        """Save the in-memory counts to the database."""
        if not self.pending:
            return
        rows = [(g, u, d, c) for (g, u, d), c in self.pending.items()]
        self.pending.clear()
        try:
            with closing(connect()) as conn, conn:
                conn.executemany(
                    """
                    INSERT INTO activity (guild_id, user_id, day, count) VALUES (?, ?, ?, ?)
                    ON CONFLICT(guild_id, user_id, day)
                    DO UPDATE SET count = count + excluded.count
                    """,
                    rows,
                )
        except Exception:
            log.exception("Couldn't save activity counts, will retry")
            for g, u, d, c in rows:
                self.pending[(g, u, d)] += c

    @tasks.loop(seconds=60)
    async def flush_loop(self):
        self.flush()

    # ---------- reading ----------

    def top(self, guild_id: int, since_day: str, limit: int = 5):
        self.flush()
        with closing(connect()) as conn:
            return conn.execute(
                """
                SELECT user_id, SUM(count) AS total FROM activity
                WHERE guild_id = ? AND day >= ?
                GROUP BY user_id ORDER BY total DESC LIMIT ?
                """,
                (guild_id, since_day, limit),
            ).fetchall()

    def active_now(self, guild_id: int):
        now = time.monotonic()
        users = self.recent.get(guild_id, {})
        for uid in [u for u, t in users.items() if now - t > ACTIVE_WINDOW]:
            del users[uid]
        return sorted(users, key=lambda u: users[u], reverse=True)

    @staticmethod
    def name_of(guild: discord.Guild, user_id: int) -> str:
        member = guild.get_member(user_id)
        return member.display_name if member else "someone who left"

    def _lines(self, guild, rows):
        return [
            f"{i}. {self.name_of(guild, r['user_id'])}: {r['total']}"
            for i, r in enumerate(rows, start=1)
        ]

    def snapshot(self, guild: discord.Guild) -> str:
        """Short text summary that gets given to the AI."""
        now_names = [self.name_of(guild, u) for u in self.active_now(guild.id)[:8]]
        today = self._lines(guild, self.top(guild.id, today_str()))
        week = self._lines(guild, self.top(guild.id, days_ago_str(6)))
        return (
            f"Members: {guild.member_count}\n"
            f"Chatting in the last 15 minutes: {', '.join(now_names) or 'nobody'}\n"
            f"Most messages today: {'; '.join(today) or 'no data yet'}\n"
            f"Most messages in the last 7 days: {'; '.join(week) or 'no data yet'}\n"
            "(These are message counts only, tracked since the bot started counting. "
            "Online/offline status is not available.)"
        )

    # ---------- command ----------

    @app_commands.command(name="active", description="See who's active in the server.")
    @app_commands.guild_only()
    @app_commands.checks.cooldown(1, 10.0, key=lambda i: i.user.id)
    async def active(self, interaction: discord.Interaction):
        guild = interaction.guild
        now_names = [self.name_of(guild, u) for u in self.active_now(guild.id)[:10]]
        today = self._lines(guild, self.top(guild.id, today_str()))
        week = self._lines(guild, self.top(guild.id, days_ago_str(6)))

        embed = discord.Embed(title="Server activity", color=discord.Color.blurple())
        embed.add_field(
            name="Chatting right now (last 15 min)",
            value=discord.utils.escape_markdown(", ".join(now_names)) or "nobody",
            inline=False,
        )
        embed.add_field(
            name="Most messages today",
            value=discord.utils.escape_markdown("\n".join(today)) or "no data yet",
            inline=False,
        )
        embed.add_field(
            name="Most messages, last 7 days",
            value=discord.utils.escape_markdown("\n".join(week)) or "no data yet",
            inline=False,
        )
        embed.set_footer(text="Counts messages only, never what's said.")
        await interaction.response.send_message(embed=embed)

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ):
        if isinstance(error, app_commands.CommandOnCooldown):
            msg = f"slow down, try again in {error.retry_after:.0f}s"
        else:
            log.exception("Activity command error", exc_info=error)
            msg = "Something went wrong on my end. Try again in a moment."

        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Activity(bot))
