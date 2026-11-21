"""송금 제한 및 /송금"""

import asyncio
import copy
import discord
import time
from discord import app_commands

from .core import bot, report_error, supabase
from .status import _check_updated, _require_db, check_jail, get_user_data, update_user_settings
from .alignment import get_title_blob
from .admin import (
    TRANSFER_ALERT_AMOUNT,
    TRANSFER_COOLDOWN_SEC,
    TRANSFER_DAILY_RECEIVE_LIMIT,
    TRANSFER_DAILY_SEND_LIMIT,
    TRANSFER_MAX_PER_TX,
    TRANSFER_MIN_ACCOUNT_AGE_DAYS,
    TRANSFER_MIN_SERVER_STAY_HOURS,
    TRANSFER_WINDOW_SEC,
    admin_audit,
    check_maintenance,
    check_membership,
)

# ==========================================
# [송금 제한]
#   기록은 유저 데이터(data)의 "transfers" 항목에 24시간 분만 보관됩니다.  {"sent": [[시각, 금액]...], "received": [...]}
# ==========================================
def _transfer_ledger(blob: dict, kind: str, now: float) -> list:
    tr = blob.get("transfers") if isinstance(blob.get("transfers"), dict) else {}
    rows = []
    for row in (tr.get(kind) or []):
        try:
            t, a = float(row[0]), int(row[1])
        except (TypeError, ValueError, IndexError):
            continue
        if now - t < TRANSFER_WINDOW_SEC:
            rows.append([t, a])
    rows.sort()
    tr[kind] = rows
    blob["transfers"] = tr
    return rows

def transfer_money_limited_sync(sender_id: int, receiver_id: int, amount: int, bypass: bool = False, commit: bool = True) -> dict:
    """제한을 확인하고 송금합니다. (commit=False 이면 확인만 하고 저장하지 않음)
    반환: {"ok": bool, "reason": 실패 사유, "sender_money", "receiver_money", "sent_today"}"""
    from .crafting import _inv_lock   # 순환 import 방지: 호출 시점에 가져옴
    _require_db()
    now = time.time()
    with _inv_lock:
        s_info, r_info = get_user_data(sender_id), get_user_data(receiver_id)
        s_money, r_money = int(s_info.get("money") or 0), int(r_info.get("money") or 0)
        s_data_before = copy.deepcopy(s_info.get("data"))      # 되돌릴 때 쓸 원본
        s_blob, r_blob = get_title_blob(copy.deepcopy(s_info)), get_title_blob(copy.deepcopy(r_info))
        sent = _transfer_ledger(s_blob, "sent", now)
        recv = _transfer_ledger(r_blob, "received", now)
        sent_total = sum(a for _, a in sent)

        def fail(reason):
            return {"ok": False, "reason": reason, "sender_money": s_money, "receiver_money": r_money, "sent_today": sent_total}

        if s_money < amount:
            return fail(f"보유 자금이 부족합니다!\n- 보유 중인 자금: `${s_money:,}`\n- 송금하려는 금액: `${amount:,}`")
        if not bypass:
            if amount > TRANSFER_MAX_PER_TX:
                return fail(f"1회 송금은 최대 `${TRANSFER_MAX_PER_TX:,}` 까지 가능합니다.")
            if sent and now - sent[-1][0] < TRANSFER_COOLDOWN_SEC:
                wait = int(TRANSFER_COOLDOWN_SEC - (now - sent[-1][0]) + 0.999)
                return fail(f"연속 송금은 제한되어 있습니다. **{wait}초** 뒤에 다시 시도해 주세요.")
            if sent_total + amount > TRANSFER_DAILY_SEND_LIMIT:
                left = max(0, TRANSFER_DAILY_SEND_LIMIT - sent_total)
                reset = int(sent[0][0] + TRANSFER_WINDOW_SEC) if sent else int(now)
                return fail(f"하루 송금 한도를 넘습니다. (24시간 한도 `${TRANSFER_DAILY_SEND_LIMIT:,}` / 지금 보낼 수 있는 금액 `${left:,}`)\n"
                            f"가장 오래된 송금 기록이 <t:{reset}:R> 에 풀립니다.")
            recv_total = sum(a for _, a in recv)
            if recv_total + amount > TRANSFER_DAILY_RECEIVE_LIMIT:
                left = max(0, TRANSFER_DAILY_RECEIVE_LIMIT - recv_total)
                return fail(f"받는 사람의 하루 수령 한도를 넘습니다. (상대가 지금 받을 수 있는 금액 `${left:,}`)")

        if not commit:
            return {"ok": True, "reason": "", "sender_money": s_money, "receiver_money": r_money, "sent_today": sent_total}

        sent.append([now, amount])
        recv.append([now, amount])
        res = supabase.table("user_data").update({"money": s_money - amount, "data": s_blob}).eq("user_id", str(sender_id)).execute()
        _check_updated(res, "transfer (sender)")
        try:
            res = supabase.table("user_data").update({"money": r_money + amount, "data": r_blob}).eq("user_id", str(receiver_id)).execute()
            _check_updated(res, "transfer (receiver)")
        except Exception:
            try:   # 적립에 실패했다면 송금자의 자금과 기록을 원래대로 되돌린다
                supabase.table("user_data").update({"money": s_money, "data": s_data_before}).eq("user_id", str(sender_id)).execute()
            except Exception as rollback_err:
                print(f"DB Error (transfer rollback): {rollback_err}")
            raise
        return {"ok": True, "reason": "", "sender_money": s_money - amount, "receiver_money": r_money + amount,
                "sent_today": sent_total + amount}

