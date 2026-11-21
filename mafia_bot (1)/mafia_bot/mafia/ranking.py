"""조직 현황·랭킹 (/랭킹)"""

import asyncio
import discord
import time
from discord import app_commands
from datetime import datetime

from .core import bot, supabase
from .alignment import get_title_blob
from .admin import check_maintenance
from .family import (
    FAMILY_DB_ERROR_MSG,
    FAMILY_MAX_MEMBERS,
    active_raids,
    family_get_by_id,
    family_get_by_name,
    family_get_by_user,
    family_group,
    family_update,
    raid_family_cooldowns,
)
from .territory import (
    TERRITORY_MAX_PER_FAMILY,
    _fmt_sec,
    _terr_info,
    terr_accrued,
    terr_list_by_family,
    war_family_cooldowns,
    war_family_map,
)

# ==========================================================================
# [조직 현황 · 랭킹 · 조직 누적 통계]   /조직 현황 · /랭킹
#
#   • 조직의 기본 데이터(조직원·금고·레이드 점수)는 원래부터 Supabase `families` 테이블에 저장됩니다.
#   • 여기서는 누적 통계(레이드 클리어 / 점령전 승·패)를 같은 테이블의 컬럼으로 추가 저장합니다.
#       → family_stats.sql 을 Supabase 에서 한 번 실행해야 합니다.
#       → 컬럼이 없어도 레이드/점령전 보상 지급 등 본 기능은 깨지지 않고, 통계만 기록되지 않습니다.
#   • /랭킹 은 명예·자금(개인) / 조직 레이드 점수·점령전 승수(조직) 를 보여줍니다.
#       - 개인 랭킹은 user_data 가 서버별로 나뉘어 있지 않으므로, 상위 RANK_SCAN_N 명 중
#         '이 서버에 있는 사람'만 추려 보여줍니다.
# ==========================================================================
RANK_TOP_N = 10          # 랭킹에 보여줄 인원 / 조직 수
RANK_SCAN_N = 200        # 개인 랭킹 집계 시 DB 에서 읽어올 상위 인원 수
FAMILY_STATS_DB_HINT = "(관리자: `families` 테이블에 통계 컬럼이 있는지 확인해 주세요 — family_stats.sql 참고)"


def family_stat_inc(fid, field: str, n: int = 1):
    """조직 누적 통계를 DB 에서 +n. 실패해도 호출한 쪽(보상 지급 등)이 깨지지 않도록 로그만 남깁니다."""
    try:
        fam = family_get_by_id(fid)
        if fam:
            family_update(fid, {field: int(fam.get(field) or 0) + n})
    except Exception as e:
        print(f"Family stat error ({field}): {e}")


def family_member_stats(member_ids: list) -> dict:
    """{user_id(str): {money, honor}}  (가입 기록이 없는 사람은 빠짐 — get_user_data 와 달리 행을 새로 만들지 않음)"""
    if not member_ids:
        return {}
    res = (supabase.table("user_data").select("user_id,money,honor")
           .in_("user_id", [str(m) for m in member_ids]).execute())
    return {str(r["user_id"]): r for r in (res.data or [])}


def family_list_top_by(guild_id: int, field: str, limit: int = RANK_TOP_N):
    res = (supabase.table("families").select("*").eq("guild_id", str(guild_id))
           .order(field, desc=True).limit(limit).execute())
    return res.data or []


def user_rank_rows(field: str, limit: int = RANK_SCAN_N):
    res = supabase.table("user_data").select(f"user_id,{field}").order(field, desc=True).limit(limit).execute()
    return res.data or []


