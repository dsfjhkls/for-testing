"""/경매"""

import asyncio
import discord
import re
import time
from discord import app_commands
from datetime import datetime

from .core import bot, supabase, wanted_list
from .status import _check_updated, _require_db, check_jail, get_user_data
from .alignment import get_title_blob
from .admin import check_maintenance, check_membership
from .profile import POLICE_SHOP_ITEMS
from .crafting import _inv_lock

# ==========================================================================
# [/경매]  플레이어 경매장
#   • /경매 아이템 [수량] [시작가]  : 인벤토리의 아이템·장비를 경매에 올립니다. (시작가 최소 $1,000)
#   • 경찰·조직원 누구나 버튼으로 입찰할 수 있습니다. 입찰 금액은 입찰 즉시 빠져나가고,
#     다른 사람이 더 높게 부르면 즉시 전액 환불됩니다.
#   • 마지막 입찰 후 AUCTION_BID_SEC(10초) 동안 아무도 레이즈하지 않으면 낙찰 → 낙찰자가 아이템을 받고, 판매자가 금액을 받습니다.
#     (입찰 상한은 없습니다. 자기 자신의 입찰을 다시 올리는 것은 막아 두었습니다.)
#   • 입찰이 없으면 유찰되어 아이템이 판매자에게 돌아옵니다. 판매자는 입찰이 들어오기 전까지 취소할 수 있습니다.
#
#   [안전장치]  아이템·입찰금은 user_data.data 안에 "에스크로 기록"(auction_item / auction_escrow)으로 남습니다.
#               봇이 경매 도중 꺼지거나 재시작돼도, 켜질 때 on_ready 에서 자동으로 아이템 반환·입찰금 환불을 합니다.
#               (봇을 동시에 두 개 이상 띄우면 안 됩니다. 한쪽이 켜질 때 다른 쪽의 진행 중 경매를 되돌려 버립니다.)
# ==========================================================================
AUCTION_START_MIN = 1000          # 시작가 최소 금액
AUCTION_PRICE_MAX = 2_000_000_000 # 입력 안전 한도 (DB 정수 범위 보호용. 실질적으론 상한 없음)
AUCTION_MIN_RAISE = 100           # 최소 레이즈 폭
AUCTION_OPEN_SEC = 60             # 첫 입찰이 들어오기까지 기다리는 시간 (없으면 유찰)
AUCTION_BID_SEC = 10              # 마지막 입찰 후 낙찰까지 시간
AUCTION_RAISE_STEPS = (1000, 10000, 100000)   # [+금액] 버튼
AUCTION_MAX_PER_GUILD = 3         # 서버당 동시 진행 경매 수
AUCTION_FEE_RATE = 0.0            # 판매 수수료 (0.05 = 5%). 기본은 수수료 없음

active_auctions = {}              # {경매id: AuctionSession}
auction_by_seller = {}            # {판매자 user_id: 경매id}  (1인 1경매)
_auction_tasks = set()            # 진행 중인 asyncio 작업 (가비지 컬렉션 방지)

def auction_blocked_reason(item: str):
    """거래 불가 아이템이면 사유를, 가능하면 None"""
    from .dept_store import LUXURY_BY_NAME   # 순환 import 방지: 호출 시점에 가져옴
    if item.startswith(("[전리품]", "[강화보스 전리품]")):
        return "고유 전리품은 칭호·업적과 연결되어 있어 거래할 수 없습니다."
    if item in {it["name"] for it in POLICE_SHOP_ITEMS}:
        return "치안국 보급품은 거래할 수 없습니다."
    if item in LUXURY_BY_NAME:
        return "사치품은 개인 소유물이라 거래·경매·압류할 수 없습니다."
    return None

# ── DB 작업 (동기, to_thread 로 실행) ────────────────────────────────────
def _auction_retry(fn, *args, tries: int = 3):
    last = None
    for i in range(tries):
        try:
            return fn(*args)
        except Exception as e:
            last = e
            time.sleep(0.4 * (i + 1))
    raise last

def _auction_update(user_id, payload: dict):
    def _do():
        res = supabase.table("user_data").update(payload).eq("user_id", str(user_id)).execute()
        _check_updated(res, "auction_update")
    _auction_retry(_do)

