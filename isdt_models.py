#!/usr/bin/env python3
"""
ISDT Model Definitions – Central configuration for supported models.

Author: Klaus Voigt
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
    },
    "A4 Air": {
        "slots": 4,
        "max_current_mA": 1000,
        "battery_types": ["LiHV", "LiIon", "LiFe", "NiZn", "NiMh/NiCd", "LiIon(1.5V)", "Auto"],
        "display_name": "A4 Air",
        "supports_alarm": False,
        "default_current_mA": 300,
        "default_capacity_mAh": 2500,
    },
    "A8 Air": {
        "slots": 8,
        "max_current_mA": 1000,
        "battery_types": ["LiHV", "LiIon", "LiFe", "NiZn", "NiMh/NiCd", "LiIon(1.5V)", "Auto"],
        "display_name": "A8 Air",
        "supports_alarm": False,
        "default_current_mA": 300,
        "default_capacity_mAh": 2500,
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
# Battery limits
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
    return ISDT_MODELS.get(model_key, ISDT_MODELS["C4 Air"])


def get_default_current(model_key: str) -> int:
    config = get_model_config(model_key)
    return config.get("default_current_mA", 300)