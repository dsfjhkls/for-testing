"""경찰 수사팀 (/수사팀 ...)"""

import asyncio
import discord
import random
from discord import app_commands
from datetime import datetime, timedelta

from .core import bot, inc_money, supabase, keep_task
from .status import _check_updated, check_jail, compute_combat_stats, get_user_data, update_user_stats
from .achievements import police_ach_bump
from .admin import check_maintenance, check_membership
from .family import FAMILY_NAME_PATTERN, _is_jailed

# ==========================================================================
# [경찰 수사팀 시스템]  /수사팀 ...
#   • 마피아의 '조직(패밀리)' 과 같은 구조의 경찰 전용 조직입니다. (경찰 역할만 만들고 가입할 수 있어요)
#   • 데이터는 Supabase 의 `police_units` 테이블에 저장됩니다. (police_units.sql 을 먼저 실행하세요)
#   • 수사팀의 협동 콘텐츠는 '합동 소탕 작전' (/수사팀 작전) 입니다. 진행 상태는 메모리에만 두며,
#     봇이 재시작되면 진행 중이던 작전은 사라집니다. (작전비는 모집 단계에서 이미 금고에서 빠진 상태)
# ==========================================================================
# --- 수사팀 설정 ---
POLICE_UNIT_CREATE_COST = 10000        # 수사팀 창설 비용 ($)
POLICE_UNIT_CREATE_MIN_HONOR = 500     # 수사팀 창설에 필요한 최소 명예
POLICE_UNIT_MAX_MEMBERS = 10           # 수사팀 최대 인원
POLICE_UNIT_MIN_DONATE = 100           # 금고 최소 기부액
POLICE_UNIT_DB_ERROR_MSG = "수사팀 DB 처리 중 오류가 발생했습니다. (관리자: `police_units` 테이블이 만들어져 있는지 확인해 주세요 — police_units.sql 참고)"

# --- 합동 소탕 작전 설정 ---
POLICE_OP_LOBBY_SEC = 60               # 참가 모집 시간 (팀장이 '출동' 을 누르면 바로 시작)
POLICE_OP_MAX_PLAYERS = 8
POLICE_OP_COOLDOWN_SEC = 1800          # 같은 수사팀의 작전 재시작 대기시간 (30분)
POLICE_OP_STAGE_SEC = 3                # 단계 사이 연출 시간
POLICE_OP_TREASURY_RETURN = 0.15       # 성공 시 지급된 보상금의 15%가 수사팀 금고로 들어감
POLICE_OP_FAIL_MONEY_RATIO = 0.10      # 작전이 완전히 실패해도 참가자에게 주는 위로금 비율
POLICE_OP_STAGES = ["잠입·정보 수집", "현장 급습", "검거·압송"]
POLICE_OP_REWARD_FRAC = {3: 1.0, 2: 0.7, 1: 0.4, 0: 0.0}   # 성공한 단계 수 → 보상 비율

# base: 단계 1개의 기본 성공률 / ref_atk: 참가자 평균 공격력이 이 값 이상이면 장비 보너스(+12%p) 만점
# 단계 성공률 = base + 인원 보너스(최소 인원 초과 1명당 +4%p) + 장비 보너스,  30%~92% 로 제한
POLICE_OP_TIERS = {
    "smuggle": {
        "name": "밀수 조직 소탕", "min_players": 2, "min_honor": 0, "cost": 1000,
        "base": 0.65, "ref_atk": 300,
        "money": inc_money(3000), "honor": 44, "points": 10,
        "desc": "뒷골목 밀수 조직의 창고를 급습합니다. 입문자용 작전입니다.",
    },
    "cartel": {
        "name": "항만 카르텔 일망타진", "min_players": 3, "min_honor": 1500, "cost": 3000,
        "base": 0.55, "ref_atk": 1200,
        "money": inc_money(7000), "honor": 90, "points": 25,
        "desc": "에르느코항을 장악한 카르텔을 한 번에 소탕합니다. 화력과 인원이 필요합니다.",
    },
    "empire": {
        "name": "지하 범죄 제국 본거지 급습", "min_players": 4, "min_honor": 4000, "cost": 8000,
        "base": 0.50, "ref_atk": 3500,
        "money": inc_money(14000), "honor": 150, "points": 60,
        "desc": "칼렌데 지하 범죄 제국의 심장부. 정상급 수사팀만이 도전할 수 있습니다.",
    },
}

police_ops = {}                # {unit_id: PoliceOpSession}
police_op_user_map = {}        # {user_id: unit_id}  (작전에 참가 중인 경찰)
police_op_cooldowns = {}       # {unit_id: datetime}


# ---------------------------------------------------------------- 수사팀 DB 도우미 (동기)
def police_unit_get_by_user(guild_id: int, user_id: int):
    res = (supabase.table("police_units").select("*")
           .eq("guild_id", str(guild_id)).contains("member_ids", [str(user_id)]).limit(1).execute())
    return res.data[0] if res.data else None

def police_unit_get_by_id(unit_id):
    res = supabase.table("police_units").select("*").eq("id", unit_id).limit(1).execute()
    return res.data[0] if res.data else None

def police_unit_get_by_name(guild_id: int, name: str):
    res = (supabase.table("police_units").select("*")
           .eq("guild_id", str(guild_id)).eq("name", name).limit(1).execute())
    return res.data[0] if res.data else None

def police_unit_list_top(guild_id: int, limit: int = 10):
    res = (supabase.table("police_units").select("*").eq("guild_id", str(guild_id))
           .order("op_points", desc=True).limit(limit).execute())
    return res.data or []

