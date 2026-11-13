from flask import Flask
from threading import Thread
import os
import io
import re
import random
import asyncio
from datetime import datetime, timedelta

# === PIL (Pillow) 관련 Import ===
import unicodedata
from functools import lru_cache
from PIL import Image, ImageDraw, ImageFont, ImageOps

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
import time
import contextvars

# ==========================================
# [명령어 동시 실행 방지]
#   한 유저가 슬래시 명령어를 입력하면, 그 명령어가 끝나기 전까지
#   같은 유저의 다른 슬래시 명령어는 실행되지 않고 안내 메시지만 표시됩니다.
#
#   • '끝난다'의 기준
#       - 일반 명령어: 명령어 처리가 끝나는 순간
#       - 전투·블랙잭·결투처럼 버튼으로 이어지는 진행형 명령어:
#         해당 화면(View)이 종료(승패 결정/시간 초과)되는 순간
#         (View 클래스에 holds_command_lock = True 를 지정한 것만 해당)
#   • 오류로 잠금이 영원히 남지 않도록 COMMAND_LOCK_MAX_SEC 가 지나면 자동 해제됩니다.
# ==========================================
COMMAND_LOCK_MAX_SEC = 600

class CommandLock:
    __slots__ = ("user_id", "name", "interaction_id", "started", "callback_done", "views")

    def __init__(self, user_id: int, name: str, interaction_id: int):
        self.user_id = user_id
        self.name = name
        self.interaction_id = interaction_id
        self.started = time.monotonic()
        self.callback_done = False
        self.views = []

    def is_active(self) -> bool:
        if time.monotonic() - self.started > COMMAND_LOCK_MAX_SEC:
            return False
        if not self.callback_done:
            return True
        return any(not v.is_finished() for v in self.views)

active_commands = {}   # {user_id: CommandLock}
_current_command_lock = contextvars.ContextVar("current_command_lock", default=None)

def _command_label(interaction: discord.Interaction) -> str:
    """'/도박 슬롯' 처럼 그룹 하위 명령어까지 포함한 이름"""
    data = interaction.data or {}
    parts = [str(data.get("name", "?"))]
    opts = data.get("options") or []
    while opts and opts[0].get("type") in (1, 2):   # 1: 하위 명령어, 2: 하위 그룹
        parts.append(str(opts[0].get("name", "")))
        opts = opts[0].get("options") or []
    return " ".join(p for p in parts if p)

def _finish_command(interaction: discord.Interaction):
    lock = active_commands.get(interaction.user.id)
    if lock is None or lock.interaction_id != interaction.id:
        return   # 이 명령어가 잡은 잠금이 아니면 건드리지 않음
    lock.callback_done = True
    if not lock.is_active():
        active_commands.pop(lock.user_id, None)

class GuardedTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # 자동완성 등은 대상이 아님 (슬래시 명령어 실행만 잠금)
        if interaction.type is not discord.InteractionType.application_command:
            return True

        uid = interaction.user.id
        cur = active_commands.get(uid)
        if cur is not None and not cur.is_active():
            active_commands.pop(uid, None)   # 끝났거나 오래된 잠금은 정리
            cur = None

        if cur is not None:
            try:
                await interaction.response.send_message(
                    f"**/{cur.name}** 명령어가 아직 진행 중입니다. 끝난 뒤에 다시 입력해 주세요.",
                    ephemeral=True
                )
            except discord.HTTPException:
                pass
            return False

        # 확인과 등록 사이에 await 가 없으므로, 동시에 입력해도 한 명령어만 통과함
        lock = CommandLock(uid, _command_label(interaction), interaction.id)
        active_commands[uid] = lock
        _current_command_lock.set(lock)
        return True

    async def on_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        _finish_command(interaction)   # 오류로 끝난 명령어도 잠금 해제
        await super().on_error(interaction, error)

class GuardedView(discord.ui.View):
    """holds_command_lock = True 인 View 는 화면이 끝날 때까지 명령어 잠금을 유지합니다."""
    holds_command_lock = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        lock = _current_command_lock.get()
        if lock is not None and self.holds_command_lock and self.timeout is not None:
            lock.views.append(self)

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix='!', intents=intents, tree_cls=GuardedTree)

@bot.listen("on_app_command_completion")
async def _release_command_lock(interaction: discord.Interaction, command):
    _finish_command(interaction)

# ==========================================
# [Supabase DB 클라이언트 연결]
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
    print("[경고] SUPABASE_URL 또는 SUPABASE_KEY 환경 변수가 설정되지 않았습니다!")
    supabase = None
else:
    try:
        supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
        print(f"사용 중인 SUPABASE_URL: {SUPABASE_URL}")
        try:
            supabase.table("user_data").select("user_id").limit(1).execute()
            print("Supabase 데이터베이스 연결 및 user_data 테이블 접근 성공!")
        except Exception as test_err:
            print(f"Supabase 접근 테스트 실패: {test_err}")
    except Exception as e:
        print(f"Supabase 연결 실패: {e}")
        supabase = None

# ==========================================
# [점검 모드 설정 스위치]
# ==========================================
MAINTENANCE_MODE = False 

# === 수배 및 감옥 관리 데이터 ===
# {user_id: datetime (수배 만료 시간)}
wanted_list = {}

# === 서버별 보스 출현 상태 관리 ===
# {guild_id: "dark" | "ultimate" | None}  ← 서버에 지금 어떤 보스가 떠 있는지 (None/없음 = 보스 없음)
#   "dark"     = [히든보스] 어둠의 지배자  (HIDDEN_BOSS)
#   "ultimate" = [최강보스] 태초의 파괴자  (ULTIMATE_BOSS)
active_boss_guilds = {}

# 일반 /전투 승리 시 보스가 출현할 확률 (테스트할 때는 1.0 으로 바꾸면 항상 출현)
# 판정 순서: 최강보스(태초의 파괴자) 먼저 → 실패하면 어둠의 지배자 → 둘 다 실패하면 출현 없음
ULTIMATE_BOSS_SPAWN_CHANCE = 0.02   # 2%
BOSS_SPAWN_CHANCE = 0.10            # 10% (어둠의 지배자)

# {user_id: datetime (석방 예정 시간)}
jail_list = {}

# === 지역별 순찰 상태 관리 ===
# {guild_id: {location_name: expiry_timestamp}}
active_patrols = {}

# === [공통 체크 함수] 감옥 수감 여부 확인 ===
async def check_jail(interaction: discord.Interaction) -> bool:
    user_id = interaction.user.id
    now = datetime.now()
    
    if user_id in jail_list:
        release_time = jail_list[user_id]
        if now < release_time:
            remaining = int((release_time - now).total_seconds())
            await interaction.response.send_message(
                f"**현재 감옥에 수감 중입니다!** 석방까지 **{remaining}초** 남았습니다. (`/프로필`만 이용 가능)",
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

# === 명예 포인트 및 소속(마피아/경찰)에 따른 직급 계산 함수 ===
def get_user_rank(honor: int, is_police: bool = False) -> str:
    if is_police:
        if honor >= 10000:
            return "총경"
        elif honor >= 5000:
            return "경정"
        elif honor >= 3000:
            return "경위"
        elif honor >= 1000:
            return "경사"
        elif honor >= 500:
            return "경장"
        else:
            return "신입 경찰"
    else:
        if honor >= 20000:
            return "슈퍼 초 울트라 메가 하이퍼 개쩌는 대충 뭐 그런 직급"
        elif honor >= 10000:
            return "대부"
        elif honor >= 5000:
            return "언더보스"
        elif honor >= 3000:
            return "카포"
        elif honor >= 1000:
            return "솔다토"
        elif honor >= 500:
            return "행동대원"
        else:
            return "깡패"

# === 4대 지역 정보 데이터 ===
LOCATIONS = {
    "레벤하임": {
        "desc": "수도 / 시작 지점. 칼렌데의 중심 도시로 정부 기관과 중앙치안국 본부가 밀집한 도시.",
        "risk": "보통"
    },
    "던베르크": {
        "desc": "공업지대. 대규모 공장과 노동자 구역이 밀집한 산업 도시.",
        "risk": "중간"
    },
    "블랙워드": {
        "desc": "빈민가. 열악한 환경과 높은 범죄율, 음지의 범죄조직이 도사리는 구역.",
        "risk": "높음"
    },
    "에르느코항": {
        "desc": "항구도시 / 해상 무역 중심지. 합법과 불법 밀거래가 공존하는 개방형 항구.",
        "risk": "변동적"
    },
    "로젠펠트": {
        "desc": "칼렌데 내 유흥업이 발달한 지역. 부유층과 범죄조직원, 다양한 계층의 사람들이 오간다.",
        "risk": "높음"
    }
}

# === 밸런스 상수 (목표: 꾸준히 플레이했을 때 약 3시간 만에 대부 = 명예 10,000pt) ===
MISSION_COOLDOWN_SEC = 25        # 임무 기본 쿨타임
MISSION_COOLDOWN_FAST_SEC = 19   # 크로노그래프 시계 / 특수 바이크 보유 시
NPC_COOLDOWN_SEC = 30            # /전투 쿨타임
BOSS_COOLDOWN_SEC = 30           # /보스 쿨타임
NPC_LOSS_PENALTY = 500           # NPC전 패배 시 치료비
DUEL_MAX_BET = 30                # 결투 1회 최대 명예 베팅 (부계정 명예 몰아주기 방지)

# === 지역별 암시장 아이템 목록 ===
# 실제 효과는 MISSION_BUFFS(임무) / compute_combat_stats(전투) / MissionSelect(쿨타임·소모품)에서 적용됩니다.
BLACK_MARKET_ITEMS = {
    "레벤하임": [
        {"id": "gov_pass", "name": "치안국 위조 신분증", "price": 5000, "desc": "실패 시 지명수배 방어 & 방어력 +15"},
        {"id": "fail_guard", "name": "임무 실패 방지권", "price": 3000, "desc": "임무 실패 시 수배·벌금 1회 방어(소모) & 방어력 +20"},
        {"id": "chrono_watch", "name": "고급 크로노그래프 시계", "price": 16000, "desc": "임무 쿨타임 25초 → 19초"}
    ],
    "던베르크": [
        {"id": "heavy_glove", "name": "강화 작업용 장갑", "price": 3000, "desc": "임무 보상금 +15%"},
        {"id": "boost_drink", "name": "고농축 자양강장제", "price": 1500, "desc": "임무 쿨타임 1회 즉시 초기화(소모)"},
        {"id": "quick_bike", "name": "통신용 특수 바이크", "price": 16000, "desc": "임무 쿨타임 25초 → 19초"}
    ],
    "블랙워드": [
        {"id": "lockpick", "name": "녹슨 락픽 세트", "price": 1000, "desc": "임무 성공률 +3%"},
        {"id": "black_hood", "name": "익명의 덮개", "price": 2400, "desc": "임무 성공률 +3%"},
        {"id": "night_goggles", "name": "대형 밀수 전용 가방", "price": 8000, "desc": "임무 성공 시 추가 자금 +20%"}
    ],
    "에르느코항": [
        {"id": "smuggled_rum", "name": "밀수된 고급 양주", "price": 20000, "desc": "임무 명예 보상 +10%"},
        {"id": "stealth_sonar", "name": "해상 무전 도청기", "price": 10000, "desc": "성공률 +5% & 자금 +10%"},
        {"id": "pirate_map", "name": "해적 비밀 항해 지도", "price": 18000, "desc": "임무 성공률 +5%"}
    ],
    "로젠펠트": [
        {"id": "luxury_vodka", "name": "아우라 증폭기", "price": 80000, "desc": "임무 명예 보상 +20%"},
        {"id": "gold_ring", "name": "금 반지", "price": 12000, "desc": "임무 성공 시 추가 자금 +10%"},
        {"id": "diamond_necklace", "name": "다이아몬드 목걸이", "price": 24000, "desc": "임무 성공 시 추가 자금 +20%"}
    ]
}

# (인벤토리 이름, 성공률 +%p, 자금 +비율, 명예 +비율, 표시 문구) — 임무 성공 시 합산 적용
MISSION_BUFFS = [
    ("녹슨 락픽 세트",      3, 0.00, 0.00, "락픽 (+3%)"),
    ("익명의 덮개",          3, 0.00, 0.00, "덮개 (+3%)"),
    ("해적 비밀 항해 지도",  5, 0.00, 0.00, "항해 지도 (+5%)"),
    ("해상 무전 도청기",     5, 0.10, 0.00, "도청기 (+5% 성공률 & +10% 자금)"),
    ("강화 작업용 장갑",     0, 0.15, 0.00, "장갑 (+15% 자금)"),
    ("대형 밀수 전용 가방",  0, 0.20, 0.00, "밀수 가방 (+20% 자금)"),
    ("금 반지",              0, 0.10, 0.00, "금 반지 (+10% 자금)"),
    ("다이아몬드 목걸이",    0, 0.20, 0.00, "다이아 목걸이 (+20% 자금)"),
    ("밀수된 고급 양주",     0, 0.00, 0.10, "양주 (명예 +10%)"),
    ("아우라 증폭기",        0, 0.00, 0.20, "아우라 증폭기 (명예 +20%)"),
]
CHRONO_ITEMS = ("고급 크로노그래프 시계", "통신용 특수 바이크")
DRINK_ITEM = "고농축 자양강장제"

# === 임무 목록 (드롭다운 표시와 실제 판정이 같은 표를 쓰도록 통합) ===
# success_rate(%) / reward(최소, 최대 $) / honor(pt) / fine(실패 시 벌금 $)
MISSIONS = {
    "mission_1": {"title": "타 조직 구역 비밀 조사",   "label": "타 조직 구역 비밀 조사", "diff": "하",   "success_rate": 85, "reward": (150, 350),   "honor": 8,  "fine": 80},
    "mission_2": {"title": "밀수품 비밀 운송 작업",     "label": "밀수품 비밀 운송 작업", "diff": "중",   "success_rate": 60, "reward": (350, 800),   "honor": 19, "fine": 200},
    "mission_3": {"title": "라이벌 보스의 장부 탈취",   "label": "라이벌 보스의 장부 탈취", "diff": "상",   "success_rate": 40, "reward": (900, 1900),  "honor": 29, "fine": 500},
    "mission_4": {"title": "기밀 문서 야간 밀반출",     "label": "기밀 문서 야간 밀반출", "diff": "중상", "success_rate": 50, "reward": (650, 1400),  "honor": 23, "fine": 350},
    "mission_5": {"title": "중앙 은행 호송 차량 습격",  "label": "중앙 은행 호송 차량 습격", "diff": "최상", "success_rate": 20, "reward": (2500, 5000), "honor": 58, "fine": 1200},
}

# === 무기 상점 아이템 목록 ===
WEAPON_SHOP_ITEMS = [
    {"id": "dagger", "name": "단검", "price": 2000, "desc": "평범한 단검이다 (공격력 +80)"},
    {"id": "pistol", "name": "권총", "price": 6000, "desc": "평범한 권총이다 (공격력 +150)"},
    {"id": "club", "name": "몽둥이", "price": 14000, "desc": "평범한 몽둥이이다 (공격력 +200)"},
    {"id": "syringe", "name": "수상한 주사기", "price": 28000, "desc": "뭔가 수상한 주사기이다 (공격력 +300 / 체력 +1,000)"},
    {"id": "fist", "name": "슈퍼 울트라 주먹", "price": 60000, "desc": "슈퍼 울트라 주먹. (공격력 +500 / 체력 +2,000)"},
]

# 전투 스탯 계산용 (인벤토리 이름, 공격력, 체력) — 위에서부터 먼저 보유한 하나만 적용
WEAPON_TIERS = [
    ("슈퍼 울트라 주먹", 500, 2000),
    ("수상한 주사기", 300, 1000),
    ("몽둥이", 200, 0),
    ("권총", 150, 0),
    ("단검", 80, 0),
]

def compute_combat_stats(honor: int, inventory: dict) -> dict:
    """/전투, /보스, /결투 가 모두 같은 공식을 쓰도록 한 곳으로 통합"""
    hp = 1000 + honor * 5
    atk = 100 + int(honor * 0.5)
    def_val = 10
    for name, a, h in WEAPON_TIERS:
        if name in inventory:
            atk += a
            hp += h
            break
    if "임무 실패 방지권" in inventory: def_val += 20
    if "치안국 위조 신분증" in inventory: def_val += 15
    return {"hp": hp, "atk": atk, "def": def_val}

# === Supabase DB 연동 함수들 ===
# 예전에 저장된 아이템 이름(앞에 이모지가 붙어 있던 것)에서 이모지를 떼어 현재 이름으로 맞춘다.
_LEGACY_EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B50\u2B55\u231A\u231B\u23E9-\u23FF\u2300-\u2307\uFE0F\uFE0E\u200D]+")

def normalize_item_key(key: str) -> str:
    if key.startswith("\U0001F3C6"):          # 예전 전리품 접두사
        return "[전리품] " + _LEGACY_EMOJI_RE.sub("", key).strip()
    return _LEGACY_EMOJI_RE.sub("", key).strip()

def normalize_inventory(inv: dict) -> dict:
    out = {}
    for k, v in (inv or {}).items():
        nk = normalize_item_key(k)
        out[nk] = out.get(nk, 0) + v
    return out

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
            user_info["inventory"] = normalize_inventory(user_info["inventory"])
            return user_info
        
        default_data = {
            "user_id": str_id,
            "money": 1000,
            "honor": 0,
            "location": "레벤하임",
            "inventory": {},
            "settings": {"auto_fail_guard": True}
        }
        supabase.table("user_data").insert(default_data).execute()
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

# === 일일 의뢰(/일퀘) 설정 ===
# 4개를 모두 완료하면 출석 1스택 적립 → DAILY_COOLDOWN_SEC(1일) 동안 새 의뢰 없음
DAILY_QUESTS = [
    {"key": "mission", "name": "비밀 임무 성공",  "target": 3, "hint": "/임무"},
    {"key": "fight", "name": "NPC 전투 승리",   "target": 2, "hint": "/전투"},
    {"key": "gamble", "name": "카지노 도박",      "target": 3, "hint": "/도박"},
    {"key": "parttime", "name": "아르바이트",       "target": 2, "hint": "/아르바이트"},
]
DAILY_COOLDOWN_SEC = 24 * 60 * 60     # 1일 쿨타임 (완료 시각 기준)
ATTENDANCE_TITLE_STACKS = 30          # 출석 스택이 이 값에 도달하면 칭호 획득 (한 달 = 30일)

# === 칭호 시스템 ===
TITLES = {
    "attendance_month": {"name": "성실한 출석왕", "desc": f"일일 의뢰 출석 {ATTENDANCE_TITLE_STACKS}스택 달성 (한 달)", "check": lambda s, i: s["attendance"] >= ATTENDANCE_TITLE_STACKS},
    "first_step":   {"name": "첫 발걸음",       "desc": "임무 1회 성공",                        "check": lambda s, i: s["success"] >= 1},
    "veteran":      {"name": "노련한 청부꾼",   "desc": "임무 10회 성공",                       "check": lambda s, i: s["success"] >= 10},
    "legend":       {"name": "전설의 그림자",   "desc": "임무 50회 성공",                       "check": lambda s, i: s["success"] >= 50},
    "master":       {"name": "소질이 뛰어난 자", "desc": "임무 100회 성공",                      "check": lambda s, i: s["success"] >= 100},
    "hot_streak":   {"name": "불붙은 손길",     "desc": "임무 3연속 성공",                      "check": lambda s, i: s["best_streak"] >= 3},
    "unstoppable":  {"name": "멈추지 않는 자",  "desc": "임무 5연속 성공",                      "check": lambda s, i: s["best_streak"] >= 5},
    "ledger_thief": {"name": "장부 도둑",       "desc": "'라이벌 보스의 장부 탈취' 5회 성공",   "check": lambda s, i: s["hard_success"] >= 5},
    "unlucky":      {"name": "불운한 쥐새끼",   "desc": "임무 5회 실패",                        "check": lambda s, i: s["fail"] >= 5},
    "rich":         {"name": "검은 돈의 주인",  "desc": "보유 자금 $100,000 이상",               "check": lambda s, i: (i.get("money") or 0) >= 100000},
    "respected":    {"name": "존경받는 자",     "desc": "조직 명예 1000pt 이상 (솔다토 이상)",   "check": lambda s, i: (i.get("honor") or 0) >= 300},
    "collector":    {"name": "장비 수집가",     "desc": "암시장 장비 10개 이상 보유",            "check": lambda s, i: len([k for k in (i.get("inventory") or {}) if not k.startswith("[전리품]")]) >= 10},
    "named_hunter": {"name": "네임드 사냥꾼",   "desc": "네임드 몬스터 1종 처치",                "check": lambda s, i: any(k.startswith("[전리품]") for k in (i.get("inventory") or {}))},
    "named_slayer": {"name": "네임드 학살자",   "desc": "네임드 몬스터 4종 모두 처치",          "check": lambda s, i: sum(1 for k in (i.get("inventory") or {}) if k.startswith("[전리품]")) >= 4},
    "gambler":      {"name": "도박꾼",          "desc": "도박 10판 플레이",                     "check": lambda s, i: s["gamble_played"] >= 10},
    "jackpot":      {"name": "잭팟의 주인공",   "desc": "슬롯머신 잭팟(7⃣7⃣7⃣) 당첨",         "check": lambda s, i: s["jackpot"] >= 1},
    "high_roller":  {"name": "카지노의 큰손",   "desc": "도박 누적 베팅액 $100,000 이상",        "check": lambda s, i: s["gamble_wagered"] >= 100000},
    "broke":        {"name": "거덜 난 도박사",  "desc": "도박으로 보유 자금이 $0이 됨",         "check": lambda s, i: s["broke"] >= 1},
}

