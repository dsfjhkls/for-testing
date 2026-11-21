"""전투 스킬, NPC 턴제 전투, /전투 /보스 /네임드"""

import discord
import random
from discord import app_commands
from datetime import datetime, timedelta

from .core import (
    GuardedView,
    active_boss_guilds,
    boss_challengers,
    bot,
    report_error,
    set_active_boss,
    supabase,
)
from .status import (
    BOSS_COOLDOWN_SEC,
    NPC_LOSS_PENALTY,
    _require_db,
    check_jail,
    compute_combat_stats,
    get_user_data,
)
from .alignment import evaluate_new_titles, format_new_titles, get_title_blob
from .achievements import daily_record
from .admin import check_maintenance, check_membership
from .battle_data import (
    BOSS_KINDS,
    BOSS_NAMES,
    BOSS_SPAWN_TABLE,
    ELITE_PREFIX,
    NAMED_MONSTERS,
    NPC_DATA,
    boss_chance_text,
    boss_image_file,
)

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
        from .crafting import add_materials, format_material_drops, roll_material_drops   # 순환 import 방지: 호출 시점에 가져옴
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
