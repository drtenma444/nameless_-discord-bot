import logging
import os
import time
from collections import defaultdict, deque

import discord
from discord import app_commands
from discord.ext import commands

from llm import SimpleClient

log = logging.getLogger("nameless")

MODEL = "mistralai/mistral-medium-3.5"
NO_PINGS = discord.AllowedMentions.none()  # the AI can never ping anyone
COOLDOWN_SECONDS = 4

# Initialize the xKiro client
client = SimpleClient(
    api_key=os.getenv("XKIRO_API_KEY"),
    base_url="https://api.xkiro.com/v1"
)

# Grounded, simple, street-smart friend persona
HUMAN_PERSONA = (
    "You are nameless_, a regular guy hanging out in a Discord server with friends. "
    "Use simple, everyday words that normal people actually text—no weird metaphors, no random nonsense, "
    "and no big fancy words. "
    "Understand the context of what someone says before replying. If someone roasts or insults you, "
    "don't get confused—just roast them back casually or laugh it off like a normal friend would. "
    "Keep replies super short (1-2 sentences max), mostly lowercase, natural, and easy to understand. "
    "Reply in the same language the person uses (english, bangla, or banglish). "
    "Never act like an AI or use bullet points."
)


class AI(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # last few messages per channel, in memory only (reset when the bot restarts)
        self.history = defaultdict(lambda: deque(maxlen=8))
        self.last_used = {}

    @app_commands.command(name="chat", description="Talk to nameless_")
    @app_commands.describe(prompt="Say something")
    async def chat(self, interaction: discord.Interaction, prompt: str):
        await interaction.response.defer()

        try:
            response = await client.chat.completions.create(
                model=MODEL,
                messages=[
                    {"role": "system", "content": HUMAN_PERSONA},
                    {"role": "user", "content": f"{interaction.user.display_name}: {prompt}"}
                ],
                max_tokens=200,
                temperature=0.65
            )

            answer = (response.choices[0].message.content or "")[:1900]
            await interaction.followup.send(answer, allowed_mentions=NO_PINGS)

        except Exception as e:
            await interaction.followup.send(f"api error: `{e}`", ephemeral=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return

        me = self.bot.user
        mentioned = me in message.mentions
        replied = False
        ref = message.reference
        if ref and isinstance(ref.resolved, discord.Message):
            replied = ref.resolved.author.id == me.id
        if not (mentioned or replied):
            return

        # remove the @mention so the AI only sees what the person actually said
        text = (
            message.content
            .replace(f"<@{me.id}>", "")
            .replace(f"<@!{me.id}>", "")
            .strip()
        )
        if not text:
            await message.reply("yeah?", mention_author=False, allowed_mentions=NO_PINGS)
            return

        # per-user cooldown so nobody can spam the AI
        now = time.monotonic()
        if now - self.last_used.get(message.author.id, 0) < COOLDOWN_SECONDS:
            return
        self.last_used[message.author.id] = now

        past = self.history[message.channel.id]
        user_line = f"{message.author.display_name}: {text[:500]}"
        messages = (
            [{"role": "system", "content": HUMAN_PERSONA}]
            + list(past)
            + [{"role": "user", "content": user_line}]
        )

        try:
            async with message.channel.typing():
                response = await client.chat.completions.create(
                    model=MODEL,
                    messages=messages,
                    max_tokens=200,
                    temperature=0.65,
                )
            answer = (response.choices[0].message.content or "").strip()[:1900]
            if not answer:
                return
        except Exception:
            log.exception("AI mention chat failed")
            await message.reply(
                "my brain lagged, try again in a sec",
                mention_author=False,
                allowed_mentions=NO_PINGS,
            )
            return

        past.append({"role": "user", "content": user_line})
        past.append({"role": "assistant", "content": answer})
        await message.reply(answer, mention_author=False, allowed_mentions=NO_PINGS)


async def setup(bot):
    await bot.add_cog(AI(bot))
