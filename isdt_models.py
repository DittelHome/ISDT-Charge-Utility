#!/usr/bin/env python3
"""
ISDT Model Definitions – Central configuration for all supported models.

This file contains all model-specific settings including:
- Number of slots
- Maximum charging current
- Supported battery types
- Display names
- Device name patterns for auto-detection
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
# - name_patterns: List of strings to match against BLE device names
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
        "name_patterns": ["C4Air", "C4 Air", "0000C4Air"],
    },
    "A4 Air": {
        "slots": 4,
        "max_current_mA": 1000,
        "battery_types": ["NiMh/NiCd", "LiIon", "LiFe", "Auto"],
        "display_name": "A4 Air",
        "supports_alarm": True,
        "default_current_mA": 300,
        "default_capacity_mAh": 2500,
        "name_patterns": ["A4Air", "A4 Air", "0000A4Air"],
    },
    "A8 Air": {
        "slots": 8,
        "max_current_mA": 1000,
        "battery_types": ["LiHV", "NiMh/NiCd", "LiIon", "LiFe", "Auto"],
        "display_name": "A8 Air",
        "supports_alarm": True,
        "default_current_mA": 300,
        "default_capacity_mAh": 2500,
        "name_patterns": ["A8Air", "A8 Air", "0000A8Air", "A8"],
    },
    "NP2 Air": {
        "slots": 2,
        "max_current_mA": 1500,
        "battery_types": ["LiIon", "Auto"],
        "display_name": "NP2 Air",
        "supports_alarm": True,
        "default_current_mA": 300,
        "default_capacity_mAh": 5000,
        "name_patterns": ["NP2Air", "NP2 Air", "0000NP2Air"],
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


def detect_model_from_device_name(device_name: str) -> str:
    """
    Detect the ISDT model from the BLE device name.
    
    The device name typically follows this pattern:
    - "0000C4Air S00" → C4 Air
    - "0000A4Air S00" → A4 Air
    - "0000A8Air S00" → A8 Air
    - "0000NP2Air S00" → NP2 Air
    
    Args:
        device_name: The BLE device name as reported during scan
        
    Returns:
        The model key (e.g., "C4 Air") or "C4 Air" as fallback
    """
    if not device_name:
        return "C4 Air"
    
    # Remove spaces and convert to uppercase for case-insensitive matching
    device_name_clean = device_name.replace(" ", "").upper()
    
    # Try to match against known name patterns for each model
    for model_key, config in ISDT_MODELS.items():
        for pattern in config.get("name_patterns", []):
            pattern_clean = pattern.replace(" ", "").upper()
            if pattern_clean in device_name_clean:
                return model_key
    
    # Fallback: Look for model identifiers in the device name
    if "C4" in device_name_clean:
        return "C4 Air"
    elif "A4" in device_name_clean:
        return "A4 Air"
    elif "A8" in device_name_clean:
        return "A8 Air"
    elif "NP2" in device_name_clean:
        return "NP2 Air"
    
    # Ultimate fallback - assume C4 Air
    return "C4 Air"


def detect_model_from_bind_response(bind_response: bytes) -> str:
    """
    Fallback: Detect model from the bind response.
    
    The bind response may contain a model ID at byte position 2.
    This is used when no device name is available.
    
    Args:
        bind_response: The raw bytes from the bind response
        
    Returns:
        The model key or "C4 Air" as fallback
    """
    if not bind_response or len(bind_response) < 3:
        return "C4 Air"
    
    # Byte 2 often contains a model identifier
    if len(bind_response) >= 3:
        model_id = bind_response[2]
        
        # Map model IDs to model keys
        if model_id == 0x04:
            return "C4 Air"
        elif model_id == 0x02:
            return "A4 Air"
        elif model_id == 0x03:
            return "A8 Air"
        elif model_id == 0x05:
            return "A8 Air"
        elif model_id == 0x06:
            return "NP2 Air"
        elif model_id == 0x01:
            return "C4 Air"
    
    return "C4 Air"
