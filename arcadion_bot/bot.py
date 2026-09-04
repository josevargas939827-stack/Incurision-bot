from __future__ import annotations

import asyncio
import json
import random
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands

from .config import load_settings
from .game import (
    ModifierType,
    RaidState,
    UNIT_LABELS,
    UNIT_VALUES,
    Units,
    apply_wounded_combat_damage,
    combat_health_from_row,
    combat_power_from_row,
    apply_damage_to_units,
    calculate_loot_distribution,
    determine_arcadion_phase,
    format_number,
    format_units,
    parse_integer,
    resolve_surprise_attack,
    roll_arcadion_counterattack_dice,
    roll_dice,
)
from .storage import Store, utc_now

ARCADION_THREAT_LINES = [
    "Their resistance is futile.",
    "Corruption will consume this squad.",
    "They will not stop me.",
    "Their technology will be destroyed.",
    "There is no hope for you.",
]

TURN_TIME_LIMIT_SECONDS = 120
TURN_REMINDER_SECONDS = (60,)
TURN_INACTIVITY_TIMEOUTS_BEFORE_KICK = 2
ARCADION_DICE_MIN = 1
ARCADION_DICE_MAX = 5
MECHA_BONUS_DEFINITIONS = {
    "mecha_lion": [
        ("Burst", 10000, "damage", "Deals 10,000 bonus damage. Effectiveness: 50%."),
        ("Metallic Scratch", 30000, "damage", "Deals 30,000 bonus damage. Effectiveness: 40%."),
    ],
    "mecha_eagle": [
        ("Storm", 20000, "damage", "Deals 20,000 bonus damage. Effectiveness: 50%."),
        ("High Flight", 0, "avoid_bonus_attack", "Avoids Arcadion's special bonus attack."),
    ],
    "mecha_dolphin": [
        ("Aquatic Repair", 0, "heal", "Restores 60% battle power to an allied commander."),
        ("Aquatic Projectile", 50000, "damage", "Deals 50,000 bonus damage. Effectiveness: 40%."),
    ],
    "mecha_tiger": [
        ("Super Bite", 25000, "damage", "Deals 25,000 bonus damage. Mecha Tiger loses 5,000 Military Power."),
        ("Stealth", 0, "avoid_bonus_attack", "Avoids Arcadion's special bonus attack."),
    ],
    "mecha_bull": [
        ("Deadly Charge", 40000, "bypass_guard", "Deals 40,000 bonus damage and ignores physical protection."),
        ("Last Stand", 0, "redirect_bonus_attack", "Redirects Arcadion's special bonus attack to the Mecha Bull."),
    ],
    "mecha_black_lion": [
        ("Rockets", 30000, "damage", "Deals 30,000 bonus damage. Effectiveness: 40%."),
        ("Speed", 25000, "damage", "Deals 25,000 bonus damage. Effectiveness: 60%."),
    ],
    "mecha_shark": [
        ("Waves", 30000, "damage", "Deals 30,000 bonus damage. Effectiveness: 80%."),
        ("Sharp Teeth", 50000, "damage", "Deals 50,000 bonus damage. Effectiveness: 40%."),
    ],
}
MECHA_BONUS_ORDER = (
    "mecha_lion",
    "mecha_eagle",
    "mecha_dolphin",
    "mecha_tiger",
    "mecha_bull",
    "mecha_black_lion",
    "mecha_shark",
)
ARMY_VARIANT_LABELS = {
    "general_dolphin": "General Dolphin",
    "general_eagle": "General Eagle",
    "mecha_lion": "Mecha Lion",
    "mecha_eagle": "Mecha Eagle",
    "mecha_dolphin": "Mecha Dolphin",
    "mecha_tiger": "Mecha Tiger",
    "mecha_bull": "Mecha Bull",
    "mecha_black_lion": "Black Lion Mecha",
    "mecha_shark": "Mecha Shark",
}
ARMY_VARIANT_GROUPS = {
    "general_dolphin": "generals",
    "general_eagle": "generals",
    "mecha_lion": "mechas",
    "mecha_eagle": "mechas",
    "mecha_dolphin": "mechas",
    "mecha_tiger": "mechas",
    "mecha_bull": "mechas",
    "mecha_black_lion": "mechas",
    "mecha_shark": "mechas",
}
ARMY_VARIANT_ORDER = (
    "general_dolphin",
    "general_eagle",
    "mecha_lion",
    "mecha_eagle",
    "mecha_dolphin",
    "mecha_tiger",
    "mecha_bull",
    "mecha_black_lion",
    "mecha_shark",
)


class ArcadionBot(commands.Bot):
    def __init__(self, store: Store, guild_id: int | None) -> None:
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)
        self.store = store
        self.guild_id = guild_id
        self.raid_leaders: dict[int, int] = {}
        self.turn_timeout_tasks: dict[int, asyncio.Task[None]] = {}
        self.turn_inactivity_strikes: dict[tuple[int, int], int] = {}

    async def setup_hook(self) -> None:
        self.store.init()
        register_commands(self)
        print(f'GUILD_ID: {self.guild_id}')
        print(f'Registered commands: {len(self.tree.get_commands())}')
        self.loop.create_task(self.manage_turn_notifications())
        try:
            global_commands = await self.tree.sync()
            print(f'Global commands synced: {len(global_commands)}')
        except discord.Forbidden as exc:
            print(f'Global command sync failed: 403 Forbidden / Missing Access: {exc}')
            print('Bot startup will continue despite global sync failure.')
        except discord.HTTPException as exc:
            print(f'Global command sync failed: HTTP {exc.status}: {exc}')
            print('Bot startup will continue despite global sync failure.')

    async def manage_turn_notifications(self) -> None:
        while True:
            await asyncio.sleep(10)
            try:
                raid = self.store.get_active_raid()
                if raid is None or raid["state"] != RaidState.BATTLE.value:
                    continue
                if not raid["current_turn_discord_id"] or not raid["turn_deadline_at"]:
                    continue
                deadline = datetime.fromisoformat(raid["turn_deadline_at"])
                remaining_seconds = (deadline - datetime.now(timezone.utc)).total_seconds()
                channel = self._get_raid_channel(raid)
                reminder_state = raid["turn_reminder_state"] or "NONE"
                if remaining_seconds <= TURN_REMINDER_SECONDS[0] and reminder_state == "NONE":
                    self.store.set_turn_reminder_state(raid["id"], "ONE_MINUTE")
                    if channel is not None:
                        await announce_turn_reminder(self, raid, channel, 1)
            except Exception:
                continue

    def _get_raid_channel(self, raid: object) -> discord.abc.Messageable | None:
        channel_id = raid["announcement_channel_id"] if raid["announcement_channel_id"] else None
        if not channel_id:
            return None
        channel = self.get_channel(int(channel_id))
        return channel if isinstance(channel, discord.abc.Messageable) else None

    def _channel_id_for_raid(self, raid: object) -> int | None:
        channel_id = raid["announcement_channel_id"] if raid["announcement_channel_id"] else None
        if not channel_id:
            return None
        return int(channel_id)

    def set_raid_leader(self, raid_id: int, leader_id: int) -> None:
        self.raid_leaders[raid_id] = leader_id

    def get_raid_leader(self, raid_id: int) -> int | None:
        return self.raid_leaders.get(raid_id)

    def clear_raid_leader(self, raid_id: int) -> None:
        self.raid_leaders.pop(raid_id, None)

    def is_raid_admin(self, raid: object, user: discord.abc.User) -> bool:
        raid_admin_id = raid["raid_admin_discord_id"] if "raid_admin_discord_id" in raid.keys() else None
        return bool(user.guild_permissions.manage_guild) or str(user.id) == str(raid_admin_id)

    def cancel_turn_timeout(self, raid_id: int) -> None:
        task = self.turn_timeout_tasks.pop(raid_id, None)
        if task is not None:
            task.cancel()

    def start_turn_timeout(self, raid_id: int, discord_id: int | str, channel_id: int | None) -> None:
        self.cancel_turn_timeout(raid_id)
        self.turn_timeout_tasks[raid_id] = self.loop.create_task(self._run_turn_timeout(raid_id, int(discord_id), channel_id))

    async def _run_turn_timeout(self, raid_id: int, discord_id: int, channel_id: int | None) -> None:
        try:
            await asyncio.sleep(TURN_TIME_LIMIT_SECONDS)
            raid = self.store.get_raid(raid_id)
            if raid is None or raid["state"] != RaidState.BATTLE.value:
                return
            if str(raid["current_turn_discord_id"] or "") != str(discord_id):
                return
            if not raid["turn_deadline_at"]:
                return
            deadline = datetime.fromisoformat(raid["turn_deadline_at"])
            if datetime.now(timezone.utc) < deadline:
                return
            await self.handle_inactivity_timeout(raid_id, discord_id, channel_id)
        except asyncio.CancelledError:
            return
        finally:
            current = self.turn_timeout_tasks.get(raid_id)
            if current is not None and current.done():
                self.turn_timeout_tasks.pop(raid_id, None)

    async def handle_inactivity_timeout(self, raid_id: int, discord_id: int, channel_id: int | None) -> None:
        raid = self.store.get_raid(raid_id)
        if raid is None or raid["state"] != RaidState.BATTLE.value:
            return
        if str(raid["current_turn_discord_id"] or "") != str(discord_id):
            return
        if not raid["turn_deadline_at"]:
            return
        deadline = datetime.fromisoformat(raid["turn_deadline_at"])
        if datetime.now(timezone.utc) < deadline:
            return

        key = (raid_id, discord_id)
        strikes = self.turn_inactivity_strikes.get(key, 0) + 1
        self.turn_inactivity_strikes[key] = strikes
        channel = self._get_raid_channel(raid)
        current_player = self.store.get_participant(raid_id, discord_id)

        if strikes >= TURN_INACTIVITY_TIMEOUTS_BEFORE_KICK:
            await self._kick_inactive_participant(raid_id, discord_id, channel_id)
            if channel is not None:
                if current_player is not None:
                    await channel.send(
                        f"?? INACTIVE COMMANDER REMOVED\n\n<@{current_player['discord_id']}> missed too many turns in this raid and has been removed from battle."
                    )
                else:
                    await channel.send("?? INACTIVE COMMANDER REMOVED\n\nA commander has been removed from battle after repeated inactivity.")
            updated_raid = self.store.get_raid(raid_id)
            if updated_raid is not None and channel is not None:
                await announce_turn_change(self, updated_raid, channel)
            return

        self.store.process_turn_timeout(raid_id, channel_id)
        updated_raid = self.store.get_raid(raid_id)
        if channel is not None:
            if current_player is not None:
                await channel.send(
                    f"?? TURN MISSED\n\n<@{current_player['discord_id']}> failed to act in time.\n\nArcadion sensed the hesitation and launched a surprise attack."
                )
            else:
                await channel.send("?? TURN MISSED\n\nArcadion sensed the hesitation and launched a surprise attack.")
            if updated_raid is not None:
                await announce_turn_change(self, updated_raid, channel)

    async def _kick_inactive_participant(self, raid_id: int, discord_id: int, channel_id: int | None) -> None:
        with self.store.connect() as conn:
            raid = conn.execute("SELECT * FROM raids WHERE id = ?", (raid_id,)).fetchone()
            if raid is None:
                return
            original_turn_order = json.loads(raid["turn_order"] or "[]")
            current_turn = raid["current_turn_discord_id"]
            is_current_player = str(current_turn) == str(discord_id)
            current_index = original_turn_order.index(str(discord_id)) if str(discord_id) in original_turn_order else -1
            turn_order = [current_id for current_id in original_turn_order if str(current_id) != str(discord_id)]
            conn.execute(
                "DELETE FROM raid_participants WHERE raid_id = ? AND discord_id = ?",
                (raid_id, str(discord_id)),
            )
            if is_current_player:
                next_id = None
                next_index = None
                if turn_order:
                    search_order = []
                    if current_index >= 0:
                        search_order.extend(original_turn_order[current_index + 1 :])
                        search_order.extend(original_turn_order[:current_index])
                    else:
                        search_order.extend(original_turn_order)
                    for candidate_id in search_order:
                        if candidate_id in turn_order:
                            next_id = candidate_id
                            next_index = turn_order.index(candidate_id)
                            break
                if next_id is not None and next_index is not None:
                    round_number = int(raid["turn_round"] or 1)
                    if current_index >= 0 and next_index < current_index:
                        round_number += 1
                    conn.execute(
                        "UPDATE raids SET turn_order = ?, current_turn_discord_id = ?, turn_index = ?, turn_round = ?, turn_started_at = ?, turn_deadline_at = ?, announcement_channel_id = ?, turn_reminder_state = 'NONE' WHERE id = ?",
                        (json.dumps(turn_order), next_id, next_index, round_number, utc_now(), (datetime.now(timezone.utc) + timedelta(seconds=TURN_TIME_LIMIT_SECONDS)).isoformat(), str(channel_id) if channel_id is not None else None, raid_id),
                    )
                else:
                    conn.execute(
                        "UPDATE raids SET turn_order = ?, current_turn_discord_id = NULL, turn_started_at = NULL, turn_deadline_at = NULL, turn_index = 0, turn_round = ?, announcement_channel_id = ?, turn_reminder_state = 'NONE' WHERE id = ?",
                        (json.dumps([]), int(raid["turn_round"] or 1), str(channel_id) if channel_id is not None else None, raid_id),
                    )
                    self.clear_raid_leader(raid_id)
            else:
                conn.execute(
                    "UPDATE raids SET turn_order = ?, announcement_channel_id = ?, turn_reminder_state = 'NONE' WHERE id = ?",
                    (json.dumps(turn_order), str(channel_id) if channel_id is not None else None, raid_id),
                )
                if current_turn is not None and str(current_turn) not in turn_order:
                    if turn_order:
                        conn.execute(
                            "UPDATE raids SET current_turn_discord_id = ?, turn_started_at = ?, turn_deadline_at = ?, turn_reminder_state = 'NONE' WHERE id = ?",
                            (turn_order[0], utc_now(), (datetime.now(timezone.utc) + timedelta(seconds=TURN_TIME_LIMIT_SECONDS)).isoformat(), raid_id),
                        )
                    else:
                        conn.execute(
                            "UPDATE raids SET current_turn_discord_id = NULL, turn_started_at = NULL, turn_deadline_at = NULL, turn_index = 0, turn_round = ? WHERE id = ?",
                            (int(raid["turn_round"] or 1), raid_id),
                        )
                        self.clear_raid_leader(raid_id)


