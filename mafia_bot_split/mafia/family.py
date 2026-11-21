"""조직(패밀리)과 레이드

합친 모듈: family.py, raid.py
"""

import asyncio
import discord
import random
import re
import time
from datetime import datetime, timedelta
from discord import app_commands

from .core import bot, inc_money, jail_list, keep_task, supabase
from .status import (
    _check_updated,
    bump_user_stats_sync,
    check_jail,
    compute_combat_stats,
    get_user_data,
    update_user_stats,
)
from .admin import check_maintenance, check_membership


# ==========================================================================
# [합친 모듈] family.py
#   조직(패밀리) 시스템 (/조직 ...)
# ==========================================================================
# ==========================================================================
# [패밀리(조직) 시스템]  /조직 ...   &   [단체 레이드]  /레이드 ...
#   • 조직 데이터는 Supabase 의 `families` 테이블에 저장됩니다. (family_setup.sql 을 먼저 실행하세요)
#   • 레이드는 짧게 끝나는 이벤트라서 진행 상태는 메모리에만 두며, 봇이 재시작되면 진행 중이던 레이드는 사라집니다.
# ==========================================================================
# --- 조직 설정 ---
FAMILY_CREATE_COST = 10000        # 조직 창설 비용 ($)
FAMILY_CREATE_MIN_HONOR = 500     # 조직 창설에 필요한 최소 명예
FAMILY_MAX_MEMBERS = 10           # 조직 최대 인원
FAMILY_NAME_PATTERN = re.compile(r"^[0-9A-Za-z가-힣 ]{2,12}$")
FAMILY_MIN_DONATE = 100           # 금고 최소 기부액
FAMILY_DB_ERROR_MSG = "조직 DB 처리 중 오류가 발생했습니다. (관리자: `families` 테이블이 만들어져 있는지 확인해 주세요)"

# --- 레이드 설정 ---
RAID_LOBBY_SEC = 120              # 참가 모집 시간
RAID_DURATION_SEC = 420           # 전투 제한 시간 (7분)
RAID_TICK_SEC = 4                 # 화면 갱신 주기
RAID_MAX_PLAYERS = 8
RAID_FAMILY_COOLDOWN_SEC = 1800   # 같은 조직의 레이드 재시작 대기시간 (30분)
RAID_GCD_SEC = 2.0                # 모든 행동 공통 대기시간
RAID_ATTACK_CD_SEC = 4.0          # 공격 대기시간
RAID_HEAL_CD_SEC = 25.0           # 응급처치 대기시간
RAID_RALLY_CD_SEC = 30.0          # 전투 함성 대기시간
RAID_RALLY_DURATION_SEC = 10.0    # 전투 함성 지속시간 (공격대 전원의 공격력 증가, 겹쳐 쓰면 시간만 갱신)
RAID_RALLY_BONUS = 0.20           # 전투 함성 효과: 공격대 전원 공격력 +20%
RAID_AOE_INTERVAL_SEC = 20        # 광역기 주기
RAID_AOE_INTERVAL_ENRAGE_SEC = 15 # 격노 후 광역기 주기
RAID_HEAL_RATIO = 0.12            # 응급처치(전원 회복) 회복량: 최대 체력의 12%
RAID_REVIVE_RATIO = 0.40          # 쓰러진 동료 부활 시 체력 비율
RAID_TREASURY_RETURN = 0.15       # 승리 시 지급된 보상금의 15%가 조직 금고로 들어감
RAID_LOSE_MONEY_RATIO = 0.10      # 실패해도 참전자에게 주는 위로금 비율
RAID_BASE_SHARE = 0.6             # 보상 중 '균등 분배' 비율 (나머지 0.4는 딜량 비례)

# hits: 참가자 1인이 '평균 몇 번 때리면' 쓰러지는지 (보스 체력은 참가자 공격력에 맞춰 자동 계산)
# def_ratio: 보스 방어력 = 참가자 평균 공격력 × 비율 / aoe: 광역기가 깎는 '최대 체력 대비' 비율
RAID_TIERS = {
    "rat": {
        "name": "하수구의 쥐왕", "min_players": 2, "min_honor": 0, "cost": 1000,
        "hits": 42, "def_ratio": 0.25, "aoe": 0.07, "aoe_enrage": 0.10,
        "money": inc_money(3000), "honor": 30, "points": 10,
        "desc": "하수도를 지배하는 거대한 쥐떼의 왕. 입문자용 레이드입니다.",
    },
    "bull": {
        "name": "부두 항만의 폭군", "min_players": 3, "min_honor": 1500, "cost": 3000,
        "hits": 51, "def_ratio": 0.30, "aoe": 0.09, "aoe_enrage": 0.13,
        "money": inc_money(7000), "honor": 60, "points": 25,
        "desc": "에르느코항 부두를 장악한 괴력의 폭군. 응급처치 담당이 꼭 필요합니다.",
    },
    "king": {
        "name": "칼렌데 지하 황제", "min_players": 4, "min_honor": 4000, "cost": 8000,
        "hits": 59, "def_ratio": 0.35, "aoe": 0.09, "aoe_enrage": 0.13,
        "money": inc_money(14000), "honor": 100, "points": 60,
        "desc": "칼렌데 지하 세계의 진짜 주인. 정상급 조직만이 도전할 수 있습니다.",
    },
}

active_raids = {}            # {family_id: RaidSession}
raid_user_map = {}           # {user_id: family_id}  (레이드 참가 중인 유저)
raid_family_cooldowns = {}   # {family_id: datetime}


# ---------------------------------------------------------------- 조직 DB 도우미 (동기)
def family_get_by_user(guild_id: int, user_id: int):
    res = (supabase.table("families").select("*")
           .eq("guild_id", str(guild_id)).contains("member_ids", [str(user_id)]).limit(1).execute())
    return res.data[0] if res.data else None

def family_get_by_id(family_id):
    res = supabase.table("families").select("*").eq("id", family_id).limit(1).execute()
    return res.data[0] if res.data else None

def family_get_by_name(guild_id: int, name: str):
    res = (supabase.table("families").select("*")
           .eq("guild_id", str(guild_id)).eq("name", name).limit(1).execute())
    return res.data[0] if res.data else None

def family_list_top(guild_id: int, limit: int = 10):
    res = (supabase.table("families").select("*").eq("guild_id", str(guild_id))
           .order("raid_points", desc=True).limit(limit).execute())
    return res.data or []