def auction_list_sync(aid: str, seller_id: int, item: str, qty: int) -> str:
    """판매 아이템을 인벤토리에서 빼서 에스크로 기록으로 옮김. 반환: ok / no_item"""
    _require_db()
    with _inv_lock:
        info = get_user_data(seller_id)
        inv = dict(info.get("inventory") or {})
        have = int(inv.get(item, 0) or 0)
        if have < qty:
            return "no_item"
        if have == qty:
            inv.pop(item, None)
        else:
            inv[item] = have - qty
        blob = get_title_blob(info)
        blob.setdefault("auction_item", {})[aid] = {"item": item, "qty": qty}
        _auction_update(seller_id, {"inventory": inv, "data": blob})
    return "ok"

def auction_return_item_sync(aid: str, seller_id: int) -> bool:
    """에스크로 기록의 아이템을 판매자에게 돌려줌 (유찰·취소)"""
    _require_db()
    with _inv_lock:
        info = get_user_data(seller_id)
        blob = get_title_blob(info)
        rec = (blob.get("auction_item") or {}).pop(aid, None)
        if not rec:
            return False
        inv = dict(info.get("inventory") or {})
        inv[rec["item"]] = inv.get(rec["item"], 0) + int(rec["qty"])
        _auction_update(seller_id, {"inventory": inv, "data": blob})
    return True

def auction_bid_sync(aid: str, bidder_id: int, amount: int, prev_id, prev_amount: int):
    """새 입찰자의 돈을 먼저 차감(에스크로)하고, 이전 최고 입찰자에게 전액 환불. 반환: (상태, 값)"""
    _require_db()
    with _inv_lock:
        info = get_user_data(bidder_id)
        money = int(info.get("money", 0) or 0)
        if money < amount:
            return "no_money", money
        blob = get_title_blob(info)
        blob.setdefault("auction_escrow", {})[aid] = amount
        _auction_update(bidder_id, {"money": money - amount, "data": blob})
        if prev_id:
            try:
                p_info = get_user_data(prev_id)
                p_blob = get_title_blob(p_info)
                (p_blob.get("auction_escrow") or {}).pop(aid, None)
                _auction_update(prev_id, {"money": int(p_info.get("money", 0) or 0) + prev_amount, "data": p_blob})
            except Exception as e:
                # 환불이 실패해도 이전 입찰자의 에스크로 기록이 남아 있어 다음 봇 시작 때 자동 환불됩니다.
                print(f"[경매 환불 실패 → 재시작 시 복구] aid={aid} user={prev_id}: {e}")
        return "ok", money - amount

def auction_settle_sync(s: "AuctionSession") -> int:
    """낙찰 정산: 낙찰자에게 아이템, 판매자에게 대금. 반환: 판매자가 받은 금액"""
    _require_db()
    fee = int(s.current * AUCTION_FEE_RATE)
    payout = s.current - fee
    with _inv_lock:
        w_info = get_user_data(s.high_id)
        w_blob = get_title_blob(w_info)
        (w_blob.get("auction_escrow") or {}).pop(s.id, None)
        w_inv = dict(w_info.get("inventory") or {})
        w_inv[s.item] = w_inv.get(s.item, 0) + s.qty
        _auction_update(s.high_id, {"inventory": w_inv, "data": w_blob})

        s_info = get_user_data(s.seller_id)
        s_blob = get_title_blob(s_info)
        (s_blob.get("auction_item") or {}).pop(s.id, None)
        _auction_update(s.seller_id, {"money": int(s_info.get("money", 0) or 0) + payout, "data": s_blob})
    return payout

def auction_recover_sync():
    """봇 시작 시: 이전 실행에서 끝나지 못한 경매의 에스크로(아이템·입찰금)를 모두 되돌림. 반환: (아이템 건수, 환불 인원)"""
    if not supabase:
        return 0, 0
    n_items = n_refunds = 0
    start = 0
    while True:
        rows = supabase.table("user_data").select("user_id,data").range(start, start + 999).execute().data or []
        for row in rows:
            raw = row.get("data")
            if not isinstance(raw, dict) or not (raw.get("auction_escrow") or raw.get("auction_item")):
                continue
            with _inv_lock:
                info = get_user_data(row["user_id"])
                blob = get_title_blob(info)
                esc = blob.pop("auction_escrow", None) or {}
                itm = blob.pop("auction_item", None) or {}
                payload = {"data": blob}
                if esc:
                    payload["money"] = int(info.get("money", 0) or 0) + sum(int(v) for v in esc.values())
                    n_refunds += 1
                if itm:
                    inv = dict(info.get("inventory") or {})
                    for rec in itm.values():
                        inv[rec["item"]] = inv.get(rec["item"], 0) + int(rec["qty"])
                        n_items += 1
                    payload["inventory"] = inv
                _auction_update(row["user_id"], payload)
        if len(rows) < 1000:
            break
        start += 1000
    return n_items, n_refunds

