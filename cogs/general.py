
import discord
from discord import app_commands
from discord.ext import commands


class General(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(
        name="help",
        description="See what I can do."
    )
    async def help(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="nameless_",
            description="A little something for your community.",
            color=discord.Color.blurple(),
        )

        embed.add_field(
            name="General",
            value=(
                "`/ping` — Check my latency\n"
                "`/server` — Server information\n"
                "`/userinfo` — Member information\n"
                "`/avatar` — View an avatar"
            ),
            inline=False,
        )

        embed.add_field(
            name="Fun",
            value=(
                "`/chat` — Talk to me\n"
                "`/roll` — Roll a dice\n"
                "`/coinflip` — Flip a coin\n"
                "`/choose` — Pick between options\n"
                "`/joke` — Get a random joke"
            ),
            inline=False,
        )

        embed.add_field(
            name="Community",
            value="`/poll` — Create a poll",
            inline=False,
        )

        embed.set_footer(text="Simple tools, whenever you need them.")

        await interaction.response.send_message(embed=embed)

    @app_commands.command(
        name="ping",
        description="Check whether I'm responding."
    )
    async def ping(self, interaction: discord.Interaction):
        latency = round(self.bot.latency * 1000)

        embed = discord.Embed(
            title="Pong! 🏓",
            description=f"Gateway latency: **{latency} ms**",
            color=discord.Color.green(),
        )

        await interaction.response.send_message(embed=embed)

    @app_commands.command(
        name="server",
        description="See information about this server."
    )
    @app_commands.guild_only()
    async def server(self, interaction: discord.Interaction):
        guild = interaction.guild

        embed = discord.Embed(
            title=guild.name,
            color=discord.Color.blurple(),
            timestamp=guild.created_at,
        )

        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)

        embed.add_field(
            name="Members",
            value=str(guild.member_count or "Unknown"),
            inline=True,
        )

        embed.add_field(
            name="Owner",
            value=f"<@{guild.owner_id}>",
            inline=True,
        )

        embed.add_field(
            name="Created",
            value=discord.utils.format_dt(guild.created_at, style="D"),
            inline=False,
        )

        embed.set_footer(text=f"Server ID: {guild.id}")

        await interaction.response.send_message(embed=embed)

    @app_commands.command(
        name="userinfo",
        description="View information about a server member."
    )
    @app_commands.guild_only()
    async def userinfo(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
    ):
        member = member or interaction.user

        embed = discord.Embed(
            title=member.display_name,
            color=discord.Color.blurple(),
        )

        embed.set_thumbnail(url=member.display_avatar.url)

        embed.add_field(
            name="Username",
            value=str(member),
            inline=True,
        )

        embed.add_field(
            name="Account created",
            value=discord.utils.format_dt(member.created_at, style="D"),
            inline=False,
        )

        embed.add_field(
            name="Joined server",
            value=(
                discord.utils.format_dt(member.joined_at, style="D")
                if member.joined_at else "Unknown"
            ),
            inline=False,
        )

        embed.add_field(
            name="Roles",
            value=str(max(0, len(member.roles) - 1)),
            inline=True,
        )

        await interaction.response.send_message(embed=embed)

    @app_commands.command(
        name="avatar",
        description="View someone's profile picture."
    )
    async def avatar(
        self,
        interaction: discord.Interaction,
        member: discord.User | None = None,
    ):
        user = member or interaction.user

        embed = discord.Embed(
            title=f"{user.display_name}'s avatar",
            color=discord.Color.blurple(),
        )

        embed.set_image(url=user.display_avatar.url)

        await interaction.response.send_message(embed=embed)


async def setup(bot):
    await bot.add_cog(General(bot))
