"""구역 점령전 (/구역 ...)"""

import asyncio
import discord
import random
import time
from discord import app_commands

from .core import bot, inc_money, report_error, supabase, keep_task
from .status import (
    LOCATIONS,
    _check_updated,
    active_patrols,
    check_jail,
    compute_combat_stats,
    get_user_data,
    update_user_stats,
)
from .achievements import bump_user_stats_sync
from .admin import check_maintenance, check_membership
from .police_shop import POLICE_TERMINAL_COST_DISCOUNT, POLICE_TERMINAL_NAME, POLICE_TERMINAL_PRIZE_BONUS
from .family import (
    FAMILY_DB_ERROR_MSG,
    _bar,
    _family_precheck,
    _is_jailed,
    _is_mafia,
    active_raids,
    family_get_by_id,
    family_get_by_user,
    family_update,
    raid_user_map,
)

# ==========================================================================
# [구역 점령전]  /구역 현황 · 점령 · 선전포고 · 수금 · 포기
#
#   • 5개 지역(LOCATIONS)을 조직이 차지하면 시간당 수입이 쌓이고, /구역 수금 으로 조직 금고에 넣습니다.
#   • 주인 없는 구역  : /구역 점령  → 조직 금고에서 점령 비용을 내고 즉시 차지
#   • 주인 있는 구역  : /구역 선전포고 → 모집(공격/수비 참전) → 실시간 점령전 → 승리 측이 구역 차지
#       - 수비 조직원이 한 명도 참전하지 않으면 NPC 수비대가 구역을 지킵니다.
#       - 점령에 성공하면 TERRITORY_PROTECT_SEC 동안 보호(공격 불가), 수비에 성공해도 짧게 보호됩니다.
#       - 경찰이 /순찰 중인 지역에서는 점령/선전포고를 할 수 없습니다.
#   • 경찰의 /구역 탈환 : 조직원이 점령한 구역을 치안국이 되찾는 작전 (공격=경찰 / 수비=구역 주인 조직원, 같은 점령전 방식)
#       - 탈환에 성공하면 구역이 압수되어 주인 없는 구역이 됩니다. 비용·인원·보상은 RECAPTURE_* 상수로 조절합니다.
#   • 구역 소유 정보는 Supabase `territories` 테이블(territory.sql)에 저장되어 봇을 재시작해도 유지됩니다.
#     (진행 중인 점령전 자체는 레이드처럼 메모리에만 있어서, 재시작되면 사라집니다.)
#   • 밸런스는 아래 상수만 바꾸면 됩니다.
# ==========================================================================
# --- 구역 설정 (income: 시간당 수입 $ / claim: 주인 없는 구역 점령 비용 $) ---
TERRITORY_INFO = {
    "레벤하임": {"income": inc_money(600),  "claim": 5000},
    "던베르크": {"income": inc_money(700),  "claim": 6000},
    "블랙워드": {"income": inc_money(900),  "claim": 8000},
    "에르느코항": {"income": inc_money(900),  "claim": 8000},
    "로젠펠트": {"income": inc_money(1000), "claim": 10000},
}
TERRITORY_DEFAULT = {"income": inc_money(700), "claim": 6000}   # TERRITORY_INFO 에 없는 지역의 기본값
TERRITORY_MAX_PER_FAMILY = 2        # 한 조직이 가질 수 있는 최대 구역 수 (눈덩이 방지)
TERRITORY_INCOME_CAP_HOURS = 12     # 수금을 안 해도 쌓이는 최대 시간 (그 이상은 사라짐)
TERRITORY_PROTECT_SEC = 7200        # 점령 성공 후 보호 시간 (2시간)
TERRITORY_DEFEND_PROTECT_SEC = 1800 # 수비 성공 후 추가 보호 시간 (30분)

# --- 점령전 설정 ---
WAR_COST = 2000                     # 선전포고 비용 (조직 금고 / 모집 실패·취소 시 환불)
WAR_LOBBY_SEC = 120                 # 참전 모집 시간
WAR_DURATION_SEC = 300              # 전투 제한 시간 (5분)
WAR_TICK_SEC = 4                    # 화면 갱신 / NPC 수비대 공격 주기
WAR_MIN_ATTACKERS = 2               # 공격 최소 인원
WAR_MAX_PER_SIDE = 6                # 한쪽 최대 인원
WAR_GCD_SEC = 2.0                   # 모든 행동 공통 대기시간
WAR_ATK_CD_SEC = 4.0                # 공격 대기시간
WAR_AMBUSH_CD_SEC = 15.0            # 기습 대기시간
WAR_AMBUSH_MULT = 2.2               # 기습 성공 시 피해 배율
WAR_AMBUSH_FAIL = 0.25              # 기습 실패 확률
WAR_DEFENDER_BONUS = 1.1            # 수비 측 피해 배율 (지형 이점)
WAR_GARRISON_HP_RATIO = 1.6         # NPC 수비대 체력 = 공격대 총 체력 × 이 값
WAR_GARRISON_ATK_RATIO = 0.6        # NPC 수비대가 틱마다 주는 피해 = 공격대 총 공격력 × 이 값
WAR_FAMILY_COOLDOWN_SEC = 1800      # 선전포고한 조직의 재선포 대기시간 (30분)
WAR_PRIZE_HOURS = 4                 # 전리품 = 구역 시간당 수입 × 이 값 (승리 측이 나눠 가짐)
WAR_BASE_SHARE = 0.6                # 전리품 중 '균등 분배' 비율 (나머지는 딜량 비례)
WAR_LOSE_MONEY_RATIO = 0.15         # 패배 측 참전자(공격 1회 이상)에게 나눠주는 위로금 비율
WAR_WIN_HONOR = 20                  # 승리 측 명예 (공격 1회 이상 한 사람만)

TERRITORY_DB_ERROR_MSG = "구역 DB 처리 중 오류가 발생했습니다. (관리자: `territories` 테이블이 만들어져 있는지 확인해 주세요 — territory.sql 참고)"

active_wars = {}             # {(guild_id, location): WarSession}
war_family_map = {}          # {str(family_id): WarSession}  (공격·수비 양쪽 조직)
war_user_map = {}            # {user_id: WarSession}
war_family_cooldowns = {}    # {str(family_id): epoch}