def get_title_blob(user_info: dict) -> dict:
    raw = user_info.get("data")
    blob = dict(raw) if isinstance(raw, dict) else {}
    stats = blob.get("stats") if isinstance(blob.get("stats"), dict) else {}
    for key in ("success", "fail", "streak", "best_streak", "hard_success",
                "gamble_played", "gamble_won", "gamble_wagered", "gamble_net", "jackpot", "broke",
                "attendance"):
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

def get_equipped_title_name(user_info: dict) -> str:
    equipped = get_title_blob(user_info).get("equipped")
    return TITLES[equipped]["name"] if equipped in TITLES else "없음"

def format_new_titles(new_ids: list) -> str:
    if not new_ids:
        return ""
    names = ", ".join(TITLES[t]["name"] for t in new_ids)
    return f"\n\n**[새 칭호 획득]** {names}\n`/칭호` 명령어로 장착할 수 있습니다."

# ==========================================
# 일일 의뢰 시스템 (데이터는 user_data.data JSON 안의 "daily" / stats.attendance 에 저장)
#   daily = {"progress": {퀘스트키: 횟수}, "cooldown_until": 완료+1일의 epoch 초}
# ==========================================
def _empty_daily_progress() -> dict:
    return {q["key"]: 0 for q in DAILY_QUESTS}

def get_daily_state(blob: dict, now: float = None) -> dict:
    """blob["daily"] 를 읽어 정리해서 돌려줍니다. 쿨타임이 끝났으면 새 의뢰(진행도 0)로 초기화."""
    now = time.time() if now is None else now
    raw = blob.get("daily")
    d = dict(raw) if isinstance(raw, dict) else {}
    prog = d.get("progress") if isinstance(d.get("progress"), dict) else {}
    d["progress"] = {q["key"]: max(0, min(int(prog.get(q["key"], 0) or 0), q["target"])) for q in DAILY_QUESTS}
    d["cooldown_until"] = float(d.get("cooldown_until", 0) or 0)
    if d["cooldown_until"] and now >= d["cooldown_until"]:
        d["cooldown_until"] = 0.0
        d["progress"] = _empty_daily_progress()
    blob["daily"] = d
    return d

def daily_record(blob: dict, key: str, info: dict, now: float = None) -> str:
    """일일 의뢰 진행도를 1 올립니다. (blob 만 수정하므로 호출한 쪽에서 DB에 저장해야 합니다)
    의뢰 1개 완료 / 4개 전부 완료 시 안내 문구를, 아니면 빈 문자열을 돌려줍니다."""
    now = time.time() if now is None else now
    d = get_daily_state(blob, now)
    if d["cooldown_until"] > now:
        return ""                       # 쿨타임 중에는 집계하지 않음
    quest = next((q for q in DAILY_QUESTS if q["key"] == key), None)
    if quest is None or d["progress"][key] >= quest["target"]:
        return ""

    d["progress"][key] += 1
    if d["progress"][key] < quest["target"]:
        return ""                       # 아직 이 의뢰는 진행 중 (조용히 집계)

    done = sum(1 for q in DAILY_QUESTS if d["progress"][q["key"]] >= q["target"])
    if done < len(DAILY_QUESTS):
        return f"\n\n**[일일 의뢰]** {quest['name']} 완료! ({done}/{len(DAILY_QUESTS)})"

    # 4개 전부 완료 → 출석 1스택 + 1일 쿨타임
    s = blob["stats"]
    s["attendance"] += 1
    d["cooldown_until"] = now + DAILY_COOLDOWN_SEC
    new_ids = evaluate_new_titles(blob, info)
    return (f"\n\n**[일일 의뢰 전부 완료]** 출석 **{s['attendance']}스택** 적립! "
            f"(다음 의뢰: <t:{int(d['cooldown_until'])}:R>)" + format_new_titles(new_ids))

# 도박처럼 결과 화면을 여러 곳에서 만드는 경우, 일일 의뢰 안내 문구를 잠시 보관했다가 꺼내 씁니다.
_daily_notices = {}

def take_daily_notice(user_id: int) -> str:
    return _daily_notices.pop(user_id, "")

def build_daily_embed(user: discord.abc.User, blob: dict) -> discord.Embed:
    now = time.time()
    d = get_daily_state(blob, now)
    stacks = blob["stats"]["attendance"]
    cooling = d["cooldown_until"] > now

    lines = []
    for q in DAILY_QUESTS:
        cur = d["progress"][q["key"]]
        mark = "[완료]" if cur >= q["target"] else "[ ]"
        lines.append(f"{mark} **{q['name']}** — `{cur}/{q['target']}`  ({q['hint']})")

    if cooling:
        status = (f"오늘의 의뢰를 모두 완료했습니다!\n"
                  f"다음 의뢰: <t:{int(d['cooldown_until'])}:R> (<t:{int(d['cooldown_until'])}:f>)")
        color = discord.Color.green()
    else:
        done = sum(1 for q in DAILY_QUESTS if d["progress"][q["key"]] >= q["target"])
        status = f"4개를 모두 완료하면 **출석 1스택**을 얻습니다. ({done}/{len(DAILY_QUESTS)} 완료)"
        color = discord.Color.blurple()

    embed = discord.Embed(title=f"[{user.name} 님의 일일 의뢰]", description=status, color=color)
    embed.add_field(name="오늘의 의뢰", value="\n".join(lines), inline=False)

    title_owned = "attendance_month" in blob["titles"]
    if title_owned:
        goal = f"{TITLES['attendance_month']['name']} 칭호 획득 완료!"
    else:
        goal = f"{ATTENDANCE_TITLE_STACKS}스택 달성 시 {TITLES['attendance_month']['name']} 칭호 (남은 스택 {max(0, ATTENDANCE_TITLE_STACKS - stacks)})"
    embed.add_field(name="출석 스택", value=f"**{stacks}** 스택\n{goal}", inline=False)
    embed.set_footer(text="의뢰를 완료한 시각부터 24시간 뒤에 새 의뢰가 열립니다.")
    if user.avatar:
        embed.set_thumbnail(url=user.avatar.url)
    return embed

@bot.tree.command(name="일퀘", description="[마피아 GM] 일일 의뢰 4개를 모두 완료하면 출석 1스택! (한 달 달성 시 칭호)")
async def daily_quest_command(interaction: discord.Interaction):
    if not await check_maintenance(interaction): return
    if not await check_membership(interaction): return
    if await check_jail(interaction): return

    user = interaction.user
    blob = get_title_blob(get_user_data(user.id))
    await interaction.response.send_message(embed=build_daily_embed(user, blob), ephemeral=True)

def build_title_embed(user: discord.abc.User, blob: dict, new_ids: list = None) -> discord.Embed:
    equipped = blob.get("equipped")
    equipped_name = TITLES[equipped]["name"] if equipped in TITLES else "없음"
    s = blob["stats"]

    earned = [f"• {TITLES[t]['name']} — {TITLES[t]['desc']}" for t in blob["titles"] if t in TITLES]
    locked = [f"• ??? — {t['desc']}" for tid, t in TITLES.items() if tid not in blob["titles"]]

    embed = discord.Embed(
        title=f"[{user.name} 님의 칭호]",
        description=f"현재 장착 중인 칭호: **{equipped_name}**" + format_new_titles(new_ids or []),
        color=discord.Color.gold()
    )
    embed.add_field(name=f"획득한 칭호 ({len(earned)}/{len(TITLES)})", value="\n".join(earned) if earned else "*아직 획득한 칭호가 없습니다.*", inline=False)
    if locked:
        embed.add_field(name="미획득 칭호", value="\n".join(locked), inline=False)
    embed.add_field(
        name="임무 기록",
        value=f"성공 `{s['success']}`회 / 실패 `{s['fail']}`회 / 최고 연속 성공 `{s['best_streak']}`회",
        inline=False
    )
    if user.avatar:
        embed.set_thumbnail(url=user.avatar.url)
    return embed

