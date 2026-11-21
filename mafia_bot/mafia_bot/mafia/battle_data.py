"""NPC·보스·네임드 데이터"""

import discord
import io
from PIL import Image

from .core import (
    BOSS_SPAWN_CHANCE,
    EDEN_BOSS_SPAWN_CHANCE,
    ELITE_BOSS_SPAWN_CHANCE,
    ULTIMATE_BOSS_SPAWN_CHANCE,
    inc_range,
)

# === NPC 전투 대상 목록 ===
# 권장 진입선: 깡패(처음부터) → 배신자(단검·명예 300) → 거상(몽둥이·명예 1,000) → 수배자(주사기·명예 3,000) → 암살자(명예 5,000)
NPC_DATA = {
    "골목길 깡패": {
        "name": "골목길 깡패",
        "hp": 700,
        "atk": 70,
        "def": 5,
        "reward_money": inc_range((60, 150)),
        "reward_honor": 2,
        "desc": "칼렌데 뒷골목에서 서성이는 초보 깡패입니다."
    },
    "조직의 배신자": {
        "name": "조직의 배신자",
        "hp": 2500,
        "atk": 170,
        "def": 30,
        "reward_money": inc_range((200, 450)),
        "reward_honor": 2,
        "desc": "패밀리의 기밀을 팔아넘기고 도망친 위험한 배신자입니다."
    },
    "암시장 악덕 거상": {
        "name": "암시장 악덕 거상",
        "hp": 5500,
        "atk": 300,
        "def": 70,
        "reward_money": inc_range((450, 1000)),
        "reward_honor": 4,
        "desc": "고용한 보디가드들을 대동하고 다니는 악명 높은 거상입니다."
    },
    "지명수배자 보스": {
        "name": "지명수배자 보스",
        "hp": 11000,
        "atk": 520,
        "def": 130,
        "reward_money": inc_range((1000, 2000)),
        "reward_honor": 6,
        "desc": "치안국에서도 손대지 못하는 강력한 수배범 보스입니다."
    },
    "칼렌데 암살자": {
        "name": "칼렌데 암살자",
        "hp": 18000,
        "atk": 800,
        "def": 220,
        "reward_money": inc_range((2000, 3600)),
        "reward_honor": 10,
        "desc": "그림자처럼 움직이며 거물만 노리는 전설적인 청부 암살자입니다."
    }
}

# === 히든 보스 데이터 (NPC_DATA 와 분리: /전투 로 출현 조건 없이 소환되는 것을 막기 위함) ===
# 권장: 명예 약 5,000pt + 슈퍼 울트라 주먹/수상한 주사기 (대부 직전의 최종 시험)
HIDDEN_BOSS = {
    "image": "boss_dark.jpg",   # 보스 그림 (assets 폴더). 파일이 없으면 그림 없이 진행됩니다.
    "image_crop": (0.07, 0.07, 0.90, 0.84),   # 얼굴만 보이도록 자를 영역 (왼쪽, 위, 오른쪽, 아래 / 그림 크기에 대한 비율 0~1)
    "name": "[히든보스] 어둠의 지배자",   # 보스 승리 시 보스가 연속 출현하지 않도록 비교하는 이름이므로 함부로 바꾸지 마세요
    "hp": 75000,
    "atk": 2750,
    "def": 600,
    "reward_money": inc_range((10000, 30000)),
    "reward_honor": 100,
    "desc": "전투의 살기에 이끌려 모습을 드러낸 칼렌데 지하 세계의 지배자입니다. 15턴 안에 쓰러뜨리지 못하면 제압당합니다."
}

# === 최강 히든 보스 데이터 (태초의 파괴자) ===
ULTIMATE_BOSS = {
    "image": "boss_ultimate.jpg",
    "image_crop": (0.30, 0.05, 0.80, 0.315),   # 모자부터 얼굴까지만 (비율). 그림을 바꾸면 이 값도 맞춰 주세요
    "name": "[최강보스] 태초의 파괴자",
    "hp": 120000,
    "atk": 5500,
    "def": 900,
    "reward_money": inc_range((30000, 70000)),
    "reward_honor": 400,
    "desc": "칼렌데 지하 세계의 근원이자 재앙 그 자체인 존재입니다. 15턴 안에 쓰러뜨리지 못하면 세계가 붕괴합니다."
}

