from flask import Flask
from threading import Thread
import os
import random
from datetime import datetime, timedelta

# 렌더 포트 검사를 통과하기 위한 가짜 웹 서버
app = Flask('')

@app.route('/')
def home():
    return "Mafia Bot is running with Supabase DB!"

def run():
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = Thread(target=run)
    t.start()

keep_alive()

import discord
from discord.ext import commands
from discord import app_commands
from supabase import create_client, Client

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix='!', intents=intents)

# ==========================================
# 🌐 [Supabase DB 클라이언트 연결]
# ==========================================
def _clean_env(value):
    return (value or "").strip().strip('"').strip("'").strip()

SUPABASE_URL = _clean_env(os.getenv("SUPABASE_URL"))
SUPABASE_KEY = _clean_env(os.getenv("SUPABASE_KEY"))

SUPABASE_URL = SUPABASE_URL.rstrip("/")
if SUPABASE_URL.endswith("/rest/v1"):
    SUPABASE_URL = SUPABASE_URL[: -len("/rest/v1")]
SUPABASE_URL = SUPABASE_URL.rstrip("/")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("🚨 [경고] SUPABASE_URL 또는 SUPABASE_KEY 환경 변수가 설정되지 않았습니다!")
    supabase = None
else:
    try:
        supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
        print(f"ℹ️ 사용 중인 SUPABASE_URL: {SUPABASE_URL}")
        try:
            supabase.table("user_data").select("user_id").limit(1).execute()
            print("✅ Supabase 데이터베이스 연결 및 user_data 테이블 접근 성공!")
        except Exception as test_err:
            print(f"🚨 Supabase 접근 테스트 실패: {test_err}")
    except Exception as e:
        print(f"🚨 Supabase 연결 실패: {e}")
        supabase = None

# ==========================================
# 🚨 [점검 모드 설정 스위치]
# ==========================================
MAINTENANCE_MODE = False 

# 쿨타임 관리를 위한 메모리 사전 (유저ID: 다음 수행 가능 시간)
mission_cooldowns = {}

# === 🎖️ 변경된 명예 포인트에 따른 직급 계산 함수 ===
def get_user_rank(honor: int) -> str:
    if honor >= 2000:
        return "👑 대부"
    elif honor >= 1000:
        return "⚔️ 언더보스"
    elif honor >= 500:
        return "🥇 카포"
    elif honor >= 300:
        return "🥈 솔다토"
    elif honor >= 100:
        return "🥉 행동대원"
    else:
        return "🌱 신입"

# === 🗺️ 4대 지역 정보 데이터 ===
LOCATIONS = {
    "레벤하임": {
        "desc": "수도 / 시작 지점. 칼렌데의 중심 도시로 정부 기관과 중앙치안국 본부가 밀집한 도시.",
        "risk": "보통",
        "emoji": "🏛️"
    },
    "던베르크": {
        "desc": "공업지대. 대규모 공장과 노동자 구역이 밀집한 산업 도시.",
        "risk": "중간",
        "emoji": "⚙️"
    },
    "블랙워드": {
        "desc": "빈민가. 열악한 환경과 높은 범죄율, 음지의 범죄조직이 도사리는 구역.",
        "risk": "높음",
        "emoji": "🏚️"
    },
    "에르느코항": {
        "desc": "항구도시 / 해상 무역 중심지. 합법과 불법 밀거래가 공존하는 개방형 항구.",
        "risk": "변동적",
        "emoji": "⚓"
    }
}

# === 🛒 지역별 암시장 아이템 목록 ===
BLACK_MARKET_ITEMS = {
    "레벤하임": [
        {"id": "gov_pass", "name": "치안국 위조 신분증", "price": 1200, "desc": "실패 시 수배 방어", "emoji": "🪪"},
        {"id": "vip_cigar", "name": "고급 시가 패키지", "price": 500, "desc": "즉시 명예 +15 pt", "emoji": "🚬"},
        {"id": "chrono_watch", "name": "고급 크로노그래프 시계", "price": 2000, "desc": "임무 쿨타임 50% 단축 (5초)", "emoji": "⏱️"}
    ],
    "던베르크": [
        {"id": "heavy_glove", "name": "강화 작업용 장갑", "price": 800, "desc": "임무 보상금 +15%", "emoji": "🥊"},
        {"id": "boost_drink", "name": "고농축 자양강장제", "price": 400, "desc": "쿨타임 즉시 초기화", "emoji": "🧪"},
        {"id": "quick_bike", "name": "통신용 특수 바이크", "price": 2200, "desc": "기동력 상승으로 쿨타임 50% 단축 (5초)", "emoji": "🛵"}
    ],
    "블랙워드": [
        {"id": "lockpick", "name": "녹슨 락픽 세트", "price": 300, "desc": "임무 성공률 +10%", "emoji": "🔑"},
        {"id": "black_hood", "name": "익명의 덮개", "price": 600, "desc": "임무 성공률 +10%", "emoji": "🥷"},
        {"id": "night_goggles", "name": "암시장 야시경", "price": 1500, "desc": "임무 성공률 +15%", "emoji": "🗡️"}
    ],
    "에르느코항": [
        {"id": "smuggled_rum", "name": "밀수된 고급 양주", "price": 1500, "desc": "임무 명예 보상 2배", "emoji": "🍾"},
        {"id": "stealth_sonar", "name": "해상 무전 도청기", "price": 2000, "desc": "성공률 +15% & 자금 +10%", "emoji": "📻"},
        {"id": "pirate_map", "name": "해적 비밀 항해 지도", "price": 2500, "desc": "임무 성공률 +20%", "emoji": "🗺️"}
    ]
}

