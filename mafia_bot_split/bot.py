import os

from mafia import core   # Flask keep-alive · bot 객체 · DB 연결

# 각 모듈을 불러오면 슬래시 명령어가 자동으로 등록됩니다 (market 안에 on_ready · 사업/주식이 들어 있음)
from mafia import (  # noqa: F401
    status,
    admin,
    battle,
    civilian,
    economy,
    family,
    market,
    misc,
    mission,
    police,
    police_unit,
    territory,
)

core.bot.run(os.getenv("DISCORD_TOKEN"))
