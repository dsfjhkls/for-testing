"""/탈옥 숫자 야구"""

import asyncio
import discord
import random
import re
import time

from .core import GuardedView, bot, jail_list
from .status import save_status, update_user_location
from .admin import check_maintenance
from .family import _is_jailed, _is_mafia

# ==========================================================================
# [/탈옥]  숫자 야구(4자리) 탈옥 게임
#
#   • 수감 중인 조직원만 사용할 수 있습니다. (수감 중에는 다른 명령어가 막혀 있으므로 check_jail 은 쓰지 않습니다)
#   • 0~9 중 서로 다른 숫자 4개(0으로 시작 가능)를 ESCAPE_MAX_TRIES 번 안에 맞히면 즉시 석방됩니다.
#       - 스트라이크(S): 숫자와 자리가 모두 같음 / 볼(B): 숫자는 있지만 자리가 다름 / 둘 다 없으면 아웃
#   • 실패·포기·시간 초과 시 형기는 그대로이며, ESCAPE_COOLDOWN_SEC 뒤에 다시 도전할 수 있습니다. (0 이면 제한 없음)
#   • 게임이 진행되는 동안에는 명령어 동시 실행 방지 잠금이 유지됩니다. (GuardedView)
# ==========================================================================
ESCAPE_MAX_TRIES = 7
ESCAPE_DIGITS = 4
ESCAPE_COOLDOWN_SEC = 60      # 도전 시작 후 재도전까지 대기 시간(초)
ESCAPE_TIMEOUT_SEC = 300      # 게임 화면 유지 시간(초)

escape_cooldowns = {}   # {user_id: 재도전 가능 시각(time.time 기준)}


def baseball_judge(answer: str, guess: str):
    """(스트라이크, 볼) 반환"""
    strike = sum(a == g for a, g in zip(answer, guess))
    ball = len(set(answer) & set(guess)) - strike
    return strike, ball


def baseball_result_text(strike: int, ball: int) -> str:
    return "아웃" if strike == 0 and ball == 0 else f"{strike}S {ball}B"


def parse_baseball_guess(text: str):
    """(숫자 문자열, 오류 메시지) — 정상이면 오류는 None"""
    text = (text or "").strip()
    if not re.fullmatch(rf"[0-9]{{{ESCAPE_DIGITS}}}", text):
        return None, f"숫자 {ESCAPE_DIGITS}자리를 입력해 주세요. (예: 1234)"
    if len(set(text)) != ESCAPE_DIGITS:
        return None, "서로 다른 숫자 4개여야 합니다. (예: 1234 ✔ / 1123 ✘)"
    return text, None


class EscapeGuessModal(discord.ui.Modal, title="탈옥 — 숫자 입력"):
    guess = discord.ui.TextInput(
        label=f"서로 다른 숫자 {ESCAPE_DIGITS}자리",
        placeholder="예: 1234",
        min_length=ESCAPE_DIGITS,
        max_length=ESCAPE_DIGITS,
    )

    def __init__(self, game: "EscapeGameView"):
        super().__init__(timeout=ESCAPE_TIMEOUT_SEC)
        self.game = game

    async def on_submit(self, interaction: discord.Interaction):
        await self.game.submit_guess(interaction, str(self.guess.value))


