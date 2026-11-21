"""정보 검색, /능력치, /도움말, /탈옥

합친 모듈: info.py, stats.py, help.py, jailbreak.py
"""

import asyncio
import discord
import random
import re
import time
from discord import app_commands

from .core import bot, GuardedView, jail_list
from .status import (
    ATTENDANCE_TITLE_STACKS,
    check_jail,
    compute_combat_stats,
    CORRUPT_ARREST_BONUS_HONOR,
    CORRUPT_ARREST_JAIL_SEC,
    CRAFT_RECIPES,
    DAILY_QUESTS,
    DONATE_UNIT,
    ELITE_TROPHY_STATS,
    get_user_data,
    join_requirement_text,
    KARMA_EVIL_MAX,
    KARMA_GOOD_MIN,
    PARTTIME_KARMA,
    PICKPOCKET_KARMA_FAIL,
    PICKPOCKET_KARMA_OK,
    save_status,
    SERVICE_KARMA,
    TITLES,
    update_user_location,
    WEAPON_TIERS,
    WEEKLY_ALL_CLEAR_BONUS,
    WEEKLY_QUESTS,
    WEEKLY_TITLE_WEEKS,
)
from .admin import check_maintenance, check_membership
from .battle import (
    boss_chance_text,
    boss_reward_text,
    SKILL_FINISHER_MIN_TURN,
    SKILL_FINISHER_MULT,
    SKILL_RALLY_BONUS,
    SKILL_RALLY_CD,
    SKILL_RALLY_TURNS,
    SKILL_SMASH_CD,
    SKILL_SMASH_MULT,
)
from .economy import CRAFT_PREFIX, crafted_gear_best, POLICE_SHOP_ITEMS
from .family import _is_jailed, _is_mafia, FAMILY_CREATE_MIN_HONOR
from .police import (
    POLICE_BADGE_HONOR_BONUS,
    POLICE_BADGE_NAME,
    POLICE_INSIGNIA_HONOR_BONUS,
    POLICE_INSIGNIA_NAME,
    POLICE_RADIO_ARREST_BONUS,
    POLICE_RADIO_NAME,
    POLICE_RADIO_OP_BONUS,
    POLICE_TERMINAL_COST_DISCOUNT,
    POLICE_TERMINAL_NAME,
    POLICE_TERMINAL_PRIZE_BONUS,
    SMUGGLE_COOLDOWN_SEC,
    SMUGGLE_IMMUNITY_SEC,
    SMUGGLE_MAX_BRIBE,
    SMUGGLE_MIN_BRIBE,
    SMUGGLE_REJECT_FINE,
    SMUGGLE_REJECT_HONOR_LOSS,
    SMUGGLE_REJECT_WANTED_SEC,
)


