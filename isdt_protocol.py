#!/usr/bin/env python3
"""
ISDT BLE Protocol – Contains all BLE commands and parsers for the ISDT C4/A4/A8/NP2 Air.

Author: Klaus Voigt
"""

# ------------------------------------------------------------------
# GATT UUIDs
# ------------------------------------------------------------------
CHAR_UUID_AF01 = "0000af01-0000-1000-8000-00805f9b34fb"
CHAR_UUID_AF02 = "0000af02-0000-1000-8000-00805f9b34fb"

# ------------------------------------------------------------------
# AF02 Commands (Connection Setup)
# ------------------------------------------------------------------
CMD_HW_INFO_REQ = 0xE0
CMD_HW_INFO_RESP = 0xE1
CMD_START_DATA = 0xE2
CMD_BIND_REQ = 0x18
CMD_BIND_RESP = 0x19

# ------------------------------------------------------------------
# AF01 Commands (Data Polling & Control)
# ------------------------------------------------------------------
CMD_WORKSTATE = bytes([0x13, 0xE6])          # Query charge state
CMD_ELECTRIC = bytes([0x12, 0xE4])           # Query voltages/currents
CMD_IR = bytes([0x13, 0xFA])                 # Query internal resistance
CMD_WORKTASKS_REQ = 0xEA                     # Set charging parameters

# Alarm tone
CMD_ALARM_TONE_REQ = bytes([0x12, 0x92])     # Query alarm tone state
CMD_ALARM_TONE_RESP = 0x93                   # Alarm tone response
CMD_ALARM_TONE_TASK_REQ = bytes([0x13, 0x9C]) # Set alarm tone
CMD_ALARM_TONE_TASK_RESP = 0x9D              # Alarm tone set confirmation

# ------------------------------------------------------------------
# Response Opcodes
# ------------------------------------------------------------------
RESP_WORKSTATE = 0xE7
RESP_ELECTRIC = 0xE5
RESP_IR = 0xFB

# ------------------------------------------------------------------
# Status Mappings
# ------------------------------------------------------------------
WORK_STATE_MAP = {
    0: "idle",
    1: "Pre-charge / trickle",
    2: "CC constant current",
    3: "Active charging",
    4: "CV constant voltage",
    5: "error",
    6: "done",
}

BATTERY_TYPE_MAP = {
    0: "LiHV",
    1: "LiIon",
    2: "LiFe",
    3: "NiZn",
    4: "NiMh/NiCd",
    5: "LiIon(1.5V)",
    6: "Auto",
}

# ------------------------------------------------------------------
# Parsers
# ------------------------------------------------------------------

def parse_hardware_info(data: bytes) -> dict | None:
    """Parse HardwareInfoResp (0xE1)."""
    if len(data) < 13 or data[0] != CMD_HW_INFO_RESP:
        return None
    return {
        "hw_version": f"{data[1]}.{data[2]}",
        "fw_version": f"{data[3]}.{data[4]}",
        "serial": int.from_bytes(data[5:13], "little"),
    }


def parse_workstate(data: bytes) -> dict | None:
    """Parse WorkStateResp (0xE7)."""
    if len(data) < 36 or data[1] != RESP_WORKSTATE:
        return None
    return {
        "channel": data[2],
        "status": data[3],
        "status_str": WORK_STATE_MAP.get(data[3], "unknown"),
        "capacity_percent": data[4],
        "capacity_mAh": int.from_bytes(data[5:9], "little"),
        "energy_mWh": int.from_bytes(data[9:13], "little"),
        "work_period_ms": int.from_bytes(data[13:17], "little"),
        "battery_type": data[17],
        "battery_type_str": BATTERY_TYPE_MAP.get(data[17], "unknown"),
        "full_charged_volt_mV": int.from_bytes(data[20:22], "little"),
        "max_current_mA": int.from_bytes(data[22:26], "little"),  # Umbenannt von work_current_mA
        # Charger stores capacity limit in this field (protocol quirk)
        "max_output_power_mW": int.from_bytes(data[32:36], "little"),
    }


def parse_electric(data: bytes) -> dict | None:
    """Parse ElectricResp (0xE5)."""
    if len(data) < 12 or data[1] != RESP_ELECTRIC:
        return None
    channel = data[2]
    is_long = len(data) > 35
    if is_long:
        input_voltage_mV = int.from_bytes(data[3:7], "little")
        input_current_mA = int.from_bytes(data[7:11], "little")
        output_voltage_mV = int.from_bytes(data[11:15], "little")
        charging_current_mA = int.from_bytes(data[15:19], "little")
        cells = []
        offset = 19
        while offset + 1 < len(data) and len(cells) < 16:
            cell_mV = int.from_bytes(data[offset:offset + 2], "little")
            if cell_mV == 0:
                break
            cells.append(cell_mV)
            offset += 2
    else:
        input_voltage_mV = int.from_bytes(data[3:5], "little")
        input_current_mA = int.from_bytes(data[5:9], "little")
        output_voltage_mV = int.from_bytes(data[9:11], "little")
        charging_current_mA = int.from_bytes(data[11:15], "little")
        cells = []
        offset = 15
        while offset + 1 < len(data) and len(cells) < 8:
            cell_mV = int.from_bytes(data[offset:offset + 2], "little")
            if cell_mV == 0:
                break
            cells.append(cell_mV)
            offset += 2
    result = {
        "channel": channel,
        "input_voltage_mV": input_voltage_mV,
        "input_current_mA": input_current_mA,
        "output_voltage_mV": output_voltage_mV,
        "charging_current_mA": charging_current_mA,
    }
    if cells:
        result["cells_mV"] = cells
        result["voltage_mV"] = sum(cells)
    else:
        result["voltage_mV"] = output_voltage_mV
    return result


def parse_ir(data: bytes) -> dict | None:
    """Parse IRResp (0xFB)."""
    if len(data) < 4 or data[1] != RESP_IR:
        return None
    channel = data[2]
    ir_values = []
    offset = 3
    while offset + 1 < len(data):
        raw = int.from_bytes(data[offset:offset + 2], "little")
        if raw == 0 or raw >= 10000:
            break
        ir_values.append(raw / 10.0)
        offset += 2
    return {
        "channel": channel,
        "ir_values_mohm": ir_values,
        "ir_total_mohm": sum(ir_values) if ir_values else 0,
    }


def parse_alarm_tone(data: bytes) -> bool | None:
    """Parse AlarmToneResp (0x93) – returns True if on, False if off."""
    if len(data) < 3 or data[1] != CMD_ALARM_TONE_RESP:
        return None
    return data[2] != 0


def parse_charger_responses(data: bytes) -> dict:
    """Auto-detect and parse any charger response."""
    if len(data) < 2:
        return {}
    cmd = data[1]
    if cmd == RESP_WORKSTATE:
        return parse_workstate(data) or {}
    elif cmd == RESP_ELECTRIC:
        return parse_electric(data) or {}
    elif cmd == RESP_IR:
        return parse_ir(data) or {}
    return {}


def build_command(cmd_bytes: bytes, slot: int) -> bytes:
    """Build a command with channel byte (0-based)."""
    return cmd_bytes + bytes([slot - 1])
