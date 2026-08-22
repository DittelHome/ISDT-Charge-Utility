#!/usr/bin/env python3
"""
ISDT Config – Loads and saves user settings in a JSON file.

Author: Klaus Voigt
"""
import json
import os

CONFIG_FILE = os.path.expanduser("~/.isdt_gui_config.json")

DEFAULT_CONFIG = {
    "mac_address": "",
    "device_name": "",
    "poll_interval": 5,
    "bind_uuid": "",          # Persistent Bind UUID (Hex string) – generated once and reused
}


def load_config():
    """Load configuration from JSON file, or return defaults."""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                return json.load(f)
        except:
            pass
    return DEFAULT_CONFIG.copy()


def save_config(config):
    """Save configuration to JSON file."""
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)
