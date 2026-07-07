from __future__ import annotations

import json
import random
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from .config import load_settings
from .game import (
    ModifierType,
    RaidState,
    UNIT_LABELS,
    Units,
    apply_damage_to_units,
    calculate_loot_distribution,
    format_number,
    format_units,
    parse_integer,
    roll_dice,
)
from .storage import Store


class ArcadionBot(commands.Bot):
    def __init__(self, store: Store, guild_id: int | None) -> None:
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self.store = store
        self.guild_id = guild_id

    async def setup_hook(self) -> None:
        self.store.init()
        register_commands(self)
        if self.guild_id:
            guild = discord.Object(id=self.guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            self.tree.clear_commands(guild=None)
            await self.tree.sync()
        else:
            await self.tree.sync()


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
            await interaction.response.send_message("❌ You do not own enough units.", ephemeral=True)
            return

        if bot.store.remove_player_units(interaction.user.id, delta):
            updated = bot.store.get_player(interaction.user.id)
            updated_units = Units.from_row(updated) if updated is not None else Units()
            await interaction.response.send_message(format_unit_change_message("Removed", delta, updated_units))
        else:
            await interaction.response.send_message("❌ You do not own enough units.", ephemeral=True)

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
    ) -> None:
        try:
            max_corruption_value = parse_integer(max_corruption)
            duration_hours_value = parse_integer(duration_hours)
            total_loot_value = parse_integer(total_loot_upx)
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
        raid_id = bot.store.create_raid(name, city, level, max_corruption_value, duration_hours_value, arcadion_units, total_loot_value)
        raid = bot.store.get_raid(raid_id)
        await interaction.response.send_message(embed=raid_created_embed(raid))

    @bot.tree.command(name="raid_join", description="Send troops to the recruiting raid.")
    async def raid_join(interaction: discord.Interaction, bulls: str = "0", rhinos: str = "0", lieutenants: str = "0", generals: str = "0", mechas: str = "0") -> None:
        raid = bot.store.get_active_raid()
        if raid is None or raid["state"] != RaidState.RECRUITING.value:
            await interaction.response.send_message("There is no raid in recruitment.", ephemeral=True)
            return

        try:
            sent = units_from_args(bulls, rhinos, lieutenants, generals, mechas)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        if not sent.has_any():
            await interaction.response.send_message("You must send at least one unit.", ephemeral=True)
            return

        participant = bot.store.get_participant(raid["id"], interaction.user.id)
        if participant is not None and participant["status"] == "ELIMINATED":
            await interaction.response.send_message("❌ You cannot rejoin this raid after being eliminated.", ephemeral=True)
            return

        player = bot.store.get_player(interaction.user.id)
        if player is None:
            await interaction.response.send_message("Register your army with `/army_set` first.", ephemeral=True)
            return
        permanent = Units.from_row(player)
        if not permanent.contains(sent):
            await interaction.response.send_message("You cannot send more units than you have in your permanent army.", ephemeral=True)
            return

        bot.store.upsert_participant(raid["id"], interaction.user.id, interaction.user.display_name, sent)
        await interaction.response.send_message(embed=joined_embed(interaction.user.display_name, sent))

    @bot.tree.command(name="raid_start", description="Start the active raid battle.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def raid_start(interaction: discord.Interaction) -> None:
        raid = bot.store.get_active_raid()
        if raid is None or raid["state"] != RaidState.RECRUITING.value:
            await interaction.response.send_message("There is no recruiting raid to start.", ephemeral=True)
            return
        if not bot.store.list_active_participants(raid["id"]):
            await interaction.response.send_message("The raid cannot start without participants.", ephemeral=True)
            return
        bot.store.start_raid(raid["id"], interaction.channel_id)
        raid = bot.store.get_raid(raid["id"])
        current_player = bot.store.get_participant(raid["id"], raid["current_turn_discord_id"]) if raid["current_turn_discord_id"] else None
        first_name = current_player["discord_name"] if current_player else "No one"
        await interaction.response.send_message(embed=battle_started_embed(raid, first_name))

    @bot.tree.command(name="attack", description="Attack Arcadion during the battle.")
    async def attack(interaction: discord.Interaction) -> None:
        raid = bot.store.get_active_raid()
        if raid is None or raid["state"] != RaidState.BATTLE.value:
            await interaction.response.send_message("There is no active battle.", ephemeral=True)
            return
        if raid_time_expired(raid):
            bot.store.finish_raid(raid["id"], "ARCADION")
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
            await interaction.response.send_message("❌ You have been eliminated from this raid.\n\nPlease wait until the next Arcadion Raid to fight again.", ephemeral=True)
            return

        current_units = Units.from_row(participant)
        if not current_units.has_any():
            await interaction.response.send_message("You have no active troops left to attack.", ephemeral=True)
            return

        turn_timeout = bot.store.process_turn_timeout(raid["id"], interaction.channel_id)
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

        dice_count = attack_dice_count(bot.store, raid["id"], interaction.user.id)
        attacker_roll = roll_dice(dice_count)
        arcadion_guard = Units.from_row(raid, "arcadion_")
        guard_loss = apply_damage_to_units(arcadion_guard, attacker_roll.damage)
        damage_to_corruption = max(0, attacker_roll.damage - guard_loss.absorbed_damage)
        new_corruption = max(0, int(raid["current_corruption"]) - damage_to_corruption)
        bot.store.update_arcadion_units(raid["id"], guard_loss.remaining)
        bot.store.update_corruption(raid["id"], new_corruption)
        bot.store.add_attack_stats(raid["id"], interaction.user.id, attacker_roll.damage)

        active_targets = bot.store.list_active_participants(raid["id"])
        arcadion_roll = roll_dice(2)
        target = random.choice(active_targets) if active_targets else None
        destroyed = Units()
        counterattack_message = None
        if target is not None:
            target_units = Units.from_row(target)
            loss = apply_damage_to_units(target_units, arcadion_roll.damage)
            destroyed = loss.destroyed
            bot.store.update_participant_units_and_losses(raid["id"], target["discord_id"], loss.remaining, loss.destroyed)
            counterattack_message = counterattack_summary(
                target["discord_name"],
                arcadion_roll.damage,
                destroyed,
                loss.remaining,
            )
            if bot.store.mark_participant_eliminated_if_needed(raid["id"], target["discord_id"], loss.remaining):
                await interaction.channel.send(
                    f"💀 COMMANDER ELIMINATED\n\nThe army of @{target['discord_name']} has been completely destroyed.\n\nThis commander has been eliminated from the current raid and can no longer participate.\n\nHowever, all damage dealt and battle statistics have been recorded.\n\nThe commander will still receive rewards based on their contribution when the raid ends."
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
            result_message = "Victory. Arcadion has been defeated."
            completed_raid = bot.store.get_raid(raid["id"])
            completed_raid = {**completed_raid, "result": "SUCCESS"}
        elif not active_after:
            bot.store.finish_raid(raid["id"], "ARCADION")
            result_message = "Arcadion wins. No active troops remain."
            completed_raid = bot.store.get_raid(raid["id"])
            completed_raid = {**completed_raid, "result": "FAILED"}
        elif not bot.store.list_active_participants(raid["id"]):
            bot.store.finish_raid(raid["id"], "ARCADION")
            result_message = "☠️ ARCADION IS VICTORIOUS\n\nAll deployed armies have been destroyed.\n\nThe city has fallen under Arcadion's corruption.\n\nRaid Failed."
            completed_raid = bot.store.get_raid(raid["id"])
            completed_raid = {**completed_raid, "result": "FAILED"}
        else:
            next_player = bot.store.advance_turn(raid["id"], interaction.channel_id)
            if next_player is not None and interaction.channel:
                await interaction.channel.send(f"🔄 Turn passed to {next_player['discord_name']}.")

        if counterattack_message and interaction.channel:
            await interaction.channel.send(counterattack_message)

        if result_message is None:
            await interaction.response.send_message(
                embed=attack_embed(
                    attacker_name=interaction.user.display_name,
                    attacker_roll=attacker_roll,
                    raid=refreshed,
                    arcadion_roll=arcadion_roll,
                    target_name=target["discord_name"] if target else "No one",
                    destroyed=destroyed,
                    arcadion_guard_destroyed=guard_loss.destroyed,
                    damage_to_corruption=damage_to_corruption,
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
            await interaction.response.send_message("❌ You have been eliminated from this raid and cannot use modifiers.", ephemeral=True)
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
            await interaction.response.send_message(f"✨ **RETURNING FORCE**\n{interaction.user.display_name} recovers 1 {revived}.")
            return

        if modifier == ModifierType.SOLDIER_ASCENDS:
            units = Units.from_row(participant)
            if units.bulls <= 0 and units.rhinos <= 0:
                await interaction.response.send_message("You need at least one active Bull or Rhino to promote.", ephemeral=True)
                return
            source_unit, promoted_label = promote_soldier(bot.store, raid["id"], interaction.user.id, units)
            bot.store.set_modifier(raid["id"], interaction.user.id, modifier, 0, 2, source_unit)
            await interaction.response.send_message(f"🛡️ **SOLDIER ASCENDS**\nOne {promoted_label} temporarily becomes a Lion Lieutenant for 2 turns.")

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
        participant = bot.store.get_participant(raid["id"], user.id)
        if participant is None:
            await interaction.response.send_message("That player is not participating in the raid.", ephemeral=True)
            return
        modifier = ModifierType(modifier.value)
        bot.store.set_modifier(raid["id"], user.id, modifier, 0, 2)
        await interaction.response.send_message(f"💀 **FALLEN LIEUTENANT**\n{user.display_name} will attack with 1 die for 2 turns.")

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
        bot.store.finish_raid(raid["id"], "MANUAL")
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
    embed = discord.Embed(title=f"⚔️ {title}", color=0xC9A227)
    embed.add_field(name="Commander", value=name, inline=False)
    embed.add_field(name="Units", value=format_units(units), inline=False)
    embed.add_field(name="Military Power", value=format_number(units.power()), inline=False)
    return embed


def raid_created_embed(raid: object) -> discord.Embed:
    embed = discord.Embed(title=f"☄️ {raid['name']} appears in {raid['city']}", color=0x8B0000)
    embed.add_field(name="Status", value=raid["state"], inline=True)
    embed.add_field(name="Level", value=raid["level"], inline=True)
    embed.add_field(name="Corruption", value=f"{format_number(raid['current_corruption'])} / {format_number(raid['max_corruption'])}", inline=False)
    embed.add_field(name="Corrupted Army", value=format_units(Units.from_row(raid, "arcadion_")), inline=False)
    embed.add_field(name="Corrupted Army Power", value=format_number(Units.from_row(raid, "arcadion_").power()), inline=False)
    embed.add_field(name="Recruitment", value="Use `/raid_join` to send troops.", inline=False)
    return embed


def joined_embed(name: str, units: Units) -> discord.Embed:
    embed = discord.Embed(title=f"🛡️ {name} joins the raid", color=0x2E8B57)
    embed.add_field(name="Troops Sent", value=format_units(units), inline=False)
    embed.add_field(name="Raid Power", value=format_number(units.power()), inline=False)
    return embed


def battle_started_embed(raid: object, first_player_name: str) -> discord.Embed:
    embed = discord.Embed(title=f"🔥 BATTLE STARTED: {raid['name']}", color=0xB22222)
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
    result_message: str | None,
) -> discord.Embed:
    embed = discord.Embed(title=f"⚔️ {attacker_name.upper()} ATTACKS", color=0xDAA520)
    embed.add_field(name="Dice", value=attacker_roll.text, inline=False)
    embed.add_field(name="Total Damage", value=format_number(attacker_roll.damage), inline=False)
    embed.add_field(
        name="Arcadion",
        value=f"{format_number(raid['current_corruption'])} / {format_number(raid['max_corruption'])}",
        inline=False,
    )
    embed.add_field(name="Damage to Corruption", value=format_number(damage_to_corruption), inline=True)
    embed.add_field(name="Corrupted Guard Destroyed", value=format_units(arcadion_guard_destroyed), inline=False)
    embed.add_field(name="☠️ ARCADION RETALIATES", value="\u200b", inline=False)
    embed.add_field(name="Dice", value=arcadion_roll.text, inline=True)
    embed.add_field(name="Damage", value=format_number(arcadion_roll.damage), inline=True)
    embed.add_field(name="Target", value=target_name, inline=False)
    embed.add_field(name="Destroyed Units", value=format_units(destroyed), inline=False)
    if result_message:
        embed.add_field(name="Result", value=result_message, inline=False)
    return embed


def counterattack_summary(target_name: str, damage: int, destroyed: Units, remaining: Units) -> str:
    lines = [
        "☣️ Arcadion Counterattack!",
        "",
        "Damage Dealt:",
        f"{format_number(damage)}",
        "",
        "Units Destroyed",
    ]

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


def status_embed(raid: object, participants: list[object]) -> discord.Embed:
    current = int(raid["current_corruption"])
    maximum = int(raid["max_corruption"])
    completed = 100 if maximum <= 0 else round((1 - current / maximum) * 100, 2)
    remaining_power = sum(Units.from_row(row).power() for row in participants)
    top = participants[:5]
    top_text = "\n".join(
        f"{index + 1}. {row['discord_name']} - {format_number(row['damage_done'])} damage ({row['attacks']} attacks)"
        for index, row in enumerate(top)
    )
    embed = discord.Embed(title=f"📜 Raid Status: {raid['name']}", color=0x4169E1)
    embed.add_field(name="Status", value=raid["state"], inline=True)
    embed.add_field(name="City", value=raid["city"], inline=True)
    arcadion_guard = Units.from_row(raid, "arcadion_")
    embed.add_field(name="Corruption", value=f"{format_number(current)} / {format_number(maximum)}", inline=False)
    embed.add_field(name="Arcadion Army", value=format_units(arcadion_guard), inline=False)
    embed.add_field(name="Arcadion Army Power", value=format_number(arcadion_guard.power()), inline=True)
    embed.add_field(name="Progress", value=f"{completed}%", inline=True)
    embed.add_field(name="Participants", value=str(len(participants)), inline=True)
    embed.add_field(name="Remaining Military Power", value=format_number(remaining_power), inline=False)
    embed.add_field(name="Top Damage", value=top_text or "No attacks recorded", inline=False)
    return embed


def finished_embed(raid: object, message: str) -> discord.Embed:
    embed = discord.Embed(title=f"🏁 FINISHED: {raid['name']}", description=message, color=0x696969)
    return embed


def loot_summary_embed(raid: object, participants: list[object], result_message: str | None = None) -> discord.Embed:
    total_loot = int(raid["total_loot_upx"] or 0)
    total_damage = sum(int(row["damage_done"]) for row in participants)
    is_success = str(raid.get("result") or "").upper() == "SUCCESS"
    is_victory = bool(result_message and "defeated" in result_message.lower())
    is_success = is_success or is_victory

    embed = discord.Embed(title="🏆 RAID RESULTS", color=0xFFD700)
    embed.add_field(name="Raid Status", value="SUCCESS" if is_success else "FAILED", inline=False)
    embed.add_field(name="Total Damage", value=format_number(total_damage), inline=False)

    if is_success:
        contributions = [(row["discord_name"], int(row["damage_done"])) for row in participants if int(row["damage_done"]) > 0]
        distribution = calculate_loot_distribution(total_loot, contributions)
        lines = []
        for entry in distribution:
            contribution_percent = 0 if total_damage <= 0 else round((int(entry["damage"]) / total_damage) * 100, 1)
            lines.append(
                f"🥇 {entry['player']}\nStatus: {next((row['status'] for row in participants if row['discord_name'] == entry['player']), 'ACTIVE')}\nDamage Dealt: {format_number(int(entry['damage']))}\nContribution: {contribution_percent:.1f}%\nReward: {format_number(int(entry['reward']))} UPX"
            )
        embed.add_field(name="Raid Loot", value=f"{format_number(total_loot)} UPX", inline=False)
        if lines:
            embed.add_field(name="Contributors", value="\n\n".join(lines), inline=False)
        else:
            embed.add_field(name="Contributors", value="No damage was dealt.", inline=False)
    else:
        lines = []
        for row in participants:
            contribution_percent = 0 if total_damage <= 0 else round((int(row["damage_done"]) / total_damage) * 100, 1)
            lines.append(
                f"{row['discord_name']}\nStatus: {row['status']}\nDamage Dealt: {format_number(int(row['damage_done']))}\nContribution: {contribution_percent:.1f}%\nAttacks: {int(row['attacks'])}\nUnits Lost: {int(row['lost_bulls']) + int(row['lost_rhinos']) + int(row['lost_lieutenants']) + int(row['lost_generals']) + int(row['lost_mechas'])}"
            )
        embed.add_field(name="Raid Loot", value="No UPX rewards have been distributed", inline=False)
        embed.add_field(name="Contributors", value="\n\n".join(lines) if lines else "No damage was dealt.", inline=False)

    if result_message:
        embed.add_field(name="Result", value=result_message, inline=False)
    return embed


async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("You need administrator permissions to use this command.", ephemeral=True)
        return
    raise error


def main() -> None:
    settings = load_settings()
    store = Store(settings.database_path)
    bot = ArcadionBot(store, settings.guild_id)
    bot.tree.on_error = on_app_command_error
    bot.run(settings.discord_token)
