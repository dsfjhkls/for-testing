from flask import Flask
from threading import Thread
import os
import random
from datetime import datetime, timedelta

# 렌더 포트 검사를 통과하기 위한 가짜 웹 서버
app = Flask('')

@app.route('/')
def home():
    return "Mafia Bot is running with Supabase DB!"

def run():
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = Thread(target=run)
    t.start()

keep_alive()

import discord
from discord.ext import commands
from discord import app_commands
from supabase import create_client, Client

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix='!', intents=intents)

# ==========================================
# 🌐 [Supabase DB 클라이언트 연결]
# ==========================================
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("🚨 [경고] SUPABASE_URL 또는 SUPABASE_KEY 환경 변수가 설정되지 않았습니다!")
    supabase = None
else:
    try:
        supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
        print("✅ Supabase 데이터베이스 연결 성공!")
    except Exception as e:
        print(f"🚨 Supabase 연결 실패: {e}")
        supabase = None

# ==========================================
# 🚨 [점검 모드 설정 스위치]
# ==========================================
MAINTENANCE_MODE = False 

# 쿨타임 관리를 위한 메모리 사전 (유저ID: 다음 수행 가능 시간)
mission_cooldowns = {}

# === 🎖️ 명예 포인트에 따른 직급 계산 함수 ===
def get_user_rank(honor: int) -> str:
    if honor >= 200:
        return "👑 패밀리 언더보스"
    elif honor >= 150:
        return "⚔️ 간부 (지휘관)"
    elif honor >= 100:
        return "🥇 행동대장"
    elif honor >= 50:
        return "🥈 정식 행동대원"
    else:
        return "🥉 신입 행동대원"

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
        {"id": "gov_pass", "name": "치안국 위조 신분증", "price": 1200, "desc": "실패 시 수배 방어", "emoji": "🪪"},
        {"id": "vip_cigar", "name": "고급 시가 패키지", "price": 500, "desc": "즉시 명예 +15 pt", "emoji": "🚬"},
        {"id": "chrono_watch", "name": "고급 크로노그래프 시계", "price": 2000, "desc": "임무 쿨타임 50% 단축 (5분)", "emoji": "⏱️"}
    ],
    "던베르크": [
        {"id": "heavy_glove", "name": "강화 작업용 장갑", "price": 800, "desc": "임무 보상금 +15%", "emoji": "🥊"},
        {"id": "boost_drink", "name": "고농축 자양강장제", "price": 400, "desc": "쿨타임 즉시 초기화", "emoji": "🧪"},
        {"id": "quick_bike", "name": "통신용 특수 바이크", "price": 2200, "desc": "기동력 상승으로 쿨타임 50% 단축 (5분)", "emoji": "🛵"}
    ],
    "블랙워드": [
        {"id": "lockpick", "name": "녹슨 락픽 세트", "price": 300, "desc": "임무 성공률 +10%", "emoji": "🔑"},
        {"id": "black_hood", "name": "익명의 덮개", "price": 600, "desc": "임무 성공률 +10%", "emoji": "🥷"},
        {"id": "night_goggles", "name": "암시장 야시경", "price": 1500, "desc": "임무 성공률 +15%", "emoji": "🗡️"}
    ],
    "에르느코항": [
        {"id": "smuggled_rum", "name": "밀수된 고급 양주", "price": 1500, "desc": "임무 명예 보상 2배", "emoji": "🍾"},
        {"id": "stealth_sonar", "name": "해상 무전 도청기", "price": 2000, "desc": "성공률 +15% & 자금 +10%", "emoji": "📻"},
        {"id": "pirate_map", "name": "해적 비밀 항해 지도", "price": 2500, "desc": "임무 성공률 +20%", "emoji": "🗺️"}
    ]
}