def family_create(guild_id: int, name: str, owner_id: int):
    row = {
        "guild_id": str(guild_id), "name": name, "owner_id": str(owner_id),
        "member_ids": [str(owner_id)], "treasury": 0, "raid_points": 0,
    }
    res = supabase.table("families").insert(row).execute()
    return res.data[0] if res.data else row

def family_update(family_id, data: dict):
    res = supabase.table("families").update(data).eq("id", family_id).execute()
    _check_updated(res, "family_update")

def family_delete(family_id):
    supabase.table("families").delete().eq("id", family_id).execute()
    try:   # 해산된 조직의 구역은 주인 없는 상태로 돌려놓는다
        supabase.table("territories").delete().eq("family_id", str(family_id)).execute()
    except Exception as e:
        print(f"Territory cleanup error: {e}")

def family_detach_user(user_id: int):
    """유저를 모든 조직에서 제거 (조직장이면 다음 조직원에게 위임, 마지막 1명이면 조직 해산). /탈퇴 초기화에서도 사용."""
    if not supabase:
        return
    uid = str(user_id)
    res = supabase.table("families").select("*").contains("member_ids", [uid]).execute()
    for fam in (res.data or []):
        members = [m for m in fam["member_ids"] if m != uid]
        if not members:
            family_delete(fam["id"])
            continue
        data = {"member_ids": members}
        if fam["owner_id"] == uid:
            data["owner_id"] = members[0]
        family_update(fam["id"], data)

def _is_mafia(member: discord.Member) -> bool:
    role = discord.utils.get(member.guild.roles, name="조직원")
    return bool(role and role in member.roles)

def _is_jailed(user_id: int) -> bool:
    return user_id in jail_list and datetime.now() < jail_list[user_id]

def _bar(cur: int, mx: int, n: int = 12) -> str:
    ratio = 0 if mx <= 0 else max(0.0, min(1.0, cur / mx))
    filled = int(round(ratio * n))
    return "█" * filled + "░" * (n - filled)


# ================================================================ /조직
family_group = app_commands.Group(name="조직", description="[패밀리] 조직(클럽)을 만들고 함께 활동합니다.")
bot.tree.add_command(family_group)

async def _family_precheck(interaction: discord.Interaction, need_mafia: bool = True) -> bool:
    if not await check_maintenance(interaction): return False
    if not await check_membership(interaction): return False
    if await check_jail(interaction): return False
    if need_mafia and not _is_mafia(interaction.user):
        await interaction.response.send_message("조직(패밀리) 기능은 마피아 조직원만 사용할 수 있습니다. (경찰은 `/수사팀` 을 이용하세요)", ephemeral=True)
        return False
    return True

@family_group.command(name="생성", description=f"조직을 창설합니다. (비용 ${FAMILY_CREATE_COST:,} / 명예 {FAMILY_CREATE_MIN_HONOR}pt 이상)")
@app_commands.describe(이름="조직 이름 (한글·영문·숫자·띄어쓰기 2~12자)")
async def family_create_cmd(interaction: discord.Interaction, 이름: str):
    if not await _family_precheck(interaction): return
    user = interaction.user
    name = 이름.strip()
    if not FAMILY_NAME_PATTERN.match(name):
        await interaction.response.send_message("조직 이름은 한글·영문·숫자·띄어쓰기로 **2~12자**여야 합니다.", ephemeral=True)
        return
    try:
        if family_get_by_user(interaction.guild_id, user.id):
            await interaction.response.send_message("이미 조직에 소속되어 있습니다. 먼저 `/조직 탈퇴` 하세요.", ephemeral=True)
            return
        if family_get_by_name(interaction.guild_id, name):
            await interaction.response.send_message("이미 같은 이름의 조직이 있습니다.", ephemeral=True)
            return
        info = get_user_data(user.id)
        if info["honor"] < FAMILY_CREATE_MIN_HONOR:
            await interaction.response.send_message(f"조직을 만들려면 명예 **{FAMILY_CREATE_MIN_HONOR}pt** 이상이어야 합니다. (현재 {info['honor']:,}pt)", ephemeral=True)
            return
        if info["money"] < FAMILY_CREATE_COST:
            await interaction.response.send_message(f"창설 비용 **${FAMILY_CREATE_COST:,}** 이(가) 부족합니다. (보유 ${info['money']:,})", ephemeral=True)
            return
        update_user_stats(user.id, -FAMILY_CREATE_COST, 0)
        family_create(interaction.guild_id, name, user.id)
    except Exception as e:
        print(f"Family create error: {e}")
        await interaction.response.send_message(FAMILY_DB_ERROR_MSG, ephemeral=True)
        return

    embed = discord.Embed(
        title="[조직 창설]",
        description=(f"**{user.mention}** 님이 새로운 조직 **「{name}」** 을(를) 창설했습니다!\n\n"
                     f"**창설 비용:** `${FAMILY_CREATE_COST:,}`\n"
                     f"`/조직 초대` 로 동료를 모아 `/레이드 시작` 에 도전해 보세요."),
        color=discord.Color.dark_gold()
    )
    await interaction.response.send_message(embed=embed)

def _family_embed(fam: dict, guild: discord.Guild) -> discord.Embed:
    members = fam.get("member_ids") or []
    lines = []
    for m in members:
        crown = "[조직장] " if m == fam["owner_id"] else "• "
        lines.append(f"{crown}<@{m}>")
    embed = discord.Embed(title=f"조직 「{fam['name']}」", color=discord.Color.dark_gold())
    embed.add_field(name=f"조직원 ({len(members)}/{FAMILY_MAX_MEMBERS})", value="\n".join(lines) or "-", inline=False)
    embed.add_field(name="금고", value=f"`${fam.get('treasury', 0):,}`", inline=True)
    embed.add_field(name="레이드 점수", value=f"`{fam.get('raid_points', 0):,} pt`", inline=True)
    raid = active_raids.get(fam["id"])
    if raid:
        embed.add_field(name="진행 중인 레이드", value=f"{raid.tier['name']} ({'모집 중' if raid.state == 'lobby' else '전투 중'})", inline=False)
    cd = raid_family_cooldowns.get(fam["id"])
    if cd and datetime.now() < cd:
        embed.add_field(name="다음 레이드", value=f"{int((cd - datetime.now()).total_seconds() // 60) + 1}분 후", inline=True)
    return embed