# ==========================================================================
# [합친 모듈] info.py
#   /정보
# ==========================================================================
# === [공용] 통합 `/정보` 명령어 (확장판) ===
@bot.tree.command(name="정보", description="[공용] 특정 명령어, 아이템 또는 세계관 설정을 상세하게 검색합니다.")
@app_commands.describe(keyword="알고 싶은 항목 (예: 전투, 보스, 슬롯, 칭호, 유토피아 오브 에덴)")
@app_commands.choices(keyword=[
    app_commands.Choice(name="전투 & 보스 시스템", value="전투"),
    app_commands.Choice(name="히든 보스 (/보스)", value="보스"),
    app_commands.Choice(name="카지노 & 도박 (/도박)", value="도박"),
    app_commands.Choice(name="칭호 시스템 (/칭호)", value="칭호"),
    app_commands.Choice(name="일일 의뢰 (/일퀘)", value="일퀘"),
    app_commands.Choice(name="주간 의뢰 (/주간)", value="주간"),
    app_commands.Choice(name="성향 · 전직 조건 · 부패 경찰", value="성향"),
    app_commands.Choice(name="경찰 상점 (/경찰상점)", value="경찰상점"),
    app_commands.Choice(name="통합 설정 (/설정)", value="설정"),
    app_commands.Choice(name="네임드 몬스터 (/네임드)", value="네임드"),
    app_commands.Choice(name="밀수 거래 (/밀수)", value="밀수"),
    app_commands.Choice(name="조직(패밀리) (/조직)", value="조직"),
    app_commands.Choice(name="조직 레이드 (/레이드)", value="레이드"),
    app_commands.Choice(name="Utopia Of Eden (세계관)", value="유토피아 오브 에덴")
])
async def info_command(interaction: discord.Interaction, keyword: str):
    from .family import FAMILY_CREATE_COST, FAMILY_CREATE_MIN_HONOR, FAMILY_MAX_MEMBERS  # 순환 import 방지: 호출 시점에 가져옴
    if not await check_maintenance(interaction): return

    kw = keyword.strip()

    # 1. 시스템 및 명령어 정보 사전
    command_infos = {
        "네임드": {
            "title": "시스템: /네임드 [몬스터]",
            "desc": "일반 NPC보다 훨씬 강한 강화 몬스터에 도전합니다.\n"
                    "• **입장 조건:** 마르코 500pt / 비앙카 1,500pt / 보로닌 2,500pt / 처형자 4,000pt (명예)\n"
                    "• **특징:** 12턴 제한, 체력이 절반 아래가 되면 **격노**하여 공격력이 크게 오릅니다.\n"
                    "• **보상:** 많은 자금 + 소량의 명예, 최초 처치 시 고유 전리품 (칭호 해금)\n"
                    "• **쿨타임:** 없음 (전투가 끝나면 바로 다시 도전할 수 있습니다)",
        },
        "전투": {
            "title": "시스템: /전투 [npc이름]",
            "desc": "지정한 NPC와 1:1 턴제 결투를 벌입니다.\n"
                    "• **특징:** 제한 턴(10턴) 내에 처치해야 승리하며, 패배 시 치료비 패널티가 발생합니다.\n"
                    f"• **행동:** `공격하기` 외에 스킬 3가지를 쓸 수 있습니다. (스킬을 쓰면 그 턴의 기본 공격을 대신합니다)\n"
                    f"  - **강타:** 대미지 x{SKILL_SMASH_MULT} (사용 후 {SKILL_SMASH_CD}번째 행동부터 재사용)\n"
                    f"  - **전투 함성:** 이후 {SKILL_RALLY_TURNS}번의 행동 동안 공격력 +{int(SKILL_RALLY_BONUS * 100)}% (재사용 {SKILL_RALLY_CD}행동 뒤)\n"
                    f"  - **필살 일격:** 대미지 x{SKILL_FINISHER_MULT}, {SKILL_FINISHER_MIN_TURN}턴째부터 전투당 1회\n"
                    f"  - 스킬이 생긴 만큼 NPC·보스 체력도 늘어나 있어서, 스킬을 잘 써야 기존과 같은 난이도입니다. `/결투`에서도 같은 스킬을 씁니다.\n"
                    "• **쿨타임:** 없음 (전투가 끝나면 바로 다음 전투를 시작할 수 있습니다)\n"
                    f"• **히든 이벤트:** 전투 승리 시 낮은 확률로 보스가 서버에 출현합니다! ({boss_chance_text()} / 한 번에 한 마리)",
        },
        "보스": {
            "title": "시스템: /보스 (히든 보스 토벌)",
            "desc": "전투 후 살기가 감지되었을 때 출현하는 최종 히든 보스전입니다.\n"
                    f"• **출현 조건:** 일반 `/전투` 승리 시 보스마다 각각 확률로 경보가 발생해야 입장 가능합니다. ({boss_chance_text()})\n"
                    "• **보스는 누군가 처치할 때까지 서버에 남습니다.** 도전에 실패해도 사라지지 않으며, 도전할 때마다 보스 체력은 가득 찬 상태로 시작합니다. 보스전은 한 번에 한 명만 도전할 수 있습니다.\n"
                    f"• **보상:** 격퇴 성공 시 막대한 자금과 명예, 제작 재료를 획득!\n{boss_reward_text()}\n"
                    "• **강화 보스:** 보스마다 0.3% 확률로 훨씬 강한 강화판이 출현합니다. 체력·공격력·방어력이 매우 높고 격노하지만, "
                    "자금·명예·제작 재료가 크게 늘고 능력치가 오르는 전용 전리품과 칭호·업적을 얻습니다.\n"
                    "• **권장:** 명예 약 5,000pt + 수상한 주사기 / 슈퍼 울트라 주먹",
        },
        "도박": {
            "title": "시스템: /도박 [슬롯/동전/블랙잭/규칙]",
            "desc": "보유 자금을 걸고 한판 승부를 벌입니다.\n"
                    "• **슬롯:** 3개 일치 시 고배당, 잭팟(7⃣7⃣7⃣) 시 100배 지급\n"
                    "• **동전:** 48% 확률로 베팅금의 2배 획득\n"
                    "• **블랙잭:** 딜러와 21에 가까운 수로 겨루는 카드 게임 (블랙잭 2.5배)",
        },
        "칭호": {
            "title": "시스템: /칭호",
            "desc": "조건을 달성하여 업적 칭호를 획득하고 프로필에 장착합니다.\n"
                    "• **효과:** `/프로필` 명령어 실행 시 내 아날로그 증명서에 장착된 칭호가 예쁘게 각인됩니다.",
        },
        "일퀘": {
            "title": "시스템: /일퀘",
            "desc": "매일 주어지는 일일 의뢰 4개를 모두 완료하면 **출석 1스택**이 쌓입니다.\n"
                    + "".join(f"• {q['name']} {q['target']}회 ({q['hint']})\n" for q in DAILY_QUESTS)
                    + "• **쿨타임:** 4개를 모두 완료한 시각부터 24시간 뒤에 새 의뢰가 열립니다.\n"
                    f"• **보상:** 출석 {ATTENDANCE_TITLE_STACKS}스택 달성 시 {TITLES['attendance_month']['name']} 칭호",
        },
        "주간": {
            "title": "시스템: /주간",
            "desc": "매주 월요일 00:00(한국 시간)에 초기화되는 주간 의뢰입니다. 이번 주에 한 활동만 집계되며, 완료한 의뢰는 `/주간` 의 버튼으로 보상을 받습니다.\n"
                    "**마피아**\n" + "".join(f"• {q['name']} {q['target']}회 → ${q['money']:,}" + (f" / 명예 +{q['honor']}" if q['honor'] else "") + "\n" for q in WEEKLY_QUESTS["mafia"])
                    + "**경찰**\n" + "".join(f"• {q['name']} {q['target']}회 → ${q['money']:,}" + (f" / 명예 +{q['honor']}" if q['honor'] else "") + "\n" for q in WEEKLY_QUESTS["police"])
                    + f"• **전부 완료 보너스:** ${WEEKLY_ALL_CLEAR_BONUS['money']:,} / 명예 +{WEEKLY_ALL_CLEAR_BONUS['honor']}\n"
                    f"• **칭호:** 모든 의뢰를 완료한 주가 {WEEKLY_TITLE_WEEKS}주가 되면 {TITLES['weekly_master']['name']}",
        },
        "성향": {
            "title": "시스템: 성향(선/악) · 전직 조건 · 부패 경찰",
            "desc": f"시민은 활동에 따라 성향 점수가 쌓입니다. (선 {KARMA_GOOD_MIN}점 이상 / 악 {KARMA_EVIL_MAX}점 이하 / 그 사이는 중립, `/프로필` 에서 확인)\n"
                    f"• **선한 활동:** `/아르바이트`(+{PARTTIME_KARMA}) `/봉사`(+{SERVICE_KARMA}) `/기부`(${DONATE_UNIT:,}당 +1)\n"
                    f"• **악한 활동:** `/소매치기`(성공 {PICKPOCKET_KARMA_OK}, 실패 {PICKPOCKET_KARMA_FAIL} + 수배)\n\n"
                    + join_requirement_text() + "\n\n"
                    "**부패 경찰**\n"
                    f"• `/검거` 로 수배 중이 아닌 시민도 체포(수감 {CORRUPT_ARREST_JAIL_SEC // 60}분, 벌금 갈취), `/갈취` 로 시민의 돈을 뜯어낼 수 있습니다.\n"
                    f"• 들키면 민원이 접수되어 수배되고, 정식 경찰이 검거하면 추가 명예(+{CORRUPT_ARREST_BONUS_HONOR})를 받습니다.\n"
                    "• 프로필에 '부패 경찰'로 표시됩니다.",
        },
        "설정": {
            "title": "시스템: /설정",
            "desc": "임무 실패 방지권 자동 사용 여부, 이벤트 긴급 알림 등 내 플레이 환경을 직접 커스텀할 수 있는 설정 창입니다.",
        },
        "이동": {
            "title": "시스템: /이동 [지역명]",
            "desc": "레벤하임(수도), 던베르크(공업), 블랙워드(빈민가), 에르느코항(항구), 로젠펠트(유흥가) 등 5대 거점으로 이동합니다.\n"
                    "• 거점마다 암시장에서 판매하는 아이템 종류가 달라집니다.",
        },
        "아르바이트": {
            "title": "시스템: /아르바이트",
            "desc": "시민/공용 전용 안전 자금 수급처입니다. 위험 부담 없이 정직한 노동으로 소량의 돈을 획득합니다.",
        },
        "송금": {
            "title": "명령어: /송금 [대상] [금액]",
            "desc": "보유 자금을 다른 조직원에게 안전하게 이체합니다. (자기 자신 송금 불가)",
        },
        "검거": {
            "title": "명령어: /검거 [대상] (경찰 전용)",
            "desc": "지명수배 중인 마피아를 현장에서 체포하여 감옥에 5분간 수감하고 포상금을 획득합니다. 체포 시 대상 조직원의 아이템 중 랜덤 1~2종(각 1개)을 압류해 내 인벤토리로 가져옵니다. (재료와 네임드 전리품 등 거래 불가 아이템은 제외)",
        },
        "밀수": {
            "title": "명령어: /밀수 [경찰] [뇌물] (조직원 전용)",
            "desc": f"경찰에게 뇌물(${SMUGGLE_MIN_BRIBE:,}~${SMUGGLE_MAX_BRIBE:,})을 제안합니다. 경찰이 **수락**하면 뇌물이 경찰에게 넘어가고 {SMUGGLE_IMMUNITY_SEC // 60}분간 그 경찰은 나를 `/검거`할 수 없습니다. "
                    f"**거절**하면 {SMUGGLE_REJECT_WANTED_SEC}초 지명수배 + 벌금 ${SMUGGLE_REJECT_FINE:,} + 명예 -{SMUGGLE_REJECT_HONOR_LOSS}pt 패널티를 받습니다. 쿨타임 {SMUGGLE_COOLDOWN_SEC // 60}분.",
        },
        "조직": {
            "title": "명령어: /조직 [생성·초대·정보·기부·탈퇴·추방·위임·해산·목록]",
            "desc": f"마피아 조직원끼리 **조직(클럽)** 을 만들어 단체 활동을 합니다.\n"
                    f"• **/조직 생성:** 명예 {FAMILY_CREATE_MIN_HONOR}pt 이상 + ${FAMILY_CREATE_COST:,} 필요, 최대 {FAMILY_MAX_MEMBERS}명\n"
                    "• **/조직 초대:** 조직장이 조직원을 초대 (상대가 수락해야 가입)\n"
                    "• **/조직 기부:** 금고에 자금 기부 → 레이드 작전비로 사용\n"
                    "• **/조직 목록:** 레이드 점수 랭킹 TOP 10",
        },
        "레이드": {
            "title": "명령어: /레이드 [시작·정보]",
            "desc": "조직원이 함께 레이드 보스를 잡는 협동 콘텐츠입니다. `/레이드 정보` 에서 보스별 조건·보상과 전투 방법을 볼 수 있습니다.\n"
                    "공격 / 응급처치 / 전투 함성 3가지 버튼으로 진행하며, 보상은 참여도(딜량)에 따라 나뉩니다.",
        },
        "경찰상점": {
            "title": "명령어: /경찰상점 (경찰 전용)",
            "desc": "치안국 전용 보급품을 구매합니다. 보유만 해도 효과가 적용됩니다.\n"
                    "• **치안국 강화 방탄복:** 방어력 +35\n"
                    "• **기동타격대 무전기:** `/검거` 포상금 +20% / `/작전` 성공률 +5%\n"
                    "• **특급 수사관 황금 배지:** 방어력 +50 / `/검거`·`/순찰`·`/심문`·`/작전` 명예 실적 +20%\n"
                    "• **전술 방탄 헬멧:** 방어력 +25 / 체력 +1,500\n"
                    "• **치안국 제식 돌격소총:** 공격력 +200\n"
                    "• **특수작전 중장갑 전투복:** 방어력 +90 / 체력 +3,000\n"
                    "• **광역 수사망 단말기:** `/구역 탈환` 포상금 +25% / 비용 50% 할인\n"
                    "• **대테러 저격 소총:** 공격력 +350\n"
                    "• **치안총감 지휘 휘장:** 방어력 +60 / 체력 +2,000 / 명예 실적 +15%\n"
                    "능력치는 `/능력치`에서 확인할 수 있습니다.",
        },
        "무기상점": {
            "title": "명령어: /무기상점",
            "desc": "전투력을 대폭 높여주는 특수 무기(슈퍼 울트라 주먹, 주사기 등)를 구매합니다.",
        },
        "프로필": {
            "title": "명령어: /프로필 [유저]",
            "desc": "본인 또는 타 유저의 직급, 명예, 자금, 거점 상태가 담긴 아날로그 종이 증명서 이미지를 생성합니다.",
        }
    }

    # 2. 아이템 정보 사전
    item_infos = {
        "슈퍼 울트라 주먹": {
            "title": "아이템: 슈퍼 울트라 주먹",
            "desc": "• **분류:** 무기 (최상급)\n• **효과:** 공격력 +500, 최대 체력 +2,000\n• **설명:** 패밀리 최고의 파괴력을 자랑하는 전설의 주먹입니다.",
        },
        "수상한 주사기": {
            "title": "아이템: 수상한 주사기",
            "desc": "• **분류:** 무기 (특수)\n• **효과:** 공격력 +300, 체력 +1,000\n• **설명:** 신체 능력을 극단적으로 끌어올리는 위험한 약물입니다.",
        },
        "임무 실패 방지권": {
            "title": "아이템: 임무 실패 방지권",
            "desc": "• **분류:** 방어구/유틸리티\n• **효과:** 방어력 +20, 임무 실패 시 수배 자동 방어\n• **설명:** 위기 상황에서 감옥행을 모면하게 해주는 티켓입니다.",
        },
        "시간 정지 회중시계": {
            "title": "아이템: 시간 정지 회중시계",
            "desc": "• **분류:** 유틸리티\n• **효과:** 임무 쿨타임 완전 제거 (영구)\n• **설명:** 멈춘 시간 속에서 쉬지 않고 작전을 이어갈 수 있게 해주는 신비한 회중시계입니다.",
        },
    }

    # 3. 세계관 정보 사전
    world_infos = {
        "유토피아 오브 에덴": {
            "title": "세계관: Utopia Of Eden (유토피아 오브 에덴)",
            "desc": (
                "**“모두가 평등한 세상. 그곳에 불평등한 자는 존재할 수 없다.”**\n\n"
                "**기본 정보**\n"
                "• **조직 분류:** 테러조직 | **활동 국가:** 칼렌데 공화국\n"
                "• **창립자:** 에칼 데 페스토 | **주요 사상:** 공산주의\n"
                "• **주요 활동:** 선전물 제작 및 배포, 극단적 테러 활동\n"
                "• **대립 관계:** 칼렌데 중앙치안국 및 기타 세력\n\n"
                "**주요 사건: 예노호 테러 사건**\n"
                "데이브 니스코프가 여객선 예노호를 통제 불능으로 만들고 침몰시킨 참사 (사망 120명, 실종 1명)."
            )
        }
    }

    # 4. 통합 검색 매칭
    target_info = None
    combined_dict = {**command_infos, **item_infos, **world_infos}

    if kw in combined_dict:
        target_info = combined_dict[kw]
    else:
        for name, data in combined_dict.items():
            if kw in name:
                target_info = data
                break

    if not target_info:
        available_keys = list(combined_dict.keys())
        keys_str = ", ".join([f"`{k}`" for k in available_keys])
        
        await interaction.response.send_message(
            f"**[{kw}]**에 대한 정보를 찾을 수 없습니다.\n\n"
            f"**검색 가능한 전체 목록:**\n{keys_str}",
            ephemeral=True
        )
        return

    # 5. 임베드 생성 및 전송
    embed = discord.Embed(
        title=target_info["title"],
        description=target_info["desc"],
        color=discord.Color.dark_red()
    )
    embed.set_footer(text="마피아 RPG 패밀리 GM 통합 정보 시스템")

    if interaction.user.avatar:
        embed.set_thumbnail(url=interaction.user.avatar.url)

    await interaction.response.send_message(embed=embed, ephemeral=True)