class TitleSelect(discord.ui.Select):
    def __init__(self, owner: discord.abc.User, blob: dict):
        self.owner = owner
        options = [discord.SelectOption(label="칭호 해제", value="__none__", description="장착한 칭호를 내려놓습니다.")]
        for tid in blob["titles"]:
            if tid in TITLES:
                options.append(discord.SelectOption(
                    label=TITLES[tid]["name"], value=tid,
                    description=TITLES[tid]["desc"], default=(tid == blob.get("equipped"))
                ))
        super().__init__(placeholder="장착할 칭호를 선택하세요...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        if interaction.user != self.owner:
            await interaction.response.send_message("타인의 칭호 창은 조작할 수 없습니다.", ephemeral=True)
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
            await interaction.followup.send("아직 획득하지 못한 칭호입니다.", ephemeral=True)
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
    global npc_cooldowns

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
                title="[시스템 점검 및 업데이트 안내]",
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
    
    is_mafia = mafia_role and mafia_role in user.roles
    is_police = police_role and police_role in user.roles

    if not is_mafia and not is_police:
        await interaction.response.send_message(
            "패밀리(마피아) 또는 치안국(경찰) 소속이 아닙니다! `/가입`을 먼저 진행해 주세요.",
            ephemeral=True
        )
        return False
    return True

# ==========================================
# 통합 설정 시스템
#   /설정 → 설정칸(카드)이 표시되고, 버튼으로 칸 이동 / 가운데 버튼으로 켜기·끄기
#
#   새 설정칸을 추가하는 방법
#     1) 아래 SETTINGS_DEFS 에 딕셔너리 1개(항목)를 추가
#     2) 해당 기능 코드에서 get_setting(유저데이터, "키") 로 값을 읽어서 사용
# ==========================================
SETTINGS_DEFS = [
    {
        "key": "auto_fail_guard",
        "category": "임무 안전 장치",
        "name": "임무 실패 방지권 자동 사용",
        "desc": "임무 실패 시 가방의 방지권을 자동으로 소모해 수배를 방어합니다.\n"
                "끄면 방지권이 있어도 사용하지 않고 일반 실패로 처리됩니다.",
        "on_text": "자동 사용",
        "off_text": "수동 사용",
        "default": True,
    },
    {
        "key": "alert_mode",
        "category": "시스템 알림 환경",
        "name": "긴급 수배 및 이벤트 알림",
        "desc": "주요 이벤트나 수배 경보 발생 시 멘션 알림을 받습니다.",
        "on_text": "활성화",
        "off_text": "차단",
        "default": True,
    },
]
SETTING_DEFAULTS = {d["key"]: d["default"] for d in SETTINGS_DEFS}
 
def get_setting(user_info: dict, key: str) -> bool:
    """유저 데이터에서 설정값을 읽음. 설정이 없거나 settings 가 비어(null) 있어도 기본값을 반환"""
    settings = user_info.get("settings")
    if isinstance(settings, dict) and key in settings:
        return bool(settings[key])
    return SETTING_DEFAULTS.get(key, False)
 
def build_settings_embed(user: discord.abc.User, settings: dict, page: int) -> discord.Embed:
    total = len(SETTINGS_DEFS)
    info = {"settings": settings}
    d = SETTINGS_DEFS[page]
    on = get_setting(info, d["key"])
    state = f"**켜짐** ({d['on_text']})" if on else f"**꺼짐** ({d['off_text']})"
 
    embed = discord.Embed(
        title="[패밀리 시스템 통합 설정]",
        description=f"**{user.mention}** 님의 게임 환경 설정을 관리합니다.\n"
                    f"버튼으로 설정칸을 넘기고, 가운데 버튼으로 **켜기 / 끄기**를 바꾸세요.",
        color=discord.Color.dark_purple()
    )
    embed.add_field(
        name=f"[설정칸 {page + 1}/{total}] {d['category']}",
        value=f"• **항목:** {d['name']}\n"
              f"• **설명:** {d['desc']}\n\n"
              f"현재 상태: {state}",
        inline=False
    )
 
    overview = []
    for i, s in enumerate(SETTINGS_DEFS):
        mark = ">" if i == page else "-"
        overview.append(f"{mark} [{'ON' if get_setting(info, s['key']) else 'OFF'}] {s['name']}")
    embed.add_field(name="전체 설정 현황", value="\n".join(overview), inline=False)
 
    if user.avatar:
        embed.set_thumbnail(url=user.avatar.url)
    embed.set_footer(text=f"설정칸 {page + 1} / {total}")
    return embed
 
class SettingsView(discord.ui.View):
    def __init__(self, user: discord.Member, settings: dict, page: int = 0):
        super().__init__(timeout=180)
        self.user = user
        self.settings = settings      # 창을 열 때 한 번 읽어 두고, 칸 이동에는 DB를 쓰지 않음
        self.page = page
        self.message = None
        self.refresh_toggle_button()
 
    def refresh_toggle_button(self):
        """현재 칸의 상태에 맞게 가운데 버튼(켜기/끄기)의 모양을 갱신"""
        on = get_setting({"settings": self.settings}, SETTINGS_DEFS[self.page]["key"])
        self.toggle_btn.label = "끄기" if on else "켜기"
        self.toggle_btn.style = discord.ButtonStyle.danger if on else discord.ButtonStyle.success
 
    def current_embed(self) -> discord.Embed:
        return build_settings_embed(self.user, self.settings, self.page)
 
    async def _guard(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("본인의 설정 창만 조작할 수 있습니다.", ephemeral=True)
            return False
        return True
 
    async def _move(self, interaction: discord.Interaction, step: int):
        self.page = (self.page + step) % len(SETTINGS_DEFS)    # 끝에서 다시 처음/끝으로 이어짐
        self.refresh_toggle_button()
        await interaction.response.edit_message(embed=self.current_embed(), view=self)
 
    @discord.ui.button(label="이전", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction): return
        await self._move(interaction, -1)
 
    @discord.ui.button(label="끄기", style=discord.ButtonStyle.danger)
    async def toggle_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction): return
        await interaction.response.defer()
 
        key = SETTINGS_DEFS[self.page]["key"]
        try:
            # 다른 설정값을 덮어쓰지 않도록 DB의 최신 설정을 읽어서 해당 항목만 변경
            settings = dict(get_user_data(self.user.id).get("settings") or {})
            settings[key] = not get_setting({"settings": settings}, key)
            update_user_settings(self.user.id, settings)
        except Exception as e:
            print(f"DB Error (settings toggle): {e}")
            await interaction.followup.send("설정 저장 중 오류가 발생했습니다.", ephemeral=True)
            return
 
        self.settings = settings
        self.refresh_toggle_button()
        await interaction.edit_original_response(embed=self.current_embed(), view=self)
 
    @discord.ui.button(label="다음", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction): return
        await self._move(interaction, 1)
 
    async def on_timeout(self):
        # 시간이 지나면 죽은 버튼이 남지 않도록 비활성화
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass
 
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

# === [경찰 전용] `/검거` 명령어 (서버 공개 알림형) ===
@bot.tree.command(name="검거", description="[경찰 전용] 지명수배 중인 마피아를 체포하여 감옥에 가둡니다.")
@app_commands.describe(member="검거할 수배 대상 마피아")
async def arrest_member(interaction: discord.Interaction, member: discord.Member):
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
        # 수배 상태가 아닐 때는 나만 보이게(ephemeral=True) 처리
        await interaction.followup.send(f"**{member.display_name}** 님은 현재 지명수배 상태가 아니거나 증거가 인멸되었습니다.", ephemeral=True)
        return

    # 수배 해제 및 감옥 수감 (3분)
    wanted_list.pop(matched_key, None)
    jail_time_seconds = 180 
    jail_list[target_id] = now + timedelta(seconds=jail_time_seconds)

    # DB 위치 변경
    try:
        update_user_location(target_id, "감옥")
    except Exception as e:
        print(f"Location update error: {e}")

    # 경찰 보상 지급
    try:
        update_user_stats(interaction.user.id, 500, 20)
    except Exception:
        pass

    police_info = get_user_data(interaction.user.id)
    police_rank = get_user_rank(police_info["honor"], is_police=True)

    # 서버 전체에 보일 검거 임베드 생성
    embed = discord.Embed(
        title="[치안국 긴급 공지] 지명수배범 검거 완료",
        description=(
            f"**{interaction.user.mention}** 경찰관이 수배범 **{member.mention}** (을)를 현장에서 체포했습니다!\n\n"
            f"**처벌 내용:** **{jail_time_seconds // 60}분** 동안 감옥에 수감되며 프로필 외 모든 활동이 통제됩니다.\n"
            f"**현재 거점:** `감옥`\n\n"
            f"**검거 포상금:** `${500:,}`\n"
            f"**경찰 실적:** `+20 pt` (현재 직급: `{police_rank}`)"
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
SMUGGLE_REJECT_HONOR_LOSS = 20     # 거절당했을 때 마피아 명예 감소
SMUGGLE_FINE_PER_MISSING_HONOR = 25  # 명예가 모자라 못 깎은 만큼 벌금으로 환산 ($/pt)
SMUGGLE_REJECT_POLICE_HONOR = 10   # 제안을 거절한 청렴한 경찰의 명예 포상

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

    # 4. 경찰 보상 지급 (예: 자금 $400~800, 명예 +10pt)
    earned_money = random.randint(400, 800)
    earned_honor = 10

    try:
        update_user_stats(user.id, earned_money, earned_honor)
    except Exception as e:
        print(f"Patrol reward error: {e}")

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
        ),
        color=discord.Color.blue()
    )
    if user.avatar:
        embed.set_thumbnail(url=user.avatar.url)

    await interaction.followup.send(embed=embed)

# === NPC 전투 대상 목록 ===
# 권장 진입선: 깡패(처음부터) → 배신자(단검·명예 300) → 거상(몽둥이·명예 1,000) → 수배자(주사기·명예 3,000) → 암살자(명예 5,000)
NPC_DATA = {
    "골목길 깡패": {
        "name": "골목길 깡패",
        "hp": 700,
        "atk": 70,
        "def": 5,
        "reward_money": (100, 250),
        "reward_honor": 1,
        "desc": "칼렌데 뒷골목에서 서성이는 초보 깡패입니다."
    },
    "조직의 배신자": {
        "name": "조직의 배신자",
        "hp": 2500,
        "atk": 170,
        "def": 30,
        "reward_money": (400, 800),
        "reward_honor": 3,
        "desc": "패밀리의 기밀을 팔아넘기고 도망친 위험한 배신자입니다."
    },
    "암시장 악덕 거상": {
        "name": "암시장 악덕 거상",
        "hp": 5500,
        "atk": 300,
        "def": 70,
        "reward_money": (1000, 2200),
        "reward_honor": 6,
        "desc": "고용한 보디가드들을 대동하고 다니는 악명 높은 거상입니다."
    },
    "지명수배자 보스": {
        "name": "지명수배자 보스",
        "hp": 11000,
        "atk": 520,
        "def": 130,
        "reward_money": (3000, 6000),
        "reward_honor": 12,
        "desc": "치안국에서도 손대지 못하는 강력한 수배범 보스입니다."
    },
    "칼렌데 암살자": {
        "name": "칼렌데 암살자",
        "hp": 18000,
        "atk": 800,
        "def": 220,
        "reward_money": (6000, 11000),
        "reward_honor": 20,
        "desc": "그림자처럼 움직이며 거물만 노리는 전설적인 청부 암살자입니다."
    }
}

# === 히든 보스 데이터 (NPC_DATA 와 분리: /전투 로 출현 조건 없이 소환되는 것을 막기 위함) ===
# 권장: 명예 약 5,000pt + 슈퍼 울트라 주먹/수상한 주사기 (대부 직전의 최종 시험)
HIDDEN_BOSS = {
    "name": "[히든보스] 어둠의 지배자",   # 보스 승리 시 보스가 연속 출현하지 않도록 비교하는 이름이므로 함부로 바꾸지 마세요
    "hp": 75000,
    "atk": 2750,
    "def": 600,
    "reward_money": (15000, 50000),
    "reward_honor": 200,
    "desc": "전투의 살기에 이끌려 모습을 드러낸 칼렌데 지하 세계의 지배자입니다. 15턴 안에 쓰러뜨리지 못하면 제압당합니다."
}

# === 최강 히든 보스 데이터 (태초의 파괴자) ===
ULTIMATE_BOSS = {
    "name": "[최강보스] 태초의 파괴자",
    "hp": 120000,
    "atk": 5500,
    "def": 900,
    "reward_money": (50000, 100000),
    "reward_honor": 1000,
    "desc": "칼렌데 지하 세계의 근원이자 재앙 그 자체인 존재입니다. 15턴 안에 쓰러뜨리지 못하면 세계가 붕괴합니다."
}

# === 네임드(강화) 몬스터 ===
# 일반 NPC보다 훨씬 강하고, 체력이 절반 아래로 떨어지면 격노(enrage)해서 공격력이 오릅니다.
# 처음 처치하면 고유 전리품()을 얻고, 명예는 조금만 주므로 대부 달성 속도에는 거의 영향이 없습니다.
# min_honor 는 입장 조건, 12턴 안에 못 쓰러뜨리면 패배합니다.
NAMED_COOLDOWN_SEC = 240
NAMED_MONSTERS = {
    "marco": {
        "name": "[네임드] 백정 마르코", "named": True, "min_honor": 500, "max_turns": 12,
        "hp": 4200, "atk": 330, "def": 55, "enrage": 1.5,
        "reward_money": (1500, 3000), "reward_honor": 4,
        "drop": "[전리품] 마르코의 정육 칼",
        "desc": "블랙워드 도살장을 지배하는 청부 해결사. 피를 보면 더 사나워집니다."
    },
    "bianca": {
        "name": "[네임드] 독사 비앙카", "named": True, "min_honor": 1500, "max_turns": 12,
        "hp": 8000, "atk": 520, "def": 100, "enrage": 1.5,
        "reward_money": (3000, 5500), "reward_honor": 8,
        "drop": "[전리품] 비앙카의 독니 반지",
        "desc": "로젠펠트 뒷무대의 독살 전문가. 체력이 줄면 맹독을 뿌리며 덤빕니다."
    },
    "boronin": {
        "name": "[네임드] 철의 장군 보로닌", "named": True, "min_honor": 2500, "max_turns": 12,
        "hp": 14000, "atk": 800, "def": 170, "enrage": 1.5,
        "reward_money": (5000, 9000), "reward_honor": 14,
        "drop": "[전리품] 보로닌의 철 훈장",
        "desc": "퇴역 후 용병단을 이끄는 철벽의 장군. 방어가 매우 단단합니다."
    },
    "executioner": {
        "name": "[네임드] 붉은 달의 처형자", "named": True, "min_honor": 4000, "max_turns": 12,
        "hp": 22000, "atk": 1200, "def": 260, "enrage": 1.6,
        "reward_money": (8000, 14000), "reward_honor": 24,
        "drop": "[전리품] 처형자의 붉은 가면",
        "desc": "칼렌데 지하 세계가 두려워하는 전설의 처형자. 정상급 장비가 없으면 상대도 안 됩니다."
    },
}

# === [조직원 전용] `/송금` 명령어 ===
@bot.tree.command(name="송금", description="[조직원 전용] 다른 조직원에게 보유 자금을 양도합니다.")
@app_commands.describe(member="돈을 받을 조직원", amount="보낼 금액 ($)")
async def transfer_money(interaction: discord.Interaction, member: discord.Member, amount: int):
    if not await check_maintenance(interaction): return
    if not await check_membership(interaction): return
    if await check_jail(interaction): return

    sender = interaction.user
    receiver = member

    # 1. 자기 자신에게 송금하는지 확인
    if sender.id == receiver.id:
        await interaction.response.send_message("자기 자신에게는 돈을 송금할 수 없습니다.", ephemeral=True)
        return

    # 2. 송금 금액 유효성 검사 (0원 이하 방지)
    if amount <= 0:
        await interaction.response.send_message("송금 금액은 1원 이상이어야 합니다.", ephemeral=True)
        return

    await interaction.response.defer()

    # 3. 송금자(나)의 최신 데이터 조회
    sender_data = get_user_data(sender.id)
    sender_money = sender_data.get("money", 0)

    # 잔액 부족 확인
    if sender_money < amount:
        await interaction.response.send_message(
            f"보유 자금이 부족합니다!\n"
            f"• 보유 중인 자금: `${sender_money:,}`\n"
            f"• 송금하려는 금액: `${amount:,}`",
            ephemeral=True
        )
        return

    # 4. 수신자(상대방) 데이터 확인 (존재하지 않으면 기본값 생성 방지를 위해 체크)
    receiver_data = get_user_data(receiver.id)
    receiver_money = receiver_data.get("money", 0)

    # 5. 금액 차감 및 적립 계산 (수수료 0% 또는 원하시는 경우 소폭 수수료 부과 가능)
    new_sender_money = sender_money - amount
    new_receiver_money = receiver_money + amount

    try:
        _require_db()
        # 송금자 자금 업데이트
        supabase.table("user_data").update({"money": new_sender_money}).eq("user_id", str(sender.id)).execute()
        # 수신자 자금 업데이트
        supabase.table("user_data").update({"money": new_receiver_money}).eq("user_id", str(receiver.id)).execute()
    except Exception as e:
        print(f"DB Error (Transfer Money): {e}")
        await interaction.followup.send("데이터베이스 오류로 송금에 실패했습니다. 나중에 다시 시도해 주세요.", ephemeral=True)
        return

    # 6. 송금 완료 임베드 생성 (서버 전체 공개 또는 알림)
    embed = discord.Embed(
        title="[조직 자금 이체 완료]",
        description=(
            f"**{sender.mention}** 님이 **{receiver.mention}** 님에게 자금을 안전하게 전달했습니다!\n\n"
            f"**송금 금액:** `${amount:,}`\n\n"
            f"**{sender.display_name} 님의 남은 자금:** `${new_sender_money:,}`\n"
            f"**{receiver.display_name} 님의 보유 자금:** `${new_receiver_money:,}`"
        ),
        color=discord.Color.green()
    )
    if sender.avatar:
        embed.set_thumbnail(url=sender.avatar.url)

    await interaction.followup.send(embed=embed)

# === NPC 턴제 전투 View 클래스 (턴 제한 추가) ===
class NPCBattleView(GuardedView):
    holds_command_lock = True   # 전투가 끝날 때까지 다른 명령어 실행 차단

    def __init__(self, user: discord.Member, user_stats_dict: dict, npc_info: dict, max_turns: int = 10):
        super().__init__(timeout=60)
        self.user = user
        self.npc_info = npc_info
        self.max_turns = max_turns  # 최대 턴 수 설정 (기본 10턴)
        
        # 유저 및 NPC 스탯 설정
        self.u_hp = user_stats_dict["hp"]
        self.u_max_hp = user_stats_dict["hp"]
        self.u_atk = user_stats_dict["atk"]
        self.u_def = user_stats_dict["def"]
        self.u_guard = False

        self.npc_hp = npc_info["hp"]
        self.npc_max_hp = npc_info["hp"]
        self.npc_atk = npc_info["atk"]
        self.npc_def = npc_info["def"]
        self.npc_guard = False

        self.turn_count = 1
        self.logs = [f"{user.display_name} 님과 **[{npc_info['name']}]**의 전투가 시작되었습니다! (제한: {max_turns}턴)"]
        self.message = None
        self.finished = False

    def get_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title=f"[NPC 토벌 전투] 턴 {self.turn_count} / {self.max_turns}",
            description=f"상대: **{self.npc_info['name']}**\n*{self.npc_info['desc']}*",
            color=discord.Color.dark_purple()
        )

        embed.add_field(
            name=f"{self.user.display_name} (나)",
            value=f"체력 : {self.u_hp} / {self.u_max_hp}\n공격력 : {self.u_atk}\n방어력 : {self.u_def}",
            inline=True
        )
        embed.add_field(
            name=f"{self.npc_info['name']}",
            value=f"체력 : {self.npc_hp} / {self.npc_max_hp}\n공격력 : {self.npc_atk}\n방어력 : {self.npc_def}",
            inline=True
        )

        log_text = "\n".join(self.logs[-6:])
        embed.add_field(
            name="전투 현황 로그",
            value=f"```diff\n{log_text}\n```",
            inline=False
        )
        return embed

    async def process_turn(self, interaction: discord.Interaction, action_type: str):
        if interaction.user != self.user:
            await interaction.response.send_message("본인의 전투 창만 조작할 수 있습니다.", ephemeral=True)
            return

        await interaction.response.defer()

        # 1. 유저 행동 처리
        self.u_guard = False
        if action_type == "attack":
            dmg = max(10, self.u_atk - (self.npc_def * 2 if self.npc_guard else self.npc_def) + random.randint(-10, 10))
            if self.npc_guard:
                dmg = int(dmg * 0.5)
                self.logs.append(f"+ {self.npc_info['name']}이(가) 방어 상태여서 대미지가 감소했습니다!")
            self.npc_hp = max(0, self.npc_hp - dmg)
            self.logs.append(f"+ {self.user.display_name}의 공격! {self.npc_info['name']}에게 {dmg} 대미지!")
        elif action_type == "guard":
            self.u_guard = True
            self.logs.append(f"+ {self.user.display_name}이(가) 방어 자세를 취했습니다.")

        # NPC 사망 확인 (승리)
        if self.npc_hp <= 0:
            await self.end_battle(interaction, outcome="win")
            return

        # 2. 턴 제한 확인 (유저 공격 후 NPC를 처치하지 못하고 최대 턴에 도달한 경우 강제 패배)
        if self.turn_count >= self.max_turns:
            self.logs.append(f"- 지정된 {self.max_turns}턴이 지나 도망치지 못하고 제압당했습니다!")
            await self.end_battle(interaction, outcome="turn_limit")
            return

        # 3. NPC 자동 반격 AI
        self.npc_guard = False
        if random.random() < 0.2:
            self.npc_guard = True
            self.logs.append(f"- {self.npc_info['name']}이(가) 방어 태세를 갖췄습니다.")
        else:
            cur_npc_atk = self.npc_atk
            enrage = self.npc_info.get("enrage")
            if enrage and self.npc_hp < self.npc_max_hp * 0.5:
                cur_npc_atk = int(self.npc_atk * enrage)
                if not getattr(self, "_enraged", False):
                    self._enraged = True
                    self.logs.append(f"- {self.npc_info['name']}이(가) 격노했습니다! 공격력 +{int((enrage - 1) * 100)}%")
            npc_dmg = max(10, cur_npc_atk - (self.u_def * 2 if self.u_guard else self.u_def) + random.randint(-10, 10))
            if self.u_guard:
                npc_dmg = int(npc_dmg * 0.5)
                self.logs.append(f"- 방어 성공! 대미지를 절반으로 감소시켰습니다.")
            self.u_hp = max(0, self.u_hp - npc_dmg)
            self.logs.append(f"- {self.npc_info['name']}의 반격! {self.user.display_name}이(가) {npc_dmg} 대미지를 입었습니다.")

        # 유저 사망 확인 (체력 0 패배)
        if self.u_hp <= 0:
            await self.end_battle(interaction, outcome="loss")
            return

        self.turn_count += 1
        await interaction.edit_original_response(embed=self.get_embed(), view=self)

    @discord.ui.button(label="공격하기", style=discord.ButtonStyle.danger)
    async def attack_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.process_turn(interaction, "attack")

    @discord.ui.button(label="방어하기", style=discord.ButtonStyle.primary)
    async def guard_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.process_turn(interaction, "guard")

    async def end_battle(self, interaction: discord.Interaction, outcome: str):
        self.finished = True
        self.stop()

        for child in self.children:
            child.disabled = True

        user_data = get_user_data(self.user.id)
        current_money = user_data.get("money", 0)
        current_honor = user_data.get("honor", 0)

        if outcome == "win":
            earned_money = random.randint(*self.npc_info["reward_money"])
            earned_honor = self.npc_info["reward_honor"]

            new_money = current_money + earned_money
            new_honor = current_honor + earned_honor

            update_payload = {"money": new_money, "honor": new_honor}
            drop_msg = ""
            drop_item = self.npc_info.get("drop")
            if drop_item:
                inv = user_data.get("inventory") or {}
                if drop_item not in inv:
                    inv[drop_item] = 1
                    update_payload["inventory"] = inv
                    drop_msg = f"\n**고유 전리품 획득:** {drop_item}"
                else:
                    drop_msg = "\n이미 전리품을 보유 중입니다."

            # 일일 의뢰 집계 (NPC 전투 승리)
            daily_blob = get_title_blob(user_data)
            daily_msg = daily_record(daily_blob, "fight", {
                **user_data, "money": new_money, "honor": new_honor,
                "inventory": update_payload.get("inventory", user_data.get("inventory"))
            })
            update_payload["data"] = daily_blob

            try:
                _require_db()
                supabase.table("user_data").update(update_payload).eq("user_id", str(self.user.id)).execute()
            except Exception as e:
                print(f"DB Error (NPC Battle Win): {e}")
            # 보스 출현 판정 (서버에 보스가 없을 때만 / 보스·네임드 처치로는 출현하지 않음)
            boss_spawn_msg = ""
            guild_id = interaction.guild_id if interaction.guild else None
            is_boss_fight = self.npc_info["name"] in (HIDDEN_BOSS["name"], ULTIMATE_BOSS["name"])
            if guild_id and not is_boss_fight and not self.npc_info.get("named") and not active_boss_guilds.get(guild_id):
                roll = random.random()
                if roll < ULTIMATE_BOSS_SPAWN_CHANCE:
                    active_boss_guilds[guild_id] = "ultimate"
                    boss_spawn_msg = (
                        "\n\n**[세계멸망 경보]** \n"
                        "차원의 균열이 갈라지며 역대급 재앙의 기운이 감지되었습니다!\n"
                        "**[최강보스] 태초의 파괴자**가 강림했습니다! (`/보스` 명령어로 토벌 가능)"
                    )
                elif roll < ULTIMATE_BOSS_SPAWN_CHANCE + BOSS_SPAWN_CHANCE:
                    active_boss_guilds[guild_id] = "dark"
                    boss_spawn_msg = (
                        "\n\n**[긴급 상황 경보]** \n"
                        "전투의 여파로 부근에서 엄청난 살기가 감지되었습니다!\n"
                        "**어둠의 지배자**가 이 지역에 출현했습니다! (`/보스` 명령어로 토벌 가능)"
                    )

            embed = discord.Embed(
                title=f"[전투 승리] {self.npc_info['name']} 격퇴!",
                description=f"**{self.user.mention}** 님이 **{self.npc_info['name']}**(을)를 완벽하게 쓰러뜨렸습니다!\n\n"
                            f"**획득 보상금:** `${earned_money:,}`\n"
                            f"**획득 명예:** `+{earned_honor} pt`\n\n"
                            f"**현재 보유 자금:** `${new_money:,}`\n"
                            f"**현재 명예:** `{new_honor} pt`"
                            f"{drop_msg}{daily_msg}{boss_spawn_msg}",  # 전리품 + 일일 의뢰 + 보스 등장 메시지 포함
                color=discord.Color.green()
            )
        else:
            # 턴 초과 패배("turn_limit") 또는 사망 패배("loss")
            penalty = min(current_money, NPC_LOSS_PENALTY)
            new_money = max(0, current_money - penalty)

            try:
                _require_db()
                supabase.table("user_data").update({"money": new_money}).eq("user_id", str(self.user.id)).execute()
            except Exception as e:
                print(f"DB Error (NPC Battle Loss): {e}")

            reason_title = "[시간 초과 패배]" if outcome == "turn_limit" else "[전투 패배]"
            reason_desc = f"지정된 **{self.max_turns}턴**이 지나 작전에 실패했습니다." if outcome == "turn_limit" else f"**{self.npc_info['name']}**에게 쓰러졌습니다."

            embed = discord.Embed(
                title=f"{reason_title} {self.user.display_name} 패배...",
                description=f"**{self.user.mention}** 님이 {reason_desc}\n\n"
                            f"**치료/패널티 지출:** `${penalty:,}`\n"
                            f"**남은 자금:** `${new_money:,}`",
                color=discord.Color.red()
            )

        log_text = "\n".join(self.logs[-8:])
        embed.add_field(name="최종 전투 기록", value=f"```diff\n{log_text}\n```", inline=False)

        if self.user.avatar:
            embed.set_thumbnail(url=self.user.avatar.url)

        # 전투 종료 메시지 업데이트
        await interaction.edit_original_response(embed=embed, view=self)

        # 승패와 상관없이, 그리고 전투가 어떤 결과로 끝났든 무조건 쿨타임 즉시 초기화
        if self.user.id in npc_cooldowns:
            del npc_cooldowns[self.user.id]

@bot.tree.command(name="전투", description="[조직원 전용] NPC 이름을 직접 입력하여 1:1 턴제 전투를 시작합니다.")
@app_commands.describe(npc_name="전투를 벌일 NPC 이름 (골목길 깡패, 조직의 배신자, 암시장 악덕 거상, 지명수배자 보스)")
async def fight_npc(interaction: discord.Interaction, npc_name: str):
    if not await check_maintenance(interaction): return
    if not await check_membership(interaction): return
    if await check_jail(interaction): return

    user = interaction.user

    # 쿨타임 체크 (20초)
    now = datetime.now()
    if user.id in npc_cooldowns:
        remaining = int((npc_cooldowns[user.id] - now).total_seconds())
        if remaining > 0:
            await interaction.response.send_message(f"부상에서 회복 중입니다. **{remaining}초** 뒤에 다시 전투할 수 있습니다.", ephemeral=True)
            return

    npc_cooldowns[user.id] = now + timedelta(seconds=NPC_COOLDOWN_SEC)

    # 텍스트 입력값과 NPC 목록 매칭 확인
    target_npc = NPC_DATA.get(npc_name.strip())
    if not target_npc:
        # 등록된 NPC 이름을 알려주는 에러 메시지 반환
        available_npcs = ", ".join([f"`{name}`" for name in NPC_DATA.keys()])
        await interaction.response.send_message(
            f"**[{npc_name}]** 은(는) 존재하지 않는 NPC입니다.\n"
            f"사용 가능한 NPC 목록: {available_npcs}",
            ephemeral=True
        )
        return

    await interaction.response.defer()

    # 유저 전투 스탯 계산 (명예 + 보유 장비)
    user_stats = get_user_data(user.id)
    inventory = user_stats.get("inventory", {})
    base_honor = user_stats.get("honor", 0)

    user_combat_stats = compute_combat_stats(base_honor, inventory)

    view = NPCBattleView(user=user, user_stats_dict=user_combat_stats, npc_info=target_npc)
    embed = view.get_embed()

    msg = await interaction.followup.send(embed=embed, view=view)
    view.message = msg


# === [조직원 전용] `/보스` 명령어 ===
@bot.tree.command(name="보스", description="[조직원 전용] 전투 후 등장한 최강의 보스와 목숨을 건 결전을 시작합니다.")
async def fight_boss(interaction: discord.Interaction):
    if not await check_maintenance(interaction): return
    if not await check_membership(interaction): return
    if await check_jail(interaction): return

    user = interaction.user
    guild_id = interaction.guild_id if interaction.guild else None

    if not guild_id or not active_boss_guilds.get(guild_id, False):
        await interaction.response.send_message(
            "현재 이 서버에 보스가 등장하지 않았습니다.\n"
            f"일반 `/전투` 승리 시 낮은 확률로 출현합니다. (어둠의 지배자 {int(BOSS_SPAWN_CHANCE * 100)}% / 태초의 파괴자 {int(ULTIMATE_BOSS_SPAWN_CHANCE * 100)}%)",
            ephemeral=True
        )
        return

    now = datetime.now()
    if user.id in npc_cooldowns:
        remaining = int((npc_cooldowns[user.id] - now).total_seconds())
        if remaining > 0:
            await interaction.response.send_message(
                f"이전 전투의 부상이 심합니다. **{remaining}초** 뒤에 도전할 수 있습니다.",
                ephemeral=True
            )
            return

    boss_kind = active_boss_guilds.get(guild_id)   # "dark" 또는 "ultimate"
    active_boss_guilds[guild_id] = None
    npc_cooldowns[user.id] = now + timedelta(seconds=BOSS_COOLDOWN_SEC)

    await interaction.response.defer()

    try:
        user_stats = get_user_data(user.id)
        honor = user_stats.get("honor", 0)
        inventory = user_stats.get("inventory", {})

        user_combat_stats = compute_combat_stats(honor, inventory)

        # 서버에 떠 있는 보스 종류에 맞는 보스와 전투 (둘 다 15턴 제한)
        if boss_kind == "ultimate":
            boss_info, boss_title = ULTIMATE_BOSS, "[멸망의 전조] 태초의 파괴자 토벌전"
        else:
            boss_info, boss_title = HIDDEN_BOSS, "[히든보스] 어둠의 지배자 토벌전"
        view = NPCBattleView(user=user, user_stats_dict=user_combat_stats, npc_info=boss_info, max_turns=15)
        embed = view.get_embed()
        embed.title = boss_title

        view.message = await interaction.followup.send(embed=embed, view=view)
    except Exception as e:
        print(f"Boss summon error: {e}")
        active_boss_guilds[guild_id] = boss_kind   # 오류 시 보스를 원래 종류 그대로 복구
        npc_cooldowns.pop(user.id, None)
        await interaction.followup.send("보스전을 시작하는 중 오류가 발생했습니다. 보스는 그대로 남아 있으니 다시 시도해주세요.", ephemeral=True)

# === [조직원 전용] `/네임드` 명령어 ===
named_cooldowns = {}

