import asyncio
import logging
import random
import time

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger("nameless")

NO_PINGS = discord.AllowedMentions.none()

MAX_PLAYERS = 6
START_CARDS = 7
TURN_SECONDS = 90      # time allowed per turn
LOBBY_SECONDS = 300    # how long the lobby stays open

COLORS = {"R": "🟥", "Y": "🟨", "G": "🟩", "B": "🟦"}
COLOR_NAMES = {"R": "Red", "Y": "Yellow", "G": "Green", "B": "Blue"}
SYMBOLS = {
    "skip": "Skip", "reverse": "Reverse", "draw2": "+2",
    "wild": "Wild", "wild4": "Wild +4",
}
COLOR_ORDER = {"R": 0, "Y": 1, "G": 2, "B": 3, "W": 4}
VALUE_ORDER = {
    v: i for i, v in enumerate(
        ["0", "1", "2", "3", "4", "5", "6", "7", "8", "9",
         "skip", "reverse", "draw2", "wild", "wild4"]
    )
}
NUMBERS = set("0123456789")


# ---------------- cards ----------------
# A card is a short text code like "R:5" (red 5), "Y:skip", "W:wild4".

def clean(user) -> str:
    return discord.utils.escape_markdown(user.display_name)


def label(card: str, emoji: bool = True) -> str:
    color, value = card[0], card[2:]
    text = SYMBOLS.get(value, value)
    if not emoji:
        return f"{COLOR_NAMES.get(color, '')} {text}".strip()
    return f"{COLORS.get(color, '🌈')} {text}"


def sort_hand(hand):
    return sorted(hand, key=lambda c: (COLOR_ORDER[c[0]], VALUE_ORDER[c[2:]]))


def build_deck():
    deck = []
    for color in "RYGB":
        deck.append(f"{color}:0")
        for v in list("123456789") + ["skip", "reverse", "draw2"]:
            deck += [f"{color}:{v}"] * 2
    deck += ["W:wild"] * 4 + ["W:wild4"] * 4
    random.shuffle(deck)
    return deck


# ---------------- the game itself ----------------

