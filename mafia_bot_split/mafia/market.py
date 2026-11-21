"""사업·주식 시장과 봇 이벤트(on_ready)

합친 모듈: biz_stock.py, events.py
"""

import asyncio
import discord
import math as _math
import random
import re
import traceback as _traceback
from datetime import datetime, timezone
from discord import app_commands

from .core import bot, inc_money, keep_task, load_boss_state_sync, supabase
from .status import (
    _check_updated,
    _require_db,
    check_jail,
    get_user_data,
    load_status_sync,
    update_user_stats,
)
from .admin import _loop_exception_handler, check_maintenance, check_membership


# ==========================================================================
# [합친 모듈] biz_stock.py
#   사업·주식 (/사업 /주식)
# ==========================================================================
# ── 사업 설정 ──
#   cost   : 창업 비용($)           income : 1레벨 기준 시간당 수익 (INCOME_MULTIPLIER 반영)
#   vol    : 상장 후 주가 변동폭     var    : 수금할 때 수익이 흔들리는 범위 (최소배율, 최대배율)
BUSINESS_SECTORS = {
    "주점":       {"cost": 8000,  "income": inc_money(500),  "vol": 0.020, "var": (1.0, 1.0),
                   "desc": "블랙워드 뒷골목의 선술집. 수익이 꾸준하고 안정적입니다."},
    "정보 중개소": {"cost": 12000, "income": inc_money(800),  "vol": 0.030, "var": (0.9, 1.1),
                   "desc": "소문과 비밀을 사고파는 가게. 수익이 조금씩 흔들립니다."},
    "무기 공방":   {"cost": 20000, "income": inc_money(1400), "vol": 0.030, "var": (0.85, 1.2),
                   "desc": "던베르크의 대장간. 큰 수주가 들어오면 수익이 뜁니다."},
    "지하 카지노": {"cost": 30000, "income": inc_money(2200), "vol": 0.050, "var": (0.5, 1.6),
                   "desc": "로젠펠트의 비밀 도박장. 대박과 쪽박이 갈리는 고위험 업종입니다."},
}
BUSINESS_MAX_LEVEL = 5
BUSINESS_LEVEL_INCOME_BONUS = 0.6      # 레벨이 1 오를 때마다 기본 수익의 +60%
BUSINESS_UPGRADE_COST_RATE = 0.8       # 강화 비용 = 창업비용 x 0.8 x 현재 레벨
BUSINESS_INCOME_CAP_HOURS = 12         # 수금을 안 해도 쌓이는 최대 시간
BUSINESS_CLOSE_REFUND_RATE = 0.3       # 폐업 시 투자금 환급 비율
BUSINESS_NAME_PATTERN = re.compile(r"^[가-힣A-Za-z0-9 ]{2,12}$")
BUSINESS_DB_ERROR_MSG = "사업 DB 처리 중 오류가 발생했습니다. (관리자: `companies` / `stocks` / `stock_holdings` 테이블이 만들어져 있는지 확인해 주세요 — stocks.sql 참고)"

# ── 주식 설정 ──
STOCK_TICK_SEC = 300                   # 시세 변동 주기 (5분)
STOCK_FEE_RATE = 0.01                  # 매수·매도 수수료 (각각 1%)
STOCK_IPO_MIN_LEVEL = 2                # 상장 가능한 최소 사업 레벨
STOCK_IPO_FEE_RATE = 0.05              # 상장 때 공모 대금에서 떼는 수수료
STOCK_TOTAL_SHARES = 100               # 상장 회사의 총 발행 주식 수
STOCK_IPO_FLOAT = 40                   # 상장 때 시장에 푸는 물량 (나머지 60주는 대표가 보유)
STOCK_OWNER_MIN_STAKE = 51             # 대표가 유지해야 하는 최소 지분(%)
STOCK_REVERT_K = 0.03                  # 적정가로 되돌아가려는 힘 (클수록 덜 흔들림)
STOCK_MAX_MOVE = 0.15                  # 한 번에 움직일 수 있는 최대 비율 (±15%)
STOCK_EVENT_CHANCE = 0.20              # 시세 변동 때 세계관 뉴스가 터질 확률
STOCK_HISTORY_LEN = 24                 # 저장해 두는 가격 기록 개수
STOCK_MAX_QTY = 100000

WORLD_STOCKS = {
    "LHB": {"name": "레벤하임 중앙은행", "price": 500, "vol": 0.010, "desc": "도시 금융의 심장. 느리지만 든든한 종목."},
    "LDI": {"name": "레벤 방위산업",     "price": 250, "vol": 0.022, "desc": "던베르크 병기 공장 연합. 수주와 전쟁 소식에 민감."},
    "SHD": {"name": "그림자 해운",       "price": 80,  "vol": 0.045, "desc": "에르느코항을 오가는 수상한 화물 회사. 단속 한 번에 요동친다."},
}

# 세계관 뉴스: 터지면 해당 종목에 pct 만큼 충격이 더해집니다.
STOCK_EVENTS = [
    {"symbol": "SHD", "pct": -0.12, "text": "치안국이 에르느코항 일대 대규모 단속에 나섰습니다. 그림자 해운 주가가 급락합니다!"},
    {"symbol": "SHD", "pct": +0.10, "text": "새 밀수 항로가 뚫렸다는 소문이 돕니다. 그림자 해운 주가가 급등합니다!"},
    {"symbol": "LDI", "pct": +0.10, "text": "던베르크 병기 공장이 대형 수주를 따냈습니다. 레벤 방위산업이 치솟습니다!"},
    {"symbol": "LDI", "pct": -0.08, "text": "병기 공장 사고 보도가 나왔습니다. 레벤 방위산업이 흔들립니다."},
    {"symbol": "LHB", "pct": +0.05, "text": "중앙은행이 금리 동결을 발표했습니다. 레벤하임 중앙은행이 소폭 오릅니다."},
    {"symbol": "LHB", "pct": -0.05, "text": "블랙워드 부실 대출 의혹이 불거졌습니다. 레벤하임 중앙은행이 밀립니다."},
]