def select_raid_join_units(units: Units, power_limit: int) -> Units:
    limit = max(0, int(power_limit))
    if limit <= 0:
        return units

    total_power = units.power()
    if total_power <= limit:
        return units

    pool: list[str] = []
    for name, amount in units.as_dict().items():
        pool.extend([name] * int(amount))

    best_pick = Units()
    best_power = 0
    attempts = max(20, len(pool) * 3)
    for _ in range(attempts):
        shuffled = list(pool)
        random.shuffle(shuffled)
        picked = {name: 0 for name in units.as_dict()}
        current_power = 0
        for name in shuffled:
            value = UNIT_VALUES[name]
            if current_power + value <= limit:
                picked[name] += 1
                current_power += value
        if current_power > best_power:
            best_power = current_power
            best_pick = Units(**picked)
            if best_power == limit:
                break

    return best_pick if best_power > 0 else units


def units_from_args(bulls: int | str, rhinos: int | str, lieutenants: int | str, generals: int | str, mechas: int | str) -> Units:
    values = tuple(parse_integer(value) for value in (bulls, rhinos, lieutenants, generals, mechas))
    if any(value < 0 for value in values):
        raise ValueError("Unit amounts cannot be negative.")
    return Units(bulls=values[0], rhinos=values[1], lieutenants=values[2], generals=values[3], mechas=values[4])


def arcadion_target_power(level: object) -> int | None:
    normalized = str(level or "").strip().lower()
    targets = {
        "1": 450000,
        "minor": 450000,
        "arcadion minor": 450000,
        "menor": 450000,
        "arcadion menor": 450000,
        "2": 700000,
        "supreme": 700000,
        "arcadion supreme": 700000,
        "supremo": 700000,
        "arcadion supremo": 700000,
        "3": 1250000,
        "mecha commander": 1250000,
        "arcadion mecha commander": 1250000,
    }
    return targets.get(normalized)


def parse_unit_change_args(
    bull: str | None = None,
    rhino: str | None = None,
    lieutenant: str | None = None,
    general: str | None = None,
    mecha: str | None = None,
) -> Units:
    raw_values: dict[str, str | None] = {
        "bulls": bull,
        "rhinos": rhino,
        "lieutenants": lieutenant,
        "generals": general,
        "mechas": mecha,
    }

    values: dict[str, int] = {}
    for field, raw_value in raw_values.items():
        if raw_value is None or str(raw_value).strip() == "":
            continue
        amount = parse_integer(raw_value)
        if amount < 0:
            raise ValueError("Unit amounts cannot be negative.")
        values[field] = amount

    if not values:
        raise ValueError("Provide at least one unit change.")

    return Units(**values)


def format_unit_change_message(action: str, delta: Units, current: Units) -> str:
    changes = []
    for field, label in (
        ("bulls", UNIT_LABELS["bulls"]),
        ("rhinos", UNIT_LABELS["rhinos"]),
        ("lieutenants", UNIT_LABELS["lieutenants"]),
        ("generals", UNIT_LABELS["generals"]),
        ("mechas", UNIT_LABELS["mechas"]),
    ):
        amount = getattr(delta, field)
        if amount:
            prefix = "+" if action == "Added" else "-"
            changes.append(f"{prefix}{amount} {label}{'' if amount == 1 else 's'}")

    if not changes:
        changes.append("No units changed")

    return "\n".join(
        [
            "Army successfully updated.",
            "",
            f"{action}:",
            *changes,
            "",
            "Current Army",
            format_units(current),
            "",
            "Military Power",
            f"{format_number(current.power())}",
        ]
    )


def army_variant_label(unit_variant: str) -> str:
    return ARMY_VARIANT_LABELS.get(unit_variant, unit_variant.replace("_", " ").title())


def army_variant_emoji(unit_variant: str) -> str:
    if unit_variant.startswith("general_"):
        return "🐬" if unit_variant.endswith("dolphin") else "🦅"
    if unit_variant.startswith("mecha_"):
        return {
            "mecha_lion": "🦁",
            "mecha_eagle": "🦅",
            "mecha_dolphin": "🐬",
            "mecha_tiger": "🐯",
            "mecha_bull": "🐂",
            "mecha_black_lion": "🦁",
            "mecha_shark": "🦈",
        }.get(unit_variant, "🤖")
    return "•"


def summarize_player_variants(variants: dict[str, int]) -> dict[str, dict[str, int]]:
    summary: dict[str, dict[str, int]] = {"generals": {}, "mechas": {}}
    for variant_key in ARMY_VARIANT_ORDER:
        group = ARMY_VARIANT_GROUPS[variant_key]
        summary[group][variant_key] = int(variants.get(variant_key, 0))
    return summary


def format_variant_summary(variants: dict[str, int], totals: Units) -> str:
    grouped = summarize_player_variants(variants)
    lines = [
        "CLASSIC ARMY VARIANT SETUP",
        "",
        f"Your current army contains:",
        f"Generals: {totals.generals}",
        f"Mechas: {totals.mechas}",
        "",
        f"General variants configured: {sum(grouped['generals'].values())} / {totals.generals}",
        f"Mecha variants configured: {sum(grouped['mechas'].values())} / {totals.mechas}",
        "",
        "GENERAL VARIANTS",
    ]
    for key in ("general_dolphin", "general_eagle"):
        lines.append(f"{army_variant_emoji(key)} {army_variant_label(key)}: {grouped['generals'][key]}")
    lines.extend(["", "MECHA VARIANTS"])
    for key in (
        "mecha_lion",
        "mecha_eagle",
        "mecha_dolphin",
        "mecha_tiger",
        "mecha_bull",
        "mecha_black_lion",
        "mecha_shark",
    ):
        lines.append(f"{army_variant_emoji(key)} {army_variant_label(key)}: {grouped['mechas'][key]}")
    return "\n".join(lines)


