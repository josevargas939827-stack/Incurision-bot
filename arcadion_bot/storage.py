from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Iterator

from .game import COMBAT_UNIT_HEALTH, ModifierType, RaidState, Units, apply_damage_to_units, apply_wounded_combat_damage, combat_health_from_row, combat_power_from_row


class Store:
    def __init__(self, database_path: str) -> None:
        self.database_path = database_path
        directory = os.path.dirname(database_path)
        if directory:
            os.makedirs(directory, exist_ok=True)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.database_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS players (
                    discord_id TEXT PRIMARY KEY,
                    discord_name TEXT NOT NULL,
                    bulls INTEGER NOT NULL DEFAULT 0,
                    rhinos INTEGER NOT NULL DEFAULT 0,
                    lieutenants INTEGER NOT NULL DEFAULT 0,
                    generals INTEGER NOT NULL DEFAULT 0,
                    mechas INTEGER NOT NULL DEFAULT 0,
                    bulls_hp INTEGER NOT NULL DEFAULT 0,
                    rhinos_hp INTEGER NOT NULL DEFAULT 0,
                    lieutenants_hp INTEGER NOT NULL DEFAULT 0,
                    generals_hp INTEGER NOT NULL DEFAULT 0,
                    mechas_hp INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS raids (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    city TEXT NOT NULL,
                    level TEXT NOT NULL,
                    max_corruption INTEGER NOT NULL,
                    current_corruption INTEGER NOT NULL,
                    arcadion_bulls INTEGER NOT NULL DEFAULT 0,
                    arcadion_rhinos INTEGER NOT NULL DEFAULT 0,
                    arcadion_lieutenants INTEGER NOT NULL DEFAULT 0,
                    arcadion_generals INTEGER NOT NULL DEFAULT 0,
                    arcadion_mechas INTEGER NOT NULL DEFAULT 0,
                    arcadion_bulls_hp INTEGER NOT NULL DEFAULT 0,
                    arcadion_rhinos_hp INTEGER NOT NULL DEFAULT 0,
                    arcadion_lieutenants_hp INTEGER NOT NULL DEFAULT 0,
                    arcadion_generals_hp INTEGER NOT NULL DEFAULT 0,
                    arcadion_mechas_hp INTEGER NOT NULL DEFAULT 0,
                    duration_hours INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    started_at TEXT,
                    ends_at TEXT,
                    created_at TEXT NOT NULL,
                    finished_at TEXT,
                    result TEXT,
                    total_loot_upx INTEGER NOT NULL DEFAULT 0,
                    power_limit INTEGER NOT NULL DEFAULT 0,
                    turn_order TEXT,
                    current_turn_discord_id TEXT,
                    turn_started_at TEXT,
                    turn_deadline_at TEXT,
                    turn_index INTEGER NOT NULL DEFAULT 0,
                    turn_round INTEGER NOT NULL DEFAULT 1,
                    announcement_channel_id TEXT,
                    turn_reminder_state TEXT NOT NULL DEFAULT 'NONE'
                );

                CREATE TABLE IF NOT EXISTS raid_participants (
                    raid_id INTEGER NOT NULL,
                    discord_id TEXT NOT NULL,
                    discord_name TEXT NOT NULL,
                    bulls INTEGER NOT NULL DEFAULT 0,
                    rhinos INTEGER NOT NULL DEFAULT 0,
                    lieutenants INTEGER NOT NULL DEFAULT 0,
                    generals INTEGER NOT NULL DEFAULT 0,
                    mechas INTEGER NOT NULL DEFAULT 0,
                    bulls_hp INTEGER NOT NULL DEFAULT 0,
                    rhinos_hp INTEGER NOT NULL DEFAULT 0,
                    lieutenants_hp INTEGER NOT NULL DEFAULT 0,
                    generals_hp INTEGER NOT NULL DEFAULT 0,
                    mechas_hp INTEGER NOT NULL DEFAULT 0,
                    lost_bulls INTEGER NOT NULL DEFAULT 0,
                    lost_rhinos INTEGER NOT NULL DEFAULT 0,
                    lost_lieutenants INTEGER NOT NULL DEFAULT 0,
                    lost_generals INTEGER NOT NULL DEFAULT 0,
                    lost_mechas INTEGER NOT NULL DEFAULT 0,
                    damage_done INTEGER NOT NULL DEFAULT 0,
                    attacks INTEGER NOT NULL DEFAULT 0,
                    turns_played INTEGER NOT NULL DEFAULT 0,
                    joined_at TEXT NOT NULL,
                    join_order INTEGER,
                    status TEXT NOT NULL DEFAULT 'ACTIVE',
                    eliminated_at TEXT,
                    PRIMARY KEY (raid_id, discord_id),
                    FOREIGN KEY (raid_id) REFERENCES raids(id)
                );

                CREATE TABLE IF NOT EXISTS attack_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    raid_id INTEGER NOT NULL,
                    attacker_id TEXT NOT NULL,
                    attacker_rolls TEXT NOT NULL,
                    attacker_damage INTEGER NOT NULL,
                    arcadion_rolls TEXT NOT NULL,
                    arcadion_damage INTEGER NOT NULL,
                    target_id TEXT,
                    destroyed_units TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (raid_id) REFERENCES raids(id)
                );

                CREATE TABLE IF NOT EXISTS participant_modifiers (
                    raid_id INTEGER NOT NULL,
                    discord_id TEXT NOT NULL,
                    modifier_type TEXT NOT NULL,
                    remaining_uses INTEGER NOT NULL DEFAULT 1,
                    remaining_turns INTEGER NOT NULL DEFAULT 0,
                    metadata TEXT,
                    PRIMARY KEY (raid_id, discord_id, modifier_type),
                    FOREIGN KEY (raid_id) REFERENCES raids(id)
                );
                """
            )

            raid_columns = {row["name"] for row in conn.execute("PRAGMA table_info(raids)")}
            for column, definition in (
                ("arcadion_bulls", "INTEGER NOT NULL DEFAULT 0"),
                ("arcadion_rhinos", "INTEGER NOT NULL DEFAULT 0"),
                ("arcadion_lieutenants", "INTEGER NOT NULL DEFAULT 0"),
                ("arcadion_generals", "INTEGER NOT NULL DEFAULT 0"),
                ("arcadion_mechas", "INTEGER NOT NULL DEFAULT 0"),
                ("arcadion_bulls_hp", "INTEGER NOT NULL DEFAULT 0"),
                ("arcadion_rhinos_hp", "INTEGER NOT NULL DEFAULT 0"),
                ("arcadion_lieutenants_hp", "INTEGER NOT NULL DEFAULT 0"),
                ("arcadion_generals_hp", "INTEGER NOT NULL DEFAULT 0"),
                ("arcadion_mechas_hp", "INTEGER NOT NULL DEFAULT 0"),
                ("total_loot_upx", "INTEGER NOT NULL DEFAULT 0"),
                ("power_limit", "INTEGER NOT NULL DEFAULT 0"),
                ("turn_order", "TEXT"),
                ("current_turn_discord_id", "TEXT"),
                ("turn_started_at", "TEXT"),
                ("turn_deadline_at", "TEXT"),
                ("turn_index", "INTEGER NOT NULL DEFAULT 0"),
                ("turn_round", "INTEGER NOT NULL DEFAULT 1"),
                ("announcement_channel_id", "TEXT"),
                ("turn_reminder_state", "TEXT NOT NULL DEFAULT 'NONE'"),
            ):
                if column not in raid_columns:
                    conn.execute(f"ALTER TABLE raids ADD COLUMN {column} {definition}")

            participant_columns = {row["name"] for row in conn.execute("PRAGMA table_info(raid_participants)")}
            for column, definition in (
                ("join_order", "INTEGER"),
                ("turns_played", "INTEGER NOT NULL DEFAULT 0"),
                ("status", "TEXT NOT NULL DEFAULT 'ACTIVE'"),
                ("eliminated_at", "TEXT"),
                ("bulls_hp", "INTEGER NOT NULL DEFAULT 0"),
                ("rhinos_hp", "INTEGER NOT NULL DEFAULT 0"),
                ("lieutenants_hp", "INTEGER NOT NULL DEFAULT 0"),
                ("generals_hp", "INTEGER NOT NULL DEFAULT 0"),
                ("mechas_hp", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if column not in participant_columns:
                    conn.execute(f"ALTER TABLE raid_participants ADD COLUMN {column} {definition}")

            conn.executescript(
                """
                UPDATE raids SET state = 'RECRUITING' WHERE state = 'RECLUTAMIENTO';
                UPDATE raids SET state = 'BATTLE' WHERE state = 'BATALLA';
                UPDATE raids SET state = 'FINISHED' WHERE state = 'FINALIZADA';
                UPDATE raids SET result = 'PLAYERS' WHERE result = 'JUGADORES';
                UPDATE participant_modifiers SET modifier_type = 'RETURNING_FORCE' WHERE modifier_type = 'DE_VUELTA';
                UPDATE participant_modifiers SET modifier_type = 'SOLDIER_ASCENDS' WHERE modifier_type = 'SOLDADO_ASCIENDE';
                UPDATE participant_modifiers SET modifier_type = 'FALLEN_LIEUTENANT' WHERE modifier_type = 'TENIENTE_CAIDO';
                """
            )

    def upsert_player(self, discord_id: int, discord_name: str, units: Units) -> None:
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO players (discord_id, discord_name, bulls, rhinos, lieutenants, generals, mechas, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(discord_id) DO UPDATE SET
                    discord_name = excluded.discord_name,
                    bulls = excluded.bulls,
                    rhinos = excluded.rhinos,
                    lieutenants = excluded.lieutenants,
                    generals = excluded.generals,
                    mechas = excluded.mechas,
                    updated_at = excluded.updated_at
                """,
                (str(discord_id), discord_name, units.bulls, units.rhinos, units.lieutenants, units.generals, units.mechas, now),
            )

    def get_player(self, discord_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM players WHERE discord_id = ?", (str(discord_id),)).fetchone()

    def add_player_units(self, discord_id: int, units: Units) -> bool:
        if not units.has_any():
            return False
        now = utc_now()
        with self.connect() as conn:
            current = conn.execute(
                "SELECT bulls, rhinos, lieutenants, generals, mechas FROM players WHERE discord_id = ?",
                (str(discord_id),),
            ).fetchone()
            if current is None:
                conn.execute(
                    """
                    INSERT INTO players (discord_id, discord_name, bulls, rhinos, lieutenants, generals, mechas, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (str(discord_id), "Unknown", units.bulls, units.rhinos, units.lieutenants, units.generals, units.mechas, now),
                )
                return True

            updated = Units(
                bulls=int(current["bulls"]) + units.bulls,
                rhinos=int(current["rhinos"]) + units.rhinos,
                lieutenants=int(current["lieutenants"]) + units.lieutenants,
                generals=int(current["generals"]) + units.generals,
                mechas=int(current["mechas"]) + units.mechas,
            )
            conn.execute(
                """
                UPDATE players
                SET bulls = ?, rhinos = ?, lieutenants = ?, generals = ?, mechas = ?, updated_at = ?
                WHERE discord_id = ?
                """,
                (updated.bulls, updated.rhinos, updated.lieutenants, updated.generals, updated.mechas, now, str(discord_id)),
            )
            return True

    def remove_player_units(self, discord_id: int, units: Units) -> bool:
        if not units.has_any():
            return False
        now = utc_now()
        with self.connect() as conn:
            current = conn.execute(
                "SELECT bulls, rhinos, lieutenants, generals, mechas FROM players WHERE discord_id = ?",
                (str(discord_id),),
            ).fetchone()
            if current is None:
                return False

            current_units = Units(
                bulls=int(current["bulls"]),
                rhinos=int(current["rhinos"]),
                lieutenants=int(current["lieutenants"]),
                generals=int(current["generals"]),
                mechas=int(current["mechas"]),
            )
            if not current_units.contains(units):
                return False

            updated = Units(
                bulls=current_units.bulls - units.bulls,
                rhinos=current_units.rhinos - units.rhinos,
                lieutenants=current_units.lieutenants - units.lieutenants,
                generals=current_units.generals - units.generals,
                mechas=current_units.mechas - units.mechas,
            )
            conn.execute(
                """
                UPDATE players
                SET bulls = ?, rhinos = ?, lieutenants = ?, generals = ?, mechas = ?, updated_at = ?
                WHERE discord_id = ?
                """,
                (updated.bulls, updated.rhinos, updated.lieutenants, updated.generals, updated.mechas, now, str(discord_id)),
            )
            return True

    def create_raid(
        self,
        name: str,
        city: str,
        level: str,
        max_corruption: int,
        duration_hours: int,
        arcadion_units: Units | None = None,
        total_loot_upx: int = 0,
        power_limit: int = 0,
    ) -> int:
        now = utc_now()
        arcadion_units = arcadion_units or Units()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO raids (
                    name, city, level, max_corruption, current_corruption,
                    arcadion_bulls, arcadion_rhinos, arcadion_lieutenants, arcadion_generals, arcadion_mechas,
                    arcadion_bulls_hp, arcadion_rhinos_hp, arcadion_lieutenants_hp, arcadion_generals_hp, arcadion_mechas_hp,
                    duration_hours, state, created_at, total_loot_upx, power_limit
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    city,
                    level,
                    max_corruption,
                    max_corruption,
                    arcadion_units.bulls,
                    arcadion_units.rhinos,
                    arcadion_units.lieutenants,
                    arcadion_units.generals,
                    arcadion_units.mechas,
                    arcadion_units.bulls * COMBAT_UNIT_HEALTH["bulls"],
                    arcadion_units.rhinos * COMBAT_UNIT_HEALTH["rhinos"],
                    arcadion_units.lieutenants * COMBAT_UNIT_HEALTH["lieutenants"],
                    arcadion_units.generals * COMBAT_UNIT_HEALTH["generals"],
                    arcadion_units.mechas * COMBAT_UNIT_HEALTH["mechas"],
                    duration_hours,
                    RaidState.RECRUITING.value,
                    now,
                    total_loot_upx,
                    power_limit,
                ),
            )
            return int(cursor.lastrowid)

    def update_corruption(self, raid_id: int, current_corruption: int) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE raids SET current_corruption = ? WHERE id = ?",
                (max(0, current_corruption), raid_id),
            )

    def update_arcadion_units(self, raid_id: int, units: Units, health: dict[str, int] | None = None) -> None:
        health = health or {name: getattr(units, name) * COMBAT_UNIT_HEALTH[name] for name in units.as_dict()}
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE raids SET
                    arcadion_bulls = ?, arcadion_bulls_hp = ?,
                    arcadion_rhinos = ?, arcadion_rhinos_hp = ?,
                    arcadion_lieutenants = ?, arcadion_lieutenants_hp = ?,
                    arcadion_generals = ?, arcadion_generals_hp = ?,
                    arcadion_mechas = ?, arcadion_mechas_hp = ?
                WHERE id = ?
                """,
                (
                    units.bulls,
                    health["bulls"],
                    units.rhinos,
                    health["rhinos"],
                    units.lieutenants,
                    health["lieutenants"],
                    units.generals,
                    health["generals"],
                    units.mechas,
                    health["mechas"],
                    raid_id,
                ),
            )


    def get_active_raid(self) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM raids WHERE state != ? ORDER BY id DESC LIMIT 1",
                (RaidState.FINISHED.value,),
            ).fetchone()

    def get_raid(self, raid_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM raids WHERE id = ?", (raid_id,)).fetchone()

    def start_raid(self, raid_id: int, channel_id: int | None = None) -> None:
        raid = self.get_raid(raid_id)
        if raid is None:
            raise ValueError("The raid does not exist.")
        started = datetime.now(timezone.utc)
        ends = started + timedelta(hours=int(raid["duration_hours"]))
        with self.connect() as conn:
            conn.execute(
                "UPDATE raids SET state = ?, started_at = ?, ends_at = ? WHERE id = ?",
                (RaidState.BATTLE.value, started.isoformat(), ends.isoformat(), raid_id),
            )
            self._initialize_turn_order(conn, raid_id, channel_id)

    def finish_raid(self, raid_id: int, result: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE raids SET state = ?, finished_at = ?, result = ? WHERE id = ?",
                (RaidState.FINISHED.value, utc_now(), result, raid_id),
            )

    def set_total_loot(self, raid_id: int, total_loot_upx: int) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE raids SET total_loot_upx = ? WHERE id = ?",
                (max(0, total_loot_upx), raid_id),
            )

    def set_turn_reminder_state(self, raid_id: int, reminder_state: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE raids SET turn_reminder_state = ? WHERE id = ?",
                (reminder_state, raid_id),
            )

    def set_announcement_channel(self, raid_id: int, channel_id: int | None) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE raids SET announcement_channel_id = ? WHERE id = ?",
                (str(channel_id) if channel_id is not None else None, raid_id),
            )

    def _initialize_turn_order(self, conn: sqlite3.Connection, raid_id: int, channel_id: int | None = None) -> None:
        participants = list(
            conn.execute(
                """
                SELECT * FROM raid_participants
                WHERE raid_id = ?
                ORDER BY COALESCE(join_order, 999999), joined_at, discord_id
                """,
                (raid_id,),
            )
        )
        active_ids = [str(row["discord_id"]) for row in participants if self._participant_has_units(row)]
        if not active_ids:
            conn.execute(
                """
                UPDATE raids
                SET turn_order = ?, current_turn_discord_id = NULL, turn_started_at = NULL,
                    turn_deadline_at = NULL, turn_index = 0, turn_round = 1,
                    announcement_channel_id = ?, turn_reminder_state = 'NONE'
                WHERE id = ?
                """,
                (json.dumps([]), str(channel_id) if channel_id is not None else None, raid_id),
            )
            return

        now = utc_now()
        deadline = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        conn.execute(
            """
            UPDATE raids
            SET turn_order = ?, current_turn_discord_id = ?, turn_started_at = ?, turn_deadline_at = ?,
                turn_index = 0, turn_round = 1, announcement_channel_id = ?, turn_reminder_state = 'NONE'
            WHERE id = ?
            """,
            (json.dumps(active_ids), active_ids[0], now, deadline, str(channel_id) if channel_id is not None else None, raid_id),
        )

    def _participant_has_units(self, participant: sqlite3.Row) -> bool:
        status = participant["status"] if "status" in participant.keys() else "ACTIVE"
        if str(status or "ACTIVE") == "ELIMINATED":
            return False
        return (
            int(participant["bulls_hp"] or 0)
            + int(participant["rhinos_hp"] or 0)
            + int(participant["lieutenants_hp"] or 0)
            + int(participant["generals_hp"] or 0)
            + int(participant["mechas_hp"] or 0)
            > 0
        )

    def _participant_has_units_from_id(self, conn: sqlite3.Connection, raid_id: int, discord_id: str) -> bool:
        participant = conn.execute(
            "SELECT bulls_hp, rhinos_hp, lieutenants_hp, generals_hp, mechas_hp, status FROM raid_participants WHERE raid_id = ? AND discord_id = ?",
            (raid_id, str(discord_id)),
        ).fetchone()
        if participant is None:
            return False
        status = participant["status"] if "status" in participant.keys() else "ACTIVE"
        if str(status or "ACTIVE") == "ELIMINATED":
            return False
        return (
            int(participant["bulls_hp"] or 0)
            + int(participant["rhinos_hp"] or 0)
            + int(participant["lieutenants_hp"] or 0)
            + int(participant["generals_hp"] or 0)
            + int(participant["mechas_hp"] or 0)
            > 0
        )

    def advance_turn(self, raid_id: int, channel_id: int | None = None) -> sqlite3.Row | None:
        with self.connect() as conn:
            raid = conn.execute("SELECT * FROM raids WHERE id = ?", (raid_id,)).fetchone()
            if raid is None:
                return None

            turn_order = json.loads(raid["turn_order"] or "[]")
            if not turn_order:
                conn.execute(
                    """
                    UPDATE raids
                    SET current_turn_discord_id = NULL, turn_started_at = NULL,
                        turn_deadline_at = NULL, turn_index = 0, turn_round = 1,
                        announcement_channel_id = ?
                    WHERE id = ?
                    """,
                    (str(channel_id) if channel_id is not None else None, raid_id),
                )
                return None

            active_ids = [discord_id for discord_id in turn_order if self._participant_has_units_from_id(conn, raid_id, discord_id)]
            if not active_ids:
                conn.execute(
                    """
                    UPDATE raids
                    SET current_turn_discord_id = NULL, turn_started_at = NULL,
                        turn_deadline_at = NULL, turn_index = 0, turn_round = 1,
                        announcement_channel_id = ?
                    WHERE id = ?
                    """,
                    (str(channel_id) if channel_id is not None else None, raid_id),
                )
                return None

            current_id = str(raid["current_turn_discord_id"]) if raid["current_turn_discord_id"] is not None else None
            current_index = turn_order.index(current_id) if current_id in turn_order else -1
            next_id = None
            next_index = None
            for offset in range(len(turn_order)):
                candidate_index = (current_index + 1 + offset) % len(turn_order)
                candidate_id = turn_order[candidate_index]
                if candidate_id in active_ids:
                    next_id = candidate_id
                    next_index = candidate_index
                    break

            if next_id is None:
                conn.execute(
                    """
                    UPDATE raids
                    SET current_turn_discord_id = NULL, turn_started_at = NULL,
                        turn_deadline_at = NULL, turn_index = 0, turn_round = 1,
                        announcement_channel_id = ?
                    WHERE id = ?
                    """,
                    (str(channel_id) if channel_id is not None else None, raid_id),
                )
                return None

            round_number = int(raid["turn_round"] or 1)
            if current_id is not None and next_index is not None and next_index <= current_index:
                round_number += 1

            now = utc_now()
            deadline = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
            conn.execute(
                """
                UPDATE raids
                SET current_turn_discord_id = ?, turn_started_at = ?, turn_deadline_at = ?,
                    turn_index = ?, turn_round = ?, announcement_channel_id = ?, turn_reminder_state = 'NONE'
                WHERE id = ?
                """,
                (str(next_id), now, deadline, next_index, round_number, str(channel_id) if channel_id is not None else None, raid_id),
            )
            return self.get_participant(raid_id, next_id)

    def process_turn_timeout(self, raid_id: int, channel_id: int | None = None) -> sqlite3.Row | None:
        raid = self.get_raid(raid_id)
        if raid is None or raid["state"] != RaidState.BATTLE.value:
            return None
        if not raid["turn_deadline_at"]:
            return None
        deadline = datetime.fromisoformat(raid["turn_deadline_at"])
        if datetime.now(timezone.utc) < deadline:
            return None

        with self.connect() as conn:
            current_player_id = raid["current_turn_discord_id"]
            if current_player_id:
                participant = conn.execute(
                    "SELECT * FROM raid_participants WHERE raid_id = ? AND discord_id = ?",
                    (raid_id, str(current_player_id)),
                ).fetchone()
                if participant is not None and self._participant_has_units_from_id(conn, raid_id, current_player_id):
                    current_units = Units.from_row(participant)
                    current_health = combat_health_from_row(participant)
                    remaining_units, destroyed_units, remaining_health, _ = apply_wounded_combat_damage(current_units, current_health, 5000)
                    self.update_participant_units_and_losses(raid_id, current_player_id, remaining_units, destroyed_units, remaining_health)
                    if not remaining_units.has_any():
                        conn.execute(
                            "UPDATE raid_participants SET status = ?, eliminated_at = ? WHERE raid_id = ? AND discord_id = ?",
                            ("ELIMINATED", utc_now(), raid_id, str(current_player_id)),
                        )

        return self.advance_turn(raid_id, channel_id)
    def upsert_participant(self, raid_id: int, discord_id: int, discord_name: str, units: Units) -> None:
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT join_order FROM raid_participants WHERE raid_id = ? AND discord_id = ?",
                (raid_id, str(discord_id)),
            ).fetchone()
            join_order = None
            if existing is None:
                join_order = int(
                    conn.execute(
                        "SELECT COALESCE(MAX(join_order), 0) + 1 FROM raid_participants WHERE raid_id = ?",
                        (raid_id,),
                    ).fetchone()[0]
                )

            existing_row = conn.execute(
                "SELECT status FROM raid_participants WHERE raid_id = ? AND discord_id = ?",
                (raid_id, str(discord_id)),
            ).fetchone()
            if existing_row is not None and existing_row["status"] == "ELIMINATED":
                return

            conn.execute(
                """
                INSERT INTO raid_participants
                    (raid_id, discord_id, discord_name, bulls, rhinos, lieutenants, generals, mechas,
                     bulls_hp, rhinos_hp, lieutenants_hp, generals_hp, mechas_hp,
                     joined_at, join_order, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(raid_id, discord_id) DO UPDATE SET
                    discord_name = excluded.discord_name,
                    bulls = excluded.bulls,
                    rhinos = excluded.rhinos,
                    lieutenants = excluded.lieutenants,
                    generals = excluded.generals,
                    mechas = excluded.mechas,
                    bulls_hp = excluded.bulls_hp,
                    rhinos_hp = excluded.rhinos_hp,
                    lieutenants_hp = excluded.lieutenants_hp,
                    generals_hp = excluded.generals_hp,
                    mechas_hp = excluded.mechas_hp
                """,
                (
                    raid_id,
                    str(discord_id),
                    discord_name,
                    units.bulls,
                    units.rhinos,
                    units.lieutenants,
                    units.generals,
                    units.mechas,
                    units.bulls * COMBAT_UNIT_HEALTH["bulls"],
                    units.rhinos * COMBAT_UNIT_HEALTH["rhinos"],
                    units.lieutenants * COMBAT_UNIT_HEALTH["lieutenants"],
                    units.generals * COMBAT_UNIT_HEALTH["generals"],
                    units.mechas * COMBAT_UNIT_HEALTH["mechas"],
                    utc_now(),
                    join_order,
                    "ACTIVE",
                ),
            )
            self._grant_initial_modifiers(conn, raid_id, str(discord_id))

    def _grant_initial_modifiers(self, conn: sqlite3.Connection, raid_id: int, discord_id: str) -> None:
        for modifier in (ModifierType.RETURNING_FORCE, ModifierType.SOLDIER_ASCENDS):
            conn.execute(
                """
                INSERT OR IGNORE INTO participant_modifiers
                    (raid_id, discord_id, modifier_type, remaining_uses, remaining_turns)
                VALUES (?, ?, ?, 1, 0)
                """,
                (raid_id, discord_id, modifier.value),
            )

    def get_participant(self, raid_id: int, discord_id: int | str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM raid_participants WHERE raid_id = ? AND discord_id = ?",
                (raid_id, str(discord_id)),
            ).fetchone()

    def mark_participant_eliminated_if_needed(self, raid_id: int, discord_id: int | str, remaining: Units) -> bool:
        with self.connect() as conn:
            participant = conn.execute(
                "SELECT status FROM raid_participants WHERE raid_id = ? AND discord_id = ?",
                (raid_id, str(discord_id)),
            ).fetchone()
            if participant is None or participant["status"] == "ELIMINATED":
                return False
            if remaining.has_any():
                return False
            conn.execute(
                "UPDATE raid_participants SET status = ?, eliminated_at = ? WHERE raid_id = ? AND discord_id = ?",
                ("ELIMINATED", utc_now(), raid_id, str(discord_id)),
            )
            return True

    def increment_turns_played(self, raid_id: int, discord_id: int | str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE raid_participants SET turns_played = turns_played + 1 WHERE raid_id = ? AND discord_id = ?",
                (raid_id, str(discord_id)),
            )

    def list_participants(self, raid_id: int) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    "SELECT * FROM raid_participants WHERE raid_id = ? ORDER BY damage_done DESC, attacks DESC",
                    (raid_id,),
                )
            )

    def list_active_participants(self, raid_id: int) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT * FROM raid_participants
                    WHERE raid_id = ?
                      AND COALESCE(status, 'ACTIVE') != 'ELIMINATED'
                      AND (COALESCE(bulls_hp, 0) + COALESCE(rhinos_hp, 0) + COALESCE(lieutenants_hp, 0) + COALESCE(generals_hp, 0) + COALESCE(mechas_hp, 0)) > 0
                    """,
                    (raid_id,),
                )
            )

    def update_participant_units_and_losses(self, raid_id: int, discord_id: int | str, remaining: Units, destroyed: Units, remaining_health: dict[str, int] | None = None) -> None:
        remaining_health = remaining_health or {name: getattr(remaining, name) * COMBAT_UNIT_HEALTH[name] for name in remaining.as_dict()}
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE raid_participants SET
                    bulls = ?, bulls_hp = ?, rhinos = ?, rhinos_hp = ?, lieutenants = ?, lieutenants_hp = ?,
                    generals = ?, generals_hp = ?, mechas = ?, mechas_hp = ?,
                    lost_bulls = lost_bulls + ?, lost_rhinos = lost_rhinos + ?, lost_lieutenants = lost_lieutenants + ?,
                    lost_generals = lost_generals + ?, lost_mechas = lost_mechas + ?
                WHERE raid_id = ? AND discord_id = ?
                """,
                (
                    remaining.bulls,
                    remaining_health["bulls"],
                    remaining.rhinos,
                    remaining_health["rhinos"],
                    remaining.lieutenants,
                    remaining_health["lieutenants"],
                    remaining.generals,
                    remaining_health["generals"],
                    remaining.mechas,
                    remaining_health["mechas"],
                    destroyed.bulls,
                    destroyed.rhinos,
                    destroyed.lieutenants,
                    destroyed.generals,
                    destroyed.mechas,
                    raid_id,
                    str(discord_id),
                ),
            )

    def add_attack_stats(self, raid_id: int, discord_id: int, damage: int) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE raid_participants
                SET damage_done = damage_done + ?, attacks = attacks + 1
                WHERE raid_id = ? AND discord_id = ?
                """,
                (damage, raid_id, str(discord_id)),
            )

    def log_attack(
        self,
        raid_id: int,
        attacker_id: int,
        attacker_rolls: str,
        attacker_damage: int,
        arcadion_rolls: str,
        arcadion_damage: int,
        target_id: str | None,
        destroyed_units: str,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO attack_log
                    (raid_id, attacker_id, attacker_rolls, attacker_damage, arcadion_rolls,
                     arcadion_damage, target_id, destroyed_units, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    raid_id,
                    str(attacker_id),
                    attacker_rolls,
                    attacker_damage,
                    arcadion_rolls,
                    arcadion_damage,
                    target_id,
                    destroyed_units,
                    utc_now(),
                ),
            )

    def get_modifier(self, raid_id: int, discord_id: int, modifier_type: ModifierType) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT * FROM participant_modifiers
                WHERE raid_id = ? AND discord_id = ? AND modifier_type = ?
                """,
                (raid_id, str(discord_id), modifier_type.value),
            ).fetchone()

    def set_modifier(self, raid_id: int, discord_id: int | str, modifier_type: ModifierType, uses: int, turns: int, metadata: str | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO participant_modifiers
                    (raid_id, discord_id, modifier_type, remaining_uses, remaining_turns, metadata)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(raid_id, discord_id, modifier_type) DO UPDATE SET
                    remaining_uses = excluded.remaining_uses,
                    remaining_turns = excluded.remaining_turns,
                    metadata = excluded.metadata
                """,
                (raid_id, str(discord_id), modifier_type.value, uses, turns, metadata),
            )

    def tick_turn_modifiers(self, raid_id: int, discord_id: int) -> None:
        with self.connect() as conn:
            modifier_rows = conn.execute(
                """
                SELECT * FROM participant_modifiers
                WHERE raid_id = ? AND discord_id = ? AND remaining_turns = 1
                """,
                (raid_id, str(discord_id)),
            ).fetchall()
            for row in modifier_rows:
                if row["modifier_type"] == ModifierType.SOLDIER_ASCENDS.value and row["metadata"]:
                    unit = row["metadata"]
                    conn.execute(
                        f"""
                        UPDATE raid_participants
                        SET {unit} = CASE WHEN {unit} > 0 THEN {unit} - 1 ELSE 0 END,
                            {unit}_hp = CASE WHEN {unit}_hp > 0 THEN {unit}_hp - {COMBAT_UNIT_HEALTH[unit]} ELSE 0 END,
                            lieutenants = lieutenants + 1,
                            lieutenants_hp = lieutenants_hp + {COMBAT_UNIT_HEALTH['lieutenants']}
                        WHERE raid_id = ? AND discord_id = ?
                        """,
                        (raid_id, str(discord_id)),
                    )
            conn.execute(
                """
                UPDATE participant_modifiers
                SET remaining_turns = MAX(0, remaining_turns - 1)
                WHERE raid_id = ? AND discord_id = ? AND remaining_turns > 0
                """,
                (raid_id, str(discord_id)),
            )



def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
