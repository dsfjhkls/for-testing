"""공용 구매 확인 창, 수사팀 시설 업그레이드"""

import asyncio
import discord
import threading as _threading

from .police_unit import (
    POLICE_OP_COOLDOWN_SEC,
    POLICE_UNIT_DB_ERROR_MSG,
    POLICE_UNIT_MAX_MEMBERS,
    _unit_precheck,
    police_unit_get_by_id,
    police_unit_get_by_user,
    police_unit_update,
    unit_group,
)

# ==========================================================================
# [공용 구매 확인 창]   (수백만 단위 구매 · 시설 업그레이드가 실수로 눌리지 않게 한 번 더 확인합니다)
# ==========================================================================
class _PurchaseConfirmView(discord.ui.View):
    def __init__(self, user_id: int, on_confirm, timeout: int = 30):
        super().__init__(timeout=timeout)
        self.user_id = user_id
        self.on_confirm = on_confirm
        self.done = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("본인만 누를 수 있습니다.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="구매 확정", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.done:
            return
        self.done = True
        self.stop()
        await interaction.response.defer()
        await self.on_confirm(interaction)

    @discord.ui.button(label="취소", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.done:
            return
        self.done = True
        self.stop()
        await interaction.response.edit_message(content="취소했습니다.", embed=None, view=None)


# ==========================================================================
# [수사팀 시설 업그레이드]   /수사팀 시설
#   • 수사팀 금고($)를 써서 시설을 단계별(Lv.1~5)로 키웁니다. 업그레이드는 팀장만 할 수 있어요.
#   • 시설 레벨은 Supabase `police_units` 테이블의 `upgrades`(jsonb) 컬럼에 저장됩니다.
#       → police_unit_upgrade.sql 을 Supabase 에서 한 번 실행하세요. (컬럼이 없어도 봇은 정상 동작하고, 업그레이드만 안 됩니다)
#   • 가격(costs)·효과(per)는 아래 표에서 자유롭게 바꾸세요. costs 의 길이가 최고 레벨입니다.
# ==========================================================================
POLICE_UNIT_UPGRADE_DB_MSG = ("시설 업그레이드 저장에 실패했습니다. "
                              "(관리자: `police_units` 테이블에 `upgrades` 컬럼이 있는지 확인해 주세요 — police_unit_upgrade.sql 참고)")

POLICE_UNIT_FACILITIES = {
    "range": {"name": "사격 훈련장",     "per": 2, "effect": "작전 단계 성공률 +{v}%p",
              "desc": "합동 소탕 작전의 단계별 성공률을 올립니다.",
              "costs": [500_000, 1_000_000, 2_000_000, 3_500_000, 5_000_000]},
    "lab":   {"name": "과학수사 연구소", "per": 6, "effect": "작전 보상금 +{v}%",
              "desc": "합동 소탕 작전의 참가자 보상금이 늘어납니다.",
              "costs": [600_000, 1_200_000, 2_400_000, 4_000_000, 6_000_000]},
    "hq":    {"name": "지휘 통제실",     "per": 6, "effect": "작전 정비 대기시간 -{v}%",
              "desc": "같은 수사팀이 작전을 다시 열 수 있기까지의 대기시간이 줄어듭니다.",
              "costs": [700_000, 1_400_000, 2_800_000, 4_500_000, 7_000_000]},
    "annex": {"name": "청사 증축",       "per": 1, "effect": "수사팀 정원 +{v}명",
              "desc": "수사팀에 들어올 수 있는 인원이 늘어납니다.",
              "costs": [800_000, 1_600_000, 3_200_000, 5_000_000, 8_000_000]},
}

_unit_upgrade_lock = _threading.Lock()   # 팀장이 연달아 눌러도 금고가 이중으로 빠지지 않게 직렬화


def unit_upgrades(unit) -> dict:
    """수사팀 행에서 시설 레벨을 읽습니다. (컬럼이 없거나 비어 있으면 전부 0)"""
    raw = (unit or {}).get("upgrades")
    if not isinstance(raw, dict):
        raw = {}
    out = {}
    for key, fac in POLICE_UNIT_FACILITIES.items():
        try:
            lv = int(raw.get(key, 0) or 0)
        except (TypeError, ValueError):
            lv = 0
        out[key] = max(0, min(lv, len(fac["costs"])))
    return out


def unit_max_members(unit) -> int:
    return POLICE_UNIT_MAX_MEMBERS + unit_upgrades(unit)["annex"] * POLICE_UNIT_FACILITIES["annex"]["per"]


def unit_op_cooldown_sec(ups: dict) -> int:
    cut = POLICE_UNIT_FACILITIES["hq"]["per"] * (ups or {}).get("hq", 0)
    return max(60, int(POLICE_OP_COOLDOWN_SEC * (100 - cut) / 100))


def unit_facility_summary(unit) -> str:
    ups = unit_upgrades(unit)
    parts = [f"{POLICE_UNIT_FACILITIES[k]['name']} Lv.{lv}" for k, lv in ups.items() if lv > 0]
    return " · ".join(parts)


def unit_facility_embed(unit: dict) -> discord.Embed:
    ups = unit_upgrades(unit)
    embed = discord.Embed(
        title=f"수사팀 「{unit['name']}」 시설",
        description=(f"**수사팀 금고:** `${int(unit.get('treasury') or 0):,}`\n"
                     f"시설은 금고에서 비용이 나가며, **팀장**만 업그레이드할 수 있습니다. (`/수사팀 기부` 로 금고를 채우세요)"),
        color=discord.Color.blue()
    )
    for key, fac in POLICE_UNIT_FACILITIES.items():
        lv, mx = ups[key], len(fac["costs"])
        bar = "■" * lv + "□" * (mx - lv)
        now_eff = fac["effect"].format(v=fac["per"] * lv) if lv else "효과 없음"
        nxt = (f"다음 Lv.{lv + 1}: `${fac['costs'][lv]:,}` ({fac['effect'].format(v=fac['per'] * (lv + 1))})"
               if lv < mx else "**최고 레벨**")
        embed.add_field(name=f"{fac['name']}  {bar}  Lv.{lv}/{mx}",
                        value=f"{fac['desc']}\n현재: {now_eff}\n{nxt}", inline=False)
    return embed


def police_unit_upgrade_sync(unit_id, user_id: int, key: str):
    """시설 1단계 업그레이드. 반환: ("ok", unit, 새 레벨, 비용) / ("no_money", 금고, 비용) / ("maxed",) / ("not_owner",) / ("no_unit",)"""
    fac = POLICE_UNIT_FACILITIES[key]
    with _unit_upgrade_lock:
        unit = police_unit_get_by_id(unit_id)
        if not unit:
            return ("no_unit",)
        if unit["owner_id"] != str(user_id):
            return ("not_owner",)
        ups = unit_upgrades(unit)
        lv = ups[key]
        if lv >= len(fac["costs"]):
            return ("maxed",)
        cost = fac["costs"][lv]
        treasury = int(unit.get("treasury") or 0)
        if treasury < cost:
            return ("no_money", treasury, cost)
        ups[key] = lv + 1
        # 금고 차감과 레벨 상승을 한 번의 저장으로 처리 (컬럼이 없으면 여기서 실패 → 금고는 그대로)
        police_unit_update(unit_id, {"treasury": treasury - cost, "upgrades": ups})
        unit["treasury"] = treasury - cost
        unit["upgrades"] = ups
        return ("ok", unit, lv + 1, cost)


class UnitFacilitySelect(discord.ui.Select):
    def __init__(self, owner: discord.Member, unit: dict):
        self.owner = owner
        self.unit = unit
        ups = unit_upgrades(unit)
        options = []
        for key, fac in POLICE_UNIT_FACILITIES.items():
            lv = ups[key]
            if lv >= len(fac["costs"]):
                continue
            options.append(discord.SelectOption(
                label=f"{fac['name']} → Lv.{lv + 1}",
                description=f"${fac['costs'][lv]:,} · {fac['effect'].format(v=fac['per'] * (lv + 1))}"[:100],
                value=key))
        super().__init__(placeholder="업그레이드할 시설을 선택하세요...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner.id:
            await interaction.response.send_message("시설 업그레이드는 팀장만 할 수 있습니다.", ephemeral=True)
            return
        key = self.values[0]
        fac = POLICE_UNIT_FACILITIES[key]
        lv = unit_upgrades(self.unit)[key]
        if lv >= len(fac["costs"]):
            await interaction.response.send_message("이미 최고 레벨입니다.", ephemeral=True)
            return
        cost = fac["costs"][lv]
        unit_id, unit_name = self.unit["id"], self.unit["name"]

        async def do_upgrade(inter: discord.Interaction):
            try:
                res = await asyncio.to_thread(police_unit_upgrade_sync, unit_id, inter.user.id, key)
            except Exception as e:
                print(f"Police unit upgrade error: {e}")
                await inter.edit_original_response(content=POLICE_UNIT_UPGRADE_DB_MSG, embed=None, view=None)
                return
            tag = res[0]
            if tag == "ok":
                _, unit, new_lv, paid = res
                embed = discord.Embed(
                    title="[시설 확장 완료]",
                    description=(f"**{fac['name']}** 이(가) **Lv.{new_lv}** 이(가) 되었습니다!\n"
                                 f"**효과:** {fac['effect'].format(v=fac['per'] * new_lv)}\n\n"
                                 f"**차감 비용:** `${paid:,}`\n**남은 금고:** `${int(unit.get('treasury') or 0):,}`"),
                    color=discord.Color.green())
                await inter.edit_original_response(content=None, embed=embed, view=None)
                try:
                    await inter.channel.send(f"수사팀 **「{unit_name}」** 이(가) **{fac['name']}** 을(를) **Lv.{new_lv}** 로 확장했습니다!")
                except Exception:
                    pass
            elif tag == "no_money":
                await inter.edit_original_response(
                    content=f"수사팀 금고가 부족합니다. (필요 `${res[2]:,}` / 금고 `${res[1]:,}`)\n`/수사팀 기부` 로 금고를 채워 주세요.",
                    embed=None, view=None)
            elif tag == "maxed":
                await inter.edit_original_response(content="이미 최고 레벨입니다.", embed=None, view=None)
            elif tag == "not_owner":
                await inter.edit_original_response(content="팀장만 시설을 업그레이드할 수 있습니다.", embed=None, view=None)
            else:
                await inter.edit_original_response(content="수사팀이 더 이상 존재하지 않습니다.", embed=None, view=None)

        confirm = discord.Embed(
            title="[시설 업그레이드 확인]",
            description=(f"**{fac['name']}** Lv.{lv} → **Lv.{lv + 1}**\n"
                         f"**효과:** {fac['effect'].format(v=fac['per'] * (lv + 1))}\n"
                         f"**비용:** `${cost:,}` (수사팀 금고에서 차감)"),
            color=discord.Color.gold())
        await interaction.response.send_message(embed=confirm, view=_PurchaseConfirmView(interaction.user.id, do_upgrade), ephemeral=True)


class UnitFacilityView(discord.ui.View):
    def __init__(self, owner: discord.Member, unit: dict):
        super().__init__(timeout=180)
        self.add_item(UnitFacilitySelect(owner, unit))


@unit_group.command(name="시설", description="수사팀 시설을 확인하고, 팀장이 금고로 업그레이드합니다.")
async def unit_facility_cmd(interaction: discord.Interaction):
    if not await _unit_precheck(interaction): return
    try:
        unit = await asyncio.to_thread(police_unit_get_by_user, interaction.guild_id, interaction.user.id)
    except Exception as e:
        print(f"Police unit facility error: {e}")
        await interaction.response.send_message(POLICE_UNIT_DB_ERROR_MSG, ephemeral=True)
        return
    if not unit:
        await interaction.response.send_message("소속된 수사팀이 없습니다. `/수사팀 생성` 또는 초대를 받아 보세요.", ephemeral=True)
        return

    embed = unit_facility_embed(unit)
    kwargs = {}
    if unit["owner_id"] == str(interaction.user.id):
        if any(lv < len(POLICE_UNIT_FACILITIES[k]["costs"]) for k, lv in unit_upgrades(unit).items()):
            kwargs["view"] = UnitFacilityView(interaction.user, unit)
    else:
        embed.set_footer(text="시설 업그레이드는 팀장만 할 수 있습니다.")
    await interaction.response.send_message(embed=embed, **kwargs)
