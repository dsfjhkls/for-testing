"""/프로필 (이미지 증명서)"""

import asyncio
import discord
import io
import os
import unicodedata
from PIL import Image, ImageDraw, ImageFont, ImageOps
from discord import app_commands
from datetime import datetime
from functools import lru_cache

from .core import bot
from .status import WEAPON_SHOP_ITEMS, check_jail, get_user_data, get_user_rank
from .alignment import alignment_text, get_equipped_title_name, get_title_blob
from .achievements import ACHIEVEMENTS, MATERIAL_PREFIX
from .admin import check_maintenance, check_membership
from .duel import WeaponShopView

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
    from .dept_store import luxury_summary   # 순환 import 방지: 호출 시점에 가져옴
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
    from .dept_store import LUXURY_BY_NAME, luxury_owned, luxury_summary   # 순환 import 방지: 호출 시점에 가져옴
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
