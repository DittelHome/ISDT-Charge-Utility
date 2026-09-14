#!/usr/bin/env python3
"""
ISDT BLE Protocol – BLE command constants and response parsers.

Author: Klaus Voigt
"""

# ------------------------------------------------------------------
# GATT UUIDs
# ------------------------------------------------------------------
CHAR_UUID_AF01 = "0000af01-0000-1000-8000-00805f9b34fb"
CHAR_UUID_AF02 = "0000af02-0000-1000-8000-00805f9b34fb"

# ------------------------------------------------------------------
# AF02 Commands (connection setup)
# ------------------------------------------------------------------
CMD_HW_INFO_REQ = 0xE0
CMD_HW_INFO_RESP = 0xE1
CMD_BIND_REQ = 0x18
CMD_BIND_RESP = 0x19

# ------------------------------------------------------------------
# AF01 Commands (data polling and control)
# ------------------------------------------------------------------
CMD_WORKSTATE = bytes([0x13, 0xE6])
CMD_ELECTRIC = bytes([0x12, 0xE4])
CMD_IR = bytes([0x13, 0xFA])
CMD_WORKTASKS_REQ = 0xEA

CMD_ALARM_TONE_REQ = bytes([0x12, 0x92])
CMD_ALARM_TONE_RESP = 0x93
CMD_ALARM_TONE_TASK_REQ = bytes([0x13, 0x9C])
CMD_ALARM_TONE_TASK_RESP = 0x9D

# ------------------------------------------------------------------
# Response opcodes
# ------------------------------------------------------------------
RESP_WORKSTATE = 0xE7
RESP_ELECTRIC = 0xE5
RESP_IR = 0xFB
RESP_A8_TASK = 0xED

# ------------------------------------------------------------------
# Work state and battery type maps
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
    0: "LiHV", 1: "LiIon", 2: "LiFe", 3: "NiZn",
    4: "NiMh/NiCd", 5: "LiIon(1.5V)", 6: "Auto",
}


# ------------------------------------------------------------------
# Parsers
# ------------------------------------------------------------------

def parse_hardware_info(data: bytes) -> dict | None:
    if len(data) < 13 or data[0] != CMD_HW_INFO_RESP:
        return None
    return {
        "hw_version": f"{data[1]}.{data[2]}",
        "fw_version": f"{data[3]}.{data[4]}",
        "serial": int.from_bytes(data[5:13], "little"),
    }


def parse_workstate(data: bytes) -> dict | None:
    if len(data) < 36 or data[1] != RESP_WORKSTATE:
        return None

    status = data[3]
    return {
        "channel": data[2],
        "status": status,
        "status_str": WORK_STATE_MAP.get(status, f"active ({status})"),
        "capacity_percent": data[4],
        "capacity_mAh": int.from_bytes(data[5:9], "little"),
        "energy_mWh": int.from_bytes(data[9:13], "little"),
        "work_period_ms": int.from_bytes(data[13:17], "little"),
        "battery_type": data[17],
        "battery_type_str": BATTERY_TYPE_MAP.get(data[17], "unknown"),
        "full_charged_volt_mV": int.from_bytes(data[20:22], "little"),
        "max_current_mA": int.from_bytes(data[22:26], "little"),
        "max_output_power_mW": int.from_bytes(data[32:36], "little"),
    }


