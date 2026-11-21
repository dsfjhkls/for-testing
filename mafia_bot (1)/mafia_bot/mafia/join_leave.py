"""패밀리 가입·탈퇴 화면"""

import discord

from .status import delete_user_data, get_user_data, mission_cooldowns, update_user_stats
from .alignment import (
    CITIZEN_JOIN_GATING,
    alignment_text,
    evaluate_join,
    get_title_blob,
    join_requirement_text,
    save_title_blob,
)
from .civilian import donate_cooldowns, parttime_cooldowns, pickpocket_cooldowns, service_cooldowns
from .corrupt_police import corrupt_arrest_cooldowns, corrupt_extort_cooldowns
from .gamble import active_blackjack, gamble_cooldowns
from . import admin as _admin

# === 1. 셀프 가입 버튼 뷰 ===
class SelfJoinView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="마피아 조직 입단", style=discord.ButtonStyle.danger, custom_id="join_mafia_btn")
    async def join_mafia(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.process_join(interaction, target_role_name="조직원", rank_title="마피아 조직원")

    @discord.ui.button(label="치안국 경찰 임용", style=discord.ButtonStyle.primary, custom_id="join_police_btn")
    async def join_police(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.process_join(interaction, target_role_name="경찰", rank_title="순경")

    async def process_join(self, interaction: discord.Interaction, target_role_name: str, rank_title: str):
        if _admin.MAINTENANCE_MODE and not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("현재 점검 중입니다.", ephemeral=True)
            return

        user = interaction.user
        citizen_role = discord.utils.get(interaction.guild.roles, name="시민")
        target_role = discord.utils.get(interaction.guild.roles, name=target_role_name)
        
        if not target_role:
            await interaction.response.send_message(f"서버에 '{target_role_name}' 역할이 없습니다! 관리자에게 문의하세요.", ephemeral=True)
            return

        mafia_role = discord.utils.get(interaction.guild.roles, name="조직원")
        police_role = discord.utils.get(interaction.guild.roles, name="경찰")

        if (mafia_role and mafia_role in user.roles) or (police_role and police_role in user.roles):
            await interaction.response.send_message("당신은 이미 소속(마피아 또는 경찰)이 결정되었습니다!", ephemeral=True)
            return

        # [전직 조건] 시민 시절의 성향·활동 기록으로 가입 가능 여부를 판단
        corrupt, fee = False, 0
        join_info = get_user_data(user.id)
        join_blob = get_title_blob(join_info)
        if CITIZEN_JOIN_GATING:
            check = evaluate_join(target_role_name, join_info, join_blob)
            if not check["ok"]:
                lines = [f"{'[충족]' if ok else '[미달]'} {name}  ({now_text})" for name, ok, now_text in check["checks"]]
                head = "부패 경찰 임용 조건" if check["corrupt"] else f"{target_role_name} 가입 조건"
                embed = discord.Embed(
                    title=f"[{head}을 아직 채우지 못했습니다]",
                    description=f"현재 성향: **{alignment_text(join_blob['karma'])}**\n\n" + "\n".join(lines)
                                + "\n\n" + join_requirement_text(),
                    color=discord.Color.dark_red())
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return
            corrupt, fee = check["corrupt"], check["fee"]

        try:   # 비용·부패 표시는 역할 변경 전에 저장 (역할 변경이 실패하면 되돌림)
            if fee:
                update_user_stats(user.id, -fee, 0)
            join_blob["corrupt"] = corrupt
            save_title_blob(user.id, join_blob)
        except Exception as e:
            print(f"DB Error (join): {e}")
            await interaction.response.send_message("가입 처리 중 오류가 발생했습니다. 잠시 뒤 다시 시도해 주세요.", ephemeral=True)
            return

        try:
            if citizen_role and citizen_role in user.roles:
                await user.remove_roles(citizen_role)
            await user.add_roles(target_role)
        except discord.Forbidden:
            try:
                if fee:
                    update_user_stats(user.id, fee, 0)
                join_blob["corrupt"] = False
                save_title_blob(user.id, join_blob)
            except Exception as e:
                print(f"Join rollback error: {e}")
            await interaction.response.send_message("봇의 역할 권한 순서를 올려주세요.", ephemeral=True)
            return

        user_stats = get_user_data(user.id)

        embed = discord.Embed(
            title=f"[{rank_title} 발령/입단]",
            description=f"**{user.mention}** 님이 시민의 삶을 마치고 **{target_role_name}**(으)로 새로운 활동을 시작합니다!\n\n"
                        f"**신분 변화:** `시민` `{target_role_name}`",
            color=discord.Color.gold()
        )
        if user.avatar:
            embed.set_thumbnail(url=user.avatar.url)

        await interaction.response.send_message(embed=embed)
        if corrupt:   # 부패 경찰 임용은 본인에게만 안내 (프로필에는 '부패 경찰'로 표시됨)
            await interaction.followup.send(
                f"로비 자금 `${fee:,}` 을(를) 써서 **부패 경찰**로 임용되었습니다.\n"
                f"• `/검거` 로 수배 중이 아닌 **시민**도 체포하고, `/갈취` 로 시민의 돈을 뜯어낼 수 있습니다.\n"
                f"• 들키면(민원 접수) 수배되어 정식 경찰에게 체포당합니다. 프로필(`/프로필`)에는 **부패 경찰**로 표시됩니다.",
                ephemeral=True)

# === 2. 탈퇴 확인/취소 버튼 뷰 ===
class LeaveConfirmView(discord.ui.View):
    def __init__(self, target_user: discord.Member):
        super().__init__(timeout=60)
        self.target_user = target_user

    @discord.ui.button(label="네, 탈퇴하겠습니다", style=discord.ButtonStyle.danger, custom_id="confirm_leave")
    async def confirm_leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        from .police_unit import police_unit_detach_user   # 순환 import 방지: 호출 시점에 가져옴
        from .family import family_detach_user   # 순환 import 방지: 호출 시점에 가져옴
        if interaction.user != self.target_user:
            await interaction.response.send_message("본인의 탈퇴 확인 창만 조작할 수 있습니다.", ephemeral=True)
            return

        await interaction.response.defer()

        member = interaction.user
        mafia_role = discord.utils.get(interaction.guild.roles, name="조직원")
        police_role = discord.utils.get(interaction.guild.roles, name="경찰")
        citizen_role = discord.utils.get(interaction.guild.roles, name="시민")

        try:
            if mafia_role and mafia_role in member.roles:
                await member.remove_roles(mafia_role)
            if police_role and police_role in member.roles:
                await member.remove_roles(police_role)
                
            if citizen_role and citizen_role not in member.roles:
                await member.add_roles(citizen_role)
        except discord.Forbidden:
            await interaction.followup.send("역할 변경 권한이 없습니다.", ephemeral=True)
            return

        try:
            family_detach_user(member.id)   # 조직에서도 제거
        except Exception as e:
            print(f"Family detach error: {e}")

        try:
            police_unit_detach_user(member.id)   # 경찰 수사팀에서도 제거
        except Exception as e:
            print(f"Police unit detach error: {e}")

        try:
            delete_user_data(member.id)
        except Exception:
            pass

        mission_cooldowns.pop(member.id, None)
        parttime_cooldowns.pop(member.id, None)
        for _t in (service_cooldowns, donate_cooldowns, pickpocket_cooldowns, corrupt_arrest_cooldowns, corrupt_extort_cooldowns):
            _t.pop(member.id, None)
        gamble_cooldowns.pop(member.id, None)
        open_table = active_blackjack.pop(member.id, None)
        if open_table:
            open_table.finished = True
            open_table.stop()

        for child in self.children:
            child.disabled = True

        embed = discord.Embed(
            title="[소속 해임 및 세이브 데이터 초기화]",
            description=(
                f"**{member.mention}** 님이 소속을 최종 탈퇴하셨습니다.\n\n"
                f"**초기화 완료:** 모든 보유 자금, 명예, 인벤토리 아이템 및 기록이 DB에서 완전히 삭제되었습니다.\n"
                f"**신분 변화:** `마피아/경찰` `시민`"
            ),
            color=discord.Color.dark_gray()
        )
        if member.avatar:
            embed.set_thumbnail(url=member.avatar.url)

        await interaction.edit_original_response(embed=embed, view=self)

    @discord.ui.button(label="취소 (소속 잔류)", style=discord.ButtonStyle.secondary, custom_id="cancel_leave")
    async def cancel_leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.target_user:
            await interaction.response.send_message("본인의 탈퇴 확인 창만 조작할 수 있습니다.", ephemeral=True)
            return

        for child in self.children:
            child.disabled = True

        embed = discord.Embed(
            title="[탈퇴 철회]",
            description="탈퇴 신청이 취소되었습니다.",
            color=discord.Color.blue()
        )
        await interaction.edit_original_response(embed=embed, view=self)
