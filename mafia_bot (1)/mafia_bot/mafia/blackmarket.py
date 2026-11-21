"""암시장"""

import asyncio
import discord
import time

from .core import bot
from .status import (
    BLACK_MARKET_ITEMS,
    DRINK_ITEM,
    NO_COOLDOWN_ITEM,
    check_jail,
    get_user_data,
    get_user_rank,
    mission_cooldowns,
    update_user_market_purchase,
)
from .alignment import CITIZEN_JOIN_GATING, join_requirement_text
from .admin import check_maintenance, check_membership
from .transfer import get_setting
from .join_leave import LeaveConfirmView, SelfJoinView
from .mission import MissionSession, MissionView

# === 4. 암시장 드롭다운 뷰 ===
class MarketSelect(discord.ui.Select):
    def __init__(self, owner: discord.Member, location: str):
        self.owner = owner
        self.location = location
        items = BLACK_MARKET_ITEMS.get(location, [])
        options = [discord.SelectOption(label=f"{i['name']} (${i['price']:,})", description=i['desc'], value=i['id']) for i in items]
        super().__init__(placeholder=f"{location} 암시장 상품을 선택하세요...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        if interaction.user != self.owner:
            await interaction.response.send_message("타인의 거래 창은 조작할 수 없습니다.", ephemeral=True)
            return

        selected_id = self.values[0]
        items = BLACK_MARKET_ITEMS.get(self.location, [])
        target_item = next((item for item in items if item['id'] == selected_id), None)

        if not target_item:
            await interaction.response.send_message("존재하지 않는 아이템입니다.", ephemeral=True)
            return

        user_info = get_user_data(interaction.user.id)
        old_rank = get_user_rank(user_info.get("honor", 0))
        inventory = user_info.get("inventory", {})
        item_name = target_item['name']
        special_effect_text = f"\n**특수 효과:** {target_item['desc']}"

        if inventory.get(item_name, 0) >= 1:
            await interaction.response.send_message("이미 소지 중인 장비입니다.", ephemeral=True)
            return

        if user_info["money"] < target_item["price"]:
            await interaction.response.send_message("자금이 부족합니다!", ephemeral=True)
            return

        new_money = user_info["money"] - target_item["price"]
        new_honor = user_info["honor"]
        inventory[item_name] = 1

        update_user_market_purchase(interaction.user.id, new_money, new_honor, inventory)

        new_rank = get_user_rank(new_honor)
        rank_up_msg = ""
        if old_rank != new_rank:
            rank_up_msg = f"\n**[조직 승급]** **{new_rank}**(으)로 승급하셨습니다!"

        embed = discord.Embed(
            title="[암시장 거래 성사]",
            description=f"**{interaction.user.mention}** 님이 **{item_name}**을(를) 구매했습니다!{special_effect_text}{rank_up_msg}\n\n"
                        f"**남은 자금:** `${new_money:,}`\n"
                        f"**현재 명예:** `{new_honor} pt`",
            color=discord.Color.gold()
        )
        if interaction.user.avatar:
            embed.set_thumbnail(url=interaction.user.avatar.url)

        await interaction.response.edit_message(embed=embed, view=None)

class MarketView(discord.ui.View):
    def __init__(self, owner: discord.Member, location: str):
        super().__init__(timeout=60)
        self.add_item(MarketSelect(owner=owner, location=location))

# === 명령어 목록 ===
@bot.tree.command(name="가입", description="[마피아 GM] 패밀리 가입 버튼을 소환합니다.")
async def join_family(interaction: discord.Interaction):
    if not await check_maintenance(interaction): return
    embed = discord.Embed(title="[패밀리 입단 안내]",
                          description="아래 버튼을 눌러 마피아 조직 또는 치안국에 합류하세요.\n"
                                      + ("시민 시절의 성향과 활동 기록에 따라 가입 조건이 다릅니다.\n\n" + join_requirement_text() if CITIZEN_JOIN_GATING else ""),
                          color=discord.Color.gold())
    await interaction.response.send_message(embed=embed, view=SelfJoinView())

@bot.tree.command(name="탈퇴", description="[마피아 GM] 패밀리 탈퇴 절차를 진행합니다.")
async def leave_family(interaction: discord.Interaction):
    if not await check_maintenance(interaction): return
    if not await check_membership(interaction): return
    if await check_jail(interaction): return

    user = interaction.user
    embed = discord.Embed(title="[패밀리 탈퇴 경고]", description=f"**{user.mention}**님, 정말로 탈퇴하시겠습니까? (모든 기록이 초기화됩니다)", color=discord.Color.red())
    await interaction.response.send_message(embed=embed, view=LeaveConfirmView(target_user=user), ephemeral=True)

@bot.tree.command(name="임무", description="[조직원 전용] 원하는 비밀 임무를 직접 선택하여 수행합니다.")
async def execute_mission(interaction: discord.Interaction):
    if not await check_maintenance(interaction): return
    if not await check_membership(interaction): return
    if await check_jail(interaction): return

    # [추가] 경찰은 조직원 임무를 수행할 수 없음 (공개 응답으로 바뀌지 않도록 defer 전에 안내)
    police_role = discord.utils.get(interaction.guild.roles, name="경찰")
    if police_role and police_role in interaction.user.roles:
        await interaction.response.send_message("치안국 경찰은 범죄 조직의 비밀 임무를 수행할 수 없습니다!", ephemeral=True)
        return

    user = interaction.user
    user_stats = await asyncio.to_thread(get_user_data, user.id)
    current_loc = user_stats.get("location", "레벤하임")

    remain = int(mission_cooldowns.get(user.id, 0) - time.time() + 0.999)
    _inv = user_stats.get("inventory") or {}
    if remain > 0 and NO_COOLDOWN_ITEM not in _inv and _inv.get(DRINK_ITEM, 0) <= 0:
        await interaction.response.send_message(f"작전 준비 중입니다. **{remain}초** 뒤에 다시 임무를 수행할 수 있습니다.", ephemeral=True)
        return
    await interaction.response.defer()

    # 설정(/설정)의 '하드코어'가 켜져 있으면 여러 단계의 현장 작업으로 진행
    if get_setting(user_stats, "hardcore_mission"):
        session = MissionSession(user, user_stats, interaction.guild_id)
        session.message = await interaction.followup.send(embed=session.build_embed(), view=session, wait=True)
        return

    embed = discord.Embed(
        title=f"[{current_loc}] 비밀 임무 작전 하달",
        description=f"**{user.mention}** 님, 수행할 작전을 선택해 주십시오.",
        color=discord.Color.dark_purple()
    )
    if user.avatar:
        embed.set_thumbnail(url=user.avatar.url)

    await interaction.followup.send(embed=embed, view=MissionView(owner=user))