_auction_recover_state = {"done": False}

@bot.listen("on_ready")
async def _auction_recover_on_ready():
    if _auction_recover_state["done"]:       # 재연결로 on_ready 가 또 불려도 진행 중인 경매를 건드리지 않도록 한 번만
        return
    _auction_recover_state["done"] = True
    try:
        n_items, n_refunds = await asyncio.to_thread(auction_recover_sync)
        print(f"경매 에스크로 복구: 아이템 {n_items}건 반환 / 입찰금 {n_refunds}명 환불")
    except Exception as e:
        print(f"경매 에스크로 복구 실패: {e}")

# ── 경매 진행 상태 ────────────────────────────────────────────────────────
class AuctionSession:
    def __init__(self, aid: str, guild_id: int, seller, item: str, qty: int, start_price: int):
        self.id, self.guild_id = aid, guild_id
        self.seller_id, self.seller_name = seller.id, seller.display_name
        self.item, self.qty, self.start_price = item, qty, start_price
        self.current = start_price
        self.high_id = None
        self.high_name = ""
        self.bids = []                          # [(이름, 금액)]
        self.deadline = time.monotonic() + AUCTION_OPEN_SEC
        self.deadline_epoch = int(time.time()) + AUCTION_OPEN_SEC
        self.ended = False
        self.cancelled = False
        self.result = None                      # sold / unsold / cancelled / error
        self.payout = 0
        self.lock = asyncio.Lock()              # 입찰·종료 판정을 한 줄로 세우는 잠금
        self.edit_lock = asyncio.Lock()         # 메시지 갱신 순서 보장
        self.channel = None
        self.message = None                     # PartialMessage (채널 권한으로 수정)
        self.message_hook = None                # 슬래시 명령 응답 메시지 (예비 수단)
        self.view = None

def _auction_item_text(s: AuctionSession) -> str:
    return f"{s.item} x{s.qty}" if s.qty > 1 else s.item

def auction_min_bid(s: AuctionSession) -> int:
    return s.current if s.high_id is None else s.current + AUCTION_MIN_RAISE

def auction_embed(s: AuctionSession) -> discord.Embed:
    item_txt = _auction_item_text(s)
    if not s.ended:
        embed = discord.Embed(
            title=f"[경매] {item_txt}",
            color=discord.Color.gold() if s.high_id else discord.Color.blurple()
        )
        embed.add_field(name="판매자", value=f"<@{s.seller_id}>", inline=True)
        embed.add_field(name="현재 최고가" if s.high_id else "시작가", value=f"`${s.current:,}`", inline=True)
        embed.add_field(name="최고 입찰자", value=f"<@{s.high_id}>" if s.high_id else "없음", inline=True)
        if s.high_id:
            timing = f"<t:{s.deadline_epoch}:R> 까지 아무도 레이즈하지 않으면 **낙찰**됩니다."
        else:
            timing = f"<t:{s.deadline_epoch}:R> 까지 입찰이 없으면 **유찰**됩니다."
        embed.add_field(name="진행", value=timing + f"\n입찰이 들어올 때마다 **{AUCTION_BID_SEC}초**로 다시 시작합니다. (입찰 상한 없음)", inline=False)
        embed.set_footer(text="입찰금은 즉시 차감되고, 더 높은 입찰이 들어오면 전액 환불됩니다.")
    else:
        if s.result == "sold":
            embed = discord.Embed(title=f"[경매 낙찰] {item_txt}", color=discord.Color.green(),
                                  description=f"<@{s.high_id}> 님이 `${s.current:,}` 에 낙찰받았습니다!\n"
                                              f"판매자 <@{s.seller_id}> 님은 `${s.payout:,}` 를 받았습니다.")
        elif s.result == "cancelled":
            embed = discord.Embed(title=f"[경매 취소] {item_txt}", color=discord.Color.dark_grey(),
                                  description="판매자가 경매를 취소했습니다. 아이템은 판매자에게 돌아갔습니다.")
        elif s.result == "error":
            embed = discord.Embed(title=f"[경매 오류] {item_txt}", color=discord.Color.red(),
                                  description="정산 중 오류가 발생했습니다. 아이템과 입찰금은 봇이 다시 시작될 때 자동으로 되돌려집니다.\n"
                                              "급하면 관리자에게 문의해 주세요.")
        else:
            embed = discord.Embed(title=f"[경매 유찰] {item_txt}", color=discord.Color.dark_grey(),
                                  description="입찰이 없어 유찰되었습니다. 아이템은 판매자에게 돌아갔습니다.")
    if s.bids:
        embed.add_field(name="최근 입찰", value="\n".join(f"• {n} `${a:,}`" for n, a in s.bids[-5:][::-1]), inline=False)
    return embed

