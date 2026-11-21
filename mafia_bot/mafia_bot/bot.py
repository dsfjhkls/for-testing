import os

from mafia import core   # Flask keep-alive · bot 객체 · DB 연결

# 각 모듈을 불러오면 슬래시 명령어가 자동으로 등록됩니다 (순서 중요: 아래가 위를 참조)
from mafia import (  # noqa: F401
    status,
    alignment,
    achievements,
    admin,
    transfer,
    police_arrest,
    police_ops,
    battle_data,
    battle,
    civilian,
    corrupt_police,
    gamble,
    duel,
    join_leave,
    mission,
    blackmarket,
    profile,
    police_shop,
    info,
    family,
    raid,
    territory,
    ranking,
    jailbreak,
    crafting,
    stats,
    auction,
    help,
    police_unit,
    salary,
    confirm_upgrade,
    dept_store,
    biz_stock,
    events,
)

core.bot.run(os.getenv("DISCORD_TOKEN"))