# === 🗄️ Supabase DB 연동 함수들 ===
def get_user_data(user_id: int) -> dict:
    str_id = str(user_id)
    if not supabase:
        return {"user_id": str_id, "money": 1000, "honor": 0, "location": "레벤하임", "inventory": {}}

    try:
        response = supabase.table("user_data").select("*").eq("user_id", str_id).execute()
        if response.data:
            user_info = response.data[0]
            if "inventory" not in user_info or user_info["inventory"] is None:
                user_info["inventory"] = {}
            return user_info
        
        default_data = {
            "user_id": str_id,
            "money": 1000,
            "honor": 0,
            "location": "레벤하임",
            "inventory": {}
        }
        supabase.table("user_data").insert(default_data).execute()
        return default_data
    except Exception as e:
        print(f"DB Error (get_user_data): {e}")
        return {"user_id": str_id, "money": 1000, "honor": 0, "location": "레벤하임", "inventory": {}}

def update_user_stats(user_id: int, money_add: int, honor_add: int):
    str_id = str(user_id)
    if not supabase: return
    user_info = get_user_data(user_id)
    
    new_money = user_info["money"] + money_add
    new_honor = user_info["honor"] + honor_add
    
    supabase.table("user_data").update({
        "money": new_money,
        "honor": new_honor
    }).eq("user_id", str_id).execute()

def update_user_location(user_id: int, new_location: str):
    str_id = str(user_id)
    if not supabase: return
    supabase.table("user_data").update({
        "location": new_location
    }).eq("user_id", str_id).execute()

def update_user_market_purchase(user_id: int, new_money: int, new_honor: int, inventory: dict):
    str_id = str(user_id)
    if not supabase: return
    supabase.table("user_data").update({
        "money": new_money,
        "honor": new_honor,
        "inventory": inventory
    }).eq("user_id", str_id).execute()

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
        current_rank = get_user_rank(user_stats['honor'])

        embed = discord.Embed(
            title="🕶️ [패밀리 입단 환영]",
            description=f"환영한다, 신입.\n**{user.mention}** 님이 패밀리의 새로운 조직원으로 합류했습니다.",
            color=discord.Color.dark_red()
        )
        if user.avatar:
            embed.set_thumbnail(url=user.avatar.url)
        embed.add_field(name="🗡️ 초기 직급", value=current_rank, inline=True)
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

