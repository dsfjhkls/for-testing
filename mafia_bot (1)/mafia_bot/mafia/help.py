"""/도움말"""

import discord
from discord import app_commands

from .core import bot
from .admin import check_maintenance
from .family import FAMILY_CREATE_MIN_HONOR

# ==========================================================================
# [/도움말]  전체 명령어 안내
#   • 봇에 등록된 슬래시 명령어(bot.tree)를 실시간으로 읽어서 보여줍니다.
#     → 새 명령어를 추가해도 따로 고칠 필요 없이 자동으로 목록에 들어갑니다. (설명은 각 명령어의 description)
#   • 아래 HELP_CATEGORIES 에 이름을 적지 않은 명령어는 '기타' 분류에 모입니다.
# ==========================================================================
HELP_CATEGORIES = [
    # (키, 표시 이름, 분류 설명, [명령어 이름 — /조직 처럼 그룹 명령어는 첫 단어만])
    ("start",   "시작 · 정보",          "가입과 내 정보 확인, 설정, 랭킹 등 기본이 되는 명령어입니다.",
     ["도움말", "가입", "탈퇴", "프로필", "정보", "설정", "일퀘", "주간", "칭호", "업적", "랭킹"]),
    ("economy", "이동 · 상점 · 경제",   "거점 이동, 장비 구매, 자금 이체, 제작 등 돈과 장비에 관한 명령어입니다.",
     ["이동", "암시장", "무기상점", "백화점", "인벤토리", "송금", "제작", "경매", "사업", "주식"]),
    ("citizen", "시민 활동",         "가입 전 시민의 활동입니다. 좋은 활동은 선, 나쁜 활동은 악 성향이 쌓이고 전직 조건에 영향을 줍니다.",
     ["아르바이트", "봉사", "기부", "소매치기"]),
    ("mafia",   "마피아 활동",          "비밀 임무 수행, 경찰 매수, 탈옥 등 조직원의 범죄 활동입니다.",
     ["임무", "밀수", "탈옥"]),
    ("battle",  "전투 · 능력치",        "NPC·네임드·보스 토벌과 결투, 전투 능력치 확인입니다.",
     ["능력치", "전투", "네임드", "보스", "결투"]),
    ("casino",  "카지노",               "자금을 걸고 즐기는 도박입니다. (`/도박 규칙` 에서 배당표 확인)",
     ["도박"]),
    ("police",  "경찰",                 "경찰 소속 전용 명령어입니다.",
     ["작전", "검거", "순찰", "심문", "월급", "경찰상점", "수사팀", "갈취"]),
    ("family",  "조직 · 레이드 · 구역", "조직(패밀리)을 만들어 함께하는 협동·점령 콘텐츠입니다.",
     ["조직", "레이드", "구역"]),
    ("admin",   "관리자",               "서버 관리자 전용 명령어입니다.",
     ["추방", "관리"]),
]


def _help_usage(cmd) -> str:
    """'/송금 <member> <amount>' 처럼 사용법 문자열 생성 (<필수> / [선택])"""
    parts = [f"/{cmd.qualified_name}"]
    for prm in cmd.parameters:
        name = getattr(prm, "display_name", None) or prm.name
        parts.append(f"<{name}>" if prm.required else f"[{name}]")
    return " ".join(parts)


def _help_pages() -> list:
    """현재 등록된 명령어를 분류별로 정리 → [{key, label, desc, cmds}]"""
    by_root = {}
    for cmd in bot.tree.walk_commands():
        if isinstance(cmd, app_commands.Group):
            continue   # 그룹 자체가 아니라 그 안의 하위 명령어를 나열
        by_root.setdefault(cmd.qualified_name.split(" ")[0], []).append(cmd)

    pages, used = [], set()
    for key, label, desc, roots in HELP_CATEGORIES:
        cmds = []
        for root in roots:
            if root in by_root:
                cmds.extend(by_root[root])
                used.add(root)
        if cmds:
            pages.append({"key": key, "label": label, "desc": desc, "cmds": cmds})

    leftover = [c for root, cs in by_root.items() if root not in used for c in cs]
    if leftover:
        pages.append({"key": "etc", "label": "기타", "desc": "분류되지 않은 명령어입니다.", "cmds": leftover})
    return pages


