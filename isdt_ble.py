#!/usr/bin/env python3
"""
ISDT BLE – Encapsulates all Bluetooth communication with the charger.

This module handles the BLE connection, data polling, and control commands
for ISDT C4/A4/A8/NP2 Air chargers. It automatically detects the connected
model and adapts the communication parameters accordingly.

Key features:
- Auto-detection of connected model via device name or bind response
- Dynamic slot count based on model
- Current limiting based on model specifications
- Battery type validation based on model support
- Polling with adaptive intervals

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


# ------------------------------------------------------------------
# Timing Constants
# ------------------------------------------------------------------
POST_CONNECT_SETTLE = 1.0      # Time to wait after connection before proceeding
POST_NOTIFICATION_SETUP = 0.5  # Time to wait after setting up notifications
COMMAND_INTERVAL = 0.1         # Delay between commands to avoid overwhelming the charger
BIND_TIMEOUT = 3.0             # Maximum time to wait for bind confirmation
HW_INFO_TIMEOUT = 3.0          # Maximum time to wait for hardware info


class ISDTBLE:
    """
    BLE communication class for ISDT C4/A4/A8 Air chargers.
    
    This class handles all BLE communication with the charger including:
    - Establishing and managing the BLE connection
    - Auto-detecting the connected model
    - Polling charging data (WorkState, Electric, IR)
    - Setting charging parameters (WorkTasksReq)
    - Controlling the alarm tone
    
    The class automatically adapts to the detected model, adjusting the
    number of slots, maximum current, and supported battery types.
    """

    def __init__(self, address, log_callback=None, debug=False, config=None, device_name=None):
        """
        Initialize the BLE device.
        
        Args:
            address: MAC address of the charger (e.g., "50:54:7B:63:4B:A3")
            log_callback: Function to call for log messages (optional)
            debug: Enable debug logging (default: False)
            config: Configuration dictionary (for persistent bind UUID)
            device_name: BLE device name from scan (used for model detection)
        """
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
        self._bind_response = None
        self._device_name = device_name or ""

        # ------------------------------------------------------------------
        # Model Detection (Primary: Device Name)
        # ------------------------------------------------------------------
        # The model is detected from the BLE device name during scan.
        # This determines the number of slots, max current, and battery types.
        self.model_key = self._detect_model_from_device_name(self._device_name)
        self.model_config = get_model_config(self.model_key)
        self.num_slots = self.model_config["slots"]
        self.max_current_mA = self.model_config["max_current_mA"]
        self.supported_battery_types = self.model_config["battery_types"]
        self.model_detected = bool(self._device_name)

        # Polling state
        self._poll_timeout_counter = 0
        self._max_timeouts = 3
        self._last_occupied_slots = None
        self._cached_workstate = {}
        self._alarm_tone_state = False

        # Log the detected model
        if self.model_detected:
            self._log(f"📱 Model detected from device name: {self.model_key}", force=True)
        else:
            self._log(f"⚠️ No device name provided, will detect later", force=True)

    def _log(self, msg, force=False):
        """Log a message if debug is enabled or force=True."""
        if self.debug or force:
            if self.log_callback:
                self.log_callback(msg)

    def _detect_model_from_device_name(self, device_name: str) -> str:
        """Detect model from BLE device name."""
        if not device_name:
            return "C4 Air"
        detected = detect_model_from_device_name(device_name)
        self._log(f"🔍 Device name '{device_name}' → Model: {detected}", force=True)
        return detected

    def notification_handler(self, sender: BleakGATTCharacteristic, data: bytearray):
        """
        Handle incoming BLE notifications.
        
        Notifications are received on both AF01 and AF02 characteristics.
        They are placed in a queue for processing by the polling loop.
        """
        if self.debug:
            self._log(f"📩 Notification: {data.hex()}")
        asyncio.create_task(self.notification_queue.put(bytes(data)))

    async def _send_command_and_wait(self, cmd: bytes, expected_opcode: int, timeout: float = 2.0) -> bytes | None:
        """
        Send a command on AF01 and wait for a notification with the expected opcode.
        
        Args:
            cmd: Bytes to write to the characteristic
            expected_opcode: The opcode to wait for in the response
            timeout: Maximum wait time in seconds
            
        Returns:
            The full notification bytes, or None on timeout
        """
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
        """
        Initialize the connection after GATT connection is established.
        
        This performs the following steps in order:
        1. Query hardware info (firmware version, hardware version, serial)
        2. Bind handshake (establishes a secure session)
        3. Model detection (from bind response if not already detected)
        4. Start data streaming
        """
        self._log("✅ Initializing...", force=True)

        # ------------------------------------------------------------------
        # Step 1: Hardware Info Query
        # ------------------------------------------------------------------
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

        # ------------------------------------------------------------------
        # Step 2: Bind Handshake
        # ------------------------------------------------------------------
        self._log("✅ Bind handshake...", force=True)

        # Generate or load persistent bind UUID
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

        # Send bind request
        cmd = struct.pack("<B", CMD_BIND_REQ) + bind_uuid + b"\x00\x00"
        await self.client.write_gatt_char(CHAR_UUID_AF02, cmd)
        if self.debug:
            self._log(f"AF02 write (Bind): {cmd.hex()}")

        # Wait for bind confirmation
        start = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() - start < BIND_TIMEOUT:
            try:
                resp = await asyncio.wait_for(self.notification_queue.get(), timeout=0.5)
                if self.debug:
                    self._log(f"Intermediate response: {resp.hex()}")

                # Check for bind response opcode
                if len(resp) > 1 and resp[1] == CMD_BIND_RESP:
                    self._log("✅ Bind successful!", force=True)
                    self.bind_done = True
                    self._bind_response = resp

                    # If model not yet detected, try from bind response
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
                    self._bind_response = resp

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

        # ------------------------------------------------------------------
        # Step 3: Log Model Information
        # ------------------------------------------------------------------
        if self.model_detected:
            self._log(f"📊 Model: {self.model_key} with {self.num_slots} slots", force=True)
            self._log(f"📊 Max current: {self.max_current_mA} mA", force=True)
            self._log(f"📊 Battery types: {', '.join(self.supported_battery_types)}", force=True)
        else:
            self._log("⚠️ Could not detect model, using default (C4 Air)", force=True)

        # ------------------------------------------------------------------
        # Step 4: Start Data Stream
        # ------------------------------------------------------------------
        try:
            await self.client.write_gatt_char(CHAR_UUID_AF02, struct.pack("<B", 0xE2))
            await asyncio.sleep(0.5)
        except Exception as e:
            self._log(f"⚠️ Start data stream error: {e}", force=True)

        await asyncio.sleep(0.5)

    async def connect(self, retries=2):
        """
        Establish BLE connection to the charger.
        
        Args:
            retries: Number of connection attempts
            
        Returns:
            True if connection successful, False otherwise
        """
        attempt = 0
        while attempt <= retries:
            try:
                self.client = BleakClient(self.address)
                self._log(f"⏳ Connecting... (attempt {attempt+1}/{retries+1})", force=True)
                await self.client.connect()
                self.connected = True
                self._log("✅ BLE connected.", force=True)

                # Wait for the connection to stabilize
                await asyncio.sleep(POST_CONNECT_SETTLE)

                # Register notification handlers
                await self.client.start_notify(CHAR_UUID_AF01, self.notification_handler)
                await self.client.start_notify(CHAR_UUID_AF02, self.notification_handler)
                self._log("✅ Notification handlers registered.", force=True)

                await asyncio.sleep(POST_NOTIFICATION_SETUP)

                # Initialize the connection
                await self._initialize()
                self._log("✅ Initialization complete.", force=True)

                # Get initial alarm tone state
                self._alarm_tone_state = await self.get_alarm_tone() or False

                return True

            except Exception as e:
                error_msg = str(e)
                self._log(f"⚠️ Connection error: {error_msg}", force=True)
                if "InProgress" in error_msg:
                    self._log("⏳ Waiting 2s and retrying...", force=True)
                    await asyncio.sleep(2)
                attempt += 1

        return False

    # ------------------------------------------------------------------
    # Data Query Methods
    # ------------------------------------------------------------------

    async def _query_workstate(self, slot: int) -> dict | None:
        """
        Query WorkState for a slot.
        
        WorkState contains status, capacity percentage, battery type,
        charge time, and other operational data.
        
        Args:
            slot: Slot number (1-based)
            
        Returns:
            Parsed dictionary or None on failure
        """
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
        """
        Query Electric data for a slot.
        
        Electric data includes input voltage, input current, output voltage,
        charging current, and individual cell voltages.
        
        Args:
            slot: Slot number (1-based)
            
        Returns:
            Parsed dictionary or None on failure
        """
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
        """
        Query Internal Resistance for a slot.
        
        Args:
            slot: Slot number (1-based)
            
        Returns:
            Parsed dictionary or None on failure
        """
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

    # ------------------------------------------------------------------
    # Polling Loop
    # ------------------------------------------------------------------

    async def poll_data(self) -> bool:
        """
        Perform a complete polling cycle.
        
        This method queries all slots for WorkState data. For slots with
        changed state, it also queries Electric and IR data.
        
        The polling adapts to the number of slots of the detected model.
        
        Returns:
            False if the device is not responding (timeout), True otherwise
        """
        if not self.connected or not self.client:
            return False

        received_any = False
        occupied_slots = []

        # ------------------------------------------------------------------
        # Step 1: Query WorkState for all slots
        # ------------------------------------------------------------------
        for slot in range(1, self.num_slots + 1):
            work = await self._query_workstate(slot)
            
            if work:
                key = f"slot{slot}_workstate"
                self.latest_data[key] = work
                received_any = True
                status = work.get("status", 0)
                
                # Track occupied slots (not idle or error)
                if status not in (0, 5):
                    occupied_slots.append(slot)

                # Check if WorkState has changed since last poll
                old = self._cached_workstate.get(slot)
                if old != work:
                    self._cached_workstate[slot] = work
                    # If state changed and slot is active, query Electric and IR
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
                # Fallback: Try direct read if notification didn't arrive
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
                    
            # Small delay between commands to avoid overwhelming the charger
            await asyncio.sleep(COMMAND_INTERVAL)

        # ------------------------------------------------------------------
        # Step 2: Query Electric for Slot 1 (input voltage/power)
        # ------------------------------------------------------------------
        elec1 = await self._query_electric(1)
        if elec1:
            self.latest_data["slot1_electric"] = elec1
            received_any = True
        await asyncio.sleep(COMMAND_INTERVAL)

        # Update occupied slots for adaptive pause
        self._last_occupied_slots = occupied_slots if occupied_slots else None

        # ------------------------------------------------------------------
        # Step 3: Timeout Detection
        # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # Alarm Tone Control
    # ------------------------------------------------------------------

    async def get_alarm_tone(self) -> bool | None:
        """
        Query the current alarm tone state.
        
        Returns:
            True if alarm is on, False if off, None on error
        """
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
        """
        Set the alarm tone on or off.
        
        Args:
            state: True to enable alarm, False to disable
            
        Returns:
            True on success, False on error
        """
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

    # ------------------------------------------------------------------
    # Charging Parameters (WorkTasks)
    # ------------------------------------------------------------------

    async def set_worktask(self, channel: int, battery_type: int,
                           work_current_mA: int, capacity_limit_mAh: int,
                           task_type: int = 0, linking_type: int = 0,
                           cells: int = 0, full_changed_volt: int = 0) -> bool:
        """
        Set charging parameters for a slot.
        
        This command configures the charger for a specific battery type
        and charging parameters. The current is automatically validated
        against the model's maximum current limit.
        
        Args:
            channel: Slot number (0-based: 0 = Slot 1, ..., 5 = Slot 6)
            battery_type: Battery chemistry (see BATTERY_TYPE_STR_TO_INT)
            work_current_mA: Charge current in milliamperes
            capacity_limit_mAh: Capacity limit in mAh (0 = unlimited)
            task_type: 0 = charge (default)
            linking_type: 0 (default)
            cells: Number of cells (0 = auto-detect)
            full_changed_volt: Cut-off voltage in mV (0 = default)
            
        Returns:
            True on success, False on error
        """
        if not self.connected or not self.client:
            self._log("Not connected", force=True)
            return False

        # Validate current against model maximum
        if work_current_mA > self.max_current_mA:
            self._log(f"⚠️ Current {work_current_mA}mA exceeds model maximum {self.max_current_mA}mA", force=True)
            return False

        # Build command according to protocol
        cmd = bytearray()
        cmd.append(0x13)                           # Command header
        cmd.append(CMD_WORKTASKS_REQ)              # Command opcode
        cmd.append(channel & 0xFF)                 # Slot (0-based)
        cmd.append(task_type & 0xFF)               # Task type
        cmd.append(battery_type & 0xFF)            # Battery type
        cmd.append(linking_type & 0xFF)            # Linking type
        cmd.extend(work_current_mA.to_bytes(4, 'little'))  # Current in mA
        cmd.append(cells & 0xFF)                   # Cell count (0 = auto)
        cmd.extend(full_changed_volt.to_bytes(2, 'little')) # Cut-off voltage
        cmd.extend(capacity_limit_mAh.to_bytes(4, 'little')) # Capacity limit

        try:
            await self.client.write_gatt_char(CHAR_UUID_AF01, bytes(cmd))
            return True
        except Exception as e:
            self._log(f"Error sending WorkTasksReq: {e}", force=True)
            return False

    # ------------------------------------------------------------------
    # Disconnect
    # ------------------------------------------------------------------

    async def disconnect(self):
        """Disconnect from the charger and clean up resources."""
        if self.client:
            try:
                # Stop notifications if they were started
                await self.client.stop_notify(CHAR_UUID_AF01)
                await self.client.stop_notify(CHAR_UUID_AF02)
            except Exception:
                pass
                
            try:
                await self.client.disconnect()
            except Exception:
                pass
                
            self.connected = False
            self._poll_timeout_counter = 0
            self._last_occupied_slots = None
            self._log("⚠️ Disconnected", force=True)