class EscapeGameView(GuardedView):
    holds_command_lock = True   # 탈옥 게임이 끝날 때까지 다른 명령어 실행 차단

    def __init__(self, owner: discord.Member):
        super().__init__(timeout=ESCAPE_TIMEOUT_SEC)
        self.owner = owner
        self.answer = "".join(random.sample("0123456789", ESCAPE_DIGITS))
        self.history = []        # [(guess, strike, ball)]
        self.finished = False
        self.message = None

    # ---------------------------------------------------------------- 화면
    def build_embed(self, title: str = "[탈옥] 숫자 야구", note: str = "", color=None) -> discord.Embed:
        left = ESCAPE_MAX_TRIES - len(self.history)
        embed = discord.Embed(title=title, color=color or discord.Color.dark_grey())
        desc = (
            f"{self.owner.mention} 님이 탈옥을 시도합니다!\n"
            f"**{ESCAPE_MAX_TRIES}번 안에** 서로 다른 숫자 {ESCAPE_DIGITS}자리를 맞히면 **즉시 탈출**합니다.\n"
            f"`S`=숫자·자리 일치 / `B`=숫자만 일치 / 둘 다 없으면 `아웃`"
        )
        if note:
            desc += f"\n\n{note}"
        embed.description = desc
        if self.history:
            lines = [f"`{i + 1}회` **{g}** → {baseball_result_text(s, b)}" for i, (g, s, b) in enumerate(self.history)]
            embed.add_field(name="추리 기록", value="\n".join(lines), inline=False)
        embed.set_footer(text=f"남은 기회 {left}번 · {ESCAPE_TIMEOUT_SEC}초 이내에 진행하세요")
        if self.owner.avatar:
            embed.set_thumbnail(url=self.owner.avatar.url)
        return embed

    def _end(self):
        self.finished = True
        self.stop()

    # ---------------------------------------------------------------- 추측 처리
    async def submit_guess(self, interaction: discord.Interaction, raw: str):
        if self.finished:
            await interaction.response.send_message("이미 끝난 게임입니다.", ephemeral=True)
            return

        # 도중에 형기가 끝났다면(자연 석방) 게임 종료
        if not _is_jailed(self.owner.id):
            self._end()
            await interaction.response.edit_message(
                embed=self.build_embed("[탈옥] 종료", "이미 석방되어 더 이상 탈옥할 필요가 없습니다.", discord.Color.blue()),
                view=None)
            return

        guess, err = parse_baseball_guess(raw)
        if err:
            await interaction.response.send_message(err, ephemeral=True)   # 잘못된 입력은 기회를 소모하지 않음
            return

        if any(g == guess for g, _, _ in self.history):
            await interaction.response.send_message("이미 시도한 숫자입니다. (기회는 소모되지 않았습니다)", ephemeral=True)
            return

        strike, ball = baseball_judge(self.answer, guess)
        self.history.append((guess, strike, ball))

        if strike == ESCAPE_DIGITS:
            await self._success(interaction)
        elif len(self.history) >= ESCAPE_MAX_TRIES:
            await self._fail(interaction, f"기회를 모두 사용했습니다. 정답은 **{self.answer}** 였습니다.")
        else:
            await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _success(self, interaction: discord.Interaction):
        self._end()
        jail_list.pop(self.owner.id, None)
        save_status(self.owner.id)
        try:   # check_jail 의 석방 처리와 동일하게 위치를 기본 거점으로 복구
            await asyncio.to_thread(update_user_location, self.owner.id, "레벤하임")
        except Exception as e:
            print(f"DB Error (escape location): {e}")

        tries = len(self.history)
        embed = self.build_embed(
            "[탈옥 성공]",
            f"**{self.answer}** — 정답! **{tries}번 만에** 경비를 따돌리고 탈출했습니다!\n"
            f"{self.owner.mention} 님은 즉시 석방되었습니다.",
            discord.Color.green())
        embed.set_footer(text="탈옥 성공!")
        await interaction.response.edit_message(embed=embed, view=None)

    async def _fail(self, interaction: discord.Interaction, note: str, edit=None):
        self._end()
        embed = self.build_embed("[탈옥 실패]", f"{note}\n경비에게 붙잡혀 다시 감방으로 돌아갑니다.", discord.Color.red())
        embed.set_footer(text=(f"{ESCAPE_COOLDOWN_SEC}초 뒤에 다시 도전할 수 있습니다." if ESCAPE_COOLDOWN_SEC else "탈옥 실패"))
        if interaction is not None:
            await interaction.response.edit_message(embed=embed, view=None)
        elif edit is not None:
            await edit(embed=embed, view=None)

    # ---------------------------------------------------------------- 버튼
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner.id:
            await interaction.response.send_message("본인의 탈옥 시도만 조작할 수 있습니다.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="숫자 입력", style=discord.ButtonStyle.primary)
    async def guess_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.finished:
            await interaction.response.send_message("이미 끝난 게임입니다.", ephemeral=True)
            return
        await interaction.response.send_modal(EscapeGuessModal(self))

    @discord.ui.button(label="포기", style=discord.ButtonStyle.danger)
    async def give_up_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.finished:
            await interaction.response.send_message("이미 끝난 게임입니다.", ephemeral=True)
            return
        await self._fail(interaction, "탈옥을 포기했습니다.")

    async def on_timeout(self):
        if self.finished or self.message is None:
            return
        self._end()
        await self._fail(None, "시간이 초과되었습니다.", edit=self.message.edit)


@bot.tree.command(name="탈옥", description=f"[조직원 전용] 숫자 야구({ESCAPE_DIGITS}자리)를 {ESCAPE_MAX_TRIES}번 안에 맞히면 즉시 탈옥합니다.")
async def escape_command(interaction: discord.Interaction):
    if not await check_maintenance(interaction): return
    user = interaction.user

    if not _is_mafia(user):
        await interaction.response.send_message("탈옥은 마피아 조직원만 시도할 수 있습니다.", ephemeral=True)
        return

    if not _is_jailed(user.id):
        await interaction.response.send_message("현재 수감 중이 아닙니다. 탈옥할 필요가 없어요!", ephemeral=True)
        return

    now = time.time()
    cd = escape_cooldowns.get(user.id, 0)
    if now < cd:
        await interaction.response.send_message(
            f"경비가 삼엄합니다. **{int(cd - now) + 1}초** 뒤에 다시 도전할 수 있습니다.", ephemeral=True)
        return
    if ESCAPE_COOLDOWN_SEC:
        escape_cooldowns[user.id] = now + ESCAPE_COOLDOWN_SEC   # 도중에 그만둬도 쿨타임을 피할 수 없게 시작 시점에 적용

    game = EscapeGameView(user)
    await interaction.response.send_message(embed=game.build_embed(), view=game)
    game.message = await interaction.original_response()