class UnoGame:
    def __init__(self, host, channel):
        self.host = host
        self.channel = channel
        self.players = [host]
        self.hands = {}
        self.deck = []
        self.discard = []
        self.color = None
        self.turn = 0
        self.direction = 1
        self.drawn = None          # card the current player just drew
        self.strikes = {}          # user id -> timeouts in a row
        self.log = ""
        self.started = False
        self.ended = False
        self.message_id = None
        self.turn_started = 0.0
        self.lock = asyncio.Lock()
        self.watchdog_task = None
        self.public_view = None
        self.on_finish = None

    # --- basics ---

    @property
    def current(self):
        return self.players[self.turn]

    @property
    def top(self):
        return self.discard[-1]

    def lobby_text(self) -> str:
        names = ", ".join(clean(p) for p in self.players)
        return (
            f"**UNO lobby**, hosted by {clean(self.host)}\n"
            f"Players ({len(self.players)}/{MAX_PLAYERS}): {names}\n\n"
            "Press **Join** to play. The host presses **Start** when everyone is in "
            "(2 players minimum)."
        )

    def draw_cards(self, n: int):
        cards = []
        for _ in range(n):
            if not self.deck:
                top = self.discard.pop()
                self.deck = self.discard
                random.shuffle(self.deck)
                self.discard = [top]
            if not self.deck:
                break
            cards.append(self.deck.pop())
        return cards

    def start(self):
        self.deck = build_deck()
        self.hands = {p.id: self.draw_cards(START_CARDS) for p in self.players}
        top = self.deck.pop()
        while top[2:] not in NUMBERS:          # the first card is always a number
            self.deck.insert(0, top)
            top = self.deck.pop()
        self.discard = [top]
        self.color = top[0]
        self.turn = 0
        self.started = True
        self.turn_started = time.monotonic()
        self.log = "Game on! Cards are dealt."

    def _advance(self, steps: int = 1):
        self.turn = (self.turn + self.direction * steps) % len(self.players)
        self.drawn = None

    def is_playable(self, card: str, hand) -> bool:
        color, value = card[0], card[2:]
        if color == "W":
            if value == "wild4":
                return not any(c[0] == self.color for c in hand)
            return True
        return color == self.color or value == self.top[2:]

    def playable_codes(self, hand):
        if self.drawn is not None:
            return [self.drawn] if self.is_playable(self.drawn, hand) else []
        seen, out = set(), []
        for c in sort_hand(hand):
            if c not in seen and self.is_playable(c, hand):
                seen.add(c)
                out.append(c)
        return out

    def _check_turn(self, player):
        if self.ended:
            return "This game is over."
        if not self.started:
            return "The game hasn't started."
        if self.current.id != player.id:
            return "It's not your turn."
        return None

    # --- actions ---

    async def play(self, player, code: str, chosen=None):
        """Returns an error text, or None if the card was played."""
        async with self.lock:
            err = self._check_turn(player)
            if err:
                return err
            hand = self.hands[player.id]
            if code not in hand:
                return "You don't have that card."
            if self.drawn is not None and code != self.drawn:
                return "You can only play the card you just drew, or pass."
            if not self.is_playable(code, hand):
                return "You can't play that card right now."
            if code[0] == "W" and chosen not in COLORS:
                return "Pick a color first."

            hand.remove(code)
            self.discard.append(code)
            self.strikes[player.id] = 0
            value = code[2:]

            if code[0] == "W":
                self.color = chosen
                played = f"{label(code)} (chose {COLORS[chosen]} {COLOR_NAMES[chosen]})"
            else:
                self.color = code[0]
                played = label(code)
            text = f"**{clean(player)}** played {played}."

            if not hand:
                self._finish(f"🏆 **{clean(player)}** played their last card and wins!")
                return None

            n = len(self.players)
            if value == "reverse":
                if n == 2:
                    self._advance(2)
                    text += " With two players, Reverse acts like a skip."
                else:
                    self.direction *= -1
                    self._advance(1)
                    text += " Direction reversed."
            elif value == "skip":
                skipped = self.players[(self.turn + self.direction) % n]
                self._advance(2)
                text += f" **{clean(skipped)}** is skipped."
            elif value in ("draw2", "wild4"):
                count = 2 if value == "draw2" else 4
                victim = self.players[(self.turn + self.direction) % n]
                self.hands[victim.id].extend(self.draw_cards(count))
                self._advance(2)
                text += f" **{clean(victim)}** draws {count} and is skipped."
            else:
                self._advance(1)

            if len(hand) == 1:
                text += f" **UNO!** ({clean(player)} has one card left)"

            self.log = text
            self.turn_started = time.monotonic()
            return None

    async def draw(self, player):
        """Returns (ok, text)."""
        async with self.lock:
            err = self._check_turn(player)
            if err:
                return False, err
            if self.drawn is not None:
                return False, "You already drew a card this turn. Play it or pass."

            hand = self.hands[player.id]
            cards = self.draw_cards(1)
            if not cards:
                self._advance(1)
                self.log = f"**{clean(player)}** couldn't draw, the deck is empty."
                self.turn_started = time.monotonic()
                return True, "The deck is empty, so your turn passes."

            card = cards[0]
            hand.append(card)
            self.strikes[player.id] = 0
            self.turn_started = time.monotonic()
            if self.is_playable(card, hand):
                self.drawn = card
                self.log = f"**{clean(player)}** drew a card."
                return True, f"You drew {label(card)}. You can play it or pass."

            self._advance(1)
            self.log = f"**{clean(player)}** drew a card and couldn't play it."
            return True, f"You drew {label(card)}, but can't play it. Your turn passes."

    async def pass_turn(self, player):
        async with self.lock:
            err = self._check_turn(player)
            if err:
                return False, err
            if self.drawn is None:
                return False, "Draw a card first."
            self._advance(1)
            self.log = f"**{clean(player)}** passed."
            self.turn_started = time.monotonic()
            return True, "You passed."

    async def leave(self, user):
        async with self.lock:
            if self.ended:
                return False, "This game is over."
            player = next((p for p in self.players if p.id == user.id), None)
            if player is None:
                return False, "You're not in this game."
            self._remove(player)
            if len(self.players) == 1:
                self._finish(
                    f"🏆 **{clean(self.players[0])}** wins because everyone else left!"
                )
            else:
                self.log = f"**{clean(player)}** left the game."
                self.turn_started = time.monotonic()
            return True, "You left the game."

    # --- housekeeping ---

    def _remove(self, player):
        idx = self.players.index(player)
        self.deck.extend(self.hands.pop(player.id, []))
        random.shuffle(self.deck)
        self.players.pop(idx)
        if idx < self.turn:
            self.turn -= 1
        elif idx == self.turn and self.direction == -1:
            self.turn -= 1
        self.turn %= len(self.players)
        self.drawn = None

    def _timeout_current(self):
        player = self.current
        self.strikes[player.id] = self.strikes.get(player.id, 0) + 1
        if self.strikes[player.id] >= 2:
            self._remove(player)
            if len(self.players) == 1:
                self._finish(
                    f"🏆 **{clean(self.players[0])}** wins because everyone else left!"
                )
                return
            self.log = f"**{clean(player)}** was removed for being away."
        else:
            self.hands[player.id].extend(self.draw_cards(1))
            self._advance(1)
            self.log = f"**{clean(player)}** ran out of time and drew a card."
        self.turn_started = time.monotonic()

    def _finish(self, text: str):
        self.ended = True
        self.log = text
        task = self.watchdog_task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        if self.public_view is not None:
            self.public_view.stop()
        if self.on_finish:
            self.on_finish()

    async def watchdog(self):
        """Handles players who go away: auto-draws for them when their time runs out."""
        try:
            while not self.ended:
                await asyncio.sleep(10)
                if self.ended:
                    break
                if time.monotonic() - self.turn_started > TURN_SECONDS:
                    async with self.lock:
                        if self.ended:
                            break
                        if time.monotonic() - self.turn_started <= TURN_SECONDS:
                            continue
                        self._timeout_current()
                    await self.refresh()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("UNO watchdog crashed")

    # --- the public message ---

    def render(self) -> str:
        arrow = "➡️" if self.direction == 1 else "⬅️"
        lines = [
            f"**UNO** {arrow}",
            f"Top card: **{label(self.top)}**, current color: "
            f"{COLORS[self.color]} {COLOR_NAMES[self.color]}",
            "",
        ]
        for i, p in enumerate(self.players):
            marker = "👉" if (i == self.turn and not self.ended) else "▫️"
            count = len(self.hands.get(p.id, []))
            lines.append(f"{marker} {clean(p)}: {count} card{'s' if count != 1 else ''}")
        lines.append("")
        if self.log:
            lines.append(self.log)
        if not self.ended:
            lines.append(
                f"\n**{clean(self.current)}**, it's your turn. Press **My hand** to play."
            )
        return "\n".join(lines)[:1900]

    async def refresh(self):
        try:
            await self.channel.get_partial_message(self.message_id).edit(
                content=self.render(),
                view=None if self.ended else self.public_view,
                allowed_mentions=NO_PINGS,
            )
        except discord.HTTPException:
            log.warning("Couldn't update the UNO message")


