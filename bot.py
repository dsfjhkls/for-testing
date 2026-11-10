from flask import Flask
from threading import Thread
import os
import random
import asyncio
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

# === 🚨 수배 및 감옥 관리 데이터 ===
# {user_id: datetime (수배 만료 시간)}
wanted_list = {}

# {user_id: datetime (석방 예정 시간)}
jail_list = {}

# === [공통 체크 함수] 감옥 수감 여부 확인 ===
async def check_jail(interaction: discord.Interaction) -> bool:
    user_id = interaction.user.id
    now = datetime.now()
    
    if user_id in jail_list:
        release_time = jail_list[user_id]
        if now < release_time:
            remaining = int((release_time - now).total_seconds())
            await interaction.response.send_message(
                f"⛓️ **현재 감옥에 수감 중입니다!** 석방까지 **{remaining}초** 남았습니다. (`/프로필`만 이용 가능)",
                ephemeral=True
            )
            return True
        else:
            # 수감 시간 종료 시 감옥 해제
            jail_list.pop(user_id, None)
            
            # DB의 위치를 기본 거점으로 복구
            try:
                update_user_location(user_id, "레벤하임")
            except Exception:
                pass
    return False

# 쿨타임 관리를 위한 메모리 사전 (유저ID: 다음 수행 가능 시간)
mission_cooldowns = {}

# === 🎖️ 명예 포인트 및 소속(마피아/경찰)에 따른 직급 계산 함수 ===
def get_user_rank(honor: int, is_police: bool = False) -> str:
    if is_police:
        # 경찰 전용 직급
        if honor >= 2000:
            return "👑 총경"
        elif honor >= 1000:
            return "⚔️ 경정"
        elif honor >= 500:
            return "🥇 경위"
        elif honor >= 300:
            return "🥈 경사"
        elif honor >= 100:
            return "🥉 경장"
        else:
            return "🌱 신입 경찰"
    else:
        # 마피아(조직원) 전용 직급
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
            return "🌱 깡패"

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
        {"id": "fail_guard", "name": "임무 실패 방지권", "price": 800, "desc": "임무 실패 시 패널티 자동 방어", "emoji": "🎟️"},
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
            "inventory": {},
            "settings": {"auto_fail_guard": True}
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

def update_user_settings(user_id: int, settings: dict):
    str_id = str(user_id)
    _require_db()
    res = supabase.table("user_data").update({
        "settings": settings
    }).eq("user_id", str_id).execute()
    _check_updated(res, "update_user_settings")

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
    "gambler":      {"name": "🎲 도박꾼",          "desc": "도박 10판 플레이",                     "check": lambda s, i: s["gamble_played"] >= 10},
    "jackpot":      {"name": "🍀 잭팟의 주인공",   "desc": "슬롯머신 잭팟(7️⃣7️⃣7️⃣) 당첨",         "check": lambda s, i: s["jackpot"] >= 1},
    "high_roller":  {"name": "💎 카지노의 큰손",   "desc": "도박 누적 베팅액 $50,000 이상",        "check": lambda s, i: s["gamble_wagered"] >= 50000},
    "broke":        {"name": "💸 거덜 난 도박사",  "desc": "도박으로 보유 자금이 $0이 됨",         "check": lambda s, i: s["broke"] >= 1},
}

