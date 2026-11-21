"""제작 시스템 (/제작)"""

import asyncio
import discord
import random
import threading as _threading

from .core import GuardedView, bot, supabase
from .status import _check_updated, _require_db, check_jail, get_user_data
from .achievements import (
    ACH_BY_ID,
    CRAFT_RECIPES,
    MATERIAL_PREFIX,
    M_ALLOY,
    M_BIANCA,
    M_BORONIN,
    M_BULL,
    M_DARK,
    M_EDEN,
    M_EXEC,
    M_KING,
    M_MARCO,
    M_ORIGIN,
    M_RAT,
    M_SCRAP,
    M_STEEL,
    _CRAFT_ALL_GOAL,
)
from .admin import check_maintenance, check_membership

CRAFT_PREFIX = "[제작] "

# 재료 → 획득처 안내 (/제작 화면에 표시)
MATERIAL_SOURCES = {
    M_SCRAP:   "/전투: 골목길 깡패·배신자·거상",
    M_STEEL:   "/전투: 배신자·거상·수배자·암살자 / 네임드",
    M_ALLOY:   "/전투: 수배자·암살자 / 네임드·보스·레이드",
    M_MARCO:   "/네임드: 백정 마르코",
    M_BIANCA:  "/네임드: 독사 비앙카",
    M_BORONIN: "/네임드: 철의 장군 보로닌",
    M_EXEC:    "/네임드: 붉은 달의 처형자",
    M_DARK:    "/보스: 어둠의 지배자",
    M_EDEN:    "/보스: 에칼 데 페스토",
    M_ORIGIN:  "/보스: 태초의 파괴자",
    M_RAT:     "/레이드: 하수구의 쥐왕",
    M_BULL:    "/레이드: 부두 항만의 폭군",
    M_KING:    "/레이드: 칼렌데 지하 황제",
}

# 드랍 표: {처치한 대상 이름: [(재료, 확률, (최소, 최대)), ...]}   — 승리했을 때만 지급
MATERIAL_DROPS = {
    # /전투
    "골목길 깡패":      [(M_SCRAP, 0.70, (1, 2))],
    "조직의 배신자":    [(M_SCRAP, 1.00, (1, 3)), (M_STEEL, 0.25, (1, 1))],
    "암시장 악덕 거상": [(M_STEEL, 0.80, (1, 2)), (M_SCRAP, 0.50, (1, 3))],
    "지명수배자 보스":  [(M_STEEL, 1.00, (1, 3)), (M_ALLOY, 0.25, (1, 1))],
    "칼렌데 암살자":    [(M_ALLOY, 0.70, (1, 2)), (M_STEEL, 0.50, (1, 2))],
    # /네임드
    "[네임드] 백정 마르코":        [(M_MARCO, 1.00, (1, 2)), (M_STEEL, 1.00, (1, 3))],
    "[네임드] 독사 비앙카":        [(M_BIANCA, 1.00, (1, 2)), (M_STEEL, 1.00, (1, 3)), (M_ALLOY, 0.30, (1, 1))],
    "[네임드] 철의 장군 보로닌":   [(M_BORONIN, 1.00, (1, 2)), (M_ALLOY, 1.00, (1, 2))],
    "[네임드] 붉은 달의 처형자":   [(M_EXEC, 1.00, (1, 2)), (M_ALLOY, 1.00, (2, 3))],
    # /보스
    "[히든보스] 어둠의 지배자":    [(M_DARK, 1.00, (2, 3)), (M_ALLOY, 1.00, (2, 4))],
    "[테러보스] 에칼 데 페스토":   [(M_EDEN, 1.00, (2, 3)), (M_ALLOY, 1.00, (3, 4))],
    "[최강보스] 태초의 파괴자":    [(M_ORIGIN, 1.00, (2, 3)), (M_DARK, 1.00, (1, 2)), (M_ALLOY, 1.00, (3, 5))],
    # 강화 보스: 일반 보스보다 재료를 훨씬 많이, 특수 합금은 확정으로 줍니다
    "[강화보스] 심연을 삼킨 어둠의 지배자":      [(M_DARK, 1.00, (5, 8)), (M_ALLOY, 1.00, (6, 10))],
    "[강화보스] 대재앙의 화신 에칼 데 페스토":    [(M_EDEN, 1.00, (5, 8)), (M_ALLOY, 1.00, (7, 11))],
    "[강화보스] 종말을 선고하는 태초의 파괴자":   [(M_ORIGIN, 1.00, (5, 8)), (M_DARK, 1.00, (3, 5)), (M_ALLOY, 1.00, (8, 12))],
    # /레이드 (RAID_TIERS 의 name)
    "하수구의 쥐왕":      [(M_RAT, 1.00, (1, 3)), (M_SCRAP, 1.00, (3, 5))],
    "부두 항만의 폭군":   [(M_BULL, 1.00, (1, 3)), (M_STEEL, 1.00, (2, 4))],
    "칼렌데 지하 황제":   [(M_KING, 1.00, (1, 3)), (M_ALLOY, 1.00, (2, 3))],
}
ACH_BY_ID["a_craft_all"]["goal"] = _CRAFT_ALL_GOAL   # 업적 '완전한 장인' 목표를 실제 레시피 수에 맞춤
CRAFT_BY_ID = {r["id"]: r for r in CRAFT_RECIPES}
CRAFT_BY_ITEM = {CRAFT_PREFIX + r["name"]: r for r in CRAFT_RECIPES}
CRAFT_SLOT_LABEL = {"weapon": "무기", "armor": "방어구"}

