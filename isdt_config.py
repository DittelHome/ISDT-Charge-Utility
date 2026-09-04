#!/usr/bin/env python3
"""
ISDT Config – Loads and saves user settings in a JSON file.

Author: Klaus Voigt
"""
import json
import os

CONFIG_FILE = os.path.expanduser("~/.isdt_gui_config.json")

DEFAULT_CONFIG = {
    "devices": [],           # List of device profiles
    "active_device": None,   # Index of the active device or None
    "poll_interval": 2,      # Fallback polling interval (per-device overrides)
}


def load_config():
    """Load configuration from JSON file, or return defaults."""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                config = json.load(f)
                # Ensure poll_interval exists and is at least 2
                if "poll_interval" not in config or config.get("poll_interval", 0) < 2:
                    config["poll_interval"] = 2
                # Ensure devices list exists
                if "devices" not in config:
                    config["devices"] = []
                return config
        except:
            pass
    return DEFAULT_CONFIG.copy()


def get_active_device(config):
    """Get the active device configuration."""
    devices = config.get("devices", [])
    active_idx = config.get("active_device")
    if active_idx is not None and 0 <= active_idx < len(devices):
        return devices[active_idx]
    return None


def get_device_by_mac(config, mac_address):
    """Find a device by MAC address."""
    devices = config.get("devices", [])
    for device in devices:
        if device.get("mac_address") == mac_address:
            return device
    return None


def save_config(config):
    """Save configuration to JSON file."""
    # Ensure poll_interval is at least 2
    if config.get("poll_interval", 0) < 2:
        config["poll_interval"] = 2
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


def add_device(config, mac_address, selected_model, name=None, poll_interval=2):
    """
    Add a new device to the configuration.
    
    Args:
        config: The configuration dictionary
        mac_address: BLE MAC address
        selected_model: Model name (e.g., "C4 Air")
        name: Optional display name (defaults to model name)
        poll_interval: Polling interval in seconds for this device
    
    Returns:
        int: Index of the added device
    """
    devices = config.get("devices", [])
    
    # Check if device already exists
    for i, device in enumerate(devices):
        if device.get("mac_address") == mac_address:
            # Update existing device
            devices[i]["selected_model"] = selected_model
            if name:
                devices[i]["name"] = name
            devices[i]["poll_interval"] = poll_interval
            config["devices"] = devices
            return i
    
    # Add new device
    new_device = {
        "mac_address": mac_address,
        "selected_model": selected_model,
        "name": name or selected_model,
        "bind_uuid": "",  # Will be generated on first connection
        "poll_interval": poll_interval,  # Per-device polling interval
    }
    devices.append(new_device)
    config["devices"] = devices
    return len(devices) - 1


def remove_device(config, index):
    """
    Remove a device from the configuration by index.
    
    Args:
        config: The configuration dictionary
        index: Index of the device to remove
    
    Returns:
        dict: The removed device, or None if invalid
    """
    devices = config.get("devices", [])
    if 0 <= index < len(devices):
        removed = devices.pop(index)
        config["devices"] = devices
        # If active device was removed, reset active_device
        if config.get("active_device") == index:
            config["active_device"] = None
        elif config.get("active_device", 0) > index:
            config["active_device"] = config["active_device"] - 1
        return removed
    return None


def set_active_device(config, index):
    """
    Set the active device by index.
    
    Args:
        config: The configuration dictionary
        index: Index of the device to set as active
    
    Returns:
        bool: True if successful
    """
    devices = config.get("devices", [])
    if 0 <= index < len(devices):
        config["active_device"] = index
        return True
    return False


def get_device_poll_interval(config, device=None):
    """
    Get the polling interval for a device.
    
    Args:
        config: The configuration dictionary
        device: Optional device dictionary (if None, uses active device)
    
    Returns:
        int: Polling interval in seconds (default 2)
    """
    if device is None:
        device = get_active_device(config)
    if device:
        return device.get("poll_interval", 2)
    return config.get("poll_interval", 2)