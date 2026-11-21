"""시민 활동 (/아르바이트 /봉사 /기부 /소매치기)"""

import discord
import random
import time
from discord import app_commands
from datetime import datetime, timedelta

from .core import bot, inc_range, wanted_list
from .status import PARTTIME_COOLDOWN_SEC, check_jail, get_user_data, save_status, update_user_stats
from .alignment import (
    DONATE_COOLDOWN_SEC,
    DONATE_MAX_KARMA,
    DONATE_MIN,
    DONATE_UNIT,
    PARTTIME_KARMA,
    PICKPOCKET_COOLDOWN_SEC,
    PICKPOCKET_FINE,
    PICKPOCKET_KARMA_FAIL,
    PICKPOCKET_KARMA_OK,
    PICKPOCKET_REWARD,
    PICKPOCKET_SUCCESS,
    PICKPOCKET_WANTED_SEC,
    SERVICE_COOLDOWN_SEC,
    SERVICE_KARMA,
    SERVICE_REWARD,
    _is_citizen,
    alignment_text,
    get_title_blob,
    karma_change,
    save_title_blob,
)
from .achievements import daily_record
from .admin import check_maintenance

# === 시민 전용 아르바이트 쿨타임 사전 ===
parttime_cooldowns = {}

@bot.tree.command(name="아르바이트", description="[시민/공용] 합법적인 일거리를 통해 안전하게 소량의 자금을 벌어듭니다.")
async def part_time_job(interaction: discord.Interaction):
    if not await check_maintenance(interaction): return
    if await check_jail(interaction): return

    await interaction.response.defer()
    user = interaction.user

    now = datetime.now()
    if user.id in parttime_cooldowns:
        next_available = parttime_cooldowns[user.id]
        if now < next_available:
            remaining = int((next_available - now).total_seconds())
            await interaction.followup.send(f"피로를 푸는 중입니다. **{remaining}초** 뒤에 다시 일할 수 있습니다.", ephemeral=True)
            return

    parttime_cooldowns[user.id] = now + timedelta(seconds=PARTTIME_COOLDOWN_SEC)

    jobs = [
        {"title": "편의점 야간 알바", "reward": inc_range((100, 250))},
        {"title": "물류 센터 상하차", "reward": inc_range((200, 350))},
        {"title": "도시 카페 서빙", "reward": inc_range((120, 280))},
        {"title": "중앙 광장 청소", "reward": inc_range((100, 200))}
    ]
    
    selected_job = random.choice(jobs)
    earned_money = random.randint(*selected_job["reward"])

    update_user_stats(user.id, earned_money, 0)
    user_stats = get_user_data(user.id)

    # 일일 의뢰 집계 (아르바이트)
    daily_msg = ""
    try:
        daily_blob = get_title_blob(user_stats)
        daily_blob["stats"]["parttime_total"] += 1                      # 전직 조건용 누적 횟수
        if _is_citizen(user):
            karma_change(daily_blob, PARTTIME_KARMA)                    # 성실한 노동은 선한 활동
        daily_msg = daily_record(daily_blob, "parttime", user_stats)
        save_title_blob(user.id, daily_blob)
    except Exception as e:
        print(f"DB Error (parttime daily quest): {e}")
        daily_msg = ""

    embed = discord.Embed(
        title=f"[{selected_job['title']}] 완료",
        description=f"**{user.mention}** 님이 정직한 노동으로 자금을 벌었습니다!\n\n"
                    f"**획득 급여:** `${earned_money:,}`\n"
                    f"**현재 보유 자금:** `${user_stats['money']:,}`{daily_msg}",
        color=discord.Color.blue()
    )
    if user.avatar:
        embed.set_thumbnail(url=user.avatar.url)

    await interaction.followup.send(embed=embed)

# ==========================================================================
# [시민 활동] /봉사 /기부 (선) · /소매치기 (악)  —  시민 전용, 성향 점수를 쌓는 활동입니다.
# ==========================================================================
service_cooldowns = {}
donate_cooldowns = {}
pickpocket_cooldowns = {}

def _cd_left(table: dict, key) -> int:
    return max(0, int(table.get(key, 0) - time.time() + 0.999))

def apply_citizen_activity(user_id: int, money_delta: int, karma_delta: int, stat_incs: dict = None):
    """돈·성향·카운터를 한 번에 반영하고 (유저 데이터, 블롭)을 돌려줍니다."""
    if money_delta:
        update_user_stats(user_id, money_delta, 0)
    info = get_user_data(user_id)
    blob = get_title_blob(info)
    karma_change(blob, karma_delta)
    for k, v in (stat_incs or {}).items():
        blob["stats"][k] += v
    save_title_blob(user_id, blob)
    return info, blob

async def _citizen_only(interaction: discord.Interaction) -> bool:
    if not await check_maintenance(interaction): return False
    if await check_jail(interaction): return False
    if not _is_citizen(interaction.user):
        await interaction.response.send_message("이 활동은 **시민**만 할 수 있습니다. (이미 소속이 정해졌어요)", ephemeral=True)
        return False
    return True

