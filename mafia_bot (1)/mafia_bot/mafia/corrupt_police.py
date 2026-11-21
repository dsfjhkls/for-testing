"""부패 경찰 (/갈취 등)"""

import discord
import random
import time
from discord import app_commands
from datetime import datetime, timedelta

from .core import bot, jail_list, wanted_list
from .status import check_jail, get_user_data, save_status, update_user_location, update_user_stats
from .alignment import (
    CORRUPT_ARREST_COOLDOWN_SEC,
    CORRUPT_ARREST_FINE,
    CORRUPT_ARREST_JAIL_SEC,
    CORRUPT_COMPLAINT_CHANCE,
    CORRUPT_COMPLAINT_WANTED_SEC,
    CORRUPT_EXTORT_COOLDOWN_SEC,
    CORRUPT_EXTORT_FAIL_WANTED_SEC,
    CORRUPT_EXTORT_MAX,
    CORRUPT_EXTORT_MIN_TARGET_MONEY,
    CORRUPT_EXTORT_PCT,
    CORRUPT_EXTORT_SUCCESS,
    CORRUPT_VICTIM_SHIELD_SEC,
    _is_citizen,
    get_title_blob,
    save_title_blob,
)
from .admin import check_maintenance
from .civilian import _cd_left

# ==========================================================================
# [부패 경찰] 악 성향으로 임용된 경찰: /검거 로 무차별 체포, /갈취 로 시민 금품 갈취
# ==========================================================================
corrupt_arrest_cooldowns = {}
corrupt_extort_cooldowns = {}
corrupt_victim_until = {}      # {시민 id: 시각(epoch)} — 최근에 당한 시민 보호

def is_corrupt_police(member: discord.Member) -> bool:
    if "경찰" not in {r.name for r in member.roles}:
        return False
    try:
        return bool(get_title_blob(get_user_data(member.id)).get("corrupt"))
    except Exception:
        return False

def _corrupt_victim_ok(target: discord.Member) -> str:
    """부패 경찰이 건드릴 수 있는 시민인지 검사. 문제가 있으면 안내 문구, 괜찮으면 빈 문자열"""
    from .family import _is_jailed   # 순환 import 방지: 호출 시점에 가져옴
    if target.bot:
        return "봇은 건드릴 수 없습니다."
    if not _is_citizen(target):
        return "시민(소속이 없는 사람)만 대상으로 삼을 수 있습니다."
    if _is_jailed(target.id):
        return "이미 감옥에 있는 사람입니다."
    if corrupt_victim_until.get(target.id, 0) > time.time():
        return "그 시민은 최근에 당한 탓에 경계가 삼엄합니다. 잠시 뒤에 노려 보세요."
    return ""

def _corrupt_complaint(cop: discord.Member, sec: int):
    """민원이 접수되어 부패 경찰이 수배 상태가 됨 (정식 경찰이 /검거 할 수 있음)"""
    wanted_list[cop.id] = datetime.now() + timedelta(seconds=sec)
    save_status(cop.id)

async def try_corrupt_arrest(interaction: discord.Interaction, target: discord.Member) -> bool:
    """/검거 에서 대상이 수배 중이 아닐 때 호출: 부패 경찰이 시민을 무차별 체포. 처리했으면 True"""
    cop = interaction.user
    if target.id == cop.id or not is_corrupt_police(cop) or not _is_citizen(target):
        return False
    left = _cd_left(corrupt_arrest_cooldowns, cop.id)
    if left > 0:
        await interaction.followup.send(f"방금 단속을 했습니다. **{left}초** 뒤에 다시 체포할 수 있습니다.", ephemeral=True)
        return True
    problem = _corrupt_victim_ok(target)
    if problem:
        await interaction.followup.send(problem, ephemeral=True)
        return True
    corrupt_arrest_cooldowns[cop.id] = time.time() + CORRUPT_ARREST_COOLDOWN_SEC

    now = datetime.now()
    jail_list[target.id] = now + timedelta(seconds=CORRUPT_ARREST_JAIL_SEC)
    wanted_list.pop(target.id, None)
    save_status(target.id)
    try:
        update_user_location(target.id, "감옥")
    except Exception as e:
        print(f"Location update error: {e}")

    take = 0
    try:
        victim_money = int(get_user_data(target.id).get("money", 0))
        take = min(victim_money, random.randint(*CORRUPT_ARREST_FINE))
        if take:
            update_user_stats(target.id, -take, 0)
            update_user_stats(cop.id, take, 0)
        blob = get_title_blob(get_user_data(cop.id))
        blob["stats"]["corrupt_arrests"] += 1
        save_title_blob(cop.id, blob)
    except Exception as e:
        print(f"DB Error (corrupt arrest): {e}")
    corrupt_victim_until[target.id] = time.time() + CORRUPT_VICTIM_SHIELD_SEC

    complaint = random.random() < CORRUPT_COMPLAINT_CHANCE
    if complaint:
        _corrupt_complaint(cop, CORRUPT_COMPLAINT_WANTED_SEC)
    embed = discord.Embed(
        title="[치안국 단속] 시민 체포",
        description=(f"**{cop.mention}** 경찰관이 **{target.mention}** 님을 '수상한 행동' 혐의로 현장에서 체포했습니다.\n\n"
                     f"**수감:** {CORRUPT_ARREST_JAIL_SEC // 60}분 동안 감옥\n**벌금 징수:** `${take:,}`"
                     + (f"\n\n**[민원 접수]** 부당 체포 민원이 들어와 **{cop.display_name}** 경관에게 {CORRUPT_COMPLAINT_WANTED_SEC}초간 수배가 내려졌습니다!" if complaint else "")),
        color=discord.Color.dark_blue())
    if target.avatar:
        embed.set_thumbnail(url=target.avatar.url)
    await interaction.followup.send(embed=embed)
    return True