async def auction_refresh(s: AuctionSession):
    """현재 상태 그대로 경매 메시지를 갱신 (항상 최신 상태로 그려지도록 잠금 안에서 만든다)"""
    async with s.edit_lock:
        if s.view:
            s.view.sync_labels()
        kwargs = {"embed": auction_embed(s), "view": None if s.ended else s.view}
        last = None
        for target in (s.message, s.message_hook):
            if target is None:
                continue
            try:
                await target.edit(**kwargs)
                return
            except (discord.Forbidden, discord.NotFound, discord.HTTPException) as e:
                last = e
        print(f"[경매 메시지 갱신 실패] aid={s.id}: {last}")

async def auction_place_bid(s: AuctionSession, user: discord.abc.User, amount: int):
    """입찰 처리. 실패하면 사유 문자열, 성공하면 None"""
    if user.id == s.seller_id:
        return "자신이 올린 경매에는 입찰할 수 없습니다."
    if user.id == s.high_id:
        return "이미 최고 입찰자입니다. 다른 사람이 더 높게 부르면 다시 도전하세요."
    if amount > AUCTION_PRICE_MAX:
        return f"입찰 금액이 너무 큽니다. (최대 `${AUCTION_PRICE_MAX:,}`)"

    async with s.lock:
        if s.ended or time.monotonic() >= s.deadline:
            return "이미 종료된 경매입니다."
        need = auction_min_bid(s)
        if amount < need:
            return f"현재 최소 입찰액은 `${need:,}` 입니다."
        prev_id, prev_amount = s.high_id, (s.current if s.high_id else 0)
        try:
            status, val = await asyncio.to_thread(auction_bid_sync, s.id, user.id, amount, prev_id, prev_amount)
        except Exception as e:
            print(f"[경매 입찰 오류] aid={s.id}: {e}")
            return "입찰을 처리하는 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."
        if status == "no_money":
            return f"자금이 부족합니다. (보유 `${val:,}` / 입찰 `${amount:,}`)"
        s.current, s.high_id, s.high_name = amount, user.id, user.display_name
        s.bids.append((user.display_name, amount))
        s.deadline = time.monotonic() + AUCTION_BID_SEC          # 레이즈가 들어올 때마다 10초 다시 시작
        s.deadline_epoch = int(time.time()) + AUCTION_BID_SEC
    await auction_refresh(s)
    return None

async def auction_precheck(interaction: discord.Interaction) -> bool:
    """점검 / 가입(조직원·경찰) / 수감 여부. 실패하면 직접 안내를 보내므로 호출 전에 응답하지 않은 상태여야 한다."""
    if not await check_maintenance(interaction): return False
    if not await check_membership(interaction): return False
    if await check_jail(interaction): return False
    return True

# ── 버튼 / 입력창 ────────────────────────────────────────────────────────
class _AuctionBidButton(discord.ui.Button):
    def __init__(self, s: AuctionSession, **kw):
        super().__init__(**kw)
        self.s = s

    def amount(self) -> int:
        raise NotImplementedError

    async def callback(self, interaction: discord.Interaction):
        s = self.s
        if s.ended:
            await interaction.response.send_message("이미 종료된 경매입니다.", ephemeral=True)
            return
        if not await auction_precheck(interaction): return
        await interaction.response.defer()
        msg = await auction_place_bid(s, interaction.user, self.amount())
        if msg:
            await interaction.followup.send(msg, ephemeral=True)

class AuctionMinButton(_AuctionBidButton):
    def __init__(self, s):
        super().__init__(s, label="입찰", style=discord.ButtonStyle.success, row=0)
    def amount(self): return auction_min_bid(self.s)

class AuctionRaiseButton(_AuctionBidButton):
    def __init__(self, s, step: int):
        super().__init__(s, label=f"+${step:,}", style=discord.ButtonStyle.primary, row=0)
        self.step = step
    def amount(self): return self.s.current + self.step

