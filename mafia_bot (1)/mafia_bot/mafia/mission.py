"""비밀 임무 시스템 (/임무)"""

import asyncio
import discord
import random
import re
import time
from datetime import datetime, timedelta

from .core import GuardedView, supabase, wanted_list, keep_task
from .status import (
    CHRONO_ITEMS,
    DRINK_ITEM,
    MISSIONS,
    MISSION_BUFFS,
    MISSION_COOLDOWN_FAST_SEC,
    MISSION_COOLDOWN_SEC,
    NO_COOLDOWN_ITEM,
    _require_db,
    active_patrols,
    get_user_data,
    get_user_rank,
    mission_cooldowns,
    save_status,
)
from .alignment import evaluate_new_titles, format_new_titles, get_title_blob
from .achievements import daily_record

# === 3. 임무 선택 뷰 ===
class MissionSelect(discord.ui.Select):
    def __init__(self, owner: discord.Member):
        self.owner = owner
        options = [
            discord.SelectOption(
                label=m["label"],
                description=f"난이도: {m['diff']} | 성공률 {m['success_rate']}% | ${m['reward'][0]:,}~${m['reward'][1]:,} / 명예 +{m['honor']}pt / 실패 -${m['fine']:,}",
                value=key
            ) for key, m in MISSIONS.items()
        ]
        super().__init__(placeholder="수행할 비밀 임무를 선택하세요...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()

        if interaction.user != self.owner:
            await interaction.followup.send("타인의 임무 선택 창은 조작할 수 없습니다.", ephemeral=True)
            return

        user = interaction.user
        user_stats = get_user_data(user.id)
        old_rank = get_user_rank(user_stats.get("honor", 0))
        inventory = user_stats.get("inventory", {})

        # 임무 쿨타임: 크로노그래프 시계/특수 바이크 보유 시 단축, 자양강장제는 1회 즉시 초기화(소모)
        drink_used = False
        now_ts = time.time()
        remain = int(mission_cooldowns.get(user.id, 0) - now_ts + 0.999)
        no_cd = NO_COOLDOWN_ITEM in inventory
        if remain > 0 and not no_cd:
            if inventory.get(DRINK_ITEM, 0) > 0:
                inventory[DRINK_ITEM] -= 1
                if inventory[DRINK_ITEM] <= 0:
                    del inventory[DRINK_ITEM]
                drink_used = True
            else:
                await interaction.followup.send(f"작전 준비 중입니다. **{remain}초** 뒤에 다시 임무를 수행할 수 있습니다.", ephemeral=True)
                return
        has_fast = any(k in inventory for k in CHRONO_ITEMS)
        if not no_cd:
            mission_cooldowns[user.id] = now_ts + (MISSION_COOLDOWN_FAST_SEC if has_fast else MISSION_COOLDOWN_SEC)

        selected = MISSIONS[self.values[0]]
        current_loc = user_stats.get("location", "레벤하임")

        success_bonus = 0
        money_mult = 1.0
        honor_mult = 1.0
        active_buffs = []

        for item_key, s_bonus, m_bonus, h_bonus, label in MISSION_BUFFS:
            if item_key in inventory:
                success_bonus += s_bonus
                money_mult += m_bonus
                honor_mult += h_bonus
                active_buffs.append(label)
        if drink_used:
            active_buffs.append("자양강장제 (쿨타임 초기화·1회 소모)")

        # [추가] 해당 지역에 경찰의 순찰이 진행 중인 경우 성공률 감소 (-15%)
        patrol_penalty = 0
        guild_id = interaction.guild_id if interaction.guild else None
        if guild_id and guild_id in active_patrols:
            patrol_expiry = active_patrols[guild_id].get(current_loc, 0)
            if time.time() < patrol_expiry:
                patrol_penalty = 15  # 순찰 지역이면 성공률 15% 하락
                active_buffs.append("경찰 순찰망 가동 (-15% 성공률)")    

        final_success_rate = max(1, min(98, selected["success_rate"] + success_bonus - patrol_penalty))
        is_success = random.randint(1, 100) <= final_success_rate
        buff_desc = f"\n**적용된 보너스:** {', '.join(active_buffs)}" if active_buffs else ""

        current_money = user_stats.get("money", 0)
        current_honor = user_stats.get("honor", 0)
        
        fail_msg = ""
        rank_up_msg = ""

        blob = get_title_blob(user_stats)
        s = blob["stats"]

        if is_success:
            base_money = random.randint(*selected["reward"])
            earned_money = int(base_money * money_mult)
            earned_honor = int(selected["honor"] * honor_mult)
            
            current_money += earned_money
            current_honor += earned_honor

            s["success"] += 1
            s["streak"] += 1
            s["best_streak"] = max(s["best_streak"], s["streak"])
            if self.values[0] in ("mission_3", "mission_5"):
                s["hard_success"] += 1

            new_rank = get_user_rank(current_honor)
            if old_rank != new_rank:
                rank_up_msg = f"\n\n**[조직 승급 축하]** **{old_rank}** **{new_rank}**(으)로 승급하셨습니다!"

            rank_up_msg += daily_record(blob, "mission", {**user_stats, "money": current_money, "honor": current_honor})

            new_title_ids = evaluate_new_titles(blob, {**user_stats, "money": current_money, "honor": current_honor})
            rank_up_msg += format_new_titles(new_title_ids)

            embed = discord.Embed(
                title=f"[{current_loc}] 임무 성공",
                description=f"**{user.mention}** 님이 작전을 완벽하게 완수했습니다!{buff_desc}\n\n"
                            f"**작전명:** {selected['title']}\n"
                            f"**획득 보상금:** `${earned_money:,}`\n"
                            f"**획득 명예:** `+{earned_honor} pt`\n\n"
                            f"**현재 직급:** `{new_rank}`\n"
                            f"**보유 자금:** `${current_money:,}`\n"
                            f"**총 명예:** `{current_honor} pt`{rank_up_msg}",
                color=discord.Color.green()
            )
        else:
            settings = user_stats.get("settings", {"auto_fail_guard": True})
            guard_item_key = "임무 실패 방지권"
            guard_used = False

            if settings.get("auto_fail_guard", True) and inventory.get(guard_item_key, 0) > 0:
                inventory[guard_item_key] -= 1
                if inventory[guard_item_key] <= 0:
                    del inventory[guard_item_key]
                guard_used = True

            if guard_used:
                fail_msg = "**[실패 방지권 발동]** 임무에 실패했으나, 가방에 있던 **임무 실패 방지권**이 소모되어 수배 및 벌금이 면제되었습니다!"
            else:
                fine = min(current_money, selected["fine"])
                current_money -= fine
                wanted_list[user.id] = datetime.now() + timedelta(seconds=60)
                save_status(user.id)
                fail_msg = f"**작전 실패!** 현장에 흔적이 남아 **1분간 경찰 지명수배 상태**가 됩니다! (벌금 `-${fine:,}`)"

            s["fail"] += 1
            s["streak"] = 0
            new_title_ids = evaluate_new_titles(blob, user_stats)

            current_rank = get_user_rank(current_honor)
            embed = discord.Embed(
                title=f"[{current_loc}] 임무 실패",
                description=f"**{user.mention}** 님이 작전 도중 문제가 발생했습니다!{buff_desc}\n\n"
                            f"**작전명:** {selected['title']}\n"
                            f"{fail_msg}\n\n"
                            f"**현재 직급:** `{current_rank}`\n"
                            f"**보유 자금:** `${current_money:,}`\n"
                            f"**총 명예:** `{current_honor} pt`{format_new_titles(new_title_ids)}",
                color=discord.Color.red()
            )

        try:
            _require_db()
            supabase.table("user_data").update({
                "money": current_money,
                "honor": current_honor,
                "inventory": inventory,
                "data": blob
            }).eq("user_id", str(user.id)).execute()
        except Exception as e:
            print(f"DB Error (mission update): {e}")
            await interaction.followup.send("DB 저장 중 오류가 발생했습니다.", ephemeral=True)
            return

        if user.avatar:
            embed.set_thumbnail(url=user.avatar.url)

        await interaction.edit_original_response(embed=embed, view=None)

class MissionView(discord.ui.View):
    def __init__(self, owner: discord.Member):
        super().__init__(timeout=180)
        self.add_item(MissionSelect(owner=owner))

# === 3. 비밀 임무 시스템 (여러 단계의 현장 작업을 직접 수행하는 방식) ===
# 임무를 고르면 브리핑이 나오고, 작전을 개시하면 단계별 작업(이동 / 순서 누르기 / 전선 연결 / 신호 기억 /
# 진행형 작업 / 금고 해제 / 돌발 상황)을 시간을 들여 하나씩 처리합니다.
#
# 성공 여부는 '경계도'로 결정됩니다.
#   - 실수하거나 돌발 상황에 잘못 대처하면 경계도가 오르고, 100%가 되면 작전이 노출되어 실패합니다.
#   - 장비 효과와 경찰 순찰이 반영된 임무 성공률(final_rate)은 '경계도 상승 배율'로 바뀝니다.
#       배율 = 50 / max(10, 성공률)   (성공률 85% -> x0.59 / 50% -> x1.00 / 20% -> x2.50)
#   - 중간에 포기하거나 오래 응답하지 않으면 실패로 처리됩니다. (벌금, 수배 등 기존 실패 처리와 동일)
#
# 튜닝용 상수
MISSION_TIME_SCALE = 1.0          # 이동 / 진행 / 신호 재생 시간 배율 (0.5 = 절반으로 단축)
MISSION_CHECK_WINDOW_SEC = 8      # 진행형 작업 중 '신호 유지' 버튼을 눌러야 하는 제한 시간
MISSION_IDLE_TIMEOUT = 120        # 이 시간 동안 아무 조작이 없으면 작전 실패로 처리
MISSION_SIGNALS = ("적색", "청색", "녹색", "황색")
MISSION_WIRE_COLORS = ("빨강", "파랑", "노랑", "초록")
MISSION_TERMINALS = ("가", "나", "다", "라")

# 현장 돌발 상황 (한 임무에서 같은 상황이 두 번 나오지 않음)
# alert: 경계도 상승치(배율 적용 전). 장면 설명 속 단서를 읽으면 가장 안전한 선택을 알 수 있습니다.
MISSION_ENCOUNTERS = [
    {
        "title": "순찰 경비 접근",
        "text": "복도 끝에서 손전등 불빛이 흔들리며 다가온다. 경비는 왼쪽 통로를 따라 걸어오고 있고, 오른쪽에는 문이 열린 청소 도구실이 있다.",
        "options": [
            {"label": "청소 도구실로 몸을 숨긴다", "alert": 0, "result": "경비는 도구실 앞을 그대로 지나쳤다. 숨을 죽이고 기다린 뒤 다시 움직였다."},
            {"label": "태연한 척 정면으로 지나친다", "alert": 20, "result": "경비가 신분을 캐물었다. 가까스로 둘러댔지만 의심스러운 눈빛이 오래 남았다."},
            {"label": "반대편 통로로 달려 도망친다", "alert": 35, "result": "급한 발소리가 복도에 크게 울렸다. 경비가 무전기를 꺼내 드는 소리가 들린다."},
        ],
    },
    {
        "title": "잠긴 문",
        "text": "목표 지점으로 가는 문이 잠겨 있다. 문 옆 화분 아래에서 무언가 반짝이고, 천장의 환풍구는 사람이 지나가기엔 좁아 보인다.",
        "options": [
            {"label": "화분 아래를 뒤져 본다", "alert": 0, "result": "화분 밑에서 예비 열쇠가 나왔다. 소리 없이 문이 열렸다."},
            {"label": "환풍구로 기어 들어간다", "alert": 15, "result": "좁은 틈에 몸이 걸려 한참을 버둥거렸다. 금속이 긁히는 소리가 났다."},
            {"label": "문을 걷어차 연다", "alert": 40, "result": "굉음과 함께 문이 열렸다. 건물 전체가 그 소리를 들었을 것이다."},
        ],
    },
    {
        "title": "행인의 시선",
        "text": "골목 가판대의 노인이 당신의 가방을 계속 쳐다본다. 노인은 말동무가 필요한 듯 연신 신문을 접었다 펴고 있다.",
        "options": [
            {"label": "신문을 사며 날씨 얘기를 건넨다", "alert": 0, "result": "노인은 신이 나서 이야기를 늘어놓았다. 시선은 더 이상 가방에 머물지 않았다."},
            {"label": "못 본 척 빠르게 걸어간다", "alert": 15, "result": "노인이 고개를 갸웃하며 뒷모습을 오래 바라보았다."},
            {"label": "돈을 쥐여주며 입을 막는다", "alert": 25, "result": "노인은 돈을 받았지만, 수상한 사람이 지나갔다는 말은 어딘가에서 흘러나갈 것이다."},
        ],
    },
    {
        "title": "순찰차 접근",
        "text": "맞은편 도로에서 순찰차가 천천히 다가온다. 가까운 곳에 쓰레기 수거 트럭이 정차해 있고, 트럭 뒤편에는 짙은 그림자가 드리워져 있다.",
        "options": [
            {"label": "트럭 뒤 그림자에 몸을 붙인다", "alert": 0, "result": "순찰차의 헤드라이트가 트럭 앞을 훑고 지나갔다. 아무도 이쪽을 보지 않았다."},
            {"label": "고개를 숙이고 태연히 걷는다", "alert": 20, "result": "순찰차가 속도를 줄여 한참을 따라붙다가 떠났다."},
            {"label": "가방을 안고 골목으로 뛴다", "alert": 35, "result": "뛰는 모습이 순찰차의 사이드미러에 잡혔다. 차가 속도를 줄인다."},
        ],
    },
    {
        "title": "적외선 경보 센서",
        "text": "바닥 가까이에서 붉은 적외선 줄기가 교차하고 있다. 벽 쪽에 손거울 조각이 떨어져 있고, 센서 전원 박스는 천장 가까이 달려 있다.",
        "options": [
            {"label": "거울 조각으로 빛줄기를 꺾어 우회한다", "alert": 0, "result": "빛줄기가 거울에 반사되어 벽을 비췄다. 센서는 아무것도 감지하지 못했다."},
            {"label": "의자에 올라가 전원 박스를 연다", "alert": 15, "result": "전원을 내리는 순간 짧은 작동음이 울렸지만 경보로 이어지진 않았다."},
            {"label": "빛줄기 사이로 몸을 접어 통과한다", "alert": 30, "result": "옷자락이 빛줄기를 스쳤다. 어딘가에서 낮은 경고음이 한 번 울렸다."},
        ],
    },
    {
        "title": "무전기 호출",
        "text": "허리에 찬 경비 무전기에서 잡음 섞인 호출이 들린다. 상대는 당신의 존재를 모르는 눈치다. 근처 책상에 오늘자 경비 근무표가 놓여 있다.",
        "options": [
            {"label": "근무표를 보고 해당 근무자인 척 응답한다", "alert": 0, "result": "근무표의 이름으로 응답하자 상대는 별다른 의심 없이 교신을 끝냈다."},
            {"label": "응답하지 않고 자리를 옮긴다", "alert": 15, "result": "호출이 두 번 더 이어졌다. 응답이 없다는 사실이 오히려 이상하게 들렸을 것이다."},
            {"label": "무전기를 꺼 버린다", "alert": 25, "result": "갑자기 끊긴 교신에 상대가 확인하러 오겠다는 말이 마지막으로 들렸다."},
        ],
    },
]

# 임무별 시나리오 (briefing: 의뢰 내용 / steps: 수행할 작업 순서)
MISSION_SCRIPTS = {
    "mission_1": {
        "briefing": "경쟁 조직이 드나드는 창고 골목을 은밀히 조사한다. 의뢰인이 원하는 것은 출입 인원과 화물 반입 시간이 적힌 장부 사진이다. 눈에 띄지 말고, 아무것도 건드리지 말고, 증거만 들고 돌아와라.",
        "steps": [
            {"type": "travel", "title": "잠입 경로 이동", "seconds": 6,
             "text": "해가 진 직후라 골목에는 사람이 드물다. 낮은 담장을 따라 목표 창고 뒤편으로 접근한다.",
             "lines": ["골목 입구에서 주변 시야를 확인한다.", "담장 그림자를 따라 천천히 이동한다.", "창고 뒷문이 보이는 위치에 도착했다."]},
            {"type": "sequence", "title": "감시 카메라 차단", "count": 6,
             "text": "벽면 배전함 안에 번호가 붙은 차단기 6개가 늘어서 있다. 1번부터 순서대로 내려야 카메라 전원이 안전하게 꺼진다. 순서가 틀리면 전원이 되돌아와 처음부터 다시 해야 한다."},
            {"type": "encounter"},
            {"type": "memory", "title": "신호등 암호 관찰", "length": 4,
             "text": "창고 창문에서 색색의 불빛이 정해진 순서로 깜빡인다. 안에 있는 조직원들이 쓰는 확인 신호다. 순서를 기억했다가 그대로 재현해 문 앞 감시자를 속인다."},
            {"type": "progress", "title": "출입 장부 촬영 및 전송", "seconds": 10, "checks": 1,
             "text": "열린 창틈으로 출입 장부를 소형 카메라에 담고 의뢰인의 수신기로 전송한다. 전송 도중 신호가 불안정해지면 수동으로 연결을 유지해야 한다.",
             "status": ["카메라 초점을 맞추는 중이다.", "장부 첫 장을 촬영했다.", "암호화하여 전송 중이다.", "수신 확인을 기다리는 중이다."]},
        ],
    },
    "mission_2": {
        "briefing": "항구 창고에 쌓인 밀수품을 검문 없이 도시 밖으로 빼내야 한다. 컨테이너 봉인을 풀고, 화물을 적재하고, 검문소를 통과할 서류를 맞춰야 한다. 한 군데라도 어긋나면 세관 조사가 들어온다.",
        "steps": [
            {"type": "travel", "title": "항구 창고 진입", "seconds": 8,
             "text": "안개 낀 부두를 가로질러 목표 컨테이너가 있는 창고로 향한다. 하역 인부들의 교대 시간을 노려야 한다.",
             "lines": ["부두 입구의 경비 초소를 우회한다.", "컨테이너 사이 좁은 통로를 따라 이동한다.", "교대 시간에 맞춰 창고 안으로 들어간다.", "목표 컨테이너 앞에 도착했다."]},
            {"type": "wires", "title": "컨테이너 전자 봉인 해제", "count": 4, "rounds": 1,
             "text": "컨테이너 문에 달린 전자 봉인 패널을 열자 색이 다른 전선 네 가닥이 드러났다. 같은 색의 전선과 단자를 연결해야 봉인이 풀린다."},
            {"type": "encounter"},
            {"type": "progress", "title": "화물 적재", "seconds": 14, "checks": 2,
             "text": "지게차를 최소한의 소음으로 움직여 밀수품을 트럭에 옮겨 싣는다. 작업 중 무전 신호가 불안정해지면 망을 보는 동료와의 연결을 유지해야 한다.",
             "status": ["첫 번째 팔레트를 들어 올렸다.", "트럭 적재함에 화물을 고정하는 중이다.", "두 번째 팔레트를 옮기는 중이다.", "마지막 상자를 싣고 있다."]},
            {"type": "safe", "title": "화물칸 자물쇠 잠금", "digits": 3, "attempts": 6,
             "text": "적재가 끝난 화물칸에는 3자리 숫자 자물쇠가 달려 있다. 운송 책임자만 알고 있는 번호이므로, 다이얼을 돌려 나오는 반응으로 번호를 추리해야 한다."},
            {"type": "sequence", "title": "검문소 서류 정리", "count": 7,
             "text": "검문소에 제출할 서류 7장에 번호표가 붙어 있다. 번호 순서가 뒤섞이면 서류가 위조임이 들통난다. 1번부터 차례대로 정렬해 제출 순서를 맞춘다."},
        ],
    },
    "mission_3": {
        "briefing": "라이벌 보스의 저택 지하 금고에는 상납 내역이 적힌 비밀 장부가 있다. 저택 경비망은 촘촘하며 경보 장치가 곳곳에 설치되어 있다. 장부를 복사하고 흔적 없이 빠져나오는 것이 목표다.",
        "steps": [
            {"type": "travel", "title": "저택 외곽 침투", "seconds": 8,
             "text": "높은 담장 아래 배수로를 통해 저택 정원으로 들어간다. 정원에는 사냥개가 풀려 있다는 소문이 있다.",
             "lines": ["배수로의 철망을 뜯고 정원으로 올라선다.", "분수대 뒤편에 몸을 붙이고 순찰 간격을 센다.", "별채 쪽 그림자로 이동한다.", "지하실로 이어지는 문 앞에 도착했다."]},
            {"type": "sequence", "title": "경보 회로 차단", "count": 8,
             "text": "지하 계단 입구의 제어함에 번호가 붙은 스위치 8개가 있다. 1번부터 순서대로 내려야 경보 회로가 차례로 꺼진다. 한 번이라도 순서가 틀리면 회로가 재가동된다."},
            {"type": "wires", "title": "보안 패널 우회", "count": 4, "rounds": 1,
             "text": "금고실 앞 보안 패널 뒤쪽 배선이 노출되어 있다. 전선과 단자의 색을 정확히 맞춰 패널이 경비실에 신호를 보내지 못하게 만든다."},
            {"type": "encounter"},
            {"type": "memory", "title": "경비 교대 신호 기억", "length": 5,
             "text": "복도 끝 조명이 경비 교대를 알리는 신호를 보낸다. 조명 순서를 외워 두었다가 같은 순서로 응답해야 교대 경비가 이상을 느끼지 못한다."},
            {"type": "safe", "title": "장부 금고 해제", "digits": 4, "attempts": 8,
             "text": "보스가 아끼는 금고는 4자리 숫자 자물쇠다. 다이얼을 돌릴 때마다 손끝에 느껴지는 반응을 단서로 번호를 좁혀 나가야 한다. 시도 횟수가 다하면 자물쇠를 부수게 되고, 큰 소음이 난다."},
            {"type": "progress", "title": "장부 복사 및 전송", "seconds": 18, "checks": 2,
             "text": "휴대용 스캐너로 장부 전체를 복사해 외부로 전송한다. 양이 많아 시간이 걸린다. 지하라 신호가 약해 중간중간 수동으로 연결을 잡아줘야 한다.",
             "status": ["장부 첫 묶음을 스캔하는 중이다.", "페이지를 넘기며 복사를 이어간다.", "복사본을 압축해 전송 준비를 한다.", "외부 수신기로 전송 중이다.", "마지막 페이지를 전송하고 있다."]},
            {"type": "travel", "title": "탈출", "seconds": 8,
             "text": "장부를 원래 자리에 돌려놓고 들어왔던 길로 빠져나간다. 마지막까지 긴장을 늦춰서는 안 된다.",
             "lines": ["금고 문을 닫고 흔적을 지운다.", "계단을 올라 별채 그림자로 이동한다.", "배수로를 통해 담장 밖으로 나왔다."]},
        ],
    },
    "mission_4": {
        "briefing": "야간에 비어 있는 사무실에서 기밀 문서를 반출해야 한다. 건물에는 야간 순찰이 돌고 있고, 문서는 잠긴 서류함 안에 있다. 문서가 없어졌다는 사실을 아침까지 눈치채지 못하게 하는 것이 조건이다.",
        "steps": [
            {"type": "travel", "title": "야간 사무실 진입", "seconds": 7,
             "text": "청소 업체 직원으로 위장해 뒷문으로 들어간다. 출입증은 미리 복제해 두었다.",
             "lines": ["뒷문 출입 단말기에 복제한 출입증을 댄다.", "비상계단을 통해 목표 층으로 올라간다.", "불 꺼진 사무실 복도에 도착했다."]},
            {"type": "memory", "title": "야간 순찰 경로 파악", "length": 5,
             "text": "복도의 보안등이 순찰 경로를 따라 차례로 켜진다. 순찰이 지나가는 순서를 기억해 두어야 서류함까지 가는 안전한 타이밍을 계산할 수 있다."},
            {"type": "encounter"},
            {"type": "safe", "title": "기밀 서류함 개방", "digits": 3, "attempts": 6,
             "text": "서류함은 3자리 숫자 자물쇠로 잠겨 있다. 다이얼 반응을 통해 번호를 추리한다. 시간이 오래 걸릴수록 순찰이 돌아올 가능성이 커진다."},
            {"type": "wires", "title": "서류함 경보 배선 차단", "count": 4, "rounds": 1,
             "text": "서류함 뒤판 아래에는 문이 열리면 울리는 경보 배선이 숨겨져 있다. 색을 맞춰 배선을 바이패스하지 않으면 문서를 꺼내는 순간 경보가 울린다."},
            {"type": "progress", "title": "문서 사본 제작 및 원본 반출", "seconds": 16, "checks": 2,
             "text": "원본을 가방에 넣고 같은 두께의 가짜 서류철로 빈자리를 채운다. 작업 중 경비실과 연결된 무전기를 계속 지켜보다가 신호가 올 때마다 응답해야 한다.",
             "status": ["서류철을 분류하는 중이다.", "가짜 서류철의 두께를 맞추고 있다.", "원본을 가방 안쪽에 숨겼다.", "서류함을 원래 상태로 되돌리는 중이다."]},
            {"type": "travel", "title": "건물 이탈", "seconds": 7,
             "text": "들어올 때와 같은 경로로 조용히 건물을 빠져나간다.",
             "lines": ["사무실 불을 끄고 복도로 나선다.", "비상계단을 내려가 뒷문 앞에 선다.", "새벽 공기 속으로 빠져나왔다."]},
        ],
    },
    "mission_5": {
        "briefing": "중앙 은행의 현금 호송 차량이 새벽에 외곽 도로를 지난다. 호송 차량은 장갑 처리되어 있고 무장 경호원이 동승한다. 정확한 시간에, 정확한 순서로 움직이지 못하면 이 작전은 곧바로 총격전이 된다.",
        "steps": [
            {"type": "travel", "title": "매복 지점 이동", "seconds": 10,
             "text": "호송 경로 중 가장 좁은 고가도로 아래에 매복한다. 동료들과의 합류 시간이 촉박하다.",
             "lines": ["새벽 도로를 가로질러 고가도로 아래로 이동한다.", "미리 준비한 차량 두 대를 도로 양쪽에 배치한다.", "무전기로 동료들의 위치를 확인한다.", "호송 차량의 접근 신호를 기다린다."]},
            {"type": "memory", "title": "호송 차량 신호 해독", "length": 6,
             "text": "호송 차량은 도로 신호등과 무선으로 통신하며 지나간다. 신호의 색 순서를 외워 두었다가 같은 순서로 재현해 신호 체계를 교란해야 한다."},
            {"type": "encounter"},
            {"type": "wires", "title": "차량 통신 장치 무력화", "count": 4, "rounds": 2,
             "text": "차량 정차 직후 통신 장치 패널을 열고 전선을 정확히 연결해 위치 추적 신호를 끊어야 한다. 패널은 두 겹이라 두 번에 걸쳐 작업해야 한다."},
            {"type": "sequence", "title": "후방 도어 잠금 해제", "count": 9,
             "text": "후방 도어의 기계식 잠금장치에는 번호가 붙은 레버 9개가 있다. 1번부터 순서대로 당겨야 잠금이 풀린다. 순서가 틀리면 레버가 모두 원위치로 돌아간다."},
            {"type": "encounter"},
            {"type": "safe", "title": "현금 보관함 개방", "digits": 4, "attempts": 7,
             "text": "차량 내부의 현금 보관함은 4자리 숫자 자물쇠로 잠겨 있다. 경호원이 곧 정신을 차릴 것이므로 시간이 많지 않다."},
            {"type": "progress", "title": "현금 이송 및 도주", "seconds": 22, "checks": 3,
             "text": "현금 가방을 도주 차량으로 옮겨 싣고 추격을 따돌려야 한다. 무전으로 연락하는 동료의 경고에 즉시 응답하지 않으면 추격을 막지 못한다.",
             "status": ["첫 번째 가방을 도주 차량으로 옮기고 있다.", "두 번째 가방을 싣는 중이다.", "멀리서 사이렌 소리가 희미하게 들린다.", "차량이 도로를 벗어나 샛길로 접어들었다.", "추격 차량과의 거리가 벌어지고 있다."]},
        ],
    },
}

_MISSION_EST_SECONDS = {"sequence": 20, "wires": 25, "memory": 30, "safe": 45, "encounter": 15}

def mission_estimate_text(key: str) -> str:
    total = 0
    for st in MISSION_SCRIPTS[key]["steps"]:
        t = st["type"]
        if t == "travel":
            total += st["seconds"] + 3
        elif t == "progress":
            total += st["seconds"] + 10 + st.get("checks", 0) * 4
        else:
            total += _MISSION_EST_SECONDS.get(t, 20)
    return f"약 {max(1, round(total / 60))}분"

def mission_step_title(step: dict) -> str:
    return step.get("title") or "현장 돌발 상황 대응"

def _mission_bar(ratio: float, fill: str = "=", width: int = 20) -> str:
    ratio = max(0.0, min(1.0, ratio))
    filled = int(round(ratio * width))
    return f"[{fill * filled}{'-' * (width - filled)}] {int(ratio * 100)}%"

def _tail_lines(lines: list, limit: int = 950) -> str:
    """최근 기록부터 거꾸로 담아 필드 글자 제한(1024자)을 넘지 않게 자름"""
    picked, size = [], 0
    for line in reversed(lines):
        if size + len(line) + 1 > limit:
            break
        picked.append(line)
        size += len(line) + 1
    return "\n".join(reversed(picked)) if picked else "-"

async def mission_sleep(seconds: float):
    await asyncio.sleep(max(0.0, seconds * MISSION_TIME_SCALE))

async def _ack(interaction: discord.Interaction):
    try:
        if not interaction.response.is_done():
            await interaction.response.defer()
    except discord.HTTPException:
        pass

# --- 화면 구성 요소 ---
class MissionButton(discord.ui.Button):
    def __init__(self, session, key: str, label: str, style, row: int = None, disabled: bool = False, lock_free: bool = False):
        super().__init__(label=label, style=style, row=row, disabled=disabled)
        self.session = session
        self.key = key
        self.lock_free = lock_free
        self.step_id = session.step_id

    async def callback(self, interaction: discord.Interaction):
        await self.session.dispatch(interaction, self.key, None, self.step_id, self.lock_free)

class MissionSelectItem(discord.ui.Select):
    def __init__(self, session, key: str, placeholder: str, options: list, row: int = None):
        super().__init__(placeholder=placeholder, min_values=1, max_values=1, options=options, row=row)
        self.session = session
        self.key = key
        self.step_id = session.step_id

    async def callback(self, interaction: discord.Interaction):
        await self.session.dispatch(interaction, self.key, list(self.values), self.step_id, False)

class MissionCodeModal(discord.ui.Modal):
    def __init__(self, task):
        super().__init__(title="자물쇠 다이얼", timeout=MISSION_IDLE_TIMEOUT)
        self.task = task
        digits = task.digits
        self.code_input = discord.ui.TextInput(
            label=f"{digits}자리 숫자 (서로 다른 숫자)", placeholder="예: " + "".join(str(i) for i in range(digits)),
            min_length=digits, max_length=digits, required=True
        )
        self.add_item(self.code_input)

    async def on_submit(self, interaction: discord.Interaction):
        s = self.task.s
        async with s.lock:
            if s.phase != "step" or s.task is not self.task:
                await _ack(interaction)
                return
            await self.task.submit(interaction, str(self.code_input.value).strip())

# --- 작업(미니게임) 종류 ---
class MissionTask:
    def __init__(self, session, cfg: dict):
        self.s = session
        self.cfg = cfg
        self.title = cfg.get("title", "")
        self.text = cfg.get("text", "")
        self.locked = False       # True 이면 자동 진행 중이라 포기 버튼을 누를 수 없음

    def body(self) -> str:
        return ""

    def items(self) -> list:
        return []

    async def start(self, interaction: discord.Interaction):
        pass

    async def handle(self, interaction: discord.Interaction, key: str, values):
        await _ack(interaction)

    def btn(self, key, label, style=None, row=0, disabled=False, lock_free=False):
        return MissionButton(self.s, key, label, style or discord.ButtonStyle.secondary, row, disabled, lock_free)

class TravelTask(MissionTask):
    """이동 / 잠입: 안내 문장이 시간에 따라 하나씩 나타남 (자동 진행)"""
    def __init__(self, session, cfg):
        super().__init__(session, cfg)
        self.shown = 0
        self.ratio = 0.0
        self.locked = True

    def body(self) -> str:
        lines = "\n".join(f"- {line}" for line in self.cfg["lines"][:self.shown])
        return f"{lines}\n\n진행 {_mission_bar(self.ratio)}"

    async def start(self, interaction):
        self.s.spawn(self.run(interaction))

    async def run(self, interaction):
        lines = self.cfg["lines"]
        per = self.cfg["seconds"] / len(lines)
        for i in range(len(lines)):
            if self.s.phase != "step" or self.s.task is not self:
                return
            self.shown = i + 1
            self.ratio = i / len(lines)
            await self.s.push(interaction)
            await mission_sleep(per)
        self.ratio = 1.0
        await self.s.locked_call(self.s.advance, interaction, f"{self.title}: 완료")

class SequenceTask(MissionTask):
    """번호 순서대로 누르기: 틀리면 처음부터 다시"""
    def __init__(self, session, cfg):
        super().__init__(session, cfg)
        self.count = cfg["count"]
        self.order = random.sample(range(1, self.count + 1), self.count)
        self.next = 1

    def body(self) -> str:
        return f"1번부터 {self.count}번까지 순서대로 누르십시오.\n다음에 누를 번호: **{self.next}**"

    def items(self) -> list:
        return [
            self.btn(f"seq:{num}", str(num),
                     style=discord.ButtonStyle.success if num < self.next else discord.ButtonStyle.secondary,
                     row=i // 5, disabled=num < self.next)
            for i, num in enumerate(self.order)
        ]

    async def handle(self, interaction, key, values):
        await _ack(interaction)
        num = int(key.split(":", 1)[1])
        if num == self.next:
            self.next += 1
            if self.next > self.count:
                await self.s.advance(interaction, f"{self.title}: 완료")
            else:
                await self.s.push(interaction)
        else:
            self.next = 1
            if await self.s.add_alert(interaction, 8, f"{self.title}: 순서 오류 (번호 {num}), 처음부터 다시"):
                return
            await self.s.push(interaction)

class WiresTask(MissionTask):
    """같은 색 전선과 단자 연결"""
    def __init__(self, session, cfg):
        super().__init__(session, cfg)
        self.n = cfg.get("count", 4)
        self.rounds = cfg.get("rounds", 1)
        self.round = 1
        self.new_round()

    def new_round(self):
        self.left = random.sample(MISSION_WIRE_COLORS, self.n)
        self.right = random.sample(self.left, self.n)
        self.choice = [None] * self.n

    def body(self) -> str:
        left = ", ".join(f"{i + 1}번 {c}" for i, c in enumerate(self.left))
        right = ", ".join(f"{MISSION_TERMINALS[j]} {c}" for j, c in enumerate(self.right))
        return (f"같은 색의 전선과 단자를 연결한 뒤 [연결 확인]을 누르십시오. (작업 {self.round}/{self.rounds})\n\n"
                f"전선: {left}\n단자: {right}")

    def items(self) -> list:
        out = []
        for i in range(self.n):
            options = [
                discord.SelectOption(label=f"{MISSION_TERMINALS[j]} 단자 ({self.right[j]})", value=str(j), default=(self.choice[i] == j))
                for j in range(self.n)
            ]
            out.append(MissionSelectItem(self.s, f"wire:{i}", f"{i + 1}번 {self.left[i]} 전선을 연결할 단자", options, row=i))
        out.append(self.btn("confirm", "연결 확인", discord.ButtonStyle.primary, row=4))
        return out

    async def handle(self, interaction, key, values):
        await _ack(interaction)
        if key.startswith("wire:"):
            self.choice[int(key.split(":", 1)[1])] = int(values[0])
            await self.s.push(interaction)
            return
        if None in self.choice:
            try:
                await interaction.followup.send("아직 연결하지 않은 전선이 있습니다.", ephemeral=True)
            except discord.HTTPException:
                pass
            return
        wrong = sum(1 for i in range(self.n) if self.right[self.choice[i]] != self.left[i])
        if wrong == 0:
            if self.round < self.rounds:
                self.round += 1
                self.s.log.append(f"{self.title}: {self.round - 1}차 연결 성공")
                self.new_round()
                await self.s.push(interaction)
            else:
                await self.s.advance(interaction, f"{self.title}: 완료")
            return
        self.choice = [None] * self.n
        if await self.s.add_alert(interaction, 6 * wrong, f"{self.title}: 잘못 연결된 전선 {wrong}개, 합선 발생"):
            return
        await self.s.push(interaction)

class MemoryTask(MissionTask):
    """신호 기억하기: 화면에 나오는 색 신호 순서를 외웠다가 그대로 누름"""
    def __init__(self, session, cfg):
        super().__init__(session, cfg)
        self.length = cfg.get("length", 4)
        seq = []
        while len(seq) < self.length:
            c = random.choice(MISSION_SIGNALS)
            if not seq or c != seq[-1]:      # 같은 신호가 연속되면 구분이 안 되므로 제외
                seq.append(c)
        self.seq = seq
        self.pos = 0
        self.showing = True
        self.cur = None
        self.cur_no = 0
        self.plays = 0
        self.locked = True

    def body(self) -> str:
        if self.showing:
            text = "신호를 잘 보고 순서를 기억하십시오."
            if self.plays > 1:
                text += f" (재생 {self.plays}회차)"
            if self.cur:
                return f"{text}\n\n현재 신호 ({self.cur_no}/{self.length}): **{self.cur}**"
            return f"{text}\n\n잠시 후 신호가 시작됩니다..."
        return f"기억한 신호를 순서대로 누르십시오. ({self.pos}/{self.length})"

    def items(self) -> list:
        return [self.btn(f"sig:{c}", c, row=0, disabled=self.showing) for c in MISSION_SIGNALS]

    async def start(self, interaction):
        self.s.spawn(self.show(interaction))

    async def show(self, interaction):
        self.showing = True
        self.locked = True
        self.pos = 0
        self.cur = None
        self.plays += 1
        await self.s.push(interaction)
        await mission_sleep(1.5)
        for i, c in enumerate(self.seq, 1):
            if self.s.phase != "step" or self.s.task is not self:
                return
            self.cur, self.cur_no = c, i
            await self.s.push(interaction)
            await mission_sleep(1.4)
        if self.s.phase != "step" or self.s.task is not self:
            return
        self.cur = None
        self.showing = False
        self.locked = False
        await self.s.push(interaction)

    async def handle(self, interaction, key, values):
        await _ack(interaction)
        if self.showing:
            return
        sym = key.split(":", 1)[1]
        if sym == self.seq[self.pos]:
            self.pos += 1
            if self.pos >= self.length:
                await self.s.advance(interaction, f"{self.title}: 완료")
            else:
                await self.s.push(interaction)
        else:
            if await self.s.add_alert(interaction, 10, f"{self.title}: 신호 순서 오류, 신호를 다시 재생"):
                return
            self.s.spawn(self.show(interaction))

class ProgressTask(MissionTask):
    """진행형 작업: 게이지가 차는 동안 기다리다가, 신호가 불안정해지면 제한 시간 안에 [신호 유지]를 눌러야 함"""
    def __init__(self, session, cfg):
        super().__init__(session, cfg)
        self.state = "ready"
        self.ratio = 0.0
        self.status = "작업 준비를 마쳤다."
        self.check_open = False
        self.signal = asyncio.Event()

    def body(self) -> str:
        if self.state == "ready":
            return (f"[작업 시작]을 누르면 작업이 진행됩니다. 작업에는 시간이 걸립니다.\n"
                    f"진행 중 신호가 불안정해지면 **{MISSION_CHECK_WINDOW_SEC}초 안에** [신호 유지]를 눌러야 합니다.\n"
                    f"신호를 놓치면 경계도가 오릅니다.")
        warn = ""
        if self.check_open:
            warn = f"\n\n**경고: 신호가 불안정합니다! {MISSION_CHECK_WINDOW_SEC}초 안에 [신호 유지]를 누르십시오.**"
        return f"진행 {_mission_bar(self.ratio)}\n{self.status}{warn}"

    def items(self) -> list:
        if self.state == "ready":
            return [self.btn("go", "작업 시작", discord.ButtonStyle.primary, row=0)]
        return [self.btn("signal", "신호 유지",
                         discord.ButtonStyle.danger if self.check_open else discord.ButtonStyle.secondary,
                         row=0, disabled=not self.check_open, lock_free=True)]

    async def handle(self, interaction, key, values):
        await _ack(interaction)
        if key == "go" and self.state == "ready":
            self.state = "running"
            self.locked = True
            self.s.spawn(self.run(interaction))
        elif key == "signal" and self.check_open:
            self.signal.set()

    async def run(self, interaction):
        total = self.cfg["seconds"]
        ticks = max(2, int(round(total / 2.0)))
        checks = {max(1, min(ticks - 1, round(ticks * (i + 1) / (self.cfg.get("checks", 0) + 1))))
                  for i in range(self.cfg.get("checks", 0))}
        statuses = self.cfg.get("status") or ["작업을 진행 중이다."]
        await self.s.push(interaction)
        for t in range(1, ticks + 1):
            await mission_sleep(total / ticks)
            if self.s.phase != "step" or self.s.task is not self:
                return
            self.ratio = t / ticks
            self.status = statuses[min(len(statuses) - 1, int(self.ratio * len(statuses)))]
            await self.s.push(interaction)
            if t in checks:
                self.check_open = True
                self.signal.clear()
                await self.s.push(interaction)
                ok = True
                try:
                    await asyncio.wait_for(self.signal.wait(), timeout=MISSION_CHECK_WINDOW_SEC)
                except asyncio.TimeoutError:
                    ok = False
                self.check_open = False
                if not ok:
                    self.status = "신호가 끊겨 연결을 다시 잡는 중이다."
                    if await self.s.locked_call(self.s.add_alert, interaction, 10, f"{self.title}: 신호 유지 실패"):
                        return
                else:
                    self.status = "신호를 안정시켰다."
                if self.s.phase != "step" or self.s.task is not self:
                    return
                await self.s.push(interaction)
        self.ratio = 1.0
        await self.s.locked_call(self.s.advance, interaction, f"{self.title}: 완료")

class SafeTask(MissionTask):
    """금고 / 자물쇠 해제: 숫자 맞히기 (위치까지 일치 / 숫자만 일치 힌트)"""
    def __init__(self, session, cfg):
        super().__init__(session, cfg)
        self.digits = cfg.get("digits", 3)
        self.max_tries = cfg.get("attempts", 6)
        self.code = "".join(random.sample("0123456789", self.digits))
        self.history = []

    def body(self) -> str:
        left = self.max_tries - len(self.history)
        text = (f"서로 다른 숫자 {self.digits}개로 이루어진 번호입니다. 남은 시도: **{left}회**\n"
                f"[다이얼 돌리기]를 눌러 번호를 입력하면 다음 반응을 알려 줍니다.\n"
                f"- 위치까지 일치: 숫자와 자리가 모두 맞음\n- 숫자만 일치: 숫자는 있지만 자리가 다름")
        if self.history:
            text += "\n\n" + "\n".join(self.history[-8:])
        return text

    def items(self) -> list:
        return [self.btn("dial", "다이얼 돌리기", discord.ButtonStyle.primary, row=0)]

    async def handle(self, interaction, key, values):
        if key == "dial":
            await interaction.response.send_modal(MissionCodeModal(self))
        else:
            await _ack(interaction)

    async def submit(self, interaction, text: str):
        if not re.fullmatch(r"[0-9]+", text) or len(text) != self.digits or len(set(text)) != self.digits:
            await interaction.response.send_message(
                f"서로 다른 숫자 {self.digits}개를 입력해야 합니다. (시도 횟수는 차감되지 않았습니다)", ephemeral=True)
            return
        await _ack(interaction)
        strikes = sum(1 for a, b in zip(text, self.code) if a == b)
        balls = sum(1 for ch in text if ch in self.code) - strikes
        self.history.append(f"{len(self.history) + 1}회차  {text}  -  위치까지 일치 {strikes}, 숫자만 일치 {balls}")
        if text == self.code:
            await self.s.advance(interaction, f"{self.title}: {len(self.history)}번 만에 해제")
        elif len(self.history) >= self.max_tries:
            self.s.log.append(f"{self.title}: 시도 횟수 소진, 자물쇠를 부수고 열었다 (정답 {self.code})")
            if await self.s.add_alert(interaction, 25, f"{self.title}: 자물쇠를 부수는 소음"):
                return
            await self.s.advance(interaction, None)
        else:
            await self.s.push(interaction)

class EncounterTask(MissionTask):
    """현장 돌발 상황: 장면 속 단서를 읽고 대처 방법을 선택"""
    def __init__(self, session, cfg):
        sc = session.pick_encounter()
        super().__init__(session, {"title": sc["title"], "text": sc["text"]})
        self.options = random.sample(sc["options"], len(sc["options"]))

    def body(self) -> str:
        return "어떻게 대처하시겠습니까?\n\n" + "\n".join(f"{chr(65 + i)}. {o['label']}" for i, o in enumerate(self.options))

    def items(self) -> list:
        return [self.btn(f"enc:{i}", f"{chr(65 + i)}. {o['label']}"[:80], row=i) for i, o in enumerate(self.options)]

    async def handle(self, interaction, key, values):
        await _ack(interaction)
        opt = self.options[int(key.split(":", 1)[1])]
        self.s.log.append(f"{self.title}: {opt['result']}")
        if opt["alert"] and await self.s.add_alert(interaction, opt["alert"], f"{self.title}: {opt['label']}"):
            return
        await self.s.advance(interaction, None)

MISSION_TASK_TYPES = {
    "travel": TravelTask, "sequence": SequenceTask, "wires": WiresTask, "memory": MemoryTask,
    "progress": ProgressTask, "safe": SafeTask, "encounter": EncounterTask,
}

# --- 임무 진행 화면 ---
MISSION_GLOBAL_KEYS = {"pick", "start", "back", "cancel", "abandon"}

class MissionSession(GuardedView):
    holds_command_lock = True    # 임무가 끝날 때까지 다른 명령어 실행 차단

    def __init__(self, owner: discord.Member, user_stats: dict, guild_id):
        super().__init__(timeout=MISSION_IDLE_TIMEOUT)
        self.owner = owner
        self.location = user_stats.get("location", "레벤하임")
        self.guild_id = guild_id
        self.inventory_snapshot = dict(user_stats.get("inventory") or {})
        self.phase = "select"           # select -> brief -> step -> done
        self.mission_key = None
        self.message = None
        self.lock = asyncio.Lock()
        self.step_id = 0
        self.task = None
        self.index = 0
        self.steps = []
        self.alert = 0.0
        self.peak_alert = 0.0
        self.alert_mult = 1.0
        self.final_rate = 0
        self.money_mult = 1.0
        self.honor_mult = 1.0
        self.buffs = []
        self.log = []
        self.used_encounters = set()
        self.abandon_armed = False
        self.drink_pending = False      # 쿨타임 중 시작했다면 자양강장제를 1회 소모 (결과 정산 때 차감)
        self._bg = set()
        self.rebuild()

    # ----- 보정치 계산 (장비 효과 / 경찰 순찰) -----
    def compute_modifiers(self):
        m = MISSIONS[self.mission_key]
        success_bonus, money_mult, honor_mult, buffs = 0, 1.0, 1.0, []
        for item_key, s_bonus, m_bonus, h_bonus, label in MISSION_BUFFS:
            if item_key in self.inventory_snapshot:
                success_bonus += s_bonus
                money_mult += m_bonus
                honor_mult += h_bonus
                buffs.append(label)
        patrol_penalty = 0
        if self.guild_id and self.guild_id in active_patrols:
            if time.time() < active_patrols[self.guild_id].get(self.location, 0):
                patrol_penalty = 15
                buffs.append("경찰 순찰망 가동 (-15% 성공률)")
        self.final_rate = max(1, min(98, m["success_rate"] + success_bonus - patrol_penalty))
        self.alert_mult = 50.0 / max(10, self.final_rate)
        self.money_mult, self.honor_mult, self.buffs = money_mult, honor_mult, buffs

    def pick_encounter(self) -> dict:
        pool = [i for i in range(len(MISSION_ENCOUNTERS)) if i not in self.used_encounters] or list(range(len(MISSION_ENCOUNTERS)))
        idx = random.choice(pool)
        self.used_encounters.add(idx)
        return MISSION_ENCOUNTERS[idx]

    # ----- 화면 -----
    def rebuild(self):
        self.clear_items()
        if self.phase == "select":
            options = [
                discord.SelectOption(
                    label=m["label"],
                    description=(f"난이도 {m['diff']} | {len(MISSION_SCRIPTS[k]['steps'])}단계 | {mission_estimate_text(k)} | "
                                 f"${m['reward'][0]:,}~${m['reward'][1]:,} / 명예 +{m['honor']} / 실패 -${m['fine']:,}"),
                    value=k)
                for k, m in MISSIONS.items()
            ]
            self.add_item(MissionSelectItem(self, "pick", "수행할 비밀 임무를 선택하세요...", options, row=0))
            self.add_item(MissionButton(self, "cancel", "취소", discord.ButtonStyle.secondary, row=1))
        elif self.phase == "brief":
            self.add_item(MissionButton(self, "start", "작전 개시", discord.ButtonStyle.success, row=0))
            self.add_item(MissionButton(self, "back", "다른 임무 선택", discord.ButtonStyle.secondary, row=0))
            self.add_item(MissionButton(self, "cancel", "취소", discord.ButtonStyle.danger, row=0))
        elif self.phase == "step" and self.task is not None:
            for it in self.task.items():
                self.add_item(it)
            self.add_item(MissionButton(
                self, "abandon", "포기 확정 (실패 처리)" if self.abandon_armed else "작전 포기",
                discord.ButtonStyle.danger, row=4, disabled=self.task.locked))

    def build_embed(self) -> discord.Embed:
        if self.phase == "select":
            embed = discord.Embed(
                title=f"[{self.location}] 비밀 임무 작전 하달",
                description=(f"**{self.owner.mention}** 님, 수행할 작전을 선택해 주십시오.\n\n"
                             "임무는 여러 단계의 현장 작업으로 이루어져 있으며 끝까지 수행하려면 시간이 걸립니다.\n"
                             "임무가 진행되는 동안에는 다른 명령어를 사용할 수 없습니다."),
                color=discord.Color.dark_purple())
        elif self.phase == "brief":
            m, sc = MISSIONS[self.mission_key], MISSION_SCRIPTS[self.mission_key]
            embed = discord.Embed(title=f"[{self.location}] 작전 브리핑 - {m['title']}", description=sc["briefing"],
                                  color=discord.Color.dark_purple())
            embed.add_field(name="작전 개요",
                            value=f"난이도: {m['diff']}\n작업 수: {len(sc['steps'])}단계\n예상 소요 시간: {mission_estimate_text(self.mission_key)}",
                            inline=True)
            embed.add_field(name="보수",
                            value=f"자금 ${m['reward'][0]:,} ~ ${m['reward'][1]:,}\n명예 +{m['honor']} pt\n실패 시 벌금 ${m['fine']:,}, 1분간 지명수배",
                            inline=True)
            embed.add_field(name="작업 순서",
                            value="\n".join(f"{i + 1}. {mission_step_title(st)}" for i, st in enumerate(sc["steps"])), inline=False)
            rules = ("실수하거나 돌발 상황에 잘못 대처하면 경계도가 오릅니다. 경계도가 100%가 되면 작전이 노출되어 실패합니다.\n"
                     "작전 도중 포기하거나 오랫동안 응답하지 않아도 실패로 처리됩니다.\n"
                     f"경계도 상승 배율: **x{self.alert_mult:.2f}** (장비 효과와 순찰 상황이 반영됨)")
            if self.buffs:
                rules += "\n적용 중인 효과: " + ", ".join(self.buffs)
            embed.add_field(name="경계도 규칙", value=rules, inline=False)
        else:
            sc_title = MISSIONS[self.mission_key]["title"]
            total = len(self.steps)
            embed = discord.Embed(
                title=f"{sc_title} - 단계 {self.index + 1}/{total}: {self.task.title}",
                description=f"{self.task.text}\n\n{self.task.body()}",
                color=(discord.Color.dark_purple() if self.alert < 40 else
                       discord.Color.orange() if self.alert < 70 else discord.Color.red()))
            embed.add_field(name="경계도", value=f"`{_mission_bar(self.alert / 100.0, '#')}`", inline=False)
            embed.add_field(name="작전 기록", value=_tail_lines(self.log[-6:], 700), inline=False)
        if self.owner.avatar:
            embed.set_thumbnail(url=self.owner.avatar.url)
        return embed

    async def push(self, interaction: discord.Interaction):
        if self.phase == "done":
            return
        self.rebuild()
        try:
            await interaction.edit_original_response(embed=self.build_embed(), view=self)
        except discord.HTTPException as e:
            print(f"Mission push error: {e}")

    # ----- 진행 제어 -----
    def spawn(self, coro):
        t = asyncio.create_task(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg_done)

    def _bg_done(self, t):
        self._bg.discard(t)
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            import traceback
            traceback.print_exception(type(exc), exc, exc.__traceback__)
            keep_task(asyncio.ensure_future(self._abort_no_penalty()))

    async def locked_call(self, fn, *args):
        async with self.lock:
            if self.phase != "step":
                return False
            return await fn(*args)

    async def _abort_no_penalty(self):
        """프로그램 오류로 중단된 경우: 보상도 벌금도 없이 종료"""
        if self.phase == "done":
            return
        self.phase = "done"
        self.stop()
        try:
            await self.message.edit(embed=discord.Embed(
                title="[작전 중단]", description="시스템 오류로 작전이 중단되었습니다. 보상과 벌금은 적용되지 않았습니다.",
                color=discord.Color.light_grey()), view=None)
        except Exception:
            pass

    async def _begin_step(self, interaction):
        cfg = self.steps[self.index]
        self.step_id += 1
        self.task = MISSION_TASK_TYPES[cfg["type"]](self, cfg)
        await self.push(interaction)
        await self.task.start(interaction)

    async def advance(self, interaction, note):
        if self.phase != "step":
            return
        if note:
            self.log.append(note)
        self.index += 1
        if self.index >= len(self.steps):
            await self._finalize(interaction, True, "모든 작업을 마쳤다.")
        else:
            await self._begin_step(interaction)

    async def add_alert(self, interaction, base: float, reason: str) -> bool:
        """경계도를 올림. 100%에 도달해 작전이 끝났으면 True"""
        inc = base * self.alert_mult
        self.alert = min(100.0, self.alert + inc)
        self.peak_alert = max(self.peak_alert, self.alert)
        self.log.append(f"경계도 +{inc:.0f}%  ({reason})")
        if self.alert >= 100.0:
            await self._finalize(interaction, False, "경계도가 한계에 도달해 작전이 노출되었다.")
            return True
        return False

    async def _finalize(self, interaction, success: bool, reason: str):
        self.phase = "done"
        self.stop()
        await finalize_mission(self, success, reason, interaction.edit_original_response)

    # ----- 입력 처리 -----
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner.id:
            await interaction.response.send_message("본인의 임무 창만 조작할 수 있습니다.", ephemeral=True)
            return False
        return True

    async def dispatch(self, interaction, key, values, step_id, lock_free):
        if self.phase == "done" or (key not in MISSION_GLOBAL_KEYS and step_id != self.step_id):
            await _ack(interaction)          # 이미 끝났거나 이전 단계의 오래된 버튼
            return
        if lock_free:
            await self.task.handle(interaction, key, values)
            return
        async with self.lock:
            if self.phase == "done":
                await _ack(interaction)
                return
            if key != "abandon":
                self.abandon_armed = False
            if key in MISSION_GLOBAL_KEYS:
                await self.handle_global(interaction, key, values)
            else:
                await self.task.handle(interaction, key, values)

    async def handle_global(self, interaction, key, values):
        await _ack(interaction)
        if key == "pick":
            self.mission_key = values[0]
            self.compute_modifiers()
            self.phase = "brief"
            await self.push(interaction)
        elif key == "back":
            self.phase = "select"
            await self.push(interaction)
        elif key == "cancel":
            self.phase = "done"
            self.stop()
            await interaction.edit_original_response(embed=discord.Embed(
                title="[작전 취소]", description="임무를 시작하지 않고 취소했습니다.", color=discord.Color.light_grey()), view=None)
        elif key == "start":
            self.compute_modifiers()
            _inv = self.inventory_snapshot
            if (mission_cooldowns.get(self.owner.id, 0) > time.time() and NO_COOLDOWN_ITEM not in _inv
                    and _inv.get(DRINK_ITEM, 0) > 0):
                self.drink_pending = True
                self.buffs.append("자양강장제 (쿨타임 초기화·1회 소모)")
            self.steps = MISSION_SCRIPTS[self.mission_key]["steps"]
            self.index = 0
            self.phase = "step"
            self.log = [f"작전 개시: {MISSIONS[self.mission_key]['title']}"]
            await self._begin_step(interaction)
        elif key == "abandon":
            if self.phase != "step":
                return
            if not self.abandon_armed:
                self.abandon_armed = True
                await self.push(interaction)
            else:
                await self._finalize(interaction, False, "작전을 포기하고 현장에서 철수했다.")

    async def on_timeout(self):
        if self.phase in ("select", "brief"):
            self.phase = "done"
            if self.message:
                try:
                    await self.message.edit(embed=discord.Embed(
                        title="[작전 취소]", description="응답이 없어 임무 선택이 취소되었습니다.", color=discord.Color.light_grey()), view=None)
                except discord.HTTPException:
                    pass
        elif self.phase == "step":
            self.phase = "done"
            if self.message:
                await finalize_mission(self, False, "오랫동안 응답하지 않아 작전이 중단되었다.", self.message.edit)

    async def on_error(self, interaction, error, item):
        import traceback
        traceback.print_exception(type(error), error, error.__traceback__)
        await self._abort_no_penalty()

async def finalize_mission(session: MissionSession, success: bool, reason: str, edit):
    """임무 결과 정산 (보상 / 실패 처리 / 칭호 / 일일 의뢰). edit: 결과 화면을 표시할 함수"""
    user = session.owner
    selected = MISSIONS[session.mission_key]
    user_stats = get_user_data(user.id)
    old_rank = get_user_rank(user_stats.get("honor", 0))
    inventory = user_stats.get("inventory", {})
    if session.drink_pending and inventory.get(DRINK_ITEM, 0) > 0:   # 쿨타임 초기화 효과로 시작했다면 자양강장제 소모
        inventory[DRINK_ITEM] -= 1
        if inventory[DRINK_ITEM] <= 0:
            del inventory[DRINK_ITEM]
    if NO_COOLDOWN_ITEM not in inventory:   # 쿨타임은 임무가 끝난 시점부터 계산
        fast = any(k in inventory for k in CHRONO_ITEMS)
        mission_cooldowns[user.id] = time.time() + (MISSION_COOLDOWN_FAST_SEC if fast else MISSION_COOLDOWN_SEC)
    current_loc = session.location
    current_money = user_stats.get("money", 0)
    current_honor = user_stats.get("honor", 0)

    buff_desc = f"\n**적용된 효과:** {', '.join(session.buffs)}" if session.buffs else ""
    record = f"\n\n**작전 기록**\n{_tail_lines(session.log, 800)}\n**최대 경계도:** `{int(session.peak_alert)}%`"
    fail_msg = ""
    rank_up_msg = ""

    blob = get_title_blob(user_stats)
    s = blob["stats"]

    if success:
        base_money = random.randint(*selected["reward"])
        earned_money = int(base_money * session.money_mult)
        earned_honor = int(selected["honor"] * session.honor_mult)

        current_money += earned_money
        current_honor += earned_honor

        s["success"] += 1
        s["streak"] += 1
        s["best_streak"] = max(s["best_streak"], s["streak"])
        if session.mission_key in ("mission_3", "mission_5"):
            s["hard_success"] += 1

        new_rank = get_user_rank(current_honor)
        if old_rank != new_rank:
            rank_up_msg = f"\n\n**[조직 승급 축하]** **{old_rank}** **{new_rank}**(으)로 승급하셨습니다!"

        rank_up_msg += daily_record(blob, "mission", {**user_stats, "money": current_money, "honor": current_honor})

        new_title_ids = evaluate_new_titles(blob, {**user_stats, "money": current_money, "honor": current_honor})
        rank_up_msg += format_new_titles(new_title_ids)

        embed = discord.Embed(
            title=f"[{current_loc}] 임무 성공",
            description=f"**{user.mention}** 님이 작전을 완수했습니다. {reason}{buff_desc}\n\n"
                        f"**작전명:** {selected['title']}\n"
                        f"**획득 보상금:** `${earned_money:,}`\n"
                        f"**획득 명예:** `+{earned_honor} pt`\n\n"
                        f"**현재 직급:** `{new_rank}`\n"
                        f"**보유 자금:** `${current_money:,}`\n"
                        f"**총 명예:** `{current_honor} pt`{record}{rank_up_msg}",
            color=discord.Color.green()
        )
    else:
        settings = user_stats.get("settings") or {}
        guard_item_key = "임무 실패 방지권"
        guard_used = False

        if settings.get("auto_fail_guard", True) and inventory.get(guard_item_key, 0) > 0:
            inventory[guard_item_key] -= 1
            if inventory[guard_item_key] <= 0:
                del inventory[guard_item_key]
            guard_used = True

        if guard_used:
            fail_msg = "**[실패 방지권 발동]** 임무에 실패했으나, 가방에 있던 **임무 실패 방지권**이 소모되어 수배 및 벌금이 면제되었습니다!"
        else:
            fine = min(current_money, selected["fine"])
            current_money -= fine
            wanted_list[user.id] = datetime.now() + timedelta(seconds=60)
            save_status(user.id)
            fail_msg = f"**작전 실패!** 현장에 흔적이 남아 **1분간 경찰 지명수배 상태**가 됩니다! (벌금 `-${fine:,}`)"

        s["fail"] += 1
        s["streak"] = 0
        new_title_ids = evaluate_new_titles(blob, user_stats)

        current_rank = get_user_rank(current_honor)
        embed = discord.Embed(
            title=f"[{current_loc}] 임무 실패",
            description=f"**{user.mention}** 님의 작전이 실패했습니다. {reason}{buff_desc}\n\n"
                        f"**작전명:** {selected['title']}\n"
                        f"{fail_msg}\n\n"
                        f"**현재 직급:** `{current_rank}`\n"
                        f"**보유 자금:** `${current_money:,}`\n"
                        f"**총 명예:** `{current_honor} pt`{record}{format_new_titles(new_title_ids)}",
            color=discord.Color.red()
        )

    try:
        _require_db()
        supabase.table("user_data").update({
            "money": current_money,
            "honor": current_honor,
            "inventory": inventory,
            "data": blob
        }).eq("user_id", str(user.id)).execute()
    except Exception as e:
        print(f"DB Error (mission update): {e}")
        embed = discord.Embed(title="[저장 오류]", description="DB 저장 중 오류가 발생해 임무 결과를 반영하지 못했습니다.",
                              color=discord.Color.dark_grey())

    if user.avatar:
        embed.set_thumbnail(url=user.avatar.url)

    try:
        await edit(embed=embed, view=None)
    except discord.HTTPException as e:
        print(f"Mission result edit error: {e}")
