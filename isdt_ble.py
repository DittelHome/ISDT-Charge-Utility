#!/usr/bin/env python3
"""
ISDT BLE – Encapsulates all Bluetooth communication with the charger.

Author: Klaus Voigt
"""

import sys
import time
import asyncio
import struct
import uuid

from bleak import BleakClient

from isdt_protocol import (
    CHAR_UUID_AF01, CHAR_UUID_AF02,
    CMD_HW_INFO_REQ, CMD_HW_INFO_RESP,
    CMD_BIND_REQ, CMD_BIND_RESP,
    CMD_WORKSTATE, CMD_ELECTRIC, CMD_IR, CMD_WORKTASKS_REQ,
    CMD_ALARM_TONE_REQ, CMD_ALARM_TONE_RESP,
    CMD_ALARM_TONE_TASK_REQ, CMD_ALARM_TONE_TASK_RESP,
    RESP_WORKSTATE, RESP_ELECTRIC, RESP_IR, RESP_A8_TASK,
    build_command, parse_charger_responses, parse_hardware_info,
    parse_alarm_tone, parse_a8_workstate_mega, parse_a8_task_resp,
)

from isdt_models import get_model_config


# ==================================================================
# TIMING CONSTANTS
# All delays in seconds unless noted otherwise.
# This is the ONLY place to tune timing. Everything else uses these.
# ==================================================================

# --- Connection ---
CONNECT_RETRIES = 2               # how many connect attempts
CONNECT_TIMEOUT_S = 20.0          # hard timeout for BleakClient.connect()
POST_CONNECT_SETTLE = 1.0         # wait after BLE connect
POST_NOTIFICATION_SETUP = 0.5     # wait after notify handlers are set up
BIND_TIMEOUT = 3.0                # bind handshake response timeout
HW_INFO_TIMEOUT = 3.0             # hardware-info response timeout

# --- Polling ---
COMMAND_INTERVAL = 1.0            # pause between two GATT writes in one poll
                                  # (A8 needs this – it gets overwhelmed otherwise)
RESPONSE_TIMEOUT = 5.0            # wait for any single response
MAX_POLL_FAILURES = 3             # disconnect after this many empty polls

# --- Reconnect ---
RECONNECT_DELAY_S = 15            # fixed delay before reconnect (no backoff)
START_POLL_DELAY_MS = 1000        # delay between connect and first poll


# Human-readable command names for the debug traffic log
COMMAND_NAMES = {
    0xE0: "HardwareInfoReq", 0xE1: "HardwareInfoResp", 0xE2: "StartDataStream",
    0x18: "BindReq", 0x19: "BindResp",
    0xE4: "ElectricReq", 0xE5: "ElectricResp",
    0xE6: "WorkStateReq", 0xE7: "WorkStateResp",
    0xEA: "WorkTasksReq", 0xEB: "WorkTasksResp",
    0xEC: "A8TaskReq", 0xED: "A8TaskResp",
    0xFA: "IRReq", 0xFB: "IRResp",
    0x92: "AlarmToneReq", 0x93: "AlarmToneResp",
    0x9C: "AlarmToneSet", 0x9D: "AlarmToneTaskResp",
}