_STOCK_CACHE = {}          # {symbol: 종목 row}  (자동완성/시세 표시용, 시세 변동·거래 때마다 갱신)
_stock_trend = {}          # {symbol: 직전 추세}  (봇을 재시작하면 초기화)
_stock_news = []           # [(시각, 문구)]  최근 뉴스 3개
_stock_last_tick = None    # 마지막으로 시세를 변동한 시각 (UTC)


class StockError(Exception):
    """유저에게 그대로 보여줄 수 있는 거래/사업 오류"""


def _now_utc():
    return datetime.now(timezone.utc)

def _parse_ts(value):
    """Supabase timestamptz 문자열 → aware datetime (실패하면 None)"""
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    s = str(value).strip().replace("Z", "+00:00")
    m = re.match(r"^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})(\.\d+)?(.*)$", s)
    if not m:
        return None
    base, frac, tz = m.group(1), m.group(2) or "", m.group(3) or "+00:00"
    frac = (frac + "000000")[:7] if frac else ""
    try:
        dt = datetime.fromisoformat(base.replace(" ", "T") + frac + tz)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ───────────── 사업 계산 ─────────────
def biz_sector(co: dict) -> dict:
    return BUSINESS_SECTORS.get(co.get("sector"), next(iter(BUSINESS_SECTORS.values())))

def biz_income_per_hour(co: dict) -> int:
    lv = max(1, int(co.get("level") or 1))
    return int(biz_sector(co)["income"] * (1 + BUSINESS_LEVEL_INCOME_BONUS * (lv - 1)))

def biz_upgrade_cost(co: dict) -> int:
    return int(biz_sector(co)["cost"] * BUSINESS_UPGRADE_COST_RATE * max(1, int(co.get("level") or 1)))

def biz_valuation(co: dict) -> int:
    """회사 가치 = 투자금 x 1.3 + 시간당 수익 x 10  (상장가·적정 주가의 기준)"""
    return int(int(co.get("invested") or 0) * 1.3 + biz_income_per_hour(co) * 10)

def biz_pending(co: dict, now=None):
    """(쌓인 수익, 쌓인 시간) — 수금 안 해도 최대 BUSINESS_INCOME_CAP_HOURS 까지만 쌓임"""
    now = now or _now_utc()
    last = _parse_ts(co.get("last_collect")) or now
    hours = max(0.0, min((now - last).total_seconds() / 3600, BUSINESS_INCOME_CAP_HOURS))
    return int(biz_income_per_hour(co) * hours), hours

def biz_roll_income(co: dict, base_amount: int) -> int:
    lo, hi = biz_sector(co)["var"]
    if lo == hi:
        return int(base_amount * lo)
    return int(base_amount * random.uniform(lo, hi))

def stock_ticker_for(company_id) -> str:
    return f"B{company_id}"

def spark(history, n: int = 12) -> str:
    h = [int(x) for x in (history or [])][-n:]
    if len(h) < 2:
        return ""
    lo, hi = min(h), max(h)
    bars = "▁▂▃▄▅▆▇█"
    if hi == lo:
        return bars[3] * len(h)
    return "".join(bars[int((x - lo) / (hi - lo) * (len(bars) - 1))] for x in h)


# ───────────── 사업 DB ─────────────
def _biz_get_by_owner(uid):
    _require_db()
    res = supabase.table("companies").select("*").eq("owner_id", str(uid)).limit(1).execute()
    return res.data[0] if res.data else None

def _biz_get_by_name(name: str):
    _require_db()
    res = supabase.table("companies").select("*").eq("name", name).limit(1).execute()
    return res.data[0] if res.data else None

def _biz_get_by_id(cid):
    _require_db()
    res = supabase.table("companies").select("*").eq("id", cid).limit(1).execute()
    return res.data[0] if res.data else None

def _biz_all():
    _require_db()
    return supabase.table("companies").select("*").execute().data or []

def _biz_insert(row: dict) -> dict:
    _require_db()
    res = supabase.table("companies").insert(row).execute()
    _check_updated(res, "biz_insert")
    return res.data[0]

def _biz_update(cid, data: dict) -> dict:
    _require_db()
    res = supabase.table("companies").update(data).eq("id", cid).execute()
    _check_updated(res, "biz_update")
    return res.data[0]

def _biz_delete(cid):
    _require_db()
    supabase.table("companies").delete().eq("id", cid).execute()


# ───────────── 주식 DB ─────────────
def _stock_all_sync() -> list:
    _require_db()
    rows = supabase.table("stocks").select("*").execute().data or []
    _STOCK_CACHE.clear()
    for r in rows:
        _STOCK_CACHE[r["symbol"]] = r
    return rows

def _stock_find(query: str):
    """종목 코드(LHB, B3) 또는 이름(일부 가능)으로 종목을 찾는다."""
    q = (query or "").strip()
    if not q:
        return None
    rows = _stock_all_sync()
    for r in rows:
        if r["symbol"].upper() == q.upper():
            return r
    for r in rows:
        if r["name"] == q:
            return r
    hits = [r for r in rows if q.lower() in r["name"].lower()]
    return hits[0] if len(hits) == 1 else None

def _stock_update(symbol: str, data: dict):
    res = supabase.table("stocks").update(data).eq("symbol", symbol).execute()
    _check_updated(res, "stock_update")
    return res.data[0]

def _stock_cas_float(symbol: str, old: int, new: int) -> bool:
    """시장 물량을 '내가 읽은 값 그대로일 때만' 바꾼다 (동시에 여러 명이 거래해도 물량이 꼬이지 않게)."""
    res = supabase.table("stocks").update({"float_shares": new}).eq("symbol", symbol).eq("float_shares", old).execute()
    return bool(res.data)

def _stock_float_add(symbol: str, delta: int):
    """시장 물량을 delta 만큼 늘리거나(+) 줄인다(-). 충돌하면 몇 번 다시 시도."""
    for _ in range(6):
        row = supabase.table("stocks").select("float_shares").eq("symbol", symbol).limit(1).execute().data
        if not row:
            raise StockError("종목이 사라졌습니다.")
        cur = int(row[0].get("float_shares") or 0)
        if cur + delta < 0:
            raise StockError("시장 물량이 부족합니다.")
        if _stock_cas_float(symbol, cur, cur + delta):
            return
    raise StockError("거래가 몰렸습니다. 잠시 후 다시 시도해 주세요.")

