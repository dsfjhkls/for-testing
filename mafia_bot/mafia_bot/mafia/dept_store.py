"""/백화점"""

import asyncio
import discord

from .core import bot, supabase
from .status import _check_updated, _require_db, check_jail, get_user_data
from .admin import check_maintenance
from .crafting import _inv_lock
from .confirm_upgrade import _PurchaseConfirmView

# ==========================================================================
# [백화점 · 사치품]   /백화점
#   • 돈을 아낌없이 쓰는 과시용 상품입니다. 전투 능력치는 올려주지 않고, /프로필 · /인벤토리 에 보유 현황이 표시됩니다.
#   • 사치품은 한 종류당 1개만 살 수 있고, 거래·경매·압류가 불가능합니다. (되팔아 돈을 돌려받는 구멍을 막기 위해)
#   • 가격·상품은 아래 LUXURY_ITEMS 에서 자유롭게 바꾸세요. (id 와 name 은 서로 겹치지 않게)
#   • LUXURY_ANNOUNCE_MIN 이상 구매하면 채널에 공개 알림이 올라갑니다. (과시 효과)
# ==========================================================================
LUXURY_ANNOUNCE_MIN = 3_000_000

LUXURY_ITEMS = [
    # --- 패션 ---
    {"id": "lx_suit",     "cat": "패션",   "name": "맞춤 이탈리안 정장",     "price": 300_000,     "desc": "장인이 손수 지은 정장. 입는 순간 분위기가 달라집니다."},
    {"id": "lx_fur",      "cat": "패션",   "name": "러시안 밍크 코트",       "price": 1_200_000,   "desc": "겨울 사교계의 필수품. 한눈에 부자임이 드러납니다."},
    {"id": "lx_necklace", "cat": "패션",   "name": "블루 다이아 목걸이",     "price": 8_000_000,   "desc": "경매장에서도 보기 힘든 푸른 다이아몬드."},
    # --- 시계 ---
    {"id": "lx_watch1",   "cat": "시계",   "name": "스위스 골드 손목시계",   "price": 700_000,     "desc": "정확하고 묵직한 황금빛 시계."},
    {"id": "lx_watch2",   "cat": "시계",   "name": "다이아 베젤 시계",       "price": 2_000_000,   "desc": "시침마다 보석이 박힌 사치스러운 시계."},
    {"id": "lx_watch3",   "cat": "시계",   "name": "한정판 투르비옹 시계",   "price": 5_000_000,   "desc": "전 세계 열 개뿐인 수제 시계. 값은 부르는 대로."},
    # --- 차량 ---
    {"id": "lx_classic",  "cat": "차량",   "name": "빈티지 클래식카",        "price": 2_500_000,   "desc": "반질반질하게 복원된 옛 시절의 명차."},
    {"id": "lx_limo",     "cat": "차량",   "name": "방탄 리무진",            "price": 6_000_000,   "desc": "총알도 튕겨내는 검은 리무진. 기사 포함."},
    {"id": "lx_super",    "cat": "차량",   "name": "이탈리아 슈퍼카",        "price": 10_000_000,  "desc": "시동 소리만으로 골목이 조용해지는 슈퍼카."},
    # --- 부동산 ---
    {"id": "lx_apt",      "cat": "부동산", "name": "시내 고급 아파트",       "price": 15_000_000,  "desc": "레벤하임 중심가가 한눈에 보이는 고층 아파트."},
    {"id": "lx_villa",    "cat": "부동산", "name": "해안 별장",              "price": 30_000_000,  "desc": "에르느코항 절벽 위에 지은 프라이빗 별장."},
    # --- 최상위 ---
    {"id": "lx_yacht",    "cat": "최상위", "name": "전용 초호화 요트",       "price": 50_000_000,  "desc": "파티홀과 수영장을 갖춘 바다 위의 궁전."},
    {"id": "lx_jet",      "cat": "최상위", "name": "전용 제트기",            "price": 100_000_000, "desc": "원하는 곳 어디든, 원하는 시간에 이륙합니다."},
    {"id": "lx_island",   "cat": "최상위", "name": "개인 소유 섬",           "price": 300_000_000, "desc": "지도에 당신의 이름이 박힌 섬. 부의 끝판왕."},
]
LUXURY_BY_ID = {it["id"]: it for it in LUXURY_ITEMS}
LUXURY_BY_NAME = {it["name"]: it for it in LUXURY_ITEMS}


def luxury_owned(inv: dict) -> list:
    inv = inv or {}
    return [it for it in LUXURY_ITEMS if int(inv.get(it["name"], 0) or 0) > 0]


def luxury_summary(inv: dict):
    """(보유 개수, 보유 사치품 정가 합계)"""
    owned = luxury_owned(inv)
    return len(owned), sum(it["price"] for it in owned)


def luxury_buy_sync(user_id: int, item_id: str):
    """사치품 구매. 반환: ("ok", 남은 돈) / ("no_money", 보유 돈) / ("owned",) / ("no_item",)"""
    item = LUXURY_BY_ID.get(item_id)
    if not item:
        return ("no_item",)
    _require_db()
    with _inv_lock:   # 인벤토리를 읽고 다시 쓰는 작업이 서로 덮어쓰지 않도록
        info = get_user_data(user_id)
        money = int(info.get("money") or 0)
        inv = dict(info.get("inventory") or {})
        if int(inv.get(item["name"], 0) or 0) >= 1:
            return ("owned",)
        if money < item["price"]:
            return ("no_money", money)
        inv[item["name"]] = 1
        res = supabase.table("user_data").update({"money": money - item["price"], "inventory": inv}).eq("user_id", str(user_id)).execute()
        _check_updated(res, "luxury_buy")
        return ("ok", money - item["price"])