def get_title_blob(user_info: dict) -> dict:
    raw = user_info.get("data")
    blob = dict(raw) if isinstance(raw, dict) else {}
    stats = blob.get("stats") if isinstance(blob.get("stats"), dict) else {}
    for key in ("success", "fail", "streak", "best_streak", "hard_success",
                "gamble_played", "gamble_won", "gamble_wagered", "gamble_net", "jackpot", "broke"):
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
    if s["gamble_played"] > 0:
        net = s["gamble_net"]
        net_text = f"{'+' if net >= 0 else '-'}${abs(net):,}"
        embed.add_field(
            name="🎰 도박 기록",
            value=f"총 `{s['gamble_played']}`판 / 승리 `{s['gamble_won']}`판 / 누적 손익 `{net_text}`",
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
    if await check_jail(interaction): return

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

# === [공통 체크 함수] 마피아 또는 경찰 가입 여부 확인 ===
async def check_membership(interaction: discord.Interaction) -> bool:
    user = interaction.user
    mafia_role = discord.utils.get(interaction.guild.roles, name="조직원")
    police_role = discord.utils.get(interaction.guild.roles, name="경찰")
    
    # 마피아나 경찰 역할 중 하나라도 소속되어 있다면 통과
    is_mafia = mafia_role and mafia_role in user.roles
    is_police = police_role and police_role in user.roles

    if not is_mafia and not is_police:
        await interaction.response.send_message(
            "❌ 패밀리(마피아) 또는 치안국(경찰) 소속이 아닙니다! `/가입`을 먼저 진행해 주세요.",
            ephemeral=True
        )
        return False
    return True

# === ⚙️ 다중 페이지 설정 뷰 (SettingsView) ===
class SettingsView(discord.ui.View):
    def __init__(self, user: discord.Member, page: int = 0):
        super().__init__(timeout=120)
        self.user = user
        self.page = page
        self.max_page = 1  # 총 2페이지 (0, 1)
        self.update_buttons()

    def update_buttons(self):
        self.prev_btn.disabled = (self.page == 0)
        self.next_btn.disabled = (self.page == self.max_page)

    def create_embed(self) -> discord.Embed:
        user_stats = get_user_data(self.user.id)
        settings = user_stats.get("settings", {"auto_fail_guard": True, "alert_mode": True})

        embed = discord.Embed(
            title="⚙️ [패밀리 시스템 통합 설정]",
            description=f"**{self.user.mention}** 님의 게임 환경 설정을 관리합니다.\n아래 버튼을 눌러 페이지를 이동하거나 설정을 변경하세요.",
            color=discord.Color.dark_purple()
        )

        if self.page == 0:
            auto_guard = settings.get("auto_fail_guard", True)
            status_text = "🟢 **켜짐 (자동 사용)**" if auto_guard else "🔴 **꺼짐 (수동 사용)**"
            
            embed.add_field(
                name="📄 [페이지 1/2] 임무 안전 장치",
                value=f"• **항목:** 임무 실패 방지권 자동 사용\n"
                      f"• **설명:** 임무 실패 시 가방의 방지권을 자동으로 소모해 수배를 방어합니다.\n\n"
                      f"현재 상태: {status_text}",
                inline=False
            )
            self.toggle_btn.label = "상태 전환 (토글)"
            self.toggle_btn.style = discord.ButtonStyle.green if auto_guard else discord.ButtonStyle.danger

        elif self.page == 1:
            alert_mode = settings.get("alert_mode", True)
            status_text = "🟢 **켜짐 (활성화)**" if alert_mode else "🔴 **꺼짐 (차단)**"
            
            embed.add_field(
                name="📄 [페이지 2/2] 시스템 알림 환경",
                value=f"• **항목:** 긴급 수배 및 이벤트 알림\n"
                      f"• **설명:** 주요 이벤트나 수배 경보 발생 시 멘션 알림을 받습니다.\n\n"
                      f"현재 상태: {status_text}",
                inline=False
            )
            self.toggle_btn.label = "상태 전환 (토글)"
            self.toggle_btn.style = discord.ButtonStyle.green if alert_mode else discord.ButtonStyle.danger

        if self.user.avatar:
            embed.set_thumbnail(url=self.user.avatar.url)
            
        embed.set_footer(text=f"페이지 {self.page + 1} / {self.max_page + 1}")
        return embed

    @discord.ui.button(label="◀ 이전", style=discord.ButtonStyle.secondary, custom_id="settings_prev")
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.user:
            await interaction.response.send_message("❌ 본인의 설정 창만 조작할 수 있습니다.", ephemeral=True)
            return
        
        if self.page > 0:
            self.page -= 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.create_embed(), view=self)

    @discord.ui.button(label="상태 전환", style=discord.ButtonStyle.primary, custom_id="settings_toggle")
    async def toggle_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.user:
            await interaction.response.send_message("❌ 본인의 설정 창만 조작할 수 있습니다.", ephemeral=True)
            return

        user_stats = get_user_data(self.user.id)
        settings = user_stats.get("settings", {"auto_fail_guard": True, "alert_mode": True})

        if self.page == 0:
            settings["auto_fail_guard"] = not settings.get("auto_fail_guard", True)
        elif self.page == 1:
            settings["alert_mode"] = not settings.get("alert_mode", True)

        try:
            update_user_settings(self.user.id, settings)
        except Exception:
            await interaction.response.send_message("🚨 설정 저장 중 오류가 발생했습니다.", ephemeral=True)
            return

        await interaction.response.edit_message(embed=self.create_embed(), view=self)

    @discord.ui.button(label="다음 ▶", style=discord.ButtonStyle.secondary, custom_id="settings_next")
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.user:
            await interaction.response.send_message("❌ 본인의 설정 창만 조작할 수 있습니다.", ephemeral=True)
            return
        
        if self.page < self.max_page:
            self.page += 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.create_embed(), view=self)

# === ⚙️ 통합 `/설정` 슬래시 명령어 ===
@bot.tree.command(name="설정", description="[조직원/경찰 전용] 게임 내 통합 환경 설정 창을 엽니다.")
async def settings_command(interaction: discord.Interaction):
    if not await check_maintenance(interaction): return
    if await check_jail(interaction): return
    if not await check_membership(interaction): return

    user = interaction.user
    view = SettingsView(user=user, page=0)
    embed = view.create_embed()

    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