# ==========================================================================
# [합친 모듈] stats.py
#   /능력치
# ==========================================================================
# ==========================================================================
# [/능력치]  전투 능력치 확인 (체력/공격력/방어력 + 구성 내역)
#   - 실제 전투에 쓰이는 compute_combat_stats 결과를 그대로 보여주고, 어디서 얼마가 붙었는지 풀어서 설명합니다.
# ==========================================================================
def combat_stats_breakdown(honor: int, inventory: dict) -> dict:
    inventory = inventory or {}
    # 상점 무기 (먼저 보유한 하나)
    shop_name, shop = None, (0, 0)
    for name, a, h in WEAPON_TIERS:
        if name in inventory:
            shop_name, shop = name, (a, h)
            break
    craft_w, craft_a = crafted_gear_best(inventory)

    if craft_w and (craft_w["atk"], craft_w["hp"]) > shop:
        weapon = {"name": craft_w["name"], "src": "제작", "atk": craft_w["atk"], "hp": craft_w["hp"]}
    elif shop_name:
        weapon = {"name": shop_name, "src": "상점", "atk": shop[0], "hp": shop[1]}
    else:
        weapon = None
    armor = {"name": craft_a["name"], "def": craft_a["def"], "hp": craft_a["hp"]} if craft_a else None

    # 보유했지만 적용되지 않는 장비
    unused = []
    for name, a, h in WEAPON_TIERS:
        if name in inventory and not (weapon and weapon["src"] == "상점" and weapon["name"] == name):
            unused.append(f"{name} (무기)")
    for r in CRAFT_RECIPES:
        if (CRAFT_PREFIX + r["name"]) not in inventory:
            continue
        if r["slot"] == "weapon" and not (weapon and weapon["src"] == "제작" and weapon["name"] == r["name"]):
            unused.append(f"{r['name']} (무기)")
        if r["slot"] == "armor" and not (armor and armor["name"] == r["name"]):
            unused.append(f"{r['name']} (방어구)")

    return {
        "base_hp": 1000 + honor * 5,
        "base_atk": 100 + int(honor * 0.5),
        "base_def": 10,
        "weapon": weapon,
        "armor": armor,
        "ticket": "임무 실패 방지권" in inventory,
        "police": [(it["name"], it.get("atk_bonus", 0), it.get("hp_bonus", 0), it.get("def_bonus", 0))
                   for it in POLICE_SHOP_ITEMS if it["name"] in inventory and (it.get("atk_bonus") or it.get("hp_bonus") or it.get("def_bonus"))],
        "elite": [(n, a, h, d) for n, (a, h, d) in ELITE_TROPHY_STATS.items() if n in inventory],
        "unused": unused,
        "total": compute_combat_stats(honor, inventory),   # 실제 전투에서 쓰는 값
    }