def police_unit_create(guild_id: int, name: str, owner_id: int):
    row = {
        "guild_id": str(guild_id), "name": name, "owner_id": str(owner_id),
        "member_ids": [str(owner_id)], "treasury": 0, "op_points": 0, "ops_cleared": 0,
    }
    res = supabase.table("police_units").insert(row).execute()
    return res.data[0] if res.data else row

def police_unit_update(unit_id, data: dict):
    res = supabase.table("police_units").update(data).eq("id", unit_id).execute()
    _check_updated(res, "police_unit_update")

def police_unit_add_treasury_atomic(unit_id, delta: int) -> int:
    """금고에 delta 를 더하고 새 금고 값을 돌려줍니다.
    읽은 값이 그대로일 때만 갱신하므로, 동시에 다른 팀원이 기부해도 서로의 금액을 덮어쓰지 않습니다. (바뀌었으면 다시 읽고 재시도)"""
    for _ in range(5):
        unit = police_unit_get_by_id(unit_id)
        if not unit:
            raise RuntimeError("수사팀을 찾을 수 없습니다")
        old = unit.get("treasury", 0)
        res = supabase.table("police_units").update({"treasury": old + delta}).eq("id", unit_id).eq("treasury", old).execute()
        if res.data:
            return old + delta
    raise RuntimeError("금고 갱신이 계속 겹쳐 실패했습니다")

def police_unit_delete(unit_id):
    supabase.table("police_units").delete().eq("id", unit_id).execute()

def police_unit_detach_user(user_id: int):
    """유저를 모든 수사팀에서 제거 (팀장이면 다음 팀원에게 위임, 마지막 1명이면 수사팀 해산). /탈퇴 초기화에서도 사용."""
    if not supabase:
        return
    uid = str(user_id)
    res = supabase.table("police_units").select("*").contains("member_ids", [uid]).execute()
    for unit in (res.data or []):
        members = [m for m in unit["member_ids"] if m != uid]
        if not members:
            police_unit_delete(unit["id"])
            continue
        data = {"member_ids": members}
        if unit["owner_id"] == uid:
            data["owner_id"] = members[0]
        police_unit_update(unit["id"], data)

def police_unit_add_treasury(unit_id, amount: int):
    unit = police_unit_get_by_id(unit_id)
    if unit and amount:
        police_unit_update(unit_id, {"treasury": unit.get("treasury", 0) + amount})

def police_unit_record_op(unit_id, treasury_add: int, points: int, cleared: bool):
    unit = police_unit_get_by_id(unit_id)
    if not unit:
        return
    police_unit_update(unit_id, {
        "treasury": unit.get("treasury", 0) + treasury_add,
        "op_points": unit.get("op_points", 0) + points,
        "ops_cleared": unit.get("ops_cleared", 0) + (1 if cleared else 0),
    })

def _is_police(member: discord.Member) -> bool:
    role = discord.utils.get(member.guild.roles, name="경찰")
    return bool(role and role in member.roles)


# ================================================================ /수사팀
unit_group = app_commands.Group(name="수사팀", description="[경찰] 경찰 전용 조직(수사팀)을 만들고 함께 활동합니다.")
bot.tree.add_command(unit_group)

async def _unit_precheck(interaction: discord.Interaction) -> bool:
    if not await check_maintenance(interaction): return False
    if not await check_membership(interaction): return False
    if await check_jail(interaction): return False
    if not _is_police(interaction.user):
        await interaction.response.send_message("수사팀 기능은 치안국 경찰만 사용할 수 있습니다. (마피아 조직원은 `/조직` 을 이용하세요)", ephemeral=True)
        return False
    return True

@unit_group.command(name="생성", description=f"수사팀을 창설합니다. (비용 ${POLICE_UNIT_CREATE_COST:,} / 명예 {POLICE_UNIT_CREATE_MIN_HONOR}pt 이상)")
@app_commands.describe(이름="수사팀 이름 (한글·영문·숫자·띄어쓰기 2~12자)")
async def unit_create_cmd(interaction: discord.Interaction, 이름: str):
    if not await _unit_precheck(interaction): return
    user = interaction.user
    name = 이름.strip()
    if not FAMILY_NAME_PATTERN.match(name):
        await interaction.response.send_message("수사팀 이름은 한글·영문·숫자·띄어쓰기로 **2~12자**여야 합니다.", ephemeral=True)
        return
    paid = False
    try:
        if await asyncio.to_thread(police_unit_get_by_user, interaction.guild_id, user.id):
            await interaction.response.send_message("이미 수사팀에 소속되어 있습니다. 먼저 `/수사팀 탈퇴` 하세요.", ephemeral=True)
            return
        if await asyncio.to_thread(police_unit_get_by_name, interaction.guild_id, name):
            await interaction.response.send_message("이미 같은 이름의 수사팀이 있습니다.", ephemeral=True)
            return
        info = await asyncio.to_thread(get_user_data, user.id)
        if info["honor"] < POLICE_UNIT_CREATE_MIN_HONOR:
            await interaction.response.send_message(f"수사팀을 만들려면 명예 **{POLICE_UNIT_CREATE_MIN_HONOR}pt** 이상이어야 합니다. (현재 {info['honor']:,}pt)", ephemeral=True)
            return
        if info["money"] < POLICE_UNIT_CREATE_COST:
            await interaction.response.send_message(f"창설 비용 **${POLICE_UNIT_CREATE_COST:,}** 이(가) 부족합니다. (보유 ${info['money']:,})", ephemeral=True)
            return
        await asyncio.to_thread(update_user_stats, user.id, -POLICE_UNIT_CREATE_COST, 0)
        paid = True
        await asyncio.to_thread(police_unit_create, interaction.guild_id, name, user.id)
    except Exception as e:
        print(f"Police unit create error: {e}")
        if paid:   # 돈만 빠지고 수사팀이 안 만들어진 경우 되돌려 준다
            try:
                await asyncio.to_thread(update_user_stats, user.id, POLICE_UNIT_CREATE_COST, 0)
            except Exception as e2:
                print(f"Police unit create refund error: {e2}")
        await interaction.response.send_message(POLICE_UNIT_DB_ERROR_MSG, ephemeral=True)
        return

    embed = discord.Embed(
        title="[수사팀 창설]",
        description=(f"**{user.mention}** 님이 새로운 수사팀 **「{name}」** 을(를) 창설했습니다!\n\n"
                     f"**창설 비용:** `${POLICE_UNIT_CREATE_COST:,}`\n"
                     f"`/수사팀 초대` 로 동료 경찰을 모아 `/수사팀 작전` 에 도전해 보세요."),
        color=discord.Color.blue()
    )
    await interaction.response.send_message(embed=embed)

