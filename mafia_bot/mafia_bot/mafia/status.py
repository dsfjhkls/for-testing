"""감옥·수배 상태 저장/복구, 유저 데이터 접근 함수"""

import asyncio
import discord
import re
import threading as _status_threading
import time
from datetime import datetime, timedelta, timezone

from .core import inc_range, jail_list, supabase, wanted_list

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
    from .crafting import crafted_gear_best   # 순환 import 방지: 호출 시점에 가져옴
    from .profile import POLICE_SHOP_ITEMS   # 순환 import 방지: 호출 시점에 가져옴
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