async def check_transfer_member_eligibility(interaction: discord.Interaction, sender: discord.Member, receiver: discord.Member) -> bool:
    """계정 생성일 / 서버 가입 시간 제한 (신규·다계정 악용 방지). 통과하면 True"""
    now = discord.utils.utcnow()
    for who, label in ((sender, "보내는 사람"), (receiver, "받는 사람")):
        age_days = (now - who.created_at).days
        if age_days < TRANSFER_MIN_ACCOUNT_AGE_DAYS:
            await interaction.response.send_message(
                f"{label}({who.display_name})의 디스코드 계정이 만들어진 지 **{TRANSFER_MIN_ACCOUNT_AGE_DAYS}일**이 지나야 송금할 수 있습니다.", ephemeral=True)
            return False
        if who.joined_at is not None:
            stay_hours = (now - who.joined_at).total_seconds() / 3600
            if stay_hours < TRANSFER_MIN_SERVER_STAY_HOURS:
                left = int(TRANSFER_MIN_SERVER_STAY_HOURS - stay_hours + 0.999)
                await interaction.response.send_message(
                    f"{label}({who.display_name})가 서버에 들어온 지 **{TRANSFER_MIN_SERVER_STAY_HOURS}시간**이 지나야 송금할 수 있습니다. (약 {left}시간 남음)", ephemeral=True)
                return False
    return True

# ==========================================
# 통합 설정 시스템
#   /설정 → 설정칸(카드)이 표시되고, 버튼으로 칸 이동 / 가운데 버튼으로 켜기·끄기
#
#   새 설정칸을 추가하는 방법
#     1) 아래 SETTINGS_DEFS 에 딕셔너리 1개(항목)를 추가
#     2) 해당 기능 코드에서 get_setting(유저데이터, "키") 로 값을 읽어서 사용
# ==========================================
SETTINGS_DEFS = [
    {
        "key": "auto_fail_guard",
        "category": "임무 안전 장치",
        "name": "임무 실패 방지권 자동 사용",
        "desc": "임무 실패 시 가방의 방지권을 자동으로 소모해 수배를 방어합니다.\n"
                "끄면 방지권이 있어도 사용하지 않고 일반 실패로 처리됩니다.",
        "on_text": "자동 사용",
        "off_text": "수동 사용",
        "default": True,
    },
    {
        "key": "hardcore_mission",
        "category": "임무 난이도",
        "name": "임무(작전) 수행 하드코어",
        "desc": "임무(작전)를 여러 단계의 현장 작업(이동, 차단기, 전선 연결, 신호 기억, 금고 해제, 돌발 상황)으로 세세히 수행합니다.\n"
                "임무 한 번에 2~3분이 걸리고 어려우므로 켜는 것을 추천하지 않습니다.\n"
                "끄면 원래 방식(임무를 고르면 바로 결과 확인)으로 진행됩니다.",
        "on_text": "하드코어",
        "off_text": "일반",
        "default": False,
    },
    {
        "key": "alert_mode",
        "category": "시스템 알림 환경",
        "name": "긴급 수배 및 이벤트 알림",
        "desc": "주요 이벤트나 수배 경보 발생 시 멘션 알림을 받습니다.",
        "on_text": "활성화",
        "off_text": "차단",
        "default": True,
    },
]
SETTING_DEFAULTS = {d["key"]: d["default"] for d in SETTINGS_DEFS}
 
def get_setting(user_info: dict, key: str) -> bool:
    """유저 데이터에서 설정값을 읽음. 설정이 없거나 settings 가 비어(null) 있어도 기본값을 반환"""
    settings = user_info.get("settings")
    if isinstance(settings, dict) and key in settings:
        return bool(settings[key])
    return SETTING_DEFAULTS.get(key, False)
 