@family_group.command(name="정보", description="조직 정보를 확인합니다. (이름을 비우면 내 조직)")
@app_commands.describe(이름="확인할 조직 이름 (비우면 내 조직)")
async def family_info_cmd(interaction: discord.Interaction, 이름: str = None):
    if not await check_maintenance(interaction): return
    try:
        fam = family_get_by_name(interaction.guild_id, 이름.strip()) if 이름 else family_get_by_user(interaction.guild_id, interaction.user.id)
    except Exception as e:
        print(f"Family info error: {e}")
        await interaction.response.send_message(FAMILY_DB_ERROR_MSG, ephemeral=True)
        return
    if not fam:
        msg = "해당 이름의 조직이 없습니다." if 이름 else "소속된 조직이 없습니다. `/조직 생성` 또는 초대를 받아 보세요."
        await interaction.response.send_message(msg, ephemeral=True)
        return
    await interaction.response.send_message(embed=_family_embed(fam, interaction.guild))

@family_group.command(name="목록", description="레이드 점수 기준 조직 랭킹 TOP 10")
async def family_rank_cmd(interaction: discord.Interaction):
    if not await check_maintenance(interaction): return
    try:
        rows = family_list_top(interaction.guild_id, 10)
    except Exception as e:
        print(f"Family list error: {e}")
        await interaction.response.send_message(FAMILY_DB_ERROR_MSG, ephemeral=True)
        return
    if not rows:
        await interaction.response.send_message("아직 창설된 조직이 없습니다.", ephemeral=True)
        return
    lines = [f"`{i + 1}위` **{r['name']}** — {r.get('raid_points', 0):,} pt · {len(r.get('member_ids') or [])}명" for i, r in enumerate(rows)]
    embed = discord.Embed(title="조직 랭킹 (레이드 점수)", description="\n".join(lines), color=discord.Color.gold())
    await interaction.response.send_message(embed=embed)