_inv_lock = _threading.Lock()   # 재료 지급·제작처럼 인벤토리를 읽고 다시 쓰는 작업이 서로 덮어쓰지 않도록


def crafted_gear_best(inventory: dict):
    """보유한 제작 장비 중 가장 강한 (무기, 방어구) 레시피. 없으면 None."""
    best_w, best_a = None, None
    for key, r in CRAFT_BY_ITEM.items():
        if key not in (inventory or {}):
            continue
        if r["slot"] == "weapon":
            if best_w is None or (r["atk"], r["hp"]) > (best_w["atk"], best_w["hp"]):
                best_w = r
        else:
            if best_a is None or (r["def"], r["hp"]) > (best_a["def"], best_a["hp"]):
                best_a = r
    return best_w, best_a


def _mat_short(name: str) -> str:
    return name[len(MATERIAL_PREFIX):] if name.startswith(MATERIAL_PREFIX) else name


def roll_material_drops(source_name: str) -> dict:
    """처치한 대상 이름으로 드랍 판정 → {재료: 개수}"""
    drops = {}
    for mat, chance, (lo, hi) in MATERIAL_DROPS.get(source_name, []):
        if random.random() < chance:
            drops[mat] = drops.get(mat, 0) + random.randint(lo, hi)
    return drops


def add_materials(inventory: dict, drops: dict) -> dict:
    for mat, n in drops.items():
        inventory[mat] = inventory.get(mat, 0) + n
    return inventory


def format_material_drops(drops: dict) -> str:
    return ", ".join(f"{_mat_short(k)} x{v}" for k, v in drops.items())


def grant_materials_sync(user_id: int, drops: dict):
    """(레이드 정산 등) 인벤토리에 재료를 더해 DB 에 저장"""
    if not drops:
        return
    _require_db()
    with _inv_lock:
        data = get_user_data(user_id)
        inv = dict(data.get("inventory") or {})
        add_materials(inv, drops)
        res = supabase.table("user_data").update({"inventory": inv}).eq("user_id", str(user_id)).execute()
        _check_updated(res, "grant_materials")


def craft_missing(inv: dict, money: int, r: dict) -> list:
    """부족한 항목 설명 목록 (비어 있으면 제작 가능)"""
    lacks = [f"{_mat_short(m)} {need - inv.get(m, 0)}개" for m, need in r["mats"].items() if inv.get(m, 0) < need]
    if money < r["money"]:
        lacks.append(f"제작비 ${r['money'] - money:,}")
    return lacks