# ---------------------------------------------------------------- /조직 현황
def family_status_embed(fam: dict, stats: dict, terr_rows, now: float = None) -> discord.Embed:
    """terr_rows: 보유 구역 행 목록 (None 이면 구역 정보를 불러오지 못한 것)"""
    now = now or time.time()
    members = list(fam.get("member_ids") or [])
    wins, losses = int(fam.get("war_wins") or 0), int(fam.get("war_losses") or 0)
    total_war = wins + losses
    rate = f" (승률 {int(wins * 100 / total_war)}%)" if total_war else ""
    total_honor = sum(int((stats.get(str(m)) or {}).get("honor") or 0) for m in members)

    embed = discord.Embed(title=f"조직 현황 「{fam['name']}」", color=discord.Color.dark_gold())
    embed.add_field(
        name="개요",
        value=(f"**조직장:** <@{fam['owner_id']}> · **조직원:** {len(members)}/{FAMILY_MAX_MEMBERS}\n"
               f"**금고:** `${fam.get('treasury', 0):,}` · **조직원 명예 합계:** `{total_honor:,} pt`"),
        inline=False)
    embed.add_field(
        name="전적",
        value=(f"레이드 점수 `{int(fam.get('raid_points') or 0):,} pt` · 클리어 `{int(fam.get('raid_clears') or 0)}회`\n"
               f"구역 점령전 `{wins}승 {losses}패`{rate}"),
        inline=False)

    if terr_rows is None:
        terr_text = "구역 정보를 불러오지 못했습니다."
    elif not terr_rows:
        terr_text = "보유 구역 없음 (`/구역 현황` 에서 점령 가능한 구역을 확인하세요)"
    else:
        lines, hourly, pending = [], 0, 0
        for row in sorted(terr_rows, key=lambda r: r["location"]):
            inc = _terr_info(row["location"])["income"]
            acc = terr_accrued(row, now)
            hourly += inc
            pending += acc
            prot = float(row.get("protected_until") or 0) - now
            tail = f" · 보호 {_fmt_sec(prot)}" if prot > 0 else ""
            lines.append(f"• **{row['location']}** `${inc:,}`/시간 · 미수금 `${acc:,}`{tail}")
        lines.append(f"합계 `${hourly:,}`/시간 · 미수금 `${pending:,}` (`/구역 수금`)")
        terr_text = "\n".join(lines)
    embed.add_field(name=f"보유 구역 ({len(terr_rows) if terr_rows else 0}/{TERRITORY_MAX_PER_FAMILY})", value=terr_text, inline=False)

    def _honor(m):
        return int((stats.get(str(m)) or {}).get("honor") or 0)
    lines = []
    for m in sorted(members, key=lambda m: (-_honor(m), str(m))):
        st = stats.get(str(m))
        crown = "[조직장] " if str(m) == str(fam["owner_id"]) else "• "
        if st:
            lines.append(f"{crown}<@{m}> — 명예 `{int(st.get('honor') or 0):,}` · `${int(st.get('money') or 0):,}`")
        else:
            lines.append(f"{crown}<@{m}>")
    embed.add_field(name="조직원 (명예순)", value="\n".join(lines) or "-", inline=False)

    status = []
    raid = active_raids.get(fam["id"])
    if raid:
        status.append(f"레이드: {raid.tier['name']} ({'모집 중' if raid.state == 'lobby' else '전투 중'})")
    cd = raid_family_cooldowns.get(fam["id"])
    if cd and datetime.now() < cd:
        status.append(f"다음 레이드까지 {int((cd - datetime.now()).total_seconds() // 60) + 1}분")
    war = war_family_map.get(str(fam["id"]))
    if war:
        role = "공격" if str(war.aid) == str(fam["id"]) else "수비"
        status.append(f"점령전: {war.location} ({role}, {'모집 중' if war.state == 'lobby' else '전투 중'})")
    wcd = war_family_cooldowns.get(str(fam["id"]), 0)
    if now < wcd:
        status.append(f"다음 선전포고까지 {_fmt_sec(wcd - now)}")
    if status:
        embed.add_field(name="진행 상태", value="\n".join(status), inline=False)
    return embed


@family_group.command(name="현황", description="조직 종합 현황(금고·전적·구역·조직원)을 확인합니다. (이름을 비우면 내 조직)")
@app_commands.describe(이름="확인할 조직 이름 (비우면 내 조직)")
async def family_status_cmd(interaction: discord.Interaction, 이름: str = None):
    if not await check_maintenance(interaction): return
    try:
        fam = family_get_by_name(interaction.guild_id, 이름.strip()) if 이름 else family_get_by_user(interaction.guild_id, interaction.user.id)
        stats = family_member_stats(fam.get("member_ids") or []) if fam else {}
    except Exception as e:
        print(f"Family status error: {e}")
        await interaction.response.send_message(FAMILY_DB_ERROR_MSG, ephemeral=True)
        return
    if not fam:
        msg = "해당 이름의 조직이 없습니다." if 이름 else "소속된 조직이 없습니다. `/조직 생성` 또는 초대를 받아 보세요."
        await interaction.response.send_message(msg, ephemeral=True)
        return
    try:
        terr_rows = terr_list_by_family(fam["id"])
    except Exception as e:   # territories 테이블이 아직 없어도 현황은 보여준다
        print(f"Family status territory error: {e}")
        terr_rows = None
    await interaction.response.send_message(embed=family_status_embed(fam, stats, terr_rows))


