"""자금 흐름 로그: user_data 의 money 가 바뀔 때마다 기록하고, 고액 변동은 관리자 로그 채널로 알림

동작 방식
  Supabase 클라이언트를 얇게 감싸서(LoggedClient) `user_data` 테이블의 select / update 를 지켜봅니다.
  - select 결과로 유저별 '마지막으로 아는 잔고'를 기억하고
  - money 를 바꾸는 update 가 실행되면 (새 잔고 - 기억해 둔 잔고) 를 변동액으로 기록합니다.
  그래서 송금·경매·도박·주식·레이드처럼 돈을 바꾸는 모든 코드가 따로 수정하지 않아도 자동으로 잡힙니다.
  어떤 슬래시 명령어 때문에 바뀌었는지도 함께 남습니다 (스레드로 넘어가도 유지됨).

설정
  환경변수 MONEY_LOG_THRESHOLD (기본 500000)  이 금액 이상의 한 번 변동은 로그 채널로 알림
  /관리 자금로그기준 으로 재시작 전까지 바꿀 수 있고, /관리 자금로그 로 최근 기록을 볼 수 있습니다.
  로그 채널은 기존 /관리 로그채널 (또는 환경변수 ERROR_LOG_CHANNEL_ID) 을 그대로 씁니다.

한계: 기록은 메모리에만 있어 봇을 재시작하면 사라집니다 (채널 알림은 남음). 봇 말고 다른 곳에서
      DB 를 직접 고치면 그 변동은 다음 거래 때 섞여서 잡힐 수 있습니다.
"""

import asyncio
import os
import threading
import time
from collections import deque

LEDGER_KEEP = 3000            # 메모리에 보관할 최근 기록 수
SURGE_WINDOW_SEC = 600        # '단시간 급증' 판단 구간 (10분)
SURGE_FACTOR = 3              # 구간 내 누적 증가액이 기준의 몇 배면 알릴지
SURGE_COOLDOWN_SEC = 600      # 같은 유저 급증 알림 최소 간격

def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, default)).strip().replace(",", "").replace("_", ""))
    except Exception:
        return default

_cfg = {"threshold": _env_int("MONEY_LOG_THRESHOLD", 500_000)}
_lock = threading.Lock()
_last_money = {}                      # {user_id(str): 마지막으로 아는 잔고}
_entries = deque(maxlen=LEDGER_KEEP)  # 최근 변동 기록 (dict)
_recent_gain = {}                     # {user_id: deque[(ts, 증가액)]}
_surge_alerted = {}                   # {user_id: 마지막 급증 알림 시각}


def get_threshold() -> int:
    return _cfg["threshold"]

def set_threshold(value: int):
    _cfg["threshold"] = max(1, int(value))


def _command_label() -> str:
    """지금 이 변동을 일으킨 슬래시 명령어 이름 (명령어 밖이면 '-')"""
    try:
        from .core import _current_command_lock   # 순환 import 방지: 호출 시점에 가져옴
        lock = _current_command_lock.get()
        return getattr(lock, "name", None) or "-"
    except Exception:
        return "-"


def remember(user_id, money):
    """DB 에서 읽은 잔고를 기억해 둠 (다음 변동액 계산용)"""
    with _lock:
        _last_money[str(user_id)] = int(money)


def record_change(user_id, new_money: int):
    """money 가 new_money 로 저장됨. 변동액을 계산해 기록하고 필요하면 알린다."""
    uid = str(user_id)
    new_money = int(new_money)
    now = time.time()
    with _lock:
        old = _last_money.get(uid)
        _last_money[uid] = new_money
        if old is None or old == new_money:
            return None
        delta = new_money - old
        entry = {"ts": now, "uid": uid, "delta": delta, "old": old, "new": new_money, "cmd": _command_label()}
        _entries.append(entry)
        surge_total = 0
        if delta > 0:
            q = _recent_gain.setdefault(uid, deque())
            q.append((now, delta))
            while q and now - q[0][0] > SURGE_WINDOW_SEC:
                q.popleft()
            surge_total = sum(a for _, a in q)
        big = abs(delta) >= _cfg["threshold"]
        surge = (delta > 0 and not big and surge_total >= _cfg["threshold"] * SURGE_FACTOR
                 and now - _surge_alerted.get(uid, 0) > SURGE_COOLDOWN_SEC)
        if surge:
            _surge_alerted[uid] = now
    if big:
        _notify(entry, "고액 변동")
    elif surge:
        _notify(entry, f"단시간 급증 (최근 {SURGE_WINDOW_SEC // 60}분 누적 +${surge_total:,})")
    return entry