def build_stats_embed(target: discord.abc.User, data: dict) -> discord.Embed:
    honor = int(data.get("honor", 0) or 0)
    money = int(data.get("money", 0) or 0)
    inv = data.get("inventory") or {}
    b = combat_stats_breakdown(honor, inv)
    t = b["total"]

    embed = discord.Embed(title=f"[{target.display_name} 님의 전투 능력치]", color=discord.Color.dark_teal())
    embed.description = (f"**체력** `{t['hp']:,}`\n**공격력** `{t['atk']:,}`\n**방어력** `{t['def']:,}`\n\n"
                         f"명예 `{honor:,} pt` · 보유 자금 `${money:,}`")

    lines = [f"• **기본** (명예 반영): 체력 +{b['base_hp']:,} / 공격력 +{b['base_atk']:,} / 방어력 +{b['base_def']}"]
    w = b["weapon"]
    if w:
        parts = [f"공격력 +{w['atk']:,}"] + ([f"체력 +{w['hp']:,}"] if w["hp"] else [])
        lines.append(f"• **무기** [{w['src']}] {w['name']}: " + " / ".join(parts))
    else:
        lines.append("• **무기**: 없음")
    a = b["armor"]
    if a:
        lines.append(f"• **방어구** [제작] {a['name']}: 방어력 +{a['def']:,} / 체력 +{a['hp']:,}")
    else:
        lines.append("• **방어구**: 없음 (`/제작`에서 만들 수 있습니다)")
    if b["ticket"]:
        lines.append("• **임무 실패 방지권**: 방어력 +20")
    for ename, eatk, ehp, edef in b["elite"]:
        lines.append(f"• **강화 보스 전리품** {ename.split('] ', 1)[-1]}: 공격력 +{eatk:,} / 방어력 +{edef:,} / 체력 +{ehp:,}")
    for pname, patk, php, pdef in b["police"]:
        parts = ([f"공격력 +{patk:,}"] if patk else []) + ([f"방어력 +{pdef:,}"] if pdef else []) + ([f"체력 +{php:,}"] if php else [])
        lines.append(f"• **치안국 보급품** {pname}: " + " / ".join(parts))
    if POLICE_RADIO_NAME in inv:
        lines.append(f"• **{POLICE_RADIO_NAME}**: /검거 포상금 +{round(POLICE_RADIO_ARREST_BONUS * 100)}% · /작전 성공률 +{POLICE_RADIO_OP_BONUS}%")
    if POLICE_BADGE_NAME in inv:
        lines.append(f"• **{POLICE_BADGE_NAME}**: 검거·순찰·심문·작전 명예 실적 +{round(POLICE_BADGE_HONOR_BONUS * 100)}%")
    if POLICE_INSIGNIA_NAME in inv:
        lines.append(f"• **{POLICE_INSIGNIA_NAME}**: 명예 실적 +{round(POLICE_INSIGNIA_HONOR_BONUS * 100)}%")
    if POLICE_TERMINAL_NAME in inv:
        lines.append(f"• **{POLICE_TERMINAL_NAME}**: 구역 탈환 포상금 +{round(POLICE_TERMINAL_PRIZE_BONUS * 100)}% / 탈환 비용 {round(POLICE_TERMINAL_COST_DISCOUNT * 100)}% 할인")
    embed.add_field(name="능력치 구성", value="\n".join(lines), inline=False)

    if b["unused"]:
        embed.add_field(name="보유 중이지만 적용되지 않는 장비",
                        value="\n".join(f"• {x}" for x in b["unused"])[:1000] +
                              "\n*무기·방어구는 각각 가장 강한 1개만 적용됩니다.*", inline=False)
    embed.set_footer(text="/전투 · /네임드 · /보스 · /결투 · 레이드 · 점령전에 모두 이 능력치가 쓰입니다.")
    if target.avatar:
        embed.set_thumbnail(url=target.avatar.url)
    return embed