# === 🚨 [경찰 전용] `/검거` 명령어 ===
@bot.tree.command(name="검거", description="[경찰 전용] 지명수배 중인 마피아를 체포하여 감옥에 가둡니다.")
@app_commands.describe(member="검거할 수배 대상 마피아")
async def arrest_member(interaction: discord.Interaction, member: discord.Member):
    if not await check_maintenance(interaction): return

    police_role = discord.utils.get(interaction.guild.roles, name="경찰")
    if not police_role or police_role not in interaction.user.roles:
        await interaction.response.send_message("❌ 치안국 경찰만 검거 명령어를 사용할 수 있습니다!", ephemeral=True)
        return

    now = datetime.now()

    # 1. 수배 대상 여부 및 1분 초과 여부 확인
    if member.id not in wanted_list or now > wanted_list[member.id]:
        wanted_list.pop(member.id, None)
        await interaction.response.send_message(f"❌ {member.mention} 님은 현재 지명수배 상태가 아니거나 증거가 인멸되었습니다.", ephemeral=True)
        return

    # 2. 수배 상태 해제 후 감옥 투옥 (예: 180초 = 3분 수감)
    wanted_list.pop(member.id, None)
    jail_time_seconds = 180 
    jail_list[member.id] = now + timedelta(seconds=jail_time_seconds)

    # 3. DB 위치를 '감옥'으로 변경
    try:
        update_user_location(member.id, "감옥")
    except Exception as e:
        print(f"Location update error: {e}")

    # 4. 경찰 실적 보상 (자금 $500, 명예 +20pt)
    try:
        update_user_stats(interaction.user.id, 500, 20)
    except Exception:
        pass

    police_info = get_user_data(interaction.user.id)
    police_rank = get_user_rank(police_info["honor"], is_police=True)

    embed = discord.Embed(
        title="🚨 [지명수배자 검거 완료]",
        description=(
            f"🚔 **{interaction.user.mention}** 경찰관이 수배범 **{member.mention}** (을)를 현장에서 체포했습니다!\n\n"
            f"⚖️ **처벌:** **{jail_time_seconds // 60}분** 동안 감옥에 수감되며 프로필 외 명령어가 통제됩니다.\n"
            f"📍 **현재 거점:** `감옥`\n\n"
            f"💰 **검거 포상금:** `$500`\n"
            f"🏆 **경찰 실적:** `+20 pt` (현재 직급: `{police_rank}`)"
        ),
        color=discord.Color.dark_blue()
    )
    if member.avatar:
        embed.set_thumbnail(url=member.avatar.url)

    await interaction.response.send_message(embed=embed)

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

# ==========================================
# 🎰 도박 시스템 (카지노)
#   /도박 슬롯 · /도박 동전 · /도박 블랙잭 · /도박 규칙
#   - 누구나 이용 가능 (감옥 수감 중에는 불가)
#   - 돈을 걸고 잃으면 자금이 사라지는 '자금 소각(머니 싱크)' 시스템
# ==========================================
GAMBLE_MIN_BET = 100        # 최소 베팅금
GAMBLE_MAX_BET = 5000       # 최대 베팅금
GAMBLE_COOLDOWN = 3         # 베팅 사이 대기 시간(초) - 연타/도배 방지
COIN_WIN_RATE = 0.48        # 동전 던지기 승률 (승리 시 베팅금의 2배 지급)

# 슬롯머신: (이모지, 등장 가중치, 3개 일치 시 배당 배수)
# 2개 일치는 본전(SLOT_PAIR_MULT배)을 돌려받고, 모두 다르면 꽝. (환수율은 /도박 규칙에서 자동 계산)
SLOT_SYMBOLS = [
    ("🍒", 30, 3),
    ("🍋", 25, 5),
    ("🔔", 20, 8),
    ("💎", 10, 20),
    ("7️⃣", 5, 100),
]
SLOT_PAIR_MULT = 1.0
SLOT_JACKPOT_SYMBOL = "7️⃣"

# 블랙잭 결과별 총 지급 배수 (베팅금 포함)
BLACKJACK_PAYOUT = {"blackjack": 2.5, "win": 2.0, "push": 1.0, "lose": 0}

gamble_cooldowns = {}   # {user_id: datetime}  다음 베팅 가능 시각
gamble_locks = {}       # {user_id: asyncio.Lock}  같은 유저의 동시 베팅(이중 결제) 방지
active_blackjack = {}   # {user_id: BlackjackView}  진행 중인 블랙잭 테이블

class InsufficientFunds(Exception):
    pass

def _gamble_lock(user_id: int) -> asyncio.Lock:
    lock = gamble_locks.get(user_id)
    if lock is None:
        lock = gamble_locks[user_id] = asyncio.Lock()
    return lock

def fmt_net(n: int) -> str:
    return f"{'+' if n >= 0 else '-'}${abs(n):,}"

# --- DB 정산 ---
def settle_gamble(user_id: int, bet: int, payout: int, info: dict = None, prepaid: bool = False, jackpot: bool = False):
    """한 판의 결과를 DB에 한 번에 반영 (자금 + 도박 기록 + 칭호).
    prepaid=True 이면 베팅금이 이미 차감된 상태(블랙잭)라서 payout 만 더해줌.
    반환: (새 보유 자금, 새로 얻은 칭호 ID 목록)"""
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
    new_ids = evaluate_new_titles(blob, {**info, "money": new_money})

    res = supabase.table("user_data").update({"money": new_money, "data": blob}).eq("user_id", str(user_id)).execute()
    _check_updated(res, "settle_gamble")
    return new_money, new_ids

