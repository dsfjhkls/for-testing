"""시민·도박·가입: 아르바이트·봉사·기부·소매치기, 부패 경찰, /도박, 가입·탈퇴 화면

합친 모듈: civilian.py, corrupt_police.py, gamble.py, join_leave.py
"""

import asyncio
import discord
import random
import time
from datetime import datetime, timedelta
from discord import app_commands

from .core import bot, GuardedView, inc_range, jail_list, supabase, wanted_list
from .status import (
    _check_updated,
    _daily_notices,
    _is_citizen,
    _require_db,
    alignment_text,
    check_jail,
    CITIZEN_JOIN_GATING,
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
    daily_record,
    delete_user_data,
    DONATE_COOLDOWN_SEC,
    DONATE_MAX_KARMA,
    DONATE_MIN,
    DONATE_UNIT,
    evaluate_join,
    evaluate_new_titles,
    format_new_titles,
    get_title_blob,
    get_user_data,
    join_requirement_text,
    karma_change,
    mission_cooldowns,
    PARTTIME_COOLDOWN_SEC,
    PARTTIME_KARMA,
    PICKPOCKET_COOLDOWN_SEC,
    PICKPOCKET_FINE,
    PICKPOCKET_KARMA_FAIL,
    PICKPOCKET_KARMA_OK,
    PICKPOCKET_REWARD,
    PICKPOCKET_SUCCESS,
    PICKPOCKET_WANTED_SEC,
    save_status,
    save_title_blob,
    SERVICE_COOLDOWN_SEC,
    SERVICE_KARMA,
    SERVICE_REWARD,
    take_daily_notice,
    update_user_location,
    update_user_stats,
)
from .admin import check_maintenance


# ==========================================================================
# [합친 모듈] civilian.py
#   시민 활동 (/아르바이트 /봉사 /기부 /소매치기)
# ==========================================================================
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


# ==========================================================================
# [합친 모듈] corrupt_police.py
#   부패 경찰 (/갈취 등)
# ==========================================================================
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
    from .family import _is_jailed  # 순환 import 방지: 호출 시점에 가져옴
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


# ==========================================================================
# [합친 모듈] gamble.py
#   도박 시스템 (/도박 ...)
# ==========================================================================
# ==========================================
# 도박 시스템 (카지노)
# ==========================================
GAMBLE_MIN_BET = 1000
GAMBLE_MAX_BET = 50000
GAMBLE_COOLDOWN = 3
COIN_WIN_RATE = 0.48

SLOT_SYMBOLS = [
    ("♣", 30, 3),
    ("♦", 25, 5),
    ("♥", 20, 8),
    ("★", 10, 20),
    ("7", 5, 100),
]
SLOT_PAIR_MULT = 1.0
SLOT_JACKPOT_SYMBOL = "7"

BLACKJACK_PAYOUT = {"blackjack": 2.5, "win": 2.0, "push": 1.0, "lose": 0}

gamble_cooldowns = {}
gamble_locks = {}
active_blackjack = {}

class InsufficientFunds(Exception):
    pass

def _gamble_lock(user_id: int) -> asyncio.Lock:
    lock = gamble_locks.get(user_id)
    if lock is None:
        lock = gamble_locks[user_id] = asyncio.Lock()
    return lock

def fmt_net(n: int) -> str:
    return f"{'+' if n >= 0 else '-'}${abs(n):,}"

def settle_gamble(user_id: int, bet: int, payout: int, info: dict = None, prepaid: bool = False, jackpot: bool = False):
    _require_db()
    if info is None:
        info = get_user_data(user_id)
    money = int(info.get("money") or 0)

    if not prepaid and money < bet:
        raise InsufficientFunds(money)

    new_money = money + (payout if prepaid else payout - bet)

    blob = get_title_blob(info)
    s = blob["stats"]
    s["gamble_played"] += 1
    s["gamble_wagered"] += bet
    s["gamble_net"] += payout - bet
    if payout > bet:
        s["gamble_won"] += 1
    if jackpot:
        s["jackpot"] += 1
    if new_money <= 0:
        s["broke"] += 1
    daily_msg = daily_record(blob, "gamble", {**info, "money": new_money})   # 일일 의뢰 집계 (도박 1판)
    new_ids = evaluate_new_titles(blob, {**info, "money": new_money})

    res = supabase.table("user_data").update({"money": new_money, "data": blob}).eq("user_id", str(user_id)).execute()
    _check_updated(res, "settle_gamble")
    if daily_msg:
        _daily_notices[user_id] = daily_msg   # 저장에 성공했을 때만 안내 문구를 남김
    return new_money, new_ids

