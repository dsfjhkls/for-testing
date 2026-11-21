"""/설정, /검거, /밀수"""

import asyncio
import discord
import random
import time
from discord import app_commands
from datetime import datetime, timedelta

from .core import bot, inc_money, jail_list, supabase, wanted_list
from .status import (
    _check_updated,
    _require_db,
    active_patrols,
    check_jail,
    get_user_data,
    get_user_rank,
    save_status,
    update_user_location,
    update_user_stats,
)
from .alignment import CORRUPT_ARREST_BONUS_HONOR, get_title_blob
from .achievements import POLICE_BIG_FISH_HONOR, police_ach_bump
from .admin import check_maintenance, check_membership
from .transfer import SettingsView

 
# === 통합 `/설정` 슬래시 명령어 ===
@bot.tree.command(name="설정", description="[조직원/경찰 전용] 게임 내 통합 환경 설정 창을 엽니다.")
async def settings_command(interaction: discord.Interaction):
    if not await check_maintenance(interaction): return
    if await check_jail(interaction): return
    if not await check_membership(interaction): return
 
    user = interaction.user
    settings = dict(get_user_data(user.id).get("settings") or {})
 
    view = SettingsView(user=user, settings=settings)
    await interaction.response.send_message(embed=view.current_embed(), view=view, ephemeral=True)
    view.message = await interaction.original_response()

# === [경찰 전용] `/검거` 성공 시 아이템 압류 ===
ARREST_SEIZE_MIN = 1   # 한 번에 압류하는 아이템 종류 수 (최소)
ARREST_SEIZE_MAX = 2   # 한 번에 압류하는 아이템 종류 수 (최대)

def seize_items_sync(police_id: int, target_id: int) -> list:
    """검거된 조직원의 아이템 중 랜덤 1~2종을 1개씩 빼앗아 경찰 인벤토리에 넣습니다. (동기 DB 작업 → to_thread 로 실행)
    재료와 거래 불가 아이템(네임드 전리품 등)은 압류하지 않습니다. 반환: 압류한 아이템 이름 목록"""
    from .auction import auction_blocked_reason   # 순환 import 방지: 호출 시점에 가져옴
    from .crafting import _inv_lock   # 순환 import 방지: 호출 시점에 가져옴
    _require_db()
    with _inv_lock:
        t_inv = dict(get_user_data(target_id).get("inventory") or {})
        candidates = [k for k, v in t_inv.items() if int(v or 0) > 0 and not k.startswith("[재료]") and auction_blocked_reason(k) is None]
        if not candidates:
            return []
        picked = random.sample(candidates, min(random.randint(ARREST_SEIZE_MIN, ARREST_SEIZE_MAX), len(candidates)))
        new_t_inv = dict(t_inv)
        for k in picked:
            if int(new_t_inv[k]) <= 1:
                new_t_inv.pop(k, None)
            else:
                new_t_inv[k] = int(new_t_inv[k]) - 1

        p_inv = dict(get_user_data(police_id).get("inventory") or {})
        for k in picked:
            p_inv[k] = int(p_inv.get(k, 0) or 0) + 1

        # '조직원에게서 빼앗기 → 경찰에게 넣기' 순서로 저장하고, 경찰 쪽 저장이 실패하면 조직원 인벤토리를 되돌린다
        res = supabase.table("user_data").update({"inventory": new_t_inv}).eq("user_id", str(target_id)).execute()
        _check_updated(res, "seize (target)")
        try:
            res = supabase.table("user_data").update({"inventory": p_inv}).eq("user_id", str(police_id)).execute()
            _check_updated(res, "seize (police)")
        except Exception:
            try:
                supabase.table("user_data").update({"inventory": t_inv}).eq("user_id", str(target_id)).execute()
            except Exception as e2:
                print(f"DB Error (seize rollback): {e2}")
            raise
        return picked

