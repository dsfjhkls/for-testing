"""경제: /제작, /프로필 /인벤토리, /경매

합친 모듈: crafting.py, profile.py, auction.py
"""

import asyncio
import discord
import io
import os
import random
import re
import threading as _threading
import time
import unicodedata
from datetime import datetime
from discord import app_commands
from functools import lru_cache
from PIL import Image, ImageDraw, ImageFont, ImageOps

from .core import bot, GuardedView, supabase, wanted_list
from .status import (
    _check_updated,
    _CRAFT_ALL_GOAL,
    _require_db,
    ACH_BY_ID,
    ACHIEVEMENTS,
    alignment_text,
    check_jail,
    CRAFT_RECIPES,
    get_equipped_title_name,
    get_title_blob,
    get_user_data,
    get_user_rank,
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
    MATERIAL_PREFIX,
    WEAPON_SHOP_ITEMS,
)
from .admin import check_maintenance, check_membership
from .battle import WeaponShopView


# ==========================================================================
# [합친 모듈] crafting.py
#   제작 시스템 (/제작)
# ==========================================================================
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
    "[심판보스] 침묵의 심판자":    [(M_EDEN, 1.00, (2, 3)), (M_DARK, 1.00, (1, 2)), (M_ALLOY, 1.00, (3, 5))],
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


# ==========================================================================
# [합친 모듈] profile.py
#   /프로필 (이미지 증명서)
# ==========================================================================
# === [공용] `/프로필` 명령어 (이미지 증명서 - 아날로그 종이 테마) ===
# 필요한 파일(봇 파일과 같은 폴더 또는 assets 폴더): paper_background.jpg, font_1.ttf (또는 font_fallback.otf)
PROFILE_BG_NAMES = ("paper_background.jpg",)
PROFILE_FONT_NAMES = ("font_1.ttf", "font.ttf", "font_fallback.otf")   # 앞에서부터 시도, 안 되면 다음 폰트
PROFILE_AREA = (70, 60, 468, 330)      # 배경(543x394) 안쪽 점선 테두리 안에서 글씨를 쓸 영역 (x1, y1, x2, y2)
PROFILE_SCALE = 2                      # 배경이 작아서 2배로 키워 그림 (글씨가 선명해짐)
PROFILE_INK = (60, 40, 20)             # 짙은 세피아 잉크색
PROFILE_MUTED = (125, 98, 66)          # 라벨·하단 문구용 옅은 갈색
PROFILE_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 프로젝트 루트 (assets 폴더 기준)

def _asset_paths(names):
    """봇 폴더 → assets 폴더 순서로 실제 존재하는 파일 경로를 하나씩 반환"""
    for folder in (PROFILE_BASE_DIR, os.path.join(PROFILE_BASE_DIR, "assets")):
        for name in names:
            path = os.path.join(folder, name)
            if os.path.isfile(path):
                yield path

