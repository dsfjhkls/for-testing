"""관리자 도구(/관리), 점검 모드, 공통 체크 함수"""

import asyncio
import discord
import sys
import time
import traceback as _traceback
from discord import app_commands
from datetime import datetime

from .core import (
    keep_task,
    _env_int,
    active_boss_guilds,
    bot,
    jail_list,
    report_error,
    send_log_embed,
    set_active_boss,
    supabase,
    wanted_list,
)
from .status import (
    BLACK_MARKET_ITEMS,
    WEAPON_SHOP_ITEMS,
    _check_updated,
    _require_db,
    get_user_data,
    get_user_rank,
    save_status,
    update_user_location,
)
from .alignment import get_title_blob, save_title_blob
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
TRANSFER_MAX_PER_TX = 20000              # 1회 최대 송금액
TRANSFER_DAILY_SEND_LIMIT = 30000        # 보내는 사람의 24시간 누적 송금 한도
TRANSFER_DAILY_RECEIVE_LIMIT = 50000     # 받는 사람의 24시간 누적 수령 한도 (한 계정으로 몰아주기 방지)
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
    from .crafting import CRAFT_BY_ITEM, MATERIAL_SOURCES   # 순환 import 방지: 호출 시점에 가져옴
    from .profile import POLICE_SHOP_ITEMS   # 순환 import 방지: 호출 시점에 가져옴
    from .battle_data import BOSS_KINDS, NAMED_MONSTERS, NPC_DATA   # 순환 import 방지: 호출 시점에 가져옴
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
    from .crafting import _inv_lock   # 순환 import 방지: 호출 시점에 가져옴
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
    from .crafting import _inv_lock   # 순환 import 방지: 호출 시점에 가져옴
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
    from .crafting import _inv_lock   # 순환 import 방지: 호출 시점에 가져옴
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
    from .transfer import _transfer_ledger   # 순환 import 방지: 호출 시점에 가져옴
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
    from .battle_data import BOSS_KINDS   # 순환 import 방지: 호출 시점에 가져옴
    cur = (current or "").lower()
    options = [("보스 없음 (제거)", "none")] + [(k["data"]["name"], key) for key, k in BOSS_KINDS.items()]
    return [app_commands.Choice(name=n[:100], value=v) for n, v in options if cur in n.lower() or cur in v][:25]

@admin_group.command(name="보스", description="이 서버의 보스를 직접 출현시키거나 없앱니다. (테스트·정리용)")
@app_commands.describe(kind="출현시킬 보스 (목록에서 선택 / 보스 없음 = 현재 보스 제거)")
@app_commands.autocomplete(kind=_admin_boss_autocomplete)
async def admin_boss(interaction: discord.Interaction, kind: str):
    from .battle_data import BOSS_KINDS   # 순환 import 방지: 호출 시점에 가져옴
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