def _hold_get(uid, symbol: str):
    res = supabase.table("stock_holdings").select("*").eq("user_id", str(uid)).eq("symbol", symbol).limit(1).execute()
    return res.data[0] if res.data else None

def _hold_set(uid, symbol: str, shares: int, cost: int):
    if shares <= 0:
        supabase.table("stock_holdings").delete().eq("user_id", str(uid)).eq("symbol", symbol).execute()
        return
    res = supabase.table("stock_holdings").upsert(
        {"user_id": str(uid), "symbol": symbol, "shares": int(shares), "cost": int(cost)},
        on_conflict="user_id,symbol").execute()
    _check_updated(res, "hold_set")

def _hold_all(uid) -> list:
    return supabase.table("stock_holdings").select("*").eq("user_id", str(uid)).execute().data or []


# ───────────── 시세 변동 ─────────────
def stock_seed_sync():
    """세계관 종목이 DB에 없으면 만든다."""
    _require_db()
    exist = {r["symbol"] for r in _stock_all_sync()}
    for sym, cfg in WORLD_STOCKS.items():
        if sym in exist:
            continue
        p = int(cfg["price"])
        supabase.table("stocks").insert({
            "symbol": sym, "name": cfg["name"], "kind": "world",
            "price": p, "prev_price": p, "base_price": p,
            "total_shares": 0, "float_shares": 0,
            "history": [p], "updated_at": _now_utc().isoformat(),
        }).execute()
    _stock_all_sync()

def stock_last_tick_sync():
    _require_db()
    res = supabase.table("stocks").select("updated_at").order("updated_at", desc=True).limit(1).execute()
    if res.data:
        return _parse_ts(res.data[0].get("updated_at"))
    return None

