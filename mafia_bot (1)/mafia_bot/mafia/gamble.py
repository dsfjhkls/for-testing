"""도박 시스템 (/도박 ...)"""

import asyncio
import discord
import random
from discord import app_commands
from datetime import datetime, timedelta

from .core import GuardedView, bot, supabase
from .status import _check_updated, _require_db, check_jail, get_user_data
from .alignment import evaluate_new_titles, format_new_titles, get_title_blob
from .achievements import _daily_notices, daily_record, take_daily_notice
from .admin import check_maintenance

# ==========================================
# 도박 시스템 (카지노)
# ==========================================
GAMBLE_MIN_BET = 1000
GAMBLE_MAX_BET = 50000
GAMBLE_COOLDOWN = 3
COIN_WIN_RATE = 0.48

SLOT_SYMBOLS = [
    ("♣", 30, 3),
    ("♦", 25, 5),
    ("♥", 20, 8),
    ("★", 10, 20),
    ("7", 5, 100),
]
SLOT_PAIR_MULT = 1.0
SLOT_JACKPOT_SYMBOL = "7"

BLACKJACK_PAYOUT = {"blackjack": 2.5, "win": 2.0, "push": 1.0, "lose": 0}

gamble_cooldowns = {}
gamble_locks = {}
active_blackjack = {}

class InsufficientFunds(Exception):
    pass

def _gamble_lock(user_id: int) -> asyncio.Lock:
    lock = gamble_locks.get(user_id)
    if lock is None:
        lock = gamble_locks[user_id] = asyncio.Lock()
    return lock

def fmt_net(n: int) -> str:
    return f"{'+' if n >= 0 else '-'}${abs(n):,}"

def settle_gamble(user_id: int, bet: int, payout: int, info: dict = None, prepaid: bool = False, jackpot: bool = False):
    _require_db()
    if info is None:
        info = get_user_data(user_id)
    money = int(info.get("money") or 0)

    if not prepaid and money < bet:
        raise InsufficientFunds(money)

    new_money = money + (payout if prepaid else payout - bet)

    blob = get_title_blob(info)
    s = blob["stats"]
    s["gamble_played"] += 1
    s["gamble_wagered"] += bet
    s["gamble_net"] += payout - bet
    if payout > bet:
        s["gamble_won"] += 1
    if jackpot:
        s["jackpot"] += 1
    if new_money <= 0:
        s["broke"] += 1
    daily_msg = daily_record(blob, "gamble", {**info, "money": new_money})   # 일일 의뢰 집계 (도박 1판)
    new_ids = evaluate_new_titles(blob, {**info, "money": new_money})

    res = supabase.table("user_data").update({"money": new_money, "data": blob}).eq("user_id", str(user_id)).execute()
    _check_updated(res, "settle_gamble")
    if daily_msg:
        _daily_notices[user_id] = daily_msg   # 저장에 성공했을 때만 안내 문구를 남김
    return new_money, new_ids

def take_bet(user_id: int, bet: int) -> int:
    _require_db()
    info = get_user_data(user_id)
    money = int(info.get("money") or 0)
    if money < bet:
        raise InsufficientFunds(money)
    res = supabase.table("user_data").update({"money": money - bet}).eq("user_id", str(user_id)).execute()
    _check_updated(res, "take_bet")
    return money - bet

async def gamble_precheck(interaction: discord.Interaction) -> bool:
    if not await check_maintenance(interaction): return False
    if await check_jail(interaction): return False

    now = datetime.now()
    next_ok = gamble_cooldowns.get(interaction.user.id)
    if next_ok and now < next_ok:
        remaining = int((next_ok - now).total_seconds()) + 1
        await interaction.response.send_message(f"잠시 후 다시 베팅할 수 있습니다. (**{remaining}초**)", ephemeral=True)
        return False
    gamble_cooldowns[interaction.user.id] = now + timedelta(seconds=GAMBLE_COOLDOWN)
    return True

async def _safe_edit(interaction: discord.Interaction, **kwargs):
    try:
        await interaction.edit_original_response(**kwargs)
    except discord.HTTPException:
        pass

def spin_slots() -> list:
    return random.choices([s[0] for s in SLOT_SYMBOLS], weights=[s[1] for s in SLOT_SYMBOLS], k=3)