@bot.tree.command(name="능력치", description="[공용] 본인 또는 다른 유저의 전투 능력치(체력·공격력·방어력)와 구성을 확인합니다.")
@app_commands.describe(member="조회할 유저 (비우면 본인)")
async def stats_command(interaction: discord.Interaction, member: discord.Member = None):
    if not await check_maintenance(interaction): return
    if not await check_membership(interaction): return
    if await check_jail(interaction): return

    target = member or interaction.user
    if target.bot:
        await interaction.response.send_message("봇의 능력치는 확인할 수 없습니다.", ephemeral=True)
        return
    await interaction.response.defer()
    data = await asyncio.to_thread(get_user_data, target.id)
    await interaction.followup.send(embed=build_stats_embed(target, data))


# ==========================================================================
# [합친 모듈] help.py
#   /도움말
# ==========================================================================
# ==========================================================================
# [/도움말]  전체 명령어 안내
#   • 봇에 등록된 슬래시 명령어(bot.tree)를 실시간으로 읽어서 보여줍니다.
#     → 새 명령어를 추가해도 따로 고칠 필요 없이 자동으로 목록에 들어갑니다. (설명은 각 명령어의 description)
#   • 아래 HELP_CATEGORIES 에 이름을 적지 않은 명령어는 '기타' 분류에 모입니다.
# ==========================================================================
HELP_CATEGORIES = [
    # (키, 표시 이름, 분류 설명, [명령어 이름 — /조직 처럼 그룹 명령어는 첫 단어만])
    ("start",   "시작 · 정보",          "가입과 내 정보 확인, 설정, 랭킹 등 기본이 되는 명령어입니다.",
     ["도움말", "가입", "탈퇴", "프로필", "정보", "설정", "일퀘", "주간", "칭호", "업적", "랭킹"]),
    ("economy", "이동 · 상점 · 경제",   "거점 이동, 장비 구매, 자금 이체, 제작 등 돈과 장비에 관한 명령어입니다.",
     ["이동", "암시장", "무기상점", "백화점", "인벤토리", "송금", "제작", "경매", "사업", "주식"]),
    ("citizen", "시민 활동",         "가입 전 시민의 활동입니다. 좋은 활동은 선, 나쁜 활동은 악 성향이 쌓이고 전직 조건에 영향을 줍니다.",
     ["아르바이트", "봉사", "기부", "소매치기"]),
    ("mafia",   "마피아 활동",          "비밀 임무 수행, 경찰 매수, 탈옥 등 조직원의 범죄 활동입니다.",
     ["임무", "밀수", "탈옥"]),
    ("battle",  "전투 · 능력치",        "NPC·네임드·보스 토벌과 결투, 전투 능력치 확인입니다.",
     ["능력치", "전투", "네임드", "보스", "결투"]),
    ("casino",  "카지노",               "자금을 걸고 즐기는 도박입니다. (`/도박 규칙` 에서 배당표 확인)",
     ["도박"]),
    ("police",  "경찰",                 "경찰 소속 전용 명령어입니다.",
     ["작전", "검거", "순찰", "심문", "월급", "경찰상점", "수사팀", "갈취"]),
    ("family",  "조직 · 레이드 · 구역", "조직(패밀리)을 만들어 함께하는 협동·점령 콘텐츠입니다.",
     ["조직", "레이드", "구역"]),
    ("admin",   "관리자",               "서버 관리자 전용 명령어입니다.",
     ["추방", "관리"]),
]


