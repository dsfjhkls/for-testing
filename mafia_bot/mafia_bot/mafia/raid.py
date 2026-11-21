"""단체 레이드 (/레이드 ...)"""

import asyncio
import discord
import random
import time
from discord import app_commands
from datetime import datetime, timedelta

from .core import bot, keep_task
from .status import compute_combat_stats, get_user_data, update_user_stats
from .achievements import bump_user_stats_sync
from .admin import check_maintenance
from .family import (
    FAMILY_DB_ERROR_MSG,
    RAID_AOE_INTERVAL_ENRAGE_SEC,
    RAID_AOE_INTERVAL_SEC,
    RAID_ATTACK_CD_SEC,
    RAID_BASE_SHARE,
    RAID_DURATION_SEC,
    RAID_FAMILY_COOLDOWN_SEC,
    RAID_GCD_SEC,
    RAID_HEAL_CD_SEC,
    RAID_HEAL_RATIO,
    RAID_LOBBY_SEC,
    RAID_LOSE_MONEY_RATIO,
    RAID_MAX_PLAYERS,
    RAID_RALLY_BONUS,
    RAID_RALLY_CD_SEC,
    RAID_RALLY_DURATION_SEC,
    RAID_REVIVE_RATIO,
    RAID_TICK_SEC,
    RAID_TIERS,
    RAID_TREASURY_RETURN,
    _bar,
    _family_precheck,
    _is_jailed,
    _is_mafia,
    active_raids,
    family_get_by_id,
    family_get_by_user,
    family_update,
    raid_family_cooldowns,
    raid_user_map,
)

# ================================================================ /레이드
class RaidPlayer:
    __slots__ = ("member", "uid", "name", "max_hp", "hp", "atk", "dmg_done", "hits",
                 "gcd_until", "atk_ready", "heal_ready", "rally_ready")

    def __init__(self, member: discord.Member, stats: dict):
        self.member = member
        self.uid = member.id
        self.name = member.display_name
        self.max_hp = int(stats["hp"])
        self.hp = self.max_hp
        self.atk = int(stats["atk"])
        self.dmg_done = 0
        self.hits = 0
        self.gcd_until = 0.0
        self.atk_ready = 0.0
        self.heal_ready = 0.0
        self.rally_ready = 0.0

    @property
    def alive(self) -> bool:
        return self.hp > 0


class RaidSession:
    def __init__(self, family: dict, tier_key: str, starter: discord.Member):
        self.family = family
        self.fid = family["id"]
        self.tier_key = tier_key
        self.tier = RAID_TIERS[tier_key]
        self.starter = starter
        self.players = {}              # {user_id: RaidPlayer}
        self.state = "lobby"           # lobby → fighting → done
        self.cancelled = False
        self.start_event = asyncio.Event()
        self.wake = asyncio.Event()
        self.message = None
        self.logs = []
        self.boss_hp = 0
        self.boss_max = 0
        self.boss_def = 0
        self.fight_end = 0.0
        self.next_aoe = 0.0
        self.enraged = False
        self.fight_started = False
        self.rally_until = 0.0         # 전투 함성 효과가 끝나는 시각 (공격대 전체에 적용)

    def log(self, text: str):
        self.logs.append(text)
        self.logs = self.logs[-6:]


def raid_lobby_embed(s: RaidSession) -> discord.Embed:
    t = s.tier
    names = "\n".join(f"• {p.member.mention} (HP {p.max_hp:,} / ATK {p.atk:,})" for p in s.players.values()) or "-"
    embed = discord.Embed(
        title=f"[레이드 모집] {t['name']}",
        description=(f"{t['desc']}\n\n"
                     f"**조직:** 「{s.family['name']}」\n"
                     f"**모집 인원:** {len(s.players)}/{RAID_MAX_PLAYERS} (최소 {t['min_players']}명)\n"
                     f"**참가 조건:** 명예 {t['min_honor']:,}pt 이상\n"
                     f"**예상 보상(1인 평균):** `${t['money']:,}` / 명예 `+{t['honor']}pt`\n"
                     f"**{RAID_LOBBY_SEC}초** 안에 `참가`를 눌러 주세요. (시작자가 `바로 시작`도 가능)"),
        color=discord.Color.orange()
    )
    embed.add_field(name="참가자", value=names, inline=False)
    return embed