def craft_sync(user_id: int, recipe_id: str):
    """제작 실행. 반환: (성공 여부, 메시지, 남은 자금)"""
    r = CRAFT_BY_ID.get(recipe_id)
    if not r:
        return False, "존재하지 않는 제작법입니다.", None
    _require_db()
    with _inv_lock:
        data = get_user_data(user_id)
        inv = dict(data.get("inventory") or {})
        money = int(data.get("money") or 0)
        item = CRAFT_PREFIX + r["name"]
        if item in inv:
            return False, "이미 보유한 장비입니다.", money
        lacks = craft_missing(inv, money, r)
        if lacks:
            return False, "부족한 항목: " + ", ".join(lacks), money
        for m, need in r["mats"].items():
            inv[m] -= need
            if inv[m] <= 0:
                del inv[m]
        inv[item] = 1
        money -= r["money"]
        res = supabase.table("user_data").update({"money": money, "inventory": inv}).eq("user_id", str(user_id)).execute()
        _check_updated(res, "craft")
        return True, "", money


def craft_stat_text(r: dict) -> str:
    parts = []
    if r["atk"]: parts.append(f"공격력 +{r['atk']:,}")
    if r["def"]: parts.append(f"방어력 +{r['def']:,}")
    if r["hp"]:  parts.append(f"체력 +{r['hp']:,}")
    return " / ".join(parts)


# ---------------------------------------------------------------- UI
def craft_main_embed(user: discord.Member, data: dict, note: str = "") -> discord.Embed:
    inv = data.get("inventory") or {}
    money = int(data.get("money") or 0)
    embed = discord.Embed(title="[제작소] 장비 제작", color=discord.Color.dark_orange())
    desc = ("전투에서 모은 **재료**로 무기와 방어구를 만들 수 있습니다. 아래 목록에서 제작할 장비를 고르세요.\n"
            "무기·방어구는 각각 **가장 강한 1개만** 전투 능력치에 적용됩니다. (상점 무기와도 비교해 더 강한 쪽 적용)")
    if note:
        desc = f"{note}\n\n{desc}"
    embed.description = desc

    mats = [(m, inv.get(m, 0)) for m in MATERIAL_SOURCES]
    lines = [f"• **{_mat_short(m)}** x{n} — *{MATERIAL_SOURCES[m]}*" for m, n in mats]
    embed.add_field(name="재료 보유 현황 / 획득처", value="\n".join(lines), inline=False)

    bw, ba = crafted_gear_best(inv)
    applied = (f"무기: **{bw['name']}** ({craft_stat_text(bw)})" if bw else "무기: 없음") + "\n" + \
              (f"방어구: **{ba['name']}** ({craft_stat_text(ba)})" if ba else "방어구: 없음")
    embed.add_field(name="현재 적용 중인 제작 장비", value=applied, inline=False)
    embed.set_footer(text=f"보유 자금 ${money:,}")
    if user.avatar:
        embed.set_thumbnail(url=user.avatar.url)
    return embed


def craft_detail_embed(user: discord.Member, data: dict, r: dict, note: str = "") -> discord.Embed:
    inv = data.get("inventory") or {}
    money = int(data.get("money") or 0)
    owned = (CRAFT_PREFIX + r["name"]) in inv
    embed = discord.Embed(title=f"[제작] {r['name']}", color=discord.Color.dark_orange())
    desc = f"*{r['desc']}*\n\n**종류:** {CRAFT_SLOT_LABEL[r['slot']]}\n**효과:** {craft_stat_text(r)}"
    if note:
        desc = f"{note}\n\n{desc}"
    if owned:
        desc += "\n\n**이미 보유 중인 장비입니다.**"
    embed.description = desc
    req = []
    for m, need in r["mats"].items():
        have = inv.get(m, 0)
        req.append(f"{'[충족]' if have >= need else '[부족]'} {_mat_short(m)} {have}/{need}")
    req.append(f"{'[충족]' if money >= r['money'] else '[부족]'} 제작비 ${money:,} / ${r['money']:,}")
    embed.add_field(name="필요 재료", value="\n".join(req), inline=False)
    if user.avatar:
        embed.set_thumbnail(url=user.avatar.url)
    return embed


