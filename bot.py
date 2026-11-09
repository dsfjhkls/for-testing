from flask import Flask
from threading import Thread

# 렌더 포트 검사를 통과하기 위한 가짜 웹 서버
app = Flask('')

@app.route('/')
def home():
    return "Mafia Bot is running!"

def run():
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = Thread(target=run)
    t.start()

# 봇이 실행되기 전에 웹 서버를 먼저 켜줌
keep_alive()

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

# === 🗺️ 4대 지역 정보 데이터 ===
LOCATIONS = {
    "레벤하임": {
        "desc": "수도 / 시작 지점. 칼렌데의 중심 도시로 정부 기관과 중앙치안국 본부가 밀집한 도시.",
        "risk": "보통",
        "emoji": "🏛️"
    },
    "던베르크": {
        "desc": "공업지대. 대규모 공장과 노동자 구역이 밀집한 산업 도시.",
        "risk": "중간",
        "emoji": "⚙️"
    },
    "블랙워드": {
        "desc": "빈민가. 열악한 환경과 높은 범죄율, 음지의 범죄조직이 도사리는 구역.",
        "risk": "높음",
        "emoji": "🏚️"
    },
    "에르느코항": {
        "desc": "항구도시 / 해상 무역 중심지. 합법과 불법 밀거래가 공존하는 개방형 항구.",
        "risk": "변동적",
        "emoji": "⚓"
    }
}

# === 🛒 지역별 암시장 아이템 목록 ===
BLACK_MARKET_ITEMS = {
    "레벤하임": [
        {"id": "gov_pass", "name": "치안국 위조 신분증", "price": 1200, "desc": "치안국의 감시를 피하는 위조 신분증", "emoji": "🪪"},
        {"id": "vip_cigar", "name": "고급 시가 패키지", "price": 500, "desc": "고위층 커넥션용 기품 있는 시가", "emoji": "🚬"}
    ],
    "던베르크": [
        {"id": "heavy_glove", "name": "강화 작업용 장갑", "price": 800, "desc": "공업지대의 묵직한 작업용 장갑", "emoji": "🥊"},
        {"id": "boost_drink", "name": "고농축 자양강장제", "price": 400, "desc": "체력을 단숨에 회복시키는 특수 음료", "emoji": "🧪"}
    ],
    "블랙워드": [
        {"id": "lockpick", "name": "녹슨 락픽 세트", "price": 300, "desc": "뒷골목 도둑들이 즐겨 쓰는 해킹 툴", "emoji": "🔑"},
        {"id": "black_hood", "name": "익명의 덮개", "price": 600, "desc": "신원을 완벽히 은폐해 주는 검은 후드", "emoji": "🥷"}
    ],
    "에르느코항": [
        {"id": "smuggled_rum", "name": "밀수된 고급 양주", "price": 1500, "desc": "해상 선적을 통해 몰래 들어온 양주", "emoji": "🍾"},
        {"id": "stealth_sonar", "name": "해상 무전 도청기", "price": 2000, "desc": "해상 정보와 주파수를 감청하는 장치", "emoji": "📻"}
    ]
}

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
        data[str_id] = {"money": 1000, "honor": 0, "location": "레벤하임", "inventory": {}}
        save_data(data)
    
    # 기존 데이터 구조 호환성 및 미흡한 필드 자동 보완
    if isinstance(data[str_id], int):
        data[str_id] = {"money": data[str_id], "honor": 0, "location": "레벤하임", "inventory": {}}
        save_data(data)
    if not isinstance(data[str_id], dict):
        data[str_id] = {"money": 1000, "honor": 0, "location": "레벤하임", "inventory": {}}
        save_data(data)
    if "honor" not in data[str_id]:
        data[str_id]["honor"] = 0
        save_data(data)
    if "location" not in data[str_id]:
        data[str_id]["location"] = "레벤하임"
        save_data(data)
    if "inventory" not in data[str_id]:
        data[str_id]["inventory"] = {}
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

