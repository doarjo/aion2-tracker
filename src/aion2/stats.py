"""Stat vocabulary for AION 2 Global.

Everything here was derived empirically in Phase 0 and is documented with
evidence in docs/endpoints.md. The traps are real; read the comments before
changing a set.
"""

from __future__ import annotations

# --------------------------------------------------------------------------
# Accuracy
# --------------------------------------------------------------------------
# TRAP: `AbnormalAccuracy` displays as "Status Effect Chance" and is measured
# in percent. It is NOT accuracy. Any substring match on "Accuracy" picks it up
# and silently inflates the total -- it added a phantom 18 points on our Ranger.
# Always match stat ids exactly, never with `in` or `startswith`.
#
# Conversely there are genuinely TWO flat accuracy ids and both must count.
ACCURACY_IDS: frozenset[str] = frozenset({
    "WeaponAccuracy",  # displays as "Accuracy"
    "Accuracy",        # displays as "Accuracy Bonus"
})

NOT_ACCURACY_DESPITE_THE_NAME: frozenset[str] = frozenset({
    "AbnormalAccuracy",  # "Status Effect Chance", percent
})

# --------------------------------------------------------------------------
# Wasted-DPS stats for a Ranger PvE build (brief section 5)
# --------------------------------------------------------------------------
# These are reroll / replace candidates when they appear on a rolled sub-stat
# line or a manastone. They are NOT flagged on an item's fixed `mainStats`,
# because those cannot be changed.
WASTED_DPS_IDS: frozenset[str] = frozenset({
    "DEX",             # Dexterity -- gives evasion/block/crit-resist, not DPS
    "Evasion",         # "Evasion Bonus"
    "ArmorEvasion",    # "Evasion"
    "Block",
    "ArmorDefense",    # "Defense"
    "CriticalResist",  # "Critical Hit Resist"
    "CriticalDamageDefense",
    "FrontAttackDefense",
    "PvPDamageDefense",
})

# Stats that actively help a Ranger's PvE damage. Used to rank what a wasted
# line should be replaced WITH.
DPS_IDS: frozenset[str] = frozenset({
    "WeaponAccuracy", "Accuracy",
    "Critical", "CriticalDamage",
    "WeaponFixingDamage", "DamageRatio",
    "AmplifyAllDamage", "AmplifyWeaponDamage", "PvEAmplifyDamage",
    "DefensePierce", "AdditionalHitRate", "Perfect",
    "AGI", "STR",
})

# --------------------------------------------------------------------------
# Base attributes, keyed on `type` in info.stat.statList
# --------------------------------------------------------------------------
# TRAP: the `name` field is localized and `ItemLevel` comes back as untranslated
# Korean even with lang=en-US. Always key on `type`, never on `name`.
BASE_ATTRIBUTES: dict[str, str] = {
    "STR": "Might",
    "DEX": "Dexterity",
    "INT": "Intelligence",
    "CON": "Constitution",
    "AGI": "Precision",      # note: Precision is AGI, not "PRE"
    "WIS": "Willpower",
}

# The ten god stats. Each carries a statSecondList of derived percentages at a
# ratio of value / 10 = percent.
GOD_STATS: dict[str, str] = {
    "Justice": "Nezekan",
    "Freedom": "Vaizel",
    "Illusion": "Kaisinel",
    "Life": "Yustiel",
    "Time": "Siel",
    "Destruction": "Zikel",
    "Death": "Triniel",
    "Wisdom": "Lumiel",
    "Destiny": "Marchutan",
    "Space": "Israphel",
}

