#!/usr/bin/env python3
"""
ISDT Model Definitions – Central configuration for all supported models.

This file contains all model-specific settings including:
- Number of slots
- Maximum charging current
- Supported battery types
- Display names
- Battery type mappings
- Battery-specific validation limits (including default cut-off values)
- Global current limits

Author: Klaus Voigt
"""

from typing import Dict, Any, List

# ------------------------------------------------------------------
# Model Definitions
# ------------------------------------------------------------------
# Each model has the following properties:
# - slots: Number of charging slots (2, 4, 6, or 8)
# - max_current_mA: Maximum charging current in milliamperes
# - battery_types: List of supported battery chemistries (including "Auto")
# - display_name: Human-readable name shown in the GUI
# - supports_alarm: Whether the device supports the alarm tone feature
# - default_current_mA: Default charging current (used as initial value)
# - default_capacity_mAh: Default capacity limit (used as initial value)
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
        "supports_alarm": False,  # A4 Air does NOT support alarm tone
        "default_current_mA": 300,
        "default_capacity_mAh": 2500,
    },
    "A8 Air": {
        "slots": 8,
        "max_current_mA": 1000,
        "battery_types": ["LiHV", "LiIon", "LiFe", "NiZn", "NiMh/NiCd", "LiIon(1.5V)", "Auto"],
        "display_name": "A8 Air",
        "supports_alarm": False,  # A8 Air does NOT support alarm tone
        "default_current_mA": 300,
        "default_capacity_mAh": 2500,
    },
    "NP2 Air": {
        "slots": 2,
        "max_current_mA": 1500,
        "battery_types": ["LiIon", "Auto"],
        "display_name": "NP2 Air",
        "supports_alarm": True,
        "default_current_mA": 300,
        "default_capacity_mAh": 5000,
    },
}

# ------------------------------------------------------------------
# Battery Type Mappings
# ------------------------------------------------------------------
# Maps string representations to integer values used by the charger protocol.
# NiMh and NiCd share the same protocol value (4) because they are handled
# identically by the charger.
# ------------------------------------------------------------------
BATTERY_TYPE_STR_TO_INT = {
    "LiHV": 0,
    "LiIon": 1,
    "LiFe": 2,
    "NiZn": 3,
    "NiMh/NiCd": 4,      # NiMh and NiCd share the same protocol value
    "LiIon(1.5V)": 5,
    "Auto": 6,
}

# Reverse mapping for displaying battery type names
BATTERY_TYPE_INT_TO_STR = {v: k for k, v in BATTERY_TYPE_STR_TO_INT.items()}

# ------------------------------------------------------------------
# Battery-Specific Limits (Validation + Defaults)
# ------------------------------------------------------------------
# Each battery type has validation limits and defaults for:
# - capacity_min / capacity_max: Allowed capacity range in mAh (0 = unlimited)
# - cutoff_min / cutoff_max: Allowed cut-off voltage range in mV (0 = disabled)
# - cutoff_enabled: Whether the cut-off setting is available for this type
# - cutoff_default: Default cut-off value in mV (used when battery type is selected)
# ------------------------------------------------------------------
BATTERY_LIMITS = {
    "LiHV": {
        "capacity_min": 2000,
        "capacity_max": 7000,
        "cutoff_min": 4250,
        "cutoff_max": 4450,
        "cutoff_enabled": True,
        "cutoff_default": 4350,
    },
    "LiIon": {
        "capacity_min": 2000,
        "capacity_max": 7000,
        "cutoff_min": 4100,
        "cutoff_max": 4300,
        "cutoff_enabled": True,
        "cutoff_default": 4200,
    },
    "LiFe": {
        "capacity_min": 2000,
        "capacity_max": 7000,
        "cutoff_min": 3550,
        "cutoff_max": 3750,
        "cutoff_enabled": True,
        "cutoff_default": 3650,
    },
    "NiZn": {
        "capacity_min": 2000,
        "capacity_max": 7000,
        "cutoff_min": 1800,
        "cutoff_max": 2000,
        "cutoff_enabled": True,
        "cutoff_default": 1900,
    },
    "NiMh/NiCd": {
        "capacity_min": 1000,
        "capacity_max": 4000,
        "cutoff_min": 3,
        "cutoff_max": 12,
        "cutoff_enabled": True,
        "cutoff_default": 4,
    },
    "LiIon(1.5V)": {
        "capacity_min": 1000,
        "capacity_max": 4000,
        "cutoff_min": 0,
        "cutoff_max": 0,
        "cutoff_enabled": False,
        "cutoff_default": 0,
    },
    "Auto": {
        "capacity_min": 0,
        "capacity_max": 0,
        "cutoff_min": 0,
        "cutoff_max": 0,
        "cutoff_enabled": False,
        "cutoff_default": 0,
    },
}

# ------------------------------------------------------------------
# Global Current Limits
# ------------------------------------------------------------------
# These apply to all battery types and models
CURRENT_MIN_MA = 100   # Minimum charging current (0.1A)
CURRENT_MAX_MA = 2500  # Maximum charging current (2.0A) - highest across all models

# ------------------------------------------------------------------
# Helper Functions
# ------------------------------------------------------------------

def get_model_config(model_key: str) -> Dict[str, Any]:
    """
    Get the configuration for a specific model.
    
    Args:
        model_key: The model identifier (e.g., "C4 Air")
        
    Returns:
        Dictionary with model configuration, or C4 Air config as fallback
    """
    return ISDT_MODELS.get(model_key, ISDT_MODELS["C4 Air"])


def get_default_cutoff(battery_type: str) -> int:
    """
    Get the default cut-off value for a battery type.
    
    Args:
        battery_type: The battery type string (e.g., "LiHV", "NiMh/NiCd")
        
    Returns:
        Default cut-off value in mV, or 0 if not supported
    """
    limits = BATTERY_LIMITS.get(battery_type)
    if limits:
        return limits.get("cutoff_default", 0)
    return 0


def get_default_current(model_key: str) -> int:
    """
    Get the default charging current for a model.
    
    Args:
        model_key: The model identifier (e.g., "C4 Air")
        
    Returns:
        Default current in mA (300 for all models)
    """
    config = get_model_config(model_key)
    return config.get("default_current_mA", 300)


# ------------------------------------------------------------------
# Deprecated Auto-Detection Functions (kept for reference)
# These are no longer used because model selection is manual.
# ------------------------------------------------------------------

def detect_model_from_device_name(device_name: str) -> str:
    """
    DEPRECATED: Model detection from BLE device name.
    No longer used because model selection is manual.
    
    Args:
        device_name: The BLE device name as reported during scan
        
    Returns:
        "unknown" always (placeholder)
    """
    return "unknown"


def detect_model_from_bind_response(bind_response: bytes) -> str:
    """
    DEPRECATED: Model detection from bind response.
    No longer used because model selection is manual.
    
    Args:
        bind_response: The raw bytes from the bind response
        
    Returns:
        "unknown" always (placeholder)
    """
    return "unknown"
