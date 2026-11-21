"""/월급"""

import asyncio
import discord
import time

from .core import bot, inc_money
from .status import check_jail, get_user_data, get_user_rank, update_user_stats
from .achievements import police_ach_bump
from .admin import check_maintenance, check_membership
from .police_shop import police_bonuses
from .police_unit import _is_police

# ==========================================================================
# [경찰 전용] /월급  —  10분 근무 평가
#   • /월급 을 쓰면 10분짜리 근무가 시작되고, 그동안 한 경찰 활동(순찰·검거·심문·작전·수사팀 작전)이 점수로 쌓입니다.
#   • 10분이 지나면 자동으로 정산되어 점수에 비례한 월급(보상금 + 명예)이 지급됩니다.
#   • 근무 기록은 메모리에만 있어서, 근무 중에 봇이 재시작되면 그 근무는 사라집니다.
#   • 정산 직후에도 바로 새 근무를 시작할 수 있습니다. (쿨타임 없음, 단 근무는 동시에 하나만)
# ==========================================================================
POLICE_SALARY_SHIFT_SEC = 600          # 근무(추적) 시간
POLICE_SALARY_FULL_SCORE = 100         # 이 점수에 도달하면 기본 월급의 100%
POLICE_SALARY_MAX_RATIO = 1.5          # 월급 상한 (기본 월급의 150% = 150점)
POLICE_SALARY_BASE_MONEY = inc_money(2000)        # 100% 월급: 보상금
POLICE_SALARY_BASE_HONOR = 44          # 100% 월급: 명예 (황금 배지 보너스 적용)
POLICE_SALARY_ARREST_BONUS_PER = 100   # 거물 검거 보너스: 수배범 명예 100pt 당 +1점
POLICE_SALARY_ARREST_BONUS_MAX = 50    # 거물 검거 보너스 상한 → 검거 1회 50 ~ 100점

# 행동 하나당 점수
POLICE_SALARY_POINTS = {
    "patrol": 30,              # 순찰 1회 (쿨타임 3분 → 10분 동안 최대 4회)
    "arrest": 50,              # 수배범 검거 1회
    "interrogate": 25,         # 심문 재판 완료 1회
    "interrogate_match": 15,   # 재판 결과가 내 의견과 일치 (심문 점수에 추가)
    "op_stage": 15,            # 수사팀 합동 작전에서 성공한 단계 1개당
    "operation": 8,            # /작전 성공 1회 (+ 난이도별 추가 점수 0~12)
}
POLICE_SALARY_LABELS = {
    "patrol": "순찰", "arrest": "검거", "interrogate": "심문 재판",
    "interrogate_match": "의견 일치 판결", "op_stage": "작전 성공 단계", "operation": "/작전 성공",
}

police_shifts = {}              # {user_id: PoliceShift}  (진행 중인 근무)

class PoliceShift:
    def __init__(self, member: discord.Member, interaction: discord.Interaction):
        self.user_id = member.id
        self.member = member
        self.interaction = interaction
        self.channel = interaction.channel
        self.started = time.time()
        self.ends = self.started + POLICE_SALARY_SHIFT_SEC
        self.counts = {}
        self.bonus_points = 0   # 거물 검거 보너스 등 행동마다 달라지는 추가 점수
        self.settled = False
        self.task = None

def police_shift_record(user_id: int, kind: str, n: int = 1, bonus: int = 0):
    """경찰 명령어들이 호출: 근무 중인 경찰이면 행동을 점수로 기록 (근무 중이 아니면 아무 일도 없음)"""
    s = police_shifts.get(user_id)
    if s is None or s.settled or time.time() >= s.ends:
        return
    s.counts[kind] = s.counts.get(kind, 0) + n
    s.bonus_points += max(0, int(bonus))

def police_shift_score(s: PoliceShift) -> int:
    return sum(POLICE_SALARY_POINTS.get(k, 0) * c for k, c in s.counts.items()) + s.bonus_points

def police_shift_grade(ratio: float) -> str:
    if ratio >= 1.0: return "S (우수)"
    if ratio >= 0.7: return "A (양호)"
    if ratio >= 0.4: return "B (보통)"
    if ratio > 0:    return "C (미흡)"
    return "D (무실적)"

def police_shift_breakdown(s: PoliceShift) -> str:
    lines = []
    for kind, label in POLICE_SALARY_LABELS.items():
        c = s.counts.get(kind, 0)
        if c:
            lines.append(f"• {label} ×{c} → `+{POLICE_SALARY_POINTS[kind] * c}점`")
    if s.bonus_points:
        lines.append(f"• 거물 검거 보너스 → `+{s.bonus_points}점`")
    return "\n".join(lines) or "아직 기록된 실적이 없습니다."

def police_shift_payout(score: int):
    ratio = min(POLICE_SALARY_MAX_RATIO, score / POLICE_SALARY_FULL_SCORE)
    return ratio, int(POLICE_SALARY_BASE_MONEY * ratio)

def police_shift_embed(s: PoliceShift, title: str) -> discord.Embed:
    score = police_shift_score(s)
    ratio, money = police_shift_payout(score)
    remain = max(0, int(s.ends - time.time()))
    embed = discord.Embed(
        title=title,
        description=(f"근무 종료까지 **{remain // 60}분 {remain % 60}초**\n"
                     f"현재 실적 **{score}점** / 기본 월급 기준 {POLICE_SALARY_FULL_SCORE}점 (최대 {int(POLICE_SALARY_MAX_RATIO * 100)}%)\n"
                     f"예상 월급 `${money:,}` · 평가 **{police_shift_grade(ratio)}**\n\n"
                     f"{police_shift_breakdown(s)}"),
        color=discord.Color.blue()
    )
    return embed