# ---------------------------------------------------------------- /랭킹
# ---------------------------------------------------------------- 경찰 랭킹
#   /랭킹 에서 고르는 경찰 전용 랭킹입니다. 현재 '경찰' 역할을 가진 사람만 집계합니다.
#   검거·순찰·심문 횟수는 업적 카운터(user_data.data 의 stats)를 씁니다.
POLICE_RANK_KINDS = {
    # kind: (제목, stats 키 — None 이면 명예, 값 표시)
    "police_honor":          ("경찰 명예 랭킹", None,                    lambda v: f"{v:,} pt"),
    "police_arrests":        ("경찰 검거 랭킹", "police_arrests",        lambda v: f"검거 {v:,}회"),
    "police_patrols":        ("경찰 순찰 랭킹", "police_patrols",        lambda v: f"순찰 {v:,}회"),
    "police_interrogations": ("경찰 심문 랭킹", "police_interrogations", lambda v: f"심문 재판 {v:,}회"),
}

def police_rank_rows_sync(user_ids: list) -> list:
    """경찰 유저들의 명예·통계 행을 읽어옴 (100명씩 나눠서 조회)"""
    ids = [str(i) for i in user_ids]
    rows = []
    for k in range(0, len(ids), 100):
        res = supabase.table("user_data").select("user_id,honor,data").in_("user_id", ids[k:k + 100]).execute()
        rows.extend(res.data or [])
    return rows

