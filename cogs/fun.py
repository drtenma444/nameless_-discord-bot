import random

import discord
from discord import app_commands
from discord.ext import commands
from cogs.ai import client, HUMAN_PERSONA


class Fun(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.recent_jokes = []

    @app_commands.command(
        name="roll",
        description="Roll a dice."
    )
    @app_commands.describe(sides="Number of sides (2–1000)")
    async def roll(
        self,
        interaction: discord.Interaction,
        sides: app_commands.Range[int, 2, 1000] = 6,
    ):
        result = random.randint(1, sides)

        embed = discord.Embed(
            title="🎲 Dice roll",
            description=f"You rolled **{result}** out of **{sides}**.",
            color=discord.Color.blurple(),
        )

        await interaction.response.send_message(embed=embed)

    @app_commands.command(
        name="coinflip",
        description="Flip a coin."
    )
    async def coinflip(self, interaction: discord.Interaction):
        result = random.choice(["Heads", "Tails"])

        embed = discord.Embed(
            title="🪙 Coin flip",
            description=f"**{result}!**",
            color=discord.Color.gold(),
        )

        await interaction.response.send_message(embed=embed)

    @app_commands.command(
        name="choose",
        description="Let me pick one option for you."
    )
    @app_commands.describe(
        options="Separate your choices with commas."
    )
    async def choose(
        self,
        interaction: discord.Interaction,
        options: app_commands.Range[str, 3, 500],
    ):
        choices = [
            option.strip()
            for option in options.split(",")
            if option.strip()
        ]

        if len(choices) < 2:
            await interaction.response.send_message(
                "Give me at least two options, separated by commas.",
                ephemeral=True,
            )
            return

        if len(choices) > 20:
            await interaction.response.send_message(
                "That's a lot of choices! Please keep it to 20 or fewer.",
                ephemeral=True,
            )
            return

        result = random.choice(choices)

        embed = discord.Embed(
            title="My pick",
            description=f"I choose **{result}**.",
            color=discord.Color.blurple(),
        )

        await interaction.response.send_message(embed=embed)

    @app_commands.command(
        name="joke",
        description="Say something funny."
    )
    @app_commands.describe(topic="Optional topic")
    async def joke(self, interaction: discord.Interaction, topic: str = None):
        await interaction.response.defer()

        user_prompt = (
            f"say one funny, relatable roast or observation about {topic}"
            if topic
            else "say one random, funny, relatable thought or hot take about everyday life, school, or friends"
        )

        messages = [{"role": "system", "content": HUMAN_PERSONA}]
        if self.recent_jokes:
            messages.append({
                "role": "system",
                "content": f"Do NOT repeat or reuse anything similar to these: {self.recent_jokes}"
            })
        messages.append({
            "role": "system",
            "content": "STRICT RULE: Never tell a 'Why did X' or setup-and-punchline dad joke. Just say 1-2 funny, relatable sentences like a normal person texting."
        })
        messages.append({"role": "user", "content": f"{interaction.user.display_name}: {user_prompt}"})

        try:
            response = await client.chat.completions.create(
                model="mistralai/mistral-medium-3.5",
                messages=messages,
                max_tokens=120,
                temperature=0.75
            )

            answer = response.choices[0].message.content

            self.recent_jokes.append(answer)
            if len(self.recent_jokes) > 5:
                self.recent_jokes.pop(0)

            await interaction.followup.send(answer)

        except Exception as e:
            await interaction.followup.send(f"api error: `{e}`", ephemeral=True)


async def setup(bot):
    await bot.add_cog(Fun(bot))
