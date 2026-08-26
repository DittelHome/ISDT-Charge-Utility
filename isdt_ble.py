#!/usr/bin/env python3
"""
ISDT BLE – Encapsulates all Bluetooth communication with the charger.

Author: Klaus Voigt
"""

import asyncio
import struct
import uuid
from bleak import BleakClient
from bleak.backends.characteristic import BleakGATTCharacteristic

from isdt_protocol import (
    CHAR_UUID_AF01,
    CHAR_UUID_AF02,
    CMD_HW_INFO_REQ,
    CMD_BIND_REQ,
    CMD_BIND_RESP,
    CMD_WORKSTATE,
    CMD_ELECTRIC,
    CMD_IR,
    CMD_WORKTASKS_REQ,
    CMD_ALARM_TONE_REQ,
    CMD_ALARM_TONE_RESP,
    CMD_ALARM_TONE_TASK_REQ,
    CMD_ALARM_TONE_TASK_RESP,
    build_command,
    parse_charger_responses,
    parse_hardware_info,
    parse_alarm_tone,
)

from isdt_models import (
    get_model_config,
    detect_model_from_device_name,
    detect_model_from_bind_response,
)


POST_CONNECT_SETTLE = 1.0
POST_NOTIFICATION_SETUP = 0.5
COMMAND_INTERVAL = 0.1
BIND_TIMEOUT = 3.0
HW_INFO_TIMEOUT = 3.0