def take_bet(user_id: int, bet: int) -> int:
    """블랙잭 시작 시 베팅금을 먼저 차감. 반환: 차감 후 보유 자금"""
    _require_db()
    info = get_user_data(user_id)
    money = int(info.get("money") or 0)
    if money < bet:
        raise InsufficientFunds(money)
    res = supabase.table("user_data").update({"money": money - bet}).eq("user_id", str(user_id)).execute()
    _check_updated(res, "take_bet")
    return money - bet

# --- 공통 체크 ---
async def gamble_precheck(interaction: discord.Interaction) -> bool:
    if not await check_maintenance(interaction): return False
    if await check_jail(interaction): return False

    now = datetime.now()
    next_ok = gamble_cooldowns.get(interaction.user.id)
    if next_ok and now < next_ok:
        remaining = int((next_ok - now).total_seconds()) + 1
        await interaction.response.send_message(f"⏳ 잠시 후 다시 베팅할 수 있습니다. (**{remaining}초**)", ephemeral=True)
        return False
    gamble_cooldowns[interaction.user.id] = now + timedelta(seconds=GAMBLE_COOLDOWN)
    return True

async def _safe_edit(interaction: discord.Interaction, **kwargs):
    """연출용 메시지 수정이 실패해도(레이트리밋 등) 결과 처리는 계속되도록 보호"""
    try:
        await interaction.edit_original_response(**kwargs)
    except discord.HTTPException:
        pass

# ==========================================
# 🎰 슬롯머신 로직
# ==========================================
def spin_slots() -> list:
    return random.choices([s[0] for s in SLOT_SYMBOLS], weights=[s[1] for s in SLOT_SYMBOLS], k=3)

def slot_multiplier(reels: list) -> float:
    if reels[0] == reels[1] == reels[2]:
        for emoji, _, mult in SLOT_SYMBOLS:
            if emoji == reels[0]:
                return float(mult)
    if len(set(reels)) == 2:
        return SLOT_PAIR_MULT
    return 0.0

def slot_rtp() -> float:
    """현재 설정 기준 슬롯머신 환수율(베팅금 대비 평균 지급 비율)을 정확히 계산"""
    total_w = sum(w for _, w, _ in SLOT_SYMBOLS)
    rtp = 0.0
    for a in SLOT_SYMBOLS:
        for b in SLOT_SYMBOLS:
            for c in SLOT_SYMBOLS:
                p = (a[1] / total_w) * (b[1] / total_w) * (c[1] / total_w)
                rtp += p * slot_multiplier([a[0], b[0], c[0]])
    return rtp

def slot_board(reels: list) -> str:
    return "🎰 ┃ " + " ┃ ".join(reels) + " ┃ 🎰"

# ==========================================
# 🃏 블랙잭 로직
# ==========================================
CARD_RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]
CARD_SUITS = ["♠️", "♥️", "♦️", "♣️"]

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
    while total > 21 and aces:   # A는 필요하면 11 -> 1로 계산
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
    """딜러의 플레이가 끝난 뒤의 결과: blackjack / win / push / lose"""
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

