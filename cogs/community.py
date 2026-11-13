
import discord
from discord import app_commands
from discord.ext import commands


class PollButton(discord.ui.Button):
    def __init__(self, poll, option_index, label):
        super().__init__(
            label=label,
            style=discord.ButtonStyle.primary,
        )
        self.poll = poll
        self.option_index = option_index

    async def callback(self, interaction: discord.Interaction):
        self.poll.votes[interaction.user.id] = self.option_index

        await interaction.response.edit_message(
            embed=self.poll.make_embed(),
            view=self.poll,
        )


class PollView(discord.ui.View):
    def __init__(self, question, option_a, option_b):
        super().__init__(timeout=86400)

        self.question = question
        self.options = [option_a, option_b]
        self.votes = {}
        self.message = None

        self.add_item(PollButton(self, 0, option_a))
        self.add_item(PollButton(self, 1, option_b))

    def make_embed(self):
        counts = [
            sum(1 for vote in self.votes.values() if vote == i)
            for i in range(2)
        ]

        total = sum(counts)

        embed = discord.Embed(
            title=f"📊 {self.question}",
            color=discord.Color.blurple(),
        )

        for i, option in enumerate(self.options):
            percentage = (
                round(counts[i] / total * 100)
                if total else 0
            )

            embed.add_field(
                name=option,
                value=f"**{counts[i]}** votes · {percentage}%",
                inline=True,
            )

        embed.set_footer(
            text=f"{total} vote(s) • You can change your vote."
        )

        return embed

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True

        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.NotFound:
                pass
            except discord.HTTPException:
                pass


class Community(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(
        name="poll",
        description="Create a two-option poll."
    )
    @app_commands.guild_only()
    @app_commands.describe(
        question="What do you want to ask?",
        option_a="First option",
        option_b="Second option",
    )
    async def poll(
        self,
        interaction: discord.Interaction,
        question: app_commands.Range[str, 1, 180],
        option_a: app_commands.Range[str, 1, 80],
        option_b: app_commands.Range[str, 1, 80],
    ):
        if option_a.casefold() == option_b.casefold():
            await interaction.response.send_message(
                "Please provide two different options.",
                ephemeral=True,
            )
            return

        view = PollView(question, option_a, option_b)

        await interaction.response.send_message(
            embed=view.make_embed(),
            view=view,
        )

        view.message = await interaction.original_response()


async def setup(bot):
    await bot.add_cog(Community(bot))