def raid_fight_embed(s: RaidSession) -> discord.Embed:
    t = s.tier
    now = time.time()
    rage = "**격노 상태!** 광역 공격이 강해지고 더 자주 옵니다!\n" if s.enraged else ""
    aoe_warn = ""
    if s.state == "fighting":
        left = int(s.next_aoe - now)
        aoe_warn = f"**광역 공격까지 {max(left, 0)}초!** 응급처치 준비!\n" if left <= 6 else ""
    rally_line = f"**전투 함성 효과 중** (공격력 +{int(RAID_RALLY_BONUS * 100)}% / <t:{int(s.rally_until)}:R> 종료)\n" if s.state == "fighting" and s.rally_until > now else ""
    embed = discord.Embed(
        title=f"[레이드 전투] {t['name']}",
        description=(f"{rage}{rally_line}"
                     f"보스 `{_bar(s.boss_hp, s.boss_max, 20)}` **{max(0, int(s.boss_hp * 100 / max(1, s.boss_max)))}%**\n"
                     f"종료 <t:{int(s.fight_end)}:R>  ·  다음 광역 <t:{int(s.next_aoe)}:R>\n"
                     f"{aoe_warn}"),
        color=discord.Color.red() if s.enraged else discord.Color.orange()
    )
    lines = []
    for p in s.players.values():
        if not p.alive:
            icon = "[전투불능]"
        else:
            icon = "[전투]"
        lines.append(f"{icon} **{p.name}** `{_bar(p.hp, p.max_hp, 8)}` {int(p.hp * 100 / max(1, p.max_hp))}% · 딜 {p.dmg_done:,}")
    embed.add_field(name="공격대", value="\n".join(lines) or "-", inline=False)
    embed.add_field(name="전황", value="\n".join(s.logs) or "전투 시작!", inline=False)
    embed.set_footer(text=f"공격 {int(RAID_ATTACK_CD_SEC)}초 / 응급처치 {int(RAID_HEAL_CD_SEC)}초 / 전투 함성 {int(RAID_RALLY_CD_SEC)}초 쿨타임")
    return embed


def raid_release(s: RaidSession):
    for uid in list(s.players.keys()):
        if raid_user_map.get(uid) == s.fid:
            raid_user_map.pop(uid, None)
    if active_raids.get(s.fid) is s:
        active_raids.pop(s.fid, None)


async def _raid_edit(s: RaidSession, **kwargs):
    if not s.message:
        return
    try:
        await s.message.edit(**kwargs)
    except discord.HTTPException as e:
        print(f"Raid message edit error: {e}")


