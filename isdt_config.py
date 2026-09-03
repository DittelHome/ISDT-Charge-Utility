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
    "bind_uuid": "",          # Persistent Bind UUID (Hex string) – generated once and reused
    "selected_model": "C4 Air",  # Manually selected model: C4 Air, A4 Air, A8 Air, NP2 Air
    "poll_interval": 2,       # Polling interval in seconds (minimum 2)
}


def load_config():
    """Load configuration from JSON file, or return defaults."""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                config = json.load(f)
                # Ensure selected_model exists
                if "selected_model" not in config:
                    config["selected_model"] = "C4 Air"
                # Ensure poll_interval exists and is at least 3
                if "poll_interval" not in config or config.get("poll_interval", 0) < 2:
                    config["poll_interval"] = 2
                return config
        except:
            pass
    return DEFAULT_CONFIG.copy()


def save_config(config):
    """Save configuration to JSON file."""
    # Ensure poll_interval is at least 2
    if config.get("poll_interval", 0) < 2:
        config["poll_interval"] = 2
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)