@bot.tree.command(name="갈취", description="[부패 경찰 전용] 시민에게 겁을 주어 돈을 뜯어냅니다. (들키면 수배)")
@app_commands.describe(member="뜯어낼 시민")
async def extort_command(interaction: discord.Interaction, member: discord.Member):
    if not await check_maintenance(interaction): return
    if await check_jail(interaction): return
    cop = interaction.user
    if not is_corrupt_police(cop):
        await interaction.response.send_message("부패 경찰만 사용할 수 있는 명령어입니다.", ephemeral=True)
        return
    if member.id == cop.id:
        await interaction.response.send_message("자기 자신은 갈취할 수 없습니다.", ephemeral=True)
        return
    problem = _corrupt_victim_ok(member)
    if problem:
        await interaction.response.send_message(problem, ephemeral=True)
        return
    left = _cd_left(corrupt_extort_cooldowns, cop.id)
    if left > 0:
        await interaction.response.send_message(f"방금 한탕을 했습니다. **{left}초** 뒤에 다시 시도하세요.", ephemeral=True)
        return
    victim_money = int(get_user_data(member.id).get("money", 0))
    if victim_money < CORRUPT_EXTORT_MIN_TARGET_MONEY:
        await interaction.response.send_message("털어 갈 만한 돈이 없는 시민입니다.", ephemeral=True)
        return
    corrupt_extort_cooldowns[cop.id] = time.time() + CORRUPT_EXTORT_COOLDOWN_SEC
    await interaction.response.defer(ephemeral=True)

    if random.random() < CORRUPT_EXTORT_SUCCESS:
        take = min(CORRUPT_EXTORT_MAX, max(1, int(victim_money * random.uniform(*CORRUPT_EXTORT_PCT))))
        try:
            update_user_stats(member.id, -take, 0)
            update_user_stats(cop.id, take, 0)
            blob = get_title_blob(get_user_data(cop.id))
            blob["stats"]["extortions"] += 1
            save_title_blob(cop.id, blob)
        except Exception as e:
            print(f"DB Error (extort): {e}")
            corrupt_extort_cooldowns.pop(cop.id, None)
            await interaction.followup.send("처리 중 오류가 발생했습니다.", ephemeral=True)
            return
        corrupt_victim_until[member.id] = time.time() + CORRUPT_VICTIM_SHIELD_SEC
        await interaction.followup.send(f"**{member.display_name}** 님을 위협해 `${take:,}` 을(를) 뜯어냈습니다.", ephemeral=True)
        try:
            await interaction.channel.send(embed=discord.Embed(
                title="[소문] 수상한 단속",
                description=f"**{member.mention}** 님이 어느 경찰에게 '보호비' 명목으로 돈을 뜯겼다는 소문이 돌고 있습니다...",
                color=discord.Color.dark_grey()))
        except Exception as e:
            print(f"[갈취 공지 실패] {type(e).__name__}: {e}")
    else:
        _corrupt_complaint(cop, CORRUPT_EXTORT_FAIL_WANTED_SEC)
        await interaction.followup.send(f"**{member.display_name}** 님이 소리를 질러 주변에 들켰습니다! {CORRUPT_EXTORT_FAIL_WANTED_SEC}초간 수배 상태가 됩니다.", ephemeral=True)
        try:
            await interaction.channel.send(embed=discord.Embed(
                title="[민원 접수] 경찰관의 갈취 시도",
                description=f"**{cop.mention}** 경찰관이 시민을 위협했다는 신고가 접수되어 **{CORRUPT_EXTORT_FAIL_WANTED_SEC}초간 수배**되었습니다. 정식 경찰은 `/검거` 하세요!",
                color=discord.Color.red()))
        except Exception as e:
            print(f"[민원 공지 실패] {type(e).__name__}: {e}")
