import discord
from discord.ext import commands
from discord import app_commands
import random
import json
import os
from datetime import datetime, timedelta

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix='!', intents=intents)

# ==========================================
# 🚨 [점검 모드 설정 스위치]
# ==========================================
MAINTENANCE_MODE = False 
# ==========================================

# 쿨타임 관리를 위한 사전 (유저ID: 다음 수행 가능 시간)
mission_cooldowns = {}

# 데이터 파일 경로
DATA_FILE = "user_data.json"

# === 📁 유저 데이터 불러오기 및 저장 함수 ===
def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

def get_user_data(user_id: int) -> dict:
    data = load_data()
    str_id = str(user_id)
    if str_id not in data:
        data[str_id] = {"money": 1000, "honor": 0}  # 초기 자금 $1,000, 명예 0pt
        save_data(data)
    # 기존 데이터 구조 호환성 유지 (구버전 유저 데이터 보완)
    if isinstance(data[str_id], int):
        data[str_id] = {"money": data[str_id], "honor": 0}
        save_data(data)
    if "honor" not in data[str_id]:
        data[str_id]["honor"] = 0
        save_data(data)
    return data[str_id]

def update_user_stats(user_id: int, money_add: int, honor_add: int):
    data = load_data()
    str_id = str(user_id)
    user_info = get_user_data(user_id)
    
    user_info["money"] += money_add
    user_info["honor"] += honor_add
    data[str_id] = user_info
    save_data(data)

@bot.event
async def on_ready():
    try:
        synced = await bot.tree.sync()
        print(f"총 {len(synced)}개의 슬래시 명령어가 동기화되었습니다.")
    except Exception as e:
        print(e)
    print(f'로그인 완료: {bot.user.name}')

# === [공통 체크 함수] 점검 모드 확인 ===
async def check_maintenance(interaction: discord.Interaction) -> bool:
    if MAINTENANCE_MODE:
        if not interaction.user.guild_permissions.administrator:
            embed = discord.Embed(
                title="🛠️ [시스템 점검 및 업데이트 안내]",
                description="현재 패밀리 GM 봇을 업데이트 중입니다. 잠시만 기다려 주세요!",
                color=discord.Color.orange()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return False
    return True

# === 1. 셀프 가입 버튼 뷰 ===
class SelfJoinView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🔫 패밀리 가입하기", style=discord.ButtonStyle.green, custom_id="self_join_btn")
    async def join_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if MAINTENANCE_MODE and not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("🛠️ 현재 점검 중이어서 가입 버튼을 누를 수 없습니다.", ephemeral=True)
            return

        user = interaction.user
        role = discord.utils.get(interaction.guild.roles, name="조직원")
        
        if not role:
            await interaction.response.send_message("❌ 서버에 '조직원' 역할이 없습니다! 관리자에게 문의하세요.", ephemeral=True)
            return

        if role in user.roles:
            await interaction.response.send_message("⚠️ 당신은 이미 패밀리의 정식 조직원입니다!", ephemeral=True)
            return

        await user.add_roles(role)
        
        user_stats = get_user_data(user.id)

        embed = discord.Embed(
            title="🕶️ [패밀리 입단 환영]",
            description=f"환영한다, 신입.\n**{user.mention}** 님이 패밀리의 새로운 조직원으로 합류했습니다.",
            color=discord.Color.dark_red()
        )
        if user.avatar:
            embed.set_thumbnail(url=user.avatar.url)
        embed.add_field(name="🗡️ 초기 직급", value="신입 행동대원", inline=True)
        embed.add_field(name="💰 초기 자금", value=f"${user_stats['money']:,}", inline=True)
        embed.add_field(name="🏆 조직 명예", value=f"{user_stats['honor']} pt", inline=True)
        
        await interaction.response.send_message(embed=embed, ephemeral=False)

# === 2. 탈퇴 확인/취소 버튼 뷰 ===
class LeaveConfirmView(discord.ui.View):
    def __init__(self, target_user: discord.Member):
        super().__init__(timeout=60)
        self.target_user = target_user

    @discord.ui.button(label="네, 탈퇴하겠습니다", style=discord.ButtonStyle.danger, custom_id="confirm_leave")
    async def confirm_leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.target_user:
            await interaction.response.send_message("❌ 본인의 탈퇴 확인 창만 조작할 수 있습니다.", ephemeral=True)
            return

        role = discord.utils.get(interaction.guild.roles, name="조직원")
        if role and role in self.target_user.roles:
            await self.target_user.remove_roles(role)

        for child in self.children:
            child.disabled = True

        embed = discord.Embed(
            title="🚪 [패밀리 조직원 해임]",
            description=f"**{self.target_user.mention}** 님이 패밀리를 최종 탈퇴하셨습니다.",
            color=discord.Color.dark_gray()
        )
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="취소 (패밀리에 잔류)", style=discord.ButtonStyle.secondary, custom_id="cancel_leave")
    async def cancel_leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.target_user:
            await interaction.response.send_message("❌ 본인의 탈퇴 확인 창만 조작할 수 있습니다.", ephemeral=True)
            return

        for child in self.children:
            child.disabled = True

        embed = discord.Embed(
            title="🛡️ [탈퇴 철회]",
            description="패밀리 탈퇴 신청이 취소되었습니다.",
            color=discord.Color.blue()
        )
        await interaction.response.edit_message(embed=embed, view=self)