# === 🗄️ Supabase DB 연동 함수들 ===
def get_user_data(user_id: int) -> dict:
    str_id = str(user_id)
    if not supabase:
        return {"user_id": str_id, "money": 1000, "honor": 0, "location": "레벤하임", "inventory": {}}

    try:
        response = supabase.table("user_data").select("*").eq("user_id", str_id).execute()
        if response.data:
            user_info = response.data[0]
            if "inventory" not in user_info or user_info["inventory"] is None:
                user_info["inventory"] = {}
            return user_info
        
        default_data = {
            "user_id": str_id,
            "money": 1000,
            "honor": 0,
            "location": "레벤하임",
            "inventory": {}
        }
        ins = supabase.table("user_data").insert(default_data).execute()
        return default_data
    except Exception as e:
        print(f"DB Error (get_user_data): {e}")
        return {"user_id": str_id, "money": 1000, "honor": 0, "location": "레벤하임", "inventory": {}}

def _require_db():
    if not supabase:
        raise RuntimeError("Supabase 클라이언트가 초기화되지 않았습니다")

def _check_updated(res, action: str):
    if not res.data:
        raise RuntimeError(f"{action}: 반영된 행이 없습니다")

def update_user_stats(user_id: int, money_add: int, honor_add: int):
    str_id = str(user_id)
    _require_db()
    user_info = get_user_data(user_id)
    
    new_money = user_info["money"] + money_add
    new_honor = user_info["honor"] + honor_add
    
    res = supabase.table("user_data").update({
        "money": new_money,
        "honor": new_honor
    }).eq("user_id", str_id).execute()
    _check_updated(res, "update_user_stats")

def update_user_location(user_id: int, new_location: str):
    str_id = str(user_id)
    _require_db()
    res = supabase.table("user_data").update({
        "location": new_location
    }).eq("user_id", str_id).execute()
    _check_updated(res, "update_user_location")

def update_user_market_purchase(user_id: int, new_money: int, new_honor: int, inventory: dict):
    str_id = str(user_id)
    _require_db()
    res = supabase.table("user_data").update({
        "money": new_money,
        "honor": new_honor,
        "inventory": inventory
    }).eq("user_id", str_id).execute()
    _check_updated(res, "update_user_market_purchase")

def delete_user_data(user_id: int):
    str_id = str(user_id)
    _require_db()
    res = supabase.table("user_data").delete().eq("user_id", str_id).execute()
    _check_updated(res, "delete_user_data")

# === 🏷️ 칭호 시스템 ===
TITLES = {
    "first_step":   {"name": "🌱 첫 발걸음",       "desc": "임무 1회 성공",                        "check": lambda s, i: s["success"] >= 1},
    "veteran":      {"name": "🗡️ 노련한 청부꾼",   "desc": "임무 10회 성공",                       "check": lambda s, i: s["success"] >= 10},
    "legend":       {"name": "💀 전설의 그림자",   "desc": "임무 50회 성공",                       "check": lambda s, i: s["success"] >= 50},
    "hot_streak":   {"name": "🔥 불붙은 손길",     "desc": "임무 3연속 성공",                      "check": lambda s, i: s["best_streak"] >= 3},
    "unstoppable":  {"name": "⚡ 멈추지 않는 자",  "desc": "임무 5연속 성공",                      "check": lambda s, i: s["best_streak"] >= 5},
    "ledger_thief": {"name": "🏛️ 장부 도둑",       "desc": "'라이벌 보스의 장부 탈취' 5회 성공",   "check": lambda s, i: s["hard_success"] >= 5},
    "unlucky":      {"name": "🩹 불운한 쥐새끼",   "desc": "임무 5회 실패",                        "check": lambda s, i: s["fail"] >= 5},
    "rich":         {"name": "💰 검은 돈의 주인",  "desc": "보유 자금 $10,000 이상",               "check": lambda s, i: (i.get("money") or 0) >= 10000},
    "respected":    {"name": "🎩 존경받는 자",     "desc": "조직 명예 300pt 이상 (솔다토 이상)",   "check": lambda s, i: (i.get("honor") or 0) >= 300},
    "collector":    {"name": "🎒 장비 수집가",     "desc": "암시장 장비 3개 이상 보유",            "check": lambda s, i: len(i.get("inventory") or {}) >= 3},
}

def get_title_blob(user_info: dict) -> dict:
    raw = user_info.get("data")
    blob = dict(raw) if isinstance(raw, dict) else {}
    stats = blob.get("stats") if isinstance(blob.get("stats"), dict) else {}
    for key in ("success", "fail", "streak", "best_streak", "hard_success"):
        stats[key] = int(stats.get(key, 0) or 0)
    blob["stats"] = stats
    blob["titles"] = list(blob.get("titles") or [])
    blob["equipped"] = blob.get("equipped")
    return blob