# ---------------------------------------------------------------- 모집 화면
class RaidLobbyView(discord.ui.View):
    def __init__(self, session: RaidSession):
        super().__init__(timeout=None)
        self.s = session

    @discord.ui.button(label="참가", style=discord.ButtonStyle.success)
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        s = self.s
        user = interaction.user
        if s.state != "lobby":
            await interaction.response.send_message("이미 모집이 끝났습니다.", ephemeral=True)
            return
        if user.id in s.players:
            await interaction.response.send_message("이미 참가 중입니다.", ephemeral=True)
            return
        if len(s.players) >= RAID_MAX_PLAYERS:
            await interaction.response.send_message("정원이 가득 찼습니다.", ephemeral=True)
            return
        if user.id in raid_user_map:
            await interaction.response.send_message("이미 다른 레이드에 참가 중입니다.", ephemeral=True)
            return
        if not _is_mafia(user) or _is_jailed(user.id):
            await interaction.response.send_message("마피아 조직원만(감옥 수감 중 제외) 참가할 수 있습니다.", ephemeral=True)
            return
        try:
            fam = family_get_by_id(s.fid)
            if not fam or str(user.id) not in (fam.get("member_ids") or []):
                await interaction.response.send_message("이 조직의 조직원만 참가할 수 있습니다.", ephemeral=True)
                return
            info = get_user_data(user.id)
        except Exception as e:
            print(f"Raid join error: {e}")
            await interaction.response.send_message(FAMILY_DB_ERROR_MSG, ephemeral=True)
            return
        if info["honor"] < s.tier["min_honor"]:
            await interaction.response.send_message(f"이 레이드는 명예 **{s.tier['min_honor']:,}pt** 이상만 참가할 수 있습니다.", ephemeral=True)
            return
        if user.id in s.players or s.state != "lobby":   # DB 호출 사이에 상태가 바뀐 경우
            await interaction.response.send_message("참가할 수 없는 상태입니다.", ephemeral=True)
            return
        s.players[user.id] = RaidPlayer(user, compute_combat_stats(info["honor"], info.get("inventory", {})))
        raid_user_map[user.id] = s.fid
        await interaction.response.edit_message(embed=raid_lobby_embed(s), view=self)

    @discord.ui.button(label="바로 시작", style=discord.ButtonStyle.primary)
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        s = self.s
        if interaction.user.id != s.starter.id:
            await interaction.response.send_message("레이드를 연 사람만 시작할 수 있습니다.", ephemeral=True)
            return
        if len(s.players) < s.tier["min_players"]:
            await interaction.response.send_message(f"최소 **{s.tier['min_players']}명**이 필요합니다. (현재 {len(s.players)}명)", ephemeral=True)
            return
        await interaction.response.defer()
        s.start_event.set()

    @discord.ui.button(label="취소", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        s = self.s
        if interaction.user.id != s.starter.id:
            await interaction.response.send_message("레이드를 연 사람만 취소할 수 있습니다.", ephemeral=True)
            return
        await interaction.response.defer()
        s.cancelled = True
        s.start_event.set()


# ---------------------------------------------------------------- 전투 화면 (버튼 3개)
class RaidFightView(discord.ui.View):
    def __init__(self, session: RaidSession):
        super().__init__(timeout=None)
        self.s = session

    async def _prep(self, interaction: discord.Interaction):
        """전투 중인 참가자인지, 살아 있는지, 공통 대기시간이 지났는지 확인"""
        s = self.s
        p = s.players.get(interaction.user.id)
        if s.state != "fighting":
            await interaction.response.send_message("이미 전투가 끝났습니다.", ephemeral=True)
            return None
        if not p:
            await interaction.response.send_message("이 레이드의 참가자가 아닙니다.", ephemeral=True)
            return None
        if not p.alive:
            await interaction.response.send_message("전투불능 상태입니다. 동료의 **응급처치**를 기다리세요!", ephemeral=True)
            return None
        wait = p.gcd_until - time.time()
        if wait > 0:
            await interaction.response.send_message(f"아직 손이 풀리지 않았습니다. ({wait:.1f}초)", ephemeral=True)
            return None
        return p

    @discord.ui.button(label="공격", style=discord.ButtonStyle.danger)
    async def attack(self, interaction: discord.Interaction, button: discord.ui.Button):
        p = await self._prep(interaction)
        if not p: return
        s = self.s
        now = time.time()
        if p.atk_ready > now:
            await interaction.response.send_message(f"공격 쿨타임 {p.atk_ready - now:.1f}초", ephemeral=True)
            return
        mult = 1 + RAID_RALLY_BONUS if now < s.rally_until else 1.0   # 전투 함성 효과
        dmg = max(10, int(p.atk * mult) - s.boss_def + random.randint(-10, 10))
        s.boss_hp = max(0, s.boss_hp - dmg)
        p.dmg_done += dmg
        p.hits += 1
        p.atk_ready = now + RAID_ATTACK_CD_SEC
        p.gcd_until = now + RAID_GCD_SEC
        await interaction.response.defer()
        if s.boss_hp <= 0:
            s.wake.set()

    @discord.ui.button(label="응급처치", style=discord.ButtonStyle.success)
    async def heal(self, interaction: discord.Interaction, button: discord.ui.Button):
        p = await self._prep(interaction)
        if not p: return
        s = self.s
        now = time.time()
        if p.heal_ready > now:
            await interaction.response.send_message(f"응급처치 쿨타임 {p.heal_ready - now:.1f}초", ephemeral=True)
            return
        downed = [x for x in s.players.values() if not x.alive]
        if downed:
            target = random.choice(downed)
            target.hp = max(1, int(target.max_hp * RAID_REVIVE_RATIO))
            s.log(f"{p.name} → {target.name} 부활! (체력 {int(RAID_REVIVE_RATIO * 100)}%)")
        else:
            hurt = [x for x in s.players.values() if x.alive and x.hp < x.max_hp]
            if not hurt:
                await interaction.response.send_message("치료가 필요한 동료가 없습니다.", ephemeral=True)
                return
            for x in s.players.values():
                if x.alive:
                    x.hp = min(x.max_hp, x.hp + int(x.max_hp * RAID_HEAL_RATIO))
            s.log(f"{p.name}의 응급처치! 전원 체력 +{int(RAID_HEAL_RATIO * 100)}%")
        p.heal_ready = now + RAID_HEAL_CD_SEC
        p.gcd_until = now + RAID_GCD_SEC
        await interaction.response.defer()

    @discord.ui.button(label="전투 함성", style=discord.ButtonStyle.primary)
    async def rally(self, interaction: discord.Interaction, button: discord.ui.Button):
        p = await self._prep(interaction)
        if not p: return
        s = self.s
        now = time.time()
        if p.rally_ready > now:
            await interaction.response.send_message(f"전투 함성 쿨타임 {p.rally_ready - now:.1f}초", ephemeral=True)
            return
        s.rally_until = max(s.rally_until, now + RAID_RALLY_DURATION_SEC)
        p.rally_ready = now + RAID_RALLY_CD_SEC
        p.gcd_until = now + RAID_GCD_SEC
        s.log(f"{p.name}의 전투 함성! {int(RAID_RALLY_DURATION_SEC)}초간 공격대 공격력 +{int(RAID_RALLY_BONUS * 100)}%")
        await interaction.response.defer()


# ---------------------------------------------------------------- 진행 / 정산
def raid_do_aoe(s: RaidSession):
    pct = s.tier["aoe_enrage"] if s.enraged else s.tier["aoe"]
    now = time.time()
    downed = []
    for p in s.players.values():
        if not p.alive:
            continue
        dmg = int(p.max_hp * pct * random.uniform(0.9, 1.1))
        p.hp = max(0, p.hp - dmg)
        if p.hp == 0:
            downed.append(p.name)
    text = f"광역 공격! (최대 체력의 약 {int(pct * 100)}%)"
    if downed:
        text += f" {', '.join(downed)} 전투불능!"
    s.log(text)
    s.next_aoe = now + (RAID_AOE_INTERVAL_ENRAGE_SEC if s.enraged else RAID_AOE_INTERVAL_SEC)


def _raid_settle_sync(s: RaidSession, win: bool):
    """보상 지급 + 조직 갱신 (동기 DB 작업이라 to_thread 로 실행). 반환: [(RaidPlayer, money, honor)], total_money"""
    from .crafting import grant_materials_sync, roll_material_drops   # 순환 import 방지: 호출 시점에 가져옴
    from .ranking import family_stat_inc   # 순환 import 방지: 호출 시점에 가져옴
    t = s.tier
    s.material_drops = {}   # {user_id: {재료: 개수}}  (정산 결과 화면에 표시)
    attackers = [p for p in s.players.values() if p.dmg_done > 0]
    results = []
    total_money = 0
    if attackers:
        total_dmg = sum(p.dmg_done for p in attackers)
        n = len(attackers)
        for p in attackers:
            weight = RAID_BASE_SHARE / n + (1 - RAID_BASE_SHARE) * (p.dmg_done / total_dmg)
            if win:
                money = int(t["money"] * n * weight)
                honor = max(1, int(t["honor"] * n * weight))
            else:
                money = int(t["money"] * n * weight * RAID_LOSE_MONEY_RATIO)
                honor = 0
            results.append((p, money, honor))
            total_money += money
            try:
                update_user_stats(p.uid, money, honor)
            except Exception as e:
                print(f"Raid reward error ({p.uid}): {e}")
            if win:   # 업적 카운터 (레이드 클리어)
                try:
                    incs = {"raid_clears": 1}
                    if t["name"] == RAID_TIERS["king"]["name"]:
                        incs["raid_king_clears"] = 1
                    bump_user_stats_sync(p.uid, incs, pending=True)
                except Exception as e:
                    print(f"Raid achievement error ({p.uid}): {e}")
            if win:   # 제작 재료 (/제작)
                drops = roll_material_drops(t["name"])
                if drops:
                    try:
                        grant_materials_sync(p.uid, drops)
                        s.material_drops[p.uid] = drops
                    except Exception as e:
                        print(f"Raid material error ({p.uid}): {e}")
    if win:
        try:
            fam = family_get_by_id(s.fid)
            if fam:
                family_update(s.fid, {
                    "treasury": fam.get("treasury", 0) + int(total_money * RAID_TREASURY_RETURN),
                    "raid_points": fam.get("raid_points", 0) + t["points"],
                })
        except Exception as e:
            print(f"Raid family update error: {e}")
        family_stat_inc(s.fid, "raid_clears")
    return results, total_money


def _raid_refund_sync(s: RaidSession):
    fam = family_get_by_id(s.fid)
    if fam:
        family_update(s.fid, {"treasury": fam.get("treasury", 0) + s.tier["cost"]})


async def run_raid(s: RaidSession):
    from .crafting import format_material_drops   # 순환 import 방지: 호출 시점에 가져옴
    try:
        # ── 1) 모집
        try:
            await asyncio.wait_for(s.start_event.wait(), timeout=RAID_LOBBY_SEC)
        except asyncio.TimeoutError:
            pass

        if s.cancelled or len(s.players) < s.tier["min_players"]:
            s.state = "done"
            try:
                await asyncio.to_thread(_raid_refund_sync, s)
            except Exception as e:
                print(f"Raid refund error: {e}")
            reason = "시작자가 레이드를 취소했습니다." if s.cancelled else f"최소 인원({s.tier['min_players']}명)이 모이지 않았습니다."
            embed = discord.Embed(title="[레이드 취소]", description=f"{reason}\n작전 자금 `${s.tier['cost']:,}` 은(는) 조직 금고로 반환되었습니다.", color=discord.Color.dark_grey())
            await _raid_edit(s, embed=embed, view=None)
            return

        # ── 2) 전투 준비: 보스 체력은 참가자 공격력에 맞춰 자동 계산
        players = list(s.players.values())
        avg_atk = sum(p.atk for p in players) / len(players)
        s.boss_def = int(avg_atk * s.tier["def_ratio"])
        per_hit_total = sum(max(10, p.atk - s.boss_def) for p in players)
        s.boss_max = s.boss_hp = int(per_hit_total * s.tier["hits"])
        s.state = "fighting"
        s.fight_started = True
        now = time.time()
        s.fight_end = now + RAID_DURATION_SEC
        s.next_aoe = now + RAID_AOE_INTERVAL_SEC
        raid_family_cooldowns[s.fid] = datetime.now() + timedelta(seconds=RAID_FAMILY_COOLDOWN_SEC)
        s.log(f"전투 시작! 참가자 {len(players)}명")
        await _raid_edit(s, embed=raid_fight_embed(s), view=RaidFightView(s))

        # ── 3) 전투 루프
        win = False
        reason = ""
        while True:
            try:
                await asyncio.wait_for(s.wake.wait(), timeout=RAID_TICK_SEC)
            except asyncio.TimeoutError:
                pass
            s.wake.clear()
            now = time.time()

            if s.boss_hp <= 0:
                win, reason = True, "보스를 쓰러뜨렸습니다!"
                break
            if not s.enraged and s.boss_hp <= s.boss_max * 0.5:
                s.enraged = True
                s.next_aoe = min(s.next_aoe, now + RAID_AOE_INTERVAL_ENRAGE_SEC)
                s.log("보스가 격노했습니다! 광역 공격이 강해지고 더 자주 옵니다!")
            if now >= s.next_aoe:
                raid_do_aoe(s)
            if not any(p.alive for p in s.players.values()):
                reason = "공격대가 전멸했습니다..."
                break
            if now >= s.fight_end:
                reason = "제한 시간이 지났습니다..."
                break
            await _raid_edit(s, embed=raid_fight_embed(s))

        s.state = "done"

        # ── 4) 정산
        results, total_money = await asyncio.to_thread(_raid_settle_sync, s, win)
        if win:
            title, color = "[레이드 성공]", discord.Color.gold()
        else:
            title, color = "[레이드 실패]", discord.Color.dark_red()
        lines = []
        for p, money, honor in sorted(results, key=lambda r: -r[0].dmg_done):
            _mats = getattr(s, "material_drops", {}).get(p.uid)
            lines.append(f"• **{p.name}** — 딜 {p.dmg_done:,} ({p.hits}회) → `${money:,}`" + (f" / 명예 `+{honor}pt`" if honor else "")
                         + (f"\n  └ 재료: {format_material_drops(_mats)}" if _mats else ""))
        if not lines:
            lines.append("공격에 참여한 사람이 없어 보상이 없습니다.")
        desc = f"{reason}\n\n" + "\n".join(lines)
        if win:
            desc += (f"\n\n조직 금고 `+${int(total_money * RAID_TREASURY_RETURN):,}` / 레이드 점수 `+{s.tier['points']}`"
                     f"\n**MVP:** {max(results, key=lambda r: r[0].dmg_done)[0].member.mention}" if results else "")
        else:
            desc += f"\n\n(실패해도 참전자에게는 소정의 위로금이 지급됩니다.)"
        embed = discord.Embed(title=f"{title} {s.tier['name']}", description=desc, color=color)
        await _raid_edit(s, embed=embed, view=None)
    except Exception as e:
        print(f"Raid error: {e}")
        if not s.fight_started:   # 전투 시작 전에 오류가 났다면 작전비 반환
            try:
                await asyncio.to_thread(_raid_refund_sync, s)
            except Exception:
                pass
        s.state = "done"
        await _raid_edit(s, embed=discord.Embed(title="[레이드 중단]", description="처리 중 오류가 발생해 레이드가 중단되었습니다.", color=discord.Color.red()), view=None)
    finally:
        s.state = "done"
        raid_release(s)


raid_group = app_commands.Group(name="레이드", description="[패밀리] 조직원들과 함께 레이드 보스에 도전합니다.")
bot.tree.add_command(raid_group)

@raid_group.command(name="시작", description="우리 조직의 레이드를 엽니다. (조직 금고에서 작전 자금이 소모됩니다)")
@app_commands.describe(보스="도전할 레이드 보스")
@app_commands.choices(보스=[
    app_commands.Choice(name=f"{t['name']} (인원 {t['min_players']}+ · 명예 {t['min_honor']:,}+ · 작전비 ${t['cost']:,})", value=k)
    for k, t in RAID_TIERS.items()
])
async def raid_start_cmd(interaction: discord.Interaction, 보스: app_commands.Choice[str]):
    if not await _family_precheck(interaction): return
    user = interaction.user
    tier = RAID_TIERS[보스.value]

    try:
        fam = family_get_by_user(interaction.guild_id, user.id)
        if not fam:
            await interaction.response.send_message("소속된 조직이 없습니다. `/조직 생성` 또는 초대를 받아 보세요.", ephemeral=True)
            return
        if fam["id"] in active_raids:
            await interaction.response.send_message("우리 조직의 레이드가 이미 진행 중입니다.", ephemeral=True)
            return
        if user.id in raid_user_map:
            await interaction.response.send_message("이미 레이드에 참가 중입니다.", ephemeral=True)
            return
        cd = raid_family_cooldowns.get(fam["id"])
        if cd and datetime.now() < cd:
            remain = int((cd - datetime.now()).total_seconds())
            await interaction.response.send_message(f"조직이 정비 중입니다. **{remain // 60}분 {remain % 60}초** 뒤에 다시 레이드를 열 수 있습니다.", ephemeral=True)
            return
        if len(fam.get("member_ids") or []) < tier["min_players"]:
            await interaction.response.send_message(f"이 레이드는 조직원이 최소 **{tier['min_players']}명** 이상이어야 합니다.", ephemeral=True)
            return
        if fam.get("treasury", 0) < tier["cost"]:
            await interaction.response.send_message(f"조직 금고가 부족합니다. (작전비 `${tier['cost']:,}` / 금고 `${fam.get('treasury', 0):,}`)\n`/조직 기부` 로 금고를 채워 보세요.", ephemeral=True)
            return
        info = get_user_data(user.id)
        if info["honor"] < tier["min_honor"]:
            await interaction.response.send_message(f"이 레이드는 명예 **{tier['min_honor']:,}pt** 이상만 도전할 수 있습니다.", ephemeral=True)
            return

        # 자리 선점 (여기부터 await 전까지는 다른 명령어가 끼어들 수 없음)
        session = RaidSession(fam, 보스.value, user)
        session.players[user.id] = RaidPlayer(user, compute_combat_stats(info["honor"], info.get("inventory", {})))
        active_raids[fam["id"]] = session
        raid_user_map[user.id] = fam["id"]
        try:
            family_update(fam["id"], {"treasury": fam["treasury"] - tier["cost"]})
        except Exception:
            raid_release(session)
            raise
    except Exception as e:
        print(f"Raid start error: {e}")
        await interaction.response.send_message(FAMILY_DB_ERROR_MSG, ephemeral=True)
        return

    await interaction.response.send_message(embed=raid_lobby_embed(session), view=RaidLobbyView(session))
    session.message = await interaction.original_response()
    keep_task(asyncio.create_task(run_raid(session)))

@raid_group.command(name="정보", description="레이드 보스 목록과 진행 방법을 확인합니다.")
async def raid_info_cmd(interaction: discord.Interaction):
    if not await check_maintenance(interaction): return
    lines = []
    for t in RAID_TIERS.values():
        lines.append(
            f"**{t['name']}**\n"
            f"└ 인원 {t['min_players']}명+ · 명예 {t['min_honor']:,}pt+ · 작전비 `${t['cost']:,}` (조직 금고)\n"
            f"└ 보상(1인 평균) `${t['money']:,}` / 명예 `+{t['honor']}pt` · 조직 점수 `+{t['points']}`"
        )
    embed = discord.Embed(
        title="조직 레이드 안내",
        description=("\n\n".join(lines) + "\n\n"
                     f"**진행:** `/레이드 시작` → 조직원이 `참가` ({RAID_LOBBY_SEC}초) → 전투 {RAID_DURATION_SEC // 60}분\n"
                     "**공격**: 보스 체력을 깎습니다.\n"
                     "**응급처치**: 쓰러진 동료를 되살리거나, 없으면 전원 체력을 회복합니다.\n"
                     f"**전투 함성**: {int(RAID_RALLY_DURATION_SEC)}초 동안 공격대 전원의 공격력 +{int(RAID_RALLY_BONUS * 100)}%! (쿨타임 {int(RAID_RALLY_CD_SEC)}초, 여러 명이 번갈아 쓰면 효과를 계속 유지할 수 있어요)\n"
                     "보스는 체력이 절반이 되면 **격노**하고, 시간 안에 못 잡거나 전멸하면 실패합니다.\n"
                     f"보상은 {int(RAID_BASE_SHARE * 100)}% 균등 + {100 - int(RAID_BASE_SHARE * 100)}% 딜량 비례이며, 한 번도 공격하지 않으면 받을 수 없습니다.\n"
                     f"조직 레이드 재도전 대기시간은 {RAID_FAMILY_COOLDOWN_SEC // 60}분입니다."),
        color=discord.Color.orange()
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)
