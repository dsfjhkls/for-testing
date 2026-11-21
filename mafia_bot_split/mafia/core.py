"""공통 기반: 자금 흐름 로그, Flask keep-alive, 수입 배율, 명령어 잠금, Supabase 연결, bot 객체, 오류 보고

합친 모듈: ledger.py, core.py
"""

import asyncio
import contextvars
import discord
import os
import sys
import threading
import time
from collections import deque
from discord import app_commands
from discord.ext import commands
from flask import Flask
from supabase import Client, create_client
from threading import Thread


# ==========================================================================
# [합친 모듈] ledger.py
#   자금 흐름 로그: user_data 의 money 가 바뀔 때마다 기록하고, 고액 변동은 관리자 로그 채널로 알림
#
#   동작 방식
#     Supabase 클라이언트를 얇게 감싸서(LoggedClient) `user_data` 테이블의 select / update 를 지켜봅니다.
#     - select 결과로 유저별 '마지막으로 아는 잔고'를 기억하고
#     - money 를 바꾸는 update 가 실행되면 (새 잔고 - 기억해 둔 잔고) 를 변동액으로 기록합니다.
#     그래서 송금·경매·도박·주식·레이드처럼 돈을 바꾸는 모든 코드가 따로 수정하지 않아도 자동으로 잡힙니다.
#     어떤 슬래시 명령어 때문에 바뀌었는지도 함께 남습니다 (스레드로 넘어가도 유지됨).
#
#   설정
#     환경변수 MONEY_LOG_THRESHOLD (기본 500000)  이 금액 이상의 한 번 변동은 로그 채널로 알림
#     /관리 자금로그기준 으로 재시작 전까지 바꿀 수 있고, /관리 자금로그 로 최근 기록을 볼 수 있습니다.
#     로그 채널은 기존 /관리 로그채널 (또는 환경변수 ERROR_LOG_CHANNEL_ID) 을 그대로 씁니다.
#
#   한계: 기록은 메모리에만 있어 봇을 재시작하면 사라집니다 (채널 알림은 남음). 봇 말고 다른 곳에서
#         DB 를 직접 고치면 그 변동은 다음 거래 때 섞여서 잡힐 수 있습니다.
# ==========================================================================
LEDGER_KEEP = 3000            # 메모리에 보관할 최근 기록 수
SURGE_WINDOW_SEC = 600        # '단시간 급증' 판단 구간 (10분)
SURGE_FACTOR = 3              # 구간 내 누적 증가액이 기준의 몇 배면 알릴지
SURGE_COOLDOWN_SEC = 600      # 같은 유저 급증 알림 최소 간격

def _ledger_env_int(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, default)).strip().replace(",", "").replace("_", ""))
    except Exception:
        return default

_cfg = {"threshold": _ledger_env_int("MONEY_LOG_THRESHOLD", 500_000)}
_lock = threading.Lock()
_last_money = {}                      # {user_id(str): 마지막으로 아는 잔고}
_entries = deque(maxlen=LEDGER_KEEP)  # 최근 변동 기록 (dict)
_recent_gain = {}                     # {user_id: deque[(ts, 증가액)]}
_surge_alerted = {}                   # {user_id: 마지막 급증 알림 시각}


def get_threshold() -> int:
    return _cfg["threshold"]

def set_threshold(value: int):
    _cfg["threshold"] = max(1, int(value))


def _ledger_command_label() -> str:
    """지금 이 변동을 일으킨 슬래시 명령어 이름 (명령어 밖이면 '-')"""
    try:
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
        entry = {"ts": now, "uid": uid, "delta": delta, "old": old, "new": new_money, "cmd": _ledger_command_label()}
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
        c = sys.modules[__name__]   # (합쳐짐) 이 파일이 곧 core 모듈
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


# ==========================================================================
# [합친 모듈] core.py
#   공통 기반: Flask keep-alive, 수입 배율, 명령어 잠금, Supabase 연결, bot 객체, 오류 보고
# ==========================================================================
# 렌더 포트 검사를 통과하기 위한 가짜 웹 서버
app = Flask('')

@app.route('/')
def home():
    return "Mafia Bot is running with Supabase DB!"

def run():
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))   # Render 등은 PORT 환경변수를 줌

def keep_alive():
    t = Thread(target=run)
    t.start()

keep_alive()

# ==========================================
# [전체 수입 배율]
#   활동으로 번 돈(임무·전투·아르바이트·서비스·소매치기·순찰·검거·심문·작전·월급·레이드·구역 수입 등)에 곱해지는 배율입니다.
#   수입을 더 늘리거나 줄이고 싶으면 이 숫자 하나만 바꾸세요. (1.0 = 원래대로)
#   도박·유저 간 송금·환불·업적/주간 의뢰 보상에는 적용되지 않습니다.
# ==========================================
INCOME_MULTIPLIER = 1.5

def inc_money(n):
    return int(round(n * INCOME_MULTIPLIER))