def _draws_hangul(font, size: int) -> bool:
    """폰트가 한글을 실제로 그리는지 확인 (한글이 없으면 '가'와 '힣'이 똑같은 빈칸 모양으로 나옴)"""
    def glyph(ch):
        img = Image.new("L", (size * 2, size * 2), 0)
        ImageDraw.Draw(img).text((size // 4, size // 4), ch, fill=255, font=font)
        return img.tobytes()
    return glyph("가") != glyph("힣")

@lru_cache(maxsize=1)
def _profile_font_path():
    """한글이 실제로 그려지는 첫 번째 폰트 파일 (손상된 파일/한글 없는 폰트는 건너뜀). 처음 한 번만 검사"""
    for path in _asset_paths(PROFILE_FONT_NAMES):
        try:
            if _draws_hangul(ImageFont.truetype(path, 40), 40):
                return path
            print(f"한글 글자가 없는 폰트라 건너뜁니다: {path}")
        except OSError:
            print(f"폰트를 불러올 수 없어 건너뜁니다 (파일 손상?): {path}")
    print("사용 가능한 한글 폰트가 없습니다! font_1.ttf 또는 font_fallback.otf 를 봇 폴더에 넣어주세요.")
    return None

@lru_cache(maxsize=None)
def _profile_font(size: int):
    path = _profile_font_path()
    return ImageFont.truetype(path, size) if path else ImageFont.load_default(size)

def _clean_text(text) -> str:
    """이미지 폰트로 그릴 수 없는 이모지·기호 제거 (예: '깡패' -> '깡패')"""
    return "".join(
        c for c in str(text)
        if unicodedata.category(c) not in ("So", "Sk", "Cf", "Cs", "Co", "Cn") and c != "\ufe0f"
    ).strip()

def _fit_text(draw, text: str, font, max_width: int) -> str:
    """글자가 칸을 넘으면 '…' 로 줄임"""
    if draw.textlength(text, font=font) <= max_width:
        return text
    while text and draw.textlength(text + "…", font=font) > max_width:
        text = text[:-1]
    return text + "…"

async def _load_avatar(member: discord.Member, size: int = 190) -> Image.Image:
    """프로필 사진을 가져와 오래된 증명사진처럼 세피아 톤으로 변환 (실패하면 빈 사진)"""
    try:
        data = await member.display_avatar.replace(size=256, static_format="png").read()
        img = Image.open(io.BytesIO(data)).convert("RGB")
    except Exception as e:
        print(f"Avatar load error: {e}")
        img = Image.new("RGB", (256, 256), (185, 172, 150))
    img = img.resize((size, size), Image.Resampling.LANCZOS)
    return ImageOps.colorize(ImageOps.grayscale(img), black=(35, 22, 10), white=(238, 224, 196))

def make_profile_image(kind: str, rows: list, user_id: int, avatar: Image.Image, stamp_text: str = "승인") -> io.BytesIO:
    """증명서 이미지를 그려서 PNG 바이트로 반환. kind: police / mafia / citizen"""
    S = PROFILE_SCALE
    path = next(_asset_paths(PROFILE_BG_NAMES), None)
    if path:
        paper = Image.open(path).convert("RGBA")
        paper = paper.resize((paper.width * S, paper.height * S), Image.Resampling.LANCZOS)
    else:
        print("paper_background.jpg 를 찾을 수 없어 기본 종이색을 사용합니다.")
        paper = Image.new("RGBA", (543 * S, 394 * S), (234, 228, 205, 255))

    draw = ImageDraw.Draw(paper)
    x1, y1, x2, y2 = (v * S for v in PROFILE_AREA)
    cx = (x1 + x2) // 2

    # [1] 제목 + 이중선
    title = {"police": "치안국 신분증", "mafia": "패밀리 증명서", "citizen": "시민 등록증"}[kind]
    draw.text((cx, y1 + 8), " ".join(title), fill=PROFILE_INK, font=_profile_font(54), anchor="mt")
    draw.line((x1 + 30, y1 + 88, x2 - 30, y1 + 88), fill=PROFILE_INK, width=3)
    draw.line((x1 + 30, y1 + 96, x2 - 30, y1 + 96), fill=PROFILE_INK, width=1)
    draw.text((x2 - 30, y1 + 108), f"No. {str(user_id)[-6:]}", fill=PROFILE_MUTED, font=_profile_font(24), anchor="ra")

    # [2] 증명사진 (오른쪽)
    top = y1 + 140
    photo_x = x2 - 30 - (avatar.width + 12)
    paper.paste(ImageOps.expand(avatar, border=6, fill=PROFILE_INK), (photo_x, top))

    # [3] 인적 사항 (왼쪽): 라벨 + 값 + 밑줄
    dense = len(rows) > 6          # 줄이 7개 이상이면 간격과 글씨를 조금 줄여서 하단 문구와 겹치지 않게 함
    tight = len(rows) > 7          # 8줄 이상(사치품 줄까지 있는 시민)이면 한 단계 더 줄임
    pitch = 42 if tight else 48 if dense else 58
    f_label, f_value = _profile_font(22 if tight else 24 if dense else 26), _profile_font(27 if tight else 30 if dense else 34)
    label_x, value_x = x1 + 30, x1 + 150
    line_end = photo_x - 30
    for i, (label, value) in enumerate(rows):
        y = top + i * pitch
        draw.text((label_x, y + 6), label, fill=PROFILE_MUTED, font=f_label)
        draw.text((value_x, y), _fit_text(draw, value, f_value, line_end - value_x), fill=PROFILE_INK, font=f_value)
        draw.line((label_x, y + pitch - 8, line_end, y + pitch - 8), fill=(170, 145, 110), width=1)

    # [4] 붉은 도장 (사진 아래쪽에 살짝 걸치게)
    stamp = Image.new("RGBA", (170, 170), (0, 0, 0, 0))
    sd = ImageDraw.Draw(stamp)
    red = (170, 40, 40, 210)
    sd.ellipse((6, 6, 164, 164), outline=red, width=6)
    sd.ellipse((20, 20, 150, 150), outline=red, width=2)
    sd.text((85, 85), stamp_text, fill=red, font=_profile_font(52), anchor="mm")
    stamp = stamp.rotate(-14, resample=Image.Resampling.BICUBIC)
    paper.alpha_composite(stamp, (photo_x - 20, top + avatar.height + 12 - 50))

    # [5] 하단 발급 문구
    footer = f"발급처  패밀리 GM 통합 정보국   ·   발급일  {datetime.now():%Y-%m-%d}"
    draw.text((cx, y2 - 6), footer, fill=PROFILE_MUTED, font=_profile_font(22), anchor="mb")

    buf = io.BytesIO()
    paper.convert("RGB").save(buf, format="PNG")
    buf.seek(0)
    return buf

def make_achievement_certificate(recipient: str, ach: dict, achieved_at: int, user_id: int) -> io.BytesIO:
    """업적 증명서 이미지 (종이 배경 + 붉은 도장). /프로필 과 같은 배경·폰트를 씁니다."""
    S = PROFILE_SCALE
    path = next(_asset_paths(PROFILE_BG_NAMES), None)
    if path:
        paper = Image.open(path).convert("RGBA")
        paper = paper.resize((paper.width * S, paper.height * S), Image.Resampling.LANCZOS)
    else:
        paper = Image.new("RGBA", (543 * S, 394 * S), (234, 228, 205, 255))

    draw = ImageDraw.Draw(paper)
    x1, y1, x2, y2 = (v * S for v in PROFILE_AREA)
    cx = (x1 + x2) // 2

    # [1] 제목 + 이중선 + 번호
    draw.text((cx, y1 + 8), "업 적 증 명 서", fill=PROFILE_INK, font=_profile_font(54), anchor="mt")
    draw.line((x1 + 30, y1 + 88, x2 - 30, y1 + 88), fill=PROFILE_INK, width=3)
    draw.line((x1 + 30, y1 + 96, x2 - 30, y1 + 96), fill=PROFILE_INK, width=1)
    draw.text((x2 - 30, y1 + 106), f"No. {str(user_id)[-6:]}-{ACHIEVEMENTS.index(ach) + 1:02d}",
              fill=PROFILE_MUTED, font=_profile_font(22), anchor="ra")

    # [2] 수여 문구
    name = _fit_text(draw, _clean_text(recipient) or "무명", _profile_font(40), 640)
    draw.text((cx, y1 + 140), f"{name} 님", fill=PROFILE_INK, font=_profile_font(40), anchor="mt")
    draw.text((cx, y1 + 196), "위 사람은 아래의 업적을 달성하였기에 이 증서를 수여합니다.",
              fill=PROFILE_MUTED, font=_profile_font(24), anchor="mt")

    # [3] 업적 이름 + 조건
    draw.text((cx, y1 + 250), f"─  {ach['tier']} 업적  ─", fill=PROFILE_MUTED, font=_profile_font(28), anchor="mt")
    title = _fit_text(draw, _clean_text(ach["name"]), _profile_font(58), 700)
    draw.text((cx, y1 + 296), title, fill=PROFILE_INK, font=_profile_font(58), anchor="mt")
    cond = _fit_text(draw, _clean_text(ach["desc"]), _profile_font(28), 540)
    draw.text((cx, y1 + 372), cond, fill=PROFILE_MUTED, font=_profile_font(28), anchor="mt")

    # [4] 붉은 도장 (오른쪽 아래)
    stamp = Image.new("RGBA", (160, 160), (0, 0, 0, 0))
    sd = ImageDraw.Draw(stamp)
    red = (170, 40, 40, 215)
    sd.ellipse((5, 5, 155, 155), outline=red, width=6)
    sd.ellipse((18, 18, 142, 142), outline=red, width=2)
    sd.text((80, 68), "달성", fill=red, font=_profile_font(48), anchor="mm")
    sd.text((80, 112), ach["tier"], fill=red, font=_profile_font(26), anchor="mm")
    stamp = stamp.resize((128, 128), Image.Resampling.LANCZOS).rotate(-12, resample=Image.Resampling.BICUBIC)
    paper.alpha_composite(stamp, (x2 - 30 - 132, y2 - 190))   # 이름 글자를 가리지 않도록 조금 작게, 아래쪽에

    # [5] 하단 문구
    ach_day = datetime.fromtimestamp(achieved_at) if achieved_at else datetime.now()
    footer = f"달성일  {ach_day:%Y-%m-%d}   ·   발급일  {datetime.now():%Y-%m-%d}   ·   발급처  패밀리 GM 통합 정보국"
    draw.text((cx, y2 - 6), footer, fill=PROFILE_MUTED, font=_profile_font(20), anchor="mb")

    buf = io.BytesIO()
    paper.convert("RGB").save(buf, format="PNG")
    buf.seek(0)
    return buf

@bot.tree.command(name="프로필", description="[공용] 본인 또는 다른 유저의 아날로그 증명서를 이미지로 발급합니다.")
@app_commands.describe(member="조회할 유저 (비우면 본인)")
async def profile_image_command(interaction: discord.Interaction, member: discord.Member = None):
    from .police_unit import luxury_summary  # 순환 import 방지: 호출 시점에 가져옴
    if not await check_maintenance(interaction): return
    await interaction.response.defer()   # 이미지 생성 시간이 걸릴 수 있어 먼저 응답 예약

    target = member or interaction.user
    data = get_user_data(target.id)

    role_names = {r.name for r in target.roles}
    kind = "police" if "경찰" in role_names else "mafia" if "조직원" in role_names else "citizen"
    honor = data.get("honor", 0)

    pblob = get_title_blob(data)
    corrupt = (kind == "police" and pblob["corrupt"])
    rank_text = "시민" if kind == "citizen" else _clean_text(get_user_rank(honor, is_police=(kind == "police")))
    if corrupt:
        rank_text = f"부패 경찰 · {rank_text}"
    rows = [
        ("성 명", _clean_text(target.display_name) or _clean_text(target.name) or "무명"),
        ("직 급", rank_text),
        ("칭 호", _clean_text(get_equipped_title_name(data))),
        ("거 점", _clean_text(data.get("location", "레벤하임"))),
        ("자 금", f"${data.get('money', 0):,}"),
        ("명 예", f"{honor:,} pt"),
    ]
    if kind == "citizen":
        rows.insert(2, ("성 향", alignment_text(pblob["karma"])))   # 시민일 때만 성향 표시
    _lux_n, _lux_total = luxury_summary(data.get("inventory") or {})
    if _lux_n:
        rows.append(("사치품", f"{_lux_n}점 · ${_lux_total:,}"))   # 백화점에서 산 사치품 (과시용)

    try:
        avatar = await _load_avatar(target)
        buf = await asyncio.to_thread(make_profile_image, kind, rows, target.id, avatar, "매수" if corrupt else "승인")
    except Exception as e:
        print(f"Profile image error: {e}")
        await interaction.followup.send("증명서를 발급하는 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.", ephemeral=True)
        return

    await interaction.followup.send(file=discord.File(buf, filename=f"profile_{target.id}.png"))

@bot.tree.command(name="인벤토리", description="[마피아 GM] 소지한 장비를 확인합니다.")
async def show_inventory(interaction: discord.Interaction):
    from .police_unit import LUXURY_BY_NAME, luxury_owned, luxury_summary  # 순환 import 방지: 호출 시점에 가져옴
    if not await check_maintenance(interaction): return
    if not await check_membership(interaction): return
    if await check_jail(interaction): return

    user_stats = get_user_data(interaction.user.id)
    inventory = user_stats.get("inventory", {})

    embed = discord.Embed(title=f"[{interaction.user.name} 님의 장비 가방]", color=discord.Color.dark_green())
    gear_keys = [k for k in inventory.keys() if not k.startswith(MATERIAL_PREFIX) and k not in LUXURY_BY_NAME]
    inv_list = [f"• **{item}** `[보유 중]`" for item in gear_keys] if gear_keys else ["*보유 중인 장비가 없습니다.*"]
    embed.add_field(name="보유 중인 전용 장비", value="\n".join(inv_list), inline=False)
    mat_lines = [f"• {k[len(MATERIAL_PREFIX):]} x{v}" for k, v in inventory.items() if k.startswith(MATERIAL_PREFIX) and v > 0]
    if mat_lines:
        embed.add_field(name="제작 재료 (`/제작`)", value="\n".join(mat_lines), inline=False)
    _lux = luxury_owned(inventory)
    if _lux:
        _lux_n, _lux_total = luxury_summary(inventory)
        embed.add_field(name=f"사치품 ({_lux_n}점 · 정가 합계 ${_lux_total:,})",
                        value="\n".join(f"• **{it['name']}**" for it in _lux)[:1000], inline=False)
    
    if interaction.user.avatar:
        embed.set_thumbnail(url=interaction.user.avatar.url)

    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="무기상점", description="[조직원 전용] 특수 무기(?)-장비를 판매하는 상점을 엽니다.")
async def open_weapon_shop(interaction: discord.Interaction):
    if not await check_maintenance(interaction): return
    if not await check_membership(interaction): return
    if await check_jail(interaction): return

    await interaction.response.defer()

    user = interaction.user
    user_stats = get_user_data(user.id)
    current_money = user_stats.get("money", 0)

    items_desc = "\n".join([
        f"• **{item['name']}** - `${item['price']:,}`\n  └ *{item['desc']}*"
        for item in WEAPON_SHOP_ITEMS
    ])

    embed = discord.Embed(
        title="[지하 무기 상점] 비밀 카탈로그",
        description=f"**{user.mention}** 님, 환영합니다. 결투에 필요한 특수 장비들을 구매할 수 있습니다.\n\n"
                    f"{items_desc}\n\n"
                    f"**현재 보유 자금:** `${current_money:,}`",
        color=discord.Color.dark_red()
    )
    if user.avatar:
        embed.set_thumbnail(url=user.avatar.url)

    await interaction.followup.send(embed=embed, view=WeaponShopView(owner=user))

    # === 경찰 전용 상점 아이템 목록 ===
POLICE_SHOP_ITEMS = [
    {"id": "police_shield", "name": "치안국 강화 방탄복", "price": 4000, "desc": "경찰의 방어력을 대폭 강화합니다 (방어력 +35)", "def_bonus": 35},
    {"id": "police_radio", "name": "기동타격대 무전기", "price": 8000, "desc": "/검거 포상금 +20% · /작전 성공률 +5% (보유만 해도 자동 적용)", "bonus_type": "radio"},
    {"id": "police_badge", "name": "특급 수사관 황금 배지", "price": 20000, "desc": "방어력 +50 / 검거·순찰·심문·작전 명예 실적 +20%", "def_bonus": 50},
    # --- 고급 장비 (비싼 만큼 강력합니다. 공격력·방어력·체력은 모든 전투 콘텐츠에 반영되고, 서로 겹쳐서 적용됩니다) ---
    {"id": "police_helmet", "name": "전술 방탄 헬멧", "price": 30000, "desc": "방어력 +25 / 체력 +1,500", "def_bonus": 25, "hp_bonus": 1500},
    {"id": "police_rifle", "name": "치안국 제식 돌격소총", "price": 60000, "desc": "공격력 +200", "atk_bonus": 200},
    {"id": "police_swat", "name": "특수작전 중장갑 전투복", "price": 90000, "desc": "방어력 +90 / 체력 +3,000", "def_bonus": 90, "hp_bonus": 3000},
    {"id": "police_terminal", "name": "광역 수사망 단말기", "price": 120000, "desc": "구역 탈환 포상금 +25% / 탈환 비용 50% 할인", "bonus_type": "terminal"},
    {"id": "police_sniper", "name": "대테러 저격 소총", "price": 150000, "desc": "공격력 +350", "atk_bonus": 350},
    {"id": "police_insignia", "name": "치안총감 지휘 휘장", "price": 250000, "desc": "방어력 +60 / 체력 +2,000 / 명예 실적 +15%", "def_bonus": 60, "hp_bonus": 2000, "bonus_type": "insignia"},
]


# ==========================================================================
# [합친 모듈] auction.py
#   /경매
# ==========================================================================
# ==========================================================================
# [/경매]  플레이어 경매장
#   • /경매 아이템 [수량] [시작가]  : 인벤토리의 아이템·장비를 경매에 올립니다. (시작가 최소 $1,000)
#   • 경찰·조직원 누구나 버튼으로 입찰할 수 있습니다. 입찰 금액은 입찰 즉시 빠져나가고,
#     다른 사람이 더 높게 부르면 즉시 전액 환불됩니다.
#   • 마지막 입찰 후 AUCTION_BID_SEC(10초) 동안 아무도 레이즈하지 않으면 낙찰 → 낙찰자가 아이템을 받고, 판매자가 금액을 받습니다.
#     (입찰 상한은 없습니다. 자기 자신의 입찰을 다시 올리는 것은 막아 두었습니다.)
#   • 입찰이 없으면 유찰되어 아이템이 판매자에게 돌아옵니다. 판매자는 입찰이 들어오기 전까지 취소할 수 있습니다.
#
#   [안전장치]  아이템·입찰금은 user_data.data 안에 "에스크로 기록"(auction_item / auction_escrow)으로 남습니다.
#               봇이 경매 도중 꺼지거나 재시작돼도, 켜질 때 on_ready 에서 자동으로 아이템 반환·입찰금 환불을 합니다.
#               (봇을 동시에 두 개 이상 띄우면 안 됩니다. 한쪽이 켜질 때 다른 쪽의 진행 중 경매를 되돌려 버립니다.)
# ==========================================================================
AUCTION_START_MIN = 1000          # 시작가 최소 금액
AUCTION_PRICE_MAX = 2_000_000_000 # 입력 안전 한도 (DB 정수 범위 보호용. 실질적으론 상한 없음)
AUCTION_MIN_RAISE = 100           # 최소 레이즈 폭
AUCTION_OPEN_SEC = 60             # 첫 입찰이 들어오기까지 기다리는 시간 (없으면 유찰)
AUCTION_BID_SEC = 10              # 마지막 입찰 후 낙찰까지 시간
AUCTION_RAISE_STEPS = (1000, 10000, 100000)   # [+금액] 버튼
AUCTION_MAX_PER_GUILD = 3         # 서버당 동시 진행 경매 수
AUCTION_FEE_RATE = 0.0            # 판매 수수료 (0.05 = 5%). 기본은 수수료 없음

active_auctions = {}              # {경매id: AuctionSession}
auction_by_seller = {}            # {판매자 user_id: 경매id}  (1인 1경매)
_auction_tasks = set()            # 진행 중인 asyncio 작업 (가비지 컬렉션 방지)

def auction_blocked_reason(item: str):
    """거래 불가 아이템이면 사유를, 가능하면 None"""
    from .police_unit import LUXURY_BY_NAME  # 순환 import 방지: 호출 시점에 가져옴
    if item.startswith(("[전리품]", "[강화보스 전리품]")):
        return "고유 전리품은 칭호·업적과 연결되어 있어 거래할 수 없습니다."
    if item in {it["name"] for it in POLICE_SHOP_ITEMS}:
        return "치안국 보급품은 거래할 수 없습니다."
    if item in LUXURY_BY_NAME:
        return "사치품은 개인 소유물이라 거래·경매·압류할 수 없습니다."
    return None

# ── DB 작업 (동기, to_thread 로 실행) ────────────────────────────────────
def _auction_retry(fn, *args, tries: int = 3):
    last = None
    for i in range(tries):
        try:
            return fn(*args)
        except Exception as e:
            last = e
            time.sleep(0.4 * (i + 1))
    raise last

def _auction_update(user_id, payload: dict):
    def _do():
        res = supabase.table("user_data").update(payload).eq("user_id", str(user_id)).execute()
        _check_updated(res, "auction_update")
    _auction_retry(_do)

def auction_list_sync(aid: str, seller_id: int, item: str, qty: int) -> str:
    """판매 아이템을 인벤토리에서 빼서 에스크로 기록으로 옮김. 반환: ok / no_item"""
    _require_db()
    with _inv_lock:
        info = get_user_data(seller_id)
        inv = dict(info.get("inventory") or {})
        have = int(inv.get(item, 0) or 0)
        if have < qty:
            return "no_item"
        if have == qty:
            inv.pop(item, None)
        else:
            inv[item] = have - qty
        blob = get_title_blob(info)
        blob.setdefault("auction_item", {})[aid] = {"item": item, "qty": qty}
        _auction_update(seller_id, {"inventory": inv, "data": blob})
    return "ok"

def auction_return_item_sync(aid: str, seller_id: int) -> bool:
    """에스크로 기록의 아이템을 판매자에게 돌려줌 (유찰·취소)"""
    _require_db()
    with _inv_lock:
        info = get_user_data(seller_id)
        blob = get_title_blob(info)
        rec = (blob.get("auction_item") or {}).pop(aid, None)
        if not rec:
            return False
        inv = dict(info.get("inventory") or {})
        inv[rec["item"]] = inv.get(rec["item"], 0) + int(rec["qty"])
        _auction_update(seller_id, {"inventory": inv, "data": blob})
    return True

def auction_bid_sync(aid: str, bidder_id: int, amount: int, prev_id, prev_amount: int):
    """새 입찰자의 돈을 먼저 차감(에스크로)하고, 이전 최고 입찰자에게 전액 환불. 반환: (상태, 값)"""
    _require_db()
    with _inv_lock:
        info = get_user_data(bidder_id)
        money = int(info.get("money", 0) or 0)
        if money < amount:
            return "no_money", money
        blob = get_title_blob(info)
        blob.setdefault("auction_escrow", {})[aid] = amount
        _auction_update(bidder_id, {"money": money - amount, "data": blob})
        if prev_id:
            try:
                p_info = get_user_data(prev_id)
                p_blob = get_title_blob(p_info)
                (p_blob.get("auction_escrow") or {}).pop(aid, None)
                _auction_update(prev_id, {"money": int(p_info.get("money", 0) or 0) + prev_amount, "data": p_blob})
            except Exception as e:
                # 환불이 실패해도 이전 입찰자의 에스크로 기록이 남아 있어 다음 봇 시작 때 자동 환불됩니다.
                print(f"[경매 환불 실패 → 재시작 시 복구] aid={aid} user={prev_id}: {e}")
        return "ok", money - amount

def auction_settle_sync(s: "AuctionSession") -> int:
    """낙찰 정산: 낙찰자에게 아이템, 판매자에게 대금. 반환: 판매자가 받은 금액"""
    _require_db()
    fee = int(s.current * AUCTION_FEE_RATE)
    payout = s.current - fee
    with _inv_lock:
        w_info = get_user_data(s.high_id)
        w_blob = get_title_blob(w_info)
        (w_blob.get("auction_escrow") or {}).pop(s.id, None)
        w_inv = dict(w_info.get("inventory") or {})
        w_inv[s.item] = w_inv.get(s.item, 0) + s.qty
        _auction_update(s.high_id, {"inventory": w_inv, "data": w_blob})

        s_info = get_user_data(s.seller_id)
        s_blob = get_title_blob(s_info)
        (s_blob.get("auction_item") or {}).pop(s.id, None)
        _auction_update(s.seller_id, {"money": int(s_info.get("money", 0) or 0) + payout, "data": s_blob})
    return payout

def auction_recover_sync():
    """봇 시작 시: 이전 실행에서 끝나지 못한 경매의 에스크로(아이템·입찰금)를 모두 되돌림. 반환: (아이템 건수, 환불 인원)"""
    if not supabase:
        return 0, 0
    n_items = n_refunds = 0
    start = 0
    while True:
        rows = supabase.table("user_data").select("user_id,data").range(start, start + 999).execute().data or []
        for row in rows:
            raw = row.get("data")
            if not isinstance(raw, dict) or not (raw.get("auction_escrow") or raw.get("auction_item")):
                continue
            with _inv_lock:
                info = get_user_data(row["user_id"])
                blob = get_title_blob(info)
                esc = blob.pop("auction_escrow", None) or {}
                itm = blob.pop("auction_item", None) or {}
                payload = {"data": blob}
                if esc:
                    payload["money"] = int(info.get("money", 0) or 0) + sum(int(v) for v in esc.values())
                    n_refunds += 1
                if itm:
                    inv = dict(info.get("inventory") or {})
                    for rec in itm.values():
                        inv[rec["item"]] = inv.get(rec["item"], 0) + int(rec["qty"])
                        n_items += 1
                    payload["inventory"] = inv
                _auction_update(row["user_id"], payload)
        if len(rows) < 1000:
            break
        start += 1000
    return n_items, n_refunds

_auction_recover_state = {"done": False}

@bot.listen("on_ready")
async def _auction_recover_on_ready():
    if _auction_recover_state["done"]:       # 재연결로 on_ready 가 또 불려도 진행 중인 경매를 건드리지 않도록 한 번만
        return
    _auction_recover_state["done"] = True
    try:
        n_items, n_refunds = await asyncio.to_thread(auction_recover_sync)
        print(f"경매 에스크로 복구: 아이템 {n_items}건 반환 / 입찰금 {n_refunds}명 환불")
    except Exception as e:
        print(f"경매 에스크로 복구 실패: {e}")

# ── 경매 진행 상태 ────────────────────────────────────────────────────────
class AuctionSession:
    def __init__(self, aid: str, guild_id: int, seller, item: str, qty: int, start_price: int):
        self.id, self.guild_id = aid, guild_id
        self.seller_id, self.seller_name = seller.id, seller.display_name
        self.item, self.qty, self.start_price = item, qty, start_price
        self.current = start_price
        self.high_id = None
        self.high_name = ""
        self.bids = []                          # [(이름, 금액)]
        self.deadline = time.monotonic() + AUCTION_OPEN_SEC
        self.deadline_epoch = int(time.time()) + AUCTION_OPEN_SEC
        self.ended = False
        self.cancelled = False
        self.result = None                      # sold / unsold / cancelled / error
        self.payout = 0
        self.lock = asyncio.Lock()              # 입찰·종료 판정을 한 줄로 세우는 잠금
        self.edit_lock = asyncio.Lock()         # 메시지 갱신 순서 보장
        self.channel = None
        self.message = None                     # PartialMessage (채널 권한으로 수정)
        self.message_hook = None                # 슬래시 명령 응답 메시지 (예비 수단)
        self.view = None

def _auction_item_text(s: AuctionSession) -> str:
    return f"{s.item} x{s.qty}" if s.qty > 1 else s.item

def auction_min_bid(s: AuctionSession) -> int:
    return s.current if s.high_id is None else s.current + AUCTION_MIN_RAISE

def auction_embed(s: AuctionSession) -> discord.Embed:
    item_txt = _auction_item_text(s)
    if not s.ended:
        embed = discord.Embed(
            title=f"[경매] {item_txt}",
            color=discord.Color.gold() if s.high_id else discord.Color.blurple()
        )
        embed.add_field(name="판매자", value=f"<@{s.seller_id}>", inline=True)
        embed.add_field(name="현재 최고가" if s.high_id else "시작가", value=f"`${s.current:,}`", inline=True)
        embed.add_field(name="최고 입찰자", value=f"<@{s.high_id}>" if s.high_id else "없음", inline=True)
        if s.high_id:
            timing = f"<t:{s.deadline_epoch}:R> 까지 아무도 레이즈하지 않으면 **낙찰**됩니다."
        else:
            timing = f"<t:{s.deadline_epoch}:R> 까지 입찰이 없으면 **유찰**됩니다."
        embed.add_field(name="진행", value=timing + f"\n입찰이 들어올 때마다 **{AUCTION_BID_SEC}초**로 다시 시작합니다. (입찰 상한 없음)", inline=False)
        embed.set_footer(text="입찰금은 즉시 차감되고, 더 높은 입찰이 들어오면 전액 환불됩니다.")
    else:
        if s.result == "sold":
            embed = discord.Embed(title=f"[경매 낙찰] {item_txt}", color=discord.Color.green(),
                                  description=f"<@{s.high_id}> 님이 `${s.current:,}` 에 낙찰받았습니다!\n"
                                              f"판매자 <@{s.seller_id}> 님은 `${s.payout:,}` 를 받았습니다.")
        elif s.result == "cancelled":
            embed = discord.Embed(title=f"[경매 취소] {item_txt}", color=discord.Color.dark_grey(),
                                  description="판매자가 경매를 취소했습니다. 아이템은 판매자에게 돌아갔습니다.")
        elif s.result == "error":
            embed = discord.Embed(title=f"[경매 오류] {item_txt}", color=discord.Color.red(),
                                  description="정산 중 오류가 발생했습니다. 아이템과 입찰금은 봇이 다시 시작될 때 자동으로 되돌려집니다.\n"
                                              "급하면 관리자에게 문의해 주세요.")
        else:
            embed = discord.Embed(title=f"[경매 유찰] {item_txt}", color=discord.Color.dark_grey(),
                                  description="입찰이 없어 유찰되었습니다. 아이템은 판매자에게 돌아갔습니다.")
    if s.bids:
        embed.add_field(name="최근 입찰", value="\n".join(f"• {n} `${a:,}`" for n, a in s.bids[-5:][::-1]), inline=False)
    return embed

async def auction_refresh(s: AuctionSession):
    """현재 상태 그대로 경매 메시지를 갱신 (항상 최신 상태로 그려지도록 잠금 안에서 만든다)"""
    async with s.edit_lock:
        if s.view:
            s.view.sync_labels()
        kwargs = {"embed": auction_embed(s), "view": None if s.ended else s.view}
        last = None
        for target in (s.message, s.message_hook):
            if target is None:
                continue
            try:
                await target.edit(**kwargs)
                return
            except (discord.Forbidden, discord.NotFound, discord.HTTPException) as e:
                last = e
        print(f"[경매 메시지 갱신 실패] aid={s.id}: {last}")

async def auction_place_bid(s: AuctionSession, user: discord.abc.User, amount: int):
    """입찰 처리. 실패하면 사유 문자열, 성공하면 None"""
    if user.id == s.seller_id:
        return "자신이 올린 경매에는 입찰할 수 없습니다."
    if user.id == s.high_id:
        return "이미 최고 입찰자입니다. 다른 사람이 더 높게 부르면 다시 도전하세요."
    if amount > AUCTION_PRICE_MAX:
        return f"입찰 금액이 너무 큽니다. (최대 `${AUCTION_PRICE_MAX:,}`)"

    async with s.lock:
        if s.ended or time.monotonic() >= s.deadline:
            return "이미 종료된 경매입니다."
        need = auction_min_bid(s)
        if amount < need:
            return f"현재 최소 입찰액은 `${need:,}` 입니다."
        prev_id, prev_amount = s.high_id, (s.current if s.high_id else 0)
        try:
            status, val = await asyncio.to_thread(auction_bid_sync, s.id, user.id, amount, prev_id, prev_amount)
        except Exception as e:
            print(f"[경매 입찰 오류] aid={s.id}: {e}")
            return "입찰을 처리하는 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."
        if status == "no_money":
            return f"자금이 부족합니다. (보유 `${val:,}` / 입찰 `${amount:,}`)"
        s.current, s.high_id, s.high_name = amount, user.id, user.display_name
        s.bids.append((user.display_name, amount))
        s.deadline = time.monotonic() + AUCTION_BID_SEC          # 레이즈가 들어올 때마다 10초 다시 시작
        s.deadline_epoch = int(time.time()) + AUCTION_BID_SEC
    await auction_refresh(s)
    return None

async def auction_precheck(interaction: discord.Interaction) -> bool:
    """점검 / 가입(조직원·경찰) / 수감 여부. 실패하면 직접 안내를 보내므로 호출 전에 응답하지 않은 상태여야 한다."""
    if not await check_maintenance(interaction): return False
    if not await check_membership(interaction): return False
    if await check_jail(interaction): return False
    return True

# ── 버튼 / 입력창 ────────────────────────────────────────────────────────
class _AuctionBidButton(discord.ui.Button):
    def __init__(self, s: AuctionSession, **kw):
        super().__init__(**kw)
        self.s = s

    def amount(self) -> int:
        raise NotImplementedError

    async def callback(self, interaction: discord.Interaction):
        s = self.s
        if s.ended:
            await interaction.response.send_message("이미 종료된 경매입니다.", ephemeral=True)
            return
        if not await auction_precheck(interaction): return
        await interaction.response.defer()
        msg = await auction_place_bid(s, interaction.user, self.amount())
        if msg:
            await interaction.followup.send(msg, ephemeral=True)

class AuctionMinButton(_AuctionBidButton):
    def __init__(self, s):
        super().__init__(s, label="입찰", style=discord.ButtonStyle.success, row=0)
    def amount(self): return auction_min_bid(self.s)

class AuctionRaiseButton(_AuctionBidButton):
    def __init__(self, s, step: int):
        super().__init__(s, label=f"+${step:,}", style=discord.ButtonStyle.primary, row=0)
        self.step = step
    def amount(self): return self.s.current + self.step

class AuctionBidModal(discord.ui.Modal, title="입찰 금액 입력"):
    amount_input = discord.ui.TextInput(label="입찰 금액 ($)", placeholder="예: 25000", min_length=1, max_length=14)

    def __init__(self, s: AuctionSession):
        super().__init__()
        self.s = s

    async def on_submit(self, interaction: discord.Interaction):
        raw = re.sub(r"[,\s$]", "", str(self.amount_input.value))
        if not raw.isdigit():
            await interaction.response.send_message("숫자만 입력해 주세요.", ephemeral=True)
            return
        amount = int(raw)
        await interaction.response.defer(ephemeral=True, thinking=True)
        msg = await auction_place_bid(self.s, interaction.user, amount)
        await interaction.followup.send(msg or f"`${amount:,}` 입찰 완료!", ephemeral=True)

class AuctionCustomButton(discord.ui.Button):
    def __init__(self, s):
        super().__init__(label="직접 입력", style=discord.ButtonStyle.secondary, row=0)
        self.s = s

    async def callback(self, interaction: discord.Interaction):
        if self.s.ended:
            await interaction.response.send_message("이미 종료된 경매입니다.", ephemeral=True)
            return
        if not await auction_precheck(interaction): return
        await interaction.response.send_modal(AuctionBidModal(self.s))

class AuctionCancelButton(discord.ui.Button):
    def __init__(self, s):
        super().__init__(label="경매 취소 (판매자)", style=discord.ButtonStyle.danger, row=1)
        self.s = s

    async def callback(self, interaction: discord.Interaction):
        s = self.s
        if interaction.user.id != s.seller_id:
            await interaction.response.send_message("판매자만 취소할 수 있습니다.", ephemeral=True)
            return
        async with s.lock:
            if s.ended:
                err = "이미 종료된 경매입니다."
            elif s.high_id:
                err = "이미 입찰이 들어와 취소할 수 없습니다."
            else:
                err = None
                s.cancelled = True
                s.deadline = 0          # 진행 루프가 바로 종료 처리
        if err:
            await interaction.response.send_message(err, ephemeral=True)
        else:
            await interaction.response.defer()

class AuctionView(discord.ui.View):
    def __init__(self, s: AuctionSession):
        super().__init__(timeout=None)
        self.s = s
        self.min_btn = AuctionMinButton(s)
        self.cancel_btn = AuctionCancelButton(s)
        self.add_item(self.min_btn)
        for step in AUCTION_RAISE_STEPS:
            self.add_item(AuctionRaiseButton(s, step))
        self.add_item(AuctionCustomButton(s))
        self.add_item(self.cancel_btn)
        self.sync_labels()

    def sync_labels(self):
        need = auction_min_bid(self.s)
        self.min_btn.label = f"시작가 ${need:,} 입찰" if self.s.high_id is None else f"최소 ${need:,} 입찰"
        self.cancel_btn.disabled = bool(self.s.high_id)      # 입찰이 들어오면 취소 불가

# ── 진행 / 종료 ──────────────────────────────────────────────────────────
async def auction_finish(s: AuctionSession):
    if s.cancelled or s.high_id is None:
        s.result = "cancelled" if s.cancelled else "unsold"
        try:
            await asyncio.to_thread(auction_return_item_sync, s.id, s.seller_id)
        except Exception as e:
            print(f"[경매 아이템 반환 실패 → 재시작 시 복구] aid={s.id}: {e}")
            s.result = "error"
    else:
        try:
            s.payout = await asyncio.to_thread(auction_settle_sync, s)
            s.result = "sold"
        except Exception as e:
            print(f"[경매 정산 실패 → 재시작 시 복구] aid={s.id}: {e}")
            s.result = "error"
    await auction_refresh(s)

    if s.result == "sold" and s.channel is not None:       # 낙찰자·판매자에게 알림
        try:
            await s.channel.send(f"<@{s.high_id}> 님이 **{_auction_item_text(s)}** 을(를) `${s.current:,}` 에 낙찰받았습니다! (판매자 <@{s.seller_id}>)")
        except discord.HTTPException:
            pass

async def run_auction(s: AuctionSession):
    try:
        while True:
            async with s.lock:
                remaining = s.deadline - time.monotonic()
                if remaining <= 0:
                    s.ended = True          # 종료 판정은 잠금 안에서 → 이 순간부터 새 입찰은 거절됨
                    break
            await asyncio.sleep(min(max(remaining, 0.05), 1.0))
        await auction_finish(s)
    except Exception as e:
        print(f"[경매 진행 오류] aid={s.id}: {type(e).__name__}: {e}")
        s.ended = True
        s.result = "error"
        try:
            await auction_refresh(s)
        except Exception:
            pass
    finally:
        active_auctions.pop(s.id, None)
        if auction_by_seller.get(s.seller_id) == s.id:
            auction_by_seller.pop(s.seller_id, None)

# ── 슬래시 명령어 ────────────────────────────────────────────────────────
async def _auction_item_autocomplete(interaction: discord.Interaction, current: str):
    try:
        info = await asyncio.to_thread(get_user_data, interaction.user.id)
    except Exception:
        return []
    inv = info.get("inventory") or {}
    cur = current.strip().lower()
    out = []
    for name, cnt in inv.items():
        if int(cnt or 0) <= 0 or auction_blocked_reason(name):
            continue
        if cur and cur not in name.lower():
            continue
        label = f"{name} (x{cnt})" if int(cnt) > 1 else name
        out.append(app_commands.Choice(name=label[:100], value=name[:100]))
        if len(out) >= 25:
            break
    return out

@bot.tree.command(name="경매", description="[공용] 인벤토리의 아이템·장비를 경매에 올립니다. 누구나 입찰할 수 있어요.")
@app_commands.describe(아이템="경매에 올릴 아이템 (목록에서 선택)",
                       수량="올릴 개수 (재료·소모품만 여러 개 가능, 기본 1)",
                       시작가=f"시작 금액 (최소 ${AUCTION_START_MIN:,}, 기본 ${AUCTION_START_MIN:,})")
@app_commands.autocomplete(아이템=_auction_item_autocomplete)
async def auction_command(interaction: discord.Interaction, 아이템: str,
                          수량: app_commands.Range[int, 1, 9999] = 1,
                          시작가: app_commands.Range[int, AUCTION_START_MIN, AUCTION_PRICE_MAX] = AUCTION_START_MIN):
    if not await auction_precheck(interaction): return

    user = interaction.user
    now_dt = datetime.now()
    if any(int(k) == int(user.id) and now_dt <= exp for k, exp in list(wanted_list.items())):
        # 수배 중에 경매로 아이템을 빼돌려 /검거 압류를 피하는 것을 막습니다. (입찰은 가능)
        await interaction.response.send_message("**지명수배 중에는 경매에 물건을 올릴 수 없습니다.** 수배가 풀린 뒤에 다시 시도해 주세요.", ephemeral=True)
        return
    reason = auction_blocked_reason(아이템)
    if reason:
        await interaction.response.send_message(reason, ephemeral=True)
        return
    if auction_by_seller.get(user.id) in active_auctions:
        await interaction.response.send_message("이미 진행 중인 내 경매가 있습니다. 끝난 뒤에 다시 올려주세요.", ephemeral=True)
        return
    gid = interaction.guild_id
    if sum(1 for a in active_auctions.values() if a.guild_id == gid) >= AUCTION_MAX_PER_GUILD:
        await interaction.response.send_message(f"이 서버에서는 동시에 {AUCTION_MAX_PER_GUILD}개까지만 경매를 열 수 있습니다. 잠시 후 다시 시도해 주세요.", ephemeral=True)
        return

    await interaction.response.defer()
    import uuid
    aid = uuid.uuid4().hex[:8]
    try:
        status = await asyncio.to_thread(auction_list_sync, aid, user.id, 아이템, 수량)
    except Exception as e:
        print(f"[경매 등록 오류] {e}")
        await interaction.followup.send("경매를 등록하는 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.", ephemeral=True)
        return
    if status == "no_item":
        await interaction.followup.send("해당 아이템을 그만큼 보유하고 있지 않습니다.", ephemeral=True)
        return

    s = AuctionSession(aid, gid, user, 아이템, 수량, 시작가)
    s.channel = interaction.channel
    s.view = AuctionView(s)
    active_auctions[aid] = s
    auction_by_seller[user.id] = aid
    try:
        msg = await interaction.followup.send(embed=auction_embed(s), view=s.view, wait=True)
    except Exception as e:                       # 메시지를 못 보냈으면 경매를 열지 않고 아이템 반환
        print(f"[경매 메시지 전송 실패] {e}")
        active_auctions.pop(aid, None)
        auction_by_seller.pop(user.id, None)
        await asyncio.to_thread(auction_return_item_sync, aid, user.id)
        return
    s.message_hook = msg
    if interaction.channel is not None and hasattr(interaction.channel, "get_partial_message"):
        s.message = interaction.channel.get_partial_message(msg.id)

    task = asyncio.create_task(run_auction(s))
    _auction_tasks.add(task)
    task.add_done_callback(_auction_tasks.discard)
