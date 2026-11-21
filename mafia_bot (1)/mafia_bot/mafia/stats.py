"""/능력치"""

import asyncio
import discord
from discord import app_commands

from .core import bot
from .status import ELITE_TROPHY_STATS, WEAPON_TIERS, check_jail, compute_combat_stats, get_user_data
from .achievements import CRAFT_RECIPES
from .admin import check_maintenance, check_membership
from .police_ops import POLICE_RADIO_OP_BONUS
from .profile import POLICE_SHOP_ITEMS
from .police_shop import (
    POLICE_BADGE_HONOR_BONUS,
    POLICE_BADGE_NAME,
    POLICE_INSIGNIA_HONOR_BONUS,
    POLICE_INSIGNIA_NAME,
    POLICE_RADIO_ARREST_BONUS,
    POLICE_RADIO_NAME,
    POLICE_TERMINAL_COST_DISCOUNT,
    POLICE_TERMINAL_NAME,
    POLICE_TERMINAL_PRIZE_BONUS,
)
from .crafting import CRAFT_PREFIX, crafted_gear_best

# ==========================================================================
# [/능력치]  전투 능력치 확인 (체력/공격력/방어력 + 구성 내역)
#   - 실제 전투에 쓰이는 compute_combat_stats 결과를 그대로 보여주고, 어디서 얼마가 붙었는지 풀어서 설명합니다.
# ==========================================================================
def combat_stats_breakdown(honor: int, inventory: dict) -> dict:
    inventory = inventory or {}
    # 상점 무기 (먼저 보유한 하나)
    shop_name, shop = None, (0, 0)
    for name, a, h in WEAPON_TIERS:
        if name in inventory:
            shop_name, shop = name, (a, h)
            break
    craft_w, craft_a = crafted_gear_best(inventory)

    if craft_w and (craft_w["atk"], craft_w["hp"]) > shop:
        weapon = {"name": craft_w["name"], "src": "제작", "atk": craft_w["atk"], "hp": craft_w["hp"]}
    elif shop_name:
        weapon = {"name": shop_name, "src": "상점", "atk": shop[0], "hp": shop[1]}
    else:
        weapon = None
    armor = {"name": craft_a["name"], "def": craft_a["def"], "hp": craft_a["hp"]} if craft_a else None

    # 보유했지만 적용되지 않는 장비
    unused = []
    for name, a, h in WEAPON_TIERS:
        if name in inventory and not (weapon and weapon["src"] == "상점" and weapon["name"] == name):
            unused.append(f"{name} (무기)")
    for r in CRAFT_RECIPES:
        if (CRAFT_PREFIX + r["name"]) not in inventory:
            continue
        if r["slot"] == "weapon" and not (weapon and weapon["src"] == "제작" and weapon["name"] == r["name"]):
            unused.append(f"{r['name']} (무기)")
        if r["slot"] == "armor" and not (armor and armor["name"] == r["name"]):
            unused.append(f"{r['name']} (방어구)")

    return {
        "base_hp": 1000 + honor * 5,
        "base_atk": 100 + int(honor * 0.5),
        "base_def": 10,
        "weapon": weapon,
        "armor": armor,
        "ticket": "임무 실패 방지권" in inventory,
        "police": [(it["name"], it.get("atk_bonus", 0), it.get("hp_bonus", 0), it.get("def_bonus", 0))
                   for it in POLICE_SHOP_ITEMS if it["name"] in inventory and (it.get("atk_bonus") or it.get("hp_bonus") or it.get("def_bonus"))],
        "elite": [(n, a, h, d) for n, (a, h, d) in ELITE_TROPHY_STATS.items() if n in inventory],
        "unused": unused,
        "total": compute_combat_stats(honor, inventory),   # 실제 전투에서 쓰는 값
    }


