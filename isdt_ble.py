#!/usr/bin/env python3
"""
ISDT BLE – Encapsulates all Bluetooth communication with the charger.

Author: Klaus Voigt
"""



import sys
import os
import asyncio
import struct
import uuid


from bleak import BleakClient
from bleak.backends.characteristic import BleakGATTCharacteristic

from isdt_protocol import (
    CHAR_UUID_AF01,
    CHAR_UUID_AF02,
    CMD_HW_INFO_REQ,
    CMD_HW_INFO_RESP,
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
    RESP_WORKSTATE,
    RESP_ELECTRIC,
    RESP_IR,
    RESP_A8_TASK,
    build_command,
    parse_charger_responses,
    parse_hardware_info,
    parse_alarm_tone,
    parse_a8_workstate_mega,
    parse_a8_task_resp,
)

from isdt_models import (
    get_model_config,
    ISDT_MODELS,
)

POST_CONNECT_SETTLE = 1.0
POST_NOTIFICATION_SETUP = 0.5
COMMAND_INTERVAL = 0.1
BIND_TIMEOUT = 3.0
HW_INFO_TIMEOUT = 3.0


class ISDTBLE:
    """
    BLE communication class for the ISDT C4/A4/A8/NP2 Air chargers.
    Model is selected manually in the GUI settings.
    """

    def __init__(self, device_info, log_callback=None, debug=False, config=None):
        """
        Initialize with device information.
        
        Args:
            device_info: Dictionary with device profile
                - mac_address: BLE MAC address
                - selected_model: Model name (e.g., "C4 Air")
                - bind_uuid: Optional persistent bind UUID (hex string)
                - name: Optional device name
                - poll_interval: Optional polling interval
            log_callback: Function for logging messages
            debug: Enable debug output
            config: Full configuration dictionary (for saving updates)
        """
        self.device_info = device_info
        self.address = device_info.get("mac_address", "")
        self.client = None
        self.connected = False
        self.notification_queue = asyncio.Queue()
        self.latest_data = {}
        self.log_callback = log_callback
        self.bind_done = False
        self.debug = debug
        self.hardware_info = None
        self.config = config or {}
        self._disconnect_lock = asyncio.Lock()

        # Get model from device_info
        self._selected_model = device_info.get("selected_model", "C4 Air")

        # Model selection: Manual selection only
        if self._selected_model and self._selected_model in ISDT_MODELS:
            self.model_key = self._selected_model
            self.model_detected = True
            self._log(f"📱 Model: {self.model_key}", force=True)
        else:
            # Fallback - should never happen
            self.model_key = "C4 Air"
            self.model_detected = True
            self._log(f"⚠️ Invalid model '{self._selected_model}' in config, using fallback: {self.model_key}", force=True)

        self.model_config = get_model_config(self.model_key)
        self.num_slots = self.model_config["slots"]
        self.max_current_mA = self.model_config["max_current_mA"]
        self.supported_battery_types = self.model_config["battery_types"]
        self.supports_alarm = self.model_config.get("supports_alarm", True)
        self.is_a8 = (self.model_key == "A8 Air")

        # Per-device poll interval
        self.poll_interval = device_info.get("poll_interval", 2)

        self._poll_timeout_counter = 0
        self._max_timeouts = 3
        self._last_occupied_slots = None
        self._cached_workstate = {}
        self._alarm_tone_state = False
        self._last_bind_response = None
        self._a8_mega_packet_processed = False

    def _log(self, msg, force=False):
        if self.debug or force:
            if self.log_callback:
                self.log_callback(msg)

    def notification_handler(self, sender: BleakGATTCharacteristic, data: bytearray):
        if self.debug:
            self._log(f"📩 Notification: {data.hex()}")
        asyncio.create_task(self.notification_queue.put(bytes(data)))

    async def _send_command_and_wait(self, cmd: bytes, expected_opcode: int, timeout: float = 2.0) -> bytes | None:
        """Send a command and wait for a response with the expected opcode."""
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
        """Initialize the connection: get hardware info, bind, and start data stream."""
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

        bind_uuid_hex = self.device_info.get("bind_uuid", "")
        if bind_uuid_hex:
            try:
                bind_uuid = bytes.fromhex(bind_uuid_hex)
                self._log(f"📋 Using stored UUID: {bind_uuid_hex}", force=True)
            except ValueError:
                bind_uuid = uuid.uuid4().bytes
                self._log("⚠️ Invalid stored UUID – generating new one.", force=True)
                self.device_info["bind_uuid"] = bind_uuid.hex()
                from isdt_config import save_config
                save_config(self.config)
        else:
            bind_uuid = uuid.uuid4().bytes
            self._log("📋 Generated new UUID.", force=True)
            self.device_info["bind_uuid"] = bind_uuid.hex()
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
                    self._last_bind_response = resp
                    break
                elif len(resp) > 0 and resp[0] == CMD_BIND_RESP:
                    self._log("✅ Bind successful!", force=True)
                    self.bind_done = True
                    self._last_bind_response = resp
                    break
            except asyncio.TimeoutError:
                continue

        if not self.bind_done:
            self._log("⚠️ No bind confirmation.", force=True)

        self._log(f"📊 Model: {self.model_key} with {self.num_slots} slots", force=True)
        self._log(f"📊 Max current: {self.max_current_mA} mA", force=True)
        self._log(f"📊 Battery types: {', '.join(self.supported_battery_types)}", force=True)
        if not self.supports_alarm:
            self._log("🔇 Alarm tone not supported by this model", force=True)

        # --- Start data stream ---
        try:
            await self.client.write_gatt_char(CHAR_UUID_AF02, struct.pack("<B", 0xE2))
            await asyncio.sleep(0.5)
        except Exception as e:
            self._log(f"⚠️ Start data stream error: {e}", force=True)

        await asyncio.sleep(0.5)

    async def connect(self, retries=3):
        """
        Establish BLE connection to the charger.
        """
        attempt = 0
        while attempt < retries:
            client = None
            connect_task = None
            try:
                if sys.platform == "win32":
                    client = BleakClient(self.address, timeout=15.0)
                else:
                    client = BleakClient(self.address, timeout=10.0)

                self.client = client
                self._log(f"⏳ Connecting... (attempt {attempt+1}/{retries})", force=True)

                # Create a task for the connection attempt
                async def do_connect():
                    await client.connect()
                
                connect_task = asyncio.create_task(do_connect())
                
                # Wait for connection with timeout
                try:
                    await asyncio.wait_for(asyncio.shield(connect_task), timeout=20.0)
                except asyncio.TimeoutError:
                    self._log(f"⏳ Connect timeout (attempt {attempt+1})", force=True)
                    # Cancel the task if it's still running
                    if not connect_task.done():
                        connect_task.cancel()
                        try:
                            await connect_task
                        except asyncio.CancelledError:
                            pass
                    raise  # Re-raise to be caught by outer except

                self.connected = True
                self._log("✅ BLE connected.", force=True)

                # Display MTU size
                try:
                    self._log(f"📡 MTU: {client.mtu_size}", force=True)
                except Exception:
                    pass

                await asyncio.sleep(POST_CONNECT_SETTLE)

                await client.start_notify(CHAR_UUID_AF01, self.notification_handler)
                await client.start_notify(CHAR_UUID_AF02, self.notification_handler)
                self._log("✅ Notification handlers registered.", force=True)

                # A8 Air on Windows needs extra settle time
                if sys.platform == "win32" and self.is_a8:
                    self._log("🔄 Windows: Extra settle time for A8 Air...", force=True)
                    await asyncio.sleep(1.5)
                else:
                    await asyncio.sleep(POST_NOTIFICATION_SETUP)

                await self._initialize()
                self._log("✅ Initialization complete.", force=True)

                if self.supports_alarm:
                    self._alarm_tone_state = await self.get_alarm_tone() or False
                else:
                    self._alarm_tone_state = False

                return True

            except asyncio.TimeoutError:
                self._log(f"⏳ Connection timeout (attempt {attempt+1}/{retries})", force=True)
                if connect_task and not connect_task.done():
                    connect_task.cancel()
                    try:
                        await connect_task
                    except asyncio.CancelledError:
                        pass
                if client is not None:
                    try:
                        await asyncio.wait_for(client.disconnect(), timeout=2.0)
                    except Exception:
                        pass
                self.client = None
                self.connected = False
                attempt += 1
                if attempt < retries:
                    wait = 4.0 if sys.platform == "win32" else 2.0
                    self._log(f"⏳ Waiting {wait:.0f}s before retry...", force=True)
                    await asyncio.sleep(wait)

            except Exception as e:
                error_msg = str(e).strip()
                if not error_msg:
                    error_msg = f"{type(e).__name__} (empty message) {e!r}"
                self._log(f"⚠️ Connection error: {error_msg}", force=True)

                if connect_task and not connect_task.done():
                    connect_task.cancel()
                    try:
                        await connect_task
                    except asyncio.CancelledError:
                        pass

                if client is not None:
                    try:
                        await asyncio.wait_for(client.stop_notify(CHAR_UUID_AF01), timeout=1.0)
                    except Exception:
                        pass
                    try:
                        await asyncio.wait_for(client.stop_notify(CHAR_UUID_AF02), timeout=1.0)
                    except Exception:
                        pass
                    try:
                        await asyncio.wait_for(client.disconnect(), timeout=2.0)
                    except Exception:
                        pass

                self.client = None
                self.connected = False
                attempt += 1
                if attempt < retries:
                    wait = 4.0 if sys.platform == "win32" else 2.0
                    self._log(f"⏳ Waiting {wait:.0f}s before retry...", force=True)
                    await asyncio.sleep(wait)

        self._log("⚠️ All connection attempts failed.", force=True)
        if sys.platform == "win32":
            self._log("💡 Tip: Power-cycle the charger (unplug 15s), then try again.", force=True)
            self._log("💡 Tip: Close the ISD Link app on your smartphone.", force=True)
            self._log("💡 Tip: Update your Bluetooth driver (especially Intel adapters).", force=True)
        else:
            self._log("💡 Tip: Please close the ISD Link app on your smartphone.", force=True)
            self._log("💡 Tip: Make sure the charger is powered on.", force=True)
        return False

    async def _query_with_filter(self, cmd: bytes, slot: int, expected_opcode: int, timeout: float = 3.0) -> dict | None:
        """
        Send a command and wait for a response with the correct opcode AND channel.
        Wrong-slot responses are put back into the queue silently.
        """
        if not self.connected or not self.client:
            return None

        # Additional check: client must be valid
        if self.client is None:
            return None

        # A8 Air: Skip IR queries (mega-packet handles it)
        if self.is_a8 and expected_opcode == RESP_IR:
            return None

        # Windows BLE stack needs more time
        if sys.platform == "win32":
            timeout = 5.0  # Windows needs longer
        else:
            timeout = 3.0  # Linux/macOS are faster

        try:
            await self.client.write_gatt_char(CHAR_UUID_AF01, cmd)
        except Exception:
            return None

        start = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() - start < timeout:
            try:
                resp = await asyncio.wait_for(self.notification_queue.get(), timeout=0.5)
                if len(resp) < 3:
                    continue

                # Check opcode
                if resp[1] != expected_opcode:
                    # Unrelated response → put back silently
                    await self.notification_queue.put(resp)
                    continue

                # Special case: A8 Air Electric response with channel 8 (all slots)
                if self.is_a8 and expected_opcode == RESP_ELECTRIC and resp[2] == 8:
                    parsed = parse_charger_responses(resp)
                    if parsed:
                        return parsed
                    continue

                # Check channel (slot - 1)
                if resp[2] != (slot - 1):
                    # Wrong slot → put back silently
                    await self.notification_queue.put(resp)
                    continue

                parsed = parse_charger_responses(resp)
                if parsed:
                    return parsed

            except asyncio.TimeoutError:
                continue

        # Only log timeout if we didn't get any response at all
        self._log(f"Timeout waiting for opcode 0x{expected_opcode:02X} slot {slot}", force=True)
        return None

    async def _query_workstate(self, slot: int) -> dict | None:
        """Query work state for a specific slot."""
        # A8 Air: WorkState is handled by mega-packet
        if self.is_a8:
            return None
        cmd = build_command(CMD_WORKSTATE, slot)
        return await self._query_with_filter(cmd, slot, RESP_WORKSTATE)

    async def _query_electric(self, slot: int) -> dict | None:
        """Query electric data (voltage, current) for a specific slot with channel filtering."""
        # A8 Air: Electric is supported (9-byte response with channel 8)
        cmd = build_command(CMD_ELECTRIC, slot)
        return await self._query_with_filter(cmd, slot, RESP_ELECTRIC)

    async def _query_ir(self, slot: int) -> dict | None:
        """Query internal resistance for a specific slot with channel filtering."""
        # A8 Air: IR is handled by mega-packet
        if self.is_a8:
            return None
        cmd = build_command(CMD_IR, slot)
        return await self._query_with_filter(cmd, slot, RESP_IR)

    async def _poll_a8_mega(self) -> bool:
        """
        A8 Air: Poll the mega-packet and A8TaskResp for all slots.
        Mega-packet: WorkState data (voltage, current, IR, status)
        A8TaskResp (0xED): Max Current, Cap Limit, Cut-off
        """
        if not self.connected or not self.client:
            return False

        received_any = False
        mega_timeout = 8.0 if sys.platform == "win32" else 4.0
        task_timeout = 6.0 if sys.platform == "win32" else 3.0

        # Stale notifications verwerfen (wichtig unter Windows)
        while not self.notification_queue.empty():
            try:
                self.notification_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

        # 1. Query WorkState mega-packet (0x13 0xE6)
        try:
            cmd = build_command(CMD_WORKSTATE, 1)
            await self.client.write_gatt_char(CHAR_UUID_AF01, cmd)

            start = asyncio.get_event_loop().time()
            while asyncio.get_event_loop().time() - start < mega_timeout:
                try:
                    resp = await asyncio.wait_for(self.notification_queue.get(), timeout=0.5)
                    if len(resp) < 3:
                        continue

                    if resp[1] == RESP_WORKSTATE and len(resp) >= 200:
                        self._log(f"📦 A8 mega-packet: {len(resp)} bytes", force=False)
                        parsed = parse_a8_workstate_mega(resp)
                        if parsed:
                            for slot in range(1, self.num_slots + 1):
                                slot_data = parsed.get(f"slot{slot}")
                                if slot_data:
                                    self.latest_data[f"slot{slot}_workstate"] = slot_data
                                    ir_data = {
                                        "channel": slot - 1,
                                        "ir_values_mohm": [slot_data.get("ir_mohm", 0)],
                                        "ir_total_mohm": slot_data.get("ir_mohm", 0),
                                    }
                                    self.latest_data[f"slot{slot}_ir"] = ir_data
                            received_any = True
                            self._a8_mega_packet_processed = True
                            break
                    else:
                        if resp[1] == RESP_WORKSTATE:
                            self._log(f"⚠️ A8 WorkState too short: {len(resp)} bytes", force=True)
                        await self.notification_queue.put(resp)
                except asyncio.TimeoutError:
                    continue

            if not received_any:
                self._log("⚠️ A8 Air: No mega-packet received", force=True)

        except Exception as e:
            err = str(e).lower()
            if "unreachable" in err or "not connected" in err or "disconnected" in err:
                self._log(f"⚠️ Connection lost (mega): {e}", force=True)
                raise
            self._log(f"⚠️ A8 Air mega-packet error: {e}", force=True)

        await asyncio.sleep(0.3)

        # 2. Query Electric for input voltage
        try:
            elec1 = await self._query_electric(1)
            if elec1:
                self.latest_data["slot1_electric"] = elec1
                received_any = True
        except Exception as e:
            err = str(e).lower()
            if "unreachable" in err or "not connected" in err:
                self._log(f"⚠️ Connection lost (electric): {e}", force=True)
                raise
            self._log(f"⚠️ A8 Electric error: {e}", force=True)

        await asyncio.sleep(0.3)

        # 3. Query A8TaskReq (0x12 0xEC)
        try:
            cmd = bytes([0x12, 0xEC, 0x00])
            await self.client.write_gatt_char(CHAR_UUID_AF01, cmd)

            start = asyncio.get_event_loop().time()
            while asyncio.get_event_loop().time() - start < task_timeout:
                try:
                    resp = await asyncio.wait_for(self.notification_queue.get(), timeout=0.5)
                    if len(resp) < 3:
                        continue

                    if resp[1] == RESP_A8_TASK:
                        parsed = parse_a8_task_resp(resp)
                        if parsed:
                            for slot in range(1, self.num_slots + 1):
                                task_data = parsed.get(f"slot{slot}")
                                if task_data:
                                    key = f"slot{slot}_workstate"
                                    if key in self.latest_data:
                                        slot_data = self.latest_data[key]
                                        slot_data["max_current_mA"] = task_data.get("max_current_mA", 0)
                                        slot_data["max_output_power_mW"] = task_data.get("max_output_power_mW", 0)
                                        if task_data.get("voltage_mV", 0) > 0:
                                            slot_data["full_charged_volt_mV"] = task_data.get("voltage_mV", 0)
                                        self.latest_data[key] = slot_data
                                    else:
                                        self.latest_data[f"slot{slot}_workstate"] = task_data
                            received_any = True
                            break
                    else:
                        await self.notification_queue.put(resp)
                except asyncio.TimeoutError:
                    continue

            if not received_any:
                self._log("⚠️ A8 Air: No A8TaskResp received", force=True)

        except Exception as e:
            err = str(e).lower()
            if "unreachable" in err or "not connected" in err or "disconnected" in err:
                self._log(f"⚠️ Connection lost (A8Task): {e}", force=True)
                raise
            self._log(f"⚠️ A8 Air A8TaskReq error: {e}", force=True)

        return received_any

    async def poll_data(self) -> bool:
        """
        Poll all data from the charger.
        
        Returns:
            bool: True if data was received, False if connection is lost.
        """
        if not self.connected or not self.client:
            return False

        # A8 Air: Use mega-packet polling
        # A8 Air: Use mega-packet polling
        if self.is_a8:
            try:
                received_any = await self._poll_a8_mega()
            except Exception as e:
                err = str(e).lower()
                if "unreachable" in err or "not connected" in err or "disconnected" in err:
                    self._log(
                        "ℹ️ Connection dropped by charger – will reconnect.",
                        force=True
                    )
                    await self.disconnect()
                    return False
                self._log(f"⚠️ A8 poll error: {e}", force=True)
                received_any = False

            if received_any:
                self._poll_timeout_counter = 0
            else:
                self._poll_timeout_counter += 1
                max_timeouts = 5 if sys.platform == "win32" else 3
                self._log(
                    f"⏳ No response (timeout counter: {self._poll_timeout_counter}/{max_timeouts})",
                    force=True
                )
                if self._poll_timeout_counter >= max_timeouts:
                    self._log("⚠️ Device no longer responding – disconnecting.", force=True)
                    await self.disconnect()
                    return False
            return True

        # Standard polling for C4/A4/NP2
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
                    if parsed and parsed.get("channel") == slot - 1:
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
        """Get the current alarm tone state. Returns None if not supported."""
        if not self.supports_alarm:
            self._log("🔇 Alarm tone not supported by this model", force=True)
            return False
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
        """Set the alarm tone on/off. Returns False if not supported."""
        if not self.supports_alarm:
            self._log("🔇 Alarm tone not supported by this model", force=True)
            return False
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
        """
        Set charging parameters for a specific channel.
        
        Args:
            channel: Slot number (0-based)
            battery_type: Battery type code (see BATTERY_TYPE_STR_TO_INT)
            work_current_mA: Charging current in mA
            capacity_limit_mAh: Capacity limit in mAh (0 = unlimited)
            task_type: Task type (0 = normal)
            linking_type: Linking type (0 = none)
            cells: Number of cells (0 = auto)
            full_charged_volt: Cut-off voltage in mV
            
        Returns:
            bool: True if successful
        """
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
            # TODO: Wait for confirmation response if protocol supports it
            return True
        except Exception as e:
            self._log(f"Error sending WorkTasksReq: {e}", force=True)
            return False

    async def disconnect(self):
        """Disconnect from the charger with proper cleanup."""
        async with self._disconnect_lock:
            self.connected = False
            if self.client:
                try:
                    await self.client.stop_notify(CHAR_UUID_AF01)
                    await self.client.stop_notify(CHAR_UUID_AF02)
                except Exception:
                    pass
                try:
                    await self.client.disconnect()
                except Exception:
                    pass
                self.client = None  # Important: Set client to None
            self._poll_timeout_counter = 0
            self._last_occupied_slots = None
            self._log("⚠️ Disconnected", force=True)