@bot.tree.command(name="네임드", description="[조직원 전용] 강화된 네임드 몬스터에 도전합니다. (명예 조건 / 고유 전리품)")
@app_commands.describe(monster="도전할 네임드 몬스터")
@app_commands.choices(monster=[
    app_commands.Choice(name=f"{m['name']} (명예 {m['min_honor']:,}+)", value=k) for k, m in NAMED_MONSTERS.items()
])
async def fight_named(interaction: discord.Interaction, monster: app_commands.Choice[str]):
    if not await check_maintenance(interaction): return
    if not await check_membership(interaction): return
    if await check_jail(interaction): return

    user = interaction.user
    target = NAMED_MONSTERS.get(monster.value)
    if not target:
        await interaction.response.send_message("존재하지 않는 네임드 몬스터입니다.", ephemeral=True)
        return

    user_stats = get_user_data(user.id)
    honor = user_stats.get("honor", 0)
    if honor < target["min_honor"]:
        await interaction.response.send_message(
            f"**{target['name']}** 은(는) 명예 **{target['min_honor']:,}pt** 이상이어야 도전할 수 있습니다. (현재 {honor:,}pt)",
            ephemeral=True
        )
        return

    now = datetime.now()
    if user.id in named_cooldowns:
        remaining = int((named_cooldowns[user.id] - now).total_seconds())
        if remaining > 0:
            await interaction.response.send_message(f"네임드 사냥 후 숨을 고르는 중입니다. **{remaining}초** 뒤에 다시 도전할 수 있습니다.", ephemeral=True)
            return
    named_cooldowns[user.id] = now + timedelta(seconds=NAMED_COOLDOWN_SEC)

    await interaction.response.defer()

    stats = compute_combat_stats(honor, user_stats.get("inventory", {}))
    view = NPCBattleView(user=user, user_stats_dict=stats, npc_info=target, max_turns=target["max_turns"])
    embed = view.get_embed()
    embed.title = f"[네임드 전투] {target['name']} - 턴 {view.turn_count} / {view.max_turns}"
    view.message = await interaction.followup.send(embed=embed, view=view)

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
            await interaction.followup.send(f"피로를 푸는 중입니다. **{remaining}초** 뒤에 다시 일할 수 있습니다.", ephemeral=True)
            return

    parttime_cooldowns[user.id] = now + timedelta(seconds=10)

    jobs = [
        {"title": "편의점 야간 알바", "reward": (100, 250)},
        {"title": "물류 센터 상하차", "reward": (200, 350)},
        {"title": "도시 카페 서빙", "reward": (120, 280)},
        {"title": "중앙 광장 청소", "reward": (100, 200)}
    ]
    
    selected_job = random.choice(jobs)
    earned_money = random.randint(*selected_job["reward"])

    update_user_stats(user.id, earned_money, 0)
    user_stats = get_user_data(user.id)

    # 일일 의뢰 집계 (아르바이트)
    daily_msg = ""
    try:
        daily_blob = get_title_blob(user_stats)
        daily_msg = daily_record(daily_blob, "parttime", user_stats)
        save_title_blob(user.id, daily_blob)
    except Exception as e:
        print(f"DB Error (parttime daily quest): {e}")
        daily_msg = ""

    embed = discord.Embed(
        title=f"[{selected_job['title']}] 완료",
        description=f"**{user.mention}** 님이 정직한 노동으로 자금을 벌었습니다!\n\n"
                    f"**획득 급여:** `${earned_money:,}`\n"
                    f"**현재 보유 자금:** `${user_stats['money']:,}`{daily_msg}",
        color=discord.Color.blue()
    )
    if user.avatar:
        embed.set_thumbnail(url=user.avatar.url)

    await interaction.followup.send(embed=embed)

# ==========================================
# 도박 시스템 (카지노)
# ==========================================
GAMBLE_MIN_BET = 1000
GAMBLE_MAX_BET = 50000
GAMBLE_COOLDOWN = 3
COIN_WIN_RATE = 0.48

SLOT_SYMBOLS = [
    ("♣", 30, 3),
    ("♦", 25, 5),
    ("♥", 20, 8),
    ("★", 10, 20),
    ("7", 5, 100),
]
SLOT_PAIR_MULT = 1.0
SLOT_JACKPOT_SYMBOL = "7"

BLACKJACK_PAYOUT = {"blackjack": 2.5, "win": 2.0, "push": 1.0, "lose": 0}

gamble_cooldowns = {}
gamble_locks = {}
active_blackjack = {}

class InsufficientFunds(Exception):
    pass

def _gamble_lock(user_id: int) -> asyncio.Lock:
    lock = gamble_locks.get(user_id)
    if lock is None:
        lock = gamble_locks[user_id] = asyncio.Lock()
    return lock

def fmt_net(n: int) -> str:
    return f"{'+' if n >= 0 else '-'}${abs(n):,}"

def settle_gamble(user_id: int, bet: int, payout: int, info: dict = None, prepaid: bool = False, jackpot: bool = False):
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
    daily_msg = daily_record(blob, "gamble", {**info, "money": new_money})   # 일일 의뢰 집계 (도박 1판)
    new_ids = evaluate_new_titles(blob, {**info, "money": new_money})

    res = supabase.table("user_data").update({"money": new_money, "data": blob}).eq("user_id", str(user_id)).execute()
    _check_updated(res, "settle_gamble")
    if daily_msg:
        _daily_notices[user_id] = daily_msg   # 저장에 성공했을 때만 안내 문구를 남김
    return new_money, new_ids

def take_bet(user_id: int, bet: int) -> int:
    _require_db()
    info = get_user_data(user_id)
    money = int(info.get("money") or 0)
    if money < bet:
        raise InsufficientFunds(money)
    res = supabase.table("user_data").update({"money": money - bet}).eq("user_id", str(user_id)).execute()
    _check_updated(res, "take_bet")
    return money - bet

async def gamble_precheck(interaction: discord.Interaction) -> bool:
    if not await check_maintenance(interaction): return False
    if await check_jail(interaction): return False

    now = datetime.now()
    next_ok = gamble_cooldowns.get(interaction.user.id)
    if next_ok and now < next_ok:
        remaining = int((next_ok - now).total_seconds()) + 1
        await interaction.response.send_message(f"잠시 후 다시 베팅할 수 있습니다. (**{remaining}초**)", ephemeral=True)
        return False
    gamble_cooldowns[interaction.user.id] = now + timedelta(seconds=GAMBLE_COOLDOWN)
    return True

async def _safe_edit(interaction: discord.Interaction, **kwargs):
    try:
        await interaction.edit_original_response(**kwargs)
    except discord.HTTPException:
        pass

def spin_slots() -> list:
    return random.choices([s[0] for s in SLOT_SYMBOLS], weights=[s[1] for s in SLOT_SYMBOLS], k=3)

def slot_multiplier(reels: list) -> float:
    if reels[0] == reels[1] == reels[2]:
        for symbol, _, mult in SLOT_SYMBOLS:
            if symbol == reels[0]:
                return float(mult)
    if len(set(reels)) == 2:
        return SLOT_PAIR_MULT
    return 0.0

def slot_rtp() -> float:
    total_w = sum(w for _, w, _ in SLOT_SYMBOLS)
    rtp = 0.0
    for a in SLOT_SYMBOLS:
        for b in SLOT_SYMBOLS:
            for c in SLOT_SYMBOLS:
                p = (a[1] / total_w) * (b[1] / total_w) * (c[1] / total_w)
                rtp += p * slot_multiplier([a[0], b[0], c[0]])
    return rtp

def slot_board(reels: list) -> str:
    return "┃ " + " ┃ ".join(reels) + " ┃"

CARD_RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]
CARD_SUITS = ["♠", "♥", "♦", "♣"]

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
    while total > 21 and aces:
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

