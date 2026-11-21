"""경찰 활동: 경찰상점·이동·암시장 입구, /작전, /검거 /순찰 /심문

합친 모듈: police_shop.py, police_ops.py, police_arrest.py
"""

import asyncio
import discord
import random
import time
from datetime import datetime, timedelta
from discord import app_commands

from .core import bot, inc_money, inc_range, jail_list, supabase, wanted_list
from .status import (
    _ach_lock,
    _check_updated,
    _require_db,
    active_patrols,
    BLACK_MARKET_ITEMS,
    check_jail,
    CORRUPT_ARREST_BONUS_HONOR,
    delete_user_data,
    evaluate_new_titles,
    format_new_titles,
    get_title_blob,
    get_user_data,
    get_user_rank,
    LOCATIONS,
    police_ach_bump,
    POLICE_BIG_FISH_HONOR,
    save_status,
    update_user_location,
    update_user_stats,
)
from .admin import check_maintenance, check_membership, SettingsView
from .mission import MarketView
from .economy import POLICE_SHOP_ITEMS


# ==========================================================================
# [합친 모듈] police_shop.py
#   치안국 보급품 / 경찰상점
# ==========================================================================
# --- 치안국 보급품 효과 ---
#   • 강화 방탄복 / 황금 배지 : 방어력 (compute_combat_stats 가 전투·결투·레이드·점령전에 모두 반영)
#   • 기동타격대 무전기       : /검거 포상금 배율, /작전 성공률 +5%
#   • 황금 배지               : /검거 · /순찰 · /심문(의견 일치) · /작전 명예 실적 배율
POLICE_RADIO_NAME = "기동타격대 무전기"
POLICE_BADGE_NAME = "특급 수사관 황금 배지"
POLICE_INSIGNIA_NAME = "치안총감 지휘 휘장"
POLICE_TERMINAL_NAME = "광역 수사망 단말기"
POLICE_INSIGNIA_HONOR_BONUS = 0.15   # 지휘 휘장: 명예 실적 +15% (황금 배지와 합산)
POLICE_TERMINAL_PRIZE_BONUS = 0.25   # 수사망 단말기: 구역 탈환 포상금 +25%
POLICE_TERMINAL_COST_DISCOUNT = 0.50 # 수사망 단말기: 구역 탈환 비용 할인
POLICE_RADIO_ARREST_BONUS = 0.20    # 무전기: 검거 포상금 +20%
POLICE_BADGE_HONOR_BONUS = 0.20     # 황금 배지: 명예 실적 +20%
ARREST_BASE_REWARD = inc_money(500)            # /검거 기본 포상금
ARREST_BASE_HONOR = 60              # /검거 기본 명예 실적
ARREST_HONOR_PER_POINT = 25         # 수배범 명예 25pt 당 검거 명예 +1  (거물 검거 보너스)
ARREST_HONOR_BONUS_MAX = 200        # 거물 검거 보너스 상한  → 검거 1회 명예 60 ~ 260

def police_bonuses(inventory) -> dict:
    inv = inventory or {}
    return {
        "money_mult": 1.0 + (POLICE_RADIO_ARREST_BONUS if POLICE_RADIO_NAME in inv else 0.0),
        "honor_mult": 1.0 + (POLICE_BADGE_HONOR_BONUS if POLICE_BADGE_NAME in inv else 0.0)
                          + (POLICE_INSIGNIA_HONOR_BONUS if POLICE_INSIGNIA_NAME in inv else 0.0),
    }

