"""/정보"""

import discord
from discord import app_commands

from .core import bot
from .status import (
    ATTENDANCE_TITLE_STACKS,
    DAILY_QUESTS,
    WEEKLY_ALL_CLEAR_BONUS,
    WEEKLY_QUESTS,
    WEEKLY_TITLE_WEEKS,
)
from .alignment import (
    CORRUPT_ARREST_BONUS_HONOR,
    CORRUPT_ARREST_JAIL_SEC,
    DONATE_UNIT,
    KARMA_EVIL_MAX,
    KARMA_GOOD_MIN,
    PARTTIME_KARMA,
    PICKPOCKET_KARMA_FAIL,
    PICKPOCKET_KARMA_OK,
    SERVICE_KARMA,
    TITLES,
    join_requirement_text,
)
from .admin import check_maintenance
from .police_arrest import (
    SMUGGLE_COOLDOWN_SEC,
    SMUGGLE_IMMUNITY_SEC,
    SMUGGLE_MAX_BRIBE,
    SMUGGLE_MIN_BRIBE,
    SMUGGLE_REJECT_FINE,
    SMUGGLE_REJECT_HONOR_LOSS,
    SMUGGLE_REJECT_WANTED_SEC,
)
from .battle_data import boss_chance_text, boss_reward_text
from .battle import (
    SKILL_FINISHER_MIN_TURN,
    SKILL_FINISHER_MULT,
    SKILL_RALLY_BONUS,
    SKILL_RALLY_CD,
    SKILL_RALLY_TURNS,
    SKILL_SMASH_CD,
    SKILL_SMASH_MULT,
)

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
    from .family import FAMILY_CREATE_COST, FAMILY_CREATE_MIN_HONOR, FAMILY_MAX_MEMBERS   # 순환 import 방지: 호출 시점에 가져옴
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