def take_bet(user_id: int, bet: int) -> int:
    _require_db()
    info = get_user_data(user_id)
    money = int(info.get("money") or 0)
    if money < bet:
        raise InsufficientFunds(money)
    res = supabase.table("user_data").update({"money": money - bet}).eq("user_id", str(user_id)).execute()
    _check_updated(res, "take_bet")
    return money - bet

async def gamble_precheck(interaction: discord.Interaction) -> bool:
    if not await check_maintenance(interaction): return False
    if await check_jail(interaction): return False

    now = datetime.now()
    next_ok = gamble_cooldowns.get(interaction.user.id)
    if next_ok and now < next_ok:
        remaining = int((next_ok - now).total_seconds()) + 1
        await interaction.response.send_message(f"잠시 후 다시 베팅할 수 있습니다. (**{remaining}초**)", ephemeral=True)
        return False
    gamble_cooldowns[interaction.user.id] = now + timedelta(seconds=GAMBLE_COOLDOWN)
    return True

async def _safe_edit(interaction: discord.Interaction, **kwargs):
    try:
        await interaction.edit_original_response(**kwargs)
    except discord.HTTPException:
        pass

def spin_slots() -> list:
    return random.choices([s[0] for s in SLOT_SYMBOLS], weights=[s[1] for s in SLOT_SYMBOLS], k=3)

def slot_multiplier(reels: list) -> float:
    if reels[0] == reels[1] == reels[2]:
        for symbol, _, mult in SLOT_SYMBOLS:
            if symbol == reels[0]:
                return float(mult)
    if len(set(reels)) == 2:
        return SLOT_PAIR_MULT
    return 0.0

def slot_rtp() -> float:
    total_w = sum(w for _, w, _ in SLOT_SYMBOLS)
    rtp = 0.0
    for a in SLOT_SYMBOLS:
        for b in SLOT_SYMBOLS:
            for c in SLOT_SYMBOLS:
                p = (a[1] / total_w) * (b[1] / total_w) * (c[1] / total_w)
                rtp += p * slot_multiplier([a[0], b[0], c[0]])
    return rtp

def slot_board(reels: list) -> str:
    return "┃ " + " ┃ ".join(reels) + " ┃"

CARD_RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]
CARD_SUITS = ["♠", "♥", "♦", "♣"]

def new_deck() -> list:
    deck = [(r, s) for r in CARD_RANKS for s in CARD_SUITS]
    random.shuffle(deck)
    return deck

def hand_value(hand: list) -> int:
    total = 0
    aces = 0
    for rank, _ in hand:
        if rank == "A":
            total += 11
            aces += 1
        elif rank in ("J", "Q", "K"):
            total += 10
        else:
            total += int(rank)
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total

def is_blackjack(hand: list) -> bool:
    return len(hand) == 2 and hand_value(hand) == 21

def card_str(card: tuple) -> str:
    return f"**{card[0]}{card[1]}**"

def hand_str(hand: list) -> str:
    return " ".join(card_str(c) for c in hand)

def blackjack_outcome(player: list, dealer: list) -> str:
    p, d = hand_value(player), hand_value(dealer)
    if p > 21:
        return "lose"
    if is_blackjack(player):
        return "push" if is_blackjack(dealer) else "blackjack"
    if is_blackjack(dealer):
        return "lose"
    if d > 21 or p > d:
        return "win"
    if p == d:
        return "push"
    return "lose"