# === 테러 보스: 유토피아 오브 에덴의 창립자 (세계관 /정보 참고) ===
# 어둠의 지배자(75,000)와 태초의 파괴자(120,000) 사이의 난이도입니다.
EDEN_BOSS = {
    "image": "boss_eden.png",   # 보스 그림 (assets 폴더, 십자가까지 잘라 둔 상반신). 파일이 없으면 그림 없이 진행됩니다.
    "name": "[테러보스] 에칼 데 페스토",
    "hp": 95000,
    "atk": 3800,
    "def": 750,
    "reward_money": inc_range((18000, 45000)),
    "reward_honor": 200,
    "desc": "모두가 평등한 세상을 외치며 칼렌데를 불바다로 만들려는 유토피아 오브 에덴의 창립자입니다. 15턴 안에 쓰러뜨리지 못하면 도시가 불탑니다."
}

# === 강화 보스 (각 보스의 강화판: 출현 확률 0.3% / 체력·공격력·방어력이 훨씬 높고 격노하지만 보상이 훨씬 큼) ===
#   • 그림은 원래 보스와 같은 것을 씁니다. 이름이 "[강화보스]" 로 시작하면 강화 보스로 취급합니다. (칭호·업적 카운터)
#   • 고유 전리품은 처음 처치할 때 1회 지급되며, 가지고만 있어도 능력치가 오릅니다. (ELITE_TROPHY_STATS)
ELITE_PREFIX = "[강화보스]"
ELITE_DARK_BOSS = {**HIDDEN_BOSS,
    "name": "[강화보스] 심연을 삼킨 어둠의 지배자",
    "hp": 140000, "atk": 4400, "def": 900, "enrage": 1.5,
    "reward_money": inc_range((40000, 90000)), "reward_honor": 300,
    "drop": "[강화보스 전리품] 심연의 왕관",
    "desc": "어둠의 지배자가 심연의 힘을 삼키고 다시 돌아왔습니다. 체력이 절반 아래로 떨어지면 격노합니다. 15턴 안에 쓰러뜨리지 못하면 제압당합니다."}
ELITE_EDEN_BOSS = {**EDEN_BOSS,
    "name": "[강화보스] 대재앙의 화신 에칼 데 페스토",
    "hp": 175000, "atk": 6000, "def": 1100, "enrage": 1.5,
    "reward_money": inc_range((70000, 150000)), "reward_honor": 600,
    "drop": "[강화보스 전리품] 대재앙의 붉은 깃발",
    "desc": "평등의 이름으로 도시를 불태우려던 창립자가 스스로 재앙이 되었습니다. 체력이 절반 아래로 떨어지면 격노합니다. 15턴 안에 쓰러뜨리지 못하면 도시가 잿더미가 됩니다."}
ELITE_ULTIMATE_BOSS = {**ULTIMATE_BOSS,
    "name": "[강화보스] 종말을 선고하는 태초의 파괴자",
    "hp": 240000, "atk": 9000, "def": 1400, "enrage": 1.5,
    "reward_money": inc_range((120000, 250000)), "reward_honor": 1200,
    "drop": "[강화보스 전리품] 종말의 심판 인장",
    "desc": "태초의 파괴자가 마침내 세계의 종말을 선고했습니다. 체력이 절반 아래로 떨어지면 격노합니다. 15턴 안에 쓰러뜨리지 못하면 모든 것이 사라집니다."}

