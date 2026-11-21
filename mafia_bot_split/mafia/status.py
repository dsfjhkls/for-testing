"""상태·데이터: 유저 데이터/감옥·수배/장비 표, 성향·칭호, 업적·일일·주간 의뢰

합친 모듈: status.py, alignment.py, achievements.py
"""

import asyncio
import discord
import re
import threading as _status_threading
import threading as _ach_threading
import time
from datetime import datetime, timedelta, timezone

from .core import bot, inc_range, jail_list, supabase, wanted_list


# ==========================================================================
# [합친 모듈] status.py
#   감옥·수배 상태 저장/복구, 유저 데이터 접근 함수
# ==========================================================================
_status_lock = _status_threading.Lock()
_status_warned = False
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
# 목표 페이스 (쉬지 않고 열심히, 아이템을 효율적으로 샀을 때 기준 / 가끔 접속하는 유저는 이것의 몇 배):
#   행동대원(500pt) 약 0.5시간 → 솔다토(1,000) 약 1시간 → 카포(3,000) 약 2.5시간
#   → 언더보스(5,000) 약 3.3시간 → 대부(10,000) 약 4~5시간
# 명예 보상은 MISSIONS / NPC_DATA / NAMED_MONSTERS / 레이드·작전 표 / 경찰 활동 상수(PATROL_HONOR, ARREST_BASE_HONOR 등)에 있습니다.
# 페이스를 조절하려면 이 값들을 같은 비율로 올리거나 내리고, 쿨타임(아래 *_COOLDOWN_SEC)을 함께 조절하세요.
MISSION_COOLDOWN_SEC = 40        # 임무 기본 쿨타임
MISSION_COOLDOWN_FAST_SEC = 30   # 크로노그래프 시계 / 특수 바이크 보유 시
# (/전투 와 /네임드 는 쿨타임이 없습니다. 전투 화면이 끝날 때까지 다른 명령어가 잠기므로 동시에 여러 판을 열 수는 없습니다)
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
    "mission_1": {"title": "타 조직 구역 비밀 조사",   "label": "타 조직 구역 비밀 조사", "diff": "하",   "success_rate": 85, "reward": inc_range((150, 350)),   "honor": 6,  "fine": 80},
    "mission_2": {"title": "밀수품 비밀 운송 작업",     "label": "밀수품 비밀 운송 작업", "diff": "중",   "success_rate": 60, "reward": inc_range((350, 800)),   "honor": 12,  "fine": 200},
    "mission_3": {"title": "라이벌 보스의 장부 탈취",   "label": "라이벌 보스의 장부 탈취", "diff": "상",   "success_rate": 40, "reward": inc_range((900, 1900)),  "honor": 16,  "fine": 500},
    "mission_4": {"title": "기밀 문서 야간 밀반출",     "label": "기밀 문서 야간 밀반출", "diff": "중상", "success_rate": 50, "reward": inc_range((650, 1400)),  "honor": 14,  "fine": 350},
    "mission_5": {"title": "중앙 은행 호송 차량 습격",  "label": "중앙 은행 호송 차량 습격", "diff": "최상", "success_rate": 20, "reward": inc_range((2500, 5000)), "honor": 36, "fine": 1200},
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

# 강화 보스 전리품: 보유만 해도 능력치가 오릅니다 (모두 합산). {이름: (공격력, 체력, 방어력)}
ELITE_TROPHY_STATS = {
    "[강화보스 전리품] 심연의 왕관":        (150, 2500, 40),
    "[강화보스 전리품] 대재앙의 붉은 깃발":  (250, 4000, 60),
    "[강화보스 전리품] 종말의 심판 인장":    (400, 6000, 100),
}

def compute_combat_stats(honor: int, inventory: dict) -> dict:
    """/전투, /보스, /결투 가 모두 같은 공식을 쓰도록 한 곳으로 통합"""
    from .economy import crafted_gear_best  # 순환 import 방지: 호출 시점에 가져옴
    from .economy import POLICE_SHOP_ITEMS  # 순환 import 방지: 호출 시점에 가져옴
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
    for it in POLICE_SHOP_ITEMS:   # 치안국 보급품의 공격력·방어력·체력 (보유한 것은 모두 합산)
        if it["name"] in inventory:
            atk += it.get("atk_bonus", 0)
            def_val += it.get("def_bonus", 0)
            hp += it.get("hp_bonus", 0)
    for tname, (ta, th, td) in ELITE_TROPHY_STATS.items():   # 강화 보스 전리품
        if tname in inventory:
            atk += ta
            hp += th
            def_val += td
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

class DBReadError(RuntimeError):
    """DB에서 유저 데이터를 읽지 못했을 때 (일시적인 네트워크/DB 오류 등)"""


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
        try:
            supabase.table("user_data").insert(default_data).execute()
        except Exception:
            # 거의 동시에 처음 접속해 이미 만들어졌을 수 있음 → 다시 읽어 본다
            again = supabase.table("user_data").select("*").eq("user_id", str_id).execute()
            if again.data:
                row = again.data[0]
                row["inventory"] = normalize_inventory(row.get("inventory") or {})
                return row
            raise
        return default_data
    except Exception as e:
        print(f"DB Error (get_user_data): {e}")
        # [수정] 예전에는 여기서 '잔고 1000짜리 가짜 데이터'를 돌려줬는데, 그 값으로 계산한 결과가
        # 저장되면서 실제 잔고·인벤토리가 덮어써질 수 있었다. 이제는 오류로 알리고 아무것도 저장하지 않는다.
        raise DBReadError(f"유저 데이터를 읽지 못했습니다: {e}") from e

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