# === 3. 확률을 숨긴 드롭다운 메뉴 (Select Menu) ===
class MissionSelect(discord.ui.Select):
    def __init__(self, owner: discord.Member):
        self.owner = owner
        options = [
            discord.SelectOption(
                label="타 조직 구역 비밀 조사", 
                description="난이도: 하 | 보상: $300 ~ $700 / 명예 +10pt", 
                emoji="🕶️", 
                value="mission_1"
            ),
            discord.SelectOption(
                label="밀수품 비밀 운송 작업", 
                description="난이도: 중 | 보상: $600 ~ $1,500 / 명예 +25pt", 
                emoji="💼", 
                value="mission_2"
            ),
            discord.SelectOption(
                label="라이벌 보스의 장부 탈취", 
                description="난이도: 상 | 보상: $1,500 ~ $3,500 / 명예 +50pt", 
                emoji="🏛️", 
                value="mission_3"
            ),
        ]
        super().__init__(placeholder="🎯 수행할 비밀 임무를 선택하세요...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        if interaction.user != self.owner:
            await interaction.response.send_message("❌ 타인의 임무 선택 창은 조작할 수 없습니다.", ephemeral=True)
            return

        user = interaction.user

        now = datetime.now()
        if user.id in mission_cooldowns:
            next_available = mission_cooldowns[user.id]
            if now < next_available:
                remaining = int((next_available - now).total_seconds())
                mins, secs = divmod(remaining, 60)
                await interaction.response.send_message(f"⏳ 체력을 회복 중입니다. **{mins}분 {secs}초** 뒤에 다시 임무를 수행할 수 있습니다.", ephemeral=True)
                return

        mission_cooldowns[user.id] = now + timedelta(minutes=10)

        # 확률 정보는 코드 내부에서만 유지하고 디스플레이에는 보이지 않음
        mission_data = {
            "mission_1": {"title": "🕶️ 타 조직 구역 비밀 조사", "success_rate": 80, "reward": (300, 700), "honor": 10},
            "mission_2": {"title": "💼 밀수품 비밀 운송 작업", "success_rate": 65, "reward": (600, 1500), "honor": 25},
            "mission_3": {"title": "🏛️ 라이벌 보스의 장부 탈취", "success_rate": 50, "reward": (1500, 3500), "honor": 50},
        }

        selected = mission_data[self.values[0]]
        is_success = random.randint(1, 100) <= selected["success_rate"]

        if is_success:
            earned_money = random.randint(*selected["reward"])
            earned_honor = selected["honor"]
            
            # 자금 및 명예 포인트 동시 추가
            update_user_stats(user.id, earned_money, earned_honor)
            current_stats = get_user_data(user.id)

            embed = discord.Embed(
                title="🎯 [임무 성공]",
                description=f"**{user.mention}** 님이 선택하신 작전을 완벽하게 완수했습니다!\n\n"
                            f"📌 **작전명:** {selected['title']}\n"
                            f"💰 **획득 보상금:** `${earned_money:,}`\n"
                            f"🏆 **획득 명예:** `+{earned_honor} pt`\n\n"
                            f"🏦 **보유 자금:** `${current_stats['money']:,}`\n"
                            f"🏅 **총 명예:** `{current_stats['honor']} pt`",
                color=discord.Color.green()
            )
        else:
            current_stats = get_user_data(user.id)
            embed = discord.Embed(
                title="💥 [임무 실패]",
                description=f"**{user.mention}** 님이 선택하신 작전 도중 경비대에 적발되었습니다!\n\n"
                            f"📌 **작전명:** {selected['title']}\n"
                            f"⚠️ 보상과 명예를 얻지 못했습니다.\n\n"
                            f"🏦 **보유 자금:** `${current_stats['money']:,}`\n"
                            f"🏅 **총 명예:** `{current_stats['honor']} pt`",
                color=discord.Color.red()
            )

        if user.avatar:
            embed.set_thumbnail(url=user.avatar.url)

        await interaction.response.edit_message(embed=embed, view=None)

class MissionView(discord.ui.View):
    def __init__(self, owner: discord.Member):
        super().__init__(timeout=60)
        self.add_item(MissionSelect(owner=owner))

# === 4. `/가입` 명령어 ===
@bot.tree.command(name="가입", description="[마피아 GM] 패밀리 가입 버튼을 소환합니다.")
async def join_family(interaction: discord.Interaction):
    if not await check_maintenance(interaction): return
    
    embed = discord.Embed(
        title="📜 [패밀리 입단 안내]",
        description="아래 버튼을 눌러 패밀리에 합류하세요.",
        color=discord.Color.gold()
    )
    view = SelfJoinView()
    await interaction.response.send_message(embed=embed, view=view)

# === 5. `/탈퇴` 명령어 ===
@bot.tree.command(name="탈퇴", description="[마피아 GM] 패밀리 탈퇴 절차를 진행합니다.")
async def leave_family(interaction: discord.Interaction):
    if not await check_maintenance(interaction): return

    user = interaction.user
    role = discord.utils.get(interaction.guild.roles, name="조직원")

    if not role or role not in user.roles:
        await interaction.response.send_message("⚠️ 당신은 패밀리 조직원이 아닙니다.", ephemeral=True)
        return

    embed = discord.Embed(
        title="⚠️ [패밀리 탈퇴 경고]",
        description=f"**{user.mention}**님, 정말로 패밀리에서 탈퇴하시겠습니까?",
        color=discord.Color.red()
    )
    view = LeaveConfirmView(target_user=user)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

# === 6. `/임무` 명령어 ===
@bot.tree.command(name="임무", description="[조직원 전용] 원하는 비밀 임무를 직접 선택하여 수행합니다.")
async def execute_mission(interaction: discord.Interaction):
    if not await check_maintenance(interaction): return

    user = interaction.user
    role = discord.utils.get(interaction.guild.roles, name="조직원")

    if not role or role not in user.roles:
        await interaction.response.send_message("❌ 패밀리의 정식 조직원만 임무를 하달받을 수 있습니다! `/가입`을 먼저 해주세요.", ephemeral=True)
        return

    embed = discord.Embed(
        title="📂 [비밀 임무 작전 하달]",
        description=f"**{user.mention}** 님, 아래 메뉴에서 이번에 수행할 작전을 직접 선택해 주십시오.\n\n"
                    "⚠️ **주의:** 난이도가 높을수록 위험하지만 큰 보상과 높은 명예를 얻습니다.",
        color=discord.Color.dark_purple()
    )
    if user.avatar:
        embed.set_thumbnail(url=user.avatar.url)

    view = MissionView(owner=user)
    await interaction.response.send_message(embed=embed, view=view)

# === 7. `/프로필` 명령어 (명예 시스템 적용) ===
@bot.tree.command(name="프로필", description="[마피아 GM] 패밀리 조직원 신원 조회 서류를 확인합니다.")
async def profile(interaction: discord.Interaction):
    if not await check_maintenance(interaction): return

    user = interaction.user
    user_stats = get_user_data(user.id)

    embed = discord.Embed(
        title="🕵️‍♂️ [패밀리 비밀 문서] 조직원 신원 조회",
        description=f"**{user.name}** 패밀리원에 대한 보안 기록입니다.",
        color=discord.Color.dark_red()
    )
    if user.avatar:
        embed.set_thumbnail(url=user.avatar.url)

    embed.add_field(name="📜 조직원명", value=user.mention, inline=True)
    embed.add_field(name="🗡️ 직급", value="신입 행동대원", inline=True)
    embed.add_field(name="💰 보유 자금", value=f"${user_stats['money']:,}", inline=True)
    embed.add_field(name="🏆 조직 명예", value=f"{user_stats['honor']} pt", inline=True)
    embed.set_footer(text=f"문서 요청자: {user.name}")

    await interaction.response.send_message(embed=embed)

# === 8. `/추방` 명령어 ===
@bot.tree.command(name="추방", description="[간부 전용] 규칙을 어긴 조직원을 패밀리에서 강제로 추방합니다.")
@app_commands.describe(member="추방할 조직원", reason="추방 사유 (선택 사항)")
async def kick_member(interaction: discord.Interaction, member: discord.Member, reason: str = "규율 위반 및 배신"):
    if not await check_maintenance(interaction): return

    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ 패밀리 간부(관리자) 권한을 가진 자만 추방 명령을 내릴 수 있습니다!", ephemeral=True)
        return

    role = discord.utils.get(interaction.guild.roles, name="조직원")
    if not role or role not in member.roles:
        await interaction.response.send_message(f"⚠️ {member.mention} 님은 패밀리 조직원이 아닙니다.", ephemeral=True)
        return

    await member.remove_roles(role)

    embed = discord.Embed(
        title="⚖️ [패밀리 숙청: 조직원 추방]",
        description=f"보스의 엄중한 명령으로 **{member.mention}** 님이 패밀리에서 영구 추방되었습니다.",
        color=discord.Color.dark_embed()
    )
    embed.add_field(name="👤 대상자", value=member.name, inline=True)
    embed.add_field(name="🔨 집행자", value=interaction.user.mention, inline=True)
    embed.add_field(name="📜 숙청 사유", value=reason, inline=False)
    
    if member.avatar:
        embed.set_thumbnail(url=member.avatar.url)

    await interaction.response.send_message(embed=embed)

# === 9. `/도움말` 명령어 ===
@bot.tree.command(name="도움말", description="[마피아 GM] 봇의 명령어 가이드를 확인합니다.")
async def help_command(interaction: discord.Interaction):
    if not await check_maintenance(interaction): return

    embed = discord.Embed(
        title="📜 [패밀리 GM] 시스템 가이드",
        description="마피아 조직 세계관에 오신 것을 환영합니다.",
        color=discord.Color.dark_red()
    )
    embed.add_field(name="🔫 `/가입`", value="패밀리 입단 신청 버튼을 불러옵니다.", inline=False)
    embed.add_field(name="🚪 `/탈퇴`", value="패밀리 탈퇴 경고 확인 후 역할을 해임합니다.", inline=False)
    embed.add_field(name="🗡️ `/임무`", value="원하는 비밀 작전을 직접 선택하여 수행합니다. (10분 쿨타임)", inline=False)
    embed.add_field(name="🕵️‍♂️ `/프로필`", value="나의 조직원 신원 서류, 보유 자금 및 명예를 확인합니다.", inline=False)
    embed.add_field(name="⚖️ `/추방 [유저]`", value="[간부 전용] 대상 조직원을 강제 추방합니다.", inline=False)
    embed.add_field(name="❓ `/도움말`", value="봇의 명령어 가이드를 확인합니다.", inline=False)
    
    embed.set_footer(text="이 메시지는 명령어를 입력한 본인에게만 표시됩니다.")

    await interaction.response.send_message(embed=embed, ephemeral=True)

# 봇 토큰 입력
bot.run(os.getenv('DISCORD_TOKEN'))