class BlackjackView(GuardedView):
    holds_command_lock = True   # 블랙잭 테이블이 끝날 때까지 다른 명령어 실행 차단

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
        embed = discord.Embed(title="[블랙잭]", color=colors[outcome])

        dealer_text = (f"{hand_str(self.dealer)}  (합계 **{hand_value(self.dealer)}**)" if reveal
                       else f"{card_str(self.dealer[0])} **[?]**")
        embed.add_field(name=f"{self.owner.display_name}의 패 (합계 {hand_value(self.player)})", value=hand_str(self.player), inline=False)
        embed.add_field(name="딜러의 패", value=dealer_text, inline=False)

        if outcome is None:
            embed.description = f"베팅금 **${self.bet:,}**\n**히트**(카드 받기) 또는 **스탠드**(멈추기)를 선택하세요. (60초 이내)"
        else:
            if note == "timeout":
                msg = "**시간 초과** — 자동으로 패배 처리되었습니다."
            elif outcome == "blackjack":
                msg = "**블랙잭!** 3:2 배당으로 지급됩니다."
            elif outcome == "win":
                msg = "**승리!**" + (" 딜러가 버스트했습니다." if hand_value(self.dealer) > 21 else "")
            elif outcome == "push":
                msg = "**무승부** — 베팅금이 그대로 돌아옵니다."
            else:
                msg = "**버스트!** 21을 초과했습니다." if hand_value(self.player) > 21 else "**패배**"
            net = payout - self.bet
            embed.description = (f"{msg}\n\n베팅금: `${self.bet:,}`\n지급액: `${payout:,}`\n"
                                 f"손익: `{fmt_net(net)}`\n보유 자금: `${money:,}`{format_new_titles(new_ids or [])}")

        embed.set_footer(text="딜러는 16 이하에서 카드를 받고 17 이상에서 멈춥니다 · 블랙잭 3:2")
        if self.owner.avatar:
            embed.set_thumbnail(url=self.owner.avatar.url)
        return embed

    async def finish(self, edit, outcome: str, note: str = ""):
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
                title="[블랙잭 정산 오류]",
                description="데이터베이스 오류로 결과를 정산하지 못했습니다. 관리자에게 문의해주세요.",
                color=discord.Color.red()), view=None)
            return

        final_embed = self.build_embed(reveal=True, outcome=outcome, payout=payout,
                                       money=new_money, new_ids=new_ids, note=note)
        final_embed.description = (final_embed.description or "") + take_daily_notice(self.owner.id)
        await edit(embed=final_embed, view=None)

    async def dealer_turn(self, edit):
        while hand_value(self.dealer) < 17:
            self.dealer.append(self.deck.pop())
        await self.finish(edit, blackjack_outcome(self.player, self.dealer))

    async def _guard(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner.id:
            await interaction.response.send_message("본인의 블랙잭 테이블만 조작할 수 있습니다.", ephemeral=True)
            return False
        await interaction.response.defer()
        return not (self.finished or self.busy)

    @discord.ui.button(label="히트 (카드 받기)", style=discord.ButtonStyle.primary)
    async def hit(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction): return
        self.busy = True
        try:
            self.player.append(self.deck.pop())
            value = hand_value(self.player)
            if value > 21:
                await self.finish(interaction.edit_original_response, "lose")
            elif value == 21:
                await self.dealer_turn(interaction.edit_original_response)
            else:
                await interaction.edit_original_response(embed=self.build_embed(), view=self)
        finally:
            self.busy = False

    @discord.ui.button(label="스탠드 (멈추기)", style=discord.ButtonStyle.secondary)
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
# /도박 명령어 그룹
# ==========================================
gamble_group = app_commands.Group(name="도박", description="[카지노] 자금을 걸고 도박을 즐깁니다.")
bot.tree.add_command(gamble_group)

BET_DESC = f"베팅할 금액 (${GAMBLE_MIN_BET:,} ~ ${GAMBLE_MAX_BET:,})"

@gamble_group.command(name="슬롯", description="슬롯머신을 돌립니다. (3개 일치 고배당 / 2개 일치 본전)")
@app_commands.describe(베팅금=BET_DESC)
async def gamble_slots(interaction: discord.Interaction, 베팅금: app_commands.Range[int, GAMBLE_MIN_BET, GAMBLE_MAX_BET]):
    if not await gamble_precheck(interaction): return

    user = interaction.user
    bet = 베팅금

    money = int(get_user_data(user.id).get("money") or 0)
    if money < bet:
        gamble_cooldowns.pop(user.id, None)
        await interaction.response.send_message(f"자금이 부족합니다! (보유 자금: ${money:,})", ephemeral=True)
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
        await interaction.edit_original_response(content="자금이 부족합니다!")
        return
    except Exception as e:
        print(f"DB Error (slots): {e}")
        gamble_cooldowns.pop(user.id, None)
        await interaction.edit_original_response(content="데이터베이스 오류로 베팅이 취소되었습니다. (자금은 차감되지 않았습니다)")
        return

    spinning = discord.Embed(title="[슬롯머신]", description=slot_board(["?", "?", "?"]) + f"\n\n베팅금 `${bet:,}`", color=discord.Color.dark_gold())
    await _safe_edit(interaction, embed=spinning)
    await asyncio.sleep(0.8)
    spinning.description = slot_board([reels[0], "?", "?"]) + f"\n\n베팅금 `${bet:,}`"
    await _safe_edit(interaction, embed=spinning)
    await asyncio.sleep(0.8)
    spinning.description = slot_board([reels[0], reels[1], "?"]) + f"\n\n베팅금 `${bet:,}`"
    await _safe_edit(interaction, embed=spinning)
    await asyncio.sleep(0.8)

    if jackpot:
        msg, color = "**JACKPOT!!!** 7⃣7⃣7⃣ 대박이 터졌습니다!", discord.Color.gold()
    elif mult >= 2:
        msg, color = f"**{reels[0]} 3개 일치!** 배당 x{mult:g}", discord.Color.green()
    elif mult == SLOT_PAIR_MULT and mult > 0:
        msg, color = "**2개 일치** — 베팅금이 그대로 돌아옵니다.", discord.Color.blue()
    else:
        msg, color = "**꽝!** 베팅금을 잃었습니다.", discord.Color.red()

    embed = discord.Embed(
        title="[슬롯머신] 결과",
        description=(f"{slot_board(reels)}\n\n{msg}\n\n베팅금: `${bet:,}`\n지급액: `${payout:,}`\n"
                     f"손익: `{fmt_net(payout - bet)}`\n보유 자금: `${new_money:,}`{format_new_titles(new_ids)}{take_daily_notice(user.id)}"),
        color=color
    )
    if user.avatar:
        embed.set_thumbnail(url=user.avatar.url)
    await _safe_edit(interaction, embed=embed)

@gamble_group.command(name="동전", description="동전 던지기에 베팅합니다. (승리 시 2배)")
@app_commands.describe(베팅금=BET_DESC, 선택="동전의 어느 면에 걸까요?")
@app_commands.choices(선택=[
    app_commands.Choice(name="앞면", value="front"),
    app_commands.Choice(name="뒷면", value="back")
])
async def gamble_coin(interaction: discord.Interaction, 베팅금: app_commands.Range[int, GAMBLE_MIN_BET, GAMBLE_MAX_BET], 선택: app_commands.Choice[str]):
    if not await gamble_precheck(interaction): return

    user = interaction.user
    bet = 베팅금

    money = int(get_user_data(user.id).get("money") or 0)
    if money < bet:
        gamble_cooldowns.pop(user.id, None)
        await interaction.response.send_message(f"자금이 부족합니다! (보유 자금: ${money:,})", ephemeral=True)
        return

    await interaction.response.defer()

    names = {"front": "앞면", "back": "뒷면"}
    won = random.random() < COIN_WIN_RATE
    result_side = 선택.value if won else ("back" if 선택.value == "front" else "front")
    payout = bet * 2 if won else 0

    try:
        async with _gamble_lock(user.id):
            new_money, new_ids = settle_gamble(user.id, bet, payout)
    except InsufficientFunds:
        gamble_cooldowns.pop(user.id, None)
        await interaction.edit_original_response(content="자금이 부족합니다!")
        return
    except Exception as e:
        print(f"DB Error (coin): {e}")
        gamble_cooldowns.pop(user.id, None)
        await interaction.edit_original_response(content="데이터베이스 오류로 베팅이 취소되었습니다. (자금은 차감되지 않았습니다)")
        return

    flipping = discord.Embed(
        title="[동전 던지기]",
        description=f"{user.mention} 님이 **{names[선택.value]}**에 `${bet:,}` 를 걸었습니다!\n\n동전이 공중에서 회전 중...",
        color=discord.Color.dark_gold()
    )
    await _safe_edit(interaction, embed=flipping)
    await asyncio.sleep(1.2)

    embed = discord.Embed(
        title="[동전 던지기] 결과",
        description=(f"결과: **{names[result_side]}**\n"
                     f"{'**적중!** 베팅금의 2배를 받았습니다.' if won else '**빗나갔습니다.** 베팅금을 잃었습니다.'}\n\n"
                     f"베팅금: `${bet:,}`\n지급액: `${payout:,}`\n"
                     f"손익: `{fmt_net(payout - bet)}`\n보유 자금: `${new_money:,}`{format_new_titles(new_ids)}{take_daily_notice(user.id)}"),
        color=discord.Color.green() if won else discord.Color.red()
    )
    if user.avatar:
        embed.set_thumbnail(url=user.avatar.url)
    await _safe_edit(interaction, embed=embed)

@gamble_group.command(name="블랙잭", description="딜러와 블랙잭 승부를 겨룹니다. (블랙잭 3:2 / 승리 2배)")
@app_commands.describe(베팅금=BET_DESC)
async def gamble_blackjack(interaction: discord.Interaction, 베팅금: app_commands.Range[int, GAMBLE_MIN_BET, GAMBLE_MAX_BET]):
    if not await gamble_precheck(interaction): return

    user = interaction.user
    bet = 베팅금

    if user.id in active_blackjack:
        gamble_cooldowns.pop(user.id, None)
        await interaction.response.send_message("이미 진행 중인 블랙잭 테이블이 있습니다! 먼저 끝내주세요.", ephemeral=True)
        return

    money = int(get_user_data(user.id).get("money") or 0)
    if money < bet:
        gamble_cooldowns.pop(user.id, None)
        await interaction.response.send_message(f"자금이 부족합니다! (보유 자금: ${money:,})", ephemeral=True)
        return

    await interaction.response.defer()

    try:
        async with _gamble_lock(user.id):
            take_bet(user.id, bet)
    except InsufficientFunds:
        gamble_cooldowns.pop(user.id, None)
        await interaction.edit_original_response(content="자금이 부족합니다!")
        return
    except Exception as e:
        print(f"DB Error (blackjack take_bet): {e}")
        gamble_cooldowns.pop(user.id, None)
        await interaction.edit_original_response(content="데이터베이스 오류로 베팅이 취소되었습니다. (자금은 차감되지 않았습니다)")
        return

    view = BlackjackView(owner=user, bet=bet)
    active_blackjack[user.id] = view

    if is_blackjack(view.player) or is_blackjack(view.dealer):
        view.message = await interaction.edit_original_response(embed=view.build_embed())
        await view.finish(interaction.edit_original_response, blackjack_outcome(view.player, view.dealer))
    else:
        view.message = await interaction.edit_original_response(embed=view.build_embed(), view=view)

@gamble_group.command(name="규칙", description="카지노 규칙·배당표와 나의 도박 기록을 확인합니다.")
async def gamble_rules(interaction: discord.Interaction):
    if not await check_maintenance(interaction): return

    s = get_title_blob(get_user_data(interaction.user.id))["stats"]
    paytable = "\n".join(f"{e}{e}{e} → **x{m}**" for e, _, m in SLOT_SYMBOLS)
    net = s["gamble_net"]

    embed = discord.Embed(
        title="[칼렌데 카지노 규칙]",
        description=f"베팅 한도: **${GAMBLE_MIN_BET:,} ~ ${GAMBLE_MAX_BET:,}** · 베팅 간격 {GAMBLE_COOLDOWN}초\n"
                    f"도박은 평균적으로 자금이 줄어듭니다. 감옥에서는 이용할 수 없습니다.",
        color=discord.Color.dark_gold()
    )
    embed.add_field(
        name="`/도박 슬롯`",
        value=f"{paytable}\n같은 그림 **2개** → 베팅금 반환 / 모두 다르면 꽝\n환수율 약 **{slot_rtp() * 100:.1f}%**",
        inline=False
    )
    embed.add_field(name="`/도박 동전`", value=f"앞면/뒷면 중 선택 · 적중 시 **2배** (승률 **{COIN_WIN_RATE * 100:.0f}%**)", inline=False)
    embed.add_field(
        name="`/도박 블랙잭`",
        value="21에 가까운 쪽이 승리 (21 초과 시 버스트)\n승리 **2배** · 블랙잭(처음 2장 21) **2.5배** · 무승부 베팅금 반환\n딜러는 16 이하 히트 / 17 이상 스탠드 · 60초 내 미선택 시 패배",
        inline=False
    )
    embed.add_field(
        name="나의 도박 기록",
        value=f"총 `{s['gamble_played']}`판 / 승리 `{s['gamble_won']}`판 / 누적 베팅 `${s['gamble_wagered']:,}`\n누적 손익 `{fmt_net(net)}` / 잭팟 `{s['jackpot']}`회",
        inline=False
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

# 쿨타임 관리를 위한 사전
duel_cooldowns = {}
npc_cooldowns = {}

# === 턴제 PVP 결투 시스템 (버튼 선택형) ===
class DuelBattleView(GuardedView):
    holds_command_lock = True   # 결투가 끝날 때까지 (신청한 사람의) 다른 명령어 실행 차단

    def __init__(self, challenger: discord.Member, target: discord.Member, bet: int, c_stats: dict, t_stats: dict):
        super().__init__(timeout=30)
        self.challenger = challenger
        self.target = target
        self.bet = bet
        
        # 스탯 및 전투 데이터 초기화
        self.players = {
            challenger.id: {"member": challenger, "hp": c_stats["hp"], "max_hp": c_stats["hp"], "atk": c_stats["atk"], "def": c_stats["def"], "guard": False},
            target.id: {"member": target, "hp": t_stats["hp"], "max_hp": t_stats["hp"], "atk": t_stats["atk"], "def": t_stats["def"], "guard": False}
        }
        
        self.current_turn_id = challenger.id  # 도전자부터 시작
        self.turn_count = 1
        self.logs = [f"{challenger.display_name} 님과 {target.display_name} 님의 결투가 시작되었습니다!"]
        self.message = None
        self.winner = None

    def get_embed(self) -> discord.Embed:
        c_p = self.players[self.challenger.id]
        t_p = self.players[self.target.id]
        
        current_turn_user = self.players[self.current_turn_id]["member"]

        embed = discord.Embed(
            title=f"[패밀리 결투 아레나] 턴 {self.turn_count}",
            description=f"현재 차례: **{current_turn_user.mention}** 님의 행동을 기다리는 중... (제한 시간 30초)",
            color=discord.Color.dark_red()
        )

        embed.add_field(
            name=f"{c_p['member'].display_name}",
            value=f"체력 : {c_p['hp']} / {c_p['max_hp']}\n공격력 : {c_p['atk']}\n방어력 : {c_p['def']}",
            inline=True
        )
        embed.add_field(
            name=f"{t_p['member'].display_name}",
            value=f"체력 : {t_p['hp']} / {t_p['max_hp']}\n공격력 : {t_p['atk']}\n방어력 : {t_p['def']}",
            inline=True
        )

        log_text = "\n".join(self.logs[-6:])  # 최근 로그 6개만 표시
        embed.add_field(
            name="전투 현황 로그",
            value=f"```diff\n{log_text}\n```",
            inline=False
        )
        return embed

    async def process_action(self, interaction: discord.Interaction, action_type: str):
        if interaction.user.id != self.current_turn_id:
            await interaction.response.send_message("지금은 당신의 차례가 아닙니다!", ephemeral=True)
            return

        await interaction.response.defer()

        attacker_id = self.current_turn_id
        defender_id = self.target.id if attacker_id == self.challenger.id else self.challenger.id
        
        att = self.players[attacker_id]
        defn = self.players[defender_id]

        # 방어 상태 초기화
        att["guard"] = False

        if action_type == "attack":
            # 기본 공격 계산
            damage = max(10, att["atk"] - (defn["def"] * 2 if defn["guard"] else defn["def"]) + random.randint(-15, 15))
            if defn["guard"]:
                damage = int(damage * 0.5)  # 방어 중이면 50% 경감
                self.logs.append(f"+ {defn['member'].display_name}이(가) 방어 자세를 취해 피해를 줄였습니다!")
            
            defn["hp"] = max(0, defn["hp"] - damage)
            self.logs.append(f"+ {att['member'].display_name}의 공격! {defn['member'].display_name}의 체력 - {damage}")

        elif action_type == "guard":
            att["guard"] = True
            self.logs.append(f"+ {att['member'].display_name}이(가) 단단히 방어 태세를 갖췄습니다. (다음 피격 대미지 50% 감소)")

        # 승리 조건 체크
        if defn["hp"] <= 0:
            self.winner = att["member"]
            await self.end_battle(interaction)
            return

        # 턴 넘기기
        self.current_turn_id = defender_id
        if self.current_turn_id == self.challenger.id:
            self.turn_count += 1

        await self.message.edit(embed=self.get_embed(), view=self)

    @discord.ui.button(label="공격하기", style=discord.ButtonStyle.danger)
    async def attack_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.process_action(interaction, "attack")

    @discord.ui.button(label="방어하기", style=discord.ButtonStyle.primary)
    async def guard_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.process_action(interaction, "guard")

    async def end_battle(self, interaction: discord.Interaction):
        self.stop()
        
        loser = self.target if self.winner == self.challenger else self.challenger
        w_stats = get_user_data(self.winner.id)
        l_stats = get_user_data(loser.id)

        new_w_honor = w_stats["honor"] + self.bet
        new_l_honor = max(0, l_stats["honor"] - self.bet)

        try:
            _require_db()
            supabase.table("user_data").update({"honor": new_w_honor}).eq("user_id", str(self.winner.id)).execute()
            supabase.table("user_data").update({"honor": new_l_honor}).eq("user_id", str(loser.id)).execute()
        except Exception as e:
            print(f"DB Error (turn duel end): {e}")

        embed = discord.Embed(
            title="[결투 종료: 승패 결정]",
            description=f"피비린내 나는 공방 끝에 **{self.winner.mention}** 님이 승리했습니다!\n\n"
                        f"**명예 변동:**\n"
                        f"• {self.winner.mention} `+{self.bet} pt` (총 {new_w_honor} pt)\n"
                        f"• {loser.mention} `-{self.bet} pt` (총 {new_l_honor} pt)",
            color=discord.Color.gold()
        )
        log_text = "\n".join(self.logs[-8:])
        embed.add_field(name="최종 전투 기록", value=f"```diff\n{log_text}\n```", inline=False)

        if self.winner.avatar:
            embed.set_thumbnail(url=self.winner.avatar.url)

        for child in self.children:
            child.disabled = True

        await interaction.edit_original_response(embed=embed, view=self)

    async def on_timeout(self):
        if self.winner:
            return
        # 시간 초과 시 먼저 누른 사람의 승리 처리 혹은 무승부 처리
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(content="결투 시간이 초과되어 무승부로 종료되었습니다.", view=self)
            except:
                pass


# === /결투 슬래시 명령어 ===
duel_cooldowns = {}
npc_cooldowns = {}

@bot.tree.command(name="결투", description="[조직원 전용] 버튼을 눌러 턴제로 진행하는 1:1 결투를 신청합니다.")
@app_commands.describe(member="결투를 신청할 상대 조직원", 베팅명예=f"걸고 싸울 명예 포인트 (1~{DUEL_MAX_BET}pt, 기본 10pt)")
async def duel_member(interaction: discord.Interaction, member: discord.Member, 베팅명예: app_commands.Range[int, 1, DUEL_MAX_BET] = 10):
    if not await check_maintenance(interaction): return
    if not await check_membership(interaction): return
    if await check_jail(interaction): return

    user = interaction.user

    if member.id == user.id:
        await interaction.response.send_message("자기 자신과는 결투할 수 없습니다!", ephemeral=True)
        return
    if member.bot:
        await interaction.response.send_message("봇과는 결투할 수 없습니다.", ephemeral=True)
        return

    now = datetime.now()
    if user.id in duel_cooldowns:
        remaining = int((duel_cooldowns[user.id] - now).total_seconds())
        if remaining > 0:
            await interaction.response.send_message(f"결투 여파로 지쳐 있습니다. **{remaining}초** 뒤에 다시 도전할 수 있습니다.", ephemeral=True)
            return

    duel_cooldowns[user.id] = now + timedelta(seconds=120)

    mafia_role = discord.utils.get(interaction.guild.roles, name="조직원")
    if not mafia_role or mafia_role not in member.roles:
        await interaction.response.send_message(f"{member.mention} 님은 조직원이 아니므로 결투할 수 없습니다.", ephemeral=True)
        return

    if member.id in jail_list and now < jail_list[member.id]:
        await interaction.response.send_message(f"{member.mention} 님은 현재 감옥에 수감 중이어서 결투할 수 없습니다.", ephemeral=True)
        return

    user_stats = get_user_data(user.id)
    target_stats = get_user_data(member.id)

    if user_stats["honor"] < 베팅명예:
        await interaction.response.send_message(f"보유한 명예 포인트가 부족합니다! (현재 명예: {user_stats['honor']} pt)", ephemeral=True)
        return
    if target_stats["honor"] < 베팅명예:
        await interaction.response.send_message(f"상대방의 명예 포인트가 부족하여 해당 금액으로 결투할 수 없습니다.", ephemeral=True)
        return

    await interaction.response.defer()

    # 장비 스탯 계산 함수
    def get_combat_stats(inventory: dict, base_honor: int):
        return compute_combat_stats(base_honor, inventory)

    c_stats = get_combat_stats(user_stats.get("inventory", {}), user_stats["honor"])
    t_stats = get_combat_stats(target_stats.get("inventory", {}), target_stats["honor"])

    view = DuelBattleView(challenger=user, target=member, bet=베팅명예, c_stats=c_stats, t_stats=t_stats)
    embed = view.get_embed()

    msg = await interaction.followup.send(embed=embed, view=view)
    view.message = msg
    
class WeaponShopSelect(discord.ui.Select):
    def __init__(self, owner: discord.Member):
        self.owner = owner
        options = [
            discord.SelectOption(
                label=item["name"],
                description=f"가격: ${item['price']:,} | {item['desc']}",
                value=item["id"]
            ) for item in WEAPON_SHOP_ITEMS
        ]
        super().__init__(placeholder="구매할 물품을 선택하세요...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        if interaction.user != self.owner:
            await interaction.followup.send("다른 사람의 상점 메뉴는 조작할 수 없습니다.", ephemeral=True)
            return

        user = interaction.user
        user_stats = get_user_data(user.id)
        current_money = user_stats.get("money", 0)
        inventory = user_stats.get("inventory", {})

        target_item = next((item for item in WEAPON_SHOP_ITEMS if item["id"] == self.values[0]), None)
        if not target_item:
            await interaction.followup.send("존재하지 않는 아이템입니다.", ephemeral=True)
            return

        item_name = target_item["name"]
        item_price = target_item["price"]

        if current_money < item_price:
            await interaction.followup.send(f"자금이 부족합니다! (필요: `${item_price:,}`, 보유: `${current_money:,}`)", ephemeral=True)
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
            print(f"DB Error (weapon shop buy): {e}")
            await interaction.followup.send("구매 처리 중 DB 오류가 발생했습니다.", ephemeral=True)
            return

        embed = discord.Embed(
            title="[무기 상점] 구매 완료",
            description=f"**{user.mention}** 님이 **{item_name}**을(를) 구매하셨습니다!\n\n"
                        f"**지불 금액:** `${item_price:,}`\n"
                        f"**잔여 자금:** `${current_money:,}`\n"
                        f"**효과:** `{target_item['desc']}`",
            color=discord.Color.green()
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

class WeaponShopView(discord.ui.View):
    def __init__(self, owner: discord.Member):
        super().__init__(timeout=60)
        self.add_item(WeaponShopSelect(owner))

# === 1. 셀프 가입 버튼 뷰 ===
class SelfJoinView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="마피아 조직 입단", style=discord.ButtonStyle.danger, custom_id="join_mafia_btn")
    async def join_mafia(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.process_join(interaction, target_role_name="조직원", rank_title="마피아 조직원")

    @discord.ui.button(label="치안국 경찰 임용", style=discord.ButtonStyle.primary, custom_id="join_police_btn")
    async def join_police(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.process_join(interaction, target_role_name="경찰", rank_title="순경")

    async def process_join(self, interaction: discord.Interaction, target_role_name: str, rank_title: str):
        if MAINTENANCE_MODE and not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("현재 점검 중입니다.", ephemeral=True)
            return

        user = interaction.user
        citizen_role = discord.utils.get(interaction.guild.roles, name="시민")
        target_role = discord.utils.get(interaction.guild.roles, name=target_role_name)
        
        if not target_role:
            await interaction.response.send_message(f"서버에 '{target_role_name}' 역할이 없습니다! 관리자에게 문의하세요.", ephemeral=True)
            return

        mafia_role = discord.utils.get(interaction.guild.roles, name="조직원")
        police_role = discord.utils.get(interaction.guild.roles, name="경찰")

        if (mafia_role and mafia_role in user.roles) or (police_role and police_role in user.roles):
            await interaction.response.send_message("당신은 이미 소속(마피아 또는 경찰)이 결정되었습니다!", ephemeral=True)
            return

        try:
            if citizen_role and citizen_role in user.roles:
                await user.remove_roles(citizen_role)
            await user.add_roles(target_role)
        except discord.Forbidden:
            await interaction.response.send_message("봇의 역할 권한 순서를 올려주세요.", ephemeral=True)
            return

        user_stats = get_user_data(user.id)

        embed = discord.Embed(
            title=f"[{rank_title} 발령/입단]",
            description=f"**{user.mention}** 님이 시민의 삶을 마치고 **{target_role_name}**(으)로 새로운 활동을 시작합니다!\n\n"
                        f"**신분 변화:** `시민` `{target_role_name}`",
            color=discord.Color.gold()
        )
        if user.avatar:
            embed.set_thumbnail(url=user.avatar.url)

        await interaction.response.send_message(embed=embed)

# === 2. 탈퇴 확인/취소 버튼 뷰 ===
class LeaveConfirmView(discord.ui.View):
    def __init__(self, target_user: discord.Member):
        super().__init__(timeout=60)
        self.target_user = target_user

    @discord.ui.button(label="네, 탈퇴하겠습니다", style=discord.ButtonStyle.danger, custom_id="confirm_leave")
    async def confirm_leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.target_user:
            await interaction.response.send_message("본인의 탈퇴 확인 창만 조작할 수 있습니다.", ephemeral=True)
            return

        await interaction.response.defer()

        member = interaction.user
        mafia_role = discord.utils.get(interaction.guild.roles, name="조직원")
        police_role = discord.utils.get(interaction.guild.roles, name="경찰")
        citizen_role = discord.utils.get(interaction.guild.roles, name="시민")

        try:
            if mafia_role and mafia_role in member.roles:
                await member.remove_roles(mafia_role)
            if police_role and police_role in member.roles:
                await member.remove_roles(police_role)
                
            if citizen_role and citizen_role not in member.roles:
                await member.add_roles(citizen_role)
        except discord.Forbidden:
            await interaction.followup.send("역할 변경 권한이 없습니다.", ephemeral=True)
            return

        try:
            family_detach_user(member.id)   # 조직에서도 제거
        except Exception as e:
            print(f"Family detach error: {e}")

        try:
            delete_user_data(member.id)
        except Exception:
            pass

        mission_cooldowns.pop(member.id, None)
        parttime_cooldowns.pop(member.id, None)
        gamble_cooldowns.pop(member.id, None)
        open_table = active_blackjack.pop(member.id, None)
        if open_table:
            open_table.finished = True
            open_table.stop()

        for child in self.children:
            child.disabled = True

        embed = discord.Embed(
            title="[소속 해임 및 세이브 데이터 초기화]",
            description=(
                f"**{member.mention}** 님이 소속을 최종 탈퇴하셨습니다.\n\n"
                f"**초기화 완료:** 모든 보유 자금, 명예, 인벤토리 아이템 및 기록이 DB에서 완전히 삭제되었습니다.\n"
                f"**신분 변화:** `마피아/경찰` `시민`"
            ),
            color=discord.Color.dark_gray()
        )
        if member.avatar:
            embed.set_thumbnail(url=member.avatar.url)

        await interaction.edit_original_response(embed=embed, view=self)

    @discord.ui.button(label="취소 (소속 잔류)", style=discord.ButtonStyle.secondary, custom_id="cancel_leave")
    async def cancel_leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.target_user:
            await interaction.response.send_message("본인의 탈퇴 확인 창만 조작할 수 있습니다.", ephemeral=True)
            return

        for child in self.children:
            child.disabled = True

        embed = discord.Embed(
            title="[탈퇴 철회]",
            description="탈퇴 신청이 취소되었습니다.",
            color=discord.Color.blue()
        )
        await interaction.edit_original_response(embed=embed, view=self)

# === 3. 임무 선택 뷰 ===
class MissionSelect(discord.ui.Select):
    def __init__(self, owner: discord.Member):
        self.owner = owner
        options = [
            discord.SelectOption(
                label=m["label"],
                description=f"난이도: {m['diff']} | 성공률 {m['success_rate']}% | ${m['reward'][0]:,}~${m['reward'][1]:,} / 명예 +{m['honor']}pt / 실패 -${m['fine']:,}",
                value=key
            ) for key, m in MISSIONS.items()
        ]
        super().__init__(placeholder="수행할 비밀 임무를 선택하세요...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()

        if interaction.user != self.owner:
            await interaction.followup.send("타인의 임무 선택 창은 조작할 수 없습니다.", ephemeral=True)
            return

        user = interaction.user
        user_stats = get_user_data(user.id)
        old_rank = get_user_rank(user_stats.get("honor", 0))
        inventory = user_stats.get("inventory", {})

        drink_used = False

        selected = MISSIONS[self.values[0]]
        current_loc = user_stats.get("location", "레벤하임")

        success_bonus = 0
        money_mult = 1.0
        honor_mult = 1.0
        active_buffs = []

        for item_key, s_bonus, m_bonus, h_bonus, label in MISSION_BUFFS:
            if item_key in inventory:
                success_bonus += s_bonus
                money_mult += m_bonus
                honor_mult += h_bonus
                active_buffs.append(label)
        if drink_used:
            active_buffs.append("자양강장제 (쿨타임 초기화·1회 소모)")

        # [추가] 해당 지역에 경찰의 순찰이 진행 중인 경우 성공률 감소 (-15%)
        patrol_penalty = 0
        guild_id = interaction.guild_id if interaction.guild else None
        if guild_id and guild_id in active_patrols:
            patrol_expiry = active_patrols[guild_id].get(current_loc, 0)
            if time.time() < patrol_expiry:
                patrol_penalty = 15  # 순찰 지역이면 성공률 15% 하락
                active_buffs.append("경찰 순찰망 가동 (-15% 성공률)")    

        final_success_rate = max(1, min(98, selected["success_rate"] + success_bonus - patrol_penalty))
        is_success = random.randint(1, 100) <= final_success_rate
        buff_desc = f"\n**적용된 보너스:** {', '.join(active_buffs)}" if active_buffs else ""

        current_money = user_stats.get("money", 0)
        current_honor = user_stats.get("honor", 0)
        
        fail_msg = ""
        rank_up_msg = ""

        blob = get_title_blob(user_stats)
        s = blob["stats"]

        if is_success:
            base_money = random.randint(*selected["reward"])
            earned_money = int(base_money * money_mult)
            earned_honor = int(selected["honor"] * honor_mult)
            
            current_money += earned_money
            current_honor += earned_honor

            s["success"] += 1
            s["streak"] += 1
            s["best_streak"] = max(s["best_streak"], s["streak"])
            if self.values[0] in ("mission_3", "mission_5"):
                s["hard_success"] += 1

            new_rank = get_user_rank(current_honor)
            if old_rank != new_rank:
                rank_up_msg = f"\n\n**[조직 승급 축하]** **{old_rank}** **{new_rank}**(으)로 승급하셨습니다!"

            rank_up_msg += daily_record(blob, "mission", {**user_stats, "money": current_money, "honor": current_honor})

            new_title_ids = evaluate_new_titles(blob, {**user_stats, "money": current_money, "honor": current_honor})
            rank_up_msg += format_new_titles(new_title_ids)

            embed = discord.Embed(
                title=f"[{current_loc}] 임무 성공",
                description=f"**{user.mention}** 님이 작전을 완벽하게 완수했습니다!{buff_desc}\n\n"
                            f"**작전명:** {selected['title']}\n"
                            f"**획득 보상금:** `${earned_money:,}`\n"
                            f"**획득 명예:** `+{earned_honor} pt`\n\n"
                            f"**현재 직급:** `{new_rank}`\n"
                            f"**보유 자금:** `${current_money:,}`\n"
                            f"**총 명예:** `{current_honor} pt`{rank_up_msg}",
                color=discord.Color.green()
            )
        else:
            settings = user_stats.get("settings", {"auto_fail_guard": True})
            guard_item_key = "임무 실패 방지권"
            guard_used = False

            if settings.get("auto_fail_guard", True) and inventory.get(guard_item_key, 0) > 0:
                inventory[guard_item_key] -= 1
                if inventory[guard_item_key] <= 0:
                    del inventory[guard_item_key]
                guard_used = True

            if guard_used:
                fail_msg = "**[실패 방지권 발동]** 임무에 실패했으나, 가방에 있던 **임무 실패 방지권**이 소모되어 수배 및 벌금이 면제되었습니다!"
            else:
                fine = min(current_money, selected["fine"])
                current_money -= fine
                wanted_list[user.id] = datetime.now() + timedelta(seconds=60)
                if "치안국 위조 신분증" in inventory:
                    fail_msg = f"임무에 실패했으나 **치안국 위조 신분증** 덕분에 지명수배를 면했습니다! (벌금 `-${fine:,}`)"
                    wanted_list.pop(user.id, None)
                else:
                    fail_msg = f"**작전 실패!** 현장에 흔적이 남아 **1분간 경찰 지명수배 상태**가 됩니다! (벌금 `-${fine:,}`)"

            s["fail"] += 1
            s["streak"] = 0
            new_title_ids = evaluate_new_titles(blob, user_stats)

            current_rank = get_user_rank(current_honor)
            embed = discord.Embed(
                title=f"[{current_loc}] 임무 실패",
                description=f"**{user.mention}** 님이 작전 도중 문제가 발생했습니다!{buff_desc}\n\n"
                            f"**작전명:** {selected['title']}\n"
                            f"{fail_msg}\n\n"
                            f"**현재 직급:** `{current_rank}`\n"
                            f"**보유 자금:** `${current_money:,}`\n"
                            f"**총 명예:** `{current_honor} pt`{format_new_titles(new_title_ids)}",
                color=discord.Color.red()
            )

        try:
            _require_db()
            supabase.table("user_data").update({
                "money": current_money,
                "honor": current_honor,
                "inventory": inventory,
                "data": blob
            }).eq("user_id", str(user.id)).execute()
        except Exception as e:
            print(f"DB Error (mission update): {e}")
            await interaction.followup.send("DB 저장 중 오류가 발생했습니다.", ephemeral=True)
            return

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
    embed = discord.Embed(title="[패밀리 입단 안내]", description="아래 버튼을 눌러 패밀리에 합류하세요.", color=discord.Color.gold())
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
    await interaction.response.defer()

    # [추가] 경찰은 조직원 임무를 수행할 수 없음
    police_role = discord.utils.get(interaction.guild.roles, name="경찰")
    if police_role and police_role in interaction.user.roles:
        await interaction.response.send_message("치안국 경찰은 범죄 조직의 비밀 임무를 수행할 수 없습니다!", ephemeral=True)
        return

    user = interaction.user
    user_stats = get_user_data(user.id)
    current_loc = user_stats.get("location", "레벤하임")


    embed = discord.Embed(
        title=f"[{current_loc}] 비밀 임무 작전 하달",
        description=f"**{user.mention}** 님, 수행할 작전을 선택해 주십시오.",
        color=discord.Color.dark_purple()
    )
    if user.avatar:
        embed.set_thumbnail(url=user.avatar.url)

    await interaction.followup.send(embed=embed, view=MissionView(owner=user))

# === [공용] `/프로필` 명령어 (이미지 증명서 - 아날로그 종이 테마) ===
# 필요한 파일(봇 파일과 같은 폴더 또는 assets 폴더): paper_background.jpg, font_1.ttf (또는 font_fallback.otf)
PROFILE_BG_NAMES = ("paper_background.jpg",)
PROFILE_FONT_NAMES = ("font_1.ttf", "font.ttf", "font_fallback.otf")   # 앞에서부터 시도, 안 되면 다음 폰트
PROFILE_AREA = (70, 60, 468, 330)      # 배경(543x394) 안쪽 점선 테두리 안에서 글씨를 쓸 영역 (x1, y1, x2, y2)
PROFILE_SCALE = 2                      # 배경이 작아서 2배로 키워 그림 (글씨가 선명해짐)
PROFILE_INK = (60, 40, 20)             # 짙은 세피아 잉크색
PROFILE_MUTED = (125, 98, 66)          # 라벨·하단 문구용 옅은 갈색
PROFILE_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

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

def make_profile_image(kind: str, rows: list, user_id: int, avatar: Image.Image) -> io.BytesIO:
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
    f_label, f_value = _profile_font(26), _profile_font(34)
    label_x, value_x = x1 + 30, x1 + 150
    line_end = photo_x - 30
    for i, (label, value) in enumerate(rows):
        y = top + i * 58
        draw.text((label_x, y + 6), label, fill=PROFILE_MUTED, font=f_label)
        draw.text((value_x, y), _fit_text(draw, value, f_value, line_end - value_x), fill=PROFILE_INK, font=f_value)
        draw.line((label_x, y + 50, line_end, y + 50), fill=(170, 145, 110), width=1)

    # [4] 붉은 도장 (사진 아래쪽에 살짝 걸치게)
    stamp = Image.new("RGBA", (170, 170), (0, 0, 0, 0))
    sd = ImageDraw.Draw(stamp)
    red = (170, 40, 40, 210)
    sd.ellipse((6, 6, 164, 164), outline=red, width=6)
    sd.ellipse((20, 20, 150, 150), outline=red, width=2)
    sd.text((85, 85), "승인", fill=red, font=_profile_font(52), anchor="mm")
    stamp = stamp.rotate(-14, resample=Image.Resampling.BICUBIC)
    paper.alpha_composite(stamp, (photo_x - 20, top + avatar.height + 12 - 50))

    # [5] 하단 발급 문구
    footer = f"발급처  패밀리 GM 통합 정보국   ·   발급일  {datetime.now():%Y-%m-%d}"
    draw.text((cx, y2 - 6), footer, fill=PROFILE_MUTED, font=_profile_font(22), anchor="mb")

    buf = io.BytesIO()
    paper.convert("RGB").save(buf, format="PNG")
    buf.seek(0)
    return buf

@bot.tree.command(name="프로필", description="[공용] 본인 또는 다른 유저의 아날로그 증명서를 이미지로 발급합니다.")
@app_commands.describe(member="조회할 유저 (비우면 본인)")
async def profile_image_command(interaction: discord.Interaction, member: discord.Member = None):
    if not await check_maintenance(interaction): return
    await interaction.response.defer()   # 이미지 생성 시간이 걸릴 수 있어 먼저 응답 예약

    target = member or interaction.user
    data = get_user_data(target.id)

    role_names = {r.name for r in target.roles}
    kind = "police" if "경찰" in role_names else "mafia" if "조직원" in role_names else "citizen"
    honor = data.get("honor", 0)

    rows = [
        ("성 명", _clean_text(target.display_name) or _clean_text(target.name) or "무명"),
        ("직 급", "시민" if kind == "citizen" else _clean_text(get_user_rank(honor, is_police=(kind == "police")))),
        ("칭 호", _clean_text(get_equipped_title_name(data))),
        ("거 점", _clean_text(data.get("location", "레벤하임"))),
        ("자 금", f"${data.get('money', 0):,}"),
        ("명 예", f"{honor:,} pt"),
    ]

    try:
        avatar = await _load_avatar(target)
        buf = await asyncio.to_thread(make_profile_image, kind, rows, target.id, avatar)
    except Exception as e:
        print(f"Profile image error: {e}")
        await interaction.followup.send("증명서를 발급하는 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.", ephemeral=True)
        return

    await interaction.followup.send(file=discord.File(buf, filename=f"profile_{target.id}.png"))

@bot.tree.command(name="인벤토리", description="[마피아 GM] 소지한 장비를 확인합니다.")
async def show_inventory(interaction: discord.Interaction):
    if not await check_maintenance(interaction): return
    if not await check_membership(interaction): return
    if await check_jail(interaction): return

    user_stats = get_user_data(interaction.user.id)
    inventory = user_stats.get("inventory", {})

    embed = discord.Embed(title=f"[{interaction.user.name} 님의 장비 가방]", color=discord.Color.dark_green())
    inv_list = [f"• **{item}** `[보유 중]`" for item in inventory.keys()] if inventory else ["*가방이 비어 있습니다.*"]
    embed.add_field(name="보유 중인 전용 장비", value="\n".join(inv_list), inline=False)
    
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
    {"id": "police_radio", "name": "기동타격대 무전기", "price": 8000, "desc": "검거 포상금 +20% 증가 효과", "bonus_type": "radio"},
    {"id": "police_badge", "name": "특급 수사관 황금 배지", "price": 20000, "desc": "치안국의 명예와 권위를 상징합니다 (방어력 +50 / 명예 보너스)", "def_bonus": 50},
]

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
            supabase.type = "user_data" # 혹은 기존 update 구문 사용
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

# === [공용] 통합 `/정보` 명령어 (확장판) ===
@bot.tree.command(name="정보", description="[공용] 특정 명령어, 아이템 또는 세계관 설정을 상세하게 검색합니다.")
@app_commands.describe(keyword="알고 싶은 항목 (예: 전투, 보스, 슬롯, 칭호, 유토피아 오브 에덴)")
@app_commands.choices(keyword=[
    app_commands.Choice(name="전투 & 보스 시스템", value="전투"),
    app_commands.Choice(name="히든 보스 (/보스)", value="보스"),
    app_commands.Choice(name="카지노 & 도박 (/도박)", value="도박"),
    app_commands.Choice(name="칭호 시스템 (/칭호)", value="칭호"),
    app_commands.Choice(name="일일 의뢰 (/일퀘)", value="일퀘"),
    app_commands.Choice(name="통합 설정 (/설정)", value="설정"),
    app_commands.Choice(name="네임드 몬스터 (/네임드)", value="네임드"),
    app_commands.Choice(name="밀수 거래 (/밀수)", value="밀수"),
    app_commands.Choice(name="조직(패밀리) (/조직)", value="조직"),
    app_commands.Choice(name="조직 레이드 (/레이드)", value="레이드"),
    app_commands.Choice(name="Utopia Of Eden (세계관)", value="유토피아 오브 에덴")
])
async def info_command(interaction: discord.Interaction, keyword: str):
    if not await check_maintenance(interaction): return

    kw = keyword.strip()

    # 1. 시스템 및 명령어 정보 사전
    command_infos = {
        "네임드": {
            "title": "시스템: /네임드 [몬스터]",
            "desc": "일반 NPC보다 훨씬 강한 강화 몬스터에 도전합니다.\n"
                    "• **입장 조건:** 마르코 500pt / 비앙카 1,500pt / 보로닌 2,500pt / 처형자 4,000pt (명예)\n"
                    "• **특징:** 12턴 제한, 체력이 절반 아래가 되면 **격노**하여 공격력이 크게 오릅니다.\n"
                    "• **보상:** 많은 자금 + 소량의 명예, 최초 처치 시 고유 전리품 (칭호 해금)\n"
                    "• **쿨타임:** 4분",
        },
        "전투": {
            "title": "시스템: /전투 [npc이름]",
            "desc": "지정한 NPC와 1:1 턴제 결투를 벌입니다.\n"
                    "• **특징:** 제한 턴(10턴) 내에 처치해야 승리하며, 패배 시 치료비 패널티가 발생합니다.\n"
                    "• **히든 이벤트:** 전투 승리 시 10% 확률로 **[어둠의 지배자]**, 2% 확률로 **[태초의 파괴자]** 가 서버에 출현합니다! (한 번에 한 마리)",
        },
        "보스": {
            "title": "시스템: /보스 (히든 보스 토벌)",
            "desc": "전투 후 살기가 감지되었을 때 출현하는 최종 히든 보스전입니다.\n"
                    "• **출현 조건:** 일반 `/전투` 승리 시 10% 확률로 소환 경보가 발생해야 입장 가능합니다.\n"
                    "• **보상:** 격퇴 성공 시 **$15,000~$30,000**의 막대한 자금과 **+100 pt** 명예 획득!\n"
                    "• **권장:** 명예 약 5,000pt + 수상한 주사기 / 슈퍼 울트라 주먹",
        },
        "도박": {
            "title": "시스템: /도박 [슬롯/동전/블랙잭/규칙]",
            "desc": "보유 자금을 걸고 한판 승부를 벌입니다.\n"
                    "• **슬롯:** 3개 일치 시 고배당, 잭팟(7⃣7⃣7⃣) 시 100배 지급\n"
                    "• **동전:** 48% 확률로 베팅금의 2배 획득\n"
                    "• **블랙잭:** 딜러와 21에 가까운 수로 겨루는 카드 게임 (블랙잭 2.5배)",
        },
        "칭호": {
            "title": "시스템: /칭호",
            "desc": "조건을 달성하여 업적 칭호를 획득하고 프로필에 장착합니다.\n"
                    "• **효과:** `/프로필` 명령어 실행 시 내 아날로그 증명서에 장착된 칭호가 예쁘게 각인됩니다.",
        },
        "일퀘": {
            "title": "시스템: /일퀘",
            "desc": "매일 주어지는 일일 의뢰 4개를 모두 완료하면 **출석 1스택**이 쌓입니다.\n"
                    + "".join(f"• {q['name']} {q['target']}회 ({q['hint']})\n" for q in DAILY_QUESTS)
                    + "• **쿨타임:** 4개를 모두 완료한 시각부터 24시간 뒤에 새 의뢰가 열립니다.\n"
                    f"• **보상:** 출석 {ATTENDANCE_TITLE_STACKS}스택 달성 시 {TITLES['attendance_month']['name']} 칭호",
        },
        "설정": {
            "title": "시스템: /설정",
            "desc": "임무 실패 방지권 자동 사용 여부, 이벤트 긴급 알림 등 내 플레이 환경을 직접 커스텀할 수 있는 설정 창입니다.",
        },
        "이동": {
            "title": "시스템: /이동 [지역명]",
            "desc": "레벤하임(수도), 던베르크(공업), 블랙워드(빈민가), 에르느코항(항구), 로젠펠트(유흥가) 등 5대 거점으로 이동합니다.\n"
                    "• 거점마다 암시장에서 판매하는 아이템 종류가 달라집니다.",
        },
        "아르바이트": {
            "title": "시스템: /아르바이트",
            "desc": "시민/공용 전용 안전 자금 수급처입니다. 위험 부담 없이 정직한 노동으로 소량의 돈을 획득합니다.",
        },
        "송금": {
            "title": "명령어: /송금 [대상] [금액]",
            "desc": "보유 자금을 다른 조직원에게 안전하게 이체합니다. (자기 자신 송금 불가)",
        },
        "검거": {
            "title": "명령어: /검거 [대상] (경찰 전용)",
            "desc": "지명수배 중인 마피아를 현장에서 체포하여 감옥에 3분간 수감하고 포상금을 획득합니다.",
        },
        "밀수": {
            "title": "명령어: /밀수 [경찰] [뇌물] (조직원 전용)",
            "desc": f"경찰에게 뇌물(${SMUGGLE_MIN_BRIBE:,}~${SMUGGLE_MAX_BRIBE:,})을 제안합니다. 경찰이 **수락**하면 뇌물이 경찰에게 넘어가고 {SMUGGLE_IMMUNITY_SEC // 60}분간 그 경찰은 나를 `/검거`할 수 없습니다. "
                    f"**거절**하면 {SMUGGLE_REJECT_WANTED_SEC}초 지명수배 + 벌금 ${SMUGGLE_REJECT_FINE:,} + 명예 -{SMUGGLE_REJECT_HONOR_LOSS}pt 패널티를 받습니다. 쿨타임 {SMUGGLE_COOLDOWN_SEC // 60}분.",
        },
        "조직": {
            "title": "명령어: /조직 [생성·초대·정보·기부·탈퇴·추방·위임·해산·목록]",
            "desc": f"마피아 조직원끼리 **조직(클럽)** 을 만들어 단체 활동을 합니다.\n"
                    f"• **/조직 생성:** 명예 {FAMILY_CREATE_MIN_HONOR}pt 이상 + ${FAMILY_CREATE_COST:,} 필요, 최대 {FAMILY_MAX_MEMBERS}명\n"
                    "• **/조직 초대:** 조직장이 조직원을 초대 (상대가 수락해야 가입)\n"
                    "• **/조직 기부:** 금고에 자금 기부 → 레이드 작전비로 사용\n"
                    "• **/조직 목록:** 레이드 점수 랭킹 TOP 10",
        },
        "레이드": {
            "title": "명령어: /레이드 [시작·정보]",
            "desc": "조직원이 함께 레이드 보스를 잡는 협동 콘텐츠입니다. `/레이드 정보` 에서 보스별 조건·보상과 전투 방법을 볼 수 있습니다.\n"
                    "공격 / 응급처치 / 방어 태세 3가지 버튼으로 진행하며, 보상은 참여도(딜량)에 따라 나뉩니다.",
        },
        "무기상점": {
            "title": "명령어: /무기상점",
            "desc": "전투력을 대폭 높여주는 특수 무기(슈퍼 울트라 주먹, 주사기 등)를 구매합니다.",
        },
        "프로필": {
            "title": "명령어: /프로필 [유저]",
            "desc": "본인 또는 타 유저의 직급, 명예, 자금, 거점 상태가 담긴 아날로그 종이 증명서 이미지를 생성합니다.",
        }
    }

    # 2. 아이템 정보 사전
    item_infos = {
        "슈퍼 울트라 주먹": {
            "title": "아이템: 슈퍼 울트라 주먹",
            "desc": "• **분류:** 무기 (최상급)\n• **효과:** 공격력 +500, 최대 체력 +2,000\n• **설명:** 패밀리 최고의 파괴력을 자랑하는 전설의 주먹입니다.",
        },
        "수상한 주사기": {
            "title": "아이템: 수상한 주사기",
            "desc": "• **분류:** 무기 (특수)\n• **효과:** 공격력 +300, 체력 +1,000\n• **설명:** 신체 능력을 극단적으로 끌어올리는 위험한 약물입니다.",
        },
        "임무 실패 방지권": {
            "title": "아이템: 임무 실패 방지권",
            "desc": "• **분류:** 방어구/유틸리티\n• **효과:** 방어력 +20, 임무 실패 시 수배 자동 방어\n• **설명:** 위기 상황에서 감옥행을 모면하게 해주는 티켓입니다.",
        },
        "치안국 위조 신분증": {
            "title": "아이템: 치안국 위조 신분증",
            "desc": "• **분류:** 방어구/유틸리티\n• **효과:** 방어력 +15, 지명수배 방지\n• **설명:** 경찰의 눈을 잠시 속일 수 있는 정교한 위조 신분증입니다.",
        }
    }

    # 3. 세계관 정보 사전
    world_infos = {
        "유토피아 오브 에덴": {
            "title": "세계관: Utopia Of Eden (유토피아 오브 에덴)",
            "desc": (
                "**“모두가 평등한 세상. 그곳에 불평등한 자는 존재할 수 없다.”**\n\n"
                "**기본 정보**\n"
                "• **조직 분류:** 테러조직 | **활동 국가:** 칼렌데 공화국\n"
                "• **창립자:** 에칼 데 페스토 | **주요 사상:** 공산주의\n"
                "• **주요 활동:** 선전물 제작 및 배포, 극단적 테러 활동\n"
                "• **대립 관계:** 칼렌데 중앙치안국 및 기타 세력\n\n"
                "**주요 사건: 예노호 테러 사건**\n"
                "데이브 니스코프가 여객선 예노호를 통제 불능으로 만들고 침몰시킨 참사 (사망 120명, 실종 1명)."
            )
        }
    }

    # 4. 통합 검색 매칭
    target_info = None
    combined_dict = {**command_infos, **item_infos, **world_infos}

    if kw in combined_dict:
        target_info = combined_dict[kw]
    else:
        for name, data in combined_dict.items():
            if kw in name:
                target_info = data
                break

    if not target_info:
        available_keys = list(combined_dict.keys())
        keys_str = ", ".join([f"`{k}`" for k in available_keys])
        
        await interaction.response.send_message(
            f"**[{kw}]**에 대한 정보를 찾을 수 없습니다.\n\n"
            f"**검색 가능한 전체 목록:**\n{keys_str}",
            ephemeral=True
        )
        return

    # 5. 임베드 생성 및 전송
    embed = discord.Embed(
        title=target_info["title"],
        description=target_info["desc"],
        color=discord.Color.dark_red()
    )
    embed.set_footer(text="마피아 RPG 패밀리 GM 통합 정보 시스템")

    if interaction.user.avatar:
        embed.set_thumbnail(url=interaction.user.avatar.url)

    await interaction.response.send_message(embed=embed, ephemeral=True)

# ==========================================================================
# [패밀리(조직) 시스템]  /조직 ...   &   [단체 레이드]  /레이드 ...
#   • 조직 데이터는 Supabase 의 `families` 테이블에 저장됩니다. (family_setup.sql 을 먼저 실행하세요)
#   • 레이드는 짧게 끝나는 이벤트라서 진행 상태는 메모리에만 두며, 봇이 재시작되면 진행 중이던 레이드는 사라집니다.
# ==========================================================================
# --- 조직 설정 ---
FAMILY_CREATE_COST = 10000        # 조직 창설 비용 ($)
FAMILY_CREATE_MIN_HONOR = 500     # 조직 창설에 필요한 최소 명예
FAMILY_MAX_MEMBERS = 10           # 조직 최대 인원
FAMILY_NAME_PATTERN = re.compile(r"^[0-9A-Za-z가-힣 ]{2,12}$")
FAMILY_MIN_DONATE = 100           # 금고 최소 기부액
FAMILY_DB_ERROR_MSG = "조직 DB 처리 중 오류가 발생했습니다. (관리자: `families` 테이블이 만들어져 있는지 확인해 주세요)"

# --- 레이드 설정 ---
RAID_LOBBY_SEC = 120              # 참가 모집 시간
RAID_DURATION_SEC = 420           # 전투 제한 시간 (7분)
RAID_TICK_SEC = 4                 # 화면 갱신 주기
RAID_MAX_PLAYERS = 8
RAID_FAMILY_COOLDOWN_SEC = 1800   # 같은 조직의 레이드 재시작 대기시간 (30분)
RAID_GCD_SEC = 2.0                # 모든 행동 공통 대기시간
RAID_ATTACK_CD_SEC = 4.0          # 공격 대기시간
RAID_HEAL_CD_SEC = 25.0           # 응급처치 대기시간
RAID_GUARD_CD_SEC = 20.0          # 방어 태세 대기시간
RAID_GUARD_DURATION_SEC = 10.0    # 방어 태세 지속시간 (이 안에 광역기가 오면 피해 절반)
RAID_AOE_INTERVAL_SEC = 20        # 광역기 주기
RAID_AOE_INTERVAL_ENRAGE_SEC = 15 # 격노 후 광역기 주기
RAID_HEAL_RATIO = 0.12            # 응급처치(전원 회복) 회복량: 최대 체력의 12%
RAID_REVIVE_RATIO = 0.40          # 쓰러진 동료 부활 시 체력 비율
RAID_TREASURY_RETURN = 0.15       # 승리 시 지급된 보상금의 15%가 조직 금고로 들어감
RAID_LOSE_MONEY_RATIO = 0.10      # 실패해도 참전자에게 주는 위로금 비율
RAID_BASE_SHARE = 0.6             # 보상 중 '균등 분배' 비율 (나머지 0.4는 딜량 비례)

# hits: 참가자 1인이 '평균 몇 번 때리면' 쓰러지는지 (보스 체력은 참가자 공격력에 맞춰 자동 계산)
# def_ratio: 보스 방어력 = 참가자 평균 공격력 × 비율 / aoe: 광역기가 깎는 '최대 체력 대비' 비율
RAID_TIERS = {
    "rat": {
        "name": "하수구의 쥐왕", "min_players": 2, "min_honor": 0, "cost": 1000,
        "hits": 38, "def_ratio": 0.25, "aoe": 0.07, "aoe_enrage": 0.10,
        "money": 20000, "honor": 250, "points": 10,
        "desc": "하수도를 지배하는 거대한 쥐떼의 왕. 입문자용 레이드입니다.",
    },
    "bull": {
        "name": "부두 항만의 폭군", "min_players": 3, "min_honor": 1500, "cost": 3000,
        "hits": 46, "def_ratio": 0.30, "aoe": 0.09, "aoe_enrage": 0.13,
        "money": 40000, "honor": 500, "points": 25,
        "desc": "에르느코항 부두를 장악한 괴력의 폭군. 응급처치 담당이 꼭 필요합니다.",
    },
    "king": {
        "name": "칼렌데 지하 황제", "min_players": 4, "min_honor": 4000, "cost": 8000,
        "hits": 54, "def_ratio": 0.35, "aoe": 0.09, "aoe_enrage": 0.13,
        "money": 60000, "honor": 1000, "points": 60,
        "desc": "칼렌데 지하 세계의 진짜 주인. 정상급 조직만이 도전할 수 있습니다.",
    },
}

active_raids = {}            # {family_id: RaidSession}
raid_user_map = {}           # {user_id: family_id}  (레이드 참가 중인 유저)
raid_family_cooldowns = {}   # {family_id: datetime}


# ---------------------------------------------------------------- 조직 DB 도우미 (동기)
def family_get_by_user(guild_id: int, user_id: int):
    res = (supabase.table("families").select("*")
           .eq("guild_id", str(guild_id)).contains("member_ids", [str(user_id)]).limit(1).execute())
    return res.data[0] if res.data else None

def family_get_by_id(family_id):
    res = supabase.table("families").select("*").eq("id", family_id).limit(1).execute()
    return res.data[0] if res.data else None

def family_get_by_name(guild_id: int, name: str):
    res = (supabase.table("families").select("*")
           .eq("guild_id", str(guild_id)).eq("name", name).limit(1).execute())
    return res.data[0] if res.data else None

def family_list_top(guild_id: int, limit: int = 10):
    res = (supabase.table("families").select("*").eq("guild_id", str(guild_id))
           .order("raid_points", desc=True).limit(limit).execute())
    return res.data or []

def family_create(guild_id: int, name: str, owner_id: int):
    row = {
        "guild_id": str(guild_id), "name": name, "owner_id": str(owner_id),
        "member_ids": [str(owner_id)], "treasury": 0, "raid_points": 0,
    }
    res = supabase.table("families").insert(row).execute()
    return res.data[0] if res.data else row

def family_update(family_id, data: dict):
    res = supabase.table("families").update(data).eq("id", family_id).execute()
    _check_updated(res, "family_update")

def family_delete(family_id):
    supabase.table("families").delete().eq("id", family_id).execute()

def family_detach_user(user_id: int):
    """유저를 모든 조직에서 제거 (조직장이면 다음 조직원에게 위임, 마지막 1명이면 조직 해산). /탈퇴 초기화에서도 사용."""
    if not supabase:
        return
    uid = str(user_id)
    res = supabase.table("families").select("*").contains("member_ids", [uid]).execute()
    for fam in (res.data or []):
        members = [m for m in fam["member_ids"] if m != uid]
        if not members:
            family_delete(fam["id"])
            continue
        data = {"member_ids": members}
        if fam["owner_id"] == uid:
            data["owner_id"] = members[0]
        family_update(fam["id"], data)

def _is_mafia(member: discord.Member) -> bool:
    role = discord.utils.get(member.guild.roles, name="조직원")
    return bool(role and role in member.roles)

def _is_jailed(user_id: int) -> bool:
    return user_id in jail_list and datetime.now() < jail_list[user_id]

def _bar(cur: int, mx: int, n: int = 12) -> str:
    ratio = 0 if mx <= 0 else max(0.0, min(1.0, cur / mx))
    filled = int(round(ratio * n))
    return "█" * filled + "░" * (n - filled)


# ================================================================ /조직
family_group = app_commands.Group(name="조직", description="[패밀리] 조직(클럽)을 만들고 함께 활동합니다.")
bot.tree.add_command(family_group)

async def _family_precheck(interaction: discord.Interaction, need_mafia: bool = True) -> bool:
    if not await check_maintenance(interaction): return False
    if not await check_membership(interaction): return False
    if await check_jail(interaction): return False
    if need_mafia and not _is_mafia(interaction.user):
        await interaction.response.send_message("조직(패밀리) 기능은 마피아 조직원만 사용할 수 있습니다.", ephemeral=True)
        return False
    return True

@family_group.command(name="생성", description=f"조직을 창설합니다. (비용 ${FAMILY_CREATE_COST:,} / 명예 {FAMILY_CREATE_MIN_HONOR}pt 이상)")
@app_commands.describe(이름="조직 이름 (한글·영문·숫자·띄어쓰기 2~12자)")
async def family_create_cmd(interaction: discord.Interaction, 이름: str):
    if not await _family_precheck(interaction): return
    user = interaction.user
    name = 이름.strip()
    if not FAMILY_NAME_PATTERN.match(name):
        await interaction.response.send_message("조직 이름은 한글·영문·숫자·띄어쓰기로 **2~12자**여야 합니다.", ephemeral=True)
        return
    try:
        if family_get_by_user(interaction.guild_id, user.id):
            await interaction.response.send_message("이미 조직에 소속되어 있습니다. 먼저 `/조직 탈퇴` 하세요.", ephemeral=True)
            return
        if family_get_by_name(interaction.guild_id, name):
            await interaction.response.send_message("이미 같은 이름의 조직이 있습니다.", ephemeral=True)
            return
        info = get_user_data(user.id)
        if info["honor"] < FAMILY_CREATE_MIN_HONOR:
            await interaction.response.send_message(f"조직을 만들려면 명예 **{FAMILY_CREATE_MIN_HONOR}pt** 이상이어야 합니다. (현재 {info['honor']:,}pt)", ephemeral=True)
            return
        if info["money"] < FAMILY_CREATE_COST:
            await interaction.response.send_message(f"창설 비용 **${FAMILY_CREATE_COST:,}** 이(가) 부족합니다. (보유 ${info['money']:,})", ephemeral=True)
            return
        update_user_stats(user.id, -FAMILY_CREATE_COST, 0)
        family_create(interaction.guild_id, name, user.id)
    except Exception as e:
        print(f"Family create error: {e}")
        await interaction.response.send_message(FAMILY_DB_ERROR_MSG, ephemeral=True)
        return

    embed = discord.Embed(
        title="[조직 창설]",
        description=(f"**{user.mention}** 님이 새로운 조직 **「{name}」** 을(를) 창설했습니다!\n\n"
                     f"**창설 비용:** `${FAMILY_CREATE_COST:,}`\n"
                     f"`/조직 초대` 로 동료를 모아 `/레이드 시작` 에 도전해 보세요."),
        color=discord.Color.dark_gold()
    )
    await interaction.response.send_message(embed=embed)

def _family_embed(fam: dict, guild: discord.Guild) -> discord.Embed:
    members = fam.get("member_ids") or []
    lines = []
    for m in members:
        crown = "[조직장] " if m == fam["owner_id"] else "• "
        lines.append(f"{crown}<@{m}>")
    embed = discord.Embed(title=f"조직 「{fam['name']}」", color=discord.Color.dark_gold())
    embed.add_field(name=f"조직원 ({len(members)}/{FAMILY_MAX_MEMBERS})", value="\n".join(lines) or "-", inline=False)
    embed.add_field(name="금고", value=f"`${fam.get('treasury', 0):,}`", inline=True)
    embed.add_field(name="레이드 점수", value=f"`{fam.get('raid_points', 0):,} pt`", inline=True)
    raid = active_raids.get(fam["id"])
    if raid:
        embed.add_field(name="진행 중인 레이드", value=f"{raid.tier['name']} ({'모집 중' if raid.state == 'lobby' else '전투 중'})", inline=False)
    cd = raid_family_cooldowns.get(fam["id"])
    if cd and datetime.now() < cd:
        embed.add_field(name="다음 레이드", value=f"{int((cd - datetime.now()).total_seconds() // 60) + 1}분 후", inline=True)
    return embed

@family_group.command(name="정보", description="조직 정보를 확인합니다. (이름을 비우면 내 조직)")
@app_commands.describe(이름="확인할 조직 이름 (비우면 내 조직)")
async def family_info_cmd(interaction: discord.Interaction, 이름: str = None):
    if not await check_maintenance(interaction): return
    try:
        fam = family_get_by_name(interaction.guild_id, 이름.strip()) if 이름 else family_get_by_user(interaction.guild_id, interaction.user.id)
    except Exception as e:
        print(f"Family info error: {e}")
        await interaction.response.send_message(FAMILY_DB_ERROR_MSG, ephemeral=True)
        return
    if not fam:
        msg = "해당 이름의 조직이 없습니다." if 이름 else "소속된 조직이 없습니다. `/조직 생성` 또는 초대를 받아 보세요."
        await interaction.response.send_message(msg, ephemeral=True)
        return
    await interaction.response.send_message(embed=_family_embed(fam, interaction.guild))

@family_group.command(name="목록", description="레이드 점수 기준 조직 랭킹 TOP 10")
async def family_rank_cmd(interaction: discord.Interaction):
    if not await check_maintenance(interaction): return
    try:
        rows = family_list_top(interaction.guild_id, 10)
    except Exception as e:
        print(f"Family list error: {e}")
        await interaction.response.send_message(FAMILY_DB_ERROR_MSG, ephemeral=True)
        return
    if not rows:
        await interaction.response.send_message("아직 창설된 조직이 없습니다.", ephemeral=True)
        return
        lines = [f"`{i + 1}위` **{r['name']}** — {r.get('raid_points', 0):,} pt · {len(r.get('member_ids') or [])}명" for i, r in enumerate(rows)]
    embed = discord.Embed(title="조직 랭킹 (레이드 점수)", description="\n".join(lines), color=discord.Color.gold())
    await interaction.response.send_message(embed=embed)

class FamilyInviteView(discord.ui.View):
    def __init__(self, family_id, family_name: str, inviter: discord.Member, target: discord.Member):
        super().__init__(timeout=60)
        self.family_id = family_id
        self.family_name = family_name
        self.inviter = inviter
        self.target = target
        self.resolved = False
        self.message = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.target.id:
            await interaction.response.send_message("초대받은 본인만 응답할 수 있습니다.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="수락", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.resolved: return
        self.resolved = True
        self.stop()
        try:
            if family_get_by_user(interaction.guild_id, self.target.id):
                await interaction.response.edit_message(content="이미 다른 조직에 소속되어 있어 가입할 수 없습니다.", embed=None, view=None)
                return
            fam = family_get_by_id(self.family_id)
            if not fam:
                await interaction.response.edit_message(content="조직이 더 이상 존재하지 않습니다.", embed=None, view=None)
                return
            members = list(fam.get("member_ids") or [])
            if len(members) >= FAMILY_MAX_MEMBERS:
                await interaction.response.edit_message(content="조직 정원이 가득 찼습니다.", embed=None, view=None)
                return
            members.append(str(self.target.id))
            family_update(self.family_id, {"member_ids": members})
        except Exception as e:
            print(f"Family invite accept error: {e}")
            await interaction.response.edit_message(content=FAMILY_DB_ERROR_MSG, embed=None, view=None)
            return
        embed = discord.Embed(title="[조직 가입]",
                              description=f"**{self.target.mention}** 님이 **「{self.family_name}」** 에 합류했습니다! ({len(members)}/{FAMILY_MAX_MEMBERS})",
                              color=discord.Color.green())
        await interaction.response.edit_message(content=None, embed=embed, view=None)

    @discord.ui.button(label="거절", style=discord.ButtonStyle.secondary)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.resolved: return
        self.resolved = True
        self.stop()
        await interaction.response.edit_message(content=f"{self.target.mention} 님이 **「{self.family_name}」** 초대를 거절했습니다.", embed=None, view=None)

    async def on_timeout(self):
        if self.resolved or not self.message: return
        try:
            await self.message.edit(content="초대가 만료되었습니다.", embed=None, view=None)
        except Exception:
            pass

@family_group.command(name="초대", description="조직원을 우리 조직에 초대합니다. (조직장 전용)")
@app_commands.describe(member="초대할 조직원")
async def family_invite_cmd(interaction: discord.Interaction, member: discord.Member):
    if not await _family_precheck(interaction): return
    user = interaction.user
    if member.bot or member.id == user.id:
        await interaction.response.send_message("올바른 초대 대상이 아닙니다.", ephemeral=True)
        return
    if not _is_mafia(member):
        await interaction.response.send_message("마피아 조직원만 초대할 수 있습니다.", ephemeral=True)
        return
    try:
        fam = family_get_by_user(interaction.guild_id, user.id)
        if not fam:
            await interaction.response.send_message("소속된 조직이 없습니다.", ephemeral=True)
            return
        if fam["owner_id"] != str(user.id):
            await interaction.response.send_message("조직장만 초대할 수 있습니다.", ephemeral=True)
            return
        if len(fam.get("member_ids") or []) >= FAMILY_MAX_MEMBERS:
            await interaction.response.send_message("조직 정원이 가득 찼습니다.", ephemeral=True)
            return
        if family_get_by_user(interaction.guild_id, member.id):
            await interaction.response.send_message("상대는 이미 다른 조직에 소속되어 있습니다.", ephemeral=True)
            return
    except Exception as e:
        print(f"Family invite error: {e}")
        await interaction.response.send_message(FAMILY_DB_ERROR_MSG, ephemeral=True)
        return

    view = FamilyInviteView(fam["id"], fam["name"], user, member)
    embed = discord.Embed(title="[조직 초대장]",
                          description=f"**{user.mention}** 님이 **{member.mention}** 님을 조직 **「{fam['name']}」** 에 초대했습니다.\n60초 안에 응답해 주세요.",
                          color=discord.Color.dark_gold())
    await interaction.response.send_message(content=member.mention, embed=embed, view=view)
    view.message = await interaction.original_response()

@family_group.command(name="탈퇴", description="현재 조직에서 탈퇴합니다.")
async def family_leave_cmd(interaction: discord.Interaction):
    if not await _family_precheck(interaction): return
    user = interaction.user
    if user.id in raid_user_map:
        await interaction.response.send_message("레이드 진행 중에는 탈퇴할 수 없습니다.", ephemeral=True)
        return
    try:
        fam = family_get_by_user(interaction.guild_id, user.id)
        if not fam:
            await interaction.response.send_message("소속된 조직이 없습니다.", ephemeral=True)
            return
        members = fam.get("member_ids") or []
        if fam["owner_id"] == str(user.id) and len(members) > 1:
            await interaction.response.send_message("조직장은 탈퇴할 수 없습니다. `/조직 위임` 으로 조직장을 넘기거나 `/조직 해산` 하세요.", ephemeral=True)
            return
        family_detach_user(user.id)
    except Exception as e:
        print(f"Family leave error: {e}")
        await interaction.response.send_message(FAMILY_DB_ERROR_MSG, ephemeral=True)
        return
    note = " (마지막 조직원이라 조직이 해산되었습니다)" if len(members) <= 1 else ""
    await interaction.response.send_message(f"**{user.mention}** 님이 **「{fam['name']}」** 에서 탈퇴했습니다.{note}")

@family_group.command(name="추방", description="조직원을 추방합니다. (조직장 전용)")
@app_commands.describe(member="추방할 조직원")
async def family_kick_cmd(interaction: discord.Interaction, member: discord.Member):
    if not await _family_precheck(interaction): return
    user = interaction.user
    try:
        fam = family_get_by_user(interaction.guild_id, user.id)
        if not fam or fam["owner_id"] != str(user.id):
            await interaction.response.send_message("조직장만 사용할 수 있습니다.", ephemeral=True)
            return
        if member.id == user.id or str(member.id) not in (fam.get("member_ids") or []):
            await interaction.response.send_message("추방할 수 없는 대상입니다.", ephemeral=True)
            return
        if member.id in raid_user_map:
            await interaction.response.send_message("레이드 참가 중인 조직원은 추방할 수 없습니다.", ephemeral=True)
            return
        family_update(fam["id"], {"member_ids": [m for m in fam["member_ids"] if m != str(member.id)]})
    except Exception as e:
        print(f"Family kick error: {e}")
        await interaction.response.send_message(FAMILY_DB_ERROR_MSG, ephemeral=True)
        return
    await interaction.response.send_message(f"**{member.mention}** 님이 조직 **「{fam['name']}」** 에서 추방되었습니다.")

@family_group.command(name="위임", description="조직장 자리를 다른 조직원에게 넘깁니다. (조직장 전용)")
@app_commands.describe(member="새 조직장")
async def family_transfer_cmd(interaction: discord.Interaction, member: discord.Member):
    if not await _family_precheck(interaction): return
    user = interaction.user
    try:
        fam = family_get_by_user(interaction.guild_id, user.id)
        if not fam or fam["owner_id"] != str(user.id):
            await interaction.response.send_message("조직장만 사용할 수 있습니다.", ephemeral=True)
            return
        if member.id == user.id or str(member.id) not in (fam.get("member_ids") or []):
            await interaction.response.send_message("같은 조직의 다른 조직원에게만 위임할 수 있습니다.", ephemeral=True)
            return
        family_update(fam["id"], {"owner_id": str(member.id)})
    except Exception as e:
        print(f"Family transfer error: {e}")
        await interaction.response.send_message(FAMILY_DB_ERROR_MSG, ephemeral=True)
        return
    await interaction.response.send_message(f"조직 **「{fam['name']}」** 의 새 조직장은 **{member.mention}** 님입니다!")

@family_group.command(name="기부", description=f"조직 금고에 자금을 기부합니다. (레이드 작전 자금으로 쓰입니다)")
@app_commands.describe(금액=f"기부할 금액 (${FAMILY_MIN_DONATE:,} 이상)")
async def family_donate_cmd(interaction: discord.Interaction, 금액: app_commands.Range[int, FAMILY_MIN_DONATE, 10_000_000]):
    if not await _family_precheck(interaction): return
    user = interaction.user
    try:
        fam = family_get_by_user(interaction.guild_id, user.id)
        if not fam:
            await interaction.response.send_message("소속된 조직이 없습니다.", ephemeral=True)
            return
        if get_user_data(user.id)["money"] < 금액:
            await interaction.response.send_message("보유 자금이 부족합니다.", ephemeral=True)
            return
        update_user_stats(user.id, -금액, 0)
        fam = family_get_by_id(fam["id"])   # 최신 금고 값으로 갱신
        family_update(fam["id"], {"treasury": fam.get("treasury", 0) + 금액})
    except Exception as e:
        print(f"Family donate error: {e}")
        await interaction.response.send_message(FAMILY_DB_ERROR_MSG, ephemeral=True)
        return
    await interaction.response.send_message(f"**{user.mention}** 님이 조직 금고에 `${금액:,}` 을(를) 기부했습니다! (금고 `${fam.get('treasury', 0) + 금액:,}`)")

class FamilyDisbandView(discord.ui.View):
    def __init__(self, owner: discord.Member, family_id, family_name: str):
        super().__init__(timeout=30)
        self.owner = owner
        self.family_id = family_id
        self.family_name = family_name

    @discord.ui.button(label="정말 해산합니다", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner.id:
            await interaction.response.send_message("조직장만 누를 수 있습니다.", ephemeral=True)
            return
        self.stop()
        try:
            if self.family_id in active_raids:
                await interaction.response.edit_message(content="레이드 진행 중에는 해산할 수 없습니다.", view=None)
                return
            family_delete(self.family_id)
        except Exception as e:
            print(f"Family disband error: {e}")
            await interaction.response.edit_message(content=FAMILY_DB_ERROR_MSG, view=None)
            return
        await interaction.response.edit_message(content=f"조직 **「{self.family_name}」** 이(가) 해산되었습니다. (금고의 자금도 함께 사라집니다)", view=None)

@family_group.command(name="해산", description="조직을 해산합니다. (조직장 전용, 금고 자금은 사라집니다)")
async def family_disband_cmd(interaction: discord.Interaction):
    if not await _family_precheck(interaction): return
    user = interaction.user
    try:
        fam = family_get_by_user(interaction.guild_id, user.id)
    except Exception as e:
        print(f"Family disband lookup error: {e}")
        await interaction.response.send_message(FAMILY_DB_ERROR_MSG, ephemeral=True)
        return
    if not fam or fam["owner_id"] != str(user.id):
        await interaction.response.send_message("조직장만 사용할 수 있습니다.", ephemeral=True)
        return
    await interaction.response.send_message(
        f"정말 **「{fam['name']}」** 을(를) 해산할까요? 금고 `${fam.get('treasury', 0):,}` 도 함께 사라집니다.",
        view=FamilyDisbandView(user, fam["id"], fam["name"]), ephemeral=True)


# ================================================================ /레이드
class RaidPlayer:
    __slots__ = ("member", "uid", "name", "max_hp", "hp", "atk", "dmg_done", "hits",
                 "gcd_until", "atk_ready", "heal_ready", "guard_ready", "guard_until")

    def __init__(self, member: discord.Member, stats: dict):
        self.member = member
        self.uid = member.id
        self.name = member.display_name
        self.max_hp = int(stats["hp"])
        self.hp = self.max_hp
        self.atk = int(stats["atk"])
        self.dmg_done = 0
        self.hits = 0
        self.gcd_until = 0.0
        self.atk_ready = 0.0
        self.heal_ready = 0.0
        self.guard_ready = 0.0
        self.guard_until = 0.0

    @property
    def alive(self) -> bool:
        return self.hp > 0


class RaidSession:
    def __init__(self, family: dict, tier_key: str, starter: discord.Member):
        self.family = family
        self.fid = family["id"]
        self.tier_key = tier_key
        self.tier = RAID_TIERS[tier_key]
        self.starter = starter
        self.players = {}              # {user_id: RaidPlayer}
        self.state = "lobby"           # lobby → fighting → done
        self.cancelled = False
        self.start_event = asyncio.Event()
        self.wake = asyncio.Event()
        self.message = None
        self.logs = []
        self.boss_hp = 0
        self.boss_max = 0
        self.boss_def = 0
        self.fight_end = 0.0
        self.next_aoe = 0.0
        self.enraged = False
        self.fight_started = False

    def log(self, text: str):
        self.logs.append(text)
        self.logs = self.logs[-6:]


def raid_lobby_embed(s: RaidSession) -> discord.Embed:
    t = s.tier
    names = "\n".join(f"• {p.member.mention} (HP {p.max_hp:,} / ATK {p.atk:,})" for p in s.players.values()) or "-"
    embed = discord.Embed(
        title=f"[레이드 모집] {t['name']}",
        description=(f"{t['desc']}\n\n"
                     f"**조직:** 「{s.family['name']}」\n"
                     f"**모집 인원:** {len(s.players)}/{RAID_MAX_PLAYERS} (최소 {t['min_players']}명)\n"
                     f"**참가 조건:** 명예 {t['min_honor']:,}pt 이상\n"
                     f"**예상 보상(1인 평균):** `${t['money']:,}` / 명예 `+{t['honor']}pt`\n"
                     f"**{RAID_LOBBY_SEC}초** 안에 `참가`를 눌러 주세요. (시작자가 `바로 시작`도 가능)"),
        color=discord.Color.orange()
    )
    embed.add_field(name="참가자", value=names, inline=False)
    return embed


def raid_fight_embed(s: RaidSession) -> discord.Embed:
    t = s.tier
    now = time.time()
    rage = "**격노 상태!** 광역 공격이 강해지고 더 자주 옵니다!\n" if s.enraged else ""
    aoe_warn = ""
    if s.state == "fighting":
        left = int(s.next_aoe - now)
        aoe_warn = f"**광역 공격까지 {max(left, 0)}초!** (방어 태세는 {int(RAID_GUARD_DURATION_SEC)}초간 유지)\n" if left <= 6 else ""
    embed = discord.Embed(
        title=f"[레이드 전투] {t['name']}",
        description=(f"{rage}"
                     f"보스 `{_bar(s.boss_hp, s.boss_max, 20)}` **{max(0, int(s.boss_hp * 100 / max(1, s.boss_max)))}%**\n"
                     f"종료 <t:{int(s.fight_end)}:R>  ·  다음 광역 <t:{int(s.next_aoe)}:R>\n"
                     f"{aoe_warn}"),
        color=discord.Color.red() if s.enraged else discord.Color.orange()
    )
    lines = []
    for p in s.players.values():
        if not p.alive:
            icon = "[전투불능]"
        elif p.guard_until > now:
            icon = "[방어]"
        else:
            icon = "[전투]"
        lines.append(f"{icon} **{p.name}** `{_bar(p.hp, p.max_hp, 8)}` {int(p.hp * 100 / max(1, p.max_hp))}% · 딜 {p.dmg_done:,}")
    embed.add_field(name="공격대", value="\n".join(lines) or "-", inline=False)
    embed.add_field(name="전황", value="\n".join(s.logs) or "전투 시작!", inline=False)
    embed.set_footer(text="공격 4초 / 응급처치 25초 / 방어 20초 쿨타임")
    return embed


def raid_release(s: RaidSession):
    for uid in list(s.players.keys()):
        if raid_user_map.get(uid) == s.fid:
            raid_user_map.pop(uid, None)
    if active_raids.get(s.fid) is s:
        active_raids.pop(s.fid, None)


async def _raid_edit(s: RaidSession, **kwargs):
    if not s.message:
        return
    try:
        await s.message.edit(**kwargs)
    except discord.HTTPException as e:
        print(f"Raid message edit error: {e}")


# ---------------------------------------------------------------- 모집 화면
class RaidLobbyView(discord.ui.View):
    def __init__(self, session: RaidSession):
        super().__init__(timeout=None)
        self.s = session

    @discord.ui.button(label="참가", style=discord.ButtonStyle.success)
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        s = self.s
        user = interaction.user
        if s.state != "lobby":
            await interaction.response.send_message("이미 모집이 끝났습니다.", ephemeral=True)
            return
        if user.id in s.players:
            await interaction.response.send_message("이미 참가 중입니다.", ephemeral=True)
            return
        if len(s.players) >= RAID_MAX_PLAYERS:
            await interaction.response.send_message("정원이 가득 찼습니다.", ephemeral=True)
            return
        if user.id in raid_user_map:
            await interaction.response.send_message("이미 다른 레이드에 참가 중입니다.", ephemeral=True)
            return
        if not _is_mafia(user) or _is_jailed(user.id):
            await interaction.response.send_message("마피아 조직원만(감옥 수감 중 제외) 참가할 수 있습니다.", ephemeral=True)
            return
        try:
            fam = family_get_by_id(s.fid)
            if not fam or str(user.id) not in (fam.get("member_ids") or []):
                await interaction.response.send_message("이 조직의 조직원만 참가할 수 있습니다.", ephemeral=True)
                return
            info = get_user_data(user.id)
        except Exception as e:
            print(f"Raid join error: {e}")
            await interaction.response.send_message(FAMILY_DB_ERROR_MSG, ephemeral=True)
            return
        if info["honor"] < s.tier["min_honor"]:
            await interaction.response.send_message(f"이 레이드는 명예 **{s.tier['min_honor']:,}pt** 이상만 참가할 수 있습니다.", ephemeral=True)
            return
        if user.id in s.players or s.state != "lobby":   # DB 호출 사이에 상태가 바뀐 경우
            await interaction.response.send_message("참가할 수 없는 상태입니다.", ephemeral=True)
            return
        s.players[user.id] = RaidPlayer(user, compute_combat_stats(info["honor"], info.get("inventory", {})))
        raid_user_map[user.id] = s.fid
        await interaction.response.edit_message(embed=raid_lobby_embed(s), view=self)

    @discord.ui.button(label="바로 시작", style=discord.ButtonStyle.primary)
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        s = self.s
        if interaction.user.id != s.starter.id:
            await interaction.response.send_message("레이드를 연 사람만 시작할 수 있습니다.", ephemeral=True)
            return
        if len(s.players) < s.tier["min_players"]:
            await interaction.response.send_message(f"최소 **{s.tier['min_players']}명**이 필요합니다. (현재 {len(s.players)}명)", ephemeral=True)
            return
        await interaction.response.defer()
        s.start_event.set()

    @discord.ui.button(label="취소", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        s = self.s
        if interaction.user.id != s.starter.id:
            await interaction.response.send_message("레이드를 연 사람만 취소할 수 있습니다.", ephemeral=True)
            return
        await interaction.response.defer()
        s.cancelled = True
        s.start_event.set()


# ---------------------------------------------------------------- 전투 화면 (버튼 3개)
class RaidFightView(discord.ui.View):
    def __init__(self, session: RaidSession):
        super().__init__(timeout=None)
        self.s = session

    async def _prep(self, interaction: discord.Interaction):
        """전투 중인 참가자인지, 살아 있는지, 공통 대기시간이 지났는지 확인"""
        s = self.s
        p = s.players.get(interaction.user.id)
        if s.state != "fighting":
            await interaction.response.send_message("이미 전투가 끝났습니다.", ephemeral=True)
            return None
        if not p:
            await interaction.response.send_message("이 레이드의 참가자가 아닙니다.", ephemeral=True)
            return None
        if not p.alive:
            await interaction.response.send_message("전투불능 상태입니다. 동료의 **응급처치**를 기다리세요!", ephemeral=True)
            return None
        wait = p.gcd_until - time.time()
        if wait > 0:
            await interaction.response.send_message(f"아직 손이 풀리지 않았습니다. ({wait:.1f}초)", ephemeral=True)
            return None
        return p

    @discord.ui.button(label="공격", style=discord.ButtonStyle.danger)
    async def attack(self, interaction: discord.Interaction, button: discord.ui.Button):
        p = await self._prep(interaction)
        if not p: return
        s = self.s
        now = time.time()
        if p.atk_ready > now:
            await interaction.response.send_message(f"공격 쿨타임 {p.atk_ready - now:.1f}초", ephemeral=True)
            return
        dmg = max(10, p.atk - s.boss_def + random.randint(-10, 10))
        s.boss_hp = max(0, s.boss_hp - dmg)
        p.dmg_done += dmg
        p.hits += 1
        p.atk_ready = now + RAID_ATTACK_CD_SEC
        p.gcd_until = now + RAID_GCD_SEC
        await interaction.response.defer()
        if s.boss_hp <= 0:
            s.wake.set()

    @discord.ui.button(label="응급처치", style=discord.ButtonStyle.success)
    async def heal(self, interaction: discord.Interaction, button: discord.ui.Button):
        p = await self._prep(interaction)
        if not p: return
        s = self.s
        now = time.time()
        if p.heal_ready > now:
            await interaction.response.send_message(f"응급처치 쿨타임 {p.heal_ready - now:.1f}초", ephemeral=True)
            return
        downed = [x for x in s.players.values() if not x.alive]
        if downed:
            target = random.choice(downed)
            target.hp = max(1, int(target.max_hp * RAID_REVIVE_RATIO))
            s.log(f"{p.name} → {target.name} 부활! (체력 {int(RAID_REVIVE_RATIO * 100)}%)")
        else:
            hurt = [x for x in s.players.values() if x.alive and x.hp < x.max_hp]
            if not hurt:
                await interaction.response.send_message("치료가 필요한 동료가 없습니다.", ephemeral=True)
                return
            for x in s.players.values():
                if x.alive:
                    x.hp = min(x.max_hp, x.hp + int(x.max_hp * RAID_HEAL_RATIO))
            s.log(f"{p.name}의 응급처치! 전원 체력 +{int(RAID_HEAL_RATIO * 100)}%")
        p.heal_ready = now + RAID_HEAL_CD_SEC
        p.gcd_until = now + RAID_GCD_SEC
        await interaction.response.defer()

    @discord.ui.button(label="방어 태세", style=discord.ButtonStyle.primary)
    async def guard(self, interaction: discord.Interaction, button: discord.ui.Button):
        p = await self._prep(interaction)
        if not p: return
        now = time.time()
        if p.guard_ready > now:
            await interaction.response.send_message(f"방어 태세 쿨타임 {p.guard_ready - now:.1f}초", ephemeral=True)
            return
        p.guard_until = now + RAID_GUARD_DURATION_SEC
        p.guard_ready = now + RAID_GUARD_CD_SEC
        p.gcd_until = now + RAID_GCD_SEC
        await interaction.response.send_message(f"방어 태세! {int(RAID_GUARD_DURATION_SEC)}초 안에 오는 광역 공격 피해가 절반이 됩니다.", ephemeral=True)


# ---------------------------------------------------------------- 진행 / 정산
def raid_do_aoe(s: RaidSession):
    pct = s.tier["aoe_enrage"] if s.enraged else s.tier["aoe"]
    now = time.time()
    downed = []
    for p in s.players.values():
        if not p.alive:
            continue
        dmg = int(p.max_hp * pct * random.uniform(0.9, 1.1))
        if p.guard_until > now:
            dmg //= 2
        p.hp = max(0, p.hp - dmg)
        if p.hp == 0:
            downed.append(p.name)
    text = f"광역 공격! (최대 체력의 약 {int(pct * 100)}%)"
    if downed:
        text += f" {', '.join(downed)} 전투불능!"
    s.log(text)
    s.next_aoe = now + (RAID_AOE_INTERVAL_ENRAGE_SEC if s.enraged else RAID_AOE_INTERVAL_SEC)


def _raid_settle_sync(s: RaidSession, win: bool):
    """보상 지급 + 조직 갱신 (동기 DB 작업이라 to_thread 로 실행). 반환: [(RaidPlayer, money, honor)], total_money"""
    t = s.tier
    attackers = [p for p in s.players.values() if p.dmg_done > 0]
    results = []
    total_money = 0
    if attackers:
        total_dmg = sum(p.dmg_done for p in attackers)
        n = len(attackers)
        for p in attackers:
            weight = RAID_BASE_SHARE / n + (1 - RAID_BASE_SHARE) * (p.dmg_done / total_dmg)
            if win:
                money = int(t["money"] * n * weight)
                honor = max(1, int(t["honor"] * n * weight))
            else:
                money = int(t["money"] * n * weight * RAID_LOSE_MONEY_RATIO)
                honor = 0
            results.append((p, money, honor))
            total_money += money
            try:
                update_user_stats(p.uid, money, honor)
            except Exception as e:
                print(f"Raid reward error ({p.uid}): {e}")
    if win:
        try:
            fam = family_get_by_id(s.fid)
            if fam:
                family_update(s.fid, {
                    "treasury": fam.get("treasury", 0) + int(total_money * RAID_TREASURY_RETURN),
                    "raid_points": fam.get("raid_points", 0) + t["points"],
                })
        except Exception as e:
            print(f"Raid family update error: {e}")
    return results, total_money


def _raid_refund_sync(s: RaidSession):
    fam = family_get_by_id(s.fid)
    if fam:
        family_update(s.fid, {"treasury": fam.get("treasury", 0) + s.tier["cost"]})


async def run_raid(s: RaidSession):
    try:
        # ── 1) 모집
        try:
            await asyncio.wait_for(s.start_event.wait(), timeout=RAID_LOBBY_SEC)
        except asyncio.TimeoutError:
            pass

        if s.cancelled or len(s.players) < s.tier["min_players"]:
            s.state = "done"
            try:
                await asyncio.to_thread(_raid_refund_sync, s)
            except Exception as e:
                print(f"Raid refund error: {e}")
            reason = "시작자가 레이드를 취소했습니다." if s.cancelled else f"최소 인원({s.tier['min_players']}명)이 모이지 않았습니다."
            embed = discord.Embed(title="[레이드 취소]", description=f"{reason}\n작전 자금 `${s.tier['cost']:,}` 은(는) 조직 금고로 반환되었습니다.", color=discord.Color.dark_grey())
            await _raid_edit(s, embed=embed, view=None)
            return

        # ── 2) 전투 준비: 보스 체력은 참가자 공격력에 맞춰 자동 계산
        players = list(s.players.values())
        avg_atk = sum(p.atk for p in players) / len(players)
        s.boss_def = int(avg_atk * s.tier["def_ratio"])
        per_hit_total = sum(max(10, p.atk - s.boss_def) for p in players)
        s.boss_max = s.boss_hp = int(per_hit_total * s.tier["hits"])
        s.state = "fighting"
        s.fight_started = True
        now = time.time()
        s.fight_end = now + RAID_DURATION_SEC
        s.next_aoe = now + RAID_AOE_INTERVAL_SEC
        raid_family_cooldowns[s.fid] = datetime.now() + timedelta(seconds=RAID_FAMILY_COOLDOWN_SEC)
        s.log(f"전투 시작! 참가자 {len(players)}명")
        await _raid_edit(s, embed=raid_fight_embed(s), view=RaidFightView(s))

        # ── 3) 전투 루프
        win = False
        reason = ""
        while True:
            try:
                await asyncio.wait_for(s.wake.wait(), timeout=RAID_TICK_SEC)
            except asyncio.TimeoutError:
                pass
            s.wake.clear()
            now = time.time()

            if s.boss_hp <= 0:
                win, reason = True, "보스를 쓰러뜨렸습니다!"
                break
            if not s.enraged and s.boss_hp <= s.boss_max * 0.5:
                s.enraged = True
                s.next_aoe = min(s.next_aoe, now + RAID_AOE_INTERVAL_ENRAGE_SEC)
                s.log("보스가 격노했습니다! 광역 공격이 강해지고 더 자주 옵니다!")
            if now >= s.next_aoe:
                raid_do_aoe(s)
            if not any(p.alive for p in s.players.values()):
                reason = "공격대가 전멸했습니다..."
                break
            if now >= s.fight_end:
                reason = "제한 시간이 지났습니다..."
                break
            await _raid_edit(s, embed=raid_fight_embed(s))

        s.state = "done"

        # ── 4) 정산
        results, total_money = await asyncio.to_thread(_raid_settle_sync, s, win)
        if win:
            title, color = "[레이드 성공]", discord.Color.gold()
        else:
            title, color = "[레이드 실패]", discord.Color.dark_red()
        lines = []
        for p, money, honor in sorted(results, key=lambda r: -r[0].dmg_done):
            lines.append(f"• **{p.name}** — 딜 {p.dmg_done:,} ({p.hits}회) → `${money:,}`" + (f" / 명예 `+{honor}pt`" if honor else ""))
        if not lines:
            lines.append("공격에 참여한 사람이 없어 보상이 없습니다.")
        desc = f"{reason}\n\n" + "\n".join(lines)
        if win:
            desc += (f"\n\n조직 금고 `+${int(total_money * RAID_TREASURY_RETURN):,}` / 레이드 점수 `+{s.tier['points']}`"
                     f"\n**MVP:** {max(results, key=lambda r: r[0].dmg_done)[0].member.mention}" if results else "")
        else:
            desc += f"\n\n(실패해도 참전자에게는 소정의 위로금이 지급됩니다.)"
        embed = discord.Embed(title=f"{title} {s.tier['name']}", description=desc, color=color)
        await _raid_edit(s, embed=embed, view=None)
    except Exception as e:
        print(f"Raid error: {e}")
        if not s.fight_started:   # 전투 시작 전에 오류가 났다면 작전비 반환
            try:
                await asyncio.to_thread(_raid_refund_sync, s)
            except Exception:
                pass
        s.state = "done"
        await _raid_edit(s, embed=discord.Embed(title="[레이드 중단]", description="처리 중 오류가 발생해 레이드가 중단되었습니다.", color=discord.Color.red()), view=None)
    finally:
        s.state = "done"
        raid_release(s)


raid_group = app_commands.Group(name="레이드", description="[패밀리] 조직원들과 함께 레이드 보스에 도전합니다.")
bot.tree.add_command(raid_group)

@raid_group.command(name="시작", description="우리 조직의 레이드를 엽니다. (조직 금고에서 작전 자금이 소모됩니다)")
@app_commands.describe(보스="도전할 레이드 보스")
@app_commands.choices(보스=[
    app_commands.Choice(name=f"{t['name']} (인원 {t['min_players']}+ · 명예 {t['min_honor']:,}+ · 작전비 ${t['cost']:,})", value=k)
    for k, t in RAID_TIERS.items()
])
async def raid_start_cmd(interaction: discord.Interaction, 보스: app_commands.Choice[str]):
    if not await _family_precheck(interaction): return
    user = interaction.user
    tier = RAID_TIERS[보스.value]

    try:
        fam = family_get_by_user(interaction.guild_id, user.id)
        if not fam:
            await interaction.response.send_message("소속된 조직이 없습니다. `/조직 생성` 또는 초대를 받아 보세요.", ephemeral=True)
            return
        if fam["id"] in active_raids:
            await interaction.response.send_message("우리 조직의 레이드가 이미 진행 중입니다.", ephemeral=True)
            return
        if user.id in raid_user_map:
            await interaction.response.send_message("이미 레이드에 참가 중입니다.", ephemeral=True)
            return
        cd = raid_family_cooldowns.get(fam["id"])
        if cd and datetime.now() < cd:
            remain = int((cd - datetime.now()).total_seconds())
            await interaction.response.send_message(f"조직이 정비 중입니다. **{remain // 60}분 {remain % 60}초** 뒤에 다시 레이드를 열 수 있습니다.", ephemeral=True)
            return
        if len(fam.get("member_ids") or []) < tier["min_players"]:
            await interaction.response.send_message(f"이 레이드는 조직원이 최소 **{tier['min_players']}명** 이상이어야 합니다.", ephemeral=True)
            return
        if fam.get("treasury", 0) < tier["cost"]:
            await interaction.response.send_message(f"조직 금고가 부족합니다. (작전비 `${tier['cost']:,}` / 금고 `${fam.get('treasury', 0):,}`)\n`/조직 기부` 로 금고를 채워 보세요.", ephemeral=True)
            return
        info = get_user_data(user.id)
        if info["honor"] < tier["min_honor"]:
            await interaction.response.send_message(f"이 레이드는 명예 **{tier['min_honor']:,}pt** 이상만 도전할 수 있습니다.", ephemeral=True)
            return

        # 자리 선점 (여기부터 await 전까지는 다른 명령어가 끼어들 수 없음)
        session = RaidSession(fam, 보스.value, user)
        session.players[user.id] = RaidPlayer(user, compute_combat_stats(info["honor"], info.get("inventory", {})))
        active_raids[fam["id"]] = session
        raid_user_map[user.id] = fam["id"]
        try:
            family_update(fam["id"], {"treasury": fam["treasury"] - tier["cost"]})
        except Exception:
            raid_release(session)
            raise
    except Exception as e:
        print(f"Raid start error: {e}")
        await interaction.response.send_message(FAMILY_DB_ERROR_MSG, ephemeral=True)
        return

    await interaction.response.send_message(embed=raid_lobby_embed(session), view=RaidLobbyView(session))
    session.message = await interaction.original_response()
    asyncio.create_task(run_raid(session))

@raid_group.command(name="정보", description="레이드 보스 목록과 진행 방법을 확인합니다.")
async def raid_info_cmd(interaction: discord.Interaction):
    if not await check_maintenance(interaction): return
    lines = []
    for t in RAID_TIERS.values():
        lines.append(
            f"**{t['name']}**\n"
            f"└ 인원 {t['min_players']}명+ · 명예 {t['min_honor']:,}pt+ · 작전비 `${t['cost']:,}` (조직 금고)\n"
            f"└ 보상(1인 평균) `${t['money']:,}` / 명예 `+{t['honor']}pt` · 조직 점수 `+{t['points']}`"
        )
    embed = discord.Embed(
        title="조직 레이드 안내",
        description=("\n\n".join(lines) + "\n\n"
                     f"**진행:** `/레이드 시작` → 조직원이 `참가` ({RAID_LOBBY_SEC}초) → 전투 {RAID_DURATION_SEC // 60}분\n"
                     "**공격**: 보스 체력을 깎습니다.\n"
                     "**응급처치**: 쓰러진 동료를 되살리거나, 없으면 전원 체력을 회복합니다.\n"
                     "**방어 태세**: 10초 동안 광역 공격 피해가 절반! (광역 공격 직전에 사용하세요)\n"
                     "보스는 체력이 절반이 되면 **격노**하고, 시간 안에 못 잡거나 전멸하면 실패합니다.\n"
                     f"보상은 {int(RAID_BASE_SHARE * 100)}% 균등 + {100 - int(RAID_BASE_SHARE * 100)}% 딜량 비례이며, 한 번도 공격하지 않으면 받을 수 없습니다.\n"
                     f"조직 레이드 재도전 대기시간은 {RAID_FAMILY_COOLDOWN_SEC // 60}분입니다."),
        color=discord.Color.orange()
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


bot.run(os.getenv("DISCORD_TOKEN"))