class PoliceShopSelect(discord.ui.Select):
    def __init__(self, owner: discord.Member):
        self.owner = owner
        options = [
            discord.SelectOption(
                label=item["name"],
                description=f"가격: ${item['price']:,} | {item['desc']}",
                value=item["id"]
            ) for item in POLICE_SHOP_ITEMS
        ]
        super().__init__(placeholder="치안국 보급품을 선택하세요...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        if interaction.user != self.owner:
            await interaction.followup.send("본인의 상점 창만 조작할 수 있습니다.", ephemeral=True)
            return

        user = interaction.user
        user_stats = get_user_data(user.id)
        current_money = user_stats.get("money", 0)
        inventory = user_stats.get("inventory", {})

        target_item = next((item for item in POLICE_SHOP_ITEMS if item["id"] == self.values[0]), None)
        if not target_item:
            await interaction.followup.send("존재하지 않는 보급품입니다.", ephemeral=True)
            return

        item_name = target_item["name"]
        item_price = target_item["price"]

        if current_money < item_price:
            await interaction.followup.send(f"자금이 부족합니다! (필요: `${item_price:,}`, 보유: `${current_money:,}`)", ephemeral=True)
            return

        if inventory.get(item_name, 0) >= 1:
            await interaction.followup.send("이미 지급받은 보급품입니다.", ephemeral=True)
            return

        current_money -= item_price
        inventory[item_name] = inventory.get(item_name, 0) + 1

        try:
            _require_db()
            supabase.table("user_data").update({
                "money": current_money,
                "inventory": inventory
            }).eq("user_id", str(user.id)).execute()
        except Exception as e:
            print(f"DB Error (police shop buy): {e}")
            await interaction.followup.send("보급품 신청 중 DB 오류가 발생했습니다.", ephemeral=True)
            return

        embed = discord.Embed(
            title="[치안국 보급고] 지급 완료",
            description=f"**{user.mention}** 님이 **{item_name}**을(를) 보급받았습니다!\n\n"
                        f"**차감 비용:** `${item_price:,}`\n"
                        f"**잔여 자금:** `${current_money:,}`\n"
                        f"**효과:** `{target_item['desc']}`",
            color=discord.Color.blue()
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

class PoliceShopView(discord.ui.View):
    def __init__(self, owner: discord.Member):
        super().__init__(timeout=60)
        self.add_item(PoliceShopSelect(owner))

@bot.tree.command(name="경찰상점", description="[경찰 전용] 치안국 전용 장비와 보급품을 구매합니다.")
async def open_police_shop(interaction: discord.Interaction):
    if not await check_maintenance(interaction): return
    if await check_jail(interaction): return

    # 경찰 역할 검증
    police_role = discord.utils.get(interaction.guild.roles, name="경찰")
    if not police_role or police_role not in interaction.user.roles:
        await interaction.response.send_message("치안국 소속 경찰만 이용할 수 있는 전용 상점입니다!", ephemeral=True)
        return

    await interaction.response.defer()

    user = interaction.user
    user_stats = get_user_data(user.id)
    current_money = user_stats.get("money", 0)

    items_desc = "\n".join([
        f"• **{item['name']}** - `${item['price']:,}`\n  └ *{item['desc']}*"
        for item in POLICE_SHOP_ITEMS
    ])

    embed = discord.Embed(
        title="[치안국 보급고] 공식 카탈로그",
        description=f"**{user.mention}** 경관님, 환영합니다. 치안 활동을 위한 공식 보급품을 구매할 수 있습니다.\n\n"
                    f"{items_desc}\n\n"
                    f"**현재 보유 자금:** `${current_money:,}`",
        color=discord.Color.dark_blue()
    )
    if user.avatar:
        embed.set_thumbnail(url=user.avatar.url)

    await interaction.followup.send(embed=embed, view=PoliceShopView(owner=user), ephemeral=True)

@bot.tree.command(name="이동", description="[마피아 GM] 다른 지역으로 이동합니다.")
@app_commands.choices(지역=[
    app_commands.Choice(name="레벤하임 (수도)", value="레벤하임"),
    app_commands.Choice(name="던베르크 (공업지대)", value="던베르크"),
    app_commands.Choice(name="블랙워드 (빈민가)", value="블랙워드"),
    app_commands.Choice(name="에르느코항 (항구도시)", value="에르느코항"),
    app_commands.Choice(name="로젠펠트 (유흥업소)", value="로젠펠트")
])
async def move_location(interaction: discord.Interaction, 지역: app_commands.Choice[str]):
    if not await check_maintenance(interaction): return
    if not await check_membership(interaction): return
    if await check_jail(interaction): return

    user = interaction.user
    selected_loc = 지역.value
    loc_info = LOCATIONS[selected_loc]

    update_user_location(user.id, selected_loc)

    embed = discord.Embed(
        title=f"[{selected_loc}] (으)로 이동 완료",
        description=f"**{user.mention}** 님이 성공적으로 도착했습니다.",
        color=discord.Color.dark_blue()
    )
    embed.add_field(name="지역 특징", value=loc_info["desc"], inline=False)
    if user.avatar:
        embed.set_thumbnail(url=user.avatar.url)

    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="암시장", description="[조직원 전용] 암시장에 입장합니다.")
async def open_market(interaction: discord.Interaction):
    if not await check_maintenance(interaction): return
    if not await check_membership(interaction): return
    if await check_jail(interaction): return

    # [추가] 경찰은 암시장 이용 불가
    police_role = discord.utils.get(interaction.guild.roles, name="경찰")
    if police_role and police_role in interaction.user.roles:
        await interaction.response.send_message("치안국 경찰은 음지의 암시장을 이용할 수 없습니다! 대신 `/경찰상점`을 이용해 주세요.", ephemeral=True)
        return

    user = interaction.user
    user_stats = get_user_data(user.id)
    current_loc = user_stats.get("location", "레벤하임")
    items = BLACK_MARKET_ITEMS.get(current_loc, [])

    item_details = [f"• **{i['name']}** (`${i['price']:,}`)\n  └ {i['desc']}" for i in items]

    embed = discord.Embed(
        title=f"[{current_loc} 비밀 암시장]",
        description=f"현재 보유 자금: **${user_stats['money']:,}**\n\n**[판매 물품]**\n" + "\n".join(item_details),
        color=discord.Color.gold()
    )
    if user.avatar:
        embed.set_thumbnail(url=user.avatar.url)

    await interaction.response.send_message(embed=embed, view=MarketView(owner=user, location=current_loc))

@bot.tree.command(name="추방", description="[간부 전용] 조직원을 추방합니다.")
@app_commands.describe(member="추방할 조직원", reason="추방 사유")
async def kick_member(interaction: discord.Interaction, member: discord.Member, reason: str = "규율 위반"):
    if not await check_maintenance(interaction): return

    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("관리자 권한이 필요합니다!", ephemeral=True)
        return

    role = discord.utils.get(interaction.guild.roles, name="조직원")
    citizen_role = discord.utils.get(interaction.guild.roles, name="시민")
    
    if not role or role not in member.roles:
        await interaction.response.send_message(f"{member.mention} 님은 조직원이 아닙니다.", ephemeral=True)
        return

    try:
        await member.remove_roles(role)
        if citizen_role:
            await member.add_roles(citizen_role)
        delete_user_data(member.id)
    except Exception:
        pass

    embed = discord.Embed(
        title="[패밀리 숙청]",
        description=f"**{member.mention}** 님이 추방되고 모든 기록이 초기화되었습니다.",
        color=discord.Color.dark_embed()
    )
    await interaction.response.send_message(embed=embed)


# ==========================================================================
# [합친 모듈] police_ops.py
#   /작전, /심문
# ==========================================================================
# ==========================================================================
# [경찰 전용] /작전  —  경찰판 '임무'
#   • 조직원의 /임무 처럼, 경찰이 수사 작전을 직접 골라 성공률 판정을 받고 포상금·명예를 얻습니다.
#   • 성공 → 포상금 + 경찰 명예 (황금 배지 +20%), 실패 → 작전 경비로 벌금 + 쿨타임 연장 (조직원처럼 지명수배는 없음)
#   • 성공률 보너스 (경찰만의 요소)
#       - 순찰망 가동: 내가 있는 지역에 /순찰 효과가 남아 있으면 +10%
#       - 수배범 정보: 지금 지명수배 중인 마피아 1명당 +2% (최대 +10%)
#       - 기동타격대 무전기 보유: +5%
#   • /월급 근무 중이면 성공한 작전이 실적 점수로 쌓입니다. (작전 난이도가 높을수록 점수가 큼)
#   • 업적: police_ops(작전 성공 수), police_ops_boss(최상 난이도 성공 수)
#   ※ /수사팀 작전(여럿이 함께하는 합동 소탕)과는 별개의 명령어입니다.
# ==========================================================================
OPERATION_COOLDOWN_SEC = 45          # 작전 쿨타임
OPERATION_FAIL_EXTRA_SEC = 30        # 실패하면 보고서 작성으로 쿨타임이 이만큼 늘어남
OPERATION_PATROL_BONUS = 10          # 순찰망 가동 지역 성공률 보너스(%)
OPERATION_WANTED_BONUS_PER = 2       # 지명수배 중인 마피아 1명당 성공률 보너스(%)
OPERATION_WANTED_BONUS_MAX = 10      #   위 보너스의 상한(%)
POLICE_RADIO_OP_BONUS = 5            # 기동타격대 무전기: 성공률 +5%

# success_rate(%) / reward(최소, 최대 $) / honor(pt) / fine(실패 시 벌금 $) / shift_bonus(/월급 실적 추가 점수)
POLICE_OPERATIONS = {
    "op_1": {"title": "불법 노점·소매치기 단속",     "diff": "하",   "success_rate": 85, "reward": inc_range((150, 350)),   "honor": 6,  "fine": 80,   "shift_bonus": 0},
    "op_2": {"title": "밀수 의심 화물 검문",         "diff": "중",   "success_rate": 60, "reward": inc_range((350, 800)),   "honor": 12,  "fine": 200,  "shift_bonus": 2},
    "op_3": {"title": "마약 조직 정보원 접선",       "diff": "상",   "success_rate": 40, "reward": inc_range((900, 1900)),  "honor": 16,  "fine": 500,  "shift_bonus": 4},
    "op_4": {"title": "조직 아지트 잠복 수사",       "diff": "중상", "success_rate": 50, "reward": inc_range((650, 1400)),  "honor": 14,  "fine": 350,  "shift_bonus": 3},
    "op_5": {"title": "거물 마피아 보스 소탕 작전",  "diff": "최상", "success_rate": 20, "reward": inc_range((2500, 5000)), "honor": 36, "fine": 1200, "shift_bonus": 12},
}
operation_cooldowns = {}   # {user_id: 다음 작전 가능 시각(time.time 기준)}

def _active_wanted_count() -> int:
    """지금 지명수배 중인 마피아 수 (만료된 항목은 제외)"""
    now = datetime.now()
    return sum(1 for exp in list(wanted_list.values()) if exp > now)

def operation_bonus_preview(in_patrol: bool, wanted_n: int) -> list:
    """경찰 장비와 무관한 지역·수배 보너스 (성공률 %, 표시 문구) 목록"""
    out = []
    if in_patrol:
        out.append((OPERATION_PATROL_BONUS, f"순찰망 가동 (+{OPERATION_PATROL_BONUS}%)"))
    if wanted_n:
        w = min(OPERATION_WANTED_BONUS_MAX, wanted_n * OPERATION_WANTED_BONUS_PER)
        out.append((w, f"수배범 정보 확보 {wanted_n}명 (+{w}%)"))
    return out

def operation_success_rate(op: dict, inventory: dict, in_patrol: bool, wanted_n: int):
    """(최종 성공률, 적용된 보너스 문구 목록)"""
    rate = op["success_rate"]
    labels = []
    for pct, label in operation_bonus_preview(in_patrol, wanted_n):
        rate += pct
        labels.append(label)
    if POLICE_RADIO_NAME in (inventory or {}):
        rate += POLICE_RADIO_OP_BONUS
        labels.append(f"기동타격대 무전기 (+{POLICE_RADIO_OP_BONUS}%)")
    return max(1, min(98, rate)), labels

def operation_resolve_sync(user_id: int, op_key: str, in_patrol: bool, wanted_n: int) -> dict:
    """작전 판정 + 보상/벌금 + 업적 카운터를 한 번에 처리해 저장 (경매·업적 쓰기와 겹치지 않도록 잠금)"""
    from .economy import _auction_update  # 순환 import 방지: 호출 시점에 가져옴
    from .economy import _inv_lock  # 순환 import 방지: 호출 시점에 가져옴
    _require_db()
    op = POLICE_OPERATIONS[op_key]
    with _ach_lock, _inv_lock:
        info = get_user_data(user_id)
        inv = info.get("inventory") or {}
        money, honor = int(info.get("money", 0) or 0), int(info.get("honor", 0) or 0)
        rate, labels = operation_success_rate(op, inv, in_patrol, wanted_n)
        success = random.randint(1, 100) <= rate
        old_rank = get_user_rank(honor, is_police=True)
        blob = get_title_blob(info)
        s = blob["stats"]

        earned_money = earned_honor = fine = 0
        if success:
            earned_money = random.randint(*op["reward"])
            earned_honor = int(round(op["honor"] * police_bonuses(inv)["honor_mult"]))
            money += earned_money
            honor += earned_honor
            s["police_ops"] += 1
            if op_key == "op_5":
                s["police_ops_boss"] += 1
        else:
            fine = min(money, op["fine"])
            money -= fine

        new_ids = evaluate_new_titles(blob, {**info, "money": money, "honor": honor})   # 새 칭호·업적 (대기 알림 포함)
        _auction_update(user_id, {"money": money, "honor": honor, "data": blob})

    return {"success": success, "rate": rate, "labels": labels, "money": money, "honor": honor,
            "earned_money": earned_money, "earned_honor": earned_honor, "fine": fine,
            "old_rank": old_rank, "new_rank": get_user_rank(honor, is_police=True),
            "notice": format_new_titles(new_ids)}

class OperationSelect(discord.ui.Select):
    def __init__(self, owner: discord.Member):
        self.owner = owner
        options = [
            discord.SelectOption(
                label=m["title"],
                description=f"난이도: {m['diff']} | 성공률 {m['success_rate']}% | ${m['reward'][0]:,}~${m['reward'][1]:,} / 명예 +{m['honor']}pt / 실패 -${m['fine']:,}",
                value=key
            ) for key, m in POLICE_OPERATIONS.items()
        ]
        super().__init__(placeholder="수행할 수사 작전을 선택하세요...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        from .police_unit import police_shift_record  # 순환 import 방지: 호출 시점에 가져옴
        from .police_unit import _is_police  # 순환 import 방지: 호출 시점에 가져옴
        await interaction.response.defer()
        if interaction.user != self.owner:
            await interaction.followup.send("타인의 작전 선택 창은 조작할 수 없습니다.", ephemeral=True)
            return
        user = interaction.user
        if not _is_police(user):
            await interaction.followup.send("치안국 경찰만 수사 작전을 수행할 수 있습니다!", ephemeral=True)
            return

        now_ts = time.time()                       # 확인과 등록 사이에 await 가 없어 중복 클릭으로 쿨타임을 피할 수 없음
        remain = int(operation_cooldowns.get(user.id, 0) - now_ts + 0.999)
        if remain > 0:
            await interaction.followup.send(f"작전 보고서를 작성 중입니다. **{remain}초** 뒤에 다시 작전을 수행할 수 있습니다.", ephemeral=True)
            return
        operation_cooldowns[user.id] = now_ts + OPERATION_COOLDOWN_SEC

        key = self.values[0]
        op = POLICE_OPERATIONS[key]
        guild_id = interaction.guild_id
        try:
            loc = (await asyncio.to_thread(get_user_data, user.id)).get("location", "레벤하임")
            in_patrol = bool(guild_id and active_patrols.get(guild_id, {}).get(loc, 0) > time.time())
            res = await asyncio.to_thread(operation_resolve_sync, user.id, key, in_patrol, _active_wanted_count())
        except Exception as e:
            print(f"DB Error (police operation): {e}")
            operation_cooldowns.pop(user.id, None)
            await interaction.followup.send("작전 결과를 저장하는 중 오류가 발생했습니다. 다시 시도해 주세요.", ephemeral=True)
            return

        buff_desc = f"\n**적용된 보너스:** {', '.join(res['labels'])}" if res["labels"] else ""
        rank_up = ""
        if res["old_rank"] != res["new_rank"]:
            rank_up = f"\n\n**[승진 축하]** **{res['old_rank']}** → **{res['new_rank']}**(으)로 승진하셨습니다!"

        if res["success"]:
            police_shift_record(user.id, "operation", 1, bonus=op["shift_bonus"])   # /월급 근무 실적
            embed = discord.Embed(
                title=f"[{loc}] 작전 성공",
                description=(f"**{user.mention}** 경관이 작전을 성공적으로 마쳤습니다!{buff_desc}\n\n"
                             f"**작전명:** {op['title']}\n"
                             f"**지급 포상금:** `${res['earned_money']:,}`\n"
                             f"**경찰 실적:** `+{res['earned_honor']} pt`\n\n"
                             f"**현재 직급:** `{res['new_rank']}`\n"
                             f"**보유 자금:** `${res['money']:,}`\n"
                             f"**총 명예:** `{res['honor']} pt`{rank_up}{res['notice']}"),
                color=discord.Color.blue()
            )
        else:
            operation_cooldowns[user.id] = now_ts + OPERATION_COOLDOWN_SEC + OPERATION_FAIL_EXTRA_SEC
            embed = discord.Embed(
                title=f"[{loc}] 작전 실패",
                description=(f"**{user.mention}** 경관의 작전 도중 문제가 발생했습니다!{buff_desc}\n\n"
                             f"**작전명:** {op['title']}\n"
                             f"**작전 실패!** 증거 확보에 실패해 작전 경비로 벌금 `-${res['fine']:,}` 이(가) 부과되었습니다.\n"
                             f"보고서 작성으로 다음 작전까지 **{OPERATION_COOLDOWN_SEC + OPERATION_FAIL_EXTRA_SEC}초**가 걸립니다.\n\n"
                             f"**현재 직급:** `{res['new_rank']}`\n"
                             f"**보유 자금:** `${res['money']:,}`\n"
                             f"**총 명예:** `{res['honor']} pt`{res['notice']}"),
                color=discord.Color.red()
            )
        if user.avatar:
            embed.set_thumbnail(url=user.avatar.url)
        await interaction.edit_original_response(embed=embed, view=None)

class OperationView(discord.ui.View):
    def __init__(self, owner: discord.Member):
        super().__init__(timeout=180)
        self.add_item(OperationSelect(owner=owner))

@bot.tree.command(name="작전", description="[경찰 전용] 원하는 수사 작전을 직접 선택하여 수행하고 포상금과 명예를 얻습니다.")
async def police_operation_command(interaction: discord.Interaction):
    from .police_unit import _is_police  # 순환 import 방지: 호출 시점에 가져옴
    if not await check_maintenance(interaction): return
    if not await check_membership(interaction): return
    if await check_jail(interaction): return
    user = interaction.user
    if not _is_police(user):
        await interaction.response.send_message("치안국 경찰만 수사 작전을 수행할 수 있습니다! (조직원은 `/임무` 를 이용하세요)", ephemeral=True)
        return
    remain = int(operation_cooldowns.get(user.id, 0) - time.time() + 0.999)
    if remain > 0:
        await interaction.response.send_message(f"작전 보고서를 작성 중입니다. **{remain}초** 뒤에 다시 작전을 수행할 수 있습니다.", ephemeral=True)
        return
    await interaction.response.defer()

    try:
        loc = (await asyncio.to_thread(get_user_data, user.id)).get("location", "레벤하임")
    except Exception:
        loc = "레벤하임"
    in_patrol = bool(interaction.guild_id and active_patrols.get(interaction.guild_id, {}).get(loc, 0) > time.time())
    wanted_n = _active_wanted_count()
    notes = [label for _, label in operation_bonus_preview(in_patrol, wanted_n)]
    if not in_patrol:
        notes.append(f"`/순찰` 로 순찰망을 가동하면 이 지역 작전 성공률 +{OPERATION_PATROL_BONUS}%")
    embed = discord.Embed(
        title=f"[{loc}] 수사 작전 하달",
        description=(f"**{user.mention}** 경관님, 수행할 작전을 선택해 주십시오.\n\n"
                     f"**현재 지명수배 중인 마피아:** {wanted_n}명 (1명당 성공률 +{OPERATION_WANTED_BONUS_PER}%, 최대 +{OPERATION_WANTED_BONUS_MAX}%)\n"
                     f"**적용 중인 보너스:** " + (", ".join(notes) if notes else "없음")),
        color=discord.Color.dark_blue()
    )
    if user.avatar:
        embed.set_thumbnail(url=user.avatar.url)
    await interaction.followup.send(embed=embed, view=OperationView(owner=user))

# === [경찰 전용] `/심문` 명령어 (수감자 심문 -> 재판 -> 감형 / 실형 가중) ===
#   1) 경찰이 /심문 으로 수감자에게 질문을 던집니다.
#   2) 수감자가 버튼으로 답변합니다. (자백 / 묵비권 / 결백 주장)  ※ 시간 내 무응답 = 묵비권
#   3) 경찰이 재판부에 의견을 제출합니다. (감형 권고 / 가중 권고)
#   4) 재판 결과가 확률로 결정됩니다. (감형 / 유지 / 실형 가중) -> jail_list 의 석방 시간이 바뀝니다.
INTERROGATE_COOLDOWN_SEC = 120        # 경찰의 심문 쿨타임
INTERROGATE_STEP_TIMEOUT_SEC = 45     # 수감자 답변 / 경찰 의견 각 단계 제한 시간
INTERROGATE_MIN_REMAIN_SEC = 30       # 석방까지 이 시간 이상 남아 있어야 심문 가능
INTERROGATE_MAX_JAIL_SEC = 900        # 가중되어도 남은 형기는 이 시간을 넘지 않음
INTERROGATE_DELTA_RANGE = (40, 90)    # 감형 / 가중 시간 범위 (초)
INTERROGATE_DENY_MULT = 1.5           # 결백 주장은 도박수라 변동폭이 1.5배
INTERROGATE_MATCH_REWARD_MONEY = inc_money(300)  # 경찰 의견과 판결이 일치했을 때 수당
INTERROGATE_MATCH_REWARD_HONOR = 30   # 경찰 의견과 판결이 일치했을 때 명예

INTERROGATE_ANSWERS = {"confess": "자백 (협조)", "silent": "묵비권", "deny": "결백 주장"}
INTERROGATE_OPINIONS = {"reduce": "감형 권고", "extend": "가중 권고"}

# (수감자 답변, 경찰 의견) -> (감형, 유지, 가중) 확률 가중치
INTERROGATE_VERDICT_WEIGHTS = {
    ("confess", "reduce"): (70, 25, 5),
    ("confess", "extend"): (25, 45, 30),
    ("silent",  "reduce"): (25, 55, 20),
    ("silent",  "extend"): (10, 40, 50),
    ("deny",    "reduce"): (45, 5, 50),
    ("deny",    "extend"): (20, 5, 75),
}

interrogate_cooldowns = {}     # {police_id: datetime}
interrogate_active = {}        # {prisoner_id: 만료 timestamp}  (진행 중인 심문, 만료되면 자동 해제)
interrogate_done_terms = {}    # {prisoner_id: 그 수감의 석방 예정 시각}  (한 번 수감당 심문 1회)


class InterrogationSession:
    def __init__(self, police: discord.Member, prisoner: discord.Member, release_at: datetime, question: str):
        self.police = police
        self.prisoner = prisoner
        self.release_at = release_at   # 심문 시작 시점의 석방 예정 시각 (수감 식별용)
        self.question = question
        self.message = None
        self.answer = None

    def end(self):
        interrogate_active.pop(self.prisoner.id, None)


class InterrogatePrisonerView(discord.ui.View):
    def __init__(self, s: InterrogationSession):
        super().__init__(timeout=INTERROGATE_STEP_TIMEOUT_SEC)
        self.s = s
        self.answered = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.s.prisoner.id:
            await interaction.response.send_message("심문을 받는 수감자만 답변할 수 있습니다.", ephemeral=True)
            return False
        return True

    async def _answer(self, interaction: discord.Interaction, key: str):
        if self.answered:
            await interaction.response.send_message("이미 답변했습니다.", ephemeral=True)
            return
        self.answered = True
        for child in self.children:
            child.disabled = True
        self.stop()
        await interaction.response.defer()
        await interrogate_police_stage(self.s, key)

    @discord.ui.button(label="자백 (협조)", style=discord.ButtonStyle.success)
    async def confess(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._answer(interaction, "confess")

    @discord.ui.button(label="묵비권", style=discord.ButtonStyle.secondary)
    async def silent(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._answer(interaction, "silent")

    @discord.ui.button(label="결백 주장", style=discord.ButtonStyle.danger)
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._answer(interaction, "deny")

    async def on_timeout(self):
        if self.answered:
            return
        self.answered = True
        await interrogate_police_stage(self.s, "silent", timed_out=True)


class InterrogatePoliceView(discord.ui.View):
    def __init__(self, s: InterrogationSession):
        super().__init__(timeout=INTERROGATE_STEP_TIMEOUT_SEC)
        self.s = s
        self.resolved = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.s.police.id:
            await interaction.response.send_message("담당 경찰만 재판부에 의견을 제출할 수 있습니다.", ephemeral=True)
            return False
        return True

    async def _submit(self, interaction: discord.Interaction, opinion: str):
        if self.resolved:
            await interaction.response.send_message("이미 제출된 의견입니다.", ephemeral=True)
            return
        self.resolved = True
        for child in self.children:
            child.disabled = True
        self.stop()
        await interaction.response.defer()
        await interrogate_trial(self.s, opinion)

    @discord.ui.button(label="감형 권고", style=discord.ButtonStyle.success)
    async def reduce(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._submit(interaction, "reduce")

    @discord.ui.button(label="가중 권고", style=discord.ButtonStyle.danger)
    async def extend(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._submit(interaction, "extend")

    async def on_timeout(self):
        if self.resolved:
            return
        self.resolved = True
        s = self.s
        s.end()
        embed = discord.Embed(
            title="[재판 불성립]",
            description=f"{s.police.mention} 경관이 의견을 제출하지 않아 재판이 열리지 못했습니다. (형량 변동 없음)",
            color=discord.Color.dark_grey()
        )
        try:
            await s.message.edit(content=None, embed=embed, view=None)
        except Exception:
            pass


async def interrogate_police_stage(s: InterrogationSession, answer: str, timed_out: bool = False):
    """수감자 답변이 끝난 뒤 -> 경찰이 재판부 의견을 고르는 단계"""
    s.answer = answer
    note = "(시간 내 답변이 없어 **묵비권**으로 처리되었습니다.)\n\n" if timed_out else ""
    embed = discord.Embed(
        title="[치안국 심문실] 재판부 의견 제출",
        description=(
            f"{note}"
            f"**질문:** {s.question}\n"
            f"**{s.prisoner.display_name}** 님의 답변: **{INTERROGATE_ANSWERS[answer]}**\n\n"
            f"{s.police.mention} 경관은 {INTERROGATE_STEP_TIMEOUT_SEC}초 안에 재판부에 의견을 제출하세요.\n"
            f"의견이 판결과 일치하면 수당 `${INTERROGATE_MATCH_REWARD_MONEY:,}` / 실적 `+{INTERROGATE_MATCH_REWARD_HONOR} pt` 를 받습니다."
        ),
        color=discord.Color.dark_blue()
    )
    try:
        await s.message.edit(content=s.police.mention, embed=embed, view=InterrogatePoliceView(s))
    except Exception as e:
        print(f"Interrogate stage error: {e}")
        s.end()


async def interrogate_trial(s: InterrogationSession, opinion: str):
    """재판: 확률로 판결을 정하고 jail_list 의 석방 시간을 조정"""
    from .police_unit import police_shift_record  # 순환 import 방지: 호출 시점에 가져옴
    try:
        weights = INTERROGATE_VERDICT_WEIGHTS[(s.answer, opinion)]
        verdict = random.choices(["reduce", "keep", "extend"], weights=weights, k=1)[0]

        delta = random.randint(*INTERROGATE_DELTA_RANGE)
        if s.answer == "deny":
            delta = int(delta * INTERROGATE_DENY_MULT)

        now = datetime.now()
        cur = jail_list.get(s.prisoner.id)

        # 심문 도중 형기가 끝났거나 다른 수감으로 바뀐 경우
        if cur is None or now >= cur:
            embed = discord.Embed(
                title="[재판 무효]",
                description=f"{s.prisoner.mention} 님은 재판 전에 이미 석방되어 판결이 무효 처리되었습니다.",
                color=discord.Color.dark_grey()
            )
            await s.message.edit(content=None, embed=embed, view=None)
            return

        if verdict == "reduce":
            new_release = max(now + timedelta(seconds=5), cur - timedelta(seconds=delta))
        elif verdict == "extend":
            new_release = min(now + timedelta(seconds=INTERROGATE_MAX_JAIL_SEC), cur + timedelta(seconds=delta))
        else:
            new_release = cur
        jail_list[s.prisoner.id] = new_release
        save_status(s.prisoner.id)
        remain = max(0, int((new_release - now).total_seconds()))
        real_delta = abs(int((new_release - cur).total_seconds()))

        if verdict == "reduce":
            title, color = "[재판 결과] 감형", discord.Color.green()
            result = f"재판부는 수감자의 태도를 참작해 **감형**을 선고했습니다.\n**수감 시간 `-{real_delta}초`**"
        elif verdict == "extend":
            title, color = "[재판 결과] 실형 가중", discord.Color.dark_red()
            result = f"재판부는 죄질이 불량하다고 판단해 **실형을 가중**했습니다.\n**수감 시간 `+{real_delta}초`**"
        else:
            title, color = "[재판 결과] 형량 유지", discord.Color.light_grey()
            result = "재판부는 **기존 형량을 유지**하기로 했습니다."

        police_shift_record(s.police.id, "interrogate")   # /월급 근무 실적
        ach_note = await police_ach_bump(s.police.id, police_interrogations=1,
                                         police_matches=1 if verdict == opinion else 0)   # 업적 카운터
        reward_text = ""
        if verdict == opinion:   # 경찰 의견과 판결이 일치
            police_shift_record(s.police.id, "interrogate_match")
            try:
                _pb = police_bonuses(get_user_data(s.police.id).get("inventory"))
                reward_honor = int(round(INTERROGATE_MATCH_REWARD_HONOR * _pb["honor_mult"]))
                update_user_stats(s.police.id, INTERROGATE_MATCH_REWARD_MONEY, reward_honor)
                info = get_user_data(s.police.id)
                rank = get_user_rank(info["honor"], is_police=True)
                reward_text = (f"\n\n**의견 일치 수당:** `${INTERROGATE_MATCH_REWARD_MONEY:,}`\n"
                               f"**경찰 실적:** `+{reward_honor} pt` (현재 직급: `{rank}`)")
            except Exception as e:
                print(f"Interrogate reward error: {e}")

        embed = discord.Embed(
            title=title,
            description=(
                f"**질문:** {s.question}\n"
                f"**답변:** {INTERROGATE_ANSWERS[s.answer]} / **경찰 의견:** {INTERROGATE_OPINIONS[opinion]}\n\n"
                f"{result}\n"
                f"**{s.prisoner.display_name}** 님의 석방까지 **{remain}초** 남았습니다."
                f"{reward_text}{ach_note}"
            ),
            color=color
        )
        await s.message.edit(content=None, embed=embed, view=None)
    except Exception as e:
        print(f"Interrogate trial error: {e}")
    finally:
        s.end()


@bot.tree.command(name="심문", description="[경찰 전용] 수감자를 심문하고 재판을 열어 수감 시간을 줄이거나 늘립니다.")
@app_commands.describe(수감자="심문할 수감자", 질문="수감자에게 던질 질문 (선택)")
async def interrogate_command(interaction: discord.Interaction, 수감자: discord.Member, 질문: str = "공범과 범행 동기를 낱낱이 말해라."):
    if not await check_maintenance(interaction): return
    if await check_jail(interaction): return

    user = interaction.user
    now = datetime.now()

    # 1. 경찰 역할 검증
    police_role = discord.utils.get(interaction.guild.roles, name="경찰")
    if not police_role or police_role not in user.roles:
        await interaction.response.send_message("치안국 소속 경찰만 심문을 진행할 수 있습니다!", ephemeral=True)
        return

    # 2. 쿨타임 체크
    cd = interrogate_cooldowns.get(user.id)
    if cd and now < cd:
        remaining = int((cd - now).total_seconds())
        await interaction.response.send_message(f"심문 준비 중입니다. **{remaining}초** 뒤에 다시 심문할 수 있습니다.", ephemeral=True)
        return

    # 3. 대상 검증
    if 수감자.bot or 수감자.id == user.id:
        await interaction.response.send_message("심문할 수 없는 대상입니다.", ephemeral=True)
        return

    release_at = jail_list.get(수감자.id)
    if release_at is None or now >= release_at:
        await interaction.response.send_message(f"**{수감자.display_name}** 님은 현재 수감 중이 아닙니다.", ephemeral=True)
        return

    remain = int((release_at - now).total_seconds())
    if remain < INTERROGATE_MIN_REMAIN_SEC:
        await interaction.response.send_message(f"석방이 임박해 재판을 열 수 없습니다. (남은 형기 {remain}초)", ephemeral=True)
        return

    if interrogate_done_terms.get(수감자.id) == release_at:
        await interaction.response.send_message(f"**{수감자.display_name}** 님은 이번 수감에서 이미 심문이 진행되었습니다.", ephemeral=True)
        return

    if time.time() < interrogate_active.get(수감자.id, 0):
        await interaction.response.send_message(f"**{수감자.display_name}** 님은 이미 심문을 받고 있습니다.", ephemeral=True)
        return

    # 4. 심문 시작 (여기부터 await 전까지 다른 명령어가 끼어들 수 없음)
    interrogate_cooldowns[user.id] = now + timedelta(seconds=INTERROGATE_COOLDOWN_SEC)
    interrogate_done_terms[수감자.id] = release_at
    interrogate_active[수감자.id] = time.time() + INTERROGATE_STEP_TIMEOUT_SEC * 2 + 30

    question = 질문.strip()[:100] or "공범과 범행 동기를 낱낱이 말해라."
    session = InterrogationSession(user, 수감자, release_at, question)

    embed = discord.Embed(
        title="[치안국 심문실] 심문 개시",
        description=(
            f"**{user.mention}** 경관이 수감자 **{수감자.mention}** 을(를) 심문실로 불러냈습니다.\n\n"
            f"**질문:** {question}\n\n"
            f"{수감자.mention} 님은 {INTERROGATE_STEP_TIMEOUT_SEC}초 안에 답변을 선택하세요.\n"
            f"- **자백:** 감형 가능성 높음 (단, 경찰이 가중을 권고하면 위험)\n"
            f"- **묵비권:** 무난하지만 크게 기대할 건 없음\n"
            f"- **결백 주장:** 큰 감형 또는 큰 가중, 도박수\n"
            f"(무응답은 묵비권 처리)"
        ),
        color=discord.Color.dark_blue()
    )
    await interaction.response.send_message(content=수감자.mention, embed=embed, view=InterrogatePrisonerView(session))
    session.message = await interaction.original_response()


# ==========================================================================
# [합친 모듈] police_arrest.py
#   /설정, /검거, /밀수
# ==========================================================================
 
# === 통합 `/설정` 슬래시 명령어 ===
@bot.tree.command(name="설정", description="[조직원/경찰 전용] 게임 내 통합 환경 설정 창을 엽니다.")
async def settings_command(interaction: discord.Interaction):
    if not await check_maintenance(interaction): return
    if await check_jail(interaction): return
    if not await check_membership(interaction): return
 
    user = interaction.user
    settings = dict(get_user_data(user.id).get("settings") or {})
 
    view = SettingsView(user=user, settings=settings)
    await interaction.response.send_message(embed=view.current_embed(), view=view, ephemeral=True)
    view.message = await interaction.original_response()

# === [경찰 전용] `/검거` 성공 시 아이템 압류 ===
ARREST_SEIZE_MIN = 1   # 한 번에 압류하는 아이템 종류 수 (최소)
ARREST_SEIZE_MAX = 2   # 한 번에 압류하는 아이템 종류 수 (최대)

def seize_items_sync(police_id: int, target_id: int) -> list:
    """검거된 조직원의 아이템 중 랜덤 1~2종을 1개씩 빼앗아 경찰 인벤토리에 넣습니다. (동기 DB 작업 → to_thread 로 실행)
    재료와 거래 불가 아이템(네임드 전리품 등)은 압류하지 않습니다. 반환: 압류한 아이템 이름 목록"""
    from .economy import auction_blocked_reason  # 순환 import 방지: 호출 시점에 가져옴
    from .economy import _inv_lock  # 순환 import 방지: 호출 시점에 가져옴
    _require_db()
    with _inv_lock:
        t_inv = dict(get_user_data(target_id).get("inventory") or {})
        candidates = [k for k, v in t_inv.items() if int(v or 0) > 0 and not k.startswith("[재료]") and auction_blocked_reason(k) is None]
        if not candidates:
            return []
        picked = random.sample(candidates, min(random.randint(ARREST_SEIZE_MIN, ARREST_SEIZE_MAX), len(candidates)))
        new_t_inv = dict(t_inv)
        for k in picked:
            if int(new_t_inv[k]) <= 1:
                new_t_inv.pop(k, None)
            else:
                new_t_inv[k] = int(new_t_inv[k]) - 1

        p_inv = dict(get_user_data(police_id).get("inventory") or {})
        for k in picked:
            p_inv[k] = int(p_inv.get(k, 0) or 0) + 1

        # '조직원에게서 빼앗기 → 경찰에게 넣기' 순서로 저장하고, 경찰 쪽 저장이 실패하면 조직원 인벤토리를 되돌린다
        res = supabase.table("user_data").update({"inventory": new_t_inv}).eq("user_id", str(target_id)).execute()
        _check_updated(res, "seize (target)")
        try:
            res = supabase.table("user_data").update({"inventory": p_inv}).eq("user_id", str(police_id)).execute()
            _check_updated(res, "seize (police)")
        except Exception:
            try:
                supabase.table("user_data").update({"inventory": t_inv}).eq("user_id", str(target_id)).execute()
            except Exception as e2:
                print(f"DB Error (seize rollback): {e2}")
            raise
        return picked

# === [경찰 전용] `/검거` 명령어 (서버 공개 알림형) ===
@bot.tree.command(name="검거", description="[경찰 전용] 지명수배 중인 마피아를 체포하여 감옥에 가두고 소지품 일부를 압류합니다.")
@app_commands.describe(member="검거할 수배 대상 마피아")
async def arrest_member(interaction: discord.Interaction, member: discord.Member):
    from .police_unit import POLICE_SALARY_ARREST_BONUS_MAX, POLICE_SALARY_ARREST_BONUS_PER, police_shift_record  # 순환 import 방지: 호출 시점에 가져옴
    from .civilian import try_corrupt_arrest  # 순환 import 방지: 호출 시점에 가져옴
    if not await check_maintenance(interaction): return

    police_role = discord.utils.get(interaction.guild.roles, name="경찰")
    if not police_role or police_role not in interaction.user.roles:
        await interaction.response.send_message("치안국 경찰만 검거 명령어를 사용할 수 있습니다!", ephemeral=True)
        return

    # 3초 타임아웃 방지용 defer (ephemeral=True를 제거하여 서버 공개 메시지로 전환)
    await interaction.response.defer()

    now = datetime.now()
    target_id = member.id

    # [밀수 거래] 이 경찰이 뇌물을 받은 마피아는 면제 시간 동안 검거할 수 없음
    immune_until = smuggle_immunity.get((interaction.user.id, target_id))
    if immune_until:
        if now < immune_until:
            remain_min = max(1, int((immune_until - now).total_seconds() // 60) + 1)
            await interaction.followup.send(
                f"**{member.display_name}** 님과는 밀수 거래가 성사된 상태라 **약 {remain_min}분간** 검거할 수 없습니다.",
                ephemeral=True
            )
            return
        smuggle_immunity.pop((interaction.user.id, target_id), None)

    # 수배 상태 검증
    is_wanted = False
    matched_key = None

    for key in list(wanted_list.keys()):
        if int(key) == int(target_id):
            matched_key = key
            expiry_time = wanted_list[key]
            if now <= expiry_time:
                is_wanted = True
            break

    if not is_wanted:
        if matched_key is not None:
            wanted_list.pop(matched_key, None)
            save_status(target_id)
        # [부패 경찰] 수배 상태가 아니어도 시민은 마음대로 체포할 수 있음
        if await try_corrupt_arrest(interaction, member):
            return
        # 수배 상태가 아닐 때는 나만 보이게(ephemeral=True) 처리
        await interaction.followup.send(f"**{member.display_name}** 님은 현재 지명수배 상태가 아니거나 증거가 인멸되었습니다.", ephemeral=True)
        return

    # 수배 해제 및 감옥 수감 (5분)
    wanted_list.pop(matched_key, None)
    jail_time_seconds = 300
    jail_list[target_id] = now + timedelta(seconds=jail_time_seconds)
    save_status(target_id)

    # DB 위치 변경
    try:
        update_user_location(target_id, "감옥")
    except Exception as e:
        print(f"Location update error: {e}")

    # 경찰 보상 지급 (무전기: 포상금 +20% / 황금 배지: 명예 +20%)
    _pb = police_bonuses(get_user_data(interaction.user.id).get("inventory"))
    arrest_money = int(round(ARREST_BASE_REWARD * _pb["money_mult"]))
    _target_corrupt = False
    try:
        _tdata = await asyncio.to_thread(get_user_data, target_id)
        _target_honor = max(0, int(_tdata.get("honor", 0)))
        _target_corrupt = "경찰" in {r.name for r in member.roles} and bool(get_title_blob(_tdata).get("corrupt"))
    except Exception:
        _target_honor = 0
    arrest_extra = min(ARREST_HONOR_BONUS_MAX, _target_honor // ARREST_HONOR_PER_POINT)   # 거물 검거 보너스
    arrest_honor = int(round((ARREST_BASE_HONOR + arrest_extra) * _pb["honor_mult"]))
    money_note = f" (무전기 +{round(POLICE_RADIO_ARREST_BONUS * 100)}%)" if _pb["money_mult"] > 1 else ""
    honor_note = f" (황금 배지 +{round(POLICE_BADGE_HONOR_BONUS * 100)}%)" if _pb["honor_mult"] > 1 else ""
    if arrest_extra > 0:
        honor_note = f" (거물 검거 보너스 +{arrest_extra}){honor_note}"
    if _target_corrupt:   # 부패 경찰을 잡으면 추가 명예
        arrest_honor += CORRUPT_ARREST_BONUS_HONOR
        honor_note += f" (부패 경찰 검거 +{CORRUPT_ARREST_BONUS_HONOR})"
    try:
        update_user_stats(interaction.user.id, arrest_money, arrest_honor)
    except Exception:
        pass
    police_shift_record(interaction.user.id, "arrest",
                        bonus=min(POLICE_SALARY_ARREST_BONUS_MAX, _target_honor // POLICE_SALARY_ARREST_BONUS_PER))   # /월급 근무 실적
    ach_note = await police_ach_bump(interaction.user.id, police_arrests=1,
                                     police_big_arrests=1 if _target_honor >= POLICE_BIG_FISH_HONOR else 0)   # 업적 카운터

    # 압류: 조직원 아이템 랜덤 1~2종을 경찰이 가져감
    try:
        seized_items = await asyncio.to_thread(seize_items_sync, interaction.user.id, target_id)
    except Exception as e:
        print(f"Seize error: {e}")
        seized_items = []
    seize_text = ", ".join(f"`{it}`" for it in seized_items) if seized_items else "압류할 수 있는 물품이 없었습니다."

    police_info = get_user_data(interaction.user.id)
    police_rank = get_user_rank(police_info["honor"], is_police=True)

    # 서버 전체에 보일 검거 임베드 생성
    embed = discord.Embed(
        title="[치안국 긴급 공지] 지명수배범 검거 완료",
        description=(
            f"**{interaction.user.mention}** 경찰관이 수배범 **{member.mention}** (을)를 현장에서 체포했습니다!\n\n"
            f"**처벌 내용:** **{jail_time_seconds // 60}분** 동안 감옥에 수감되며 프로필 외 모든 활동이 통제됩니다.\n"
            f"**현재 거점:** `감옥`\n\n"
            f"**검거 포상금:** `${arrest_money:,}`{money_note}\n"
            f"**경찰 실적:** `+{arrest_honor} pt`{honor_note} (현재 직급: `{police_rank}`)\n"
            f"**압류 물품:** {seize_text}"
            + ach_note
        ),
        color=discord.Color.dark_blue()
    )
    if member.avatar:
        embed.set_thumbnail(url=member.avatar.url)

    # 서버 전체(채널)에 공개적으로 메시지 전송
    await interaction.followup.send(embed=embed)

# === [조직원 전용] `/밀수` 명령어 (경찰 매수) ===
SMUGGLE_MIN_BRIBE = 500            # 최소 뇌물
SMUGGLE_MAX_BRIBE = 50000          # 최대 뇌물
SMUGGLE_COOLDOWN_SEC = 300         # 마피아의 밀수 제안 쿨타임 (5분)
SMUGGLE_TIMEOUT_SEC = 60           # 경찰이 응답할 수 있는 시간
SMUGGLE_IMMUNITY_SEC = 600         # 수락 시 그 경찰이 해당 마피아를 검거할 수 없는 시간 (10분)
SMUGGLE_REJECT_WANTED_SEC = 90     # 거절당했을 때 마피아에게 붙는 지명수배 시간
SMUGGLE_REJECT_FINE = 300          # 거절당했을 때 마피아 벌금
SMUGGLE_REJECT_HONOR_LOSS = 40     # 거절당했을 때 마피아 명예 감소
SMUGGLE_FINE_PER_MISSING_HONOR = 25  # 명예가 모자라 못 깎은 만큼 벌금으로 환산 ($/pt)
SMUGGLE_REJECT_POLICE_HONOR = 20   # 제안을 거절한 청렴한 경찰의 명예 포상

smuggle_cooldowns = {}   # {mafia_id: datetime}
smuggle_pending = set()  # 응답 대기 중인 마피아 id (동시에 한 건만)
smuggle_immunity = {}    # {(police_id, mafia_id): datetime (면제 만료)}

class SmuggleView(discord.ui.View):
    def __init__(self, mafia: discord.Member, police: discord.Member, amount: int):
        super().__init__(timeout=SMUGGLE_TIMEOUT_SEC)
        self.mafia = mafia
        self.police = police
        self.amount = amount
        self.resolved = False
        self.message = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.police.id:
            await interaction.response.send_message("이 제안은 대상 경찰만 응답할 수 있습니다.", ephemeral=True)
            return False
        return True

    def _close(self):
        self.resolved = True
        smuggle_pending.discard(self.mafia.id)
        for child in self.children:
            child.disabled = True
        self.stop()

    @discord.ui.button(label="수락 (뒷돈 받기)", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.resolved:
            await interaction.response.send_message("이미 처리된 제안입니다.", ephemeral=True)
            return
        self._close()
        await interaction.response.defer()

        now = datetime.now()
        mafia_data = get_user_data(self.mafia.id)

        if self.mafia.id in jail_list and now < jail_list[self.mafia.id]:
            embed = discord.Embed(title="거래 불발", description=f"{self.mafia.mention} 님이 감옥에 수감되어 거래가 무산되었습니다.", color=discord.Color.dark_grey())
            await interaction.edit_original_response(embed=embed, view=None)
            return
        if mafia_data["money"] < self.amount:
            embed = discord.Embed(title="거래 불발", description=f"{self.mafia.mention} 님의 자금이 부족해 거래가 무산되었습니다. (제안 `${self.amount:,}`)", color=discord.Color.dark_grey())
            await interaction.edit_original_response(embed=embed, view=None)
            return

        try:
            update_user_stats(self.mafia.id, -self.amount, 0)
            update_user_stats(self.police.id, self.amount, 0)
        except Exception as e:
            print(f"Smuggle accept DB error: {e}")
            await interaction.edit_original_response(
                embed=discord.Embed(title="처리 오류", description="DB 오류로 거래가 취소되었습니다.", color=discord.Color.red()), view=None)
            return

        smuggle_immunity[(self.police.id, self.mafia.id)] = now + timedelta(seconds=SMUGGLE_IMMUNITY_SEC)

        embed = discord.Embed(
            title="[밀수 거래 성사]",
            description=(
                f"**{self.police.mention}** 경관이 **{self.mafia.mention}** 님의 제안을 받아들였습니다.\n\n"
                f"**오간 뒷돈:** `${self.amount:,}`\n"
                f"**효과:** 앞으로 **{SMUGGLE_IMMUNITY_SEC // 60}분간** 이 경찰은 해당 마피아를 `/검거`할 수 없습니다.\n"
                f"다른 경찰에게는 효과가 없습니다."
            ),
            color=discord.Color.dark_gold()
        )
        await interaction.edit_original_response(embed=embed, view=None)

    @discord.ui.button(label="거절 (청렴)", style=discord.ButtonStyle.danger)
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.resolved:
            await interaction.response.send_message("이미 처리된 제안입니다.", ephemeral=True)
            return
        self._close()
        await interaction.response.defer()

        mafia_data = get_user_data(self.mafia.id)
        honor_loss = min(mafia_data["honor"], SMUGGLE_REJECT_HONOR_LOSS)
        # 명예가 부족해 다 못 깎은 만큼은 벌금으로 대신 부과 (명예 0pt 신입도 패널티를 체감하도록)
        missing_honor = SMUGGLE_REJECT_HONOR_LOSS - honor_loss
        fine = min(mafia_data["money"], SMUGGLE_REJECT_FINE + missing_honor * SMUGGLE_FINE_PER_MISSING_HONOR)

        try:
            update_user_stats(self.mafia.id, -fine, -honor_loss)
            update_user_stats(self.police.id, 0, SMUGGLE_REJECT_POLICE_HONOR)
        except Exception as e:
            print(f"Smuggle reject DB error: {e}")

        wanted_list[self.mafia.id] = datetime.now() + timedelta(seconds=SMUGGLE_REJECT_WANTED_SEC)
        save_status(self.mafia.id)

        embed = discord.Embed(
            title="[밀수 시도 적발]",
            description=(
                f"**{self.police.mention}** 경관이 **{self.mafia.mention}** 님의 뇌물 제안을 단호히 거절했습니다!\n\n"
                f"**마피아 패널티:** **{SMUGGLE_REJECT_WANTED_SEC}초간 지명수배** / 벌금 `-${fine:,}` / 명예 `-{honor_loss} pt`"
                + (f" (명예 부족분 {missing_honor}pt는 벌금으로 환산)" if missing_honor > 0 else "") + "\n"
                f"**경찰 포상:** 청렴 포상 `+{SMUGGLE_REJECT_POLICE_HONOR} pt`"
            ),
            color=discord.Color.red()
        )
        await interaction.edit_original_response(embed=embed, view=None)

    async def on_timeout(self):
        if self.resolved:
            return
        self._close()
        if self.message:
            try:
                embed = discord.Embed(
                    title="[밀수 제안 만료]",
                    description=f"{self.police.mention} 경관이 응답하지 않아 제안이 사라졌습니다. (패널티 없음)",
                    color=discord.Color.dark_grey()
                )
                await self.message.edit(embed=embed, view=None)
            except Exception:
                pass


@bot.tree.command(name="밀수", description="[조직원 전용] 경찰에게 뇌물을 건네 밀수를 눈감아 달라고 제안합니다. (수락/거절은 경찰 마음)")
@app_commands.describe(경찰="뇌물을 건넬 경찰", 뇌물=f"건넬 자금 (${SMUGGLE_MIN_BRIBE:,}~${SMUGGLE_MAX_BRIBE:,})")
async def smuggle_command(interaction: discord.Interaction, 경찰: discord.Member, 뇌물: app_commands.Range[int, SMUGGLE_MIN_BRIBE, SMUGGLE_MAX_BRIBE]):
    if not await check_maintenance(interaction): return
    if not await check_membership(interaction): return
    if await check_jail(interaction): return

    user = interaction.user
    mafia_role = discord.utils.get(interaction.guild.roles, name="조직원")
    police_role = discord.utils.get(interaction.guild.roles, name="경찰")

    if not mafia_role or mafia_role not in user.roles:
        await interaction.response.send_message("밀수는 마피아 조직원만 시도할 수 있습니다!", ephemeral=True)
        return
    if 경찰.id == user.id or 경찰.bot or not police_role or police_role not in 경찰.roles:
        await interaction.response.send_message("뇌물을 건넬 대상은 **경찰** 역할을 가진 유저여야 합니다.", ephemeral=True)
        return

    now = datetime.now()
    if user.id in smuggle_pending:
        await interaction.response.send_message("이미 응답을 기다리는 밀수 제안이 있습니다.", ephemeral=True)
        return
    if user.id in smuggle_cooldowns:
        remaining = int((smuggle_cooldowns[user.id] - now).total_seconds())
        if remaining > 0:
            await interaction.response.send_message(f"눈치가 보여 당분간 밀수를 못 합니다. **{remaining}초** 뒤에 다시 시도하세요.", ephemeral=True)
            return
    existing = smuggle_immunity.get((경찰.id, user.id))
    if existing and now < existing:
        await interaction.response.send_message(f"이미 **{경찰.display_name}** 경관과 거래가 성사된 상태입니다.", ephemeral=True)
        return
    if get_user_data(user.id)["money"] < 뇌물:
        await interaction.response.send_message("건넬 자금이 부족합니다!", ephemeral=True)
        return

    smuggle_cooldowns[user.id] = now + timedelta(seconds=SMUGGLE_COOLDOWN_SEC)
    smuggle_pending.add(user.id)

    embed = discord.Embed(
        title="[은밀한 밀수 제안]",
        description=(
            f"**{user.mention}** 님이 **{경찰.mention}** 경관에게 접근해 봉투를 내밉니다...\n\n"
            f"**제안 금액:** `${뇌물:,}`\n"
            f"**조건:** 수락하면 **{SMUGGLE_IMMUNITY_SEC // 60}분간** 경관은 이 마피아를 검거할 수 없습니다.\n"
            f"거절하면 마피아는 지명수배·벌금·명예 하락의 패널티를 받습니다.\n\n"
            f"**{SMUGGLE_TIMEOUT_SEC}초** 안에 응답하세요."
        ),
        color=discord.Color.dark_purple()
    )
    view = SmuggleView(mafia=user, police=경찰, amount=뇌물)
    await interaction.response.send_message(content=경찰.mention, embed=embed, view=view)
    view.message = await interaction.original_response()

    # === [경찰 전용] `/순찰` 명령어 ===
# 경찰 활동 명예 (이 값들을 조절해서 경찰 성장 속도를 맞추세요)
PATROL_HONOR = 28                   # /순찰 1회 명예

patrol_cooldowns = {}

@bot.tree.command(name="순찰", description="[경찰 전용] 지정한 거점을 순찰하여 마피아들의 임무 성공률을 낮추고 수당을 받습니다.")
@app_commands.choices(지역=[
    app_commands.Choice(name="레벤하임 (수도)", value="레벤하임"),
    app_commands.Choice(name="던베르크 (공업지대)", value="던베르크"),
    app_commands.Choice(name="블랙워드 (빈민가)", value="블랙워드"),
    app_commands.Choice(name="에르느코항 (항구도시)", value="에르느코항"),
    app_commands.Choice(name="로젠펠트 (유흥업소)", value="로젠펠트")
])
async def patrol_location(interaction: discord.Interaction, 지역: app_commands.Choice[str]):
    from .police_unit import police_shift_record  # 순환 import 방지: 호출 시점에 가져옴
    if not await check_maintenance(interaction): return
    if await check_jail(interaction): return

    user = interaction.user
    guild_id = interaction.guild_id

    # 1. 경찰 역할 검증
    police_role = discord.utils.get(interaction.guild.roles, name="경찰")
    if not police_role or police_role not in user.roles:
        await interaction.response.send_message("치안국 소속 경찰만 순찰 임무를 수행할 수 있습니다!", ephemeral=True)
        return

    # 2. 쿨타임 체크 (3분)
    now = datetime.now()
    if user.id in patrol_cooldowns:
        remaining = int((patrol_cooldowns[user.id] - now).total_seconds())
        if remaining > 0:
            await interaction.response.send_message(f"순찰 피로가 아직 안 풀렸습니다. **{remaining}초** 뒤에 다시 순찰할 수 있습니다.", ephemeral=True)
            return

    patrol_cooldowns[user.id] = now + timedelta(seconds=180)
    await interaction.response.defer()

    selected_loc = 지역.value

    # 3. 해당 지역에 5분(300초) 동안 순찰 효과 부여
    if guild_id not in active_patrols:
        active_patrols[guild_id] = {}
    
    patrol_duration = 300  # 5분
    active_patrols[guild_id][selected_loc] = time.time() + patrol_duration

    # 4. 경찰 보상 지급 (자금 $400~800, 명예 PATROL_HONOR)
    earned_money = inc_money(random.randint(400, 800))
    earned_honor = int(round(PATROL_HONOR * police_bonuses(get_user_data(user.id).get("inventory"))["honor_mult"]))

    try:
        update_user_stats(user.id, earned_money, earned_honor)
    except Exception as e:
        print(f"Patrol reward error: {e}")
    police_shift_record(user.id, "patrol")   # /월급 근무 실적
    ach_note = await police_ach_bump(user.id, police_patrols=1)   # 업적 카운터

    police_info = get_user_data(user.id)
    police_rank = get_user_rank(police_info["honor"], is_police=True)

    embed = discord.Embed(
        title=f"[{selected_loc}] 치안 순찰 완료",
        description=(
            f"**{user.mention}** 경관이 **{selected_loc}** 구역의 순찰을 철저히 마쳤습니다!\n\n"
            f"**효과:** 향후 **5분간** 해당 지역에서 활동하는 마피아들의 임무 성공률이 **15% 감소**합니다.\n\n"
            f"**순찰 수당:** `${earned_money:,}`\n"
            f"**경찰 실적:** `+{earned_honor} pt` (현재 직급: `{police_rank}`)\n"
            f"**다음 순찰 가능:** 3분 후"
            + ach_note
        ),
        color=discord.Color.blue()
    )
    if user.avatar:
        embed.set_thumbnail(url=user.avatar.url)

    await interaction.followup.send(embed=embed)
