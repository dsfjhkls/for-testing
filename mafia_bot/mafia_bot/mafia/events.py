"""봇 이벤트 (on_ready 등)"""

import asyncio

from .core import bot, load_boss_state_sync, keep_task
from .status import load_status_sync
from .admin import _loop_exception_handler
from .biz_stock import stock_market_loop
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