class AuctionBidModal(discord.ui.Modal, title="입찰 금액 입력"):
    amount_input = discord.ui.TextInput(label="입찰 금액 ($)", placeholder="예: 25000", min_length=1, max_length=14)

    def __init__(self, s: AuctionSession):
        super().__init__()
        self.s = s

    async def on_submit(self, interaction: discord.Interaction):
        raw = re.sub(r"[,\s$]", "", str(self.amount_input.value))
        if not raw.isdigit():
            await interaction.response.send_message("숫자만 입력해 주세요.", ephemeral=True)
            return
        amount = int(raw)
        await interaction.response.defer(ephemeral=True, thinking=True)
        msg = await auction_place_bid(self.s, interaction.user, amount)
        await interaction.followup.send(msg or f"`${amount:,}` 입찰 완료!", ephemeral=True)

class AuctionCustomButton(discord.ui.Button):
    def __init__(self, s):
        super().__init__(label="직접 입력", style=discord.ButtonStyle.secondary, row=0)
        self.s = s

    async def callback(self, interaction: discord.Interaction):
        if self.s.ended:
            await interaction.response.send_message("이미 종료된 경매입니다.", ephemeral=True)
            return
        if not await auction_precheck(interaction): return
        await interaction.response.send_modal(AuctionBidModal(self.s))

class AuctionCancelButton(discord.ui.Button):
    def __init__(self, s):
        super().__init__(label="경매 취소 (판매자)", style=discord.ButtonStyle.danger, row=1)
        self.s = s

    async def callback(self, interaction: discord.Interaction):
        s = self.s
        if interaction.user.id != s.seller_id:
            await interaction.response.send_message("판매자만 취소할 수 있습니다.", ephemeral=True)
            return
        async with s.lock:
            if s.ended:
                err = "이미 종료된 경매입니다."
            elif s.high_id:
                err = "이미 입찰이 들어와 취소할 수 없습니다."
            else:
                err = None
                s.cancelled = True
                s.deadline = 0          # 진행 루프가 바로 종료 처리
        if err:
            await interaction.response.send_message(err, ephemeral=True)
        else:
            await interaction.response.defer()

class AuctionView(discord.ui.View):
    def __init__(self, s: AuctionSession):
        super().__init__(timeout=None)
        self.s = s
        self.min_btn = AuctionMinButton(s)
        self.cancel_btn = AuctionCancelButton(s)
        self.add_item(self.min_btn)
        for step in AUCTION_RAISE_STEPS:
            self.add_item(AuctionRaiseButton(s, step))
        self.add_item(AuctionCustomButton(s))
        self.add_item(self.cancel_btn)
        self.sync_labels()

    def sync_labels(self):
        need = auction_min_bid(self.s)
        self.min_btn.label = f"시작가 ${need:,} 입찰" if self.s.high_id is None else f"최소 ${need:,} 입찰"
        self.cancel_btn.disabled = bool(self.s.high_id)      # 입찰이 들어오면 취소 불가

# ── 진행 / 종료 ──────────────────────────────────────────────────────────
async def auction_finish(s: AuctionSession):
    if s.cancelled or s.high_id is None:
        s.result = "cancelled" if s.cancelled else "unsold"
        try:
            await asyncio.to_thread(auction_return_item_sync, s.id, s.seller_id)
        except Exception as e:
            print(f"[경매 아이템 반환 실패 → 재시작 시 복구] aid={s.id}: {e}")
            s.result = "error"
    else:
        try:
            s.payout = await asyncio.to_thread(auction_settle_sync, s)
            s.result = "sold"
        except Exception as e:
            print(f"[경매 정산 실패 → 재시작 시 복구] aid={s.id}: {e}")
            s.result = "error"
    await auction_refresh(s)

    if s.result == "sold" and s.channel is not None:       # 낙찰자·판매자에게 알림
        try:
            await s.channel.send(f"<@{s.high_id}> 님이 **{_auction_item_text(s)}** 을(를) `${s.current:,}` 에 낙찰받았습니다! (판매자 <@{s.seller_id}>)")
        except discord.HTTPException:
            pass

async def run_auction(s: AuctionSession):
    try:
        while True:
            async with s.lock:
                remaining = s.deadline - time.monotonic()
                if remaining <= 0:
                    s.ended = True          # 종료 판정은 잠금 안에서 → 이 순간부터 새 입찰은 거절됨
                    break
            await asyncio.sleep(min(max(remaining, 0.05), 1.0))
        await auction_finish(s)
    except Exception as e:
        print(f"[경매 진행 오류] aid={s.id}: {type(e).__name__}: {e}")
        s.ended = True
        s.result = "error"
        try:
            await auction_refresh(s)
        except Exception:
            pass
    finally:
        active_auctions.pop(s.id, None)
        if auction_by_seller.get(s.seller_id) == s.id:
            auction_by_seller.pop(s.seller_id, None)