async def send_police_ranking(interaction: discord.Interaction, kind: str):
    from .police_unit import POLICE_UNIT_DB_ERROR_MSG, _is_police, police_unit_list_top   # 순환 import 방지: 호출 시점에 가져옴
    guild = interaction.guild
    footer = None
    if kind == "police_unit":
        try:
            rows = await asyncio.to_thread(police_unit_list_top, interaction.guild_id, RANK_TOP_N)
        except Exception as e:
            print(f"Police unit ranking error: {e}")
            await interaction.followup.send(POLICE_UNIT_DB_ERROR_MSG, ephemeral=True)
            return
        title = "수사팀 랭킹 (작전 점수)"
        lines = [f"`{i + 1}위` **{r['name']}** — 작전 점수 {int(r.get('op_points') or 0):,} · 성공 {int(r.get('ops_cleared') or 0)}회 · {len(r.get('member_ids') or [])}명"
                 for i, r in enumerate(rows) if int(r.get("op_points") or 0) > 0]
    else:
        title, stat_key, fmt = POLICE_RANK_KINDS[kind]
        role = discord.utils.get(guild.roles, name="경찰")
        members = [m for m in (role.members if role else []) if not m.bot]
        if not members:
            await interaction.followup.send("아직 경찰 소속이 없습니다.", ephemeral=True)
            return
        try:
            rows = await asyncio.to_thread(police_rank_rows_sync, [m.id for m in members])
        except Exception as e:
            print(f"Police ranking error: {e}")
            await interaction.followup.send("랭킹을 불러오지 못했습니다. 잠시 후 다시 시도해 주세요.", ephemeral=True)
            return
        by_id = {}
        for r in rows:
            try:
                by_id[int(r["user_id"])] = r
            except (KeyError, ValueError, TypeError):
                continue
        board = []   # [(member, 값, 명예)]
        for m in members:
            r = by_id.get(m.id)
            if not r:
                continue
            honor = int(r.get("honor") or 0)
            value = honor if stat_key is None else int(get_title_blob({"data": r.get("data")})["stats"].get(stat_key, 0))
            if stat_key is not None and value <= 0:      # 기록이 없는 사람은 순위에 올리지 않음
                continue
            board.append((m, value, honor))
        board.sort(key=lambda t: (-t[1], -t[2]))             # 값이 같으면 명예가 높은 쪽이 위
        lines = [f"`{i + 1}위` **{m.display_name}** — {fmt(v)}" for i, (m, v, _) in enumerate(board[:RANK_TOP_N])]
        me = next((i + 1 for i, (m, _, _) in enumerate(board) if m.id == interaction.user.id), None)
        if me:
            footer = f"내 순위: {me}위"
        elif _is_police(interaction.user):
            footer = "내 순위: 아직 기록이 없습니다."
        else:
            footer = "경찰 소속만 집계되는 랭킹입니다."
        if stat_key is not None:
            footer += " · 업적 시스템 도입 이후의 기록부터 집계됩니다."

    if not lines:
        await interaction.followup.send("아직 집계할 기록이 없습니다.", ephemeral=True)
        return
    embed = discord.Embed(title=title, description="\n".join(lines), color=discord.Color.blue())
    if footer:
        embed.set_footer(text=footer)
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="랭킹", description="[공용] 서버 랭킹을 확인합니다. (명예 / 자금 / 조직 / 경찰)")
@app_commands.describe(종류="확인할 랭킹")
@app_commands.choices(종류=[
    app_commands.Choice(name="명예 (개인)", value="honor"),
    app_commands.Choice(name="자금 (개인)", value="money"),
    app_commands.Choice(name="레이드 점수 (조직)", value="raid_points"),
    app_commands.Choice(name="점령전 승수 (조직)", value="war_wins"),
    app_commands.Choice(name="경찰 명예 (경찰)", value="police_honor"),
    app_commands.Choice(name="경찰 검거 수 (경찰)", value="police_arrests"),
    app_commands.Choice(name="경찰 순찰 수 (경찰)", value="police_patrols"),
    app_commands.Choice(name="경찰 심문 수 (경찰)", value="police_interrogations"),
    app_commands.Choice(name="수사팀 작전 점수 (수사팀)", value="police_unit"),
])
async def ranking_cmd(interaction: discord.Interaction, 종류: app_commands.Choice[str]):
    if not await check_maintenance(interaction): return
    kind = 종류.value
    guild = interaction.guild
    await interaction.response.defer()
    if kind in POLICE_RANK_KINDS or kind == "police_unit":      # 경찰 관련 랭킹은 따로 처리
        await send_police_ranking(interaction, kind)
        return
    try:
        if kind in ("honor", "money"):
            rows = await asyncio.to_thread(user_rank_rows, kind, RANK_SCAN_N)
        else:
            rows = await asyncio.to_thread(family_list_top_by, interaction.guild_id, kind, RANK_TOP_N)
    except Exception as e:
        print(f"Ranking error: {e}")
        hint = FAMILY_STATS_DB_HINT if kind == "war_wins" else ""
        await interaction.followup.send(f"랭킹을 불러오지 못했습니다. {hint}".strip(), ephemeral=True)
        return

    if kind in ("honor", "money"):
        board = []   # [(member, value)]  이 서버에 있는 사람만
        for r in rows:
            try:
                m = guild.get_member(int(r["user_id"]))
            except (ValueError, TypeError):
                continue
            if m and not m.bot:
                board.append((m, int(r.get(kind) or 0)))
        unit = (lambda v: f"{v:,} pt") if kind == "honor" else (lambda v: f"${v:,}")
        title = "명예 랭킹" if kind == "honor" else "자금 랭킹"
        lines = [f"`{i + 1}위` **{m.display_name}** — {unit(v)}" for i, (m, v) in enumerate(board[:RANK_TOP_N])]
        me = next((i + 1 for i, (m, _) in enumerate(board) if m.id == interaction.user.id), None)
        footer = f"내 순위: {me}위" if me else f"내 순위: 집계 밖 (전체 상위 {RANK_SCAN_N}명 안에 없음)"
    else:
        title = "조직 랭킹 (레이드 점수)" if kind == "raid_points" else "조직 랭킹 (점령전 승수)"
        lines = []
        for i, r in enumerate(rows):
            n = len(r.get("member_ids") or [])
            if kind == "raid_points":
                lines.append(f"`{i + 1}위` **{r['name']}** — {int(r.get('raid_points') or 0):,} pt · {n}명")
            else:
                lines.append(f"`{i + 1}위` **{r['name']}** — {int(r.get('war_wins') or 0)}승 {int(r.get('war_losses') or 0)}패 · {n}명")
        footer = None

    if not lines:
        await interaction.followup.send("아직 집계할 기록이 없습니다.", ephemeral=True)
        return
    embed = discord.Embed(title=title, description="\n".join(lines), color=discord.Color.gold())
    if footer:
        embed.set_footer(text=footer)
    await interaction.followup.send(embed=embed)
