import discord
from discord import app_commands
from discord.ext import commands
import json
import os
import random
from flask import Flask
from threading import Thread

# 1. 렌더 웹 서비스 포트 검사를 통과하기 위한 가짜 웹 서버 (Keep-Alive)
app = Flask('')

@app.route('/')
def home():
    return "Mafia Bot is running!"

def run():
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = Thread(target=run)
    t.start()

# 봇 실행 전 웹 서버 구동
keep_alive()

# 2. 디스코드 봇 설정
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

DATA_FILE = "user_data.json"

# 지역 정보 데이터
LOCATIONS = {
    "레벤하임": {
        "desc": "수도 / 시작 지점. 정부 기관과 치안국 본부가 밀집한 중심 도시.",
        "risk": "보통"
    },
    "던베르크": {
        "desc": "공업지대. 대규모 공장과 노동자 거주 구역이 있는 산업 도시.",
        "risk": "중간"
    },
    "블랙워드": {
        "desc": "빈민가. 열악한 환경과 높은 범죄율로 악명 높은 골목.",
        "risk": "높음"
    },
    "에르느코항": {
        "desc": "항구도시. 해상 무역과 밀거래가 공존하는 개방형 항구.",
        "risk": "변동적"
    }
}

# 데이터 불러오기 함수
def load_user_data():
    if not os.path.exists(DATA_FILE):
        return {}
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

# 데이터 저장하기 함수
def save_user_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

@bot.event
async def on_ready():
    print(f"로그인 완료: {bot.user}")
    try:
        synced = await bot.tree.sync()
        print(f"슬래시 명령어 동기화 완료: {len(synced)}개")
    except Exception as e:
        print(e)

# 3. 명령어 구현

# /가입
@bot.tree.command(name="가입", description="마피아 조직에 가입합니다.")
async def join_mafia(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    data = load_user_data()
    
    if user_id in data:
        await interaction.response.send_message("❌ 이미 조직에 가입되어 있습니다!", ephemeral=True)
        return
        
    data[user_id] = {
        "name": interaction.user.name,
        "money": 1000,  # 초기 자금
        "honor": 50,    # 초기 명예
        "location": "레벤하임" # 기본 시작 지역
    }
    save_user_data(data)
    await interaction.response.send_message("🔫 마피아 조직에 가입하셨습니다! '레벤하임'에서 시작합니다.", ephemeral=True)

# /탈퇴
@bot.tree.command(name="탈퇴", description="조직을 탈퇴하고 모든 데이터를 초기화합니다.")
async def leave_mafia(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    data = load_user_data()
    
    if user_id not in data:
        await interaction.response.send_message("❌ 가입되어 있지 않습니다.", ephemeral=True)
        return
        
    del data[user_id]
    save_user_data(data)
    await interaction.response.send_message("👋 조직에서 탈퇴하셨습니다. 모든 기록이 삭제됩니다.", ephemeral=True)

# /프로필
@bot.tree.command(name="프로필", description="자신의 조직원 프로필을 확인합니다.")
async def profile(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    data = load_user_data()
    
    if user_id not in data:
        await interaction.response.send_message("❌ 먼저 `/가입` 명령어를 통해 조직에 합류해주세요!", ephemeral=True)
        return
        
    user = data[user_id]
    location = user.get("location", "레벤하임")
    
    embed = discord.Embed(title=f"🏴‍☠️ {interaction.user.name}의 조직원 프로필", color=discord.Color.red())
    embed.add_field(name="💰 자금", value=f"{user['money']:,} 원", inline=True)
    embed.add_field(name="⭐ 명예", value=f"{user['honor']} 점", inline=True)
    embed.add_field(name="📍 현재 위치", value=location, inline=False)
    embed.set_footer(text="칼렌데 언더그라운드")
    
    await interaction.response.send_message(embed=embed)

# /임무
@bot.tree.command(name="임무", description="현재 지역에서 은밀한 임무를 수행합니다.")
async def mission(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    data = load_user_data()
    
    if user_id not in data:
        await interaction.response.send_message("❌ 먼저 `/가입` 명령어를 통해 조직에 합류해주세요!", ephemeral=True)
        return
        
    success = random.choice([True, False])
    money_change = random.randint(300, 800)
    honor_change = random.randint(5, 15)
    location = data[user_id].get("location", "레벤하임")
    
    if success:
        data[user_id]["money"] += money_change
        data[user_id]["honor"] += honor_change
        result_text = f"성공! 임무를 완수하여 보상을 획득했습니다.\n💰 자금 +{money_change:,} | ⭐ 명예 +{honor_change}"
        color = discord.Color.green()
    else:
        data[user_id]["money"] = max(0, data[user_id]["money"] - int(money_change / 2))
        data[user_id]["honor"] = max(0, data[user_id]["honor"] - int(honor_change / 2))
        result_text = f"실패... 치안국이나 경쟁 조직에 발목을 잡혔습니다.\n💰 자금 차감 | ⭐ 명예 하락"
        color = discord.Color.orange()
        
    save_user_data(data)
    
    embed = discord.Embed(title=f"🎯 [{location}] 임무 결과", description=result_text, color=color)
    await interaction.response.send_message(embed=embed)

# /이동
@bot.tree.command(name="이동", description="칼렌데 내의 다른 지역으로 이동합니다.")
@app_commands.choices(지역=[
    app_commands.Choice(name="레벤하임 (수도/중심지)", value="레벤하임"),
    app_commands.Choice(name="던베르크 (공업지대)", value="던베르크"),
    app_commands.Choice(name="블랙워드 (빈민가)", value="블랙워드"),
    app_commands.Choice(name="에르느코항 (항구도시)", value="에르느코항")
])
async def move_location(interaction: discord.Interaction, 지역: app_commands.Choice[str]):
    user_id = str(interaction.user.id)
    data = load_user_data()

    if user_id not in data:
        await interaction.response.send_message("❌ 먼저 `/가입` 명령어를 통해 조직에 합류해주세요!", ephemeral=True)
        return

    selected_loc = 지역.value
    loc_info = LOCATIONS[selected_loc]

    data[user_id]["location"] = selected_loc
    save_user_data(data)

    embed = discord.Embed(
        title=f"🚂 [{selected_loc}] (으)로 이동 완료",
        description=f"성공적으로 **{selected_loc}**(으)로 이동했습니다.",
        color=discord.Color.dark_blue()
    )
    embed.add_field(name="지역 특징", value=loc_info["desc"], inline=False)
    embed.add_field(name="치안 위험도", value=loc_info["risk"], inline=True)
    embed.set_footer(text=f"요청자: {interaction.user.name}", icon_url=interaction.user.display_avatar.url)

    await interaction.response.send_message(embed=embed)

# 4. 렌더 환경 변수(DISCORD_TOKEN)를 통해 봇 실행
bot.run(os.getenv('DISCORD_TOKEN'))