def slot_multiplier(reels: list) -> float:
    if reels[0] == reels[1] == reels[2]:
        for symbol, _, mult in SLOT_SYMBOLS:
            if symbol == reels[0]:
                return float(mult)
    if len(set(reels)) == 2:
        return SLOT_PAIR_MULT
    return 0.0

def slot_rtp() -> float:
    total_w = sum(w for _, w, _ in SLOT_SYMBOLS)
    rtp = 0.0
    for a in SLOT_SYMBOLS:
        for b in SLOT_SYMBOLS:
            for c in SLOT_SYMBOLS:
                p = (a[1] / total_w) * (b[1] / total_w) * (c[1] / total_w)
                rtp += p * slot_multiplier([a[0], b[0], c[0]])
    return rtp

def slot_board(reels: list) -> str:
    return "┃ " + " ┃ ".join(reels) + " ┃"

CARD_RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]
CARD_SUITS = ["♠", "♥", "♦", "♣"]

def new_deck() -> list:
    deck = [(r, s) for r in CARD_RANKS for s in CARD_SUITS]
    random.shuffle(deck)
    return deck

def hand_value(hand: list) -> int:
    total = 0
    aces = 0
    for rank, _ in hand:
        if rank == "A":
            total += 11
            aces += 1
        elif rank in ("J", "Q", "K"):
            total += 10
        else:
            total += int(rank)
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total

def is_blackjack(hand: list) -> bool:
    return len(hand) == 2 and hand_value(hand) == 21

def card_str(card: tuple) -> str:
    return f"**{card[0]}{card[1]}**"

def hand_str(hand: list) -> str:
    return " ".join(card_str(c) for c in hand)

def blackjack_outcome(player: list, dealer: list) -> str:
    p, d = hand_value(player), hand_value(dealer)
    if p > 21:
        return "lose"
    if is_blackjack(player):
        return "push" if is_blackjack(dealer) else "blackjack"
    if is_blackjack(dealer):
        return "lose"
    if d > 21 or p > d:
        return "win"
    if p == d:
        return "push"
    return "lose"