class BlackjackView(discord.ui.View):
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
        embed = discord.Embed(title="🃏 [블랙잭]", color=colors[outcome])

        dealer_text = (f"{hand_str(self.dealer)}  (합계 **{hand_value(self.dealer)}**)" if reveal
                       else f"{card_str(self.dealer[0])} **[?]**")
        embed.add_field(name=f"🧑 {self.owner.display_name}의 패 (합계 {hand_value(self.player)})", value=hand_str(self.player), inline=False)
        embed.add_field(name="🎩 딜러의 패", value=dealer_text, inline=False)

        if outcome is None:
            embed.description = f"💵 베팅금 **${self.bet:,}**\n🃏 **히트**(카드 받기) 또는 ✋ **스탠드**(멈추기)를 선택하세요. (60초 이내)"
        else:
            if note == "timeout":
                msg = "⌛ **시간 초과** — 자동으로 패배 처리되었습니다."
            elif outcome == "blackjack":
                msg = "🃏 **블랙잭!** 3:2 배당으로 지급됩니다."
            elif outcome == "win":
                msg = "🎉 **승리!**" + (" 딜러가 버스트했습니다." if hand_value(self.dealer) > 21 else "")
            elif outcome == "push":
                msg = "🤝 **무승부** — 베팅금이 그대로 돌아옵니다."
            else:
                msg = "💥 **버스트!** 21을 초과했습니다." if hand_value(self.player) > 21 else "💀 **패배**"
            net = payout - self.bet
            embed.description = (f"{msg}\n\n💵 베팅금: `${self.bet:,}`\n💰 지급액: `${payout:,}`\n"
                                 f"📈 손익: `{fmt_net(net)}`\n🏦 보유 자금: `${money:,}`{format_new_titles(new_ids or [])}")

        embed.set_footer(text="딜러는 16 이하에서 카드를 받고 17 이상에서 멈춥니다 · 블랙잭 3:2")
        if self.owner.avatar:
            embed.set_thumbnail(url=self.owner.avatar.url)
        return embed

    async def finish(self, edit, outcome: str, note: str = ""):
        """결과를 정산하고 최종 화면을 표시. edit 은 메시지 수정 함수(embed=, view= 인자)"""
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
                title="🚨 [블랙잭 정산 오류]",
                description="데이터베이스 오류로 결과를 정산하지 못했습니다. 관리자에게 문의해주세요.",
                color=discord.Color.red()), view=None)
            return

        await edit(embed=self.build_embed(reveal=True, outcome=outcome, payout=payout,
                                          money=new_money, new_ids=new_ids, note=note), view=None)

    async def dealer_turn(self, edit):
        while hand_value(self.dealer) < 17:
            self.dealer.append(self.deck.pop())
        await self.finish(edit, blackjack_outcome(self.player, self.dealer))

    async def _guard(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner.id:
            await interaction.response.send_message("❌ 본인의 블랙잭 테이블만 조작할 수 있습니다.", ephemeral=True)
            return False
        await interaction.response.defer()
        return not (self.finished or self.busy)

    @discord.ui.button(label="히트 (카드 받기)", emoji="🃏", style=discord.ButtonStyle.primary)
    async def hit(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction): return
        self.busy = True
        try:
            self.player.append(self.deck.pop())
            value = hand_value(self.player)
            if value > 21:
                await self.finish(interaction.edit_original_response, "lose")
            elif value == 21:   # 21이면 자동 스탠드
                await self.dealer_turn(interaction.edit_original_response)
            else:
                await interaction.edit_original_response(embed=self.build_embed(), view=self)
        finally:
            self.busy = False

    @discord.ui.button(label="스탠드 (멈추기)", emoji="✋", style=discord.ButtonStyle.secondary)
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
# 🎰 /도박 명령어 그룹
# ==========================================
gamble_group = app_commands.Group(name="도박", description="[카지노] 자금을 걸고 도박을 즐깁니다.")
bot.tree.add_command(gamble_group)

BET_DESC = f"베팅할 금액 (${GAMBLE_MIN_BET:,} ~ ${GAMBLE_MAX_BET:,})"

@gamble_group.command(name="슬롯", description="🎰 슬롯머신을 돌립니다. (3개 일치 고배당 / 2개 일치 본전)")
@app_commands.describe(베팅금=BET_DESC)
async def gamble_slots(interaction: discord.Interaction, 베팅금: app_commands.Range[int, GAMBLE_MIN_BET, GAMBLE_MAX_BET]):
    if not await gamble_precheck(interaction): return

    user = interaction.user
    bet = 베팅금

    money = int(get_user_data(user.id).get("money") or 0)
    if money < bet:
        gamble_cooldowns.pop(user.id, None)
        await interaction.response.send_message(f"❌ 자금이 부족합니다! (보유 자금: ${money:,})", ephemeral=True)
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
        await interaction.edit_original_response(content="❌ 자금이 부족합니다!")
        return
    except Exception as e:
        print(f"DB Error (slots): {e}")
        gamble_cooldowns.pop(user.id, None)
        await interaction.edit_original_response(content="🚨 데이터베이스 오류로 베팅이 취소되었습니다. (자금은 차감되지 않았습니다)")
        return

    # 릴이 하나씩 멈추는 연출 (결과는 이미 정산 완료)
    spinning = discord.Embed(title="🎰 [슬롯머신]", description=slot_board(["🔄", "🔄", "🔄"]) + f"\n\n💵 베팅금 `${bet:,}`", color=discord.Color.dark_gold())
    await _safe_edit(interaction, embed=spinning)
    await asyncio.sleep(0.8)
    spinning.description = slot_board([reels[0], "🔄", "🔄"]) + f"\n\n💵 베팅금 `${bet:,}`"
    await _safe_edit(interaction, embed=spinning)
    await asyncio.sleep(0.8)
    spinning.description = slot_board([reels[0], reels[1], "🔄"]) + f"\n\n💵 베팅금 `${bet:,}`"
    await _safe_edit(interaction, embed=spinning)
    await asyncio.sleep(0.8)

    if jackpot:
        msg, color = "🎊 **JACKPOT!!!** 7️⃣7️⃣7️⃣ 대박이 터졌습니다!", discord.Color.gold()
    elif mult >= 2:
        msg, color = f"✨ **{reels[0]} 3개 일치!** 배당 x{mult:g}", discord.Color.green()
    elif mult == SLOT_PAIR_MULT and mult > 0:
        msg, color = "🤝 **2개 일치** — 베팅금이 그대로 돌아옵니다.", discord.Color.blue()
    else:
        msg, color = "💨 **꽝!** 베팅금을 잃었습니다.", discord.Color.red()

    embed = discord.Embed(
        title="🎰 [슬롯머신] 결과",
        description=(f"{slot_board(reels)}\n\n{msg}\n\n💵 베팅금: `${bet:,}`\n💰 지급액: `${payout:,}`\n"
                     f"📈 손익: `{fmt_net(payout - bet)}`\n🏦 보유 자금: `${new_money:,}`{format_new_titles(new_ids)}"),
        color=color
    )
    if user.avatar:
        embed.set_thumbnail(url=user.avatar.url)
    await _safe_edit(interaction, embed=embed)

@gamble_group.command(name="동전", description="🪙 동전 던지기에 베팅합니다. (승리 시 2배)")
@app_commands.describe(베팅금=BET_DESC, 선택="동전의 어느 면에 걸까요?")
@app_commands.choices(선택=[
    app_commands.Choice(name="🪙 앞면", value="front"),
    app_commands.Choice(name="🌑 뒷면", value="back")
])
async def gamble_coin(interaction: discord.Interaction, 베팅금: app_commands.Range[int, GAMBLE_MIN_BET, GAMBLE_MAX_BET], 선택: app_commands.Choice[str]):
    if not await gamble_precheck(interaction): return

    user = interaction.user
    bet = 베팅금

    money = int(get_user_data(user.id).get("money") or 0)
    if money < bet:
        gamble_cooldowns.pop(user.id, None)
        await interaction.response.send_message(f"❌ 자금이 부족합니다! (보유 자금: ${money:,})", ephemeral=True)
        return

    await interaction.response.defer()

    names = {"front": "🪙 앞면", "back": "🌑 뒷면"}
    won = random.random() < COIN_WIN_RATE
    result_side = 선택.value if won else ("back" if 선택.value == "front" else "front")
    payout = bet * 2 if won else 0

    try:
        async with _gamble_lock(user.id):
            new_money, new_ids = settle_gamble(user.id, bet, payout)
    except InsufficientFunds:
        gamble_cooldowns.pop(user.id, None)
        await interaction.edit_original_response(content="❌ 자금이 부족합니다!")
        return
    except Exception as e:
        print(f"DB Error (coin): {e}")
        gamble_cooldowns.pop(user.id, None)
        await interaction.edit_original_response(content="🚨 데이터베이스 오류로 베팅이 취소되었습니다. (자금은 차감되지 않았습니다)")
        return

    flipping = discord.Embed(
        title="🪙 [동전 던지기]",
        description=f"{user.mention} 님이 **{names[선택.value]}**에 `${bet:,}` 를 걸었습니다!\n\n🪙 동전이 공중에서 회전 중...",
        color=discord.Color.dark_gold()
    )
    await _safe_edit(interaction, embed=flipping)
    await asyncio.sleep(1.2)

    embed = discord.Embed(
        title="🪙 [동전 던지기] 결과",
        description=(f"결과: **{names[result_side]}**\n"
                     f"{'🎉 **적중!** 베팅금의 2배를 받았습니다.' if won else '💨 **빗나갔습니다.** 베팅금을 잃었습니다.'}\n\n"
                     f"💵 베팅금: `${bet:,}`\n💰 지급액: `${payout:,}`\n"
                     f"📈 손익: `{fmt_net(payout - bet)}`\n🏦 보유 자금: `${new_money:,}`{format_new_titles(new_ids)}"),
        color=discord.Color.green() if won else discord.Color.red()
    )
    if user.avatar:
        embed.set_thumbnail(url=user.avatar.url)
    await _safe_edit(interaction, embed=embed)

@gamble_group.command(name="블랙잭", description="🃏 딜러와 블랙잭 승부를 겨룹니다. (블랙잭 3:2 / 승리 2배)")
@app_commands.describe(베팅금=BET_DESC)
async def gamble_blackjack(interaction: discord.Interaction, 베팅금: app_commands.Range[int, GAMBLE_MIN_BET, GAMBLE_MAX_BET]):
    if not await gamble_precheck(interaction): return

    user = interaction.user
    bet = 베팅금

    if user.id in active_blackjack:
        gamble_cooldowns.pop(user.id, None)
        await interaction.response.send_message("⚠️ 이미 진행 중인 블랙잭 테이블이 있습니다! 먼저 끝내주세요.", ephemeral=True)
        return

    money = int(get_user_data(user.id).get("money") or 0)
    if money < bet:
        gamble_cooldowns.pop(user.id, None)
        await interaction.response.send_message(f"❌ 자금이 부족합니다! (보유 자금: ${money:,})", ephemeral=True)
        return

    await interaction.response.defer()

    # 베팅금을 먼저 차감하고 테이블에 앉힘 (결과 정산 시 지급액만 더해줌)
    try:
        async with _gamble_lock(user.id):
            take_bet(user.id, bet)
    except InsufficientFunds:
        gamble_cooldowns.pop(user.id, None)
        await interaction.edit_original_response(content="❌ 자금이 부족합니다!")
        return
    except Exception as e:
        print(f"DB Error (blackjack take_bet): {e}")
        gamble_cooldowns.pop(user.id, None)
        await interaction.edit_original_response(content="🚨 데이터베이스 오류로 베팅이 취소되었습니다. (자금은 차감되지 않았습니다)")
        return

    view = BlackjackView(owner=user, bet=bet)
    active_blackjack[user.id] = view

    if is_blackjack(view.player) or is_blackjack(view.dealer):
        # 시작부터 승부가 결정된 경우 (블랙잭)
        view.message = await interaction.edit_original_response(embed=view.build_embed())
        await view.finish(interaction.edit_original_response, blackjack_outcome(view.player, view.dealer))
    else:
        view.message = await interaction.edit_original_response(embed=view.build_embed(), view=view)

@gamble_group.command(name="규칙", description="📖 카지노 규칙·배당표와 나의 도박 기록을 확인합니다.")
async def gamble_rules(interaction: discord.Interaction):
    if not await check_maintenance(interaction): return

    s = get_title_blob(get_user_data(interaction.user.id))["stats"]
    paytable = "\n".join(f"{e}{e}{e} → **x{m}**" for e, _, m in SLOT_SYMBOLS)
    net = s["gamble_net"]

    embed = discord.Embed(
        title="📖 [칼렌데 카지노 규칙]",
        description=f"💵 베팅 한도: **${GAMBLE_MIN_BET:,} ~ ${GAMBLE_MAX_BET:,}** · 베팅 간격 {GAMBLE_COOLDOWN}초\n"
                    f"⚠️ 도박은 평균적으로 자금이 줄어듭니다. 감옥에서는 이용할 수 없습니다.",
        color=discord.Color.dark_gold()
    )
    embed.add_field(
        name="🎰 `/도박 슬롯`",
        value=f"{paytable}\n같은 그림 **2개** → 베팅금 반환 / 모두 다르면 꽝\n📊 환수율 약 **{slot_rtp() * 100:.1f}%**",
        inline=False
    )
    embed.add_field(name="🪙 `/도박 동전`", value=f"앞면/뒷면 중 선택 · 적중 시 **2배** (승률 **{COIN_WIN_RATE * 100:.0f}%**)", inline=False)
    embed.add_field(
        name="🃏 `/도박 블랙잭`",
        value="21에 가까운 쪽이 승리 (21 초과 시 버스트)\n승리 **2배** · 블랙잭(처음 2장 21) **2.5배** · 무승부 베팅금 반환\n딜러는 16 이하 히트 / 17 이상 스탠드 · 60초 내 미선택 시 패배",
        inline=False
    )
    embed.add_field(
        name="📊 나의 도박 기록",
        value=f"총 `{s['gamble_played']}`판 / 승리 `{s['gamble_won']}`판 / 누적 베팅 `${s['gamble_wagered']:,}`\n누적 손익 `{fmt_net(net)}` / 잭팟 `{s['jackpot']}`회",
        inline=False
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

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

# === 2. 탈퇴 확인/취소 버튼 뷰 (마피아 & 경찰 공용 데이터 삭제/초기화) ===
class LeaveConfirmView(discord.ui.View):
    def __init__(self, target_user: discord.Member):
        super().__init__(timeout=60)
        self.target_user = target_user

    @discord.ui.button(label="네, 탈퇴하겠습니다", style=discord.ButtonStyle.danger, custom_id="confirm_leave")
    async def confirm_leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.target_user:
            await interaction.response.send_message("❌ 본인의 탈퇴 확인 창만 조작할 수 있습니다.", ephemeral=True)
            return

        # 3초 타임아웃 방지 지연 응답
        await interaction.response.defer()

        member = interaction.user
        mafia_role = discord.utils.get(interaction.guild.roles, name="조직원")
        police_role = discord.utils.get(interaction.guild.roles, name="경찰")
        citizen_role = discord.utils.get(interaction.guild.roles, name="시민")

        # 1. 소속 역할(마피아 또는 경찰) 제거 & 시민 역할 복구
        try:
            if mafia_role and mafia_role in member.roles:
                await member.remove_roles(mafia_role)
            if police_role and police_role in member.roles:
                await member.remove_roles(police_role)
                
            if citizen_role and citizen_role not in member.roles:
                await member.add_roles(citizen_role)
        except discord.Forbidden:
            await interaction.followup.send(
                "🚨 역할 변경 권한이 없습니다. 서버 설정 > 역할에서 봇의 역할 순서를 상위로 올려주세요.",
                ephemeral=True
            )
            return
        except discord.HTTPException as e:
            print(f"Leave Error (role update): {e}")
            await interaction.followup.send("🚨 디스코드 오류로 탈퇴 처리에 실패했습니다.", ephemeral=True)
            return

        # 2. Supabase DB 데이터 완전 삭제 (초기화)
        try:
            delete_user_data(member.id)
        except Exception:
            pass

        # 3. 쿨타임 메모리 제거
        mission_cooldowns.pop(member.id, None)
        parttime_cooldowns.pop(member.id, None)
        gamble_cooldowns.pop(member.id, None)
        open_table = active_blackjack.pop(member.id, None)
        if open_table:  # 진행 중이던 블랙잭은 초기화된 계정에 정산되지 않도록 종료
            open_table.finished = True
            open_table.stop()

        for child in self.children:
            child.disabled = True

        embed = discord.Embed(
            title="💥 [소속 해임 및 세이브 데이터 초기화]",
            description=(
                f"**{member.mention}** 님이 소속을 최종 탈퇴하셨습니다.\n\n"
                f"⚠️ **초기화 완료:** 모든 보유 자금, 명예, 인벤토리 아이템 및 기록이 DB에서 완전히 삭제되었습니다.\n"
                f"🏷️ **신분 변화:** `마피아/경찰` ➔ `시민`"
            ),
            color=discord.Color.dark_gray()
        )
        if member.avatar:
            embed.set_thumbnail(url=member.avatar.url)

        await interaction.edit_original_response(embed=embed, view=self)

    @discord.ui.button(label="취소 (소속 잔류)", style=discord.ButtonStyle.secondary, custom_id="cancel_leave")
    async def cancel_leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.target_user:
            await interaction.response.send_message("❌ 본인의 탈퇴 확인 창만 조작할 수 있습니다.", ephemeral=True)
            return

        for child in self.children:
            child.disabled = True

        embed = discord.Embed(
            title="🛡️ [탈퇴 철회]",
            description="탈퇴 신청이 취소되었습니다.",
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
        # (임무 실패 로직 내부)
        else:
            current_stats = get_user_data(user.id)
            inventory = current_stats.get("inventory", {})
            settings = current_stats.get("settings", {"auto_fail_guard": True})
            
            guard_item_key = "🎟️ 임무 실패 방지권"
            guard_used = False

            # 실패 방지권 자동 사용 조건 확인
            if settings.get("auto_fail_guard", True) and inventory.get(guard_item_key, 0) > 0:
                inventory[guard_item_key] -= 1
                if inventory[guard_item_key] <= 0:
                    del inventory[guard_item_key]
                
                # DB 인벤토리 반영
                try:
                    update_user_market_purchase(user.id, current_stats["money"], current_stats["honor"], inventory)
                except Exception:
                    pass
                guard_used = True

            if guard_used:
                fail_msg = "🛡️ **[실패 방지권 발동]** 임무에 실패했으나, 가방에 있던 **🎟️ 임무 실패 방지권**이 소모되어 수배 및 패널티가 면제되었습니다!"
            else:
                # 방지권이 없거나 설정이 꺼져있으면 기존대로 수배 등록
                wanted_list[user.id] = datetime.now() + timedelta(seconds=60)

                if "🪪 치안국 위조 신분증" in inventory:
                    fail_msg = "⚠️ 임무에 실패했으나 **🪪 치안국 위조 신분증** 덕분에 지명수배를 면했습니다!"
                    wanted_list.pop(user.id, None)
                else:
                    fail_msg = "🚨 **작전 실패!** 현장에 흔적이 남아 **1분간 경찰 지명수배 상태**가 됩니다!"

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
        special_effect_text = f"\n✨ **특수 효과:** {target_item['desc']}"

        if inventory.get(item_name, 0) >= 1:
            await interaction.response.send_message("⚠️ 이미 소지 중인 장비입니다.", ephemeral=True)
            return

        if user_info["money"] < target_item["price"]:
            await interaction.response.send_message("❌ 자금이 부족합니다!", ephemeral=True)
            return

        new_money = user_info["money"] - target_item["price"]
        new_honor = user_info["honor"]
        inventory[item_name] = 1
        

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
    if await check_jail(interaction): return

    user = interaction.user
    embed = discord.Embed(title="⚠️ [패밀리 탈퇴 경고]", description=f"**{user.mention}**님, 정말로 탈퇴하시겠습니까? (모든 기록이 초기화됩니다)", color=discord.Color.red())
    await interaction.response.send_message(embed=embed, view=LeaveConfirmView(target_user=user), ephemeral=True)

@bot.tree.command(name="임무", description="[조직원 전용] 원하는 비밀 임무를 직접 선택하여 수행합니다.")
async def execute_mission(interaction: discord.Interaction):
    if not await check_maintenance(interaction): return
    if not await check_membership(interaction): return
    if await check_jail(interaction): return
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
    mafia_role = discord.utils.get(interaction.guild.roles, name="조직원")
    police_role = discord.utils.get(interaction.guild.roles, name="경찰")
    
    # 소속에 따른 직급 판별
    if police_role and police_role in user.roles:
        user_stats = get_user_data(user.id)
        user_rank = get_user_rank(user_stats.get("honor", 0), is_police=True)
    elif mafia_role and mafia_role in user.roles:
        user_stats = get_user_data(user.id)
        user_rank = get_user_rank(user_stats.get("honor", 0), is_police=False)
    else:
        user_rank = "시민"
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
    if await check_jail(interaction): return

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
    if await check_jail(interaction): return

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
    if await check_jail(interaction): return

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
    embed.add_field(
        name="🎰 `/도박 슬롯` · `/도박 동전` · `/도박 블랙잭` · `/도박 규칙`",
        value="[누구나 이용 가능] 자금을 걸고 도박을 즐깁니다. 규칙과 배당표는 `/도박 규칙`에서 확인하세요. (감옥 수감 중 이용 불가)",
        inline=False
    )
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

bot.run(os.getenv("DISCORD_TOKEN"))