def register_commands(bot: ArcadionBot) -> None:
    @bot.tree.command(name="army_set", description="Register or update your permanent army.")
    async def army_set(interaction: discord.Interaction, bulls: str = "0", rhinos: str = "0", lieutenants: str = "0", generals: str = "0", mechas: str = "0") -> None:
        try:
            units = units_from_args(bulls, rhinos, lieutenants, generals, mechas)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        bot.store.upsert_player(interaction.user.id, interaction.user.display_name, units)
        await interaction.response.send_message(
            embed=army_embed(interaction.user.display_name, units, "Permanent army updated"),
            ephemeral=True,
        )

    @bot.tree.command(name="army_view", description="Show a player's permanent army.")
    async def army_view(interaction: discord.Interaction, user: discord.Member | None = None) -> None:
        member = user or interaction.user
        row = bot.store.get_player(member.id)
        if row is None:
            await interaction.response.send_message("That player does not have a registered army yet.", ephemeral=True)
            return
        variants = bot.store.get_player_unit_variants(member.id)
        embed = army_embed(row["discord_name"], Units.from_row(row), "Permanent army")
        if variants:
            embed = army_embed_with_variants(row["discord_name"], Units.from_row(row), "Permanent army", variants)
        await interaction.response.send_message(embed=embed)

    @bot.tree.command(name="add_units", description="Add units to your permanent army inventory.")
    async def add_units(
        interaction: discord.Interaction,
        bull: str | None = None,
        rhino: str | None = None,
        lieutenant: str | None = None,
        general: str | None = None,
        mecha: str | None = None,
    ) -> None:
        try:
            delta = parse_unit_change_args(bull, rhino, lieutenant, general, mecha)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        if not delta.has_any():
            await interaction.response.send_message("Provide at least one unit change.", ephemeral=True)
            return

        if bot.store.add_player_units(interaction.user.id, delta):
            current = bot.store.get_player(interaction.user.id)
            current_units = Units.from_row(current) if current is not None else Units()
            await interaction.response.send_message(format_unit_change_message("Added", delta, current_units))
        else:
            await interaction.response.send_message("Could not update your permanent army.", ephemeral=True)

    @bot.tree.command(name="remove_units", description="Remove units from your permanent army inventory.")
    async def remove_units(
        interaction: discord.Interaction,
        bull: str | None = None,
        rhino: str | None = None,
        lieutenant: str | None = None,
        general: str | None = None,
        mecha: str | None = None,
    ) -> None:
        try:
            delta = parse_unit_change_args(bull, rhino, lieutenant, general, mecha)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        current = bot.store.get_player(interaction.user.id)
        if current is None:
            await interaction.response.send_message("You do not have a registered army yet.", ephemeral=True)
            return

        current_units = Units.from_row(current)
        if not current_units.contains(delta):
            await interaction.response.send_message("? You do not own enough units.", ephemeral=True)
            return

        if bot.store.remove_player_units(interaction.user.id, delta):
            updated = bot.store.get_player(interaction.user.id)
            updated_units = Units.from_row(updated) if updated is not None else Units()
            await interaction.response.send_message(format_unit_change_message("Removed", delta, updated_units))
        else:
            await interaction.response.send_message("? You do not own enough units.", ephemeral=True)

    @bot.tree.command(name="army_variants", description="Configure exact General and Mecha variants for your permanent army.")
    async def army_variants(interaction: discord.Interaction) -> None:
        player = bot.store.get_player(interaction.user.id)
        if player is None:
            await interaction.response.send_message("Register your army with `/army_set` first.", ephemeral=True)
            return
        totals = Units.from_row(player)
        variants = bot.store.get_player_unit_variants(interaction.user.id)
        view = ArmyVariantSetupView(bot, interaction.user.id, totals, variants)
        await interaction.response.send_message(
            content=view._message(),
            view=view,
            ephemeral=True,
        )

    @bot.tree.command(name="arcadion_create", description="Create an Arcadion raid in recruitment.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def arcadion_create(
        interaction: discord.Interaction,
        name: str,
        city: str,
        level: str,
        max_corruption: str,
        duration_hours: str,
        arcadion_bulls: str = "0",
        arcadion_rhinos: str = "0",
        arcadion_lieutenants: str = "0",
        arcadion_generals: str = "0",
        arcadion_mechas: str = "0",
        total_loot_upx: str = "0",
        power_limit: str = "0",
    ) -> None:
        try:
            max_corruption_value = parse_integer(max_corruption)
            duration_hours_value = parse_integer(duration_hours)
            total_loot_value = parse_integer(total_loot_upx)
            power_limit_value = parse_integer(power_limit)
            arcadion_units = units_from_args(arcadion_bulls, arcadion_rhinos, arcadion_lieutenants, arcadion_generals, arcadion_mechas)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        if max_corruption_value <= 0 or duration_hours_value <= 0:
            await interaction.response.send_message("Maximum corruption and duration must be greater than 0.", ephemeral=True)
            return
        active = bot.store.get_active_raid()
        if active:
            await interaction.response.send_message("There is already an active or recruiting raid.", ephemeral=True)
            return
        raid_id = bot.store.create_raid(name, city, level, interaction.user.id, max_corruption_value, duration_hours_value, arcadion_units, total_loot_value, power_limit_value)
        bot.set_raid_leader(raid_id, interaction.user.id)
        raid = bot.store.get_raid(raid_id)
        await interaction.response.send_message(embed=raid_created_embed(raid))

    @bot.tree.command(name="arcadion_army", description="Configure the active raid's Arcadion army.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def arcadion_army(
        interaction: discord.Interaction,
        bulls: str = "0",
        rhinos: str = "0",
        lieutenants: str = "0",
        generals: str = "0",
        mechas: str = "0",
    ) -> None:
        raid = bot.store.get_active_raid()
        if raid is None:
            await interaction.response.send_message("There is no active raid to configure.", ephemeral=True)
            return
        if not bot.is_raid_admin(raid, interaction.user):
            await interaction.response.send_message("You do not have permission to configure the Arcadion army.", ephemeral=True)
            return
        if raid["state"] != RaidState.RECRUITING.value:
            await interaction.response.send_message("The Arcadion army can only be configured before the raid starts.", ephemeral=True)
            return
        try:
            units = units_from_args(bulls, rhinos, lieutenants, generals, mechas)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        bot.store.update_arcadion_units(raid["id"], units)
        updated = bot.store.get_raid(raid["id"])
        await interaction.response.send_message(embed=arcadion_army_embed(updated), ephemeral=True)

    @bot.tree.command(name="raid_join", description="Send troops to the recruiting raid.")
    async def raid_join(interaction: discord.Interaction) -> None:
        raid = bot.store.get_active_raid()
        if raid is None or raid["state"] != RaidState.RECRUITING.value:
            await interaction.response.send_message("There is no raid in recruitment.", ephemeral=True)
            return

        participant = bot.store.get_participant(raid["id"], interaction.user.id)
        if participant is not None and participant["status"] == "ELIMINATED":
            await interaction.response.send_message("? You cannot rejoin this raid after being eliminated.", ephemeral=True)
            return

        player = bot.store.get_player(interaction.user.id)
        if player is None:
            await interaction.response.send_message("Register your army with `/army_set` first.", ephemeral=True)
            return

        permanent = Units.from_row(player)
        if not permanent.has_any():
            await interaction.response.send_message("Register your army with `/army_set` first.", ephemeral=True)
            return

        raid_power_limit = int(raid["power_limit"] or 0)
        sent = select_raid_join_units(permanent, raid_power_limit)
        bot.store.upsert_participant(raid["id"], interaction.user.id, interaction.user.display_name, sent)
        variants = bot.store.get_player_unit_variants(interaction.user.id)
        owned_raid_variants = {
            key: int(variants.get(key, 0))
            for key in ARMY_VARIANT_ORDER
            if key.startswith("general_") or key.startswith("mecha_")
        }
        if sent.generals > 0 or sent.mechas > 0:
            if not any(value > 0 for value in owned_raid_variants.values()):
                await interaction.response.send_message(
                    "You need to configure your General and Mecha variants before using raid loadouts.",
                    ephemeral=True,
                )
                return
            view = RaidLoadoutVariantView(bot, raid["id"], interaction.user.id, sent, variants)
            await interaction.response.send_message(
                content=view._message(),
                view=view,
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(embed=joined_embed(interaction.user.display_name, sent))

    @bot.tree.command(name="raid_start", description="Start the active raid battle.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def raid_start(interaction: discord.Interaction) -> None:
        raid = bot.store.get_active_raid()
        if raid is None or raid["state"] != RaidState.RECRUITING.value:
            await interaction.response.send_message("There is no recruiting raid to start.", ephemeral=True)
            return
        if not bot.is_raid_admin(raid, interaction.user):
            await interaction.response.send_message("You do not have permission to use raid administration.", ephemeral=True)
            return
        bot.store.start_raid(raid["id"], interaction.channel_id)
        raid = bot.store.get_raid(raid["id"])
        await announce_turn_change(bot, raid, interaction.channel)
        await interaction.response.send_message(embed=battle_started_embed(raid, raid["current_turn_discord_id"] or "No one"))

    @bot.tree.command(name="attack", description="Attack Arcadion during the battle.")
    async def attack(interaction: discord.Interaction) -> None:
        raid = bot.store.get_active_raid()
        if raid is None or raid["state"] != RaidState.BATTLE.value:
            await interaction.response.send_message("There is no active battle.", ephemeral=True)
            return
        if raid_time_expired(raid):
            bot.store.finish_raid(raid["id"], "ARCADION")
            bot.clear_raid_leader(raid["id"])
            finished_raid = bot.store.get_raid(raid["id"])
            if not interaction.response.is_done():
                await interaction.response.defer()
            await interaction.followup.send(
                embed=loot_summary_embed(finished_raid, bot.store.list_participants(raid["id"]), "Arcadion wins. The raid timer has expired.")
            )
            return

        participant = bot.store.get_participant(raid["id"], interaction.user.id)
        if participant is None:
            await interaction.response.send_message("You are not participating in this raid.", ephemeral=True)
            return
        if participant["status"] == "ELIMINATED":
            await interaction.response.send_message("? You have been eliminated from this raid.\n\nPlease wait until the next Arcadion Raid to fight again.", ephemeral=True)
            return

        current_units = Units.from_row(participant)
        if not current_units.has_any():
            await interaction.response.send_message("You have no active troops left to attack.", ephemeral=True)
            return

        raid = bot.store.get_active_raid()
        if raid is None or raid["state"] != RaidState.BATTLE.value:
            await interaction.response.send_message("There is no active battle.", ephemeral=True)
            return

        if raid["current_turn_discord_id"] and str(raid["current_turn_discord_id"]) != str(interaction.user.id):
            current_holder = bot.store.get_participant(raid["id"], raid["current_turn_discord_id"])
            current_holder_name = current_holder["discord_name"] if current_holder else "the current player"
            await interaction.response.send_message(f"It's not your turn. The turn currently belongs to @{current_holder_name}", ephemeral=True)
            return

        if not raid["current_turn_discord_id"]:
            bot.store.advance_turn(raid["id"], interaction.channel_id)
            raid = bot.store.get_active_raid()

        if raid is None or raid["state"] != RaidState.BATTLE.value:
            await interaction.response.send_message("There is no active battle.", ephemeral=True)
            return

        if raid["current_turn_discord_id"] and str(raid["current_turn_discord_id"]) != str(interaction.user.id):
            current_holder = bot.store.get_participant(raid["id"], raid["current_turn_discord_id"])
            current_holder_name = current_holder["discord_name"] if current_holder else "the current player"
            await interaction.response.send_message(f"It's not your turn. The turn currently belongs to @{current_holder_name}", ephemeral=True)
            return

        if raid["turn_deadline_at"] and datetime.fromisoformat(raid["turn_deadline_at"]) <= datetime.now(timezone.utc):
            current_player = bot.store.get_participant(raid["id"], raid["current_turn_discord_id"])
        capacity = attack_dice_capacity(participant)
        bonus_options = get_available_mecha_bonus_options(bot, raid["id"], participant)
        await interaction.response.send_message(
            content="\n".join(
                [
                    "⚔️ CHOOSE YOUR ATTACK DICE",
                    "",
                    "Dice determine the attack roll for this attack.",
                    "The existing Classic attack system remains unchanged.",
                    "",
                    f"Your army allows up to:",
                    f"{attack_dice_emoji(capacity)} {capacity} dice",
                    "",
                    "Choose one Mecha bonus ability if available.",
                ]
            ),
            view=ClassicAttackDiceView(bot, raid["id"], interaction.user.id, capacity, bonus_options),
            ephemeral=True,
        )

    @bot.tree.command(name="modifier_use", description="Use an available modifier.")
    @app_commands.choices(
        modifier=[
            app_commands.Choice(name="Returning Force", value=ModifierType.RETURNING_FORCE.value),
            app_commands.Choice(name="Soldier Ascends", value=ModifierType.SOLDIER_ASCENDS.value),
        ]
    )
    async def modifier_use(interaction: discord.Interaction, modifier: app_commands.Choice[str]) -> None:
        raid = bot.store.get_active_raid()
        if raid is None or raid["state"] != RaidState.BATTLE.value:
            await interaction.response.send_message("Modifiers can only be used during an active battle.", ephemeral=True)
            return
        participant = bot.store.get_participant(raid["id"], interaction.user.id)
        if participant is None:
            await interaction.response.send_message("You are not participating in this raid.", ephemeral=True)
            return
        if participant["status"] == "ELIMINATED":
            await interaction.response.send_message("? You have been eliminated from this raid and cannot use modifiers.", ephemeral=True)
            return

        modifier = ModifierType(modifier.value)
        row = bot.store.get_modifier(raid["id"], interaction.user.id, modifier)
        if row is None or int(row["remaining_uses"]) <= 0:
            await interaction.response.send_message("You have no uses available for that modifier.", ephemeral=True)
            return

        if modifier == ModifierType.RETURNING_FORCE:
            revived = revive_one_unit(bot.store, raid["id"], interaction.user.id, participant)
            if revived is None:
                await interaction.response.send_message("You have no lost units to recover.", ephemeral=True)
                return
            bot.store.set_modifier(raid["id"], interaction.user.id, modifier, 0, 0)
            await interaction.response.send_message(f"? **RETURNING FORCE**\n{interaction.user.display_name} recovers 1 {revived}.")
            return

        if modifier == ModifierType.SOLDIER_ASCENDS:
            units = Units.from_row(participant)
            if units.bulls <= 0 and units.rhinos <= 0:
                await interaction.response.send_message("You need at least one active Bull or Rhino to promote.", ephemeral=True)
                return
            source_unit, promoted_label = promote_soldier(bot.store, raid["id"], interaction.user.id, units)
            bot.store.set_modifier(raid["id"], interaction.user.id, modifier, 0, 2, source_unit)
            await interaction.response.send_message(f"??? **SOLDIER ASCENDS**\nOne {promoted_label} temporarily becomes a Lion Lieutenant for 2 turns.")

    @bot.tree.command(name="modifier_apply", description="Apply a modifier to a participant.")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.choices(
        modifier=[
            app_commands.Choice(name="Fallen Lieutenant", value=ModifierType.FALLEN_LIEUTENANT.value),
        ]
    )
    async def modifier_apply(interaction: discord.Interaction, user: discord.Member, modifier: app_commands.Choice[str]) -> None:
        raid = bot.store.get_active_raid()
        if raid is None or raid["state"] != RaidState.BATTLE.value:
            await interaction.response.send_message("There is no active battle.", ephemeral=True)
            return
        if not bot.is_raid_admin(raid, interaction.user):
            await interaction.response.send_message("You do not have permission to use raid administration.", ephemeral=True)
            return
        participant = bot.store.get_participant(raid["id"], user.id)
        if participant is None:
            await interaction.response.send_message("That player is not participating in the raid.", ephemeral=True)
            return
        modifier = ModifierType(modifier.value)
        bot.store.set_modifier(raid["id"], user.id, modifier, 0, 2)
        await interaction.response.send_message(f"?? **FALLEN LIEUTENANT**\n{user.display_name} will attack with 1 die for 2 turns.")

    @bot.tree.command(name="raid_kick", description="Remove a player from the active raid.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def raid_kick(interaction: discord.Interaction, user: discord.Member) -> None:
        raid = bot.store.get_active_raid()
        if raid is None or raid["state"] != RaidState.BATTLE.value:
            await interaction.response.send_message("There is no active battle.", ephemeral=True)
            return
        if not bot.is_raid_admin(raid, interaction.user):
            await interaction.response.send_message("You do not have permission to use raid administration.", ephemeral=True)
            return
        participant = bot.store.get_participant(raid["id"], user.id)
        if participant is None:
            await interaction.response.send_message("That player is not participating in the raid.", ephemeral=True)
            return

        with bot.store.connect() as conn:
            turn_order = json.loads(raid["turn_order"] or "[]")
            turn_order = [discord_id for discord_id in turn_order if str(discord_id) != str(user.id)]
            current_turn = raid["current_turn_discord_id"]
            current_turn_str = str(current_turn) if current_turn is not None else None
            is_current_player = current_turn_str == str(user.id)
            conn.execute(
                "DELETE FROM raid_participants WHERE raid_id = ? AND discord_id = ?",
                (raid["id"], str(user.id)),
            )
            if is_current_player:
                bot.cancel_turn_timeout(raid["id"])
                if turn_order:
                    next_player_id = turn_order[0]
                    conn.execute(
                        "UPDATE raids SET turn_order = ?, current_turn_discord_id = ?, turn_index = 0, turn_round = 1, turn_started_at = ?, turn_deadline_at = ?, turn_reminder_state = 'NONE' WHERE id = ?",
                        (json.dumps(turn_order), next_player_id, utc_now(), (datetime.now(timezone.utc) + timedelta(seconds=TURN_TIME_LIMIT_SECONDS)).isoformat(), raid["id"]),
                    )
                else:
                    conn.execute(
                        "UPDATE raids SET turn_order = ?, current_turn_discord_id = NULL, turn_started_at = NULL, turn_deadline_at = NULL, turn_index = 0, turn_round = 1 WHERE id = ?",
                        (json.dumps([]), raid["id"]),
                    )
                    bot.clear_raid_leader(raid["id"])
            else:
                bot.cancel_turn_timeout(raid["id"])
                conn.execute(
                    "UPDATE raids SET turn_order = ? WHERE id = ?",
                    (json.dumps(turn_order), raid["id"]),
                )
                if current_turn_str not in turn_order:
                    if turn_order:
                        conn.execute(
                            "UPDATE raids SET current_turn_discord_id = ?, turn_started_at = ?, turn_deadline_at = ?, turn_reminder_state = 'NONE' WHERE id = ?",
                            (turn_order[0], utc_now(), (datetime.now(timezone.utc) + timedelta(seconds=TURN_TIME_LIMIT_SECONDS)).isoformat(), raid["id"]),
                        )
                    else:
                        conn.execute(
                            "UPDATE raids SET current_turn_discord_id = NULL, turn_started_at = NULL, turn_deadline_at = NULL, turn_index = 0, turn_round = 1 WHERE id = ?",
                            (raid["id"],),
                        )
                        bot.clear_raid_leader(raid["id"])

        updated_raid = bot.store.get_raid(raid["id"])
        await interaction.response.send_message(f"? {user.display_name} has been removed from the raid.")
        if updated_raid is not None and interaction.channel is not None:
            await announce_turn_change(bot, updated_raid, interaction.channel)

    @bot.tree.command(name="raid_skip_turn", description="Skip the current player's turn.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def raid_skip_turn(interaction: discord.Interaction) -> None:
        raid = bot.store.get_active_raid()
        if raid is None or raid["state"] != RaidState.BATTLE.value:
            await interaction.response.send_message("There is no active battle.", ephemeral=True)
            return
        if not bot.is_raid_admin(raid, interaction.user):
            await interaction.response.send_message("You do not have permission to use raid administration.", ephemeral=True)
            return
        bot.cancel_turn_timeout(raid["id"])
        next_player = bot.store.advance_turn(raid["id"], interaction.channel_id)
        if next_player is None:
            await interaction.response.send_message("The turn order could not advance.", ephemeral=True)
            return
        if interaction.channel is not None:
            await announce_turn_change(bot, bot.store.get_raid(raid["id"]), interaction.channel)
        await interaction.response.send_message("? The current turn has been skipped.", ephemeral=True)
    @bot.tree.command(name="raid_status", description="Show the active raid status.")
    async def raid_status(interaction: discord.Interaction) -> None:
        raid = bot.store.get_active_raid()
        if raid is None:
            await interaction.response.send_message("There is no active raid.", ephemeral=True)
            return
        await interaction.response.send_message(embed=status_embed(raid, bot.store.list_participants(raid["id"])))

    class RaidAdminConfirmView(discord.ui.View):
        def __init__(self, bot: ArcadionBot, raid_id: int, action: str) -> None:
            super().__init__(timeout=120)
            self.bot = bot
            self.raid_id = raid_id
            self.action = action
            self.confirm_button = discord.ui.Button(label="Confirm", style=discord.ButtonStyle.danger)
            self.cancel_button = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
            self.confirm_button.callback = self._handle_confirm
            self.cancel_button.callback = self._handle_cancel
            self.add_item(self.confirm_button)
            self.add_item(self.cancel_button)

        async def interaction_check(self, interaction: discord.Interaction) -> bool:
            raid = self.bot.store.get_raid(self.raid_id)
            if raid is None or not self.bot.is_raid_admin(raid, interaction.user):
                await interaction.response.send_message("You do not have permission to use raid administration.", ephemeral=True)
                return False
            return True

        async def _handle_confirm(self, interaction: discord.Interaction) -> None:
            raid = self.bot.store.get_raid(self.raid_id)
            if raid is None:
                await interaction.response.send_message("That raid no longer exists.", ephemeral=True)
                return
            self.bot.cancel_turn_timeout(self.raid_id)
            if self.action == "finish":
                self.bot.store.finish_raid(self.raid_id, "MANUAL")
                self.bot.clear_raid_leader(self.raid_id)
                await interaction.response.edit_message(content="Raid finished.", view=None)
                return
            if self.action == "cancel":
                self.bot.store.finish_raid(self.raid_id, "CANCELLED")
                self.bot.clear_raid_leader(self.raid_id)
                await interaction.response.edit_message(content="Raid cancelled.", view=None)
                return
            if self.action == "restart":
                self.bot.store.finish_raid(self.raid_id, "RESTARTED")
                self.bot.clear_raid_leader(self.raid_id)
                await interaction.response.edit_message(content="Raid restart completed.", view=None)
                return

        async def _handle_cancel(self, interaction: discord.Interaction) -> None:
            await interaction.response.edit_message(content="Raid administration cancelled.", view=None)

    class RaidAdminControlView(discord.ui.View):
        def __init__(self, bot: ArcadionBot, raid_id: int) -> None:
            super().__init__(timeout=600)
            self.bot = bot
            self.raid_id = raid_id
            self.start_button = discord.ui.Button(label="Start Raid", style=discord.ButtonStyle.success)
            self.force_button = discord.ui.Button(label="Force Next Turn", style=discord.ButtonStyle.primary)
            self.skip_button = discord.ui.Button(label="Skip Player", style=discord.ButtonStyle.secondary)
            self.kick_button = discord.ui.Button(label="Kick Player", style=discord.ButtonStyle.secondary)
            self.finish_button = discord.ui.Button(label="Finish Raid", style=discord.ButtonStyle.danger)
            self.cancel_button = discord.ui.Button(label="Cancel Raid", style=discord.ButtonStyle.danger)
            self.restart_button = discord.ui.Button(label="Restart Raid", style=discord.ButtonStyle.danger)
            self.status_button = discord.ui.Button(label="Raid Status", style=discord.ButtonStyle.secondary)
            self.start_button.callback = self._handle_start
            self.force_button.callback = self._handle_force
            self.skip_button.callback = self._handle_skip
            self.kick_button.callback = self._handle_kick
            self.finish_button.callback = self._handle_finish
            self.cancel_button.callback = self._handle_cancel
            self.restart_button.callback = self._handle_restart
            self.status_button.callback = self._handle_status
            for item in (self.start_button, self.force_button, self.skip_button, self.kick_button, self.finish_button, self.cancel_button, self.restart_button, self.status_button):
                self.add_item(item)

        def _raid(self) -> object | None:
            return self.bot.store.get_raid(self.raid_id)

        async def interaction_check(self, interaction: discord.Interaction) -> bool:
            raid = self._raid()
            if raid is None or not self.bot.is_raid_admin(raid, interaction.user):
                await interaction.response.send_message("You do not have permission to use raid administration.", ephemeral=True)
                return False
            return True

        async def _handle_start(self, interaction: discord.Interaction) -> None:
            raid = self._raid()
            if raid is None or raid["state"] != RaidState.RECRUITING.value:
                await interaction.response.send_message("The raid is not in recruitment.", ephemeral=True)
                return
            self.bot.store.start_raid(self.raid_id, interaction.channel_id)
            updated = self._raid()
            if updated is not None and interaction.channel is not None:
                await announce_turn_change(self.bot, updated, interaction.channel)
            await interaction.response.send_message("Raid started.", ephemeral=True)

        async def _handle_force(self, interaction: discord.Interaction) -> None:
            raid = self._raid()
            if raid is None or raid["state"] != RaidState.BATTLE.value:
                await interaction.response.send_message("There is no active battle.", ephemeral=True)
                return
            self.bot.cancel_turn_timeout(self.raid_id)
            next_player = self.bot.store.advance_turn(self.raid_id, interaction.channel_id)
            if next_player is None:
                await interaction.response.send_message("The turn order could not advance.", ephemeral=True)
                return
            updated = self._raid()
            if updated is not None and interaction.channel is not None:
                await announce_turn_change(self.bot, updated, interaction.channel)
            await interaction.response.send_message("Turn advanced.", ephemeral=True)

        async def _handle_skip(self, interaction: discord.Interaction) -> None:
            await self._handle_force(interaction)

        async def _handle_kick(self, interaction: discord.Interaction) -> None:
            await interaction.response.send_message("Use `/raid_kick` to remove a specific player.", ephemeral=True)

        async def _open_confirm(self, interaction: discord.Interaction, action: str) -> None:
            await interaction.response.send_message(
                f"Are you sure you want to {action} this raid?",
                view=RaidAdminConfirmView(self.bot, self.raid_id, action),
                ephemeral=True,
            )

        async def _handle_finish(self, interaction: discord.Interaction) -> None:
            await self._open_confirm(interaction, "finish")

        async def _handle_cancel(self, interaction: discord.Interaction) -> None:
            await self._open_confirm(interaction, "cancel")

        async def _handle_restart(self, interaction: discord.Interaction) -> None:
            await self._open_confirm(interaction, "restart")

        async def _handle_status(self, interaction: discord.Interaction) -> None:
            raid = self._raid()
            if raid is None:
                await interaction.response.send_message("There is no active raid.", ephemeral=True)
                return
            await interaction.response.send_message(embed=status_embed(raid, self.bot.store.list_participants(self.raid_id)), ephemeral=True)

    @bot.tree.command(name="raid_admin", description="Open the Classic Raid administrator control panel.")
    async def raid_admin(interaction: discord.Interaction) -> None:
        raid = bot.store.get_active_raid()
        if raid is None:
            await interaction.response.send_message("There is no active raid.", ephemeral=True)
            return
        if not bot.is_raid_admin(raid, interaction.user):
            await interaction.response.send_message("You do not have permission to use raid administration.", ephemeral=True)
            return
        await interaction.response.send_message(
            content="🛠️ RAID ADMIN CONTROL",
            embed=status_embed(raid, bot.store.list_participants(raid["id"])),
            view=RaidAdminControlView(bot, raid["id"]),
            ephemeral=True,
        )

    @bot.tree.command(name="raid_finish", description="Manually finish the active raid.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def raid_finish(interaction: discord.Interaction) -> None:
        raid = bot.store.get_active_raid()
        if raid is None:
            await interaction.response.send_message("There is no active raid.", ephemeral=True)
            return
        if not bot.is_raid_admin(raid, interaction.user):
            await interaction.response.send_message("You do not have permission to use raid administration.", ephemeral=True)
            return
        bot.cancel_turn_timeout(raid["id"])
        bot.store.finish_raid(raid["id"], "MANUAL")
        bot.clear_raid_leader(raid["id"])
        finished_raid = bot.store.get_raid(raid["id"])
        if not interaction.response.is_done():
            await interaction.response.defer()
        await interaction.followup.send(embed=loot_summary_embed(finished_raid, bot.store.list_participants(raid["id"]), "The raid was manually finished."))


def attack_dice_count(store: Store, raid_id: int, discord_id: int) -> int:
    fallen = store.get_modifier(raid_id, discord_id, ModifierType.FALLEN_LIEUTENANT)
    if fallen is not None and int(fallen["remaining_turns"]) > 0:
        return 1
    return 3


def active_combat_unit_state(participant: object) -> dict[str, bool]:
    return {
        "bulls": int(participant["bulls_hp"] or 0) > 0,
        "rhinos": int(participant["rhinos_hp"] or 0) > 0,
        "lieutenants": int(participant["lieutenants_hp"] or 0) > 0,
        "generals": int(participant["generals_hp"] or 0) > 0,
        "mechas": int(participant["mechas_hp"] or 0) > 0,
    }


def attack_dice_capacity(participant: object) -> int:
    active = active_combat_unit_state(participant)
    if active["mechas"] and active["generals"]:
        return 5
    if active["mechas"] or active["generals"] or active["lieutenants"]:
        return 3 if active["generals"] or active["mechas"] else 2
    return 1


def attack_dice_emoji(count: int) -> str:
    return "🎲" * max(1, int(count))


def count_dice_successes(rolls: list[int]) -> int:
    return sum(1 for roll in rolls if roll >= 4)


def format_dice_rolls(rolls: list[int]) -> str:
    return " + ".join(str(roll) for roll in rolls) if rolls else "No dice"


def mecha_bonus_label(unit_variant: str, ability_name: str) -> str:
    return f"{army_variant_label(unit_variant)} — {ability_name}"


def get_active_mecha_variant_counts(bot: ArcadionBot, raid_id: int, participant: object) -> dict[str, int]:
    loaded_variants = bot.store.get_participant_loaded_variants(raid_id, participant["discord_id"])
    active_mechas_hp = max(0, int(participant["mechas_hp"] or 0))
    active_mecha_units = (active_mechas_hp + 29999) // 30000 if active_mechas_hp > 0 else 0
    active_counts: dict[str, int] = {}
    for unit_variant in MECHA_BONUS_ORDER:
        loaded_count = max(0, int(loaded_variants.get(unit_variant, 0)))
        if loaded_count <= 0 or active_mecha_units <= 0:
            continue
        assigned = min(loaded_count, active_mecha_units)
        if assigned > 0:
            active_counts[unit_variant] = assigned
            active_mecha_units -= assigned
    return active_counts


def get_available_mecha_bonus_options(bot: ArcadionBot, raid_id: int, participant: object) -> list[dict[str, object]]:
    active = active_combat_unit_state(participant)
    if not active["mechas"] or not active["generals"]:
        return []
    variants = get_active_mecha_variant_counts(bot, raid_id, participant)
    options: list[dict[str, object]] = []
    for unit_variant in MECHA_BONUS_ORDER:
        owned = int(variants.get(unit_variant, 0))
        if owned <= 0:
            continue
        for ability_name, bonus_value, bonus_type, description in MECHA_BONUS_DEFINITIONS[unit_variant]:
            used = bot.store.count_mecha_bonus_activations(raid_id, participant["discord_id"], unit_variant, ability_name)
            remaining = owned - used
            if remaining <= 0:
                continue
            options.append(
                {
                    "unit_variant": unit_variant,
                    "ability_name": ability_name,
                    "bonus_value": bonus_value,
                    "bonus_type": bonus_type,
                    "description": description,
                    "remaining": remaining,
                }
            )
    return options


def resolve_dice_clash(player_dice_count: int) -> dict[str, object]:
    if player_dice_count == 5:
        arcadion_dice_count = 5
    elif random.random() < 0.30:
        arcadion_dice_count = player_dice_count
    else:
        arcadion_dice_count = 5
    player_roll = roll_dice(player_dice_count)
    arcadion_roll = roll_dice(arcadion_dice_count)
    player_successes = count_dice_successes(player_roll.rolls)
    arcadion_successes = count_dice_successes(arcadion_roll.rolls)
    winner = "PLAYER" if player_successes >= arcadion_successes else "ARCADION"
    return {
        "player_dice_count": player_dice_count,
        "player_roll": player_roll,
        "player_successes": player_successes,
        "arcadion_dice_count": arcadion_dice_count,
        "arcadion_roll": arcadion_roll,
        "arcadion_successes": arcadion_successes,
        "winner": winner,
    }


def format_dice_clash_message(attacker_name: str, clash: dict[str, object]) -> str:
    player_roll: DiceRoll = clash["player_roll"]  # type: ignore[assignment]
    arcadion_roll: DiceRoll = clash["arcadion_roll"]  # type: ignore[assignment]
    player_successes = int(clash["player_successes"])
    arcadion_successes = int(clash["arcadion_successes"])
    winner = "🟢 PLAYER WINS THE DICE CLASH" if clash["winner"] == "PLAYER" else "🔴 ARCADION WINS THE DICE CLASH"
    return "\n".join(
        [
            "━━━━━━━━━━━━━━━━━━━━",
            "⚔️ DICE CLASH",
            "━━━━━━━━━━━━━━━━━━━━",
            "",
            f"@{attacker_name}",
            f"🎲 {format_dice_rolls(player_roll.rolls)}",
            f"Successes: {player_successes}",
            "",
            "Arcadion",
            f"🎲 {format_dice_rolls(arcadion_roll.rolls)}",
            f"Successes: {arcadion_successes}",
            "",
            winner,
            "",
            "━━━━━━━━━━━━━━━━━━━━",
        ]
    )


def apply_mecha_bonus(
    bot: ArcadionBot,
    raid: object,
    participant: object,
    bonus_choice: dict[str, object] | None,
    activated: bool,
) -> dict[str, object]:
    result = {
        "activated": False,
        "bonus_damage": 0,
        "bonus_label": None,
        "details": None,
        "ignore_guard": False,
        "avoid_counterattack": False,
    }
    if bonus_choice is None:
        return result

    unit_variant = str(bonus_choice["unit_variant"])
    ability_name = str(bonus_choice["ability_name"])
    bonus_type = str(bonus_choice["bonus_type"])
    result["bonus_label"] = mecha_bonus_label(unit_variant, ability_name)

    if not activated:
        return result

    if bonus_type == "damage":
        result["activated"] = True
        result["bonus_damage"] = int(bonus_choice["bonus_value"])
        result["details"] = f"+{format_number(int(bonus_choice['bonus_value']))} damage"
        return result

    if bonus_type == "bypass_guard":
        result["activated"] = True
        result["bonus_damage"] = int(bonus_choice["bonus_value"])
        result["ignore_guard"] = True
        result["details"] = f"+{format_number(int(bonus_choice['bonus_value']))} damage (ignores physical protection)"
        return result

    if bonus_type == "heal":
        candidates = []
        for ally in bot.store.list_active_participants(int(raid["id"])):
            counts = Units.from_row(ally)
            current_health = combat_health_from_row(ally)
            maximum_power = counts.power()
            current_power = combat_power_from_row(ally)
            if current_power < maximum_power:
                candidates.append((ally, current_power, maximum_power, current_health))
        if not candidates:
            result["details"] = "No allied commander requires repair."
            return result
        lowest_power = min(candidate[1] for candidate in candidates)
        weakest = [candidate for candidate in candidates if candidate[1] == lowest_power]
        target, _, maximum_power, before_health = random.choice(weakest)
        requested_healing = max(0, int(maximum_power * 0.6))
        bot.store.heal_participant_units(int(raid["id"]), target["discord_id"], requested_healing)
        refreshed_target = bot.store.get_participant(int(raid["id"]), target["discord_id"])
        restored = 0
        if refreshed_target is not None:
            after_health = combat_health_from_row(refreshed_target)
            restored = max(0, sum(after_health.values()) - sum(before_health.values()))
        if restored <= 0:
            result["details"] = "No allied commander requires repair."
            return result
        result["activated"] = True
        result["details"] = f"Target: {target['discord_name']} | Restored: {format_number(restored)} battle power"
        return result

    if bonus_type in {"avoid_bonus_attack", "redirect_bonus_attack"}:
        result["activated"] = True
        if bonus_type == "avoid_bonus_attack":
            result["avoid_counterattack"] = True
            result["details"] = "Arcadion's special bonus attack was avoided."
        else:
            result["details"] = "Arcadion's special bonus attack was redirected."
        return result

    return result


async def resolve_classic_attack(
    bot: ArcadionBot,
    interaction: discord.Interaction,
    dice_count: int,
    bonus_choice: dict[str, object] | None = None,
) -> None:
    raid = bot.store.get_active_raid()
    if raid is None or raid["state"] != RaidState.BATTLE.value:
        await interaction.response.send_message("There is no active battle.", ephemeral=True)
        return
    if raid_time_expired(raid):
        bot.store.finish_raid(raid["id"], "ARCADION")
        bot.clear_raid_leader(raid["id"])
        finished_raid = bot.store.get_raid(raid["id"])
        if not interaction.response.is_done():
            await interaction.response.defer()
        await interaction.followup.send(
            embed=loot_summary_embed(finished_raid, bot.store.list_participants(raid["id"]), "Arcadion wins. The raid timer has expired.")
        )
        return

    participant = bot.store.get_participant(raid["id"], interaction.user.id)
    if participant is None:
        await interaction.response.send_message("You are not participating in this raid.", ephemeral=True)
        return
    if participant["status"] == "ELIMINATED":
        await interaction.response.send_message("? You have been eliminated from this raid.\n\nPlease wait until the next Arcadion Raid to fight again.", ephemeral=True)
        return

    current_units = Units.from_row(participant)
    if not current_units.has_any():
        await interaction.response.send_message("You have no active troops left to attack.", ephemeral=True)
        return

    raid = bot.store.get_active_raid()
    if raid is None or raid["state"] != RaidState.BATTLE.value:
        await interaction.response.send_message("There is no active battle.", ephemeral=True)
        return

    if raid["current_turn_discord_id"] and str(raid["current_turn_discord_id"]) != str(interaction.user.id):
        current_holder = bot.store.get_participant(raid["id"], raid["current_turn_discord_id"])
        current_holder_name = current_holder["discord_name"] if current_holder else "the current player"
        await interaction.response.send_message(f"It's not your turn. The turn currently belongs to @{current_holder_name}", ephemeral=True)
        return

    if not raid["current_turn_discord_id"]:
        bot.store.advance_turn(raid["id"], interaction.channel_id)
        raid = bot.store.get_active_raid()

    if raid is None or raid["state"] != RaidState.BATTLE.value:
        await interaction.response.send_message("There is no active battle.", ephemeral=True)
        return

    if raid["current_turn_discord_id"] and str(raid["current_turn_discord_id"]) != str(interaction.user.id):
        current_holder = bot.store.get_participant(raid["id"], raid["current_turn_discord_id"])
        current_holder_name = current_holder["discord_name"] if current_holder else "the current player"
        await interaction.response.send_message(f"It's not your turn. The turn currently belongs to @{current_holder_name}", ephemeral=True)
        return

    current_capacity = attack_dice_capacity(participant)
    if dice_count not in (1, 2, 3, 5) or dice_count > current_capacity:
        await interaction.response.send_message(
            f"Your army changed during this attack. Your current army allows a maximum of {current_capacity} dice. Please select your dice again.",
            ephemeral=True,
        )
        return

    if raid["turn_deadline_at"] and datetime.fromisoformat(raid["turn_deadline_at"]) <= datetime.now(timezone.utc):
        current_player = bot.store.get_participant(raid["id"], raid["current_turn_discord_id"])
    bot.cancel_turn_timeout(raid["id"])
    clash = resolve_dice_clash(dice_count)

    attacker_roll = clash["player_roll"]
    bonus_result = apply_mecha_bonus(bot, raid, participant, bonus_choice, clash["winner"] == "PLAYER")
    if (
        bonus_choice is not None
        and str(bonus_choice["ability_name"]) == "High Flight"
        and clash["winner"] == "ARCADION"
    ):
        original_rolls = clash["arcadion_roll"].rolls
        reduced_rolls = [max(1, roll - 2) for roll in original_rolls]
        bonus_result["details"] = (
            "Arcadion's attack hits the commander. "
            f"Dice effect: Original: {format_dice_rolls(original_rolls)} | "
            f"High Flight: {format_dice_rolls(reduced_rolls)}"
        )
    if bonus_result["bonus_label"] and interaction.channel is not None:
        bonus_label = bonus_result["bonus_label"] or "Mecha Bonus"
        bonus_state = "BONUS ACTIVATED" if bonus_result["activated"] else "BONUS NOT ACTIVATED"
        bonus_lines = [
            "━━━━━━━━━━━━━━━━━━━━",
            "✨ MECHA BONUS",
            "━━━━━━━━━━━━━━━━━━━━",
            "",
            f"{bonus_label}",
            bonus_state,
        ]
        if bonus_result["details"]:
            bonus_lines.append(str(bonus_result["details"]))
        bonus_lines.append("━━━━━━━━━━━━━━━━━━━━")
        await interaction.channel.send("\n".join(bonus_lines))
    arcadion_guard = Units.from_row(raid, "arcadion_")
    arcadion_guard_health = combat_health_from_row(raid, "arcadion_")
    bonus_damage = int(bonus_result["bonus_damage"])
    classic_damage = attacker_roll.damage
    guard_remaining, guard_destroyed, guard_remaining_health, guard_absorbed = apply_wounded_combat_damage(arcadion_guard, arcadion_guard_health, classic_damage)
    damage_to_corruption = max(0, classic_damage - guard_absorbed) + bonus_damage
    new_corruption = max(0, int(raid["current_corruption"]) - damage_to_corruption)
    bot.store.update_arcadion_units(raid["id"], guard_remaining, guard_remaining_health)
    bot.store.update_corruption(raid["id"], new_corruption)
    bot.store.add_attack_stats(raid["id"], interaction.user.id, attacker_roll.damage)

    active_targets = bot.store.list_active_participants(raid["id"])
    arcadion_roll = roll_arcadion_counterattack_dice()
    target = None
    if active_targets:
        target_powers = [(row, combat_power_from_row(row)) for row in active_targets]
        highest_power = max(power for _, power in target_powers)
        strongest_targets = [row for row, power in target_powers if power == highest_power]
        target = random.choice(strongest_targets) if len(strongest_targets) > 1 else strongest_targets[0]
    destroyed = Units()
    wounded_lines: list[str] = []
    counterattack_message = None
    if target is not None and not bonus_result["avoid_counterattack"]:
        target_units = Units.from_row(target)
        target_health = combat_health_from_row(target)
        remaining_units, destroyed_units, remaining_health, absorbed_damage = apply_wounded_combat_damage(target_units, target_health, arcadion_roll.damage)
        destroyed = destroyed_units
        wounded_lines = build_wounded_lines(target_health, remaining_health, remaining_units)
        bot.store.update_participant_units_and_losses(raid["id"], target["discord_id"], remaining_units, destroyed_units, remaining_health)
        counterattack_message = counterattack_summary(
            target["discord_name"],
            arcadion_roll.damage,
            destroyed,
            remaining_units,
            arcadion_roll.text,
        )
        if bot.store.mark_participant_eliminated_if_needed(raid["id"], target["discord_id"], remaining_units):
            await interaction.channel.send(
                f"?? COMMANDER ELIMINATED\n\nThe army of @{target['discord_name']} has been completely destroyed.\n\nThis commander has been eliminated from the current raid and can no longer participate.\n\nHowever, all damage dealt and battle statistics have been recorded.\n\nThe commander will still receive rewards based on their contribution when the raid ends."
            )
    bot.store.log_attack(
        raid["id"],
        interaction.user.id,
        attacker_roll.text,
        int(clash["player_dice_count"]),
        int(clash["player_successes"]),
        attacker_roll.damage,
        arcadion_roll.text,
        int(clash["arcadion_dice_count"]),
        int(clash["arcadion_successes"]),
        arcadion_roll.damage,
        target["discord_id"] if target else None,
        str(clash["winner"]),
        str(bonus_result["bonus_label"]) if bonus_result["bonus_label"] else None,
        str(bonus_result["details"]) if bonus_result["details"] else None,
        bool(bonus_result["activated"]),
        bonus_damage,
        json.dumps(destroyed.as_dict()),
    )
    bot.store.tick_turn_modifiers(raid["id"], interaction.user.id)
    bot.store.increment_turns_played(raid["id"], interaction.user.id)

    refreshed = bot.store.get_raid(raid["id"])
    active_after = bot.store.list_active_participants(raid["id"])
    result_message = None
    if new_corruption <= 0:
        bot.store.finish_raid(raid["id"], "PLAYERS")
        bot.clear_raid_leader(raid["id"])
        result_message = "Victory. Arcadion has been defeated."
        completed_raid = bot.store.get_raid(raid["id"])
        completed_raid = {**completed_raid, "result": "SUCCESS"}
    elif not active_after:
        bot.store.finish_raid(raid["id"], "ARCADION")
        bot.clear_raid_leader(raid["id"])
        result_message = "Arcadion wins. No active troops remain."
        completed_raid = bot.store.get_raid(raid["id"])
        completed_raid = {**completed_raid, "result": "FAILED"}
    elif not bot.store.list_active_participants(raid["id"]):
        bot.cancel_turn_timeout(raid["id"])
        bot.store.finish_raid(raid["id"], "ARCADION")
        result_message = "?? ARCADION IS VICTORIOUS\n\nAll deployed armies have been destroyed.\n\nThe city has fallen under Arcadion's corruption.\n\nRaid Failed."
        completed_raid = bot.store.get_raid(raid["id"])
        completed_raid = {**completed_raid, "result": "FAILED"}
    else:
        bot.cancel_turn_timeout(raid["id"])
        next_player = bot.store.advance_turn(raid["id"], interaction.channel_id)
        if next_player is not None and interaction.channel:
            await announce_turn_change(bot, bot.store.get_raid(raid["id"]), interaction.channel)

    if counterattack_message and interaction.channel:
        threat_line = random.choice(ARCADION_THREAT_LINES)
        await interaction.channel.send(f"{threat_line}\n\n{counterattack_message}")

    if result_message is None:
        await interaction.response.send_message(
            embed=attack_embed(
                attacker_name=interaction.user.display_name,
                attacker_roll=attacker_roll,
                raid=refreshed,
                arcadion_roll=arcadion_roll,
                target_name=target["discord_name"] if target else "No one",
                destroyed=destroyed,
                arcadion_guard_destroyed=guard_destroyed,
                damage_to_corruption=damage_to_corruption,
                bonus_label=str(bonus_result["bonus_label"]) if bonus_result["bonus_label"] else None,
                bonus_details=str(bonus_result["details"]) if bonus_result["details"] else None,
                bonus_activated=bool(bonus_result["activated"]),
                bonus_damage=bonus_damage,
                wounded_lines=wounded_lines,
                result_message=result_message,
            )
        )
        return

    completed_raid = bot.store.get_raid(raid["id"])
    await interaction.response.send_message(
        embed=loot_summary_embed(completed_raid, bot.store.list_participants(raid["id"]), result_message)
    )

class ClassicAttackDiceView(discord.ui.View):
    def __init__(self, bot: ArcadionBot, raid_id: int, user_id: int, capacity: int, bonus_options: list[dict[str, object]] | None = None) -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self.raid_id = raid_id
        self.user_id = user_id
        self.capacity = max(1, int(capacity))
        self.selected_dice: int | None = None
        self.bonus_options = bonus_options or []
        self.selected_bonus_key: str | None = None
        self.selected_bonus_disabled = False
        self._bonus_lookup = {
            f"{option['unit_variant']}|{option['ability_name']}": option
            for option in self.bonus_options
        }
        self.one_button = discord.ui.Button(label="1 Die", style=discord.ButtonStyle.secondary)
        self.two_button = discord.ui.Button(label="2 Dice", style=discord.ButtonStyle.secondary)
        self.three_button = discord.ui.Button(label="3 Dice", style=discord.ButtonStyle.secondary)
        self.five_button = discord.ui.Button(label="5 Dice", style=discord.ButtonStyle.secondary)
        self.confirm_button = discord.ui.Button(label="Attack", style=discord.ButtonStyle.danger, emoji="⚔️", disabled=True)
        self.change_button = discord.ui.Button(label="Change Dice", style=discord.ButtonStyle.secondary, emoji="✏️")
        self.bonus_menu: discord.ui.Select | None = None
        self._sync_components()
        for value, button in ((1, self.one_button), (2, self.two_button), (3, self.three_button), (5, self.five_button)):
            if value <= self.capacity:
                button.callback = self._make_select_callback(value)
        self.confirm_button.callback = self._handle_confirm
        self.change_button.callback = self._handle_change
        self._sync_components()

    def _sync_components(self) -> None:
        self.clear_items()
        for value, button in ((1, self.one_button), (2, self.two_button), (3, self.three_button), (5, self.five_button)):
            if value <= self.capacity:
                self.add_item(button)
        if self.selected_dice == 5 and self.bonus_options:
            self.bonus_menu = discord.ui.Select(
                placeholder="Choose a Mecha bonus",
                min_values=1,
                max_values=1,
                options=self._bonus_select_options(),
            )
            self.bonus_menu.callback = self._handle_bonus_select
            self.add_item(self.bonus_menu)
        else:
            self.bonus_menu = None
            self.selected_bonus_key = None
        self.confirm_button.disabled = not self._has_required_selections()
        self.add_item(self.confirm_button)
        self.add_item(self.change_button)

    def _bonus_select_options(self) -> list[discord.SelectOption]:
        options: list[discord.SelectOption] = []
        options.append(
            discord.SelectOption(
                label="No Bonus",
                value="__none__",
                description="Attack without activating a Mecha bonus.",
            )
        )
        for option in self.bonus_options:
            unit_variant = str(option["unit_variant"])
            ability_name = str(option["ability_name"])
            remaining = int(option["remaining"])
            label = f"{army_variant_label(unit_variant)} — {ability_name}"
            options.append(
                discord.SelectOption(
                    label=label[:100],
                    value=f"{unit_variant}|{ability_name}",
                    description=(str(option["description"])[:100] if option.get("description") else f"Uses remaining: {remaining}"),
                )
            )
        return options

    def _has_required_selections(self) -> bool:
        return self.selected_dice is not None

    def _refresh_confirm_state(self) -> None:
        self.confirm_button.disabled = not self._has_required_selections()

    def _make_select_callback(self, dice_count: int):
        async def _callback(interaction: discord.Interaction) -> None:
            self.selected_dice = dice_count
            if self.selected_dice != 5:
                self.selected_bonus_key = None
            self._sync_components()
            self._refresh_confirm_state()
            await interaction.response.edit_message(content=self._message(), view=self)

        return _callback

    def _message(self) -> str:
        if self.selected_dice is None:
            return "\n".join(
                [
                    "⚔️ CHOOSE YOUR ATTACK DICE",
                    "",
                    f"Your army allows up to:",
                    "",
                    f"{attack_dice_emoji(self.capacity)} {self.capacity} dice",
                    "",
                    "Select how many dice to use for this attack.",
                    "The Classic attack system remains unchanged.",
                    "",
                    "Mecha bonus will be selected after you choose the dice.",
                ]
            )
        return "\n".join(
            [
                "⚔️ ATTACK READY",
                "",
                f"Dice:\n{attack_dice_emoji(self.selected_dice)} {self.selected_dice} Dice",
                "",
                f"Mecha Bonus: {self._selected_bonus_label()}",
            ]
        )

    def _selected_bonus_label(self) -> str:
        if self.selected_dice != 5:
            return "Not available for this attack"
        if self.bonus_menu is None:
            return "No active Mecha available."
        if self.selected_bonus_key is None:
            return "No Bonus"
        if self.selected_bonus_key == "__none__":
            return "No Bonus"
        option = self._bonus_lookup.get(self.selected_bonus_key)
        if option is None:
            return "No Bonus"
        return f"{army_variant_label(str(option['unit_variant']))} — {option['ability_name']}"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This attack menu is not for you.", ephemeral=True)
            return False
        return True

    async def _handle_change(self, interaction: discord.Interaction) -> None:
        self.selected_dice = None
        self.selected_bonus_key = None
        self._sync_components()
        await interaction.response.edit_message(content=self._message(), view=self)

    async def _handle_bonus_select(self, interaction: discord.Interaction) -> None:
        selected_value = self.bonus_menu.values[0] if self.bonus_menu is not None else None
        self.selected_bonus_key = None if selected_value in (None, "__none__") else selected_value
        self._refresh_confirm_state()
        await interaction.response.edit_message(content=self._message(), view=self)

    async def _handle_confirm(self, interaction: discord.Interaction) -> None:
        if self.selected_dice is None:
            await interaction.response.send_message("Choose the number of dice first.", ephemeral=True)
            return
        bonus_choice = self._bonus_lookup.get(self.selected_bonus_key) if self.selected_bonus_key else None
        if bonus_choice is not None and self.selected_dice == 5:
            raid = self.bot.store.get_active_raid()
            participant = self.bot.store.get_participant(self.raid_id, interaction.user.id) if raid is not None else None
            live_options = get_available_mecha_bonus_options(self.bot, self.raid_id, participant) if participant is not None else []
            live_lookup = {
                f"{option['unit_variant']}|{option['ability_name']}": option
                for option in live_options
            }
            if self.selected_bonus_key not in live_lookup:
                await interaction.response.send_message("This Mecha is no longer active in the raid.", ephemeral=True)
                return
            bonus_choice = live_lookup[self.selected_bonus_key]
        await resolve_classic_attack(self.bot, interaction, self.selected_dice, bonus_choice)



def raid_time_expired(raid: object) -> bool:
    if not raid["ends_at"]:
        return False
    return datetime.fromisoformat(raid["ends_at"]) <= datetime.now(timezone.utc)


async def announce_turn_change(bot: ArcadionBot, raid: object, channel: discord.TextChannel | None) -> None:
    if channel is None:
        return
    current_player = bot.store.get_participant(raid["id"], raid["current_turn_discord_id"]) if raid["current_turn_discord_id"] else None
    if current_player is None:
        return
    participant = Units.from_row(current_player)
    phase = determine_arcadion_phase(int(raid["current_corruption"]), int(raid["max_corruption"]))
    phase_header = {
        1: "?? ARCADION STANDS UNBROKEN",
        2: "?? ARCADION GROWS ENRAGED",
        3: "?? ARCADION BECOMES CORRUPTED",
        4: "?? ARCADION ENTERS BERSERK STATE",
    }.get(phase, "?? COMMANDER TURN")
    phase_message = {
        1: "Normal State",
        2: "Enraged State",
        3: "Corrupted State",
        4: "Berserk State",
    }.get(phase, "Normal State")
    await channel.send(
        f"{phase_header}\n\nState: {phase_message}\n\nCommander:\n<@{current_player['discord_id']}>\n\nRemaining Military Power:\n{format_number(participant.power())}\n\nTime available:\n2 minutes"
    )
    bot.start_turn_timeout(raid["id"], current_player["discord_id"], bot._channel_id_for_raid(raid))


async def announce_turn_reminder(bot: ArcadionBot, raid: object, channel: discord.TextChannel | None, minutes: int) -> None:
    if channel is None:
        return
    current_player = bot.store.get_participant(raid["id"], raid["current_turn_discord_id"]) if raid["current_turn_discord_id"] else None
    if current_player is None:
        return
    if minutes == 1:
        await channel.send(f"⚠️ <@{current_player['discord_id']}> — 1 minute remaining to launch your attack.")


def revive_one_unit(store: Store, raid_id: int, discord_id: int, participant: object) -> str | None:
    lost_columns = {
        "mechas": "lost_mechas",
        "generals": "lost_generals",
        "lieutenants": "lost_lieutenants",
        "rhinos": "lost_rhinos",
        "bulls": "lost_bulls",
    }
    for unit, column in lost_columns.items():
        if int(participant[column]) > 0:
            units = Units.from_row(participant).as_dict()
            units[unit] += 1
            remaining = Units(**units)
            with store.connect() as conn:
                conn.execute(
                    f"""
                    UPDATE raid_participants
                    SET {unit} = {unit} + 1, {column} = {column} - 1
                    WHERE raid_id = ? AND discord_id = ?
                    """,
                    (raid_id, str(discord_id)),
                )
            return unit
    return None


def promote_soldier(store: Store, raid_id: int, discord_id: int, units: Units) -> tuple[str, str]:
    values = units.as_dict()
    promoted = "Bull Soldier" if values["bulls"] > 0 else "Rhino Soldier"
    source = "bulls" if values["bulls"] > 0 else "rhinos"
    values[source] -= 1
    values["lieutenants"] += 1
    updated = Units(**values)
    with store.connect() as conn:
        conn.execute(
            """
            UPDATE raid_participants
            SET bulls = ?, rhinos = ?, lieutenants = ?, generals = ?, mechas = ?
            WHERE raid_id = ? AND discord_id = ?
            """,
            (updated.bulls, updated.rhinos, updated.lieutenants, updated.generals, updated.mechas, raid_id, str(discord_id)),
        )
    return source, promoted


def army_embed(name: str, units: Units, title: str) -> discord.Embed:
    embed = discord.Embed(title=f"?? {title}", color=0xC9A227)
    embed.add_field(name="Commander", value=name, inline=False)
    embed.add_field(name="Units", value=format_units(units), inline=False)
    embed.add_field(name="Military Power", value=format_number(units.power()), inline=False)
    return embed


def army_embed_with_variants(name: str, units: Units, title: str, variants: dict[str, int]) -> discord.Embed:
    embed = army_embed(name, units, title)
    grouped = summarize_player_variants(variants)
    variant_lines = [f"{army_variant_emoji(key)} {army_variant_label(key)}: {grouped['generals'][key]}" for key in ("general_dolphin", "general_eagle")]
    variant_lines.extend(
        f"{army_variant_emoji(key)} {army_variant_label(key)}: {grouped['mechas'][key]}"
        for key in (
            "mecha_lion",
            "mecha_eagle",
            "mecha_dolphin",
            "mecha_tiger",
            "mecha_bull",
            "mecha_black_lion",
            "mecha_shark",
        )
    )
    embed.add_field(name="Variant Breakdown", value="\n".join(variant_lines), inline=False)
    return embed


class RaidLoadoutVariantView(discord.ui.View):
    def __init__(self, bot: ArcadionBot, raid_id: int, user_id: int, units: Units, variants: dict[str, int]) -> None:
        super().__init__(timeout=900)
        self.bot = bot
        self.raid_id = raid_id
        self.user_id = user_id
        self.units = units
        self.variant_inventory = {key: max(0, int(variants.get(key, 0))) for key in ARMY_VARIANT_ORDER}
        self.variant_counts = {key: 0 for key in ARMY_VARIANT_ORDER}
        self.selected_variant = self._first_owned_variant()
        self.variant_menu = discord.ui.Select(
            placeholder="Choose a variant",
            min_values=1,
            max_values=1,
            options=self._variant_options(),
        )
        self.minus_one = discord.ui.Button(label="−", style=discord.ButtonStyle.secondary)
        self.plus_one = discord.ui.Button(label="+", style=discord.ButtonStyle.primary)
        self.save_button = discord.ui.Button(label="Save Loadout", style=discord.ButtonStyle.green)
        self.variant_menu.callback = self._handle_variant_select
        self.minus_one.callback = self._handle_minus_one
        self.plus_one.callback = self._handle_plus_one
        self.save_button.callback = self._handle_save
        self.add_item(self.variant_menu)
        self.add_item(self.minus_one)
        self.add_item(self.plus_one)
        self.add_item(self.save_button)
        self._refresh_button_state()

    def _first_owned_variant(self) -> str:
        for key in ARMY_VARIANT_ORDER:
            if self.variant_inventory.get(key, 0) > 0:
                return key
        return ARMY_VARIANT_ORDER[0]

    def _variant_options(self) -> list[discord.SelectOption]:
        options: list[discord.SelectOption] = []
        for key in ARMY_VARIANT_ORDER:
            owned = self.variant_inventory.get(key, 0)
            if owned <= 0:
                continue
            options.append(
                discord.SelectOption(
                    label=f"{army_variant_emoji(key)} {army_variant_label(key)}",
                    value=key,
                    description=f"Owned: {owned} | Assigned: {self.variant_counts[key]}",
                )
            )
        return options

    def _totals(self) -> dict[str, int]:
        return {
            "generals": sum(self.variant_counts[key] for key in ("general_dolphin", "general_eagle")),
            "mechas": sum(self.variant_counts[key] for key in (
                "mecha_lion", "mecha_eagle", "mecha_dolphin", "mecha_tiger", "mecha_bull", "mecha_black_lion", "mecha_shark"
            )),
        }

    def _owned_totals(self) -> dict[str, int]:
        return {
            "generals": sum(self.variant_inventory[key] for key in ("general_dolphin", "general_eagle")),
            "mechas": sum(self.variant_inventory[key] for key in (
                "mecha_lion", "mecha_eagle", "mecha_dolphin", "mecha_tiger", "mecha_bull", "mecha_black_lion", "mecha_shark"
            )),
        }

    def _selected_group_total(self) -> int:
        return self._totals()[ARMY_VARIANT_GROUPS[self.selected_variant]]

    def _selected_group_limit(self) -> int:
        return getattr(self.units, ARMY_VARIANT_GROUPS[self.selected_variant])

    def _can_save(self) -> bool:
        totals = self._totals()
        return totals["generals"] == self.units.generals and totals["mechas"] == self.units.mechas

    def _refresh_button_state(self) -> None:
        self.save_button.disabled = not self._can_save()

    def _message(self) -> str:
        totals = self._totals()
        owned = self._owned_totals()
        selected_owned = self.variant_inventory.get(self.selected_variant, 0)
        return "\n".join(
            [
                "SELECT YOUR RAID LOADOUT",
                "",
                "GENERAL SLOTS",
                f"Allowed in this raid: {self.units.generals}",
                f"Assigned: {totals['generals']} / {self.units.generals}",
                f"Owned in inventory: {owned['generals']}",
                "",
                "MECHA SLOTS",
                f"Allowed in this raid: {self.units.mechas}",
                f"Assigned: {totals['mechas']} / {self.units.mechas}",
                f"Owned in inventory: {owned['mechas']}",
                "",
                f"Selected variant: {army_variant_label(self.selected_variant)}",
                f"Owned: {selected_owned}",
                f"Assigned: {self.variant_counts[self.selected_variant]}",
                "Use the buttons to change the assigned amount.",
            ]
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This setup menu is not for you.", ephemeral=True)
            return False
        return True

    async def _refresh(self, interaction: discord.Interaction) -> None:
        self.variant_menu.options = self._variant_options()
        self._refresh_button_state()
        await interaction.response.edit_message(content=self._message(), view=self)

    async def _handle_variant_select(self, interaction: discord.Interaction) -> None:
        self.selected_variant = self.variant_menu.values[0]
        await self._refresh(interaction)

    def _adjust(self, delta: int) -> str | None:
        group = ARMY_VARIANT_GROUPS[self.selected_variant]
        total = self._selected_group_total()
        limit = self._selected_group_limit()
        owned = self.variant_inventory.get(self.selected_variant, 0)
        new_value = max(0, self.variant_counts[self.selected_variant] + delta)
        if new_value > owned:
            return f"You only own {owned} {army_variant_label(self.selected_variant)}."
        projected = total - self.variant_counts[self.selected_variant] + new_value
        if projected > limit:
            remaining = max(0, limit - total)
            return f"That would exceed the available {group} slots. {remaining} slot(s) remain."
        self.variant_counts[self.selected_variant] = new_value
        return None

    async def _adjust_and_refresh(self, interaction: discord.Interaction, delta: int) -> None:
        error = self._adjust(delta)
        if error is not None:
            await interaction.response.send_message(error, ephemeral=True)
            return
        await self._refresh(interaction)

    async def _handle_minus_one(self, interaction: discord.Interaction) -> None:
        await self._adjust_and_refresh(interaction, -1)

    async def _handle_plus_one(self, interaction: discord.Interaction) -> None:
        await self._adjust_and_refresh(interaction, 1)

    async def _handle_save(self, interaction: discord.Interaction) -> None:
        totals = self._totals()
        if totals["generals"] != self.units.generals or totals["mechas"] != self.units.mechas:
            parts: list[str] = []
            if totals["generals"] != self.units.generals:
                parts.append(f"Select {self.units.generals - totals['generals']} more General slot(s).")
            if totals["mechas"] != self.units.mechas:
                parts.append(f"Select {self.units.mechas - totals['mechas']} more Mecha slot(s).")
            await interaction.response.send_message(" ".join(parts), ephemeral=True)
            return
        self.bot.store.set_participant_loaded_variants(self.raid_id, self.user_id, self.variant_counts)
        await interaction.response.edit_message(content="\n".join(["Raid loadout saved.", self._message()]), view=None)
        participant = self.bot.store.get_participant(self.raid_id, self.user_id)
        if participant is not None and interaction.channel is not None:
            saved_variants = self.bot.store.get_participant_loaded_variants(self.raid_id, self.user_id)
            await interaction.channel.send(
                embed=joined_embed(
                    participant["discord_name"],
                    Units.from_row(participant),
                    saved_variants,
                )
            )


class ArmyVariantSetupView(discord.ui.View):
    def __init__(self, bot: ArcadionBot, user_id: int, units: Units, variants: dict[str, int]) -> None:
        super().__init__(timeout=900)
        self.bot = bot
        self.user_id = user_id
        self.units = units
        self.variant_counts = {key: max(0, int(variants.get(key, 0))) for key in ARMY_VARIANT_ORDER}
        self.selected_variant = "general_dolphin"
        self.variant_menu = discord.ui.Select(
            placeholder="Choose a variant",
            min_values=1,
            max_values=1,
            options=self._variant_options(),
        )
        self.minus_one = discord.ui.Button(label="-1", style=discord.ButtonStyle.secondary)
        self.minus_five = discord.ui.Button(label="-5", style=discord.ButtonStyle.secondary)
        self.plus_one = discord.ui.Button(label="+1", style=discord.ButtonStyle.primary)
        self.plus_five = discord.ui.Button(label="+5", style=discord.ButtonStyle.primary)
        self.save_button = discord.ui.Button(label="Save Setup", style=discord.ButtonStyle.green)
        self.variant_menu.callback = self._handle_variant_select
        self.minus_one.callback = self._handle_minus_one
        self.minus_five.callback = self._handle_minus_five
        self.plus_one.callback = self._handle_plus_one
        self.plus_five.callback = self._handle_plus_five
        self.save_button.callback = self._handle_save
        self.add_item(self.variant_menu)
        self.add_item(self.minus_one)
        self.add_item(self.minus_five)
        self.add_item(self.plus_one)
        self.add_item(self.plus_five)
        self.add_item(self.save_button)

    def _variant_options(self) -> list[discord.SelectOption]:
        return [
            discord.SelectOption(
                label=f"{army_variant_emoji(key)} {army_variant_label(key)} ({self.variant_counts[key]})",
                value=key,
                description=f"Configured: {self.variant_counts[key]}",
            )
            for key in ARMY_VARIANT_ORDER
        ]

    def _variant_totals(self) -> dict[str, int]:
        return {
            "generals": sum(self.variant_counts[key] for key in ("general_dolphin", "general_eagle")),
            "mechas": sum(self.variant_counts[key] for key in (
                "mecha_lion",
                "mecha_eagle",
                "mecha_dolphin",
                "mecha_tiger",
                "mecha_bull",
                "mecha_black_lion",
                "mecha_shark",
            )),
        }

    def _message(self) -> str:
        grouped = self._variant_totals()
        lines = [
            "CLASSIC ARMY VARIANT SETUP",
            "",
            f"Generals: {grouped['generals']} / {self.units.generals} configured",
            f"Remaining: {max(0, self.units.generals - grouped['generals'])}",
            "",
            f"Mechas: {grouped['mechas']} / {self.units.mechas} configured",
            f"Remaining: {max(0, self.units.mechas - grouped['mechas'])}",
            "",
            f"Selected variant: {army_variant_label(self.selected_variant)}",
            f"Current selected amount: {self.variant_counts[self.selected_variant]}",
        ]
        return "\n".join(lines)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This setup menu is not for you.", ephemeral=True)
            return False
        return True

    async def _refresh(self, interaction: discord.Interaction) -> None:
        self.variant_menu.options = self._variant_options()
        await interaction.response.edit_message(content=self._message(), view=self)

    async def _handle_variant_select(self, interaction: discord.Interaction) -> None:
        self.selected_variant = self.variant_menu.values[0]
        await self._refresh(interaction)

    def _adjust(self, delta: int) -> str | None:
        total = self._variant_totals()[ARMY_VARIANT_GROUPS[self.selected_variant]]
        limit = getattr(self.units, ARMY_VARIANT_GROUPS[self.selected_variant])
        new_value = max(0, self.variant_counts[self.selected_variant] + delta)
        projected = total - self.variant_counts[self.selected_variant] + new_value
        if projected > limit:
            return f"Configured {ARMY_VARIANT_GROUPS[self.selected_variant].title()} variants would exceed the available total: {projected} / {limit}"
        self.variant_counts[self.selected_variant] = new_value
        return None

    async def _adjust_and_refresh(self, interaction: discord.Interaction, delta: int) -> None:
        error = self._adjust(delta)
        if error is not None:
            await interaction.response.send_message(error, ephemeral=True)
            return
        await self._refresh(interaction)

    async def _handle_minus_one(self, interaction: discord.Interaction) -> None:
        await self._adjust_and_refresh(interaction, -1)

    async def _handle_minus_five(self, interaction: discord.Interaction) -> None:
        await self._adjust_and_refresh(interaction, -5)

    async def _handle_plus_one(self, interaction: discord.Interaction) -> None:
        await self._adjust_and_refresh(interaction, 1)

    async def _handle_plus_five(self, interaction: discord.Interaction) -> None:
        await self._adjust_and_refresh(interaction, 5)

    async def _handle_save(self, interaction: discord.Interaction) -> None:
        totals = self._variant_totals()
        if totals["generals"] != self.units.generals:
            await interaction.response.send_message(f"General variants must equal the total Generals: {totals['generals']} / {self.units.generals}", ephemeral=True)
            return
        if totals["mechas"] != self.units.mechas:
            await interaction.response.send_message(f"Mecha variants must equal the total Mechas: {totals['mechas']} / {self.units.mechas}", ephemeral=True)
            return
        try:
            self.bot.store.set_player_unit_variants(self.user_id, self.variant_counts)
        except Exception:
            await interaction.response.send_message("Could not save your variant setup.", ephemeral=True)
            return
        await interaction.response.edit_message(
            content="\n".join(
                [
                    "CLASSIC ARMY VARIANT SETUP SAVED",
                    "",
                    format_variant_summary(self.variant_counts, self.units),
                ]
            ),
            view=None,
        )


def raid_created_embed(raid: object) -> discord.Embed:
    embed = discord.Embed(title=f"?? {raid['name']} appears in {raid['city']}", color=0x8B0000)
    embed.add_field(name="Status", value=raid["state"], inline=True)
    embed.add_field(name="Level", value=raid["level"], inline=True)
    embed.add_field(name="Corruption", value=f"{format_number(raid['current_corruption'])} / {format_number(raid['max_corruption'])}", inline=False)
    embed.add_field(name="Corrupted Army", value=format_units(Units.from_row(raid, "arcadion_")), inline=False)
    embed.add_field(name="Corrupted Army Power", value=format_number(Units.from_row(raid, "arcadion_").power()), inline=False)
    embed.add_field(name="Military Power Limit", value=format_number(int(raid["power_limit"] or 0)) if int(raid["power_limit"] or 0) > 0 else "No limit", inline=False)
    embed.add_field(name="Recruitment", value="Use `/raid_join` to send troops.", inline=False)
    return embed


def status_embed(raid: object, participants: list[object]) -> discord.Embed:
    active_participants = [participant for participant in participants if int(participant["bulls_hp"] or 0) + int(participant["rhinos_hp"] or 0) + int(participant["lieutenants_hp"] or 0) + int(participant["generals_hp"] or 0) + int(participant["mechas_hp"] or 0) > 0 and str(participant["status"] or "ACTIVE") != "ELIMINATED"]
    total_damage = sum(int(participant["damage_done"] or 0) for participant in participants)
    total_attacks = sum(int(participant["attacks"] or 0) for participant in participants)
    participant_count = len(active_participants)
    raid_power = sum(
        int(participant["bulls_hp"] or 0)
        + int(participant["rhinos_hp"] or 0)
        + int(participant["lieutenants_hp"] or 0)
        + int(participant["generals_hp"] or 0)
        + int(participant["mechas_hp"] or 0)
        for participant in active_participants
    )
    corruption_current = int(raid["current_corruption"] or 0)
    corruption_max = max(1, int(raid["max_corruption"] or 1))
    percent_complete = round((1 - (corruption_current / corruption_max)) * 100)

    embed = discord.Embed(title=f"?? RAID STATUS: {raid['name']}", color=0x1F8B4C if raid["state"] == RaidState.RECRUITING.value else 0xB22222)
    embed.add_field(name="City", value=raid["city"], inline=True)
    embed.add_field(name="Level", value=raid["level"], inline=True)
    embed.add_field(name="State", value=raid["state"], inline=True)
    embed.add_field(name="Corruption", value=f"{format_number(corruption_current)} / {format_number(int(raid['max_corruption']))}", inline=False)
    embed.add_field(name="Completion", value=f"{percent_complete}%", inline=True)
    embed.add_field(name="Participants", value=str(participant_count), inline=True)
    embed.add_field(name="Remaining Military Power", value=format_number(raid_power), inline=True)
    embed.add_field(name="Total Damage Dealt", value=format_number(total_damage), inline=True)
    embed.add_field(name="Total Attacks", value=str(total_attacks), inline=True)
    embed.add_field(name="Loot Upx", value=format_number(int(raid['total_loot_upx'] or 0)), inline=True)
    embed.add_field(name="Power Limit", value=format_number(int(raid['power_limit'] or 0)) if int(raid['power_limit'] or 0) > 0 else "No limit", inline=True)

    if raid["state"] == RaidState.BATTLE.value and raid["current_turn_discord_id"]:
        current_player = next((participant for participant in participants if str(participant["discord_id"]) == str(raid["current_turn_discord_id"])), None)
        if current_player is not None:
            embed.add_field(name="Current Turn", value=f"<@{current_player['discord_id']}>", inline=False)
            embed.add_field(name="Turn Military Power", value=format_number(sum(int(current_player[field]) or 0 for field in ("bulls_hp", "rhinos_hp", "lieutenants_hp", "generals_hp", "mechas_hp"))), inline=False)
            embed.add_field(name="Turn Deadline", value=raid["turn_deadline_at"] or "Unknown", inline=False)

    if participants:
        ordered = sorted(participants, key=lambda participant: (-int(participant["damage_done"] or 0), -int(participant["attacks"] or 0), str(participant["discord_name"])))
        lines = []
        for index, participant in enumerate(ordered[:10], start=1):
            remaining_power = int(participant["bulls_hp"] or 0) + int(participant["rhinos_hp"] or 0) + int(participant["lieutenants_hp"] or 0) + int(participant["generals_hp"] or 0) + int(participant["mechas_hp"] or 0)
            lines.append(f"{index}. {participant['discord_name']} - {format_number(remaining_power)} HP - {format_number(int(participant['damage_done'] or 0))} dmg")
        embed.add_field(name="Top Commanders", value="\n".join(lines), inline=False)

    return embed


def counterattack_summary(target_name: str, damage: int, destroyed: Units, remaining: Units, arcadion_dice: str | None = None) -> str:
    lines = [
        "?? Arcadion Counterattack!",
        "",
        "Damage Dealt:",
        f"{format_number(damage)}",
    ]
    if arcadion_dice:
        lines.extend(["", f"Dice: {arcadion_dice}"])
    lines.extend(["", "Units Destroyed"])

    destroyed_any = False
    for field in ("bulls", "rhinos", "lieutenants", "generals", "mechas"):
        amount = getattr(destroyed, field)
        if amount > 0:
            destroyed_any = True
            label = UNIT_LABELS[field]
            lines.append(f"-{amount} {label}{'' if amount == 1 else 's'}")

    if not destroyed_any:
        lines.append("-No units destroyed")

    lines.extend(["", "Remaining Army"])
    for field in ("bulls", "rhinos", "lieutenants", "generals", "mechas"):
        amount = getattr(remaining, field)
        if amount > 0:
            label = UNIT_LABELS[field]
            lines.append(f"{label}: {amount}")

    if not any(getattr(remaining, field) > 0 for field in ("bulls", "rhinos", "lieutenants", "generals", "mechas")):
        lines.append("All units destroyed")

    lines.extend(["", "Remaining Military Power", f"{format_number(remaining.power())}"])
    return "\n".join(lines)


def loot_summary_embed(raid: object, participants: list[object], result_message: str) -> discord.Embed:
    total_loot = max(0, int(raid["total_loot_upx"] or 0))
    contributions = [(participant["discord_name"], int(participant["damage_done"] or 0)) for participant in participants]
    distribution = calculate_loot_distribution(total_loot, contributions)

    embed = discord.Embed(title=f"?? RAID COMPLETE: {raid['name']}", color=0x1F8B4C if str(raid['result'] or '').upper() in ('PLAYERS', 'SUCCESS') else 0x8B0000)
    embed.add_field(name="Result", value=result_message, inline=False)
    embed.add_field(name="City", value=raid["city"], inline=True)
    embed.add_field(name="Level", value=raid["level"], inline=True)
    embed.add_field(name="Final Corruption", value=f"{format_number(int(raid['current_corruption'] or 0))} / {format_number(int(raid['max_corruption'] or 0))}", inline=False)
    embed.add_field(name="Total Loot UPX", value=format_number(total_loot), inline=True)
    embed.add_field(name="Participants", value=str(len(participants)), inline=True)

    if distribution:
        lines = []
        for entry in distribution:
            lines.append(f"{entry['player']} - {entry['participation_percent']} - {format_number(int(entry['damage']))} damage - {format_number(int(entry['reward']))} UPX")
        embed.add_field(name="Loot Distribution", value="\n".join(lines[:10]), inline=False)

    if participants:
        ordered = sorted(participants, key=lambda participant: (-int(participant["damage_done"] or 0), -int(participant["attacks"] or 0), str(participant["discord_name"])))
        lines = []
        for index, participant in enumerate(ordered[:10], start=1):
            lines.append(f"{index}. {participant['discord_name']} - {format_number(int(participant['damage_done'] or 0))} damage - {format_number(int(participant['attacks'] or 0))} attacks")
        embed.add_field(name="Top Damage", value="\n".join(lines), inline=False)

    return embed


def joined_embed(name: str, units: Units, variants: dict[str, int] | None = None) -> discord.Embed:
    embed = discord.Embed(title="🛡️ COMMANDER DEPLOYED", color=0x2E8B57)
    force_lines = []
    for key, label in (
        ("bulls", "Bull Soldiers"),
        ("rhinos", "Rhino Soldiers"),
        ("lieutenants", "Lion Lieutenants"),
    ):
        amount = int(getattr(units, key))
        if amount:
            force_lines.append(f"{label} ×{amount}")
    variant_keys = (
        "general_dolphin",
        "general_eagle",
        "mecha_lion",
        "mecha_eagle",
        "mecha_dolphin",
        "mecha_tiger",
        "mecha_bull",
        "mecha_black_lion",
        "mecha_shark",
    )
    if variants is None:
        if units.generals:
            force_lines.append(f"Generals ×{units.generals}")
        if units.mechas:
            force_lines.append(f"Mechas ×{units.mechas}")
    else:
        for key in variant_keys:
            amount = int(variants.get(key, 0))
            if amount:
                force_lines.append(f"{army_variant_emoji(key)} {army_variant_label(key)} ×{amount}")
    embed.add_field(name=f"⚔️ {name} has joined the battle!", value="\u200b", inline=False)
    embed.add_field(name="🪖 Forces Deployed", value="\n".join(force_lines) or "No units", inline=False)
    embed.add_field(name="⚡ Military Power", value=format_number(units.power()), inline=True)
    embed.add_field(name="🔥 Status", value="Ready to stand against Arcadion.", inline=False)
    return embed


def arcadion_army_embed(raid: object) -> discord.Embed:
    units = Units.from_row(raid, "arcadion_")
    current_power = combat_power_from_row(raid, "arcadion_")
    target = arcadion_target_power(raid["level"])
    target_text = format_number(target) if target is not None else "Not defined for this level"
    embed = discord.Embed(title="☣️ ARCADION ARMY", color=0x8B0000)
    embed.add_field(name="Level", value=str(raid["level"]), inline=True)
    embed.add_field(name="⚔️ Military Power", value=f"{format_number(current_power)} / {target_text}", inline=False)
    embed.add_field(
        name="Army",
        value=(
            f"🐂 Bulls: {units.bulls}\n"
            f"🦏 Rhinos: {units.rhinos}\n"
            f"🦁 Lieutenants: {units.lieutenants}\n"
            f"🦅 Generals: {units.generals}\n"
            f"🤖 Mechas: {units.mechas}"
        ),
        inline=False,
    )
    return embed


def battle_started_embed(raid: object, first_player_name: str) -> discord.Embed:
    embed = discord.Embed(title=f"?? BATTLE STARTED: {raid['name']}", color=0xB22222)
    embed.add_field(name="City", value=raid["city"], inline=True)
    embed.add_field(name="Ends At", value=raid["ends_at"], inline=False)
    embed.add_field(name="Current Turn", value=first_player_name, inline=False)
    embed.add_field(name="Turn Time", value="2 minutes", inline=False)
    embed.add_field(name="Order", value="Use `/attack` to strike Arcadion.", inline=False)
    return embed


def attack_embed(
    attacker_name: str,
    attacker_roll: object,
    raid: object,
    arcadion_roll: object,
    target_name: str,
    destroyed: Units,
    arcadion_guard_destroyed: Units,
    damage_to_corruption: int,
    bonus_label: str | None,
    bonus_details: str | None,
    bonus_activated: bool,
    bonus_damage: int,
    wounded_lines: list[str] | None,
    result_message: str | None,
) -> discord.Embed:
    player_rolls = " · ".join(str(roll) for roll in getattr(attacker_roll, "rolls", [])) or "No dice"
    arcadion_rolls = " · ".join(str(roll) for roll in getattr(arcadion_roll, "rolls", [])) or "No dice"
    embed = discord.Embed(title=f"⚔️ {attacker_name.upper()} ATTACKS ARCADION", color=0xDAA520)

    embed.add_field(
        name="🎲 DICE CLASH",
        value=(
            f"**{attacker_name}**\n"
            f"🎲 {player_rolls}\n"
            f"✅ {count_dice_successes(getattr(attacker_roll, 'rolls', []))} successes"
        ),
        inline=True,
    )
    embed.add_field(
        name="💥 DAMAGE",
        value=(
            f"Classic: **{format_number(attacker_roll.damage)}**\n"
            + (f"Mecha Bonus: **+{format_number(bonus_damage)}**\n" if bonus_label is not None else "")
            + f"Total: **{format_number(attacker_roll.damage + bonus_damage)}**"
        ),
        inline=True,
    )
    if bonus_label is not None:
        bonus_state = "✅ ACTIVATED" if bonus_activated else "❌ NOT ACTIVATED"
        bonus_value = f"{bonus_label}\n{bonus_state}"
        if bonus_details:
            bonus_value += f"\n{bonus_details}"
        embed.add_field(name="🤖 MECHA BONUS", value=bonus_value, inline=False)

    arcadion_value = (
        f"Corruption: **{format_number(raid['current_corruption'])} / "
        f"{format_number(raid['max_corruption'])}**\n"
        f"Damage to corruption: **{format_number(damage_to_corruption)}**"
    )
    if arcadion_guard_destroyed.has_any():
        arcadion_value += f"\nGuard destroyed: **{format_units(arcadion_guard_destroyed)}**"
    embed.add_field(name="☠️ ARCADION", value=arcadion_value, inline=False)

    retaliation_value = (
        f"🎲 {arcadion_rolls}\n"
        f"💥 **{format_number(arcadion_roll.damage)} damage**\n"
        f"🎯 Target: **{target_name}**"
    )
    if destroyed.has_any():
        retaliation_value += f"\n❌ {format_units(destroyed)}"
    if wounded_lines:
        retaliation_value += f"\n🩹 {'; '.join(wounded_lines)}"
    embed.add_field(name="⚡ ARCADION RETALIATES", value=retaliation_value, inline=False)
    if result_message:
        embed.add_field(name="➡️ RESULT", value=result_message, inline=False)
    return embed


def build_wounded_lines(original_health: dict[str, int], remaining_health: dict[str, int], remaining_units: Units) -> list[str]:
    lines: list[str] = []
    names = {
        "bulls": "Bull Soldier",
        "rhinos": "Rhino Soldier",
        "lieutenants": "Lion Lieutenant",
        "generals": "General",
        "mechas": "Mecha",
    }
    for name in ("bulls", "rhinos", "lieutenants", "generals", "mechas"):
        if getattr(remaining_units, name) <= 0:
            continue
        original = int(original_health.get(name, 0))
        remaining = int(remaining_health.get(name, 0))
        if original > remaining and remaining > 0:
            lines.append(f"The {names[name]} has been wounded in combat.")
    return lines


def main() -> None:
    settings = load_settings()
    store = Store(settings.database_path)
    bot = ArcadionBot(store, settings.guild_id)
    bot.run(settings.discord_token)