def _unit_embed(unit: dict, guild: discord.Guild) -> discord.Embed:
    from .confirm_upgrade import unit_facility_summary, unit_max_members   # 순환 import 방지: 호출 시점에 가져옴
    members = unit.get("member_ids") or []
    lines = []
    for m in members:
        crown = "[팀장] " if m == unit["owner_id"] else "• "
        lines.append(f"{crown}<@{m}>")
    embed = discord.Embed(title=f"수사팀 「{unit['name']}」", color=discord.Color.blue())
    embed.add_field(name=f"팀원 ({len(members)}/{unit_max_members(unit)})", value="\n".join(lines) or "-", inline=False)
    embed.add_field(name="금고", value=f"`${unit.get('treasury', 0):,}`", inline=True)
    embed.add_field(name="작전 점수", value=f"`{unit.get('op_points', 0):,} pt`", inline=True)
    embed.add_field(name="소탕 성공", value=f"`{unit.get('ops_cleared', 0):,}회`", inline=True)
    _fac_line = unit_facility_summary(unit)
    if _fac_line:
        embed.add_field(name="시설", value=_fac_line, inline=False)
    op = police_ops.get(unit["id"])
    if op:
        embed.add_field(name="진행 중인 작전", value=f"{op.tier['name']} ({'모집 중' if op.state == 'lobby' else '작전 수행 중'})", inline=False)
    cd = police_op_cooldowns.get(unit["id"])
    if cd and datetime.now() < cd:
        embed.add_field(name="다음 작전", value=f"{int((cd - datetime.now()).total_seconds() // 60) + 1}분 후", inline=True)
    return embed

@unit_group.command(name="정보", description="수사팀 정보를 확인합니다. (이름을 비우면 내 수사팀)")
@app_commands.describe(이름="확인할 수사팀 이름 (비우면 내 수사팀)")
async def unit_info_cmd(interaction: discord.Interaction, 이름: str = None):
    if not await check_maintenance(interaction): return
    try:
        if 이름:
            unit = await asyncio.to_thread(police_unit_get_by_name, interaction.guild_id, 이름.strip())
        else:
            unit = await asyncio.to_thread(police_unit_get_by_user, interaction.guild_id, interaction.user.id)
    except Exception as e:
        print(f"Police unit info error: {e}")
        await interaction.response.send_message(POLICE_UNIT_DB_ERROR_MSG, ephemeral=True)
        return
    if not unit:
        msg = "해당 이름의 수사팀이 없습니다." if 이름 else "소속된 수사팀이 없습니다. `/수사팀 생성` 또는 초대를 받아 보세요."
        await interaction.response.send_message(msg, ephemeral=True)
        return
    await interaction.response.send_message(embed=_unit_embed(unit, interaction.guild))

@unit_group.command(name="목록", description="작전 점수 기준 수사팀 랭킹 TOP 10")
async def unit_rank_cmd(interaction: discord.Interaction):
    if not await check_maintenance(interaction): return
    try:
        rows = await asyncio.to_thread(police_unit_list_top, interaction.guild_id, 10)
    except Exception as e:
        print(f"Police unit list error: {e}")
        await interaction.response.send_message(POLICE_UNIT_DB_ERROR_MSG, ephemeral=True)
        return
    if not rows:
        await interaction.response.send_message("아직 창설된 수사팀이 없습니다.", ephemeral=True)
        return
    lines = [f"`{i + 1}위` **{r['name']}** — {r.get('op_points', 0):,} pt · {len(r.get('member_ids') or [])}명" for i, r in enumerate(rows)]
    embed = discord.Embed(title="수사팀 랭킹 (작전 점수)", description="\n".join(lines), color=discord.Color.blue())
    await interaction.response.send_message(embed=embed)

