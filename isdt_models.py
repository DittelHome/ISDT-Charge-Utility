#!/usr/bin/env python3
"""
ISDT Model Definitions – Central configuration for supported charger models.

All model-specific parameters live here: slot count, current limits, supported
battery types, poll interval bounds, IR scaling and slot ordering.
"""

from typing import Dict, Any


# ------------------------------------------------------------------
# Model definitions
# ------------------------------------------------------------------
ISDT_MODELS = {
    "C4 Air": {
        "slots": 6,
        "max_current_mA": 2000,
        "battery_types": ["LiHV", "LiIon", "LiFe", "NiZn", "NiMh/NiCd", "LiIon(1.5V)", "Auto"],
        "display_name": "C4 Air",
        "supports_alarm": True,
        "default_current_mA": 300,
        "default_capacity_mAh": 2500,
        "poll_interval": 4,
        "min_poll_interval": 2,
        "max_poll_interval": 60,
        # IR raw value is transmitted in 0.1 mΩ units -> divide by 10
        "ir_divisor": 10.0,
        # C4 slot order matches the GUI slot order
        "slot_order_reversed": False,
    },
    "A4 Air": {
        "slots": 4,
        "max_current_mA": 1000,
        "battery_types": ["LiHV", "LiIon", "LiFe", "NiZn", "NiMh/NiCd", "LiIon(1.5V)", "Auto"],
        "display_name": "A4 Air",
        "supports_alarm": False,
        "default_current_mA": 300,
        "default_capacity_mAh": 2500,
        "poll_interval": 10,
        "min_poll_interval": 8,
        "max_poll_interval": 60,
        # IR raw value is transmitted in 0.01 mΩ units -> divide by 100
        "ir_divisor": 100.0,
        # A4 reports slots in reverse order (physical slot 1 = protocol channel 3)
        "slot_order_reversed": True,
    },
    "A8 Air": {
        "slots": 8,
        "max_current_mA": 1000,
        "battery_types": ["LiHV", "LiIon", "LiFe", "NiZn", "NiMh/NiCd", "LiIon(1.5V)", "Auto"],
        "display_name": "A8 Air",
        "supports_alarm": False,
        "default_current_mA": 300,
        "default_capacity_mAh": 2500,
        "poll_interval": 10,
        "min_poll_interval": 10,
        "max_poll_interval": 60,
        # A8 IR is parsed directly in parse_a8_workstate_mega (hardcoded /100)
        "ir_divisor": 100.0,
        "slot_order_reversed": False,
    },
}


# ------------------------------------------------------------------
# Battery type mappings
# ------------------------------------------------------------------
BATTERY_TYPE_STR_TO_INT = {
    "LiHV": 0,
    "LiIon": 1,
    "LiFe": 2,
    "NiZn": 3,
    "NiMh/NiCd": 4,
    "LiIon(1.5V)": 5,
    "Auto": 6,
}


# ------------------------------------------------------------------
# Battery limits (capacity and cut-off voltage ranges per chemistry)
# ------------------------------------------------------------------
BATTERY_LIMITS = {
    "LiHV": {
        "capacity_min": 2000, "capacity_max": 7000,
        "cutoff_min": 4250, "cutoff_max": 4450,
        "cutoff_enabled": True, "cutoff_default": 4350,
    },
    "LiIon": {
        "capacity_min": 2000, "capacity_max": 7000,
        "cutoff_min": 4100, "cutoff_max": 4300,
        "cutoff_enabled": True, "cutoff_default": 4200,
    },
    "LiFe": {
        "capacity_min": 2000, "capacity_max": 7000,
        "cutoff_min": 3550, "cutoff_max": 3750,
        "cutoff_enabled": True, "cutoff_default": 3650,
    },
    "NiZn": {
        "capacity_min": 2000, "capacity_max": 7000,
        "cutoff_min": 1800, "cutoff_max": 2000,
        "cutoff_enabled": True, "cutoff_default": 1900,
    },
    "NiMh/NiCd": {
        "capacity_min": 1000, "capacity_max": 4000,
        # Cut-off is a delta in mV, not an absolute voltage
        "cutoff_min": 3, "cutoff_max": 12,
        "cutoff_enabled": True, "cutoff_default": 4,
    },
    "LiIon(1.5V)": {
        "capacity_min": 1000, "capacity_max": 4000,
        "cutoff_min": 0, "cutoff_max": 0,
        "cutoff_enabled": False, "cutoff_default": 0,
    },
    "Auto": {
        "capacity_min": 0, "capacity_max": 0,
        "cutoff_min": 0, "cutoff_max": 0,
        "cutoff_enabled": False, "cutoff_default": 0,
    },
}


# ------------------------------------------------------------------
# Global current limits
# ------------------------------------------------------------------
CURRENT_MIN_MA = 100
CURRENT_MAX_MA = 2000


# ------------------------------------------------------------------
# Helper functions
# ------------------------------------------------------------------

def get_model_config(model_key: str) -> Dict[str, Any]:
    """Return the full configuration dict for a model.

    Falls back to the C4 Air entry if the model is unknown, so callers
    always get a valid dict.
    """
    return ISDT_MODELS.get(model_key, ISDT_MODELS["C4 Air"])


def get_default_current(model_key: str) -> int:
    """Default charge current (mA) for a freshly selected battery type."""
    config = get_model_config(model_key)
    return config.get("default_current_mA", 300)


def get_poll_interval(model_key: str) -> int:
    """Default poll interval (seconds) for the model."""
    config = get_model_config(model_key)
    return config.get("poll_interval", 10)


def get_min_poll_interval(model_key: str) -> int:
    """Minimum allowed poll interval (seconds) for the model."""
    config = get_model_config(model_key)
    return config.get("min_poll_interval", 2)


def get_max_poll_interval(model_key: str) -> int:
    """Maximum allowed poll interval (seconds) for the model."""
    config = get_model_config(model_key)
    return config.get("max_poll_interval", 60)


def is_poll_interval_fixed(model_key: str) -> bool:
    """True if the poll interval cannot be changed by the user.

    No model uses a fixed interval anymore – kept for backwards compatibility.
    """
    return False