class ISDTBLE:
    """BLE communication class for the ISDT chargers."""

    def __init__(self, device_info, log_callback=None, config=None):
        self.device_info = device_info
        self.address = device_info.get("mac_address", "")
        self.client = None
        self.connected = False
        self.notification_queue = asyncio.Queue()
        self.latest_data = {}
        self.log_callback = log_callback
        self.config = config or {}
        self._disconnect_lock = asyncio.Lock()

        self.model_key = device_info.get("selected_model", "C4 Air")
        self.model_config = get_model_config(self.model_key)
        self.num_slots = self.model_config["slots"]
        self.max_current_mA = self.model_config["max_current_mA"]
        self.supported_battery_types = self.model_config["battery_types"]
        self.supports_alarm = self.model_config.get("supports_alarm", True)
        self.is_a8 = (self.model_key == "A8 Air")

        self.poll_interval = device_info.get("poll_interval", 10)

        self.debug_traffic = False
        self.debug_log_file = None
        self._alarm_tone_state = False

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    def _log(self, msg):
        if self.log_callback:
            self.log_callback(msg)

    def _log_traffic(self, direction: str, data: bytes, description: str = ""):
        if not self.debug_traffic:
            return
        log_file = self.debug_log_file
        if not log_file:
            return
        from datetime import datetime
        timestamp = datetime.now().strftime("%d/%m/%Y %H:%M:%S.%f")[:-3]
        hex_str = data.hex().upper()
        hex_pairs = ' '.join(hex_str[i:i+2] for i in range(0, len(hex_str), 2))
        cmd_name = ""
        if len(data) >= 2:
            if data[0] == 0x31:
                cmd_byte = data[1]
            elif data[0] in (0x19, 0xE1):
                cmd_byte = data[0]
            elif data[0] in (0x18, 0xE0, 0xE2):
                cmd_byte = data[0]
            else:
                cmd_byte = data[1] if len(data) > 1 else data[0]
            cmd_name = COMMAND_NAMES.get(cmd_byte, "")
        name_part = f" ({cmd_name})" if cmd_name else ""
        if description:
            msg = f"[{timestamp}] {description} {direction} {hex_pairs}{name_part}"
        else:
            msg = f"[{timestamp}] {direction} {hex_pairs}{name_part}"
        try:
            log_file.write(msg + "\n")
            log_file.flush()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Notification handling
    # ------------------------------------------------------------------

    def notification_handler(self, sender, data):
        data = bytes(data)
        self._log_traffic("📥 NOTIFY", data)
        asyncio.create_task(self.notification_queue.put(data))

    async def _wait_for_opcode(self, expected_opcode, timeout, expected_channel=None):
        """Wait for a response with the given opcode. Discards anything else."""
        start = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() - start < timeout:
            try:
                resp = await asyncio.wait_for(self.notification_queue.get(), timeout=0.5)
                if len(resp) < 2:
                    continue
                if resp[1] != expected_opcode:
                    continue
                if expected_channel is not None and len(resp) >= 3:
                    if resp[2] != expected_channel:
                        continue
                return resp
            except asyncio.TimeoutError:
                continue
        return None

    # ------------------------------------------------------------------
    # Connection setup
    # ------------------------------------------------------------------

    async def _initialize(self):
        self._log("✅ Initializing...")

        # Hardware info
        try:
            hw_cmd = struct.pack("<B", CMD_HW_INFO_REQ)
            await self.client.write_gatt_char(CHAR_UUID_AF02, hw_cmd)
            self._log_traffic("📤 WRITE", hw_cmd, "AF02")
            await asyncio.sleep(0.5)
        except Exception as e:
            self._log(f"⚠️ HW-Info error: {e}")

        self._log("✅ Bind handshake...")

        # Build bind UUID (stored or new)
        bind_uuid_hex = self.device_info.get("bind_uuid", "")
        if bind_uuid_hex:
            try:
                bind_uuid = bytes.fromhex(bind_uuid_hex)
            except ValueError:
                bind_uuid = uuid.uuid4().bytes
                self.device_info["bind_uuid"] = bind_uuid.hex()
        else:
            bind_uuid = uuid.uuid4().bytes
            self.device_info["bind_uuid"] = bind_uuid.hex()

        # Send BindReq – no waiting, no queue check
        try:
            cmd = struct.pack("<B", CMD_BIND_REQ) + bind_uuid + b"\x00\x00"
            await self.client.write_gatt_char(CHAR_UUID_AF02, cmd)
            self._log_traffic("📤 WRITE", cmd, "AF02")
        except Exception as e:
            self._log(f"⚠️ Bind write failed: {e}")

        await asyncio.sleep(0.5)

        # Start data stream
        try:
            stream_cmd = struct.pack("<B", 0xE2)
            await self.client.write_gatt_char(CHAR_UUID_AF02, stream_cmd)
            self._log_traffic("📤 WRITE", stream_cmd, "AF02")
        except Exception as e:
            self._log(f"⚠️ Start data stream error: {e}")

        await asyncio.sleep(0.5)

    async def connect(self, retries=CONNECT_RETRIES):
        attempt = 0
        while attempt < retries:
            client = None
            try:
                client = BleakClient(self.address, timeout=CONNECT_TIMEOUT_S)
                self.client = client
                self._log(f"⏳ Connecting... (attempt {attempt+1}/{retries})")

                await asyncio.wait_for(client.connect(), timeout=CONNECT_TIMEOUT_S)
                self.connected = True
                self._log("✅ BLE connected.")

                await asyncio.sleep(POST_CONNECT_SETTLE)
                await client.start_notify(CHAR_UUID_AF01, self.notification_handler)
                await client.start_notify(CHAR_UUID_AF02, self.notification_handler)
                self._log("✅ Notification handlers registered.")
                await asyncio.sleep(POST_NOTIFICATION_SETUP)

                await self._initialize()
                self._log("✅ Initialization complete.")

                if self.supports_alarm:
                    self._alarm_tone_state = await self.get_alarm_tone() or False
                return True

            except Exception as e:
                err_msg = str(e).strip()
                if not err_msg:
                    err_msg = f"{type(e).__name__} (no details)"
                self._log(f"⚠️ Connection error: {err_msg}")
                if client is not None:
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
                self.client = None
                self.connected = False
                attempt += 1
                if attempt < retries:
                    await asyncio.sleep(4.0)

        self._log("⚠️ All connection attempts failed.")
        if sys.platform == "win32":
            self._log("💡 Tip: Close the ISD Link app on your smartphone.")
            self._log("💡 Tip: Restart the Application.")
        else:
            self._log("💡 Tip: Please close the ISD Link app on your smartphone.")
            self._log("💡 Tip: Make sure the charger is powered on.")
        return False

    # ------------------------------------------------------------------
    # A8 mega-packet poll
    # ------------------------------------------------------------------

    async def _poll_a8_mega(self):
        """One poll cycle for A8 Air.

        Two writes per cycle, separated by COMMAND_INTERVAL.
        Any write failure (E_ABORT, Not connected, Unreachable) means the A8
        has dropped or is dropping the link – we set self.connected = False
        so the poll loop can trigger an immediate reconnect.
        """
        if not self.connected or not self.client:
            return False

        # --- Write 1: WorkState (mega-packet for all 8 slots) ---
        cmd = build_command(CMD_WORKSTATE, 1)
        try:
            await self.client.write_gatt_char(CHAR_UUID_AF01, cmd)
            self._log_traffic("📤 WRITE", cmd, "AF01")
        except Exception as e:
            err = str(e).lower()
            if ("-2147023673" in err or "abgebrochen" in err or "abort" in err
                    or "not connected" in err or "unreachable" in err or "disconnected" in err):
                self._log("ℹ️ A8 dropped the link – reconnecting")
                self.connected = False
                return False
            self._log(f"⚠️ A8 write failed: {e}")
            return False

        resp = await self._wait_for_opcode(RESP_WORKSTATE, RESPONSE_TIMEOUT)
        if not resp or len(resp) < 200:
            self._log("⚠️ No mega-packet received")
            return False

        parsed = parse_a8_workstate_mega(resp)
        if not parsed:
            self._log("⚠️ Mega-packet parse failed")
            return False

        for slot in range(1, self.num_slots + 1):
            slot_data = parsed.get(f"slot{slot}")
            if not slot_data:
                continue
            key = f"slot{slot}_workstate"
            old_data = self.latest_data.get(key, {})
            # Preserve task settings from previous A8Task
            if old_data.get("max_current_mA"):
                slot_data["max_current_mA"] = old_data["max_current_mA"]
            if old_data.get("max_output_power_mW"):
                slot_data["max_output_power_mW"] = old_data["max_output_power_mW"]
            if old_data.get("full_charged_volt_mV"):
                slot_data["full_charged_volt_mV"] = old_data["full_charged_volt_mV"]
            self.latest_data[key] = slot_data
            self.latest_data[f"slot{slot}_ir"] = {
                "channel": slot - 1,
                "ir_values_mohm": [slot_data.get("ir_mohm", 0)],
                "ir_total_mohm": slot_data.get("ir_mohm", 0),
            }

        # Pause between the two writes – this is the critical gap for A8
        await asyncio.sleep(COMMAND_INTERVAL)

        # --- Write 2: Electric (input voltage/current) ---
        elec_cmd = build_command(CMD_ELECTRIC, 1)
        try:
            await self.client.write_gatt_char(CHAR_UUID_AF01, elec_cmd)
            self._log_traffic("📤 WRITE", elec_cmd, "AF01")
        except Exception as e:
            err = str(e).lower()
            if ("-2147023673" in err or "abgebrochen" in err or "abort" in err
                    or "not connected" in err or "unreachable" in err or "disconnected" in err):
                self._log("ℹ️ A8 dropped the link – reconnecting")
                self.connected = False
                return False
            self._log(f"⚠️ Electric write failed: {e}")
            return True  # we already have the main data

        elec = await self._wait_for_opcode(RESP_ELECTRIC, RESPONSE_TIMEOUT)
        if elec:
            parsed_elec = parse_charger_responses(elec)
            if parsed_elec:
                self.latest_data["slot1_electric"] = parsed_elec

        return True

    # ------------------------------------------------------------------
    # C4 / A4 poll
    # ------------------------------------------------------------------

    async def _poll_c4_a4(self):
        """Simple slot-by-slot polling for C4 / A4."""
        if not self.connected or not self.client:
            return False

        received_any = False

        for slot in range(1, self.num_slots + 1):
            cmd = build_command(CMD_WORKSTATE, slot)
            try:
                await self.client.write_gatt_char(CHAR_UUID_AF01, cmd)
                self._log_traffic("📤 WRITE", cmd, "AF01")
            except Exception:
                continue

            resp = await self._wait_for_opcode(RESP_WORKSTATE, RESPONSE_TIMEOUT, expected_channel=slot - 1)
            if resp:
                parsed = parse_charger_responses(resp)
                if parsed:
                    self.latest_data[f"slot{slot}_workstate"] = parsed
                    received_any = True

                    # If slot is active, get IR + Electric
                    status_str = parsed.get("status_str", "idle")
                    if status_str not in ("idle", "empty", "done", "error"):
                        await asyncio.sleep(COMMAND_INTERVAL)

                        ir_cmd = build_command(CMD_IR, slot)
                        try:
                            await self.client.write_gatt_char(CHAR_UUID_AF01, ir_cmd)
                            self._log_traffic("📤 WRITE", ir_cmd, "AF01")
                        except Exception:
                            pass
                        ir_resp = await self._wait_for_opcode(RESP_IR, RESPONSE_TIMEOUT, expected_channel=slot - 1)
                        if ir_resp:
                            parsed_ir = parse_charger_responses(ir_resp)
                            if parsed_ir:
                                self.latest_data[f"slot{slot}_ir"] = parsed_ir

                        await asyncio.sleep(COMMAND_INTERVAL)

                        elec_cmd = build_command(CMD_ELECTRIC, slot)
                        try:
                            await self.client.write_gatt_char(CHAR_UUID_AF01, elec_cmd)
                            self._log_traffic("📤 WRITE", elec_cmd, "AF01")
                        except Exception:
                            pass
                        elec_resp = await self._wait_for_opcode(RESP_ELECTRIC, RESPONSE_TIMEOUT, expected_channel=slot - 1)
                        if elec_resp:
                            parsed_elec = parse_charger_responses(elec_resp)
                            if parsed_elec:
                                self.latest_data[f"slot{slot}_electric"] = parsed_elec

            await asyncio.sleep(COMMAND_INTERVAL)

        # Input voltage (channel 8 electric)
        elec_cmd = build_command(CMD_ELECTRIC, 1)
        try:
            await self.client.write_gatt_char(CHAR_UUID_AF01, elec_cmd)
            self._log_traffic("📤 WRITE", elec_cmd, "AF01")
        except Exception:
            pass
        elec_resp = await self._wait_for_opcode(RESP_ELECTRIC, RESPONSE_TIMEOUT)
        if elec_resp:
            parsed_elec = parse_charger_responses(elec_resp)
            if parsed_elec:
                self.latest_data["slot1_electric"] = parsed_elec
                received_any = True

        return received_any

    # ------------------------------------------------------------------
    # Public poll entry
    # ------------------------------------------------------------------

    async def poll_data(self):
        if not self.connected or not self.client:
            return False
        try:
            if self.is_a8:
                return await self._poll_a8_mega()
            else:
                return await self._poll_c4_a4()
        except Exception as e:
            err = str(e).lower()
            if "not connected" in err or "unreachable" in err or "disconnected" in err:
                self._log(f"⚠️ Connection dropped: {e}")
                await self.disconnect()
                return False
            self._log(f"⚠️ Poll error: {e}")
            return False

    # ------------------------------------------------------------------
    # A8Task (only after connect and after Apply)
    # ------------------------------------------------------------------

    async def request_a8_task(self):
        """Fetch per-slot A8 task settings (max current, cut-off, cap limit).

        Only called after connect and after Apply – never during the poll loop.
        Both branches (slot exists / slot is new) write the SAME three fields
        with the SAME names, so the subsequent mega-poll can carry them over:

            max_current_mA          <- task_data["max_current_mA"]
            max_output_power_mW     <- task_data["max_output_power_mW"]
            full_charged_volt_mV    <- task_data["voltage_mV"]   (renamed!)
        """
        if not self.connected or not self.client or not self.is_a8:
            return False
        try:
            cmd = bytes([0x12, 0xEC, 0x00])
            await self.client.write_gatt_char(CHAR_UUID_AF01, cmd)
            self._log_traffic("📤 WRITE", cmd, "AF01")
        except Exception as e:
            self._log(f"⚠️ A8Task write failed: {e}")
            return False

        resp = await self._wait_for_opcode(RESP_A8_TASK, RESPONSE_TIMEOUT)
        if not resp:
            self._log("⚠️ A8Task timeout")
            return False

        parsed = parse_a8_task_resp(resp)
        if not parsed:
            return False

        for slot in range(1, self.num_slots + 1):
            task_data = parsed.get(f"slot{slot}")
            if not task_data:
                continue

            key = f"slot{slot}_workstate"
            new_max_current = task_data.get("max_current_mA", 0)
            new_cap_limit = task_data.get("max_output_power_mW", 0)
            new_cutoff_mV = task_data.get("voltage_mV", 0)

            if key in self.latest_data:
                # Slot already known from a mega-poll – merge the three fields
                slot_data = self.latest_data[key]
                slot_data["max_current_mA"] = new_max_current
                slot_data["max_output_power_mW"] = new_cap_limit
                if new_cutoff_mV > 0:
                    slot_data["full_charged_volt_mV"] = new_cutoff_mV
                self.latest_data[key] = slot_data
            else:
                # Slot not yet seen – create a minimal normalised entry.
                # The next mega-poll will merge its own fields on top.
                self.latest_data[key] = {
                    "max_current_mA": new_max_current,
                    "max_output_power_mW": new_cap_limit,
                    "full_charged_volt_mV": new_cutoff_mV,
                }

        self._log("📋 Slot settings received ...")
        return True

    # ------------------------------------------------------------------
    # Alarm tone
    # ------------------------------------------------------------------

    async def get_alarm_tone(self):
        if not self.supports_alarm or not self.connected or not self.client:
            return None
        try:
            await self.client.write_gatt_char(CHAR_UUID_AF01, CMD_ALARM_TONE_REQ)
            self._log_traffic("📤 WRITE", CMD_ALARM_TONE_REQ, "AF01")
        except Exception:
            return None
        resp = await self._wait_for_opcode(CMD_ALARM_TONE_RESP, RESPONSE_TIMEOUT)
        if not resp:
            return None
        state = parse_alarm_tone(resp)
        if state is not None:
            self._alarm_tone_state = state
        return state

    async def set_alarm_tone(self, state):
        if not self.supports_alarm or not self.connected or not self.client:
            return False
        task_type = 1 if state else 0
        cmd = bytes(CMD_ALARM_TONE_TASK_REQ) + bytes([task_type])
        try:
            await self.client.write_gatt_char(CHAR_UUID_AF01, cmd)
            self._log_traffic("📤 WRITE", cmd, "AF01")
        except Exception:
            return False
        resp = await self._wait_for_opcode(CMD_ALARM_TONE_TASK_RESP, RESPONSE_TIMEOUT)
        if resp is None:
            return False
        self._alarm_tone_state = state
        return True

    # ------------------------------------------------------------------
    # Work task (start charging)
    # ------------------------------------------------------------------

    async def set_worktask(self, channel, battery_type, work_current_mA,
                           capacity_limit_mAh, task_type=0, linking_type=0,
                           cells=0, full_charged_volt=0):
        if not self.connected or not self.client:
            return False
        if work_current_mA > self.max_current_mA:
            self._log(f"⚠️ Current {work_current_mA}mA exceeds max {self.max_current_mA}mA")
            return False
        cmd = bytearray()
        cmd.append(0x13)
        cmd.append(CMD_WORKTASKS_REQ)
        cmd.append(channel & 0xFF)
        cmd.append(task_type & 0xFF)
        cmd.append(battery_type & 0xFF)
        cmd.append(linking_type & 0xFF)
        cmd.extend(work_current_mA.to_bytes(4, 'little'))
        cmd.append(cells & 0xFF)
        cmd.extend(full_charged_volt.to_bytes(2, 'little'))
        cmd.extend(capacity_limit_mAh.to_bytes(4, 'little'))
        try:
            await self.client.write_gatt_char(CHAR_UUID_AF01, bytes(cmd))
            self._log_traffic("📤 WRITE", bytes(cmd), "AF01")
            return True
        except Exception as e:
            self._log(f"⚠️ WorkTask error: {e}")
            return False

    # ------------------------------------------------------------------
    # Disconnect
    # ------------------------------------------------------------------

    async def disconnect(self):
        async with self._disconnect_lock:
            self.connected = False
            if self.client:
                try:
                    await self.client.stop_notify(CHAR_UUID_AF01)
                except Exception:
                    pass
                try:
                    await self.client.stop_notify(CHAR_UUID_AF02)
                except Exception:
                    pass
                try:
                    await self.client.disconnect()
                except Exception:
                    pass
                self.client = None
            self._log("⚠️ Disconnected")