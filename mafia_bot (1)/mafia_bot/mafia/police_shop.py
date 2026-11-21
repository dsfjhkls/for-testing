"""치안국 보급품 / 경찰상점"""

import discord
from discord import app_commands

from .core import bot, inc_money, supabase
from .status import (
    BLACK_MARKET_ITEMS,
    LOCATIONS,
    _require_db,
    check_jail,
    delete_user_data,
    get_user_data,
    update_user_location,
)
from .admin import check_maintenance, check_membership
from .blackmarket import MarketView
from .profile import POLICE_SHOP_ITEMS

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
