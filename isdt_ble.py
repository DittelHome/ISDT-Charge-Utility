#!/usr/bin/env python3
"""
ISDT BLE – Encapsulates all Bluetooth communication with the charger.

This module owns the BLE client, the notification queue, the connection
handshake (HW info + bind + stream), both poll paths (A8 mega packet and
C4/A4 slot-by-slot) and the control commands (worktask, alarm tone).
"""

import sys
import asyncio
import struct
import uuid

from bleak import BleakClient
try:
    from bleak.args.winrt import WinRTClientArgs
except ImportError:
    WinRTClientArgs = None

from isdt_protocol import (
    CHAR_UUID_AF01, CHAR_UUID_AF02,
    CMD_HW_INFO_REQ, CMD_HW_INFO_RESP,
    CMD_BIND_REQ,
    CMD_WORKSTATE, CMD_ELECTRIC, CMD_IR, CMD_WORKTASKS_REQ,
    CMD_ALARM_TONE_REQ, CMD_ALARM_TONE_RESP,
    CMD_ALARM_TONE_TASK_REQ, CMD_ALARM_TONE_TASK_RESP,
    RESP_WORKSTATE, RESP_ELECTRIC, RESP_IR, RESP_A8_TASK,
    build_command, parse_charger_responses, parse_hardware_info,
    parse_alarm_tone, parse_a8_workstate_mega, parse_a8_task_resp,
)

from isdt_models import get_model_config, get_poll_interval


# ==================================================================
# TIMING CONSTANTS
# All delays are in seconds unless noted otherwise.
# This is the ONLY place to tune timing. Everything else uses these.
# ==================================================================

# --- Connection ---
CONNECT_RETRIES = 2               # how many connect attempts
CONNECT_TIMEOUT_S = 15.0          # hard timeout for BleakClient.connect()
POST_CONNECT_SETTLE = 1.0         # wait after BLE connect
POST_NOTIFICATION_SETUP = 0.5     # wait after notify handlers are set up
HW_INFO_TIMEOUT = 2.0             # hardware-info response timeout
RETRY_PAUSE_S = 10.0              # pause between two connect attempts

# --- Polling ---
COMMAND_INTERVAL = 0.2            # pause between two GATT writes in one poll
RESPONSE_TIMEOUT = 2.0            # wait for any single response
MAX_POLL_FAILURES = 2             # disconnect after this many empty polls
RECONNECT_DELAY_S = 10            # fixed delay before reconnect
START_POLL_DELAY_MS = 500         # delay between connect and first poll

