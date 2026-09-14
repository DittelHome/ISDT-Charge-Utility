#!/usr/bin/env python3
"""
ISDT Config – Loads and saves user settings in a JSON file.

Author: Klaus Voigt
"""
import json
import os

CONFIG_FILE = os.path.expanduser("~/.isdt_gui_config.json")

DEFAULT_CONFIG = {
    "devices": [],
    "active_device": None,
    "poll_interval": 2,
    "heartbeat_interval": 10,
    "debug_log_folder": "",
}


def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                config = json.load(f)
                if "poll_interval" not in config or config.get("poll_interval", 0) < 2:
                    config["poll_interval"] = 2
                if "heartbeat_interval" not in config or config.get("heartbeat_interval", 0) < 1:
                    config["heartbeat_interval"] = 10
                if "debug_log_folder" not in config:
                    config["debug_log_folder"] = ""
                if "devices" not in config:
                    config["devices"] = []
                return config
        except Exception:
            pass
    return DEFAULT_CONFIG.copy()


def get_active_device(config):
    devices = config.get("devices", [])
    active_idx = config.get("active_device")
    if active_idx is not None and 0 <= active_idx < len(devices):
        return devices[active_idx]
    return None


def get_device_by_mac(config, mac_address):
    devices = config.get("devices", [])
    for device in devices:
        if device.get("mac_address") == mac_address:
            return device
    return None


def save_config(config):
    if config.get("poll_interval", 0) < 2:
        config["poll_interval"] = 2
    if config.get("heartbeat_interval", 0) < 1:
        config["heartbeat_interval"] = 10
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


def add_device(config, mac_address, selected_model, name=None, poll_interval=2):
    devices = config.get("devices", [])
    for i, device in enumerate(devices):
        if device.get("mac_address") == mac_address:
            devices[i]["selected_model"] = selected_model
            if name:
                devices[i]["name"] = name
            devices[i]["poll_interval"] = poll_interval
            config["devices"] = devices
            return i
    new_device = {
        "mac_address": mac_address,
        "selected_model": selected_model,
        "name": name or selected_model,
        "bind_uuid": "",
        "poll_interval": poll_interval,
    }
    devices.append(new_device)
    config["devices"] = devices
    return len(devices) - 1


def remove_device(config, index):
    devices = config.get("devices", [])
    if 0 <= index < len(devices):
        removed = devices.pop(index)
        config["devices"] = devices
        if config.get("active_device") == index:
            config["active_device"] = None
        elif config.get("active_device", 0) > index:
            config["active_device"] = config["active_device"] - 1
        return removed
    return None


def set_active_device(config, index):
    devices = config.get("devices", [])
    if 0 <= index < len(devices):
        config["active_device"] = index
        return True
    return False


def get_debug_log_folder(config):
    folder = config.get("debug_log_folder", "")
    if not folder:
        folder = os.path.expanduser("~")
    return folder