def build_settings_embed(user: discord.abc.User, settings: dict, page: int) -> discord.Embed:
    total = len(SETTINGS_DEFS)
    info = {"settings": settings}
    d = SETTINGS_DEFS[page]
    on = get_setting(info, d["key"])
    state = f"**켜짐** ({d['on_text']})" if on else f"**꺼짐** ({d['off_text']})"
 
    embed = discord.Embed(
        title="[패밀리 시스템 통합 설정]",
        description=f"**{user.mention}** 님의 게임 환경 설정을 관리합니다.\n"
                    f"버튼으로 설정칸을 넘기고, 가운데 버튼으로 **켜기 / 끄기**를 바꾸세요.",
        color=discord.Color.dark_purple()
    )
    embed.add_field(
        name=f"[설정칸 {page + 1}/{total}] {d['category']}",
        value=f"• **항목:** {d['name']}\n"
              f"• **설명:** {d['desc']}\n\n"
              f"현재 상태: {state}",
        inline=False
    )
 
    overview = []
    for i, s in enumerate(SETTINGS_DEFS):
        mark = ">" if i == page else "-"
        overview.append(f"{mark} [{'ON' if get_setting(info, s['key']) else 'OFF'}] {s['name']}")
    embed.add_field(name="전체 설정 현황", value="\n".join(overview), inline=False)
 
    if user.avatar:
        embed.set_thumbnail(url=user.avatar.url)
    embed.set_footer(text=f"설정칸 {page + 1} / {total}")
    return embed
 
class SettingsView(discord.ui.View):
    def __init__(self, user: discord.Member, settings: dict, page: int = 0):
        super().__init__(timeout=180)
        self.user = user
        self.settings = settings      # 창을 열 때 한 번 읽어 두고, 칸 이동에는 DB를 쓰지 않음
        self.page = page
        self.message = None
        self.refresh_toggle_button()
 
    def refresh_toggle_button(self):
        """현재 칸의 상태에 맞게 가운데 버튼(켜기/끄기)의 모양을 갱신"""
        on = get_setting({"settings": self.settings}, SETTINGS_DEFS[self.page]["key"])
        self.toggle_btn.label = "끄기" if on else "켜기"
        self.toggle_btn.style = discord.ButtonStyle.danger if on else discord.ButtonStyle.success
 
    def current_embed(self) -> discord.Embed:
        return build_settings_embed(self.user, self.settings, self.page)
 
    async def _guard(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("본인의 설정 창만 조작할 수 있습니다.", ephemeral=True)
            return False
        return True
 
    async def _move(self, interaction: discord.Interaction, step: int):
        self.page = (self.page + step) % len(SETTINGS_DEFS)    # 끝에서 다시 처음/끝으로 이어짐
        self.refresh_toggle_button()
        await interaction.response.edit_message(embed=self.current_embed(), view=self)
 
    @discord.ui.button(label="이전", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction): return
        await self._move(interaction, -1)
 
    @discord.ui.button(label="끄기", style=discord.ButtonStyle.danger)
    async def toggle_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction): return
        await interaction.response.defer()
 
        key = SETTINGS_DEFS[self.page]["key"]
        try:
            # 다른 설정값을 덮어쓰지 않도록 DB의 최신 설정을 읽어서 해당 항목만 변경
            settings = dict(get_user_data(self.user.id).get("settings") or {})
            settings[key] = not get_setting({"settings": settings}, key)
            update_user_settings(self.user.id, settings)
        except Exception as e:
            print(f"DB Error (settings toggle): {e}")
            await interaction.followup.send("설정 저장 중 오류가 발생했습니다.", ephemeral=True)
            return
 
        self.settings = settings
        self.refresh_toggle_button()
        await interaction.edit_original_response(embed=self.current_embed(), view=self)
 
    @discord.ui.button(label="다음", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction): return
        await self._move(interaction, 1)
 
    async def on_timeout(self):
        # 시간이 지나면 죽은 버튼이 남지 않도록 비활성화
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

# === [조직원 전용] `/송금` 명령어 ===
def transfer_money_sync(sender_id: int, receiver_id: int, amount: int):
    """송금 처리 (동기 DB 작업이라 to_thread 로 실행). 반환: (성공 여부, 송금자 잔액, 수신자 잔액)
    잔액을 다시 확인한 뒤 '차감 → 적립' 순서로 저장하고, 적립이 실패하면 차감을 되돌립니다."""
    from .crafting import _inv_lock   # 순환 import 방지: 호출 시점에 가져옴
    _require_db()
    with _inv_lock:
        s_money = int(get_user_data(sender_id).get("money") or 0)
        r_money = int(get_user_data(receiver_id).get("money") or 0)
        if s_money < amount:
            return False, s_money, r_money

        res = supabase.table("user_data").update({"money": s_money - amount}).eq("user_id", str(sender_id)).execute()
        _check_updated(res, "transfer (sender)")
        try:
            res = supabase.table("user_data").update({"money": r_money + amount}).eq("user_id", str(receiver_id)).execute()
            _check_updated(res, "transfer (receiver)")
        except Exception:
            try:   # 적립에 실패했다면 송금자의 자금을 원래대로 되돌린다
                supabase.table("user_data").update({"money": s_money}).eq("user_id", str(sender_id)).execute()
            except Exception as rollback_err:
                print(f"DB Error (transfer rollback): {rollback_err}")
            raise
        return True, s_money - amount, r_money + amount


