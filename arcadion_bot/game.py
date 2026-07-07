from __future__ import annotations

import re
from dataclasses import dataclass
import random
from decimal import Decimal, InvalidOperation
from enum import Enum
from random import randint


class RaidState(str, Enum):
    RECRUITING = "RECRUITING"
    BATTLE = "BATTLE"
    FINISHED = "FINISHED"


class ModifierType(str, Enum):
    RETURNING_FORCE = "RETURNING_FORCE"
    SOLDIER_ASCENDS = "SOLDIER_ASCENDS"
    FALLEN_LIEUTENANT = "FALLEN_LIEUTENANT"


UNIT_VALUES = {
    "bulls": 1500,
    "rhinos": 1500,
    "lieutenants": 5000,
    "generals": 8500,
    "mechas": 25000,
}

UNIT_LABELS = {
    "bulls": "Bull Soldier",
    "rhinos": "Rhino Soldier",
    "lieutenants": "Lion Lieutenant",
    "generals": "General",
    "mechas": "Mecha",
}

DICE_DAMAGE = {
    1: 0,
    2: 1000,
    3: 2000,
    4: 4000,
    5: 6000,
    6: 10000,
}

LOSS_ORDER = ("mechas", "generals", "lieutenants", "rhinos", "bulls")
REVIVE_ORDER = ("mechas", "generals", "lieutenants", "rhinos", "bulls")
COUNTERATTACK_WEIGHTS = {
    "bulls": 70,
    "rhinos": 20,
    "lieutenants": 7,
    "generals": 2,
    "mechas": 1,
}


@dataclass(frozen=True)
class Units:
    bulls: int = 0
    rhinos: int = 0
    lieutenants: int = 0
    generals: int = 0
    mechas: int = 0

    @classmethod
    def from_row(cls, row: object, prefix: str = "") -> "Units":
        return cls(
            bulls=int(row[f"{prefix}bulls"]),
            rhinos=int(row[f"{prefix}rhinos"]),
            lieutenants=int(row[f"{prefix}lieutenants"]),
            generals=int(row[f"{prefix}generals"]),
            mechas=int(row[f"{prefix}mechas"]),
        )

    def as_dict(self) -> dict[str, int]:
        return {
            "bulls": self.bulls,
            "rhinos": self.rhinos,
            "lieutenants": self.lieutenants,
            "generals": self.generals,
            "mechas": self.mechas,
        }

    def power(self) -> int:
        return sum(amount * UNIT_VALUES[name] for name, amount in self.as_dict().items())

    def has_any(self) -> bool:
        return any(amount > 0 for amount in self.as_dict().values())

    def contains(self, other: "Units") -> bool:
        return all(self.as_dict()[name] >= other.as_dict()[name] for name in UNIT_VALUES)

    def add(self, other: "Units") -> "Units":
        return Units(**{name: self.as_dict()[name] + other.as_dict()[name] for name in UNIT_VALUES})

    def subtract(self, other: "Units") -> "Units":
        return Units(**{name: max(0, self.as_dict()[name] - other.as_dict()[name]) for name in UNIT_VALUES})


@dataclass(frozen=True)
class DiceRoll:
    rolls: list[int]

    @property
    def damage(self) -> int:
        return sum(DICE_DAMAGE[roll] for roll in self.rolls)

    @property
    def text(self) -> str:
        return " - ".join(str(roll) for roll in self.rolls)


@dataclass(frozen=True)
class LossResult:
    remaining: Units
    destroyed: Units
    absorbed_damage: int


def roll_dice(count: int) -> DiceRoll:
    return DiceRoll([randint(1, 6) for _ in range(count)])


