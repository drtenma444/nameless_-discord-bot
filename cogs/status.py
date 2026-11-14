import logging
import platform
import subprocess
import time
from contextlib import closing
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from checks import owner_only
from database import connect

log = logging.getLogger("nameless")
ROOT = Path(__file__).resolve().parent.parent


def get_version():
    """Short git commit hash and the commit message the bot is running."""
    try:
        out = subprocess.run(
            ["git", "log", "-1", "--pretty=%h|%s"],
            capture_output=True, text=True, timeout=5, cwd=ROOT,
        )
        short, _, subject = out.stdout.strip().partition("|")
        return short or "unknown", subject
    except Exception:
        return "unknown", ""


def fmt_uptime(seconds: float) -> str:
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


class Status(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.started = time.time()
        self.announced = False

    @commands.Cog.listener()
    async def on_ready(self):
        # on_ready can fire again after a reconnect, so only announce once per start
        if self.announced:
            return
        self.announced = True

        short, subject = get_version()
        with closing(connect()) as conn:
            rows = conn.execute("SELECT * FROM status_channel").fetchall()

        for row in rows:
            channel = self.bot.get_channel(row["channel_id"])
            if channel is None:
                continue
            try:
                await channel.send(
                    f"🟢 back online · version `{short}` · {subject}"[:1900],
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                log.warning("Couldn't post the startup message in channel %s", row["channel_id"])

    @app_commands.command(name="status", description="Show the bot's version and uptime.")
    async def status(self, interaction: discord.Interaction):
        short, subject = get_version()
        await interaction.response.send_message(
            f"**version:** `{short}` {subject}\n"
            f"**uptime:** {fmt_uptime(time.time() - self.started)}\n"
            f"**ping:** {round(self.bot.latency * 1000)} ms\n"
            f"**python:** {platform.python_version()}",
            ephemeral=True,
        )

    @app_commands.command(
        name="setstatuschannel",
        description="Pick the channel where I post when I restart.",
    )
    @app_commands.guild_only()
    @owner_only()
    async def set_status_channel(
        self, interaction: discord.Interaction, channel: discord.TextChannel
    ):
        perms = channel.permissions_for(interaction.guild.me)
        if not (perms.view_channel and perms.send_messages):
            await interaction.response.send_message(
                f"I need **View Channel** and **Send Messages** in {channel.mention}.",
                ephemeral=True,
            )
            return

        with closing(connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO status_channel (guild_id, channel_id) VALUES (?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET channel_id = excluded.channel_id
                """,
                (interaction.guild_id, channel.id),
            )

        short, subject = get_version()
        await channel.send(
            f"✅ status channel set · running version `{short}` · {subject}"[:1900],
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await interaction.response.send_message(
            f"Done. I'll post in {channel.mention} every time I restart.",
            ephemeral=True,
        )

    @app_commands.command(
        name="clearstatuschannel", description="Stop posting restart messages."
    )
    @app_commands.guild_only()
    @owner_only()
    async def clear_status_channel(self, interaction: discord.Interaction):
        with closing(connect()) as conn, conn:
            conn.execute(
                "DELETE FROM status_channel WHERE guild_id = ?", (interaction.guild_id,)
            )
        await interaction.response.send_message(
            "Okay, no more restart messages.", ephemeral=True
        )

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ):
        if isinstance(error, app_commands.CheckFailure):
            msg = "Only the bot owner can use that command."
        else:
            log.exception("Status command error", exc_info=error)
            msg = "Something went wrong on my end. Try again in a moment."

        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Status(bot))