def stock_tick_sync() -> int:
    """모든 종목의 가격을 한 번 변동시킨다. 변동시킨 종목 수를 돌려준다."""
    global _stock_last_tick
    _require_db()
    rows = _stock_all_sync()
    companies = {c["id"]: c for c in _biz_all()}

    shock = {}
    if random.random() < STOCK_EVENT_CHANCE:
        ev = random.choice(STOCK_EVENTS)
        shock[ev["symbol"]] = ev["pct"]
        _stock_news.append((_now_utc(), ev["text"]))
        del _stock_news[:-3]

    now_iso = _now_utc().isoformat()
    changed = 0
    for r in rows:
        try:
            sym, price = r["symbol"], max(1, int(r["price"]))
            if r["kind"] == "world":
                cfg = WORLD_STOCKS.get(sym)
                if not cfg:
                    continue
                fair, vol = max(1, int(r["base_price"])), cfg["vol"]
            else:
                co = companies.get(r.get("company_id"))
                if not co:
                    continue
                fair = max(1, biz_valuation(co) // max(1, int(r.get("total_shares") or STOCK_TOTAL_SHARES)))
                vol = biz_sector(co)["vol"]
            trend = _stock_trend.get(sym, 0.0)
            pct = trend + vol * random.gauss(0, 1) - STOCK_REVERT_K * _math.log(price / fair) + shock.get(sym, 0.0)
            pct = max(-STOCK_MAX_MOVE, min(STOCK_MAX_MOVE, pct))
            _stock_trend[sym] = 0.5 * trend + 0.35 * vol * random.gauss(0, 1)
            new = max(1, int(round(price * (1 + pct))))
            if new == price and abs(pct) >= 0.003:      # 싼 종목이 반올림 때문에 멈춰 있지 않게
                new = max(1, price + (1 if pct > 0 else -1))
            if r["kind"] == "world":
                base = int(r["base_price"])
                new = max(max(1, int(base * 0.3)), min(int(base * 3), new))
            hist = list(r.get("history") or [])[-(STOCK_HISTORY_LEN - 1):] + [new]
            supabase.table("stocks").update({
                "price": new, "prev_price": price, "history": hist, "updated_at": now_iso,
            }).eq("symbol", sym).execute()
            changed += 1
        except Exception as e:
            print(f"Stock tick error ({r.get('symbol')}): {e}")
    _stock_last_tick = _now_utc()
    _stock_all_sync()
    return changed


# ───────────── 거래 ─────────────
def stock_buy_sync(uid, query: str, qty: int) -> dict:
    row = _stock_find(query)
    if not row:
        raise StockError("해당 종목을 찾을 수 없습니다. `/주식 시세` 에서 종목 코드를 확인해 주세요.")
    sym, price = row["symbol"], int(row["price"])
    cost = price * qty
    fee = int(_math.ceil(cost * STOCK_FEE_RATE))
    total = cost + fee
    money = int(get_user_data(uid)["money"])
    if money < total:
        raise StockError(f"자금이 부족합니다. (필요 ${total:,} / 보유 ${money:,})")

    company = row["kind"] == "company"
    if company:
        cur = int(row.get("float_shares") or 0)
        if cur < qty:
            raise StockError(f"시장에 풀린 물량이 부족합니다. (남은 물량 {cur:,}주)")
        if not _stock_cas_float(sym, cur, cur - qty):
            raise StockError("다른 사람과 거래가 겹쳤습니다. 다시 시도해 주세요.")
    try:
        update_user_stats(uid, -total, 0)
    except Exception:
        if company:
            _stock_float_add(sym, qty)
        raise
    try:
        h = _hold_get(uid, sym)
        shares = (int(h["shares"]) if h else 0) + qty
        spent = (int(h["cost"]) if h else 0) + cost
        _hold_set(uid, sym, shares, spent)
    except Exception:
        try:
            update_user_stats(uid, total, 0)
            if company:
                _stock_float_add(sym, qty)
        except Exception as e2:
            print(f"Stock buy rollback error: {e2}")
        raise
    return {"row": row, "price": price, "qty": qty, "fee": fee, "total": total, "shares": shares,
            "avg": spent // max(1, shares), "money": money - total}

def stock_sell_sync(uid, query: str, qty: int) -> dict:
    row = _stock_find(query)
    if not row:
        raise StockError("해당 종목을 찾을 수 없습니다. `/주식 시세` 에서 종목 코드를 확인해 주세요.")
    sym, price = row["symbol"], int(row["price"])
    h = _hold_get(uid, sym)
    have = int(h["shares"]) if h else 0
    if have < qty:
        raise StockError(f"보유 수량이 부족합니다. (보유 {have:,}주)")
    company = row["kind"] == "company"
    if company:
        co = _biz_get_by_id(row.get("company_id"))
        if co and str(co["owner_id"]) == str(uid):
            total_sh = int(row.get("total_shares") or STOCK_TOTAL_SHARES)
            keep = int(_math.ceil(total_sh * STOCK_OWNER_MIN_STAKE / 100))
            if have - qty < keep:
                can = max(0, have - keep)
                raise StockError(f"대표는 지분 {STOCK_OWNER_MIN_STAKE}% ({keep}주) 이상을 유지해야 합니다. (지금 팔 수 있는 수량 {can}주)")
    gross = price * qty
    fee = int(_math.ceil(gross * STOCK_FEE_RATE))
    net = gross - fee
    old_cost = int(h["cost"])
    new_shares = have - qty
    new_cost = old_cost * new_shares // have
    basis = old_cost - new_cost            # 팔린 주식의 취득 원가

    _hold_set(uid, sym, new_shares, new_cost)
    float_done = False
    try:
        if company:
            _stock_float_add(sym, qty)
            float_done = True
        update_user_stats(uid, net, 0)
    except Exception:
        try:
            _hold_set(uid, sym, have, old_cost)
            if float_done:
                _stock_float_add(sym, -qty)
        except Exception as e2:
            print(f"Stock sell rollback error: {e2}")
        raise
    return {"row": row, "price": price, "qty": qty, "fee": fee, "net": net, "left": new_shares,
            "profit": net - basis}


# ───────────── 사업 동작 (수금 / 강화 / 상장 / 폐업) ─────────────
def biz_create_sync(uid, owner_name: str, name: str, sector: str) -> dict:
    cfg = BUSINESS_SECTORS[sector]
    if _biz_get_by_owner(uid):
        raise StockError("이미 사업을 운영 중입니다. (사업은 1인 1개) `/사업 정보` 로 확인하세요.")
    if _biz_get_by_name(name):
        raise StockError("이미 같은 이름의 회사가 있습니다.")
    money = int(get_user_data(uid)["money"])
    if money < cfg["cost"]:
        raise StockError(f"창업 자금이 부족합니다. (필요 ${cfg['cost']:,} / 보유 ${money:,})")
    update_user_stats(uid, -cfg["cost"], 0)
    try:
        co = _biz_insert({"owner_id": str(uid), "owner_name": owner_name, "name": name, "sector": sector,
                          "level": 1, "invested": cfg["cost"], "last_collect": _now_utc().isoformat(),
                          "listed": False})
    except Exception:
        try:
            update_user_stats(uid, cfg["cost"], 0)
        except Exception as e2:
            print(f"Biz create refund error: {e2}")
        raise
    return co

def biz_collect_sync(uid) -> dict:
    co = _biz_get_by_owner(uid)
    if not co:
        raise StockError("운영 중인 사업이 없습니다. `/사업 창업` 으로 시작하세요.")
    now = _now_utc()
    base, hours = biz_pending(co, now)
    if base < 1:
        raise StockError("아직 수금할 수익이 없습니다. 조금 더 기다려 주세요.")
    amount = max(1, biz_roll_income(co, base))
    old_ts = co.get("last_collect")
    _biz_update(co["id"], {"last_collect": now.isoformat()})
    try:
        update_user_stats(uid, amount, 0)
    except Exception:
        try:
            _biz_update(co["id"], {"last_collect": old_ts})
        except Exception as e2:
            print(f"Biz collect rollback error: {e2}")
        raise
    return {"co": co, "amount": amount, "base": base, "hours": hours}

def biz_upgrade_sync(uid) -> dict:
    co = _biz_get_by_owner(uid)
    if not co:
        raise StockError("운영 중인 사업이 없습니다.")
    lv = int(co["level"])
    if lv >= BUSINESS_MAX_LEVEL:
        raise StockError(f"이미 최고 레벨(Lv.{BUSINESS_MAX_LEVEL})입니다.")
    cost = biz_upgrade_cost(co)
    now = _now_utc()
    base, _ = biz_pending(co, now)           # 올리기 전 기준으로 밀린 수익을 먼저 정산
    pending = max(0, biz_roll_income(co, base)) if base > 0 else 0
    money = int(get_user_data(uid)["money"])
    if money + pending < cost:
        raise StockError(f"강화 자금이 부족합니다. (필요 ${cost:,} / 보유 ${money:,})")
    old = {"level": co["level"], "invested": co["invested"], "last_collect": co.get("last_collect")}
    new_co = _biz_update(co["id"], {"level": lv + 1, "invested": int(co["invested"]) + cost, "last_collect": now.isoformat()})
    try:
        update_user_stats(uid, pending - cost, 0)
    except Exception:
        try:
            _biz_update(co["id"], old)
        except Exception as e2:
            print(f"Biz upgrade rollback error: {e2}")
        raise
    return {"co": new_co, "cost": cost, "pending": pending}

def biz_ipo_sync(uid) -> dict:
    co = _biz_get_by_owner(uid)
    if not co:
        raise StockError("운영 중인 사업이 없습니다.")
    if co.get("listed"):
        raise StockError("이미 상장된 회사입니다.")
    if int(co["level"]) < STOCK_IPO_MIN_LEVEL:
        raise StockError(f"상장은 사업 Lv.{STOCK_IPO_MIN_LEVEL} 이상부터 가능합니다. `/사업 강화` 로 먼저 키우세요.")
    price = max(10, biz_valuation(co) // STOCK_TOTAL_SHARES)
    float_sh = STOCK_IPO_FLOAT
    owner_sh = STOCK_TOTAL_SHARES - float_sh
    proceeds = int(price * float_sh * (1 - STOCK_IPO_FEE_RATE))
    sym = stock_ticker_for(co["id"])

    _biz_update(co["id"], {"listed": True})
    stock_inserted = False
    try:
        supabase.table("stocks").insert({
            "symbol": sym, "name": co["name"], "kind": "company", "company_id": co["id"],
            "price": price, "prev_price": price, "base_price": price,
            "total_shares": STOCK_TOTAL_SHARES, "float_shares": float_sh,
            "history": [price], "updated_at": _now_utc().isoformat(),
        }).execute()
        stock_inserted = True
        _hold_set(uid, sym, owner_sh, price * owner_sh)
        update_user_stats(uid, proceeds, 0)
    except Exception:
        try:
            if stock_inserted:
                supabase.table("stock_holdings").delete().eq("symbol", sym).execute()
                supabase.table("stocks").delete().eq("symbol", sym).execute()
            _biz_update(co["id"], {"listed": False})
        except Exception as e2:
            print(f"Biz IPO rollback error: {e2}")
        raise
    _stock_all_sync()
    return {"co": co, "symbol": sym, "price": price, "float": float_sh, "owner_shares": owner_sh, "proceeds": proceeds}

def _biz_delist_payout(co: dict, uid) -> dict:
    """상장폐지 정산: 대표 외 주주의 주식을 현재가로 대표가 사들인다."""
    sym = stock_ticker_for(co["id"])
    row = supabase.table("stocks").select("*").eq("symbol", sym).limit(1).execute().data
    if not row:
        return {"symbol": sym, "price": 0, "holders": [], "total": 0}
    price = int(row[0]["price"])
    holds = supabase.table("stock_holdings").select("*").eq("symbol", sym).execute().data or []
    holders = [h for h in holds if str(h["user_id"]) != str(uid) and int(h["shares"]) > 0]
    total = sum(int(h["shares"]) * price for h in holders)
    return {"symbol": sym, "price": price, "holders": holders, "total": total}

def biz_delist_sync(uid) -> dict:
    co = _biz_get_by_owner(uid)
    if not co:
        raise StockError("운영 중인 사업이 없습니다.")
    if not co.get("listed"):
        raise StockError("상장된 회사가 아닙니다.")
    pay = _biz_delist_payout(co, uid)
    money = int(get_user_data(uid)["money"])
    if money < pay["total"]:
        raise StockError(f"상장폐지 정산 자금이 부족합니다. (주주 보상금 ${pay['total']:,} / 보유 ${money:,})")
    paid = 0
    if pay["total"] > 0:
        update_user_stats(uid, -pay["total"], 0)
    for h in pay["holders"]:
        try:
            update_user_stats(int(h["user_id"]), int(h["shares"]) * pay["price"], 0)
            paid += 1
        except Exception as e:
            print(f"Delist payout error (user {h['user_id']}): {e}")
    sym = pay["symbol"]
    supabase.table("stock_holdings").delete().eq("symbol", sym).execute()
    supabase.table("stocks").delete().eq("symbol", sym).execute()
    _biz_update(co["id"], {"listed": False})
    _STOCK_CACHE.pop(sym, None)
    return {"co": co, "price": pay["price"], "total": pay["total"], "holders": len(pay["holders"]), "paid": paid}

def biz_close_sync(uid) -> dict:
    co = _biz_get_by_owner(uid)
    if not co:
        raise StockError("운영 중인 사업이 없습니다.")
    if co.get("listed"):
        raise StockError("상장된 회사는 폐업할 수 없습니다. 먼저 `/사업 상장폐지` 를 하세요.")
    base, _ = biz_pending(co)
    pending = max(0, biz_roll_income(co, base)) if base > 0 else 0
    refund = int(int(co["invested"]) * BUSINESS_CLOSE_REFUND_RATE)
    _biz_delete(co["id"])
    try:
        update_user_stats(uid, refund + pending, 0)
    except Exception:
        print(f"Biz close payout error: user {uid}, {refund + pending}")
        raise
    return {"co": co, "refund": refund, "pending": pending}


# ───────────── 5분마다 시세를 바꾸는 백그라운드 루프 ─────────────
async def stock_market_loop():
    global _stock_last_tick
    await bot.wait_until_ready()
    try:
        await asyncio.to_thread(stock_seed_sync)
        _stock_last_tick = await asyncio.to_thread(stock_last_tick_sync)
        print("주식시장 시작: 종목 %d개" % len(_STOCK_CACHE))
    except Exception as e:
        print(f"주식시장 준비 실패 (stocks 테이블 확인): {e}")
    while not bot.is_closed():
        wait = 15
        try:
            now = _now_utc()
            elapsed = (now - _stock_last_tick).total_seconds() if _stock_last_tick else None
            if elapsed is None or elapsed >= STOCK_TICK_SEC:
                n = await asyncio.to_thread(stock_tick_sync)
                wait = STOCK_TICK_SEC
            else:
                wait = max(5, STOCK_TICK_SEC - elapsed)
        except Exception as e:
            print(f"주식 시세 변동 오류: {e}")
            wait = 60
        await asyncio.sleep(wait)


# ───────────── 명령어 공통 ─────────────
biz_group = app_commands.Group(name="사업", description="사업(회사)을 세워 수익을 내고, 주식시장에 상장합니다.")
bot.tree.add_command(biz_group)
stock_group = app_commands.Group(name="주식", description="레벤하임 주식시장에서 종목을 사고팝니다. (시세 5분마다 변동)")
bot.tree.add_command(stock_group)

async def _biz_precheck(interaction: discord.Interaction) -> bool:
    if not await check_maintenance(interaction): return False
    if not await check_membership(interaction): return False
    if await check_jail(interaction): return False
    return True

async def _biz_run(interaction: discord.Interaction, func, *args):
    """DB 작업을 스레드에서 실행. 실패하면 유저에게 안내하고 None 을 돌려준다. (응답은 미리 defer 해 둘 것)"""
    try:
        return await asyncio.to_thread(func, *args)
    except StockError as e:
        await interaction.followup.send(str(e), ephemeral=True)
    except Exception as e:
        print(f"Biz/Stock error ({getattr(func, '__name__', func)}): {e}")
        _traceback.print_exc()
        await interaction.followup.send(BUSINESS_DB_ERROR_MSG, ephemeral=True)
    return None

async def _announce(interaction: discord.Interaction, text: str):
    try:
        await interaction.channel.send(text)
    except Exception:
        pass

def _pct(price: int, prev: int) -> str:
    if not prev:
        return "0.0%"
    return f"{(price - prev) / prev * 100:+.1f}%"

def _arrow(price: int, prev: int) -> str:
    return "▲" if price > prev else ("▼" if price < prev else "—")

async def _stock_autocomplete(interaction: discord.Interaction, current: str):
    cur = (current or "").strip().lower()
    out = []
    for r in sorted(_STOCK_CACHE.values(), key=lambda x: (x["kind"] != "world", x["symbol"])):
        if cur and cur not in r["symbol"].lower() and cur not in r["name"].lower():
            continue
        out.append(app_commands.Choice(name=f"{r['name']} ({r['symbol']}) ${int(r['price']):,}"[:100], value=r["symbol"]))
    return out[:25]


# ───────────── /사업 ─────────────
def biz_embed(co: dict, stock_row=None) -> discord.Embed:
    sec = biz_sector(co)
    pending, hours = biz_pending(co)
    lv = int(co["level"])
    e = discord.Embed(title=f"[사업] {co['name']}", description=f"*{sec['desc']}*", color=discord.Color.dark_gold())
    e.add_field(name="업종 / 레벨", value=f"{co.get('sector')} / Lv.{lv} (최대 {BUSINESS_MAX_LEVEL})", inline=True)
    e.add_field(name="대표", value=co.get("owner_name") or "-", inline=True)
    e.add_field(name="시간당 수익", value=f"${biz_income_per_hour(co):,}", inline=True)
    lo, hi = sec["var"]
    note = "" if lo == hi else f" (수금 시 x{lo}~x{hi})"
    e.add_field(name="쌓인 수익", value=f"${pending:,} ({hours:.1f}시간 / 최대 {BUSINESS_INCOME_CAP_HOURS}시간){note}", inline=False)
    e.add_field(name="회사 가치", value=f"${biz_valuation(co):,}  (총 투자금 ${int(co['invested']):,})", inline=True)
    if lv < BUSINESS_MAX_LEVEL:
        e.add_field(name="다음 강화 비용", value=f"${biz_upgrade_cost(co):,}", inline=True)
    if co.get("listed") and stock_row:
        e.add_field(name="상장 정보", value=f"`{stock_row['symbol']}` ${int(stock_row['price']):,} ({_pct(int(stock_row['price']), int(stock_row['prev_price']))})\n"
                                          f"시장 물량 {int(stock_row.get('float_shares') or 0)}/{int(stock_row.get('total_shares') or 0)}주", inline=False)
    elif co.get("listed"):
        e.add_field(name="상장 정보", value="상장됨", inline=False)
    else:
        e.set_footer(text=f"Lv.{STOCK_IPO_MIN_LEVEL} 이상이면 /사업 상장 으로 주식시장에 올릴 수 있습니다.")
    return e

@biz_group.command(name="창업", description="사업(회사)을 세웁니다. (1인 1개)")
@app_commands.describe(이름="회사 이름 (한글·영문·숫자·띄어쓰기 2~12자)", 업종="어떤 사업을 할까요?")
@app_commands.choices(업종=[app_commands.Choice(name=f"{k} (창업 ${v['cost']:,} / 시간당 ${v['income']:,})", value=k) for k, v in BUSINESS_SECTORS.items()])
async def biz_create_cmd(interaction: discord.Interaction, 이름: str, 업종: app_commands.Choice[str]):
    if not await _biz_precheck(interaction): return
    name = 이름.strip()
    if not BUSINESS_NAME_PATTERN.match(name):
        await interaction.response.send_message("회사 이름은 한글·영문·숫자·띄어쓰기로 **2~12자**여야 합니다.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    co = await _biz_run(interaction, biz_create_sync, interaction.user.id, interaction.user.display_name, name, 업종.value)
    if not co: return
    await interaction.followup.send(embed=biz_embed(co), ephemeral=True)
    await _announce(interaction, f"**{interaction.user.display_name}** 님이 {co['sector']} **{co['name']}** 을(를) 창업했습니다!")

@biz_group.command(name="정보", description="내 사업의 상태와 쌓인 수익을 확인합니다.")
async def biz_info_cmd(interaction: discord.Interaction):
    if not await _biz_precheck(interaction): return
    await interaction.response.defer(ephemeral=True)
    def work():
        co = _biz_get_by_owner(interaction.user.id)
        if not co:
            raise StockError("운영 중인 사업이 없습니다. `/사업 창업` 으로 시작하세요.")
        row = None
        if co.get("listed"):
            rows = supabase.table("stocks").select("*").eq("symbol", stock_ticker_for(co["id"])).limit(1).execute().data
            row = rows[0] if rows else None
        return co, row
    res = await _biz_run(interaction, work)
    if not res: return
    await interaction.followup.send(embed=biz_embed(*res), ephemeral=True)

@biz_group.command(name="수금", description="사업에서 쌓인 수익을 거둡니다.")
async def biz_collect_cmd(interaction: discord.Interaction):
    if not await _biz_precheck(interaction): return
    await interaction.response.defer(ephemeral=True)
    r = await _biz_run(interaction, biz_collect_sync, interaction.user.id)
    if not r: return
    lo, hi = biz_sector(r["co"])["var"]
    extra = ""
    if lo != hi:
        extra = " (대박!)" if r["amount"] > r["base"] * 1.15 else (" (손님이 뜸했습니다...)" if r["amount"] < r["base"] * 0.85 else "")
    await interaction.followup.send(f"**{r['co']['name']}** 에서 {r['hours']:.1f}시간 치 수익 **${r['amount']:,}** 을(를) 수금했습니다.{extra}", ephemeral=True)

@biz_group.command(name="강화", description="사업 레벨을 올려 시간당 수익과 회사 가치를 키웁니다.")
async def biz_upgrade_cmd(interaction: discord.Interaction):
    if not await _biz_precheck(interaction): return
    await interaction.response.defer(ephemeral=True)
    r = await _biz_run(interaction, biz_upgrade_sync, interaction.user.id)
    if not r: return
    co = r["co"]
    msg = f"**{co['name']}** 이(가) **Lv.{co['level']}** 로 강화되었습니다! (비용 ${r['cost']:,})\n시간당 수익 ${biz_income_per_hour(co):,}"
    if r["pending"]:
        msg += f"\n밀린 수익 ${r['pending']:,} 도 함께 수금했습니다."
    await interaction.followup.send(msg, ephemeral=True)

@biz_group.command(name="상장", description=f"내 회사를 주식시장에 올립니다. (사업 Lv.{STOCK_IPO_MIN_LEVEL} 이상)")
async def biz_ipo_cmd(interaction: discord.Interaction):
    if not await _biz_precheck(interaction): return
    await interaction.response.defer(ephemeral=True)
    r = await _biz_run(interaction, biz_ipo_sync, interaction.user.id)
    if not r: return
    await interaction.followup.send(
        f"**{r['co']['name']}** 이(가) 주식시장에 상장되었습니다!\n"
        f"종목 코드 `{r['symbol']}` / 공모가 ${r['price']:,}\n"
        f"시장에 {r['float']}주를 풀어 **${r['proceeds']:,}** 을(를) 조달했습니다. (수수료 {int(STOCK_IPO_FEE_RATE * 100)}% 차감)\n"
        f"대표 보유 {r['owner_shares']}주 — 지분 {STOCK_OWNER_MIN_STAKE}% 이상은 유지해야 합니다.", ephemeral=True)
    await _announce(interaction, f"**{r['co']['name']}** (`{r['symbol']}`) 이(가) 주식시장에 상장했습니다! 공모가 **${r['price']:,}** — `/주식 시세` 로 확인하세요.")

@biz_group.command(name="상장폐지", description="내 회사를 주식시장에서 내립니다. (다른 주주의 주식을 현재가로 모두 사들입니다)")
async def biz_delist_cmd(interaction: discord.Interaction):
    if not await _biz_precheck(interaction): return
    await interaction.response.defer(ephemeral=True)
    r = await _biz_run(interaction, biz_delist_sync, interaction.user.id)
    if not r: return
    await interaction.followup.send(f"**{r['co']['name']}** 이(가) 상장폐지되었습니다. 주주 {r['holders']}명에게 현재가 ${r['price']:,} 기준 총 **${r['total']:,}** 을(를) 보상했습니다.", ephemeral=True)
    await _announce(interaction, f"**{r['co']['name']}** 이(가) 주식시장에서 상장폐지되었습니다.")

@biz_group.command(name="폐업", description="사업을 접고 투자금의 일부를 돌려받습니다. (상장 중이면 먼저 상장폐지)")
async def biz_close_cmd(interaction: discord.Interaction):
    if not await _biz_precheck(interaction): return
    await interaction.response.defer(ephemeral=True)
    r = await _biz_run(interaction, biz_close_sync, interaction.user.id)
    if not r: return
    msg = f"**{r['co']['name']}** 을(를) 폐업했습니다. 투자금 환급 ${r['refund']:,}"
    if r["pending"]:
        msg += f" + 밀린 수익 ${r['pending']:,}"
    await interaction.followup.send(msg, ephemeral=True)

@biz_group.command(name="순위", description="회사 가치가 높은 사업 TOP 10을 봅니다.")
async def biz_rank_cmd(interaction: discord.Interaction):
    if not await _biz_precheck(interaction): return
    await interaction.response.defer(ephemeral=True)
    rows = await _biz_run(interaction, _biz_all)
    if rows is None: return
    if not rows:
        await interaction.followup.send("아직 세워진 회사가 없습니다.", ephemeral=True)
        return
    rows.sort(key=biz_valuation, reverse=True)
    lines = [f"**{i}.** {c['name']} ({c['sector']} Lv.{c['level']}) — 가치 ${biz_valuation(c):,} / 대표 {c.get('owner_name') or '-'}" + (" [상장]" if c.get("listed") else "")
             for i, c in enumerate(rows[:10], 1)]
    await interaction.followup.send(embed=discord.Embed(title="[사업] 회사 가치 순위", description="\n".join(lines), color=discord.Color.dark_gold()), ephemeral=True)


# ───────────── /주식 ─────────────
@stock_group.command(name="시세", description="현재 주가와 세계관 뉴스를 봅니다.")
async def stock_quote_cmd(interaction: discord.Interaction):
    if not await _biz_precheck(interaction): return
    await interaction.response.defer()
    rows = await _biz_run(interaction, _stock_all_sync)
    if rows is None: return
    world = [r for r in rows if r["kind"] == "world"]
    comps = sorted([r for r in rows if r["kind"] == "company"], key=lambda r: int(r["price"]) * int(r.get("total_shares") or 0), reverse=True)
    def line(r):
        p, pv = int(r["price"]), int(r["prev_price"])
        s = f"{_arrow(p, pv)} **{r['name']}** `{r['symbol']}` — **${p:,}** ({_pct(p, pv)}) {spark(r.get('history'))}"
        if r["kind"] == "company":
            s += f" · 물량 {int(r.get('float_shares') or 0)}/{int(r.get('total_shares') or 0)}주"
        return s
    desc = "**[레벤하임 시장]**\n" + "\n".join(line(r) for r in world)
    if comps:
        desc += "\n\n**[상장 기업]**\n" + "\n".join(line(r) for r in comps[:15])
        if len(comps) > 15:
            desc += f"\n…외 {len(comps) - 15}개"
    else:
        desc += "\n\n**[상장 기업]**\n아직 상장한 회사가 없습니다. `/사업 창업` → `/사업 상장`"
    e = discord.Embed(title="[주식시장] 시세", description=desc, color=discord.Color.dark_teal())
    if _stock_news:
        ts, text = _stock_news[-1]
        e.add_field(name="최근 뉴스", value=f"{text}\n(<t:{int(ts.timestamp())}:R>)", inline=False)
    if _stock_last_tick:
        nxt = int(_stock_last_tick.timestamp()) + STOCK_TICK_SEC
        e.add_field(name="다음 시세 변동", value=f"<t:{nxt}:R>", inline=False)
    e.set_footer(text=f"매수·매도 수수료 {STOCK_FEE_RATE * 100:g}% / 그래프는 최근 변동 흐름")
    await interaction.followup.send(embed=e)

@stock_group.command(name="매수", description="종목을 삽니다.")
@app_commands.describe(종목="종목 이름 또는 코드 (자동완성)", 수량="살 주식 수")
@app_commands.autocomplete(종목=_stock_autocomplete)
async def stock_buy_cmd(interaction: discord.Interaction, 종목: str, 수량: app_commands.Range[int, 1, STOCK_MAX_QTY]):
    if not await _biz_precheck(interaction): return
    await interaction.response.defer(ephemeral=True)
    r = await _biz_run(interaction, stock_buy_sync, interaction.user.id, 종목, 수량)
    if not r: return
    await interaction.followup.send(
        f"**{r['row']['name']}** (`{r['row']['symbol']}`) {r['qty']:,}주를 주당 ${r['price']:,} 에 매수했습니다.\n"
        f"결제 ${r['total']:,} (수수료 ${r['fee']:,}) / 보유 {r['shares']:,}주 (평단 ${r['avg']:,}) / 남은 자금 ${r['money']:,}", ephemeral=True)

@stock_group.command(name="매도", description="가진 종목을 팝니다.")
@app_commands.describe(종목="종목 이름 또는 코드 (자동완성)", 수량="팔 주식 수")
@app_commands.autocomplete(종목=_stock_autocomplete)
async def stock_sell_cmd(interaction: discord.Interaction, 종목: str, 수량: app_commands.Range[int, 1, STOCK_MAX_QTY]):
    if not await _biz_precheck(interaction): return
    await interaction.response.defer(ephemeral=True)
    r = await _biz_run(interaction, stock_sell_sync, interaction.user.id, 종목, 수량)
    if not r: return
    sign = "이익" if r["profit"] >= 0 else "손실"
    await interaction.followup.send(
        f"**{r['row']['name']}** (`{r['row']['symbol']}`) {r['qty']:,}주를 주당 ${r['price']:,} 에 매도했습니다.\n"
        f"정산 ${r['net']:,} (수수료 ${r['fee']:,}) / 실현 {sign} ${abs(r['profit']):,} / 남은 보유 {r['left']:,}주", ephemeral=True)

@stock_group.command(name="보유", description="내 주식 보유 현황과 수익률을 봅니다.")
async def stock_portfolio_cmd(interaction: discord.Interaction):
    if not await _biz_precheck(interaction): return
    await interaction.response.defer(ephemeral=True)
    def work():
        return _hold_all(interaction.user.id), _stock_all_sync()
    res = await _biz_run(interaction, work)
    if not res: return
    holds, rows = res
    price_of = {r["symbol"]: r for r in rows}
    lines, total_val, total_cost = [], 0, 0
    for h in holds:
        r = price_of.get(h["symbol"])
        if not r or int(h["shares"]) <= 0:
            continue
        sh, cost, p = int(h["shares"]), int(h["cost"]), int(r["price"])
        val = sh * p
        total_val += val
        total_cost += cost
        pl = (val - cost) / cost * 100 if cost else 0
        lines.append(f"**{r['name']}** `{r['symbol']}` {sh:,}주 — 평단 ${cost // max(1, sh):,} / 현재 ${p:,} / 평가 ${val:,} ({pl:+.1f}%)")
    if not lines:
        await interaction.followup.send("보유 중인 주식이 없습니다. `/주식 시세` 로 종목을 확인해 보세요.", ephemeral=True)
        return
    total_pl = (total_val - total_cost) / total_cost * 100 if total_cost else 0
    e = discord.Embed(title="[주식] 내 보유 현황", description="\n".join(lines), color=discord.Color.dark_teal())
    e.add_field(name="합계", value=f"평가금액 ${total_val:,} / 매입금액 ${total_cost:,} ({total_pl:+.1f}%)", inline=False)
    await interaction.followup.send(embed=e, ephemeral=True)


# ==========================================================================
# [합친 모듈] events.py
#   봇 이벤트 (on_ready 등)
# ==========================================================================
from . import core as _core

_status_loaded = False
_tree_synced = False   # 명령어 동기화는 프로세스당 한 번만 (재연결 때마다 하면 API 제한에 걸릴 수 있음)

@bot.event
async def on_ready():
    global _status_loaded, _tree_synced
    _main_loop = asyncio.get_running_loop()
    _core._main_loop = _main_loop   # report_error_sync(core)가 읽는 공유 값
    _main_loop.set_exception_handler(_loop_exception_handler)
    if not _status_loaded:   # on_ready 는 재연결 때 또 불릴 수 있으므로 처음 한 번만
        _status_loaded = True
        try:
            jailed, wanted = await asyncio.to_thread(load_status_sync)
            print(f"감옥/수배 상태 복구: 수감 {jailed}명 / 수배 {wanted}명")
        except Exception as e:
            print(f"감옥/수배 상태 복구 실패: {e}")
    global _stock_loop_started
    if not _stock_loop_started:   # 주식 시세 변동 루프 (재연결 때 중복 실행 방지)
        _stock_loop_started = True
        keep_task(asyncio.create_task(stock_market_loop()))
    try:
        n_boss = await asyncio.to_thread(load_boss_state_sync)
        if n_boss:
            print(f"보스 상태 복구: {n_boss}개 서버에 보스가 남아 있습니다.")
    except Exception as e:
        print(f"보스 상태 복구 실패: {e}")
    if not _tree_synced:
        try:
            synced = await bot.tree.sync()
            _tree_synced = True
            print(f"총 {len(synced)}개의 슬래시 명령어가 동기화되었습니다.")
        except Exception as e:
            print(e)
    print(f'로그인 완료: {bot.user.name}')
_stock_loop_started = False