def department_store_embed(user: discord.Member, money: int, inv: dict) -> discord.Embed:
    n, total = luxury_summary(inv)
    embed = discord.Embed(
        title="[레벤하임 백화점] 프리미엄 갤러리아",
        description=(f"**{user.mention}** 님, 어서 오세요. 부를 증명할 최고급 상품을 준비했습니다.\n"
                     f"사치품은 능력치를 올려주진 않지만, `/프로필` 에 당신의 재력이 새겨집니다.\n\n"
                     f"**현재 보유 자금:** `${money:,}`\n"
                     f"**보유 사치품:** `{n}점` (정가 합계 `${total:,}`)"),
        color=discord.Color.gold())
    cats = {}
    for it in LUXURY_ITEMS:
        cats.setdefault(it["cat"], []).append(it)
    for cat, items in cats.items():
        lines = []
        for it in items:
            tag = " `[보유 중]`" if int(inv.get(it["name"], 0) or 0) > 0 else ""
            lines.append(f"• **{it['name']}** - `${it['price']:,}`{tag}\n  └ *{it['desc']}*")
        embed.add_field(name=cat, value="\n".join(lines)[:1024], inline=False)
    embed.set_footer(text="사치품은 한 종류당 1개 · 거래, 경매, 압류 불가")
    return embed


class DepartmentStoreSelect(discord.ui.Select):
    def __init__(self, owner: discord.Member, items: list):
        self.owner = owner
        options = [discord.SelectOption(label=it["name"], description=f"${it['price']:,} · {it['desc']}"[:100], value=it["id"])
                   for it in items]
        super().__init__(placeholder="구매할 사치품을 선택하세요...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner.id:
            await interaction.response.send_message("본인의 백화점 창만 조작할 수 있습니다.", ephemeral=True)
            return
        item = LUXURY_BY_ID.get(self.values[0])
        if not item:
            await interaction.response.send_message("존재하지 않는 상품입니다.", ephemeral=True)
            return

        async def do_buy(inter: discord.Interaction):
            try:
                res = await asyncio.to_thread(luxury_buy_sync, inter.user.id, item["id"])
            except Exception as e:
                print(f"Luxury buy error: {e}")
                await inter.edit_original_response(content="구매 처리 중 DB 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.", embed=None, view=None)
                return
            if res[0] == "ok":
                embed = discord.Embed(
                    title="[백화점] 구매 완료",
                    description=(f"**{inter.user.mention}** 님이 **{item['name']}** 을(를) 구매했습니다!\n\n"
                                 f"**결제 금액:** `${item['price']:,}`\n**잔여 자금:** `${res[1]:,}`\n"
                                 f"`/프로필` 에서 보유 사치품을 확인해 보세요."),
                    color=discord.Color.gold())
                await inter.edit_original_response(content=None, embed=embed, view=None)
                if item["price"] >= LUXURY_ANNOUNCE_MIN:
                    try:
                        await inter.channel.send(f"[백화점] {inter.user.mention} 님이 **{item['name']}** 을(를) `${item['price']:,}` 에 구매했습니다!")
                    except Exception:
                        pass
            elif res[0] == "owned":
                await inter.edit_original_response(content="이미 보유 중인 사치품입니다.", embed=None, view=None)
            elif res[0] == "no_money":
                await inter.edit_original_response(content=f"자금이 부족합니다! (필요 `${item['price']:,}` / 보유 `${res[1]:,}`)", embed=None, view=None)
            else:
                await inter.edit_original_response(content="존재하지 않는 상품입니다.", embed=None, view=None)

        confirm = discord.Embed(
            title="[백화점] 구매 확인",
            description=f"**{item['name']}**\n*{item['desc']}*\n\n**가격:** `${item['price']:,}`",
            color=discord.Color.gold())
        await interaction.response.send_message(embed=confirm, view=_PurchaseConfirmView(interaction.user.id, do_buy), ephemeral=True)


class DepartmentStoreView(discord.ui.View):
    def __init__(self, owner: discord.Member, items: list):
        super().__init__(timeout=180)
        self.add_item(DepartmentStoreSelect(owner, items))


@bot.tree.command(name="백화점", description="[공용] 명품·차량·부동산 같은 사치품을 사서 재력을 과시합니다. (프로필에 표시)")
async def department_store_cmd(interaction: discord.Interaction):
    if not await check_maintenance(interaction): return
    if await check_jail(interaction): return
    try:
        data = await asyncio.to_thread(get_user_data, interaction.user.id)
    except Exception as e:
        print(f"Department store error: {e}")
        await interaction.response.send_message("백화점 정보를 불러오지 못했습니다. 잠시 후 다시 시도해 주세요.", ephemeral=True)
        return
    inv = data.get("inventory") or {}
    money = int(data.get("money") or 0)
    items = [it for it in LUXURY_ITEMS if int(inv.get(it["name"], 0) or 0) < 1]
    kwargs = {"view": DepartmentStoreView(interaction.user, items)} if items else {}
    await interaction.response.send_message(embed=department_store_embed(interaction.user, money, inv), ephemeral=True, **kwargs)