# === 주간 의뢰(/주간) 설정 ===
# 이미 업적용으로 쌓이고 있는 통계 카운터(stats)가 이번 주에 얼마나 늘었는지로 진행도를 계산합니다.
#   → 임무·전투·경찰 활동 코드를 따로 고칠 필요가 없고, 매주 월요일 00:00(한국 시간)에 기준점이 새로 잡힙니다.
# counters: 합산할 stats 카운터 이름들 / target: 목표 / money·honor: 의뢰 1개 완료 보상
WEEKLY_QUESTS = {
    "mafia": [
        {"key": "mission",  "name": "비밀 임무 성공",              "counters": ["success"],                  "target": 40, "money": 6000,  "honor": 200, "hint": "/임무"},
        {"key": "fight",    "name": "NPC 전투 승리",               "counters": ["fight_wins"],               "target": 20, "money": 5000,  "honor": 120,  "hint": "/전투"},
        {"key": "gamble",   "name": "카지노 도박",                 "counters": ["gamble_played"],            "target": 15, "money": 4000,  "honor": 0,   "hint": "/도박"},
        {"key": "family",   "name": "조직 활동 (레이드 클리어 · 구역전 승리)", "counters": ["raid_clears", "war_wins"], "target": 1,  "money": 8000,  "honor": 120,  "hint": "/레이드 · /구역"},
        {"key": "named",    "name": "네임드 · 보스 처치",          "counters": ["named_kills", "boss_kills"], "target": 1,  "money": 10000, "honor": 160,  "hint": "/네임드 · /보스"},
    ],
    "police": [
        {"key": "patrol",   "name": "순찰",                        "counters": ["police_patrols"],           "target": 15, "money": 5000,  "honor": 160,  "hint": "/순찰"},
        {"key": "arrest",   "name": "수배범 검거",                 "counters": ["police_arrests"],           "target": 3,  "money": 8000,  "honor": 200, "hint": "/검거"},
        {"key": "interro",  "name": "심문",                        "counters": ["police_interrogations"],    "target": 5,  "money": 4000,  "honor": 120,  "hint": "/심문"},
        {"key": "match",    "name": "심문 판결 일치",              "counters": ["police_matches"],           "target": 3,  "money": 4000,  "honor": 120,  "hint": "/심문"},
        {"key": "ops",      "name": "수사 작전 성공",              "counters": ["police_ops"],               "target": 2,  "money": 10000, "honor": 160,  "hint": "/작전"},
    ],
}
WEEKLY_ALL_CLEAR_BONUS = {"money": 15000, "honor": 200}   # 5개를 모두 완료·수령했을 때 한 번 더 주는 보너스
WEEKLY_TITLE_WEEKS = 4                                    # 모든 의뢰를 이 주 수만큼 완료하면 칭호
WEEKLY_COUNTERS = sorted({c for qs in WEEKLY_QUESTS.values() for q in qs for c in q["counters"]})
KST = timezone(timedelta(hours=9))