def _help_usage(cmd) -> str:
    """'/송금 <member> <amount>' 처럼 사용법 문자열 생성 (<필수> / [선택])"""
    parts = [f"/{cmd.qualified_name}"]
    for prm in cmd.parameters:
        name = getattr(prm, "display_name", None) or prm.name
        parts.append(f"<{name}>" if prm.required else f"[{name}]")
    return " ".join(parts)


def _help_pages() -> list:
    """현재 등록된 명령어를 분류별로 정리 → [{key, label, desc, cmds}]"""
    by_root = {}
    for cmd in bot.tree.walk_commands():
        if isinstance(cmd, app_commands.Group):
            continue   # 그룹 자체가 아니라 그 안의 하위 명령어를 나열
        by_root.setdefault(cmd.qualified_name.split(" ")[0], []).append(cmd)

    pages, used = [], set()
    for key, label, desc, roots in HELP_CATEGORIES:
        cmds = []
        for root in roots:
            if root in by_root:
                cmds.extend(by_root[root])
                used.add(root)
        if cmds:
            pages.append({"key": key, "label": label, "desc": desc, "cmds": cmds})

    leftover = [c for root, cs in by_root.items() if root not in used for c in cs]
    if leftover:
        pages.append({"key": "etc", "label": "기타", "desc": "분류되지 않은 명령어입니다.", "cmds": leftover})
    return pages


def help_overview_embed(pages: list) -> discord.Embed:
    from .police_unit import POLICE_UNIT_CREATE_MIN_HONOR  # 순환 import 방지: 호출 시점에 가져옴
    total = sum(len(pg["cmds"]) for pg in pages)
    lines = [f"**{pg['label']}** ({len(pg['cmds'])}개)\n└ {pg['desc']}" for pg in pages]
    embed = discord.Embed(
        title="[패밀리 GM 도움말]",
        description=(f"총 **{total}개**의 명령어가 있습니다. 아래 메뉴에서 분류를 고르면 사용법과 설명을 볼 수 있습니다.\n"
                     f"사용법 표기: `<필수>` `[선택]`  ·  설명 앞의 `[ ]` 는 사용할 수 있는 대상입니다.\n\n"
                     + "\n\n".join(lines)),
        color=discord.Color.blurple()
    )
    embed.add_field(
        name="처음이라면",
        value=("`/가입` 으로 조직원 또는 경찰이 되세요.\n"
               "조직원은 `/임무` · `/전투` 로 자금과 명예를 모으고, `/무기상점` · `/암시장` · `/제작` 으로 장비를 갖춥니다.\n"
               f"명예 {FAMILY_CREATE_MIN_HONOR}pt 이상이면 `/조직 생성` 으로 조직을 만들어 `/레이드` 와 `/구역` 에 도전할 수 있습니다.\n"
               f"경찰은 `/순찰` · `/검거` 로 활동하고, 명예 {POLICE_UNIT_CREATE_MIN_HONOR}pt 이상이면 `/수사팀 생성` 으로 수사팀을 만들어 `/수사팀 작전` 에 도전할 수 있습니다."),
        inline=False
    )
    embed.set_footer(text="수감 중에는 일부 명령어를 쓸 수 없습니다. (수감자는 /탈옥 으로 탈출을 시도할 수 있어요)")
    return embed


def help_category_embed(page: dict) -> discord.Embed:
    blocks = [f"**`{_help_usage(c)}`**\n└ {c.description}" for c in page["cmds"]]
    text = f"{page['desc']}\n\n" + "\n\n".join(blocks)
    if len(text) > 4000:
        text = text[:3990] + "…"
    embed = discord.Embed(title=f"[도움말] {page['label']}", description=text, color=discord.Color.blurple())
    embed.set_footer(text=f"명령어 {len(page['cmds'])}개 · 사용법 표기: <필수> [선택]")
    return embed