# --- 경찰의 구역 탈환 (조직원이 점령한 구역을 치안국이 되찾는 작전. 점령전 엔진을 그대로 씁니다) ---
RECAPTURE_COST = 3000               # 탈환 비용 (작전 책임자 개인 자금 / 모집 실패·취소 시 환불)
RECAPTURE_MIN_ATTACKERS = 2         # 경찰 최소 인원
RECAPTURE_COOLDOWN_SEC = 1800       # 탈환 작전 재신청 대기시간 (작전 책임자 기준)
RECAPTURE_WIN_HONOR = 30            # 탈환 성공 시 경찰 명예 (공격 1회 이상 한 사람만)
recapture_cooldowns = {}            # {user_id: epoch}


def _terr_info(loc: str) -> dict:
    return TERRITORY_INFO.get(loc, TERRITORY_DEFAULT)

def _fmt_sec(sec: float) -> str:
    sec = max(0, int(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}시간 {m}분"
    if m:
        return f"{m}분 {s}초"
    return f"{s}초"

def _patrolled(guild_id: int, loc: str) -> bool:
    return active_patrols.get(guild_id, {}).get(loc, 0) > time.time()


# ---------------------------------------------------------------- 구역 DB 도우미 (동기)
def terr_list(guild_id: int) -> dict:
    res = supabase.table("territories").select("*").eq("guild_id", str(guild_id)).execute()
    return {r["location"]: r for r in (res.data or [])}

def terr_get(guild_id: int, loc: str):
    res = (supabase.table("territories").select("*")
           .eq("guild_id", str(guild_id)).eq("location", loc).limit(1).execute())
    return res.data[0] if res.data else None

def terr_list_by_family(fid) -> list:
    res = supabase.table("territories").select("*").eq("family_id", str(fid)).execute()
    return res.data or []

def terr_insert(guild_id: int, loc: str, fid, now: float):
    row = {"guild_id": str(guild_id), "location": loc, "family_id": str(fid),
           "collected_at": int(now), "protected_until": int(now + TERRITORY_PROTECT_SEC)}
    res = supabase.table("territories").insert(row).execute()   # (guild_id, location) 유니크 → 동시 선점 시 예외
    return res.data[0] if res.data else row

def terr_update(row_id, data: dict):
    res = supabase.table("territories").update(data).eq("id", row_id).execute()
    _check_updated(res, "terr_update")

def terr_delete(row_id):
    supabase.table("territories").delete().eq("id", row_id).execute()

def terr_accrued(row: dict, now: float = None) -> int:
    """마지막 수금 이후 쌓인 수입 (최대 TERRITORY_INCOME_CAP_HOURS 시간분)"""
    now = now or time.time()
    hours = min(TERRITORY_INCOME_CAP_HOURS, max(0.0, now - float(row.get("collected_at") or 0)) / 3600)
    return int(hours * _terr_info(row["location"])["income"])


# ---------------------------------------------------------------- 점령전 세션
class WarPlayer:
    __slots__ = ("member", "uid", "name", "side", "max_hp", "atk", "dmg_done", "hits",
                 "gcd_until", "atk_ready", "ambush_ready")

    def __init__(self, member: discord.Member, side: str, stats: dict):
        self.member = member
        self.uid = member.id
        self.name = member.display_name
        self.side = side                  # "A" 공격 / "D" 수비
        self.max_hp = int(stats["hp"])
        self.atk = int(stats["atk"])
        self.dmg_done = 0
        self.hits = 0
        self.gcd_until = 0.0
        self.atk_ready = 0.0
        self.ambush_ready = 0.0


class WarSession:
    def __init__(self, guild_id: int, location: str, atk_fam: dict, def_fam: dict, starter: discord.Member,
                 recapture: bool = False, cost: int = 0):
        self.recapture = recapture        # True: 경찰이 공격 측인 '구역 탈환 작전'
        self.cost = cost if recapture else WAR_COST
        self.min_atk = RECAPTURE_MIN_ATTACKERS if recapture else WAR_MIN_ATTACKERS
        self.label = "탈환전" if recapture else "점령전"
        self.guild_id = guild_id
        self.location = location
        self.aid = atk_fam["id"]
        self.did = def_fam["id"]
        self.atk_name = atk_fam["name"]
        self.def_name = def_fam["name"]
        self.def_owner_id = def_fam.get("owner_id")
        self.starter = starter
        self.prize = _terr_info(location)["income"] * WAR_PRIZE_HOURS
        self.players = {}                 # {user_id: WarPlayer}
        self.state = "lobby"              # lobby → fighting → done
        self.cancelled = False
        self.start_event = asyncio.Event()
        self.wake = asyncio.Event()
        self.message = None
        self.logs = []
        self.pool = {"A": 0, "D": 0}
        self.pool_max = {"A": 1, "D": 1}
        self.npc = False                  # 수비 조직원이 없어서 NPC 수비대가 방어 중인지
        self.npc_atk = 0
        self.fight_end = 0.0
        self.fight_started = False

    def log(self, text: str):
        self.logs.append(text)
        self.logs = self.logs[-6:]

    def side_players(self, side: str) -> list:
        return [p for p in self.players.values() if p.side == side]


def war_release(s: WarSession):
    for uid in list(s.players.keys()):
        if war_user_map.get(uid) is s:
            war_user_map.pop(uid, None)
    for fid in (str(s.aid), str(s.did)):
        if war_family_map.get(fid) is s:
            war_family_map.pop(fid, None)
    key = (s.guild_id, s.location)
    if active_wars.get(key) is s:
        active_wars.pop(key, None)


async def _war_edit(s: WarSession, **kwargs):
    if not s.message:
        return
    try:
        await s.message.edit(**kwargs)
    except discord.HTTPException as e:
        print(f"War message edit error: {e}")


def war_lobby_embed(s: WarSession) -> discord.Embed:
    def fmt(ps):
        return "\n".join(f"• {p.member.mention} (HP {p.max_hp:,} / ATK {p.atk:,})" for p in ps) or "-"
    atk, dfn = s.side_players("A"), s.side_players("D")
    embed = discord.Embed(
        title=(f"[구역 탈환 작전] {s.location}" if s.recapture else f"[구역 선전포고] {s.location}"),
        description=((f"**치안국** 이(가) **「{s.def_name}」** 이(가) 점령한 구역을 되찾으려 합니다!\n"
                      f"경찰은 `공격 참전`, 해당 조직의 조직원은 `수비 참전` 을 눌러 주세요.\n\n" if s.recapture
                      else f"**「{s.atk_name}」** 이(가) **「{s.def_name}」** 의 구역을 노립니다!\n\n") +
                     f"**전리품(승리 측 분배):** `${s.prize:,}` / 명예 `+{RECAPTURE_WIN_HONOR if s.recapture else WAR_WIN_HONOR}pt`\n"
                     + (f"**탈환에 성공하면 구역이 압수되어 주인 없는 구역이 됩니다.**\n" if s.recapture else "") +
                     f"**공격 최소 인원:** {s.min_atk}명 · 한쪽 최대 {WAR_MAX_PER_SIDE}명\n"
                     f"**{WAR_LOBBY_SEC}초** 안에 참전해 주세요. 수비 조직원이 아무도 오지 않으면 **NPC 수비대**가 구역을 지킵니다."),
        color=discord.Color.dark_red()
    )
    embed.add_field(name=f"공격 「{s.atk_name}」 ({len(atk)}/{WAR_MAX_PER_SIDE})", value=fmt(atk), inline=True)
    embed.add_field(name=f"수비 「{s.def_name}」 ({len(dfn)}/{WAR_MAX_PER_SIDE})", value=fmt(dfn), inline=True)
    return embed


def war_fight_embed(s: WarSession) -> discord.Embed:
    def pct(side):
        return max(0, int(s.pool[side] * 100 / max(1, s.pool_max[side])))
    dname = "NPC 수비대" if s.npc else f"「{s.def_name}」"
    embed = discord.Embed(
        title=f"[구역 {s.label}] {s.location}",
        description=(f"공격 「{s.atk_name}」\n`{_bar(s.pool['A'], s.pool_max['A'], 16)}` **{pct('A')}%**\n"
                     f"수비 {dname}\n`{_bar(s.pool['D'], s.pool_max['D'], 16)}` **{pct('D')}%**\n"
                     f"종료 <t:{int(s.fight_end)}:R> (시간 종료 시 남은 병력 비율이 높은 쪽이 승리, 동률이면 수비 승리)"),
        color=discord.Color.red()
    )
    def lines(ps):
        return "\n".join(f"**{p.name}** · 딜 {p.dmg_done:,} ({p.hits}회)" for p in ps) or "-"
    embed.add_field(name="공격대", value=lines(s.side_players("A")), inline=True)
    embed.add_field(name="수비대", value=("NPC 수비대 (자동 반격)" if s.npc else lines(s.side_players("D"))), inline=True)
    embed.add_field(name="전황", value="\n".join(s.logs) or "전투 시작!", inline=False)
    embed.set_footer(text=f"공격 {int(WAR_ATK_CD_SEC)}초 / 기습 {int(WAR_AMBUSH_CD_SEC)}초 쿨타임 (기습은 {int(WAR_AMBUSH_FAIL * 100)}% 확률로 빗나감)")
    return embed


# ---------------------------------------------------------------- 모집 화면
class WarLobbyView(discord.ui.View):
    def __init__(self, session: WarSession):
        super().__init__(timeout=None)
        self.s = session

    async def _join(self, interaction: discord.Interaction, side: str):
        from .police_unit import _is_police   # 순환 import 방지: 호출 시점에 가져옴
        s = self.s
        user = interaction.user
        if s.state != "lobby":
            await interaction.response.send_message("이미 모집이 끝났습니다.", ephemeral=True)
            return
        if user.id in s.players:
            await interaction.response.send_message("이미 참전 중입니다.", ephemeral=True)
            return
        if user.id in war_user_map or user.id in raid_user_map:
            await interaction.response.send_message("이미 다른 점령전/레이드에 참가 중입니다.", ephemeral=True)
            return
        police_side = s.recapture and side == "A"   # 탈환 작전의 공격 측은 경찰
        if police_side:
            if not _is_police(user) or _is_jailed(user.id):
                await interaction.response.send_message("탈환 작전의 공격은 경찰만(감옥 수감 중 제외) 참전할 수 있습니다.", ephemeral=True)
                return
        elif not _is_mafia(user) or _is_jailed(user.id):
            await interaction.response.send_message("마피아 조직원만(감옥 수감 중 제외) 참전할 수 있습니다.", ephemeral=True)
            return
        if len(s.side_players(side)) >= WAR_MAX_PER_SIDE:
            await interaction.response.send_message("그쪽 정원이 가득 찼습니다.", ephemeral=True)
            return
        fid = s.aid if side == "A" else s.did
        try:
            if not police_side:
                fam = family_get_by_id(fid)
                if not fam or str(user.id) not in (fam.get("member_ids") or []):
                    who = "공격" if side == "A" else "수비"
                    await interaction.response.send_message(f"**{who} 조직**의 조직원만 이 버튼으로 참전할 수 있습니다.", ephemeral=True)
                    return
            info = get_user_data(user.id)
        except Exception as e:
            print(f"War join error: {e}")
            await interaction.response.send_message(FAMILY_DB_ERROR_MSG, ephemeral=True)
            return
        if user.id in s.players or s.state != "lobby" or user.id in war_user_map:   # DB 호출 사이에 상태가 바뀐 경우
            await interaction.response.send_message("참전할 수 없는 상태입니다.", ephemeral=True)
            return
        s.players[user.id] = WarPlayer(user, side, compute_combat_stats(info["honor"], info.get("inventory", {})))
        war_user_map[user.id] = s
        await interaction.response.edit_message(embed=war_lobby_embed(s), view=self)

    @discord.ui.button(label="공격 참전", style=discord.ButtonStyle.danger)
    async def join_atk(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._join(interaction, "A")

    @discord.ui.button(label="수비 참전", style=discord.ButtonStyle.primary)
    async def join_def(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._join(interaction, "D")

    @discord.ui.button(label="바로 시작", style=discord.ButtonStyle.success)
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        s = self.s
        if interaction.user.id != s.starter.id:
            await interaction.response.send_message("선전포고한 사람만 시작할 수 있습니다.", ephemeral=True)
            return
        n = len(s.side_players("A"))
        if n < s.min_atk:
            await interaction.response.send_message(f"공격 인원이 최소 **{s.min_atk}명** 필요합니다. (현재 {n}명)", ephemeral=True)
            return
        await interaction.response.defer()
        s.start_event.set()

    @discord.ui.button(label="취소", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        s = self.s
        if interaction.user.id != s.starter.id:
            await interaction.response.send_message("선전포고한 사람만 취소할 수 있습니다.", ephemeral=True)
            return
        await interaction.response.defer()
        s.cancelled = True
        s.start_event.set()


# ---------------------------------------------------------------- 전투 화면 (버튼 2개)
def war_apply_hit(s: WarSession, p: WarPlayer, dmg: int):
    target = "D" if p.side == "A" else "A"
    s.pool[target] = max(0, s.pool[target] - dmg)
    p.dmg_done += dmg
    p.hits += 1
    if s.pool[target] <= 0:
        s.wake.set()


class WarFightView(discord.ui.View):
    def __init__(self, session: WarSession):
        super().__init__(timeout=None)
        self.s = session

    async def _prep(self, interaction: discord.Interaction):
        s = self.s
        p = s.players.get(interaction.user.id)
        if s.state != "fighting":
            await interaction.response.send_message("이미 전투가 끝났습니다.", ephemeral=True)
            return None
        if not p:
            await interaction.response.send_message("이 점령전의 참전자가 아닙니다.", ephemeral=True)
            return None
        wait = p.gcd_until - time.time()
        if wait > 0:
            await interaction.response.send_message(f"아직 손이 풀리지 않았습니다. ({wait:.1f}초)", ephemeral=True)
            return None
        return p

    def _roll(self, p: WarPlayer, mult: float = 1.0) -> int:
        dmg = int(p.atk * random.uniform(0.9, 1.1) * mult)
        if p.side == "D":
            dmg = int(dmg * WAR_DEFENDER_BONUS)
        return max(10, dmg)

    @discord.ui.button(label="공격", style=discord.ButtonStyle.danger)
    async def attack(self, interaction: discord.Interaction, button: discord.ui.Button):
        p = await self._prep(interaction)
        if not p: return
        now = time.time()
        if p.atk_ready > now:
            await interaction.response.send_message(f"공격 쿨타임 {p.atk_ready - now:.1f}초", ephemeral=True)
            return
        war_apply_hit(self.s, p, self._roll(p))
        p.atk_ready = now + WAR_ATK_CD_SEC
        p.gcd_until = now + WAR_GCD_SEC
        await interaction.response.defer()

    @discord.ui.button(label="기습", style=discord.ButtonStyle.primary)
    async def ambush(self, interaction: discord.Interaction, button: discord.ui.Button):
        p = await self._prep(interaction)
        if not p: return
        s = self.s
        now = time.time()
        if p.ambush_ready > now:
            await interaction.response.send_message(f"기습 쿨타임 {p.ambush_ready - now:.1f}초", ephemeral=True)
            return
        p.ambush_ready = now + WAR_AMBUSH_CD_SEC
        p.gcd_until = now + WAR_GCD_SEC
        if random.random() < WAR_AMBUSH_FAIL:
            s.log(f"{p.name}의 기습이 들통났습니다! (빗나감)")
        else:
            dmg = self._roll(p, WAR_AMBUSH_MULT)
            war_apply_hit(s, p, dmg)
            s.log(f"{p.name}의 기습 성공! `{dmg:,}` 피해")
        await interaction.response.defer()


# ---------------------------------------------------------------- 진행 / 정산
def _war_refund_sync(s: WarSession):
    if s.recapture:   # 탈환 비용은 작전 책임자 개인에게 돌려준다
        update_user_stats(s.starter.id, s.cost, 0)
        return
    fam = family_get_by_id(s.aid)
    if fam:
        family_update(s.aid, {"treasury": fam.get("treasury", 0) + WAR_COST})


def _war_still_valid_sync(s: WarSession) -> bool:
    """모집 중 구역 주인이 바뀌었거나(해산 등) 공격 측이 보유 한도에 도달했는지 확인"""
    row = terr_get(s.guild_id, s.location)
    if not row or row["family_id"] != str(s.did):
        return False
    if s.recapture:
        return True   # 경찰은 구역 보유 한도가 없다
    return len(terr_list_by_family(s.aid)) < TERRITORY_MAX_PER_FAMILY


def _war_settle_sync(s: WarSession, atk_win: bool):
    from .ranking import family_stat_inc   # 순환 import 방지: 호출 시점에 가져옴
    now = time.time()
    winners = [p for p in s.players.values() if (p.side == "A") == atk_win]
    losers = [p for p in s.players.values() if (p.side == "A") != atk_win]
    results = []   # (player, money, honor, won)

    win_dmg = sum(p.dmg_done for p in winners)
    for p in winners:
        share = (p.dmg_done / win_dmg) if win_dmg > 0 else (1 / max(1, len(winners)))
        money = int(s.prize * (WAR_BASE_SHARE / max(1, len(winners)) + (1 - WAR_BASE_SHARE) * share))
        police_atk = s.recapture and p.side == "A"
        honor = (RECAPTURE_WIN_HONOR if police_atk else WAR_WIN_HONOR) if p.hits > 0 else 0
        if p.hits <= 0:
            money = 0   # 한 번도 공격하지 않으면 받을 수 없음
        elif police_atk:   # 광역 수사망 단말기: 탈환 포상금 +25%
            try:
                if POLICE_TERMINAL_NAME in (get_user_data(p.uid).get("inventory") or {}):
                    money = int(money * (1 + POLICE_TERMINAL_PRIZE_BONUS))
            except Exception as e:
                print(f"Recapture bonus error: {e}")
        try:
            update_user_stats(p.uid, money, honor)
        except Exception as e:
            print(f"War reward error: {e}")
        results.append((p, money, honor, True))
        if p.hits > 0 and not (s.recapture and p.side == "A"):   # 업적 카운터 (구역 점령전 승리 / 공격 측 승리 = 구역 점령)
            try:
                incs = {"war_wins": 1}
                if atk_win:
                    incs["war_conquests"] = 1
                bump_user_stats_sync(p.uid, incs, pending=True)
            except Exception as e:
                print(f"War achievement error ({p.uid}): {e}")

    for p in losers:
        money = int(s.prize * WAR_LOSE_MONEY_RATIO / max(1, len(losers))) if p.hits > 0 else 0
        if money:
            try:
                update_user_stats(p.uid, money, 0)
            except Exception as e:
                print(f"War consolation error: {e}")
        results.append((p, money, 0, False))

    note = ""
    row = terr_get(s.guild_id, s.location)
    if row and row["family_id"] == str(s.did):
        if atk_win and s.recapture:
            terr_delete(row["id"])   # 경찰의 탈환 성공: 구역 압수 (미수금도 몰수)
            note = (f"**{s.location}** 의 구역이 치안국에 **압수**되어 주인 없는 구역이 되었습니다! "
                    f"(쌓여 있던 미수금도 몰수) `/구역 점령` 으로 다시 차지할 수 있습니다.")
        elif atk_win:
            # 수비 조직이 아직 못 거둔 수입은 금고로 정산해 준다
            accrued = terr_accrued(row, now)
            try:
                dfam = family_get_by_id(s.did)
                if dfam and accrued > 0:
                    family_update(s.did, {"treasury": dfam.get("treasury", 0) + accrued})
            except Exception as e:
                print(f"War accrued payout error: {e}")
            if len(terr_list_by_family(s.aid)) >= TERRITORY_MAX_PER_FAMILY:
                terr_delete(row["id"])
                note = f"공격 조직이 보유 한도({TERRITORY_MAX_PER_FAMILY}곳)에 도달해 구역이 **무주공산**이 되었습니다."
            else:
                terr_update(row["id"], {"family_id": str(s.aid), "collected_at": int(now),
                                        "protected_until": int(now + TERRITORY_PROTECT_SEC)})
                note = f"**{s.location}** 은(는) 이제 **「{s.atk_name}」** 의 구역입니다! ({_fmt_sec(TERRITORY_PROTECT_SEC)} 동안 보호)"
        else:
            terr_update(row["id"], {"protected_until": int(max(float(row.get("protected_until") or 0), now + TERRITORY_DEFEND_PROTECT_SEC))})
            note = f"**「{s.def_name}」** 이(가) 구역을 지켜냈습니다!"
    else:
        note = "구역 주인이 바뀌어 점령 결과가 반영되지 않았습니다."
    if s.recapture:   # 치안국은 조직이 아니므로 수비 조직의 기록만 갱신
        family_stat_inc(s.did, "war_losses" if atk_win else "war_wins")
    else:
        win_fid, lose_fid = (s.aid, s.did) if atk_win else (s.did, s.aid)
        family_stat_inc(win_fid, "war_wins")
        family_stat_inc(lose_fid, "war_losses")
    return results, note


async def run_war(s: WarSession):
    try:
        # ── 1) 모집
        try:
            await asyncio.wait_for(s.start_event.wait(), timeout=WAR_LOBBY_SEC)
        except asyncio.TimeoutError:
            pass

        n_atk = len(s.side_players("A"))
        valid = True
        if not s.cancelled and n_atk >= s.min_atk:
            valid = await asyncio.to_thread(_war_still_valid_sync, s)
        if s.cancelled or n_atk < s.min_atk or not valid:
            s.state = "done"
            try:
                await asyncio.to_thread(_war_refund_sync, s)
            except Exception as e:
                print(f"War refund error: {e}")
            if s.cancelled:
                reason = "선전포고한 사람이 점령전을 취소했습니다."
            elif n_atk < s.min_atk:
                reason = f"공격 최소 인원({s.min_atk}명)이 모이지 않았습니다."
            else:
                reason = "구역 상황이 바뀌어(주인 변경/보유 한도) 점령전을 진행할 수 없습니다."
            refund_to = "작전 책임자에게" if s.recapture else "조직 금고로"
            embed = discord.Embed(title=f"[{s.label} 취소]", description=f"{reason}\n{'탈환' if s.recapture else '선전포고'} 비용 `${s.cost:,}` 은(는) {refund_to} 반환되었습니다.", color=discord.Color.dark_grey())
            await _war_edit(s, embed=embed, view=None)
            return

        # ── 2) 전투 준비
        atk_players = s.side_players("A")
        def_players = s.side_players("D")
        s.pool["A"] = s.pool_max["A"] = max(1, sum(p.max_hp for p in atk_players))
        if def_players:
            s.pool["D"] = s.pool_max["D"] = max(1, sum(p.max_hp for p in def_players))
        else:
            s.npc = True
            s.pool["D"] = s.pool_max["D"] = max(1, int(s.pool["A"] * WAR_GARRISON_HP_RATIO))
            s.npc_atk = int(sum(p.atk for p in atk_players) * WAR_GARRISON_ATK_RATIO)
        s.state = "fighting"
        s.fight_started = True
        s.fight_end = time.time() + WAR_DURATION_SEC
        if s.recapture:
            recapture_cooldowns[s.starter.id] = time.time() + RECAPTURE_COOLDOWN_SEC
        else:
            war_family_cooldowns[str(s.aid)] = time.time() + WAR_FAMILY_COOLDOWN_SEC
        s.log(f"점령전 시작! 공격 {len(atk_players)}명 vs " + ("NPC 수비대" if s.npc else f"수비 {len(def_players)}명"))
        await _war_edit(s, embed=war_fight_embed(s), view=WarFightView(s))

        # ── 3) 전투 루프
        atk_win = False
        reason = ""
        while True:
            try:
                await asyncio.wait_for(s.wake.wait(), timeout=WAR_TICK_SEC)
            except asyncio.TimeoutError:
                pass
            s.wake.clear()

            if s.npc and s.pool["D"] > 0 and s.pool["A"] > 0:
                s.pool["A"] = max(0, s.pool["A"] - int(s.npc_atk * random.uniform(0.8, 1.2)))
            if s.pool["D"] <= 0:
                atk_win, reason = True, "수비대가 무너졌습니다!"
                break
            if s.pool["A"] <= 0:
                atk_win, reason = False, "공격대가 전멸했습니다..."
                break
            if time.time() >= s.fight_end:
                ra = s.pool["A"] / s.pool_max["A"]
                rd = s.pool["D"] / s.pool_max["D"]
                atk_win = ra > rd
                reason = "시간이 종료되었습니다. 남은 병력 비율로 승패를 가립니다."
                break
            await _war_edit(s, embed=war_fight_embed(s))

        s.state = "done"

        # ── 4) 정산
        results, note = await asyncio.to_thread(_war_settle_sync, s, atk_win)
        if s.recapture:
            title, color = ("[탈환전 - 치안국 승리]", discord.Color.gold()) if atk_win else ("[탈환전 - 조직 수비 성공]", discord.Color.blue())
        else:
            title, color = ("[점령전 - 공격 측 승리]", discord.Color.gold()) if atk_win else ("[점령전 - 수비 측 승리]", discord.Color.blue())
        lines = []
        for p, money, honor, won in sorted(results, key=lambda r: -r[0].dmg_done):
            side = "공격" if p.side == "A" else "수비"
            tag = "승" if won else "패"
            lines.append(f"• [{side}/{tag}] **{p.name}** — 딜 {p.dmg_done:,} ({p.hits}회) → `${money:,}`" + (f" / 명예 `+{honor}pt`" if honor else ""))
        if not lines:
            lines.append("참전자가 없습니다.")
        embed = discord.Embed(title=f"{title} {s.location}", description=f"{reason}\n\n{note}\n\n" + "\n".join(lines), color=color)
        await _war_edit(s, embed=embed, view=None)
    except Exception as e:
        print(f"War error: {e}")
        if not s.fight_started:   # 전투 시작 전에 오류가 났다면 선전포고 비용 반환
            try:
                await asyncio.to_thread(_war_refund_sync, s)
            except Exception:
                pass
        s.state = "done"
        await _war_edit(s, embed=discord.Embed(title="[점령전 중단]", description="처리 중 오류가 발생해 점령전이 중단되었습니다.", color=discord.Color.red()), view=None)
    finally:
        s.state = "done"
        war_release(s)


# ================================================================ /구역
territory_group = app_commands.Group(name="구역", description="[패밀리/경찰] 구역을 점령하고 수익을 거둡니다. (경찰은 /구역 탈환)")
bot.tree.add_command(territory_group)

_LOCATION_CHOICES = [app_commands.Choice(name=k, value=k) for k in LOCATIONS]


@territory_group.command(name="현황", description="5개 지역의 점령 현황과 수입을 확인합니다.")
async def territory_status_cmd(interaction: discord.Interaction):
    if not await check_maintenance(interaction): return
    if not await check_membership(interaction): return
    guild_id = interaction.guild_id
    now = time.time()
    try:
        owned = terr_list(guild_id)
        names = {}
        for row in owned.values():
            fid = row["family_id"]
            if fid not in names:
                fam = family_get_by_id(fid)
                names[fid] = fam["name"] if fam else "(해산된 조직)"
    except Exception as e:
        print(f"Territory status error: {e}")
        await interaction.response.send_message(TERRITORY_DB_ERROR_MSG, ephemeral=True)
        return

    embed = discord.Embed(title="구역 점령 현황", color=discord.Color.dark_gold())
    for loc in LOCATIONS:
        info = _terr_info(loc)
        row = owned.get(loc)
        flags = []
        if (guild_id, loc) in active_wars:
            flags.append("점령전 진행 중")
        if _patrolled(guild_id, loc):
            flags.append("경찰 순찰 중")
        if row:
            prot = float(row.get("protected_until") or 0) - now
            if prot > 0:
                flags.append(f"보호 {_fmt_sec(prot)} 남음")
            value = (f"**주인:** 「{names.get(row['family_id'], '?')}」\n"
                     f"**수입:** `${info['income']:,}`/시간 (미수금 `${terr_accrued(row, now):,}`)")
        else:
            value = (f"**주인:** 없음 (`/구역 점령` 비용 `${info['claim']:,}`)\n"
                     f"**수입:** `${info['income']:,}`/시간")
        if flags:
            value += "\n" + " · ".join(flags)
        embed.add_field(name=loc, value=value, inline=False)
    embed.set_footer(text=f"조직당 최대 {TERRITORY_MAX_PER_FAMILY}곳 · 미수금은 최대 {TERRITORY_INCOME_CAP_HOURS}시간분까지 쌓입니다.")
    await interaction.response.send_message(embed=embed)


@territory_group.command(name="점령", description="[조직장] 주인 없는 구역을 조직 금고로 점령합니다.")
@app_commands.describe(지역="점령할 지역")
@app_commands.choices(지역=_LOCATION_CHOICES)
async def territory_claim_cmd(interaction: discord.Interaction, 지역: app_commands.Choice[str]):
    if not await _family_precheck(interaction): return
    user = interaction.user
    loc = 지역.value
    info = _terr_info(loc)
    guild_id = interaction.guild_id
    try:
        fam = family_get_by_user(guild_id, user.id)
        if not fam or fam["owner_id"] != str(user.id):
            await interaction.response.send_message("조직장만 사용할 수 있습니다.", ephemeral=True)
            return
        if _patrolled(guild_id, loc):
            await interaction.response.send_message(f"**{loc}** 은(는) 경찰이 순찰 중이라 지금은 움직일 수 없습니다.", ephemeral=True)
            return
        if str(fam["id"]) in war_family_map:
            await interaction.response.send_message("점령전이 진행 중인 조직은 구역을 점령할 수 없습니다.", ephemeral=True)
            return
        row = terr_get(guild_id, loc)
        if row:
            if row["family_id"] == str(fam["id"]):
                await interaction.response.send_message("이미 우리 조직의 구역입니다.", ephemeral=True)
            else:
                await interaction.response.send_message("이미 다른 조직이 차지한 구역입니다. `/구역 선전포고` 로 빼앗아야 합니다.", ephemeral=True)
            return
        if len(terr_list_by_family(fam["id"])) >= TERRITORY_MAX_PER_FAMILY:
            await interaction.response.send_message(f"조직은 구역을 최대 **{TERRITORY_MAX_PER_FAMILY}곳**까지만 가질 수 있습니다.", ephemeral=True)
            return
        if fam.get("treasury", 0) < info["claim"]:
            await interaction.response.send_message(f"조직 금고가 부족합니다. (점령 비용 `${info['claim']:,}` / 금고 `${fam.get('treasury', 0):,}`)\n`/조직 기부` 로 금고를 채워 보세요.", ephemeral=True)
            return
        family_update(fam["id"], {"treasury": fam["treasury"] - info["claim"]})
        try:
            terr_insert(guild_id, loc, fam["id"], time.time())
        except Exception as e:
            # 동시에 다른 조직이 먼저 차지했거나 DB 오류 → 비용 환불
            print(f"Territory claim insert error: {e}")
            fresh = family_get_by_id(fam["id"])
            family_update(fam["id"], {"treasury": (fresh or fam).get("treasury", 0) + info["claim"]})
            await interaction.response.send_message("점령에 실패했습니다. (다른 조직이 먼저 차지했거나 DB 오류) 비용은 환불되었습니다.", ephemeral=True)
            return
    except Exception as e:
        print(f"Territory claim error: {e}")
        await interaction.response.send_message(TERRITORY_DB_ERROR_MSG, ephemeral=True)
        return
    await interaction.response.send_message(
        f"**「{fam['name']}」** 이(가) **{loc}** 을(를) 점령했습니다! (비용 `${info['claim']:,}` · 시간당 수입 `${info['income']:,}` · {_fmt_sec(TERRITORY_PROTECT_SEC)} 동안 보호)")


@territory_group.command(name="선전포고", description=f"[조직장] 다른 조직의 구역에 점령전을 선포합니다. (조직 금고 ${WAR_COST:,})")
@app_commands.describe(지역="빼앗을 지역")
@app_commands.choices(지역=_LOCATION_CHOICES)
async def territory_war_cmd(interaction: discord.Interaction, 지역: app_commands.Choice[str]):
    if not await _family_precheck(interaction): return
    user = interaction.user
    loc = 지역.value
    guild_id = interaction.guild_id
    try:
        fam = family_get_by_user(guild_id, user.id)
        if not fam or fam["owner_id"] != str(user.id):
            await interaction.response.send_message("조직장만 사용할 수 있습니다.", ephemeral=True)
            return
        fid = str(fam["id"])
        if fid in active_raids or fam["id"] in active_raids:
            await interaction.response.send_message("레이드 진행 중에는 선전포고할 수 없습니다.", ephemeral=True)
            return
        if fid in war_family_map or user.id in war_user_map or user.id in raid_user_map:
            await interaction.response.send_message("이미 점령전(또는 레이드)에 참여 중입니다.", ephemeral=True)
            return
        cd = war_family_cooldowns.get(fid, 0)
        if time.time() < cd:
            await interaction.response.send_message(f"조직이 정비 중입니다. **{_fmt_sec(cd - time.time())}** 뒤에 다시 선전포고할 수 있습니다.", ephemeral=True)
            return
        if _patrolled(guild_id, loc):
            await interaction.response.send_message(f"**{loc}** 은(는) 경찰이 순찰 중이라 지금은 움직일 수 없습니다.", ephemeral=True)
            return
        row = terr_get(guild_id, loc)
        if not row:
            await interaction.response.send_message("주인 없는 구역입니다. `/구역 점령` 으로 차지하세요.", ephemeral=True)
            return
        if row["family_id"] == fid:
            await interaction.response.send_message("우리 조직의 구역입니다.", ephemeral=True)
            return
        prot = float(row.get("protected_until") or 0) - time.time()
        if prot > 0:
            await interaction.response.send_message(f"이 구역은 보호 중입니다. **{_fmt_sec(prot)}** 뒤에 공격할 수 있습니다.", ephemeral=True)
            return
        if row["family_id"] in war_family_map or (guild_id, loc) in active_wars:
            await interaction.response.send_message("상대 조직(또는 이 구역)이 이미 점령전에 휘말려 있습니다.", ephemeral=True)
            return
        if len(terr_list_by_family(fam["id"])) >= TERRITORY_MAX_PER_FAMILY:
            await interaction.response.send_message(f"조직은 구역을 최대 **{TERRITORY_MAX_PER_FAMILY}곳**까지만 가질 수 있어, 새 구역을 빼앗을 수 없습니다. (`/구역 포기` 로 정리하세요)", ephemeral=True)
            return
        if fam.get("treasury", 0) < WAR_COST:
            await interaction.response.send_message(f"조직 금고가 부족합니다. (선전포고 비용 `${WAR_COST:,}` / 금고 `${fam.get('treasury', 0):,}`)", ephemeral=True)
            return
        dfam = family_get_by_id(row["family_id"])
        if not dfam:
            await interaction.response.send_message("구역 주인 정보를 찾을 수 없습니다. 잠시 뒤 다시 시도해 주세요.", ephemeral=True)
            return
        info = get_user_data(user.id)

        # 자리 선점 (여기부터 await 전까지는 다른 명령어가 끼어들 수 없음)
        session = WarSession(guild_id, loc, fam, dfam, user)
        session.players[user.id] = WarPlayer(user, "A", compute_combat_stats(info["honor"], info.get("inventory", {})))
        active_wars[(guild_id, loc)] = session
        war_family_map[fid] = session
        war_family_map[str(dfam["id"])] = session
        war_user_map[user.id] = session
        try:
            family_update(fam["id"], {"treasury": fam["treasury"] - WAR_COST})
        except Exception:
            war_release(session)
            raise
    except Exception as e:
        print(f"Territory war error: {e}")
        await interaction.response.send_message(TERRITORY_DB_ERROR_MSG, ephemeral=True)
        return

    ping = f"<@{session.def_owner_id}> " if session.def_owner_id else ""
    await interaction.response.send_message(
        content=f"{ping}**「{session.def_name}」** 의 **{loc}** 에 선전포고가 날아왔습니다! 조직원은 `수비 참전`을 눌러 구역을 지키세요!",
        embed=war_lobby_embed(session), view=WarLobbyView(session))
    session.message = await interaction.original_response()
    keep_task(asyncio.create_task(run_war(session)))


@territory_group.command(name="탈환", description=f"[경찰] 조직원이 점령한 구역을 되찾는 탈환 작전을 엽니다. (비용 ${RECAPTURE_COST:,} / 경찰 {RECAPTURE_MIN_ATTACKERS}명 이상)")
@app_commands.describe(지역="되찾을 지역")
@app_commands.choices(지역=_LOCATION_CHOICES)
async def territory_recapture_cmd(interaction: discord.Interaction, 지역: app_commands.Choice[str]):
    from .police_unit import _is_police   # 순환 import 방지: 호출 시점에 가져옴
    if not await check_maintenance(interaction): return
    if not await check_membership(interaction): return
    if await check_jail(interaction): return
    user = interaction.user
    if not _is_police(user):
        await interaction.response.send_message("구역 탈환 작전은 치안국 경찰만 열 수 있습니다.", ephemeral=True)
        return
    loc = 지역.value
    guild_id = interaction.guild_id
    try:
        if user.id in war_user_map or user.id in raid_user_map:
            await interaction.response.send_message("이미 점령전(또는 레이드)에 참여 중입니다.", ephemeral=True)
            return
        cd = recapture_cooldowns.get(user.id, 0)
        if time.time() < cd:
            await interaction.response.send_message(f"작전 정비 중입니다. **{_fmt_sec(cd - time.time())}** 뒤에 다시 탈환 작전을 열 수 있습니다.", ephemeral=True)
            return
        row = terr_get(guild_id, loc)
        if not row:
            await interaction.response.send_message("주인 없는 구역입니다. 탈환할 구역이 없습니다.", ephemeral=True)
            return
        prot = float(row.get("protected_until") or 0) - time.time()
        if prot > 0:
            await interaction.response.send_message(f"이 구역은 보호 중입니다. **{_fmt_sec(prot)}** 뒤에 탈환할 수 있습니다.", ephemeral=True)
            return
        if row["family_id"] in war_family_map or (guild_id, loc) in active_wars:
            await interaction.response.send_message("해당 조직(또는 이 구역)이 이미 점령전·탈환전에 휘말려 있습니다.", ephemeral=True)
            return
        dfam = family_get_by_id(row["family_id"])
        if not dfam:
            await interaction.response.send_message("구역 주인 정보를 찾을 수 없습니다. 잠시 뒤 다시 시도해 주세요.", ephemeral=True)
            return
        info = get_user_data(user.id)
        cost = RECAPTURE_COST
        if POLICE_TERMINAL_NAME in (info.get("inventory") or {}):
            cost = int(cost * (1 - POLICE_TERMINAL_COST_DISCOUNT))
        if int(info.get("money") or 0) < cost:
            await interaction.response.send_message(f"자금이 부족합니다. (탈환 비용 `${cost:,}` / 보유 `${int(info.get('money') or 0):,}`)", ephemeral=True)
            return

        # 자리 선점 (여기부터 await 전까지는 다른 명령어가 끼어들 수 없음)
        session = WarSession(guild_id, loc, {"id": "POLICE", "name": "치안국"}, dfam, user, recapture=True, cost=cost)
        session.players[user.id] = WarPlayer(user, "A", compute_combat_stats(info["honor"], info.get("inventory", {})))
        active_wars[(guild_id, loc)] = session
        war_family_map[str(dfam["id"])] = session
        war_user_map[user.id] = session
        try:
            update_user_stats(user.id, -cost, 0)
        except Exception:
            war_release(session)
            raise
    except Exception as e:
        await report_error("/구역 탈환", e, interaction)
        await interaction.response.send_message(TERRITORY_DB_ERROR_MSG, ephemeral=True)
        return

    ping = f"<@{session.def_owner_id}> " if session.def_owner_id else ""
    await interaction.response.send_message(
        content=f"{ping}치안국이 **「{session.def_name}」** 의 **{loc}** 탈환 작전을 개시했습니다! 조직원은 `수비 참전`을 눌러 구역을 지키세요!",
        embed=war_lobby_embed(session), view=WarLobbyView(session))
    session.message = await interaction.original_response()
    keep_task(asyncio.create_task(run_war(session)))


@territory_group.command(name="수금", description="우리 조직 구역에 쌓인 수입을 조직 금고로 거둡니다.")
async def territory_collect_cmd(interaction: discord.Interaction):
    if not await _family_precheck(interaction): return
    user = interaction.user
    now = time.time()
    try:
        fam = family_get_by_user(interaction.guild_id, user.id)
        if not fam:
            await interaction.response.send_message("소속된 조직이 없습니다.", ephemeral=True)
            return
        rows = terr_list_by_family(fam["id"])
        if not rows:
            await interaction.response.send_message("우리 조직이 가진 구역이 없습니다. `/구역 현황` 을 확인해 보세요.", ephemeral=True)
            return
        total = 0
        lines = []
        for row in rows:
            amount = terr_accrued(row, now)
            total += amount
            lines.append(f"• **{row['location']}** `+${amount:,}`")
        if total <= 0:
            await interaction.response.send_message("아직 거둘 수입이 없습니다. 조금 뒤에 다시 와 주세요.", ephemeral=True)
            return
        fresh = family_get_by_id(fam["id"])   # 최신 금고 값으로 갱신
        family_update(fam["id"], {"treasury": (fresh or fam).get("treasury", 0) + total})
        for row in rows:
            terr_update(row["id"], {"collected_at": int(now)})
    except Exception as e:
        print(f"Territory collect error: {e}")
        await interaction.response.send_message(TERRITORY_DB_ERROR_MSG, ephemeral=True)
        return
    await interaction.response.send_message(f"**{user.mention}** 님이 구역 수입을 거뒀습니다!\n" + "\n".join(lines) + f"\n합계 `${total:,}` → 조직 금고")


class TerritoryAbandonView(discord.ui.View):
    def __init__(self, owner: discord.Member, row_id, fid, location: str):
        super().__init__(timeout=30)
        self.owner = owner
        self.row_id = row_id
        self.fid = fid
        self.location = location

    @discord.ui.button(label="정말 포기합니다", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner.id:
            await interaction.response.send_message("조직장만 누를 수 있습니다.", ephemeral=True)
            return
        self.stop()
        try:
            if str(self.fid) in war_family_map:
                await interaction.response.edit_message(content="점령전 진행 중에는 구역을 포기할 수 없습니다.", view=None)
                return
            row = terr_get(interaction.guild_id, self.location)
            if not row or row["family_id"] != str(self.fid):
                await interaction.response.edit_message(content="이미 우리 조직의 구역이 아닙니다.", view=None)
                return
            accrued = terr_accrued(row)
            fam = family_get_by_id(self.fid)
            if fam and accrued > 0:
                family_update(self.fid, {"treasury": fam.get("treasury", 0) + accrued})   # 쌓인 수입은 정산
            terr_delete(row["id"])
        except Exception as e:
            print(f"Territory abandon error: {e}")
            await interaction.response.edit_message(content=TERRITORY_DB_ERROR_MSG, view=None)
            return
        await interaction.response.edit_message(content=f"**{self.location}** 구역을 포기했습니다. (쌓인 수입은 금고로 정산, 점령 비용은 돌려받지 않습니다)", view=None)


@territory_group.command(name="포기", description="[조직장] 우리 조직의 구역을 포기합니다. (점령 비용은 돌려받지 않습니다)")
@app_commands.describe(지역="포기할 지역")
@app_commands.choices(지역=_LOCATION_CHOICES)
async def territory_abandon_cmd(interaction: discord.Interaction, 지역: app_commands.Choice[str]):
    if not await _family_precheck(interaction): return
    user = interaction.user
    loc = 지역.value
    try:
        fam = family_get_by_user(interaction.guild_id, user.id)
        if not fam or fam["owner_id"] != str(user.id):
            await interaction.response.send_message("조직장만 사용할 수 있습니다.", ephemeral=True)
            return
        row = terr_get(interaction.guild_id, loc)
    except Exception as e:
        print(f"Territory abandon lookup error: {e}")
        await interaction.response.send_message(TERRITORY_DB_ERROR_MSG, ephemeral=True)
        return
    if not row or row["family_id"] != str(fam["id"]):
        await interaction.response.send_message("우리 조직의 구역이 아닙니다.", ephemeral=True)
        return
    await interaction.response.send_message(
        f"정말 **{loc}** 을(를) 포기할까요? 점령 비용은 돌려받지 못합니다.",
        view=TerritoryAbandonView(user, fam["id"], fam["id"], loc), ephemeral=True)
