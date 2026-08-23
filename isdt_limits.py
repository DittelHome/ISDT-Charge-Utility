#!/usr/bin/env python3
"""
ISDT Battery Limits – Central configuration for all valid ranges.

This file contains the min/max values for capacity, current, and cut‑off
per battery type. It is used by the GUI for validation and UI state control.

Author: Klaus Voigt
"""

# Current limits – same for all battery types
CURRENT_MIN_MA = 100
CURRENT_MAX_MA = 2000

# Battery‑specific limits
BATTERY_LIMITS = {
    "LiHV": {
        "capacity_min": 2000,
        "capacity_max": 7000,
        "cutoff_min": 4250,
        "cutoff_max": 4450,
        "cutoff_enabled": True,
    },
    "LiIon": {
        "capacity_min": 2000,
        "capacity_max": 7000,
        "cutoff_min": 4100,
        "cutoff_max": 4300,
        "cutoff_enabled": True,
    },
    "LiFe": {
        "capacity_min": 2000,
        "capacity_max": 7000,
        "cutoff_min": 3550,
        "cutoff_max": 3750,
        "cutoff_enabled": True,
    },
    "NiZn": {
        "capacity_min": 2000,
        "capacity_max": 7000,
        "cutoff_min": 1800,
        "cutoff_max": 2000,
        "cutoff_enabled": True,
    },
    "NiMh/NiCd": {
        "capacity_min": 1000,
        "capacity_max": 4000,
        "cutoff_min": 3,
        "cutoff_max": 12,
        "cutoff_enabled": True,
    },
    "LiIon(1.5V)": {
        "capacity_min": 1000,
        "capacity_max": 4000,
        "cutoff_min": 0,
        "cutoff_max": 0,
        "cutoff_enabled": False,
    },
    "Auto": {
        "capacity_min": 0,
        "capacity_max": 0,
        "cutoff_min": 0,
        "cutoff_max": 0,
        "cutoff_enabled": False,
    },
}