class FamilyInviteView(discord.ui.View):
    def __init__(self, family_id, family_name: str, inviter: discord.Member, target: discord.Member):
        super().__init__(timeout=60)
        self.family_id = family_id
        self.family_name = family_name
        self.inviter = inviter
        self.target = target
        self.resolved = False
        self.message = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.target.id:
            await interaction.response.send_message("초대받은 본인만 응답할 수 있습니다.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="수락", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.resolved: return
        self.resolved = True
        self.stop()
        try:
            if family_get_by_user(interaction.guild_id, self.target.id):
                await interaction.response.edit_message(content="이미 다른 조직에 소속되어 있어 가입할 수 없습니다.", embed=None, view=None)
                return
            fam = family_get_by_id(self.family_id)
            if not fam:
                await interaction.response.edit_message(content="조직이 더 이상 존재하지 않습니다.", embed=None, view=None)
                return
            members = list(fam.get("member_ids") or [])
            if len(members) >= FAMILY_MAX_MEMBERS:
                await interaction.response.edit_message(content="조직 정원이 가득 찼습니다.", embed=None, view=None)
                return
            members.append(str(self.target.id))
            family_update(self.family_id, {"member_ids": members})
        except Exception as e:
            print(f"Family invite accept error: {e}")
            await interaction.response.edit_message(content=FAMILY_DB_ERROR_MSG, embed=None, view=None)
            return
        embed = discord.Embed(title="[조직 가입]",
                              description=f"**{self.target.mention}** 님이 **「{self.family_name}」** 에 합류했습니다! ({len(members)}/{FAMILY_MAX_MEMBERS})",
                              color=discord.Color.green())
        await interaction.response.edit_message(content=None, embed=embed, view=None)

    @discord.ui.button(label="거절", style=discord.ButtonStyle.secondary)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.resolved: return
        self.resolved = True
        self.stop()
        await interaction.response.edit_message(content=f"{self.target.mention} 님이 **「{self.family_name}」** 초대를 거절했습니다.", embed=None, view=None)

    async def on_timeout(self):
        if self.resolved or not self.message: return
        try:
            await self.message.edit(content="초대가 만료되었습니다.", embed=None, view=None)
        except Exception:
            pass

@family_group.command(name="초대", description="조직원을 우리 조직에 초대합니다. (조직장 전용)")
@app_commands.describe(member="초대할 조직원")
async def family_invite_cmd(interaction: discord.Interaction, member: discord.Member):
    if not await _family_precheck(interaction): return
    user = interaction.user
    if member.bot or member.id == user.id:
        await interaction.response.send_message("올바른 초대 대상이 아닙니다.", ephemeral=True)
        return
    if not _is_mafia(member):
        await interaction.response.send_message("마피아 조직원만 초대할 수 있습니다.", ephemeral=True)
        return
    try:
        fam = family_get_by_user(interaction.guild_id, user.id)
        if not fam:
            await interaction.response.send_message("소속된 조직이 없습니다.", ephemeral=True)
            return
        if fam["owner_id"] != str(user.id):
            await interaction.response.send_message("조직장만 초대할 수 있습니다.", ephemeral=True)
            return
        if len(fam.get("member_ids") or []) >= FAMILY_MAX_MEMBERS:
            await interaction.response.send_message("조직 정원이 가득 찼습니다.", ephemeral=True)
            return
        if family_get_by_user(interaction.guild_id, member.id):
            await interaction.response.send_message("상대는 이미 다른 조직에 소속되어 있습니다.", ephemeral=True)
            return
    except Exception as e:
        print(f"Family invite error: {e}")
        await interaction.response.send_message(FAMILY_DB_ERROR_MSG, ephemeral=True)
        return

    view = FamilyInviteView(fam["id"], fam["name"], user, member)
    embed = discord.Embed(title="[조직 초대장]",
                          description=f"**{user.mention}** 님이 **{member.mention}** 님을 조직 **「{fam['name']}」** 에 초대했습니다.\n60초 안에 응답해 주세요.",
                          color=discord.Color.dark_gold())
    await interaction.response.send_message(content=member.mention, embed=embed, view=view)
    view.message = await interaction.original_response()

@family_group.command(name="탈퇴", description="현재 조직에서 탈퇴합니다.")
async def family_leave_cmd(interaction: discord.Interaction):
    if not await _family_precheck(interaction): return
    user = interaction.user
    if user.id in raid_user_map:
        await interaction.response.send_message("레이드 진행 중에는 탈퇴할 수 없습니다.", ephemeral=True)
        return
    try:
        fam = family_get_by_user(interaction.guild_id, user.id)
        if not fam:
            await interaction.response.send_message("소속된 조직이 없습니다.", ephemeral=True)
            return
        members = fam.get("member_ids") or []
        if fam["owner_id"] == str(user.id) and len(members) > 1:
            await interaction.response.send_message("조직장은 탈퇴할 수 없습니다. `/조직 위임` 으로 조직장을 넘기거나 `/조직 해산` 하세요.", ephemeral=True)
            return
        family_detach_user(user.id)
    except Exception as e:
        print(f"Family leave error: {e}")
        await interaction.response.send_message(FAMILY_DB_ERROR_MSG, ephemeral=True)
        return
    note = " (마지막 조직원이라 조직이 해산되었습니다)" if len(members) <= 1 else ""
    await interaction.response.send_message(f"**{user.mention}** 님이 **「{fam['name']}」** 에서 탈퇴했습니다.{note}")

@family_group.command(name="추방", description="조직원을 추방합니다. (조직장 전용)")
@app_commands.describe(member="추방할 조직원")
async def family_kick_cmd(interaction: discord.Interaction, member: discord.Member):
    if not await _family_precheck(interaction): return
    user = interaction.user
    try:
        fam = family_get_by_user(interaction.guild_id, user.id)
        if not fam or fam["owner_id"] != str(user.id):
            await interaction.response.send_message("조직장만 사용할 수 있습니다.", ephemeral=True)
            return
        if member.id == user.id or str(member.id) not in (fam.get("member_ids") or []):
            await interaction.response.send_message("추방할 수 없는 대상입니다.", ephemeral=True)
            return
        if member.id in raid_user_map:
            await interaction.response.send_message("레이드 참가 중인 조직원은 추방할 수 없습니다.", ephemeral=True)
            return
        family_update(fam["id"], {"member_ids": [m for m in fam["member_ids"] if m != str(member.id)]})
    except Exception as e:
        print(f"Family kick error: {e}")
        await interaction.response.send_message(FAMILY_DB_ERROR_MSG, ephemeral=True)
        return
    await interaction.response.send_message(f"**{member.mention}** 님이 조직 **「{fam['name']}」** 에서 추방되었습니다.")

@family_group.command(name="위임", description="조직장 자리를 다른 조직원에게 넘깁니다. (조직장 전용)")
@app_commands.describe(member="새 조직장")
async def family_transfer_cmd(interaction: discord.Interaction, member: discord.Member):
    if not await _family_precheck(interaction): return
    user = interaction.user
    try:
        fam = family_get_by_user(interaction.guild_id, user.id)
        if not fam or fam["owner_id"] != str(user.id):
            await interaction.response.send_message("조직장만 사용할 수 있습니다.", ephemeral=True)
            return
        if member.id == user.id or str(member.id) not in (fam.get("member_ids") or []):
            await interaction.response.send_message("같은 조직의 다른 조직원에게만 위임할 수 있습니다.", ephemeral=True)
            return
        family_update(fam["id"], {"owner_id": str(member.id)})
    except Exception as e:
        print(f"Family transfer error: {e}")
        await interaction.response.send_message(FAMILY_DB_ERROR_MSG, ephemeral=True)
        return
    await interaction.response.send_message(f"조직 **「{fam['name']}」** 의 새 조직장은 **{member.mention}** 님입니다!")

@family_group.command(name="기부", description=f"조직 금고에 자금을 기부합니다. (레이드 작전 자금으로 쓰입니다)")
@app_commands.describe(금액=f"기부할 금액 (${FAMILY_MIN_DONATE:,} 이상)")
async def family_donate_cmd(interaction: discord.Interaction, 금액: app_commands.Range[int, FAMILY_MIN_DONATE, 10_000_000]):
    if not await _family_precheck(interaction): return
    user = interaction.user
    try:
        fam = family_get_by_user(interaction.guild_id, user.id)
        if not fam:
            await interaction.response.send_message("소속된 조직이 없습니다.", ephemeral=True)
            return
        if get_user_data(user.id)["money"] < 금액:
            await interaction.response.send_message("보유 자금이 부족합니다.", ephemeral=True)
            return
        update_user_stats(user.id, -금액, 0)
        fam = family_get_by_id(fam["id"])   # 최신 금고 값으로 갱신
        family_update(fam["id"], {"treasury": fam.get("treasury", 0) + 금액})
    except Exception as e:
        print(f"Family donate error: {e}")
        await interaction.response.send_message(FAMILY_DB_ERROR_MSG, ephemeral=True)
        return
    await interaction.response.send_message(f"**{user.mention}** 님이 조직 금고에 `${금액:,}` 을(를) 기부했습니다! (금고 `${fam.get('treasury', 0) + 금액:,}`)")

class FamilyDisbandView(discord.ui.View):
    def __init__(self, owner: discord.Member, family_id, family_name: str):
        super().__init__(timeout=30)
        self.owner = owner
        self.family_id = family_id
        self.family_name = family_name

    @discord.ui.button(label="정말 해산합니다", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        from .territory import war_family_map  # 순환 import 방지: 호출 시점에 가져옴
        if interaction.user.id != self.owner.id:
            await interaction.response.send_message("조직장만 누를 수 있습니다.", ephemeral=True)
            return
        self.stop()
        try:
            if self.family_id in active_raids or str(self.family_id) in war_family_map:
                await interaction.response.edit_message(content="레이드 또는 구역 점령전 진행 중에는 해산할 수 없습니다.", view=None)
                return
            family_delete(self.family_id)
        except Exception as e:
            print(f"Family disband error: {e}")
            await interaction.response.edit_message(content=FAMILY_DB_ERROR_MSG, view=None)
            return
        await interaction.response.edit_message(content=f"조직 **「{self.family_name}」** 이(가) 해산되었습니다. (금고의 자금도 함께 사라집니다)", view=None)

@family_group.command(name="해산", description="조직을 해산합니다. (조직장 전용, 금고 자금은 사라집니다)")
async def family_disband_cmd(interaction: discord.Interaction):
    if not await _family_precheck(interaction): return
    user = interaction.user
    try:
        fam = family_get_by_user(interaction.guild_id, user.id)
    except Exception as e:
        print(f"Family disband lookup error: {e}")
        await interaction.response.send_message(FAMILY_DB_ERROR_MSG, ephemeral=True)
        return
    if not fam or fam["owner_id"] != str(user.id):
        await interaction.response.send_message("조직장만 사용할 수 있습니다.", ephemeral=True)
        return
    await interaction.response.send_message(
        f"정말 **「{fam['name']}」** 을(를) 해산할까요? 금고 `${fam.get('treasury', 0):,}` 도 함께 사라집니다.",
        view=FamilyDisbandView(user, fam["id"], fam["name"]), ephemeral=True)


# ==========================================================================
# [합친 모듈] raid.py
#   단체 레이드 (/레이드 ...)
# ==========================================================================
# ================================================================ /레이드
class RaidPlayer:
    __slots__ = ("member", "uid", "name", "max_hp", "hp", "atk", "dmg_done", "hits",
                 "gcd_until", "atk_ready", "heal_ready", "rally_ready")

    def __init__(self, member: discord.Member, stats: dict):
        self.member = member
        self.uid = member.id
        self.name = member.display_name
        self.max_hp = int(stats["hp"])
        self.hp = self.max_hp
        self.atk = int(stats["atk"])
        self.dmg_done = 0
        self.hits = 0
        self.gcd_until = 0.0
        self.atk_ready = 0.0
        self.heal_ready = 0.0
        self.rally_ready = 0.0

    @property
    def alive(self) -> bool:
        return self.hp > 0


class RaidSession:
    def __init__(self, family: dict, tier_key: str, starter: discord.Member):
        self.family = family
        self.fid = family["id"]
        self.tier_key = tier_key
        self.tier = RAID_TIERS[tier_key]
        self.starter = starter
        self.players = {}              # {user_id: RaidPlayer}
        self.state = "lobby"           # lobby → fighting → done
        self.cancelled = False
        self.start_event = asyncio.Event()
        self.wake = asyncio.Event()
        self.message = None
        self.logs = []
        self.boss_hp = 0
        self.boss_max = 0
        self.boss_def = 0
        self.fight_end = 0.0
        self.next_aoe = 0.0
        self.enraged = False
        self.fight_started = False
        self.rally_until = 0.0         # 전투 함성 효과가 끝나는 시각 (공격대 전체에 적용)

    def log(self, text: str):
        self.logs.append(text)
        self.logs = self.logs[-6:]


def raid_lobby_embed(s: RaidSession) -> discord.Embed:
    t = s.tier
    names = "\n".join(f"• {p.member.mention} (HP {p.max_hp:,} / ATK {p.atk:,})" for p in s.players.values()) or "-"
    embed = discord.Embed(
        title=f"[레이드 모집] {t['name']}",
        description=(f"{t['desc']}\n\n"
                     f"**조직:** 「{s.family['name']}」\n"
                     f"**모집 인원:** {len(s.players)}/{RAID_MAX_PLAYERS} (최소 {t['min_players']}명)\n"
                     f"**참가 조건:** 명예 {t['min_honor']:,}pt 이상\n"
                     f"**예상 보상(1인 평균):** `${t['money']:,}` / 명예 `+{t['honor']}pt`\n"
                     f"**{RAID_LOBBY_SEC}초** 안에 `참가`를 눌러 주세요. (시작자가 `바로 시작`도 가능)"),
        color=discord.Color.orange()
    )
    embed.add_field(name="참가자", value=names, inline=False)
    return embed


def raid_fight_embed(s: RaidSession) -> discord.Embed:
    t = s.tier
    now = time.time()
    rage = "**격노 상태!** 광역 공격이 강해지고 더 자주 옵니다!\n" if s.enraged else ""
    aoe_warn = ""
    if s.state == "fighting":
        left = int(s.next_aoe - now)
        aoe_warn = f"**광역 공격까지 {max(left, 0)}초!** 응급처치 준비!\n" if left <= 6 else ""
    rally_line = f"**전투 함성 효과 중** (공격력 +{int(RAID_RALLY_BONUS * 100)}% / <t:{int(s.rally_until)}:R> 종료)\n" if s.state == "fighting" and s.rally_until > now else ""
    embed = discord.Embed(
        title=f"[레이드 전투] {t['name']}",
        description=(f"{rage}{rally_line}"
                     f"보스 `{_bar(s.boss_hp, s.boss_max, 20)}` **{max(0, int(s.boss_hp * 100 / max(1, s.boss_max)))}%**\n"
                     f"종료 <t:{int(s.fight_end)}:R>  ·  다음 광역 <t:{int(s.next_aoe)}:R>\n"
                     f"{aoe_warn}"),
        color=discord.Color.red() if s.enraged else discord.Color.orange()
    )
    lines = []
    for p in s.players.values():
        if not p.alive:
            icon = "[전투불능]"
        else:
            icon = "[전투]"
        lines.append(f"{icon} **{p.name}** `{_bar(p.hp, p.max_hp, 8)}` {int(p.hp * 100 / max(1, p.max_hp))}% · 딜 {p.dmg_done:,}")
    embed.add_field(name="공격대", value="\n".join(lines) or "-", inline=False)
    embed.add_field(name="전황", value="\n".join(s.logs) or "전투 시작!", inline=False)
    embed.set_footer(text=f"공격 {int(RAID_ATTACK_CD_SEC)}초 / 응급처치 {int(RAID_HEAL_CD_SEC)}초 / 전투 함성 {int(RAID_RALLY_CD_SEC)}초 쿨타임")
    return embed


def raid_release(s: RaidSession):
    for uid in list(s.players.keys()):
        if raid_user_map.get(uid) == s.fid:
            raid_user_map.pop(uid, None)
    if active_raids.get(s.fid) is s:
        active_raids.pop(s.fid, None)


async def _raid_edit(s: RaidSession, **kwargs):
    if not s.message:
        return
    try:
        await s.message.edit(**kwargs)
    except discord.HTTPException as e:
        print(f"Raid message edit error: {e}")


# ---------------------------------------------------------------- 모집 화면
class RaidLobbyView(discord.ui.View):
    def __init__(self, session: RaidSession):
        super().__init__(timeout=None)
        self.s = session

    @discord.ui.button(label="참가", style=discord.ButtonStyle.success)
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        s = self.s
        user = interaction.user
        if s.state != "lobby":
            await interaction.response.send_message("이미 모집이 끝났습니다.", ephemeral=True)
            return
        if user.id in s.players:
            await interaction.response.send_message("이미 참가 중입니다.", ephemeral=True)
            return
        if len(s.players) >= RAID_MAX_PLAYERS:
            await interaction.response.send_message("정원이 가득 찼습니다.", ephemeral=True)
            return
        if user.id in raid_user_map:
            await interaction.response.send_message("이미 다른 레이드에 참가 중입니다.", ephemeral=True)
            return
        if not _is_mafia(user) or _is_jailed(user.id):
            await interaction.response.send_message("마피아 조직원만(감옥 수감 중 제외) 참가할 수 있습니다.", ephemeral=True)
            return
        try:
            fam = family_get_by_id(s.fid)
            if not fam or str(user.id) not in (fam.get("member_ids") or []):
                await interaction.response.send_message("이 조직의 조직원만 참가할 수 있습니다.", ephemeral=True)
                return
            info = get_user_data(user.id)
        except Exception as e:
            print(f"Raid join error: {e}")
            await interaction.response.send_message(FAMILY_DB_ERROR_MSG, ephemeral=True)
            return
        if info["honor"] < s.tier["min_honor"]:
            await interaction.response.send_message(f"이 레이드는 명예 **{s.tier['min_honor']:,}pt** 이상만 참가할 수 있습니다.", ephemeral=True)
            return
        if user.id in s.players or s.state != "lobby":   # DB 호출 사이에 상태가 바뀐 경우
            await interaction.response.send_message("참가할 수 없는 상태입니다.", ephemeral=True)
            return
        s.players[user.id] = RaidPlayer(user, compute_combat_stats(info["honor"], info.get("inventory", {})))
        raid_user_map[user.id] = s.fid
        await interaction.response.edit_message(embed=raid_lobby_embed(s), view=self)

    @discord.ui.button(label="바로 시작", style=discord.ButtonStyle.primary)
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        s = self.s
        if interaction.user.id != s.starter.id:
            await interaction.response.send_message("레이드를 연 사람만 시작할 수 있습니다.", ephemeral=True)
            return
        if len(s.players) < s.tier["min_players"]:
            await interaction.response.send_message(f"최소 **{s.tier['min_players']}명**이 필요합니다. (현재 {len(s.players)}명)", ephemeral=True)
            return
        await interaction.response.defer()
        s.start_event.set()

    @discord.ui.button(label="취소", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        s = self.s
        if interaction.user.id != s.starter.id:
            await interaction.response.send_message("레이드를 연 사람만 취소할 수 있습니다.", ephemeral=True)
            return
        await interaction.response.defer()
        s.cancelled = True
        s.start_event.set()


# ---------------------------------------------------------------- 전투 화면 (버튼 3개)
class RaidFightView(discord.ui.View):
    def __init__(self, session: RaidSession):
        super().__init__(timeout=None)
        self.s = session

    async def _prep(self, interaction: discord.Interaction):
        """전투 중인 참가자인지, 살아 있는지, 공통 대기시간이 지났는지 확인"""
        s = self.s
        p = s.players.get(interaction.user.id)
        if s.state != "fighting":
            await interaction.response.send_message("이미 전투가 끝났습니다.", ephemeral=True)
            return None
        if not p:
            await interaction.response.send_message("이 레이드의 참가자가 아닙니다.", ephemeral=True)
            return None
        if not p.alive:
            await interaction.response.send_message("전투불능 상태입니다. 동료의 **응급처치**를 기다리세요!", ephemeral=True)
            return None
        wait = p.gcd_until - time.time()
        if wait > 0:
            await interaction.response.send_message(f"아직 손이 풀리지 않았습니다. ({wait:.1f}초)", ephemeral=True)
            return None
        return p

    @discord.ui.button(label="공격", style=discord.ButtonStyle.danger)
    async def attack(self, interaction: discord.Interaction, button: discord.ui.Button):
        p = await self._prep(interaction)
        if not p: return
        s = self.s
        now = time.time()
        if p.atk_ready > now:
            await interaction.response.send_message(f"공격 쿨타임 {p.atk_ready - now:.1f}초", ephemeral=True)
            return
        mult = 1 + RAID_RALLY_BONUS if now < s.rally_until else 1.0   # 전투 함성 효과
        dmg = max(10, int(p.atk * mult) - s.boss_def + random.randint(-10, 10))
        s.boss_hp = max(0, s.boss_hp - dmg)
        p.dmg_done += dmg
        p.hits += 1
        p.atk_ready = now + RAID_ATTACK_CD_SEC
        p.gcd_until = now + RAID_GCD_SEC
        await interaction.response.defer()
        if s.boss_hp <= 0:
            s.wake.set()

    @discord.ui.button(label="응급처치", style=discord.ButtonStyle.success)
    async def heal(self, interaction: discord.Interaction, button: discord.ui.Button):
        p = await self._prep(interaction)
        if not p: return
        s = self.s
        now = time.time()
        if p.heal_ready > now:
            await interaction.response.send_message(f"응급처치 쿨타임 {p.heal_ready - now:.1f}초", ephemeral=True)
            return
        downed = [x for x in s.players.values() if not x.alive]
        if downed:
            target = random.choice(downed)
            target.hp = max(1, int(target.max_hp * RAID_REVIVE_RATIO))
            s.log(f"{p.name} → {target.name} 부활! (체력 {int(RAID_REVIVE_RATIO * 100)}%)")
        else:
            hurt = [x for x in s.players.values() if x.alive and x.hp < x.max_hp]
            if not hurt:
                await interaction.response.send_message("치료가 필요한 동료가 없습니다.", ephemeral=True)
                return
            for x in s.players.values():
                if x.alive:
                    x.hp = min(x.max_hp, x.hp + int(x.max_hp * RAID_HEAL_RATIO))
            s.log(f"{p.name}의 응급처치! 전원 체력 +{int(RAID_HEAL_RATIO * 100)}%")
        p.heal_ready = now + RAID_HEAL_CD_SEC
        p.gcd_until = now + RAID_GCD_SEC
        await interaction.response.defer()

    @discord.ui.button(label="전투 함성", style=discord.ButtonStyle.primary)
    async def rally(self, interaction: discord.Interaction, button: discord.ui.Button):
        p = await self._prep(interaction)
        if not p: return
        s = self.s
        now = time.time()
        if p.rally_ready > now:
            await interaction.response.send_message(f"전투 함성 쿨타임 {p.rally_ready - now:.1f}초", ephemeral=True)
            return
        s.rally_until = max(s.rally_until, now + RAID_RALLY_DURATION_SEC)
        p.rally_ready = now + RAID_RALLY_CD_SEC
        p.gcd_until = now + RAID_GCD_SEC
        s.log(f"{p.name}의 전투 함성! {int(RAID_RALLY_DURATION_SEC)}초간 공격대 공격력 +{int(RAID_RALLY_BONUS * 100)}%")
        await interaction.response.defer()


# ---------------------------------------------------------------- 진행 / 정산
def raid_do_aoe(s: RaidSession):
    pct = s.tier["aoe_enrage"] if s.enraged else s.tier["aoe"]
    now = time.time()
    downed = []
    for p in s.players.values():
        if not p.alive:
            continue
        dmg = int(p.max_hp * pct * random.uniform(0.9, 1.1))
        p.hp = max(0, p.hp - dmg)
        if p.hp == 0:
            downed.append(p.name)
    text = f"광역 공격! (최대 체력의 약 {int(pct * 100)}%)"
    if downed:
        text += f" {', '.join(downed)} 전투불능!"
    s.log(text)
    s.next_aoe = now + (RAID_AOE_INTERVAL_ENRAGE_SEC if s.enraged else RAID_AOE_INTERVAL_SEC)


def _raid_settle_sync(s: RaidSession, win: bool):
    """보상 지급 + 조직 갱신 (동기 DB 작업이라 to_thread 로 실행). 반환: [(RaidPlayer, money, honor)], total_money"""
    from .economy import grant_materials_sync, roll_material_drops  # 순환 import 방지: 호출 시점에 가져옴
    from .territory import family_stat_inc  # 순환 import 방지: 호출 시점에 가져옴
    t = s.tier
    s.material_drops = {}   # {user_id: {재료: 개수}}  (정산 결과 화면에 표시)
    attackers = [p for p in s.players.values() if p.dmg_done > 0]
    results = []
    total_money = 0
    if attackers:
        total_dmg = sum(p.dmg_done for p in attackers)
        n = len(attackers)
        for p in attackers:
            weight = RAID_BASE_SHARE / n + (1 - RAID_BASE_SHARE) * (p.dmg_done / total_dmg)
            if win:
                money = int(t["money"] * n * weight)
                honor = max(1, int(t["honor"] * n * weight))
            else:
                money = int(t["money"] * n * weight * RAID_LOSE_MONEY_RATIO)
                honor = 0
            results.append((p, money, honor))
            total_money += money
            try:
                update_user_stats(p.uid, money, honor)
            except Exception as e:
                print(f"Raid reward error ({p.uid}): {e}")
            if win:   # 업적 카운터 (레이드 클리어)
                try:
                    incs = {"raid_clears": 1}
                    if t["name"] == RAID_TIERS["king"]["name"]:
                        incs["raid_king_clears"] = 1
                    bump_user_stats_sync(p.uid, incs, pending=True)
                except Exception as e:
                    print(f"Raid achievement error ({p.uid}): {e}")
            if win:   # 제작 재료 (/제작)
                drops = roll_material_drops(t["name"])
                if drops:
                    try:
                        grant_materials_sync(p.uid, drops)
                        s.material_drops[p.uid] = drops
                    except Exception as e:
                        print(f"Raid material error ({p.uid}): {e}")
    if win:
        try:
            fam = family_get_by_id(s.fid)
            if fam:
                family_update(s.fid, {
                    "treasury": fam.get("treasury", 0) + int(total_money * RAID_TREASURY_RETURN),
                    "raid_points": fam.get("raid_points", 0) + t["points"],
                })
        except Exception as e:
            print(f"Raid family update error: {e}")
        family_stat_inc(s.fid, "raid_clears")
    return results, total_money


def _raid_refund_sync(s: RaidSession):
    fam = family_get_by_id(s.fid)
    if fam:
        family_update(s.fid, {"treasury": fam.get("treasury", 0) + s.tier["cost"]})


async def run_raid(s: RaidSession):
    from .economy import format_material_drops  # 순환 import 방지: 호출 시점에 가져옴
    try:
        # ── 1) 모집
        try:
            await asyncio.wait_for(s.start_event.wait(), timeout=RAID_LOBBY_SEC)
        except asyncio.TimeoutError:
            pass

        if s.cancelled or len(s.players) < s.tier["min_players"]:
            s.state = "done"
            try:
                await asyncio.to_thread(_raid_refund_sync, s)
            except Exception as e:
                print(f"Raid refund error: {e}")
            reason = "시작자가 레이드를 취소했습니다." if s.cancelled else f"최소 인원({s.tier['min_players']}명)이 모이지 않았습니다."
            embed = discord.Embed(title="[레이드 취소]", description=f"{reason}\n작전 자금 `${s.tier['cost']:,}` 은(는) 조직 금고로 반환되었습니다.", color=discord.Color.dark_grey())
            await _raid_edit(s, embed=embed, view=None)
            return

        # ── 2) 전투 준비: 보스 체력은 참가자 공격력에 맞춰 자동 계산
        players = list(s.players.values())
        avg_atk = sum(p.atk for p in players) / len(players)
        s.boss_def = int(avg_atk * s.tier["def_ratio"])
        per_hit_total = sum(max(10, p.atk - s.boss_def) for p in players)
        s.boss_max = s.boss_hp = int(per_hit_total * s.tier["hits"])
        s.state = "fighting"
        s.fight_started = True
        now = time.time()
        s.fight_end = now + RAID_DURATION_SEC
        s.next_aoe = now + RAID_AOE_INTERVAL_SEC
        raid_family_cooldowns[s.fid] = datetime.now() + timedelta(seconds=RAID_FAMILY_COOLDOWN_SEC)
        s.log(f"전투 시작! 참가자 {len(players)}명")
        await _raid_edit(s, embed=raid_fight_embed(s), view=RaidFightView(s))

        # ── 3) 전투 루프
        win = False
        reason = ""
        while True:
            try:
                await asyncio.wait_for(s.wake.wait(), timeout=RAID_TICK_SEC)
            except asyncio.TimeoutError:
                pass
            s.wake.clear()
            now = time.time()

            if s.boss_hp <= 0:
                win, reason = True, "보스를 쓰러뜨렸습니다!"
                break
            if not s.enraged and s.boss_hp <= s.boss_max * 0.5:
                s.enraged = True
                s.next_aoe = min(s.next_aoe, now + RAID_AOE_INTERVAL_ENRAGE_SEC)
                s.log("보스가 격노했습니다! 광역 공격이 강해지고 더 자주 옵니다!")
            if now >= s.next_aoe:
                raid_do_aoe(s)
            if not any(p.alive for p in s.players.values()):
                reason = "공격대가 전멸했습니다..."
                break
            if now >= s.fight_end:
                reason = "제한 시간이 지났습니다..."
                break
            await _raid_edit(s, embed=raid_fight_embed(s))

        s.state = "done"

        # ── 4) 정산
        results, total_money = await asyncio.to_thread(_raid_settle_sync, s, win)
        if win:
            title, color = "[레이드 성공]", discord.Color.gold()
        else:
            title, color = "[레이드 실패]", discord.Color.dark_red()
        lines = []
        for p, money, honor in sorted(results, key=lambda r: -r[0].dmg_done):
            _mats = getattr(s, "material_drops", {}).get(p.uid)
            lines.append(f"• **{p.name}** — 딜 {p.dmg_done:,} ({p.hits}회) → `${money:,}`" + (f" / 명예 `+{honor}pt`" if honor else "")
                         + (f"\n  └ 재료: {format_material_drops(_mats)}" if _mats else ""))
        if not lines:
            lines.append("공격에 참여한 사람이 없어 보상이 없습니다.")
        desc = f"{reason}\n\n" + "\n".join(lines)
        if win:
            desc += (f"\n\n조직 금고 `+${int(total_money * RAID_TREASURY_RETURN):,}` / 레이드 점수 `+{s.tier['points']}`"
                     f"\n**MVP:** {max(results, key=lambda r: r[0].dmg_done)[0].member.mention}" if results else "")
        else:
            desc += f"\n\n(실패해도 참전자에게는 소정의 위로금이 지급됩니다.)"
        embed = discord.Embed(title=f"{title} {s.tier['name']}", description=desc, color=color)
        await _raid_edit(s, embed=embed, view=None)
    except Exception as e:
        print(f"Raid error: {e}")
        if not s.fight_started:   # 전투 시작 전에 오류가 났다면 작전비 반환
            try:
                await asyncio.to_thread(_raid_refund_sync, s)
            except Exception:
                pass
        s.state = "done"
        await _raid_edit(s, embed=discord.Embed(title="[레이드 중단]", description="처리 중 오류가 발생해 레이드가 중단되었습니다.", color=discord.Color.red()), view=None)
    finally:
        s.state = "done"
        raid_release(s)


raid_group = app_commands.Group(name="레이드", description="[패밀리] 조직원들과 함께 레이드 보스에 도전합니다.")
bot.tree.add_command(raid_group)

@raid_group.command(name="시작", description="우리 조직의 레이드를 엽니다. (조직 금고에서 작전 자금이 소모됩니다)")
@app_commands.describe(보스="도전할 레이드 보스")
@app_commands.choices(보스=[
    app_commands.Choice(name=f"{t['name']} (인원 {t['min_players']}+ · 명예 {t['min_honor']:,}+ · 작전비 ${t['cost']:,})", value=k)
    for k, t in RAID_TIERS.items()
])
async def raid_start_cmd(interaction: discord.Interaction, 보스: app_commands.Choice[str]):
    if not await _family_precheck(interaction): return
    user = interaction.user
    tier = RAID_TIERS[보스.value]

    try:
        fam = family_get_by_user(interaction.guild_id, user.id)
        if not fam:
            await interaction.response.send_message("소속된 조직이 없습니다. `/조직 생성` 또는 초대를 받아 보세요.", ephemeral=True)
            return
        if fam["id"] in active_raids:
            await interaction.response.send_message("우리 조직의 레이드가 이미 진행 중입니다.", ephemeral=True)
            return
        if user.id in raid_user_map:
            await interaction.response.send_message("이미 레이드에 참가 중입니다.", ephemeral=True)
            return
        cd = raid_family_cooldowns.get(fam["id"])
        if cd and datetime.now() < cd:
            remain = int((cd - datetime.now()).total_seconds())
            await interaction.response.send_message(f"조직이 정비 중입니다. **{remain // 60}분 {remain % 60}초** 뒤에 다시 레이드를 열 수 있습니다.", ephemeral=True)
            return
        if len(fam.get("member_ids") or []) < tier["min_players"]:
            await interaction.response.send_message(f"이 레이드는 조직원이 최소 **{tier['min_players']}명** 이상이어야 합니다.", ephemeral=True)
            return
        if fam.get("treasury", 0) < tier["cost"]:
            await interaction.response.send_message(f"조직 금고가 부족합니다. (작전비 `${tier['cost']:,}` / 금고 `${fam.get('treasury', 0):,}`)\n`/조직 기부` 로 금고를 채워 보세요.", ephemeral=True)
            return
        info = get_user_data(user.id)
        if info["honor"] < tier["min_honor"]:
            await interaction.response.send_message(f"이 레이드는 명예 **{tier['min_honor']:,}pt** 이상만 도전할 수 있습니다.", ephemeral=True)
            return

        # 자리 선점 (여기부터 await 전까지는 다른 명령어가 끼어들 수 없음)
        session = RaidSession(fam, 보스.value, user)
        session.players[user.id] = RaidPlayer(user, compute_combat_stats(info["honor"], info.get("inventory", {})))
        active_raids[fam["id"]] = session
        raid_user_map[user.id] = fam["id"]
        try:
            family_update(fam["id"], {"treasury": fam["treasury"] - tier["cost"]})
        except Exception:
            raid_release(session)
            raise
    except Exception as e:
        print(f"Raid start error: {e}")
        await interaction.response.send_message(FAMILY_DB_ERROR_MSG, ephemeral=True)
        return

    await interaction.response.send_message(embed=raid_lobby_embed(session), view=RaidLobbyView(session))
    session.message = await interaction.original_response()
    keep_task(asyncio.create_task(run_raid(session)))

@raid_group.command(name="정보", description="레이드 보스 목록과 진행 방법을 확인합니다.")
async def raid_info_cmd(interaction: discord.Interaction):
    if not await check_maintenance(interaction): return
    lines = []
    for t in RAID_TIERS.values():
        lines.append(
            f"**{t['name']}**\n"
            f"└ 인원 {t['min_players']}명+ · 명예 {t['min_honor']:,}pt+ · 작전비 `${t['cost']:,}` (조직 금고)\n"
            f"└ 보상(1인 평균) `${t['money']:,}` / 명예 `+{t['honor']}pt` · 조직 점수 `+{t['points']}`"
        )
    embed = discord.Embed(
        title="조직 레이드 안내",
        description=("\n\n".join(lines) + "\n\n"
                     f"**진행:** `/레이드 시작` → 조직원이 `참가` ({RAID_LOBBY_SEC}초) → 전투 {RAID_DURATION_SEC // 60}분\n"
                     "**공격**: 보스 체력을 깎습니다.\n"
                     "**응급처치**: 쓰러진 동료를 되살리거나, 없으면 전원 체력을 회복합니다.\n"
                     f"**전투 함성**: {int(RAID_RALLY_DURATION_SEC)}초 동안 공격대 전원의 공격력 +{int(RAID_RALLY_BONUS * 100)}%! (쿨타임 {int(RAID_RALLY_CD_SEC)}초, 여러 명이 번갈아 쓰면 효과를 계속 유지할 수 있어요)\n"
                     "보스는 체력이 절반이 되면 **격노**하고, 시간 안에 못 잡거나 전멸하면 실패합니다.\n"
                     f"보상은 {int(RAID_BASE_SHARE * 100)}% 균등 + {100 - int(RAID_BASE_SHARE * 100)}% 딜량 비례이며, 한 번도 공격하지 않으면 받을 수 없습니다.\n"
                     f"조직 레이드 재도전 대기시간은 {RAID_FAMILY_COOLDOWN_SEC // 60}분입니다."),
        color=discord.Color.orange()
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)
