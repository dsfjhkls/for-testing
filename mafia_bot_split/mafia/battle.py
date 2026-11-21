"""전투: NPC·보스·네임드 데이터, /전투 /보스 /네임드, /결투

합친 모듈: battle_data.py, battle.py, duel.py
"""

import asyncio
import discord
import io
import random
from datetime import datetime, timedelta
from discord import app_commands
from PIL import Image

from .core import (
    active_boss_guilds,
    boss_challengers,
    BOSS_SPAWN_CHANCE,
    bot,
    EDEN_BOSS_SPAWN_CHANCE,
    ELITE_BOSS_SPAWN_CHANCE,
    GuardedView,
    inc_range,
    jail_list,
    report_error,
    set_active_boss,
    SILENT_BOSS_SPAWN_CHANCE,
    supabase,
    ULTIMATE_BOSS_SPAWN_CHANCE,
)
from .status import (
    _require_db,
    BOSS_COOLDOWN_SEC,
    bump_user_stats_sync,
    check_jail,
    compute_combat_stats,
    daily_record,
    DUEL_MAX_BET,
    evaluate_new_titles,
    format_new_titles,
    get_title_blob,
    get_user_data,
    NPC_LOSS_PENALTY,
    WEAPON_SHOP_ITEMS,
)
from .admin import check_maintenance, check_membership


# ==========================================================================
# [합친 모듈] battle_data.py
#   NPC·보스·네임드 데이터
# ==========================================================================
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