# ---------------- private hand (only the player sees it) ----------------

async def play_and_update(interaction, game, player, code, chosen):
    err = await game.play(player, code, chosen)
    view = HandView(game, player)
    note = err or f"You played {label(code)}."
    await interaction.response.edit_message(content=view.text(note), view=view)
    if err is None:
        await game.refresh()


async def draw_or_pass(interaction, game, player, action):
    if action == "draw":
        ok, note = await game.draw(player)
    else:
        ok, note = await game.pass_turn(player)
    view = HandView(game, player)
    await interaction.response.edit_message(content=view.text(note), view=view)
    if ok:
        await game.refresh()


class ColorView(discord.ui.View):
    def __init__(self, game, player, code):
        super().__init__(timeout=300)
        styles = {
            "R": discord.ButtonStyle.danger,
            "Y": discord.ButtonStyle.secondary,
            "G": discord.ButtonStyle.success,
            "B": discord.ButtonStyle.primary,
        }
        for color in "RYGB":
            button = discord.ui.Button(
                label=COLOR_NAMES[color], emoji=COLORS[color], style=styles[color]
            )
            button.callback = self._make_callback(game, player, code, color)
            self.add_item(button)

    @staticmethod
    def _make_callback(game, player, code, color):
        async def callback(interaction: discord.Interaction):
            await play_and_update(interaction, game, player, code, color)

        return callback