@bot.tree.command(name="봉사", description="[시민 전용] 도시를 위해 봉사활동을 하고 선한 성향을 쌓습니다. (소정의 사례금)")
async def service_command(interaction: discord.Interaction):
    if not await _citizen_only(interaction): return
    user = interaction.user
    left = _cd_left(service_cooldowns, user.id)
    if left > 0:
        await interaction.response.send_message(f"잠시 쉬는 중입니다. **{left}초** 뒤에 다시 봉사할 수 있습니다.", ephemeral=True)
        return
    service_cooldowns[user.id] = time.time() + SERVICE_COOLDOWN_SEC
    await interaction.response.defer()

    work = random.choice(["항구 해변 쓰레기 줍기", "독거 노인 댁 방문 도우미", "고아원 급식 봉사", "중앙 광장 벽화 복원 돕기"])
    pay = random.randint(*SERVICE_REWARD)
    try:
        info, blob = apply_citizen_activity(user.id, pay, SERVICE_KARMA, {"service_total": 1})
    except Exception as e:
        print(f"DB Error (service): {e}")
        service_cooldowns.pop(user.id, None)
        await interaction.followup.send("정산 중 오류가 발생했습니다. 잠시 뒤 다시 시도해 주세요.", ephemeral=True)
        return
    embed = discord.Embed(
        title=f"[{work}] 완료",
        description=f"**{user.mention}** 님의 선행이 이웃들의 칭찬을 받았습니다.\n\n"
                    f"**사례금:** `${pay:,}`\n**성향:** {alignment_text(blob['karma'])} (`{SERVICE_KARMA:+d}`)",
        color=discord.Color.green())
    if user.avatar:
        embed.set_thumbnail(url=user.avatar.url)
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="기부", description=f"[시민 전용] 이웃돕기 성금을 기부하고 선한 성향을 쌓습니다. (${DONATE_UNIT:,} 당 성향 +1)")
@app_commands.describe(금액=f"기부할 금액 (${DONATE_MIN:,} 이상)")
async def donate_command(interaction: discord.Interaction, 금액: app_commands.Range[int, DONATE_MIN, 10_000_000]):
    if not await _citizen_only(interaction): return
    user = interaction.user
    left = _cd_left(donate_cooldowns, user.id)
    if left > 0:
        await interaction.response.send_message(f"잠시 뒤에 다시 기부해 주세요. ({left}초)", ephemeral=True)
        return
    if get_user_data(user.id)["money"] < 금액:
        await interaction.response.send_message("보유 자금이 부족합니다.", ephemeral=True)
        return
    donate_cooldowns[user.id] = time.time() + DONATE_COOLDOWN_SEC
    await interaction.response.defer()

    gain = min(DONATE_MAX_KARMA, 금액 // DONATE_UNIT)
    try:
        info, blob = apply_citizen_activity(user.id, -금액, gain, {"donated_total": 금액})
    except Exception as e:
        print(f"DB Error (donate): {e}")
        donate_cooldowns.pop(user.id, None)
        await interaction.followup.send("처리 중 오류가 발생했습니다.", ephemeral=True)
        return
    embed = discord.Embed(
        title="[이웃돕기 성금]",
        description=f"**{user.mention}** 님이 `${금액:,}` 을(를) 기부했습니다. 도시가 따뜻해졌습니다.\n\n"
                    f"**성향:** {alignment_text(blob['karma'])} (`{gain:+d}`)\n**남은 자금:** `${info['money'] - 금액:,}`",
        color=discord.Color.green())
    if user.avatar:
        embed.set_thumbnail(url=user.avatar.url)
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="소매치기", description="[시민 전용] 행인의 지갑을 노립니다. 성공하면 돈을 얻지만 악한 성향이 쌓이고, 들키면 수배됩니다.")
async def pickpocket_command(interaction: discord.Interaction):
    if not await _citizen_only(interaction): return
    user = interaction.user
    left = _cd_left(pickpocket_cooldowns, user.id)
    if left > 0:
        await interaction.response.send_message(f"주변을 살피는 중입니다. **{left}초** 뒤에 다시 시도할 수 있습니다.", ephemeral=True)
        return
    pickpocket_cooldowns[user.id] = time.time() + PICKPOCKET_COOLDOWN_SEC
    await interaction.response.defer()

    target = random.choice(["관광객", "술 취한 선원", "바쁜 회사원", "시장 상인"])
    try:
        if random.random() < PICKPOCKET_SUCCESS:
            loot = random.randint(*PICKPOCKET_REWARD)
            info, blob = apply_citizen_activity(user.id, loot, PICKPOCKET_KARMA_OK, {"pickpocket_ok": 1})
            embed = discord.Embed(
                title="[소매치기] 성공",
                description=f"**{user.mention}** 님이 **{target}** 의 지갑을 감쪽같이 털었습니다.\n\n"
                            f"**챙긴 돈:** `${loot:,}`\n**성향:** {alignment_text(blob['karma'])} (`{PICKPOCKET_KARMA_OK:+d}`)",
                color=discord.Color.dark_grey())
        else:
            fine = min(int(get_user_data(user.id)["money"]), PICKPOCKET_FINE)
            info, blob = apply_citizen_activity(user.id, -fine, PICKPOCKET_KARMA_FAIL, {"pickpocket_fail": 1})
            wanted_list[user.id] = datetime.now() + timedelta(seconds=PICKPOCKET_WANTED_SEC)
            save_status(user.id)
            embed = discord.Embed(
                title="[소매치기] 현장에서 들킴",
                description=f"**{user.mention}** 님이 **{target}** 에게 손목을 붙잡혔습니다!\n\n"
                            f"**합의금:** `-${fine:,}`\n**성향:** {alignment_text(blob['karma'])} (`{PICKPOCKET_KARMA_FAIL:+d}`)\n"
                            f"**{PICKPOCKET_WANTED_SEC}초간 지명수배** 상태가 됩니다. 경찰에게 붙잡히기 전에 몸을 숨기세요!",
                color=discord.Color.red())
    except Exception as e:
        print(f"DB Error (pickpocket): {e}")
        pickpocket_cooldowns.pop(user.id, None)
        await interaction.followup.send("처리 중 오류가 발생했습니다.", ephemeral=True)
        return
    if user.avatar:
        embed.set_thumbnail(url=user.avatar.url)
    await interaction.followup.send(embed=embed)