class BlackjackView(GuardedView):
    holds_command_lock = True   # 블랙잭 테이블이 끝날 때까지 다른 명령어 실행 차단

    def __init__(self, owner: discord.Member, bet: int):
        super().__init__(timeout=60)
        self.owner = owner
        self.bet = bet
        self.deck = new_deck()
        self.player = [self.deck.pop(), self.deck.pop()]
        self.dealer = [self.deck.pop(), self.deck.pop()]
        self.finished = False
        self.busy = False
        self.message = None

    def build_embed(self, reveal: bool = False, outcome: str = None, payout: int = 0,
                    money: int = 0, new_ids: list = None, note: str = "") -> discord.Embed:
        colors = {
            None: discord.Color.dark_green(), "blackjack": discord.Color.gold(),
            "win": discord.Color.green(), "push": discord.Color.blue(), "lose": discord.Color.red()
        }
        embed = discord.Embed(title="[블랙잭]", color=colors[outcome])

        dealer_text = (f"{hand_str(self.dealer)}  (합계 **{hand_value(self.dealer)}**)" if reveal
                       else f"{card_str(self.dealer[0])} **[?]**")
        embed.add_field(name=f"{self.owner.display_name}의 패 (합계 {hand_value(self.player)})", value=hand_str(self.player), inline=False)
        embed.add_field(name="딜러의 패", value=dealer_text, inline=False)

        if outcome is None:
            embed.description = f"베팅금 **${self.bet:,}**\n**히트**(카드 받기) 또는 **스탠드**(멈추기)를 선택하세요. (60초 이내)"
        else:
            if note == "timeout":
                msg = "**시간 초과** — 자동으로 패배 처리되었습니다."
            elif outcome == "blackjack":
                msg = "**블랙잭!** 3:2 배당으로 지급됩니다."
            elif outcome == "win":
                msg = "**승리!**" + (" 딜러가 버스트했습니다." if hand_value(self.dealer) > 21 else "")
            elif outcome == "push":
                msg = "**무승부** — 베팅금이 그대로 돌아옵니다."
            else:
                msg = "**버스트!** 21을 초과했습니다." if hand_value(self.player) > 21 else "**패배**"
            net = payout - self.bet
            embed.description = (f"{msg}\n\n베팅금: `${self.bet:,}`\n지급액: `${payout:,}`\n"
                                 f"손익: `{fmt_net(net)}`\n보유 자금: `${money:,}`{format_new_titles(new_ids or [])}")

        embed.set_footer(text="딜러는 16 이하에서 카드를 받고 17 이상에서 멈춥니다 · 블랙잭 3:2")
        if self.owner.avatar:
            embed.set_thumbnail(url=self.owner.avatar.url)
        return embed

    async def finish(self, edit, outcome: str, note: str = ""):
        self.finished = True
        self.stop()
        active_blackjack.pop(self.owner.id, None)

        payout = int(self.bet * BLACKJACK_PAYOUT[outcome])
        try:
            async with _gamble_lock(self.owner.id):
                new_money, new_ids = settle_gamble(self.owner.id, self.bet, payout, prepaid=True)
        except Exception as e:
            print(f"DB Error (blackjack settle): {e}")
            await edit(embed=discord.Embed(
                title="[블랙잭 정산 오류]",
                description="데이터베이스 오류로 결과를 정산하지 못했습니다. 관리자에게 문의해주세요.",
                color=discord.Color.red()), view=None)
            return

        final_embed = self.build_embed(reveal=True, outcome=outcome, payout=payout,
                                       money=new_money, new_ids=new_ids, note=note)
        final_embed.description = (final_embed.description or "") + take_daily_notice(self.owner.id)
        await edit(embed=final_embed, view=None)

    async def dealer_turn(self, edit):
        while hand_value(self.dealer) < 17:
            self.dealer.append(self.deck.pop())
        await self.finish(edit, blackjack_outcome(self.player, self.dealer))

    async def _guard(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner.id:
            await interaction.response.send_message("본인의 블랙잭 테이블만 조작할 수 있습니다.", ephemeral=True)
            return False
        await interaction.response.defer()
        return not (self.finished or self.busy)

    @discord.ui.button(label="히트 (카드 받기)", style=discord.ButtonStyle.primary)
    async def hit(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction): return
        self.busy = True
        try:
            self.player.append(self.deck.pop())
            value = hand_value(self.player)
            if value > 21:
                await self.finish(interaction.edit_original_response, "lose")
            elif value == 21:
                await self.dealer_turn(interaction.edit_original_response)
            else:
                await interaction.edit_original_response(embed=self.build_embed(), view=self)
        finally:
            self.busy = False

    @discord.ui.button(label="스탠드 (멈추기)", style=discord.ButtonStyle.secondary)
    async def stand(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction): return
        self.busy = True
        try:
            await self.dealer_turn(interaction.edit_original_response)
        finally:
            self.busy = False

    async def on_timeout(self):
        if self.finished or self.busy or self.message is None:
            return
        await self.finish(self.message.edit, "lose", note="timeout")

# ==========================================
# /도박 명령어 그룹
# ==========================================
gamble_group = app_commands.Group(name="도박", description="[카지노] 자금을 걸고 도박을 즐깁니다.")
bot.tree.add_command(gamble_group)

BET_DESC = f"베팅할 금액 (${GAMBLE_MIN_BET:,} ~ ${GAMBLE_MAX_BET:,})"

@gamble_group.command(name="슬롯", description="슬롯머신을 돌립니다. (3개 일치 고배당 / 2개 일치 본전)")
@app_commands.describe(베팅금=BET_DESC)
async def gamble_slots(interaction: discord.Interaction, 베팅금: app_commands.Range[int, GAMBLE_MIN_BET, GAMBLE_MAX_BET]):
    if not await gamble_precheck(interaction): return

    user = interaction.user
    bet = 베팅금

    money = int(get_user_data(user.id).get("money") or 0)
    if money < bet:
        gamble_cooldowns.pop(user.id, None)
        await interaction.response.send_message(f"자금이 부족합니다! (보유 자금: ${money:,})", ephemeral=True)
        return

    await interaction.response.defer()

    reels = spin_slots()
    mult = slot_multiplier(reels)
    payout = int(bet * mult)
    jackpot = (reels[0] == reels[1] == reels[2] == SLOT_JACKPOT_SYMBOL)

    try:
        async with _gamble_lock(user.id):
            new_money, new_ids = settle_gamble(user.id, bet, payout, jackpot=jackpot)
    except InsufficientFunds:
        gamble_cooldowns.pop(user.id, None)
        await interaction.edit_original_response(content="자금이 부족합니다!")
        return
    except Exception as e:
        print(f"DB Error (slots): {e}")
        gamble_cooldowns.pop(user.id, None)
        await interaction.edit_original_response(content="데이터베이스 오류로 베팅이 취소되었습니다. (자금은 차감되지 않았습니다)")
        return

    spinning = discord.Embed(title="[슬롯머신]", description=slot_board(["?", "?", "?"]) + f"\n\n베팅금 `${bet:,}`", color=discord.Color.dark_gold())
    await _safe_edit(interaction, embed=spinning)
    await asyncio.sleep(0.8)
    spinning.description = slot_board([reels[0], "?", "?"]) + f"\n\n베팅금 `${bet:,}`"
    await _safe_edit(interaction, embed=spinning)
    await asyncio.sleep(0.8)
    spinning.description = slot_board([reels[0], reels[1], "?"]) + f"\n\n베팅금 `${bet:,}`"
    await _safe_edit(interaction, embed=spinning)
    await asyncio.sleep(0.8)

    if jackpot:
        msg, color = "**JACKPOT!!!** 7⃣7⃣7⃣ 대박이 터졌습니다!", discord.Color.gold()
    elif mult >= 2:
        msg, color = f"**{reels[0]} 3개 일치!** 배당 x{mult:g}", discord.Color.green()
    elif mult == SLOT_PAIR_MULT and mult > 0:
        msg, color = "**2개 일치** — 베팅금이 그대로 돌아옵니다.", discord.Color.blue()
    else:
        msg, color = "**꽝!** 베팅금을 잃었습니다.", discord.Color.red()

    embed = discord.Embed(
        title="[슬롯머신] 결과",
        description=(f"{slot_board(reels)}\n\n{msg}\n\n베팅금: `${bet:,}`\n지급액: `${payout:,}`\n"
                     f"손익: `{fmt_net(payout - bet)}`\n보유 자금: `${new_money:,}`{format_new_titles(new_ids)}{take_daily_notice(user.id)}"),
        color=color
    )
    if user.avatar:
        embed.set_thumbnail(url=user.avatar.url)
    await _safe_edit(interaction, embed=embed)

@gamble_group.command(name="동전", description="동전 던지기에 베팅합니다. (승리 시 2배)")
@app_commands.describe(베팅금=BET_DESC, 선택="동전의 어느 면에 걸까요?")
@app_commands.choices(선택=[
    app_commands.Choice(name="앞면", value="front"),
    app_commands.Choice(name="뒷면", value="back")
])
async def gamble_coin(interaction: discord.Interaction, 베팅금: app_commands.Range[int, GAMBLE_MIN_BET, GAMBLE_MAX_BET], 선택: app_commands.Choice[str]):
    if not await gamble_precheck(interaction): return

    user = interaction.user
    bet = 베팅금

    money = int(get_user_data(user.id).get("money") or 0)
    if money < bet:
        gamble_cooldowns.pop(user.id, None)
        await interaction.response.send_message(f"자금이 부족합니다! (보유 자금: ${money:,})", ephemeral=True)
        return

    await interaction.response.defer()

    names = {"front": "앞면", "back": "뒷면"}
    won = random.random() < COIN_WIN_RATE
    result_side = 선택.value if won else ("back" if 선택.value == "front" else "front")
    payout = bet * 2 if won else 0

    try:
        async with _gamble_lock(user.id):
            new_money, new_ids = settle_gamble(user.id, bet, payout)
    except InsufficientFunds:
        gamble_cooldowns.pop(user.id, None)
        await interaction.edit_original_response(content="자금이 부족합니다!")
        return
    except Exception as e:
        print(f"DB Error (coin): {e}")
        gamble_cooldowns.pop(user.id, None)
        await interaction.edit_original_response(content="데이터베이스 오류로 베팅이 취소되었습니다. (자금은 차감되지 않았습니다)")
        return

    flipping = discord.Embed(
        title="[동전 던지기]",
        description=f"{user.mention} 님이 **{names[선택.value]}**에 `${bet:,}` 를 걸었습니다!\n\n동전이 공중에서 회전 중...",
        color=discord.Color.dark_gold()
    )
    await _safe_edit(interaction, embed=flipping)
    await asyncio.sleep(1.2)

    embed = discord.Embed(
        title="[동전 던지기] 결과",
        description=(f"결과: **{names[result_side]}**\n"
                     f"{'**적중!** 베팅금의 2배를 받았습니다.' if won else '**빗나갔습니다.** 베팅금을 잃었습니다.'}\n\n"
                     f"베팅금: `${bet:,}`\n지급액: `${payout:,}`\n"
                     f"손익: `{fmt_net(payout - bet)}`\n보유 자금: `${new_money:,}`{format_new_titles(new_ids)}{take_daily_notice(user.id)}"),
        color=discord.Color.green() if won else discord.Color.red()
    )
    if user.avatar:
        embed.set_thumbnail(url=user.avatar.url)
    await _safe_edit(interaction, embed=embed)

@gamble_group.command(name="블랙잭", description="딜러와 블랙잭 승부를 겨룹니다. (블랙잭 3:2 / 승리 2배)")
@app_commands.describe(베팅금=BET_DESC)
async def gamble_blackjack(interaction: discord.Interaction, 베팅금: app_commands.Range[int, GAMBLE_MIN_BET, GAMBLE_MAX_BET]):
    if not await gamble_precheck(interaction): return

    user = interaction.user
    bet = 베팅금

    if user.id in active_blackjack:
        gamble_cooldowns.pop(user.id, None)
        await interaction.response.send_message("이미 진행 중인 블랙잭 테이블이 있습니다! 먼저 끝내주세요.", ephemeral=True)
        return

    money = int(get_user_data(user.id).get("money") or 0)
    if money < bet:
        gamble_cooldowns.pop(user.id, None)
        await interaction.response.send_message(f"자금이 부족합니다! (보유 자금: ${money:,})", ephemeral=True)
        return

    await interaction.response.defer()

    try:
        async with _gamble_lock(user.id):
            take_bet(user.id, bet)
    except InsufficientFunds:
        gamble_cooldowns.pop(user.id, None)
        await interaction.edit_original_response(content="자금이 부족합니다!")
        return
    except Exception as e:
        print(f"DB Error (blackjack take_bet): {e}")
        gamble_cooldowns.pop(user.id, None)
        await interaction.edit_original_response(content="데이터베이스 오류로 베팅이 취소되었습니다. (자금은 차감되지 않았습니다)")
        return

    view = BlackjackView(owner=user, bet=bet)
    active_blackjack[user.id] = view

    if is_blackjack(view.player) or is_blackjack(view.dealer):
        view.message = await interaction.edit_original_response(embed=view.build_embed())
        await view.finish(interaction.edit_original_response, blackjack_outcome(view.player, view.dealer))
    else:
        view.message = await interaction.edit_original_response(embed=view.build_embed(), view=view)

@gamble_group.command(name="규칙", description="카지노 규칙·배당표와 나의 도박 기록을 확인합니다.")
async def gamble_rules(interaction: discord.Interaction):
    if not await check_maintenance(interaction): return

    s = get_title_blob(get_user_data(interaction.user.id))["stats"]
    paytable = "\n".join(f"{e}{e}{e} → **x{m}**" for e, _, m in SLOT_SYMBOLS)
    net = s["gamble_net"]

    embed = discord.Embed(
        title="[칼렌데 카지노 규칙]",
        description=f"베팅 한도: **${GAMBLE_MIN_BET:,} ~ ${GAMBLE_MAX_BET:,}** · 베팅 간격 {GAMBLE_COOLDOWN}초\n"
                    f"도박은 평균적으로 자금이 줄어듭니다. 감옥에서는 이용할 수 없습니다.",
        color=discord.Color.dark_gold()
    )
    embed.add_field(
        name="`/도박 슬롯`",
        value=f"{paytable}\n같은 그림 **2개** → 베팅금 반환 / 모두 다르면 꽝\n환수율 약 **{slot_rtp() * 100:.1f}%**",
        inline=False
    )
    embed.add_field(name="`/도박 동전`", value=f"앞면/뒷면 중 선택 · 적중 시 **2배** (승률 **{COIN_WIN_RATE * 100:.0f}%**)", inline=False)
    embed.add_field(
        name="`/도박 블랙잭`",
        value="21에 가까운 쪽이 승리 (21 초과 시 버스트)\n승리 **2배** · 블랙잭(처음 2장 21) **2.5배** · 무승부 베팅금 반환\n딜러는 16 이하 히트 / 17 이상 스탠드 · 60초 내 미선택 시 패배",
        inline=False
    )
    embed.add_field(
        name="나의 도박 기록",
        value=f"총 `{s['gamble_played']}`판 / 승리 `{s['gamble_won']}`판 / 누적 베팅 `${s['gamble_wagered']:,}`\n누적 손익 `{fmt_net(net)}` / 잭팟 `{s['jackpot']}`회",
        inline=False
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)