def help_overview_embed(pages: list) -> discord.Embed:
    from .police_unit import POLICE_UNIT_CREATE_MIN_HONOR   # 순환 import 방지: 호출 시점에 가져옴
    total = sum(len(pg["cmds"]) for pg in pages)
    lines = [f"**{pg['label']}** ({len(pg['cmds'])}개)\n└ {pg['desc']}" for pg in pages]
    embed = discord.Embed(
        title="[패밀리 GM 도움말]",
        description=(f"총 **{total}개**의 명령어가 있습니다. 아래 메뉴에서 분류를 고르면 사용법과 설명을 볼 수 있습니다.\n"
                     f"사용법 표기: `<필수>` `[선택]`  ·  설명 앞의 `[ ]` 는 사용할 수 있는 대상입니다.\n\n"
                     + "\n\n".join(lines)),
        color=discord.Color.blurple()
    )
    embed.add_field(
        name="처음이라면",
        value=("`/가입` 으로 조직원 또는 경찰이 되세요.\n"
               "조직원은 `/임무` · `/전투` 로 자금과 명예를 모으고, `/무기상점` · `/암시장` · `/제작` 으로 장비를 갖춥니다.\n"
               f"명예 {FAMILY_CREATE_MIN_HONOR}pt 이상이면 `/조직 생성` 으로 조직을 만들어 `/레이드` 와 `/구역` 에 도전할 수 있습니다.\n"
               f"경찰은 `/순찰` · `/검거` 로 활동하고, 명예 {POLICE_UNIT_CREATE_MIN_HONOR}pt 이상이면 `/수사팀 생성` 으로 수사팀을 만들어 `/수사팀 작전` 에 도전할 수 있습니다."),
        inline=False
    )
    embed.set_footer(text="수감 중에는 일부 명령어를 쓸 수 없습니다. (수감자는 /탈옥 으로 탈출을 시도할 수 있어요)")
    return embed


def help_category_embed(page: dict) -> discord.Embed:
    blocks = [f"**`{_help_usage(c)}`**\n└ {c.description}" for c in page["cmds"]]
    text = f"{page['desc']}\n\n" + "\n\n".join(blocks)
    if len(text) > 4000:
        text = text[:3990] + "…"
    embed = discord.Embed(title=f"[도움말] {page['label']}", description=text, color=discord.Color.blurple())
    embed.set_footer(text=f"명령어 {len(page['cmds'])}개 · 사용법 표기: <필수> [선택]")
    return embed


class HelpSelect(discord.ui.Select):
    def __init__(self, pages: list):
        self.pages = pages
        options = [discord.SelectOption(label="전체 보기", value="__all__", description="분류 목록과 처음 시작하는 방법", default=True)]
        for pg in pages:
            options.append(discord.SelectOption(label=pg["label"], value=pg["key"], description=f"명령어 {len(pg['cmds'])}개"))
        super().__init__(placeholder="분류를 선택하세요...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        key = self.values[0]
        for opt in self.options:
            opt.default = (opt.value == key)
        if key == "__all__":
            embed = help_overview_embed(self.pages)
        else:
            embed = help_category_embed(next(pg for pg in self.pages if pg["key"] == key))
        await interaction.response.edit_message(embed=embed, view=self.view)


class HelpView(discord.ui.View):
    def __init__(self, pages: list):
        super().__init__(timeout=180)
        self.interaction = None
        self.add_item(HelpSelect(pages))

    async def on_timeout(self):
        if self.interaction is None:
            return
        try:
            await self.interaction.edit_original_response(view=None)   # 시간이 지나면 죽은 메뉴를 치운다
        except discord.HTTPException:
            pass


@bot.tree.command(name="도움말", description="[공용] 모든 명령어의 사용법과 설명을 분류별로 확인합니다.")
async def help_command(interaction: discord.Interaction):
    if not await check_maintenance(interaction): return

    pages = _help_pages()
    view = HelpView(pages)
    view.interaction = interaction
    await interaction.response.send_message(embed=help_overview_embed(pages), view=view, ephemeral=True)
