"""관리자 도구, 점검 모드, 권한 검사, 설정·송금

합친 모듈: admin.py, transfer.py
"""

import asyncio
import copy
import discord
import sys
import time
import traceback as _traceback
from datetime import datetime
from discord import app_commands

from .core import (
    _env_int,
    active_boss_guilds,
    bot,
    jail_list,
    keep_task,
    report_error,
    send_log_embed,
    set_active_boss,
    supabase,
    wanted_list,
)
from .status import (
    _check_updated,
    _require_db,
    BLACK_MARKET_ITEMS,
    check_jail,
    get_title_blob,
    get_user_data,
    get_user_rank,
    save_status,
    save_title_blob,
    update_user_location,
    update_user_settings,
    WEAPON_SHOP_ITEMS,
)


# ==========================================================================
# [합친 모듈] admin.py
#   관리자 도구(/관리), 점검 모드, 공통 체크 함수
# ==========================================================================
from . import core as _core

# ==========================================
# [점검 모드 설정 스위치]
# ==========================================
MAINTENANCE_MODE = False 

# === [공통 체크 함수] 점검 모드 확인 ===
async def check_maintenance(interaction: discord.Interaction) -> bool:
    if MAINTENANCE_MODE:
        if not interaction.user.guild_permissions.administrator:
            embed = discord.Embed(
                title="[시스템 점검 및 업데이트 안내]",
                description="현재 패밀리 GM 봇을 업데이트 중입니다. 잠시만 기다려 주세요!",
                color=discord.Color.orange()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return False
    return True

# === [공통 체크 함수] 마피아 또는 경찰 가입 여부 확인 ===
async def check_membership(interaction: discord.Interaction) -> bool:
    user = interaction.user
    mafia_role = discord.utils.get(interaction.guild.roles, name="조직원")
    police_role = discord.utils.get(interaction.guild.roles, name="경찰")
    
    is_mafia = mafia_role and mafia_role in user.roles
    is_police = police_role and police_role in user.roles

    if not is_mafia and not is_police:
        await interaction.response.send_message(
            "패밀리(마피아) 또는 치안국(경찰) 소속이 아닙니다! `/가입`을 먼저 진행해 주세요.",
            ephemeral=True
        )
        return False
    return True
ADMIN_LOG_CHANNEL_ID = _env_int("ADMIN_LOG_CHANNEL_ID")   # None 이면 오류 로그 채널을 같이 사용
ERROR_DEDUPE_SEC = 60        # 같은 오류는 이 시간 안에 다시 보내지 않음
ERROR_MAX_PER_MIN = 10       # 1분에 보낼 수 있는 오류 보고 상한

# --- 송금 제한 설정 ---
TRANSFER_MAX_PER_TX = 500000             # 1회 최대 송금액
TRANSFER_DAILY_SEND_LIMIT = 500000      # 보내는 사람의 24시간 누적 송금 한도
TRANSFER_DAILY_RECEIVE_LIMIT = 500000  # 받는 사람의 24시간 누적 수령 한도 (한 계정으로 몰아주기 방지)
TRANSFER_COOLDOWN_SEC = 30               # 연속 송금 대기 시간
TRANSFER_MIN_ACCOUNT_AGE_DAYS = 7        # 디스코드 계정을 만든 지 이만큼 지나야 송금 가능 (보내는 사람 / 받는 사람 모두)
TRANSFER_MIN_SERVER_STAY_HOURS = 24      # 서버에 들어온 지 이만큼 지나야 송금 가능 (보내는 사람 / 받는 사람 모두)
TRANSFER_ALERT_AMOUNT = 10000            # 이 금액 이상 송금은 관리자 기록 채널에 알림
TRANSFER_WINDOW_SEC = 24 * 60 * 60
_err_recent = {}       # {오류 서명: 마지막 보고 시각}
_err_sent_times = []   # 최근 보고 시각들 (분당 상한 계산용)

def _unwrap_error(error):
    return getattr(error, "original", None) or error

def _tb_text(error) -> str:
    if isinstance(error, BaseException):
        return "".join(_traceback.format_exception(type(error), error, error.__traceback__))
    return str(error)

def report_error_sync(context: str, error):
    """스레드(to_thread 안의 DB 작업 등)에서도 부를 수 있는 오류 보고"""
    loop = _core._main_loop
    if loop is not None and loop.is_running():
        try:
            asyncio.run_coroutine_threadsafe(report_error(context, error), loop)
            return
        except Exception:
            pass
    print(f"[오류] {context}: {error}")

async def _notify_user_error(interaction: discord.Interaction):
    """오류가 났을 때 유저에게 본인만 보이는 안내를 보내서, 응답 없이 멈춘 것처럼 보이지 않게 함"""
    msg = "처리 중 오류가 발생했습니다. 관리자에게 보고되었습니다. 잠시 후 다시 시도해 주세요."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception:
        pass

# 화면(View) / 입력창(Modal) 안에서 난 오류도 같은 곳으로 보고 (각 클래스가 따로 on_error 를 만들었다면 그쪽이 우선)
async def _view_on_error(self, interaction: discord.Interaction, error: Exception, item) -> None:
    await report_error(f"버튼/메뉴: {type(self).__name__}", error, interaction)
    await _notify_user_error(interaction)

async def _modal_on_error(self, interaction: discord.Interaction, error: Exception) -> None:
    await report_error(f"입력창: {type(self).__name__}", error, interaction)
    await _notify_user_error(interaction)

discord.ui.View.on_error = _view_on_error
discord.ui.Modal.on_error = _modal_on_error

@bot.event
async def on_error(event_method: str, *args, **kwargs):
    await report_error(f"이벤트: {event_method}", sys.exc_info()[1] or "알 수 없는 오류")

def _loop_exception_handler(loop, context):
    loop.default_exception_handler(context)
    exc = context.get("exception")
    if isinstance(exc, Exception):
        keep_task(loop.create_task(report_error("백그라운드 작업", exc, print_tb=False)))

# ==========================================
# [관리자 도구] /관리 ...
# ==========================================
def _admin_log_channel():
    return ADMIN_LOG_CHANNEL_ID or _core.ERROR_LOG_CHANNEL_ID

async def admin_audit(interaction: discord.Interaction, action: str, target=None, detail: str = ""):
    """관리자 작업을 콘솔과 기록 채널에 남김"""
    who = f"{interaction.user} ({interaction.user.id})"
    tgt = f"{target} ({target.id})" if target is not None else "-"
    print(f"[관리자 작업] {who} / {action} / 대상 {tgt} / {detail}")
    embed = discord.Embed(title=f"[관리자 작업] {action}", color=discord.Color.orange(), timestamp=discord.utils.utcnow())
    embed.add_field(name="실행자", value=f"{interaction.user.mention} ({interaction.user.id})", inline=True)
    if target is not None:
        embed.add_field(name="대상", value=f"{target.mention} ({target.id})", inline=True)
    if detail:
        embed.add_field(name="내용", value=detail[:1000], inline=False)
    await send_log_embed(_admin_log_channel(), embed)

async def admin_only(interaction: discord.Interaction) -> bool:
    if not interaction.guild or not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("서버 관리자 권한이 필요합니다.", ephemeral=True)
        return False
    return True

def _known_item_names() -> set:
    from .economy import CRAFT_BY_ITEM, MATERIAL_SOURCES  # 순환 import 방지: 호출 시점에 가져옴
    from .economy import POLICE_SHOP_ITEMS  # 순환 import 방지: 호출 시점에 가져옴
    from .battle import BOSS_KINDS, NAMED_MONSTERS, NPC_DATA  # 순환 import 방지: 호출 시점에 가져옴
    names = set()
    for items in BLACK_MARKET_ITEMS.values():
        names.update(it["name"] for it in items)
    names.update(it["name"] for it in WEAPON_SHOP_ITEMS)
    names.update(it["name"] for it in POLICE_SHOP_ITEMS)
    names.update(CRAFT_BY_ITEM.keys())
    names.update(MATERIAL_SOURCES.keys())
    for table in (NPC_DATA, NAMED_MONSTERS):
        for d in table.values():
            if d.get("drop"):
                names.add(d["drop"])
    for k in BOSS_KINDS.values():
        if k["data"].get("drop"):
            names.add(k["data"]["drop"])
    return names

def admin_adjust_sync(user_id: int, money_delta: int = 0, honor_delta: int = 0):
    """자금/명예 증감 (0 아래로는 내려가지 않음). 반환: (이전 자금, 새 자금, 이전 명예, 새 명예)"""
    from .economy import _inv_lock  # 순환 import 방지: 호출 시점에 가져옴
    _require_db()
    with _inv_lock:
        info = get_user_data(user_id)
        old_money, old_honor = int(info.get("money") or 0), int(info.get("honor") or 0)
        new_money, new_honor = max(0, old_money + money_delta), max(0, old_honor + honor_delta)
        res = supabase.table("user_data").update({"money": new_money, "honor": new_honor}).eq("user_id", str(user_id)).execute()
        _check_updated(res, "admin_adjust")
        return old_money, new_money, old_honor, new_honor

def admin_item_sync(user_id: int, name: str, delta: int, force: bool):
    """아이템 지급(+) / 회수(-). 반환: (이전 수량, 새 수량) 또는 알 수 없는 이름이면 None"""
    from .economy import _inv_lock  # 순환 import 방지: 호출 시점에 가져옴
    _require_db()
    with _inv_lock:
        info = get_user_data(user_id)
        inv = dict(info.get("inventory") or {})
        if not force and name not in inv and name not in _known_item_names():
            return None
        old = int(inv.get(name, 0) or 0)
        new = old + delta
        if new <= 0:
            inv.pop(name, None)
            new = 0
        else:
            inv[name] = new
        res = supabase.table("user_data").update({"inventory": inv}).eq("user_id", str(user_id)).execute()
        _check_updated(res, "admin_item")
        return old, new

def admin_reset_transfers_sync(user_id: int):
    from .economy import _inv_lock  # 순환 import 방지: 호출 시점에 가져옴
    _require_db()
    with _inv_lock:
        blob = get_title_blob(get_user_data(user_id))
        blob["transfers"] = {"sent": [], "received": []}
        save_title_blob(user_id, blob)

admin_group = app_commands.Group(
    name="관리", description="[서버 관리자 전용] 운영 도구",
    default_permissions=discord.Permissions(administrator=True), guild_only=True
)
bot.tree.add_command(admin_group)

@admin_group.command(name="조회", description="유저의 자금, 명예, 상태, 인벤토리를 확인합니다.")
@app_commands.describe(member="조회할 유저")
async def admin_lookup(interaction: discord.Interaction, member: discord.Member):
    if not await admin_only(interaction): return
    await interaction.response.defer(ephemeral=True)
    info = await asyncio.to_thread(get_user_data, member.id)
    blob = get_title_blob(info)
    now_dt, now_ts = datetime.now(), time.time()

    roles = [r for r in ("조직원", "경찰", "시민") if discord.utils.get(member.roles, name=r)]
    status = []
    if jail_list.get(member.id) and jail_list[member.id] > now_dt:
        status.append(f"수감 중 ({int((jail_list[member.id] - now_dt).total_seconds())}초 남음)")
    if wanted_list.get(member.id) and wanted_list[member.id] > now_dt:
        status.append(f"수배 중 ({int((wanted_list[member.id] - now_dt).total_seconds())}초 남음)")

    inv = info.get("inventory") or {}
    inv_text = "\n".join(f"- {k} x{v}" for k, v in list(inv.items())[:20]) or "없음"
    if len(inv) > 20:
        inv_text += f"\n... 외 {len(inv) - 20}종"
    sent = sum(a for t, a in _transfer_ledger(blob, "sent", now_ts))
    received = sum(a for t, a in _transfer_ledger(blob, "received", now_ts))

    embed = discord.Embed(title=f"[유저 조회] {member.display_name}", color=discord.Color.blurple())
    embed.add_field(name="기본 정보", value=(f"역할: {', '.join(roles) or '없음'}\n직급: {get_user_rank(int(info.get('honor') or 0), '경찰' in roles)}\n"
                                              f"위치: {info.get('location', '-')}\n상태: {', '.join(status) or '정상'}"), inline=True)
    embed.add_field(name="재산", value=f"자금: ${int(info.get('money') or 0):,}\n명예: {int(info.get('honor') or 0):,} pt", inline=True)
    embed.add_field(name="최근 24시간 송금", value=f"보낸 금액: ${sent:,}\n받은 금액: ${received:,}", inline=False)
    embed.add_field(name="인벤토리", value=inv_text[:1000], inline=False)
    await interaction.followup.send(embed=embed, ephemeral=True)

@admin_group.command(name="자금", description="유저의 자금을 늘리거나 줄입니다. (음수 입력 시 차감)")
@app_commands.describe(member="대상 유저", amount="증감할 금액 (예: 5000, -3000)", reason="사유 (기록 채널에 남음)")
async def admin_money(interaction: discord.Interaction, member: discord.Member, amount: int, reason: str = "사유 없음"):
    if not await admin_only(interaction): return
    if amount == 0 or abs(amount) > 1_000_000_000:
        await interaction.response.send_message("0이 아닌 10억 이하의 금액을 입력해 주세요.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        old_m, new_m, _, _ = await asyncio.to_thread(admin_adjust_sync, member.id, amount, 0)
    except Exception as e:
        await report_error("/관리 자금", e, interaction)
        await interaction.followup.send("DB 오류로 처리하지 못했습니다.", ephemeral=True)
        return
    await admin_audit(interaction, "자금 조정", member, f"${old_m:,} -> ${new_m:,} ({amount:+,}) / 사유: {reason}")
    await interaction.followup.send(f"{member.mention} 님의 자금: `${old_m:,}` -> `${new_m:,}`", ephemeral=True)

@admin_group.command(name="자금로그", description="최근 자금 변동 기록을 봅니다. (유저를 고르면 그 유저만 / 봇을 재시작하면 초기화)")
@app_commands.describe(member="이 유저의 기록만 보기 (비우면 전체)", count="보여줄 개수 (기본 15, 최대 30)")
async def admin_money_log(interaction: discord.Interaction, member: discord.Member = None, count: app_commands.Range[int, 1, 30] = 15):
    if not await admin_only(interaction): return
    from . import core as ledger  # 자금 변동 기록 (ledger.py)
    rows = ledger.recent(member.id if member else None, count)
    if not rows:
        await interaction.response.send_message("아직 기록이 없습니다. (봇을 켠 뒤 일어난 변동만 기록됩니다)", ephemeral=True)
        return
    lines = []
    for r in rows:
        sign = "+" if r["delta"] >= 0 else "-"
        lines.append(f"<t:{int(r['ts'])}:T> <@{r['uid']}> **{sign}${abs(r['delta']):,}** → ${r['new']:,} · `{r['cmd']}`")
    embed = discord.Embed(title="[자금 로그] 최근 변동" + (f" — {member.display_name}" if member else ""),
                          description="\n".join(lines)[:4000], color=discord.Color.blurple())
    embed.set_footer(text=f"고액 알림 기준: ${ledger.get_threshold():,} 이상")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@admin_group.command(name="자금로그기준", description="자금 로그 채널로 알릴 고액 변동 기준을 바꿉니다. (봇을 재시작하면 환경변수 값으로 복귀)")
@app_commands.describe(amount="이 금액 이상의 한 번 변동을 알림 (예: 500000)")
async def admin_money_log_threshold(interaction: discord.Interaction, amount: app_commands.Range[int, 1, 1_000_000_000]):
    if not await admin_only(interaction): return
    from . import core as ledger  # 자금 변동 기록 (ledger.py)
    old = ledger.get_threshold()
    ledger.set_threshold(amount)
    await admin_audit(interaction, "자금 로그 기준 변경", None, f"${old:,} -> ${amount:,}")
    await interaction.response.send_message(f"고액 변동 알림 기준: `${old:,}` -> `${amount:,}`", ephemeral=True)

@admin_group.command(name="명예", description="유저의 명예를 늘리거나 줄입니다. (음수 입력 시 차감)")
@app_commands.describe(member="대상 유저", amount="증감할 명예 (예: 100, -50)", reason="사유 (기록 채널에 남음)")
async def admin_honor(interaction: discord.Interaction, member: discord.Member, amount: int, reason: str = "사유 없음"):
    if not await admin_only(interaction): return
    if amount == 0 or abs(amount) > 10_000_000:
        await interaction.response.send_message("0이 아닌 1,000만 이하의 값을 입력해 주세요.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        _, _, old_h, new_h = await asyncio.to_thread(admin_adjust_sync, member.id, 0, amount)
    except Exception as e:
        await report_error("/관리 명예", e, interaction)
        await interaction.followup.send("DB 오류로 처리하지 못했습니다.", ephemeral=True)
        return
    await admin_audit(interaction, "명예 조정", member, f"{old_h:,} -> {new_h:,} pt ({amount:+,}) / 사유: {reason}")
    await interaction.followup.send(f"{member.mention} 님의 명예: `{old_h:,} pt` -> `{new_h:,} pt`", ephemeral=True)

async def _item_autocomplete(interaction: discord.Interaction, current: str):
    cur = (current or "").lower()
    names = [n for n in sorted(_known_item_names()) if cur in n.lower()]
    return [app_commands.Choice(name=n[:100], value=n[:100]) for n in names[:25]]

@admin_group.command(name="아이템", description="유저에게 아이템을 지급하거나 회수합니다. (수량이 음수면 회수)")
@app_commands.describe(member="대상 유저", item="아이템 이름 (자동완성에서 선택)", amount="수량 (기본 1, 음수면 회수)",
                       force="목록에 없는 이름도 강제로 지급 (오타 주의)")
@app_commands.autocomplete(item=_item_autocomplete)
async def admin_item(interaction: discord.Interaction, member: discord.Member, item: str, amount: int = 1, force: bool = False):
    if not await admin_only(interaction): return
    if amount == 0 or abs(amount) > 10000:
        await interaction.response.send_message("0이 아닌 1만 이하의 수량을 입력해 주세요.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        result = await asyncio.to_thread(admin_item_sync, member.id, item, amount, force)
    except Exception as e:
        await report_error("/관리 아이템", e, interaction)
        await interaction.followup.send("DB 오류로 처리하지 못했습니다.", ephemeral=True)
        return
    if result is None:
        await interaction.followup.send(
            "게임에 없는 아이템 이름입니다. 자동완성 목록에서 선택해 주세요. (이 이름이 확실하다면 `force` 옵션을 켜세요)", ephemeral=True)
        return
    old, new = result
    await admin_audit(interaction, "아이템 조정", member, f"{item}: {old} -> {new} ({amount:+d})")
    await interaction.followup.send(f"{member.mention} 님의 `{item}`: {old}개 -> {new}개", ephemeral=True)

async def _admin_boss_autocomplete(interaction: discord.Interaction, current: str):
    from .battle import BOSS_KINDS  # 순환 import 방지: 호출 시점에 가져옴
    cur = (current or "").lower()
    options = [("보스 없음 (제거)", "none")] + [(k["data"]["name"], key) for key, k in BOSS_KINDS.items()]
    return [app_commands.Choice(name=n[:100], value=v) for n, v in options if cur in n.lower() or cur in v][:25]

@admin_group.command(name="보스", description="이 서버의 보스를 직접 출현시키거나 없앱니다. (테스트·정리용)")
@app_commands.describe(kind="출현시킬 보스 (목록에서 선택 / 보스 없음 = 현재 보스 제거)")
@app_commands.autocomplete(kind=_admin_boss_autocomplete)
async def admin_boss(interaction: discord.Interaction, kind: str):
    from .battle import BOSS_KINDS  # 순환 import 방지: 호출 시점에 가져옴
    if not await admin_only(interaction): return
    if kind != "none" and kind not in BOSS_KINDS:
        await interaction.response.send_message("목록에 없는 보스입니다. 자동완성 목록에서 선택해 주세요.", ephemeral=True)
        return
    gid = interaction.guild_id
    cur = active_boss_guilds.get(gid)
    new = None if kind == "none" else kind
    set_active_boss(gid, new)
    await admin_audit(interaction, "보스 조작", None, f"{cur or '없음'} -> {new or '없음'}")
    if new:
        await interaction.response.send_message(f"**{BOSS_KINDS[new]['data']['name']}** 을(를) 출현시켰습니다. (`/보스` 로 도전)", ephemeral=True)
    else:
        await interaction.response.send_message("이 서버의 보스를 제거했습니다.", ephemeral=True)

@admin_group.command(name="점검", description="점검 모드를 켜거나 끕니다. (봇을 재시작하면 코드의 기본값으로 돌아갑니다)")
@app_commands.describe(mode="점검 모드")
@app_commands.choices(mode=[app_commands.Choice(name="켜기", value="on"), app_commands.Choice(name="끄기", value="off")])
async def admin_maintenance(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    global MAINTENANCE_MODE
    if not await admin_only(interaction): return
    MAINTENANCE_MODE = (mode.value == "on")
    await admin_audit(interaction, "점검 모드 변경", None, "켜짐" if MAINTENANCE_MODE else "꺼짐")
    await interaction.response.send_message(
        f"점검 모드를 **{'켰습니다. 관리자 외에는 명령어를 쓸 수 없습니다' if MAINTENANCE_MODE else '껐습니다'}**.", ephemeral=True)

@admin_group.command(name="석방", description="유저의 수감 / 수배 상태를 해제합니다.")
@app_commands.describe(member="대상 유저")
async def admin_release(interaction: discord.Interaction, member: discord.Member):
    if not await admin_only(interaction): return
    was_jailed = member.id in jail_list
    was_wanted = member.id in wanted_list
    jail_list.pop(member.id, None)
    wanted_list.pop(member.id, None)
    save_status(member.id)
    if was_jailed:
        try:
            await asyncio.to_thread(update_user_location, member.id, "레벤하임")
        except Exception as e:
            await report_error("/관리 석방 (위치 복구)", e, interaction)
    await admin_audit(interaction, "수감/수배 해제", member, f"수감 {'해제' if was_jailed else '없음'} / 수배 {'해제' if was_wanted else '없음'}")
    await interaction.response.send_message(
        f"{member.mention} 님: 수감 {'해제' if was_jailed else '없음'} / 수배 {'해제' if was_wanted else '없음'}", ephemeral=True)

@admin_group.command(name="송금초기화", description="유저의 24시간 송금 기록(한도 계산용)을 지웁니다.")
@app_commands.describe(member="대상 유저")
async def admin_reset_transfers(interaction: discord.Interaction, member: discord.Member):
    if not await admin_only(interaction): return
    await interaction.response.defer(ephemeral=True)
    try:
        await asyncio.to_thread(admin_reset_transfers_sync, member.id)
    except Exception as e:
        await report_error("/관리 송금초기화", e, interaction)
        await interaction.followup.send("DB 오류로 처리하지 못했습니다.", ephemeral=True)
        return
    await admin_audit(interaction, "송금 기록 초기화", member)
    await interaction.followup.send(f"{member.mention} 님의 송금 기록을 초기화했습니다.", ephemeral=True)

@admin_group.command(name="로그채널", description="오류 보고 / 관리자 작업 기록을 받을 채널을 지정합니다. (영구 설정은 환경 변수 사용)")
@app_commands.describe(channel="로그를 받을 텍스트 채널")
async def admin_log_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    global ADMIN_LOG_CHANNEL_ID
    if not await admin_only(interaction): return
    await interaction.response.defer(ephemeral=True)
    test = discord.Embed(title="[로그 채널 설정]", description="이 채널로 오류 보고와 관리자 작업 기록이 전송됩니다.", color=discord.Color.green())
    if not await send_log_embed(channel.id, test):
        await interaction.followup.send("해당 채널에 메시지를 보낼 수 없습니다. 봇에게 채널 보기 / 메시지 보내기 / 링크 첨부 권한이 있는지 확인해 주세요.", ephemeral=True)
        return
    _core.ERROR_LOG_CHANNEL_ID = channel.id
    ADMIN_LOG_CHANNEL_ID = channel.id
    await admin_audit(interaction, "로그 채널 지정", None, f"{channel.mention}")
    await interaction.followup.send(
        f"로그 채널을 {channel.mention} 으로 지정했습니다.\n"
        f"봇을 재시작해도 유지하려면 환경 변수 `ERROR_LOG_CHANNEL_ID={channel.id}` 를 설정해 주세요.", ephemeral=True)

@admin_group.command(name="오류테스트", description="오류 로그 채널 연결이 잘 되었는지 일부러 오류를 발생시켜 확인합니다.")
async def admin_error_test(interaction: discord.Interaction):
    if not await admin_only(interaction): return
    raise RuntimeError("오류 로그 테스트입니다. 이 메시지가 로그 채널에 보이면 정상입니다.")


# ==========================================================================
# [합친 모듈] transfer.py
#   송금 제한 및 /송금
# ==========================================================================
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
    from .economy import _inv_lock  # 순환 import 방지: 호출 시점에 가져옴
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
    from .economy import _inv_lock  # 순환 import 방지: 호출 시점에 가져옴
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