class HelpSelect(discord.ui.Select):
    def __init__(self, pages: list):
        self.pages = pages
        options = [discord.SelectOption(label="전체 보기", value="__all__", description="분류 목록과 처음 시작하는 방법", default=True)]
        for pg in pages:
            options.append(discord.SelectOption(label=pg["label"], value=pg["key"], description=f"명령어 {len(pg['cmds'])}개"))
        super().__init__(placeholder="분류를 선택하세요...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        key = self.values[0]
        for opt in self.options:
            opt.default = (opt.value == key)
        if key == "__all__":
            embed = help_overview_embed(self.pages)
        else:
            embed = help_category_embed(next(pg for pg in self.pages if pg["key"] == key))
        await interaction.response.edit_message(embed=embed, view=self.view)


class HelpView(discord.ui.View):
    def __init__(self, pages: list):
        super().__init__(timeout=180)
        self.interaction = None
        self.add_item(HelpSelect(pages))

    async def on_timeout(self):
        if self.interaction is None:
            return
        try:
            await self.interaction.edit_original_response(view=None)   # 시간이 지나면 죽은 메뉴를 치운다
        except discord.HTTPException:
            pass


@bot.tree.command(name="도움말", description="[공용] 모든 명령어의 사용법과 설명을 분류별로 확인합니다.")
async def help_command(interaction: discord.Interaction):
    if not await check_maintenance(interaction): return

    pages = _help_pages()
    view = HelpView(pages)
    view.interaction = interaction
    await interaction.response.send_message(embed=help_overview_embed(pages), view=view, ephemeral=True)


# ==========================================================================
# [합친 모듈] jailbreak.py
#   /탈옥 숫자 야구
# ==========================================================================
# ==========================================================================
# [/탈옥]  숫자 야구(4자리) 탈옥 게임
#
#   • 수감 중인 조직원만 사용할 수 있습니다. (수감 중에는 다른 명령어가 막혀 있으므로 check_jail 은 쓰지 않습니다)
#   • 0~9 중 서로 다른 숫자 4개(0으로 시작 가능)를 ESCAPE_MAX_TRIES 번 안에 맞히면 즉시 석방됩니다.
#       - 스트라이크(S): 숫자와 자리가 모두 같음 / 볼(B): 숫자는 있지만 자리가 다름 / 둘 다 없으면 아웃
#   • 실패·포기·시간 초과 시 형기는 그대로이며, ESCAPE_COOLDOWN_SEC 뒤에 다시 도전할 수 있습니다. (0 이면 제한 없음)
#   • 게임이 진행되는 동안에는 명령어 동시 실행 방지 잠금이 유지됩니다. (GuardedView)
# ==========================================================================
ESCAPE_MAX_TRIES = 7
ESCAPE_DIGITS = 4
ESCAPE_COOLDOWN_SEC = 60      # 도전 시작 후 재도전까지 대기 시간(초)
ESCAPE_TIMEOUT_SEC = 300      # 게임 화면 유지 시간(초)

escape_cooldowns = {}   # {user_id: 재도전 가능 시각(time.time 기준)}


def baseball_judge(answer: str, guess: str):
    """(스트라이크, 볼) 반환"""
    strike = sum(a == g for a, g in zip(answer, guess))
    ball = len(set(answer) & set(guess)) - strike
    return strike, ball


def baseball_result_text(strike: int, ball: int) -> str:
    return "아웃" if strike == 0 and ball == 0 else f"{strike}S {ball}B"


def parse_baseball_guess(text: str):
    """(숫자 문자열, 오류 메시지) — 정상이면 오류는 None"""
    text = (text or "").strip()
    if not re.fullmatch(rf"[0-9]{{{ESCAPE_DIGITS}}}", text):
        return None, f"숫자 {ESCAPE_DIGITS}자리를 입력해 주세요. (예: 1234)"
    if len(set(text)) != ESCAPE_DIGITS:
        return None, "서로 다른 숫자 4개여야 합니다. (예: 1234 ✔ / 1123 ✘)"
    return text, None


class EscapeGuessModal(discord.ui.Modal, title="탈옥 — 숫자 입력"):
    guess = discord.ui.TextInput(
        label=f"서로 다른 숫자 {ESCAPE_DIGITS}자리",
        placeholder="예: 1234",
        min_length=ESCAPE_DIGITS,
        max_length=ESCAPE_DIGITS,
    )

    def __init__(self, game: "EscapeGameView"):
        super().__init__(timeout=ESCAPE_TIMEOUT_SEC)
        self.game = game

    async def on_submit(self, interaction: discord.Interaction):
        await self.game.submit_guess(interaction, str(self.guess.value))


class EscapeGameView(GuardedView):
    holds_command_lock = True   # 탈옥 게임이 끝날 때까지 다른 명령어 실행 차단

    def __init__(self, owner: discord.Member):
        super().__init__(timeout=ESCAPE_TIMEOUT_SEC)
        self.owner = owner
        self.answer = "".join(random.sample("0123456789", ESCAPE_DIGITS))
        self.history = []        # [(guess, strike, ball)]
        self.finished = False
        self.message = None

    # ---------------------------------------------------------------- 화면
    def build_embed(self, title: str = "[탈옥] 숫자 야구", note: str = "", color=None) -> discord.Embed:
        left = ESCAPE_MAX_TRIES - len(self.history)
        embed = discord.Embed(title=title, color=color or discord.Color.dark_grey())
        desc = (
            f"{self.owner.mention} 님이 탈옥을 시도합니다!\n"
            f"**{ESCAPE_MAX_TRIES}번 안에** 서로 다른 숫자 {ESCAPE_DIGITS}자리를 맞히면 **즉시 탈출**합니다.\n"
            f"`S`=숫자·자리 일치 / `B`=숫자만 일치 / 둘 다 없으면 `아웃`"
        )
        if note:
            desc += f"\n\n{note}"
        embed.description = desc
        if self.history:
            lines = [f"`{i + 1}회` **{g}** → {baseball_result_text(s, b)}" for i, (g, s, b) in enumerate(self.history)]
            embed.add_field(name="추리 기록", value="\n".join(lines), inline=False)
        embed.set_footer(text=f"남은 기회 {left}번 · {ESCAPE_TIMEOUT_SEC}초 이내에 진행하세요")
        if self.owner.avatar:
            embed.set_thumbnail(url=self.owner.avatar.url)
        return embed

    def _end(self):
        self.finished = True
        self.stop()

    # ---------------------------------------------------------------- 추측 처리
    async def submit_guess(self, interaction: discord.Interaction, raw: str):
        if self.finished:
            await interaction.response.send_message("이미 끝난 게임입니다.", ephemeral=True)
            return

        # 도중에 형기가 끝났다면(자연 석방) 게임 종료
        if not _is_jailed(self.owner.id):
            self._end()
            await interaction.response.edit_message(
                embed=self.build_embed("[탈옥] 종료", "이미 석방되어 더 이상 탈옥할 필요가 없습니다.", discord.Color.blue()),
                view=None)
            return

        guess, err = parse_baseball_guess(raw)
        if err:
            await interaction.response.send_message(err, ephemeral=True)   # 잘못된 입력은 기회를 소모하지 않음
            return

        if any(g == guess for g, _, _ in self.history):
            await interaction.response.send_message("이미 시도한 숫자입니다. (기회는 소모되지 않았습니다)", ephemeral=True)
            return

        strike, ball = baseball_judge(self.answer, guess)
        self.history.append((guess, strike, ball))

        if strike == ESCAPE_DIGITS:
            await self._success(interaction)
        elif len(self.history) >= ESCAPE_MAX_TRIES:
            await self._fail(interaction, f"기회를 모두 사용했습니다. 정답은 **{self.answer}** 였습니다.")
        else:
            await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _success(self, interaction: discord.Interaction):
        self._end()
        jail_list.pop(self.owner.id, None)
        save_status(self.owner.id)
        try:   # check_jail 의 석방 처리와 동일하게 위치를 기본 거점으로 복구
            await asyncio.to_thread(update_user_location, self.owner.id, "레벤하임")
        except Exception as e:
            print(f"DB Error (escape location): {e}")

        tries = len(self.history)
        embed = self.build_embed(
            "[탈옥 성공]",
            f"**{self.answer}** — 정답! **{tries}번 만에** 경비를 따돌리고 탈출했습니다!\n"
            f"{self.owner.mention} 님은 즉시 석방되었습니다.",
            discord.Color.green())
        embed.set_footer(text="탈옥 성공!")
        await interaction.response.edit_message(embed=embed, view=None)

    async def _fail(self, interaction: discord.Interaction, note: str, edit=None):
        self._end()
        embed = self.build_embed("[탈옥 실패]", f"{note}\n경비에게 붙잡혀 다시 감방으로 돌아갑니다.", discord.Color.red())
        embed.set_footer(text=(f"{ESCAPE_COOLDOWN_SEC}초 뒤에 다시 도전할 수 있습니다." if ESCAPE_COOLDOWN_SEC else "탈옥 실패"))
        if interaction is not None:
            await interaction.response.edit_message(embed=embed, view=None)
        elif edit is not None:
            await edit(embed=embed, view=None)

    # ---------------------------------------------------------------- 버튼
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner.id:
            await interaction.response.send_message("본인의 탈옥 시도만 조작할 수 있습니다.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="숫자 입력", style=discord.ButtonStyle.primary)
    async def guess_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.finished:
            await interaction.response.send_message("이미 끝난 게임입니다.", ephemeral=True)
            return
        await interaction.response.send_modal(EscapeGuessModal(self))

    @discord.ui.button(label="포기", style=discord.ButtonStyle.danger)
    async def give_up_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.finished:
            await interaction.response.send_message("이미 끝난 게임입니다.", ephemeral=True)
            return
        await self._fail(interaction, "탈옥을 포기했습니다.")

    async def on_timeout(self):
        if self.finished or self.message is None:
            return
        self._end()
        await self._fail(None, "시간이 초과되었습니다.", edit=self.message.edit)


@bot.tree.command(name="탈옥", description=f"[조직원 전용] 숫자 야구({ESCAPE_DIGITS}자리)를 {ESCAPE_MAX_TRIES}번 안에 맞히면 즉시 탈옥합니다.")
async def escape_command(interaction: discord.Interaction):
    if not await check_maintenance(interaction): return
    user = interaction.user

    if not _is_mafia(user):
        await interaction.response.send_message("탈옥은 마피아 조직원만 시도할 수 있습니다.", ephemeral=True)
        return

    if not _is_jailed(user.id):
        await interaction.response.send_message("현재 수감 중이 아닙니다. 탈옥할 필요가 없어요!", ephemeral=True)
        return

    now = time.time()
    cd = escape_cooldowns.get(user.id, 0)
    if now < cd:
        await interaction.response.send_message(
            f"경비가 삼엄합니다. **{int(cd - now) + 1}초** 뒤에 다시 도전할 수 있습니다.", ephemeral=True)
        return
    if ESCAPE_COOLDOWN_SEC:
        escape_cooldowns[user.id] = now + ESCAPE_COOLDOWN_SEC   # 도중에 그만둬도 쿨타임을 피할 수 없게 시작 시점에 적용

    game = EscapeGameView(user)
    await interaction.response.send_message(embed=game.build_embed(), view=game)
    game.message = await interaction.original_response()
