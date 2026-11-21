"""턴제 PVP 결투 (/결투)"""

import asyncio
import discord
from discord import app_commands
from datetime import datetime, timedelta

from .core import GuardedView, bot, jail_list, supabase
from .status import (
    DUEL_MAX_BET,
    WEAPON_SHOP_ITEMS,
    _require_db,
    check_jail,
    compute_combat_stats,
    get_user_data,
)
from .alignment import format_new_titles
from .achievements import bump_user_stats_sync
from .admin import check_maintenance, check_membership
from .battle import (
    SKILL_FOOTER,
    SKILL_LABELS,
    SKILL_RALLY_BONUS,
    SKILL_RALLY_TURNS,
    SkillState,
    duel_cooldowns,
    skill_hit,
)

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