def recent(user_id=None, limit: int = 15):
    with _lock:
        rows = list(_entries)
    if user_id is not None:
        rows = [r for r in rows if r["uid"] == str(user_id)]
    return rows[-limit:][::-1]   # 최신순


# ---------------------------------------------------------------- 채널 알림
def _notify(entry: dict, kind: str):
    """스레드·이벤트 루프 어디서 불려도 안전하게 관리자 로그 채널로 보낸다 (실패해도 게임에는 영향 없음)."""
    try:
        from . import core as c   # 순환 import 방지: 호출 시점에 가져옴
        loop = c._main_loop
        if loop is None or not loop.is_running():
            return

        async def go():
            import discord
            from .admin import _admin_log_channel
            sign = "+" if entry["delta"] >= 0 else "-"
            color = discord.Color.green() if entry["delta"] >= 0 else discord.Color.red()
            e = discord.Embed(title=f"[자금 로그] {kind}", color=color, timestamp=discord.utils.utcnow())
            e.add_field(name="유저", value=f"<@{entry['uid']}> ({entry['uid']})", inline=True)
            e.add_field(name="변동", value=f"{sign}${abs(entry['delta']):,}", inline=True)
            e.add_field(name="잔고", value=f"${entry['old']:,} → ${entry['new']:,}", inline=True)
            e.add_field(name="원인", value=f"`{entry['cmd']}`", inline=False)
            await c.send_log_embed(_admin_log_channel(), e)

        asyncio.run_coroutine_threadsafe(go(), loop)
    except Exception as err:
        print(f"[자금 로그 알림 실패] {type(err).__name__}: {err}")


# ---------------------------------------------------------------- Supabase 클라이언트 감싸기
class _Builder:
    """postgrest 요청 빌더를 감싸서, execute() 때 user_data 의 잔고 변화를 지켜본다."""
    __slots__ = ("_b", "_op", "_data", "_uid")

    def __init__(self, builder, op, data=None, uid=None):
        self._b, self._op, self._data, self._uid = builder, op, data, uid

    def __getattr__(self, name):
        attr = getattr(self._b, name)
        if not callable(attr):
            return attr

        def call(*a, **k):
            uid = self._uid
            if name == "eq" and len(a) >= 2 and a[0] == "user_id":
                uid = a[1]
            result = attr(*a, **k)
            if result is self._b:
                self._uid = uid
                return self
            if hasattr(result, "execute"):
                return _Builder(result, self._op, self._data, uid)
            return result
        return call

    def execute(self, *a, **k):
        res = self._b.execute(*a, **k)
        try:
            if self._op == "select":
                for row in (getattr(res, "data", None) or []):
                    if isinstance(row, dict) and "user_id" in row and row.get("money") is not None:
                        remember(row["user_id"], row["money"])
            elif self._op == "update" and isinstance(self._data, dict) and "money" in self._data and self._uid is not None:
                if getattr(res, "data", None):          # 실제로 반영된 경우만
                    record_change(self._uid, self._data["money"])
            elif self._op in ("insert", "upsert") and isinstance(self._data, dict) and "money" in self._data and "user_id" in self._data:
                remember(self._data["user_id"], self._data["money"])
        except Exception as err:   # 로그 때문에 게임 동작이 깨지면 안 됨
            print(f"[자금 로그 오류] {type(err).__name__}: {err}")
        return res


class _Table:
    __slots__ = ("_t",)

    def __init__(self, table):
        self._t = table

    def select(self, *a, **k):
        return _Builder(self._t.select(*a, **k), "select")

    def update(self, data, *a, **k):
        return _Builder(self._t.update(data, *a, **k), "update", data)

    def insert(self, data, *a, **k):
        return _Builder(self._t.insert(data, *a, **k), "insert", data if isinstance(data, dict) else None)

    def upsert(self, data, *a, **k):
        return _Builder(self._t.upsert(data, *a, **k), "upsert", data if isinstance(data, dict) else None)

    def __getattr__(self, name):
        return getattr(self._t, name)


class LoggedClient:
    """supabase 클라이언트와 똑같이 쓰되, user_data 테이블만 지켜본다."""
    def __init__(self, client):
        self._c = client

    def table(self, name, *a, **k):
        t = self._c.table(name, *a, **k)
        return _Table(t) if name == "user_data" else t

    def from_(self, name, *a, **k):
        return self.table(name, *a, **k)

    def __bool__(self):
        return True

    def __getattr__(self, name):
        return getattr(self._c, name)