def apply_damage_to_units(units: Units, damage: int) -> LossResult:
    remaining = units.as_dict()
    destroyed = {name: 0 for name in UNIT_VALUES}
    remaining_damage = max(0, damage)
    absorbed = 0

    while remaining_damage > 0:
        affordable = [
            name
            for name in UNIT_VALUES
            if remaining[name] > 0 and UNIT_VALUES[name] <= remaining_damage
        ]
        if not affordable:
            break

        weights = [COUNTERATTACK_WEIGHTS[name] for name in affordable]
        selected_name = random.choices(affordable, weights=weights, k=1)[0]
        remaining[selected_name] -= 1
        destroyed[selected_name] += 1
        remaining_damage -= UNIT_VALUES[selected_name]
        absorbed += UNIT_VALUES[selected_name]

    return LossResult(
        remaining=Units(**remaining),
        destroyed=Units(**destroyed),
        absorbed_damage=absorbed,
    )


def format_units(units: Units) -> str:
    parts = []
    for name, amount in units.as_dict().items():
        if amount:
            label = UNIT_LABELS[name]
            parts.append(f"{amount} {label}{'' if amount == 1 else 's'}")
    return "\n".join(parts) if parts else "No units"


def parse_integer(value: str | int) -> int:
    if isinstance(value, bool):
        raise ValueError("Invalid numeric value. Accepted formats: 1500000, 1.500.000, 1,500,000, 1500k, 250k, 1.5m, 2m.")
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        raise TypeError("Numeric values must be provided as strings or integers.")

    text = value.strip()
    if not text:
        raise ValueError("Invalid numeric value. Accepted formats: 1500000, 1.500.000, 1,500,000, 1500k, 250k, 1.5m, 2m.")

    if re.fullmatch(r"\d+(?:[.,]\d+)?[kKmM]", text):
        number_text = text[:-1]
        suffix = text[-1].lower()
        multiplier = 1000 if suffix == "k" else 1000000
        normalized = number_text.replace(",", ".")
        try:
            return int(Decimal(normalized) * multiplier)
        except InvalidOperation as exc:
            raise ValueError("Invalid numeric value. Accepted formats: 1500000, 1.500.000, 1,500,000, 1500k, 250k, 1.5m, 2m.") from exc

    if re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", text):
        separator = "." if "." in text else ","
        if text.count(separator) > 0 and text.count(",") > 0 and text.count(".") > 0:
            raise ValueError("Invalid numeric value. Accepted formats: 1500000, 1.500.000, 1,500,000, 1500k, 250k, 1.5m, 2m.")
        parts = text.split(separator)
        if any(not part.isdigit() or len(part) != 3 for part in parts[1:]):
            raise ValueError("Invalid numeric value. Accepted formats: 1500000, 1.500.000, 1,500,000, 1500k, 250k, 1.5m, 2m.")
        return int("".join(parts))

    if re.fullmatch(r"\d+", text):
        return int(text)

    raise ValueError("Invalid numeric value. Accepted formats: 1500000, 1.500.000, 1,500,000, 1500k, 250k, 1.5m, 2m.")


def calculate_loot_distribution(total_loot: int, contributions: list[tuple[str, int]]) -> list[dict[str, object]]:
    total_damage = sum(damage for _, damage in contributions)
    if total_damage <= 0:
        return [
            {
                "player": player,
                "damage": damage,
                "participation_percent": "0%",
                "reward": 0,
            }
            for player, damage in contributions
        ]

    entries: list[dict[str, object]] = []
    base_rewards = []
    for player, damage in contributions:
        raw_reward = total_loot * damage / total_damage
        base_rewards.append((player, damage, raw_reward))

    rewards = [int(raw_reward) for _, _, raw_reward in base_rewards]
    remainder = total_loot - sum(rewards)
    if remainder > 0:
        sorted_indices = sorted(
            range(len(base_rewards)),
            key=lambda index: (base_rewards[index][2] - rewards[index], -index),
            reverse=True,
        )
        for index in sorted_indices[:remainder]:
            rewards[index] += 1

    for (player, damage, raw_reward), reward in zip(base_rewards, rewards):
        participation = (damage / total_damage * 100) if total_damage else 0
        entries.append(
            {
                "player": player,
                "damage": damage,
                "participation_percent": f"{round(participation)}%",
                "reward": reward,
            }
        )

    return sorted(entries, key=lambda entry: (-int(entry["reward"]), -int(entry["damage"]), str(entry["player"])))


def format_number(value: int) -> str:
    return f"{value:,}".replace(",", ".")