# ── 슬래시 명령어 ────────────────────────────────────────────────────────
async def _auction_item_autocomplete(interaction: discord.Interaction, current: str):
    try:
        info = await asyncio.to_thread(get_user_data, interaction.user.id)
    except Exception:
        return []
    inv = info.get("inventory") or {}
    cur = current.strip().lower()
    out = []
    for name, cnt in inv.items():
        if int(cnt or 0) <= 0 or auction_blocked_reason(name):
            continue
        if cur and cur not in name.lower():
            continue
        label = f"{name} (x{cnt})" if int(cnt) > 1 else name
        out.append(app_commands.Choice(name=label[:100], value=name[:100]))
        if len(out) >= 25:
            break
    return out

@bot.tree.command(name="경매", description="[공용] 인벤토리의 아이템·장비를 경매에 올립니다. 누구나 입찰할 수 있어요.")
@app_commands.describe(아이템="경매에 올릴 아이템 (목록에서 선택)",
                       수량="올릴 개수 (재료·소모품만 여러 개 가능, 기본 1)",
                       시작가=f"시작 금액 (최소 ${AUCTION_START_MIN:,}, 기본 ${AUCTION_START_MIN:,})")
@app_commands.autocomplete(아이템=_auction_item_autocomplete)
async def auction_command(interaction: discord.Interaction, 아이템: str,
                          수량: app_commands.Range[int, 1, 9999] = 1,
                          시작가: app_commands.Range[int, AUCTION_START_MIN, AUCTION_PRICE_MAX] = AUCTION_START_MIN):
    if not await auction_precheck(interaction): return

    user = interaction.user
    now_dt = datetime.now()
    if any(int(k) == int(user.id) and now_dt <= exp for k, exp in list(wanted_list.items())):
        # 수배 중에 경매로 아이템을 빼돌려 /검거 압류를 피하는 것을 막습니다. (입찰은 가능)
        await interaction.response.send_message("**지명수배 중에는 경매에 물건을 올릴 수 없습니다.** 수배가 풀린 뒤에 다시 시도해 주세요.", ephemeral=True)
        return
    reason = auction_blocked_reason(아이템)
    if reason:
        await interaction.response.send_message(reason, ephemeral=True)
        return
    if auction_by_seller.get(user.id) in active_auctions:
        await interaction.response.send_message("이미 진행 중인 내 경매가 있습니다. 끝난 뒤에 다시 올려주세요.", ephemeral=True)
        return
    gid = interaction.guild_id
    if sum(1 for a in active_auctions.values() if a.guild_id == gid) >= AUCTION_MAX_PER_GUILD:
        await interaction.response.send_message(f"이 서버에서는 동시에 {AUCTION_MAX_PER_GUILD}개까지만 경매를 열 수 있습니다. 잠시 후 다시 시도해 주세요.", ephemeral=True)
        return

    await interaction.response.defer()
    import uuid
    aid = uuid.uuid4().hex[:8]
    try:
        status = await asyncio.to_thread(auction_list_sync, aid, user.id, 아이템, 수량)
    except Exception as e:
        print(f"[경매 등록 오류] {e}")
        await interaction.followup.send("경매를 등록하는 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.", ephemeral=True)
        return
    if status == "no_item":
        await interaction.followup.send("해당 아이템을 그만큼 보유하고 있지 않습니다.", ephemeral=True)
        return

    s = AuctionSession(aid, gid, user, 아이템, 수량, 시작가)
    s.channel = interaction.channel
    s.view = AuctionView(s)
    active_auctions[aid] = s
    auction_by_seller[user.id] = aid
    try:
        msg = await interaction.followup.send(embed=auction_embed(s), view=s.view, wait=True)
    except Exception as e:                       # 메시지를 못 보냈으면 경매를 열지 않고 아이템 반환
        print(f"[경매 메시지 전송 실패] {e}")
        active_auctions.pop(aid, None)
        auction_by_seller.pop(user.id, None)
        await asyncio.to_thread(auction_return_item_sync, aid, user.id)
        return
    s.message_hook = msg
    if interaction.channel is not None and hasattr(interaction.channel, "get_partial_message"):
        s.message = interaction.channel.get_partial_message(msg.id)

    task = asyncio.create_task(run_auction(s))
    _auction_tasks.add(task)
    task.add_done_callback(_auction_tasks.discard)