@bot.tree.command(name="송금", description="[조직원 전용] 다른 조직원에게 보유 자금을 양도합니다.")
@app_commands.describe(member="돈을 받을 조직원", amount="보낼 금액 ($)")
async def transfer_money(interaction: discord.Interaction, member: discord.Member, amount: int):
    if not await check_maintenance(interaction): return
    if not await check_membership(interaction): return
    if await check_jail(interaction): return

    sender = interaction.user
    receiver = member

    # 1. 대상·금액 검사 (아직 응답 전이므로 실패 안내는 본인에게만 보이는 메시지로 보낸다)
    if sender.id == receiver.id:
        await interaction.response.send_message("자기 자신에게는 돈을 송금할 수 없습니다.", ephemeral=True)
        return
    if receiver.bot:
        await interaction.response.send_message("봇에게는 송금할 수 없습니다.", ephemeral=True)
        return
    if amount <= 0:
        await interaction.response.send_message("송금 금액은 1원 이상이어야 합니다.", ephemeral=True)
        return

    mafia_role = discord.utils.get(interaction.guild.roles, name="조직원")
    police_role = discord.utils.get(interaction.guild.roles, name="경찰")
    receiver_ok = (mafia_role and mafia_role in receiver.roles) or (police_role and police_role in receiver.roles)
    if not receiver_ok:
        await interaction.response.send_message("받는 사람은 **조직원 또는 경찰**이어야 합니다. (가입하지 않은 사람에게는 송금할 수 없습니다)", ephemeral=True)
        return

    # 2. 송금 제한 (서버 관리자는 면제). 응답 전에 확인해야 실패 안내가 본인에게만 보인다.
    is_admin = bool(sender.guild_permissions.administrator)
    if not is_admin:
        if not await check_transfer_member_eligibility(interaction, sender, receiver): return
    try:
        pre = await asyncio.to_thread(transfer_money_limited_sync, sender.id, receiver.id, amount, is_admin, False)
    except Exception as e:
        await report_error("/송금 (사전 확인)", e, interaction)
        await interaction.response.send_message("데이터베이스 오류로 송금에 실패했습니다. 나중에 다시 시도해 주세요.", ephemeral=True)
        return
    if not pre["ok"]:
        await interaction.response.send_message(pre["reason"], ephemeral=True)
        return

    await interaction.response.defer()

    # 3. 실제 이체 (제한 재확인 → 차감 → 적립, 실패 시 되돌림)
    try:
        result = await asyncio.to_thread(transfer_money_limited_sync, sender.id, receiver.id, amount, is_admin, True)
    except Exception as e:
        await report_error("/송금 (이체)", e, interaction)
        await interaction.followup.send("데이터베이스 오류로 송금에 실패했습니다. 나중에 다시 시도해 주세요.", ephemeral=True)
        return

    if not result["ok"]:   # 확인과 이체 사이에 상태가 바뀐 경우
        await interaction.followup.send(result["reason"], ephemeral=True)
        return
    new_sender_money, new_receiver_money = result["sender_money"], result["receiver_money"]

    if amount >= TRANSFER_ALERT_AMOUNT:   # 큰 금액은 관리자 기록 채널에 알림
        await admin_audit(interaction, "고액 송금", receiver, f"${amount:,} (보낸 사람 {sender.display_name})")

    # 4. 송금 완료 임베드 (서버 전체 공개)
    embed = discord.Embed(
        title="[조직 자금 이체 완료]",
        description=(
            f"**{sender.mention}** 님이 **{receiver.mention}** 님에게 자금을 안전하게 전달했습니다!\n\n"
            f"**송금 금액:** `${amount:,}`\n\n"
            f"**{sender.display_name} 님의 남은 자금:** `${new_sender_money:,}`\n"
            f"**{receiver.display_name} 님의 보유 자금:** `${new_receiver_money:,}`"
        ),
        color=discord.Color.green()
    )
    if sender.avatar:
        embed.set_thumbnail(url=sender.avatar.url)
    if not is_admin:
        embed.set_footer(text=f"오늘 보낸 금액 ${result['sent_today']:,} / 하루 한도 ${TRANSFER_DAILY_SEND_LIMIT:,}")

    await interaction.followup.send(embed=embed)