def update_user_location(user_id: int, new_location: str):
    data = load_data()
    str_id = str(user_id)
    user_info = get_user_data(user_id)
    
    user_info["location"] = new_location
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
        embed.add_field(name="📍 시작 거점", value=user_stats['location'], inline=False)
        
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

        mission_data = {
            "mission_1": {"title": "🕶️ 타 조직 구역 비밀 조사", "success_rate": 80, "reward": (300, 700), "honor": 10},
            "mission_2": {"title": "💼 밀수품 비밀 운송 작업", "success_rate": 65, "reward": (600, 1500), "honor": 25},
            "mission_3": {"title": "🏛️ 라이벌 보스의 장부 탈취", "success_rate": 50, "reward": (1500, 3500), "honor": 50},
        }

        selected = mission_data[self.values[0]]
        is_success = random.randint(1, 100) <= selected["success_rate"]
        
        user_stats = get_user_data(user.id)
        current_loc = user_stats.get("location", "레벤하임")

        if is_success:
            earned_money = random.randint(*selected["reward"])
            earned_honor = selected["honor"]
            
            update_user_stats(user.id, earned_money, earned_honor)
            current_stats = get_user_data(user.id)

            embed = discord.Embed(
                title=f"🎯 [{current_loc}] 임무 성공",
                description=f"**{user.mention}** 님이 선택하신 작전을 완벽하게 완수했습니다!\n\n"
                            f"📌 **작전명:** {selected['title']}\n"
                            f"📍 **수행 구역:** {current_loc}\n"
                            f"💰 **획득 보상금:** `${earned_money:,}`\n"
                            f"🏆 **획득 명예:** `+{earned_honor} pt`\n\n"
                            f"🏦 **보유 자금:** `${current_stats['money']:,}`\n"
                            f"🏅 **총 명예:** `{current_stats['honor']} pt`",
                color=discord.Color.green()
            )
        else:
            current_stats = get_user_data(user.id)
            embed = discord.Embed(
                title=f"💥 [{current_loc}] 임무 실패",
                description=f"**{user.mention}** 님이 선택하신 작전 도중 경비대에 적발되었습니다!\n\n"
                            f"📌 **작전명:** {selected['title']}\n"
                            f"📍 **수행 구역:** {current_loc}\n"
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

# === 4. 암시장 드롭다운 및 뷰 ===
class MarketSelect(discord.ui.Select):
    def __init__(self, owner: discord.Member, location: str):
        self.owner = owner
        self.location = location
        items = BLACK_MARKET_ITEMS.get(location, [])
        
        options = []
        for item in items:
            options.append(discord.SelectOption(
                label=f"{item['name']} (${item['price']:,})",
                description=item['desc'],
                emoji=item['emoji'],
                value=item['id']
            ))
            
        super().__init__(placeholder=f"🛒 {location} 암시장 상품을 선택하세요...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        if interaction.user != self.owner:
            await interaction.response.send_message("❌ 타인의 거래 창은 조작할 수 없습니다.", ephemeral=True)
            return

        selected_id = self.values[0]
        items = BLACK_MARKET_ITEMS.get(self.location, [])
        target_item = next((item for item in items if item['id'] == selected_id), None)

        if not target_item:
            await interaction.response.send_message("❌ 존재하지 않는 아이템입니다.", ephemeral=True)
            return

        user_id = str(interaction.user.id)
        data = load_data()
        user_info = get_user_data(interaction.user.id)

        if user_info["money"] < target_item["price"]:
            await interaction.response.send_message(
                f"❌ 자금이 부족합니다! (필요 자금: ${target_item['price']:,} / 보유 자금: ${user_info['money']:,})", 
                ephemeral=True
            )
            return

        user_info["money"] -= target_item["price"]
        
        if "inventory" not in user_info:
            user_info["inventory"] = {}

        item_name = f"{target_item['emoji']} {target_item['name']}"
        user_info["inventory"][item_name] = user_info["inventory"].get(item_name, 0) + 1
        
        data[user_id] = user_info
        save_data(data)

        embed = discord.Embed(
            title="💰 [암시장 거래 성사]",
            description=f"**{interaction.user.mention}** 님이 **{item_name}**을(를) 구매하셨습니다!\n\n"
                        f"💵 **결제 금액:** `${target_item['price']:,}`\n"
                        f"🏦 **남은 자금:** `${user_info['money']:,}`\n"
                        f"🎒 **보유 수량:** `{user_info['inventory'][item_name]}개`",
            color=discord.Color.gold()
        )
        if interaction.user.avatar:
            embed.set_thumbnail(url=interaction.user.avatar.url)

        await interaction.response.edit_message(embed=embed, view=None)

class MarketView(discord.ui.View):
    def __init__(self, owner: discord.Member, location: str):
        super().__init__(timeout=60)
        self.add_item(MarketSelect(owner=owner, location=location))

# === 5. `/가입` 명령어 ===
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

# === 6. `/탈퇴` 명령어 ===
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

# === 7. `/임무` 명령어 ===
@bot.tree.command(name="임무", description="[조직원 전용] 원하는 비밀 임무를 직접 선택하여 수행합니다.")
async def execute_mission(interaction: discord.Interaction):
    if not await check_maintenance(interaction): return

    user = interaction.user
    role = discord.utils.get(interaction.guild.roles, name="조직원")

    if not role or role not in user.roles:
        await interaction.response.send_message("❌ 패밀리의 정식 조직원만 임무를 하달받을 수 있습니다! `/가입`을 먼저 해주세요.", ephemeral=True)
        return

    user_stats = get_user_data(user.id)
    current_loc = user_stats.get("location", "레벤하임")

    embed = discord.Embed(
        title=f"📂 [{current_loc}] 비밀 임무 작전 하달",
        description=f"**{user.mention}** 님, 현재 거점인 **{current_loc}**에서 수행할 작전을 직접 선택해 주십시오.\n\n"
                    "⚠️ **주의:** 난이도가 높을수록 위험하지만 큰 보상과 높은 명예를 얻습니다.",
        color=discord.Color.dark_purple()
    )
    if user.avatar:
        embed.set_thumbnail(url=user.avatar.url)

    view = MissionView(owner=user)
    await interaction.response.send_message(embed=embed, view=view)

# === 8. `/프로필` 명령어 (인벤토리 표기 연동) ===
@bot.tree.command(name="프로필", description="[마피아 GM] 패밀리 조직원 신원 조회 서류 및 인벤토리를 확인합니다.")
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
    embed.add_field(name="📍 현재 거점", value=user_stats.get('location', '레벤하임'), inline=False)
    
    # 인벤토리 항목 가공
    inventory = user_stats.get("inventory", {})
    if inventory:
        inv_text = "\n".join([f"• **{item}**: `{count}개`" for item, count in inventory.items()])
    else:
        inv_text = "*소지한 아이템이 없습니다.*"
        
    embed.add_field(name="🎒 소지품 (인벤토리)", value=inv_text, inline=False)
    embed.set_footer(text=f"문서 요청자: {user.name}")

    await interaction.response.send_message(embed=embed)

# === 9. `/이동` 명령어 ===
@bot.tree.command(name="이동", description="[마피아 GM] 칼렌데 내의 다른 지역으로 이동합니다.")
@app_commands.choices(지역=[
    app_commands.Choice(name="🏛️ 레벤하임 (수도 / 시작 지점)", value="레벤하임"),
    app_commands.Choice(name="⚙️ 던베르크 (공업지대)", value="던베르크"),
    app_commands.Choice(name="🏚️ 블랙워드 (빈민가)", value="블랙워드"),
    app_commands.Choice(name="⚓ 에르느코항 (항구도시)", value="에르느코항")
])
async def move_location(interaction: discord.Interaction, 지역: app_commands.Choice[str]):
    if not await check_maintenance(interaction): return

    user = interaction.user
    role = discord.utils.get(interaction.guild.roles, name="조직원")

    if not role or role not in user.roles:
        await interaction.response.send_message("❌ 패밀리의 정식 조직원만 이 지역을 자유롭게 이동할 수 있습니다. `/가입`을 먼저 해주세요.", ephemeral=True)
        return

    selected_loc = 지역.value
    loc_info = LOCATIONS[selected_loc]

    update_user_location(user.id, selected_loc)

    embed = discord.Embed(
        title=f"{loc_info['emoji']} [{selected_loc}] (으)로 거점 이동 완료",
        description=f"**{user.mention}** 님이 성공적으로 **{selected_loc}**(으)로 도착했습니다.",
        color=discord.Color.dark_blue()
    )
    embed.add_field(name="📜 지역 특징", value=loc_info["desc"], inline=False)
    embed.add_field(name="🚨 치안 위험도", value=loc_info["risk"], inline=True)
    embed.set_footer(text=f"요청자: {user.name}")

    if user.avatar:
        embed.set_thumbnail(url=user.avatar.url)

    await interaction.response.send_message(embed=embed)

# === 10. `/암시장` 명령어 ===
@bot.tree.command(name="암시장", description="[조직원 전용] 현재 위치한 지역의 밀거래 암시장에 입장합니다.")
async def open_market(interaction: discord.Interaction):
    if not await check_maintenance(interaction): return

    user = interaction.user
    role = discord.utils.get(interaction.guild.roles, name="조직원")

    if not role or role not in user.roles:
        await interaction.response.send_message("❌ 패밀리 조직원만 암시장에 접근할 수 있습니다. `/가입`을 먼저 해주세요.", ephemeral=True)
        return

    user_stats = get_user_data(user.id)
    current_loc = user_stats.get("location", "레벤하임")
    loc_emoji = LOCATIONS.get(current_loc, {}).get("emoji", "📍")

    embed = discord.Embed(
        title=f"🕵️‍♂️ {loc_emoji} [{current_loc} 비밀 암시장]",
        description=f"**{user.name}** 님, 은밀한 음지 거래소에 오신 것을 환영합니다.\n"
                    f"현재 보유 자금: **${user_stats['money']:,}**\n\n"
                    f"아래 메뉴에서 물품을 선택하여 구매할 수 있습니다.",
        color=discord.Color.gold()
    )
    if user.avatar:
        embed.set_thumbnail(url=user.avatar.url)

    view = MarketView(owner=user, location=current_loc)
    await interaction.response.send_message(embed=embed, view=view)

# === 11. `/추방` 명령어 ===
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

# === 12. `/도움말` 명령어 ===
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
    embed.add_field(name="📍 `/이동`", value="칼렌데 내부 4개 핵심 거점으로 이동합니다.", inline=False)
    embed.add_field(name="🕵️‍♂️ `/암시장`", value="현재 머무는 거점의 비밀 암시장에서 특수 밀수품을 거래합니다.", inline=False)
    embed.add_field(name="🗡️ `/임무`", value="원하는 비밀 작전을 직접 선택하여 수행합니다. (10분 쿨타임)", inline=False)
    embed.add_field(name="🕵️‍♂️ `/프로필`", value="나의 조직원 신원 서류, 자금, 명예, 위치 및 소지품을 확인합니다.", inline=False)
    embed.add_field(name="⚖️ `/추방 [유저]`", value="[간부 전용] 대상 조직원을 강제 추방합니다.", inline=False)
    embed.add_field(name="❓ `/도움말`", value="봇의 명령어 가이드를 확인합니다.", inline=False)
    
    embed.set_footer(text="이 메시지는 명령어를 입력한 본인에게만 표시됩니다.")

    await interaction.response.send_message(embed=embed, ephemeral=True)

# 봇 토큰 입력
import os
bot.run(os.getenv("DISCORD_TOKEN"))