class HandView(discord.ui.View):
    def __init__(self, game, player):
        super().__init__(timeout=600)
        self.game = game
        self.player = player
        self.select = None

        my_turn = (
            game.started and not game.ended and game.current.id == player.id
        )
        if not my_turn:
            return

        hand = game.hands.get(player.id, [])
        codes = game.playable_codes(hand)[:25]
        if codes:
            self.select = discord.ui.Select(
                placeholder="Play a card...",
                options=[
                    discord.SelectOption(
                        label=label(c, emoji=False),
                        value=c,
                        emoji=COLORS.get(c[0], "🌈"),
                    )
                    for c in codes
                ],
            )
            self.select.callback = self._on_select
            self.add_item(self.select)

        draw = discord.ui.Button(
            label="Draw a card",
            style=discord.ButtonStyle.primary,
            disabled=game.drawn is not None,
        )
        draw.callback = self._on_draw
        self.add_item(draw)

        if game.drawn is not None:
            skip = discord.ui.Button(label="Pass", style=discord.ButtonStyle.secondary)
            skip.callback = self._on_pass
            self.add_item(skip)

    def text(self, note: str = "") -> str:
        g = self.game
        hand = g.hands.get(self.player.id, [])
        lines = []
        if note:
            lines += [note, ""]
        lines.append(
            f"Top card: **{label(g.top)}**, color: {COLORS[g.color]} {COLOR_NAMES[g.color]}"
        )
        lines.append(
            "**Your hand:** " + (" ".join(label(c) for c in sort_hand(hand)) or "empty")
        )
        if g.ended:
            lines.append("The game is over.")
        elif g.current.id == self.player.id:
            lines.append("It's **your turn**.")
        else:
            lines.append(f"Waiting for **{clean(g.current)}**...")
        return "\n".join(lines)[:1900]

    async def _on_select(self, interaction: discord.Interaction):
        code = self.select.values[0]
        if code[0] == "W":
            await interaction.response.edit_message(
                content="Pick a color for your wild card:",
                view=ColorView(self.game, self.player, code),
            )
            return
        await play_and_update(interaction, self.game, self.player, code, None)

    async def _on_draw(self, interaction: discord.Interaction):
        await draw_or_pass(interaction, self.game, self.player, "draw")

    async def _on_pass(self, interaction: discord.Interaction):
        await draw_or_pass(interaction, self.game, self.player, "pass")


# ---------------- the public game message ----------------

class GameView(discord.ui.View):
    def __init__(self, game):
        super().__init__(timeout=None)   # the game ends on its own, see the watchdog
        self.game = game

    @discord.ui.button(label="My hand", emoji="🃏", style=discord.ButtonStyle.primary)
    async def my_hand(self, interaction: discord.Interaction, button: discord.ui.Button):
        g = self.game
        if g.ended:
            await interaction.response.send_message("This game is over.", ephemeral=True)
            return
        if interaction.user.id not in g.hands:
            await interaction.response.send_message(
                "You're not in this game.", ephemeral=True
            )
            return
        view = HandView(g, interaction.user)
        await interaction.response.send_message(
            view.text(),
            view=view if view.children else discord.utils.MISSING,
            ephemeral=True,
        )

    @discord.ui.button(label="Leave game", style=discord.ButtonStyle.secondary)
    async def leave_game(self, interaction: discord.Interaction, button: discord.ui.Button):
        ok, note = await self.game.leave(interaction.user)
        await interaction.response.send_message(note, ephemeral=True)
        if ok:
            await self.game.refresh()


# ---------------- the lobby ----------------