def evaluate_new_titles(blob: dict, user_info: dict) -> list:
    new_ids = []
    for tid, t in TITLES.items():
        if tid not in blob["titles"] and t["check"](blob["stats"], user_info):
            blob["titles"].append(tid)
            new_ids.append(tid)
    if new_ids and blob.get("equipped") not in TITLES:
        blob["equipped"] = new_ids[0]
    return new_ids

def save_title_blob(user_id: int, blob: dict):
    _require_db()
    res = supabase.table("user_data").update({"data": blob}).eq("user_id", str(user_id)).execute()
    _check_updated(res, "save_title_blob")

def record_mission_result(user_id: int, user_info: dict, success: bool, hard: bool) -> list:
    blob = get_title_blob(user_info)
    s = blob["stats"]
    if success:
        s["success"] += 1
        s["streak"] += 1
        s["best_streak"] = max(s["best_streak"], s["streak"])
        if hard:
            s["hard_success"] += 1
    else:
        s["fail"] += 1
        s["streak"] = 0
    new_ids = evaluate_new_titles(blob, user_info)
    save_title_blob(user_id, blob)
    return new_ids

def get_equipped_title_name(user_info: dict) -> str:
    equipped = get_title_blob(user_info).get("equipped")
    return TITLES[equipped]["name"] if equipped in TITLES else "없음"

def format_new_titles(new_ids: list) -> str:
    if not new_ids:
        return ""
    names = ", ".join(TITLES[t]["name"] for t in new_ids)
    return f"\n\n🏷️ **[새 칭호 획득]** {names}\n`/칭호` 명령어로 장착할 수 있습니다."

def build_title_embed(user: discord.abc.User, blob: dict, new_ids: list = None) -> discord.Embed:
    equipped = blob.get("equipped")
    equipped_name = TITLES[equipped]["name"] if equipped in TITLES else "없음"
    s = blob["stats"]

    earned = [f"• {TITLES[t]['name']} — {TITLES[t]['desc']}" for t in blob["titles"] if t in TITLES]
    locked = [f"• 🔒 ??? — {t['desc']}" for tid, t in TITLES.items() if tid not in blob["titles"]]

    embed = discord.Embed(
        title=f"🏷️ [{user.name} 님의 칭호]",
        description=f"현재 장착 중인 칭호: **{equipped_name}**" + format_new_titles(new_ids or []),
        color=discord.Color.gold()
    )
    embed.add_field(name=f"✅ 획득한 칭호 ({len(earned)}/{len(TITLES)})", value="\n".join(earned) if earned else "*아직 획득한 칭호가 없습니다.*", inline=False)
    if locked:
        embed.add_field(name="🔒 미획득 칭호", value="\n".join(locked), inline=False)
    embed.add_field(
        name="📊 임무 기록",
        value=f"성공 `{s['success']}`회 / 실패 `{s['fail']}`회 / 최고 연속 성공 `{s['best_streak']}`회",
        inline=False
    )
    if user.avatar:
        embed.set_thumbnail(url=user.avatar.url)
    return embed