async def settle_police_shift(s: PoliceShift):
    if s.settled:
        return
    s.settled = True   # 동시에 두 번 정산되지 않도록 await 전에 표시
    if police_shifts.get(s.user_id) is s:
        police_shifts.pop(s.user_id, None)

    score = police_shift_score(s)
    ratio, money = police_shift_payout(score)
    honor = 0
    note = ""
    rank = None
    guild = s.member.guild
    member = guild.get_member(s.user_id) or s.member
    try:
        if not _is_police(member):
            money, honor, note = 0, 0, "\n경찰 신분이 아니어서 월급이 지급되지 않았습니다."
        elif score > 0:
            info = await asyncio.to_thread(get_user_data, s.user_id)
            mult = police_bonuses(info.get("inventory"))["honor_mult"]
            honor = int(round(POLICE_SALARY_BASE_HONOR * ratio * mult))
            await asyncio.to_thread(update_user_stats, s.user_id, money, honor)
            info = await asyncio.to_thread(get_user_data, s.user_id)
            rank = get_user_rank(info["honor"], is_police=True)
        else:
            money = 0
    except Exception as e:
        print(f"Police salary payout error: {e}")
        money, honor, note = 0, 0, "\n정산 중 오류가 발생해 월급이 지급되지 않았습니다. 관리자에게 문의해 주세요."

    desc = (f"**{member.mention}** 경관의 10분 근무 평가가 끝났습니다.\n\n"
            f"**평가:** {police_shift_grade(ratio)} · 실적 **{score}점**\n"
            f"{police_shift_breakdown(s)}\n\n")
    if money > 0 or honor > 0:
        desc += f"**월급:** `${money:,}`\n**경찰 실적:** `+{honor} pt`"
        if rank:
            desc += f" (현재 직급: `{rank}`)"
    else:
        desc += "이번 근무에는 지급된 월급이 없습니다." if not note else ""
    desc += note
    if score > 0 and not note and ratio >= 1.0:   # S(우수) 평가 → 업적 카운터
        desc += await police_ach_bump(s.user_id, police_salary_s=1)
    desc += "\n\n`/월급` 으로 바로 새 근무를 시작할 수 있습니다."
    embed = discord.Embed(title="[치안국 월급 명세서]", description=desc,
                          color=discord.Color.green() if money > 0 else discord.Color.dark_grey())
    if member.avatar:
        embed.set_thumbnail(url=member.avatar.url)
    try:
        await s.channel.send(content=member.mention, embed=embed)
    except Exception:
        try:
            await s.interaction.followup.send(content=member.mention, embed=embed, ephemeral=True)
        except Exception as e:
            print(f"Police salary notify error: {e}")

async def run_police_shift(s: PoliceShift):
    try:
        await asyncio.sleep(max(0.0, s.ends - time.time()))
        await settle_police_shift(s)
    except Exception as e:
        print(f"Police shift error: {e}")
        if not s.settled:
            police_shifts.pop(s.user_id, None)

@bot.tree.command(name="월급", description="[경찰 전용] 10분간 근무 실적을 평가받아 점수에 비례한 월급(보상금·명예)을 받습니다.")
async def salary_command(interaction: discord.Interaction):
    if not await check_maintenance(interaction): return
    if not await check_membership(interaction): return
    if await check_jail(interaction): return
    user = interaction.user
    if not _is_police(user):
        await interaction.response.send_message("치안국 경찰만 월급을 받을 수 있습니다!", ephemeral=True)
        return

    s = police_shifts.get(user.id)
    if s is not None and not s.settled:
        if time.time() >= s.ends:   # 정산 타이머가 놓친 경우를 대비한 안전장치
            await interaction.response.send_message("근무 시간이 끝나 정산을 진행합니다. 잠시 후 월급 명세서가 올라옵니다.", ephemeral=True)
            await settle_police_shift(s)
            return
        await interaction.response.send_message(embed=police_shift_embed(s, "[근무 중] 월급 평가 진행 상황"), ephemeral=True)
        return

    s = PoliceShift(user, interaction)
    police_shifts[user.id] = s   # 확인과 등록 사이에 await 가 없어 중복 시작이 불가능
    points_text = "\n".join(f"• {POLICE_SALARY_LABELS[k]}: `+{p}점`" for k, p in POLICE_SALARY_POINTS.items())
    points_text += f"\n• 거물 검거 보너스: 수배범 명예 {POLICE_SALARY_ARREST_BONUS_PER}pt 당 `+1점` (최대 +{POLICE_SALARY_ARREST_BONUS_MAX}점)"
    embed = discord.Embed(
        title="[근무 시작] 월급 평가",
        description=(f"지금부터 **{POLICE_SALARY_SHIFT_SEC // 60}분간** 경찰 활동이 실적으로 기록됩니다.\n"
                     f"{POLICE_SALARY_SHIFT_SEC // 60}분 뒤 자동으로 정산되어 월급이 지급됩니다.\n\n"
                     f"**실적 점수**\n{points_text}\n\n"
                     f"**월급 기준:** {POLICE_SALARY_FULL_SCORE}점 = `${POLICE_SALARY_BASE_MONEY:,}` · 명예 `+{POLICE_SALARY_BASE_HONOR}` "
                     f"(점수에 비례, 최대 {int(POLICE_SALARY_MAX_RATIO * 100)}%)\n"
                     f"`/월급` 을 다시 입력하면 진행 상황을 볼 수 있어요."),
        color=discord.Color.blue()
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)
    s.task = asyncio.create_task(run_police_shift(s))