# === 3. 임무 선택 뷰 (승급 알림 기능 포함) ===
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
        user_stats = get_user_data(user.id)
        old_rank = get_user_rank(user_stats.get("honor", 0))
        inventory = user_stats.get("inventory", {})

        now = datetime.now()
        if user.id in mission_cooldowns:
            next_available = mission_cooldowns[user.id]
            if now < next_available:
                remaining = int((next_available - now).total_seconds())
                mins, secs = divmod(remaining, 60)
                await interaction.response.send_message(f"⏳ 체력을 회복 중입니다. **{mins}분 {secs}초** 뒤에 다시 임무를 수행할 수 있습니다.", ephemeral=True)
                return

        cooldown_minutes = 10
        if "⏱️ 고급 크로노그래프 시계" in inventory or "🛵 통신용 특수 바이크" in inventory:
            cooldown_minutes = 5

        mission_cooldowns[user.id] = now + timedelta(minutes=cooldown_minutes)

        mission_data = {
            "mission_1": {"title": "🕶️ 타 조직 구역 비밀 조사", "success_rate": 80, "reward": (300, 700), "honor": 10},
            "mission_2": {"title": "💼 밀수품 비밀 운송 작업", "success_rate": 65, "reward": (600, 1500), "honor": 25},
            "mission_3": {"title": "🏛️ 라이벌 보스의 장부 탈취", "success_rate": 50, "reward": (1500, 3500), "honor": 50},
        }

        selected = mission_data[self.values[0]]
        current_loc = user_stats.get("location", "레벤하임")

        success_bonus = 0
        money_mult = 1.0
        honor_mult = 1.0
        active_buffs = []

        if "🔑 녹슨 락픽 세트" in inventory:
            success_bonus += 10
            active_buffs.append("🔑 락픽 (+10%)")
        if "🥷 익명의 덮개" in inventory:
            success_bonus += 10
            active_buffs.append("🥷 덮개 (+10%)")
        if "🗡️ 암시장 야시경" in inventory:
            success_bonus += 15
            active_buffs.append("🗡️ 야시경 (+15%)")
        if "🗺️ 해적 비밀 항해 지도" in inventory:
            success_bonus += 20
            active_buffs.append("🗺️ 항해 지도 (+20%)")
        if "📻 해상 무전 도청기" in inventory:
            success_bonus += 15
            money_mult += 0.10
            active_buffs.append("📻 도청기 (+15% 성공률 & +10% 자금)")
        if "🥊 강화 작업용 장갑" in inventory:
            money_mult += 0.15
            active_buffs.append("🥊 장갑 (+15% 자금)")
        if "🍾 밀수된 고급 양주" in inventory:
            honor_mult += 1.0
            active_buffs.append("🍾 양주 (명예 2배)")
        if cooldown_minutes == 5:
            active_buffs.append("⏱️ 시간/이동 장비 (쿨타임 5분 적용)")

        final_success_rate = min(98, selected["success_rate"] + success_bonus)
        is_success = random.randint(1, 100) <= final_success_rate

        buff_desc = f"\n✨ **적용된 보너스:** {', '.join(active_buffs)}" if active_buffs else ""

        if is_success:
            base_money = random.randint(*selected["reward"])
            earned_money = int(base_money * money_mult)
            earned_honor = int(selected["honor"] * honor_mult)
            
            update_user_stats(user.id, earned_money, earned_honor)
            current_stats = get_user_data(user.id)
            new_rank = get_user_rank(current_stats["honor"])

            rank_up_msg = ""
            if old_rank != new_rank:
                rank_up_msg = f"\n\n🎉 **[조직 승급 축하]** **{old_rank}** ➔ **{new_rank}**(으)로 승급하셨습니다!"

            embed = discord.Embed(
                title=f"🎯 [{current_loc}] 임무 성공",
                description=f"**{user.mention}** 님이 선택하신 작전을 완벽하게 완수했습니다!{buff_desc}\n\n"
                            f"📌 **작전명:** {selected['title']}\n"
                            f"📍 **수행 구역:** {current_loc}\n"
                            f"💰 **획득 보상금:** `${earned_money:,}`\n"
                            f"🏆 **획득 명예:** `+{earned_honor} pt`\n\n"
                            f"🗡️ **현재 직급:** `{new_rank}`\n"
                            f"🏦 **보유 자금:** `${current_stats['money']:,}`\n"
                            f"🏅 **총 명예:** `{current_stats['honor']} pt`{rank_up_msg}",
                color=discord.Color.green()
            )
        else:
            if "🪪 치안국 위조 신분증" in inventory:
                fail_msg = "⚠️ 임무에 실패했으나 **🪪 치안국 위조 신분증** 덕분에 무사히 수배를 면했습니다!"
            else:
                fail_msg = "⚠️ 경비대에 적발되어 임무 보상을 얻지 못했습니다."

            current_stats = get_user_data(user.id)
            current_rank = get_user_rank(current_stats["honor"])
            embed = discord.Embed(
                title=f"💥 [{current_loc}] 임무 실패",
                description=f"**{user.mention}** 님이 선택하신 작전 도중 문제가 발생했습니다!{buff_desc}\n\n"
                            f"📌 **작전명:** {selected['title']}\n"
                            f"📍 **수행 구역:** {current_loc}\n"
                            f"{fail_msg}\n\n"
                            f"🗡️ **현재 직급:** `{current_rank}`\n"
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

# === 4. 암시장 드롭다운 및 Supabase DB 저장 뷰 ===
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

        user_info = get_user_data(interaction.user.id)
        old_rank = get_user_rank(user_info.get("honor", 0))
        inventory = user_info.get("inventory", {})
        item_name = f"{target_item['emoji']} {target_item['name']}"

        if inventory.get(item_name, 0) >= 1:
            await interaction.response.send_message(
                f"⚠️ **{item_name}**은(는) 이미 소지 중입니다! 암시장 물품은 1개만 보유할 수 있습니다.", 
                ephemeral=True
            )
            return

        if user_info["money"] < target_item["price"]:
            await interaction.response.send_message(
                f"❌ 자금이 부족합니다! (필요 자금: ${target_item['price']:,} / 보유 자금: ${user_info['money']:,})", 
                ephemeral=True
            )
            return

        new_money = user_info["money"] - target_item["price"]
        new_honor = user_info["honor"]
        inventory[item_name] = 1
        
        special_effect_text = ""
        if target_item['id'] == "vip_cigar":
            new_honor += 15
            special_effect_text = "\n✨ **특수 효과:** 명예 포인트가 즉시 **+15 pt** 증가했습니다!"
        elif target_item['id'] == "boost_drink":
            if interaction.user.id in mission_cooldowns:
                del mission_cooldowns[interaction.user.id]
            special_effect_text = "\n⚡ **특수 효과:** 임무 쿨타임이 **즉시 초기화**되었습니다!"

        update_user_market_purchase(interaction.user.id, new_money, new_honor, inventory)

        new_rank = get_user_rank(new_honor)
        rank_up_msg = ""
        if old_rank != new_rank:
            rank_up_msg = f"\n🎉 **[조직 승급]** 명예 상승으로 **{new_rank}**(으)로 승급하셨습니다!"

        embed = discord.Embed(
            title="💰 [암시장 거래 성사]",
            description=f"**{interaction.user.mention}** 님이 **{item_name}**을(를) 구매하셨습니다!{special_effect_text}{rank_up_msg}\n\n"
                        f"💵 **결제 금액:** `${target_item['price']:,}`\n"
                        f"🏦 **남은 자금:** `${new_money:,}`\n"
                        f"🏆 **현재 명예:** `{new_honor} pt`\n"
                        f"🎒 **보유 상태:** `소지 중 (Supabase DB 영구 저장완료)`",
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
                    "⚠️ **주의:** 소지한 암시장 아이템 효과가 작전 수행 시 자동으로 반영됩니다.",
        color=discord.Color.dark_purple()
    )
    if user.avatar:
        embed.set_thumbnail(url=user.avatar.url)

    view = MissionView(owner=user)
    await interaction.response.send_message(embed=embed, view=view)

# === 8. `/프로필` 명령어 ===
@bot.tree.command(name="프로필", description="[마피아 GM] 패밀리 조직원 신원 조회 서류 및 인벤토리를 확인합니다.")
async def profile(interaction: discord.Interaction):
    if not await check_maintenance(interaction): return

    user = interaction.user
    user_stats = get_user_data(user.id)
    user_rank = get_user_rank(user_stats.get("honor", 0))

    embed = discord.Embed(
        title="🕵️‍♂️ [패밀리 비밀 문서] 조직원 신원 조회",
        description=f"**{user.name}** 패밀리원에 대한 보안 기록입니다.",
        color=discord.Color.dark_red()
    )
    if user.avatar:
        embed.set_thumbnail(url=user.avatar.url)

    embed.add_field(name="📜 조직원명", value=user.mention, inline=True)
    embed.add_field(name="🗡️ 현재 직급", value=user_rank, inline=True)
    embed.add_field(name="💰 보유 자금", value=f"${user_stats['money']:,}", inline=True)
    embed.add_field(name="🏆 조직 명예", value=f"{user_stats['honor']} pt", inline=True)
    embed.add_field(name="📍 현재 거점", value=user_stats.get('location', '레벤하임'), inline=False)
    
    inventory = user_stats.get("inventory", {})
    if inventory:
        inv_text = "\n".join([f"• **{item}**: `소지 중`" for item in inventory.keys()])
    else:
        inv_text = "*소지한 아이템이 없습니다.*"
        
    embed.add_field(name="🎒 소지품 (인벤토리)", value=inv_text, inline=False)
    embed.set_footer(text=f"문서 요청자: {user.name}")

    await interaction.response.send_message(embed=embed)

# === 9. 🎒 `/인벤토리` 명령어 ===
@bot.tree.command(name="인벤토리", description="[마피아 GM] 자신이 소지한 암시장 아이템과 적용 효과를 확인합니다.")
async def show_inventory(interaction: discord.Interaction):
    if not await check_maintenance(interaction): return

    user = interaction.user
    role = discord.utils.get(interaction.guild.roles, name="조직원")

    if not role or role not in user.roles:
        await interaction.response.send_message("❌ 패밀리 조직원만 인벤토리를 열 수 있습니다. `/가입`을 먼저 해주세요.", ephemeral=True)
        return

    user_stats = get_user_data(user.id)
    inventory = user_stats.get("inventory", {})

    embed = discord.Embed(
        title=f"🎒 [{user.name} 님의 장비 가방]",
        description=f"현재 **${user_stats['money']:,}** 자금을 보유 중이며, 암시장 장비는 **중복 소지가 불가능**합니다.\n",
        color=discord.Color.dark_green()
    )

    if inventory:
        inv_list = [f"• **{item}** `[보유 중]`" for item in inventory.keys()]
        
        embed.add_field(name="📦 보유 중인 전용 장비", value="\n".join(inv_list), inline=False)
        embed.add_field(
            name="💡 보유 아이템 주요 보너스 효과",
            value="• **⏱️ 시계 / 🛵 바이크**: 임무 쿨타임 50% 단축 (5분)\n"
                  "• **🔑 락픽 / 🥷 덮개 / 🗡️ 야시경 / 🗺️ 지도**: 성공 확률 증가 (+10% ~ +20%)\n"
                  "• **📻 도청기**: 성공률 +15% & 보상 자금 +10%\n"
                  "• **🥊 장갑**: 임무 성공 보상 자금 +15%\n"
                  "• **🧪 자양강장제**: 구매 시 쿨타임 즉시 초기화",
            inline=False
        )
    else:
        embed.add_field(name="📦 소지 아이템 목록", value="*가방이 비어 있습니다. `/암시장`에서 전용 장비를 구해보세요!*", inline=False)

    if user.avatar:
        embed.set_thumbnail(url=user.avatar.url)
    embed.set_footer(text=f"조직원: {user.name}")

    await interaction.response.send_message(embed=embed)

# === 10. `/이동` 명령어 ===
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

# === 11. `/암시장` 명령어 ===
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
    items = BLACK_MARKET_ITEMS.get(current_loc, [])

    item_details = []
    for item in items:
        item_details.append(f"• **{item['emoji']} {item['name']}** (`${item['price']:,}`)\n  └ {item['desc']}")

    embed = discord.Embed(
        title=f"🕵️‍♂️ {loc_emoji} [{current_loc} 비밀 암시장]",
        description=f"**{user.name}** 님, 은밀한 음지 거래소에 오신 것을 환영합니다.\n"
                    f"현재 보유 자금: **${user_stats['money']:,}**\n\n"
                    f"📜 **[판매 물품 목록 및 상세 효과]**\n" + "\n".join(item_details) + "\n\n"
                    f"⚠️ *암시장 물품은 중복 보유가 불가능하며 종류별 1개만 구매 가능합니다.*",
        color=discord.Color.gold()
    )
    if user.avatar:
        embed.set_thumbnail(url=user.avatar.url)

    view = MarketView(owner=user, location=current_loc)
    await interaction.response.send_message(embed=embed, view=view)

# === 12. `/추방` 명령어 ===
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

# === 13. `/도움말` 명령어 ===
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
    embed.add_field(name="🕵️‍♂️ `/암시장`", value="현재 거점의 비밀 암시장에서 고성능 밀수품을 거래합니다.", inline=False)
    embed.add_field(name="🎒 `/인벤토리`", value="소지 중인 암시장 장비와 적용 중인 특수 효과를 확인합니다.", inline=False)
    embed.add_field(name="🗡️ `/임무`", value="원하는 비밀 작전을 선택하여 수행합니다. (보유 아이템 버프 자동 적용)", inline=False)
    embed.add_field(name="🕵️‍♂️ `/프로필`", value="나의 조직원 신원 서류, 자금, 명예, 위치 및 소지품을 확인합니다.", inline=False)
    embed.add_field(name="⚖️ `/추방 [유저]`", value="[간부 전용] 대상 조직원을 강제 추방합니다.", inline=False)
    embed.add_field(name="❓ `/도움말`", value="봇의 명령어 가이드를 확인합니다.", inline=False)
    
    embed.set_footer(text="이 메시지는 명령어를 입력한 본인에게만 표시됩니다.")

    await interaction.response.send_message(embed=embed, ephemeral=True)

# 봇 토큰 입력
bot.run(os.getenv("DISCORD_TOKEN"))