class LobbyView(discord.ui.View):
    def __init__(self, game):
        super().__init__(timeout=LOBBY_SECONDS)
        self.game = game
        self.message = None

    @discord.ui.button(label="Join", style=discord.ButtonStyle.success)
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        g = self.game
        if any(p.id == interaction.user.id for p in g.players):
            await interaction.response.send_message("You're already in.", ephemeral=True)
            return
        if len(g.players) >= MAX_PLAYERS:
            await interaction.response.send_message(
                f"The lobby is full ({MAX_PLAYERS} players).", ephemeral=True
            )
            return
        g.players.append(interaction.user)
        await interaction.response.edit_message(
            content=g.lobby_text(), view=self, allowed_mentions=NO_PINGS
        )

    @discord.ui.button(label="Leave", style=discord.ButtonStyle.secondary)
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        g = self.game
        if interaction.user.id == g.host.id:
            await interaction.response.send_message(
                "You're the host. Use **Cancel** to close the lobby.", ephemeral=True
            )
            return
        player = next((p for p in g.players if p.id == interaction.user.id), None)
        if player is None:
            await interaction.response.send_message("You're not in the lobby.", ephemeral=True)
            return
        g.players.remove(player)
        await interaction.response.edit_message(
            content=g.lobby_text(), view=self, allowed_mentions=NO_PINGS
        )

    @discord.ui.button(label="Start", style=discord.ButtonStyle.primary)
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        g = self.game
        if interaction.user.id != g.host.id:
            await interaction.response.send_message(
                "Only the host can start the game.", ephemeral=True
            )
            return
        if len(g.players) < 2:
            await interaction.response.send_message(
                "You need at least 2 players.", ephemeral=True
            )
            return

        g.start()
        view = GameView(g)
        g.public_view = view
        self.stop()
        await interaction.response.edit_message(
            content=g.render(), view=view, allowed_mentions=NO_PINGS
        )
        g.watchdog_task = asyncio.create_task(g.watchdog())

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        g = self.game
        if interaction.user.id != g.host.id:
            await interaction.response.send_message(
                "Only the host can cancel the lobby.", ephemeral=True
            )
            return
        g.ended = True
        if g.on_finish:
            g.on_finish()
        self.stop()
        await interaction.response.edit_message(content="UNO lobby cancelled.", view=None)

    async def on_timeout(self):
        g = self.game
        if g.started or g.ended:
            return
        g.ended = True
        if g.on_finish:
            g.on_finish()
        if self.message is not None:
            try:
                await self.message.edit(content="UNO lobby expired.", view=None)
            except discord.HTTPException:
                pass


# ---------------- the cog ----------------

class Uno(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.games = {}   # channel id -> UnoGame

    async def cog_unload(self):
        for game in self.games.values():
            if game.watchdog_task:
                game.watchdog_task.cancel()

    @app_commands.command(name="uno", description="Start a game of UNO in this channel.")
    @app_commands.guild_only()
    @app_commands.checks.cooldown(1, 20.0, key=lambda i: i.user.id)
    async def uno(self, interaction: discord.Interaction):
        channel = interaction.channel
        if channel.id in self.games:
            await interaction.response.send_message(
                "There's already a UNO game in this channel.", ephemeral=True
            )
            return

        game = UnoGame(interaction.user, channel)
        game.on_finish = lambda: self.games.pop(channel.id, None)
        self.games[channel.id] = game

        view = LobbyView(game)
        try:
            msg = await channel.send(
                game.lobby_text(), view=view, allowed_mentions=NO_PINGS
            )
        except discord.HTTPException:
            self.games.pop(channel.id, None)
            await interaction.response.send_message(
                "I couldn't post in this channel. I need **Send Messages** here.",
                ephemeral=True,
            )
            return

        game.message_id = msg.id
        view.message = msg
        await interaction.response.send_message(
            "Lobby created! Others can press **Join**.", ephemeral=True
        )

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ):
        if isinstance(error, app_commands.CommandOnCooldown):
            msg = f"Slow down, try again in {error.retry_after:.0f}s."
        else:
            log.exception("UNO command error", exc_info=error)
            msg = "Something went wrong on my end. Try again in a moment."

        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Uno(bot))
