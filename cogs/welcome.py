import logging
from contextlib import closing

import discord
from discord import app_commands
from discord.ext import commands

from database import connect
from checks import owner_only

log = logging.getLogger("nameless")

DEFAULT_MESSAGE = (
    "Welcome to **{server}**, {user}! "
    "You're member #{count}. Glad you're here."
)

PLACEHOLDER_HELP = (
    "`{user}` mention · `{username}` name · `{server}` server name · `{count}` member number"
)


def render(template: str, member: discord.Member) -> str:
    """Fill in placeholders. Uses replace() so stray { } can't crash it."""
    return (
        template.replace("{user}", member.mention)
        .replace("{username}", member.display_name)
        .replace("{server}", member.guild.name)
        .replace("{count}", str(member.guild.member_count or 0))
    )


def build_embed(template: str, member: discord.Member) -> discord.Embed:
    embed = discord.Embed(
        title="Welcome!",
        description=render(template, member),
        color=discord.Color.blurple(),
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    return embed


def get_settings(guild_id: int):
    with closing(connect()) as conn:
        return conn.execute(
            "SELECT * FROM welcome_settings WHERE guild_id = ?", (guild_id,)
        ).fetchone()


class MessageModal(discord.ui.Modal, title="Welcome message"):
    text = discord.ui.TextInput(
        label="Message",
        style=discord.TextStyle.paragraph,
        max_length=1000,
        placeholder="Use {user}, {username}, {server}, {count}",
    )

    def __init__(self, current: str):
        super().__init__()
        self.text.default = current

    async def on_submit(self, interaction: discord.Interaction):
        with closing(connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO welcome_settings (guild_id, message) VALUES (?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET message = excluded.message
                """,
                (interaction.guild_id, str(self.text.value)),
            )

        await interaction.response.send_message(
            "Welcome message updated. Use `/welcome preview` to see it.",
            ephemeral=True,
        )


class Welcome(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    welcome = app_commands.Group(
        name="welcome",
        description="Set up welcome messages for new members.",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @welcome.command(name="setup", description="Choose the channel for welcome messages.")
    @app_commands.describe(channel="Where new members get welcomed")
    @owner_only()
    async def setup_channel(
        self, interaction: discord.Interaction, channel: discord.TextChannel
    ):
        perms = channel.permissions_for(interaction.guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            await interaction.response.send_message(
                f"I need **View Channel**, **Send Messages** and **Embed Links** in {channel.mention}.",
                ephemeral=True,
            )
            return

        with closing(connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO welcome_settings (guild_id, channel_id, enabled)
                VALUES (?, ?, 1)
                ON CONFLICT(guild_id) DO UPDATE
                SET channel_id = excluded.channel_id, enabled = 1
                """,
                (interaction.guild_id, channel.id),
            )

        await interaction.response.send_message(
            f"Welcome messages are on in {channel.mention}.\n"
            "Customize with `/welcome message`, or check it with `/welcome preview`.",
            ephemeral=True,
        )

    @welcome.command(name="message", description="Customize the welcome message.")
    @owner_only()
    async def set_message(self, interaction: discord.Interaction):
        row = get_settings(interaction.guild_id)
        current = row["message"] if row and row["message"] else DEFAULT_MESSAGE
        await interaction.response.send_modal(MessageModal(current))

    @welcome.command(name="preview", description="See what a welcome message looks like.")
    @owner_only()
    async def preview(self, interaction: discord.Interaction):
        row = get_settings(interaction.guild_id)
        template = row["message"] if row and row["message"] else DEFAULT_MESSAGE

        state = "on" if row and row["enabled"] and row["channel_id"] else "off"
        await interaction.response.send_message(
            content=f"Preview (welcome messages are currently **{state}**). "
            f"Placeholders: {PLACEHOLDER_HELP}",
            embed=build_embed(template, interaction.user),
            ephemeral=True,
        )

    @welcome.command(name="disable", description="Turn welcome messages off.")
    @owner_only()
    async def disable(self, interaction: discord.Interaction):
        with closing(connect()) as conn, conn:
            conn.execute(
                "UPDATE welcome_settings SET enabled = 0 WHERE guild_id = ?",
                (interaction.guild_id,),
            )

        await interaction.response.send_message(
            "Welcome messages are off. `/welcome setup` turns them back on.",
            ephemeral=True,
        )

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot:
            return

        row = get_settings(member.guild.id)
        if not row or not row["enabled"] or not row["channel_id"]:
            return

        channel = member.guild.get_channel(row["channel_id"])
        if channel is None:
            log.warning("Welcome channel missing in guild %s", member.guild.id)
            return

        perms = channel.permissions_for(member.guild.me)
        if not (perms.send_messages and perms.embed_links):
            log.warning("Can't send welcome in #%s (missing permissions)", channel)
            return

        template = row["message"] or DEFAULT_MESSAGE
        try:
            await channel.send(
                content=member.mention,
                embed=build_embed(template, member),
                allowed_mentions=discord.AllowedMentions(users=[member]),
            )
        except discord.HTTPException:
            log.exception("Failed to send welcome message")

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ):
        if isinstance(error, app_commands.CheckFailure):
            msg = "Only the bot owner can use that command."
        else:
            log.exception("Welcome command error", exc_info=error)
            msg = "Something went wrong on my end. Try again in a moment."

        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Welcome(bot))
