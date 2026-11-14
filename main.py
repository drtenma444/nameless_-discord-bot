
import logging
import os

import discord
from discord.ext import commands
from dotenv import load_dotenv

from database import init_db

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
TEST_GUILD_ID = os.getenv("TEST_GUILD_ID")

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
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True

        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            help_command=None,
        )

    async def setup_hook(self):
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
        ]

        for extension in extensions:
            await self.load_extension(extension)
            log.info("Loaded extension: %s", extension)

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


def main():
    if not TOKEN:
        raise RuntimeError(
            "DISCORD_TOKEN is missing. Check your .env file."
        )

    bot = NamelessBot()

    try:
        bot.run(TOKEN)
    except discord.LoginFailure:
        log.error("Invalid Discord token.")
        raise


if __name__ == "__main__":
    main()