# === 심판 보스: 침묵의 심판자 ===
# 태초의 파괴자(120,000) 바로 아래, 에칼 데 페스토(95,000) 위의 '두 번째로 강한 보스'입니다.
#   (원래 값을 정확히 몰라서 이 위치에 맞춰 새로 정했습니다. 아래 수치만 바꾸면 난이도·보상이 조절됩니다)
#   그림을 쓰려면 assets 폴더에 boss_silent.jpg 를 넣으세요. (없으면 그림 없이 진행, image_crop 으로 얼굴 영역 조절)
SILENT_BOSS = {
    "image": "boss_silent.jpg",
    "image_crop": (0.10, 0.05, 0.90, 0.60),
    "name": "[심판보스] 침묵의 심판자",
    "hp": 108000,
    "atk": 4600,
    "def": 830,
    "reward_money": inc_range((25000, 58000)),
    "reward_honor": 300,
    "desc": "한마디 말도 없이 죄인을 가려내 심판을 내리는 칼렌데의 처형자입니다. 15턴 안에 쓰러뜨리지 못하면 모두가 침묵 속에 심판받습니다."
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
    "silent": {
        "data": SILENT_BOSS,
        "title": "[심판보스] 침묵의 심판자 토벌전",
        "color": discord.Color.from_rgb(70, 70, 110),
        "spawn_msg": ("**[침묵 경보]** \n"
                      "도시의 소음이 일순간 모두 사라지고 숨 막히는 정적이 내려앉았습니다!\n"
                      "**[심판보스] 침묵의 심판자**가 모습을 드러냈습니다! (`/보스` 명령어로 토벌 가능)"),
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
    from .economy import _asset_paths  # 순환 import 방지: 호출 시점에 가져옴
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
    ("silent", SILENT_BOSS_SPAWN_CHANCE),
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
    """'어둠의 지배자 3% / 에칼 데 페스토 3% / 침묵의 심판자 3% / 태초의 파괴자 3% / ... 강화 보스 각 0.3%'"""
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


# ==========================================================================
# [합친 모듈] battle.py
#   전투 스킬, NPC 턴제 전투, /전투 /보스 /네임드
# ==========================================================================
# === 전투 스킬 (NPC·보스·네임드 전투 / 결투 공통) ===
# '방어하기' 대신 쓰는 행동입니다. 스킬을 쓰면 그 턴의 기본 공격을 대신하고, 쿨다운은 '내 행동 수' 기준입니다.
SKILL_SMASH_MULT = 1.5          # 강타: 대미지 배율
SKILL_SMASH_CD = 3              # 강타: 사용 후 이 행동 수가 지나야 다시 사용 (3 = 두 번 쉬고 세 번째 행동에 재사용)
SKILL_RALLY_BONUS = 0.25        # 전투 함성: 공격력 증가율
SKILL_RALLY_TURNS = 3           # 전투 함성: 사용 뒤 몇 번의 행동 동안 효과가 유지되는지
SKILL_RALLY_CD = 5              # 전투 함성: 재사용 대기 (행동 수)
SKILL_FINISHER_MULT = 2.5       # 필살 일격: 대미지 배율
SKILL_FINISHER_MIN_TURN = 5     # 필살 일격: 이 행동부터 사용 가능 (전투당 1회)
PVE_HP_SCALE = 1.25             # 스킬이 생긴 만큼 NPC·보스 체력을 늘려 기존과 비슷한 난이도를 유지

SKILL_LABELS = {"attack": "공격하기", "smash": "강타", "rally": "전투 함성", "finisher": "필살 일격"}


class SkillState:
    """한 전투에서 한 사람의 스킬 사용 상태"""

    def __init__(self):
        self.actions = 0              # 지금까지 한 행동 수
        self.smash_ready = 1          # 이 행동 번호부터 강타 사용 가능
        self.rally_ready = 1
        self.rally_until = 0          # 이 행동 번호까지 전투 함성 효과 유지
        self.finisher_used = False

    @property
    def turn(self) -> int:
        """지금 하려는 행동의 번호 (1부터)"""
        return self.actions + 1

    def atk_mult(self) -> float:
        return 1 + SKILL_RALLY_BONUS if self.turn <= self.rally_until else 1.0

    def buff_turns_left(self) -> int:
        return max(0, self.rally_until - self.turn + 1)

    def wait(self, key: str) -> str:
        """지금 쓸 수 없으면 그 이유(짧은 문구), 쓸 수 있으면 빈 문자열"""
        t = self.turn
        if key == "smash" and self.smash_ready > t:
            return f"{self.smash_ready - t}턴 뒤"
        if key == "rally" and self.rally_ready > t:
            return f"{self.rally_ready - t}턴 뒤"
        if key == "finisher":
            if self.finisher_used:
                return "사용함"
            if t < SKILL_FINISHER_MIN_TURN:
                return f"{SKILL_FINISHER_MIN_TURN}턴째부터"
        return ""

    def use(self, key: str):
        """스킬 사용 기록 (행동 수를 올리기 전에 호출)"""
        t = self.turn
        if key == "smash":
            self.smash_ready = t + SKILL_SMASH_CD
        elif key == "rally":
            self.rally_ready = t + SKILL_RALLY_CD
            self.rally_until = t + SKILL_RALLY_TURNS
        elif key == "finisher":
            self.finisher_used = True

    def status_text(self) -> str:
        lines = []
        for key in ("smash", "rally", "finisher"):
            w = self.wait(key)
            lines.append(f"{SKILL_LABELS[key]} : {w if w else '사용 가능'}")
        return "\n".join(lines)


def skill_hit(state: SkillState, key: str, atk: int, defense: int, spread: int) -> int:
    """기본 공격 / 강타 / 필살 일격의 대미지 (전투 함성 효과 반영). 상대의 방어 자세 보정은 호출하는 쪽에서 처리한다."""
    base = max(10, int(atk * state.atk_mult()) - defense + random.randint(-spread, spread))
    if key == "smash":
        return int(base * SKILL_SMASH_MULT)
    if key == "finisher":
        return int(base * SKILL_FINISHER_MULT)
    return base


SKILL_FOOTER = (f"강타 x{SKILL_SMASH_MULT} · 전투 함성 공격력 +{int(SKILL_RALLY_BONUS * 100)}% ({SKILL_RALLY_TURNS}턴) · "
                f"필살 일격 x{SKILL_FINISHER_MULT} ({SKILL_FINISHER_MIN_TURN}턴째부터 1회)")


# === NPC 턴제 전투 View 클래스 (턴 제한 추가) ===
class NPCBattleView(GuardedView):
    holds_command_lock = True   # 전투가 끝날 때까지 다른 명령어 실행 차단

    def __init__(self, user: discord.Member, user_stats_dict: dict, npc_info: dict, max_turns: int = 10):
        super().__init__(timeout=60)
        self.user = user
        self.npc_info = npc_info
        self.max_turns = max_turns  # 최대 턴 수 설정 (기본 10턴)

        # 유저 및 NPC 스탯 설정
        self.u_hp = user_stats_dict["hp"]
        self.u_max_hp = user_stats_dict["hp"]
        self.u_atk = user_stats_dict["atk"]
        self.u_def = user_stats_dict["def"]
        self.skill = SkillState()   # 강타 / 전투 함성 / 필살 일격 사용 상태

        self.npc_hp = int(npc_info["hp"] * PVE_HP_SCALE)
        self.npc_max_hp = self.npc_hp
        self.npc_atk = npc_info["atk"]
        self.npc_def = npc_info["def"]
        self.npc_guard = False

        self.turn_count = 1
        self.logs = [f"{user.display_name} 님과 **[{npc_info['name']}]**의 전투가 시작되었습니다! (제한: {max_turns}턴)"]
        self.message = None
        self.finished = False
        self._refresh_buttons()

    def _refresh_buttons(self):
        """스킬 버튼에 남은 대기 턴을 표시하고, 쓸 수 없으면 비활성화"""
        for key, btn in (("smash", self.smash_btn), ("rally", self.rally_btn), ("finisher", self.finisher_btn)):
            w = self.skill.wait(key)
            btn.label = SKILL_LABELS[key] + (f" ({w})" if w else "")
            btn.disabled = bool(w)

    def get_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title=f"[NPC 토벌 전투] 턴 {self.turn_count} / {self.max_turns}",
            description=f"상대: **{self.npc_info['name']}**\n*{self.npc_info['desc']}*",
            color=discord.Color.dark_purple()
        )

        buff = self.skill.buff_turns_left()
        buff_line = f"\n버프 : 공격력 +{int(SKILL_RALLY_BONUS * 100)}% ({buff}턴)" if buff else ""
        embed.add_field(
            name=f"{self.user.display_name} (나)",
            value=f"체력 : {self.u_hp} / {self.u_max_hp}\n공격력 : {self.u_atk}\n방어력 : {self.u_def}{buff_line}",
            inline=True
        )
        embed.add_field(
            name=f"{self.npc_info['name']}",
            value=f"체력 : {self.npc_hp} / {self.npc_max_hp}\n공격력 : {self.npc_atk}\n방어력 : {self.npc_def}",
            inline=True
        )
        embed.add_field(name="스킬", value=self.skill.status_text(), inline=False)

        log_text = "\n".join(self.logs[-6:])
        embed.add_field(
            name="전투 현황 로그",
            value=f"```diff\n{log_text}\n```",
            inline=False
        )
        embed.set_footer(text=SKILL_FOOTER)
        if self.npc_info.get("image"):   # 보스 그림 (최초 전송 때 첨부한 파일을 계속 참조)
            embed.set_image(url=f"attachment://{self.npc_info['image']}")
        return embed

    async def process_turn(self, interaction: discord.Interaction, action_type: str):
        if interaction.user != self.user:
            await interaction.response.send_message("본인의 전투 창만 조작할 수 있습니다.", ephemeral=True)
            return

        wait = self.skill.wait(action_type) if action_type != "attack" else ""
        if wait:
            await interaction.response.send_message(f"{SKILL_LABELS[action_type]} 은(는) 아직 사용할 수 없습니다. ({wait})", ephemeral=True)
            return

        await interaction.response.defer()

        # 1. 유저 행동 처리
        name = self.user.display_name
        npc_name = self.npc_info["name"]
        if action_type == "rally":
            self.skill.use("rally")
            self.logs.append(f"+ {name}의 전투 함성! 앞으로 {SKILL_RALLY_TURNS}번의 행동 동안 공격력 +{int(SKILL_RALLY_BONUS * 100)}%")
        else:
            buffed = self.skill.atk_mult() > 1
            defense = self.npc_def * 2 if self.npc_guard else self.npc_def
            dmg = skill_hit(self.skill, action_type, self.u_atk, defense, 10)
            if self.npc_guard:
                dmg = int(dmg * 0.5)
                self.logs.append(f"+ {npc_name}이(가) 방어 상태여서 대미지가 감소했습니다!")
            self.npc_hp = max(0, self.npc_hp - dmg)
            verb = {"attack": "공격", "smash": "강타", "finisher": "필살 일격!!"}[action_type]
            self.logs.append(f"+ {name}의 {verb}! {npc_name}에게 {dmg} 대미지!" + (" [함성 효과]" if buffed else ""))
            if action_type != "attack":
                self.skill.use(action_type)
        self.skill.actions += 1

        # NPC 사망 확인 (승리)
        if self.npc_hp <= 0:
            await self.end_battle(interaction, outcome="win")
            return

        # 2. 턴 제한 확인 (유저 공격 후 NPC를 처치하지 못하고 최대 턴에 도달한 경우 강제 패배)
        if self.turn_count >= self.max_turns:
            self.logs.append(f"- 지정된 {self.max_turns}턴이 지나 도망치지 못하고 제압당했습니다!")
            await self.end_battle(interaction, outcome="turn_limit")
            return

        # 3. NPC 자동 반격 AI
        self.npc_guard = False
        if random.random() < 0.2:
            self.npc_guard = True
            self.logs.append(f"- {npc_name}이(가) 방어 태세를 갖췄습니다.")
        else:
            cur_npc_atk = self.npc_atk
            enrage = self.npc_info.get("enrage")
            if enrage and self.npc_hp < self.npc_max_hp * 0.5:
                cur_npc_atk = int(self.npc_atk * enrage)
                if not getattr(self, "_enraged", False):
                    self._enraged = True
                    self.logs.append(f"- {npc_name}이(가) 격노했습니다! 공격력 +{int((enrage - 1) * 100)}%")
            npc_dmg = max(10, cur_npc_atk - self.u_def + random.randint(-10, 10))
            self.u_hp = max(0, self.u_hp - npc_dmg)
            self.logs.append(f"- {npc_name}의 반격! {name}이(가) {npc_dmg} 대미지를 입었습니다.")

        # 유저 사망 확인 (체력 0 패배)
        if self.u_hp <= 0:
            await self.end_battle(interaction, outcome="loss")
            return

        self.turn_count += 1
        self._refresh_buttons()
        await interaction.edit_original_response(embed=self.get_embed(), view=self)

    @discord.ui.button(label="공격하기", style=discord.ButtonStyle.danger)
    async def attack_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.process_turn(interaction, "attack")

    @discord.ui.button(label="강타", style=discord.ButtonStyle.primary)
    async def smash_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.process_turn(interaction, "smash")

    @discord.ui.button(label="전투 함성", style=discord.ButtonStyle.success)
    async def rally_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.process_turn(interaction, "rally")

    @discord.ui.button(label="필살 일격", style=discord.ButtonStyle.secondary)
    async def finisher_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.process_turn(interaction, "finisher")

    async def end_battle(self, interaction: discord.Interaction, outcome: str):
        from .economy import add_materials, format_material_drops, roll_material_drops  # 순환 import 방지: 호출 시점에 가져옴
        self.finished = True
        self.stop()

        for child in self.children:
            child.disabled = True

        user_data = get_user_data(self.user.id)
        current_money = user_data.get("money", 0)
        current_honor = user_data.get("honor", 0)

        boss_spawn_msg = ""
        boss_spawn_kind = None
        guild_id = None

        if outcome == "win":
            earned_money = random.randint(*self.npc_info["reward_money"])
            earned_honor = self.npc_info["reward_honor"]

            new_money = current_money + earned_money
            new_honor = current_honor + earned_honor

            update_payload = {"money": new_money, "honor": new_honor}
            drop_msg = ""
            drop_item = self.npc_info.get("drop")
            if drop_item:
                inv = user_data.get("inventory") or {}
                if drop_item not in inv:
                    inv[drop_item] = 1
                    update_payload["inventory"] = inv
                    drop_msg = f"\n**고유 전리품 획득:** {drop_item}"
                else:
                    drop_msg = "\n이미 전리품을 보유 중입니다."

            # 제작 재료 드랍 (/제작) — 일반 전투·네임드·보스 공통
            mat_drops = roll_material_drops(self.npc_info["name"])
            if mat_drops:
                inv = update_payload.get("inventory")
                if inv is None:
                    inv = user_data.get("inventory") or {}
                add_materials(inv, mat_drops)
                update_payload["inventory"] = inv
                drop_msg += f"\n**제작 재료 획득:** {format_material_drops(mat_drops)}"

            # 일일 의뢰 집계 (NPC 전투 승리)
            daily_blob = get_title_blob(user_data)
            ach_stats = daily_blob["stats"]                      # 업적용 카운터
            ach_stats["fight_wins"] += 1
            if self.npc_info.get("named"):
                ach_stats["named_kills"] += 1
            if self.npc_info["name"] in BOSS_NAMES:
                ach_stats["boss_kills"] += 1
            if self.npc_info["name"].startswith(ELITE_PREFIX):
                ach_stats["elite_boss_kills"] += 1
            after_info = {
                **user_data, "money": new_money, "honor": new_honor,
                "inventory": update_payload.get("inventory", user_data.get("inventory"))
            }
            daily_msg = daily_record(daily_blob, "fight", after_info)
            daily_msg += format_new_titles(evaluate_new_titles(daily_blob, after_info))   # 새 칭호·업적 안내
            update_payload["data"] = daily_blob

            try:
                _require_db()
                supabase.table("user_data").update(update_payload).eq("user_id", str(self.user.id)).execute()
            except Exception as e:
                print(f"DB Error (NPC Battle Win): {e}")
            # 보스 출현 판정 (서버에 보스가 없을 때만 / 보스·네임드 처치로는 출현하지 않음)
            guild_id = interaction.guild_id if interaction.guild else None
            is_boss_fight = self.npc_info["name"] in BOSS_NAMES
            if guild_id and is_boss_fight:   # 보스는 처치된 순간에만 사라집니다
                set_active_boss(guild_id, None)
                if boss_challengers.get(guild_id) is self:
                    boss_challengers.pop(guild_id, None)
            if guild_id and not is_boss_fight and not self.npc_info.get("named") and not active_boss_guilds.get(guild_id):
                roll = random.random()
                acc = 0.0
                for kind, chance in BOSS_SPAWN_TABLE:   # 확률을 누적해서 한 번의 주사위로 어느 보스가 나올지 정함
                    acc += chance
                    if roll < acc:
                        set_active_boss(guild_id, kind)
                        boss_spawn_kind = kind
                        boss_spawn_msg = BOSS_KINDS[kind]["spawn_msg"]
                        break

            embed = discord.Embed(
                title=f"[전투 승리] {self.npc_info['name']} 격퇴!",
                description=f"**{self.user.mention}** 님이 **{self.npc_info['name']}**(을)를 완벽하게 쓰러뜨렸습니다!\n\n"
                            f"**획득 보상금:** `${earned_money:,}`\n"
                            f"**획득 명예:** `+{earned_honor} pt`\n\n"
                            f"**현재 보유 자금:** `${new_money:,}`\n"
                            f"**현재 명예:** `{new_honor} pt`"
                            f"{drop_msg}{daily_msg}",  # 전리품 + 일일 의뢰 (보스 출현 안내는 아래에서 별도 메시지로 전송)
                color=discord.Color.green()
            )
        else:
            # 턴 초과 패배("turn_limit") 또는 사망 패배("loss")
            penalty = min(current_money, NPC_LOSS_PENALTY)
            new_money = max(0, current_money - penalty)

            try:
                _require_db()
                supabase.table("user_data").update({"money": new_money}).eq("user_id", str(self.user.id)).execute()
            except Exception as e:
                print(f"DB Error (NPC Battle Loss): {e}")

            boss_left = ""
            if self.npc_info["name"] in BOSS_NAMES:   # 보스전 패배: 보스는 남아 있고, 다음 도전자는 체력이 가득 찬 보스와 싸움
                g = interaction.guild_id if interaction.guild else None
                if g and boss_challengers.get(g) is self:
                    boss_challengers.pop(g, None)
                boss_left = "\n\n**보스는 아직 쓰러지지 않았습니다.** 누구든 다시 `/보스`로 도전할 수 있습니다. (도전할 때마다 보스 체력은 가득 찬 상태로 시작)"
            reason_title = "[시간 초과 패배]" if outcome == "turn_limit" else "[전투 패배]"
            reason_desc = f"지정된 **{self.max_turns}턴**이 지나 작전에 실패했습니다." if outcome == "turn_limit" else f"**{self.npc_info['name']}**에게 쓰러졌습니다."

            embed = discord.Embed(
                title=f"{reason_title} {self.user.display_name} 패배...",
                description=f"**{self.user.mention}** 님이 {reason_desc}\n\n"
                            f"**치료/패널티 지출:** `${penalty:,}`\n"
                            f"**남은 자금:** `${new_money:,}`{boss_left}",
                color=discord.Color.red()
            )

        log_text = "\n".join(self.logs[-8:])
        embed.add_field(name="최종 전투 기록", value=f"```diff\n{log_text}\n```", inline=False)

        if self.user.avatar:
            embed.set_thumbnail(url=self.user.avatar.url)

        # 전투 종료 메시지 업데이트 (실패해도 보상·보스 처리는 이미 끝났으므로 로그를 남기고 새 메시지로 대신 알림)
        try:
            await interaction.edit_original_response(embed=embed, view=self)
        except Exception as e:
            print(f"[전투 결과 수정 실패] {type(e).__name__}: {e}")
            try:
                await interaction.followup.send(embed=embed)
            except Exception as e2:
                print(f"[전투 결과 전송 실패] {type(e2).__name__}: {e2}")

        # 보스 출현 공지: 결과 임베드와 별개의 공개 메시지. 공지를 못 보냈다면 보스도 취소해서 '조용한 보스'가 생기지 않게 함
        if boss_spawn_kind and guild_id:
            print(f"[보스 출현] guild={guild_id} kind={boss_spawn_kind}")
            try:
                boss_embed = discord.Embed(
                    title="[보스 출현]",
                    description=boss_spawn_msg.strip(),
                    color=BOSS_KINDS[boss_spawn_kind]["color"]
                )
                spawn_kwargs = {"embed": boss_embed}
                spawn_file = boss_image_file(BOSS_KINDS[boss_spawn_kind]["data"])
                if spawn_file:
                    boss_embed.set_image(url=f"attachment://{BOSS_KINDS[boss_spawn_kind]['data']['image']}")
                    spawn_kwargs["file"] = spawn_file
                await interaction.followup.send(**spawn_kwargs)
            except Exception as e:
                print(f"[보스 공지 실패 → 출현 취소] {type(e).__name__}: {e}")
                if active_boss_guilds.get(guild_id) == boss_spawn_kind:
                    set_active_boss(guild_id, None)

        # 승패와 상관없이, 그리고 전투가 어떤 결과로 끝났든 /보스 쿨타임은 무조건 즉시 초기화 (/전투·/네임드 는 쿨타임 자체가 없음)
        if self.user.id in npc_cooldowns:
            del npc_cooldowns[self.user.id]

@bot.tree.command(name="전투", description="[조직원 전용] NPC 이름을 직접 입력하여 1:1 턴제 전투를 시작합니다.")
@app_commands.describe(npc_name="전투를 벌일 NPC 이름 (예: 골목길 깡패, 조직의 배신자)")
async def fight_npc(interaction: discord.Interaction, npc_name: str):
    if not await check_maintenance(interaction): return
    if not await check_membership(interaction): return
    if await check_jail(interaction): return

    user = interaction.user

    # /전투 는 쿨타임이 없습니다. (전투 화면이 끝날 때까지 다른 명령어가 잠기므로 동시에 여러 판은 열리지 않음)

    # 텍스트 입력값과 NPC 목록 매칭 확인
    target_npc = NPC_DATA.get(npc_name.strip())
    if not target_npc:
        # 등록된 NPC 이름을 알려주는 에러 메시지 반환
        available_npcs = ", ".join([f"`{name}`" for name in NPC_DATA.keys()])
        await interaction.response.send_message(
            f"**[{npc_name}]** 은(는) 존재하지 않는 NPC입니다.\n"
            f"사용 가능한 NPC 목록: {available_npcs}",
            ephemeral=True
        )
        return

    await interaction.response.defer()

    # 유저 전투 스탯 계산 (명예 + 보유 장비)
    user_stats = get_user_data(user.id)
    inventory = user_stats.get("inventory", {})
    base_honor = user_stats.get("honor", 0)

    user_combat_stats = compute_combat_stats(base_honor, inventory)

    view = NPCBattleView(user=user, user_stats_dict=user_combat_stats, npc_info=target_npc)
    embed = view.get_embed()

    msg = await interaction.followup.send(embed=embed, view=view)
    view.message = msg


# === [조직원 전용] `/보스` 명령어 ===
@bot.tree.command(name="보스", description="[조직원 전용] 전투 후 등장한 최강의 보스와 목숨을 건 결전을 시작합니다.")
async def fight_boss(interaction: discord.Interaction):
    if not await check_maintenance(interaction): return
    if not await check_membership(interaction): return
    if await check_jail(interaction): return

    user = interaction.user
    guild_id = interaction.guild_id if interaction.guild else None

    if not guild_id or not active_boss_guilds.get(guild_id, False):
        await interaction.response.send_message(
            "현재 이 서버에 보스가 등장하지 않았습니다.\n"
            f"일반 `/전투` 승리 시 낮은 확률로 출현합니다. ({boss_chance_text()})",
            ephemeral=True
        )
        return

    now = datetime.now()
    if user.id in npc_cooldowns:
        remaining = int((npc_cooldowns[user.id] - now).total_seconds())
        if remaining > 0:
            await interaction.response.send_message(
                f"이전 전투의 부상이 심합니다. **{remaining}초** 뒤에 도전할 수 있습니다.",
                ephemeral=True
            )
            return

    boss_kind = active_boss_guilds.get(guild_id)   # BOSS_KINDS 의 키 ("dark" / "eden" / "ultimate" ...)

    # 보스전은 서버에서 한 번에 한 명만 (보스는 처치될 때까지 남아 있으므로, 도전자가 끝난 뒤 다시 도전할 수 있음)
    current = boss_challengers.get(guild_id)
    if current is not None and not current.is_finished() and current.user.id != user.id:
        await interaction.response.send_message(
            f"지금 {current.user.mention} 님이 보스와 교전 중입니다. 전투가 끝난 뒤에 도전해 주세요. (보스는 처치될 때까지 사라지지 않습니다)",
            ephemeral=True
        )
        return
    npc_cooldowns[user.id] = now + timedelta(seconds=BOSS_COOLDOWN_SEC)

    await interaction.response.defer()

    try:
        user_stats = get_user_data(user.id)
        honor = user_stats.get("honor", 0)
        inventory = user_stats.get("inventory", {})

        user_combat_stats = compute_combat_stats(honor, inventory)

        # 서버에 떠 있는 보스 종류에 맞는 보스와 전투 (둘 다 15턴 제한)
        kind_info = BOSS_KINDS.get(boss_kind) or BOSS_KINDS["dark"]
        boss_info, boss_title = kind_info["data"], kind_info["title"]
        view = NPCBattleView(user=user, user_stats_dict=user_combat_stats, npc_info=boss_info, max_turns=15)   # 보스 체력은 매번 가득 찬 상태
        boss_challengers[guild_id] = view
        embed = view.get_embed()
        embed.title = boss_title

        send_kwargs = {"embed": embed, "view": view}
        boss_file = boss_image_file(boss_info)
        if boss_file:
            send_kwargs["file"] = boss_file
        else:
            embed.set_image(url=None)   # 그림 파일이 없으면 깨진 이미지 표시를 막음
        view.message = await interaction.followup.send(**send_kwargs)
    except Exception as e:
        await report_error("/보스 (전투 시작)", e, interaction)
        boss_challengers.pop(guild_id, None)
        npc_cooldowns.pop(user.id, None)   # 보스는 그대로 남아 있음
        await interaction.followup.send("보스전을 시작하는 중 오류가 발생했습니다. 보스는 그대로 남아 있으니 다시 시도해주세요.", ephemeral=True)

# === [조직원 전용] `/네임드` 명령어 ===
@bot.tree.command(name="네임드", description="[조직원 전용] 강화된 네임드 몬스터에 도전합니다. (명예 조건 / 고유 전리품)")
@app_commands.describe(monster="도전할 네임드 몬스터")
@app_commands.choices(monster=[
    app_commands.Choice(name=f"{m['name']} (명예 {m['min_honor']:,}+)", value=k) for k, m in NAMED_MONSTERS.items()
])
async def fight_named(interaction: discord.Interaction, monster: app_commands.Choice[str]):
    if not await check_maintenance(interaction): return
    if not await check_membership(interaction): return
    if await check_jail(interaction): return

    user = interaction.user
    target = NAMED_MONSTERS.get(monster.value)
    if not target:
        await interaction.response.send_message("존재하지 않는 네임드 몬스터입니다.", ephemeral=True)
        return

    user_stats = get_user_data(user.id)
    honor = user_stats.get("honor", 0)
    if honor < target["min_honor"]:
        await interaction.response.send_message(
            f"**{target['name']}** 은(는) 명예 **{target['min_honor']:,}pt** 이상이어야 도전할 수 있습니다. (현재 {honor:,}pt)",
            ephemeral=True
        )
        return

    await interaction.response.defer()

    stats = compute_combat_stats(honor, user_stats.get("inventory", {}))
    view = NPCBattleView(user=user, user_stats_dict=stats, npc_info=target, max_turns=target["max_turns"])
    embed = view.get_embed()
    embed.title = f"[네임드 전투] {target['name']} - 턴 {view.turn_count} / {view.max_turns}"
    send_kwargs = {"embed": embed, "view": view}
    named_file = boss_image_file(target)   # 보스와 같은 방식으로 그림 첨부
    if named_file:
        send_kwargs["file"] = named_file
    elif target.get("image"):
        embed.set_image(url=None)          # 그림 파일이 없으면 깨진 이미지 표시를 막음
    view.message = await interaction.followup.send(**send_kwargs)

# 쿨타임 관리를 위한 사전
duel_cooldowns = {}
npc_cooldowns = {}


# ==========================================================================
# [합친 모듈] duel.py
#   턴제 PVP 결투 (/결투)
# ==========================================================================
# === 턴제 PVP 결투 시스템 (버튼 선택형) ===
class DuelBattleView(GuardedView):
    holds_command_lock = True   # 결투가 끝날 때까지 (신청한 사람의) 다른 명령어 실행 차단

    def __init__(self, challenger: discord.Member, target: discord.Member, bet: int, c_stats: dict, t_stats: dict, cross: bool = False):
        super().__init__(timeout=30)
        self.challenger = challenger
        self.target = target
        self.bet = bet
        self.cross = cross   # 조직원 vs 경찰 (진영이 다른 결투)

        # 스탯 및 전투 데이터 초기화
        self.players = {
            challenger.id: {"member": challenger, "hp": c_stats["hp"], "max_hp": c_stats["hp"], "atk": c_stats["atk"], "def": c_stats["def"], "skill": SkillState()},
            target.id: {"member": target, "hp": t_stats["hp"], "max_hp": t_stats["hp"], "atk": t_stats["atk"], "def": t_stats["def"], "skill": SkillState()}
        }

        self.current_turn_id = challenger.id  # 도전자부터 시작
        self.turn_count = 1
        self.logs = [f"{challenger.display_name} 님과 {target.display_name} 님의 결투가 시작되었습니다!"]
        self.message = None
        self.winner = None
        self._refresh_buttons()

    def _refresh_buttons(self):
        """지금 차례인 사람의 스킬 상태에 맞춰 버튼을 갱신"""
        skill = self.players[self.current_turn_id]["skill"]
        for key, btn in (("smash", self.smash_btn), ("rally", self.rally_btn), ("finisher", self.finisher_btn)):
            w = skill.wait(key)
            btn.label = SKILL_LABELS[key] + (f" ({w})" if w else "")
            btn.disabled = bool(w)

    def get_embed(self) -> discord.Embed:
        c_p = self.players[self.challenger.id]
        t_p = self.players[self.target.id]

        current_turn_user = self.players[self.current_turn_id]["member"]

        embed = discord.Embed(
            title=(f"[조직 vs 치안국 결투] 턴 {self.turn_count}" if self.cross else f"[패밀리 결투 아레나] 턴 {self.turn_count}"),
            description=f"현재 차례: **{current_turn_user.mention}** 님의 행동을 기다리는 중... (제한 시간 30초)",
            color=discord.Color.dark_red()
        )

        for p in (c_p, t_p):
            buff = p["skill"].buff_turns_left()
            buff_line = f"\n버프 : 공격력 +{int(SKILL_RALLY_BONUS * 100)}% ({buff}턴)" if buff else ""
            embed.add_field(
                name=f"{p['member'].display_name}",
                value=f"체력 : {p['hp']} / {p['max_hp']}\n공격력 : {p['atk']}\n방어력 : {p['def']}{buff_line}",
                inline=True
            )
        embed.add_field(name=f"스킬 ({current_turn_user.display_name})", value=self.players[self.current_turn_id]["skill"].status_text(), inline=False)

        log_text = "\n".join(self.logs[-6:])  # 최근 로그 6개만 표시
        embed.add_field(
            name="전투 현황 로그",
            value=f"```diff\n{log_text}\n```",
            inline=False
        )
        embed.set_footer(text=SKILL_FOOTER)
        return embed

    async def process_action(self, interaction: discord.Interaction, action_type: str):
        if interaction.user.id != self.current_turn_id:
            await interaction.response.send_message("지금은 당신의 차례가 아닙니다!", ephemeral=True)
            return

        attacker_id = self.current_turn_id
        defender_id = self.target.id if attacker_id == self.challenger.id else self.challenger.id

        att = self.players[attacker_id]
        defn = self.players[defender_id]
        skill = att["skill"]

        wait = skill.wait(action_type) if action_type != "attack" else ""
        if wait:
            await interaction.response.send_message(f"{SKILL_LABELS[action_type]} 은(는) 아직 사용할 수 없습니다. ({wait})", ephemeral=True)
            return

        await interaction.response.defer()

        name = att["member"].display_name
        if action_type == "rally":
            skill.use("rally")
            self.logs.append(f"+ {name}의 전투 함성! 앞으로 {SKILL_RALLY_TURNS}번의 행동 동안 공격력 +{int(SKILL_RALLY_BONUS * 100)}%")
        else:
            buffed = skill.atk_mult() > 1
            damage = skill_hit(skill, action_type, att["atk"], defn["def"], 15)
            defn["hp"] = max(0, defn["hp"] - damage)
            verb = {"attack": "공격", "smash": "강타", "finisher": "필살 일격!!"}[action_type]
            self.logs.append(f"+ {name}의 {verb}! {defn['member'].display_name}의 체력 - {damage}" + (" [함성 효과]" if buffed else ""))
            if action_type != "attack":
                skill.use(action_type)
        skill.actions += 1

        # 승리 조건 체크
        if defn["hp"] <= 0:
            self.winner = att["member"]
            await self.end_battle(interaction)
            return

        # 턴 넘기기
        self.current_turn_id = defender_id
        if self.current_turn_id == self.challenger.id:
            self.turn_count += 1

        self._refresh_buttons()
        await self.message.edit(embed=self.get_embed(), view=self)

    @discord.ui.button(label="공격하기", style=discord.ButtonStyle.danger)
    async def attack_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.process_action(interaction, "attack")

    @discord.ui.button(label="강타", style=discord.ButtonStyle.primary)
    async def smash_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.process_action(interaction, "smash")

    @discord.ui.button(label="전투 함성", style=discord.ButtonStyle.success)
    async def rally_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.process_action(interaction, "rally")

    @discord.ui.button(label="필살 일격", style=discord.ButtonStyle.secondary)
    async def finisher_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.process_action(interaction, "finisher")

    async def end_battle(self, interaction: discord.Interaction):
        self.stop()
        
        loser = self.target if self.winner == self.challenger else self.challenger
        w_stats = get_user_data(self.winner.id)
        l_stats = get_user_data(loser.id)

        new_w_honor = w_stats["honor"] + self.bet
        new_l_honor = max(0, l_stats["honor"] - self.bet)

        try:
            _require_db()
            supabase.table("user_data").update({"honor": new_w_honor}).eq("user_id", str(self.winner.id)).execute()
            supabase.table("user_data").update({"honor": new_l_honor}).eq("user_id", str(loser.id)).execute()
        except Exception as e:
            print(f"DB Error (turn duel end): {e}")

        duel_ach = ""
        try:   # 업적 카운터 (승자만)
            duel_new = await asyncio.to_thread(bump_user_stats_sync, self.winner.id, {"duel_wins": 1})
            if duel_new:
                duel_ach = f"\n\n{self.winner.mention}" + format_new_titles(duel_new)
        except Exception as e:
            print(f"Achievement error (duel): {e}")

        embed = discord.Embed(
            title="[결투 종료: 승패 결정]",
            description=(f"조직과 치안국의 자존심을 건 대결 끝에 **{self.winner.mention}** 님이 승리했습니다!\n\n" if self.cross
                         else f"피비린내 나는 공방 끝에 **{self.winner.mention}** 님이 승리했습니다!\n\n") +
                        f"**명예 변동:**\n"
                        f"• {self.winner.mention} `+{self.bet} pt` (총 {new_w_honor} pt)\n"
                        f"• {loser.mention} `-{self.bet} pt` (총 {new_l_honor} pt)" + duel_ach,
            color=discord.Color.gold()
        )
        log_text = "\n".join(self.logs[-8:])
        embed.add_field(name="최종 전투 기록", value=f"```diff\n{log_text}\n```", inline=False)

        if self.winner.avatar:
            embed.set_thumbnail(url=self.winner.avatar.url)

        for child in self.children:
            child.disabled = True

        await interaction.edit_original_response(embed=embed, view=self)

    async def on_timeout(self):
        if self.winner:
            return
        # 시간 초과 시 먼저 누른 사람의 승리 처리 혹은 무승부 처리
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(content="결투 시간이 초과되어 무승부로 종료되었습니다.", view=self)
            except Exception:
                pass

@bot.tree.command(name="결투", description="[조직원/경찰] 버튼을 눌러 턴제로 진행하는 1:1 결투를 신청합니다. (조직원과 경찰끼리도 가능)")
@app_commands.describe(member="결투를 신청할 상대 (조직원 또는 경찰)", 베팅명예=f"걸고 싸울 명예 포인트 (1~{DUEL_MAX_BET}pt, 기본 10pt)")
async def duel_member(interaction: discord.Interaction, member: discord.Member, 베팅명예: app_commands.Range[int, 1, DUEL_MAX_BET] = 10):
    if not await check_maintenance(interaction): return
    if not await check_membership(interaction): return
    if await check_jail(interaction): return

    user = interaction.user

    if member.id == user.id:
        await interaction.response.send_message("자기 자신과는 결투할 수 없습니다!", ephemeral=True)
        return
    if member.bot:
        await interaction.response.send_message("봇과는 결투할 수 없습니다.", ephemeral=True)
        return

    now = datetime.now()
    if user.id in duel_cooldowns:
        remaining = int((duel_cooldowns[user.id] - now).total_seconds())
        if remaining > 0:
            await interaction.response.send_message(f"결투 여파로 지쳐 있습니다. **{remaining}초** 뒤에 다시 도전할 수 있습니다.", ephemeral=True)
            return

    mafia_role = discord.utils.get(interaction.guild.roles, name="조직원")
    police_role = discord.utils.get(interaction.guild.roles, name="경찰")
    target_mafia = bool(mafia_role and mafia_role in member.roles)
    target_police = bool(police_role and police_role in member.roles)
    if not target_mafia and not target_police:
        await interaction.response.send_message(f"{member.mention} 님은 조직원도 경찰도 아니므로 결투할 수 없습니다.", ephemeral=True)
        return
    user_police = bool(police_role and police_role in user.roles)
    cross = user_police != target_police   # 한쪽은 경찰, 한쪽은 조직원

    if member.id in jail_list and now < jail_list[member.id]:
        await interaction.response.send_message(f"{member.mention} 님은 현재 감옥에 수감 중이어서 결투할 수 없습니다.", ephemeral=True)
        return

    user_stats = get_user_data(user.id)
    target_stats = get_user_data(member.id)

    if user_stats["honor"] < 베팅명예:
        await interaction.response.send_message(f"보유한 명예 포인트가 부족합니다! (현재 명예: {user_stats['honor']} pt)", ephemeral=True)
        return
    if target_stats["honor"] < 베팅명예:
        await interaction.response.send_message(f"상대방의 명예 포인트가 부족하여 해당 금액으로 결투할 수 없습니다.", ephemeral=True)
        return

    duel_cooldowns[user.id] = now + timedelta(seconds=120)   # 검사를 모두 통과한 뒤에만 쿨타임을 건다
    await interaction.response.defer()

    # 장비 스탯 계산 함수
    def get_combat_stats(inventory: dict, base_honor: int):
        return compute_combat_stats(base_honor, inventory)

    c_stats = get_combat_stats(user_stats.get("inventory", {}), user_stats["honor"])
    t_stats = get_combat_stats(target_stats.get("inventory", {}), target_stats["honor"])

    view = DuelBattleView(challenger=user, target=member, bet=베팅명예, c_stats=c_stats, t_stats=t_stats, cross=cross)
    embed = view.get_embed()

    msg = await interaction.followup.send(embed=embed, view=view)
    view.message = msg
    
class WeaponShopSelect(discord.ui.Select):
    def __init__(self, owner: discord.Member):
        self.owner = owner
        options = [
            discord.SelectOption(
                label=item["name"],
                description=f"가격: ${item['price']:,} | {item['desc']}",
                value=item["id"]
            ) for item in WEAPON_SHOP_ITEMS
        ]
        super().__init__(placeholder="구매할 물품을 선택하세요...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        if interaction.user != self.owner:
            await interaction.followup.send("다른 사람의 상점 메뉴는 조작할 수 없습니다.", ephemeral=True)
            return

        user = interaction.user
        user_stats = get_user_data(user.id)
        current_money = user_stats.get("money", 0)
        inventory = user_stats.get("inventory", {})

        target_item = next((item for item in WEAPON_SHOP_ITEMS if item["id"] == self.values[0]), None)
        if not target_item:
            await interaction.followup.send("존재하지 않는 아이템입니다.", ephemeral=True)
            return

        item_name = target_item["name"]
        item_price = target_item["price"]

        if current_money < item_price:
            await interaction.followup.send(f"자금이 부족합니다! (필요: `${item_price:,}`, 보유: `${current_money:,}`)", ephemeral=True)
            return

        current_money -= item_price
        inventory[item_name] = inventory.get(item_name, 0) + 1

        try:
            _require_db()
            supabase.table("user_data").update({
                "money": current_money,
                "inventory": inventory
            }).eq("user_id", str(user.id)).execute()
        except Exception as e:
            print(f"DB Error (weapon shop buy): {e}")
            await interaction.followup.send("구매 처리 중 DB 오류가 발생했습니다.", ephemeral=True)
            return

        embed = discord.Embed(
            title="[무기 상점] 구매 완료",
            description=f"**{user.mention}** 님이 **{item_name}**을(를) 구매하셨습니다!\n\n"
                        f"**지불 금액:** `${item_price:,}`\n"
                        f"**잔여 자금:** `${current_money:,}`\n"
                        f"**효과:** `{target_item['desc']}`",
            color=discord.Color.green()
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

class WeaponShopView(discord.ui.View):
    def __init__(self, owner: discord.Member):
        super().__init__(timeout=60)
        self.add_item(WeaponShopSelect(owner))
