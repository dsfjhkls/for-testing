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
# {guild_id: "dark" | "eden" | "ultimate" | None}  ← 서버에 지금 어떤 보스가 떠 있는지 (None/없음 = 보스 없음)
#   "dark"     = [히든보스] 어둠의 지배자  (HIDDEN_BOSS)
#   "eden"     = [테러보스] 에칼 데 페스토  (EDEN_BOSS)
#   "ultimate" = [최강보스] 태초의 파괴자  (ULTIMATE_BOSS)
active_boss_guilds = {}

# 일반 /전투 승리 시 '보스마다 각각' 출현할 확률 (테스트할 때는 0.3 처럼 크게 바꾸세요)
#   • 서버에 보스가 없을 때만 판정하고, 한 번에 보스 한 마리만 출현합니다.
#   • 보스가 3종이라 '아무 보스나' 나올 확률은 합계 9% 입니다. (BOSS_SPAWN_TABLE 참고)
BOSS_SPAWN_CHANCE = 0.03            # 3% (어둠의 지배자)
EDEN_BOSS_SPAWN_CHANCE = 0.03       # 3% (에칼 데 페스토)
ULTIMATE_BOSS_SPAWN_CHANCE = 0.03   # 3% (태초의 파괴자)

# {user_id: datetime (석방 예정 시간)}
jail_list = {}

# ==========================================
# [감옥 / 수배 DB 저장]
#   jail_list / wanted_list 는 빠른 조회용 메모리 캐시이고, 값이 바뀔 때마다 Supabase `user_status` 테이블에 저장됩니다.
#   봇이 재시작되면 on_ready 에서 아직 끝나지 않은 감옥·수배를 다시 불러옵니다.
#   → status_setup.sql 을 Supabase 에서 한 번 실행해 주세요.
#   → 테이블이 없어도 봇은 정상 동작하며, 그때는 예전처럼 메모리에만 저장됩니다.
# ==========================================
import threading as _status_threading

_status_lock = _status_threading.Lock()
_status_warned = False
_status_loaded = False
_status_tasks = set()   # 백그라운드 저장 작업이 중간에 사라지지 않도록 붙잡아 두는 집합


def _status_warn(action: str, err):
    global _status_warned
    if not _status_warned:
        _status_warned = True
        print(f"[경고] 감옥/수배 DB {action} 실패: {err}\n"
              f"       Supabase 에서 status_setup.sql 을 실행했는지 확인해 주세요. (그동안은 메모리에만 저장됩니다)")


def save_status_sync(user_id: int):
    """현재 메모리의 감옥/수배 상태를 DB 에 반영합니다. (둘 다 없으면 행을 삭제)"""
    if not supabase:
        return
    with _status_lock:   # 읽기와 쓰기를 한 덩어리로 묶어서, 연달아 저장해도 오래된 값이 덮어쓰지 않게 함
        now = datetime.now()
        jail = jail_list.get(user_id)
        wanted = wanted_list.get(user_id)
        jail = jail if jail and jail > now else None
        wanted = wanted if wanted and wanted > now else None
        try:
            if jail is None and wanted is None:
                supabase.table("user_status").delete().eq("user_id", str(user_id)).execute()
            else:
                supabase.table("user_status").upsert({
                    "user_id": str(user_id),
                    "jail_until": int(jail.timestamp()) if jail else None,
                    "wanted_until": int(wanted.timestamp()) if wanted else None,
                }, on_conflict="user_id").execute()
        except Exception as e:
            _status_warn("저장", e)


def save_status(user_id: int):
    """비동기 코드에서 호출: DB 저장을 백그라운드로 넘겨서 명령어 응답이 늦어지지 않게 합니다."""
    if not supabase:
        return
    try:
        task = asyncio.get_running_loop().create_task(asyncio.to_thread(save_status_sync, user_id))
        _status_tasks.add(task)
        task.add_done_callback(_status_tasks.discard)
    except RuntimeError:   # 실행 중인 이벤트 루프가 없으면 그냥 바로 저장
        save_status_sync(user_id)


def load_status_sync():
    """봇 시작 시 DB 에 저장된 감옥/수배를 메모리로 복구합니다. 반환: (수감자 수, 수배자 수)"""
    if not supabase:
        return 0, 0
    try:
        rows = supabase.table("user_status").select("*").execute().data or []
    except Exception as e:
        _status_warn("불러오기", e)
        return 0, 0

    now = datetime.now()
    jailed = wanted = 0
    for row in rows:
        try:
            uid = int(row["user_id"])
        except (KeyError, ValueError, TypeError):
            continue
        j = datetime.fromtimestamp(row["jail_until"]) if row.get("jail_until") else None
        w = datetime.fromtimestamp(row["wanted_until"]) if row.get("wanted_until") else None
        active = False

        if j and j > now:
            if uid not in jail_list or jail_list[uid] < j:
                jail_list[uid] = j
            jailed += 1
            active = True
        elif j:
            # 봇이 꺼져 있는 동안 형기가 끝난 수감자 → 위치가 '감옥' 으로 남지 않게 기본 거점으로 복구
            try:
                (supabase.table("user_data").update({"location": "레벤하임"})
                 .eq("user_id", str(uid)).eq("location", "감옥").execute())
            except Exception as e:
                print(f"DB Error (status restore location): {e}")

        if w and w > now:
            if uid not in wanted_list or wanted_list[uid] < w:
                wanted_list[uid] = w
            wanted += 1
            active = True

        if not active:   # 둘 다 끝난 행은 정리
            try:
                supabase.table("user_status").delete().eq("user_id", str(uid)).execute()
            except Exception:
                pass
    return jailed, wanted

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
            
            save_status(user_id)   # 석방 → DB 에서도 정리
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

# === 밸런스 상수 ===
# 목표 페이스 (쉬지 않고 열심히 플레이했을 때 기준 / 가끔 접속하는 유저는 이것의 몇 배):
#   행동대원(500pt) 약 1.5시간 → 솔다토(1,000) 약 3시간 → 카포(3,000) 약 7시간
#   → 언더보스(5,000) 약 11시간 → 대부(10,000) 약 19시간
# 페이스를 더 늘리고 싶으면 MISSIONS / NPC_DATA 의 명예(honor) 값을 줄이거나 쿨타임을 늘리면 됩니다.
MISSION_COOLDOWN_SEC = 40        # 임무 기본 쿨타임
MISSION_COOLDOWN_FAST_SEC = 30   # 크로노그래프 시계 / 특수 바이크 보유 시
NPC_COOLDOWN_SEC = 45            # /전투 쿨타임
BOSS_COOLDOWN_SEC = 60           # /보스 쿨타임
PARTTIME_COOLDOWN_SEC = 45       # /아르바이트 쿨타임
NPC_LOSS_PENALTY = 500           # NPC전 패배 시 치료비
DUEL_MAX_BET = 30                # 결투 1회 최대 명예 베팅 (부계정 명예 몰아주기 방지)