# --- A8-specific ---
# Keep these separate from the C4/A4 timings. The A8 uses the same BLE
# connection, but its startup and response handling get their own margins.
A8_HW_INFO_TIMEOUT = 5.0          # allow a slower HW-info response during reconnect
A8_WORKSTATE_TIMEOUT = 15.0       # mega packet; measured responses are normally <1 s
A8_ELECTRIC_TIMEOUT = 4.0         # A8 input telemetry response
A8_DIAGNOSTIC_ELECTRIC_TIMEOUT = 3.0  # probe BLE after a missing WorkState mega-packet
A8_TASK_TIMEOUT = 5.0             # A8 per-slot task settings response


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
        self._disconnecting = False
        self._disconnect_task = None
        self._disconnect_client = None
        # Keep the client identity after teardown so delayed Bleak callbacks
        # from an intentional disconnect are not mistaken for link loss.
        self._expected_disconnect_client = None

        self.model_key = device_info.get("selected_model", "C4 Air")
        self.model_config = get_model_config(self.model_key)
        self.num_slots = self.model_config["slots"]
        self.max_current_mA = self.model_config["max_current_mA"]
        self.supported_battery_types = self.model_config["battery_types"]
        self.supports_alarm = self.model_config.get("supports_alarm", True)
        self.is_a8 = (self.model_key == "A8 Air")

        # Per-model IR scaling (C4: 10.0, A4: 100.0)
        self.ir_divisor = self.model_config.get("ir_divisor", 10.0)
        # Per-model slot order (A4 reports slots reversed)
        self.slot_order_reversed = self.model_config.get("slot_order_reversed", False)

        # Poll interval comes straight from the config. The GUI enforces
        # the model-specific min/max on save, so the value here is always
        # valid once the user has saved the settings.
        self.poll_interval = device_info.get(
            "poll_interval", get_poll_interval(self.model_key)
        )

        self.debug_traffic = False
        self.debug_log_file = None
        self._alarm_tone_state = False
        self.hw_info = {}
        self._expected_disconnect = False
        
        self.poll_loop_task = None

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    def _log(self, msg):
        if self.log_callback:
            self.log_callback(msg)

    def _log_traffic(self, direction: str, data: bytes, description: str = ""):
        """Write a hex dump line to the debug log file (if enabled)."""
        if not self.debug_traffic:
            return
        log_file = self.debug_log_file
        if not log_file:
            return
        from datetime import datetime
        timestamp = datetime.now().strftime("%d/%m/%Y %H:%M:%S.%f")[:-3]
        hex_str = data.hex().upper()
        hex_pairs = ' '.join(hex_str[i:i+2] for i in range(0, len(hex_str), 2))

        # Try to resolve the command name from the first two bytes
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
        """Queue every incoming BLE notification for the poll loop."""
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

    async def _wait_for_hw_info(self, timeout):
        """HW-Info response starts with 0xE1 at position 0 (not [0x31, 0xE1])."""
        start = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() - start < timeout:
            try:
                resp = await asyncio.wait_for(self.notification_queue.get(), timeout=0.2)
                if len(resp) >= 13 and resp[0] == CMD_HW_INFO_RESP:
                    return resp
            except asyncio.TimeoutError:
                continue
        return None

    # ------------------------------------------------------------------
    # Connection setup
    # ------------------------------------------------------------------

    async def _initialize(self) -> bool:
        """Run the connection handshake: HW info, bind, start data stream.

        Returns True only if all three steps succeed. A failed step aborts
        the whole connect attempt so we never end up half-connected.
        """
        self._log("✅ Initializing...")

        # Hardware info
        try:
            hw_cmd = struct.pack("<B", CMD_HW_INFO_REQ)
            await self.client.write_gatt_char(CHAR_UUID_AF02, hw_cmd, response=False)
            self._log_traffic("📤 WRITE", hw_cmd, "AF02")
        except Exception as e:
            self._log(f"⚠️ HW-Info write failed: {e}")
            return False

        hw_timeout = A8_HW_INFO_TIMEOUT if self.is_a8 else HW_INFO_TIMEOUT
        hw_resp = await self._wait_for_hw_info(hw_timeout)

        if not hw_resp:
            self._log("⚠️ HW-Info timeout – aborting init")
            return False

        parsed = parse_hardware_info(hw_resp)
        if not parsed:
            self._log("⚠️ HW-Info parse failed – aborting init")
            return False

        self.hw_info = parsed
        self._log(
            f"ℹ️ HW v{parsed['hw_version']} | "
            f"FW v{parsed['fw_version']} | "
            f"S/N {parsed['serial']}"
        )

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

        # Send BindReq
        try:
            cmd = struct.pack("<B", CMD_BIND_REQ) + bind_uuid + b"\x00\x00"
            await self.client.write_gatt_char(CHAR_UUID_AF02, cmd, response=False)
            self._log_traffic("📤 WRITE", cmd, "AF02")
        except Exception as e:
            self._log(f"⚠️ Bind write failed: {e}")
            return False

        await asyncio.sleep(0.2)

        # Start data stream
        try:
            stream_cmd = struct.pack("<B", 0xE2)
            await self.client.write_gatt_char(CHAR_UUID_AF02, stream_cmd, response=False)
            self._log_traffic("📤 WRITE", stream_cmd, "AF02")
        except Exception as e:
            self._log(f"⚠️ Start data stream failed: {e}")
            return False

        await asyncio.sleep(0.2)

        return True

    async def connect(self, retries=CONNECT_RETRIES):
        """Connect to the charger with the configured number of retries.

        Windows-specific tuning:
          - disconnected_callback: fires as soon as WinRT reports the link
            is gone, so the poll loop can react immediately.
          - use_cached_services=False: forces Windows to re-read the GATT
            database on every connect. This is critical for the A4/A8,
            which appear to re-initialize their GATT server when a battery
            is inserted or removed. Using the cache leads to stale handles
            and 'Not connected' errors.

        On Linux (and any other non-Windows platform), WinRTClientArgs is
        None and the winrt kwarg is omitted entirely.
        """
        attempt = 0
        while attempt < retries:
            client = None
            try:
                if WinRTClientArgs is not None and sys.platform == "win32":
                    client = BleakClient(
                        self.address,
                        timeout=CONNECT_TIMEOUT_S,
                        disconnected_callback=self._on_disconnected,
                        winrt=WinRTClientArgs(use_cached_services=False)
                    )
                else:
                    client = BleakClient(
                        self.address,
                        timeout=CONNECT_TIMEOUT_S,
                        disconnected_callback=self._on_disconnected
                    )

                self.client = client
                self._expected_disconnect_client = None
                self._log(f"⏳ Connecting... (attempt {attempt+1}/{retries})")

                await asyncio.wait_for(client.connect(), timeout=CONNECT_TIMEOUT_S)
                self.connected = bool(client.is_connected)
                if self.is_a8 and not self.connected:
                    raise ConnectionError("A8 disconnected immediately after BLE connect")
                self._log("✅ BLE connected.")

                await asyncio.sleep(POST_CONNECT_SETTLE)
                # The A8 can report a successful BLE connect and then drop the
                # link during the settle delay. Do not continue with GATT setup
                # on a client that has already disconnected.
                if self.is_a8 and (
                    self.client is not client or not client.is_connected or not self.connected
                ):
                    raise ConnectionError("A8 disconnected before notification setup")

                await client.start_notify(CHAR_UUID_AF01, self.notification_handler)
                if self.is_a8 and (
                    self.client is not client or not client.is_connected or not self.connected
                ):
                    raise ConnectionError("A8 disconnected while setting up AF01 notifications")

                await client.start_notify(CHAR_UUID_AF02, self.notification_handler)
                if self.is_a8 and (
                    self.client is not client or not client.is_connected or not self.connected
                ):
                    raise ConnectionError("A8 disconnected while setting up AF02 notifications")

                self._log("✅ Notification handlers registered.")
                await asyncio.sleep(POST_NOTIFICATION_SETUP)

                if self.is_a8 and (
                    self.client is not client or not client.is_connected or not self.connected
                ):
                    raise ConnectionError("A8 disconnected before initialization")

                ok = await self._initialize()
                if self.is_a8 and (
                    self.client is not client or not client.is_connected or not self.connected
                ):
                    raise ConnectionError("A8 disconnected during initialization")
                if not ok:
                    self._log("⚠️ Initialization failed – aborting attempt.")
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
                    self.client = None
                    self.connected = False
                    attempt += 1
                    if attempt < retries:
                        await asyncio.sleep(RETRY_PAUSE_S)
                    continue

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
                    await asyncio.sleep(RETRY_PAUSE_S)

        self._log("⚠️ All connection attempts failed.")
        if sys.platform == "win32":
            self._log("💡 Tip: Close the ISD Link app on your smartphone.")
            self._log("💡 Tip: Restart the Application.")
        else:
            self._log("💡 Tip: Please close the ISD Link app on your smartphone.")
            self._log("💡 Tip: Make sure the charger is powered on.")
        return False
        
        
    def _on_disconnected(self, client):
        """Handle BLE disconnect events fired by Bleak.

        Called from the platform event thread. When the GUI initiated
        the disconnect (closing the app or manual disconnect), we do
        nothing here – no reconnect is wanted.
        """
        # Intentional disconnect callbacks can arrive after disconnect()
        # has returned. Match the actual client, not a short-lived global flag.
        if client is self._expected_disconnect_client or self._disconnecting:
            self._log("ℹ️ Expected disconnect – no action")
            return

        # Ignore late callbacks from an older client/retry. They must not
        # invalidate a newer active BLE client.
        if client is not self.client:
            self._log("ℹ️ Stale BLE disconnect event ignored")
            return

        self.connected = False
        self._log("ℹ️ BLE disconnect event received")

        task = self.poll_loop_task
        if task is not None and not task.done():
            task.cancel()

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

        # --- Write 1: WorkState (mega packet for all 8 slots) ---
        cmd = build_command(CMD_WORKSTATE, 1)
        try:
            await self.client.write_gatt_char(CHAR_UUID_AF01, cmd, response=False)
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

        resp = await self._wait_for_opcode(RESP_WORKSTATE, A8_WORKSTATE_TIMEOUT)
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
            # Preserve task settings from the previous A8Task response
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

        # Pause between the two writes – this is the critical gap for the A8
        await asyncio.sleep(COMMAND_INTERVAL)

        # --- Write 2: Electric (input voltage / current) ---
        elec_cmd = build_command(CMD_ELECTRIC, 1)
        try:
            await self.client.write_gatt_char(CHAR_UUID_AF01, elec_cmd, response=False)
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

        elec_timeout = A8_ELECTRIC_TIMEOUT if self.is_a8 else RESPONSE_TIMEOUT
        elec = await self._wait_for_opcode(RESP_ELECTRIC, elec_timeout)
        if elec:
            parsed_elec = parse_charger_responses(elec)
            if parsed_elec:
                # Keep the slot-1 electric data and expose the same A8
                # channel-0 reading as the charger's input telemetry.
                self.latest_data["slot1_electric"] = parsed_elec
                self.latest_data["input_electric"] = parsed_elec

        return True

    async def diagnose_a8_ble(self):
        """Silently probe the A8 notification path before releasing a failed link."""
        if not self.is_a8 or not self.connected or not self.client:
            return

        diagnostic_cmd = build_command(CMD_ELECTRIC, 1)
        try:
            await self.client.write_gatt_char(
                CHAR_UUID_AF01, diagnostic_cmd, response=False
            )
            await self._wait_for_opcode(
                RESP_ELECTRIC, A8_DIAGNOSTIC_ELECTRIC_TIMEOUT
            )
        except Exception as e:
            err = str(e).lower()
            if ("-2147023673" in err or "abgebrochen" in err or "abort" in err
                    or "not connected" in err or "unreachable" in err or "disconnected" in err):
                self.connected = False

    # ------------------------------------------------------------------
    # C4 / A4 poll
    # ------------------------------------------------------------------

    async def _poll_c4_a4(self):
        """Slot-by-slot polling for C4 / A4.

        On the A4, the physical slot order is reversed relative to the
        protocol channel order: physical slot 1 = channel 3, slot 2 = channel 2,
        slot 3 = channel 1, slot 4 = channel 0. `slot_order_reversed` in the
        model config controls this.

        The input-voltage reading (channel 0 electric) is stored under its own
        key `input_electric` and does NOT overwrite `slot1_electric`.
        """
        if not self.connected or not self.client:
            return False

        received_any = False

        for slot in range(1, self.num_slots + 1):
            # Map GUI slot to protocol channel
            if self.slot_order_reversed:
                channel = self.num_slots - slot
            else:
                channel = slot - 1

            cmd = CMD_WORKSTATE + bytes([channel])
            try:
                await self.client.write_gatt_char(CHAR_UUID_AF01, cmd, response=False)
                self._log_traffic("📤 WRITE", cmd, "AF01")
            except Exception:
                continue

            resp = await self._wait_for_opcode(RESP_WORKSTATE, RESPONSE_TIMEOUT, expected_channel=channel)
            if resp:
                parsed = parse_charger_responses(resp)
                if parsed:
                    self.latest_data[f"slot{slot}_workstate"] = parsed
                    received_any = True

                    # If the slot is active, get IR + Electric
                    status_str = parsed.get("status_str", "idle")
                    if status_str not in ("idle", "empty", "error"):
                        await asyncio.sleep(COMMAND_INTERVAL)

                        ir_cmd = CMD_IR + bytes([channel])
                        try:
                            await self.client.write_gatt_char(CHAR_UUID_AF01, ir_cmd, response=False)
                            self._log_traffic("📤 WRITE", ir_cmd, "AF01")
                        except Exception:
                            pass
                        ir_resp = await self._wait_for_opcode(RESP_IR, RESPONSE_TIMEOUT, expected_channel=channel)
                        if ir_resp:
                            parsed_ir = parse_charger_responses(ir_resp, ir_divisor=self.ir_divisor)
                            if parsed_ir:
                                self.latest_data[f"slot{slot}_ir"] = parsed_ir

                        await asyncio.sleep(COMMAND_INTERVAL)

                        elec_cmd = CMD_ELECTRIC + bytes([channel])
                        try:
                            await self.client.write_gatt_char(CHAR_UUID_AF01, elec_cmd, response=False)
                            self._log_traffic("📤 WRITE", elec_cmd, "AF01")
                        except Exception:
                            pass
                        elec_resp = await self._wait_for_opcode(RESP_ELECTRIC, RESPONSE_TIMEOUT, expected_channel=channel)
                        if elec_resp:
                            parsed_elec = parse_charger_responses(elec_resp)
                            if parsed_elec:
                                self.latest_data[f"slot{slot}_electric"] = parsed_elec

            await asyncio.sleep(COMMAND_INTERVAL)

        # Input voltage (channel 0 electric).
        # IMPORTANT: store under its own key so it does NOT overwrite
        # slot1_electric. On the A4, slot 1 maps to channel 3, so the
        # input-voltage reading on channel 0 belongs to a different slot.
        elec_cmd = CMD_ELECTRIC + bytes([0])
        try:
            await self.client.write_gatt_char(CHAR_UUID_AF01, elec_cmd, response=False)
            self._log_traffic("📤 WRITE", elec_cmd, "AF01")
        except Exception:
            pass
        elec_resp = await self._wait_for_opcode(RESP_ELECTRIC, RESPONSE_TIMEOUT)
        if elec_resp:
            parsed_elec = parse_charger_responses(elec_resp)
            if parsed_elec:
                self.latest_data["input_electric"] = parsed_elec
                received_any = True

        return received_any

    # ------------------------------------------------------------------
    # Public poll entry
    # ------------------------------------------------------------------

    async def poll_data(self):
        """Run one poll cycle and return True if at least one value was read."""
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
            await self.client.write_gatt_char(CHAR_UUID_AF01, cmd, response=False)
            self._log_traffic("📤 WRITE", cmd, "AF01")
        except Exception as e:
            self._log(f"⚠️ A8Task write failed: {e}")
            return False

        resp = await self._wait_for_opcode(RESP_A8_TASK, A8_TASK_TIMEOUT)
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
        """Read the current alarm-tone state from the charger."""
        if not self.supports_alarm or not self.connected or not self.client:
            return None
        try:
            await self.client.write_gatt_char(CHAR_UUID_AF01, CMD_ALARM_TONE_REQ, response=False)
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
        """Enable or disable the charger's alarm tone."""
        if not self.supports_alarm or not self.connected or not self.client:
            return False
        task_type = 1 if state else 0
        cmd = bytes(CMD_ALARM_TONE_TASK_REQ) + bytes([task_type])
        try:
            await self.client.write_gatt_char(CHAR_UUID_AF01, cmd, response=False)
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
        """Send a work task to a slot (start charging with the given settings).

        `channel` is the 0-based GUI slot index. On models with reversed slot
        order it is mapped back to the protocol channel here.
        """
        if not self.connected or not self.client:
            return False
        if work_current_mA > self.max_current_mA:
            self._log(f"⚠️ Current {work_current_mA}mA exceeds max {self.max_current_mA}mA")
            return False

        # The caller passes a 0-based slot index. On the A4 we need to map
        # it back to the protocol channel if the slot order is reversed.
        if self.slot_order_reversed:
            channel = self.num_slots - 1 - channel

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
            await self.client.write_gatt_char(CHAR_UUID_AF01, bytes(cmd), response=False)
            self._log_traffic("📤 WRITE", bytes(cmd), "AF01")
            return True
        except Exception as e:
            self._log(f"⚠️ WorkTask error: {e}")
            return False

    # ------------------------------------------------------------------
    # Disconnect
    # ------------------------------------------------------------------

    async def disconnect(self):
        """Release the BLE client without treating a timed-out disconnect as success.

        On Windows, Bleak/WinRT disconnect can remain pending after the charger
        has emitted its disconnect callback. Keep the client and the original
        disconnect task until that operation really finishes. This prevents a
        second charger from being connected while Windows may still own the
        previous GATT session.
        """
        async with self._disconnect_lock:
            retry_finished_disconnect = (
                self._disconnect_task is not None
                and self._disconnect_task.done()
                and self._disconnect_client is not None
                and getattr(self._disconnect_client, "is_connected", False)
            )
            if self._disconnect_task is None or retry_finished_disconnect:
                self.connected = False
                client = self._disconnect_client if retry_finished_disconnect else self.client
                if client is None:
                    return

                self._disconnecting = True
                self._expected_disconnect_client = client
                self._disconnect_client = client
                self._log("ℹ️ Starting BLE client disconnect...")
                self._disconnect_task = asyncio.create_task(client.disconnect())
            else:
                client = self._disconnect_client
                self._log("ℹ️ Waiting for previous BLE disconnect to finish...")

            disconnect_task = self._disconnect_task

        done, pending = await asyncio.wait({disconnect_task}, timeout=10.0)
        if pending:
            self._log(
                "⚠️ BLE disconnect is still pending after 10s; "
                "keeping the client reserved and blocking device switch"
            )
            raise TimeoutError("BLE client disconnect still pending after 10 seconds")

        disconnect_error = None
        try:
            disconnect_task.result()
        except Exception as e:
            disconnect_error = e

        # A completed disconnect call is not enough if the backend still says
        # the link is connected. In that case retain the client for a retry.
        if client is not None and getattr(client, "is_connected", False):
            detail = str(disconnect_error).strip() if disconnect_error else "backend still connected"
            self._log(f"⚠️ BLE disconnect completed but link remains active: {detail}")
            raise RuntimeError(f"BLE link remains active after disconnect: {detail}")

        async with self._disconnect_lock:
            if self._disconnect_task is disconnect_task:
                self._disconnect_task = None
                self._disconnect_client = None
                if self.client is client:
                    self.client = None
                self._disconnecting = False

        if disconnect_error is not None:
            self._log(f"ℹ️ BLE disconnect completed with backend notice: {disconnect_error}")
        self._log("✅ BLE client released; disconnected")