# === [경찰 전용] `/검거` 명령어 (서버 공개 알림형) ===
@bot.tree.command(name="검거", description="[경찰 전용] 지명수배 중인 마피아를 체포하여 감옥에 가두고 소지품 일부를 압류합니다.")
@app_commands.describe(member="검거할 수배 대상 마피아")
async def arrest_member(interaction: discord.Interaction, member: discord.Member):
    from .salary import POLICE_SALARY_ARREST_BONUS_MAX, POLICE_SALARY_ARREST_BONUS_PER, police_shift_record   # 순환 import 방지: 호출 시점에 가져옴
    from .police_shop import ARREST_BASE_HONOR, ARREST_BASE_REWARD, ARREST_HONOR_BONUS_MAX, ARREST_HONOR_PER_POINT, POLICE_BADGE_HONOR_BONUS, POLICE_RADIO_ARREST_BONUS, police_bonuses   # 순환 import 방지: 호출 시점에 가져옴
    from .corrupt_police import try_corrupt_arrest   # 순환 import 방지: 호출 시점에 가져옴
    if not await check_maintenance(interaction): return

    police_role = discord.utils.get(interaction.guild.roles, name="경찰")
    if not police_role or police_role not in interaction.user.roles:
        await interaction.response.send_message("치안국 경찰만 검거 명령어를 사용할 수 있습니다!", ephemeral=True)
        return

    # 3초 타임아웃 방지용 defer (ephemeral=True를 제거하여 서버 공개 메시지로 전환)
    await interaction.response.defer()

    now = datetime.now()
    target_id = member.id

    # [밀수 거래] 이 경찰이 뇌물을 받은 마피아는 면제 시간 동안 검거할 수 없음
    immune_until = smuggle_immunity.get((interaction.user.id, target_id))
    if immune_until:
        if now < immune_until:
            remain_min = max(1, int((immune_until - now).total_seconds() // 60) + 1)
            await interaction.followup.send(
                f"**{member.display_name}** 님과는 밀수 거래가 성사된 상태라 **약 {remain_min}분간** 검거할 수 없습니다.",
                ephemeral=True
            )
            return
        smuggle_immunity.pop((interaction.user.id, target_id), None)

    # 수배 상태 검증
    is_wanted = False
    matched_key = None

    for key in list(wanted_list.keys()):
        if int(key) == int(target_id):
            matched_key = key
            expiry_time = wanted_list[key]
            if now <= expiry_time:
                is_wanted = True
            break

    if not is_wanted:
        if matched_key is not None:
            wanted_list.pop(matched_key, None)
            save_status(target_id)
        # [부패 경찰] 수배 상태가 아니어도 시민은 마음대로 체포할 수 있음
        if await try_corrupt_arrest(interaction, member):
            return
        # 수배 상태가 아닐 때는 나만 보이게(ephemeral=True) 처리
        await interaction.followup.send(f"**{member.display_name}** 님은 현재 지명수배 상태가 아니거나 증거가 인멸되었습니다.", ephemeral=True)
        return

    # 수배 해제 및 감옥 수감 (5분)
    wanted_list.pop(matched_key, None)
    jail_time_seconds = 300
    jail_list[target_id] = now + timedelta(seconds=jail_time_seconds)
    save_status(target_id)

    # DB 위치 변경
    try:
        update_user_location(target_id, "감옥")
    except Exception as e:
        print(f"Location update error: {e}")

    # 경찰 보상 지급 (무전기: 포상금 +20% / 황금 배지: 명예 +20%)
    _pb = police_bonuses(get_user_data(interaction.user.id).get("inventory"))
    arrest_money = int(round(ARREST_BASE_REWARD * _pb["money_mult"]))
    _target_corrupt = False
    try:
        _tdata = await asyncio.to_thread(get_user_data, target_id)
        _target_honor = max(0, int(_tdata.get("honor", 0)))
        _target_corrupt = "경찰" in {r.name for r in member.roles} and bool(get_title_blob(_tdata).get("corrupt"))
    except Exception:
        _target_honor = 0
    arrest_extra = min(ARREST_HONOR_BONUS_MAX, _target_honor // ARREST_HONOR_PER_POINT)   # 거물 검거 보너스
    arrest_honor = int(round((ARREST_BASE_HONOR + arrest_extra) * _pb["honor_mult"]))
    money_note = f" (무전기 +{round(POLICE_RADIO_ARREST_BONUS * 100)}%)" if _pb["money_mult"] > 1 else ""
    honor_note = f" (황금 배지 +{round(POLICE_BADGE_HONOR_BONUS * 100)}%)" if _pb["honor_mult"] > 1 else ""
    if arrest_extra > 0:
        honor_note = f" (거물 검거 보너스 +{arrest_extra}){honor_note}"
    if _target_corrupt:   # 부패 경찰을 잡으면 추가 명예
        arrest_honor += CORRUPT_ARREST_BONUS_HONOR
        honor_note += f" (부패 경찰 검거 +{CORRUPT_ARREST_BONUS_HONOR})"
    try:
        update_user_stats(interaction.user.id, arrest_money, arrest_honor)
    except Exception:
        pass
    police_shift_record(interaction.user.id, "arrest",
                        bonus=min(POLICE_SALARY_ARREST_BONUS_MAX, _target_honor // POLICE_SALARY_ARREST_BONUS_PER))   # /월급 근무 실적
    ach_note = await police_ach_bump(interaction.user.id, police_arrests=1,
                                     police_big_arrests=1 if _target_honor >= POLICE_BIG_FISH_HONOR else 0)   # 업적 카운터

    # 압류: 조직원 아이템 랜덤 1~2종을 경찰이 가져감
    try:
        seized_items = await asyncio.to_thread(seize_items_sync, interaction.user.id, target_id)
    except Exception as e:
        print(f"Seize error: {e}")
        seized_items = []
    seize_text = ", ".join(f"`{it}`" for it in seized_items) if seized_items else "압류할 수 있는 물품이 없었습니다."

    police_info = get_user_data(interaction.user.id)
    police_rank = get_user_rank(police_info["honor"], is_police=True)

    # 서버 전체에 보일 검거 임베드 생성
    embed = discord.Embed(
        title="[치안국 긴급 공지] 지명수배범 검거 완료",
        description=(
            f"**{interaction.user.mention}** 경찰관이 수배범 **{member.mention}** (을)를 현장에서 체포했습니다!\n\n"
            f"**처벌 내용:** **{jail_time_seconds // 60}분** 동안 감옥에 수감되며 프로필 외 모든 활동이 통제됩니다.\n"
            f"**현재 거점:** `감옥`\n\n"
            f"**검거 포상금:** `${arrest_money:,}`{money_note}\n"
            f"**경찰 실적:** `+{arrest_honor} pt`{honor_note} (현재 직급: `{police_rank}`)\n"
            f"**압류 물품:** {seize_text}"
            + ach_note
        ),
        color=discord.Color.dark_blue()
    )
    if member.avatar:
        embed.set_thumbnail(url=member.avatar.url)

    # 서버 전체(채널)에 공개적으로 메시지 전송
    await interaction.followup.send(embed=embed)

# === [조직원 전용] `/밀수` 명령어 (경찰 매수) ===
SMUGGLE_MIN_BRIBE = 500            # 최소 뇌물
SMUGGLE_MAX_BRIBE = 50000          # 최대 뇌물
SMUGGLE_COOLDOWN_SEC = 300         # 마피아의 밀수 제안 쿨타임 (5분)
SMUGGLE_TIMEOUT_SEC = 60           # 경찰이 응답할 수 있는 시간
SMUGGLE_IMMUNITY_SEC = 600         # 수락 시 그 경찰이 해당 마피아를 검거할 수 없는 시간 (10분)
SMUGGLE_REJECT_WANTED_SEC = 90     # 거절당했을 때 마피아에게 붙는 지명수배 시간
SMUGGLE_REJECT_FINE = 300          # 거절당했을 때 마피아 벌금
SMUGGLE_REJECT_HONOR_LOSS = 40     # 거절당했을 때 마피아 명예 감소
SMUGGLE_FINE_PER_MISSING_HONOR = 25  # 명예가 모자라 못 깎은 만큼 벌금으로 환산 ($/pt)
SMUGGLE_REJECT_POLICE_HONOR = 20   # 제안을 거절한 청렴한 경찰의 명예 포상

smuggle_cooldowns = {}   # {mafia_id: datetime}
smuggle_pending = set()  # 응답 대기 중인 마피아 id (동시에 한 건만)
smuggle_immunity = {}    # {(police_id, mafia_id): datetime (면제 만료)}

class SmuggleView(discord.ui.View):
    def __init__(self, mafia: discord.Member, police: discord.Member, amount: int):
        super().__init__(timeout=SMUGGLE_TIMEOUT_SEC)
        self.mafia = mafia
        self.police = police
        self.amount = amount
        self.resolved = False
        self.message = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.police.id:
            await interaction.response.send_message("이 제안은 대상 경찰만 응답할 수 있습니다.", ephemeral=True)
            return False
        return True

    def _close(self):
        self.resolved = True
        smuggle_pending.discard(self.mafia.id)
        for child in self.children:
            child.disabled = True
        self.stop()

    @discord.ui.button(label="수락 (뒷돈 받기)", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.resolved:
            await interaction.response.send_message("이미 처리된 제안입니다.", ephemeral=True)
            return
        self._close()
        await interaction.response.defer()

        now = datetime.now()
        mafia_data = get_user_data(self.mafia.id)

        if self.mafia.id in jail_list and now < jail_list[self.mafia.id]:
            embed = discord.Embed(title="거래 불발", description=f"{self.mafia.mention} 님이 감옥에 수감되어 거래가 무산되었습니다.", color=discord.Color.dark_grey())
            await interaction.edit_original_response(embed=embed, view=None)
            return
        if mafia_data["money"] < self.amount:
            embed = discord.Embed(title="거래 불발", description=f"{self.mafia.mention} 님의 자금이 부족해 거래가 무산되었습니다. (제안 `${self.amount:,}`)", color=discord.Color.dark_grey())
            await interaction.edit_original_response(embed=embed, view=None)
            return

        try:
            update_user_stats(self.mafia.id, -self.amount, 0)
            update_user_stats(self.police.id, self.amount, 0)
        except Exception as e:
            print(f"Smuggle accept DB error: {e}")
            await interaction.edit_original_response(
                embed=discord.Embed(title="처리 오류", description="DB 오류로 거래가 취소되었습니다.", color=discord.Color.red()), view=None)
            return

        smuggle_immunity[(self.police.id, self.mafia.id)] = now + timedelta(seconds=SMUGGLE_IMMUNITY_SEC)

        embed = discord.Embed(
            title="[밀수 거래 성사]",
            description=(
                f"**{self.police.mention}** 경관이 **{self.mafia.mention}** 님의 제안을 받아들였습니다.\n\n"
                f"**오간 뒷돈:** `${self.amount:,}`\n"
                f"**효과:** 앞으로 **{SMUGGLE_IMMUNITY_SEC // 60}분간** 이 경찰은 해당 마피아를 `/검거`할 수 없습니다.\n"
                f"다른 경찰에게는 효과가 없습니다."
            ),
            color=discord.Color.dark_gold()
        )
        await interaction.edit_original_response(embed=embed, view=None)

    @discord.ui.button(label="거절 (청렴)", style=discord.ButtonStyle.danger)
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.resolved:
            await interaction.response.send_message("이미 처리된 제안입니다.", ephemeral=True)
            return
        self._close()
        await interaction.response.defer()

        mafia_data = get_user_data(self.mafia.id)
        honor_loss = min(mafia_data["honor"], SMUGGLE_REJECT_HONOR_LOSS)
        # 명예가 부족해 다 못 깎은 만큼은 벌금으로 대신 부과 (명예 0pt 신입도 패널티를 체감하도록)
        missing_honor = SMUGGLE_REJECT_HONOR_LOSS - honor_loss
        fine = min(mafia_data["money"], SMUGGLE_REJECT_FINE + missing_honor * SMUGGLE_FINE_PER_MISSING_HONOR)

        try:
            update_user_stats(self.mafia.id, -fine, -honor_loss)
            update_user_stats(self.police.id, 0, SMUGGLE_REJECT_POLICE_HONOR)
        except Exception as e:
            print(f"Smuggle reject DB error: {e}")

        wanted_list[self.mafia.id] = datetime.now() + timedelta(seconds=SMUGGLE_REJECT_WANTED_SEC)
        save_status(self.mafia.id)

        embed = discord.Embed(
            title="[밀수 시도 적발]",
            description=(
                f"**{self.police.mention}** 경관이 **{self.mafia.mention}** 님의 뇌물 제안을 단호히 거절했습니다!\n\n"
                f"**마피아 패널티:** **{SMUGGLE_REJECT_WANTED_SEC}초간 지명수배** / 벌금 `-${fine:,}` / 명예 `-{honor_loss} pt`"
                + (f" (명예 부족분 {missing_honor}pt는 벌금으로 환산)" if missing_honor > 0 else "") + "\n"
                f"**경찰 포상:** 청렴 포상 `+{SMUGGLE_REJECT_POLICE_HONOR} pt`"
            ),
            color=discord.Color.red()
        )
        await interaction.edit_original_response(embed=embed, view=None)

    async def on_timeout(self):
        if self.resolved:
            return
        self._close()
        if self.message:
            try:
                embed = discord.Embed(
                    title="[밀수 제안 만료]",
                    description=f"{self.police.mention} 경관이 응답하지 않아 제안이 사라졌습니다. (패널티 없음)",
                    color=discord.Color.dark_grey()
                )
                await self.message.edit(embed=embed, view=None)
            except Exception:
                pass


@bot.tree.command(name="밀수", description="[조직원 전용] 경찰에게 뇌물을 건네 밀수를 눈감아 달라고 제안합니다. (수락/거절은 경찰 마음)")
@app_commands.describe(경찰="뇌물을 건넬 경찰", 뇌물=f"건넬 자금 (${SMUGGLE_MIN_BRIBE:,}~${SMUGGLE_MAX_BRIBE:,})")
async def smuggle_command(interaction: discord.Interaction, 경찰: discord.Member, 뇌물: app_commands.Range[int, SMUGGLE_MIN_BRIBE, SMUGGLE_MAX_BRIBE]):
    if not await check_maintenance(interaction): return
    if not await check_membership(interaction): return
    if await check_jail(interaction): return

    user = interaction.user
    mafia_role = discord.utils.get(interaction.guild.roles, name="조직원")
    police_role = discord.utils.get(interaction.guild.roles, name="경찰")

    if not mafia_role or mafia_role not in user.roles:
        await interaction.response.send_message("밀수는 마피아 조직원만 시도할 수 있습니다!", ephemeral=True)
        return
    if 경찰.id == user.id or 경찰.bot or not police_role or police_role not in 경찰.roles:
        await interaction.response.send_message("뇌물을 건넬 대상은 **경찰** 역할을 가진 유저여야 합니다.", ephemeral=True)
        return

    now = datetime.now()
    if user.id in smuggle_pending:
        await interaction.response.send_message("이미 응답을 기다리는 밀수 제안이 있습니다.", ephemeral=True)
        return
    if user.id in smuggle_cooldowns:
        remaining = int((smuggle_cooldowns[user.id] - now).total_seconds())
        if remaining > 0:
            await interaction.response.send_message(f"눈치가 보여 당분간 밀수를 못 합니다. **{remaining}초** 뒤에 다시 시도하세요.", ephemeral=True)
            return
    existing = smuggle_immunity.get((경찰.id, user.id))
    if existing and now < existing:
        await interaction.response.send_message(f"이미 **{경찰.display_name}** 경관과 거래가 성사된 상태입니다.", ephemeral=True)
        return
    if get_user_data(user.id)["money"] < 뇌물:
        await interaction.response.send_message("건넬 자금이 부족합니다!", ephemeral=True)
        return

    smuggle_cooldowns[user.id] = now + timedelta(seconds=SMUGGLE_COOLDOWN_SEC)
    smuggle_pending.add(user.id)

    embed = discord.Embed(
        title="[은밀한 밀수 제안]",
        description=(
            f"**{user.mention}** 님이 **{경찰.mention}** 경관에게 접근해 봉투를 내밉니다...\n\n"
            f"**제안 금액:** `${뇌물:,}`\n"
            f"**조건:** 수락하면 **{SMUGGLE_IMMUNITY_SEC // 60}분간** 경관은 이 마피아를 검거할 수 없습니다.\n"
            f"거절하면 마피아는 지명수배·벌금·명예 하락의 패널티를 받습니다.\n\n"
            f"**{SMUGGLE_TIMEOUT_SEC}초** 안에 응답하세요."
        ),
        color=discord.Color.dark_purple()
    )
    view = SmuggleView(mafia=user, police=경찰, amount=뇌물)
    await interaction.response.send_message(content=경찰.mention, embed=embed, view=view)
    view.message = await interaction.original_response()

    # === [경찰 전용] `/순찰` 명령어 ===
# 경찰 활동 명예 (이 값들을 조절해서 경찰 성장 속도를 맞추세요)
PATROL_HONOR = 28                   # /순찰 1회 명예

patrol_cooldowns = {}

@bot.tree.command(name="순찰", description="[경찰 전용] 지정한 거점을 순찰하여 마피아들의 임무 성공률을 낮추고 수당을 받습니다.")
@app_commands.choices(지역=[
    app_commands.Choice(name="레벤하임 (수도)", value="레벤하임"),
    app_commands.Choice(name="던베르크 (공업지대)", value="던베르크"),
    app_commands.Choice(name="블랙워드 (빈민가)", value="블랙워드"),
    app_commands.Choice(name="에르느코항 (항구도시)", value="에르느코항"),
    app_commands.Choice(name="로젠펠트 (유흥업소)", value="로젠펠트")
])
async def patrol_location(interaction: discord.Interaction, 지역: app_commands.Choice[str]):
    from .salary import police_shift_record   # 순환 import 방지: 호출 시점에 가져옴
    from .police_shop import police_bonuses   # 순환 import 방지: 호출 시점에 가져옴
    if not await check_maintenance(interaction): return
    if await check_jail(interaction): return

    user = interaction.user
    guild_id = interaction.guild_id

    # 1. 경찰 역할 검증
    police_role = discord.utils.get(interaction.guild.roles, name="경찰")
    if not police_role or police_role not in user.roles:
        await interaction.response.send_message("치안국 소속 경찰만 순찰 임무를 수행할 수 있습니다!", ephemeral=True)
        return

    # 2. 쿨타임 체크 (3분)
    now = datetime.now()
    if user.id in patrol_cooldowns:
        remaining = int((patrol_cooldowns[user.id] - now).total_seconds())
        if remaining > 0:
            await interaction.response.send_message(f"순찰 피로가 아직 안 풀렸습니다. **{remaining}초** 뒤에 다시 순찰할 수 있습니다.", ephemeral=True)
            return

    patrol_cooldowns[user.id] = now + timedelta(seconds=180)
    await interaction.response.defer()

    selected_loc = 지역.value

    # 3. 해당 지역에 5분(300초) 동안 순찰 효과 부여
    if guild_id not in active_patrols:
        active_patrols[guild_id] = {}
    
    patrol_duration = 300  # 5분
    active_patrols[guild_id][selected_loc] = time.time() + patrol_duration

    # 4. 경찰 보상 지급 (자금 $400~800, 명예 PATROL_HONOR)
    earned_money = inc_money(random.randint(400, 800))
    earned_honor = int(round(PATROL_HONOR * police_bonuses(get_user_data(user.id).get("inventory"))["honor_mult"]))

    try:
        update_user_stats(user.id, earned_money, earned_honor)
    except Exception as e:
        print(f"Patrol reward error: {e}")
    police_shift_record(user.id, "patrol")   # /월급 근무 실적
    ach_note = await police_ach_bump(user.id, police_patrols=1)   # 업적 카운터

    police_info = get_user_data(user.id)
    police_rank = get_user_rank(police_info["honor"], is_police=True)

    embed = discord.Embed(
        title=f"[{selected_loc}] 치안 순찰 완료",
        description=(
            f"**{user.mention}** 경관이 **{selected_loc}** 구역의 순찰을 철저히 마쳤습니다!\n\n"
            f"**효과:** 향후 **5분간** 해당 지역에서 활동하는 마피아들의 임무 성공률이 **15% 감소**합니다.\n\n"
            f"**순찰 수당:** `${earned_money:,}`\n"
            f"**경찰 실적:** `+{earned_honor} pt` (현재 직급: `{police_rank}`)\n"
            f"**다음 순찰 가능:** 3분 후"
            + ach_note
        ),
        color=discord.Color.blue()
    )
    if user.avatar:
        embed.set_thumbnail(url=user.avatar.url)

    await interaction.followup.send(embed=embed)
