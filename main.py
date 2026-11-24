
import logging
import os
import asyncio

import discord
from discord.ext import commands
from dotenv import load_dotenv

from database import init_db
from lock import InstanceLock

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
TEST_GUILD_ID = os.getenv("TEST_GUILD_ID")
LOCK_CHANNEL_ID = os.getenv("LOCK_CHANNEL_ID")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("logs/nameless.log", encoding="utf-8"),
    ],
)

log = logging.getLogger("nameless")


class NamelessBot(commands.Bot):
    def __init__(self, instance_lock):
        self.instance_lock = instance_lock
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True

        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            help_command=None,
        )

    async def setup_hook(self):
        if self.instance_lock.enabled:
            self.lock_task = asyncio.create_task(self.instance_lock.heartbeat(self))
        init_db()
        
        extensions = [
            "cogs.general",
            "cogs.fun",
            "cogs.community",
            "cogs.welcome",
            "cogs.ai",
            "cogs.study",
            "cogs.status",
            "cogs.studytools",
            "cogs.activity",
            "cogs.reminders",
            "cogs.uno",
        ]

        for extension in extensions:
            try:
                await self.load_extension(extension)
                log.info("Loaded extension: %s", extension)
            except Exception:
                log.exception("Couldn't load extension %s (skipping it)", extension)

        if TEST_GUILD_ID:
            guild = discord.Object(id=int(TEST_GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("Synced %s commands to test guild.", len(synced))
        else:
            synced = await self.tree.sync()
            log.info("Synced %s global commands.", len(synced))

    async def on_ready(self):
        log.info("Logged in as %s (ID: %s)", self.user, self.user.id)
        log.info("Connected to %s server(s).", len(self.guilds))

    async def on_command_error(self, ctx, error):
        # The bot only uses slash commands, so "@nameless_ how ..." isn't a real command.
        if isinstance(error, commands.CommandNotFound):
            return
        log.error("Command error: %s", error)


def main():
    if not TOKEN:
        raise RuntimeError(
            "DISCORD_TOKEN is missing. Check your .env file."
        )

    lock = InstanceLock(TOKEN, LOCK_CHANNEL_ID)
    if lock.enabled:
        try:
            asyncio.run(lock.acquire())   # waits here in standby if the other machine is running
        except KeyboardInterrupt:
            return

    bot = NamelessBot(lock)

    try:
        bot.run(TOKEN, log_handler=None)
    except discord.LoginFailure:
        log.error("Invalid Discord token.")
        raise
    finally:
        if lock.enabled:
            asyncio.run(lock.release())


if __name__ == "__main__":
    main()