# === 보스 종류 표 (출현 판정 · 출현 공지 · /보스 전투가 모두 이 표를 사용합니다) ===
#   보스를 더 추가하려면: 보스 데이터를 만들고 → 여기에 한 줄 추가 → BOSS_SPAWN_TABLE 에 확률 한 줄 추가
BOSS_KINDS = {
    "dark": {
        "data": HIDDEN_BOSS,
        "title": "[히든보스] 어둠의 지배자 토벌전",
        "color": discord.Color.dark_red(),
        "spawn_msg": ("**[긴급 상황 경보]** \n"
                      "전투의 여파로 부근에서 엄청난 살기가 감지되었습니다!\n"
                      "**어둠의 지배자**가 이 지역에 출현했습니다! (`/보스` 명령어로 토벌 가능)"),
    },
    "eden": {
        "data": EDEN_BOSS,
        "title": "[테러보스] 에칼 데 페스토 토벌전",
        "color": discord.Color.from_rgb(200, 40, 40),
        "spawn_msg": ("**[테러 경보]** \n"
                      "도시 곳곳에서 붉은 깃발이 펄럭이고 폭발음이 들려옵니다!\n"
                      "**유토피아 오브 에덴의 창립자 에칼 데 페스토**가 모습을 드러냈습니다! (`/보스` 명령어로 토벌 가능)"),
    },
    "ultimate": {
        "data": ULTIMATE_BOSS,
        "title": "[멸망의 전조] 태초의 파괴자 토벌전",
        "color": discord.Color.from_rgb(120, 0, 160),
        "spawn_msg": ("**[세계멸망 경보]** \n"
                      "차원의 균열이 갈라지며 역대급 재앙의 기운이 감지되었습니다!\n"
                      "**[최강보스] 태초의 파괴자**가 강림했습니다! (`/보스` 명령어로 토벌 가능)"),
    },
}
BOSS_KINDS["dark_x"] = {
    "data": ELITE_DARK_BOSS,
    "title": "[강화보스] 심연을 삼킨 어둠의 지배자 토벌전",
    "color": discord.Color.from_rgb(60, 0, 0),
    "spawn_msg": ("**[최상위 긴급 경보]** \n"
                  "지하 세계 전체가 비명을 지르듯 떨리고 있습니다! 어둠의 지배자가 심연의 힘을 삼키고 되돌아왔습니다!\n"
                  "**[강화보스] 심연을 삼킨 어둠의 지배자**가 출현했습니다! 극도로 위험합니다! (`/보스` 명령어로 토벌 가능)"),
}
BOSS_KINDS["eden_x"] = {
    "data": ELITE_EDEN_BOSS,
    "title": "[강화보스] 대재앙의 화신 에칼 데 페스토 토벌전",
    "color": discord.Color.from_rgb(255, 40, 0),
    "spawn_msg": ("**[최상위 테러 경보]** \n"
                  "하늘이 붉게 물들고 도시의 모든 종이 한꺼번에 울립니다! 에칼 데 페스토가 재앙 그 자체가 되어 나타났습니다!\n"
                  "**[강화보스] 대재앙의 화신 에칼 데 페스토**가 출현했습니다! 극도로 위험합니다! (`/보스` 명령어로 토벌 가능)"),
}
BOSS_KINDS["ultimate_x"] = {
    "data": ELITE_ULTIMATE_BOSS,
    "title": "[강화보스] 종말을 선고하는 태초의 파괴자 토벌전",
    "color": discord.Color.from_rgb(200, 0, 255),
    "spawn_msg": ("**[종말 경보]** \n"
                  "하늘이 갈라지고 시간이 멈췄습니다! 태초의 파괴자가 세계의 종말을 선고했습니다!\n"
                  "**[강화보스] 종말을 선고하는 태초의 파괴자**가 강림했습니다! 극도로 위험합니다! (`/보스` 명령어로 토벌 가능)"),
}
BOSS_NAMES = tuple(k["data"]["name"] for k in BOSS_KINDS.values())

def boss_image_file(boss_data: dict):
    """보스 그림을 discord.File 로 만들어 돌려준다. 그림이 지정되지 않았거나 파일이 없으면 None.
    (임베드에서는 set_image(url="attachment://파일이름") 로 참조한다)"""
    from .profile import _asset_paths   # 순환 import 방지: 호출 시점에 가져옴
    name = boss_data.get("image")
    if not name:
        return None
    for path in _asset_paths((name,)):
        crop = boss_data.get("image_crop")
        if crop:   # 전신 그림에서 얼굴 부분만 잘라서 보냄 (실패하면 원본을 그대로 보냄)
            try:
                img = Image.open(path).convert("RGB")
                w, h = img.size
                img = img.crop((int(crop[0] * w), int(crop[1] * h), int(crop[2] * w), int(crop[3] * h)))
                if img.width < 640:   # 잘라낸 그림이 작으면 선명하게 확대
                    img = img.resize((640, int(img.height * 640 / img.width)), Image.Resampling.LANCZOS)
                buf = io.BytesIO()
                if name.lower().endswith(".png"):
                    img.save(buf, format="PNG")
                else:
                    img.save(buf, format="JPEG", quality=95)
                buf.seek(0)
                return discord.File(buf, filename=name)
            except Exception as e:
                print(f"[보스 그림 자르기 실패 → 원본 사용] {name}: {e}")
        return discord.File(path, filename=name)
    print(f"[보스 그림 없음] {name} 파일을 assets 폴더에서 찾지 못했습니다.")
    return None