class ISDTBLE:
    """
    BLE communication class for the ISDT C4/A4/A8/NP2 Air chargers.
    """

    def __init__(self, address, log_callback=None, debug=False, config=None, device_name=None):
        self.address = address
        self.client = None
        self.connected = False
        self.notification_queue = asyncio.Queue()
        self.latest_data = {}
        self.log_callback = log_callback
        self.bind_done = False
        self.debug = debug
        self.hardware_info = None
        self.config = config or {}
        self._device_name = device_name or ""

        self.model_key = self._detect_model_from_device_name(self._device_name)
        self.model_config = get_model_config(self.model_key)
        self.num_slots = self.model_config["slots"]
        self.max_current_mA = self.model_config["max_current_mA"]
        self.supported_battery_types = self.model_config["battery_types"]
        self.model_detected = bool(self._device_name)

        self._poll_timeout_counter = 0
        self._max_timeouts = 3
        self._last_occupied_slots = None
        self._cached_workstate = {}
        self._alarm_tone_state = False

        if self.model_detected:
            self._log(f"📱 Model detected from device name: {self.model_key}", force=True)
        else:
            self._log("⚠️ No device name provided, will detect later", force=True)

    def _log(self, msg, force=False):
        if self.debug or force:
            if self.log_callback:
                self.log_callback(msg)

    def _detect_model_from_device_name(self, device_name: str) -> str:
        if not device_name:
            return "C4 Air"
        detected = detect_model_from_device_name(device_name)
        self._log(f"🔍 Device name '{device_name}' → Model: {detected}", force=True)
        return detected

    def notification_handler(self, sender: BleakGATTCharacteristic, data: bytearray):
        if self.debug:
            self._log(f"📩 Notification: {data.hex()}")
        asyncio.create_task(self.notification_queue.put(bytes(data)))

    async def _send_command_and_wait(self, cmd: bytes, expected_opcode: int, timeout: float = 2.0) -> bytes | None:
        if not self.connected or not self.client:
            self._log("Not connected", force=True)
            return None
        try:
            await self.client.write_gatt_char(CHAR_UUID_AF01, cmd)
        except Exception as e:
            self._log(f"Write error: {e}", force=True)
            return None
        start = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() - start < timeout:
            try:
                resp = await asyncio.wait_for(self.notification_queue.get(), timeout=0.5)
                if len(resp) < 2:
                    continue
                if resp[1] == expected_opcode:
                    return resp
            except asyncio.TimeoutError:
                continue
        self._log(f"Timeout waiting for opcode 0x{expected_opcode:02X}", force=True)
        return None

    async def _initialize(self):
        self._log("✅ Initializing...", force=True)

        # --- Hardware info ---
        try:
            await self.client.write_gatt_char(CHAR_UUID_AF02, struct.pack("<B", CMD_HW_INFO_REQ))
            try:
                resp = await asyncio.wait_for(self.notification_queue.get(), timeout=HW_INFO_TIMEOUT)
                self.hardware_info = resp
                parsed = parse_hardware_info(resp)
                if parsed:
                    self._log(
                        f"📋 FW: v{parsed['fw_version']}, HW: v{parsed['hw_version']}, SN: {parsed['serial']}",
                        force=True
                    )
                else:
                    self._log(f"📋 Hardware-Info (raw): {resp.hex()}", force=True)
            except asyncio.TimeoutError:
                self._log("⚠️ HW-Info timeout – continuing", force=True)
        except Exception as e:
            self._log(f"HW-Info error: {e}", force=True)

        # --- Bind handshake ---
        self._log("✅ Bind handshake...", force=True)

        bind_uuid_hex = self.config.get("bind_uuid", "")
        if bind_uuid_hex:
            try:
                bind_uuid = bytes.fromhex(bind_uuid_hex)
                self._log(f"📋 Using stored UUID: {bind_uuid_hex}", force=True)
            except ValueError:
                bind_uuid = uuid.uuid4().bytes
                self._log("⚠️ Invalid stored UUID – generating new one.", force=True)
                self.config["bind_uuid"] = bind_uuid.hex()
                from isdt_config import save_config
                save_config(self.config)
                self._log(f"📋 New UUID saved to config.", force=True)
        else:
            bind_uuid = uuid.uuid4().bytes
            self._log("📋 Generated new UUID.", force=True)
            self.config["bind_uuid"] = bind_uuid.hex()
            from isdt_config import save_config
            save_config(self.config)
            self._log(f"📋 New UUID saved to config.", force=True)

        cmd = struct.pack("<B", CMD_BIND_REQ) + bind_uuid + b"\x00\x00"
        await self.client.write_gatt_char(CHAR_UUID_AF02, cmd)
        if self.debug:
            self._log(f"AF02 write (Bind): {cmd.hex()}")

        start = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() - start < BIND_TIMEOUT:
            try:
                resp = await asyncio.wait_for(self.notification_queue.get(), timeout=0.5)
                if self.debug:
                    self._log(f"Intermediate response: {resp.hex()}")
                if len(resp) > 1 and resp[1] == CMD_BIND_RESP:
                    self._log("✅ Bind successful!", force=True)
                    self.bind_done = True
                    if not self.model_detected:
                        model_key = detect_model_from_bind_response(resp)
                        if model_key != self.model_key:
                            self.model_key = model_key
                            self.model_config = get_model_config(model_key)
                            self.num_slots = self.model_config["slots"]
                            self.max_current_mA = self.model_config["max_current_mA"]
                            self.supported_battery_types = self.model_config["battery_types"]
                            self.model_detected = True
                            self._log(f"📱 Model detected from bind response: {self.model_key}", force=True)
                    break
                elif len(resp) > 0 and resp[0] == CMD_BIND_RESP:
                    self._log("✅ Bind successful!", force=True)
                    self.bind_done = True
                    if not self.model_detected:
                        model_key = detect_model_from_bind_response(resp)
                        if model_key != self.model_key:
                            self.model_key = model_key
                            self.model_config = get_model_config(model_key)
                            self.num_slots = self.model_config["slots"]
                            self.max_current_mA = self.model_config["max_current_mA"]
                            self.supported_battery_types = self.model_config["battery_types"]
                            self.model_detected = True
                            self._log(f"📱 Model detected from bind response: {self.model_key}", force=True)
                    break
            except asyncio.TimeoutError:
                continue

        if not self.bind_done:
            self._log("⚠️ No bind confirmation.", force=True)

        if self.model_detected:
            self._log(f"📊 Model: {self.model_key} with {self.num_slots} slots", force=True)
            self._log(f"📊 Max current: {self.max_current_mA} mA", force=True)
            self._log(f"📊 Battery types: {', '.join(self.supported_battery_types)}", force=True)
        else:
            self._log("⚠️ Could not detect model, using default (C4 Air)", force=True)

        # --- Start data stream ---
        try:
            await self.client.write_gatt_char(CHAR_UUID_AF02, struct.pack("<B", 0xE2))
            await asyncio.sleep(0.5)
        except Exception as e:
            self._log(f"⚠️ Start data stream error: {e}", force=True)

        await asyncio.sleep(0.5)

    async def connect(self, retries=2):
        """
        Establish BLE connection to the charger.
        """
        attempt = 0
        while attempt <= retries:
            try:
                self.client = BleakClient(self.address)
                self._log(f"⏳ Connecting... (attempt {attempt+1}/{retries+1})", force=True)
                await self.client.connect()
                self.connected = True
                self._log("✅ BLE connected.", force=True)

                await asyncio.sleep(POST_CONNECT_SETTLE)

                await self.client.start_notify(CHAR_UUID_AF01, self.notification_handler)
                await self.client.start_notify(CHAR_UUID_AF02, self.notification_handler)
                self._log("✅ Notification handlers registered.", force=True)

                await asyncio.sleep(POST_NOTIFICATION_SETUP)

                await self._initialize()
                self._log("✅ Initialization complete.", force=True)

                # Get initial alarm tone state
                self._alarm_tone_state = await self.get_alarm_tone() or False

                return True


            
            except Exception as e:
                error_msg = str(e)
                if error_msg == "":
                    error_msg = "Unknown BLE error (empty error message)"
                self._log(f"⚠️ Connection error: {error_msg}", force=True)
                if "InProgress" in error_msg or "already" in error_msg.lower():
                    self._log("⏳ Waiting 2s and retrying...", force=True)
                    await asyncio.sleep(2)
            attempt += 1
            
            

        return False

    async def _query_workstate(self, slot: int) -> dict | None:
        if not self.connected or not self.client:
            return None
        cmd = build_command(CMD_WORKSTATE, slot)
        try:
            await self.client.write_gatt_char(CHAR_UUID_AF01, cmd)
        except Exception:
            return None
        start = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() - start < 3.0:
            try:
                resp = await asyncio.wait_for(self.notification_queue.get(), timeout=0.5)
                parsed = parse_charger_responses(resp)
                if parsed:
                    return parsed
                else:
                    if len(resp) > 0 and resp[0] in (CMD_BIND_RESP, 0x00):
                        continue
            except asyncio.TimeoutError:
                continue
        return None

    async def _query_electric(self, slot: int) -> dict | None:
        if not self.connected or not self.client:
            return None
        cmd = build_command(CMD_ELECTRIC, slot)
        try:
            await self.client.write_gatt_char(CHAR_UUID_AF01, cmd)
        except Exception:
            return None
        start = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() - start < 3.0:
            try:
                resp = await asyncio.wait_for(self.notification_queue.get(), timeout=0.5)
                parsed = parse_charger_responses(resp)
                if parsed:
                    return parsed
                else:
                    if len(resp) > 0 and resp[0] in (CMD_BIND_RESP, 0x00):
                        continue
            except asyncio.TimeoutError:
                continue
        return None

    async def _query_ir(self, slot: int) -> dict | None:
        if not self.connected or not self.client:
            return None
        cmd = build_command(CMD_IR, slot)
        try:
            await self.client.write_gatt_char(CHAR_UUID_AF01, cmd)
        except Exception:
            return None
        start = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() - start < 3.0:
            try:
                resp = await asyncio.wait_for(self.notification_queue.get(), timeout=0.5)
                parsed = parse_charger_responses(resp)
                if parsed:
                    return parsed
                else:
                    if len(resp) > 0 and resp[0] in (CMD_BIND_RESP, 0x00):
                        continue
            except asyncio.TimeoutError:
                continue
        return None

    async def poll_data(self) -> bool:
        if not self.connected or not self.client:
            return False

        received_any = False
        occupied_slots = []

        # 1. WorkState for all slots
        for slot in range(1, self.num_slots + 1):
            work = await self._query_workstate(slot)
            if work:
                key = f"slot{slot}_workstate"
                self.latest_data[key] = work
                received_any = True
                status = work.get("status", 0)
                if status not in (0, 5):
                    occupied_slots.append(slot)

                old = self._cached_workstate.get(slot)
                if old != work:
                    self._cached_workstate[slot] = work
                    if status not in (0, 5):
                        elec = await self._query_electric(slot)
                        if elec:
                            self.latest_data[f"slot{slot}_electric"] = elec
                            received_any = True
                        ir = await self._query_ir(slot)
                        if ir:
                            self.latest_data[f"slot{slot}_ir"] = ir
                            received_any = True
            else:
                # Fallback: direct read if notification didn't arrive
                try:
                    direct = await self.client.read_gatt_char(CHAR_UUID_AF01)
                    parsed = parse_charger_responses(direct)
                    if parsed:
                        key = f"slot{slot}_workstate"
                        self.latest_data[key] = parsed
                        received_any = True
                        status = parsed.get("status", 0)
                        if status not in (0, 5):
                            occupied_slots.append(slot)
                except Exception:
                    pass
            await asyncio.sleep(COMMAND_INTERVAL)

        # 2. Electric for Slot 1 (input voltage) – always query
        elec1 = await self._query_electric(1)
        if elec1:
            self.latest_data["slot1_electric"] = elec1
            received_any = True
        await asyncio.sleep(COMMAND_INTERVAL)

        # 3. Update occupied slots for adaptive pause
        self._last_occupied_slots = occupied_slots if occupied_slots else None

        # 4. Timeout detection
        if received_any:
            self._poll_timeout_counter = 0
        else:
            self._poll_timeout_counter += 1
            self._log(f"⏳ No response (timeout counter: {self._poll_timeout_counter}/{self._max_timeouts})", force=True)
            if self._poll_timeout_counter >= self._max_timeouts:
                self._log("⚠️ Device no longer responding – disconnecting.", force=True)
                await self.disconnect()
                return False

        return True

    async def get_alarm_tone(self) -> bool | None:
        if not self.connected or not self.client:
            self._log("Not connected", force=True)
            return None
        resp = await self._send_command_and_wait(CMD_ALARM_TONE_REQ, CMD_ALARM_TONE_RESP)
        if resp is None:
            return None
        state = parse_alarm_tone(resp)
        if state is not None:
            self._alarm_tone_state = state
        return state

    async def set_alarm_tone(self, state: bool) -> bool:
        if not self.connected or not self.client:
            self._log("Not connected", force=True)
            return False
        task_type = 1 if state else 0
        cmd = bytes(CMD_ALARM_TONE_TASK_REQ) + bytes([task_type])
        resp = await self._send_command_and_wait(cmd, CMD_ALARM_TONE_TASK_RESP)
        if resp is None:
            return False
        self._alarm_tone_state = state
        return True

    async def set_worktask(self, channel: int, battery_type: int,
                           work_current_mA: int, capacity_limit_mAh: int,
                           task_type: int = 0, linking_type: int = 0,
                           cells: int = 0, full_charged_volt: int = 0) -> bool:
        if not self.connected or not self.client:
            self._log("Not connected", force=True)
            return False

        if work_current_mA > self.max_current_mA:
            self._log(f"⚠️ Current {work_current_mA}mA exceeds model maximum {self.max_current_mA}mA", force=True)
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
            return True
        except Exception as e:
            self._log(f"Error sending WorkTasksReq: {e}", force=True)
            return False

    async def disconnect(self):
        if self.client:
            try:
                await self.client.stop_notify(CHAR_UUID_AF01)
                await self.client.stop_notify(CHAR_UUID_AF02)
            except Exception:
                pass
            await self.client.disconnect()
            self.connected = False
            self._poll_timeout_counter = 0
            self._last_occupied_slots = None
            self._log("⚠️ Disconnected", force=True)