def inc_range(r):
    return (inc_money(r[0]), inc_money(r[1]))


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
        from .admin import _notify_user_error  # 순환 import 방지: 호출 시점에 가져옴
        _finish_command(interaction)   # 오류로 끝난 명령어도 잠금 해제
        if isinstance(error, (app_commands.CommandNotFound, app_commands.CheckFailure)):
            return   # 유저 쪽 사유(권한 부족 등)는 오류 보고 대상이 아님
        await report_error(f"명령어 /{_command_label(interaction)}", error, interaction)
        await _notify_user_error(interaction)

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
        supabase: Client = LoggedClient(create_client(SUPABASE_URL, SUPABASE_KEY))   # 자금 변동 기록용으로 감쌈 (ledger.py)
        print(f"사용 중인 SUPABASE_URL: {SUPABASE_URL}")
        try:
            supabase.table("user_data").select("user_id").limit(1).execute()
            print("Supabase 데이터베이스 연결 및 user_data 테이블 접근 성공!")
        except Exception as test_err:
            print(f"Supabase 접근 테스트 실패: {test_err}")
    except Exception as e:
        print(f"Supabase 연결 실패: {e}")
        supabase = None

# === 수배 및 감옥 관리 데이터 ===
# {user_id: datetime (수배 만료 시간)}
wanted_list = {}

# === 서버별 보스 출현 상태 관리 ===
# {guild_id: "dark" | "eden" | "silent" | "ultimate" | None}  ← 서버에 지금 어떤 보스가 떠 있는지 (None/없음 = 보스 없음)
#   "dark"     = [히든보스] 어둠의 지배자  (HIDDEN_BOSS)
#   "eden"     = [테러보스] 에칼 데 페스토  (EDEN_BOSS)
#   "silent"   = [심판보스] 침묵의 심판자  (SILENT_BOSS)
#   "ultimate" = [최강보스] 태초의 파괴자  (ULTIMATE_BOSS)
#   "dark_x" / "eden_x" / "ultimate_x" = [강화보스] 위 보스들의 강화판 (ELITE_*_BOSS)
active_boss_guilds = {}
# {guild_id: NPCBattleView}  ← 지금 보스와 싸우는 도전자 (한 서버에서 보스전은 한 번에 한 명만)
#   보스는 누군가 처치할 때까지 서버에 남아 있고, 도전에 실패(패배/시간 초과/자리 비움)해도 사라지지 않습니다.
#   도전할 때마다 보스 체력은 가득 찬 상태로 새로 시작합니다.
boss_challengers = {}

# ── 보스 상태 DB 저장 ──
#   서버에 떠 있는 보스는 Supabase `boss_state` 테이블(guild_id, kind)에도 저장되어, 봇을 재시작해도 처치될 때까지 남습니다.
#   → boss_setup.sql 을 Supabase 에서 한 번 실행해 주세요. 테이블이 없어도 봇은 정상 동작하며, 그때는 메모리에만 저장됩니다.
_boss_state_warned = False
_boss_state_tasks = set()

def _boss_state_warn(action: str, err):
    global _boss_state_warned
    if not _boss_state_warned:
        _boss_state_warned = True
        print(f"[경고] 보스 상태 DB {action} 실패: {err}\n"
              f"       Supabase 에서 boss_setup.sql 을 실행했는지 확인해 주세요. (그동안은 메모리에만 저장됩니다)")

def boss_state_save_sync(guild_id: int, kind):
    if not supabase:
        return
    try:
        if kind:
            supabase.table("boss_state").upsert({"guild_id": str(guild_id), "kind": kind}, on_conflict="guild_id").execute()
        else:
            supabase.table("boss_state").delete().eq("guild_id", str(guild_id)).execute()
    except Exception as e:
        _boss_state_warn("저장", e)

def set_active_boss(guild_id: int, kind):
    """서버의 보스를 바꾸고(None = 보스 없음) DB 에도 저장합니다. 보스 상태는 반드시 이 함수로만 바꿉니다."""
    active_boss_guilds[guild_id] = kind
    if not supabase:
        return
    try:
        task = asyncio.get_running_loop().create_task(asyncio.to_thread(boss_state_save_sync, guild_id, kind))
        _boss_state_tasks.add(task)
        task.add_done_callback(_boss_state_tasks.discard)
    except RuntimeError:
        boss_state_save_sync(guild_id, kind)

def load_boss_state_sync() -> int:
    """봇 시작 시 DB 에 저장된 보스를 메모리로 복구합니다. 반환: 복구한 보스 수"""
    from .battle import BOSS_KINDS  # 순환 import 방지: 호출 시점에 가져옴
    if not supabase:
        return 0
    try:
        rows = supabase.table("boss_state").select("*").execute().data or []
    except Exception as e:
        _boss_state_warn("불러오기", e)
        return 0
    n = 0
    for row in rows:
        try:
            gid, kind = int(row["guild_id"]), row.get("kind")
        except (KeyError, ValueError, TypeError):
            continue
        if kind in BOSS_KINDS and not active_boss_guilds.get(gid):
            active_boss_guilds[gid] = kind
            n += 1
    return n

