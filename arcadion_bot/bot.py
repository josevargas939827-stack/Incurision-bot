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

TURN_TIME_LIMIT_SECONDS = 300
TURN_REMINDER_SECONDS = (120, 60)
TURN_INACTIVITY_TIMEOUTS_BEFORE_KICK = 2


class ArcadionBot(commands.Bot):
    def __init__(self, store: Store, guild_id: int | None) -> None:
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self.store = store
        self.guild_id = guild_id
        self.raid_leaders: dict[int, int] = {}
        self.turn_timeout_tasks: dict[int, asyncio.Task[None]] = {}
        self.turn_inactivity_strikes: dict[tuple[int, int], int] = {}

    async def setup_hook(self) -> None:
        self.store.init()
        register_commands(self)
        self.loop.create_task(self.manage_turn_notifications())
        if self.guild_id:
            guild = discord.Object(id=self.guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            self.tree.clear_commands(guild=None)
            await self.tree.sync()
        else:
            await self.tree.sync()

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
                for threshold, reminder_name in zip(TURN_REMINDER_SECONDS, ("TWO_MINUTES", "ONE_MINUTE")):
                    if remaining_seconds <= threshold and reminder_state == "NONE":
                        self.store.set_turn_reminder_state(raid["id"], reminder_name)
                        if channel is not None:
                            await announce_turn_reminder(self, raid, channel, 2 if threshold == TURN_REMINDER_SECONDS[0] else 1)
                        break
                    if remaining_seconds <= threshold and reminder_state == "TWO_MINUTES" and threshold == TURN_REMINDER_SECONDS[1]:
                        self.store.set_turn_reminder_state(raid["id"], reminder_name)
                        if channel is not None:
                            await announce_turn_reminder(self, raid, channel, 1)
                        break
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
            turn_order = json.loads(raid["turn_order"] or "[]")
            turn_order = [current_id for current_id in turn_order if str(current_id) != str(discord_id)]
            current_turn = raid["current_turn_discord_id"]
            is_current_player = str(current_turn) == str(discord_id)
            conn.execute(
                "DELETE FROM raid_participants WHERE raid_id = ? AND discord_id = ?",
                (raid_id, str(discord_id)),
            )
            if is_current_player:
                if turn_order:
                    conn.execute(
                        "UPDATE raids SET turn_order = ?, current_turn_discord_id = ?, turn_index = 0, turn_round = 1, turn_started_at = ?, turn_deadline_at = ?, announcement_channel_id = ?, turn_reminder_state = 'NONE' WHERE id = ?",
                        (json.dumps(turn_order), turn_order[0], utc_now(), (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(), str(channel_id) if channel_id is not None else None, raid_id),
                    )
                else:
                    conn.execute(
                        "UPDATE raids SET turn_order = ?, current_turn_discord_id = NULL, turn_started_at = NULL, turn_deadline_at = NULL, turn_index = 0, turn_round = 1, announcement_channel_id = ?, turn_reminder_state = 'NONE' WHERE id = ?",
                        (json.dumps([]), str(channel_id) if channel_id is not None else None, raid_id),
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
                            (turn_order[0], utc_now(), (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(), raid_id),
                        )
                    else:
                        conn.execute(
                            "UPDATE raids SET current_turn_discord_id = NULL, turn_started_at = NULL, turn_deadline_at = NULL, turn_index = 0, turn_round = 1 WHERE id = ?",
                            (raid_id,),
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
        await interaction.response.send_message(embed=army_embed(row["discord_name"], Units.from_row(row), "Permanent army"))

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
        raid_id = bot.store.create_raid(name, city, level, max_corruption_value, duration_hours_value, arcadion_units, total_loot_value, power_limit_value)
        bot.set_raid_leader(raid_id, interaction.user.id)
        raid = bot.store.get_raid(raid_id)
        await interaction.response.send_message(embed=raid_created_embed(raid))

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
        await interaction.response.send_message(embed=joined_embed(interaction.user.display_name, sent))

    @bot.tree.command(name="raid_start", description="Start the active raid battle.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def raid_start(interaction: discord.Interaction) -> None:
        raid = bot.store.get_active_raid()
        if raid is None or raid["state"] != RaidState.RECRUITING.value:
            await interaction.response.send_message("There is no recruiting raid to start.", ephemeral=True)
            return
        leader_id = bot.get_raid_leader(raid["id"])
        if leader_id is None and interaction.user.guild_permissions.manage_guild:
            bot.set_raid_leader(raid["id"], interaction.user.id)
            leader_id = interaction.user.id
        if leader_id != interaction.user.id:
            await interaction.response.send_message("Only the raid leader can use this command.", ephemeral=True)
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
            await interaction.response.send_message(
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
        bot.cancel_turn_timeout(raid["id"])
        dice_count = attack_dice_count(bot.store, raid["id"], interaction.user.id)
        attacker_roll = roll_dice(dice_count)
        arcadion_guard = Units.from_row(raid, "arcadion_")
        arcadion_guard_health = combat_health_from_row(raid, "arcadion_")
        guard_remaining, guard_destroyed, guard_remaining_health, guard_absorbed = apply_wounded_combat_damage(arcadion_guard, arcadion_guard_health, attacker_roll.damage)
        damage_to_corruption = max(0, attacker_roll.damage - guard_absorbed)
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
        if target is not None:
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
            attacker_roll.damage,
            arcadion_roll.text,
            arcadion_roll.damage,
            target["discord_id"] if target else None,
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
                    wounded_lines=wounded_lines,
                    result_message=result_message,
                )
            )
            return

        completed_raid = bot.store.get_raid(raid["id"])
        await interaction.response.send_message(
            embed=loot_summary_embed(completed_raid, bot.store.list_participants(raid["id"]), result_message)
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
        if bot.get_raid_leader(raid["id"]) != interaction.user.id:
            await interaction.response.send_message("Only the raid leader can use this command.", ephemeral=True)
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
        if bot.get_raid_leader(raid["id"]) != interaction.user.id:
            await interaction.response.send_message("Only the raid leader can use this command.", ephemeral=True)
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
                        (json.dumps(turn_order), next_player_id, utc_now(), (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(), raid["id"]),
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
                            (turn_order[0], utc_now(), (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(), raid["id"]),
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
        if bot.get_raid_leader(raid["id"]) != interaction.user.id:
            await interaction.response.send_message("Only the raid leader can use this command.", ephemeral=True)
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

    @bot.tree.command(name="raid_finish", description="Manually finish the active raid.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def raid_finish(interaction: discord.Interaction) -> None:
        raid = bot.store.get_active_raid()
        if raid is None:
            await interaction.response.send_message("There is no active raid.", ephemeral=True)
            return
        if bot.get_raid_leader(raid["id"]) != interaction.user.id:
            await interaction.response.send_message("Only the raid leader can use this command.", ephemeral=True)
            return
        bot.cancel_turn_timeout(raid["id"])
        bot.store.finish_raid(raid["id"], "MANUAL")
        bot.clear_raid_leader(raid["id"])
        finished_raid = bot.store.get_raid(raid["id"])
        await interaction.response.send_message(embed=loot_summary_embed(finished_raid, bot.store.list_participants(raid["id"]), "The raid was manually finished."))


def attack_dice_count(store: Store, raid_id: int, discord_id: int) -> int:
    fallen = store.get_modifier(raid_id, discord_id, ModifierType.FALLEN_LIEUTENANT)
    if fallen is not None and int(fallen["remaining_turns"]) > 0:
        return 1
    return 3


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
        f"{phase_header}\n\nState: {phase_message}\n\nCommander:\n<@{current_player['discord_id']}>\n\nRemaining Military Power:\n{format_number(participant.power())}\n\nTime available:\n5 minutes"
    )
    bot.start_turn_timeout(raid["id"], current_player["discord_id"], bot._channel_id_for_raid(raid))


async def announce_turn_reminder(bot: ArcadionBot, raid: object, channel: discord.TextChannel | None, minutes: int) -> None:
    if channel is None:
        return
    current_player = bot.store.get_participant(raid["id"], raid["current_turn_discord_id"]) if raid["current_turn_discord_id"] else None
    if current_player is None:
        return
    if minutes == 2:
        await channel.send(f"? Reminder\n\n<@{current_player['discord_id']}>\n\n2 minutes remaining.\n\nAttack Arcadion before your turn expires.")
    elif minutes == 1:
        await channel.send(f"?? Final Reminder\n\n<@{current_player['discord_id']}>\n\nOnly 1 minute remaining.\n\nAttack now or Arcadion will launch a surprise attack.")


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


def joined_embed(name: str, units: Units) -> discord.Embed:
    embed = discord.Embed(title=f"??? {name} joins the raid", color=0x2E8B57)
    embed.add_field(name="Troops Sent", value=format_units(units), inline=False)
    embed.add_field(name="Raid Power", value=format_number(units.power()), inline=False)
    return embed


def battle_started_embed(raid: object, first_player_name: str) -> discord.Embed:
    embed = discord.Embed(title=f"?? BATTLE STARTED: {raid['name']}", color=0xB22222)
    embed.add_field(name="City", value=raid["city"], inline=True)
    embed.add_field(name="Ends At", value=raid["ends_at"], inline=False)
    embed.add_field(name="Current Turn", value=first_player_name, inline=False)
    embed.add_field(name="Turn Time", value="5 minutes", inline=False)
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
    wounded_lines: list[str] | None,
    result_message: str | None,
) -> discord.Embed:
    embed = discord.Embed(title=f"?? {attacker_name.upper()} ATTACKS", color=0xDAA520)
    embed.add_field(name="Dice", value=attacker_roll.text, inline=False)
    embed.add_field(name="Total Damage", value=format_number(attacker_roll.damage), inline=False)
    embed.add_field(
        name="Arcadion",
        value=f"{format_number(raid['current_corruption'])} / {format_number(raid['max_corruption'])}",
        inline=False,
    )
    embed.add_field(name="Damage to Corruption", value=format_number(damage_to_corruption), inline=True)
    embed.add_field(name="Corrupted Guard Destroyed", value=format_units(arcadion_guard_destroyed), inline=False)
    embed.add_field(name="?? ARCADION RETALIATES", value="\u200b", inline=False)
    embed.add_field(name="Dice", value=arcadion_roll.text, inline=True)
    embed.add_field(name="Damage", value=format_number(arcadion_roll.damage), inline=True)
    embed.add_field(name="Target", value=target_name, inline=False)
    embed.add_field(name="Destroyed Units", value=format_units(destroyed), inline=False)
    if wounded_lines:
        embed.add_field(name="Wounded Units", value="\n".join(wounded_lines), inline=False)
    if result_message:
        embed.add_field(name="Result", value=result_message, inline=False)
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