# 출현 판정표: (종류, 확률) — 위에서부터 확률을 누적해 한 번의 주사위로 어느 보스가 나올지 정합니다.
BOSS_SPAWN_TABLE = [
    ("dark", BOSS_SPAWN_CHANCE),
    ("eden", EDEN_BOSS_SPAWN_CHANCE),
    ("ultimate", ULTIMATE_BOSS_SPAWN_CHANCE),
    ("dark_x", ELITE_BOSS_SPAWN_CHANCE),
    ("eden_x", ELITE_BOSS_SPAWN_CHANCE),
    ("ultimate_x", ELITE_BOSS_SPAWN_CHANCE),
]

def boss_short_name(data: dict) -> str:
    """'[히든보스] 어둠의 지배자' -> '어둠의 지배자'  ('[강화보스] 심연을 삼킨 어둠의 지배자' -> '[강화] 심연을 삼킨 어둠의 지배자')"""
    if data["name"].startswith(ELITE_PREFIX):
        return "[강화] " + data["name"].split("] ", 1)[-1]
    return data["name"].split("] ", 1)[-1]

def boss_chance_text() -> str:
    """'어둠의 지배자 3% / 에칼 데 페스토 3% / 태초의 파괴자 3% / ... 강화 보스 각 0.3%'"""
    return " / ".join(f"{boss_short_name(BOSS_KINDS[kind]['data'])} {chance * 100:g}%" for kind, chance in BOSS_SPAWN_TABLE)

def boss_reward_text() -> str:
    return "\n".join(
        f"  └ {boss_short_name(k['data'])}: ${k['data']['reward_money'][0]:,}~${k['data']['reward_money'][1]:,} / 명예 +{k['data']['reward_honor']}pt"
        for k in BOSS_KINDS.values()
    )

# === 네임드(강화) 몬스터 ===
# 일반 NPC보다 훨씬 강하고, 체력이 절반 아래로 떨어지면 격노(enrage)해서 공격력이 오릅니다.
# 처음 처치하면 고유 전리품()을 얻고, 명예는 조금만 주므로 대부 달성 속도에는 거의 영향이 없습니다.
# min_honor 는 입장 조건, 12턴 안에 못 쓰러뜨리면 패배합니다.
NAMED_MONSTERS = {
    "marco": {
        "name": "[네임드] 백정 마르코", "named": True, "min_honor": 500, "max_turns": 12,
        "hp": 4200, "atk": 330, "def": 55, "enrage": 1.5,
        "reward_money": inc_range((900, 1800)), "reward_honor": 4,
        "drop": "[전리품] 마르코의 정육 칼",
        "desc": "블랙워드 도살장을 지배하는 청부 해결사. 피를 보면 더 사나워집니다."
    },
    "bianca": {
        "name": "[네임드] 독사 비앙카", "named": True, "min_honor": 1500, "max_turns": 12,
        "hp": 8000, "atk": 520, "def": 100, "enrage": 1.5,
        "reward_money": inc_range((1800, 3300)), "reward_honor": 8,
        "drop": "[전리품] 비앙카의 독니 반지",
        "image": "named_bianca.png",   # 전투 화면 그림 (assets 폴더). 파일이 없으면 그림 없이 진행됩니다.
        "desc": "로젠펠트 뒷무대의 독살 전문가. 체력이 줄면 맹독을 뿌리며 덤빕니다."
    },
    "boronin": {
        "name": "[네임드] 철의 장군 보로닌", "named": True, "min_honor": 2500, "max_turns": 12,
        "hp": 14000, "atk": 800, "def": 170, "enrage": 1.5,
        "reward_money": inc_range((3000, 5400)), "reward_honor": 14,
        "drop": "[전리품] 보로닌의 철 훈장",
        "image": "named_boronin.png",   # 전투 화면 그림 (assets 폴더). 파일이 없으면 그림 없이 진행됩니다.
        "desc": "퇴역 후 용병단을 이끄는 철벽의 장군. 방어가 매우 단단합니다."
    },
    "executioner": {
        "name": "[네임드] 붉은 달의 처형자", "named": True, "min_honor": 4000, "max_turns": 12,
        "hp": 22000, "atk": 1200, "def": 260, "enrage": 1.6,
        "reward_money": inc_range((4800, 8400)), "reward_honor": 24,
        "drop": "[전리품] 처형자의 붉은 가면",
        "desc": "칼렌데 지하 세계가 두려워하는 전설의 처형자. 정상급 장비가 없으면 상대도 안 됩니다."
    },
}
