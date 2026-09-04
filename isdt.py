#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
ISDT Charger – Monitor & Control

This is the main GUI application for ISDT C4/A4/A8/NP2 Air chargers.
It provides real-time monitoring of all charging slots and full control
over charging parameters (battery type, current, capacity limit, cut‑off).

Supports: C4 Air, A4 Air, A8 Air, NP2 Air

Key features:
- BLE connection management (connect/disconnect/auto‑connect)
- Live data display (voltage, current, capacity, IR, charge time, status)
- Parameter control with battery‑specific validation
- Alarm tone toggle
- Persistent configuration (MAC, model selection, poll interval, bind UUID)
- Adaptive polling (longer intervals when idle)
- GUI caching (only redraws when data changes)
- Manual model selection (user chooses model in settings)
- Multi-device support (save and switch between multiple chargers)

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
# VERSION
# ============================================================
APP_VERSION = "1.0.2"

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
from isdt_config import load_config, save_config, get_active_device, get_device_by_mac, add_device, remove_device, set_active_device, get_device_poll_interval
from isdt_models import (
    get_model_config,
    get_default_current,
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
        self._selected_device_index = None  # Currently selected device in settings

        # Widget references for dynamic updates
        self.slot_combo = None
        self.battery_combo = None
        self.cutoff_label = None       # Reference for cut-off label

        # Configure ttk styles
        style = ttk.Style()
        style.theme_use('clam')
        # Red, bold "Apply" button to make it stand out as the primary action
        style.configure("Red.TButton", foreground="red", font=('Helvetica', 10, 'bold'))
        # Grayed out style for disabled entries
        style.configure("Gray.TEntry", fieldbackground="lightgray")

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

        # Auto-connect if active device exists
        active = get_active_device(self.config)
        if active:
            mac = active.get("mac_address")
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
        - Data table with all values
        - Slot Settings panel (one row)
        - Log window for status messages
        """
        # ------------------------------------------------------------------
        # Top bar
        # ------------------------------------------------------------------
        frame_top = ttk.Frame(self.tab_device)
        frame_top.pack(pady=5, fill=tk.X)

        # Left section: Status, connection buttons, model info
        left_frame = ttk.Frame(frame_top)
        left_frame.pack(side=tk.LEFT)

        # Connection status
        self.status_label = ttk.Label(left_frame, text="No device connected", foreground="gray")
        self.status_label.pack(side=tk.LEFT, padx=5)

        # Input voltage (from Slot 1 Electric response)
        self.input_voltage_label = ttk.Label(left_frame, text="🔌 Input voltage: -- V", foreground="blue")
        self.input_voltage_label.pack(side=tk.LEFT, padx=15)

        # Total power (input voltage × input current)
        self.total_power_label = ttk.Label(left_frame, text="⚡ Total power: -- W", foreground="green")
        self.total_power_label.pack(side=tk.LEFT, padx=15)

        # Connect/Disconnect buttons
        self.connect_btn = ttk.Button(left_frame, text="Connect", command=self.connect_saved)
        self.connect_btn.pack(side=tk.LEFT, padx=2)

        self.disconnect_btn = ttk.Button(left_frame, text="Disconnect", command=self.disconnect_device, state=tk.DISABLED)
        self.disconnect_btn.pack(side=tk.LEFT, padx=2)

        # Alarm tone toggle button (speaker icon)
        self.alarm_btn = ttk.Button(left_frame, text="🔊", width=4, command=self.toggle_alarm_tone)
        self.alarm_btn.pack(side=tk.LEFT, padx=5)

        # Model info label (displays detected model)
        self.model_label = ttk.Label(left_frame, text="Model: --", foreground="purple")
        self.model_label.pack(side=tk.LEFT, padx=15)

        # ------------------------------------------------------------------
        # Data table
        # ------------------------------------------------------------------
        columns = (
            "Slot", "Status", "Type", "Voltage (V)", "Current (A)",
            "Max Current (mA)", "Capacity (mAh)", "IR (mΩ)", "Charge Time",
            "Cut-off (mV)", "Cap Limit (mAh)", "Charge Level"
        )
        self.tree = ttk.Treeview(self.tab_device, columns=columns, show="headings")

        col_widths = {
            "Slot": 45,
            "Status": 130,
            "Type": 75,
            "Voltage (V)": 85,
            "Current (A)": 85,
            "Max Current (mA)": 120,
            "Capacity (mAh)": 90,
            "IR (mΩ)": 65,
            "Charge Time": 90,
            "Cut-off (mV)": 85,
            "Cap Limit (mAh)": 95,
            "Charge Level": 180,
        }
        
        # Smaller font for column headings
        style = ttk.Style()
        style.configure("Treeview.Heading", font=('Helvetica', 8, 'bold'))
        
        for col in columns:
            self.tree.heading(col, text=col)
            self.tree.column(col, width=col_widths.get(col, 90), anchor="center", minwidth=40)
        self.tree.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        # Click on table row → automatically select the slot and load settings
        self.tree.bind("<<TreeviewSelect>>", self.on_tree_select)

        # ------------------------------------------------------------------
        # Slot Settings panel - one row
        # ------------------------------------------------------------------
        settings_frame = ttk.LabelFrame(self.tab_device, text="🔧 Slot Settings")
        settings_frame.pack(fill=tk.X, padx=10, pady=5)

        # Slot
        ttk.Label(settings_frame, text="Slot:").grid(row=0, column=0, padx=5, pady=5)
        self.slot_var = tk.StringVar(value="1")
        self.slot_combo = ttk.Combobox(settings_frame, textvariable=self.slot_var,
                                       values=[str(i) for i in range(1, 7)], width=5)
        self.slot_combo.grid(row=0, column=1, padx=5)
        self.slot_combo.bind("<<ComboboxSelected>>", lambda e: self.update_settings_fields())

        # Battery Type
        ttk.Label(settings_frame, text="Battery Type:").grid(row=0, column=2, padx=5)
        self.battery_type_var = tk.StringVar()
        self.battery_combo = ttk.Combobox(settings_frame, textvariable=self.battery_type_var,
                                          values=[], width=12)
        self.battery_combo.grid(row=0, column=3, padx=5)
        self.battery_combo.set("Auto")
        self.battery_combo.bind("<<ComboboxSelected>>", self._on_battery_type_changed)

        # Max Current (mA)
        ttk.Label(settings_frame, text="Max Current (mA):").grid(row=0, column=4, padx=5)
        self.current_entry = ttk.Entry(settings_frame, width=8)
        self.current_entry.grid(row=0, column=5, padx=5)
        self.current_entry.insert(0, "300")
        self._create_tooltip(self.current_entry, "Enter max current in mA\nDefault: 300 mA")

        # Cap Limit (mAh)
        ttk.Label(settings_frame, text="Cap Limit (mAh):").grid(row=0, column=6, padx=5)
        self.capacity_entry = ttk.Entry(settings_frame, width=8)
        self.capacity_entry.grid(row=0, column=7, padx=5)
        self.capacity_entry.insert(0, "no limit")
        self._create_tooltip(self.capacity_entry, "Enter limit in mAh, or use 0 or 'no limit' for unlimited\nDefault depends on battery type")

        # Cut-off (mV) - Label with reference for dynamic updates
        self.cutoff_label = ttk.Label(settings_frame, text="Cut-off (mV):")
        self.cutoff_label.grid(row=0, column=8, padx=5)
        self.cutoff_entry = ttk.Entry(settings_frame, width=6)
        self.cutoff_entry.grid(row=0, column=9, padx=5)
        self.cutoff_entry.insert(0, "0")
        self.cutoff_entry.bind("<KeyRelease>", self._update_cutoff_delta_label)
        self.cutoff_entry.bind("<FocusOut>", self._update_cutoff_delta_label)
        self._create_tooltip(self.cutoff_entry, "Enter cut-off voltage in mV\nDefault depends on battery type")

        # Apply button
        set_btn = ttk.Button(settings_frame, text="Apply", style="Red.TButton", command=self.apply_settings)
        set_btn.grid(row=0, column=10, padx=10, pady=5)

        # ------------------------------------------------------------------
        # Log window
        # ------------------------------------------------------------------
        self.log = scrolledtext.ScrolledText(self.tab_device, height=6, state='disabled')
        self.log.pack(fill=tk.X, padx=10, pady=5)

    def _create_tooltip(self, widget, text):
        """
        Creates a simple tooltip for a widget.
        
        Args:
            widget: The tkinter widget
            text: The tooltip text to display
        """
        def show_tooltip(event):
            if hasattr(widget, '_tooltip'):
                widget._tooltip.destroy()
                del widget._tooltip
            
            tooltip = tk.Toplevel(widget)
            tooltip.wm_overrideredirect(True)
            x = widget.winfo_rootx() + 10
            y = widget.winfo_rooty() + widget.winfo_height() + 5
            tooltip.wm_geometry(f"+{x}+{y}")
            
            label = tk.Label(
                tooltip, 
                text=text, 
                background="#ffffe0", 
                relief="solid", 
                borderwidth=1,
                justify="left",
                font=("Helvetica", 9)
            )
            label.pack()
            widget._tooltip = tooltip

        def hide_tooltip(event):
            if hasattr(widget, '_tooltip'):
                widget._tooltip.destroy()
                del widget._tooltip

        widget.bind("<Enter>", show_tooltip)
        widget.bind("<Leave>", hide_tooltip)

    def _build_settings_tab(self):
        """
        Builds the 'Settings' tab – with multi-device support.
        
        Left column: Scan area + Device list
        Right column: Settings for selected device
        """
        main_frame = ttk.Frame(self.tab_settings, padding=10)
        main_frame.pack(fill=tk.BOTH, expand=True)

        # ---- Left column: Scan + Device List ----
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

        # Buttons for scan results
        btn_frame = ttk.Frame(left_frame)
        btn_frame.pack(anchor=tk.W, pady=5)
        
        self.add_device_btn = ttk.Button(btn_frame, text="Add Device",
                                         command=self.add_selected_device, state=tk.DISABLED)
        self.add_device_btn.pack(side=tk.LEFT, padx=2)

        ttk.Label(left_frame, text="📱 Saved Devices:", font=("", 10, "bold")).pack(anchor=tk.W, pady=(15, 5))

        # Device list with scrollbar
        device_frame = ttk.Frame(left_frame)
        device_frame.pack(fill=tk.X, pady=5)
        
        self.saved_devices_listbox = tk.Listbox(device_frame, height=6, width=40)
        self.saved_devices_listbox.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.saved_devices_listbox.bind("<<ListboxSelect>>", self.on_saved_device_select)
        
        scrollbar = ttk.Scrollbar(device_frame, orient=tk.VERTICAL, command=self.saved_devices_listbox.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.saved_devices_listbox.config(yscrollcommand=scrollbar.set)

        # Buttons for saved devices
        saved_btn_frame = ttk.Frame(left_frame)
        saved_btn_frame.pack(anchor=tk.W, pady=5)
        
        self.select_device_btn = ttk.Button(saved_btn_frame, text="Select Device",
                                            command=self.select_saved_device, state=tk.DISABLED)
        self.select_device_btn.pack(side=tk.LEFT, padx=2)
        
        self.delete_device_btn = ttk.Button(saved_btn_frame, text="Delete Device",
                                            command=self.delete_saved_device, state=tk.DISABLED)
        self.delete_device_btn.pack(side=tk.LEFT, padx=2)

        # ---- Right column: Device Settings ----
        right_frame = ttk.Frame(main_frame)
        right_frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=(10, 0))

        ttk.Label(right_frame, text="💾 Device Settings:", font=("", 10, "bold")).pack(anchor=tk.W, pady=(0, 10))

        # Device name
        ttk.Label(right_frame, text="Device Name:").pack(anchor=tk.W, pady=(5, 0))
        self.settings_name = ttk.Entry(right_frame, width=30)
        self.settings_name.pack(anchor=tk.W, pady=2)

        # MAC address (read-only)
        ttk.Label(right_frame, text="MAC Address:").pack(anchor=tk.W, pady=(5, 0))
        self.settings_mac = ttk.Entry(right_frame, width=30, state="readonly")
        self.settings_mac.pack(anchor=tk.W, pady=2)

        # Model selection
        ttk.Label(right_frame, text="Model:").pack(anchor=tk.W, pady=(10, 0))
        self.settings_model = ttk.Combobox(
            right_frame,
            values=["C4 Air", "A4 Air", "A8 Air", "NP2 Air"],
            width=25,
            state="readonly"
        )
        self.settings_model.pack(anchor=tk.W, pady=2)

        # Poll interval (per device)
        ttk.Label(right_frame, text="Poll interval (s):").pack(anchor=tk.W, pady=(10, 0))
        self.settings_interval = ttk.Entry(right_frame, width=10)
        self.settings_interval.pack(anchor=tk.W, pady=2)
        self.settings_interval.insert(0, "2")

        # Save button
        save_settings_btn = ttk.Button(right_frame, text="Save Device Settings", command=self.save_device_settings)
        save_settings_btn.pack(anchor=tk.W, pady=20)

        # ---- Version ----
        version_label = ttk.Label(right_frame, text=f"Version: {APP_VERSION}", foreground="gray")
        version_label.pack(anchor=tk.W, pady=(20, 0))

        # Populate device lists
        self._refresh_device_lists()

    def _refresh_device_lists(self):
        """Refresh the saved devices list and select the active device."""
        self.saved_devices_listbox.delete(0, tk.END)
        devices = self.config.get("devices", [])
        active_idx = self.config.get("active_device")
        
        for i, device in enumerate(devices):
            name = device.get("name", device.get("selected_model", "ISDT"))
            mac = device.get("mac_address", "")
            prefix = "▶ " if i == active_idx else "  "
            display = f"{prefix}{name} ({mac})"
            self.saved_devices_listbox.insert(tk.END, display)
        
        # Show active device settings
        if active_idx is not None and 0 <= active_idx < len(devices):
            self._show_device_settings(active_idx)
            self.select_device_btn.config(state=tk.DISABLED)
        else:
            self._clear_device_settings()

    def _show_device_settings(self, index):
        """Display settings for a specific device."""
        devices = self.config.get("devices", [])
        if 0 <= index < len(devices):
            device = devices[index]
            self.settings_name.delete(0, tk.END)
            self.settings_name.insert(0, device.get("name", device.get("selected_model", "")))
            
            self.settings_mac.config(state="normal")
            self.settings_mac.delete(0, tk.END)
            self.settings_mac.insert(0, device.get("mac_address", ""))
            self.settings_mac.config(state="readonly")
            
            self.settings_model.set(device.get("selected_model", "C4 Air"))
            self.settings_interval.delete(0, tk.END)
            self.settings_interval.insert(0, str(device.get("poll_interval", 2)))
            self._selected_device_index = index

    def _clear_device_settings(self):
        """Clear the device settings fields."""
        self.settings_name.delete(0, tk.END)
        self.settings_mac.config(state="normal")
        self.settings_mac.delete(0, tk.END)
        self.settings_mac.config(state="readonly")
        self.settings_model.set("")
        self.settings_interval.delete(0, tk.END)
        self.settings_interval.insert(0, "2")
        self._selected_device_index = None

    # ------------------------------------------------------------------
    # Device Management
    # ------------------------------------------------------------------

    def add_selected_device(self):
        """Add a scanned device to the saved devices list."""
        selection = self.device_listbox.curselection()
        if not selection:
            return
        
        idx = selection[0]
        device = self.scanned_devices[idx]
        
        # Check if device already exists
        existing = get_device_by_mac(self.config, device.address)
        if existing:
            messagebox.showinfo("Info", f"Device {device.name} is already in the list.")
            return
        
        # Add device with default poll interval
        index = add_device(self.config, device.address, "C4 Air", device.name, 2)
        save_config(self.config)
        
        self.log_message(f"✅ Added: {device.name} ({device.address})")
        self._refresh_device_lists()
        
        # Select the new device
        self.saved_devices_listbox.selection_clear(0, tk.END)
        self.saved_devices_listbox.selection_set(index)
        self.saved_devices_listbox.see(index)
        self.on_saved_device_select()

    def on_saved_device_select(self, event=None):
        """Handle selection of a saved device."""
        selection = self.saved_devices_listbox.curselection()
        if selection:
            idx = selection[0]
            devices = self.config.get("devices", [])
            if 0 <= idx < len(devices):
                self._show_device_settings(idx)
                self.select_device_btn.config(state=tk.NORMAL)
                self.delete_device_btn.config(state=tk.NORMAL)
                
                # Check if this is the active device
                if self.config.get("active_device") == idx:
                    self.select_device_btn.config(state=tk.DISABLED, text="✅ Selected")
                else:
                    self.select_device_btn.config(state=tk.NORMAL, text="Select Device")
        else:
            self.select_device_btn.config(state=tk.DISABLED)
            self.delete_device_btn.config(state=tk.DISABLED)

    def select_saved_device(self):
        """Select a saved device as the active device."""
        selection = self.saved_devices_listbox.curselection()
        if not selection:
            return
        
        idx = selection[0]
        devices = self.config.get("devices", [])
        if 0 <= idx < len(devices):
            # Disconnect current device if any
            self.disconnect_device()
            
            # Set active device
            set_active_device(self.config, idx)
            save_config(self.config)
            
            self.log_message(f"📱 Selected device: {devices[idx].get('name', '')} ({devices[idx].get('mac_address', '')})")
            self._refresh_device_lists()
            
            # Auto-connect
            self.root.after(500, self.auto_connect)

    def delete_saved_device(self):
        """Delete a saved device from the list."""
        selection = self.saved_devices_listbox.curselection()
        if not selection:
            return
        
        idx = selection[0]
        devices = self.config.get("devices", [])
        if 0 <= idx < len(devices):
            device = devices[idx]
            
            if not messagebox.askyesno("Delete Device", 
                                       f"Delete device '{device.get('name', '')}'?\n{device.get('mac_address', '')}"):
                return
            
            # Disconnect if this is the active device
            if self.config.get("active_device") == idx:
                self.disconnect_device()
            
            remove_device(self.config, idx)
            save_config(self.config)
            
            self.log_message(f"🗑️ Deleted: {device.get('name', '')}")
            self._refresh_device_lists()
            self._clear_device_settings()

    def save_device_settings(self):
        """Save the settings for the selected device."""
        if self._selected_device_index is None:
            messagebox.showerror("Error", "No device selected.")
            return
        
        name = self.settings_name.get().strip()
        selected_model = self.settings_model.get().strip()
        
        if not name:
            messagebox.showerror("Error", "Device name cannot be empty.")
            return
        
        try:
            interval = int(self.settings_interval.get().strip())
            if interval < 2:
                raise ValueError
        except ValueError:
            messagebox.showerror("Error", "Please enter a valid number for the interval (≥ 2 seconds).")
            return
        
        # Update device
        devices = self.config.get("devices", [])
        if 0 <= self._selected_device_index < len(devices):
            devices[self._selected_device_index]["name"] = name
            devices[self._selected_device_index]["selected_model"] = selected_model
            devices[self._selected_device_index]["poll_interval"] = interval
        
        save_config(self.config)
        
        self.log_message(f"✅ Settings saved for {name}")
        self._refresh_device_lists()
        messagebox.showinfo("Success", "Device settings saved.")

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
                self._on_battery_type_changed()

        # Log model details
        self.log_message(f"📊 Model: {model_config['display_name']} ({num_slots} slots, max {model_config['max_current_mA']}mA)")

        # Update alarm button state based on model support
        if hasattr(self.device, 'supports_alarm') and not self.device.supports_alarm:
            self.alarm_btn.config(state=tk.DISABLED, text="🔇")
        else:
            self.alarm_btn.config(state=tk.NORMAL)

        # A8 Air specific hint
        if hasattr(self.device, 'is_a8') and self.device.is_a8:
            self.log_message("📊 A8 Air: Using mega-packet polling (all slots at once)")

        # Reset GUI cache for correct display
        self._last_table_values = []

    # ------------------------------------------------------------------
    # Logging & Status Updates
    # ------------------------------------------------------------------

    def log_message(self, msg):
        """Adds a message to the log window (with newline) and includes timestamp."""
        from datetime import datetime
        timestamp = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        self.log.config(state='normal')
        self.log.insert(tk.END, f"[{timestamp}] {msg}\n")
        self.log.see(tk.END)
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
            time.sleep(0.3)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Battery Type Change Handler
    # ------------------------------------------------------------------

    def _on_battery_type_changed(self, event=None):
        """
        Called when battery type is changed.
        - "Auto" → disable all input fields
        - Other types → set default values
        """
        batt_str = self.battery_type_var.get()
        is_auto = (batt_str == "Auto")
        
        # Enable/disable fields based on Auto mode
        if is_auto:
            self.current_entry.config(state='disabled', style="Gray.TEntry")
            self.capacity_entry.config(state='disabled', style="Gray.TEntry")
            self.cutoff_entry.config(state='disabled', style="Gray.TEntry")
            # Set values to "Auto"
            self.current_entry.delete(0, tk.END)
            self.current_entry.insert(0, "Auto")
            self.capacity_entry.delete(0, tk.END)
            self.capacity_entry.insert(0, "Auto")
            self.cutoff_entry.delete(0, tk.END)
            self.cutoff_entry.insert(0, "Auto")
        else:
            self.current_entry.config(state='normal', style="TEntry")
            self.capacity_entry.config(state='normal', style="TEntry")
            self.cutoff_entry.config(state='normal', style="TEntry")
            
            # Set default values for the selected battery type
            limits = BATTERY_LIMITS.get(batt_str)
            if limits:
                # Current: Default from model
                if self.device and self.device.model_key:
                    default_current = get_default_current(self.device.model_key)
                    self.current_entry.delete(0, tk.END)
                    self.current_entry.insert(0, str(default_current))
                
                # Cap Limit: Default from model
                if self.device:
                    default_capacity = self.device.model_config.get("default_capacity_mAh", 2000)
                    self.capacity_entry.delete(0, tk.END)
                    self.capacity_entry.insert(0, str(default_capacity))
                
                # Cut-off: Default from BATTERY_LIMITS
                default_cutoff = limits.get("cutoff_default", 0)
                if default_cutoff > 0:
                    self.cutoff_entry.delete(0, tk.END)
                    self.cutoff_entry.insert(0, str(default_cutoff))
                else:
                    self.cutoff_entry.delete(0, tk.END)
                    self.cutoff_entry.insert(0, "0")
        
        self._update_cutoff_delta_label()

    # ------------------------------------------------------------------
    # Cut‑off Field State
    # ------------------------------------------------------------------

    def _update_cutoff_delta_label(self, event=None):
        """
        Updates the cut-off label text based on current value.
        If value is between 1 and 5 mV, show (ΔmV), otherwise (mV).
        """
        if not hasattr(self, "cutoff_label"):
            return
        try:
            if self.cutoff_entry.cget("state") == "disabled":
                self.cutoff_label.config(text="Cut‑off (mV):")
                return
            raw = self.cutoff_entry.get().strip()
            if raw == "" or raw == "Auto" or raw.lower() == "no limit":
                self.cutoff_label.config(text="Cut‑off (mV):")
                return
            val = int(raw)
            if 0 < val < 6:
                self.cutoff_label.config(text="Cut‑off (ΔmV):")
            else:
                self.cutoff_label.config(text="Cut‑off (mV):")
        except ValueError:
            self.cutoff_label.config(text="Cut‑off (mV):")

    # ------------------------------------------------------------------
    # Connection Management
    # ------------------------------------------------------------------

    def auto_connect(self):
        """Automatic connection on startup."""
        active = get_active_device(self.config)
        if active:
            mac = active.get("mac_address")
            self.log_message(f"⏳ Auto-connecting to {mac} ...")
            self.device = ISDTBLE(active, log_callback=self.log_message, debug=False, config=self.config)
            asyncio.run_coroutine_threadsafe(self._connect_async(), self.loop)

    def connect_saved(self):
        """Manual connection using the saved device."""
        active = get_active_device(self.config)
        if not active:
            messagebox.showerror("Error", "No device selected. Please select a device in Settings.")
            return
        
        mac = active.get("mac_address")
        self.log_message(f"⏳ Connecting to {mac} ...")
        self._kill_blueman_connection(mac)
        self.device = ISDTBLE(active, log_callback=self.log_message, debug=False, config=self.config)
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
                # Reload config to get any changes made by other apps
                self.config = load_config()
                # Update BLE config if possible
                if hasattr(self.device, 'config'):
                    self.device.config = self.config

                # Status-Daten VOR dem Lambda berechnen
                active = get_active_device(self.config)
                display_name = (active.get("name") or active.get("selected_model") or "ISDT") if active else "----"

                self.root.after(0, lambda: self.log_message("✅ Connected!"))
                self.root.after(0, lambda n=display_name: self.update_status(f"Connected to {n}", "green"))
                self.root.after(0, lambda: self.connect_btn.config(state=tk.DISABLED))
                self.root.after(0, lambda: self.disconnect_btn.config(state=tk.NORMAL))
                self.root.after(0, self.update_gui_for_model)
                # Polling erst nach 500ms starten (Device muss bereit sein)
                self.root.after(500, self.start_polling)
                self.root.after(1000, self.update_settings_fields)
                await self._update_alarm_button()
            else:
                self.root.after(0, lambda: self.log_message("⚠️ Connection failed."))
        except Exception as e:
            self.root.after(0, lambda: self.log_message(f"⚠️ Error: {str(e)}"))

    def disconnect_device(self):
        """
        Disconnects from the charger and resets the GUI.

        Stops polling, disconnects BLE, clears the table, and resets buttons.
        """
        if self.device:
            # Stop polling first
            self.polling = False
            # Schedule disconnect - fire and forget (non-blocking)
            asyncio.run_coroutine_threadsafe(self.device.disconnect(), self.loop)
            # Give it a moment to start the disconnect
            time.sleep(0.1)
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
        if not self.device:
            self.log_message("⚠️ No device object.")
            return
        if not self.device.connected:
            self.log_message("⚠️ Device not connected.")
            return
        if self.polling:
            return
        self.polling = True
        
        # Use device-specific poll interval
        if hasattr(self.device, 'poll_interval'):
            interval = self.device.poll_interval
        else:
            interval = get_device_poll_interval(self.config)
        
        idle_interval = max(interval * 3, 10)  # Idle interval is 3x normal, at least 10s
        self.log_message(f"✅ Polling started (interval: {interval}s, idle: {idle_interval}s).....")
        asyncio.run_coroutine_threadsafe(self._poll_loop(interval, idle_interval), self.loop)

    async def _poll_loop(self, interval, idle_interval):
        """
        Asynchronous polling loop with adaptive interval.

        Args:
            interval: Normal polling interval in seconds (when slots are active)
            idle_interval: Polling interval when no slots are active (longer = less traffic)
        """
        # Wait for device to be ready
        for _ in range(10):  # 5 seconds max
            if self.device and self.device.connected:
                break
            await asyncio.sleep(0.5)
        
        if not self.device or not self.device.connected:
            self.log_message("⚠️ Device not ready, polling cancelled.")
            self.polling = False
            return
        
        while self.polling and self.device and self.device.connected:
            still_connected = await self.device.poll_data()
            self.root.after(0, self.update_table)

            if not still_connected:
                self.polling = False
                if sys.platform == "win32":
                    self.root.after(0, lambda: self.log_message(
                        "ℹ️ Connection dropped (typical for A8 under Windows) – reconnecting in 8s..."
                    ))
                    self.root.after(8000, self.connect_saved)
                else:
                    self.root.after(0, lambda: self.log_message(
                        "ℹ️ Connection lost – reconnecting..."
                    ))
                    self.root.after(1000, self.connect_saved)
                break

            await asyncio.sleep(interval if self.device._last_occupied_slots else idle_interval)

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

        if not self.device or not self.device.connected:
            if self._last_table_values:
                for item in self.tree.get_children():
                    self.tree.delete(item)
                self._last_table_values = []
            return

        input_voltage_mV = 0
        input_current_mA = 0
        now = time.time()
        elec1 = self.device.latest_data.get("slot1_electric", {})
        if elec1.get("input_voltage_mV", 0) > 0:
            input_voltage_mV = elec1["input_voltage_mV"]
            input_current_mA = elec1.get("input_current_mA", 0)

        num_slots = self.device.num_slots
        is_a8 = self.device.is_a8

        for slot in range(1, num_slots + 1):
            work = self.device.latest_data.get(f"slot{slot}_workstate", {})
            elec = self.device.latest_data.get(f"slot{slot}_electric", {})
            ir = self.device.latest_data.get(f"slot{slot}_ir", {})

            # If no WorkState exists → empty slot with zeros
            if not work:
                row = (slot, "idle", "Auto", "0.000", "0.00",
                       0, 0, 0, "--:--", "0", "no limit", "—")
                new_values.append(row)
                continue

            status = work.get("status_str", "idle")
            is_idle = status in ("idle", "empty") or work.get("status", 0) == 0

            # Battery type from WorkState
            batt_type = work.get("battery_type", -1)
            if batt_type >= 0:
                inv_map = {v: k for k, v in BATTERY_TYPE_STR_TO_INT.items()}
                batt_str = inv_map.get(batt_type, "Auto")
            else:
                batt_str = "Auto"
            
            battery_type = work.get("battery_type_str", "Auto")

            # --- Max Current - always from WorkState ---
            max_current = work.get("max_current_mA", 0)
            max_current_display = str(max_current) if max_current > 0 else "0"

            # --- Cap Limit - show for all models (now available for A8 Air via A8TaskResp) ---
            cap_limit = work.get("max_output_power_mW", 0)
            cap_limit_display = str(cap_limit) if cap_limit > 0 else "no limit"

            # --- Cut-off - always from WorkState (even in Auto!) ---
            cutoff_mV = work.get("full_charged_volt_mV", 0)
            if cutoff_mV > 0:
                if cutoff_mV < 6:
                    cutoff_display = f"{cutoff_mV} (ΔmV)"
                else:
                    cutoff_display = str(cutoff_mV)
            else:
                cutoff_display = "0"

            if is_idle:
                # Idle slot → no further data
                voltage_V = 0.0
                current_A = 0.0
                capacity = 0
                ir_val = 0
                capacity_percent = 0
                charge_time_str = "--:--"
                battery_display = "—"
                
                if slot in self.charge_start_times:
                    del self.charge_start_times[slot]
            else:
                # --- Active slot ---
                
                # --- Voltage ---
                voltage_mV = elec.get("voltage_mV", 0)
                if voltage_mV == 0:
                    voltage_mV = work.get("voltage_mV", 0)
                voltage_V = voltage_mV / 1000.0

                # --- Current (actual charging current) - set to 0 when "done" ---
                if status == "done":
                    current_A = 0.0
                else:
                    current_mA = elec.get("charging_current_mA", 0)
                    if current_mA == 0:
                        current_mA = work.get("max_current_mA", 0)
                    current_A = current_mA / 1000.0

                # --- Capacity ---
                capacity_percent = work.get("capacity_percent", 0)
                # Capacity is only available in WorkState, not Electric response
                capacity = work.get("capacity_mAh", 0)

                # --- Internal resistance ---
                ir_val = ir.get("ir_total_mohm", work.get("ir_mohm", 0))
                if ir_val == 0 and "ir_values_mohm" in ir:
                    ir_val = ir["ir_total_mohm"]
                ir_val = round(ir_val)

                # --- Charge time ---
                work_period_ms = work.get("work_period_ms", 0)
                if work_period_ms > 0:
                    charge_time_str = self._format_time(work_period_ms / 1000.0)
                else:
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

                battery_display = self._battery_bar(capacity_percent)

            # New column order
            row = (slot, status, battery_type, f"{voltage_V:.3f}", f"{current_A:.2f}",
                   max_current_display, capacity, ir_val, charge_time_str,
                   cutoff_display, cap_limit_display, battery_display)
            new_values.append(row)

        if new_values == self._last_table_values:
            return

        for item in self.tree.get_children():
            self.tree.delete(item)
        for row in new_values:
            self.tree.insert("", tk.END, values=row)
        self._last_table_values = new_values

        if input_voltage_mV > 0:
            self.root.after(0, lambda: self.update_device_info(input_voltage_mV, input_current_mA))

    # ------------------------------------------------------------------
    # Slot Selection
    # ------------------------------------------------------------------

    def on_tree_select(self, event):
        selection = self.tree.selection()
        if selection:
            item = selection[0]
            values = self.tree.item(item, 'values')
            if values and len(values) > 2:
                slot = values[0]
                self.slot_var.set(str(slot))
                if len(values) > 2 and values[2] and values[2] != "-" and values[2] != "—":
                    batt_str = values[2]
                    if self.device and batt_str in getattr(self.device, "supported_battery_types", []):
                        self.battery_type_var.set(batt_str)
                        self._on_battery_type_changed()
                self.update_settings_fields()

    def update_settings_fields(self):
        """
        Loads charging settings for the selected slot.
        Always shows default values (actual values are in the table).
        """
        if not self.device or not self.device.connected:
            return
        
        try:
            slot = int(self.slot_var.get()) - 1
            work = self.device.latest_data.get(f"slot{slot + 1}_workstate", {})
            
            # Read battery type from WorkState or use default
            batt_type = work.get("battery_type", -1) if work else -1
            if batt_type >= 0:
                inv_map = {v: k for k, v in BATTERY_TYPE_STR_TO_INT.items()}
                batt_str = inv_map.get(batt_type, "Auto")
            else:
                batt_str = "Auto"
            
            # Set battery type
            if batt_str in self.device.supported_battery_types:
                self.battery_type_var.set(batt_str)
            else:
                self.battery_type_var.set("Auto")
            
            # Configure fields based on Auto mode
            self._on_battery_type_changed()
            
            # If not Auto, set default values (overrides _on_battery_type_changed)
            if batt_str != "Auto":
                limits = BATTERY_LIMITS.get(batt_str)
                if limits:
                    # Current: Default from model
                    if self.device and self.device.model_key:
                        default_current = get_default_current(self.device.model_key)
                        self.current_entry.delete(0, tk.END)
                        self.current_entry.insert(0, str(default_current))
                    
                    # Cap Limit: Default from model
                    if self.device:
                        default_capacity = self.device.model_config.get("default_capacity_mAh", 2000)
                        self.capacity_entry.delete(0, tk.END)
                        self.capacity_entry.insert(0, str(default_capacity))
                    
                    # Cut-off: Default from BATTERY_LIMITS
                    default_cutoff = limits.get("cutoff_default", 0)
                    if default_cutoff > 0:
                        self.cutoff_entry.delete(0, tk.END)
                        self.cutoff_entry.insert(0, str(default_cutoff))
                    else:
                        self.cutoff_entry.delete(0, tk.END)
                        self.cutoff_entry.insert(0, "0")

            # --- Cut-off Label with delta-V support ---
            if hasattr(self, "cutoff_label") and work:
                cutoff_actual = work.get("full_charged_volt_mV", 0)
                if cutoff_actual > 0 and cutoff_actual < 6:
                    self.cutoff_label.config(text="Cut-off (ΔmV):")
                else:
                    self.cutoff_label.config(text="Cut-off (mV):")

            if hasattr(self, "_update_cutoff_delta_label"):
                self._update_cutoff_delta_label()
        except Exception as e:
            self.log_message(f"⚠️ update_settings_fields error: {e}")

    # ------------------------------------------------------------------
    # Alarm Tone
    # ------------------------------------------------------------------

    async def _update_alarm_button(self):
        if self.device and self.device.connected:
            if not self.device.supports_alarm:
                return
            state = await self.device.get_alarm_tone()
            if state is not None:
                self.root.after(0, lambda: self.alarm_btn.config(text="🔊" if state else "🔇"))

    def toggle_alarm_tone(self):
        if not self.device or not self.device.connected:
            messagebox.showerror("Error", "No device connected.")
            return
        if not self.device.supports_alarm:
            messagebox.showinfo("Info", "Alarm tone is not supported by this model.")
            return
        new_state = not self.device._alarm_tone_state
        asyncio.run_coroutine_threadsafe(self._toggle_alarm_async(new_state), self.loop)

    async def _toggle_alarm_async(self, new_state):
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
        if self.scanning:
            self.log_message("⚠️ Scan already in progress. Please wait.")
            return
        self.scanning = True
        self.scan_btn.config(state=tk.DISABLED)
        self.scan_status.config(text="Searching...", foreground="orange")
        self.device_listbox.delete(0, tk.END)
        self.log_message("🔎 Scanning for BLE devices (10 seconds)...")
        # Force GUI update
        self.root.update_idletasks()
        asyncio.run_coroutine_threadsafe(self._scan_async(), self.loop)

        # Safety timeout: re-enable scan button after 15 seconds
        self.root.after(15000, self._scan_timeout)

    def _scan_timeout(self):
        """Safety timeout for scan - re-enables scan button if scan hangs."""
        if self.scanning:
            self.scanning = False
            self.scan_btn.config(state=tk.NORMAL)
            self.scan_status.config(text="Scan timed out. Try again.", foreground="red")
            self.log_message("⚠️ Scan timed out. Please try again.")

    async def _scan_async(self):
        from bleak import BleakScanner
        scanner = None
        try:
            scanner = BleakScanner()
            devices = await scanner.discover(timeout=10)
            self.scanned_devices = [d for d in devices if d.name]
        except Exception as e:
            error_msg = str(e)
            if "InProgress" in error_msg:
                self.log_message("⚠️ Scan already in progress. Please wait a moment and try again.")
            else:
                self.log_message(f"⚠️ Scan error: {error_msg}")
            self.scanned_devices = []
        finally:
            if scanner:
                try:
                    await scanner.stop()
                except Exception:
                    pass
            self.scanning = False
            self.root.after(0, self._update_scan_results)

    def _update_scan_results(self):
        self.scan_btn.config(state=tk.NORMAL)
        self.device_listbox.delete(0, tk.END)
        for d in self.scanned_devices:
            self.device_listbox.insert(tk.END, f"{d.name} ({d.address})")
        
        if len(self.scanned_devices) == 0:
            self.scan_status.config(text="No devices found. Try scanning again.", foreground="orange")
            self.log_message("🔍 No devices found. Try scanning again - sometimes it takes multiple attempts.")
        else:
            self.scan_status.config(text=f"{len(self.scanned_devices)} devices found", foreground="green")
            self.log_message(f"✅ {len(self.scanned_devices)} devices found.")
        
        self.add_device_btn.config(state=tk.NORMAL if self.scanned_devices else tk.DISABLED)
        self.scanning = False

    def on_device_select(self, event):
        selection = self.device_listbox.curselection()
        self.add_device_btn.config(state=tk.NORMAL if selection else tk.DISABLED)

    # ------------------------------------------------------------------
    # Apply Settings (Send to Charger)
    # ------------------------------------------------------------------

    def apply_settings(self):
        if not self.device or not self.device.connected:
            messagebox.showerror("Error", "No device connected.")
            return
        try:
            slot = int(self.slot_var.get()) - 1
            batt_str = self.battery_type_var.get()
            batt_int = BATTERY_TYPE_STR_TO_INT.get(batt_str)
            if batt_int is None:
                raise ValueError(f"Unknown battery type: {batt_str}")

            current_raw = self.current_entry.get().strip()
            capacity_raw = self.capacity_entry.get().strip()
            cutoff_raw = self.cutoff_entry.get().strip()

            if current_raw == "Auto" or current_raw == "":
                current_mA = 0
            else:
                current_mA = int(current_raw)

            if capacity_raw == "no limit" or capacity_raw == "" or capacity_raw == "0":
                capacity_mAh = 0
            else:
                capacity_mAh = int(capacity_raw)

            if cutoff_raw == "Auto" or cutoff_raw == "" or self.cutoff_entry.cget('state') == "disabled":
                cutoff_mV = 0
            else:
                cutoff_mV = int(cutoff_raw)

            if batt_str == "Auto":
                self.log_message(f"⚡ Auto mode for Slot {slot+1}: Charger will detect and choose parameters automatically")
                asyncio.run_coroutine_threadsafe(
                    self.device.set_worktask(
                        channel=slot,
                        battery_type=6,
                        work_current_mA=0,
                        capacity_limit_mAh=0,
                        full_charged_volt=0
                    ),
                    self.loop
                )
                messagebox.showinfo("Auto Mode", f"Slot {slot+1} set to Auto.\nCharger will detect battery type and choose parameters automatically.")
                return

            if batt_str not in self.device.supported_battery_types:
                raise ValueError(
                    f"Battery type {batt_str} is not supported by {self.device.model_key}.\n"
                    f"Supported types: {', '.join(self.device.supported_battery_types)}"
                )

            if current_mA > self.device.max_current_mA:
                raise ValueError(
                    f"Current {current_mA}mA exceeds model maximum {self.device.max_current_mA}mA"
                )

            limits = BATTERY_LIMITS.get(batt_str)
            if limits is None:
                raise ValueError(f"Unknown battery type: {batt_str}")

            cap_min = limits["capacity_min"]
            cap_max = limits["capacity_max"]
            if cap_min > 0 or cap_max > 0:
                if capacity_mAh != 0 and (capacity_mAh < cap_min or capacity_mAh > cap_max):
                    raise ValueError(
                        f"Capacity limit for {batt_str} must be 0 (unlimited) or between {cap_min} and {cap_max} mAh."
                    )

            if limits["cutoff_enabled"]:
                if cutoff_mV < limits["cutoff_min"] or cutoff_mV > limits["cutoff_max"]:
                    raise ValueError(
                        f"Cut‑off for {batt_str} must be between {limits['cutoff_min']} and {limits['cutoff_max']} mV."
                    )
            else:
                if cutoff_mV != 0:
                    cutoff_mV = 0
                    self.log_message(f"ℹ️ {batt_str} has no cut‑off setting. Auto-set to 0.")

            if current_mA < CURRENT_MIN_MA or current_mA > CURRENT_MAX_MA:
                raise ValueError(
                    f"Current must be between {CURRENT_MIN_MA} mA (0.1 A) and {CURRENT_MAX_MA} mA (2.0 A)."
                )

            self.root.bell()

            asyncio.run_coroutine_threadsafe(
                self.device.set_worktask(
                    channel=slot,
                    battery_type=batt_int,
                    work_current_mA=current_mA,
                    capacity_limit_mAh=capacity_mAh,
                    full_charged_volt=cutoff_mV
                ),
                self.loop
            )

            self.log_message(f"⚡ Settings for Slot {slot+1} sent: "
                             f"{batt_str}, {current_mA} mA, {capacity_mAh} mAh, cut‑off {cutoff_mV} mV")
            messagebox.showinfo("Sent", f"Settings for Slot {slot+1} have been sent.")

        except ValueError as e:
            if "invalid literal" in str(e):
                messagebox.showerror("Input error", "Please enter valid numbers in all fields (or use 'Auto' for Auto mode).")
            else:
                messagebox.showerror("Input error", str(e))
        except Exception as e:
            messagebox.showerror("Input error", str(e))


# ------------------------------------------------------------------
# Application Entry Point
# ------------------------------------------------------------------

if __name__ == "__main__":
    root = tk.Tk()

    import os
    icon_path = os.path.join(os.path.dirname(__file__), 'icon.ico')
    
    if sys.platform == "win32":
        try:
            if os.path.exists(icon_path):
                root.iconbitmap(default=icon_path)
        except Exception:
            pass
    else:
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