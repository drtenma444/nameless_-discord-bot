import os
import discord
from discord import app_commands
from discord.ext import commands
from llm import SimpleClient

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
    "Never act like an AI or use bullet points."
)

class AI(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="chat", description="Talk to nameless_")
    @app_commands.describe(prompt="Say something")
    async def chat(self, interaction: discord.Interaction, prompt: str):
        await interaction.response.defer()

        try:
            response = await client.chat.completions.create(
                model="mistralai/mistral-medium-3.5",
                messages=[
                    {"role": "system", "content": HUMAN_PERSONA},
                    {"role": "user", "content": f"{interaction.user.display_name}: {prompt}"}
                ],
                max_tokens=200,
                temperature=0.65  # Balanced: keeps replies funny and grounded without making up nonsense
            )

            answer = response.choices[0].message.content
            await interaction.followup.send(answer)

        except Exception as e:
            await interaction.followup.send(f"api error: `{e}`", ephemeral=True)

async def setup(bot):
    await bot.add_cog(AI(bot))