def build_stats_embed(target: discord.abc.User, data: dict) -> discord.Embed:
    honor = int(data.get("honor", 0) or 0)
    money = int(data.get("money", 0) or 0)
    inv = data.get("inventory") or {}
    b = combat_stats_breakdown(honor, inv)
    t = b["total"]

    embed = discord.Embed(title=f"[{target.display_name} 님의 전투 능력치]", color=discord.Color.dark_teal())
    embed.description = (f"**체력** `{t['hp']:,}`\n**공격력** `{t['atk']:,}`\n**방어력** `{t['def']:,}`\n\n"
                         f"명예 `{honor:,} pt` · 보유 자금 `${money:,}`")

    lines = [f"• **기본** (명예 반영): 체력 +{b['base_hp']:,} / 공격력 +{b['base_atk']:,} / 방어력 +{b['base_def']}"]
    w = b["weapon"]
    if w:
        parts = [f"공격력 +{w['atk']:,}"] + ([f"체력 +{w['hp']:,}"] if w["hp"] else [])
        lines.append(f"• **무기** [{w['src']}] {w['name']}: " + " / ".join(parts))
    else:
        lines.append("• **무기**: 없음")
    a = b["armor"]
    if a:
        lines.append(f"• **방어구** [제작] {a['name']}: 방어력 +{a['def']:,} / 체력 +{a['hp']:,}")
    else:
        lines.append("• **방어구**: 없음 (`/제작`에서 만들 수 있습니다)")
    if b["ticket"]:
        lines.append("• **임무 실패 방지권**: 방어력 +20")
    for ename, eatk, ehp, edef in b["elite"]:
        lines.append(f"• **강화 보스 전리품** {ename.split('] ', 1)[-1]}: 공격력 +{eatk:,} / 방어력 +{edef:,} / 체력 +{ehp:,}")
    for pname, patk, php, pdef in b["police"]:
        parts = ([f"공격력 +{patk:,}"] if patk else []) + ([f"방어력 +{pdef:,}"] if pdef else []) + ([f"체력 +{php:,}"] if php else [])
        lines.append(f"• **치안국 보급품** {pname}: " + " / ".join(parts))
    if POLICE_RADIO_NAME in inv:
        lines.append(f"• **{POLICE_RADIO_NAME}**: /검거 포상금 +{round(POLICE_RADIO_ARREST_BONUS * 100)}% · /작전 성공률 +{POLICE_RADIO_OP_BONUS}%")
    if POLICE_BADGE_NAME in inv:
        lines.append(f"• **{POLICE_BADGE_NAME}**: 검거·순찰·심문·작전 명예 실적 +{round(POLICE_BADGE_HONOR_BONUS * 100)}%")
    if POLICE_INSIGNIA_NAME in inv:
        lines.append(f"• **{POLICE_INSIGNIA_NAME}**: 명예 실적 +{round(POLICE_INSIGNIA_HONOR_BONUS * 100)}%")
    if POLICE_TERMINAL_NAME in inv:
        lines.append(f"• **{POLICE_TERMINAL_NAME}**: 구역 탈환 포상금 +{round(POLICE_TERMINAL_PRIZE_BONUS * 100)}% / 탈환 비용 {round(POLICE_TERMINAL_COST_DISCOUNT * 100)}% 할인")
    embed.add_field(name="능력치 구성", value="\n".join(lines), inline=False)

    if b["unused"]:
        embed.add_field(name="보유 중이지만 적용되지 않는 장비",
                        value="\n".join(f"• {x}" for x in b["unused"])[:1000] +
                              "\n*무기·방어구는 각각 가장 강한 1개만 적용됩니다.*", inline=False)
    embed.set_footer(text="/전투 · /네임드 · /보스 · /결투 · 레이드 · 점령전에 모두 이 능력치가 쓰입니다.")
    if target.avatar:
        embed.set_thumbnail(url=target.avatar.url)
    return embed


@bot.tree.command(name="능력치", description="[공용] 본인 또는 다른 유저의 전투 능력치(체력·공격력·방어력)와 구성을 확인합니다.")
@app_commands.describe(member="조회할 유저 (비우면 본인)")
async def stats_command(interaction: discord.Interaction, member: discord.Member = None):
    if not await check_maintenance(interaction): return
    if not await check_membership(interaction): return
    if await check_jail(interaction): return

    target = member or interaction.user
    if target.bot:
        await interaction.response.send_message("봇의 능력치는 확인할 수 없습니다.", ephemeral=True)
        return
    await interaction.response.defer()
    data = await asyncio.to_thread(get_user_data, target.id)
    await interaction.followup.send(embed=build_stats_embed(target, data))
