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
    Units,
    apply_damage_to_units,
    format_number,
    format_units,
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


def units_from_args(bulls: int, rhinos: int, lieutenants: int, generals: int, mechas: int) -> Units:
    values = (bulls, rhinos, lieutenants, generals, mechas)
    if any(value < 0 for value in values):
        raise ValueError("Unit amounts cannot be negative.")
    return Units(bulls=bulls, rhinos=rhinos, lieutenants=lieutenants, generals=generals, mechas=mechas)


def register_commands(bot: ArcadionBot) -> None:
    @bot.tree.command(name="army_set", description="Register or update your permanent army.")
    async def army_set(interaction: discord.Interaction, bulls: int = 0, rhinos: int = 0, lieutenants: int = 0, generals: int = 0, mechas: int = 0) -> None:
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

    @bot.tree.command(name="arcadion_create", description="Create an Arcadion raid in recruitment.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def arcadion_create(
        interaction: discord.Interaction,
        name: str,
        city: str,
        level: str,
        max_corruption: int,
        duration_hours: int,
        arcadion_bulls: int = 0,
        arcadion_rhinos: int = 0,
        arcadion_lieutenants: int = 0,
        arcadion_generals: int = 0,
        arcadion_mechas: int = 0,
    ) -> None:
        try:
            arcadion_units = units_from_args(arcadion_bulls, arcadion_rhinos, arcadion_lieutenants, arcadion_generals, arcadion_mechas)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        if max_corruption <= 0 or duration_hours <= 0:
            await interaction.response.send_message("Maximum corruption and duration must be greater than 0.", ephemeral=True)
            return
        active = bot.store.get_active_raid()
        if active:
            await interaction.response.send_message("There is already an active or recruiting raid.", ephemeral=True)
            return
        raid_id = bot.store.create_raid(name, city, level, max_corruption, duration_hours, arcadion_units)
        raid = bot.store.get_raid(raid_id)
        await interaction.response.send_message(embed=raid_created_embed(raid))

    @bot.tree.command(name="raid_join", description="Send troops to the recruiting raid.")
    async def raid_join(interaction: discord.Interaction, bulls: int = 0, rhinos: int = 0, lieutenants: int = 0, generals: int = 0, mechas: int = 0) -> None:
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
        bot.store.start_raid(raid["id"])
        raid = bot.store.get_raid(raid["id"])
        await interaction.response.send_message(embed=battle_started_embed(raid))

    @bot.tree.command(name="attack", description="Attack Arcadion during the battle.")
    async def attack(interaction: discord.Interaction) -> None:
        raid = bot.store.get_active_raid()
        if raid is None or raid["state"] != RaidState.BATTLE.value:
            await interaction.response.send_message("There is no active battle.", ephemeral=True)
            return
        if raid_time_expired(raid):
            bot.store.finish_raid(raid["id"], "ARCADION")
            await interaction.response.send_message(embed=finished_embed(raid, "Arcadion wins. The raid timer has expired."))
            return

        participant = bot.store.get_participant(raid["id"], interaction.user.id)
        if participant is None:
            await interaction.response.send_message("You are not participating in this raid.", ephemeral=True)
            return

        current_units = Units.from_row(participant)
        if not current_units.has_any():
            await interaction.response.send_message("You have no active troops left to attack.", ephemeral=True)
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
        if target is not None:
            target_units = Units.from_row(target)
            loss = apply_damage_to_units(target_units, arcadion_roll.damage)
            destroyed = loss.destroyed
            bot.store.update_participant_units_and_losses(raid["id"], target["discord_id"], loss.remaining, loss.destroyed)

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

        refreshed = bot.store.get_raid(raid["id"])
        active_after = bot.store.list_active_participants(raid["id"])
        result_message = None
        if new_corruption <= 0:
            bot.store.finish_raid(raid["id"], "PLAYERS")
            result_message = "Victory. Arcadion has been defeated."
        elif not active_after:
            bot.store.finish_raid(raid["id"], "ARCADION")
            result_message = "Arcadion wins. No active troops remain."

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
        await interaction.response.send_message(embed=finished_embed(raid, "The raid was manually finished."))


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


def battle_started_embed(raid: object) -> discord.Embed:
    embed = discord.Embed(title=f"🔥 BATTLE STARTED: {raid['name']}", color=0xB22222)
    embed.add_field(name="City", value=raid["city"], inline=True)
    embed.add_field(name="Ends At", value=raid["ends_at"], inline=False)
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