# 일반 /전투 승리 시 '보스마다 각각' 출현할 확률 (테스트할 때는 0.3 처럼 크게 바꾸세요)
#   • 서버에 보스가 없을 때만 판정하고, 한 번에 보스 한 마리만 출현합니다.
#   • 보스 4종(각 3%) + 강화 보스 3종(각 0.3%)이라 '아무 보스나' 나올 확률은 합계 12.9% 입니다. (BOSS_SPAWN_TABLE 참고)
BOSS_SPAWN_CHANCE = 0.03            # 3% (어둠의 지배자)
EDEN_BOSS_SPAWN_CHANCE = 0.03       # 3% (에칼 데 페스토)
SILENT_BOSS_SPAWN_CHANCE = 0.03     # 3% (침묵의 심판자)
ULTIMATE_BOSS_SPAWN_CHANCE = 0.03   # 3% (태초의 파괴자)
ELITE_BOSS_SPAWN_CHANCE = 0.003     # 0.3% (강화 보스: 보스마다 각각 이 확률로 더 강한 강화판이 출현. 합계 0.9%)

# {user_id: datetime (석방 예정 시간)}
jail_list = {}

def _env_int(name: str):
    raw = _clean_env(os.getenv(name))
    try:
        return int(raw) if raw else None
    except ValueError:
        print(f"[경고] 환경 변수 {name} 값이 숫자가 아닙니다: {raw}")
        return None

ERROR_LOG_CHANNEL_ID = _env_int("ERROR_LOG_CHANNEL_ID")

_main_loop = None
_err_suppressed = 0    # 도배 방지로 건너뛴 보고 수

async def send_log_embed(channel_id, embed: discord.Embed) -> bool:
    if not channel_id:
        return False
    try:
        channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
        await channel.send(embed=embed)
        return True
    except Exception as e:
        print(f"[로그 채널 전송 실패] {type(e).__name__}: {e}")
        return False

async def report_error(context: str, error, interaction: discord.Interaction = None, print_tb: bool = True):
    """오류를 콘솔에 남기고, 오류 로그 채널이 지정되어 있으면 임베드로 보고합니다. (이 함수 자체는 절대 예외를 던지지 않음)"""
    from .admin import _err_recent, _err_sent_times, _tb_text, _unwrap_error, ERROR_DEDUPE_SEC, ERROR_MAX_PER_MIN  # 순환 import 방지: 호출 시점에 가져옴
    global _err_suppressed
    try:
        error = _unwrap_error(error)
        tb = _tb_text(error)
        if print_tb:
            print(f"[오류] {context}\n{tb}")
        if not ERROR_LOG_CHANNEL_ID:
            return
        now = time.time()
        lines = tb.strip().splitlines()
        signature = (context, lines[-1][:200] if lines else "")
        if now - _err_recent.get(signature, 0) < ERROR_DEDUPE_SEC:
            _err_suppressed += 1
            return
        _err_sent_times[:] = [t for t in _err_sent_times if now - t < 60]
        if len(_err_sent_times) >= ERROR_MAX_PER_MIN:
            _err_suppressed += 1
            return
        _err_recent[signature] = now
        _err_sent_times.append(now)

        embed = discord.Embed(title="[오류 보고]", color=discord.Color.red(), timestamp=discord.utils.utcnow())
        embed.add_field(name="발생 위치", value=str(context)[:1000], inline=False)
        if interaction is not None:
            where = []
            if getattr(interaction, "user", None):
                where.append(f"유저: {interaction.user.mention} ({interaction.user.id})")
            if getattr(interaction, "guild", None):
                where.append(f"서버: {interaction.guild.name}")
            if getattr(interaction, "channel", None) and getattr(interaction.channel, "id", None):
                where.append(f"채널: <#{interaction.channel.id}>")
            if interaction.type is discord.InteractionType.application_command:
                where.append(f"명령어: /{_command_label(interaction)}")
            if where:
                embed.add_field(name="상황", value="\n".join(where)[:1000], inline=False)
        embed.add_field(name="오류", value=f"`{type(error).__name__}: {str(error)[:300]}`", inline=False)
        tail = tb.strip()[-850:]
        embed.add_field(name="상세 (끝부분)", value=f"```py\n{tail}\n```", inline=False)
        if _err_suppressed:
            embed.set_footer(text=f"도배 방지로 건너뛴 보고 {_err_suppressed}건")
            _err_suppressed = 0
        await send_log_embed(ERROR_LOG_CHANNEL_ID, embed)
    except Exception as e:
        print(f"[오류 보고 실패] {type(e).__name__}: {e}")


# 백그라운드 태스크는 참조를 들고 있지 않으면 실행 도중 가비지 컬렉션으로 사라질 수 있다 (Python 공식 주의사항)
_bg_tasks = set()

def keep_task(task):
    """create_task / ensure_future 결과를 끝날 때까지 붙잡아 둔다."""
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return task