class TitleSelect(discord.ui.Select):
    def __init__(self, owner: discord.abc.User, blob: dict):
        self.owner = owner
        options = [discord.SelectOption(label="칭호 해제", value="__none__", description="장착한 칭호를 내려놓습니다.", emoji="❌")]
        for tid in blob["titles"]:
            if tid in TITLES:
                options.append(discord.SelectOption(
                    label=TITLES[tid]["name"], value=tid,
                    description=TITLES[tid]["desc"], default=(tid == blob.get("equipped"))
                ))
        super().__init__(placeholder="🏷️ 장착할 칭호를 선택하세요...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        if interaction.user != self.owner:
            await interaction.response.send_message("❌ 타인의 칭호 창은 조작할 수 없습니다.", ephemeral=True)
            return
        await interaction.response.defer()

        info = get_user_data(interaction.user.id)
        blob = get_title_blob(info)
        choice = self.values[0]
        if choice == "__none__":
            blob["equipped"] = None
        elif choice in blob["titles"]:
            blob["equipped"] = choice
        else:
            await interaction.followup.send("❌ 아직 획득하지 못한 칭호입니다.", ephemeral=True)
            return
        save_title_blob(interaction.user.id, blob)

        await interaction.edit_original_response(
            embed=build_title_embed(interaction.user, blob),
            view=TitleView(owner=interaction.user, blob=blob)
        )

class TitleView(discord.ui.View):
    def __init__(self, owner: discord.abc.User, blob: dict):
        super().__init__(timeout=180)
        self.add_item(TitleSelect(owner=owner, blob=blob))

@bot.tree.command(name="칭호", description="[마피아 GM] 획득한 칭호를 확인하고 장착합니다.")
async def titles_command(interaction: discord.Interaction):
    if not await check_maintenance(interaction): return
    if not await check_membership(interaction): return

    user = interaction.user
    info = get_user_data(user.id)
    blob = get_title_blob(info)

    new_ids = evaluate_new_titles(blob, info)
    if new_ids:
        save_title_blob(user.id, blob)

    embed = build_title_embed(user, blob, new_ids)
    if blob["titles"]:
        await interaction.response.send_message(embed=embed, view=TitleView(owner=user, blob=blob))
    else:
        await interaction.response.send_message(embed=embed)

@bot.event
async def on_ready():
    try:
        synced = await bot.tree.sync()
        print(f"총 {len(synced)}개의 슬래시 명령어가 동기화되었습니다.")
    except Exception as e:
        print(e)
    print(f'로그인 완료: {bot.user.name}')

# === [공통 체크 함수] 점검 모드 확인 ===
async def check_maintenance(interaction: discord.Interaction) -> bool:
    if MAINTENANCE_MODE:
        if not interaction.user.guild_permissions.administrator:
            embed = discord.Embed(
                title="🛠️ [시스템 점검 및 업데이트 안내]",
                description="현재 패밀리 GM 봇을 업데이트 중입니다. 잠시만 기다려 주세요!",
                color=discord.Color.orange()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return False
    return True

# === [공통 체크 함수] 조직원 가입 여부 확인 ===
async def check_membership(interaction: discord.Interaction) -> bool:
    user = interaction.user
    role = discord.utils.get(interaction.guild.roles, name="조직원")
    
    if not role or role not in user.roles:
        await interaction.response.send_message(
            "❌ 패밀리의 정식 조직원이 아닙니다! `/가입`을 먼저 진행해 주세요.",
            ephemeral=True
        )
        return False
    return True

# === 시민 전용 아르바이트 쿨타임 사전 ===
parttime_cooldowns = {}

@bot.tree.command(name="아르바이트", description="[시민/공용] 합법적인 일거리를 통해 안전하게 소량의 자금을 벌어듭니다.")
async def part_time_job(interaction: discord.Interaction):
    if not await check_maintenance(interaction): return

    await interaction.response.defer()
    user = interaction.user

    now = datetime.now()
    if user.id in parttime_cooldowns:
        next_available = parttime_cooldowns[user.id]
        if now < next_available:
            remaining = int((next_available - now).total_seconds())
            await interaction.followup.send(f"⏳ 피로를 푸는 중입니다. **{remaining}초** 뒤에 다시 일할 수 있습니다.", ephemeral=True)
            return

    parttime_cooldowns[user.id] = now + timedelta(seconds=10)

    jobs = [
        {"title": "🏪 편의점 야간 알바", "reward": (100, 250)},
        {"title": "📦 물류 센터 상하차", "reward": (200, 350)},
        {"title": "☕ 도시 카페 서빙", "reward": (120, 280)},
        {"title": "🧹 중앙 광장 청소", "reward": (100, 200)}
    ]
    
    selected_job = random.choice(jobs)
    earned_money = random.randint(*selected_job["reward"])

    # DB 자금 갱신 (명예는 변하지 않음)
    update_user_stats(user.id, earned_money, 0)
    user_stats = get_user_data(user.id)

    embed = discord.Embed(
        title=f"🛠️ [{selected_job['title']}] 완료",
        description=f"**{user.mention}** 님이 정직한 노동으로 자금을 벌었습니다!\n\n"
                    f"💰 **획득 급여:** `${earned_money:,}`\n"
                    f"🏦 **현재 보유 자금:** `${user_stats['money']:,}`",
        color=discord.Color.blue()
    )
    if user.avatar:
        embed.set_thumbnail(url=user.avatar.url)

    await interaction.followup.send(embed=embed)

# === 1. 셀프 가입 버튼 뷰 (시민 -> 조직원 승급) ===
class SelfJoinView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    # 1. 마피아 가입 버튼
    @discord.ui.button(label="🔫 마피아 조직 입단", style=discord.ButtonStyle.danger, custom_id="join_mafia_btn")
    async def join_mafia(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.process_join(interaction, target_role_name="조직원", rank_title="마피아 조직원")

    # 2. 경찰 가입 버튼
    @discord.ui.button(label="🚔 치안국 경찰 임용", style=discord.ButtonStyle.primary, custom_id="join_police_btn")
    async def join_police(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.process_join(interaction, target_role_name="경찰", rank_title="순경")

    async def process_join(self, interaction: discord.Interaction, target_role_name: str, rank_title: str):
        if MAINTENANCE_MODE and not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("🛠️ 현재 점검 중입니다.", ephemeral=True)
            return

        user = interaction.user
        citizen_role = discord.utils.get(interaction.guild.roles, name="시민")
        target_role = discord.utils.get(interaction.guild.roles, name=target_role_name)
        
        if not target_role:
            await interaction.response.send_message(f"❌ 서버에 '{target_role_name}' 역할이 없습니다! 관리자에게 문의하세요.", ephemeral=True)
            return

        mafia_role = discord.utils.get(interaction.guild.roles, name="조직원")
        police_role = discord.utils.get(interaction.guild.roles, name="경찰")

        if (mafia_role and mafia_role in user.roles) or (police_role and police_role in user.roles):
            await interaction.response.send_message("⚠️ 당신은 이미 소속(마피아 또는 경찰)이 결정되었습니다!", ephemeral=True)
            return

        try:
            if citizen_role and citizen_role in user.roles:
                await user.remove_roles(citizen_role)
            await user.add_roles(target_role)
        except discord.Forbidden:
            await interaction.response.send_message("🚨 봇의 역할 권한 순서를 올려주세요.", ephemeral=True)
            return

        user_stats = get_user_data(user.id)

        embed = discord.Embed(
            title=f"📜 [{rank_title} 발령/입단]",
            description=f"**{user.mention}** 님이 시민의 삶을 마치고 **{target_role_name}**(으)로 새로운 활동을 시작합니다!\n\n"
                        f"🏷️ **신분 변화:** `시민` ➔ `{target_role_name}`",
            color=discord.Color.gold()
        )
        if user.avatar:
            embed.set_thumbnail(url=user.avatar.url)

        await interaction.response.send_message(embed=embed)

# === 2. 탈퇴 확인/취소 버튼 뷰 (DB 데이터 삭제 및 시민 복귀) ===
class LeaveConfirmView(discord.ui.View):
    def __init__(self, target_user: discord.Member):
        super().__init__(timeout=60)
        self.target_user = target_user

    @discord.ui.button(label="네, 탈퇴하겠습니다", style=discord.ButtonStyle.danger, custom_id="confirm_leave")
    async def confirm_leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.target_user:
            await interaction.response.send_message("❌ 본인의 탈퇴 확인 창만 조작할 수 있습니다.", ephemeral=True)
            return

        await interaction.response.defer()

        member = interaction.user
        mafia_role = discord.utils.get(interaction.guild.roles, name="조직원")
        citizen_role = discord.utils.get(interaction.guild.roles, name="시민")

        try:
            if mafia_role and mafia_role in member.roles:
                await member.remove_roles(mafia_role)
            if citizen_role and citizen_role not in member.roles:
                await member.add_roles(citizen_role)
        except discord.Forbidden:
            await interaction.followup.send("🚨 역할 변경 권한이 없습니다.", ephemeral=True)
            return

        try:
            delete_user_data(member.id)
        except Exception:
            pass

        mission_cooldowns.pop(member.id, None)

        for child in self.children:
            child.disabled = True

        embed = discord.Embed(
            title="💥 [패밀리 조직원 해임 및 기록 삭제]",
            description=(
                f"**{member.mention}** 님이 패밀리를 최종 탈퇴하셨습니다.\n\n"
                f"⚠️ **초기화 완료:** 모든 자금, 명예, 인벤토리, 칭호 기록이 완전히 삭제되었습니다.\n"
                f"🏷️ **신분 변화:** `마피아 (조직원)` ➔ `시민`"
            ),
            color=discord.Color.dark_gray()
        )
        if member.avatar:
            embed.set_thumbnail(url=member.avatar.url)

        await interaction.edit_original_response(embed=embed, view=self)

    @discord.ui.button(label="취소 (패밀리에 잔류)", style=discord.ButtonStyle.secondary, custom_id="cancel_leave")
    async def cancel_leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.target_user:
            await interaction.response.send_message("❌ 본인의 탈퇴 확인 창만 조작할 수 있습니다.", ephemeral=True)
            return

        for child in self.children:
            child.disabled = True

        embed = discord.Embed(
            title="🛡️ [탈퇴 철회]",
            description="패밀리 탈퇴 신청이 취소되었습니다.",
            color=discord.Color.blue()
        )
        await interaction.edit_original_response(embed=embed, view=self)

# === 3. 임무 선택 뷰 ===
class MissionSelect(discord.ui.Select):
    def __init__(self, owner: discord.Member):
        self.owner = owner
        options = [
            discord.SelectOption(label="타 조직 구역 비밀 조사", description="난이도: 하 | 보상: $300~$700 / 명예 +10pt", emoji="🕶️", value="mission_1"),
            discord.SelectOption(label="밀수품 비밀 운송 작업", description="난이도: 중 | 보상: $600~$1,500 / 명예 +25pt", emoji="💼", value="mission_2"),
            discord.SelectOption(label="라이벌 보스의 장부 탈취", description="난이도: 상 | 보상: $1,500~$3,500 / 명예 +50pt", emoji="🏛️", value="mission_3"),
        ]
        super().__init__(placeholder="🎯 수행할 비밀 임무를 선택하세요...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()

        if interaction.user != self.owner:
            await interaction.followup.send("❌ 타인의 임무 선택 창은 조작할 수 없습니다.", ephemeral=True)
            return

        user = interaction.user
        user_stats = get_user_data(user.id)
        old_rank = get_user_rank(user_stats.get("honor", 0))
        inventory = user_stats.get("inventory", {})

        now = datetime.now()
        if user.id in mission_cooldowns:
            next_available = mission_cooldowns[user.id]
            if now < next_available:
                remaining = int((next_available - now).total_seconds())
                await interaction.edit_original_response(view=MissionView(owner=self.owner))
                await interaction.followup.send(f"⏳ 체력을 회복 중입니다. **{remaining}초** 뒤에 다시 임무를 수행할 수 있습니다.", ephemeral=True)
                return

        cooldown_seconds = 10
        if "⏱️ 고급 크로노그래프 시계" in inventory or "🛵 통신용 특수 바이크" in inventory:
            cooldown_seconds = 5

        mission_cooldowns[user.id] = now + timedelta(seconds=cooldown_seconds)

        mission_data = {
            "mission_1": {"title": "🕶️ 타 조직 구역 비밀 조사", "success_rate": 80, "reward": (300, 700), "honor": 10},
            "mission_2": {"title": "💼 밀수품 비밀 운송 작업", "success_rate": 65, "reward": (600, 1500), "honor": 25},
            "mission_3": {"title": "🏛️ 라이벌 보스의 장부 탈취", "success_rate": 50, "reward": (1500, 3500), "honor": 50},
        }

        selected = mission_data[self.values[0]]
        current_loc = user_stats.get("location", "레벤하임")

        success_bonus = 0
        money_mult = 1.0
        honor_mult = 1.0
        active_buffs = []

        if "🔑 녹슨 락픽 세트" in inventory:
            success_bonus += 10
            active_buffs.append("🔑 락픽 (+10%)")
        if "🥷 익명의 덮개" in inventory:
            success_bonus += 10
            active_buffs.append("🥷 덮개 (+10%)")
        if "🗡️ 암시장 야시경" in inventory:
            success_bonus += 15
            active_buffs.append("🗡️ 야시경 (+15%)")
        if "🗺️ 해적 비밀 항해 지도" in inventory:
            success_bonus += 20
            active_buffs.append("🗺️ 항해 지도 (+20%)")
        if "📻 해상 무전 도청기" in inventory:
            success_bonus += 15
            money_mult += 0.10
            active_buffs.append("📻 도청기 (+15% 성공률 & +10% 자금)")
        if "🥊 강화 작업용 장갑" in inventory:
            money_mult += 0.15
            active_buffs.append("🥊 장갑 (+15% 자금)")
        if "🍾 밀수된 고급 양주" in inventory:
            honor_mult += 1.0
            active_buffs.append("🍾 양주 (명예 2배)")

        final_success_rate = min(98, selected["success_rate"] + success_bonus)
        is_success = random.randint(1, 100) <= final_success_rate
        buff_desc = f"\n✨ **적용된 보너스:** {', '.join(active_buffs)}" if active_buffs else ""

        if is_success:
            base_money = random.randint(*selected["reward"])
            earned_money = int(base_money * money_mult)
            earned_honor = int(selected["honor"] * honor_mult)
            
            try:
                update_user_stats(user.id, earned_money, earned_honor)
            except Exception:
                mission_cooldowns.pop(user.id, None)
                await interaction.edit_original_response(view=MissionView(owner=self.owner))
                await interaction.followup.send("🚨 DB 저장 오류로 보상이 반영되지 않았습니다.", ephemeral=True)
                return

            current_stats = get_user_data(user.id)
            new_rank = get_user_rank(current_stats["honor"])

            rank_up_msg = ""
            if old_rank != new_rank:
                rank_up_msg = f"\n\n🎉 **[조직 승급 축하]** **{old_rank}** ➔ **{new_rank}**(으)로 승급하셨습니다!"

            new_title_ids = record_mission_result(user.id, current_stats, True, self.values[0] == "mission_3")
            rank_up_msg += format_new_titles(new_title_ids)

            embed = discord.Embed(
                title=f"🎯 [{current_loc}] 임무 성공",
                description=f"**{user.mention}** 님이 작전을 완벽하게 완수했습니다!{buff_desc}\n\n"
                            f"📌 **작전명:** {selected['title']}\n"
                            f"💰 **획득 보상금:** `${earned_money:,}`\n"
                            f"🏆 **획득 명예:** `+{earned_honor} pt`\n\n"
                            f"🗡️ **현재 직급:** `{new_rank}`\n"
                            f"🏦 **보유 자금:** `${current_stats['money']:,}`\n"
                            f"🏅 **총 명예:** `{current_stats['honor']} pt`{rank_up_msg}",
                color=discord.Color.green()
            )
        else:
            if "🪪 치안국 위조 신분증" in inventory:
                fail_msg = "⚠️ 임무에 실패했으나 **🪪 치안국 위조 신분증** 덕분에 수배를 면했습니다!"
            else:
                fail_msg = "⚠️ 경비대에 적발되어 임무 보상을 얻지 못했습니다."

            current_stats = get_user_data(user.id)
            current_rank = get_user_rank(current_stats["honor"])
            new_title_ids = record_mission_result(user.id, current_stats, False, False)

            embed = discord.Embed(
                title=f"💥 [{current_loc}] 임무 실패",
                description=f"**{user.mention}** 님이 작전 도중 문제가 발생했습니다!{buff_desc}\n\n"
                            f"📌 **작전명:** {selected['title']}\n"
                            f"{fail_msg}\n\n"
                            f"🗡️ **현재 직급:** `{current_rank}`\n"
                            f"🏦 **보유 자금:** `${current_stats['money']:,}`\n"
                            f"🏅 **총 명예:** `{current_stats['honor']} pt`{format_new_titles(new_title_ids)}",
                color=discord.Color.red()
            )

        if user.avatar:
            embed.set_thumbnail(url=user.avatar.url)

        await interaction.edit_original_response(embed=embed, view=None)

class MissionView(discord.ui.View):
    def __init__(self, owner: discord.Member):
        super().__init__(timeout=180)
        self.add_item(MissionSelect(owner=owner))

# === 4. 암시장 드롭다운 뷰 ===
class MarketSelect(discord.ui.Select):
    def __init__(self, owner: discord.Member, location: str):
        self.owner = owner
        self.location = location
        items = BLACK_MARKET_ITEMS.get(location, [])
        options = [discord.SelectOption(label=f"{i['name']} (${i['price']:,})", description=i['desc'], emoji=i['emoji'], value=i['id']) for i in items]
        super().__init__(placeholder=f"🛒 {location} 암시장 상품을 선택하세요...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        if interaction.user != self.owner:
            await interaction.response.send_message("❌ 타인의 거래 창은 조작할 수 없습니다.", ephemeral=True)
            return

        selected_id = self.values[0]
        items = BLACK_MARKET_ITEMS.get(self.location, [])
        target_item = next((item for item in items if item['id'] == selected_id), None)

        if not target_item:
            await interaction.response.send_message("❌ 존재하지 않는 아이템입니다.", ephemeral=True)
            return

        user_info = get_user_data(interaction.user.id)
        old_rank = get_user_rank(user_info.get("honor", 0))
        inventory = user_info.get("inventory", {})
        item_name = f"{target_item['emoji']} {target_item['name']}"

        if inventory.get(item_name, 0) >= 1:
            await interaction.response.send_message("⚠️ 이미 소지 중인 장비입니다.", ephemeral=True)
            return

        if user_info["money"] < target_item["price"]:
            await interaction.response.send_message("❌ 자금이 부족합니다!", ephemeral=True)
            return

        new_money = user_info["money"] - target_item["price"]
        new_honor = user_info["honor"]
        inventory[item_name] = 1
        
        special_effect_text = ""
        if target_item['id'] == "vip_cigar":
            new_honor += 15
            special_effect_text = "\n✨ 명예 포인트가 즉시 **+15 pt** 증가했습니다!"
        elif target_item['id'] == "boost_drink":
            mission_cooldowns.pop(interaction.user.id, None)
            special_effect_text = "\n⚡ 임무 쿨타임이 **즉시 초기화**되었습니다!"

        update_user_market_purchase(interaction.user.id, new_money, new_honor, inventory)

        new_rank = get_user_rank(new_honor)
        rank_up_msg = ""
        if old_rank != new_rank:
            rank_up_msg = f"\n🎉 **[조직 승급]** **{new_rank}**(으)로 승급하셨습니다!"

        embed = discord.Embed(
            title="💰 [암시장 거래 성사]",
            description=f"**{interaction.user.mention}** 님이 **{item_name}**을(를) 구매했습니다!{special_effect_text}{rank_up_msg}\n\n"
                        f"🏦 **남은 자금:** `${new_money:,}`\n"
                        f"🏆 **현재 명예:** `{new_honor} pt`",
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
    embed = discord.Embed(title="📜 [패밀리 입단 안내]", description="아래 버튼을 눌러 패밀리에 합류하세요.", color=discord.Color.gold())
    await interaction.response.send_message(embed=embed, view=SelfJoinView())

@bot.tree.command(name="탈퇴", description="[마피아 GM] 패밀리 탈퇴 절차를 진행합니다.")
async def leave_family(interaction: discord.Interaction):
    if not await check_maintenance(interaction): return
    if not await check_membership(interaction): return

    user = interaction.user
    embed = discord.Embed(title="⚠️ [패밀리 탈퇴 경고]", description=f"**{user.mention}**님, 정말로 탈퇴하시겠습니까? (모든 기록이 초기화됩니다)", color=discord.Color.red())
    await interaction.response.send_message(embed=embed, view=LeaveConfirmView(target_user=user), ephemeral=True)

@bot.tree.command(name="임무", description="[조직원 전용] 원하는 비밀 임무를 직접 선택하여 수행합니다.")
async def execute_mission(interaction: discord.Interaction):
    if not await check_maintenance(interaction): return
    if not await check_membership(interaction): return
    await interaction.response.defer()

    user = interaction.user
    user_stats = get_user_data(user.id)
    current_loc = user_stats.get("location", "레벤하임")

    embed = discord.Embed(
        title=f"📂 [{current_loc}] 비밀 임무 작전 하달",
        description=f"**{user.mention}** 님, 수행할 작전을 선택해 주십시오.",
        color=discord.Color.dark_purple()
    )
    if user.avatar:
        embed.set_thumbnail(url=user.avatar.url)

    await interaction.followup.send(embed=embed, view=MissionView(owner=user))

@bot.tree.command(name="프로필", description="[마피아 GM] 조직원 신원 조회 서류를 확인합니다.")
async def profile(interaction: discord.Interaction):
    if not await check_maintenance(interaction): return

    user = interaction.user
    role = discord.utils.get(interaction.guild.roles, name="조직원")
    
    if not role or role not in user.roles:
        user_rank = "시민"
    else:
        user_stats = get_user_data(user.id)
        user_rank = get_user_rank(user_stats.get("honor", 0))

    user_stats = get_user_data(user.id)

    embed = discord.Embed(
        title="🕵️‍♂️ [패밀리 비밀 문서] 신원 조회",
        description=f"**{user.name}** 님의 보안 기록입니다.",
        color=discord.Color.dark_red()
    )
    if user.avatar:
        embed.set_thumbnail(url=user.avatar.url)

    embed.add_field(name="📜 신분 / 명칭", value=user.mention, inline=True)
    embed.add_field(name="🗡️ 현재 직급", value=user_rank, inline=True)
    embed.add_field(name="🏷️ 칭호", value=get_equipped_title_name(user_stats), inline=True)
    embed.add_field(name="💰 보유 자금", value=f"${user_stats['money']:,}", inline=True)
    embed.add_field(name="🏆 조직 명예", value=f"{user_stats['honor']} pt", inline=True)
    embed.add_field(name="📍 현재 거점", value=user_stats.get('location', '레벤하임'), inline=False)
    
    inventory = user_stats.get("inventory", {})
    inv_text = "\n".join([f"• **{item}**: `소지 중`" for item in inventory.keys()]) if inventory else "*소지한 아이템이 없습니다.*"
    embed.add_field(name="🎒 소지품 (인벤토리)", value=inv_text, inline=False)

    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="인벤토리", description="[마피아 GM] 소지한 장비를 확인합니다.")
async def show_inventory(interaction: discord.Interaction):
    if not await check_maintenance(interaction): return
    if not await check_membership(interaction): return

    user_stats = get_user_data(interaction.user.id)
    inventory = user_stats.get("inventory", {})

    embed = discord.Embed(title=f"🎒 [{interaction.user.name} 님의 장비 가방]", color=discord.Color.dark_green())
    inv_list = [f"• **{item}** `[보유 중]`" for item in inventory.keys()] if inventory else ["*가방이 비어 있습니다.*"]
    embed.add_field(name="📦 보유 중인 전용 장비", value="\n".join(inv_list), inline=False)
    
    if interaction.user.avatar:
        embed.set_thumbnail(url=interaction.user.avatar.url)

    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="이동", description="[마피아 GM] 다른 지역으로 이동합니다.")
@app_commands.choices(지역=[
    app_commands.Choice(name="🏛️ 레벤하임 (수도)", value="레벤하임"),
    app_commands.Choice(name="⚙️ 던베르크 (공업지대)", value="던베르크"),
    app_commands.Choice(name="🏚️ 블랙워드 (빈민가)", value="블랙워드"),
    app_commands.Choice(name="⚓ 에르느코항 (항구도시)", value="에르느코항")
])
async def move_location(interaction: discord.Interaction, 지역: app_commands.Choice[str]):
    if not await check_maintenance(interaction): return
    if not await check_membership(interaction): return

    user = interaction.user
    selected_loc = 지역.value
    loc_info = LOCATIONS[selected_loc]

    update_user_location(user.id, selected_loc)

    embed = discord.Embed(
        title=f"{loc_info['emoji']} [{selected_loc}] (으)로 이동 완료",
        description=f"**{user.mention}** 님이 성공적으로 도착했습니다.",
        color=discord.Color.dark_blue()
    )
    embed.add_field(name="📜 지역 특징", value=loc_info["desc"], inline=False)
    if user.avatar:
        embed.set_thumbnail(url=user.avatar.url)

    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="암시장", description="[조직원 전용] 암시장에 입장합니다.")
async def open_market(interaction: discord.Interaction):
    if not await check_maintenance(interaction): return
    if not await check_membership(interaction): return

    user = interaction.user
    user_stats = get_user_data(user.id)
    current_loc = user_stats.get("location", "레벤하임")
    loc_emoji = LOCATIONS.get(current_loc, {}).get("emoji", "📍")
    items = BLACK_MARKET_ITEMS.get(current_loc, [])

    item_details = [f"• **{i['emoji']} {i['name']}** (`${i['price']:,}`)\n  └ {i['desc']}" for i in items]

    embed = discord.Embed(
        title=f"🕵️‍♂️ {loc_emoji} [{current_loc} 비밀 암시장]",
        description=f"현재 보유 자금: **${user_stats['money']:,}**\n\n📜 **[판매 물품]**\n" + "\n".join(item_details),
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
        await interaction.response.send_message("❌ 관리자 권한이 필요합니다!", ephemeral=True)
        return

    role = discord.utils.get(interaction.guild.roles, name="조직원")
    citizen_role = discord.utils.get(interaction.guild.roles, name="시민")
    
    if not role or role not in member.roles:
        await interaction.response.send_message(f"⚠️ {member.mention} 님은 조직원이 아닙니다.", ephemeral=True)
        return

    try:
        await member.remove_roles(role)
        if citizen_role:
            await member.add_roles(citizen_role)
        delete_user_data(member.id)
    except Exception:
        pass

    embed = discord.Embed(
        title="⚖️ [패밀리 숙청]",
        description=f"**{member.mention}** 님이 추방되고 모든 기록이 초기화되었습니다.",
        color=discord.Color.dark_embed()
    )
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="도움말", description="[마피아 GM] 명령어 가이드를 확인합니다.")
async def help_command(interaction: discord.Interaction):
    if not await check_maintenance(interaction): return

    embed = discord.Embed(title="📜 [패밀리 GM] 시스템 가이드", color=discord.Color.dark_red())
    embed.add_field(name="🔫 `/가입`", value="조직에 가입합니다. (시민 ➔ 조직원 역할 전환)", inline=False)
    embed.add_field(name="🕵️‍♂️ `/프로필`", value="나의 신원 서류와 직급을 확인합니다. (누구나 조회 가능)", inline=False)
    embed.add_field(name="🚪 `/탈퇴`", value="탈퇴 시 모든 데이터가 초기화되고 시민으로 돌아갑니다.", inline=False)
    embed.add_field(name="📍 `/이동` / 🕵️‍♂️ `/암시장` / 🎒 `/인벤토리` / 🗡️ `/임무` / 🏷️ `/칭호`", value="[조직원 전용 명령어]", inline=False)
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

bot.run(os.getenv("DISCORD_TOKEN"))