"""성향(선/악) 시스템, 전직 조건, 부패 경찰"""

import discord

from .core import inc_range, supabase
from .status import (
    ATTENDANCE_TITLE_STACKS,
    ELITE_TROPHY_STATS,
    WEEKLY_COUNTERS,
    WEEKLY_TITLE_WEEKS,
    _check_updated,
    _require_db,
    weekly_period,
)

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
    from .achievements import evaluate_new_achievements   # 순환 import 방지: 호출 시점에 가져옴
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
    from .achievements import ACH_BY_ID   # 순환 import 방지: 호출 시점에 가져옴
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