class CraftView(GuardedView):
    def __init__(self, owner: discord.Member, data: dict, selected: str = None):
        super().__init__(timeout=180)
        self.owner = owner
        self.selected = selected
        self.message = None
        self._build(data)

    def _build(self, data: dict):
        self.clear_items()
        inv = data.get("inventory") or {}
        money = int(data.get("money") or 0)
        options = []
        for r in CRAFT_RECIPES:
            if (CRAFT_PREFIX + r["name"]) in inv:
                tag = "[보유]"
            elif not craft_missing(inv, money, r):
                tag = "[제작 가능]"
            else:
                tag = "[재료 부족]"
            desc = f"{tag} {CRAFT_SLOT_LABEL[r['slot']]} | {craft_stat_text(r)} | ${r['money']:,}"
            options.append(discord.SelectOption(label=r["name"], value=r["id"], description=desc[:100],
                                                default=(r["id"] == self.selected)))
        select = discord.ui.Select(placeholder="제작할 장비를 선택하세요...", options=options, min_values=1, max_values=1)
        select.callback = self._on_select
        self.add_item(select)

        sel = CRAFT_BY_ID.get(self.selected)
        can = bool(sel) and (CRAFT_PREFIX + sel["name"]) not in inv and not craft_missing(inv, money, sel)
        button = discord.ui.Button(label="제작하기", style=discord.ButtonStyle.success, disabled=not can)
        button.callback = self._on_craft
        self.add_item(button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner.id:
            await interaction.response.send_message("본인의 제작 창만 조작할 수 있습니다.", ephemeral=True)
            return False
        return True

    async def _on_select(self, interaction: discord.Interaction):
        self.selected = interaction.data["values"][0]
        data = await asyncio.to_thread(get_user_data, self.owner.id)
        self._build(data)
        await interaction.response.edit_message(
            embed=craft_detail_embed(self.owner, data, CRAFT_BY_ID[self.selected]), view=self)

    async def _on_craft(self, interaction: discord.Interaction):
        rid = self.selected
        if rid not in CRAFT_BY_ID:
            await interaction.response.send_message("먼저 제작할 장비를 선택해 주세요.", ephemeral=True)
            return
        await interaction.response.defer()
        try:
            ok, msg, _ = await asyncio.to_thread(craft_sync, self.owner.id, rid)
        except Exception as e:
            print(f"DB Error (craft): {e}")
            await interaction.followup.send("데이터베이스 오류로 제작에 실패했습니다. 재료와 자금은 차감되지 않았습니다.", ephemeral=True)
            return
        data = await asyncio.to_thread(get_user_data, self.owner.id)
        r = CRAFT_BY_ID[rid]
        self._build(data)
        note = f"**제작 완료!** {r['name']} 을(를) 손에 넣었습니다." if ok else f"**제작 실패** — {msg}"
        embed = craft_detail_embed(self.owner, data, r, note)
        embed.color = discord.Color.green() if ok else discord.Color.red()
        await interaction.edit_original_response(embed=embed, view=self)

    async def on_timeout(self):
        if self.message is not None:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass


@bot.tree.command(name="제작", description="[조직원/경찰] 전투·네임드·보스·레이드에서 얻은 재료로 무기와 방어구를 제작합니다.")
async def craft_command(interaction: discord.Interaction):
    if not await check_maintenance(interaction): return
    if not await check_membership(interaction): return
    if await check_jail(interaction): return

    await interaction.response.defer()
    user = interaction.user
    data = await asyncio.to_thread(get_user_data, user.id)
    view = CraftView(user, data)
    view.message = await interaction.followup.send(embed=craft_main_embed(user, data), view=view)