class BlackjackView(GuardedView):
    holds_command_lock = True   # 블랙잭 테이블이 끝날 때까지 다른 명령어 실행 차단

    def __init__(self, owner: discord.Member, bet: int):
        super().__init__(timeout=60)
        self.owner = owner
        self.bet = bet
        self.deck = new_deck()
        self.player = [self.deck.pop(), self.deck.pop()]
        self.dealer = [self.deck.pop(), self.deck.pop()]
        self.finished = False
        self.busy = False
        self.message = None

    def build_embed(self, reveal: bool = False, outcome: str = None, payout: int = 0,
                    money: int = 0, new_ids: list = None, note: str = "") -> discord.Embed:
        colors = {
            None: discord.Color.dark_green(), "blackjack": discord.Color.gold(),
            "win": discord.Color.green(), "push": discord.Color.blue(), "lose": discord.Color.red()
        }
        embed = discord.Embed(title="[블랙잭]", color=colors[outcome])

        dealer_text = (f"{hand_str(self.dealer)}  (합계 **{hand_value(self.dealer)}**)" if reveal
                       else f"{card_str(self.dealer[0])} **[?]**")
        embed.add_field(name=f"{self.owner.display_name}의 패 (합계 {hand_value(self.player)})", value=hand_str(self.player), inline=False)
        embed.add_field(name="딜러의 패", value=dealer_text, inline=False)

        if outcome is None:
            embed.description = f"베팅금 **${self.bet:,}**\n**히트**(카드 받기) 또는 **스탠드**(멈추기)를 선택하세요. (60초 이내)"
        else:
            if note == "timeout":
                msg = "**시간 초과** — 자동으로 패배 처리되었습니다."
            elif outcome == "blackjack":
                msg = "**블랙잭!** 3:2 배당으로 지급됩니다."
            elif outcome == "win":
                msg = "**승리!**" + (" 딜러가 버스트했습니다." if hand_value(self.dealer) > 21 else "")
            elif outcome == "push":
                msg = "**무승부** — 베팅금이 그대로 돌아옵니다."
            else:
                msg = "**버스트!** 21을 초과했습니다." if hand_value(self.player) > 21 else "**패배**"
            net = payout - self.bet
            embed.description = (f"{msg}\n\n베팅금: `${self.bet:,}`\n지급액: `${payout:,}`\n"
                                 f"손익: `{fmt_net(net)}`\n보유 자금: `${money:,}`{format_new_titles(new_ids or [])}")

        embed.set_footer(text="딜러는 16 이하에서 카드를 받고 17 이상에서 멈춥니다 · 블랙잭 3:2")
        if self.owner.avatar:
            embed.set_thumbnail(url=self.owner.avatar.url)
        return embed

    async def finish(self, edit, outcome: str, note: str = ""):
        self.finished = True
        self.stop()
        active_blackjack.pop(self.owner.id, None)

        payout = int(self.bet * BLACKJACK_PAYOUT[outcome])
        try:
            async with _gamble_lock(self.owner.id):
                new_money, new_ids = settle_gamble(self.owner.id, self.bet, payout, prepaid=True)
        except Exception as e:
            print(f"DB Error (blackjack settle): {e}")
            await edit(embed=discord.Embed(
                title="[블랙잭 정산 오류]",
                description="데이터베이스 오류로 결과를 정산하지 못했습니다. 관리자에게 문의해주세요.",
                color=discord.Color.red()), view=None)
            return

        final_embed = self.build_embed(reveal=True, outcome=outcome, payout=payout,
                                       money=new_money, new_ids=new_ids, note=note)
        final_embed.description = (final_embed.description or "") + take_daily_notice(self.owner.id)
        await edit(embed=final_embed, view=None)

    async def dealer_turn(self, edit):
        while hand_value(self.dealer) < 17:
            self.dealer.append(self.deck.pop())
        await self.finish(edit, blackjack_outcome(self.player, self.dealer))

    async def _guard(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner.id:
            await interaction.response.send_message("본인의 블랙잭 테이블만 조작할 수 있습니다.", ephemeral=True)
            return False
        await interaction.response.defer()
        return not (self.finished or self.busy)

    @discord.ui.button(label="히트 (카드 받기)", style=discord.ButtonStyle.primary)
    async def hit(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction): return
        self.busy = True
        try:
            self.player.append(self.deck.pop())
            value = hand_value(self.player)
            if value > 21:
                await self.finish(interaction.edit_original_response, "lose")
            elif value == 21:
                await self.dealer_turn(interaction.edit_original_response)
            else:
                await interaction.edit_original_response(embed=self.build_embed(), view=self)
        finally:
            self.busy = False

    @discord.ui.button(label="스탠드 (멈추기)", style=discord.ButtonStyle.secondary)
    async def stand(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction): return
        self.busy = True
        try:
            await self.dealer_turn(interaction.edit_original_response)
        finally:
            self.busy = False

    async def on_timeout(self):
        if self.finished or self.busy or self.message is None:
            return
        await self.finish(self.message.edit, "lose", note="timeout")

# ==========================================
# /도박 명령어 그룹
# ==========================================
gamble_group = app_commands.Group(name="도박", description="[카지노] 자금을 걸고 도박을 즐깁니다.")
bot.tree.add_command(gamble_group)

BET_DESC = f"베팅할 금액 (${GAMBLE_MIN_BET:,} ~ ${GAMBLE_MAX_BET:,})"

@gamble_group.command(name="슬롯", description="슬롯머신을 돌립니다. (3개 일치 고배당 / 2개 일치 본전)")
@app_commands.describe(베팅금=BET_DESC)
async def gamble_slots(interaction: discord.Interaction, 베팅금: app_commands.Range[int, GAMBLE_MIN_BET, GAMBLE_MAX_BET]):
    if not await gamble_precheck(interaction): return

    user = interaction.user
    bet = 베팅금

    money = int(get_user_data(user.id).get("money") or 0)
    if money < bet:
        gamble_cooldowns.pop(user.id, None)
        await interaction.response.send_message(f"자금이 부족합니다! (보유 자금: ${money:,})", ephemeral=True)
        return

    await interaction.response.defer()

    reels = spin_slots()
    mult = slot_multiplier(reels)
    payout = int(bet * mult)
    jackpot = (reels[0] == reels[1] == reels[2] == SLOT_JACKPOT_SYMBOL)

    try:
        async with _gamble_lock(user.id):
            new_money, new_ids = settle_gamble(user.id, bet, payout, jackpot=jackpot)
    except InsufficientFunds:
        gamble_cooldowns.pop(user.id, None)
        await interaction.edit_original_response(content="자금이 부족합니다!")
        return
    except Exception as e:
        print(f"DB Error (slots): {e}")
        gamble_cooldowns.pop(user.id, None)
        await interaction.edit_original_response(content="데이터베이스 오류로 베팅이 취소되었습니다. (자금은 차감되지 않았습니다)")
        return

    spinning = discord.Embed(title="[슬롯머신]", description=slot_board(["?", "?", "?"]) + f"\n\n베팅금 `${bet:,}`", color=discord.Color.dark_gold())
    await _safe_edit(interaction, embed=spinning)
    await asyncio.sleep(0.8)
    spinning.description = slot_board([reels[0], "?", "?"]) + f"\n\n베팅금 `${bet:,}`"
    await _safe_edit(interaction, embed=spinning)
    await asyncio.sleep(0.8)
    spinning.description = slot_board([reels[0], reels[1], "?"]) + f"\n\n베팅금 `${bet:,}`"
    await _safe_edit(interaction, embed=spinning)
    await asyncio.sleep(0.8)

    if jackpot:
        msg, color = "**JACKPOT!!!** 7⃣7⃣7⃣ 대박이 터졌습니다!", discord.Color.gold()
    elif mult >= 2:
        msg, color = f"**{reels[0]} 3개 일치!** 배당 x{mult:g}", discord.Color.green()
    elif mult == SLOT_PAIR_MULT and mult > 0:
        msg, color = "**2개 일치** — 베팅금이 그대로 돌아옵니다.", discord.Color.blue()
    else:
        msg, color = "**꽝!** 베팅금을 잃었습니다.", discord.Color.red()

    embed = discord.Embed(
        title="[슬롯머신] 결과",
        description=(f"{slot_board(reels)}\n\n{msg}\n\n베팅금: `${bet:,}`\n지급액: `${payout:,}`\n"
                     f"손익: `{fmt_net(payout - bet)}`\n보유 자금: `${new_money:,}`{format_new_titles(new_ids)}{take_daily_notice(user.id)}"),
        color=color
    )
    if user.avatar:
        embed.set_thumbnail(url=user.avatar.url)
    await _safe_edit(interaction, embed=embed)

@gamble_group.command(name="동전", description="동전 던지기에 베팅합니다. (승리 시 2배)")
@app_commands.describe(베팅금=BET_DESC, 선택="동전의 어느 면에 걸까요?")
@app_commands.choices(선택=[
    app_commands.Choice(name="앞면", value="front"),
    app_commands.Choice(name="뒷면", value="back")
])
async def gamble_coin(interaction: discord.Interaction, 베팅금: app_commands.Range[int, GAMBLE_MIN_BET, GAMBLE_MAX_BET], 선택: app_commands.Choice[str]):
    if not await gamble_precheck(interaction): return

    user = interaction.user
    bet = 베팅금

    money = int(get_user_data(user.id).get("money") or 0)
    if money < bet:
        gamble_cooldowns.pop(user.id, None)
        await interaction.response.send_message(f"자금이 부족합니다! (보유 자금: ${money:,})", ephemeral=True)
        return

    await interaction.response.defer()

    names = {"front": "앞면", "back": "뒷면"}
    won = random.random() < COIN_WIN_RATE
    result_side = 선택.value if won else ("back" if 선택.value == "front" else "front")
    payout = bet * 2 if won else 0

    try:
        async with _gamble_lock(user.id):
            new_money, new_ids = settle_gamble(user.id, bet, payout)
    except InsufficientFunds:
        gamble_cooldowns.pop(user.id, None)
        await interaction.edit_original_response(content="자금이 부족합니다!")
        return
    except Exception as e:
        print(f"DB Error (coin): {e}")
        gamble_cooldowns.pop(user.id, None)
        await interaction.edit_original_response(content="데이터베이스 오류로 베팅이 취소되었습니다. (자금은 차감되지 않았습니다)")
        return

    flipping = discord.Embed(
        title="[동전 던지기]",
        description=f"{user.mention} 님이 **{names[선택.value]}**에 `${bet:,}` 를 걸었습니다!\n\n동전이 공중에서 회전 중...",
        color=discord.Color.dark_gold()
    )
    await _safe_edit(interaction, embed=flipping)
    await asyncio.sleep(1.2)

    embed = discord.Embed(
        title="[동전 던지기] 결과",
        description=(f"결과: **{names[result_side]}**\n"
                     f"{'**적중!** 베팅금의 2배를 받았습니다.' if won else '**빗나갔습니다.** 베팅금을 잃었습니다.'}\n\n"
                     f"베팅금: `${bet:,}`\n지급액: `${payout:,}`\n"
                     f"손익: `{fmt_net(payout - bet)}`\n보유 자금: `${new_money:,}`{format_new_titles(new_ids)}{take_daily_notice(user.id)}"),
        color=discord.Color.green() if won else discord.Color.red()
    )
    if user.avatar:
        embed.set_thumbnail(url=user.avatar.url)
    await _safe_edit(interaction, embed=embed)

@gamble_group.command(name="블랙잭", description="딜러와 블랙잭 승부를 겨룹니다. (블랙잭 3:2 / 승리 2배)")
@app_commands.describe(베팅금=BET_DESC)
async def gamble_blackjack(interaction: discord.Interaction, 베팅금: app_commands.Range[int, GAMBLE_MIN_BET, GAMBLE_MAX_BET]):
    if not await gamble_precheck(interaction): return

    user = interaction.user
    bet = 베팅금

    if user.id in active_blackjack:
        gamble_cooldowns.pop(user.id, None)
        await interaction.response.send_message("이미 진행 중인 블랙잭 테이블이 있습니다! 먼저 끝내주세요.", ephemeral=True)
        return

    money = int(get_user_data(user.id).get("money") or 0)
    if money < bet:
        gamble_cooldowns.pop(user.id, None)
        await interaction.response.send_message(f"자금이 부족합니다! (보유 자금: ${money:,})", ephemeral=True)
        return

    await interaction.response.defer()

    try:
        async with _gamble_lock(user.id):
            take_bet(user.id, bet)
    except InsufficientFunds:
        gamble_cooldowns.pop(user.id, None)
        await interaction.edit_original_response(content="자금이 부족합니다!")
        return
    except Exception as e:
        print(f"DB Error (blackjack take_bet): {e}")
        gamble_cooldowns.pop(user.id, None)
        await interaction.edit_original_response(content="데이터베이스 오류로 베팅이 취소되었습니다. (자금은 차감되지 않았습니다)")
        return

    view = BlackjackView(owner=user, bet=bet)
    active_blackjack[user.id] = view

    if is_blackjack(view.player) or is_blackjack(view.dealer):
        view.message = await interaction.edit_original_response(embed=view.build_embed())
        await view.finish(interaction.edit_original_response, blackjack_outcome(view.player, view.dealer))
    else:
        view.message = await interaction.edit_original_response(embed=view.build_embed(), view=view)

@gamble_group.command(name="규칙", description="카지노 규칙·배당표와 나의 도박 기록을 확인합니다.")
async def gamble_rules(interaction: discord.Interaction):
    if not await check_maintenance(interaction): return

    s = get_title_blob(get_user_data(interaction.user.id))["stats"]
    paytable = "\n".join(f"{e}{e}{e} → **x{m}**" for e, _, m in SLOT_SYMBOLS)
    net = s["gamble_net"]

    embed = discord.Embed(
        title="[칼렌데 카지노 규칙]",
        description=f"베팅 한도: **${GAMBLE_MIN_BET:,} ~ ${GAMBLE_MAX_BET:,}** · 베팅 간격 {GAMBLE_COOLDOWN}초\n"
                    f"도박은 평균적으로 자금이 줄어듭니다. 감옥에서는 이용할 수 없습니다.",
        color=discord.Color.dark_gold()
    )
    embed.add_field(
        name="`/도박 슬롯`",
        value=f"{paytable}\n같은 그림 **2개** → 베팅금 반환 / 모두 다르면 꽝\n환수율 약 **{slot_rtp() * 100:.1f}%**",
        inline=False
    )
    embed.add_field(name="`/도박 동전`", value=f"앞면/뒷면 중 선택 · 적중 시 **2배** (승률 **{COIN_WIN_RATE * 100:.0f}%**)", inline=False)
    embed.add_field(
        name="`/도박 블랙잭`",
        value="21에 가까운 쪽이 승리 (21 초과 시 버스트)\n승리 **2배** · 블랙잭(처음 2장 21) **2.5배** · 무승부 베팅금 반환\n딜러는 16 이하 히트 / 17 이상 스탠드 · 60초 내 미선택 시 패배",
        inline=False
    )
    embed.add_field(
        name="나의 도박 기록",
        value=f"총 `{s['gamble_played']}`판 / 승리 `{s['gamble_won']}`판 / 누적 베팅 `${s['gamble_wagered']:,}`\n누적 손익 `{fmt_net(net)}` / 잭팟 `{s['jackpot']}`회",
        inline=False
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ==========================================================================
# [합친 모듈] join_leave.py
#   패밀리 가입·탈퇴 화면
# ==========================================================================
from . import admin as _admin

# === 1. 셀프 가입 버튼 뷰 ===
class SelfJoinView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="마피아 조직 입단", style=discord.ButtonStyle.danger, custom_id="join_mafia_btn")
    async def join_mafia(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.process_join(interaction, target_role_name="조직원", rank_title="마피아 조직원")

    @discord.ui.button(label="치안국 경찰 임용", style=discord.ButtonStyle.primary, custom_id="join_police_btn")
    async def join_police(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.process_join(interaction, target_role_name="경찰", rank_title="순경")

    async def process_join(self, interaction: discord.Interaction, target_role_name: str, rank_title: str):
        if _admin.MAINTENANCE_MODE and not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("현재 점검 중입니다.", ephemeral=True)
            return

        user = interaction.user
        citizen_role = discord.utils.get(interaction.guild.roles, name="시민")
        target_role = discord.utils.get(interaction.guild.roles, name=target_role_name)
        
        if not target_role:
            await interaction.response.send_message(f"서버에 '{target_role_name}' 역할이 없습니다! 관리자에게 문의하세요.", ephemeral=True)
            return

        mafia_role = discord.utils.get(interaction.guild.roles, name="조직원")
        police_role = discord.utils.get(interaction.guild.roles, name="경찰")

        if (mafia_role and mafia_role in user.roles) or (police_role and police_role in user.roles):
            await interaction.response.send_message("당신은 이미 소속(마피아 또는 경찰)이 결정되었습니다!", ephemeral=True)
            return

        # [전직 조건] 시민 시절의 성향·활동 기록으로 가입 가능 여부를 판단
        corrupt, fee = False, 0
        join_info = get_user_data(user.id)
        join_blob = get_title_blob(join_info)
        if CITIZEN_JOIN_GATING:
            check = evaluate_join(target_role_name, join_info, join_blob)
            if not check["ok"]:
                lines = [f"{'[충족]' if ok else '[미달]'} {name}  ({now_text})" for name, ok, now_text in check["checks"]]
                head = "부패 경찰 임용 조건" if check["corrupt"] else f"{target_role_name} 가입 조건"
                embed = discord.Embed(
                    title=f"[{head}을 아직 채우지 못했습니다]",
                    description=f"현재 성향: **{alignment_text(join_blob['karma'])}**\n\n" + "\n".join(lines)
                                + "\n\n" + join_requirement_text(),
                    color=discord.Color.dark_red())
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return
            corrupt, fee = check["corrupt"], check["fee"]

        try:   # 비용·부패 표시는 역할 변경 전에 저장 (역할 변경이 실패하면 되돌림)
            if fee:
                update_user_stats(user.id, -fee, 0)
            join_blob["corrupt"] = corrupt
            save_title_blob(user.id, join_blob)
        except Exception as e:
            print(f"DB Error (join): {e}")
            await interaction.response.send_message("가입 처리 중 오류가 발생했습니다. 잠시 뒤 다시 시도해 주세요.", ephemeral=True)
            return

        try:
            if citizen_role and citizen_role in user.roles:
                await user.remove_roles(citizen_role)
            await user.add_roles(target_role)
        except discord.Forbidden:
            try:
                if fee:
                    update_user_stats(user.id, fee, 0)
                join_blob["corrupt"] = False
                save_title_blob(user.id, join_blob)
            except Exception as e:
                print(f"Join rollback error: {e}")
            await interaction.response.send_message("봇의 역할 권한 순서를 올려주세요.", ephemeral=True)
            return

        user_stats = get_user_data(user.id)

        embed = discord.Embed(
            title=f"[{rank_title} 발령/입단]",
            description=f"**{user.mention}** 님이 시민의 삶을 마치고 **{target_role_name}**(으)로 새로운 활동을 시작합니다!\n\n"
                        f"**신분 변화:** `시민` `{target_role_name}`",
            color=discord.Color.gold()
        )
        if user.avatar:
            embed.set_thumbnail(url=user.avatar.url)

        await interaction.response.send_message(embed=embed)
        if corrupt:   # 부패 경찰 임용은 본인에게만 안내 (프로필에는 '부패 경찰'로 표시됨)
            await interaction.followup.send(
                f"로비 자금 `${fee:,}` 을(를) 써서 **부패 경찰**로 임용되었습니다.\n"
                f"• `/검거` 로 수배 중이 아닌 **시민**도 체포하고, `/갈취` 로 시민의 돈을 뜯어낼 수 있습니다.\n"
                f"• 들키면(민원 접수) 수배되어 정식 경찰에게 체포당합니다. 프로필(`/프로필`)에는 **부패 경찰**로 표시됩니다.",
                ephemeral=True)

# === 2. 탈퇴 확인/취소 버튼 뷰 ===
class LeaveConfirmView(discord.ui.View):
    def __init__(self, target_user: discord.Member):
        super().__init__(timeout=60)
        self.target_user = target_user

    @discord.ui.button(label="네, 탈퇴하겠습니다", style=discord.ButtonStyle.danger, custom_id="confirm_leave")
    async def confirm_leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        from .police_unit import police_unit_detach_user  # 순환 import 방지: 호출 시점에 가져옴
        from .family import family_detach_user  # 순환 import 방지: 호출 시점에 가져옴
        if interaction.user != self.target_user:
            await interaction.response.send_message("본인의 탈퇴 확인 창만 조작할 수 있습니다.", ephemeral=True)
            return

        await interaction.response.defer()

        member = interaction.user
        mafia_role = discord.utils.get(interaction.guild.roles, name="조직원")
        police_role = discord.utils.get(interaction.guild.roles, name="경찰")
        citizen_role = discord.utils.get(interaction.guild.roles, name="시민")

        try:
            if mafia_role and mafia_role in member.roles:
                await member.remove_roles(mafia_role)
            if police_role and police_role in member.roles:
                await member.remove_roles(police_role)
                
            if citizen_role and citizen_role not in member.roles:
                await member.add_roles(citizen_role)
        except discord.Forbidden:
            await interaction.followup.send("역할 변경 권한이 없습니다.", ephemeral=True)
            return

        try:
            family_detach_user(member.id)   # 조직에서도 제거
        except Exception as e:
            print(f"Family detach error: {e}")

        try:
            police_unit_detach_user(member.id)   # 경찰 수사팀에서도 제거
        except Exception as e:
            print(f"Police unit detach error: {e}")

        try:
            delete_user_data(member.id)
        except Exception:
            pass

        mission_cooldowns.pop(member.id, None)
        parttime_cooldowns.pop(member.id, None)
        for _t in (service_cooldowns, donate_cooldowns, pickpocket_cooldowns, corrupt_arrest_cooldowns, corrupt_extort_cooldowns):
            _t.pop(member.id, None)
        gamble_cooldowns.pop(member.id, None)
        open_table = active_blackjack.pop(member.id, None)
        if open_table:
            open_table.finished = True
            open_table.stop()

        for child in self.children:
            child.disabled = True

        embed = discord.Embed(
            title="[소속 해임 및 세이브 데이터 초기화]",
            description=(
                f"**{member.mention}** 님이 소속을 최종 탈퇴하셨습니다.\n\n"
                f"**초기화 완료:** 모든 보유 자금, 명예, 인벤토리 아이템 및 기록이 DB에서 완전히 삭제되었습니다.\n"
                f"**신분 변화:** `마피아/경찰` `시민`"
            ),
            color=discord.Color.dark_gray()
        )
        if member.avatar:
            embed.set_thumbnail(url=member.avatar.url)

        await interaction.edit_original_response(embed=embed, view=self)

    @discord.ui.button(label="취소 (소속 잔류)", style=discord.ButtonStyle.secondary, custom_id="cancel_leave")
    async def cancel_leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.target_user:
            await interaction.response.send_message("본인의 탈퇴 확인 창만 조작할 수 있습니다.", ephemeral=True)
            return

        for child in self.children:
            child.disabled = True

        embed = discord.Embed(
            title="[탈퇴 철회]",
            description="탈퇴 신청이 취소되었습니다.",
            color=discord.Color.blue()
        )
        await interaction.edit_original_response(embed=embed, view=self)
