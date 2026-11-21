"""업적 시스템 (/업적, /일퀘, /주간, /칭호)"""

import asyncio
import discord
import threading as _ach_threading
import time

from .core import bot
from .status import (
    ATTENDANCE_TITLE_STACKS,
    DAILY_COOLDOWN_SEC,
    DAILY_QUESTS,
    ELITE_TROPHY_STATS,
    WEAPON_TIERS,
    WEEKLY_ALL_CLEAR_BONUS,
    WEEKLY_QUESTS,
    WEEKLY_TITLE_WEEKS,
    _require_db,
    check_jail,
    get_user_data,
    update_user_stats,
    weekly_period,
)
from .alignment import (
    TITLES,
    _NewIds,
    _pending_ach,
    evaluate_new_titles,
    format_new_titles,
    get_title_blob,
    save_title_blob,
)

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
    from .admin import check_maintenance, check_membership   # 순환 import 방지: 호출 시점에 가져옴
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
    from .police_unit import _is_police   # 순환 import 방지: 호출 시점에 가져옴
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
    from .admin import check_maintenance, check_membership   # 순환 import 방지: 호출 시점에 가져옴
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
    from .admin import check_maintenance, check_membership   # 순환 import 방지: 호출 시점에 가져옴


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
        from .profile import make_achievement_certificate   # 순환 import 방지: 호출 시점에 가져옴
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
    from .police_unit import _is_police   # 순환 import 방지: 호출 시점에 가져옴
    from .admin import check_maintenance, check_membership   # 순환 import 방지: 호출 시점에 가져옴
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
