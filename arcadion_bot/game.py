from __future__ import annotations

from dataclasses import dataclass
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
    remaining_damage = damage
    absorbed = 0

    while remaining_damage > 0:
        destroyed_any = False
        for unit_name in LOSS_ORDER:
            value = UNIT_VALUES[unit_name]
            if remaining[unit_name] > 0 and value <= remaining_damage:
                remaining[unit_name] -= 1
                destroyed[unit_name] += 1
                remaining_damage -= value
                absorbed += value
                destroyed_any = True
                break
        if not destroyed_any:
            break

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


def format_number(value: int) -> str:
    return f"{value:,}".replace(",", ".")
