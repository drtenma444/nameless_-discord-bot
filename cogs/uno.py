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
TURN_SECONDS = 120       # time allowed per turn
LOBBY_SECONDS = 300      # how long the lobby stays open
CARDS_PER_PAGE = 20      # card buttons per page in your hand

# Roles (by name) that can start, cancel and configure any lobby, besides the host
MANAGER_ROLES = {"admin", "subadmin", "vip"}

COLORS = {"R": "🟥", "Y": "🟨", "G": "🟩", "B": "🟦"}
COLOR_NAMES = {"R": "Red", "Y": "Yellow", "G": "Green", "B": "Blue"}
EMBED_COLORS = {"R": 0xED4245, "Y": 0xFEE75C, "G": 0x57F287, "B": 0x5865F2}
BUTTON_STYLES = {
    "R": discord.ButtonStyle.danger,
    "G": discord.ButtonStyle.success,
    "B": discord.ButtonStyle.primary,
    "Y": discord.ButtonStyle.secondary,
    "W": discord.ButtonStyle.secondary,
}
SHORT = {
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

HOW_TO = (
    "**How to play UNO**\n"
    "• **Goal:** be the first to get rid of all your cards.\n"
    "• **Your turn:** the bot pings you. Press **🃏 My hand** and click a card. "
    "Coloured cards can be played. Greyed-out cards can't be played right now.\n"
    "• A card works if it matches the **colour**, the **number** or the **symbol** of the top card. "
    "Wild cards always work, and you pick the new colour.\n"
    "• **Can't play?** Press **Draw**. If the card you drew can be played, you may play it or press **Pass**.\n"
    "• **Skip:** the next player loses their turn. **Reverse:** the direction flips. "
    "**+2:** the next player draws 2 and loses their turn. "
    "**Wild +4:** the next player draws 4 and loses their turn. "
    "You can only play a Wild +4 when you have no card of the current colour.\n"
    "• **One card left?** Press **UNO!** in your hand. If you forget and someone presses "
    "**Catch UNO!** before the next player moves, you draw 2.\n"
    "• Away for 2 minutes? You draw a card automatically. Two misses in a row and you're removed.\n"
    "• The winner scores points from the cards left in the other hands."
)


# ---------------- small helpers ----------------
# A card is a short text code: "R:5" is a red 5, "Y:skip", "W:wild4".

def clean(user) -> str:
    return discord.utils.escape_markdown(user.display_name)


def short(card: str) -> str:
    value = card[2:]
    return SHORT.get(value, value)


def emoji_of(card: str) -> str:
    return COLORS.get(card[0], "🌈")


def label(card: str) -> str:
    return f"{emoji_of(card)} {short(card)}"


def points(card: str) -> int:
    value = card[2:]
    if value.isdigit():
        return int(value)
    if value in ("skip", "reverse", "draw2"):
        return 20
    return 50


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


def can_manage(member) -> bool:
    """Staff, VIPs and anyone with Manage Server."""
    if member.guild_permissions.manage_guild:
        return True
    for role in member.roles:
        name = role.name.lower().replace(" ", "").replace("-", "").replace("_", "")
        if name in MANAGER_ROLES:
            return True
    return False


def can_run(game, user) -> bool:
    return user.id == game.host.id or can_manage(user)


# ---------------- the game itself ----------------

class UnoGame:
    def __init__(self, cog, host, channel, players=None, rules=None):
        self.cog = cog
        self.host = host
        self.channel = channel
        self.players = list(players) if players else [host]
        self.rules = dict(rules) if rules else {"stack": False, "catch": True}
        self.hands = {}
        self.deck = []
        self.discard = []
        self.color = None
        self.turn = 0
        self.direction = 1
        self.drawn = None          # the card the current player just drew
        self.pending = 0           # cards waiting to be drawn (only with stacking)
        self.pending_kind = None   # "draw2" or "wild4"
        self.uno_target = None     # id of a player with one card who hasn't called UNO
        self.history = []
        self.strikes = {}          # user id -> turns missed in a row
        self.started = False
        self.ended = False
        self.result = ""
        self.turn_started = 0.0
        self.lock = asyncio.Lock()
        self.board_lock = asyncio.Lock()
        self.board_message = None
        self.board_view = None
        self.watchdog_task = None
        self.on_finish = None

    # ----- basics -----

    @property
    def current(self):
        return self.players[self.turn]

    @property
    def top(self):
        return self.discard[-1]

    def note(self, text: str):
        self.history.append(text)
        self.history = self.history[-4:]

    def _next_index(self, steps: int = 1) -> int:
        return (self.turn + self.direction * steps) % len(self.players)

    def _advance(self, steps: int = 1):
        self.turn = self._next_index(steps)
        self.drawn = None

    def draw_cards(self, n: int):
        cards = []
        for _ in range(n):
            if not self.deck:
                if len(self.discard) <= 1:
                    break
                top = self.discard.pop()
                self.deck = self.discard
                random.shuffle(self.deck)
                self.discard = [top]
            cards.append(self.deck.pop())
        return cards

    def is_playable(self, card: str, hand) -> bool:
        color, value = card[0], card[2:]
        if self.pending:
            return value == self.pending_kind     # only stacking is allowed
        if color == "W":
            if value == "wild4":
                return not any(c[0] == self.color for c in hand)
            return True
        return color == self.color or value == self.top[2:]

    def _check_turn(self, player):
        if self.ended:
            return "This game is over."
        if not self.started:
            return "The game hasn't started."
        if self.current.id != player.id:
            return "It's not your turn."
        return None

    # ----- starting -----

    def start(self):
        n = len(self.players)
        self.deck = build_deck()
        self.hands = {p.id: self.draw_cards(START_CARDS) for p in self.players}

        while True:
            top = self.deck.pop()
            if top == "W:wild4":            # a Wild +4 can't start the game
                self.deck.insert(0, top)
                continue
            break
        self.discard = [top]

        color, value = top[0], top[2:]
        self.color = color if color != "W" else random.choice("RYGB")
        self.turn = 0
        self.direction = 1
        first = self.players[0]

        text = f"Game on! The first card is {label(top)}."
        if color == "W":
            text += f" The colour was picked at random: {COLORS[self.color]} {COLOR_NAMES[self.color]}."
        elif value == "skip":
            self.turn = 1 % n
            text += f" {clean(first)} is skipped."
        elif value == "reverse":
            if n == 2:
                self.turn = 1 % n
                text += f" {clean(first)} is skipped."
            else:
                self.direction = -1
                text += " The direction is reversed."
        elif value == "draw2":
            self.hands[first.id].extend(self.draw_cards(2))
            self.turn = 1 % n
            text += f" {clean(first)} draws 2 and is skipped."

        self.started = True
        self.turn_started = time.monotonic()
        self.note(text)

    # ----- actions -----

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
                return "You can only play the card you just drew, or press Pass."
            if not self.is_playable(code, hand):
                return "You can't play that card right now."
            if code[0] == "W" and chosen not in COLORS:
                return "Pick a colour first."

            self.uno_target = None
            hand.remove(code)
            self.discard.append(code)
            self.strikes[player.id] = 0
            value = code[2:]
            n = len(self.players)
            name = clean(player)
            victim = self.players[self._next_index(1)]

            if code[0] == "W":
                self.color = chosen
                played = f"{label(code)} and chose {COLORS[chosen]} {COLOR_NAMES[chosen]}"
            else:
                self.color = code[0]
                played = label(code)
            text = f"**{name}** played {played}."

            # last card: the round is over
            if not hand:
                if value in ("draw2", "wild4"):
                    self.hands[victim.id].extend(self.draw_cards(2 if value == "draw2" else 4))
                self._finish_round(player)
                return None

            if value == "reverse":
                if n == 2:
                    self._advance(2)
                    text += " With two players Reverse works like Skip, so they play again."
                else:
                    self.direction *= -1
                    self._advance(1)
                    text += " The direction is reversed."
            elif value == "skip":
                self._advance(2)
                text += f" **{clean(victim)}** is skipped."
            elif value in ("draw2", "wild4"):
                count = 2 if value == "draw2" else 4
                if self.rules["stack"]:
                    self.pending += count
                    self.pending_kind = value
                    self._advance(1)
                    text += (
                        f" **{clean(victim)}** must stack a {short(code)} "
                        f"or draw {self.pending}."
                    )
                else:
                    self.hands[victim.id].extend(self.draw_cards(count))
                    self._advance(2)
                    text += f" **{clean(victim)}** draws {count} and is skipped."
            else:
                self._advance(1)

            if len(hand) == 1:
                if self.rules["catch"]:
                    self.uno_target = player.id
                    text += f" ⚠️ **{name}** has ONE card left!"
                else:
                    text += f" **UNO!** {name} has one card left."

            self.note(text)
            self.turn_started = time.monotonic()
            return None

    async def draw(self, player):
        """Returns (ok, text, turn_moved)."""
        async with self.lock:
            err = self._check_turn(player)
            if err:
                return False, err, False

            hand = self.hands[player.id]
            name = clean(player)

            if self.pending:                       # stacking penalty: take it all
                self.uno_target = None
                count = self.pending
                hand.extend(self.draw_cards(count))
                self.pending = 0
                self.pending_kind = None
                self._advance(1)
                self.strikes[player.id] = 0
                self.note(f"**{name}** drew {count} cards.")
                self.turn_started = time.monotonic()
                return True, f"You drew {count} cards. Your turn is over.", True

            if self.drawn is not None:
                return False, "You already drew a card this turn. Play it or press Pass.", False

            self.uno_target = None
            self.strikes[player.id] = 0
            cards = self.draw_cards(1)
            if not cards:
                self._advance(1)
                self.note(f"**{name}** couldn't draw, the deck is empty.")
                self.turn_started = time.monotonic()
                return True, "The deck is empty, so your turn passes.", True

            card = cards[0]
            hand.append(card)
            self.turn_started = time.monotonic()
            if self.is_playable(card, hand):
                self.drawn = card
                self.note(f"**{name}** drew a card.")
                return True, f"You drew {label(card)}. You can play it or press Pass.", False

            self._advance(1)
            self.note(f"**{name}** drew a card and couldn't play it.")
            return True, f"You drew {label(card)}, but it can't be played. Your turn is over.", True

    async def pass_turn(self, player):
        """Returns (ok, text, turn_moved)."""
        async with self.lock:
            err = self._check_turn(player)
            if err:
                return False, err, False
            if self.drawn is None:
                return False, "Draw a card first.", False
            self.uno_target = None
            self._advance(1)
            self.note(f"**{clean(player)}** passed.")
            self.turn_started = time.monotonic()
            return True, "You passed.", True

    async def call_uno(self, user):
        async with self.lock:
            if self.ended:
                return False, "This game is over."
            if user.id not in self.hands:
                return False, "You're not in this game."
            if self.uno_target == user.id:
                self.uno_target = None
                self.note(f"📣 **{clean(user)}** called UNO!")
                return True, "UNO! You're safe."
            return False, "You don't need to call UNO right now."

    async def catch(self, user):
        async with self.lock:
            if self.ended:
                return False, "This game is over."
            if user.id not in self.hands:
                return False, "You're not in this game."
            target_id = self.uno_target
            if target_id is None:
                return False, "Too late, there's nobody to catch right now."
            if target_id == user.id:
                return False, "That's you! Press UNO! in your hand instead."
            target = next((p for p in self.players if p.id == target_id), None)
            self.uno_target = None
            if target is None:
                return False, "Too late, there's nobody to catch right now."
            self.hands[target.id].extend(self.draw_cards(2))
            self.note(
                f"🚨 **{clean(user)}** caught **{clean(target)}** without UNO! They draw 2."
            )
            return True, f"Got them! {clean(target)} draws 2 cards."

    async def leave(self, user):
        """Returns (ok, text, repost_board)."""
        async with self.lock:
            if self.ended:
                return False, "This game is over.", False
            player = next((p for p in self.players if p.id == user.id), None)
            if player is None:
                return False, "You're not in this game.", False
            was_current = self.current.id == player.id
            self._remove(player)
            if len(self.players) == 1:
                self._finish(
                    f"🏆 **{clean(self.players[0])}** wins because everyone else left!"
                )
                return True, "You left the game.", True
            self.note(f"**{clean(player)}** left the game.")
            if was_current:
                self.turn_started = time.monotonic()
            return True, "You left the game.", was_current

    # ----- housekeeping -----

    def _remove(self, player):
        idx = self.players.index(player)
        if idx == self.turn:
            self.pending = 0
            self.pending_kind = None
        if self.uno_target == player.id:
            self.uno_target = None
        self.deck.extend(self.hands.pop(player.id, []))
        random.shuffle(self.deck)
        self.strikes.pop(player.id, None)
        self.players.pop(idx)
        if idx < self.turn:
            self.turn -= 1
        elif idx == self.turn and self.direction == -1:
            self.turn -= 1
        self.turn %= len(self.players)
        self.drawn = None

    def _timeout_current(self):
        player = self.current
        name = clean(player)
        self.uno_target = None
        self.strikes[player.id] = self.strikes.get(player.id, 0) + 1

        if self.strikes[player.id] >= 2:
            self._remove(player)
            if len(self.players) == 1:
                self._finish(
                    f"🏆 **{clean(self.players[0])}** wins because everyone else left!"
                )
                return
            self.note(f"**{name}** was removed for being away.")
        else:
            if self.drawn is not None:
                self.note(f"**{name}** ran out of time and passed.")
            else:
                count = self.pending or 1
                self.pending = 0
                self.pending_kind = None
                self.hands[player.id].extend(self.draw_cards(count))
                self.note(
                    f"**{name}** ran out of time and drew {count} card{'s' if count != 1 else ''}."
                )
            self._advance(1)
        self.turn_started = time.monotonic()

    def _finish_round(self, winner):
        total = sum(
            points(c)
            for pid, hand in self.hands.items()
            if pid != winner.id
            for c in hand
        )
        self._finish(
            f"🏆 **{clean(winner)}** played their last card and wins the round! "
            f"They score **{total}** points from the other hands."
        )

    def _finish(self, text: str):
        self.ended = True
        self.result = text
        self.uno_target = None
        task = self.watchdog_task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        if self.on_finish:
            self.on_finish()

    async def watchdog(self):
        """Handles players who go away: draws for them when their time runs out."""
        try:
            while not self.ended:
                await asyncio.sleep(10)
                if self.ended:
                    break
                if time.monotonic() - self.turn_started <= TURN_SECONDS:
                    continue
                async with self.lock:
                    if self.ended:
                        break
                    if time.monotonic() - self.turn_started <= TURN_SECONDS:
                        continue
                    self._timeout_current()
                await self.post_board()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("UNO watchdog crashed")

    # ----- what everyone sees -----

    def lobby_embed(self) -> discord.Embed:
        names = "\n".join(
            f"{'👑' if p.id == self.host.id else '•'} {clean(p)}" for p in self.players
        )
        embed = discord.Embed(
            title="UNO lobby",
            description="Press **Join** to play. The host presses **Start** when everyone is in.",
            color=0x5865F2,
        )
        embed.add_field(
            name=f"Players ({len(self.players)}/{MAX_PLAYERS})", value=names, inline=False
        )
        embed.add_field(
            name="Rules",
            value=(
                "Official UNO rules, plus two options the host can switch:\n"
                f"• Stacking +2/+4: **{'On' if self.rules['stack'] else 'Off'}**\n"
                f"• UNO call and catch: **{'On' if self.rules['catch'] else 'Off'}**"
            ),
            inline=False,
        )
        embed.add_field(
            name="How it works",
            value=(
                "1. When it's your turn, the bot pings you.\n"
                "2. Press **🃏 My hand** and click a coloured card to play it.\n"
                "3. Nothing to play? Press **Draw**."
            ),
            inline=False,
        )
        embed.set_footer(text="2 to 6 players. The lobby closes after 5 minutes.")
        return embed

    def board_embed(self) -> discord.Embed:
        if self.ended:
            embed = discord.Embed(
                title="UNO: game over", description=self.result, color=0xF1C40F
            )
            lines = [
                f"{clean(p)}: {len(self.hands.get(p.id, []))} card(s) left"
                for p in self.players
            ]
            embed.add_field(name="Cards left", value="\n".join(lines)[:1000], inline=False)
            return embed

        arrow = "➡️ clockwise" if self.direction == 1 else "⬅️ counter-clockwise"
        desc = (
            f"👉 **{clean(self.current)}**'s turn\n"
            f"Colour: {COLORS[self.color]} **{COLOR_NAMES[self.color]}**  ·  {arrow}"
        )
        if self.pending:
            kind = SHORT[self.pending_kind]
            desc += (
                f"\n💥 **{self.pending} cards** are waiting. "
                f"Stack a {kind} or draw them."
            )
        embed = discord.Embed(
            title=f"Top card: {label(self.top)}",
            description=desc,
            color=EMBED_COLORS[self.color],
        )
        embed.set_author(name="UNO")

        lines = []
        for i, p in enumerate(self.players):
            count = len(self.hands.get(p.id, []))
            mark = "👉" if i == self.turn else "▫️"
            warn = "  ⚠️ UNO!" if count == 1 else ""
            lines.append(f"{mark} {clean(p)}: {count} card{'s' if count != 1 else ''}{warn}")
        embed.add_field(name="Players", value="\n".join(lines)[:1000], inline=False)

        if self.history:
            embed.add_field(
                name="Recent moves", value="\n".join(self.history)[:1000], inline=False
            )
        embed.set_footer(
            text="Your turn? Press 🃏 My hand, then click a card. New here? Press ❓ How to play."
        )
        return embed

    async def post_board(self):
        """Posts a fresh board (pinging whoever's turn it is) and removes the old one."""
        async with self.board_lock:
            old, old_view = self.board_message, self.board_view

            if self.ended:
                view = EndView(self)
                content = None
                mentions = NO_PINGS
            else:
                view = GameView(self)
                cur = self.current
                content = f"{cur.mention}, it's your turn!"
                mentions = discord.AllowedMentions(users=[cur])

            try:
                msg = await self.channel.send(
                    content=content,
                    embed=self.board_embed(),
                    view=view,
                    allowed_mentions=mentions,
                )
            except discord.HTTPException:
                log.exception("Couldn't post the UNO board")
                return

            self.board_message, self.board_view = msg, view
            if old_view is not None:
                old_view.stop()
            if old is not None:
                try:
                    await old.delete()
                except discord.HTTPException:
                    pass

    async def edit_board(self):
        """Updates the current board without pinging anyone."""
        async with self.board_lock:
            if self.board_message is None or self.ended:
                return
            old_view = self.board_view
            view = GameView(self)
            try:
                await self.board_message.edit(embed=self.board_embed(), view=view)
            except discord.HTTPException:
                log.warning("Couldn't update the UNO board")
                return
            self.board_view = view
            if old_view is not None:
                old_view.stop()


# ---------------- your private hand ----------------

async def play_and_update(interaction, game, player, code, chosen):
    err = await game.play(player, code, chosen)
    view = HandView(game, player)
    note = err or f"You played {label(code)}."
    await interaction.response.edit_message(content=view.text(note), view=view)
    if err is None:
        await game.post_board()


async def draw_or_pass(interaction, game, player, action):
    if action == "draw":
        ok, note, moved = await game.draw(player)
    else:
        ok, note, moved = await game.pass_turn(player)
    view = HandView(game, player)
    await interaction.response.edit_message(content=view.text(note), view=view)
    if ok:
        if moved:
            await game.post_board()
        else:
            await game.edit_board()


class ColorView(discord.ui.View):
    def __init__(self, game, player, code):
        super().__init__(timeout=300)
        self.game = game
        self.player = player
        for color in "RYGB":
            button = discord.ui.Button(
                label=COLOR_NAMES[color], emoji=COLORS[color], style=BUTTON_STYLES[color]
            )
            button.callback = self._make_callback(game, player, code, color)
            self.add_item(button)
        back = discord.ui.Button(label="Back", style=discord.ButtonStyle.secondary, row=1)
        back.callback = self._back
        self.add_item(back)

    @staticmethod
    def _make_callback(game, player, code, color):
        async def callback(interaction: discord.Interaction):
            await play_and_update(interaction, game, player, code, color)

        return callback

    async def _back(self, interaction: discord.Interaction):
        view = HandView(self.game, self.player)
        await interaction.response.edit_message(content=view.text(), view=view)


class HandView(discord.ui.View):
    def __init__(self, game, player, page: int = 0):
        super().__init__(timeout=840)    # private messages stop working after ~15 minutes
        self.game = game
        self.player = player

        hand = game.hands.get(player.id, [])
        self.my_turn = (
            game.started and not game.ended and game.current.id == player.id
        )

        groups = {}
        for card in sort_hand(hand):
            groups[card] = groups.get(card, 0) + 1
        cards = list(groups.items())

        self.pages = max(1, -(-len(cards) // CARDS_PER_PAGE))
        self.page = max(0, min(page, self.pages - 1))
        chunk = cards[self.page * CARDS_PER_PAGE:(self.page + 1) * CARDS_PER_PAGE]

        for i, (code, count) in enumerate(chunk):
            playable = (
                self.my_turn
                and game.is_playable(code, hand)
                and (game.drawn is None or code == game.drawn)
            )
            text = short(code) + (f" ×{count}" if count > 1 else "")
            button = discord.ui.Button(
                label=text,
                emoji=emoji_of(code),
                style=BUTTON_STYLES[code[0]],
                disabled=not playable,
                row=i // 5,
            )
            button.callback = self._card_callback(code)
            self.add_item(button)

        # bottom row: actions
        if self.my_turn:
            draw_label = f"Draw {game.pending}" if game.pending else "Draw a card"
            draw = discord.ui.Button(
                label=draw_label,
                style=discord.ButtonStyle.primary,
                disabled=game.drawn is not None,
                row=4,
            )
            draw.callback = self._on_draw
            self.add_item(draw)
            if game.drawn is not None:
                skip = discord.ui.Button(
                    label="Pass", style=discord.ButtonStyle.secondary, row=4
                )
                skip.callback = self._on_pass
                self.add_item(skip)
        else:
            refresh = discord.ui.Button(
                label="Refresh", emoji="🔄", style=discord.ButtonStyle.secondary, row=4
            )
            refresh.callback = self._on_refresh
            self.add_item(refresh)

        if game.uno_target == player.id:
            uno = discord.ui.Button(label="UNO!", style=discord.ButtonStyle.success, row=4)
            uno.callback = self._on_uno
            self.add_item(uno)

        if self.pages > 1:
            prev_btn = discord.ui.Button(
                label="◀", style=discord.ButtonStyle.secondary,
                disabled=self.page == 0, row=4,
            )
            prev_btn.callback = self._on_prev
            self.add_item(prev_btn)
            next_btn = discord.ui.Button(
                label="▶", style=discord.ButtonStyle.secondary,
                disabled=self.page >= self.pages - 1, row=4,
            )
            next_btn.callback = self._on_next
            self.add_item(next_btn)

    def text(self, note: str = "") -> str:
        g = self.game
        hand = g.hands.get(self.player.id, [])
        lines = []
        if note:
            lines += [note, ""]
        lines.append(
            f"**Top card:** {label(g.top)}  ·  **Colour:** {COLORS[g.color]} {COLOR_NAMES[g.color]}"
        )
        lines.append(f"**Your hand:** {len(hand)} card{'s' if len(hand) != 1 else ''}")
        if g.ended:
            lines.append("The game is over.")
        elif self.my_turn:
            if g.pending:
                lines.append(
                    f"👉 **Your turn!** {g.pending} cards are waiting. "
                    f"Stack a matching card, or press Draw {g.pending}."
                )
            elif g.drawn is not None:
                lines.append("👉 **Your turn!** Play the card you drew, or press Pass.")
            else:
                lines.append(
                    "👉 **Your turn!** Click a coloured card to play it. "
                    "Greyed-out cards can't be played right now. Nothing to play? Press Draw."
                )
        else:
            lines.append(
                f"⏳ Waiting for **{clean(g.current)}**. Press Refresh when it's your turn again."
            )
        if g.uno_target == self.player.id:
            lines.append(
                "⚠️ **You have one card left! Press UNO! before the next player moves.**"
            )
        if self.pages > 1:
            lines.append(f"Page {self.page + 1} of {self.pages}")
        return "\n".join(lines)[:1900]

    def _card_callback(self, code):
        async def callback(interaction: discord.Interaction):
            if code[0] == "W":
                await interaction.response.edit_message(
                    content="Pick a colour for your Wild card:",
                    view=ColorView(self.game, self.player, code),
                )
                return
            await play_and_update(interaction, self.game, self.player, code, None)

        return callback

    async def _show(self, interaction, page=None, note=""):
        view = HandView(self.game, self.player, self.page if page is None else page)
        await interaction.response.edit_message(content=view.text(note), view=view)

    async def _on_draw(self, interaction: discord.Interaction):
        await draw_or_pass(interaction, self.game, self.player, "draw")

    async def _on_pass(self, interaction: discord.Interaction):
        await draw_or_pass(interaction, self.game, self.player, "pass")

    async def _on_refresh(self, interaction: discord.Interaction):
        await self._show(interaction)

    async def _on_prev(self, interaction: discord.Interaction):
        await self._show(interaction, self.page - 1)

    async def _on_next(self, interaction: discord.Interaction):
        await self._show(interaction, self.page + 1)

    async def _on_uno(self, interaction: discord.Interaction):
        ok, note = await self.game.call_uno(self.player)
        await self._show(interaction, note=note)
        if ok:
            await self.game.edit_board()


# ---------------- the public board buttons ----------------

class GameView(discord.ui.View):
    def __init__(self, game):
        super().__init__(timeout=None)    # replaced every turn, stopped when replaced
        self.game = game
        if game.uno_target is not None:
            catch = discord.ui.Button(
                label="Catch UNO!", emoji="🚨", style=discord.ButtonStyle.danger
            )
            catch.callback = self._catch
            self.add_item(catch)

    @discord.ui.button(label="My hand", emoji="🃏", style=discord.ButtonStyle.primary)
    async def my_hand(self, interaction: discord.Interaction, button: discord.ui.Button):
        g = self.game
        if g.ended:
            await interaction.response.send_message("This game is over.", ephemeral=True)
            return
        if interaction.user.id not in g.hands:
            await interaction.response.send_message(
                "You're not in this game. Ask the host to start a new one with you in it.",
                ephemeral=True,
            )
            return
        view = HandView(g, interaction.user)
        await interaction.response.send_message(view.text(), view=view, ephemeral=True)

    @discord.ui.button(label="How to play", emoji="❓", style=discord.ButtonStyle.secondary)
    async def how_to(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(HOW_TO, ephemeral=True)

    @discord.ui.button(label="Leave", style=discord.ButtonStyle.secondary)
    async def leave_game(self, interaction: discord.Interaction, button: discord.ui.Button):
        g = self.game
        ok, note, repost = await g.leave(interaction.user)
        await interaction.response.send_message(note, ephemeral=True)
        if ok:
            if repost:
                await g.post_board()
            else:
                await g.edit_board()

    async def _catch(self, interaction: discord.Interaction):
        ok, note = await self.game.catch(interaction.user)
        await interaction.response.send_message(note, ephemeral=True)
        await self.game.edit_board()


class EndView(discord.ui.View):
    def __init__(self, game):
        super().__init__(timeout=180)
        self.game = game

    @discord.ui.button(label="Play again", emoji="🔁", style=discord.ButtonStyle.primary)
    async def again(self, interaction: discord.Interaction, button: discord.ui.Button):
        g = self.game
        if interaction.user.id not in {p.id for p in g.players}:
            await interaction.response.send_message(
                "Only the players of this game can start a rematch.", ephemeral=True
            )
            return
        cog = g.cog
        channel = interaction.channel
        if channel.id in cog.games:
            await interaction.response.send_message(
                "There's already a UNO game in this channel.", ephemeral=True
            )
            return
        if len(g.players) < 2:
            await interaction.response.send_message(
                "Not enough players for a rematch.", ephemeral=True
            )
            return

        players = g.players[1:] + g.players[:1]   # a different player starts this time
        new = cog.new_game(channel, players[0], players, g.rules)
        new.start()
        new.watchdog_task = asyncio.create_task(new.watchdog())
        self.stop()
        await interaction.response.edit_message(view=None)
        await new.post_board()

    async def on_timeout(self):
        msg = self.game.board_message
        if msg is not None:
            try:
                await msg.edit(view=None)
            except discord.HTTPException:
                pass


# ---------------- the lobby ----------------

class LobbyView(discord.ui.View):
    def __init__(self, game):
        super().__init__(timeout=LOBBY_SECONDS)
        self.game = game
        self.message = None
        self._sync()

    def _sync(self):
        rules = self.game.rules
        self.toggle_stack.label = f"Stacking +2/+4: {'On' if rules['stack'] else 'Off'}"
        self.toggle_stack.style = (
            discord.ButtonStyle.success if rules["stack"] else discord.ButtonStyle.secondary
        )
        self.toggle_catch.label = f"UNO catch: {'On' if rules['catch'] else 'Off'}"
        self.toggle_catch.style = (
            discord.ButtonStyle.success if rules["catch"] else discord.ButtonStyle.secondary
        )

    @discord.ui.button(label="Join", style=discord.ButtonStyle.success, row=0)
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
        await interaction.response.edit_message(embed=g.lobby_embed(), view=self)

    @discord.ui.button(label="Leave", style=discord.ButtonStyle.secondary, row=0)
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        g = self.game
        if interaction.user.id == g.host.id:
            await interaction.response.send_message(
                "You're the host. Use **Cancel** to close the lobby.", ephemeral=True
            )
            return
        player = next((p for p in g.players if p.id == interaction.user.id), None)
        if player is None:
            await interaction.response.send_message(
                "You're not in the lobby.", ephemeral=True
            )
            return
        g.players.remove(player)
        await interaction.response.edit_message(embed=g.lobby_embed(), view=self)

    @discord.ui.button(label="Start", style=discord.ButtonStyle.primary, row=0)
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        g = self.game
        if not can_run(g, interaction.user):
            await interaction.response.send_message(
                "Only the host, a VIP or staff can start the game.", ephemeral=True
            )
            return
        if len(g.players) < 2:
            await interaction.response.send_message(
                "You need at least 2 players.", ephemeral=True
            )
            return

        g.start()
        self.stop()
        await interaction.response.edit_message(
            content="The game has started! 👇", embed=None, view=None
        )
        g.watchdog_task = asyncio.create_task(g.watchdog())
        await g.post_board()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, row=0)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        g = self.game
        if not can_run(g, interaction.user):
            await interaction.response.send_message(
                "Only the host, a VIP or staff can cancel the lobby.", ephemeral=True
            )
            return
        g.ended = True
        if g.on_finish:
            g.on_finish()
        self.stop()
        await interaction.response.edit_message(
            content="UNO lobby cancelled.", embed=None, view=None
        )

    @discord.ui.button(label="Stacking", style=discord.ButtonStyle.secondary, row=1)
    async def toggle_stack(self, interaction: discord.Interaction, button: discord.ui.Button):
        g = self.game
        if not can_run(g, interaction.user):
            await interaction.response.send_message(
                "Only the host, a VIP or staff can change the rules.", ephemeral=True
            )
            return
        g.rules["stack"] = not g.rules["stack"]
        self._sync()
        await interaction.response.edit_message(embed=g.lobby_embed(), view=self)

    @discord.ui.button(label="UNO catch", style=discord.ButtonStyle.secondary, row=1)
    async def toggle_catch(self, interaction: discord.Interaction, button: discord.ui.Button):
        g = self.game
        if not can_run(g, interaction.user):
            await interaction.response.send_message(
                "Only the host, a VIP or staff can change the rules.", ephemeral=True
            )
            return
        g.rules["catch"] = not g.rules["catch"]
        self._sync()
        await interaction.response.edit_message(embed=g.lobby_embed(), view=self)

    async def on_timeout(self):
        g = self.game
        if g.started or g.ended:
            return
        g.ended = True
        if g.on_finish:
            g.on_finish()
        if self.message is not None:
            try:
                await self.message.edit(content="UNO lobby expired.", embed=None, view=None)
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

    def new_game(self, channel, host, players=None, rules=None):
        game = UnoGame(self, host, channel, players, rules)

        def done():
            if self.games.get(channel.id) is game:
                del self.games[channel.id]

        game.on_finish = done
        self.games[channel.id] = game
        return game

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

        perms = channel.permissions_for(interaction.guild.me)
        if not (perms.send_messages and perms.embed_links):
            await interaction.response.send_message(
                "I need **Send Messages** and **Embed Links** in this channel.",
                ephemeral=True,
            )
            return

        game = self.new_game(channel, interaction.user)
        view = LobbyView(game)
        await interaction.response.send_message(embed=game.lobby_embed(), view=view)
        view.message = await interaction.original_response()

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
