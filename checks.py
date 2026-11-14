import discord
from discord import app_commands


def owner_only():
    """Only the bot's owner (the account that owns the app in the Developer Portal)."""

    async def predicate(interaction: discord.Interaction) -> bool:
        if await interaction.client.is_owner(interaction.user):
            return True
        raise app_commands.CheckFailure("owner_only")

    return app_commands.check(predicate)