# === 지역별 암시장 아이템 목록 ===
# 실제 효과는 MISSION_BUFFS(임무) / compute_combat_stats(전투) / MissionSelect(쿨타임·소모품)에서 적용됩니다.
BLACK_MARKET_ITEMS = {
    "레벤하임": [
        {"id": "time_stop", "name": "시간 정지 회중시계", "price": 150000, "desc": "임무 쿨타임 완전 제거 (영구)"},
        {"id": "fail_guard", "name": "임무 실패 방지권", "price": 3000, "desc": "임무 실패 시 수배·벌금 1회 방어(소모) & 방어력 +20"},
        {"id": "chrono_watch", "name": "고급 크로노그래프 시계", "price": 16000, "desc": "임무 쿨타임 40초 → 30초"}
    ],
    "던베르크": [
        {"id": "heavy_glove", "name": "강화 작업용 장갑", "price": 3000, "desc": "임무 보상금 +15%"},
        {"id": "boost_drink", "name": "고농축 자양강장제", "price": 1500, "desc": "임무 쿨타임 1회 즉시 초기화(소모)"},
        {"id": "quick_bike", "name": "통신용 특수 바이크", "price": 16000, "desc": "임무 쿨타임 40초 → 30초"}
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
NO_COOLDOWN_ITEM = "시간 정지 회중시계"   # 보유 시 임무 쿨타임 없음 (가격은 BLACK_MARKET_ITEMS 에서 조절)

# === 임무 목록 (드롭다운 표시와 실제 판정이 같은 표를 쓰도록 통합) ===
# success_rate(%) / reward(최소, 최대 $) / honor(pt) / fine(실패 시 벌금 $)
MISSIONS = {
    "mission_1": {"title": "타 조직 구역 비밀 조사",   "label": "타 조직 구역 비밀 조사", "diff": "하",   "success_rate": 85, "reward": (150, 350),   "honor": 3,  "fine": 80},
    "mission_2": {"title": "밀수품 비밀 운송 작업",     "label": "밀수품 비밀 운송 작업", "diff": "중",   "success_rate": 60, "reward": (350, 800),   "honor": 6,  "fine": 200},
    "mission_3": {"title": "라이벌 보스의 장부 탈취",   "label": "라이벌 보스의 장부 탈취", "diff": "상",   "success_rate": 40, "reward": (900, 1900),  "honor": 8,  "fine": 500},
    "mission_4": {"title": "기밀 문서 야간 밀반출",     "label": "기밀 문서 야간 밀반출", "diff": "중상", "success_rate": 50, "reward": (650, 1400),  "honor": 7,  "fine": 350},
    "mission_5": {"title": "중앙 은행 호송 차량 습격",  "label": "중앙 은행 호송 차량 습격", "diff": "최상", "success_rate": 20, "reward": (2500, 5000), "honor": 18, "fine": 1200},
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
    shop_w = (0, 0)   # 상점 무기: 위에서부터 먼저 보유한 하나만
    for name, a, h in WEAPON_TIERS:
        if name in inventory:
            shop_w = (a, h)
            break
    # 제작 장비(/제작): 무기는 상점 무기와 비교해 더 강한 쪽, 방어구는 가장 강한 1개만 적용
    craft_w, craft_a = crafted_gear_best(inventory)
    use_w = max(shop_w, (craft_w["atk"], craft_w["hp"])) if craft_w else shop_w
    atk += use_w[0]
    hp += use_w[1]
    if craft_a:
        def_val += craft_a["def"]
        hp += craft_a["hp"]
    if "임무 실패 방지권" in inventory: def_val += 20
    for it in POLICE_SHOP_ITEMS:   # 치안국 보급품(강화 방탄복·황금 배지)의 방어력
        if it.get("def_bonus") and it["name"] in inventory:
            def_val += it["def_bonus"]
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

# 삭제된 아이템: 보유 중이면 인벤토리에서 빼고 구매가를 돌려줍니다. (환불이 필요 없으면 이 표를 비우세요)
REMOVED_ITEM_REFUNDS = {"치안국 위조 신분증": 5000}

def _refund_removed_items(user_info: dict) -> dict:
    inv = user_info.get("inventory") or {}
    refund = 0
    for name, price in REMOVED_ITEM_REFUNDS.items():
        if name in inv:
            inv.pop(name, None)
            refund += price
    if refund:
        user_info["inventory"] = inv
        user_info["money"] = (user_info.get("money") or 0) + refund
        try:
            supabase.table("user_data").update({
                "money": user_info["money"],
                "inventory": inv
            }).eq("user_id", str(user_info["user_id"])).execute()
        except Exception as e:
            print(f"DB Error (removed item refund): {e}")
    return user_info

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
            user_info = _refund_removed_items(user_info)
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
    "respected":    {"name": "존경받는 자",     "desc": "조직 명예 300pt 이상 (솔다토 이상)",   "check": lambda s, i: (i.get("honor") or 0) >= 300},
    "collector":    {"name": "장비 수집가",     "desc": "암시장 장비 10개 이상 보유",            "check": lambda s, i: len([k for k in (i.get("inventory") or {}) if not k.startswith(("[전리품]", "[재료]", "[제작]"))]) >= 10},
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
                "attendance",
                "fight_wins", "named_kills", "boss_kills",            # ← 업적용 카운터
                "duel_wins", "raid_clears", "raid_king_clears", "war_wins", "war_conquests"):
        stats[key] = int(stats.get(key, 0) or 0)
    blob["stats"] = stats
    blob["titles"] = list(blob.get("titles") or [])
    ach = blob.get("achievements")                                    # {업적id: 달성 시각(epoch)}
    blob["achievements"] = dict(ach) if isinstance(ach, dict) else {}
    blob["ach_claimed"] = list(blob.get("ach_claimed") or [])           # 보상을 이미 받은 업적 id
    blob["equipped"] = blob.get("equipped")
    return blob

class _NewIds(list):
    """새로 얻은 칭호 id 목록. .ach 에는 새로 달성한 업적 id 가 들어 있습니다.
    (업적만 새로 달성했어도 True 로 취급 → 기존 'if new_ids: 저장' 코드가 그대로 동작)"""
    ach = ()
    def __bool__(self):
        return len(self) > 0 or bool(self.ach)

_pending_ach = {}   # {user_id(str): [업적id]}  레이드·구역전처럼 결과 화면에 바로 못 붙이는 곳에서 달성한 업적의 알림 대기열

def evaluate_new_titles(blob: dict, user_info: dict, _pop_pending: bool = True) -> list:
    new_ids = _NewIds()
    for tid, t in TITLES.items():
        if tid not in blob["titles"] and t["check"](blob["stats"], user_info):
            blob["titles"].append(tid)
            new_ids.append(tid)
    if len(new_ids) and blob.get("equipped") not in TITLES:
        blob["equipped"] = new_ids[0]
    pend = []
    uid = str(user_info.get("user_id") or "")
    if _pop_pending and uid:
        pend = _pending_ach.pop(uid, [])                       # 대기 중이던 달성 알림도 함께 표시
    new_ids.ach = pend + evaluate_new_achievements(blob, user_info)   # 업적도 같은 타이밍에 함께 검사
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
    out = ""
    if len(new_ids):
        names = ", ".join(TITLES[t]["name"] for t in new_ids)
        out += f"\n\n**[새 칭호 획득]** {names}\n`/칭호` 명령어로 장착할 수 있습니다."
    ach = list(getattr(new_ids, "ach", ()) or ())
    if ach:
        shown = [f"[{ACH_BY_ID[a]['tier']}] {ACH_BY_ID[a]['name']}" for a in ach[:5]]
        more = f" 외 {len(ach) - 5}개" if len(ach) > 5 else ""
        out += f"\n\n**[업적 달성]** {', '.join(shown)}{more}\n`/업적` 에서 보상을 받고 증명서를 발급할 수 있습니다."
    return out

# ==========================================
# [업적 시스템]  /업적
#   • 등급: 기초 / 숙련 / 심화
#       기초·숙련 : 이름과 달성 조건, 진행도가 모두 보입니다.
#       심화      : hide="name" → 이름만 보이고 조건은 ???
#                   hide="full" → 이름도 조건도 ???   (달성하면 둘 다 공개)
#   • 달성 기록은 user_data.data["achievements"] 에 {업적id: 시각} 으로 저장됩니다. (DB 테이블 변경 없음)
#   • 달성 판정은 칭호와 같은 시점(evaluate_new_titles)에 함께 이뤄집니다.
#
#   새 업적 추가 방법: ACHIEVEMENTS 리스트에 _ach(...) 한 줄만 추가하면 됩니다.
#       cur  = lambda s, i: 현재 수치   (s: 통계 stats, i: 유저 데이터 — money / honor / inventory)
#       goal = 목표 수치 (cur >= goal 이면 달성)
# ==========================================
_CRAFT_ALL_GOAL = 20   # /제작 레시피 총 개수 (아래에서 CRAFT_RECIPES 가 정의되면 자동으로 맞춰집니다)
ACH_TIERS = ("기초", "숙련", "심화")
ACH_TIER_COLORS = {"기초": 0x95A5A6, "숙련": 0x3498DB, "심화": 0x9B59B6}

def _inv_count(i: dict, prefix: str) -> int:
    return sum(1 for k in (i.get("inventory") or {}) if k.startswith(prefix))

def _shop_weapon_count(i: dict) -> int:
    inv = i.get("inventory") or {}
    return sum(1 for name, _a, _h in WEAPON_TIERS if name in inv)

def _ach(aid, tier, name, desc, cur, goal, hide=None):
    return {"id": aid, "tier": tier, "name": name, "desc": desc, "cur": cur, "goal": goal, "hide": hide}

ACHIEVEMENTS = [
    # ── 기초 (조건 공개) ──
    _ach("b_mission_1",  "기초", "첫 의뢰",             "비밀 임무 1회 성공",                lambda s, i: s["success"],              1),
    _ach("b_mission_10", "기초", "손에 익은 일",         "비밀 임무 10회 성공",               lambda s, i: s["success"],              10),
    _ach("b_fight_5",    "기초", "골목 싸움꾼",          "NPC 전투 5회 승리",                 lambda s, i: s["fight_wins"],           5),
    _ach("b_gamble_5",   "기초", "호기심 많은 손님",     "도박 5판 플레이",                   lambda s, i: s["gamble_played"],        5),
    _ach("b_weapon_1",   "기초", "무장",                "무기상점 무기 1개 보유",            lambda s, i: _shop_weapon_count(i),     1),
    _ach("b_money_10k",  "기초", "두둑한 주머니",        "보유 자금 $10,000 이상",            lambda s, i: i.get("money") or 0,       10000),
    _ach("b_honor_500",  "기초", "행동대원",            "명예 500pt 이상",                   lambda s, i: i.get("honor") or 0,       500),
    _ach("b_attend_3",   "기초", "얼굴도장",            "일일 의뢰 출석 3스택",              lambda s, i: s["attendance"],           3),
    _ach("b_duel_1",     "기초", "첫 결투 승리",         "결투 1회 승리",                     lambda s, i: s["duel_wins"],            1),
    _ach("b_raid_1",     "기초", "혼자가 아닌 싸움",     "레이드 1회 클리어",                 lambda s, i: s["raid_clears"],          1),

    # ── 숙련 (조건 공개) ──
    _ach("s_mission_50", "숙련", "베테랑 조직원",        "비밀 임무 50회 성공",               lambda s, i: s["success"],              50),
    _ach("s_streak_5",   "숙련", "흐름을 탄 자",         "비밀 임무 5연속 성공",              lambda s, i: s["best_streak"],          5),
    _ach("s_hard_5",     "숙련", "장부 전문 도둑",       "'라이벌 보스의 장부 탈취' 5회 성공", lambda s, i: s["hard_success"],         5),
    _ach("s_fight_50",   "숙련", "백전노장",            "NPC 전투 50회 승리",                lambda s, i: s["fight_wins"],           50),
    _ach("s_named_2",    "숙련", "이름 있는 사냥감",     "네임드 고유 전리품 2종 획득",       lambda s, i: _inv_count(i, "[전리품]"), 2),
    _ach("s_craft_3",    "숙련", "솜씨 좋은 대장장이",   "/제작 장비 3종 보유",               lambda s, i: _inv_count(i, "[제작]"),   3),
    _ach("s_wager_100k", "숙련", "판돈이 큰 사람",       "도박 누적 베팅액 $100,000 이상",    lambda s, i: s["gamble_wagered"],       100000),
    _ach("s_honor_3000", "숙련", "카포의 자리",          "명예 3,000pt 이상",                 lambda s, i: i.get("honor") or 0,       3000),
    _ach("s_money_100k", "숙련", "검은 금고",            "보유 자금 $100,000 이상",           lambda s, i: i.get("money") or 0,       100000),
    _ach("s_duel_10",    "숙련", "결투장의 단골",        "결투 10회 승리",                    lambda s, i: s["duel_wins"],            10),
    _ach("s_raid_10",    "숙련", "레이드 베테랑",        "레이드 10회 클리어",                lambda s, i: s["raid_clears"],          10),
    _ach("s_war_3",      "숙련", "구역의 수호자",        "구역 점령전 3회 승리",              lambda s, i: s["war_wins"],             3),

    # ── 심화 (hide="name": 이름만 공개 / hide="full": 이름도 ???) ──
    _ach("a_boss_1",     "심화", "재앙을 쓰러뜨린 자",   "보스 1회 처치 (/보스)",             lambda s, i: s["boss_kills"],           1,        hide="name"),
    _ach("a_named_all",  "심화", "네임드 사냥 완결",     "네임드 몬스터 4종 모두 처치",       lambda s, i: _inv_count(i, "[전리품]"), 4,        hide="name"),
    _ach("a_mission_300","심화", "그림자 속의 전설",     "비밀 임무 300회 성공",              lambda s, i: s["success"],              300,      hide="name"),
    _ach("a_honor_10k",  "심화", "대부의 반열",          "명예 10,000pt 이상",                lambda s, i: i.get("honor") or 0,       10000,    hide="name"),
    _ach("a_money_1m",   "심화", "도시를 살 수 있는 돈", "보유 자금 $1,000,000 이상",         lambda s, i: i.get("money") or 0,       1000000,  hide="name"),
    _ach("a_attend_30",  "심화", "한 달의 약속",         "일일 의뢰 출석 30스택",             lambda s, i: s["attendance"],           30,       hide="name"),
    _ach("a_jackpot",    "심화", "7이 세 번",            "슬롯머신 잭팟 당첨",                lambda s, i: s["jackpot"],              1,        hide="full"),
    _ach("a_broke",      "심화", "바닥을 본 자",         "도박으로 보유 자금이 $0이 됨",      lambda s, i: s["broke"],                1,        hide="full"),
    _ach("a_streak_10",  "심화", "신들린 손",            "비밀 임무 10연속 성공",             lambda s, i: s["best_streak"],          10,       hide="full"),
    _ach("a_boss_5",     "심화", "종말의 사냥꾼",        "보스 5회 처치",                     lambda s, i: s["boss_kills"],           5,        hide="full"),
    _ach("a_craft_all",  "심화", "완전한 장인",          "/제작 장비를 전부 제작",            lambda s, i: _inv_count(i, "[제작]"),   _CRAFT_ALL_GOAL, hide="full"),
    _ach("a_raid_king",  "심화", "지하의 왕을 끌어내린 자", "'칼렌데 지하 황제' 레이드 클리어",   lambda s, i: s["raid_king_clears"],     1,        hide="name"),
    _ach("a_duel_50",    "심화", "결투장의 지배자",      "결투 50회 승리",                    lambda s, i: s["duel_wins"],            50,       hide="full"),
    _ach("a_war_conq_5", "심화", "도시의 새 주인",       "구역 점령전에서 구역을 5회 빼앗음", lambda s, i: s["war_conquests"],        5,        hide="full"),
]
ACH_BY_ID = {a["id"]: a for a in ACHIEVEMENTS}

# ── 업적 보상 (/업적 의 [보상 수령] 버튼으로 받습니다) ──
#   등급별 기본 보상. 특정 업적만 다르게 주고 싶으면 ACH_REWARD_OVERRIDES 에 {"업적id": {"money": .., "honor": .., "title": True}} 추가
#   "title": True 이면 그 업적 이름의 전용 칭호가 함께 지급됩니다. (/칭호 에서 장착)
ACH_REWARDS = {
    "기초": {"money": 1000,  "honor": 0},
    "숙련": {"money": 5000,  "honor": 10},
    "심화": {"money": 30000, "honor": 50, "title": True},
}
ACH_REWARD_OVERRIDES = {}

def ach_reward(a: dict) -> dict:
    return ACH_REWARD_OVERRIDES.get(a["id"]) or ACH_REWARDS[a["tier"]]

def ach_title_id(a: dict):
    """전용 칭호 id (없으면 None)"""
    return "ach_" + a["id"] if ach_reward(a).get("title") else None

for _a in ACHIEVEMENTS:             # 업적 전용 칭호를 칭호 목록에 등록 (check=False → 수령 때만 지급)
    _tid = ach_title_id(_a)
    if _tid:
        TITLES[_tid] = {"name": _a["name"], "desc": f"{_a['tier']} 업적 달성 기념 칭호", "check": lambda s, i: False}

import threading as _ach_threading
_ach_lock = _ach_threading.RLock()   # 업적 카운터·보상 수령이 서로의 data 컬럼을 덮어쓰지 않도록

def bump_user_stats_sync(user_id: int, incs: dict, pending: bool = False):
    """통계 카운터를 올리고 새 업적을 판정·저장합니다. (동기 DB 작업 → to_thread 또는 정산 스레드에서 호출)
    pending=True 이면 달성 알림을 대기열에 넣어 두었다가 그 유저의 다음 /업적·전투·도박 결과에 표시합니다."""
    _require_db()
    with _ach_lock:
        info = get_user_data(user_id)
        blob = get_title_blob(info)
        for k, v in incs.items():
            blob["stats"][k] = int(blob["stats"].get(k, 0)) + int(v)
        new_ids = evaluate_new_titles(blob, info, _pop_pending=False)
        save_title_blob(user_id, blob)
    if pending and new_ids.ach:
        _pending_ach.setdefault(str(user_id), []).extend(new_ids.ach)
    return new_ids

def claim_ach_rewards_sync(user_id: int):
    """달성했지만 아직 받지 않은 업적 보상을 한 번에 지급. 받을 게 없으면 None."""
    _require_db()
    with _ach_lock:
        info = get_user_data(user_id)
        blob = get_title_blob(info)
        evaluate_new_titles(blob, info, _pop_pending=False)              # 최신 달성 상태 반영
        claimed = set(blob["ach_claimed"])
        todo = [a for a in ACHIEVEMENTS if a["id"] in blob["achievements"] and a["id"] not in claimed]
        if not todo:
            return None
        money = sum(ach_reward(a).get("money", 0) for a in todo)
        honor = sum(ach_reward(a).get("honor", 0) for a in todo)
        update_user_stats(user_id, money, honor)                          # 돈·명예 먼저 (실패하면 여기서 중단 → 중복 수령 없음)
        got_titles = []
        for a in todo:
            claimed.add(a["id"])
            tid = ach_title_id(a)
            if tid and tid not in blob["titles"]:
                blob["titles"].append(tid)
                got_titles.append(tid)
        blob["ach_claimed"] = list(claimed)
        if got_titles and blob.get("equipped") not in TITLES:
            blob["equipped"] = got_titles[0]
        save_title_blob(user_id, blob)
        return {"todo": todo, "money": money, "honor": honor, "titles": got_titles}

def evaluate_new_achievements(blob: dict, user_info: dict) -> list:
    """새로 달성한 업적 id 목록을 돌려주고 blob['achievements'] 에 기록합니다. (저장은 호출한 쪽에서)"""
    done = blob.setdefault("achievements", {})
    now = int(time.time())
    newly = []
    for a in ACHIEVEMENTS:
        if a["id"] in done:
            continue
        try:
            reached = a["cur"](blob["stats"], user_info) >= a["goal"]
        except Exception as e:
            print(f"[업적 판정 오류] {a['id']}: {e}")
            continue
        if reached:
            done[a["id"]] = now
            newly.append(a["id"])
    return newly

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
    locked = [f"• ??? — {t['desc']}" for tid, t in TITLES.items() if tid not in blob["titles"] and not tid.startswith("ach_")]
    total_titles = sum(1 for tid in TITLES if not tid.startswith("ach_") or tid in blob["titles"])

    embed = discord.Embed(
        title=f"[{user.name} 님의 칭호]",
        description=f"현재 장착 중인 칭호: **{equipped_name}**" + format_new_titles(new_ids or []),
        color=discord.Color.gold()
    )
    embed.add_field(name=f"획득한 칭호 ({len(earned)}/{total_titles})", value="\n".join(earned) if earned else "*아직 획득한 칭호가 없습니다.*", inline=False)
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

# ==========================================================================
# [/업적]  기초 · 숙련 · 심화 업적 확인 / 보상 수령 / 증명서 발급
#   • 기초·숙련은 조건과 진행도가 보이고, 심화는 이름만 보이거나(???) 이름도 가려져 있다가 달성하면 공개됩니다.
#   • [보상 수령] 버튼: 달성했지만 아직 안 받은 보상(자금·명예, 심화는 전용 칭호)을 한 번에 지급
#   • 아래 선택 메뉴: 달성한 업적의 '업적 증명서' 이미지를 발급 (/프로필 과 같은 종이 배경)
# ==========================================================================
def _ach_fmt(n) -> str:
    return f"{int(n):,}"

def _ach_bar(cur, goal, width: int = 10) -> str:
    ratio = 0 if goal <= 0 else max(0.0, min(1.0, cur / goal))
    filled = int(ratio * width)
    return "█" * filled + "░" * (width - filled)

def _ach_reward_text(a: dict) -> str:
    r = ach_reward(a)
    parts = [f"${r.get('money', 0):,}"]
    if r.get("honor"):
        parts.append(f"명예 +{r['honor']}pt")
    if r.get("title"):
        parts.append("전용 칭호")
    return " · ".join(parts)

def _ach_unclaimed(blob: dict) -> list:
    claimed = set(blob["ach_claimed"])
    return [a for a in ACHIEVEMENTS if a["id"] in blob["achievements"] and a["id"] not in claimed]

def _ach_line(a: dict, blob: dict, info: dict) -> str:
    aid, done = a["id"], blob["achievements"]
    if aid in done:                       # 달성: 등급과 상관없이 이름·조건 모두 공개
        state = "보상 수령 완료" if aid in blob["ach_claimed"] else "보상 수령 가능"
        return f"**{a['name']}**  `달성`\n└ {a['desc']} · <t:{int(done[aid])}:d>\n└ {state} ({_ach_reward_text(a)})"

    hide = a.get("hide")
    if hide == "full":                    # 이름도 조건도 비공개
        return "**???**\n└ 조건: ???"
    if hide == "name":                    # 이름만 공개
        return f"**{a['name']}**\n└ 조건: ???"

    try:                                  # 기초·숙련: 조건 + 진행도 + 보상 공개
        cur = min(int(a["cur"](blob["stats"], info)), a["goal"])
    except Exception:
        cur = 0
    return (f"**{a['name']}**\n└ {a['desc']}\n"
            f"└ `{_ach_bar(cur, a['goal'])}` {_ach_fmt(cur)} / {_ach_fmt(a['goal'])}\n"
            f"└ 보상: {_ach_reward_text(a)}")

def ach_overview_embed(user: discord.abc.User, blob: dict, info: dict, new_ids=None) -> discord.Embed:
    done = blob["achievements"]
    total_done = sum(1 for a in ACHIEVEMENTS if a["id"] in done)
    lines = []
    for tier in ACH_TIERS:
        items = [a for a in ACHIEVEMENTS if a["tier"] == tier]
        n = sum(1 for a in items if a["id"] in done)
        lines.append(f"**{tier}**  `{_ach_bar(n, len(items))}` {n} / {len(items)}")

    unclaimed = _ach_unclaimed(blob)
    claim_txt = f"\n**수령 가능한 보상 {len(unclaimed)}개** — 아래 [보상 수령] 버튼을 눌러주세요." if unclaimed else ""

    recent = sorted((a for a in ACHIEVEMENTS if a["id"] in done), key=lambda a: done[a["id"]], reverse=True)[:5]
    recent_txt = "\n".join(f"• [{a['tier']}] {a['name']} · <t:{int(done[a['id']])}:R>" for a in recent) or "*아직 달성한 업적이 없습니다.*"

    embed = discord.Embed(
        title=f"[{user.display_name} 님의 업적]",
        description=(f"달성 **{total_done} / {len(ACHIEVEMENTS)}**\n\n" + "\n".join(lines) + claim_txt
                     + "\n\n버튼으로 등급별 업적을 확인하고, 아래 메뉴에서 달성한 업적의 **증명서**를 발급받을 수 있습니다.\n"
                       "기초·숙련은 조건이 공개되고, **심화**는 달성해야 모든 내용이 드러납니다."
                     + format_new_titles(new_ids or [])),
        color=discord.Color.gold()
    )
    embed.add_field(name="최근 달성", value=recent_txt, inline=False)
    if user.avatar:
        embed.set_thumbnail(url=user.avatar.url)
    return embed

def ach_tier_embed(user: discord.abc.User, tier: str, blob: dict, info: dict) -> discord.Embed:
    done = blob["achievements"]
    items = [a for a in ACHIEVEMENTS if a["tier"] == tier]
    n = sum(1 for a in items if a["id"] in done)
    text = "\n\n".join(_ach_line(a, blob, info) for a in items)
    if len(text) > 4000:
        text = text[:3990] + "…"
    embed = discord.Embed(title=f"[{tier} 업적]  {n} / {len(items)}", description=text,
                          color=discord.Color(ACH_TIER_COLORS[tier]))
    if tier == "심화":
        embed.set_footer(text="심화 업적은 달성하기 전까지 조건(또는 이름)이 ??? 로 표시됩니다.")
    return embed

async def _ach_owner_only(interaction: discord.Interaction, view) -> bool:
    if interaction.user.id != view.owner.id:
        await interaction.response.send_message("타인의 업적 창은 조작할 수 없습니다.", ephemeral=True)
        return False
    return True

class AchTierButton(discord.ui.Button):
    def __init__(self, key: str, label: str, style=discord.ButtonStyle.secondary):
        super().__init__(label=label, style=style, row=0)
        self.key = key

    async def callback(self, interaction: discord.Interaction):
        view: "AchievementView" = self.view
        if not await _ach_owner_only(interaction, view): return
        await interaction.response.defer()
        view.current = self.key
        view.refresh()
        await interaction.edit_original_response(embed=view.make_embed(), view=view)

class AchClaimButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="보상 수령", style=discord.ButtonStyle.success, row=0)

    async def callback(self, interaction: discord.Interaction):
        view: "AchievementView" = self.view
        if not await _ach_owner_only(interaction, view): return
        await interaction.response.defer()
        uid = view.owner.id
        try:
            result = await asyncio.to_thread(claim_ach_rewards_sync, uid)
            info, blob, _ = await asyncio.to_thread(_load_achievements_sync, uid)
        except Exception as e:
            print(f"Achievement claim error: {e}")
            await interaction.followup.send("보상을 지급하는 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.", ephemeral=True)
            return
        view.info, view.blob = info, blob
        view.refresh()
        await interaction.edit_original_response(embed=view.make_embed(), view=view)
        if not result:
            await interaction.followup.send("지금 수령할 수 있는 업적 보상이 없습니다.", ephemeral=True)
            return

        names = "\n".join(f"• [{a['tier']}] {a['name']} — {_ach_reward_text(a)}" for a in result["todo"][:15])
        if len(result["todo"]) > 15:
            names += f"\n… 외 {len(result['todo']) - 15}개"
        embed = discord.Embed(
            title="[업적 보상 수령]",
            description=(f"{view.owner.mention} 님이 업적 보상 **{len(result['todo'])}개**를 받았습니다!\n\n{names}\n\n"
                         f"**합계:** `${result['money']:,}`" + (f" · 명예 `+{result['honor']} pt`" if result["honor"] else "")
                         + (f"\n**새 칭호:** {', '.join(TITLES[t]['name'] for t in result['titles'])}\n`/칭호` 에서 장착할 수 있습니다." if result["titles"] else "")),
            color=discord.Color.green()
        )
        await interaction.followup.send(embed=embed)

_cert_cooldowns = {}   # {user_id: 마지막 발급 시각}
CERT_COOLDOWN_SEC = 10

class AchCertSelect(discord.ui.Select):
    def __init__(self, blob: dict):
        done = blob["achievements"]
        cleared = sorted((a for a in ACHIEVEMENTS if a["id"] in done),
                         key=lambda a: (-ACH_TIERS.index(a["tier"]), -done[a["id"]]))[:25]   # 심화 → 숙련 → 기초, 최근 순
        options = [discord.SelectOption(label=a["name"][:100], value=a["id"],
                                        description=f"[{a['tier']}] {a['desc']}"[:100]) for a in cleared]
        super().__init__(placeholder="증명서를 발급받을 업적을 선택하세요...", min_values=1, max_values=1, options=options, row=1)

    async def callback(self, interaction: discord.Interaction):
        view: "AchievementView" = self.view
        if not await _ach_owner_only(interaction, view): return
        uid = interaction.user.id
        now = time.monotonic()
        if now - _cert_cooldowns.get(uid, 0) < CERT_COOLDOWN_SEC:
            await interaction.response.send_message(f"증명서는 {CERT_COOLDOWN_SEC}초에 한 장씩 발급할 수 있습니다.", ephemeral=True)
            return
        a = ACH_BY_ID.get(self.values[0])
        if a is None or a["id"] not in view.blob["achievements"]:
            await interaction.response.send_message("아직 달성하지 못한 업적입니다.", ephemeral=True)
            return
        _cert_cooldowns[uid] = now
        await interaction.response.defer()
        try:
            buf = await asyncio.to_thread(make_achievement_certificate,
                                          interaction.user.display_name, a, view.blob["achievements"][a["id"]], uid)
        except Exception as e:
            print(f"Achievement certificate error: {e}")
            await interaction.followup.send("증명서를 발급하는 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.", ephemeral=True)
            return
        await interaction.followup.send(content=f"{interaction.user.mention} 님의 **[{a['tier']}] {a['name']}** 업적 증명서입니다.",
                                        file=discord.File(buf, filename=f"achievement_{a['id']}.png"))

class AchievementView(discord.ui.View):
    def __init__(self, owner: discord.abc.User, blob: dict, info: dict):
        super().__init__(timeout=180)
        self.owner, self.blob, self.info = owner, blob, info
        self.current = "__all__"
        self.interaction = None
        self.refresh()

    def refresh(self):
        """현재 탭 / 수령 가능 개수 / 발급 가능한 업적 목록에 맞춰 버튼과 메뉴를 다시 만든다."""
        self.clear_items()
        self.add_item(AchTierButton("__all__", "전체", discord.ButtonStyle.primary if self.current == "__all__" else discord.ButtonStyle.secondary))
        for tier in ACH_TIERS:
            self.add_item(AchTierButton(tier, tier, discord.ButtonStyle.primary if self.current == tier else discord.ButtonStyle.secondary))
        n = len(_ach_unclaimed(self.blob))
        claim = AchClaimButton()
        claim.label = f"보상 수령 ({n})" if n else "보상 수령"
        claim.disabled = n == 0
        self.add_item(claim)
        if self.blob["achievements"]:
            self.add_item(AchCertSelect(self.blob))

    def make_embed(self) -> discord.Embed:
        if self.current == "__all__":
            return ach_overview_embed(self.owner, self.blob, self.info)
        return ach_tier_embed(self.owner, self.current, self.blob, self.info)

    async def on_timeout(self):
        if self.interaction is None:
            return
        try:
            await self.interaction.edit_original_response(view=None)
        except discord.HTTPException:
            pass

def _load_achievements_sync(user_id: int):
    with _ach_lock:
        info = get_user_data(user_id)
        blob = get_title_blob(info)
        new_ids = evaluate_new_titles(blob, info)     # 칭호·업적을 함께 판정 (대기 중이던 알림 포함)
        if new_ids:
            save_title_blob(user_id, blob)
    return info, blob, new_ids

@bot.tree.command(name="업적", description="[마피아 GM] 업적 확인 · 보상 수령 · 증명서 발급 (기초 · 숙련 · 심화)")
async def achievements_command(interaction: discord.Interaction):
    if not await check_maintenance(interaction): return
    if not await check_membership(interaction): return

    await interaction.response.defer()
    user = interaction.user
    info, blob, new_ids = await asyncio.to_thread(_load_achievements_sync, user.id)
    view = AchievementView(user, blob, info)
    view.interaction = interaction
    await interaction.followup.send(embed=ach_overview_embed(user, blob, info, new_ids), view=view)

@bot.event
async def on_ready():
    global _status_loaded
    if not _status_loaded:   # on_ready 는 재연결 때 또 불릴 수 있으므로 처음 한 번만
        _status_loaded = True
        try:
            jailed, wanted = await asyncio.to_thread(load_status_sync)
            print(f"감옥/수배 상태 복구: 수감 {jailed}명 / 수배 {wanted}명")
        except Exception as e:
            print(f"감옥/수배 상태 복구 실패: {e}")
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

# === [경찰 전용] `/검거` 성공 시 아이템 압류 ===
ARREST_SEIZE_MIN = 1   # 한 번에 압류하는 아이템 종류 수 (최소)
ARREST_SEIZE_MAX = 2   # 한 번에 압류하는 아이템 종류 수 (최대)

def seize_items_sync(police_id: int, target_id: int) -> list:
    """검거된 조직원의 아이템 중 랜덤 1~2종을 1개씩 빼앗아 경찰 인벤토리에 넣습니다. (동기 DB 작업 → to_thread 로 실행)
    재료와 거래 불가 아이템(네임드 전리품 등)은 압류하지 않습니다. 반환: 압류한 아이템 이름 목록"""
    _require_db()
    with _inv_lock:
        t_inv = dict(get_user_data(target_id).get("inventory") or {})
        candidates = [k for k, v in t_inv.items() if int(v or 0) > 0 and not k.startswith("[재료]") and auction_blocked_reason(k) is None]
        if not candidates:
            return []
        picked = random.sample(candidates, min(random.randint(ARREST_SEIZE_MIN, ARREST_SEIZE_MAX), len(candidates)))
        new_t_inv = dict(t_inv)
        for k in picked:
            if int(new_t_inv[k]) <= 1:
                new_t_inv.pop(k, None)
            else:
                new_t_inv[k] = int(new_t_inv[k]) - 1

        p_inv = dict(get_user_data(police_id).get("inventory") or {})
        for k in picked:
            p_inv[k] = int(p_inv.get(k, 0) or 0) + 1

        # '조직원에게서 빼앗기 → 경찰에게 넣기' 순서로 저장하고, 경찰 쪽 저장이 실패하면 조직원 인벤토리를 되돌린다
        res = supabase.table("user_data").update({"inventory": new_t_inv}).eq("user_id", str(target_id)).execute()
        _check_updated(res, "seize (target)")
        try:
            res = supabase.table("user_data").update({"inventory": p_inv}).eq("user_id", str(police_id)).execute()
            _check_updated(res, "seize (police)")
        except Exception:
            try:
                supabase.table("user_data").update({"inventory": t_inv}).eq("user_id", str(target_id)).execute()
            except Exception as e2:
                print(f"DB Error (seize rollback): {e2}")
            raise
        return picked

# === [경찰 전용] `/검거` 명령어 (서버 공개 알림형) ===
@bot.tree.command(name="검거", description="[경찰 전용] 지명수배 중인 마피아를 체포하여 감옥에 가두고 소지품 일부를 압류합니다.")
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
            save_status(target_id)
        # 수배 상태가 아닐 때는 나만 보이게(ephemeral=True) 처리
        await interaction.followup.send(f"**{member.display_name}** 님은 현재 지명수배 상태가 아니거나 증거가 인멸되었습니다.", ephemeral=True)
        return

    # 수배 해제 및 감옥 수감 (3분)
    wanted_list.pop(matched_key, None)
    jail_time_seconds = 180 
    jail_list[target_id] = now + timedelta(seconds=jail_time_seconds)
    save_status(target_id)

    # DB 위치 변경
    try:
        update_user_location(target_id, "감옥")
    except Exception as e:
        print(f"Location update error: {e}")

    # 경찰 보상 지급 (무전기: 포상금 +20% / 황금 배지: 명예 +20%)
    _pb = police_bonuses(get_user_data(interaction.user.id).get("inventory"))
    arrest_money = int(round(ARREST_BASE_REWARD * _pb["money_mult"]))
    try:
        _target_honor = max(0, int((await asyncio.to_thread(get_user_data, target_id)).get("honor", 0)))
    except Exception:
        _target_honor = 0
    arrest_extra = min(ARREST_HONOR_BONUS_MAX, _target_honor // ARREST_HONOR_PER_POINT)   # 거물 검거 보너스
    arrest_honor = int(round((ARREST_BASE_HONOR + arrest_extra) * _pb["honor_mult"]))
    money_note = f" (무전기 +{round(POLICE_RADIO_ARREST_BONUS * 100)}%)" if _pb["money_mult"] > 1 else ""
    honor_note = f" (황금 배지 +{round(POLICE_BADGE_HONOR_BONUS * 100)}%)" if _pb["honor_mult"] > 1 else ""
    if arrest_extra > 0:
        honor_note = f" (거물 검거 보너스 +{arrest_extra}){honor_note}"
    try:
        update_user_stats(interaction.user.id, arrest_money, arrest_honor)
    except Exception:
        pass
    police_shift_record(interaction.user.id, "arrest",
                        bonus=min(POLICE_SALARY_ARREST_BONUS_MAX, _target_honor // POLICE_SALARY_ARREST_BONUS_PER))   # /월급 근무 실적

    # 압류: 조직원 아이템 랜덤 1~2종을 경찰이 가져감
    try:
        seized_items = await asyncio.to_thread(seize_items_sync, interaction.user.id, target_id)
    except Exception as e:
        print(f"Seize error: {e}")
        seized_items = []
    seize_text = ", ".join(f"`{it}`" for it in seized_items) if seized_items else "압류할 수 있는 물품이 없었습니다."

    police_info = get_user_data(interaction.user.id)
    police_rank = get_user_rank(police_info["honor"], is_police=True)

    # 서버 전체에 보일 검거 임베드 생성
    embed = discord.Embed(
        title="[치안국 긴급 공지] 지명수배범 검거 완료",
        description=(
            f"**{interaction.user.mention}** 경찰관이 수배범 **{member.mention}** (을)를 현장에서 체포했습니다!\n\n"
            f"**처벌 내용:** **{jail_time_seconds // 60}분** 동안 감옥에 수감되며 프로필 외 모든 활동이 통제됩니다.\n"
            f"**현재 거점:** `감옥`\n\n"
            f"**검거 포상금:** `${arrest_money:,}`{money_note}\n"
            f"**경찰 실적:** `+{arrest_honor} pt`{honor_note} (현재 직급: `{police_rank}`)\n"
            f"**압류 물품:** {seize_text}"
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
        save_status(self.mafia.id)

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
# 경찰 활동 명예 (이 값들을 조절해서 경찰 성장 속도를 맞추세요)
PATROL_HONOR = 14                   # /순찰 1회 명예 (기존 10)

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

    # 4. 경찰 보상 지급 (자금 $400~800, 명예 PATROL_HONOR)
    earned_money = random.randint(400, 800)
    earned_honor = int(round(PATROL_HONOR * police_bonuses(get_user_data(user.id).get("inventory"))["honor_mult"]))

    try:
        update_user_stats(user.id, earned_money, earned_honor)
    except Exception as e:
        print(f"Patrol reward error: {e}")
    police_shift_record(user.id, "patrol")   # /월급 근무 실적

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


# === [경찰 전용] `/심문` 명령어 (수감자 심문 -> 재판 -> 감형 / 실형 가중) ===
#   1) 경찰이 /심문 으로 수감자에게 질문을 던집니다.
#   2) 수감자가 버튼으로 답변합니다. (자백 / 묵비권 / 결백 주장)  ※ 시간 내 무응답 = 묵비권
#   3) 경찰이 재판부에 의견을 제출합니다. (감형 권고 / 가중 권고)
#   4) 재판 결과가 확률로 결정됩니다. (감형 / 유지 / 실형 가중) -> jail_list 의 석방 시간이 바뀝니다.
INTERROGATE_COOLDOWN_SEC = 120        # 경찰의 심문 쿨타임
INTERROGATE_STEP_TIMEOUT_SEC = 45     # 수감자 답변 / 경찰 의견 각 단계 제한 시간
INTERROGATE_MIN_REMAIN_SEC = 30       # 석방까지 이 시간 이상 남아 있어야 심문 가능
INTERROGATE_MAX_JAIL_SEC = 600        # 가중되어도 남은 형기는 이 시간을 넘지 않음
INTERROGATE_DELTA_RANGE = (40, 90)    # 감형 / 가중 시간 범위 (초)
INTERROGATE_DENY_MULT = 1.5           # 결백 주장은 도박수라 변동폭이 1.5배
INTERROGATE_MATCH_REWARD_MONEY = 300  # 경찰 의견과 판결이 일치했을 때 수당
INTERROGATE_MATCH_REWARD_HONOR = 15   # 경찰 의견과 판결이 일치했을 때 명예 (기존 10)

INTERROGATE_ANSWERS = {"confess": "자백 (협조)", "silent": "묵비권", "deny": "결백 주장"}
INTERROGATE_OPINIONS = {"reduce": "감형 권고", "extend": "가중 권고"}

# (수감자 답변, 경찰 의견) -> (감형, 유지, 가중) 확률 가중치
INTERROGATE_VERDICT_WEIGHTS = {
    ("confess", "reduce"): (70, 25, 5),
    ("confess", "extend"): (25, 45, 30),
    ("silent",  "reduce"): (25, 55, 20),
    ("silent",  "extend"): (10, 40, 50),
    ("deny",    "reduce"): (45, 5, 50),
    ("deny",    "extend"): (20, 5, 75),
}

interrogate_cooldowns = {}     # {police_id: datetime}
interrogate_active = {}        # {prisoner_id: 만료 timestamp}  (진행 중인 심문, 만료되면 자동 해제)
interrogate_done_terms = {}    # {prisoner_id: 그 수감의 석방 예정 시각}  (한 번 수감당 심문 1회)


class InterrogationSession:
    def __init__(self, police: discord.Member, prisoner: discord.Member, release_at: datetime, question: str):
        self.police = police
        self.prisoner = prisoner
        self.release_at = release_at   # 심문 시작 시점의 석방 예정 시각 (수감 식별용)
        self.question = question
        self.message = None
        self.answer = None

    def end(self):
        interrogate_active.pop(self.prisoner.id, None)


class InterrogatePrisonerView(discord.ui.View):
    def __init__(self, s: InterrogationSession):
        super().__init__(timeout=INTERROGATE_STEP_TIMEOUT_SEC)
        self.s = s
        self.answered = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.s.prisoner.id:
            await interaction.response.send_message("심문을 받는 수감자만 답변할 수 있습니다.", ephemeral=True)
            return False
        return True

    async def _answer(self, interaction: discord.Interaction, key: str):
        if self.answered:
            await interaction.response.send_message("이미 답변했습니다.", ephemeral=True)
            return
        self.answered = True
        for child in self.children:
            child.disabled = True
        self.stop()
        await interaction.response.defer()
        await interrogate_police_stage(self.s, key)

    @discord.ui.button(label="자백 (협조)", style=discord.ButtonStyle.success)
    async def confess(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._answer(interaction, "confess")

    @discord.ui.button(label="묵비권", style=discord.ButtonStyle.secondary)
    async def silent(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._answer(interaction, "silent")

    @discord.ui.button(label="결백 주장", style=discord.ButtonStyle.danger)
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._answer(interaction, "deny")

    async def on_timeout(self):
        if self.answered:
            return
        self.answered = True
        await interrogate_police_stage(self.s, "silent", timed_out=True)


class InterrogatePoliceView(discord.ui.View):
    def __init__(self, s: InterrogationSession):
        super().__init__(timeout=INTERROGATE_STEP_TIMEOUT_SEC)
        self.s = s
        self.resolved = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.s.police.id:
            await interaction.response.send_message("담당 경찰만 재판부에 의견을 제출할 수 있습니다.", ephemeral=True)
            return False
        return True

    async def _submit(self, interaction: discord.Interaction, opinion: str):
        if self.resolved:
            await interaction.response.send_message("이미 제출된 의견입니다.", ephemeral=True)
            return
        self.resolved = True
        for child in self.children:
            child.disabled = True
        self.stop()
        await interaction.response.defer()
        await interrogate_trial(self.s, opinion)

    @discord.ui.button(label="감형 권고", style=discord.ButtonStyle.success)
    async def reduce(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._submit(interaction, "reduce")

    @discord.ui.button(label="가중 권고", style=discord.ButtonStyle.danger)
    async def extend(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._submit(interaction, "extend")

    async def on_timeout(self):
        if self.resolved:
            return
        self.resolved = True
        s = self.s
        s.end()
        embed = discord.Embed(
            title="[재판 불성립]",
            description=f"{s.police.mention} 경관이 의견을 제출하지 않아 재판이 열리지 못했습니다. (형량 변동 없음)",
            color=discord.Color.dark_grey()
        )
        try:
            await s.message.edit(content=None, embed=embed, view=None)
        except Exception:
            pass


async def interrogate_police_stage(s: InterrogationSession, answer: str, timed_out: bool = False):
    """수감자 답변이 끝난 뒤 -> 경찰이 재판부 의견을 고르는 단계"""
    s.answer = answer
    note = "(시간 내 답변이 없어 **묵비권**으로 처리되었습니다.)\n\n" if timed_out else ""
    embed = discord.Embed(
        title="[치안국 심문실] 재판부 의견 제출",
        description=(
            f"{note}"
            f"**질문:** {s.question}\n"
            f"**{s.prisoner.display_name}** 님의 답변: **{INTERROGATE_ANSWERS[answer]}**\n\n"
            f"{s.police.mention} 경관은 {INTERROGATE_STEP_TIMEOUT_SEC}초 안에 재판부에 의견을 제출하세요.\n"
            f"의견이 판결과 일치하면 수당 `${INTERROGATE_MATCH_REWARD_MONEY:,}` / 실적 `+{INTERROGATE_MATCH_REWARD_HONOR} pt` 를 받습니다."
        ),
        color=discord.Color.dark_blue()
    )
    try:
        await s.message.edit(content=s.police.mention, embed=embed, view=InterrogatePoliceView(s))
    except Exception as e:
        print(f"Interrogate stage error: {e}")
        s.end()


async def interrogate_trial(s: InterrogationSession, opinion: str):
    """재판: 확률로 판결을 정하고 jail_list 의 석방 시간을 조정"""
    try:
        weights = INTERROGATE_VERDICT_WEIGHTS[(s.answer, opinion)]
        verdict = random.choices(["reduce", "keep", "extend"], weights=weights, k=1)[0]

        delta = random.randint(*INTERROGATE_DELTA_RANGE)
        if s.answer == "deny":
            delta = int(delta * INTERROGATE_DENY_MULT)

        now = datetime.now()
        cur = jail_list.get(s.prisoner.id)

        # 심문 도중 형기가 끝났거나 다른 수감으로 바뀐 경우
        if cur is None or now >= cur:
            embed = discord.Embed(
                title="[재판 무효]",
                description=f"{s.prisoner.mention} 님은 재판 전에 이미 석방되어 판결이 무효 처리되었습니다.",
                color=discord.Color.dark_grey()
            )
            await s.message.edit(content=None, embed=embed, view=None)
            return

        if verdict == "reduce":
            new_release = max(now + timedelta(seconds=5), cur - timedelta(seconds=delta))
        elif verdict == "extend":
            new_release = min(now + timedelta(seconds=INTERROGATE_MAX_JAIL_SEC), cur + timedelta(seconds=delta))
        else:
            new_release = cur
        jail_list[s.prisoner.id] = new_release
        save_status(s.prisoner.id)
        remain = max(0, int((new_release - now).total_seconds()))
        real_delta = abs(int((new_release - cur).total_seconds()))

        if verdict == "reduce":
            title, color = "[재판 결과] 감형", discord.Color.green()
            result = f"재판부는 수감자의 태도를 참작해 **감형**을 선고했습니다.\n**수감 시간 `-{real_delta}초`**"
        elif verdict == "extend":
            title, color = "[재판 결과] 실형 가중", discord.Color.dark_red()
            result = f"재판부는 죄질이 불량하다고 판단해 **실형을 가중**했습니다.\n**수감 시간 `+{real_delta}초`**"
        else:
            title, color = "[재판 결과] 형량 유지", discord.Color.light_grey()
            result = "재판부는 **기존 형량을 유지**하기로 했습니다."

        police_shift_record(s.police.id, "interrogate")   # /월급 근무 실적
        reward_text = ""
        if verdict == opinion:   # 경찰 의견과 판결이 일치
            police_shift_record(s.police.id, "interrogate_match")
            try:
                _pb = police_bonuses(get_user_data(s.police.id).get("inventory"))
                reward_honor = int(round(INTERROGATE_MATCH_REWARD_HONOR * _pb["honor_mult"]))
                update_user_stats(s.police.id, INTERROGATE_MATCH_REWARD_MONEY, reward_honor)
                info = get_user_data(s.police.id)
                rank = get_user_rank(info["honor"], is_police=True)
                reward_text = (f"\n\n**의견 일치 수당:** `${INTERROGATE_MATCH_REWARD_MONEY:,}`\n"
                               f"**경찰 실적:** `+{reward_honor} pt` (현재 직급: `{rank}`)")
            except Exception as e:
                print(f"Interrogate reward error: {e}")

        embed = discord.Embed(
            title=title,
            description=(
                f"**질문:** {s.question}\n"
                f"**답변:** {INTERROGATE_ANSWERS[s.answer]} / **경찰 의견:** {INTERROGATE_OPINIONS[opinion]}\n\n"
                f"{result}\n"
                f"**{s.prisoner.display_name}** 님의 석방까지 **{remain}초** 남았습니다."
                f"{reward_text}"
            ),
            color=color
        )
        await s.message.edit(content=None, embed=embed, view=None)
    except Exception as e:
        print(f"Interrogate trial error: {e}")
    finally:
        s.end()


@bot.tree.command(name="심문", description="[경찰 전용] 수감자를 심문하고 재판을 열어 수감 시간을 줄이거나 늘립니다.")
@app_commands.describe(수감자="심문할 수감자", 질문="수감자에게 던질 질문 (선택)")
async def interrogate_command(interaction: discord.Interaction, 수감자: discord.Member, 질문: str = "공범과 범행 동기를 낱낱이 말해라."):
    if not await check_maintenance(interaction): return
    if await check_jail(interaction): return

    user = interaction.user
    now = datetime.now()

    # 1. 경찰 역할 검증
    police_role = discord.utils.get(interaction.guild.roles, name="경찰")
    if not police_role or police_role not in user.roles:
        await interaction.response.send_message("치안국 소속 경찰만 심문을 진행할 수 있습니다!", ephemeral=True)
        return

    # 2. 쿨타임 체크
    cd = interrogate_cooldowns.get(user.id)
    if cd and now < cd:
        remaining = int((cd - now).total_seconds())
        await interaction.response.send_message(f"심문 준비 중입니다. **{remaining}초** 뒤에 다시 심문할 수 있습니다.", ephemeral=True)
        return

    # 3. 대상 검증
    if 수감자.bot or 수감자.id == user.id:
        await interaction.response.send_message("심문할 수 없는 대상입니다.", ephemeral=True)
        return

    release_at = jail_list.get(수감자.id)
    if release_at is None or now >= release_at:
        await interaction.response.send_message(f"**{수감자.display_name}** 님은 현재 수감 중이 아닙니다.", ephemeral=True)
        return

    remain = int((release_at - now).total_seconds())
    if remain < INTERROGATE_MIN_REMAIN_SEC:
        await interaction.response.send_message(f"석방이 임박해 재판을 열 수 없습니다. (남은 형기 {remain}초)", ephemeral=True)
        return

    if interrogate_done_terms.get(수감자.id) == release_at:
        await interaction.response.send_message(f"**{수감자.display_name}** 님은 이번 수감에서 이미 심문이 진행되었습니다.", ephemeral=True)
        return

    if time.time() < interrogate_active.get(수감자.id, 0):
        await interaction.response.send_message(f"**{수감자.display_name}** 님은 이미 심문을 받고 있습니다.", ephemeral=True)
        return

    # 4. 심문 시작 (여기부터 await 전까지 다른 명령어가 끼어들 수 없음)
    interrogate_cooldowns[user.id] = now + timedelta(seconds=INTERROGATE_COOLDOWN_SEC)
    interrogate_done_terms[수감자.id] = release_at
    interrogate_active[수감자.id] = time.time() + INTERROGATE_STEP_TIMEOUT_SEC * 2 + 30

    question = 질문.strip()[:100] or "공범과 범행 동기를 낱낱이 말해라."
    session = InterrogationSession(user, 수감자, release_at, question)

    embed = discord.Embed(
        title="[치안국 심문실] 심문 개시",
        description=(
            f"**{user.mention}** 경관이 수감자 **{수감자.mention}** 을(를) 심문실로 불러냈습니다.\n\n"
            f"**질문:** {question}\n\n"
            f"{수감자.mention} 님은 {INTERROGATE_STEP_TIMEOUT_SEC}초 안에 답변을 선택하세요.\n"
            f"- **자백:** 감형 가능성 높음 (단, 경찰이 가중을 권고하면 위험)\n"
            f"- **묵비권:** 무난하지만 크게 기대할 건 없음\n"
            f"- **결백 주장:** 큰 감형 또는 큰 가중, 도박수\n"
            f"(무응답은 묵비권 처리)"
        ),
        color=discord.Color.dark_blue()
    )
    await interaction.response.send_message(content=수감자.mention, embed=embed, view=InterrogatePrisonerView(session))
    session.message = await interaction.original_response()

# === NPC 전투 대상 목록 ===
# 권장 진입선: 깡패(처음부터) → 배신자(단검·명예 300) → 거상(몽둥이·명예 1,000) → 수배자(주사기·명예 3,000) → 암살자(명예 5,000)
NPC_DATA = {
    "골목길 깡패": {
        "name": "골목길 깡패",
        "hp": 700,
        "atk": 70,
        "def": 5,
        "reward_money": (60, 150),
        "reward_honor": 1,
        "desc": "칼렌데 뒷골목에서 서성이는 초보 깡패입니다."
    },
    "조직의 배신자": {
        "name": "조직의 배신자",
        "hp": 2500,
        "atk": 170,
        "def": 30,
        "reward_money": (200, 450),
        "reward_honor": 1,
        "desc": "패밀리의 기밀을 팔아넘기고 도망친 위험한 배신자입니다."
    },
    "암시장 악덕 거상": {
        "name": "암시장 악덕 거상",
        "hp": 5500,
        "atk": 300,
        "def": 70,
        "reward_money": (450, 1000),
        "reward_honor": 2,
        "desc": "고용한 보디가드들을 대동하고 다니는 악명 높은 거상입니다."
    },
    "지명수배자 보스": {
        "name": "지명수배자 보스",
        "hp": 11000,
        "atk": 520,
        "def": 130,
        "reward_money": (1000, 2000),
        "reward_honor": 3,
        "desc": "치안국에서도 손대지 못하는 강력한 수배범 보스입니다."
    },
    "칼렌데 암살자": {
        "name": "칼렌데 암살자",
        "hp": 18000,
        "atk": 800,
        "def": 220,
        "reward_money": (2000, 3600),
        "reward_honor": 5,
        "desc": "그림자처럼 움직이며 거물만 노리는 전설적인 청부 암살자입니다."
    }
}

# === 히든 보스 데이터 (NPC_DATA 와 분리: /전투 로 출현 조건 없이 소환되는 것을 막기 위함) ===
# 권장: 명예 약 5,000pt + 슈퍼 울트라 주먹/수상한 주사기 (대부 직전의 최종 시험)
HIDDEN_BOSS = {
    "image": "boss_dark.jpg",   # 보스 그림 (assets 폴더). 파일이 없으면 그림 없이 진행됩니다.
    "image_crop": (0.07, 0.07, 0.90, 0.84),   # 얼굴만 보이도록 자를 영역 (왼쪽, 위, 오른쪽, 아래 / 그림 크기에 대한 비율 0~1)
    "name": "[히든보스] 어둠의 지배자",   # 보스 승리 시 보스가 연속 출현하지 않도록 비교하는 이름이므로 함부로 바꾸지 마세요
    "hp": 75000,
    "atk": 2750,
    "def": 600,
    "reward_money": (10000, 30000),
    "reward_honor": 100,
    "desc": "전투의 살기에 이끌려 모습을 드러낸 칼렌데 지하 세계의 지배자입니다. 15턴 안에 쓰러뜨리지 못하면 제압당합니다."
}

# === 최강 히든 보스 데이터 (태초의 파괴자) ===
ULTIMATE_BOSS = {
    "image": "boss_ultimate.jpg",
    "image_crop": (0.30, 0.05, 0.80, 0.315),   # 모자부터 얼굴까지만 (비율). 그림을 바꾸면 이 값도 맞춰 주세요
    "name": "[최강보스] 태초의 파괴자",
    "hp": 120000,
    "atk": 5500,
    "def": 900,
    "reward_money": (30000, 70000),
    "reward_honor": 400,
    "desc": "칼렌데 지하 세계의 근원이자 재앙 그 자체인 존재입니다. 15턴 안에 쓰러뜨리지 못하면 세계가 붕괴합니다."
}

# === 테러 보스: 유토피아 오브 에덴의 창립자 (세계관 /정보 참고) ===
# 어둠의 지배자(75,000)와 태초의 파괴자(120,000) 사이의 난이도입니다.
EDEN_BOSS = {
    "name": "[테러보스] 에칼 데 페스토",
    "hp": 95000,
    "atk": 3800,
    "def": 750,
    "reward_money": (18000, 45000),
    "reward_honor": 200,
    "desc": "모두가 평등한 세상을 외치며 칼렌데를 불바다로 만들려는 유토피아 오브 에덴의 창립자입니다. 15턴 안에 쓰러뜨리지 못하면 도시가 불탑니다."
}

# === 보스 종류 표 (출현 판정 · 출현 공지 · /보스 전투가 모두 이 표를 사용합니다) ===
#   보스를 더 추가하려면: 보스 데이터를 만들고 → 여기에 한 줄 추가 → BOSS_SPAWN_TABLE 에 확률 한 줄 추가
BOSS_KINDS = {
    "dark": {
        "data": HIDDEN_BOSS,
        "title": "[히든보스] 어둠의 지배자 토벌전",
        "color": discord.Color.dark_red(),
        "spawn_msg": ("**[긴급 상황 경보]** \n"
                      "전투의 여파로 부근에서 엄청난 살기가 감지되었습니다!\n"
                      "**어둠의 지배자**가 이 지역에 출현했습니다! (`/보스` 명령어로 토벌 가능)"),
    },
    "eden": {
        "data": EDEN_BOSS,
        "title": "[테러보스] 에칼 데 페스토 토벌전",
        "color": discord.Color.from_rgb(200, 40, 40),
        "spawn_msg": ("**[테러 경보]** \n"
                      "도시 곳곳에서 붉은 깃발이 펄럭이고 폭발음이 들려옵니다!\n"
                      "**유토피아 오브 에덴의 창립자 에칼 데 페스토**가 모습을 드러냈습니다! (`/보스` 명령어로 토벌 가능)"),
    },
    "ultimate": {
        "data": ULTIMATE_BOSS,
        "title": "[멸망의 전조] 태초의 파괴자 토벌전",
        "color": discord.Color.from_rgb(120, 0, 160),
        "spawn_msg": ("**[세계멸망 경보]** \n"
                      "차원의 균열이 갈라지며 역대급 재앙의 기운이 감지되었습니다!\n"
                      "**[최강보스] 태초의 파괴자**가 강림했습니다! (`/보스` 명령어로 토벌 가능)"),
    },
}
BOSS_NAMES = tuple(k["data"]["name"] for k in BOSS_KINDS.values())

def boss_image_file(boss_data: dict):
    """보스 그림을 discord.File 로 만들어 돌려준다. 그림이 지정되지 않았거나 파일이 없으면 None.
    (임베드에서는 set_image(url="attachment://파일이름") 로 참조한다)"""
    name = boss_data.get("image")
    if not name:
        return None
    for path in _asset_paths((name,)):
        crop = boss_data.get("image_crop")
        if crop:   # 전신 그림에서 얼굴 부분만 잘라서 보냄 (실패하면 원본을 그대로 보냄)
            try:
                img = Image.open(path).convert("RGB")
                w, h = img.size
                img = img.crop((int(crop[0] * w), int(crop[1] * h), int(crop[2] * w), int(crop[3] * h)))
                if img.width < 640:   # 잘라낸 그림이 작으면 선명하게 확대
                    img = img.resize((640, int(img.height * 640 / img.width)), Image.Resampling.LANCZOS)
                buf = io.BytesIO()
                if name.lower().endswith(".png"):
                    img.save(buf, format="PNG")
                else:
                    img.save(buf, format="JPEG", quality=95)
                buf.seek(0)
                return discord.File(buf, filename=name)
            except Exception as e:
                print(f"[보스 그림 자르기 실패 → 원본 사용] {name}: {e}")
        return discord.File(path, filename=name)
    print(f"[보스 그림 없음] {name} 파일을 assets 폴더에서 찾지 못했습니다.")
    return None

# 출현 판정표: (종류, 확률) — 위에서부터 확률을 누적해 한 번의 주사위로 어느 보스가 나올지 정합니다.
BOSS_SPAWN_TABLE = [
    ("dark", BOSS_SPAWN_CHANCE),
    ("eden", EDEN_BOSS_SPAWN_CHANCE),
    ("ultimate", ULTIMATE_BOSS_SPAWN_CHANCE),
]

def boss_short_name(data: dict) -> str:
    """'[히든보스] 어둠의 지배자' -> '어둠의 지배자'"""
    return data["name"].split("] ", 1)[-1]

def boss_chance_text() -> str:
    """'어둠의 지배자 3% / 에칼 데 페스토 3% / 태초의 파괴자 3%'"""
    return " / ".join(f"{boss_short_name(BOSS_KINDS[kind]['data'])} {chance * 100:g}%" for kind, chance in BOSS_SPAWN_TABLE)

def boss_reward_text() -> str:
    return "\n".join(
        f"  └ {boss_short_name(k['data'])}: ${k['data']['reward_money'][0]:,}~${k['data']['reward_money'][1]:,} / 명예 +{k['data']['reward_honor']}pt"
        for k in BOSS_KINDS.values()
    )

# === 네임드(강화) 몬스터 ===
# 일반 NPC보다 훨씬 강하고, 체력이 절반 아래로 떨어지면 격노(enrage)해서 공격력이 오릅니다.
# 처음 처치하면 고유 전리품()을 얻고, 명예는 조금만 주므로 대부 달성 속도에는 거의 영향이 없습니다.
# min_honor 는 입장 조건, 12턴 안에 못 쓰러뜨리면 패배합니다.
NAMED_COOLDOWN_SEC = 240
NAMED_MONSTERS = {
    "marco": {
        "name": "[네임드] 백정 마르코", "named": True, "min_honor": 500, "max_turns": 12,
        "hp": 4200, "atk": 330, "def": 55, "enrage": 1.5,
        "reward_money": (900, 1800), "reward_honor": 2,
        "drop": "[전리품] 마르코의 정육 칼",
        "desc": "블랙워드 도살장을 지배하는 청부 해결사. 피를 보면 더 사나워집니다."
    },
    "bianca": {
        "name": "[네임드] 독사 비앙카", "named": True, "min_honor": 1500, "max_turns": 12,
        "hp": 8000, "atk": 520, "def": 100, "enrage": 1.5,
        "reward_money": (1800, 3300), "reward_honor": 4,
        "drop": "[전리품] 비앙카의 독니 반지",
        "desc": "로젠펠트 뒷무대의 독살 전문가. 체력이 줄면 맹독을 뿌리며 덤빕니다."
    },
    "boronin": {
        "name": "[네임드] 철의 장군 보로닌", "named": True, "min_honor": 2500, "max_turns": 12,
        "hp": 14000, "atk": 800, "def": 170, "enrage": 1.5,
        "reward_money": (3000, 5400), "reward_honor": 7,
        "drop": "[전리품] 보로닌의 철 훈장",
        "desc": "퇴역 후 용병단을 이끄는 철벽의 장군. 방어가 매우 단단합니다."
    },
    "executioner": {
        "name": "[네임드] 붉은 달의 처형자", "named": True, "min_honor": 4000, "max_turns": 12,
        "hp": 22000, "atk": 1200, "def": 260, "enrage": 1.6,
        "reward_money": (4800, 8400), "reward_honor": 12,
        "drop": "[전리품] 처형자의 붉은 가면",
        "desc": "칼렌데 지하 세계가 두려워하는 전설의 처형자. 정상급 장비가 없으면 상대도 안 됩니다."
    },
}

# === [조직원 전용] `/송금` 명령어 ===
def transfer_money_sync(sender_id: int, receiver_id: int, amount: int):
    """송금 처리 (동기 DB 작업이라 to_thread 로 실행). 반환: (성공 여부, 송금자 잔액, 수신자 잔액)
    잔액을 다시 확인한 뒤 '차감 → 적립' 순서로 저장하고, 적립이 실패하면 차감을 되돌립니다."""
    _require_db()
    with _inv_lock:
        s_money = int(get_user_data(sender_id).get("money") or 0)
        r_money = int(get_user_data(receiver_id).get("money") or 0)
        if s_money < amount:
            return False, s_money, r_money

        res = supabase.table("user_data").update({"money": s_money - amount}).eq("user_id", str(sender_id)).execute()
        _check_updated(res, "transfer (sender)")
        try:
            res = supabase.table("user_data").update({"money": r_money + amount}).eq("user_id", str(receiver_id)).execute()
            _check_updated(res, "transfer (receiver)")
        except Exception:
            try:   # 적립에 실패했다면 송금자의 자금을 원래대로 되돌린다
                supabase.table("user_data").update({"money": s_money}).eq("user_id", str(sender_id)).execute()
            except Exception as rollback_err:
                print(f"DB Error (transfer rollback): {rollback_err}")
            raise
        return True, s_money - amount, r_money + amount


@bot.tree.command(name="송금", description="[조직원 전용] 다른 조직원에게 보유 자금을 양도합니다.")
@app_commands.describe(member="돈을 받을 조직원", amount="보낼 금액 ($)")
async def transfer_money(interaction: discord.Interaction, member: discord.Member, amount: int):
    if not await check_maintenance(interaction): return
    if not await check_membership(interaction): return
    if await check_jail(interaction): return

    sender = interaction.user
    receiver = member

    # 1. 대상·금액 검사 (아직 응답 전이므로 실패 안내는 본인에게만 보이는 메시지로 보낸다)
    if sender.id == receiver.id:
        await interaction.response.send_message("자기 자신에게는 돈을 송금할 수 없습니다.", ephemeral=True)
        return
    if receiver.bot:
        await interaction.response.send_message("봇에게는 송금할 수 없습니다.", ephemeral=True)
        return
    if amount <= 0:
        await interaction.response.send_message("송금 금액은 1원 이상이어야 합니다.", ephemeral=True)
        return

    mafia_role = discord.utils.get(interaction.guild.roles, name="조직원")
    police_role = discord.utils.get(interaction.guild.roles, name="경찰")
    receiver_ok = (mafia_role and mafia_role in receiver.roles) or (police_role and police_role in receiver.roles)
    if not receiver_ok:
        await interaction.response.send_message("받는 사람은 **조직원 또는 경찰**이어야 합니다. (가입하지 않은 사람에게는 송금할 수 없습니다)", ephemeral=True)
        return

    # 2. 잔액 확인
    sender_money = int((await asyncio.to_thread(get_user_data, sender.id)).get("money", 0) or 0)
    if sender_money < amount:
        await interaction.response.send_message(
            f"보유 자금이 부족합니다!\n"
            f"• 보유 중인 자금: `${sender_money:,}`\n"
            f"• 송금하려는 금액: `${amount:,}`",
            ephemeral=True
        )
        return

    await interaction.response.defer()

    # 3. 실제 이체 (차감 → 적립, 실패 시 되돌림)
    try:
        ok, new_sender_money, new_receiver_money = await asyncio.to_thread(transfer_money_sync, sender.id, receiver.id, amount)
    except Exception as e:
        print(f"DB Error (Transfer Money): {e}")
        await interaction.followup.send("데이터베이스 오류로 송금에 실패했습니다. 나중에 다시 시도해 주세요.", ephemeral=True)
        return

    if not ok:   # 확인과 이체 사이에 잔액이 줄어든 경우
        await interaction.followup.send(f"보유 자금이 부족합니다! (보유 `${new_sender_money:,}`)", ephemeral=True)
        return

    # 4. 송금 완료 임베드 (서버 전체 공개)
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
        if self.npc_info.get("image"):   # 보스 그림 (최초 전송 때 첨부한 파일을 계속 참조)
            embed.set_image(url=f"attachment://{self.npc_info['image']}")
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

        boss_spawn_msg = ""
        boss_spawn_kind = None
        guild_id = None

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

            # 제작 재료 드랍 (/제작) — 일반 전투·네임드·보스 공통
            mat_drops = roll_material_drops(self.npc_info["name"])
            if mat_drops:
                inv = update_payload.get("inventory")
                if inv is None:
                    inv = user_data.get("inventory") or {}
                add_materials(inv, mat_drops)
                update_payload["inventory"] = inv
                drop_msg += f"\n**제작 재료 획득:** {format_material_drops(mat_drops)}"

            # 일일 의뢰 집계 (NPC 전투 승리)
            daily_blob = get_title_blob(user_data)
            ach_stats = daily_blob["stats"]                      # 업적용 카운터
            ach_stats["fight_wins"] += 1
            if self.npc_info.get("named"):
                ach_stats["named_kills"] += 1
            if self.npc_info["name"] in BOSS_NAMES:
                ach_stats["boss_kills"] += 1
            after_info = {
                **user_data, "money": new_money, "honor": new_honor,
                "inventory": update_payload.get("inventory", user_data.get("inventory"))
            }
            daily_msg = daily_record(daily_blob, "fight", after_info)
            daily_msg += format_new_titles(evaluate_new_titles(daily_blob, after_info))   # 새 칭호·업적 안내
            update_payload["data"] = daily_blob

            try:
                _require_db()
                supabase.table("user_data").update(update_payload).eq("user_id", str(self.user.id)).execute()
            except Exception as e:
                print(f"DB Error (NPC Battle Win): {e}")
            # 보스 출현 판정 (서버에 보스가 없을 때만 / 보스·네임드 처치로는 출현하지 않음)
            guild_id = interaction.guild_id if interaction.guild else None
            is_boss_fight = self.npc_info["name"] in BOSS_NAMES
            if guild_id and not is_boss_fight and not self.npc_info.get("named") and not active_boss_guilds.get(guild_id):
                roll = random.random()
                acc = 0.0
                for kind, chance in BOSS_SPAWN_TABLE:   # 확률을 누적해서 한 번의 주사위로 어느 보스가 나올지 정함
                    acc += chance
                    if roll < acc:
                        active_boss_guilds[guild_id] = kind
                        boss_spawn_kind = kind
                        boss_spawn_msg = BOSS_KINDS[kind]["spawn_msg"]
                        break

            embed = discord.Embed(
                title=f"[전투 승리] {self.npc_info['name']} 격퇴!",
                description=f"**{self.user.mention}** 님이 **{self.npc_info['name']}**(을)를 완벽하게 쓰러뜨렸습니다!\n\n"
                            f"**획득 보상금:** `${earned_money:,}`\n"
                            f"**획득 명예:** `+{earned_honor} pt`\n\n"
                            f"**현재 보유 자금:** `${new_money:,}`\n"
                            f"**현재 명예:** `{new_honor} pt`"
                            f"{drop_msg}{daily_msg}",  # 전리품 + 일일 의뢰 (보스 출현 안내는 아래에서 별도 메시지로 전송)
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

        # 전투 종료 메시지 업데이트 (실패해도 보상·보스 처리는 이미 끝났으므로 로그를 남기고 새 메시지로 대신 알림)
        try:
            await interaction.edit_original_response(embed=embed, view=self)
        except Exception as e:
            print(f"[전투 결과 수정 실패] {type(e).__name__}: {e}")
            try:
                await interaction.followup.send(embed=embed)
            except Exception as e2:
                print(f"[전투 결과 전송 실패] {type(e2).__name__}: {e2}")

        # 보스 출현 공지: 결과 임베드와 별개의 공개 메시지. 공지를 못 보냈다면 보스도 취소해서 '조용한 보스'가 생기지 않게 함
        if boss_spawn_kind and guild_id:
            print(f"[보스 출현] guild={guild_id} kind={boss_spawn_kind}")
            try:
                boss_embed = discord.Embed(
                    title="[보스 출현]",
                    description=boss_spawn_msg.strip(),
                    color=BOSS_KINDS[boss_spawn_kind]["color"]
                )
                spawn_kwargs = {"embed": boss_embed}
                spawn_file = boss_image_file(BOSS_KINDS[boss_spawn_kind]["data"])
                if spawn_file:
                    boss_embed.set_image(url=f"attachment://{BOSS_KINDS[boss_spawn_kind]['data']['image']}")
                    spawn_kwargs["file"] = spawn_file
                await interaction.followup.send(**spawn_kwargs)
            except Exception as e:
                print(f"[보스 공지 실패 → 출현 취소] {type(e).__name__}: {e}")
                if active_boss_guilds.get(guild_id) == boss_spawn_kind:
                    active_boss_guilds[guild_id] = None

        # 승패와 상관없이, 그리고 전투가 어떤 결과로 끝났든 무조건 쿨타임 즉시 초기화
        if self.user.id in npc_cooldowns:
            del npc_cooldowns[self.user.id]

@bot.tree.command(name="전투", description="[조직원 전용] NPC 이름을 직접 입력하여 1:1 턴제 전투를 시작합니다.")
@app_commands.describe(npc_name="전투를 벌일 NPC 이름 (예: 골목길 깡패, 조직의 배신자)")
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
            f"일반 `/전투` 승리 시 낮은 확률로 출현합니다. ({boss_chance_text()})",
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

    boss_kind = active_boss_guilds.get(guild_id)   # BOSS_KINDS 의 키 ("dark" / "eden" / "ultimate")
    active_boss_guilds[guild_id] = None
    npc_cooldowns[user.id] = now + timedelta(seconds=BOSS_COOLDOWN_SEC)

    await interaction.response.defer()

    try:
        user_stats = get_user_data(user.id)
        honor = user_stats.get("honor", 0)
        inventory = user_stats.get("inventory", {})

        user_combat_stats = compute_combat_stats(honor, inventory)

        # 서버에 떠 있는 보스 종류에 맞는 보스와 전투 (둘 다 15턴 제한)
        kind_info = BOSS_KINDS.get(boss_kind) or BOSS_KINDS["dark"]
        boss_info, boss_title = kind_info["data"], kind_info["title"]
        view = NPCBattleView(user=user, user_stats_dict=user_combat_stats, npc_info=boss_info, max_turns=15)
        embed = view.get_embed()
        embed.title = boss_title

        send_kwargs = {"embed": embed, "view": view}
        boss_file = boss_image_file(boss_info)
        if boss_file:
            send_kwargs["file"] = boss_file
        else:
            embed.set_image(url=None)   # 그림 파일이 없으면 깨진 이미지 표시를 막음
        view.message = await interaction.followup.send(**send_kwargs)
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

    parttime_cooldowns[user.id] = now + timedelta(seconds=PARTTIME_COOLDOWN_SEC)

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

        duel_ach = ""
        try:   # 업적 카운터 (승자만)
            duel_new = await asyncio.to_thread(bump_user_stats_sync, self.winner.id, {"duel_wins": 1})
            if duel_new:
                duel_ach = f"\n\n{self.winner.mention}" + format_new_titles(duel_new)
        except Exception as e:
            print(f"Achievement error (duel): {e}")

        embed = discord.Embed(
            title="[결투 종료: 승패 결정]",
            description=f"피비린내 나는 공방 끝에 **{self.winner.mention}** 님이 승리했습니다!\n\n"
                        f"**명예 변동:**\n"
                        f"• {self.winner.mention} `+{self.bet} pt` (총 {new_w_honor} pt)\n"
                        f"• {loser.mention} `-{self.bet} pt` (총 {new_l_honor} pt)" + duel_ach,
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
            police_unit_detach_user(member.id)   # 경찰 수사팀에서도 제거
        except Exception as e:
            print(f"Police unit detach error: {e}")

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

        # 임무 쿨타임: 크로노그래프 시계/특수 바이크 보유 시 단축, 자양강장제는 1회 즉시 초기화(소모)
        drink_used = False
        now_ts = time.time()
        remain = int(mission_cooldowns.get(user.id, 0) - now_ts + 0.999)
        no_cd = NO_COOLDOWN_ITEM in inventory
        if remain > 0 and not no_cd:
            if inventory.get(DRINK_ITEM, 0) > 0:
                inventory[DRINK_ITEM] -= 1
                if inventory[DRINK_ITEM] <= 0:
                    del inventory[DRINK_ITEM]
                drink_used = True
            else:
                await interaction.followup.send(f"작전 준비 중입니다. **{remain}초** 뒤에 다시 임무를 수행할 수 있습니다.", ephemeral=True)
                return
        has_fast = any(k in inventory for k in CHRONO_ITEMS)
        if not no_cd:
            mission_cooldowns[user.id] = now_ts + (MISSION_COOLDOWN_FAST_SEC if has_fast else MISSION_COOLDOWN_SEC)

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
                save_status(user.id)
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
        await interaction.followup.send("치안국 경찰은 범죄 조직의 비밀 임무를 수행할 수 없습니다!", ephemeral=True)
        return

    user = interaction.user
    user_stats = get_user_data(user.id)
    current_loc = user_stats.get("location", "레벤하임")

    remain = int(mission_cooldowns.get(user.id, 0) - time.time() + 0.999)
    _inv = user_stats.get("inventory") or {}
    if remain > 0 and NO_COOLDOWN_ITEM not in _inv and _inv.get(DRINK_ITEM, 0) <= 0:
        await interaction.followup.send(f"작전 준비 중입니다. **{remain}초** 뒤에 다시 임무를 수행할 수 있습니다.", ephemeral=True)
        return


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
    gear_keys = [k for k in inventory.keys() if not k.startswith(MATERIAL_PREFIX)]
    inv_list = [f"• **{item}** `[보유 중]`" for item in gear_keys] if gear_keys else ["*보유 중인 장비가 없습니다.*"]
    embed.add_field(name="보유 중인 전용 장비", value="\n".join(inv_list), inline=False)
    mat_lines = [f"• {k[len(MATERIAL_PREFIX):]} x{v}" for k, v in inventory.items() if k.startswith(MATERIAL_PREFIX) and v > 0]
    if mat_lines:
        embed.add_field(name="제작 재료 (`/제작`)", value="\n".join(mat_lines), inline=False)
    
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
    {"id": "police_radio", "name": "기동타격대 무전기", "price": 8000, "desc": "/검거 포상금 +20% (보유만 해도 자동 적용)", "bonus_type": "radio"},
    {"id": "police_badge", "name": "특급 수사관 황금 배지", "price": 20000, "desc": "방어력 +50 / 검거·순찰·심문 명예 실적 +20%", "def_bonus": 50},
]

# --- 치안국 보급품 효과 ---
#   • 강화 방탄복 / 황금 배지 : 방어력 (compute_combat_stats 가 전투·결투·레이드·점령전에 모두 반영)
#   • 기동타격대 무전기       : /검거 포상금 배율
#   • 황금 배지               : /검거 · /순찰 · /심문(의견 일치) 명예 실적 배율
POLICE_RADIO_NAME = "기동타격대 무전기"
POLICE_BADGE_NAME = "특급 수사관 황금 배지"
POLICE_RADIO_ARREST_BONUS = 0.20    # 무전기: 검거 포상금 +20%
POLICE_BADGE_HONOR_BONUS = 0.20     # 황금 배지: 명예 실적 +20%
ARREST_BASE_REWARD = 500            # /검거 기본 포상금
ARREST_BASE_HONOR = 30              # /검거 기본 명예 실적
ARREST_HONOR_PER_POINT = 50         # 수배범 명예 50pt 당 검거 명예 +1  (거물 검거 보너스)
ARREST_HONOR_BONUS_MAX = 100        # 거물 검거 보너스 상한  → 검거 1회 명예 30 ~ 130

def police_bonuses(inventory) -> dict:
    inv = inventory or {}
    return {
        "money_mult": 1.0 + (POLICE_RADIO_ARREST_BONUS if POLICE_RADIO_NAME in inv else 0.0),
        "honor_mult": 1.0 + (POLICE_BADGE_HONOR_BONUS if POLICE_BADGE_NAME in inv else 0.0),
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

# === [공용] 통합 `/정보` 명령어 (확장판) ===
@bot.tree.command(name="정보", description="[공용] 특정 명령어, 아이템 또는 세계관 설정을 상세하게 검색합니다.")
@app_commands.describe(keyword="알고 싶은 항목 (예: 전투, 보스, 슬롯, 칭호, 유토피아 오브 에덴)")
@app_commands.choices(keyword=[
    app_commands.Choice(name="전투 & 보스 시스템", value="전투"),
    app_commands.Choice(name="히든 보스 (/보스)", value="보스"),
    app_commands.Choice(name="카지노 & 도박 (/도박)", value="도박"),
    app_commands.Choice(name="칭호 시스템 (/칭호)", value="칭호"),
    app_commands.Choice(name="일일 의뢰 (/일퀘)", value="일퀘"),
    app_commands.Choice(name="경찰 상점 (/경찰상점)", value="경찰상점"),
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
                    f"• **히든 이벤트:** 전투 승리 시 낮은 확률로 보스가 서버에 출현합니다! ({boss_chance_text()} / 한 번에 한 마리)",
        },
        "보스": {
            "title": "시스템: /보스 (히든 보스 토벌)",
            "desc": "전투 후 살기가 감지되었을 때 출현하는 최종 히든 보스전입니다.\n"
                    f"• **출현 조건:** 일반 `/전투` 승리 시 보스마다 각각 확률로 경보가 발생해야 입장 가능합니다. ({boss_chance_text()})\n"
                    f"• **보상:** 격퇴 성공 시 막대한 자금과 명예, 제작 재료를 획득!\n{boss_reward_text()}\n"
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
            "desc": "지명수배 중인 마피아를 현장에서 체포하여 감옥에 3분간 수감하고 포상금을 획득합니다. 체포 시 대상 조직원의 아이템 중 랜덤 1~2종(각 1개)을 압류해 내 인벤토리로 가져옵니다. (재료와 네임드 전리품 등 거래 불가 아이템은 제외)",
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
        "경찰상점": {
            "title": "명령어: /경찰상점 (경찰 전용)",
            "desc": "치안국 전용 보급품을 구매합니다. 보유만 해도 효과가 적용됩니다.\n"
                    "• **치안국 강화 방탄복:** 방어력 +35\n"
                    "• **기동타격대 무전기:** `/검거` 포상금 +20%\n"
                    "• **특급 수사관 황금 배지:** 방어력 +50 / `/검거`·`/순찰`·`/심문` 명예 실적 +20%\n"
                    "방어력은 `/능력치`에서 확인할 수 있습니다.",
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
        "시간 정지 회중시계": {
            "title": "아이템: 시간 정지 회중시계",
            "desc": "• **분류:** 유틸리티\n• **효과:** 임무 쿨타임 완전 제거 (영구)\n• **설명:** 멈춘 시간 속에서 쉬지 않고 작전을 이어갈 수 있게 해주는 신비한 회중시계입니다.",
        },
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
        "money": 3000, "honor": 15, "points": 10,
        "desc": "하수도를 지배하는 거대한 쥐떼의 왕. 입문자용 레이드입니다.",
    },
    "bull": {
        "name": "부두 항만의 폭군", "min_players": 3, "min_honor": 1500, "cost": 3000,
        "hits": 46, "def_ratio": 0.30, "aoe": 0.09, "aoe_enrage": 0.13,
        "money": 7000, "honor": 30, "points": 25,
        "desc": "에르느코항 부두를 장악한 괴력의 폭군. 응급처치 담당이 꼭 필요합니다.",
    },
    "king": {
        "name": "칼렌데 지하 황제", "min_players": 4, "min_honor": 4000, "cost": 8000,
        "hits": 54, "def_ratio": 0.35, "aoe": 0.09, "aoe_enrage": 0.13,
        "money": 14000, "honor": 50, "points": 60,
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
    try:   # 해산된 조직의 구역은 주인 없는 상태로 돌려놓는다
        supabase.table("territories").delete().eq("family_id", str(family_id)).execute()
    except Exception as e:
        print(f"Territory cleanup error: {e}")

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
        await interaction.response.send_message("조직(패밀리) 기능은 마피아 조직원만 사용할 수 있습니다. (경찰은 `/수사팀` 을 이용하세요)", ephemeral=True)
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
            if self.family_id in active_raids or str(self.family_id) in war_family_map:
                await interaction.response.edit_message(content="레이드 또는 구역 점령전 진행 중에는 해산할 수 없습니다.", view=None)
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
    s.material_drops = {}   # {user_id: {재료: 개수}}  (정산 결과 화면에 표시)
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
            if win:   # 업적 카운터 (레이드 클리어)
                try:
                    incs = {"raid_clears": 1}
                    if t["name"] == RAID_TIERS["king"]["name"]:
                        incs["raid_king_clears"] = 1
                    bump_user_stats_sync(p.uid, incs, pending=True)
                except Exception as e:
                    print(f"Raid achievement error ({p.uid}): {e}")
            if win:   # 제작 재료 (/제작)
                drops = roll_material_drops(t["name"])
                if drops:
                    try:
                        grant_materials_sync(p.uid, drops)
                        s.material_drops[p.uid] = drops
                    except Exception as e:
                        print(f"Raid material error ({p.uid}): {e}")
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
        family_stat_inc(s.fid, "raid_clears")
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
            _mats = getattr(s, "material_drops", {}).get(p.uid)
            lines.append(f"• **{p.name}** — 딜 {p.dmg_done:,} ({p.hits}회) → `${money:,}`" + (f" / 명예 `+{honor}pt`" if honor else "")
                         + (f"\n  └ 재료: {format_material_drops(_mats)}" if _mats else ""))
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


# ==========================================================================
# [구역 점령전]  /구역 현황 · 점령 · 선전포고 · 수금 · 포기
#
#   • 5개 지역(LOCATIONS)을 조직이 차지하면 시간당 수입이 쌓이고, /구역 수금 으로 조직 금고에 넣습니다.
#   • 주인 없는 구역  : /구역 점령  → 조직 금고에서 점령 비용을 내고 즉시 차지
#   • 주인 있는 구역  : /구역 선전포고 → 모집(공격/수비 참전) → 실시간 점령전 → 승리 측이 구역 차지
#       - 수비 조직원이 한 명도 참전하지 않으면 NPC 수비대가 구역을 지킵니다.
#       - 점령에 성공하면 TERRITORY_PROTECT_SEC 동안 보호(공격 불가), 수비에 성공해도 짧게 보호됩니다.
#       - 경찰이 /순찰 중인 지역에서는 점령/선전포고를 할 수 없습니다.
#   • 구역 소유 정보는 Supabase `territories` 테이블(territory.sql)에 저장되어 봇을 재시작해도 유지됩니다.
#     (진행 중인 점령전 자체는 레이드처럼 메모리에만 있어서, 재시작되면 사라집니다.)
#   • 밸런스는 아래 상수만 바꾸면 됩니다.
# ==========================================================================
# --- 구역 설정 (income: 시간당 수입 $ / claim: 주인 없는 구역 점령 비용 $) ---
TERRITORY_INFO = {
    "레벤하임": {"income": 600,  "claim": 5000},
    "던베르크": {"income": 700,  "claim": 6000},
    "블랙워드": {"income": 900,  "claim": 8000},
    "에르느코항": {"income": 900,  "claim": 8000},
    "로젠펠트": {"income": 1000, "claim": 10000},
}
TERRITORY_DEFAULT = {"income": 700, "claim": 6000}   # TERRITORY_INFO 에 없는 지역의 기본값
TERRITORY_MAX_PER_FAMILY = 2        # 한 조직이 가질 수 있는 최대 구역 수 (눈덩이 방지)
TERRITORY_INCOME_CAP_HOURS = 12     # 수금을 안 해도 쌓이는 최대 시간 (그 이상은 사라짐)
TERRITORY_PROTECT_SEC = 7200        # 점령 성공 후 보호 시간 (2시간)
TERRITORY_DEFEND_PROTECT_SEC = 1800 # 수비 성공 후 추가 보호 시간 (30분)

# --- 점령전 설정 ---
WAR_COST = 2000                     # 선전포고 비용 (조직 금고 / 모집 실패·취소 시 환불)
WAR_LOBBY_SEC = 120                 # 참전 모집 시간
WAR_DURATION_SEC = 300              # 전투 제한 시간 (5분)
WAR_TICK_SEC = 4                    # 화면 갱신 / NPC 수비대 공격 주기
WAR_MIN_ATTACKERS = 2               # 공격 최소 인원
WAR_MAX_PER_SIDE = 6                # 한쪽 최대 인원
WAR_GCD_SEC = 2.0                   # 모든 행동 공통 대기시간
WAR_ATK_CD_SEC = 4.0                # 공격 대기시간
WAR_AMBUSH_CD_SEC = 15.0            # 기습 대기시간
WAR_AMBUSH_MULT = 2.2               # 기습 성공 시 피해 배율
WAR_AMBUSH_FAIL = 0.25              # 기습 실패 확률
WAR_DEFENDER_BONUS = 1.1            # 수비 측 피해 배율 (지형 이점)
WAR_GARRISON_HP_RATIO = 1.6         # NPC 수비대 체력 = 공격대 총 체력 × 이 값
WAR_GARRISON_ATK_RATIO = 0.6        # NPC 수비대가 틱마다 주는 피해 = 공격대 총 공격력 × 이 값
WAR_FAMILY_COOLDOWN_SEC = 1800      # 선전포고한 조직의 재선포 대기시간 (30분)
WAR_PRIZE_HOURS = 4                 # 전리품 = 구역 시간당 수입 × 이 값 (승리 측이 나눠 가짐)
WAR_BASE_SHARE = 0.6                # 전리품 중 '균등 분배' 비율 (나머지는 딜량 비례)
WAR_LOSE_MONEY_RATIO = 0.15         # 패배 측 참전자(공격 1회 이상)에게 나눠주는 위로금 비율
WAR_WIN_HONOR = 10                  # 승리 측 명예 (공격 1회 이상 한 사람만)

TERRITORY_DB_ERROR_MSG = "구역 DB 처리 중 오류가 발생했습니다. (관리자: `territories` 테이블이 만들어져 있는지 확인해 주세요 — territory.sql 참고)"

active_wars = {}             # {(guild_id, location): WarSession}
war_family_map = {}          # {str(family_id): WarSession}  (공격·수비 양쪽 조직)
war_user_map = {}            # {user_id: WarSession}
war_family_cooldowns = {}    # {str(family_id): epoch}


def _terr_info(loc: str) -> dict:
    return TERRITORY_INFO.get(loc, TERRITORY_DEFAULT)

def _fmt_sec(sec: float) -> str:
    sec = max(0, int(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}시간 {m}분"
    if m:
        return f"{m}분 {s}초"
    return f"{s}초"

def _patrolled(guild_id: int, loc: str) -> bool:
    return active_patrols.get(guild_id, {}).get(loc, 0) > time.time()


# ---------------------------------------------------------------- 구역 DB 도우미 (동기)
def terr_list(guild_id: int) -> dict:
    res = supabase.table("territories").select("*").eq("guild_id", str(guild_id)).execute()
    return {r["location"]: r for r in (res.data or [])}

def terr_get(guild_id: int, loc: str):
    res = (supabase.table("territories").select("*")
           .eq("guild_id", str(guild_id)).eq("location", loc).limit(1).execute())
    return res.data[0] if res.data else None

def terr_list_by_family(fid) -> list:
    res = supabase.table("territories").select("*").eq("family_id", str(fid)).execute()
    return res.data or []

def terr_insert(guild_id: int, loc: str, fid, now: float):
    row = {"guild_id": str(guild_id), "location": loc, "family_id": str(fid),
           "collected_at": int(now), "protected_until": int(now + TERRITORY_PROTECT_SEC)}
    res = supabase.table("territories").insert(row).execute()   # (guild_id, location) 유니크 → 동시 선점 시 예외
    return res.data[0] if res.data else row

def terr_update(row_id, data: dict):
    res = supabase.table("territories").update(data).eq("id", row_id).execute()
    _check_updated(res, "terr_update")

def terr_delete(row_id):
    supabase.table("territories").delete().eq("id", row_id).execute()

def terr_accrued(row: dict, now: float = None) -> int:
    """마지막 수금 이후 쌓인 수입 (최대 TERRITORY_INCOME_CAP_HOURS 시간분)"""
    now = now or time.time()
    hours = min(TERRITORY_INCOME_CAP_HOURS, max(0.0, now - float(row.get("collected_at") or 0)) / 3600)
    return int(hours * _terr_info(row["location"])["income"])


# ---------------------------------------------------------------- 점령전 세션
class WarPlayer:
    __slots__ = ("member", "uid", "name", "side", "max_hp", "atk", "dmg_done", "hits",
                 "gcd_until", "atk_ready", "ambush_ready")

    def __init__(self, member: discord.Member, side: str, stats: dict):
        self.member = member
        self.uid = member.id
        self.name = member.display_name
        self.side = side                  # "A" 공격 / "D" 수비
        self.max_hp = int(stats["hp"])
        self.atk = int(stats["atk"])
        self.dmg_done = 0
        self.hits = 0
        self.gcd_until = 0.0
        self.atk_ready = 0.0
        self.ambush_ready = 0.0


class WarSession:
    def __init__(self, guild_id: int, location: str, atk_fam: dict, def_fam: dict, starter: discord.Member):
        self.guild_id = guild_id
        self.location = location
        self.aid = atk_fam["id"]
        self.did = def_fam["id"]
        self.atk_name = atk_fam["name"]
        self.def_name = def_fam["name"]
        self.def_owner_id = def_fam.get("owner_id")
        self.starter = starter
        self.prize = _terr_info(location)["income"] * WAR_PRIZE_HOURS
        self.players = {}                 # {user_id: WarPlayer}
        self.state = "lobby"              # lobby → fighting → done
        self.cancelled = False
        self.start_event = asyncio.Event()
        self.wake = asyncio.Event()
        self.message = None
        self.logs = []
        self.pool = {"A": 0, "D": 0}
        self.pool_max = {"A": 1, "D": 1}
        self.npc = False                  # 수비 조직원이 없어서 NPC 수비대가 방어 중인지
        self.npc_atk = 0
        self.fight_end = 0.0
        self.fight_started = False

    def log(self, text: str):
        self.logs.append(text)
        self.logs = self.logs[-6:]

    def side_players(self, side: str) -> list:
        return [p for p in self.players.values() if p.side == side]


def war_release(s: WarSession):
    for uid in list(s.players.keys()):
        if war_user_map.get(uid) is s:
            war_user_map.pop(uid, None)
    for fid in (str(s.aid), str(s.did)):
        if war_family_map.get(fid) is s:
            war_family_map.pop(fid, None)
    key = (s.guild_id, s.location)
    if active_wars.get(key) is s:
        active_wars.pop(key, None)


async def _war_edit(s: WarSession, **kwargs):
    if not s.message:
        return
    try:
        await s.message.edit(**kwargs)
    except discord.HTTPException as e:
        print(f"War message edit error: {e}")


def war_lobby_embed(s: WarSession) -> discord.Embed:
    def fmt(ps):
        return "\n".join(f"• {p.member.mention} (HP {p.max_hp:,} / ATK {p.atk:,})" for p in ps) or "-"
    atk, dfn = s.side_players("A"), s.side_players("D")
    embed = discord.Embed(
        title=f"[구역 선전포고] {s.location}",
        description=(f"**「{s.atk_name}」** 이(가) **「{s.def_name}」** 의 구역을 노립니다!\n\n"
                     f"**전리품(승리 측 분배):** `${s.prize:,}` / 명예 `+{WAR_WIN_HONOR}pt`\n"
                     f"**공격 최소 인원:** {WAR_MIN_ATTACKERS}명 · 한쪽 최대 {WAR_MAX_PER_SIDE}명\n"
                     f"**{WAR_LOBBY_SEC}초** 안에 참전해 주세요. 수비 조직원이 아무도 오지 않으면 **NPC 수비대**가 구역을 지킵니다."),
        color=discord.Color.dark_red()
    )
    embed.add_field(name=f"공격 「{s.atk_name}」 ({len(atk)}/{WAR_MAX_PER_SIDE})", value=fmt(atk), inline=True)
    embed.add_field(name=f"수비 「{s.def_name}」 ({len(dfn)}/{WAR_MAX_PER_SIDE})", value=fmt(dfn), inline=True)
    return embed


def war_fight_embed(s: WarSession) -> discord.Embed:
    def pct(side):
        return max(0, int(s.pool[side] * 100 / max(1, s.pool_max[side])))
    dname = "NPC 수비대" if s.npc else f"「{s.def_name}」"
    embed = discord.Embed(
        title=f"[구역 점령전] {s.location}",
        description=(f"공격 「{s.atk_name}」\n`{_bar(s.pool['A'], s.pool_max['A'], 16)}` **{pct('A')}%**\n"
                     f"수비 {dname}\n`{_bar(s.pool['D'], s.pool_max['D'], 16)}` **{pct('D')}%**\n"
                     f"종료 <t:{int(s.fight_end)}:R> (시간 종료 시 남은 병력 비율이 높은 쪽이 승리, 동률이면 수비 승리)"),
        color=discord.Color.red()
    )
    def lines(ps):
        return "\n".join(f"**{p.name}** · 딜 {p.dmg_done:,} ({p.hits}회)" for p in ps) or "-"
    embed.add_field(name="공격대", value=lines(s.side_players("A")), inline=True)
    embed.add_field(name="수비대", value=("NPC 수비대 (자동 반격)" if s.npc else lines(s.side_players("D"))), inline=True)
    embed.add_field(name="전황", value="\n".join(s.logs) or "전투 시작!", inline=False)
    embed.set_footer(text=f"공격 {int(WAR_ATK_CD_SEC)}초 / 기습 {int(WAR_AMBUSH_CD_SEC)}초 쿨타임 (기습은 {int(WAR_AMBUSH_FAIL * 100)}% 확률로 빗나감)")
    return embed


# ---------------------------------------------------------------- 모집 화면
class WarLobbyView(discord.ui.View):
    def __init__(self, session: WarSession):
        super().__init__(timeout=None)
        self.s = session

    async def _join(self, interaction: discord.Interaction, side: str):
        s = self.s
        user = interaction.user
        if s.state != "lobby":
            await interaction.response.send_message("이미 모집이 끝났습니다.", ephemeral=True)
            return
        if user.id in s.players:
            await interaction.response.send_message("이미 참전 중입니다.", ephemeral=True)
            return
        if user.id in war_user_map or user.id in raid_user_map:
            await interaction.response.send_message("이미 다른 점령전/레이드에 참가 중입니다.", ephemeral=True)
            return
        if not _is_mafia(user) or _is_jailed(user.id):
            await interaction.response.send_message("마피아 조직원만(감옥 수감 중 제외) 참전할 수 있습니다.", ephemeral=True)
            return
        if len(s.side_players(side)) >= WAR_MAX_PER_SIDE:
            await interaction.response.send_message("그쪽 정원이 가득 찼습니다.", ephemeral=True)
            return
        fid = s.aid if side == "A" else s.did
        try:
            fam = family_get_by_id(fid)
            if not fam or str(user.id) not in (fam.get("member_ids") or []):
                who = "공격" if side == "A" else "수비"
                await interaction.response.send_message(f"**{who} 조직**의 조직원만 이 버튼으로 참전할 수 있습니다.", ephemeral=True)
                return
            info = get_user_data(user.id)
        except Exception as e:
            print(f"War join error: {e}")
            await interaction.response.send_message(FAMILY_DB_ERROR_MSG, ephemeral=True)
            return
        if user.id in s.players or s.state != "lobby" or user.id in war_user_map:   # DB 호출 사이에 상태가 바뀐 경우
            await interaction.response.send_message("참전할 수 없는 상태입니다.", ephemeral=True)
            return
        s.players[user.id] = WarPlayer(user, side, compute_combat_stats(info["honor"], info.get("inventory", {})))
        war_user_map[user.id] = s
        await interaction.response.edit_message(embed=war_lobby_embed(s), view=self)

    @discord.ui.button(label="공격 참전", style=discord.ButtonStyle.danger)
    async def join_atk(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._join(interaction, "A")

    @discord.ui.button(label="수비 참전", style=discord.ButtonStyle.primary)
    async def join_def(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._join(interaction, "D")

    @discord.ui.button(label="바로 시작", style=discord.ButtonStyle.success)
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        s = self.s
        if interaction.user.id != s.starter.id:
            await interaction.response.send_message("선전포고한 사람만 시작할 수 있습니다.", ephemeral=True)
            return
        n = len(s.side_players("A"))
        if n < WAR_MIN_ATTACKERS:
            await interaction.response.send_message(f"공격 인원이 최소 **{WAR_MIN_ATTACKERS}명** 필요합니다. (현재 {n}명)", ephemeral=True)
            return
        await interaction.response.defer()
        s.start_event.set()

    @discord.ui.button(label="취소", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        s = self.s
        if interaction.user.id != s.starter.id:
            await interaction.response.send_message("선전포고한 사람만 취소할 수 있습니다.", ephemeral=True)
            return
        await interaction.response.defer()
        s.cancelled = True
        s.start_event.set()


# ---------------------------------------------------------------- 전투 화면 (버튼 2개)
def war_apply_hit(s: WarSession, p: WarPlayer, dmg: int):
    target = "D" if p.side == "A" else "A"
    s.pool[target] = max(0, s.pool[target] - dmg)
    p.dmg_done += dmg
    p.hits += 1
    if s.pool[target] <= 0:
        s.wake.set()


class WarFightView(discord.ui.View):
    def __init__(self, session: WarSession):
        super().__init__(timeout=None)
        self.s = session

    async def _prep(self, interaction: discord.Interaction):
        s = self.s
        p = s.players.get(interaction.user.id)
        if s.state != "fighting":
            await interaction.response.send_message("이미 전투가 끝났습니다.", ephemeral=True)
            return None
        if not p:
            await interaction.response.send_message("이 점령전의 참전자가 아닙니다.", ephemeral=True)
            return None
        wait = p.gcd_until - time.time()
        if wait > 0:
            await interaction.response.send_message(f"아직 손이 풀리지 않았습니다. ({wait:.1f}초)", ephemeral=True)
            return None
        return p

    def _roll(self, p: WarPlayer, mult: float = 1.0) -> int:
        dmg = int(p.atk * random.uniform(0.9, 1.1) * mult)
        if p.side == "D":
            dmg = int(dmg * WAR_DEFENDER_BONUS)
        return max(10, dmg)

    @discord.ui.button(label="공격", style=discord.ButtonStyle.danger)
    async def attack(self, interaction: discord.Interaction, button: discord.ui.Button):
        p = await self._prep(interaction)
        if not p: return
        now = time.time()
        if p.atk_ready > now:
            await interaction.response.send_message(f"공격 쿨타임 {p.atk_ready - now:.1f}초", ephemeral=True)
            return
        war_apply_hit(self.s, p, self._roll(p))
        p.atk_ready = now + WAR_ATK_CD_SEC
        p.gcd_until = now + WAR_GCD_SEC
        await interaction.response.defer()

    @discord.ui.button(label="기습", style=discord.ButtonStyle.primary)
    async def ambush(self, interaction: discord.Interaction, button: discord.ui.Button):
        p = await self._prep(interaction)
        if not p: return
        s = self.s
        now = time.time()
        if p.ambush_ready > now:
            await interaction.response.send_message(f"기습 쿨타임 {p.ambush_ready - now:.1f}초", ephemeral=True)
            return
        p.ambush_ready = now + WAR_AMBUSH_CD_SEC
        p.gcd_until = now + WAR_GCD_SEC
        if random.random() < WAR_AMBUSH_FAIL:
            s.log(f"{p.name}의 기습이 들통났습니다! (빗나감)")
        else:
            dmg = self._roll(p, WAR_AMBUSH_MULT)
            war_apply_hit(s, p, dmg)
            s.log(f"{p.name}의 기습 성공! `{dmg:,}` 피해")
        await interaction.response.defer()


# ---------------------------------------------------------------- 진행 / 정산
def _war_refund_sync(s: WarSession):
    fam = family_get_by_id(s.aid)
    if fam:
        family_update(s.aid, {"treasury": fam.get("treasury", 0) + WAR_COST})


def _war_still_valid_sync(s: WarSession) -> bool:
    """모집 중 구역 주인이 바뀌었거나(해산 등) 공격 측이 보유 한도에 도달했는지 확인"""
    row = terr_get(s.guild_id, s.location)
    if not row or row["family_id"] != str(s.did):
        return False
    return len(terr_list_by_family(s.aid)) < TERRITORY_MAX_PER_FAMILY


def _war_settle_sync(s: WarSession, atk_win: bool):
    now = time.time()
    winners = [p for p in s.players.values() if (p.side == "A") == atk_win]
    losers = [p for p in s.players.values() if (p.side == "A") != atk_win]
    results = []   # (player, money, honor, won)

    win_dmg = sum(p.dmg_done for p in winners)
    for p in winners:
        share = (p.dmg_done / win_dmg) if win_dmg > 0 else (1 / max(1, len(winners)))
        money = int(s.prize * (WAR_BASE_SHARE / max(1, len(winners)) + (1 - WAR_BASE_SHARE) * share))
        honor = WAR_WIN_HONOR if p.hits > 0 else 0
        if p.hits <= 0:
            money = 0   # 한 번도 공격하지 않으면 받을 수 없음
        try:
            update_user_stats(p.uid, money, honor)
        except Exception as e:
            print(f"War reward error: {e}")
        results.append((p, money, honor, True))
        if p.hits > 0:   # 업적 카운터 (구역 점령전 승리 / 공격 측 승리 = 구역 점령)
            try:
                incs = {"war_wins": 1}
                if atk_win:
                    incs["war_conquests"] = 1
                bump_user_stats_sync(p.uid, incs, pending=True)
            except Exception as e:
                print(f"War achievement error ({p.uid}): {e}")

    for p in losers:
        money = int(s.prize * WAR_LOSE_MONEY_RATIO / max(1, len(losers))) if p.hits > 0 else 0
        if money:
            try:
                update_user_stats(p.uid, money, 0)
            except Exception as e:
                print(f"War consolation error: {e}")
        results.append((p, money, 0, False))

    note = ""
    row = terr_get(s.guild_id, s.location)
    if row and row["family_id"] == str(s.did):
        if atk_win:
            # 수비 조직이 아직 못 거둔 수입은 금고로 정산해 준다
            accrued = terr_accrued(row, now)
            try:
                dfam = family_get_by_id(s.did)
                if dfam and accrued > 0:
                    family_update(s.did, {"treasury": dfam.get("treasury", 0) + accrued})
            except Exception as e:
                print(f"War accrued payout error: {e}")
            if len(terr_list_by_family(s.aid)) >= TERRITORY_MAX_PER_FAMILY:
                terr_delete(row["id"])
                note = f"공격 조직이 보유 한도({TERRITORY_MAX_PER_FAMILY}곳)에 도달해 구역이 **무주공산**이 되었습니다."
            else:
                terr_update(row["id"], {"family_id": str(s.aid), "collected_at": int(now),
                                        "protected_until": int(now + TERRITORY_PROTECT_SEC)})
                note = f"**{s.location}** 은(는) 이제 **「{s.atk_name}」** 의 구역입니다! ({_fmt_sec(TERRITORY_PROTECT_SEC)} 동안 보호)"
        else:
            terr_update(row["id"], {"protected_until": int(max(float(row.get("protected_until") or 0), now + TERRITORY_DEFEND_PROTECT_SEC))})
            note = f"**「{s.def_name}」** 이(가) 구역을 지켜냈습니다!"
    else:
        note = "구역 주인이 바뀌어 점령 결과가 반영되지 않았습니다."
    win_fid, lose_fid = (s.aid, s.did) if atk_win else (s.did, s.aid)
    family_stat_inc(win_fid, "war_wins")
    family_stat_inc(lose_fid, "war_losses")
    return results, note


async def run_war(s: WarSession):
    try:
        # ── 1) 모집
        try:
            await asyncio.wait_for(s.start_event.wait(), timeout=WAR_LOBBY_SEC)
        except asyncio.TimeoutError:
            pass

        n_atk = len(s.side_players("A"))
        valid = True
        if not s.cancelled and n_atk >= WAR_MIN_ATTACKERS:
            valid = await asyncio.to_thread(_war_still_valid_sync, s)
        if s.cancelled or n_atk < WAR_MIN_ATTACKERS or not valid:
            s.state = "done"
            try:
                await asyncio.to_thread(_war_refund_sync, s)
            except Exception as e:
                print(f"War refund error: {e}")
            if s.cancelled:
                reason = "선전포고한 사람이 점령전을 취소했습니다."
            elif n_atk < WAR_MIN_ATTACKERS:
                reason = f"공격 최소 인원({WAR_MIN_ATTACKERS}명)이 모이지 않았습니다."
            else:
                reason = "구역 상황이 바뀌어(주인 변경/보유 한도) 점령전을 진행할 수 없습니다."
            embed = discord.Embed(title="[점령전 취소]", description=f"{reason}\n선전포고 비용 `${WAR_COST:,}` 은(는) 조직 금고로 반환되었습니다.", color=discord.Color.dark_grey())
            await _war_edit(s, embed=embed, view=None)
            return

        # ── 2) 전투 준비
        atk_players = s.side_players("A")
        def_players = s.side_players("D")
        s.pool["A"] = s.pool_max["A"] = max(1, sum(p.max_hp for p in atk_players))
        if def_players:
            s.pool["D"] = s.pool_max["D"] = max(1, sum(p.max_hp for p in def_players))
        else:
            s.npc = True
            s.pool["D"] = s.pool_max["D"] = max(1, int(s.pool["A"] * WAR_GARRISON_HP_RATIO))
            s.npc_atk = int(sum(p.atk for p in atk_players) * WAR_GARRISON_ATK_RATIO)
        s.state = "fighting"
        s.fight_started = True
        s.fight_end = time.time() + WAR_DURATION_SEC
        war_family_cooldowns[str(s.aid)] = time.time() + WAR_FAMILY_COOLDOWN_SEC
        s.log(f"점령전 시작! 공격 {len(atk_players)}명 vs " + ("NPC 수비대" if s.npc else f"수비 {len(def_players)}명"))
        await _war_edit(s, embed=war_fight_embed(s), view=WarFightView(s))

        # ── 3) 전투 루프
        atk_win = False
        reason = ""
        while True:
            try:
                await asyncio.wait_for(s.wake.wait(), timeout=WAR_TICK_SEC)
            except asyncio.TimeoutError:
                pass
            s.wake.clear()

            if s.npc and s.pool["D"] > 0 and s.pool["A"] > 0:
                s.pool["A"] = max(0, s.pool["A"] - int(s.npc_atk * random.uniform(0.8, 1.2)))
            if s.pool["D"] <= 0:
                atk_win, reason = True, "수비대가 무너졌습니다!"
                break
            if s.pool["A"] <= 0:
                atk_win, reason = False, "공격대가 전멸했습니다..."
                break
            if time.time() >= s.fight_end:
                ra = s.pool["A"] / s.pool_max["A"]
                rd = s.pool["D"] / s.pool_max["D"]
                atk_win = ra > rd
                reason = "시간이 종료되었습니다. 남은 병력 비율로 승패를 가립니다."
                break
            await _war_edit(s, embed=war_fight_embed(s))

        s.state = "done"

        # ── 4) 정산
        results, note = await asyncio.to_thread(_war_settle_sync, s, atk_win)
        title, color = ("[점령전 - 공격 측 승리]", discord.Color.gold()) if atk_win else ("[점령전 - 수비 측 승리]", discord.Color.blue())
        lines = []
        for p, money, honor, won in sorted(results, key=lambda r: -r[0].dmg_done):
            side = "공격" if p.side == "A" else "수비"
            tag = "승" if won else "패"
            lines.append(f"• [{side}/{tag}] **{p.name}** — 딜 {p.dmg_done:,} ({p.hits}회) → `${money:,}`" + (f" / 명예 `+{honor}pt`" if honor else ""))
        if not lines:
            lines.append("참전자가 없습니다.")
        embed = discord.Embed(title=f"{title} {s.location}", description=f"{reason}\n\n{note}\n\n" + "\n".join(lines), color=color)
        await _war_edit(s, embed=embed, view=None)
    except Exception as e:
        print(f"War error: {e}")
        if not s.fight_started:   # 전투 시작 전에 오류가 났다면 선전포고 비용 반환
            try:
                await asyncio.to_thread(_war_refund_sync, s)
            except Exception:
                pass
        s.state = "done"
        await _war_edit(s, embed=discord.Embed(title="[점령전 중단]", description="처리 중 오류가 발생해 점령전이 중단되었습니다.", color=discord.Color.red()), view=None)
    finally:
        s.state = "done"
        war_release(s)


# ================================================================ /구역
territory_group = app_commands.Group(name="구역", description="[패밀리] 구역을 점령하고 수익을 거둡니다.")
bot.tree.add_command(territory_group)

_LOCATION_CHOICES = [app_commands.Choice(name=k, value=k) for k in LOCATIONS]


@territory_group.command(name="현황", description="5개 지역의 점령 현황과 수입을 확인합니다.")
async def territory_status_cmd(interaction: discord.Interaction):
    if not await check_maintenance(interaction): return
    if not await check_membership(interaction): return
    guild_id = interaction.guild_id
    now = time.time()
    try:
        owned = terr_list(guild_id)
        names = {}
        for row in owned.values():
            fid = row["family_id"]
            if fid not in names:
                fam = family_get_by_id(fid)
                names[fid] = fam["name"] if fam else "(해산된 조직)"
    except Exception as e:
        print(f"Territory status error: {e}")
        await interaction.response.send_message(TERRITORY_DB_ERROR_MSG, ephemeral=True)
        return

    embed = discord.Embed(title="구역 점령 현황", color=discord.Color.dark_gold())
    for loc in LOCATIONS:
        info = _terr_info(loc)
        row = owned.get(loc)
        flags = []
        if (guild_id, loc) in active_wars:
            flags.append("점령전 진행 중")
        if _patrolled(guild_id, loc):
            flags.append("경찰 순찰 중")
        if row:
            prot = float(row.get("protected_until") or 0) - now
            if prot > 0:
                flags.append(f"보호 {_fmt_sec(prot)} 남음")
            value = (f"**주인:** 「{names.get(row['family_id'], '?')}」\n"
                     f"**수입:** `${info['income']:,}`/시간 (미수금 `${terr_accrued(row, now):,}`)")
        else:
            value = (f"**주인:** 없음 (`/구역 점령` 비용 `${info['claim']:,}`)\n"
                     f"**수입:** `${info['income']:,}`/시간")
        if flags:
            value += "\n" + " · ".join(flags)
        embed.add_field(name=loc, value=value, inline=False)
    embed.set_footer(text=f"조직당 최대 {TERRITORY_MAX_PER_FAMILY}곳 · 미수금은 최대 {TERRITORY_INCOME_CAP_HOURS}시간분까지 쌓입니다.")
    await interaction.response.send_message(embed=embed)


@territory_group.command(name="점령", description="[조직장] 주인 없는 구역을 조직 금고로 점령합니다.")
@app_commands.describe(지역="점령할 지역")
@app_commands.choices(지역=_LOCATION_CHOICES)
async def territory_claim_cmd(interaction: discord.Interaction, 지역: app_commands.Choice[str]):
    if not await _family_precheck(interaction): return
    user = interaction.user
    loc = 지역.value
    info = _terr_info(loc)
    guild_id = interaction.guild_id
    try:
        fam = family_get_by_user(guild_id, user.id)
        if not fam or fam["owner_id"] != str(user.id):
            await interaction.response.send_message("조직장만 사용할 수 있습니다.", ephemeral=True)
            return
        if _patrolled(guild_id, loc):
            await interaction.response.send_message(f"**{loc}** 은(는) 경찰이 순찰 중이라 지금은 움직일 수 없습니다.", ephemeral=True)
            return
        if str(fam["id"]) in war_family_map:
            await interaction.response.send_message("점령전이 진행 중인 조직은 구역을 점령할 수 없습니다.", ephemeral=True)
            return
        row = terr_get(guild_id, loc)
        if row:
            if row["family_id"] == str(fam["id"]):
                await interaction.response.send_message("이미 우리 조직의 구역입니다.", ephemeral=True)
            else:
                await interaction.response.send_message("이미 다른 조직이 차지한 구역입니다. `/구역 선전포고` 로 빼앗아야 합니다.", ephemeral=True)
            return
        if len(terr_list_by_family(fam["id"])) >= TERRITORY_MAX_PER_FAMILY:
            await interaction.response.send_message(f"조직은 구역을 최대 **{TERRITORY_MAX_PER_FAMILY}곳**까지만 가질 수 있습니다.", ephemeral=True)
            return
        if fam.get("treasury", 0) < info["claim"]:
            await interaction.response.send_message(f"조직 금고가 부족합니다. (점령 비용 `${info['claim']:,}` / 금고 `${fam.get('treasury', 0):,}`)\n`/조직 기부` 로 금고를 채워 보세요.", ephemeral=True)
            return
        family_update(fam["id"], {"treasury": fam["treasury"] - info["claim"]})
        try:
            terr_insert(guild_id, loc, fam["id"], time.time())
        except Exception as e:
            # 동시에 다른 조직이 먼저 차지했거나 DB 오류 → 비용 환불
            print(f"Territory claim insert error: {e}")
            fresh = family_get_by_id(fam["id"])
            family_update(fam["id"], {"treasury": (fresh or fam).get("treasury", 0) + info["claim"]})
            await interaction.response.send_message("점령에 실패했습니다. (다른 조직이 먼저 차지했거나 DB 오류) 비용은 환불되었습니다.", ephemeral=True)
            return
    except Exception as e:
        print(f"Territory claim error: {e}")
        await interaction.response.send_message(TERRITORY_DB_ERROR_MSG, ephemeral=True)
        return
    await interaction.response.send_message(
        f"**「{fam['name']}」** 이(가) **{loc}** 을(를) 점령했습니다! (비용 `${info['claim']:,}` · 시간당 수입 `${info['income']:,}` · {_fmt_sec(TERRITORY_PROTECT_SEC)} 동안 보호)")


@territory_group.command(name="선전포고", description=f"[조직장] 다른 조직의 구역에 점령전을 선포합니다. (조직 금고 ${WAR_COST:,})")
@app_commands.describe(지역="빼앗을 지역")
@app_commands.choices(지역=_LOCATION_CHOICES)
async def territory_war_cmd(interaction: discord.Interaction, 지역: app_commands.Choice[str]):
    if not await _family_precheck(interaction): return
    user = interaction.user
    loc = 지역.value
    guild_id = interaction.guild_id
    try:
        fam = family_get_by_user(guild_id, user.id)
        if not fam or fam["owner_id"] != str(user.id):
            await interaction.response.send_message("조직장만 사용할 수 있습니다.", ephemeral=True)
            return
        fid = str(fam["id"])
        if fid in active_raids or fam["id"] in active_raids:
            await interaction.response.send_message("레이드 진행 중에는 선전포고할 수 없습니다.", ephemeral=True)
            return
        if fid in war_family_map or user.id in war_user_map or user.id in raid_user_map:
            await interaction.response.send_message("이미 점령전(또는 레이드)에 참여 중입니다.", ephemeral=True)
            return
        cd = war_family_cooldowns.get(fid, 0)
        if time.time() < cd:
            await interaction.response.send_message(f"조직이 정비 중입니다. **{_fmt_sec(cd - time.time())}** 뒤에 다시 선전포고할 수 있습니다.", ephemeral=True)
            return
        if _patrolled(guild_id, loc):
            await interaction.response.send_message(f"**{loc}** 은(는) 경찰이 순찰 중이라 지금은 움직일 수 없습니다.", ephemeral=True)
            return
        row = terr_get(guild_id, loc)
        if not row:
            await interaction.response.send_message("주인 없는 구역입니다. `/구역 점령` 으로 차지하세요.", ephemeral=True)
            return
        if row["family_id"] == fid:
            await interaction.response.send_message("우리 조직의 구역입니다.", ephemeral=True)
            return
        prot = float(row.get("protected_until") or 0) - time.time()
        if prot > 0:
            await interaction.response.send_message(f"이 구역은 보호 중입니다. **{_fmt_sec(prot)}** 뒤에 공격할 수 있습니다.", ephemeral=True)
            return
        if row["family_id"] in war_family_map or (guild_id, loc) in active_wars:
            await interaction.response.send_message("상대 조직(또는 이 구역)이 이미 점령전에 휘말려 있습니다.", ephemeral=True)
            return
        if len(terr_list_by_family(fam["id"])) >= TERRITORY_MAX_PER_FAMILY:
            await interaction.response.send_message(f"조직은 구역을 최대 **{TERRITORY_MAX_PER_FAMILY}곳**까지만 가질 수 있어, 새 구역을 빼앗을 수 없습니다. (`/구역 포기` 로 정리하세요)", ephemeral=True)
            return
        if fam.get("treasury", 0) < WAR_COST:
            await interaction.response.send_message(f"조직 금고가 부족합니다. (선전포고 비용 `${WAR_COST:,}` / 금고 `${fam.get('treasury', 0):,}`)", ephemeral=True)
            return
        dfam = family_get_by_id(row["family_id"])
        if not dfam:
            await interaction.response.send_message("구역 주인 정보를 찾을 수 없습니다. 잠시 뒤 다시 시도해 주세요.", ephemeral=True)
            return
        info = get_user_data(user.id)

        # 자리 선점 (여기부터 await 전까지는 다른 명령어가 끼어들 수 없음)
        session = WarSession(guild_id, loc, fam, dfam, user)
        session.players[user.id] = WarPlayer(user, "A", compute_combat_stats(info["honor"], info.get("inventory", {})))
        active_wars[(guild_id, loc)] = session
        war_family_map[fid] = session
        war_family_map[str(dfam["id"])] = session
        war_user_map[user.id] = session
        try:
            family_update(fam["id"], {"treasury": fam["treasury"] - WAR_COST})
        except Exception:
            war_release(session)
            raise
    except Exception as e:
        print(f"Territory war error: {e}")
        await interaction.response.send_message(TERRITORY_DB_ERROR_MSG, ephemeral=True)
        return

    ping = f"<@{session.def_owner_id}> " if session.def_owner_id else ""
    await interaction.response.send_message(
        content=f"{ping}**「{session.def_name}」** 의 **{loc}** 에 선전포고가 날아왔습니다! 조직원은 `수비 참전`을 눌러 구역을 지키세요!",
        embed=war_lobby_embed(session), view=WarLobbyView(session))
    session.message = await interaction.original_response()
    asyncio.create_task(run_war(session))


@territory_group.command(name="수금", description="우리 조직 구역에 쌓인 수입을 조직 금고로 거둡니다.")
async def territory_collect_cmd(interaction: discord.Interaction):
    if not await _family_precheck(interaction): return
    user = interaction.user
    now = time.time()
    try:
        fam = family_get_by_user(interaction.guild_id, user.id)
        if not fam:
            await interaction.response.send_message("소속된 조직이 없습니다.", ephemeral=True)
            return
        rows = terr_list_by_family(fam["id"])
        if not rows:
            await interaction.response.send_message("우리 조직이 가진 구역이 없습니다. `/구역 현황` 을 확인해 보세요.", ephemeral=True)
            return
        total = 0
        lines = []
        for row in rows:
            amount = terr_accrued(row, now)
            total += amount
            lines.append(f"• **{row['location']}** `+${amount:,}`")
        if total <= 0:
            await interaction.response.send_message("아직 거둘 수입이 없습니다. 조금 뒤에 다시 와 주세요.", ephemeral=True)
            return
        fresh = family_get_by_id(fam["id"])   # 최신 금고 값으로 갱신
        family_update(fam["id"], {"treasury": (fresh or fam).get("treasury", 0) + total})
        for row in rows:
            terr_update(row["id"], {"collected_at": int(now)})
    except Exception as e:
        print(f"Territory collect error: {e}")
        await interaction.response.send_message(TERRITORY_DB_ERROR_MSG, ephemeral=True)
        return
    await interaction.response.send_message(f"**{user.mention}** 님이 구역 수입을 거뒀습니다!\n" + "\n".join(lines) + f"\n합계 `${total:,}` → 조직 금고")


class TerritoryAbandonView(discord.ui.View):
    def __init__(self, owner: discord.Member, row_id, fid, location: str):
        super().__init__(timeout=30)
        self.owner = owner
        self.row_id = row_id
        self.fid = fid
        self.location = location

    @discord.ui.button(label="정말 포기합니다", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner.id:
            await interaction.response.send_message("조직장만 누를 수 있습니다.", ephemeral=True)
            return
        self.stop()
        try:
            if str(self.fid) in war_family_map:
                await interaction.response.edit_message(content="점령전 진행 중에는 구역을 포기할 수 없습니다.", view=None)
                return
            row = terr_get(interaction.guild_id, self.location)
            if not row or row["family_id"] != str(self.fid):
                await interaction.response.edit_message(content="이미 우리 조직의 구역이 아닙니다.", view=None)
                return
            accrued = terr_accrued(row)
            fam = family_get_by_id(self.fid)
            if fam and accrued > 0:
                family_update(self.fid, {"treasury": fam.get("treasury", 0) + accrued})   # 쌓인 수입은 정산
            terr_delete(row["id"])
        except Exception as e:
            print(f"Territory abandon error: {e}")
            await interaction.response.edit_message(content=TERRITORY_DB_ERROR_MSG, view=None)
            return
        await interaction.response.edit_message(content=f"**{self.location}** 구역을 포기했습니다. (쌓인 수입은 금고로 정산, 점령 비용은 돌려받지 않습니다)", view=None)


@territory_group.command(name="포기", description="[조직장] 우리 조직의 구역을 포기합니다. (점령 비용은 돌려받지 않습니다)")
@app_commands.describe(지역="포기할 지역")
@app_commands.choices(지역=_LOCATION_CHOICES)
async def territory_abandon_cmd(interaction: discord.Interaction, 지역: app_commands.Choice[str]):
    if not await _family_precheck(interaction): return
    user = interaction.user
    loc = 지역.value
    try:
        fam = family_get_by_user(interaction.guild_id, user.id)
        if not fam or fam["owner_id"] != str(user.id):
            await interaction.response.send_message("조직장만 사용할 수 있습니다.", ephemeral=True)
            return
        row = terr_get(interaction.guild_id, loc)
    except Exception as e:
        print(f"Territory abandon lookup error: {e}")
        await interaction.response.send_message(TERRITORY_DB_ERROR_MSG, ephemeral=True)
        return
    if not row or row["family_id"] != str(fam["id"]):
        await interaction.response.send_message("우리 조직의 구역이 아닙니다.", ephemeral=True)
        return
    await interaction.response.send_message(
        f"정말 **{loc}** 을(를) 포기할까요? 점령 비용은 돌려받지 못합니다.",
        view=TerritoryAbandonView(user, fam["id"], fam["id"], loc), ephemeral=True)


# ==========================================================================
# [조직 현황 · 랭킹 · 조직 누적 통계]   /조직 현황 · /랭킹
#
#   • 조직의 기본 데이터(조직원·금고·레이드 점수)는 원래부터 Supabase `families` 테이블에 저장됩니다.
#   • 여기서는 누적 통계(레이드 클리어 / 점령전 승·패)를 같은 테이블의 컬럼으로 추가 저장합니다.
#       → family_stats.sql 을 Supabase 에서 한 번 실행해야 합니다.
#       → 컬럼이 없어도 레이드/점령전 보상 지급 등 본 기능은 깨지지 않고, 통계만 기록되지 않습니다.
#   • /랭킹 은 명예·자금(개인) / 조직 레이드 점수·점령전 승수(조직) 를 보여줍니다.
#       - 개인 랭킹은 user_data 가 서버별로 나뉘어 있지 않으므로, 상위 RANK_SCAN_N 명 중
#         '이 서버에 있는 사람'만 추려 보여줍니다.
# ==========================================================================
RANK_TOP_N = 10          # 랭킹에 보여줄 인원 / 조직 수
RANK_SCAN_N = 200        # 개인 랭킹 집계 시 DB 에서 읽어올 상위 인원 수
FAMILY_STATS_DB_HINT = "(관리자: `families` 테이블에 통계 컬럼이 있는지 확인해 주세요 — family_stats.sql 참고)"


def family_stat_inc(fid, field: str, n: int = 1):
    """조직 누적 통계를 DB 에서 +n. 실패해도 호출한 쪽(보상 지급 등)이 깨지지 않도록 로그만 남깁니다."""
    try:
        fam = family_get_by_id(fid)
        if fam:
            family_update(fid, {field: int(fam.get(field) or 0) + n})
    except Exception as e:
        print(f"Family stat error ({field}): {e}")


def family_member_stats(member_ids: list) -> dict:
    """{user_id(str): {money, honor}}  (가입 기록이 없는 사람은 빠짐 — get_user_data 와 달리 행을 새로 만들지 않음)"""
    if not member_ids:
        return {}
    res = (supabase.table("user_data").select("user_id,money,honor")
           .in_("user_id", [str(m) for m in member_ids]).execute())
    return {str(r["user_id"]): r for r in (res.data or [])}


def family_list_top_by(guild_id: int, field: str, limit: int = RANK_TOP_N):
    res = (supabase.table("families").select("*").eq("guild_id", str(guild_id))
           .order(field, desc=True).limit(limit).execute())
    return res.data or []


def user_rank_rows(field: str, limit: int = RANK_SCAN_N):
    res = supabase.table("user_data").select(f"user_id,{field}").order(field, desc=True).limit(limit).execute()
    return res.data or []


# ---------------------------------------------------------------- /조직 현황
def family_status_embed(fam: dict, stats: dict, terr_rows, now: float = None) -> discord.Embed:
    """terr_rows: 보유 구역 행 목록 (None 이면 구역 정보를 불러오지 못한 것)"""
    now = now or time.time()
    members = list(fam.get("member_ids") or [])
    wins, losses = int(fam.get("war_wins") or 0), int(fam.get("war_losses") or 0)
    total_war = wins + losses
    rate = f" (승률 {int(wins * 100 / total_war)}%)" if total_war else ""
    total_honor = sum(int((stats.get(str(m)) or {}).get("honor") or 0) for m in members)

    embed = discord.Embed(title=f"조직 현황 「{fam['name']}」", color=discord.Color.dark_gold())
    embed.add_field(
        name="개요",
        value=(f"**조직장:** <@{fam['owner_id']}> · **조직원:** {len(members)}/{FAMILY_MAX_MEMBERS}\n"
               f"**금고:** `${fam.get('treasury', 0):,}` · **조직원 명예 합계:** `{total_honor:,} pt`"),
        inline=False)
    embed.add_field(
        name="전적",
        value=(f"레이드 점수 `{int(fam.get('raid_points') or 0):,} pt` · 클리어 `{int(fam.get('raid_clears') or 0)}회`\n"
               f"구역 점령전 `{wins}승 {losses}패`{rate}"),
        inline=False)

    if terr_rows is None:
        terr_text = "구역 정보를 불러오지 못했습니다."
    elif not terr_rows:
        terr_text = "보유 구역 없음 (`/구역 현황` 에서 점령 가능한 구역을 확인하세요)"
    else:
        lines, hourly, pending = [], 0, 0
        for row in sorted(terr_rows, key=lambda r: r["location"]):
            inc = _terr_info(row["location"])["income"]
            acc = terr_accrued(row, now)
            hourly += inc
            pending += acc
            prot = float(row.get("protected_until") or 0) - now
            tail = f" · 보호 {_fmt_sec(prot)}" if prot > 0 else ""
            lines.append(f"• **{row['location']}** `${inc:,}`/시간 · 미수금 `${acc:,}`{tail}")
        lines.append(f"합계 `${hourly:,}`/시간 · 미수금 `${pending:,}` (`/구역 수금`)")
        terr_text = "\n".join(lines)
    embed.add_field(name=f"보유 구역 ({len(terr_rows) if terr_rows else 0}/{TERRITORY_MAX_PER_FAMILY})", value=terr_text, inline=False)

    def _honor(m):
        return int((stats.get(str(m)) or {}).get("honor") or 0)
    lines = []
    for m in sorted(members, key=lambda m: (-_honor(m), str(m))):
        st = stats.get(str(m))
        crown = "[조직장] " if str(m) == str(fam["owner_id"]) else "• "
        if st:
            lines.append(f"{crown}<@{m}> — 명예 `{int(st.get('honor') or 0):,}` · `${int(st.get('money') or 0):,}`")
        else:
            lines.append(f"{crown}<@{m}>")
    embed.add_field(name="조직원 (명예순)", value="\n".join(lines) or "-", inline=False)

    status = []
    raid = active_raids.get(fam["id"])
    if raid:
        status.append(f"레이드: {raid.tier['name']} ({'모집 중' if raid.state == 'lobby' else '전투 중'})")
    cd = raid_family_cooldowns.get(fam["id"])
    if cd and datetime.now() < cd:
        status.append(f"다음 레이드까지 {int((cd - datetime.now()).total_seconds() // 60) + 1}분")
    war = war_family_map.get(str(fam["id"]))
    if war:
        role = "공격" if str(war.aid) == str(fam["id"]) else "수비"
        status.append(f"점령전: {war.location} ({role}, {'모집 중' if war.state == 'lobby' else '전투 중'})")
    wcd = war_family_cooldowns.get(str(fam["id"]), 0)
    if now < wcd:
        status.append(f"다음 선전포고까지 {_fmt_sec(wcd - now)}")
    if status:
        embed.add_field(name="진행 상태", value="\n".join(status), inline=False)
    return embed


@family_group.command(name="현황", description="조직 종합 현황(금고·전적·구역·조직원)을 확인합니다. (이름을 비우면 내 조직)")
@app_commands.describe(이름="확인할 조직 이름 (비우면 내 조직)")
async def family_status_cmd(interaction: discord.Interaction, 이름: str = None):
    if not await check_maintenance(interaction): return
    try:
        fam = family_get_by_name(interaction.guild_id, 이름.strip()) if 이름 else family_get_by_user(interaction.guild_id, interaction.user.id)
        stats = family_member_stats(fam.get("member_ids") or []) if fam else {}
    except Exception as e:
        print(f"Family status error: {e}")
        await interaction.response.send_message(FAMILY_DB_ERROR_MSG, ephemeral=True)
        return
    if not fam:
        msg = "해당 이름의 조직이 없습니다." if 이름 else "소속된 조직이 없습니다. `/조직 생성` 또는 초대를 받아 보세요."
        await interaction.response.send_message(msg, ephemeral=True)
        return
    try:
        terr_rows = terr_list_by_family(fam["id"])
    except Exception as e:   # territories 테이블이 아직 없어도 현황은 보여준다
        print(f"Family status territory error: {e}")
        terr_rows = None
    await interaction.response.send_message(embed=family_status_embed(fam, stats, terr_rows))


# ---------------------------------------------------------------- /랭킹
@bot.tree.command(name="랭킹", description="[공용] 서버 랭킹을 확인합니다. (명예 / 자금 / 조직)")
@app_commands.describe(종류="확인할 랭킹")
@app_commands.choices(종류=[
    app_commands.Choice(name="명예 (개인)", value="honor"),
    app_commands.Choice(name="자금 (개인)", value="money"),
    app_commands.Choice(name="레이드 점수 (조직)", value="raid_points"),
    app_commands.Choice(name="점령전 승수 (조직)", value="war_wins"),
])
async def ranking_cmd(interaction: discord.Interaction, 종류: app_commands.Choice[str]):
    if not await check_maintenance(interaction): return
    kind = 종류.value
    guild = interaction.guild
    await interaction.response.defer()
    try:
        if kind in ("honor", "money"):
            rows = await asyncio.to_thread(user_rank_rows, kind, RANK_SCAN_N)
        else:
            rows = await asyncio.to_thread(family_list_top_by, interaction.guild_id, kind, RANK_TOP_N)
    except Exception as e:
        print(f"Ranking error: {e}")
        hint = FAMILY_STATS_DB_HINT if kind == "war_wins" else ""
        await interaction.followup.send(f"랭킹을 불러오지 못했습니다. {hint}".strip(), ephemeral=True)
        return

    if kind in ("honor", "money"):
        board = []   # [(member, value)]  이 서버에 있는 사람만
        for r in rows:
            try:
                m = guild.get_member(int(r["user_id"]))
            except (ValueError, TypeError):
                continue
            if m and not m.bot:
                board.append((m, int(r.get(kind) or 0)))
        unit = (lambda v: f"{v:,} pt") if kind == "honor" else (lambda v: f"${v:,}")
        title = "명예 랭킹" if kind == "honor" else "자금 랭킹"
        lines = [f"`{i + 1}위` **{m.display_name}** — {unit(v)}" for i, (m, v) in enumerate(board[:RANK_TOP_N])]
        me = next((i + 1 for i, (m, _) in enumerate(board) if m.id == interaction.user.id), None)
        footer = f"내 순위: {me}위" if me else f"내 순위: 집계 밖 (전체 상위 {RANK_SCAN_N}명 안에 없음)"
    else:
        title = "조직 랭킹 (레이드 점수)" if kind == "raid_points" else "조직 랭킹 (점령전 승수)"
        lines = []
        for i, r in enumerate(rows):
            n = len(r.get("member_ids") or [])
            if kind == "raid_points":
                lines.append(f"`{i + 1}위` **{r['name']}** — {int(r.get('raid_points') or 0):,} pt · {n}명")
            else:
                lines.append(f"`{i + 1}위` **{r['name']}** — {int(r.get('war_wins') or 0)}승 {int(r.get('war_losses') or 0)}패 · {n}명")
        footer = None

    if not lines:
        await interaction.followup.send("아직 집계할 기록이 없습니다.", ephemeral=True)
        return
    embed = discord.Embed(title=title, description="\n".join(lines), color=discord.Color.gold())
    if footer:
        embed.set_footer(text=footer)
    await interaction.followup.send(embed=embed)


# ==========================================================================
# [/탈옥]  숫자 야구(4자리) 탈옥 게임
#
#   • 수감 중인 조직원만 사용할 수 있습니다. (수감 중에는 다른 명령어가 막혀 있으므로 check_jail 은 쓰지 않습니다)
#   • 0~9 중 서로 다른 숫자 4개(0으로 시작 가능)를 ESCAPE_MAX_TRIES 번 안에 맞히면 즉시 석방됩니다.
#       - 스트라이크(S): 숫자와 자리가 모두 같음 / 볼(B): 숫자는 있지만 자리가 다름 / 둘 다 없으면 아웃
#   • 실패·포기·시간 초과 시 형기는 그대로이며, ESCAPE_COOLDOWN_SEC 뒤에 다시 도전할 수 있습니다. (0 이면 제한 없음)
#   • 게임이 진행되는 동안에는 명령어 동시 실행 방지 잠금이 유지됩니다. (GuardedView)
# ==========================================================================
ESCAPE_MAX_TRIES = 7
ESCAPE_DIGITS = 4
ESCAPE_COOLDOWN_SEC = 60      # 도전 시작 후 재도전까지 대기 시간(초)
ESCAPE_TIMEOUT_SEC = 300      # 게임 화면 유지 시간(초)

escape_cooldowns = {}   # {user_id: 재도전 가능 시각(time.time 기준)}


def baseball_judge(answer: str, guess: str):
    """(스트라이크, 볼) 반환"""
    strike = sum(a == g for a, g in zip(answer, guess))
    ball = len(set(answer) & set(guess)) - strike
    return strike, ball


def baseball_result_text(strike: int, ball: int) -> str:
    return "아웃" if strike == 0 and ball == 0 else f"{strike}S {ball}B"


def parse_baseball_guess(text: str):
    """(숫자 문자열, 오류 메시지) — 정상이면 오류는 None"""
    text = (text or "").strip()
    if not re.fullmatch(rf"[0-9]{{{ESCAPE_DIGITS}}}", text):
        return None, f"숫자 {ESCAPE_DIGITS}자리를 입력해 주세요. (예: 1234)"
    if len(set(text)) != ESCAPE_DIGITS:
        return None, "서로 다른 숫자 4개여야 합니다. (예: 1234 ✔ / 1123 ✘)"
    return text, None


class EscapeGuessModal(discord.ui.Modal, title="탈옥 — 숫자 입력"):
    guess = discord.ui.TextInput(
        label=f"서로 다른 숫자 {ESCAPE_DIGITS}자리",
        placeholder="예: 1234",
        min_length=ESCAPE_DIGITS,
        max_length=ESCAPE_DIGITS,
    )

    def __init__(self, game: "EscapeGameView"):
        super().__init__(timeout=ESCAPE_TIMEOUT_SEC)
        self.game = game

    async def on_submit(self, interaction: discord.Interaction):
        await self.game.submit_guess(interaction, str(self.guess.value))


class EscapeGameView(GuardedView):
    holds_command_lock = True   # 탈옥 게임이 끝날 때까지 다른 명령어 실행 차단

    def __init__(self, owner: discord.Member):
        super().__init__(timeout=ESCAPE_TIMEOUT_SEC)
        self.owner = owner
        self.answer = "".join(random.sample("0123456789", ESCAPE_DIGITS))
        self.history = []        # [(guess, strike, ball)]
        self.finished = False
        self.message = None

    # ---------------------------------------------------------------- 화면
    def build_embed(self, title: str = "[탈옥] 숫자 야구", note: str = "", color=None) -> discord.Embed:
        left = ESCAPE_MAX_TRIES - len(self.history)
        embed = discord.Embed(title=title, color=color or discord.Color.dark_grey())
        desc = (
            f"{self.owner.mention} 님이 탈옥을 시도합니다!\n"
            f"**{ESCAPE_MAX_TRIES}번 안에** 서로 다른 숫자 {ESCAPE_DIGITS}자리를 맞히면 **즉시 탈출**합니다.\n"
            f"`S`=숫자·자리 일치 / `B`=숫자만 일치 / 둘 다 없으면 `아웃`"
        )
        if note:
            desc += f"\n\n{note}"
        embed.description = desc
        if self.history:
            lines = [f"`{i + 1}회` **{g}** → {baseball_result_text(s, b)}" for i, (g, s, b) in enumerate(self.history)]
            embed.add_field(name="추리 기록", value="\n".join(lines), inline=False)
        embed.set_footer(text=f"남은 기회 {left}번 · {ESCAPE_TIMEOUT_SEC}초 이내에 진행하세요")
        if self.owner.avatar:
            embed.set_thumbnail(url=self.owner.avatar.url)
        return embed

    def _end(self):
        self.finished = True
        self.stop()

    # ---------------------------------------------------------------- 추측 처리
    async def submit_guess(self, interaction: discord.Interaction, raw: str):
        if self.finished:
            await interaction.response.send_message("이미 끝난 게임입니다.", ephemeral=True)
            return

        # 도중에 형기가 끝났다면(자연 석방) 게임 종료
        if not _is_jailed(self.owner.id):
            self._end()
            await interaction.response.edit_message(
                embed=self.build_embed("[탈옥] 종료", "이미 석방되어 더 이상 탈옥할 필요가 없습니다.", discord.Color.blue()),
                view=None)
            return

        guess, err = parse_baseball_guess(raw)
        if err:
            await interaction.response.send_message(err, ephemeral=True)   # 잘못된 입력은 기회를 소모하지 않음
            return

        if any(g == guess for g, _, _ in self.history):
            await interaction.response.send_message("이미 시도한 숫자입니다. (기회는 소모되지 않았습니다)", ephemeral=True)
            return

        strike, ball = baseball_judge(self.answer, guess)
        self.history.append((guess, strike, ball))

        if strike == ESCAPE_DIGITS:
            await self._success(interaction)
        elif len(self.history) >= ESCAPE_MAX_TRIES:
            await self._fail(interaction, f"기회를 모두 사용했습니다. 정답은 **{self.answer}** 였습니다.")
        else:
            await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _success(self, interaction: discord.Interaction):
        self._end()
        jail_list.pop(self.owner.id, None)
        save_status(self.owner.id)
        try:   # check_jail 의 석방 처리와 동일하게 위치를 기본 거점으로 복구
            await asyncio.to_thread(update_user_location, self.owner.id, "레벤하임")
        except Exception as e:
            print(f"DB Error (escape location): {e}")

        tries = len(self.history)
        embed = self.build_embed(
            "[탈옥 성공]",
            f"**{self.answer}** — 정답! **{tries}번 만에** 경비를 따돌리고 탈출했습니다!\n"
            f"{self.owner.mention} 님은 즉시 석방되었습니다.",
            discord.Color.green())
        embed.set_footer(text="탈옥 성공!")
        await interaction.response.edit_message(embed=embed, view=None)

    async def _fail(self, interaction: discord.Interaction, note: str, edit=None):
        self._end()
        embed = self.build_embed("[탈옥 실패]", f"{note}\n경비에게 붙잡혀 다시 감방으로 돌아갑니다.", discord.Color.red())
        embed.set_footer(text=(f"{ESCAPE_COOLDOWN_SEC}초 뒤에 다시 도전할 수 있습니다." if ESCAPE_COOLDOWN_SEC else "탈옥 실패"))
        if interaction is not None:
            await interaction.response.edit_message(embed=embed, view=None)
        elif edit is not None:
            await edit(embed=embed, view=None)

    # ---------------------------------------------------------------- 버튼
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner.id:
            await interaction.response.send_message("본인의 탈옥 시도만 조작할 수 있습니다.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="숫자 입력", style=discord.ButtonStyle.primary)
    async def guess_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.finished:
            await interaction.response.send_message("이미 끝난 게임입니다.", ephemeral=True)
            return
        await interaction.response.send_modal(EscapeGuessModal(self))

    @discord.ui.button(label="포기", style=discord.ButtonStyle.danger)
    async def give_up_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.finished:
            await interaction.response.send_message("이미 끝난 게임입니다.", ephemeral=True)
            return
        await self._fail(interaction, "탈옥을 포기했습니다.")

    async def on_timeout(self):
        if self.finished or self.message is None:
            return
        self._end()
        await self._fail(None, "시간이 초과되었습니다.", edit=self.message.edit)


@bot.tree.command(name="탈옥", description=f"[조직원 전용] 숫자 야구({ESCAPE_DIGITS}자리)를 {ESCAPE_MAX_TRIES}번 안에 맞히면 즉시 탈옥합니다.")
async def escape_command(interaction: discord.Interaction):
    if not await check_maintenance(interaction): return
    user = interaction.user

    if not _is_mafia(user):
        await interaction.response.send_message("탈옥은 마피아 조직원만 시도할 수 있습니다.", ephemeral=True)
        return

    if not _is_jailed(user.id):
        await interaction.response.send_message("현재 수감 중이 아닙니다. 탈옥할 필요가 없어요!", ephemeral=True)
        return

    now = time.time()
    cd = escape_cooldowns.get(user.id, 0)
    if now < cd:
        await interaction.response.send_message(
            f"경비가 삼엄합니다. **{int(cd - now) + 1}초** 뒤에 다시 도전할 수 있습니다.", ephemeral=True)
        return
    if ESCAPE_COOLDOWN_SEC:
        escape_cooldowns[user.id] = now + ESCAPE_COOLDOWN_SEC   # 도중에 그만둬도 쿨타임을 피할 수 없게 시작 시점에 적용

    game = EscapeGameView(user)
    await interaction.response.send_message(embed=game.build_embed(), view=game)
    game.message = await interaction.original_response()


# ==========================================================================
# [제작 시스템]   /제작
#
#   • 재료는 전투 승리 시 인벤토리에 "[재료] ..." 이름으로 개수가 쌓입니다.
#       - /전투(NPC)·/네임드·/보스 : NPCBattleView.end_battle 에서 지급  (MATERIAL_DROPS 의 NPC/보스 이름 기준)
#       - /레이드 : 레이드 성공 시 공격에 참여한 사람 모두에게 지급 (_raid_settle_sync)
#   • 제작한 장비는 "[제작] 이름" 으로 인벤토리에 1개 저장되고, compute_combat_stats 에서 전투 스탯에 반영됩니다.
#       - 무기: 보유한 무기(상점 무기 포함) 중 가장 강한 것 하나만 적용
#       - 방어구: 제작한 방어구 중 가장 강한 것 하나만 적용
#   • 조직원과 경찰 모두 사용할 수 있습니다. (레이드는 기존대로 조직원만 참가 가능)
#   • 드랍률·레시피·스탯은 아래 표에서 바로 조절할 수 있습니다. (제작비를 없애려면 money 를 0 으로)
# ==========================================================================
import threading as _threading

MATERIAL_PREFIX = "[재료] "
CRAFT_PREFIX = "[제작] "

M_SCRAP   = MATERIAL_PREFIX + "고철 조각"
M_STEEL   = MATERIAL_PREFIX + "단련 강철"
M_ALLOY   = MATERIAL_PREFIX + "특수 합금"
M_MARCO   = MATERIAL_PREFIX + "마르코의 정육 칼날"
M_BIANCA  = MATERIAL_PREFIX + "비앙카의 맹독 결정"
M_BORONIN = MATERIAL_PREFIX + "보로닌의 장갑판"
M_EXEC    = MATERIAL_PREFIX + "처형자의 붉은 파편"
M_DARK    = MATERIAL_PREFIX + "어둠의 핵"
M_ORIGIN  = MATERIAL_PREFIX + "태초의 파편"
M_EDEN    = MATERIAL_PREFIX + "에덴의 붉은 깃발"
M_RAT     = MATERIAL_PREFIX + "쥐왕의 송곳니"
M_BULL    = MATERIAL_PREFIX + "폭군의 닻줄"
M_KING    = MATERIAL_PREFIX + "황제의 왕관 파편"

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
    # /레이드 (RAID_TIERS 의 name)
    "하수구의 쥐왕":      [(M_RAT, 1.00, (1, 3)), (M_SCRAP, 1.00, (3, 5))],
    "부두 항만의 폭군":   [(M_BULL, 1.00, (1, 3)), (M_STEEL, 1.00, (2, 4))],
    "칼렌데 지하 황제":   [(M_KING, 1.00, (1, 3)), (M_ALLOY, 1.00, (2, 3))],
}


def _recipe(rid, name, slot, money, mats, atk=0, hp=0, df=0, desc=""):
    return {"id": rid, "name": name, "slot": slot, "money": money, "mats": mats,
            "atk": atk, "hp": hp, "def": df, "desc": desc}

# 제작 레시피 (slot: weapon / armor)
CRAFT_RECIPES = [
    # ── 무기
    _recipe("w_pipe",   "보강 쇠파이프",        "weapon",    500, {M_SCRAP: 8},                  atk=250,            desc="고철을 덧대 만든 묵직한 쇠파이프."),
    _recipe("w_rat",    "쥐왕의 송곳니 단검",   "weapon",   1000, {M_RAT: 3, M_SCRAP: 8},        atk=350, hp=200,    desc="쥐왕의 송곳니를 갈아 만든 단검."),
    _recipe("w_steel",  "단조 강철검",          "weapon",   1500, {M_STEEL: 6, M_SCRAP: 6},      atk=400, hp=300,    desc="단련 강철을 두들겨 만든 검."),
    _recipe("w_marco",  "마르코의 도살 칼",     "weapon",   3500, {M_MARCO: 3, M_STEEL: 6},      atk=700,            desc="백정 마르코의 칼날을 이어 붙인 정육 칼."),
    _recipe("w_alloy",  "합금 돌격소총",        "weapon",   4000, {M_ALLOY: 5, M_STEEL: 8},      atk=600, hp=600,    desc="특수 합금 부품으로 조립한 소총."),
    _recipe("w_bianca", "비앙카의 독니 단검",   "weapon",   6000, {M_BIANCA: 3, M_ALLOY: 5},     atk=850, hp=500,    desc="맹독 결정을 머금은 암살용 단검."),
    _recipe("w_king",   "황제의 홀",            "weapon",   7000, {M_KING: 3, M_ALLOY: 6},       atk=900, hp=1500,   desc="지하 황제의 왕관 파편으로 장식한 홀."),
    _recipe("w_exec",   "처형자의 붉은 낫",     "weapon",  10000, {M_EXEC: 3, M_ALLOY: 8},       atk=1100, hp=1000,  desc="붉은 달 아래 처형에 쓰이던 낫."),
    _recipe("w_dark",   "어둠의 대검",          "weapon",  15000, {M_DARK: 2, M_ALLOY: 8},       atk=1300, hp=2000,  desc="어둠의 핵이 박힌 거대한 대검."),
    _recipe("w_eden",   "혁명의 붉은 총검",     "weapon",  22000, {M_EDEN: 2, M_ALLOY: 9},       atk=1600, hp=2800,  desc="에덴의 붉은 깃발을 감아 만든 총검."),
    _recipe("w_origin", "태초의 파괴검",        "weapon",  30000, {M_ORIGIN: 2, M_ALLOY: 10},    atk=2000, hp=3500,  desc="태초의 파편이 깃든 파괴의 검."),
    # ── 방어구
    _recipe("a_vest",    "보강 방탄 조끼",       "armor",    600, {M_SCRAP: 10},                 df=25,  hp=400,     desc="고철 판으로 보강한 조끼."),
    _recipe("a_plate",   "강철 판금 갑옷",       "armor",   1800, {M_STEEL: 6, M_SCRAP: 6},      df=60,  hp=900,     desc="단련 강철 판금으로 만든 갑옷."),
    _recipe("a_bull",    "폭군의 닻줄 갑옷",     "armor",   2500, {M_BULL: 3, M_STEEL: 6},       df=80,  hp=1200,    desc="폭군의 닻줄을 엮어 만든 갑옷."),
    _recipe("a_alloy",   "합금 전술 갑주",       "armor",   4500, {M_ALLOY: 6, M_STEEL: 8},      df=110, hp=1600,    desc="특수 합금으로 제작한 전술 갑주."),
    _recipe("a_boronin", "보로닌의 장군 갑주",   "armor",   7500, {M_BORONIN: 3, M_ALLOY: 6},    df=200, hp=2500,    desc="철의 장군의 장갑판을 이은 갑주."),
    _recipe("a_king",    "황제의 왕관 갑주",     "armor",  10000, {M_KING: 3, M_ALLOY: 8},       df=240, hp=3000,    desc="황제의 왕관 파편이 박힌 갑주."),
    _recipe("a_dark",    "어둠의 갑주",          "armor",  15000, {M_DARK: 2, M_ALLOY: 10},      df=330, hp=5000,    desc="어둠의 핵이 맥동하는 갑주."),
    _recipe("a_eden",    "붉은 깃발 방탄 코트",  "armor",  22000, {M_EDEN: 2, M_ALLOY: 9},       df=410, hp=6500,    desc="에덴의 붉은 깃발로 안감을 댄 방탄 코트."),
    _recipe("a_origin",  "태초의 방벽",          "armor",  30000, {M_ORIGIN: 2, M_ALLOY: 10},    df=500, hp=8000,    desc="태초의 파편으로 빚어낸 절대 방벽."),
]
_CRAFT_ALL_GOAL = len(CRAFT_RECIPES)
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
        "police": [(it["name"], it["def_bonus"]) for it in POLICE_SHOP_ITEMS if it.get("def_bonus") and it["name"] in inventory],
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
    for pname, pdef in b["police"]:
        lines.append(f"• **치안국 보급품** {pname}: 방어력 +{pdef}")
    if POLICE_RADIO_NAME in inv:
        lines.append(f"• **{POLICE_RADIO_NAME}**: /검거 포상금 +{round(POLICE_RADIO_ARREST_BONUS * 100)}%")
    if POLICE_BADGE_NAME in inv:
        lines.append(f"• **{POLICE_BADGE_NAME}**: 검거·순찰·심문 명예 실적 +{round(POLICE_BADGE_HONOR_BONUS * 100)}%")
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
    if item.startswith("[전리품]"):
        return "네임드 고유 전리품은 칭호·업적과 연결되어 있어 거래할 수 없습니다."
    if item in {it["name"] for it in POLICE_SHOP_ITEMS}:
        return "치안국 보급품은 거래할 수 없습니다."
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


# ==========================================================================
# [/도움말]  전체 명령어 안내
#   • 봇에 등록된 슬래시 명령어(bot.tree)를 실시간으로 읽어서 보여줍니다.
#     → 새 명령어를 추가해도 따로 고칠 필요 없이 자동으로 목록에 들어갑니다. (설명은 각 명령어의 description)
#   • 아래 HELP_CATEGORIES 에 이름을 적지 않은 명령어는 '기타' 분류에 모입니다.
# ==========================================================================
HELP_CATEGORIES = [
    # (키, 표시 이름, 분류 설명, [명령어 이름 — /조직 처럼 그룹 명령어는 첫 단어만])
    ("start",   "시작 · 정보",          "가입과 내 정보 확인, 설정, 랭킹 등 기본이 되는 명령어입니다.",
     ["도움말", "가입", "탈퇴", "프로필", "정보", "설정", "일퀘", "칭호", "업적", "랭킹"]),
    ("economy", "이동 · 상점 · 경제",   "거점 이동, 장비 구매, 자금 이체, 제작 등 돈과 장비에 관한 명령어입니다.",
     ["이동", "암시장", "무기상점", "인벤토리", "송금", "아르바이트", "제작", "경매"]),
    ("mafia",   "마피아 활동",          "비밀 임무 수행, 경찰 매수, 탈옥 등 조직원의 범죄 활동입니다.",
     ["임무", "밀수", "탈옥"]),
    ("battle",  "전투 · 능력치",        "NPC·네임드·보스 토벌과 결투, 전투 능력치 확인입니다.",
     ["능력치", "전투", "네임드", "보스", "결투"]),
    ("casino",  "카지노",               "자금을 걸고 즐기는 도박입니다. (`/도박 규칙` 에서 배당표 확인)",
     ["도박"]),
    ("police",  "경찰",                 "경찰 소속 전용 명령어입니다.",
     ["검거", "순찰", "심문", "월급", "경찰상점", "수사팀"]),
    ("family",  "조직 · 레이드 · 구역", "조직(패밀리)을 만들어 함께하는 협동·점령 콘텐츠입니다.",
     ["조직", "레이드", "구역"]),
    ("admin",   "관리자",               "서버 관리자 전용 명령어입니다.",
     ["추방"]),
]


def _help_usage(cmd) -> str:
    """'/송금 <member> <amount>' 처럼 사용법 문자열 생성 (<필수> / [선택])"""
    parts = [f"/{cmd.qualified_name}"]
    for prm in cmd.parameters:
        name = getattr(prm, "display_name", None) or prm.name
        parts.append(f"<{name}>" if prm.required else f"[{name}]")
    return " ".join(parts)


def _help_pages() -> list:
    """현재 등록된 명령어를 분류별로 정리 → [{key, label, desc, cmds}]"""
    by_root = {}
    for cmd in bot.tree.walk_commands():
        if isinstance(cmd, app_commands.Group):
            continue   # 그룹 자체가 아니라 그 안의 하위 명령어를 나열
        by_root.setdefault(cmd.qualified_name.split(" ")[0], []).append(cmd)

    pages, used = [], set()
    for key, label, desc, roots in HELP_CATEGORIES:
        cmds = []
        for root in roots:
            if root in by_root:
                cmds.extend(by_root[root])
                used.add(root)
        if cmds:
            pages.append({"key": key, "label": label, "desc": desc, "cmds": cmds})

    leftover = [c for root, cs in by_root.items() if root not in used for c in cs]
    if leftover:
        pages.append({"key": "etc", "label": "기타", "desc": "분류되지 않은 명령어입니다.", "cmds": leftover})
    return pages


def help_overview_embed(pages: list) -> discord.Embed:
    total = sum(len(pg["cmds"]) for pg in pages)
    lines = [f"**{pg['label']}** ({len(pg['cmds'])}개)\n└ {pg['desc']}" for pg in pages]
    embed = discord.Embed(
        title="[패밀리 GM 도움말]",
        description=(f"총 **{total}개**의 명령어가 있습니다. 아래 메뉴에서 분류를 고르면 사용법과 설명을 볼 수 있습니다.\n"
                     f"사용법 표기: `<필수>` `[선택]`  ·  설명 앞의 `[ ]` 는 사용할 수 있는 대상입니다.\n\n"
                     + "\n\n".join(lines)),
        color=discord.Color.blurple()
    )
    embed.add_field(
        name="처음이라면",
        value=("`/가입` 으로 조직원 또는 경찰이 되세요.\n"
               "조직원은 `/임무` · `/전투` 로 자금과 명예를 모으고, `/무기상점` · `/암시장` · `/제작` 으로 장비를 갖춥니다.\n"
               f"명예 {FAMILY_CREATE_MIN_HONOR}pt 이상이면 `/조직 생성` 으로 조직을 만들어 `/레이드` 와 `/구역` 에 도전할 수 있습니다.\n"
               f"경찰은 `/순찰` · `/검거` 로 활동하고, 명예 {POLICE_UNIT_CREATE_MIN_HONOR}pt 이상이면 `/수사팀 생성` 으로 수사팀을 만들어 `/수사팀 작전` 에 도전할 수 있습니다."),
        inline=False
    )
    embed.set_footer(text="수감 중에는 일부 명령어를 쓸 수 없습니다. (수감자는 /탈옥 으로 탈출을 시도할 수 있어요)")
    return embed


def help_category_embed(page: dict) -> discord.Embed:
    blocks = [f"**`{_help_usage(c)}`**\n└ {c.description}" for c in page["cmds"]]
    text = f"{page['desc']}\n\n" + "\n\n".join(blocks)
    if len(text) > 4000:
        text = text[:3990] + "…"
    embed = discord.Embed(title=f"[도움말] {page['label']}", description=text, color=discord.Color.blurple())
    embed.set_footer(text=f"명령어 {len(page['cmds'])}개 · 사용법 표기: <필수> [선택]")
    return embed


class HelpSelect(discord.ui.Select):
    def __init__(self, pages: list):
        self.pages = pages
        options = [discord.SelectOption(label="전체 보기", value="__all__", description="분류 목록과 처음 시작하는 방법", default=True)]
        for pg in pages:
            options.append(discord.SelectOption(label=pg["label"], value=pg["key"], description=f"명령어 {len(pg['cmds'])}개"))
        super().__init__(placeholder="분류를 선택하세요...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        key = self.values[0]
        for opt in self.options:
            opt.default = (opt.value == key)
        if key == "__all__":
            embed = help_overview_embed(self.pages)
        else:
            embed = help_category_embed(next(pg for pg in self.pages if pg["key"] == key))
        await interaction.response.edit_message(embed=embed, view=self.view)


class HelpView(discord.ui.View):
    def __init__(self, pages: list):
        super().__init__(timeout=180)
        self.interaction = None
        self.add_item(HelpSelect(pages))

    async def on_timeout(self):
        if self.interaction is None:
            return
        try:
            await self.interaction.edit_original_response(view=None)   # 시간이 지나면 죽은 메뉴를 치운다
        except discord.HTTPException:
            pass


@bot.tree.command(name="도움말", description="[공용] 모든 명령어의 사용법과 설명을 분류별로 확인합니다.")
async def help_command(interaction: discord.Interaction):
    if not await check_maintenance(interaction): return

    pages = _help_pages()
    view = HelpView(pages)
    view.interaction = interaction
    await interaction.response.send_message(embed=help_overview_embed(pages), view=view, ephemeral=True)


# ==========================================================================
# [경찰 수사팀 시스템]  /수사팀 ...
#   • 마피아의 '조직(패밀리)' 과 같은 구조의 경찰 전용 조직입니다. (경찰 역할만 만들고 가입할 수 있어요)
#   • 데이터는 Supabase 의 `police_units` 테이블에 저장됩니다. (police_units.sql 을 먼저 실행하세요)
#   • 수사팀의 협동 콘텐츠는 '합동 소탕 작전' (/수사팀 작전) 입니다. 진행 상태는 메모리에만 두며,
#     봇이 재시작되면 진행 중이던 작전은 사라집니다. (작전비는 모집 단계에서 이미 금고에서 빠진 상태)
# ==========================================================================
# --- 수사팀 설정 ---
POLICE_UNIT_CREATE_COST = 10000        # 수사팀 창설 비용 ($)
POLICE_UNIT_CREATE_MIN_HONOR = 500     # 수사팀 창설에 필요한 최소 명예
POLICE_UNIT_MAX_MEMBERS = 10           # 수사팀 최대 인원
POLICE_UNIT_MIN_DONATE = 100           # 금고 최소 기부액
POLICE_UNIT_DB_ERROR_MSG = "수사팀 DB 처리 중 오류가 발생했습니다. (관리자: `police_units` 테이블이 만들어져 있는지 확인해 주세요 — police_units.sql 참고)"

# --- 합동 소탕 작전 설정 ---
POLICE_OP_LOBBY_SEC = 60               # 참가 모집 시간 (팀장이 '출동' 을 누르면 바로 시작)
POLICE_OP_MAX_PLAYERS = 8
POLICE_OP_COOLDOWN_SEC = 1800          # 같은 수사팀의 작전 재시작 대기시간 (30분)
POLICE_OP_STAGE_SEC = 3                # 단계 사이 연출 시간
POLICE_OP_TREASURY_RETURN = 0.15       # 성공 시 지급된 보상금의 15%가 수사팀 금고로 들어감
POLICE_OP_FAIL_MONEY_RATIO = 0.10      # 작전이 완전히 실패해도 참가자에게 주는 위로금 비율
POLICE_OP_STAGES = ["잠입·정보 수집", "현장 급습", "검거·압송"]
POLICE_OP_REWARD_FRAC = {3: 1.0, 2: 0.7, 1: 0.4, 0: 0.0}   # 성공한 단계 수 → 보상 비율

# base: 단계 1개의 기본 성공률 / ref_atk: 참가자 평균 공격력이 이 값 이상이면 장비 보너스(+12%p) 만점
# 단계 성공률 = base + 인원 보너스(최소 인원 초과 1명당 +4%p) + 장비 보너스,  30%~92% 로 제한
POLICE_OP_TIERS = {
    "smuggle": {
        "name": "밀수 조직 소탕", "min_players": 2, "min_honor": 0, "cost": 1000,
        "base": 0.65, "ref_atk": 300,
        "money": 3000, "honor": 22, "points": 10,
        "desc": "뒷골목 밀수 조직의 창고를 급습합니다. 입문자용 작전입니다.",
    },
    "cartel": {
        "name": "항만 카르텔 일망타진", "min_players": 3, "min_honor": 1500, "cost": 3000,
        "base": 0.55, "ref_atk": 1200,
        "money": 7000, "honor": 45, "points": 25,
        "desc": "에르느코항을 장악한 카르텔을 한 번에 소탕합니다. 화력과 인원이 필요합니다.",
    },
    "empire": {
        "name": "지하 범죄 제국 본거지 급습", "min_players": 4, "min_honor": 4000, "cost": 8000,
        "base": 0.50, "ref_atk": 3500,
        "money": 14000, "honor": 75, "points": 60,
        "desc": "칼렌데 지하 범죄 제국의 심장부. 정상급 수사팀만이 도전할 수 있습니다.",
    },
}

police_ops = {}                # {unit_id: PoliceOpSession}
police_op_user_map = {}        # {user_id: unit_id}  (작전에 참가 중인 경찰)
police_op_cooldowns = {}       # {unit_id: datetime}


# ---------------------------------------------------------------- 수사팀 DB 도우미 (동기)
def police_unit_get_by_user(guild_id: int, user_id: int):
    res = (supabase.table("police_units").select("*")
           .eq("guild_id", str(guild_id)).contains("member_ids", [str(user_id)]).limit(1).execute())
    return res.data[0] if res.data else None

def police_unit_get_by_id(unit_id):
    res = supabase.table("police_units").select("*").eq("id", unit_id).limit(1).execute()
    return res.data[0] if res.data else None

def police_unit_get_by_name(guild_id: int, name: str):
    res = (supabase.table("police_units").select("*")
           .eq("guild_id", str(guild_id)).eq("name", name).limit(1).execute())
    return res.data[0] if res.data else None

def police_unit_list_top(guild_id: int, limit: int = 10):
    res = (supabase.table("police_units").select("*").eq("guild_id", str(guild_id))
           .order("op_points", desc=True).limit(limit).execute())
    return res.data or []

def police_unit_create(guild_id: int, name: str, owner_id: int):
    row = {
        "guild_id": str(guild_id), "name": name, "owner_id": str(owner_id),
        "member_ids": [str(owner_id)], "treasury": 0, "op_points": 0, "ops_cleared": 0,
    }
    res = supabase.table("police_units").insert(row).execute()
    return res.data[0] if res.data else row

def police_unit_update(unit_id, data: dict):
    res = supabase.table("police_units").update(data).eq("id", unit_id).execute()
    _check_updated(res, "police_unit_update")

def police_unit_delete(unit_id):
    supabase.table("police_units").delete().eq("id", unit_id).execute()

def police_unit_detach_user(user_id: int):
    """유저를 모든 수사팀에서 제거 (팀장이면 다음 팀원에게 위임, 마지막 1명이면 수사팀 해산). /탈퇴 초기화에서도 사용."""
    if not supabase:
        return
    uid = str(user_id)
    res = supabase.table("police_units").select("*").contains("member_ids", [uid]).execute()
    for unit in (res.data or []):
        members = [m for m in unit["member_ids"] if m != uid]
        if not members:
            police_unit_delete(unit["id"])
            continue
        data = {"member_ids": members}
        if unit["owner_id"] == uid:
            data["owner_id"] = members[0]
        police_unit_update(unit["id"], data)

def police_unit_add_treasury(unit_id, amount: int):
    unit = police_unit_get_by_id(unit_id)
    if unit and amount:
        police_unit_update(unit_id, {"treasury": unit.get("treasury", 0) + amount})

def police_unit_record_op(unit_id, treasury_add: int, points: int, cleared: bool):
    unit = police_unit_get_by_id(unit_id)
    if not unit:
        return
    police_unit_update(unit_id, {
        "treasury": unit.get("treasury", 0) + treasury_add,
        "op_points": unit.get("op_points", 0) + points,
        "ops_cleared": unit.get("ops_cleared", 0) + (1 if cleared else 0),
    })

def _is_police(member: discord.Member) -> bool:
    role = discord.utils.get(member.guild.roles, name="경찰")
    return bool(role and role in member.roles)


# ================================================================ /수사팀
unit_group = app_commands.Group(name="수사팀", description="[경찰] 경찰 전용 조직(수사팀)을 만들고 함께 활동합니다.")
bot.tree.add_command(unit_group)

async def _unit_precheck(interaction: discord.Interaction) -> bool:
    if not await check_maintenance(interaction): return False
    if not await check_membership(interaction): return False
    if await check_jail(interaction): return False
    if not _is_police(interaction.user):
        await interaction.response.send_message("수사팀 기능은 치안국 경찰만 사용할 수 있습니다. (마피아 조직원은 `/조직` 을 이용하세요)", ephemeral=True)
        return False
    return True

@unit_group.command(name="생성", description=f"수사팀을 창설합니다. (비용 ${POLICE_UNIT_CREATE_COST:,} / 명예 {POLICE_UNIT_CREATE_MIN_HONOR}pt 이상)")
@app_commands.describe(이름="수사팀 이름 (한글·영문·숫자·띄어쓰기 2~12자)")
async def unit_create_cmd(interaction: discord.Interaction, 이름: str):
    if not await _unit_precheck(interaction): return
    user = interaction.user
    name = 이름.strip()
    if not FAMILY_NAME_PATTERN.match(name):
        await interaction.response.send_message("수사팀 이름은 한글·영문·숫자·띄어쓰기로 **2~12자**여야 합니다.", ephemeral=True)
        return
    paid = False
    try:
        if await asyncio.to_thread(police_unit_get_by_user, interaction.guild_id, user.id):
            await interaction.response.send_message("이미 수사팀에 소속되어 있습니다. 먼저 `/수사팀 탈퇴` 하세요.", ephemeral=True)
            return
        if await asyncio.to_thread(police_unit_get_by_name, interaction.guild_id, name):
            await interaction.response.send_message("이미 같은 이름의 수사팀이 있습니다.", ephemeral=True)
            return
        info = await asyncio.to_thread(get_user_data, user.id)
        if info["honor"] < POLICE_UNIT_CREATE_MIN_HONOR:
            await interaction.response.send_message(f"수사팀을 만들려면 명예 **{POLICE_UNIT_CREATE_MIN_HONOR}pt** 이상이어야 합니다. (현재 {info['honor']:,}pt)", ephemeral=True)
            return
        if info["money"] < POLICE_UNIT_CREATE_COST:
            await interaction.response.send_message(f"창설 비용 **${POLICE_UNIT_CREATE_COST:,}** 이(가) 부족합니다. (보유 ${info['money']:,})", ephemeral=True)
            return
        await asyncio.to_thread(update_user_stats, user.id, -POLICE_UNIT_CREATE_COST, 0)
        paid = True
        await asyncio.to_thread(police_unit_create, interaction.guild_id, name, user.id)
    except Exception as e:
        print(f"Police unit create error: {e}")
        if paid:   # 돈만 빠지고 수사팀이 안 만들어진 경우 되돌려 준다
            try:
                await asyncio.to_thread(update_user_stats, user.id, POLICE_UNIT_CREATE_COST, 0)
            except Exception as e2:
                print(f"Police unit create refund error: {e2}")
        await interaction.response.send_message(POLICE_UNIT_DB_ERROR_MSG, ephemeral=True)
        return

    embed = discord.Embed(
        title="[수사팀 창설]",
        description=(f"**{user.mention}** 님이 새로운 수사팀 **「{name}」** 을(를) 창설했습니다!\n\n"
                     f"**창설 비용:** `${POLICE_UNIT_CREATE_COST:,}`\n"
                     f"`/수사팀 초대` 로 동료 경찰을 모아 `/수사팀 작전` 에 도전해 보세요."),
        color=discord.Color.blue()
    )
    await interaction.response.send_message(embed=embed)

def _unit_embed(unit: dict, guild: discord.Guild) -> discord.Embed:
    members = unit.get("member_ids") or []
    lines = []
    for m in members:
        crown = "[팀장] " if m == unit["owner_id"] else "• "
        lines.append(f"{crown}<@{m}>")
    embed = discord.Embed(title=f"수사팀 「{unit['name']}」", color=discord.Color.blue())
    embed.add_field(name=f"팀원 ({len(members)}/{POLICE_UNIT_MAX_MEMBERS})", value="\n".join(lines) or "-", inline=False)
    embed.add_field(name="금고", value=f"`${unit.get('treasury', 0):,}`", inline=True)
    embed.add_field(name="작전 점수", value=f"`{unit.get('op_points', 0):,} pt`", inline=True)
    embed.add_field(name="소탕 성공", value=f"`{unit.get('ops_cleared', 0):,}회`", inline=True)
    op = police_ops.get(unit["id"])
    if op:
        embed.add_field(name="진행 중인 작전", value=f"{op.tier['name']} ({'모집 중' if op.state == 'lobby' else '작전 수행 중'})", inline=False)
    cd = police_op_cooldowns.get(unit["id"])
    if cd and datetime.now() < cd:
        embed.add_field(name="다음 작전", value=f"{int((cd - datetime.now()).total_seconds() // 60) + 1}분 후", inline=True)
    return embed

@unit_group.command(name="정보", description="수사팀 정보를 확인합니다. (이름을 비우면 내 수사팀)")
@app_commands.describe(이름="확인할 수사팀 이름 (비우면 내 수사팀)")
async def unit_info_cmd(interaction: discord.Interaction, 이름: str = None):
    if not await check_maintenance(interaction): return
    try:
        if 이름:
            unit = await asyncio.to_thread(police_unit_get_by_name, interaction.guild_id, 이름.strip())
        else:
            unit = await asyncio.to_thread(police_unit_get_by_user, interaction.guild_id, interaction.user.id)
    except Exception as e:
        print(f"Police unit info error: {e}")
        await interaction.response.send_message(POLICE_UNIT_DB_ERROR_MSG, ephemeral=True)
        return
    if not unit:
        msg = "해당 이름의 수사팀이 없습니다." if 이름 else "소속된 수사팀이 없습니다. `/수사팀 생성` 또는 초대를 받아 보세요."
        await interaction.response.send_message(msg, ephemeral=True)
        return
    await interaction.response.send_message(embed=_unit_embed(unit, interaction.guild))

@unit_group.command(name="목록", description="작전 점수 기준 수사팀 랭킹 TOP 10")
async def unit_rank_cmd(interaction: discord.Interaction):
    if not await check_maintenance(interaction): return
    try:
        rows = await asyncio.to_thread(police_unit_list_top, interaction.guild_id, 10)
    except Exception as e:
        print(f"Police unit list error: {e}")
        await interaction.response.send_message(POLICE_UNIT_DB_ERROR_MSG, ephemeral=True)
        return
    if not rows:
        await interaction.response.send_message("아직 창설된 수사팀이 없습니다.", ephemeral=True)
        return
    lines = [f"`{i + 1}위` **{r['name']}** — {r.get('op_points', 0):,} pt · {len(r.get('member_ids') or [])}명" for i, r in enumerate(rows)]
    embed = discord.Embed(title="수사팀 랭킹 (작전 점수)", description="\n".join(lines), color=discord.Color.blue())
    await interaction.response.send_message(embed=embed)

class PoliceUnitInviteView(discord.ui.View):
    def __init__(self, unit_id, unit_name: str, inviter: discord.Member, target: discord.Member):
        super().__init__(timeout=60)
        self.unit_id = unit_id
        self.unit_name = unit_name
        self.inviter = inviter
        self.target = target
        self.resolved = False
        self.message = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.target.id:
            await interaction.response.send_message("초대받은 본인만 응답할 수 있습니다.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        if self.resolved or self.message is None:
            return
        try:
            await self.message.edit(content="초대가 만료되었습니다.", embed=None, view=None)
        except discord.HTTPException:
            pass

    @discord.ui.button(label="수락", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.resolved: return
        self.resolved = True
        self.stop()
        try:
            if await asyncio.to_thread(police_unit_get_by_user, interaction.guild_id, self.target.id):
                await interaction.response.edit_message(content="이미 다른 수사팀에 소속되어 있어 가입할 수 없습니다.", embed=None, view=None)
                return
            unit = await asyncio.to_thread(police_unit_get_by_id, self.unit_id)
            if not unit:
                await interaction.response.edit_message(content="수사팀이 더 이상 존재하지 않습니다.", embed=None, view=None)
                return
            members = list(unit.get("member_ids") or [])
            if len(members) >= POLICE_UNIT_MAX_MEMBERS:
                await interaction.response.edit_message(content="수사팀 정원이 가득 찼습니다.", embed=None, view=None)
                return
            members.append(str(self.target.id))
            await asyncio.to_thread(police_unit_update, self.unit_id, {"member_ids": members})
        except Exception as e:
            print(f"Police unit invite accept error: {e}")
            await interaction.response.edit_message(content=POLICE_UNIT_DB_ERROR_MSG, embed=None, view=None)
            return
        embed = discord.Embed(title="[수사팀 합류]",
                              description=f"**{self.target.mention}** 님이 **「{self.unit_name}」** 에 합류했습니다! ({len(members)}/{POLICE_UNIT_MAX_MEMBERS})",
                              color=discord.Color.green())
        await interaction.response.edit_message(content=None, embed=embed, view=None)

    @discord.ui.button(label="거절", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.resolved: return
        self.resolved = True
        self.stop()
        await interaction.response.edit_message(content=f"**{self.target.display_name}** 님이 초대를 거절했습니다.", embed=None, view=None)

@unit_group.command(name="초대", description="경찰 동료를 우리 수사팀에 초대합니다. (팀장 전용)")
@app_commands.describe(member="초대할 경찰")
async def unit_invite_cmd(interaction: discord.Interaction, member: discord.Member):
    if not await _unit_precheck(interaction): return
    user = interaction.user
    if member.bot or member.id == user.id:
        await interaction.response.send_message("올바른 초대 대상이 아닙니다.", ephemeral=True)
        return
    if not _is_police(member):
        await interaction.response.send_message("치안국 경찰만 초대할 수 있습니다.", ephemeral=True)
        return
    try:
        unit = await asyncio.to_thread(police_unit_get_by_user, interaction.guild_id, user.id)
        if not unit:
            await interaction.response.send_message("소속된 수사팀이 없습니다.", ephemeral=True)
            return
        if unit["owner_id"] != str(user.id):
            await interaction.response.send_message("팀장만 초대할 수 있습니다.", ephemeral=True)
            return
        if len(unit.get("member_ids") or []) >= POLICE_UNIT_MAX_MEMBERS:
            await interaction.response.send_message("수사팀 정원이 가득 찼습니다.", ephemeral=True)
            return
        if await asyncio.to_thread(police_unit_get_by_user, interaction.guild_id, member.id):
            await interaction.response.send_message("상대는 이미 다른 수사팀에 소속되어 있습니다.", ephemeral=True)
            return
    except Exception as e:
        print(f"Police unit invite error: {e}")
        await interaction.response.send_message(POLICE_UNIT_DB_ERROR_MSG, ephemeral=True)
        return

    view = PoliceUnitInviteView(unit["id"], unit["name"], user, member)
    embed = discord.Embed(title="[수사팀 초대장]",
                          description=f"**{user.mention}** 님이 **{member.mention}** 님을 수사팀 **「{unit['name']}」** 에 초대했습니다.\n60초 안에 응답해 주세요.",
                          color=discord.Color.blue())
    await interaction.response.send_message(content=member.mention, embed=embed, view=view)
    view.message = await interaction.original_response()

@unit_group.command(name="탈퇴", description="현재 수사팀에서 탈퇴합니다.")
async def unit_leave_cmd(interaction: discord.Interaction):
    if not await _unit_precheck(interaction): return
    user = interaction.user
    if user.id in police_op_user_map:
        await interaction.response.send_message("작전 진행 중에는 탈퇴할 수 없습니다.", ephemeral=True)
        return
    try:
        unit = await asyncio.to_thread(police_unit_get_by_user, interaction.guild_id, user.id)
        if not unit:
            await interaction.response.send_message("소속된 수사팀이 없습니다.", ephemeral=True)
            return
        members = unit.get("member_ids") or []
        if unit["owner_id"] == str(user.id) and len(members) > 1:
            await interaction.response.send_message("팀장은 탈퇴할 수 없습니다. `/수사팀 위임` 으로 팀장을 넘기거나 `/수사팀 해산` 하세요.", ephemeral=True)
            return
        await asyncio.to_thread(police_unit_detach_user, user.id)
    except Exception as e:
        print(f"Police unit leave error: {e}")
        await interaction.response.send_message(POLICE_UNIT_DB_ERROR_MSG, ephemeral=True)
        return
    note = " (마지막 팀원이라 수사팀이 해산되었습니다)" if len(members) <= 1 else ""
    await interaction.response.send_message(f"**{user.mention}** 님이 **「{unit['name']}」** 에서 탈퇴했습니다.{note}")

@unit_group.command(name="추방", description="팀원을 추방합니다. (팀장 전용)")
@app_commands.describe(member="추방할 팀원")
async def unit_kick_cmd(interaction: discord.Interaction, member: discord.Member):
    if not await _unit_precheck(interaction): return
    user = interaction.user
    try:
        unit = await asyncio.to_thread(police_unit_get_by_user, interaction.guild_id, user.id)
        if not unit or unit["owner_id"] != str(user.id):
            await interaction.response.send_message("팀장만 사용할 수 있습니다.", ephemeral=True)
            return
        if member.id == user.id or str(member.id) not in (unit.get("member_ids") or []):
            await interaction.response.send_message("추방할 수 없는 대상입니다.", ephemeral=True)
            return
        if member.id in police_op_user_map:
            await interaction.response.send_message("작전 참가 중인 팀원은 추방할 수 없습니다.", ephemeral=True)
            return
        await asyncio.to_thread(police_unit_update, unit["id"], {"member_ids": [m for m in unit["member_ids"] if m != str(member.id)]})
    except Exception as e:
        print(f"Police unit kick error: {e}")
        await interaction.response.send_message(POLICE_UNIT_DB_ERROR_MSG, ephemeral=True)
        return
    await interaction.response.send_message(f"**{member.mention}** 님이 수사팀 **「{unit['name']}」** 에서 추방되었습니다.")

@unit_group.command(name="위임", description="팀장 자리를 다른 팀원에게 넘깁니다. (팀장 전용)")
@app_commands.describe(member="새 팀장")
async def unit_transfer_cmd(interaction: discord.Interaction, member: discord.Member):
    if not await _unit_precheck(interaction): return
    user = interaction.user
    try:
        unit = await asyncio.to_thread(police_unit_get_by_user, interaction.guild_id, user.id)
        if not unit or unit["owner_id"] != str(user.id):
            await interaction.response.send_message("팀장만 사용할 수 있습니다.", ephemeral=True)
            return
        if member.id == user.id or str(member.id) not in (unit.get("member_ids") or []):
            await interaction.response.send_message("같은 수사팀의 다른 팀원에게만 위임할 수 있습니다.", ephemeral=True)
            return
        await asyncio.to_thread(police_unit_update, unit["id"], {"owner_id": str(member.id)})
    except Exception as e:
        print(f"Police unit transfer error: {e}")
        await interaction.response.send_message(POLICE_UNIT_DB_ERROR_MSG, ephemeral=True)
        return
    await interaction.response.send_message(f"수사팀 **「{unit['name']}」** 의 새 팀장은 **{member.mention}** 님입니다!")

@unit_group.command(name="기부", description="수사팀 금고에 자금을 기부합니다. (작전 자금으로 쓰입니다)")
@app_commands.describe(금액=f"기부할 금액 (${POLICE_UNIT_MIN_DONATE:,} 이상)")
async def unit_donate_cmd(interaction: discord.Interaction, 금액: app_commands.Range[int, POLICE_UNIT_MIN_DONATE, 10_000_000]):
    if not await _unit_precheck(interaction): return
    user = interaction.user
    try:
        unit = await asyncio.to_thread(police_unit_get_by_user, interaction.guild_id, user.id)
        if not unit:
            await interaction.response.send_message("소속된 수사팀이 없습니다.", ephemeral=True)
            return
        info = await asyncio.to_thread(get_user_data, user.id)
        if info["money"] < 금액:
            await interaction.response.send_message("보유 자금이 부족합니다.", ephemeral=True)
            return
        await asyncio.to_thread(update_user_stats, user.id, -금액, 0)
        unit = await asyncio.to_thread(police_unit_get_by_id, unit["id"])   # 최신 금고 값으로 갱신
        new_treasury = unit.get("treasury", 0) + 금액
        await asyncio.to_thread(police_unit_update, unit["id"], {"treasury": new_treasury})
    except Exception as e:
        print(f"Police unit donate error: {e}")
        await interaction.response.send_message(POLICE_UNIT_DB_ERROR_MSG, ephemeral=True)
        return
    await interaction.response.send_message(f"**{user.mention}** 님이 수사팀 금고에 `${금액:,}` 을(를) 기부했습니다! (금고 `${new_treasury:,}`)")

class PoliceUnitDisbandView(discord.ui.View):
    def __init__(self, owner: discord.Member, unit_id, unit_name: str):
        super().__init__(timeout=30)
        self.owner = owner
        self.unit_id = unit_id
        self.unit_name = unit_name

    @discord.ui.button(label="정말 해산합니다", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner.id:
            await interaction.response.send_message("팀장만 누를 수 있습니다.", ephemeral=True)
            return
        self.stop()
        try:
            if self.unit_id in police_ops:
                await interaction.response.edit_message(content="작전 진행 중에는 해산할 수 없습니다.", view=None)
                return
            await asyncio.to_thread(police_unit_delete, self.unit_id)
        except Exception as e:
            print(f"Police unit disband error: {e}")
            await interaction.response.edit_message(content=POLICE_UNIT_DB_ERROR_MSG, view=None)
            return
        await interaction.response.edit_message(content=f"수사팀 **「{self.unit_name}」** 이(가) 해산되었습니다. (금고의 자금도 함께 사라집니다)", view=None)

@unit_group.command(name="해산", description="수사팀을 해산합니다. (팀장 전용, 금고 자금은 사라집니다)")
async def unit_disband_cmd(interaction: discord.Interaction):
    if not await _unit_precheck(interaction): return
    user = interaction.user
    try:
        unit = await asyncio.to_thread(police_unit_get_by_user, interaction.guild_id, user.id)
    except Exception as e:
        print(f"Police unit disband lookup error: {e}")
        await interaction.response.send_message(POLICE_UNIT_DB_ERROR_MSG, ephemeral=True)
        return
    if not unit or unit["owner_id"] != str(user.id):
        await interaction.response.send_message("팀장만 사용할 수 있습니다.", ephemeral=True)
        return
    await interaction.response.send_message(
        f"정말 **「{unit['name']}」** 을(를) 해산할까요? 금고 `${unit.get('treasury', 0):,}` 도 함께 사라집니다.",
        view=PoliceUnitDisbandView(user, unit["id"], unit["name"]), ephemeral=True)


# ================================================================ /수사팀 작전 (합동 소탕 작전)
class PoliceOpSession:
    def __init__(self, unit: dict, tier_key: str, leader: discord.Member, leader_stats: dict):
        self.unit_id = unit["id"]
        self.unit_name = unit["name"]
        self.member_ids = set(unit.get("member_ids") or [])
        self.tier_key = tier_key
        self.tier = POLICE_OP_TIERS[tier_key]
        self.leader = leader
        self.players = {leader.id: (leader, leader_stats)}   # {user_id: (member, combat_stats)}
        self.state = "lobby"                                 # lobby → running → done
        self.go = asyncio.Event()                            # 팀장이 '출동' 을 누르면 즉시 시작
        self.message = None

def police_op_release(session: PoliceOpSession):
    if police_ops.get(session.unit_id) is session:
        police_ops.pop(session.unit_id, None)
    for uid in list(session.players):
        if police_op_user_map.get(uid) == session.unit_id:
            police_op_user_map.pop(uid, None)

def police_op_lobby_embed(session: PoliceOpSession) -> discord.Embed:
    t = session.tier
    lines = [f"{'[팀장] ' if uid == session.leader.id else '• '}{m.mention}" for uid, (m, _) in session.players.items()]
    embed = discord.Embed(
        title=f"[합동 소탕 작전] {t['name']}",
        description=(f"{t['desc']}\n\n"
                     f"수사팀 **「{session.unit_name}」** 팀원은 아래 **참가** 버튼으로 합류하세요. "
                     f"({POLICE_OP_LOBBY_SEC}초 안에 모이거나, 팀장이 **출동** 을 누르면 시작합니다)"),
        color=discord.Color.blue()
    )
    embed.add_field(name=f"참가자 ({len(session.players)}/{POLICE_OP_MAX_PLAYERS})", value="\n".join(lines), inline=False)
    embed.add_field(name="조건", value=f"최소 **{t['min_players']}명** · 명예 **{t['min_honor']:,}pt** 이상", inline=True)
    embed.add_field(name="보상 (전원)", value=f"최대 `${t['money']:,}` · 명예 `+{t['honor']}`", inline=True)
    return embed

class PoliceOpLobbyView(discord.ui.View):
    def __init__(self, session: PoliceOpSession):
        super().__init__(timeout=POLICE_OP_LOBBY_SEC + 30)
        self.session = session

    @discord.ui.button(label="참가", style=discord.ButtonStyle.primary)
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        s = self.session
        user = interaction.user
        if s.state != "lobby":
            await interaction.response.send_message("이미 모집이 끝났습니다.", ephemeral=True)
            return
        if str(user.id) not in s.member_ids:
            await interaction.response.send_message("이 수사팀의 팀원만 참가할 수 있습니다.", ephemeral=True)
            return
        if not _is_police(user):
            await interaction.response.send_message("치안국 경찰만 참가할 수 있습니다.", ephemeral=True)
            return
        if user.id in s.players or user.id in police_op_user_map:
            await interaction.response.send_message("이미 작전에 참가 중입니다.", ephemeral=True)
            return
        if _is_jailed(user.id):
            await interaction.response.send_message("수감 중에는 참가할 수 없습니다.", ephemeral=True)
            return
        try:
            info = await asyncio.to_thread(get_user_data, user.id)
        except Exception as e:
            print(f"Police op join error: {e}")
            await interaction.response.send_message(POLICE_UNIT_DB_ERROR_MSG, ephemeral=True)
            return
        # DB 조회(await) 뒤에 상태를 다시 확인한 다음, 바로 등록한다
        if s.state != "lobby":
            await interaction.response.send_message("이미 모집이 끝났습니다.", ephemeral=True)
            return
        if info["honor"] < s.tier["min_honor"]:
            await interaction.response.send_message(f"이 작전은 명예 **{s.tier['min_honor']:,}pt** 이상만 참가할 수 있습니다.", ephemeral=True)
            return
        if len(s.players) >= POLICE_OP_MAX_PLAYERS:
            await interaction.response.send_message("참가 인원이 가득 찼습니다.", ephemeral=True)
            return
        if user.id in s.players or user.id in police_op_user_map:
            await interaction.response.send_message("이미 작전에 참가 중입니다.", ephemeral=True)
            return
        s.players[user.id] = (user, compute_combat_stats(info["honor"], info.get("inventory", {})))
        police_op_user_map[user.id] = s.unit_id
        await interaction.response.edit_message(embed=police_op_lobby_embed(s), view=self)

    @discord.ui.button(label="출동 (팀장)", style=discord.ButtonStyle.success)
    async def go(self, interaction: discord.Interaction, button: discord.ui.Button):
        s = self.session
        if interaction.user.id != s.leader.id:
            await interaction.response.send_message("작전을 연 팀장만 출동 명령을 내릴 수 있습니다.", ephemeral=True)
            return
        if s.state != "lobby":
            await interaction.response.send_message("이미 모집이 끝났습니다.", ephemeral=True)
            return
        if len(s.players) < s.tier["min_players"]:
            await interaction.response.send_message(f"최소 **{s.tier['min_players']}명** 이 모여야 출동할 수 있습니다. (현재 {len(s.players)}명)", ephemeral=True)
            return
        s.go.set()
        await interaction.response.defer()

def police_op_stage_chance(session: PoliceOpSession) -> float:
    t = session.tier
    n = len(session.players)
    avg_atk = sum(st["atk"] for _, st in session.players.values()) / max(1, n)
    chance = t["base"] + 0.04 * max(0, n - t["min_players"]) + 0.12 * min(1.0, avg_atk / t["ref_atk"])
    return max(0.30, min(0.92, chance))

def police_op_progress_embed(session: PoliceOpSession, results: list) -> discord.Embed:
    lines = []
    for i, name in enumerate(POLICE_OP_STAGES):
        if i < len(results):
            lines.append(f"{'성공' if results[i] else '실패'} — {name}")
        elif i == len(results):
            lines.append(f"진행 중… — {name}")
        else:
            lines.append(f"대기 — {name}")
    embed = discord.Embed(title=f"[합동 소탕 작전] {session.tier['name']}", description="\n".join(lines), color=discord.Color.blue())
    embed.set_footer(text=f"수사팀 「{session.unit_name}」 · 참가 {len(session.players)}명")
    return embed

async def run_police_op(session: PoliceOpSession):
    tier = session.tier
    ran = False
    try:
        try:
            await asyncio.wait_for(session.go.wait(), timeout=POLICE_OP_LOBBY_SEC)
        except asyncio.TimeoutError:
            pass
        session.state = "running"
        n = len(session.players)

        if n < tier["min_players"]:   # 인원 미달 → 작전 취소, 작전비 환불
            try:
                await asyncio.to_thread(police_unit_add_treasury, session.unit_id, tier["cost"])
            except Exception as e:
                print(f"Police op refund error: {e}")
            embed = discord.Embed(title="[합동 소탕 작전 취소]",
                                  description=f"인원이 모이지 않아 작전이 취소되었습니다. (작전비 `${tier['cost']:,}` 는 금고로 돌아갑니다)",
                                  color=discord.Color.dark_grey())
            await session.message.edit(embed=embed, view=None)
            return

        ran = True
        chance = police_op_stage_chance(session)
        results = []
        await session.message.edit(embed=police_op_progress_embed(session, results), view=None)
        for _ in POLICE_OP_STAGES:
            await asyncio.sleep(POLICE_OP_STAGE_SEC)
            results.append(random.random() < chance)
            await session.message.edit(embed=police_op_progress_embed(session, results), view=None)

        succ = sum(results)
        frac = POLICE_OP_REWARD_FRAC[succ]
        if succ > 0:
            money_each = int(tier["money"] * frac)
            honor_each = int(round(tier["honor"] * frac))
        else:
            money_each = int(tier["money"] * POLICE_OP_FAIL_MONEY_RATIO)
            honor_each = 0
        points = int(tier["points"] * frac)

        paid_to = []
        for uid, (member, _) in session.players.items():
            try:
                await asyncio.to_thread(update_user_stats, uid, money_each, honor_each)
                paid_to.append(member)
            except Exception as e:
                print(f"Police op payout error ({uid}): {e}")
        if succ > 0:
            for member in paid_to:
                police_shift_record(member.id, "op_stage", succ)   # /월급 근무 실적
        treasury_add = int(money_each * len(paid_to) * POLICE_OP_TREASURY_RETURN) if succ > 0 else 0
        try:
            await asyncio.to_thread(police_unit_record_op, session.unit_id, treasury_add, points, succ > 0)
        except Exception as e:
            print(f"Police op record error: {e}")

        titles = {3: "작전 대성공", 2: "작전 성공", 1: "작전 부분 성공", 0: "작전 실패"}
        colors = {3: discord.Color.green(), 2: discord.Color.green(), 1: discord.Color.gold(), 0: discord.Color.red()}
        desc = "\n".join(f"{'성공' if r else '실패'} — {name}" for name, r in zip(POLICE_OP_STAGES, results))
        desc += f"\n\n**{succ}/{len(POLICE_OP_STAGES)} 단계 성공**"
        if succ > 0:
            desc += f" (보상 {int(frac * 100)}%)\n참가자 전원 `${money_each:,}` · 명예 `+{honor_each}`"
            desc += f"\n수사팀 작전 점수 `+{points}` · 금고 `+${treasury_add:,}`"
        else:
            desc += f"\n작전은 실패했지만 참가자 전원에게 위로금 `${money_each:,}` 이(가) 지급됩니다."
        embed = discord.Embed(title=f"[{titles[succ]}] {tier['name']}", description=desc, color=colors[succ])
        embed.add_field(name="참가자", value="\n".join(m.mention for m in paid_to) or "-", inline=False)
        await session.message.edit(embed=embed, view=None)
    except Exception as e:
        print(f"Police op error: {e}")
    finally:
        session.state = "done"
        if ran:
            police_op_cooldowns[session.unit_id] = datetime.now() + timedelta(seconds=POLICE_OP_COOLDOWN_SEC)
        police_op_release(session)

@unit_group.command(name="작전", description="우리 수사팀의 합동 소탕 작전을 엽니다. (수사팀 금고에서 작전비가 소모됩니다)")
@app_commands.describe(대상="소탕할 범죄 조직")
@app_commands.choices(대상=[
    app_commands.Choice(name=f"{t['name']} (인원 {t['min_players']}+ · 명예 {t['min_honor']:,}+ · 작전비 ${t['cost']:,})", value=k)
    for k, t in POLICE_OP_TIERS.items()
])
async def unit_op_cmd(interaction: discord.Interaction, 대상: app_commands.Choice[str]):
    if not await _unit_precheck(interaction): return
    user = interaction.user
    tier = POLICE_OP_TIERS[대상.value]

    try:
        unit = await asyncio.to_thread(police_unit_get_by_user, interaction.guild_id, user.id)
        info = await asyncio.to_thread(get_user_data, user.id)
    except Exception as e:
        print(f"Police op start error: {e}")
        await interaction.response.send_message(POLICE_UNIT_DB_ERROR_MSG, ephemeral=True)
        return
    if not unit:
        await interaction.response.send_message("소속된 수사팀이 없습니다. `/수사팀 생성` 또는 초대를 받아 보세요.", ephemeral=True)
        return
    if unit["owner_id"] != str(user.id):
        await interaction.response.send_message("작전은 팀장만 열 수 있습니다.", ephemeral=True)
        return

    # 확인과 등록 사이에 await 가 없으므로, 같은 수사팀이 동시에 작전을 열 수 없다
    if unit["id"] in police_ops:
        await interaction.response.send_message("우리 수사팀의 작전이 이미 진행 중입니다.", ephemeral=True)
        return
    if user.id in police_op_user_map:
        await interaction.response.send_message("이미 작전에 참가 중입니다.", ephemeral=True)
        return
    cd = police_op_cooldowns.get(unit["id"])
    if cd and datetime.now() < cd:
        remain = int((cd - datetime.now()).total_seconds())
        await interaction.response.send_message(f"수사팀이 정비 중입니다. **{remain // 60}분 {remain % 60}초** 뒤에 다시 작전을 열 수 있습니다.", ephemeral=True)
        return
    if len(unit.get("member_ids") or []) < tier["min_players"]:
        await interaction.response.send_message(f"이 작전은 팀원이 최소 **{tier['min_players']}명** 이상이어야 합니다.", ephemeral=True)
        return
    if unit.get("treasury", 0) < tier["cost"]:
        await interaction.response.send_message(f"수사팀 금고가 부족합니다. (작전비 `${tier['cost']:,}` / 금고 `${unit.get('treasury', 0):,}`)\n`/수사팀 기부` 로 금고를 채워 주세요.", ephemeral=True)
        return
    if info["honor"] < tier["min_honor"]:
        await interaction.response.send_message(f"이 작전은 명예 **{tier['min_honor']:,}pt** 이상만 도전할 수 있습니다.", ephemeral=True)
        return

    session = PoliceOpSession(unit, 대상.value, user, compute_combat_stats(info["honor"], info.get("inventory", {})))
    police_ops[unit["id"]] = session
    police_op_user_map[user.id] = unit["id"]
    try:
        await asyncio.to_thread(police_unit_update, unit["id"], {"treasury": unit["treasury"] - tier["cost"]})
    except Exception as e:
        police_op_release(session)
        print(f"Police op treasury error: {e}")
        await interaction.response.send_message(POLICE_UNIT_DB_ERROR_MSG, ephemeral=True)
        return

    await interaction.response.send_message(embed=police_op_lobby_embed(session), view=PoliceOpLobbyView(session))
    session.message = await interaction.original_response()
    asyncio.create_task(run_police_op(session))

@unit_group.command(name="작전정보", description="합동 소탕 작전 목록과 진행 방법을 확인합니다.")
async def unit_op_info_cmd(interaction: discord.Interaction):
    if not await check_maintenance(interaction): return
    lines = []
    for t in POLICE_OP_TIERS.values():
        lines.append(f"**{t['name']}** — 인원 {t['min_players']}+ · 명예 {t['min_honor']:,}+ · 작전비 ${t['cost']:,}\n"
                     f"└ {t['desc']}\n└ 보상(전원, 최대): `${t['money']:,}` · 명예 `+{t['honor']}` · 작전 점수 `+{t['points']}`")
    embed = discord.Embed(
        title="[합동 소탕 작전 안내]",
        description=("1. 팀장이 `/수사팀 작전` 으로 작전을 엽니다. (금고에서 작전비가 빠집니다)\n"
                     f"2. 팀원이 **참가** 버튼으로 모입니다. ({POLICE_OP_LOBBY_SEC}초, 팀장이 **출동** 을 누르면 즉시 시작)\n"
                     "3. 잠입 → 급습 → 검거 3단계를 차례로 수행하고, 성공한 단계 수만큼 보상을 받습니다.\n"
                     "   (3단계 100% · 2단계 70% · 1단계 40% · 전부 실패 시 위로금)\n"
                     "4. 인원이 많고 장비가 좋을수록 단계 성공률이 올라갑니다.\n"
                     f"5. 같은 수사팀은 작전 후 {POLICE_OP_COOLDOWN_SEC // 60}분간 정비합니다.\n\n" + "\n\n".join(lines)),
        color=discord.Color.blue()
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)



# ==========================================================================
# [경찰 전용] /월급  —  10분 근무 평가
#   • /월급 을 쓰면 10분짜리 근무가 시작되고, 그동안 한 경찰 활동(순찰·검거·심문·수사팀 작전)이 점수로 쌓입니다.
#   • 10분이 지나면 자동으로 정산되어 점수에 비례한 월급(보상금 + 명예)이 지급됩니다.
#   • 근무 기록은 메모리에만 있어서, 근무 중에 봇이 재시작되면 그 근무는 사라집니다.
# ==========================================================================
POLICE_SALARY_SHIFT_SEC = 600          # 근무(추적) 시간
POLICE_SALARY_COOLDOWN_SEC = 600       # 정산 후 다음 근무를 시작할 수 있을 때까지 대기시간
POLICE_SALARY_FULL_SCORE = 100         # 이 점수에 도달하면 기본 월급의 100%
POLICE_SALARY_MAX_RATIO = 1.5          # 월급 상한 (기본 월급의 150% = 150점)
POLICE_SALARY_BASE_MONEY = 2000        # 100% 월급: 보상금
POLICE_SALARY_BASE_HONOR = 22          # 100% 월급: 명예 (황금 배지 보너스 적용)
POLICE_SALARY_ARREST_BONUS_PER = 100   # 거물 검거 보너스: 수배범 명예 100pt 당 +1점
POLICE_SALARY_ARREST_BONUS_MAX = 50    # 거물 검거 보너스 상한 → 검거 1회 50 ~ 100점

# 행동 하나당 점수
POLICE_SALARY_POINTS = {
    "patrol": 30,              # 순찰 1회 (쿨타임 3분 → 10분 동안 최대 4회)
    "arrest": 50,              # 수배범 검거 1회
    "interrogate": 25,         # 심문 재판 완료 1회
    "interrogate_match": 15,   # 재판 결과가 내 의견과 일치 (심문 점수에 추가)
    "op_stage": 15,            # 수사팀 합동 작전에서 성공한 단계 1개당
}
POLICE_SALARY_LABELS = {
    "patrol": "순찰", "arrest": "검거", "interrogate": "심문 재판",
    "interrogate_match": "의견 일치 판결", "op_stage": "작전 성공 단계",
}

police_shifts = {}              # {user_id: PoliceShift}  (진행 중인 근무)
police_salary_cooldowns = {}    # {user_id: datetime}

class PoliceShift:
    def __init__(self, member: discord.Member, interaction: discord.Interaction):
        self.user_id = member.id
        self.member = member
        self.interaction = interaction
        self.channel = interaction.channel
        self.started = time.time()
        self.ends = self.started + POLICE_SALARY_SHIFT_SEC
        self.counts = {}
        self.bonus_points = 0   # 거물 검거 보너스 등 행동마다 달라지는 추가 점수
        self.settled = False
        self.task = None

def police_shift_record(user_id: int, kind: str, n: int = 1, bonus: int = 0):
    """경찰 명령어들이 호출: 근무 중인 경찰이면 행동을 점수로 기록 (근무 중이 아니면 아무 일도 없음)"""
    s = police_shifts.get(user_id)
    if s is None or s.settled or time.time() >= s.ends:
        return
    s.counts[kind] = s.counts.get(kind, 0) + n
    s.bonus_points += max(0, int(bonus))

def police_shift_score(s: PoliceShift) -> int:
    return sum(POLICE_SALARY_POINTS.get(k, 0) * c for k, c in s.counts.items()) + s.bonus_points

def police_shift_grade(ratio: float) -> str:
    if ratio >= 1.0: return "S (우수)"
    if ratio >= 0.7: return "A (양호)"
    if ratio >= 0.4: return "B (보통)"
    if ratio > 0:    return "C (미흡)"
    return "D (무실적)"

def police_shift_breakdown(s: PoliceShift) -> str:
    lines = []
    for kind, label in POLICE_SALARY_LABELS.items():
        c = s.counts.get(kind, 0)
        if c:
            lines.append(f"• {label} ×{c} → `+{POLICE_SALARY_POINTS[kind] * c}점`")
    if s.bonus_points:
        lines.append(f"• 거물 검거 보너스 → `+{s.bonus_points}점`")
    return "\n".join(lines) or "아직 기록된 실적이 없습니다."

def police_shift_payout(score: int):
    ratio = min(POLICE_SALARY_MAX_RATIO, score / POLICE_SALARY_FULL_SCORE)
    return ratio, int(POLICE_SALARY_BASE_MONEY * ratio)

def police_shift_embed(s: PoliceShift, title: str) -> discord.Embed:
    score = police_shift_score(s)
    ratio, money = police_shift_payout(score)
    remain = max(0, int(s.ends - time.time()))
    embed = discord.Embed(
        title=title,
        description=(f"근무 종료까지 **{remain // 60}분 {remain % 60}초**\n"
                     f"현재 실적 **{score}점** / 기본 월급 기준 {POLICE_SALARY_FULL_SCORE}점 (최대 {int(POLICE_SALARY_MAX_RATIO * 100)}%)\n"
                     f"예상 월급 `${money:,}` · 평가 **{police_shift_grade(ratio)}**\n\n"
                     f"{police_shift_breakdown(s)}"),
        color=discord.Color.blue()
    )
    return embed

async def settle_police_shift(s: PoliceShift):
    if s.settled:
        return
    s.settled = True   # 동시에 두 번 정산되지 않도록 await 전에 표시
    if police_shifts.get(s.user_id) is s:
        police_shifts.pop(s.user_id, None)
    police_salary_cooldowns[s.user_id] = datetime.now() + timedelta(seconds=POLICE_SALARY_COOLDOWN_SEC)

    score = police_shift_score(s)
    ratio, money = police_shift_payout(score)
    honor = 0
    note = ""
    rank = None
    guild = s.member.guild
    member = guild.get_member(s.user_id) or s.member
    try:
        if not _is_police(member):
            money, honor, note = 0, 0, "\n경찰 신분이 아니어서 월급이 지급되지 않았습니다."
        elif score > 0:
            info = await asyncio.to_thread(get_user_data, s.user_id)
            mult = police_bonuses(info.get("inventory"))["honor_mult"]
            honor = int(round(POLICE_SALARY_BASE_HONOR * ratio * mult))
            await asyncio.to_thread(update_user_stats, s.user_id, money, honor)
            info = await asyncio.to_thread(get_user_data, s.user_id)
            rank = get_user_rank(info["honor"], is_police=True)
        else:
            money = 0
    except Exception as e:
        print(f"Police salary payout error: {e}")
        money, honor, note = 0, 0, "\n정산 중 오류가 발생해 월급이 지급되지 않았습니다. 관리자에게 문의해 주세요."

    desc = (f"**{member.mention}** 경관의 10분 근무 평가가 끝났습니다.\n\n"
            f"**평가:** {police_shift_grade(ratio)} · 실적 **{score}점**\n"
            f"{police_shift_breakdown(s)}\n\n")
    if money > 0 or honor > 0:
        desc += f"**월급:** `${money:,}`\n**경찰 실적:** `+{honor} pt`"
        if rank:
            desc += f" (현재 직급: `{rank}`)"
    else:
        desc += "이번 근무에는 지급된 월급이 없습니다." if not note else ""
    desc += note
    desc += f"\n\n다음 근무는 **{POLICE_SALARY_COOLDOWN_SEC // 60}분 뒤**에 시작할 수 있습니다."
    embed = discord.Embed(title="[치안국 월급 명세서]", description=desc,
                          color=discord.Color.green() if money > 0 else discord.Color.dark_grey())
    if member.avatar:
        embed.set_thumbnail(url=member.avatar.url)
    try:
        await s.channel.send(content=member.mention, embed=embed)
    except Exception:
        try:
            await s.interaction.followup.send(content=member.mention, embed=embed, ephemeral=True)
        except Exception as e:
            print(f"Police salary notify error: {e}")

async def run_police_shift(s: PoliceShift):
    try:
        await asyncio.sleep(max(0.0, s.ends - time.time()))
        await settle_police_shift(s)
    except Exception as e:
        print(f"Police shift error: {e}")
        if not s.settled:
            police_shifts.pop(s.user_id, None)

@bot.tree.command(name="월급", description="[경찰 전용] 10분간 근무 실적을 평가받아 점수에 비례한 월급(보상금·명예)을 받습니다.")
async def salary_command(interaction: discord.Interaction):
    if not await check_maintenance(interaction): return
    if not await check_membership(interaction): return
    if await check_jail(interaction): return
    user = interaction.user
    if not _is_police(user):
        await interaction.response.send_message("치안국 경찰만 월급을 받을 수 있습니다!", ephemeral=True)
        return

    s = police_shifts.get(user.id)
    if s is not None and not s.settled:
        if time.time() >= s.ends:   # 정산 타이머가 놓친 경우를 대비한 안전장치
            await interaction.response.send_message("근무 시간이 끝나 정산을 진행합니다. 잠시 후 월급 명세서가 올라옵니다.", ephemeral=True)
            await settle_police_shift(s)
            return
        await interaction.response.send_message(embed=police_shift_embed(s, "[근무 중] 월급 평가 진행 상황"), ephemeral=True)
        return

    cd = police_salary_cooldowns.get(user.id)
    if cd and datetime.now() < cd:
        remain = int((cd - datetime.now()).total_seconds())
        await interaction.response.send_message(f"월급 정산 직후입니다. **{remain // 60}분 {remain % 60}초** 뒤에 새 근무를 시작할 수 있습니다.", ephemeral=True)
        return

    s = PoliceShift(user, interaction)
    police_shifts[user.id] = s   # 확인과 등록 사이에 await 가 없어 중복 시작이 불가능
    points_text = "\n".join(f"• {POLICE_SALARY_LABELS[k]}: `+{p}점`" for k, p in POLICE_SALARY_POINTS.items())
    points_text += f"\n• 거물 검거 보너스: 수배범 명예 {POLICE_SALARY_ARREST_BONUS_PER}pt 당 `+1점` (최대 +{POLICE_SALARY_ARREST_BONUS_MAX}점)"
    embed = discord.Embed(
        title="[근무 시작] 월급 평가",
        description=(f"지금부터 **{POLICE_SALARY_SHIFT_SEC // 60}분간** 경찰 활동이 실적으로 기록됩니다.\n"
                     f"{POLICE_SALARY_SHIFT_SEC // 60}분 뒤 자동으로 정산되어 월급이 지급됩니다.\n\n"
                     f"**실적 점수**\n{points_text}\n\n"
                     f"**월급 기준:** {POLICE_SALARY_FULL_SCORE}점 = `${POLICE_SALARY_BASE_MONEY:,}` · 명예 `+{POLICE_SALARY_BASE_HONOR}` "
                     f"(점수에 비례, 최대 {int(POLICE_SALARY_MAX_RATIO * 100)}%)\n"
                     f"`/월급` 을 다시 입력하면 진행 상황을 볼 수 있어요."),
        color=discord.Color.blue()
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)
    s.task = asyncio.create_task(run_police_shift(s))


bot.run(os.getenv("DISCORD_TOKEN"))