class PoliceUnitInviteView(discord.ui.View):
    def __init__(self, unit_id, unit_name: str, inviter: discord.Member, target: discord.Member):
        super().__init__(timeout=60)
        self.unit_id = unit_id
        self.unit_name = unit_name
        self.inviter = inviter
        self.target = target
        self.resolved = False
        self.message = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.target.id:
            await interaction.response.send_message("초대받은 본인만 응답할 수 있습니다.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        if self.resolved or self.message is None:
            return
        try:
            await self.message.edit(content="초대가 만료되었습니다.", embed=None, view=None)
        except discord.HTTPException:
            pass

    @discord.ui.button(label="수락", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        from .confirm_upgrade import unit_max_members   # 순환 import 방지: 호출 시점에 가져옴
        if self.resolved: return
        self.resolved = True
        self.stop()
        try:
            if await asyncio.to_thread(police_unit_get_by_user, interaction.guild_id, self.target.id):
                await interaction.response.edit_message(content="이미 다른 수사팀에 소속되어 있어 가입할 수 없습니다.", embed=None, view=None)
                return
            unit = await asyncio.to_thread(police_unit_get_by_id, self.unit_id)
            if not unit:
                await interaction.response.edit_message(content="수사팀이 더 이상 존재하지 않습니다.", embed=None, view=None)
                return
            members = list(unit.get("member_ids") or [])
            if len(members) >= unit_max_members(unit):
                await interaction.response.edit_message(content="수사팀 정원이 가득 찼습니다.", embed=None, view=None)
                return
            members.append(str(self.target.id))
            await asyncio.to_thread(police_unit_update, self.unit_id, {"member_ids": members})
        except Exception as e:
            print(f"Police unit invite accept error: {e}")
            await interaction.response.edit_message(content=POLICE_UNIT_DB_ERROR_MSG, embed=None, view=None)
            return
        embed = discord.Embed(title="[수사팀 합류]",
                              description=f"**{self.target.mention}** 님이 **「{self.unit_name}」** 에 합류했습니다! ({len(members)}/{unit_max_members(unit)})",
                              color=discord.Color.green())
        await interaction.response.edit_message(content=None, embed=embed, view=None)

    @discord.ui.button(label="거절", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.resolved: return
        self.resolved = True
        self.stop()
        await interaction.response.edit_message(content=f"**{self.target.display_name}** 님이 초대를 거절했습니다.", embed=None, view=None)

@unit_group.command(name="초대", description="경찰 동료를 우리 수사팀에 초대합니다. (팀장 전용)")
@app_commands.describe(member="초대할 경찰")
async def unit_invite_cmd(interaction: discord.Interaction, member: discord.Member):
    from .confirm_upgrade import unit_max_members   # 순환 import 방지: 호출 시점에 가져옴
    if not await _unit_precheck(interaction): return
    user = interaction.user
    if member.bot or member.id == user.id:
        await interaction.response.send_message("올바른 초대 대상이 아닙니다.", ephemeral=True)
        return
    if not _is_police(member):
        await interaction.response.send_message("치안국 경찰만 초대할 수 있습니다.", ephemeral=True)
        return
    try:
        unit = await asyncio.to_thread(police_unit_get_by_user, interaction.guild_id, user.id)
        if not unit:
            await interaction.response.send_message("소속된 수사팀이 없습니다.", ephemeral=True)
            return
        if unit["owner_id"] != str(user.id):
            await interaction.response.send_message("팀장만 초대할 수 있습니다.", ephemeral=True)
            return
        if len(unit.get("member_ids") or []) >= unit_max_members(unit):
            await interaction.response.send_message("수사팀 정원이 가득 찼습니다.", ephemeral=True)
            return
        if await asyncio.to_thread(police_unit_get_by_user, interaction.guild_id, member.id):
            await interaction.response.send_message("상대는 이미 다른 수사팀에 소속되어 있습니다.", ephemeral=True)
            return
    except Exception as e:
        print(f"Police unit invite error: {e}")
        await interaction.response.send_message(POLICE_UNIT_DB_ERROR_MSG, ephemeral=True)
        return

    view = PoliceUnitInviteView(unit["id"], unit["name"], user, member)
    embed = discord.Embed(title="[수사팀 초대장]",
                          description=f"**{user.mention}** 님이 **{member.mention}** 님을 수사팀 **「{unit['name']}」** 에 초대했습니다.\n60초 안에 응답해 주세요.",
                          color=discord.Color.blue())
    await interaction.response.send_message(content=member.mention, embed=embed, view=view)
    view.message = await interaction.original_response()

@unit_group.command(name="탈퇴", description="현재 수사팀에서 탈퇴합니다.")
async def unit_leave_cmd(interaction: discord.Interaction):
    if not await _unit_precheck(interaction): return
    user = interaction.user
    if user.id in police_op_user_map:
        await interaction.response.send_message("작전 진행 중에는 탈퇴할 수 없습니다.", ephemeral=True)
        return
    try:
        unit = await asyncio.to_thread(police_unit_get_by_user, interaction.guild_id, user.id)
        if not unit:
            await interaction.response.send_message("소속된 수사팀이 없습니다.", ephemeral=True)
            return
        members = unit.get("member_ids") or []
        if unit["owner_id"] == str(user.id) and len(members) > 1:
            await interaction.response.send_message("팀장은 탈퇴할 수 없습니다. `/수사팀 위임` 으로 팀장을 넘기거나 `/수사팀 해산` 하세요.", ephemeral=True)
            return
        await asyncio.to_thread(police_unit_detach_user, user.id)
    except Exception as e:
        print(f"Police unit leave error: {e}")
        await interaction.response.send_message(POLICE_UNIT_DB_ERROR_MSG, ephemeral=True)
        return
    note = " (마지막 팀원이라 수사팀이 해산되었습니다)" if len(members) <= 1 else ""
    await interaction.response.send_message(f"**{user.mention}** 님이 **「{unit['name']}」** 에서 탈퇴했습니다.{note}")

@unit_group.command(name="추방", description="팀원을 추방합니다. (팀장 전용)")
@app_commands.describe(member="추방할 팀원")
async def unit_kick_cmd(interaction: discord.Interaction, member: discord.Member):
    if not await _unit_precheck(interaction): return
    user = interaction.user
    try:
        unit = await asyncio.to_thread(police_unit_get_by_user, interaction.guild_id, user.id)
        if not unit or unit["owner_id"] != str(user.id):
            await interaction.response.send_message("팀장만 사용할 수 있습니다.", ephemeral=True)
            return
        if member.id == user.id or str(member.id) not in (unit.get("member_ids") or []):
            await interaction.response.send_message("추방할 수 없는 대상입니다.", ephemeral=True)
            return
        if member.id in police_op_user_map:
            await interaction.response.send_message("작전 참가 중인 팀원은 추방할 수 없습니다.", ephemeral=True)
            return
        await asyncio.to_thread(police_unit_update, unit["id"], {"member_ids": [m for m in unit["member_ids"] if m != str(member.id)]})
    except Exception as e:
        print(f"Police unit kick error: {e}")
        await interaction.response.send_message(POLICE_UNIT_DB_ERROR_MSG, ephemeral=True)
        return
    await interaction.response.send_message(f"**{member.mention}** 님이 수사팀 **「{unit['name']}」** 에서 추방되었습니다.")

@unit_group.command(name="위임", description="팀장 자리를 다른 팀원에게 넘깁니다. (팀장 전용)")
@app_commands.describe(member="새 팀장")
async def unit_transfer_cmd(interaction: discord.Interaction, member: discord.Member):
    if not await _unit_precheck(interaction): return
    user = interaction.user
    try:
        unit = await asyncio.to_thread(police_unit_get_by_user, interaction.guild_id, user.id)
        if not unit or unit["owner_id"] != str(user.id):
            await interaction.response.send_message("팀장만 사용할 수 있습니다.", ephemeral=True)
            return
        if member.id == user.id or str(member.id) not in (unit.get("member_ids") or []):
            await interaction.response.send_message("같은 수사팀의 다른 팀원에게만 위임할 수 있습니다.", ephemeral=True)
            return
        await asyncio.to_thread(police_unit_update, unit["id"], {"owner_id": str(member.id)})
    except Exception as e:
        print(f"Police unit transfer error: {e}")
        await interaction.response.send_message(POLICE_UNIT_DB_ERROR_MSG, ephemeral=True)
        return
    await interaction.response.send_message(f"수사팀 **「{unit['name']}」** 의 새 팀장은 **{member.mention}** 님입니다!")

@unit_group.command(name="기부", description="수사팀 금고에 자금을 기부합니다. (작전 자금으로 쓰입니다)")
@app_commands.describe(금액=f"기부할 금액 (${POLICE_UNIT_MIN_DONATE:,} 이상)")
async def unit_donate_cmd(interaction: discord.Interaction, 금액: app_commands.Range[int, POLICE_UNIT_MIN_DONATE, 10_000_000]):
    if not await _unit_precheck(interaction): return
    user = interaction.user
    # 1) 조회 (두 번의 DB 조회를 동시에 — Discord 의 3초 응답 제한 안에 끝내기 위해)
    try:
        unit, info = await asyncio.gather(
            asyncio.to_thread(police_unit_get_by_user, interaction.guild_id, user.id),
            asyncio.to_thread(get_user_data, user.id),
        )
    except Exception as e:
        print(f"Police unit donate lookup error: {e}")
        await interaction.response.send_message(POLICE_UNIT_DB_ERROR_MSG, ephemeral=True)
        return
    if not unit:
        await interaction.response.send_message("소속된 수사팀이 없습니다.", ephemeral=True)
        return
    if info["money"] < 금액:
        await interaction.response.send_message("보유 자금이 부족합니다.", ephemeral=True)
        return

    # 2) 여기부터는 DB 쓰기가 여러 번이라 오래 걸릴 수 있으므로 먼저 응답을 예약해 둔다
    await interaction.response.defer()
    paid = False
    try:
        await asyncio.to_thread(update_user_stats, user.id, -금액, 0)
        paid = True
        new_treasury = await asyncio.to_thread(police_unit_add_treasury_atomic, unit["id"], 금액)
    except Exception as e:
        print(f"Police unit donate error: {e}")
        if paid:   # 돈만 빠지고 금고에 못 넣은 경우 되돌려 준다
            try:
                await asyncio.to_thread(update_user_stats, user.id, 금액, 0)
            except Exception as e2:
                print(f"Police unit donate refund error: {e2}")
        await interaction.followup.send(POLICE_UNIT_DB_ERROR_MSG + "\n(자금은 차감되지 않았습니다.)", ephemeral=True)
        return
    await interaction.followup.send(f"**{user.mention}** 님이 수사팀 금고에 `${금액:,}` 을(를) 기부했습니다! (금고 `${new_treasury:,}`)")

class PoliceUnitDisbandView(discord.ui.View):
    def __init__(self, owner: discord.Member, unit_id, unit_name: str):
        super().__init__(timeout=30)
        self.owner = owner
        self.unit_id = unit_id
        self.unit_name = unit_name

    @discord.ui.button(label="정말 해산합니다", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner.id:
            await interaction.response.send_message("팀장만 누를 수 있습니다.", ephemeral=True)
            return
        self.stop()
        try:
            if self.unit_id in police_ops:
                await interaction.response.edit_message(content="작전 진행 중에는 해산할 수 없습니다.", view=None)
                return
            await asyncio.to_thread(police_unit_delete, self.unit_id)
        except Exception as e:
            print(f"Police unit disband error: {e}")
            await interaction.response.edit_message(content=POLICE_UNIT_DB_ERROR_MSG, view=None)
            return
        await interaction.response.edit_message(content=f"수사팀 **「{self.unit_name}」** 이(가) 해산되었습니다. (금고의 자금도 함께 사라집니다)", view=None)

@unit_group.command(name="해산", description="수사팀을 해산합니다. (팀장 전용, 금고 자금은 사라집니다)")
async def unit_disband_cmd(interaction: discord.Interaction):
    if not await _unit_precheck(interaction): return
    user = interaction.user
    try:
        unit = await asyncio.to_thread(police_unit_get_by_user, interaction.guild_id, user.id)
    except Exception as e:
        print(f"Police unit disband lookup error: {e}")
        await interaction.response.send_message(POLICE_UNIT_DB_ERROR_MSG, ephemeral=True)
        return
    if not unit or unit["owner_id"] != str(user.id):
        await interaction.response.send_message("팀장만 사용할 수 있습니다.", ephemeral=True)
        return
    await interaction.response.send_message(
        f"정말 **「{unit['name']}」** 을(를) 해산할까요? 금고 `${unit.get('treasury', 0):,}` 도 함께 사라집니다.",
        view=PoliceUnitDisbandView(user, unit["id"], unit["name"]), ephemeral=True)


# ================================================================ /수사팀 작전 (합동 소탕 작전)
class PoliceOpSession:
    def __init__(self, unit: dict, tier_key: str, leader: discord.Member, leader_stats: dict):
        from .confirm_upgrade import unit_upgrades   # 순환 import 방지: 호출 시점에 가져옴
        self.unit_id = unit["id"]
        self.unit_name = unit["name"]
        self.upgrades = unit_upgrades(unit)   # 수사팀 시설 레벨 (/수사팀 시설)
        self.member_ids = set(unit.get("member_ids") or [])
        self.tier_key = tier_key
        self.tier = POLICE_OP_TIERS[tier_key]
        self.leader = leader
        self.players = {leader.id: (leader, leader_stats)}   # {user_id: (member, combat_stats)}
        self.state = "lobby"                                 # lobby → running → done
        self.go = asyncio.Event()                            # 팀장이 '출동' 을 누르면 즉시 시작
        self.message = None

def police_op_release(session: PoliceOpSession):
    if police_ops.get(session.unit_id) is session:
        police_ops.pop(session.unit_id, None)
    for uid in list(session.players):
        if police_op_user_map.get(uid) == session.unit_id:
            police_op_user_map.pop(uid, None)

def police_op_lobby_embed(session: PoliceOpSession) -> discord.Embed:
    t = session.tier
    lines = [f"{'[팀장] ' if uid == session.leader.id else '• '}{m.mention}" for uid, (m, _) in session.players.items()]
    embed = discord.Embed(
        title=f"[합동 소탕 작전] {t['name']}",
        description=(f"{t['desc']}\n\n"
                     f"수사팀 **「{session.unit_name}」** 팀원은 아래 **참가** 버튼으로 합류하세요. "
                     f"({POLICE_OP_LOBBY_SEC}초 안에 모이거나, 팀장이 **출동** 을 누르면 시작합니다)"),
        color=discord.Color.blue()
    )
    embed.add_field(name=f"참가자 ({len(session.players)}/{POLICE_OP_MAX_PLAYERS})", value="\n".join(lines), inline=False)
    embed.add_field(name="조건", value=f"최소 **{t['min_players']}명** · 명예 **{t['min_honor']:,}pt** 이상", inline=True)
    embed.add_field(name="보상 (전원)", value=f"최대 `${t['money']:,}` · 명예 `+{t['honor']}`", inline=True)
    return embed

class PoliceOpLobbyView(discord.ui.View):
    def __init__(self, session: PoliceOpSession):
        super().__init__(timeout=POLICE_OP_LOBBY_SEC + 30)
        self.session = session

    @discord.ui.button(label="참가", style=discord.ButtonStyle.primary)
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        s = self.session
        user = interaction.user
        if s.state != "lobby":
            await interaction.response.send_message("이미 모집이 끝났습니다.", ephemeral=True)
            return
        if str(user.id) not in s.member_ids:
            await interaction.response.send_message("이 수사팀의 팀원만 참가할 수 있습니다.", ephemeral=True)
            return
        if not _is_police(user):
            await interaction.response.send_message("치안국 경찰만 참가할 수 있습니다.", ephemeral=True)
            return
        if user.id in s.players or user.id in police_op_user_map:
            await interaction.response.send_message("이미 작전에 참가 중입니다.", ephemeral=True)
            return
        if _is_jailed(user.id):
            await interaction.response.send_message("수감 중에는 참가할 수 없습니다.", ephemeral=True)
            return
        try:
            info = await asyncio.to_thread(get_user_data, user.id)
        except Exception as e:
            print(f"Police op join error: {e}")
            await interaction.response.send_message(POLICE_UNIT_DB_ERROR_MSG, ephemeral=True)
            return
        # DB 조회(await) 뒤에 상태를 다시 확인한 다음, 바로 등록한다
        if s.state != "lobby":
            await interaction.response.send_message("이미 모집이 끝났습니다.", ephemeral=True)
            return
        if info["honor"] < s.tier["min_honor"]:
            await interaction.response.send_message(f"이 작전은 명예 **{s.tier['min_honor']:,}pt** 이상만 참가할 수 있습니다.", ephemeral=True)
            return
        if len(s.players) >= POLICE_OP_MAX_PLAYERS:
            await interaction.response.send_message("참가 인원이 가득 찼습니다.", ephemeral=True)
            return
        if user.id in s.players or user.id in police_op_user_map:
            await interaction.response.send_message("이미 작전에 참가 중입니다.", ephemeral=True)
            return
        s.players[user.id] = (user, compute_combat_stats(info["honor"], info.get("inventory", {})))
        police_op_user_map[user.id] = s.unit_id
        await interaction.response.edit_message(embed=police_op_lobby_embed(s), view=self)

    @discord.ui.button(label="출동 (팀장)", style=discord.ButtonStyle.success)
    async def go(self, interaction: discord.Interaction, button: discord.ui.Button):
        s = self.session
        if interaction.user.id != s.leader.id:
            await interaction.response.send_message("작전을 연 팀장만 출동 명령을 내릴 수 있습니다.", ephemeral=True)
            return
        if s.state != "lobby":
            await interaction.response.send_message("이미 모집이 끝났습니다.", ephemeral=True)
            return
        if len(s.players) < s.tier["min_players"]:
            await interaction.response.send_message(f"최소 **{s.tier['min_players']}명** 이 모여야 출동할 수 있습니다. (현재 {len(s.players)}명)", ephemeral=True)
            return
        s.go.set()
        await interaction.response.defer()

def police_op_stage_chance(session: PoliceOpSession) -> float:
    from .confirm_upgrade import POLICE_UNIT_FACILITIES   # 순환 import 방지: 호출 시점에 가져옴
    t = session.tier
    n = len(session.players)
    avg_atk = sum(st["atk"] for _, st in session.players.values()) / max(1, n)
    chance = t["base"] + 0.04 * max(0, n - t["min_players"]) + 0.12 * min(1.0, avg_atk / t["ref_atk"])
    chance += POLICE_UNIT_FACILITIES["range"]["per"] / 100 * session.upgrades.get("range", 0)   # 사격 훈련장
    return max(0.30, min(0.95, chance))

def police_op_progress_embed(session: PoliceOpSession, results: list) -> discord.Embed:
    lines = []
    for i, name in enumerate(POLICE_OP_STAGES):
        if i < len(results):
            lines.append(f"{'성공' if results[i] else '실패'} — {name}")
        elif i == len(results):
            lines.append(f"진행 중… — {name}")
        else:
            lines.append(f"대기 — {name}")
    embed = discord.Embed(title=f"[합동 소탕 작전] {session.tier['name']}", description="\n".join(lines), color=discord.Color.blue())
    embed.set_footer(text=f"수사팀 「{session.unit_name}」 · 참가 {len(session.players)}명")
    return embed

async def run_police_op(session: PoliceOpSession):
    from .confirm_upgrade import POLICE_UNIT_FACILITIES, unit_op_cooldown_sec   # 순환 import 방지: 호출 시점에 가져옴
    from .salary import police_shift_record   # 순환 import 방지: 호출 시점에 가져옴
    tier = session.tier
    ran = False
    try:
        try:
            await asyncio.wait_for(session.go.wait(), timeout=POLICE_OP_LOBBY_SEC)
        except asyncio.TimeoutError:
            pass
        session.state = "running"
        n = len(session.players)

        if n < tier["min_players"]:   # 인원 미달 → 작전 취소, 작전비 환불
            try:
                await asyncio.to_thread(police_unit_add_treasury, session.unit_id, tier["cost"])
            except Exception as e:
                print(f"Police op refund error: {e}")
            embed = discord.Embed(title="[합동 소탕 작전 취소]",
                                  description=f"인원이 모이지 않아 작전이 취소되었습니다. (작전비 `${tier['cost']:,}` 는 금고로 돌아갑니다)",
                                  color=discord.Color.dark_grey())
            await session.message.edit(embed=embed, view=None)
            return

        ran = True
        chance = police_op_stage_chance(session)
        results = []
        await session.message.edit(embed=police_op_progress_embed(session, results), view=None)
        for _ in POLICE_OP_STAGES:
            await asyncio.sleep(POLICE_OP_STAGE_SEC)
            results.append(random.random() < chance)
            await session.message.edit(embed=police_op_progress_embed(session, results), view=None)

        succ = sum(results)
        frac = POLICE_OP_REWARD_FRAC[succ]
        lab_mult = 1 + POLICE_UNIT_FACILITIES["lab"]["per"] / 100 * session.upgrades.get("lab", 0)   # 과학수사 연구소
        if succ > 0:
            money_each = int(tier["money"] * frac * lab_mult)
            honor_each = int(round(tier["honor"] * frac))
        else:
            money_each = int(tier["money"] * POLICE_OP_FAIL_MONEY_RATIO * lab_mult)
            honor_each = 0
        points = int(tier["points"] * frac)

        paid_to = []
        for uid, (member, _) in session.players.items():
            try:
                await asyncio.to_thread(update_user_stats, uid, money_each, honor_each)
                paid_to.append(member)
            except Exception as e:
                print(f"Police op payout error ({uid}): {e}")
        if succ > 0:
            for member in paid_to:
                police_shift_record(member.id, "op_stage", succ)   # /월급 근무 실적
                await police_ach_bump(member.id, pending=True, police_op_stages=succ)   # 업적 카운터
        treasury_add = int(money_each * len(paid_to) * POLICE_OP_TREASURY_RETURN) if succ > 0 else 0
        try:
            await asyncio.to_thread(police_unit_record_op, session.unit_id, treasury_add, points, succ > 0)
        except Exception as e:
            print(f"Police op record error: {e}")

        titles = {3: "작전 대성공", 2: "작전 성공", 1: "작전 부분 성공", 0: "작전 실패"}
        colors = {3: discord.Color.green(), 2: discord.Color.green(), 1: discord.Color.gold(), 0: discord.Color.red()}
        desc = "\n".join(f"{'성공' if r else '실패'} — {name}" for name, r in zip(POLICE_OP_STAGES, results))
        desc += f"\n\n**{succ}/{len(POLICE_OP_STAGES)} 단계 성공**"
        if succ > 0:
            desc += f" (보상 {int(frac * 100)}%)\n참가자 전원 `${money_each:,}` · 명예 `+{honor_each}`"
            desc += f"\n수사팀 작전 점수 `+{points}` · 금고 `+${treasury_add:,}`"
        else:
            desc += f"\n작전은 실패했지만 참가자 전원에게 위로금 `${money_each:,}` 이(가) 지급됩니다."
        embed = discord.Embed(title=f"[{titles[succ]}] {tier['name']}", description=desc, color=colors[succ])
        embed.add_field(name="참가자", value="\n".join(m.mention for m in paid_to) or "-", inline=False)
        await session.message.edit(embed=embed, view=None)
    except Exception as e:
        print(f"Police op error: {e}")
    finally:
        session.state = "done"
        if ran:
            police_op_cooldowns[session.unit_id] = datetime.now() + timedelta(seconds=unit_op_cooldown_sec(session.upgrades))
        police_op_release(session)

@unit_group.command(name="작전", description="우리 수사팀의 합동 소탕 작전을 엽니다. (수사팀 금고에서 작전비가 소모됩니다)")
@app_commands.describe(대상="소탕할 범죄 조직")
@app_commands.choices(대상=[
    app_commands.Choice(name=f"{t['name']} (인원 {t['min_players']}+ · 명예 {t['min_honor']:,}+ · 작전비 ${t['cost']:,})", value=k)
    for k, t in POLICE_OP_TIERS.items()
])
async def unit_op_cmd(interaction: discord.Interaction, 대상: app_commands.Choice[str]):
    if not await _unit_precheck(interaction): return
    user = interaction.user
    tier = POLICE_OP_TIERS[대상.value]

    try:
        unit = await asyncio.to_thread(police_unit_get_by_user, interaction.guild_id, user.id)
        info = await asyncio.to_thread(get_user_data, user.id)
    except Exception as e:
        print(f"Police op start error: {e}")
        await interaction.response.send_message(POLICE_UNIT_DB_ERROR_MSG, ephemeral=True)
        return
    if not unit:
        await interaction.response.send_message("소속된 수사팀이 없습니다. `/수사팀 생성` 또는 초대를 받아 보세요.", ephemeral=True)
        return
    if unit["owner_id"] != str(user.id):
        await interaction.response.send_message("작전은 팀장만 열 수 있습니다.", ephemeral=True)
        return

    # 확인과 등록 사이에 await 가 없으므로, 같은 수사팀이 동시에 작전을 열 수 없다
    if unit["id"] in police_ops:
        await interaction.response.send_message("우리 수사팀의 작전이 이미 진행 중입니다.", ephemeral=True)
        return
    if user.id in police_op_user_map:
        await interaction.response.send_message("이미 작전에 참가 중입니다.", ephemeral=True)
        return
    cd = police_op_cooldowns.get(unit["id"])
    if cd and datetime.now() < cd:
        remain = int((cd - datetime.now()).total_seconds())
        await interaction.response.send_message(f"수사팀이 정비 중입니다. **{remain // 60}분 {remain % 60}초** 뒤에 다시 작전을 열 수 있습니다.", ephemeral=True)
        return
    if len(unit.get("member_ids") or []) < tier["min_players"]:
        await interaction.response.send_message(f"이 작전은 팀원이 최소 **{tier['min_players']}명** 이상이어야 합니다.", ephemeral=True)
        return
    if unit.get("treasury", 0) < tier["cost"]:
        await interaction.response.send_message(f"수사팀 금고가 부족합니다. (작전비 `${tier['cost']:,}` / 금고 `${unit.get('treasury', 0):,}`)\n`/수사팀 기부` 로 금고를 채워 주세요.", ephemeral=True)
        return
    if info["honor"] < tier["min_honor"]:
        await interaction.response.send_message(f"이 작전은 명예 **{tier['min_honor']:,}pt** 이상만 도전할 수 있습니다.", ephemeral=True)
        return

    session = PoliceOpSession(unit, 대상.value, user, compute_combat_stats(info["honor"], info.get("inventory", {})))
    police_ops[unit["id"]] = session
    police_op_user_map[user.id] = unit["id"]
    try:
        await asyncio.to_thread(police_unit_update, unit["id"], {"treasury": unit["treasury"] - tier["cost"]})
    except Exception as e:
        police_op_release(session)
        print(f"Police op treasury error: {e}")
        await interaction.response.send_message(POLICE_UNIT_DB_ERROR_MSG, ephemeral=True)
        return

    await interaction.response.send_message(embed=police_op_lobby_embed(session), view=PoliceOpLobbyView(session))
    session.message = await interaction.original_response()
    keep_task(asyncio.create_task(run_police_op(session)))

@unit_group.command(name="작전정보", description="합동 소탕 작전 목록과 진행 방법을 확인합니다.")
async def unit_op_info_cmd(interaction: discord.Interaction):
    if not await check_maintenance(interaction): return
    lines = []
    for t in POLICE_OP_TIERS.values():
        lines.append(f"**{t['name']}** — 인원 {t['min_players']}+ · 명예 {t['min_honor']:,}+ · 작전비 ${t['cost']:,}\n"
                     f"└ {t['desc']}\n└ 보상(전원, 최대): `${t['money']:,}` · 명예 `+{t['honor']}` · 작전 점수 `+{t['points']}`")
    embed = discord.Embed(
        title="[합동 소탕 작전 안내]",
        description=("1. 팀장이 `/수사팀 작전` 으로 작전을 엽니다. (금고에서 작전비가 빠집니다)\n"
                     f"2. 팀원이 **참가** 버튼으로 모입니다. ({POLICE_OP_LOBBY_SEC}초, 팀장이 **출동** 을 누르면 즉시 시작)\n"
                     "3. 잠입 → 급습 → 검거 3단계를 차례로 수행하고, 성공한 단계 수만큼 보상을 받습니다.\n"
                     "   (3단계 100% · 2단계 70% · 1단계 40% · 전부 실패 시 위로금)\n"
                     "4. 인원이 많고 장비가 좋을수록 단계 성공률이 올라갑니다.\n"
                     f"5. 같은 수사팀은 작전 후 {POLICE_OP_COOLDOWN_SEC // 60}분간 정비합니다. (`/수사팀 시설` 의 시설로 성공률·보상·대기시간·정원을 키울 수 있어요)\n\n" + "\n\n".join(lines)),
        color=discord.Color.blue()
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)