# --------------------------------------------------------------------------
# Display names observed across three classes. Treat as open -- unknown ids
# must pass through rather than be dropped.
# --------------------------------------------------------------------------
STAT_DISPLAY_NAMES: dict[str, str] = {
    "WeaponFixingDamage": "Attack",
    "WeaponAccuracy": "Accuracy",
    "Accuracy": "Accuracy Bonus",
    "AbnormalAccuracy": "Status Effect Chance",
    "Critical": "Critical Hit",
    "CriticalResist": "Critical Hit Resist",
    "CriticalDamageDefense": "Critical Damage Defense",
    "Block": "Block",
    "Evasion": "Evasion Bonus",
    "ArmorEvasion": "Evasion",
    "ArmorDefense": "Defense",
    "HPMax": "HP",
    "MPMax": "MP",
    "HPRegen": "Natural HP Regen",
    "MPRegen": "Natural MP Regen",
    "Restoration": "Regeneration",
    "AmplifyAllDamage": "Damage Boost",
    "AmplifyWeaponDamage": "Weapon Damage Boost",
    "DamageRatio": "Attack increase",
    "DefensePierce": "Penetration",
    "AdditionalHitRate": "Multi-hit Chance",
    "Perfect": "Perfect Chance",
    "PvEAmplifyDamage": "PvE Damage Boost",
    "PvEDecreaseDamage": "PvE Damage Tolerance",
    "PvPDamageDefense": "PvP Defense",
    "SealStoneAddDamage": "Power Shard Damage Bonus",
    "FrontAttackDefense": "Front Defense",
    "CombatSpeed": "Combat Speed",
    "MoveSpeed": "Move Speed",
    "HpPotionRate": "HP Potion Recovery increase",
    **BASE_ATTRIBUTES,
}

# --------------------------------------------------------------------------
# Manastone value table, derived from 63 stones equipped on our own Ranger.
#
# CAVEAT: values are not fixed per grade. HP was seen at Rare +50, +60 and +80
# on the same character, so a grade defines a band rather than a number. The
# values below are the highest observed per grade and should be read as "about
# this much". Gaps are gaps in observation, not zeros.
# --------------------------------------------------------------------------
MANASTONE_GRADES: tuple[str, ...] = ("Common", "Rare", "Legend", "Unique")

MANASTONE_VALUES: dict[str, dict[str, int]] = {
    "Accuracy":             {"Rare": 7, "Legend": 10},
    "WeaponFixingDamage":   {"Rare": 3, "Legend": 8, "Unique": 10},
    "Critical":             {"Rare": 7, "Legend": 10, "Unique": 15},
    "Block":                {"Common": 7, "Rare": 10, "Legend": 15, "Unique": 20},
    "Evasion":              {"Common": 5, "Rare": 7, "Legend": 13, "Unique": 15},
    "CriticalResist":       {"Common": 5, "Rare": 7},
    "ArmorDefense":         {"Rare": 30, "Unique": 100},
    "HPMax":                {"Common": 30, "Rare": 80, "Legend": 100, "Unique": 160},
    "MPMax":                {"Common": 15, "Rare": 30, "Unique": 80},
    "PvPDamageDefense":     {"Common": 20},
    # Seen exactly once, on a cloak. Treat as a special drop, not something to
    # recommend socketing twenty of -- hence its absence from RECOMMENDABLE.
    "AmplifyWeaponDamage":  {"Unique": 100},
}

# Stones we are willing to RECOMMEND. Restricted to DPS stats observed at
# more than one grade, which is the evidence that they are ordinarily
# obtainable rather than a one-off drop.
RECOMMENDABLE_STONES: frozenset[str] = frozenset({
    "Accuracy",            # Rare +7, Legend +10
    "Critical",            # Rare +7, Legend +10, Unique +15
    "WeaponFixingDamage",  # Rare +3, Legend +8, Unique +10
})


def grade_rank(grade: str) -> int:
    """Ordinal rank of a manastone grade. Unknown grades sort lowest."""
    try:
        return MANASTONE_GRADES.index(grade)
    except ValueError:
        return -1


def stone_value(stat_id: str, grade: str) -> int | None:
    """Observed value for a stone of this stat at this grade, or None."""
    return MANASTONE_VALUES.get(stat_id, {}).get(grade)


def display_name(stat_id: str, fallback: str = "") -> str:
    """Human label for a stat id, preferring our table over the API's
    localized `name` field."""
    return STAT_DISPLAY_NAMES.get(stat_id) or fallback or stat_id


def is_wasted(stat_id: str) -> bool:
    return stat_id in WASTED_DPS_IDS


def is_accuracy(stat_id: str) -> bool:
    """Exact-match accuracy test. See the AbnormalAccuracy trap above."""
    return stat_id in ACCURACY_IDS


def best_manastone_for(stat_id: str) -> tuple[str, int] | None:
    """Highest observed grade and value for a stat, or None if unknown."""
    table = MANASTONE_VALUES.get(stat_id)
    if not table:
        return None
    grade = max(table, key=grade_rank)
    return grade, table[grade]