def parse_a8_workstate_mega(data: bytes) -> dict | None:
    if len(data) < 3 or data[1] != RESP_WORKSTATE:
        return None
    total_channels = data[2]
    if total_channels < 1 or total_channels > 8:
        return None
    # Minimum length: 3-byte header + total_channels * 25 bytes
    if len(data) < 3 + total_channels * 25:
        return None
    result = {}
    offset = 3
    for channel in range(total_channels):
        if offset + 25 > len(data):
            break
        status = data[offset]
        capacity_percent = data[offset + 1]
        capacity_mAh = int.from_bytes(data[offset + 2:offset + 6], "little")
        energy_mWh = int.from_bytes(data[offset + 6:offset + 10], "little")
        work_period_ms = int.from_bytes(data[offset + 10:offset + 14], "little")
        battery_type = data[offset + 14]
        work_current_mA = int.from_bytes(data[offset + 15:offset + 19], "little")
        voltage_mV = int.from_bytes(data[offset + 19:offset + 21], "little")
        ir_mohm = int.from_bytes(data[offset + 21:offset + 23], "little") / 100.0
        error_code = int.from_bytes(data[offset + 23:offset + 25], "little")
        slot_data = {
            "channel": channel,
            "status": status,
            "status_str": WORK_STATE_MAP.get(status, f"active ({status})"),
            "capacity_percent": capacity_percent,
            "capacity_mAh": capacity_mAh,
            "energy_mWh": energy_mWh,
            "work_period_ms": work_period_ms,
            "battery_type": battery_type,
            "battery_type_str": BATTERY_TYPE_MAP.get(battery_type, "unknown"),
            "full_charged_volt_mV": 0,
            "max_current_mA": 0,
            "work_current_mA": work_current_mA,
            "voltage_mV": voltage_mV,
            "ir_mohm": ir_mohm,
            "error_code": error_code,
        }
        result[f"slot{channel + 1}"] = slot_data
        offset += 25
    return result


def parse_a8_task_resp(data: bytes) -> dict | None:
    if len(data) < 3 or data[1] != RESP_A8_TASK:
        return None
    total_channels = data[2]
    result = {}
    offset = 3
    for channel in range(total_channels):
        if offset + 12 > len(data):
            break
        task_type = data[offset]
        battery_type = data[offset + 1]
        max_current_mA = int.from_bytes(data[offset + 2:offset + 6], "little")
        voltage_mV = int.from_bytes(data[offset + 6:offset + 8], "little")
        cap_limit_mAh = int.from_bytes(data[offset + 8:offset + 12], "little")
        slot_data = {
            "channel": channel,
            "task_type": task_type,
            "battery_type": battery_type,
            "battery_type_str": BATTERY_TYPE_MAP.get(battery_type, "unknown"),
            "max_current_mA": max_current_mA,
            "voltage_mV": voltage_mV,
            "max_output_power_mW": cap_limit_mAh,
        }
        result[f"slot{channel + 1}"] = slot_data
        offset += 12
    return result


def parse_electric(data: bytes) -> dict | None:
    if len(data) < 3 or data[1] != RESP_ELECTRIC:
        return None
    channel = data[2]
    if len(data) == 9 and channel == 8:
        input_voltage_mV = int.from_bytes(data[3:5], "little")
        input_current_mA = int.from_bytes(data[5:9], "little")
        return {
            "channel": 0,
            "input_voltage_mV": input_voltage_mV,
            "input_current_mA": input_current_mA,
            "output_voltage_mV": 0,
            "charging_current_mA": 0,
            "voltage_mV": 0,
        }
    if len(data) < 12:
        return None
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
        if channel < len(cells):
            result["voltage_mV"] = cells[channel]
        else:
            result["voltage_mV"] = cells[0] if cells else output_voltage_mV
    else:
        result["voltage_mV"] = output_voltage_mV
    return result


def parse_ir(data: bytes) -> dict | None:
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
    if channel < len(ir_values):
        ir_total = ir_values[channel]
    else:
        ir_total = ir_values[0] if ir_values else 0
    return {
        "channel": channel,
        "ir_values_mohm": ir_values,
        "ir_total_mohm": ir_total,
    }


def parse_alarm_tone(data: bytes) -> bool | None:
    if len(data) < 3 or data[1] != CMD_ALARM_TONE_RESP:
        return None
    return data[2] != 0


def parse_charger_responses(data: bytes) -> dict:
    if len(data) < 2:
        return {}
    cmd = data[1]
    if cmd == RESP_WORKSTATE:
        return parse_workstate(data) or {}
    elif cmd == RESP_ELECTRIC:
        return parse_electric(data) or {}
    elif cmd == RESP_IR:
        return parse_ir(data) or {}
    elif cmd == RESP_A8_TASK:
        return parse_a8_task_resp(data) or {}
    return {}


def build_command(cmd_bytes: bytes, slot: int) -> bytes:
    return cmd_bytes + bytes([slot - 1])