def weekly_period(now: float = None):
    """이번 주(월요일 00:00 KST ~ 다음 월요일 00:00 KST) → (주 식별자, 시작 epoch, 끝 epoch)"""
    dt = datetime.fromtimestamp(time.time() if now is None else now, KST)
    start = (dt - timedelta(days=dt.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return start.strftime("%Y-%m-%d"), int(start.timestamp()), int((start + timedelta(days=7)).timestamp())


# ==========================================================================
# [합친 모듈] alignment.py
#   성향(선/악) 시스템, 전직 조건, 부패 경찰
# ==========================================================================
# ==========================================================================
# [성향(선/악) 시스템 · 전직 조건 · 부패 경찰]
#   • 시민일 때 한 활동으로 성향 점수(karma)가 쌓입니다. 좋은 활동은 +, 나쁜 활동은 - (blob["karma"], 범위 ±KARMA_LIMIT)
#   • 성향에 따라 /가입(전직) 조건이 달라집니다. 악 성향이 경찰이 되면 '부패 경찰'(blob["corrupt"])이 됩니다.
#   • 부패 경찰은 /검거 로 수배 중이 아닌 시민도 체포할 수 있고, /갈취 로 시민의 돈을 뜯어낼 수 있습니다. (정식 경찰에게 들키면 위험)
# ==========================================================================
KARMA_LIMIT = 100
KARMA_GOOD_MIN = 20          # 이 점수 이상이면 '선'
KARMA_EVIL_MAX = -20         # 이 점수 이하이면 '악'  (그 사이는 '중립')

CITIZEN_JOIN_GATING = True   # False 로 바꾸면 전직 조건 없이 누구나 가입 (테스트용)
POLICE_JOIN_PARTTIME = 20    # 정식 경찰(선·중립): 아르바이트 20회 이상
CORRUPT_JOIN_PARTTIME = 10   # 부패 경찰(악): 위장 경력으로 아르바이트 10회 이상
CORRUPT_JOIN_FEE = 5000      # 부패 경찰(악): 임용 로비 자금
MAFIA_JOIN_PICKPOCKETS = 3   # 마피아: 소매치기 3회 성공(범죄 경력) + 성향이 '선'이 아닐 것

# --- 시민 활동 ---
PARTTIME_KARMA = 1                      # /아르바이트 1회 (시민일 때만 성향에 반영)
SERVICE_COOLDOWN_SEC = 60               # /봉사
SERVICE_REWARD = inc_range((60, 120))
SERVICE_KARMA = 3
DONATE_MIN = 200                        # /기부 최소 금액
DONATE_UNIT = 200                       # 이 금액마다 성향 +1
DONATE_MAX_KARMA = 10                   # 1회 기부로 오르는 성향 상한
DONATE_COOLDOWN_SEC = 10
PICKPOCKET_COOLDOWN_SEC = 60            # /소매치기
PICKPOCKET_SUCCESS = 0.55
PICKPOCKET_REWARD = inc_range((150, 400))
PICKPOCKET_FINE = 100                   # 실패 시 벌금
PICKPOCKET_KARMA_OK = -3
PICKPOCKET_KARMA_FAIL = -2
PICKPOCKET_WANTED_SEC = 60              # 실패하면 이 시간 동안 수배 (경찰이 /검거 가능)

# --- 부패 경찰 ---
CORRUPT_ARREST_COOLDOWN_SEC = 90        # /검거 로 무차별 체포
CORRUPT_ARREST_JAIL_SEC = 300
CORRUPT_ARREST_FINE = (100, 300)        # 체포한 시민에게서 '벌금' 명목으로 뜯는 돈
CORRUPT_COMPLAINT_CHANCE = 0.30         # 민원이 접수되어 본인이 수배당할 확률
CORRUPT_COMPLAINT_WANTED_SEC = 120
CORRUPT_EXTORT_COOLDOWN_SEC = 60        # /갈취
CORRUPT_EXTORT_SUCCESS = 0.70
CORRUPT_EXTORT_PCT = (0.08, 0.15)       # 시민 보유 자금의 8~15%
CORRUPT_EXTORT_MAX = 500
CORRUPT_EXTORT_MIN_TARGET_MONEY = 100   # 이보다 가난한 시민은 털 게 없음
CORRUPT_EXTORT_FAIL_WANTED_SEC = 90
CORRUPT_VICTIM_SHIELD_SEC = 300         # 한 번 당한 시민은 이 시간 동안 부패 경찰에게 안 당함
CORRUPT_ARREST_BONUS_HONOR = 50         # 정식 경찰이 부패 경찰을 검거하면 추가 명예

def alignment_of(karma: int) -> str:
    return "선" if karma >= KARMA_GOOD_MIN else "악" if karma <= KARMA_EVIL_MAX else "중립"

def alignment_text(karma: int) -> str:
    return f"{alignment_of(karma)} ({karma:+d})"

def karma_change(blob: dict, delta: int):
    blob["karma"] = max(-KARMA_LIMIT, min(KARMA_LIMIT, int(blob.get("karma", 0)) + delta))

def _is_citizen(member: discord.Member) -> bool:
    names = {r.name for r in member.roles}
    return "조직원" not in names and "경찰" not in names

def evaluate_join(target_role: str, info: dict, blob: dict) -> dict:
    """시민 → 조직원/경찰 전직 조건 검사. 반환: ok / checks[(조건, 충족, 현재 상태)] / corrupt(부패 경찰 임용) / fee(차감 비용)"""
    karma, st, money = blob["karma"], blob["stats"], int(info.get("money", 0) or 0)
    align = alignment_of(karma)
    corrupt, fee, checks = False, 0, []
    if target_role == "경찰":
        if align == "악":
            corrupt, fee = True, CORRUPT_JOIN_FEE
            checks = [(f"아르바이트 {CORRUPT_JOIN_PARTTIME}회 이상 (위장 경력)", st["parttime_total"] >= CORRUPT_JOIN_PARTTIME, f"{st['parttime_total']}/{CORRUPT_JOIN_PARTTIME}회"),
                      (f"임용 로비 자금 ${fee:,}", money >= fee, f"${money:,}")]
        else:
            checks = [(f"아르바이트 {POLICE_JOIN_PARTTIME}회 이상", st["parttime_total"] >= POLICE_JOIN_PARTTIME, f"{st['parttime_total']}/{POLICE_JOIN_PARTTIME}회")]
    else:
        checks = [("성향이 '선'이 아닐 것", align != "선", f"현재 {alignment_text(karma)}"),
                  (f"소매치기 {MAFIA_JOIN_PICKPOCKETS}회 성공 (범죄 경력)", st["pickpocket_ok"] >= MAFIA_JOIN_PICKPOCKETS, f"{st['pickpocket_ok']}/{MAFIA_JOIN_PICKPOCKETS}회")]
    return {"ok": all(c[1] for c in checks), "checks": checks, "corrupt": corrupt, "fee": fee, "align": align}

def join_requirement_text() -> str:
    return (f"**치안국 경찰**\n"
            f"• 성향이 선·중립: 아르바이트 {POLICE_JOIN_PARTTIME}회 이상 → 정식 경찰\n"
            f"• 성향이 악: 아르바이트 {CORRUPT_JOIN_PARTTIME}회 이상 + 로비 자금 ${CORRUPT_JOIN_FEE:,} → **부패 경찰** (프로필에 표시됨)\n"
            f"**마피아 조직**\n"
            f"• 성향이 선이 아니고, 소매치기 {MAFIA_JOIN_PICKPOCKETS}회 성공\n"
            f"**성향 쌓는 법 (시민)**\n"
            f"• 선: `/아르바이트` `/봉사` `/기부` / 악: `/소매치기`  (`/프로필` 에서 확인)")

# === 칭호 시스템 ===
TITLES = {
    "weekly_master": {"name": "주간 의뢰의 달인", "desc": f"주간 의뢰를 모두 완료한 주가 {WEEKLY_TITLE_WEEKS}주 이상", "check": lambda s, i: s["weekly_clears"] >= WEEKLY_TITLE_WEEKS},
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
    "collector":    {"name": "장비 수집가",     "desc": "암시장 장비 10개 이상 보유",            "check": lambda s, i: len([k for k in (i.get("inventory") or {}) if not k.startswith(("[전리품]", "[강화보스 전리품]", "[재료]", "[제작]"))]) >= 10},
    "named_hunter": {"name": "네임드 사냥꾼",   "desc": "네임드 몬스터 1종 처치",                "check": lambda s, i: any(k.startswith("[전리품]") for k in (i.get("inventory") or {}))},
    "named_slayer": {"name": "네임드 학살자",   "desc": "네임드 몬스터 4종 모두 처치",          "check": lambda s, i: sum(1 for k in (i.get("inventory") or {}) if k.startswith("[전리품]")) >= 4},
    "elite_hunter": {"name": "강화 보스 사냥꾼",   "desc": "강화 보스 1회 처치",                    "check": lambda s, i: s["elite_boss_kills"] >= 1},
    "elite_slayer": {"name": "재앙을 꺾은 자",     "desc": "강화 보스 3종의 전리품을 모두 획득",    "check": lambda s, i: all(t in (i.get("inventory") or {}) for t in ELITE_TROPHY_STATS)},
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
                "attendance", "weekly_clears",
                "parttime_total", "service_total", "donated_total", "pickpocket_ok", "pickpocket_fail",   # ← 성향·전직용
                "corrupt_arrests", "extortions",                                                       # ← 부패 경찰용
                "fight_wins", "named_kills", "boss_kills", "elite_boss_kills",            # ← 업적용 카운터
                "duel_wins", "raid_clears", "raid_king_clears", "war_wins", "war_conquests",
                "police_arrests", "police_big_arrests", "police_patrols", "police_interrogations",   # ← 경찰 업적용 카운터
                "police_matches", "police_op_stages", "police_salary_s",
                "police_ops", "police_ops_boss"):          # ← /작전 성공 수 · 최상 난이도 성공 수
        stats[key] = int(stats.get(key, 0) or 0)
    blob["stats"] = stats
    blob["titles"] = list(blob.get("titles") or [])
    ach = blob.get("achievements")                                    # {업적id: 달성 시각(epoch)}
    blob["achievements"] = dict(ach) if isinstance(ach, dict) else {}
    blob["ach_claimed"] = list(blob.get("ach_claimed") or [])           # 보상을 이미 받은 업적 id
    blob["equipped"] = blob.get("equipped")
    blob["karma"] = max(-KARMA_LIMIT, min(KARMA_LIMIT, int(blob.get("karma", 0) or 0)))   # 성향 점수 (선 +, 악 -)
    blob["corrupt"] = bool(blob.get("corrupt"))                                           # 부패 경찰 여부
    _weekly_sync(blob)
    return blob

def _weekly_sync(blob: dict, now: float = None):
    """블롭을 읽을 때마다 호출: 새 주가 시작됐으면 현재 통계를 기준점으로 저장하고 수령 기록을 비웁니다."""
    week_id, _, _ = weekly_period(now)
    stats = blob["stats"]
    raw = blob.get("weekly")
    w = dict(raw) if isinstance(raw, dict) else {}
    if w.get("week") != week_id:
        w = {"week": week_id, "base": {}, "claimed": [], "bonus": False}
    base = dict(w["base"]) if isinstance(w.get("base"), dict) else {}
    for c in WEEKLY_COUNTERS:
        base[c] = int(base.get(c, stats.get(c, 0)) or 0)
    w["base"] = base
    w["claimed"] = list(w.get("claimed") or [])
    w["bonus"] = bool(w.get("bonus"))
    blob["weekly"] = w

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


# ==========================================================================
# [합친 모듈] achievements.py
#   업적 시스템 (/업적, /일퀘, /주간, /칭호)
# ==========================================================================
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
    _ach("a_elite_1",    "심화", "전설을 넘어선 자",     "강화 보스 1회 처치",                lambda s, i: s["elite_boss_kills"],     1,        hide="name"),
    _ach("a_elite_all",  "심화", "재앙의 지배자",        "강화 보스 3종의 전리품을 모두 획득", lambda s, i: sum(1 for t in ELITE_TROPHY_STATS if t in (i.get("inventory") or {})), 3, hide="full"),
    _ach("a_boss_5",     "심화", "종말의 사냥꾼",        "보스 5회 처치",                     lambda s, i: s["boss_kills"],           5,        hide="full"),
    _ach("a_craft_all",  "심화", "완전한 장인",          "/제작 장비를 전부 제작",            lambda s, i: _inv_count(i, "[제작]"),   _CRAFT_ALL_GOAL, hide="full"),
    _ach("a_raid_king",  "심화", "지하의 왕을 끌어내린 자", "'칼렌데 지하 황제' 레이드 클리어",   lambda s, i: s["raid_king_clears"],     1,        hide="name"),
    _ach("a_duel_50",    "심화", "결투장의 지배자",      "결투 50회 승리",                    lambda s, i: s["duel_wins"],            50,       hide="full"),
    _ach("a_war_conq_5", "심화", "도시의 새 주인",       "구역 점령전에서 구역을 5회 빼앗음", lambda s, i: s["war_conquests"],        5,        hide="full"),
]
# ── 경찰 업적 (role="police": 경찰에게만 보임) ──
#   검거·순찰·심문·수사팀 작전·월급 평가를 센 카운터를 씁니다. (police_ach_bump 가 올림)
POLICE_BIG_FISH_HONOR = 3000   # '거물 수배범' 기준: 검거 대상의 명예가 이 값 이상
_police_ach = [
    # 기초
    _ach("p_arrest_1",     "기초", "첫 수갑",             "지명수배범 1회 검거",                    lambda s, i: s["police_arrests"],        1),
    _ach("p_patrol_5",     "기초", "순찰 순경",           "순찰 5회 완료",                          lambda s, i: s["police_patrols"],        5),
    _ach("p_interrogate_1","기초", "첫 심문",             "심문 재판 1회 완료",                     lambda s, i: s["police_interrogations"], 1),
    # 숙련
    _ach("p_arrest_10",    "숙련", "수배범 사냥꾼",       "지명수배범 10회 검거",                   lambda s, i: s["police_arrests"],        10),
    _ach("p_patrol_30",    "숙련", "거리의 파수꾼",       "순찰 30회 완료",                         lambda s, i: s["police_patrols"],        30),
    _ach("p_match_10",     "숙련", "정확한 눈",           "심문 재판 결과가 내 의견과 10회 일치",   lambda s, i: s["police_matches"],        10),
    _ach("p_op_10",        "숙련", "합동 작전의 주역",    "수사팀 작전에서 성공 단계 10개 누적",    lambda s, i: s["police_op_stages"],      10),
    _ach("p_salary_s_1",   "숙련", "우수 근무자",         "/월급 평가 S(우수) 1회",                 lambda s, i: s["police_salary_s"],       1),
    # 심화
    _ach("p_arrest_50",    "심화", "수배범의 악몽",       "지명수배범 50회 검거",                   lambda s, i: s["police_arrests"],        50,   hide="name"),
    _ach("p_interrogate_30","심화", "정의의 저울",        "심문 재판 30회 완료",                    lambda s, i: s["police_interrogations"], 30,   hide="name"),
    _ach("p_big_arrest_3", "심화", "거물 사냥꾼",         f"명예 {POLICE_BIG_FISH_HONOR:,}pt 이상 거물 수배범 3회 검거", lambda s, i: s["police_big_arrests"], 3, hide="full"),
    _ach("p_salary_s_10",  "심화", "모범 수사관",         "/월급 평가 S(우수) 10회",                lambda s, i: s["police_salary_s"],       10,   hide="full"),
    _ach("p_ops_1",        "기초", "첫 작전 완수",         "/작전 1회 성공",                         lambda s, i: s["police_ops"],            1),
    _ach("p_ops_30",       "숙련", "작전 베테랑",          "/작전 30회 성공",                        lambda s, i: s["police_ops"],            30),
    _ach("p_ops_150",      "심화", "특수 작전의 전설",     "/작전 150회 성공",                       lambda s, i: s["police_ops"],            150,  hide="name"),
    _ach("p_ops_boss_3",   "심화", "거물 소탕 작전",       "'거물 마피아 보스 소탕 작전' 3회 성공",  lambda s, i: s["police_ops_boss"],       3,    hide="full"),
    _ach("p_op_50",        "심화", "작전 총사령관",       "수사팀 작전에서 성공 단계 50개 누적",    lambda s, i: s["police_op_stages"],      50,   hide="full"),
]
for _a in _police_ach:
    _a["role"] = "police"
ACHIEVEMENTS += _police_ach

# 조직원 전용 명령(/임무 /전투 /보스 /네임드 /결투 /무기상점, 조직·레이드·구역, 일일 의뢰)으로만 달성되는 업적
_MAFIA_ONLY_ACH = {
    "b_mission_1", "b_mission_10", "b_fight_5", "b_weapon_1", "b_duel_1", "b_raid_1", "b_attend_3",
    "s_mission_50", "s_streak_5", "s_hard_5", "s_fight_50", "s_named_2", "s_duel_10", "s_raid_10", "s_war_3",
    "a_boss_1", "a_elite_1", "a_elite_all", "a_named_all", "a_mission_300", "a_attend_30", "a_streak_10", "a_boss_5", "a_raid_king", "a_duel_50", "a_war_conq_5",
}
for _a in ACHIEVEMENTS:
    _a.setdefault("role", "mafia" if _a["id"] in _MAFIA_ONLY_ACH else None)   # None = 공통

def ach_pool(is_police: bool) -> list:
    """이 유저에게 보여줄 업적 목록 (경찰은 경찰·공통, 그 외는 조직원·공통). 이미 달성한 것은 항상 보이도록 호출부에서 처리"""
    want = "police" if is_police else "mafia"
    return [a for a in ACHIEVEMENTS if a["role"] in (None, want)]

ACH_BY_ID = {a["id"]: a for a in ACHIEVEMENTS}

# ── 업적 보상 (/업적 의 [보상 수령] 버튼으로 받습니다) ──
#   등급별 기본 보상. 특정 업적만 다르게 주고 싶으면 ACH_REWARD_OVERRIDES 에 {"업적id": {"money": .., "honor": .., "title": True}} 추가
#   "title": True 이면 그 업적 이름의 전용 칭호가 함께 지급됩니다. (/칭호 에서 장착)
ACH_REWARDS = {
    "기초": {"money": 1000,  "honor": 0},
    "숙련": {"money": 5000,  "honor": 10},
    "심화": {"money": 30000, "honor": 50, "title": True},
}
ACH_REWARD_OVERRIDES = {   # 강화 보스 업적은 일반 심화 업적보다 보상이 큽니다
    "a_elite_1":   {"money": 100000, "honor": 150, "title": True},
    "a_elite_all": {"money": 300000, "honor": 400, "title": True},
}

def ach_reward(a: dict) -> dict:
    return ACH_REWARD_OVERRIDES.get(a["id"]) or ACH_REWARDS[a["tier"]]

def ach_title_id(a: dict):
    """전용 칭호 id (없으면 None)"""
    return "ach_" + a["id"] if ach_reward(a).get("title") else None

for _a in ACHIEVEMENTS:             # 업적 전용 칭호를 칭호 목록에 등록 (check=False → 수령 때만 지급)
    _tid = ach_title_id(_a)
    if _tid:
        TITLES[_tid] = {"name": _a["name"], "desc": f"{_a['tier']} 업적 달성 기념 칭호", "check": lambda s, i: False}
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

async def police_ach_bump(user_id: int, pending: bool = False, **incs) -> str:
    """경찰 활동 카운터를 올리고 새로 달성한 업적이 있으면 결과 메시지에 붙일 안내 문구를 돌려준다.
    (대기 중이던 알림도 함께 표시. 실패해도 명령어 처리는 막지 않고 빈 문자열을 돌려줌)
    pending=True 이면 안내를 바로 표시하지 않고 그 유저의 다음 경찰 활동·/업적 화면에서 보여준다."""
    incs = {k: int(v) for k, v in incs.items() if v}
    if not incs:
        return ""
    try:
        new_ids = await asyncio.to_thread(bump_user_stats_sync, user_id, incs, pending)
        if pending:
            return ""
        out = _NewIds(list(new_ids))
        out.ach = list(_pending_ach.pop(str(user_id), [])) + list(new_ids.ach)
        return format_new_titles(out)
    except Exception as e:
        print(f"[경찰 업적 카운터 오류] user={user_id}: {e}")
        return ""

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
    from .admin import check_maintenance, check_membership  # 순환 import 방지: 호출 시점에 가져옴
    if not await check_maintenance(interaction): return
    if not await check_membership(interaction): return
    if await check_jail(interaction): return

    user = interaction.user
    blob = get_title_blob(get_user_data(user.id))
    await interaction.response.send_message(embed=build_daily_embed(user, blob), ephemeral=True)

# ==========================================
# 주간 의뢰 (/주간)
#   blob["weekly"] = {"week": 주 식별자, "base": {카운터: 주 시작 때 값}, "claimed": [수령한 의뢰 key], "bonus": 보너스 수령 여부}
# ==========================================
def weekly_rows(blob: dict, role: str) -> list:
    """[{q, cur, done, claimed}] — 진행도는 (현재 통계 - 주 시작 기준점) 의 합"""
    w, stats = blob["weekly"], blob["stats"]
    rows = []
    for q in WEEKLY_QUESTS[role]:
        cur = sum(max(0, stats.get(c, 0) - w["base"].get(c, 0)) for c in q["counters"])
        rows.append({"q": q, "cur": min(cur, q["target"]), "done": cur >= q["target"], "claimed": q["key"] in w["claimed"]})
    return rows

def weekly_claimable(rows: list) -> list:
    return [r for r in rows if r["done"] and not r["claimed"]]

def weekly_role(member) -> str:
    from .police_unit import _is_police  # 순환 import 방지: 호출 시점에 가져옴
    return "police" if _is_police(member) else "mafia"

def claim_weekly_sync(user_id: int, role: str) -> dict:
    """완료한 의뢰의 보상을 한 번에 지급 (이미 받은 의뢰는 제외). DB 작업이 있어 to_thread 로 실행합니다."""
    info = get_user_data(user_id)
    blob = get_title_blob(info)
    w = blob["weekly"]
    rows = weekly_rows(blob, role)
    todo = weekly_claimable(rows)
    if not todo:
        return {"ok": False, "msg": "지금 받을 수 있는 주간 보상이 없습니다."}

    money = sum(r["q"]["money"] for r in todo)
    honor = sum(r["q"]["honor"] for r in todo)
    lines = [f"• {r['q']['name']}: `${r['q']['money']:,}`" + (f" / 명예 `+{r['q']['honor']}`" if r["q"]["honor"] else "") for r in todo]
    backup = (list(w["claimed"]), w["bonus"], blob["stats"]["weekly_clears"])
    w["claimed"] += [r["q"]["key"] for r in todo]

    all_cleared = all(r["done"] for r in rows) and all(r["q"]["key"] in w["claimed"] for r in rows)
    if all_cleared and not w["bonus"]:
        w["bonus"] = True
        blob["stats"]["weekly_clears"] += 1
        money += WEEKLY_ALL_CLEAR_BONUS["money"]
        honor += WEEKLY_ALL_CLEAR_BONUS["honor"]
        lines.append(f"• 전부 완료 보너스: `${WEEKLY_ALL_CLEAR_BONUS['money']:,}` / 명예 `+{WEEKLY_ALL_CLEAR_BONUS['honor']}`")

    save_title_blob(user_id, blob)               # 수령 기록을 먼저 저장 (중복 수령 방지)
    try:
        update_user_stats(user_id, money, honor)
    except Exception:
        w["claimed"], w["bonus"], blob["stats"]["weekly_clears"] = backup   # 지급에 실패하면 수령 기록을 되돌림
        save_title_blob(user_id, blob)
        raise

    new_ids = evaluate_new_titles(blob, {**info, "money": info["money"] + money, "honor": info["honor"] + honor})
    if new_ids:
        save_title_blob(user_id, blob)
    return {"ok": True, "money": money, "honor": honor, "lines": lines, "titles": format_new_titles(new_ids)}

def build_weekly_embed(user: discord.abc.User, blob: dict, role: str, notice: str = "") -> discord.Embed:
    _, start, end = weekly_period()
    rows = weekly_rows(blob, role)
    done_cnt = sum(1 for r in rows if r["done"])
    lines = []
    for r in rows:
        q = r["q"]
        mark = "[완료]" if r["done"] else "[ ]"
        reward = f"${q['money']:,}" + (f" / 명예 +{q['honor']}" if q["honor"] else "")
        tail = " (수령 완료)" if r["claimed"] else ""
        lines.append(f"{mark} **{q['name']}** — `{r['cur']}/{q['target']}`  ({q['hint']})\n    보상: {reward}{tail}")

    claimable = len(weekly_claimable(rows))
    all_claimed = all(r["claimed"] for r in rows)
    if blob["weekly"]["bonus"]:
        status = "이번 주 의뢰를 모두 완료하고 보상도 모두 받았습니다."
        color = discord.Color.green()
    elif claimable:
        status = f"받을 수 있는 보상이 **{claimable}개** 있습니다. 아래 버튼을 눌러 받으세요."
        color = discord.Color.gold()
    else:
        status = f"이번 주 의뢰 {done_cnt}/{len(rows)} 완료"
        color = discord.Color.blurple()

    embed = discord.Embed(
        title=f"[{user.name} 님의 주간 의뢰]",
        description=f"{notice}{status}\n기간: <t:{start}:D> ~ <t:{end}:D>  (초기화 <t:{end}:R>)",
        color=color)
    embed.add_field(name="이번 주 의뢰", value="\n".join(lines), inline=False)
    embed.add_field(
        name="전부 완료 보너스",
        value=f"${WEEKLY_ALL_CLEAR_BONUS['money']:,} / 명예 +{WEEKLY_ALL_CLEAR_BONUS['honor']}" + (" (수령 완료)" if blob["weekly"]["bonus"] else ""),
        inline=False)
    clears = blob["stats"]["weekly_clears"]
    if "weekly_master" in blob["titles"]:
        goal = f"{TITLES['weekly_master']['name']} 칭호 획득 완료!"
    else:
        goal = f"{WEEKLY_TITLE_WEEKS}주 완료 시 {TITLES['weekly_master']['name']} 칭호 (현재 {clears}/{WEEKLY_TITLE_WEEKS}주)"
    embed.add_field(name="주간 완료 기록", value=goal, inline=False)
    embed.set_footer(text="매주 월요일 00:00(한국 시간)에 새 의뢰가 시작됩니다. 진행도는 이번 주 활동만 집계돼요.")
    if user.avatar:
        embed.set_thumbnail(url=user.avatar.url)
    return embed

class WeeklyView(discord.ui.View):
    def __init__(self, owner: discord.abc.User, role: str, claimable: int):
        super().__init__(timeout=180)
        self.owner = owner
        self.role = role
        self.claim_btn.label = f"보상 받기 ({claimable})" if claimable else "받을 보상 없음"
        self.claim_btn.disabled = claimable == 0

    @discord.ui.button(label="보상 받기", style=discord.ButtonStyle.success)
    async def claim_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner.id:
            await interaction.response.send_message("본인의 주간 의뢰 창만 조작할 수 있습니다.", ephemeral=True)
            return
        await interaction.response.defer()
        try:
            result = await asyncio.to_thread(claim_weekly_sync, self.owner.id, self.role)
        except Exception as e:
            print(f"DB Error (weekly claim): {e}")
            await interaction.followup.send("보상 지급 중 오류가 발생했습니다. 잠시 뒤 다시 시도해 주세요.", ephemeral=True)
            return

        info = get_user_data(self.owner.id)
        blob = get_title_blob(info)
        if result["ok"]:
            notice = ("**[주간 보상 수령]**\n" + "\n".join(result["lines"])
                      + f"\n합계: `${result['money']:,}` / 명예 `+{result['honor']}`" + result["titles"] + "\n\n")
        else:
            notice = result["msg"] + "\n\n"
        rows = weekly_rows(blob, self.role)
        await interaction.edit_original_response(
            embed=build_weekly_embed(self.owner, blob, self.role, notice),
            view=WeeklyView(self.owner, self.role, len(weekly_claimable(rows))))

@bot.tree.command(name="주간", description="[마피아 GM / 경찰] 매주 월요일에 초기화되는 주간 의뢰를 확인하고 보상을 받습니다.")
async def weekly_quest_command(interaction: discord.Interaction):
    from .admin import check_maintenance, check_membership  # 순환 import 방지: 호출 시점에 가져옴
    if not await check_maintenance(interaction): return
    if not await check_membership(interaction): return
    if await check_jail(interaction): return

    user = interaction.user
    role = weekly_role(user)
    blob = get_title_blob(get_user_data(user.id))
    claimable = len(weekly_claimable(weekly_rows(blob, role)))
    await interaction.response.send_message(
        embed=build_weekly_embed(user, blob, role),
        view=WeeklyView(user, role, claimable), ephemeral=True)

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
    from .admin import check_maintenance, check_membership  # 순환 import 방지: 호출 시점에 가져옴


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

def ach_overview_embed(user: discord.abc.User, blob: dict, info: dict, new_ids=None, is_police: bool = False) -> discord.Embed:
    done = blob["achievements"]
    pool = ach_pool(is_police)
    total_done = sum(1 for a in pool if a["id"] in done)
    lines = []
    for tier in ACH_TIERS:
        items = [a for a in pool if a["tier"] == tier]
        n = sum(1 for a in items if a["id"] in done)
        lines.append(f"**{tier}**  `{_ach_bar(n, len(items))}` {n} / {len(items)}")

    unclaimed = _ach_unclaimed(blob)
    claim_txt = f"\n**수령 가능한 보상 {len(unclaimed)}개** — 아래 [보상 수령] 버튼을 눌러주세요." if unclaimed else ""

    recent = sorted((a for a in ACHIEVEMENTS if a["id"] in done), key=lambda a: done[a["id"]], reverse=True)[:5]
    recent_txt = "\n".join(f"• [{a['tier']}] {a['name']} · <t:{int(done[a['id']])}:R>" for a in recent) or "*아직 달성한 업적이 없습니다.*"

    embed = discord.Embed(
        title=f"[{user.display_name} 님의 업적]",
        description=(f"달성 **{total_done} / {len(pool)}**\n\n" + "\n".join(lines) + claim_txt
                     + "\n\n버튼으로 등급별 업적을 확인하고, 아래 메뉴에서 달성한 업적의 **증명서**를 발급받을 수 있습니다.\n"
                       "기초·숙련은 조건이 공개되고, **심화**는 달성해야 모든 내용이 드러납니다."
                     + format_new_titles(new_ids or [])),
        color=discord.Color.gold()
    )
    embed.add_field(name="최근 달성", value=recent_txt, inline=False)
    if user.avatar:
        embed.set_thumbnail(url=user.avatar.url)
    return embed

def ach_tier_embed(user: discord.abc.User, tier: str, blob: dict, info: dict, is_police: bool = False) -> discord.Embed:
    done = blob["achievements"]
    # 내 역할에 맞는 업적 + (역할이 바뀌었더라도) 이미 달성한 업적
    items = [a for a in ACHIEVEMENTS if a["tier"] == tier and (a in ach_pool(is_police) or a["id"] in done)]
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
        from .economy import make_achievement_certificate  # 순환 import 방지: 호출 시점에 가져옴
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
    def __init__(self, owner: discord.abc.User, blob: dict, info: dict, is_police: bool = False):
        super().__init__(timeout=180)
        self.owner, self.blob, self.info = owner, blob, info
        self.is_police = is_police
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
            return ach_overview_embed(self.owner, self.blob, self.info, is_police=self.is_police)
        return ach_tier_embed(self.owner, self.current, self.blob, self.info, is_police=self.is_police)

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
    from .police_unit import _is_police  # 순환 import 방지: 호출 시점에 가져옴
    from .admin import check_maintenance, check_membership  # 순환 import 방지: 호출 시점에 가져옴
    if not await check_maintenance(interaction): return
    if not await check_membership(interaction): return

    await interaction.response.defer()
    user = interaction.user
    info, blob, new_ids = await asyncio.to_thread(_load_achievements_sync, user.id)
    is_police = _is_police(user)
    view = AchievementView(user, blob, info, is_police=is_police)
    view.interaction = interaction
    await interaction.followup.send(embed=ach_overview_embed(user, blob, info, new_ids, is_police=is_police), view=view)

MATERIAL_PREFIX = "[재료] "

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
