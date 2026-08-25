#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
ISDT Charger – Monitor & Control

This is the main GUI application for ISDT C4/A4/A8 Air chargers.
It provides real-time monitoring of all charging slots and full control
over charging parameters (battery type, current, capacity limit, cut‑off).

Supports: C4 Air, A4 Air, A8 Air, NP2 Air

Key features:
- BLE connection management (connect/disconnect/auto‑connect)
- Live data display (voltage, current, capacity, IR, charge time, status)
- Parameter control with battery‑specific validation
- Alarm tone toggle
- Persistent configuration (MAC, device name, poll interval, bind UUID)
- Adaptive polling (longer intervals when idle)
- GUI caching (only redraws when data changes)
- Automatic model detection

Dependencies:
- bleak (BLE library)
- tkinter (GUI)
- asyncio (async BLE communication)

Author: Klaus Voigt
License: MIT
"""

import asyncio
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
import threading
import time
import subprocess
import sys
import os

# ============================================================
# WINDOWS TASKLEISTE ICON FIX
# ============================================================
if sys.platform == "win32":
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ISDT.Charge.Utility")
    except Exception:
        pass

from isdt_ble import ISDTBLE
from isdt_config import load_config, save_config
from isdt_protocol import WORK_STATE_MAP
from isdt_models import (
    get_model_config,
    BATTERY_LIMITS,
    CURRENT_MIN_MA,
    CURRENT_MAX_MA,
    BATTERY_TYPE_STR_TO_INT,
)


class ISDTGui:
    """
    Main GUI class for the ISDT Charger Monitor.
    
    This class builds and manages the tkinter interface, handles BLE
    connections, displays live data, and provides controls for charging
    parameters. It automatically adapts to the connected charger model.
    """

    def __init__(self, root):
        """
        Constructor – initializes the GUI, loads config, and starts auto‑connection.
        
        Args:
            root: The tkinter main window (tk.Tk())
        """
        self.root = root
        self.root.title("ISDT Charger – Monitor & Control")

        # Load saved configuration from ~/.isdt_gui_config.json
        self.config = load_config()

        # BLE device object (initialized after successful connection)
        self.device = None

        # State variables
        self.scanning = False          # Is a BLE scan currently running?
        self.polling = False           # Is the polling loop running?
        self.scanned_devices = []      # List of discovered BLE devices
        self.charge_start_times = {}   # Fallback charge time per slot
        self._last_table_values = []   # GUI caching: last displayed rows

        # Widget references for dynamic updates
        self.slot_combo = None
        self.battery_combo = None

        # Configure ttk styles
        style = ttk.Style()
        style.theme_use('clam')
        # Red, bold "Apply" button to make it stand out as the primary action
        style.configure("Red.TButton", foreground="red", font=('Helvetica', 10, 'bold'))

        # Create notebook (tabbed interface)
        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        # Tab 1: "Device" – main view with table and controls
        self.tab_device = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_device, text="Device")
        self._build_device_tab()

        # Tab 2: "Settings" – scan and configuration
        self.tab_settings = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_settings, text="Settings")
        self._build_settings_tab()

        # Start asyncio event loop in background thread
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()

        # Auto-connect if MAC address is saved
        if self.config.get("mac_address"):
            mac = self.config.get("mac_address")
            self._kill_blueman_connection(mac)
            self.root.after(500, self.auto_connect)

    def _run_loop(self):
        """Starts the asyncio event loop in the background thread."""
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    # ------------------------------------------------------------------
    # GUI Builders
    # ------------------------------------------------------------------

    def _build_device_tab(self):
        """
        Builds the 'Device' tab – the main view.
        
        Contains:
        - Status bar (connection status, input voltage, total power)
        - Control buttons (Connect, Disconnect, Alarm toggle)
        - Model info label
        - Data table with 9 columns (Slot, Status, Type, Voltage, Current,
          Capacity, IR, Charge Time, Charge Level)
        - Slot Settings panel (battery type, current, capacity limit, cut‑off)
        - Log window for status messages
        """
        # ------------------------------------------------------------------
        # Top bar
        # ------------------------------------------------------------------
        frame_top = ttk.Frame(self.tab_device)
        frame_top.pack(pady=5, fill=tk.X)

        # Connection status
        self.status_label = ttk.Label(frame_top, text="No device connected", foreground="gray")
        self.status_label.pack(side=tk.LEFT, padx=5)

        # Input voltage (from Slot 1 Electric response)
        self.input_voltage_label = ttk.Label(frame_top, text="🔌 Input voltage: -- V", foreground="blue")
        self.input_voltage_label.pack(side=tk.LEFT, padx=15)

        # Total power (input voltage × input current)
        self.total_power_label = ttk.Label(frame_top, text="⚡ Total power: -- W", foreground="green")
        self.total_power_label.pack(side=tk.LEFT, padx=15)

        # Connect/Disconnect buttons
        self.connect_btn = ttk.Button(frame_top, text="Connect (saved)", command=self.connect_saved)
        self.connect_btn.pack(side=tk.LEFT, padx=2)

        self.disconnect_btn = ttk.Button(frame_top, text="Disconnect", command=self.disconnect_device, state=tk.DISABLED)
        self.disconnect_btn.pack(side=tk.LEFT, padx=2)

        # Alarm tone toggle button (speaker icon)
        self.alarm_btn = ttk.Button(frame_top, text="🔊", width=4, command=self.toggle_alarm_tone)
        self.alarm_btn.pack(side=tk.LEFT, padx=5)

        # Model info label (displays detected model)
        self.model_label = ttk.Label(frame_top, text="Model: --", foreground="purple")
        self.model_label.pack(side=tk.LEFT, padx=15)

        # ------------------------------------------------------------------
        # Data table
        # ------------------------------------------------------------------
        columns = (
            "Slot", "Status", "Type", "Voltage (V)", "Current (A)",
            "Capacity (mAh)", "IR (mΩ)", "Charge Time", "Charge Level"
        )
        self.tree = ttk.Treeview(self.tab_device, columns=columns, show="headings")

        col_widths = {
            "Slot": 50, "Status": 150, "Type": 80, "Voltage (V)": 100,
            "Current (A)": 100, "Capacity (mAh)": 120,
            "IR (mΩ)": 80, "Charge Time": 100, "Charge Level": 170,
        }
        for col in columns:
            self.tree.heading(col, text=col)
            self.tree.column(col, width=col_widths.get(col, 100), anchor="center", minwidth=50)
        self.tree.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        # Click on table row → automatically select the slot and load settings
        self.tree.bind("<<TreeviewSelect>>", self.on_tree_select)

        # ------------------------------------------------------------------
        # Slot Settings panel
        # ------------------------------------------------------------------
        settings_frame = ttk.LabelFrame(self.tab_device, text="🔧 Slot Settings")
        settings_frame.pack(fill=tk.X, padx=10, pady=5)

        # Slot selection (1-6 default, will be dynamically updated)
        ttk.Label(settings_frame, text="Slot:").grid(row=0, column=0, padx=5, pady=5)
        self.slot_var = tk.StringVar(value="1")
        self.slot_combo = ttk.Combobox(settings_frame, textvariable=self.slot_var,
                                       values=[str(i) for i in range(1, 7)], width=5)
        self.slot_combo.grid(row=0, column=1, padx=5)
        self.slot_combo.bind("<<ComboboxSelected>>", lambda e: self.update_settings_fields())

        # Battery type dropdown (will be dynamically updated based on model)
        ttk.Label(settings_frame, text="Battery type:").grid(row=0, column=2, padx=5)
        self.battery_type_var = tk.StringVar()
        self.battery_combo = ttk.Combobox(settings_frame, textvariable=self.battery_type_var,
                                          values=[], width=14)  # Empty, will be populated dynamically
        self.battery_combo.grid(row=0, column=3, padx=5)
        self.battery_combo.set("Auto")
        self.battery_combo.bind("<<ComboboxSelected>>", self._update_cutoff_state)

        # Charge current (mA) – 100–2000 mA range (validated per model)
        ttk.Label(settings_frame, text="Current (mA):").grid(row=0, column=4, padx=5)
        self.current_entry = ttk.Entry(settings_frame, width=8)
        self.current_entry.grid(row=0, column=5, padx=5)
        self.current_entry.insert(0, "1000")

        # Capacity limit (mAh) – battery-specific range, 0 = unlimited
        ttk.Label(settings_frame, text="Capacity limit (mAh):").grid(row=0, column=6, padx=5)
        self.capacity_entry = ttk.Entry(settings_frame, width=8)
        self.capacity_entry.grid(row=0, column=7, padx=5)
        self.capacity_entry.insert(0, "2000")

        # Cut‑off voltage (mV) – battery-specific range, 0 = default
        # Values < 6 mV → label "Cut-off (ΔmV)", otherwise "Cut-off (mV)"
        self.cutoff_label = ttk.Label(settings_frame, text="Cut‑off (mV):")
        self.cutoff_label.grid(row=0, column=8, padx=5)
        self.cutoff_entry = ttk.Entry(settings_frame, width=6)
        self.cutoff_entry.grid(row=0, column=9, padx=5)
        self.cutoff_entry.insert(0, "5")
        self.cutoff_entry.bind("<KeyRelease>", self._update_cutoff_delta_label)
        self.cutoff_entry.bind("<FocusOut>", self._update_cutoff_delta_label)

        # Apply button – red, bold, sends settings to charger
        set_btn = ttk.Button(settings_frame, text="Apply", style="Red.TButton", command=self.apply_settings)
        set_btn.grid(row=0, column=10, padx=10)

        # ------------------------------------------------------------------
        # Log window
        # ------------------------------------------------------------------
        self.log = scrolledtext.ScrolledText(self.tab_device, height=6, state='disabled')
        self.log.pack(fill=tk.X, padx=10, pady=5)

    def _build_settings_tab(self):
        """
        Builds the 'Settings' tab – two columns side by side.

        Left column: Scan area
        - Scan button
        - Listbox with found devices
        - Save button to store selected device

        Right column: Settings
        - MAC address (read‑only, shown for reference)
        - Device name
        - Poll interval (seconds)
        """
        main_frame = ttk.Frame(self.tab_settings, padding=10)
        main_frame.pack(fill=tk.BOTH, expand=True)

        # ---- Left column: Scan ----
        left_frame = ttk.Frame(main_frame)
        left_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 10))

        ttk.Label(left_frame, text="🔍 Scan for devices:", font=("", 10, "bold")).pack(anchor=tk.W, pady=(0, 10))

        self.scan_btn = ttk.Button(left_frame, text="Scan", command=self.scan_devices)
        self.scan_btn.pack(anchor=tk.W, pady=5)

        self.scan_status = ttk.Label(left_frame, text="", foreground="gray")
        self.scan_status.pack(anchor=tk.W, pady=5)

        ttk.Label(left_frame, text="Found devices:").pack(anchor=tk.W, pady=(10, 5))
        self.device_listbox = tk.Listbox(left_frame, height=6, width=40)
        self.device_listbox.pack(fill=tk.X, pady=5)
        self.device_listbox.bind("<<ListboxSelect>>", self.on_device_select)

        self.save_btn = ttk.Button(left_frame, text="Save selected device",
                                   command=self.save_selected_device, state=tk.DISABLED)
        self.save_btn.pack(anchor=tk.W, pady=10)

        # ---- Right column: Settings ----
        right_frame = ttk.Frame(main_frame)
        right_frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=(10, 0))

        ttk.Label(right_frame, text="💾 Settings:", font=("", 10, "bold")).pack(anchor=tk.W, pady=(0, 10))

        ttk.Label(right_frame, text="MAC address:").pack(anchor=tk.W, pady=(5, 0))
        self.settings_mac = ttk.Entry(right_frame, width=30)
        self.settings_mac.pack(anchor=tk.W, pady=2)
        self.settings_mac.insert(0, self.config.get("mac_address", ""))

        ttk.Label(right_frame, text="Device name:").pack(anchor=tk.W, pady=(10, 0))
        self.settings_name = ttk.Entry(right_frame, width=30)
        self.settings_name.pack(anchor=tk.W, pady=2)
        self.settings_name.insert(0, self.config.get("device_name", ""))

        ttk.Label(right_frame, text="Poll interval (s):").pack(anchor=tk.W, pady=(10, 0))
        self.settings_interval = ttk.Entry(right_frame, width=10)
        self.settings_interval.pack(anchor=tk.W, pady=2)
        self.settings_interval.insert(0, str(self.config.get("poll_interval", 5)))

        save_settings_btn = ttk.Button(right_frame, text="Save settings", command=self.save_settings)
        save_settings_btn.pack(anchor=tk.W, pady=20)

    # ------------------------------------------------------------------
    # Model-specific GUI Updates
    # ------------------------------------------------------------------

    def update_gui_for_model(self):
        """
        Update GUI elements based on the detected model.
        
        This method is called after a successful connection and updates:
        - Window title with model name
        - Model label
        - Slot dropdown values (1 to num_slots)
        - Battery type dropdown with supported types
        - Log message with model details
        """
        if not self.device or not self.device.connected:
            return

        model_key = self.device.model_key
        model_config = self.device.model_config

        # Update window title
        self.root.title(f"ISDT {model_config['display_name']} – Monitor & Control")

        # Update model label
        self.model_label.config(text=f"Model: {model_config['display_name']}")

        # Update slot dropdown
        num_slots = model_config["slots"]
        if self.slot_combo:
            self.slot_combo['values'] = [str(i) for i in range(1, num_slots + 1)]
            if int(self.slot_var.get()) > num_slots:
                self.slot_var.set("1")

        # Update battery type dropdown with supported types
        supported_types = model_config["battery_types"]
        if self.battery_combo:
            self.battery_combo['values'] = supported_types
            if self.battery_type_var.get() not in supported_types:
                self.battery_type_var.set(supported_types[0] if supported_types else "Auto")

        # Log model details
        max_current = model_config["max_current_mA"]
        self.log_message(f"📊 Model: {model_config['display_name']} ({num_slots} slots, max {max_current}mA)")

        # Reset GUI cache for correct display
        self._last_table_values = []

    # ------------------------------------------------------------------
    # Logging & Status Updates
    # ------------------------------------------------------------------

    def log_message(self, msg):
        """Adds a message to the log window (with newline)."""
        self.log.config(state='normal')
        self.log.insert(tk.END, msg + "\n")
        self.log.see(tk.END)  # Auto‑scroll to bottom
        self.log.config(state='disabled')

    def update_status(self, text, color="black"):
        """Updates the status label at the top."""
        self.status_label.config(text=text, foreground=color)

    def update_device_info(self, input_voltage_mV, input_current_mA):
        """
        Updates the input voltage and total power display in the top bar.

        Args:
            input_voltage_mV: Input voltage in millivolts
            input_current_mA: Input current in milliamperes
        """
        self.input_voltage_label.config(text=f"🔌 Input voltage: {input_voltage_mV/1000:.1f} V")
        if input_voltage_mV > 0 and input_current_mA > 0:
            total_power_W = (input_voltage_mV * input_current_mA) / 1_000_000
            self.total_power_label.config(text=f"⚡ Total power: {total_power_W:.1f} W")
        else:
            self.total_power_label.config(text="⚡ Total power: -- W")

    # ------------------------------------------------------------------
    # Bluetooth Helpers
    # ------------------------------------------------------------------

    def _kill_blueman_connection(self, mac):
        """
        Disconnects an active Blueman connection to the given MAC address.

        Blueman sometimes keeps the connection active and blocks BLE access.
        This function releases the connection so that bleak can use it.

        Args:
            mac: MAC address of the device
        """
        if not mac:
            return
        try:
            subprocess.run(["bluetoothctl", "disconnect", mac], capture_output=True, timeout=3)
            time.sleep(0.3)  # Wait for the disconnection to settle
        except Exception:
            pass  # Ignore errors – not critical

    # ------------------------------------------------------------------
    # Cut‑off Field State
    # ------------------------------------------------------------------

    def _get_default_cutoff(self, batt_str: str) -> int:
        """
        Get default cut-off value for a battery type.
        
        Args:
            batt_str: Battery type string (e.g., "LiHV", "NiMh/NiCd")
            
        Returns:
            Default cut-off value in mV, or 0 if not supported
        """
        limits = BATTERY_LIMITS.get(batt_str)
        if limits and limits["cutoff_enabled"]:
            # Return the middle of the allowed range as default
            return (limits["cutoff_min"] + limits["cutoff_max"]) // 2
        return 0

    def _update_cutoff_delta_label(self, event=None):
        """Label shows Cut-off (ΔmV) when value is < 6 mV (delta-V mode)."""
        if not hasattr(self, "cutoff_label"):
            return
        try:
            if self.cutoff_entry.cget("state") == "disabled":
                self.cutoff_label.config(text="Cut‑off (mV):")
                return
            raw = self.cutoff_entry.get().strip()
            if raw == "":
                self.cutoff_label.config(text="Cut‑off (mV):")
                return
            val = int(raw)
            if 0 < val < 6:
                self.cutoff_label.config(text="Cut‑off (ΔmV):")
            else:
                self.cutoff_label.config(text="Cut‑off (mV):")
        except ValueError:
            self.cutoff_label.config(text="Cut‑off (mV):")

    def _update_cutoff_state(self, event=None):
        """
        Enables or disables the cut‑off field based on the selected battery type.

        For Auto and LiIon(1.5V), cut‑off is not supported – the field is disabled.
        For all other types, the field is enabled.
        """
        batt_str = self.battery_type_var.get()
        limits = BATTERY_LIMITS.get(batt_str)
        if limits and not limits["cutoff_enabled"]:
            self.cutoff_entry.config(state='disabled')
            self.cutoff_entry.delete(0, tk.END)
            self.cutoff_entry.insert(0, "0")
        else:
            self.cutoff_entry.config(state='normal')
            # Set a sensible default if field is empty or "0"
            current_value = self.cutoff_entry.get()
            if current_value == "0" or current_value == "":
                default_cutoff = self._get_default_cutoff(batt_str)
                self.cutoff_entry.delete(0, tk.END)
                self.cutoff_entry.insert(0, str(default_cutoff))
        self._update_cutoff_delta_label()

    # ------------------------------------------------------------------
    # Connection Management
    # ------------------------------------------------------------------

    def auto_connect(self):
        """
        Automatic connection on startup.

        Called by __init__ after a 500ms delay. Uses the saved MAC address.
        """
        mac = self.config.get("mac_address")
        device_name = self.config.get("device_name", "")
        if mac:
            self.log_message(f"⏳ Auto-connecting to {mac} ...")
            self.device = ISDTBLE(mac, log_callback=self.log_message, debug=False,
                                  config=self.config, device_name=device_name)
            asyncio.run_coroutine_threadsafe(self._connect_async(), self.loop)

    def connect_saved(self):
        """Manual connection using the saved MAC address (button click)."""
        mac = self.config.get("mac_address")
        device_name = self.config.get("device_name", "")
        if not mac:
            messagebox.showerror("Error", "No MAC address saved.")
            return
        self.log_message(f"⏳ Connecting to saved address {mac} ...")
        self._kill_blueman_connection(mac)
        self.device = ISDTBLE(mac, log_callback=self.log_message, debug=False,
                              config=self.config, device_name=device_name)
        asyncio.run_coroutine_threadsafe(self._connect_async(), self.loop)

    async def _connect_async(self):
        """
        Asynchronous connection routine.

        Attempts to connect to the device and starts polling on success.
        Shows helpful tips in the log on failure.
        """
        try:
            success = await self.device.connect(retries=2)
            if success:
                self.root.after(0, lambda: self.log_message("✅ Connected!"))
                self.root.after(0, lambda: self.update_status(
                    f"Connected to {self.config.get('device_name', self.device.address)}", "green"
                ))
                self.root.after(0, lambda: self.connect_btn.config(state=tk.DISABLED))
                self.root.after(0, lambda: self.disconnect_btn.config(state=tk.NORMAL))
                self.root.after(0, self.update_gui_for_model)  # Update GUI for detected model
                self.start_polling()
                self.root.after(1000, self.update_settings_fields)
                await self._update_alarm_button()
            else:
                self.root.after(0, lambda: self.log_message("⚠️ Connection failed."))
                self.root.after(0, lambda: self.log_message("💡 Tip: Please close the ISD Link app on your smartphone."))
                self.root.after(0, lambda: self.log_message("💡 Tip: Make sure the charger is powered on."))
        except Exception as e:
            self.root.after(0, lambda: self.log_message(f"⚠️ Error: {str(e)}"))
            self.root.after(0, lambda: self.log_message("💡 Tip: Please close the ISD Link app on your smartphone."))
            self.root.after(0, lambda: self.log_message("💡 Tip: Make sure the charger is powered on."))

    def disconnect_device(self):
        """
        Disconnects from the charger and resets the GUI.

        Stops polling, disconnects BLE, clears the table, and resets buttons.
        """
        # Immediately mark as disconnected to prevent further polling
        if self.device:
            self.device.connected = False
            self.polling = False
            asyncio.run_coroutine_threadsafe(self.device.disconnect(), self.loop)
            self.device = None

        self.connect_btn.config(state=tk.NORMAL)
        self.disconnect_btn.config(state=tk.DISABLED)
        self.update_status("Disconnected", "gray")
        self.update_device_info(0, 0)
        self.charge_start_times.clear()
        self._last_table_values = []
        self.update_table()
        self.alarm_btn.config(text="🔊")
        self.model_label.config(text="Model: --")

    # ------------------------------------------------------------------
    # Polling (Adaptive)
    # ------------------------------------------------------------------

    def start_polling(self):
        """Starts the polling loop (automatically started after connection)."""
        if not self.device or not self.device.connected:
            self.log_message("⚠️ No device connected.")
            return
        if self.polling:
            return
        self.polling = True
        interval = self.config.get("poll_interval", 5)
        idle_interval = 10  # Longer pause when no slots are active
        self.log_message(f"✅ Polling started (interval: {interval}s, idle: {idle_interval}s).....")
        asyncio.run_coroutine_threadsafe(self._poll_loop(interval, idle_interval), self.loop)

    async def _poll_loop(self, interval, idle_interval):
        """
        Asynchronous polling loop with adaptive interval.

        Args:
            interval: Normal polling interval in seconds (when slots are active)
            idle_interval: Polling interval when no slots are active (longer = less traffic)
        """
        while self.polling and self.device and self.device.connected:
            # poll_data() returns False if connection was lost
            still_connected = await self.device.poll_data()
            self.root.after(0, self.update_table)

            if not still_connected:
                self.polling = False
                self.root.after(0, lambda: self.log_message("⚠️ Connection lost – reconnecting..."))
                self.root.after(2000, self.connect_saved)
                break

            # Adaptive pause: longer when idle (no occupied slots)
            if self.device._last_occupied_slots:
                await asyncio.sleep(interval)
            else:
                await asyncio.sleep(idle_interval)

        self.log_message("✅ Polling stopped")

    # ------------------------------------------------------------------
    # Display Helpers
    # ------------------------------------------------------------------

    def _format_time(self, seconds):
        """
        Formats seconds into a human‑readable time string.

        Examples:
        - 45s → "45s"
        - 125s → "2m 5s"
        - 3725s → "1h 2m"

        Args:
            seconds: Time in seconds (int or float)

        Returns:
            Formatted time string
        """
        if seconds < 0:
            seconds = 0
        if seconds < 60:
            return f"{seconds:.0f}s"
        elif seconds < 3600:
            return f"{seconds//60:.0f}m {seconds%60:.0f}s"
        else:
            h = seconds // 3600
            m = (seconds % 3600) // 60
            return f"{h:.0f}h {m:.0f}m"

    def _battery_bar(self, percent):
        """
        Creates a visual battery bar with 12 segments.

        Format: ████████░░░░ 67%
        - █ = filled segment
        - ░ = empty segment

        Args:
            percent: Battery percentage (0–100)

        Returns:
            String with bar and percentage
        """
        if percent < 0:
            percent = 0
        if percent > 100:
            percent = 100
        filled = int(percent / 100 * 12)
        empty = 12 - filled
        return "█" * filled + "░" * empty + f" {percent:3d}%"

    # ------------------------------------------------------------------
    # Table Update (with GUI Caching)
    # ------------------------------------------------------------------

    def update_table(self):
        """
        Updates the data table – but only if data has actually changed.

        This is a performance optimization: the table is only redrawn when
        new data differs from the previous display. This reduces CPU usage
        and prevents flickering.
        """
        new_values = []

        # If not connected, clear the table (but only once)
        if not self.device or not self.device.connected:
            if self._last_table_values:
                for item in self.tree.get_children():
                    self.tree.delete(item)
                self._last_table_values = []
            return

        # Read input voltage and current from Slot 1 (device‑wide values)
        input_voltage_mV = 0
        input_current_mA = 0
        now = time.time()
        elec1 = self.device.latest_data.get("slot1_electric", {})
        if elec1.get("input_voltage_mV", 0) > 0:
            input_voltage_mV = elec1["input_voltage_mV"]
            input_current_mA = elec1.get("input_current_mA", 0)

        # Build table rows for all slots (using detected model's slot count)
        num_slots = self.device.num_slots

        for slot in range(1, num_slots + 1):
            work = self.device.latest_data.get(f"slot{slot}_workstate", {})
            elec = self.device.latest_data.get(f"slot{slot}_electric", {})
            ir = self.device.latest_data.get(f"slot{slot}_ir", {})

            # Skip if no data for this slot
            if not work and not elec and not ir:
                continue

            status = work.get("status_str", "unknown")
            is_idle = status in ("idle", "empty") or work.get("status", 0) == 0

            if is_idle:
                # Empty slot – all values 0 / placeholder
                voltage_V = 0.0
                current_A = 0.0
                capacity = 0
                ir_val = 0
                battery_type = "-"
                capacity_percent = 0
                charge_time_str = "--:--"
                battery_display = "—"
                if slot in self.charge_start_times:
                    del self.charge_start_times[slot]
            else:
                # --- Voltage ---
                voltage_mV = elec.get("voltage_mV", 0)
                if voltage_mV == 0:
                    voltage_mV = work.get("voltage_mV", 0)
                voltage_V = voltage_mV / 1000.0

                # --- Current ---
                current_mA = work.get("work_current_mA", 0)
                if current_mA == 0:
                    current_mA = elec.get("charging_current_mA", 0)
                current_A = current_mA / 1000.0

                # --- Battery type ---
                battery_type = work.get("battery_type_str", "unknown")

                # --- Capacity ---
                capacity_percent = work.get("capacity_percent", 0)
                capacity = elec.get("capacity_mAh", work.get("capacity_mAh", 0))

                # --- Internal resistance (rounded to integer) ---
                ir_val = ir.get("ir_total_mohm", work.get("ir_mohm", 0))
                if ir_val == 0 and "ir_values_mohm" in ir:
                    ir_val = ir["ir_total_mohm"]
                ir_val = round(ir_val)

                # --- Charge time ---
                # 1st attempt: Read from device (work_period_ms)
                work_period_ms = work.get("work_period_ms", 0)
                if work_period_ms > 0:
                    charge_time_str = self._format_time(work_period_ms / 1000.0)
                else:
                    # 2nd attempt: Self‑calculated (fallback)
                    is_charging = status in (
                        "Pre-charge / trickle",
                        "CC constant current",
                        "Active charging",
                        "CV constant voltage"
                    ) or work.get("status", 0) in (1, 2, 3, 4)

                    if is_charging:
                        if slot not in self.charge_start_times:
                            self.charge_start_times[slot] = now
                        charge_time_str = self._format_time(now - self.charge_start_times[slot])
                    else:
                        if slot in self.charge_start_times:
                            if status == "done" or status == "error":
                                del self.charge_start_times[slot]
                                charge_time_str = "done"
                            else:
                                charge_time_str = self._format_time(now - self.charge_start_times[slot]) + " (⏸️)"
                        else:
                            charge_time_str = "--:--"

                # --- Battery bar ---
                battery_display = self._battery_bar(capacity_percent)

            # Build row tuple
            row = (slot, status, battery_type, f"{voltage_V:.3f}", f"{current_A:.2f}",
                   capacity, ir_val, charge_time_str, battery_display)
            new_values.append(row)

        # Only update if data has changed (GUI caching)
        if new_values == self._last_table_values:
            return

        # Redraw table
        for item in self.tree.get_children():
            self.tree.delete(item)
        for row in new_values:
            self.tree.insert("", tk.END, values=row)
        self._last_table_values = new_values

        # Update device info (input voltage, total power)
        if input_voltage_mV > 0:
            self.root.after(0, lambda: self.update_device_info(input_voltage_mV, input_current_mA))

    # ------------------------------------------------------------------
    # Slot Selection
    # ------------------------------------------------------------------

    def on_tree_select(self, event):
        """
        Called when a table row is clicked.

        Selects the slot and loads settings from WorkState (and table Type).
        """
        selection = self.tree.selection()
        if selection:
            item = selection[0]
            values = self.tree.item(item, 'values')
            if values:
                slot = values[0]  # First column is "Slot"
                self.slot_var.set(str(slot))
                # Prefer battery type shown in the table (column "Type")
                if len(values) > 2 and values[2] and values[2] != "--":
                    batt_str = values[2]
                    if self.device and batt_str in getattr(self.device, "supported_battery_types", []):
                        self.battery_type_var.set(batt_str)
                        self._update_cutoff_state()
                self.update_settings_fields()

    def update_settings_fields(self):
        """
        Loads charging settings for the selected slot from latest WorkState.

        Also works for idle slots when the charger still reports parameters.
        """
        if not self.device or not self.device.connected:
            return
        try:
            slot = int(self.slot_var.get()) - 1
            work = self.device.latest_data.get(f"slot{slot + 1}_workstate", {})
            if not work:
                return

            # Battery type
            batt_type = work.get("battery_type", -1)
            if batt_type >= 0:
                inv_map = {v: k for k, v in BATTERY_TYPE_STR_TO_INT.items()}
                batt_str = inv_map.get(batt_type, "Auto")
                if batt_str in self.device.supported_battery_types:
                    self.battery_type_var.set(batt_str)
                    self._update_cutoff_state()

            # Configured charge current (mA)
            current_mA = work.get("work_current_mA", 0)
            if current_mA > 0:
                self.current_entry.delete(0, tk.END)
                self.current_entry.insert(0, str(current_mA))

            # Capacity limit (mAh)
            capacity = work.get("max_output_power_mW", 0)
            if capacity > 0:
                self.capacity_entry.delete(0, tk.END)
                self.capacity_entry.insert(0, str(capacity))

            # Cut-off voltage (mV)
            if self.cutoff_entry.cget("state") != "disabled":
                cutoff = work.get("full_charged_volt_mV", 0)
                if cutoff > 0:
                    self.cutoff_entry.delete(0, tk.END)
                    self.cutoff_entry.insert(0, str(cutoff))

            if hasattr(self, "_update_cutoff_delta_label"):
                self._update_cutoff_delta_label()
        except Exception as e:
            self.log_message(f"⚠️ update_settings_fields error: {e}")

    # ------------------------------------------------------------------
    # Alarm Tone

    # ------------------------------------------------------------------

    async def _update_alarm_button(self):
        """Fetches the current alarm tone state from the device and updates the button."""
        if self.device and self.device.connected:
            state = await self.device.get_alarm_tone()
            if state is not None:
                self.root.after(0, lambda: self.alarm_btn.config(text="🔊" if state else "🔇"))

    def toggle_alarm_tone(self):
        """Toggles the alarm tone on/off (button click)."""
        if not self.device or not self.device.connected:
            messagebox.showerror("Error", "No device connected.")
            return
        new_state = not self.device._alarm_tone_state
        asyncio.run_coroutine_threadsafe(self._toggle_alarm_async(new_state), self.loop)

    async def _toggle_alarm_async(self, new_state):
        """Asynchronously sets the alarm tone."""
        success = await self.device.set_alarm_tone(new_state)
        if success:
            self.root.after(0, lambda: self.alarm_btn.config(text="🔊" if new_state else "🔇"))
            self.log_message(f"🔊 Alarm tone {'on' if new_state else 'off'}")
        else:
            self.root.after(0, lambda: self.log_message("⚠️ Failed to set alarm tone."))
            self.device._alarm_tone_state = not new_state

    # ------------------------------------------------------------------
    # Device Scan
    # ------------------------------------------------------------------

    def scan_devices(self):
        """Starts a BLE scan for ISDT devices (10 seconds)."""
        if self.scanning:
            return
        self.scanning = True
        self.scan_btn.config(state=tk.DISABLED)
        self.scan_status.config(text="Searching...")
        self.device_listbox.delete(0, tk.END)
        self.log_message("🔎 Scanning for BLE devices (10 seconds)...")
        asyncio.run_coroutine_threadsafe(self._scan_async(), self.loop)

    async def _scan_async(self):
        """Asynchronous BLE scan."""
        from bleak import BleakScanner
        devices = await BleakScanner.discover(timeout=10)
        # Only show devices with a name (filters out unnamed BLE devices)
        self.scanned_devices = [d for d in devices if d.name]
        self.scanning = False
        self.root.after(0, self._update_scan_results)

    def _update_scan_results(self):
        """Updates the device listbox with scan results."""
        self.scan_btn.config(state=tk.NORMAL)
        self.device_listbox.delete(0, tk.END)
        for d in self.scanned_devices:
            self.device_listbox.insert(tk.END, f"{d.name} ({d.address})")
        self.scan_status.config(text=f"{len(self.scanned_devices)} devices found")
        self.log_message(f"✅ {len(self.scanned_devices)} devices found.")
        self.save_btn.config(state=tk.NORMAL if self.scanned_devices else tk.DISABLED)

    def on_device_select(self, event):
        """Called when a device is selected in the listbox."""
        selection = self.device_listbox.curselection()
        self.save_btn.config(state=tk.NORMAL if selection else tk.DISABLED)

    def save_selected_device(self):
        """Saves the selected device to the configuration."""
        selection = self.device_listbox.curselection()
        if not selection:
            return
        idx = selection[0]
        device = self.scanned_devices[idx]

        self.config["mac_address"] = device.address
        self.config["device_name"] = device.name or ""
        save_config(self.config)

        self.settings_mac.delete(0, tk.END)
        self.settings_mac.insert(0, device.address)
        self.settings_name.delete(0, tk.END)
        self.settings_name.insert(0, device.name or "")

        self.log_message(f"✅ Saved: {device.name} ({device.address})")
        messagebox.showinfo("Success", f"Device saved:\n{device.name}\n{device.address}")

    # ------------------------------------------------------------------
    # Save Settings
    # ------------------------------------------------------------------

    def save_settings(self):
        """Saves settings from the 'Settings' tab to the config file."""
        mac = self.settings_mac.get().strip()
        name = self.settings_name.get().strip()
        try:
            interval = int(self.settings_interval.get().strip())
            if interval < 1:
                raise ValueError
        except ValueError:
            messagebox.showerror("Error", "Please enter a valid number for the interval (≥ 1).")
            return

        self.config["mac_address"] = mac
        self.config["device_name"] = name
        self.config["poll_interval"] = interval
        save_config(self.config)

        self.log_message("✅ Settings saved.")
        messagebox.showinfo("Success", "Settings saved.")

    # ------------------------------------------------------------------
    # Apply Settings (Send to Charger)
    # ------------------------------------------------------------------

    def apply_settings(self):
        """
        Called when the user clicks the "Apply" button.

        Reads the values from the GUI, validates them against battery‑specific
        and model-specific limits, and sends them to the charger via set_worktask().

        A beep confirms that the settings were sent.
        """
        if not self.device or not self.device.connected:
            messagebox.showerror("Error", "No device connected.")
            return
        try:
            slot = int(self.slot_var.get()) - 1
            batt_str = self.battery_type_var.get()
            batt_int = BATTERY_TYPE_STR_TO_INT.get(batt_str)
            if batt_int is None:
                raise ValueError(f"Unknown battery type: {batt_str}")
            current_mA = int(self.current_entry.get())
            capacity_mAh = int(self.capacity_entry.get())
            cutoff_mV = int(self.cutoff_entry.get()) if self.cutoff_entry.cget('state') != 'disabled' else 0

            # --- Model-specific validation ---
            # Check if battery type is supported by this model
            if batt_str not in self.device.supported_battery_types:
                raise ValueError(
                    f"Battery type {batt_str} is not supported by {self.device.model_key}.\n"
                    f"Supported types: {', '.join(self.device.supported_battery_types)}"
                )

            # Check if current exceeds model maximum
            if current_mA > self.device.max_current_mA:
                raise ValueError(
                    f"Current {current_mA}mA exceeds model maximum {self.device.max_current_mA}mA"
                )

            # --- Battery‑specific validation ---
            limits = BATTERY_LIMITS.get(batt_str)
            if limits is None:
                raise ValueError(f"Unknown battery type: {batt_str}")

            # Capacity limit validation
            cap_min = limits["capacity_min"]
            cap_max = limits["capacity_max"]
            if cap_min > 0 or cap_max > 0:
                if capacity_mAh != 0 and (capacity_mAh < cap_min or capacity_mAh > cap_max):
                    raise ValueError(
                        f"Capacity limit for {batt_str} must be 0 (unlimited) or between {cap_min} and {cap_max} mAh."
                    )

            # Cut‑off validation (only if enabled for this battery type)
            if limits["cutoff_enabled"]:
                if cutoff_mV < limits["cutoff_min"] or cutoff_mV > limits["cutoff_max"]:
                    raise ValueError(
                        f"Cut‑off for {batt_str} must be between {limits['cutoff_min']} and {limits['cutoff_max']} mV."
                    )
            else:
                if cutoff_mV != 0:
                    raise ValueError(f"{batt_str} has no cut‑off setting. Please set it to 0.")
                cutoff_mV = 0

            # Global current validation
            if current_mA < CURRENT_MIN_MA or current_mA > CURRENT_MAX_MA:
                raise ValueError(
                    f"Current must be between {CURRENT_MIN_MA} mA (0.1 A) and {CURRENT_MAX_MA} mA (2.0 A)."
                )

            # Beep as confirmation
            self.root.bell()

            # Send the command asynchronously
            future = asyncio.run_coroutine_threadsafe(
                self.device.set_worktask(
                    channel=slot,
                    battery_type=batt_int,
                    work_current_mA=current_mA,
                    capacity_limit_mAh=capacity_mAh,
                    full_charged_volt=cutoff_mV
                ),
                self.loop
            )

            try:
                success = future.result(timeout=5.0)
                if success:
                    self.log_message(f"⚡ Settings for Slot {slot+1} sent: "
                                     f"{batt_str}, {current_mA} mA, {capacity_mAh} mAh, cut‑off {cutoff_mV} mV")
                    messagebox.showinfo("Sent", f"Settings for Slot {slot+1} have been sent.")
                else:
                    raise Exception("Charger rejected the settings")
            except Exception as e:
                self.log_message(f"⚠️ Failed to send settings: {e}")
                messagebox.showerror("Error", f"Failed to send settings: {e}")

        except Exception as e:
            messagebox.showerror("Input error", str(e))


# ------------------------------------------------------------------
# Application Entry Point
# ------------------------------------------------------------------

if __name__ == "__main__":
    root = tk.Tk()

    # Load icon from the same directory (optional)
    import os
    icon_path = os.path.join(os.path.dirname(__file__), 'icon.ico')
    
    # Windows: iconbitmap für .ico (besseres Taskleisten-Icon)
    if sys.platform == "win32":
        try:
            if os.path.exists(icon_path):
                root.iconbitmap(default=icon_path)
        except Exception:
            pass
    else:
        # Linux/Mac: PNG mit PhotoImage (Pillow benötigt)
        try:
            png_path = os.path.join(os.path.dirname(__file__), 'icon.png')
            if os.path.exists(png_path):
                from PIL import Image, ImageTk
                image = Image.open(png_path)
                icon = ImageTk.PhotoImage(image)
                root.iconphoto(True, icon)
                root.icon_image = icon
        except Exception:
            pass

    app = ISDTGui(root)
    root.mainloop()