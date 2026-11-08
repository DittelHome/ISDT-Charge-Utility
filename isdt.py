#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
ISDT Charger – Monitor & Control

Main GUI application for ISDT C4 / A4 / A8 Air chargers.

License: MIT
"""

import sys

# Windows WinRT CoInit fix – must run before any WinRT import
if sys.platform == "win32":
    sys.coinit_flags = 0

import asyncio
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
import threading
import time
import os

APP_VERSION = "1.0.4"

# Set a taskbar AppUserModelID so the icon shows correctly on Windows
if sys.platform == "win32":
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ISDT.Charge.Utility")
    except Exception:
        pass

from isdt_ble import (
    ISDTBLE,
    MAX_POLL_FAILURES,
    RECONNECT_DELAY_S,
    START_POLL_DELAY_MS,
)
from isdt_config import (
    load_config, save_config, get_active_device, get_device_by_mac,
    add_device, remove_device, set_active_device,
    get_debug_log_folder,
)
from isdt_models import (
    get_default_current, get_poll_interval,
    get_min_poll_interval, get_max_poll_interval,
    BATTERY_LIMITS,
    CURRENT_MIN_MA, CURRENT_MAX_MA, BATTERY_TYPE_STR_TO_INT, ISDT_MODELS,
)


class ISDTGui:
    """Main GUI class for the ISDT Charger Monitor."""

    def __init__(self, root):
        self.root = root
        self.root.title("ISDT Charger – Monitor & Control")
        self.root.protocol("WM_DELETE_WINDOW", self._on_closing)

        self.config = load_config()
        self.device = None

        self.scanning = False
        self.polling = False
        self.scanned_devices = []
        self.charge_start_times = {}
        self._last_table_values = []
        self._selected_device_index = None

        self._connect_start_time = 0
        self._heartbeat_timer_id = None
        self._closing = False
        self._reconnect_pending = False
        self._reconnect_after_id = None
        self._disconnect_future = None
        self._disconnect_device_ref = None
        self._disconnect_started_at = None
        self._disconnect_progress_after_id = None
        self._disconnect_completion_callback = None
        self._disconnect_last_log_second = 0

        self._debug_enabled = False
        self._debug_log_file = None

        style = ttk.Style()
        style.theme_use('clam')
        style.configure("Red.TButton", foreground="red", font=('Helvetica', 10, 'bold'))
        style.configure("Gray.TEntry", fieldbackground="lightgray")

        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        self.tab_device = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_device, text="Device")
        self._build_device_tab()

        self.tab_settings = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_settings, text="Settings")
        self._build_settings_tab()
        self._set_table_placeholders()

        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()

    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    # ------------------------------------------------------------------
    # Application shutdown
    # ------------------------------------------------------------------

    def _on_closing(self):
        """Gracefully stop polling, disconnect and shut down the asyncio loop."""
        self._closing = True
        self.polling = False
        self._stop_heartbeat()

        if self._debug_log_file:
            try:
                self._debug_log_file.close()
            except Exception:
                pass
            self._debug_log_file = None

        from datetime import datetime

        # Message 1: always shown
        ts = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        self.log.config(state='normal')
        self.log.insert(tk.END, f"[{ts}] ⏳ Closing application...\n")
        self.log.see(tk.END)
        self.log.config(state='disabled')
        try:
            self.root.update()
        except Exception:
            pass
        time.sleep(0.8)

        # Disconnect if connected
        if self.device and self.device.connected:
            self.device._expected_disconnect = True
            try:
                asyncio.run_coroutine_threadsafe(self.device.disconnect(), self.loop)
            except Exception:
                pass
            time.sleep(0.8)

            ts = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
            self.log.config(state='normal')
            self.log.insert(tk.END, f"[{ts}] ⏳ Disconnected before exit\n")
            self.log.see(tk.END)
            self.log.config(state='disabled')
            try:
                self.root.update()
            except Exception:
                pass
            time.sleep(0.8)

        # Cancel pending tasks
        try:
            async def _cancel_all():
                tasks = [t for t in asyncio.all_tasks()
                         if t is not asyncio.current_task() and not t.done()]
                for t in tasks:
                    t.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
            fut = asyncio.run_coroutine_threadsafe(_cancel_all(), self.loop)
            fut.result(timeout=2.0)
        except Exception:
            pass

        try:
            self.loop.call_soon_threadsafe(self.loop.stop)
        except Exception:
            pass

        time.sleep(0.2)
        self.root.destroy()

    # ------------------------------------------------------------------
    # Device tab
    # ------------------------------------------------------------------

    def _build_device_tab(self):
        # Top bar: status, input voltage, total power, buttons
        frame_top = ttk.Frame(self.tab_device)
        frame_top.pack(pady=5, fill=tk.X)

        left_frame = ttk.Frame(frame_top)
        left_frame.pack(side=tk.LEFT)

        self.status_label = ttk.Label(left_frame, text="No device connected", foreground="gray")
        self.status_label.pack(side=tk.LEFT, padx=5)

        self.input_voltage_label = ttk.Label(left_frame, text="🔌 Input voltage: -- V", foreground="blue")
        self.input_voltage_label.pack(side=tk.LEFT, padx=15)

        self.total_power_label = ttk.Label(left_frame, text="⚡ Total power: -- W", foreground="green")
        self.total_power_label.pack(side=tk.LEFT, padx=15)

        self.connect_btn = ttk.Button(left_frame, text="Connect", command=self.connect_saved)
        self.connect_btn.pack(side=tk.LEFT, padx=2)

        self.disconnect_btn = ttk.Button(left_frame, text="Disconnect",
                                         command=self.disconnect_device, state=tk.DISABLED)
        self.disconnect_btn.pack(side=tk.LEFT, padx=2)

        self.alarm_btn = ttk.Button(left_frame, text="🔊", width=4, command=self.toggle_alarm_tone)
        self.alarm_btn.pack(side=tk.LEFT, padx=5)

        self.debug_btn = ttk.Button(left_frame, text="DEBUG ON/OFF", width=13, command=self.toggle_debug)
        self.debug_btn.pack(side=tk.LEFT, padx=2)

        self.device_label = ttk.Label(left_frame, text="Selected: none", foreground="darkblue")
        self.device_label.pack(side=tk.LEFT, padx=15)

        # Main data table
        columns = (
            "Slot", "Status", "Type", "Voltage (V)", "Current (A)",
            "Max Current (mA)", "Capacity (mAh)", "IR (mΩ)", "Charge Time",
            "Cut-off (mV)", "Cap Limit (mAh)", "Charge Level"
        )
        self.tree = ttk.Treeview(self.tab_device, columns=columns, show="headings")

        col_widths = {
            "Slot": 45, "Status": 130, "Type": 75, "Voltage (V)": 85,
            "Current (A)": 85, "Max Current (mA)": 120, "Capacity (mAh)": 90,
            "IR (mΩ)": 80, "Charge Time": 90, "Cut-off (mV)": 85,
            "Cap Limit (mAh)": 95, "Charge Level": 180,
        }
        style = ttk.Style()
        style.configure("Treeview.Heading", font=('Helvetica', 8, 'bold'))
        for col in columns:
            self.tree.heading(col, text=col)
            self.tree.column(col, width=col_widths.get(col, 90), anchor="center", minwidth=40)
        self.tree.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        self.tree.bind("<<TreeviewSelect>>", self.on_tree_select)
        self.tree.bind("<Motion>", self._on_tree_motion)
        self.tree.bind("<Leave>", self._hide_tree_tooltip)
        self._ir_tooltip = None

        # Slot settings bar (battery type, current, capacity, cut-off)
        settings_frame = ttk.LabelFrame(self.tab_device, text="🔧 Slot Settings")
        settings_frame.pack(fill=tk.X, padx=10, pady=5)

        ttk.Label(settings_frame, text="Slot:").grid(row=0, column=0, padx=5, pady=5)
        self.slot_var = tk.StringVar(value="1")
        self.slot_combo = ttk.Combobox(settings_frame, textvariable=self.slot_var,
                                       values=[str(i) for i in range(1, 7)], width=5)
        self.slot_combo.grid(row=0, column=1, padx=5)
        self.slot_combo.bind("<<ComboboxSelected>>", lambda e: self.update_settings_fields())

        ttk.Label(settings_frame, text="Battery Type:").grid(row=0, column=2, padx=5)
        self.battery_type_var = tk.StringVar()
        self.battery_combo = ttk.Combobox(settings_frame, textvariable=self.battery_type_var,
                                          values=[], width=12)
        self.battery_combo.grid(row=0, column=3, padx=5)
        self.battery_combo.set("Auto")
        self.battery_combo.bind("<<ComboboxSelected>>", self._on_battery_type_changed)

        ttk.Label(settings_frame, text="Max Current (mA):").grid(row=0, column=4, padx=5)
        self.current_entry = ttk.Entry(settings_frame, width=8)
        self.current_entry.grid(row=0, column=5, padx=5)
        self.current_entry.insert(0, "300")

        ttk.Label(settings_frame, text="Cap Limit (mAh):").grid(row=0, column=6, padx=5)
        self.capacity_entry = ttk.Entry(settings_frame, width=8)
        self.capacity_entry.grid(row=0, column=7, padx=5)
        self.capacity_entry.insert(0, "no limit")

        self.cutoff_label = ttk.Label(settings_frame, text="Cut-off (mV):")
        self.cutoff_label.grid(row=0, column=8, padx=5)
        self.cutoff_entry = ttk.Entry(settings_frame, width=6)
        self.cutoff_entry.grid(row=0, column=9, padx=5)
        self.cutoff_entry.insert(0, "0")
        self.cutoff_entry.bind("<KeyRelease>", self._update_cutoff_delta_label)
        self.cutoff_entry.bind("<FocusOut>", self._update_cutoff_delta_label)

        set_btn = ttk.Button(settings_frame, text="Apply", style="Red.TButton", command=self.apply_settings)
        set_btn.grid(row=0, column=10, padx=10, pady=5)

        # Log output
        self.log = scrolledtext.ScrolledText(self.tab_device, height=6, state='disabled')
        self.log.pack(fill=tk.X, padx=10, pady=5)

    # ------------------------------------------------------------------
    # Settings tab
    # ------------------------------------------------------------------

    def _build_settings_tab(self):
        main_frame = ttk.Frame(self.tab_settings, padding=10)
        main_frame.pack(fill=tk.BOTH, expand=True)

        # Left side: scan + saved devices
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

        btn_frame = ttk.Frame(left_frame)
        btn_frame.pack(anchor=tk.W, pady=5)
        self.add_device_btn = ttk.Button(btn_frame, text="Add Device",
                                         command=self.add_selected_device, state=tk.DISABLED)
        self.add_device_btn.pack(side=tk.LEFT, padx=2)

        ttk.Label(left_frame, text="📱 Saved Devices:", font=("", 10, "bold")).pack(anchor=tk.W, pady=(15, 5))

        device_frame = ttk.Frame(left_frame)
        device_frame.pack(fill=tk.X, pady=5)
        self.saved_devices_listbox = tk.Listbox(device_frame, height=6, width=40)
        self.saved_devices_listbox.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.saved_devices_listbox.bind("<<ListboxSelect>>", self.on_saved_device_select)
        scrollbar = ttk.Scrollbar(device_frame, orient=tk.VERTICAL, command=self.saved_devices_listbox.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.saved_devices_listbox.config(yscrollcommand=scrollbar.set)

        saved_btn_frame = ttk.Frame(left_frame)
        saved_btn_frame.pack(anchor=tk.W, pady=5)
        self.select_device_btn = ttk.Button(saved_btn_frame, text="Select Device",
                                            command=self.select_saved_device, state=tk.DISABLED)
        self.select_device_btn.pack(side=tk.LEFT, padx=2)
        self.delete_device_btn = ttk.Button(saved_btn_frame, text="Delete Device",
                                            command=self.delete_saved_device, state=tk.DISABLED)
        self.delete_device_btn.pack(side=tk.LEFT, padx=2)

        # Right side: per-device settings
        right_frame = ttk.Frame(main_frame)
        right_frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=(10, 0))

        ttk.Label(right_frame, text="💾 Device Settings:", font=("", 10, "bold")).pack(anchor=tk.W, pady=(0, 10))

        ttk.Label(right_frame, text="Device Name:").pack(anchor=tk.W, pady=(5, 0))
        self.settings_name = ttk.Entry(right_frame, width=30)
        self.settings_name.pack(anchor=tk.W, pady=2)

        ttk.Label(right_frame, text="MAC Address:").pack(anchor=tk.W, pady=(5, 0))
        self.settings_mac = ttk.Entry(right_frame, width=30, state="readonly")
        self.settings_mac.pack(anchor=tk.W, pady=2)

        ttk.Label(right_frame, text="Model:").pack(anchor=tk.W, pady=(10, 0))
        self.settings_model = ttk.Combobox(right_frame,
                                           values=["C4 Air", "A4 Air", "A8 Air"],
                                           width=25, state="readonly")
        self.settings_model.pack(anchor=tk.W, pady=2)
        self.settings_model.bind("<<ComboboxSelected>>", self._on_model_changed)

        ttk.Label(right_frame, text="Poll interval (s):").pack(anchor=tk.W, pady=(10, 0))
        self.settings_interval = ttk.Entry(right_frame, width=10)
        self.settings_interval.pack(anchor=tk.W, pady=2)
        self.settings_interval.insert(0, "10")

        ttk.Label(right_frame, text="Heartbeat interval (min):").pack(anchor=tk.W, pady=(10, 0))
        self.settings_heartbeat = ttk.Entry(right_frame, width=10)
        self.settings_heartbeat.pack(anchor=tk.W, pady=2)
        self.settings_heartbeat.insert(0, "10")

        ttk.Label(right_frame, text="Debug log folder:").pack(anchor=tk.W, pady=(10, 0))
        folder_frame = ttk.Frame(right_frame)
        folder_frame.pack(anchor=tk.W, pady=2)
        self.settings_debug_folder = ttk.Entry(folder_frame, width=25)
        self.settings_debug_folder.pack(side=tk.LEFT, padx=(0, 5))
        self.browse_debug_btn = ttk.Button(folder_frame, text="...", width=3, command=self.browse_debug_folder)
        self.browse_debug_btn.pack(side=tk.LEFT)

        save_settings_btn = ttk.Button(right_frame, text="Save Device Settings", command=self.save_device_settings)
        save_settings_btn.pack(anchor=tk.W, pady=20)

        version_label = ttk.Label(right_frame, text=f"Version: {APP_VERSION}", foreground="gray")
        version_label.pack(anchor=tk.W, pady=(20, 0))

        self._refresh_device_lists()

    def browse_debug_folder(self):
        """Open a folder picker for the debug log folder."""
        from tkinter import filedialog
        current = self.settings_debug_folder.get().strip() or os.path.expanduser("~")
        folder = filedialog.askdirectory(title="Select Debug Log Folder", initialdir=current)
        if folder:
            self.settings_debug_folder.delete(0, tk.END)
            self.settings_debug_folder.insert(0, folder)

    # ------------------------------------------------------------------
    # Debug logging
    # ------------------------------------------------------------------

    def toggle_debug(self):
        """Enable or disable the BLE traffic debug log."""
        import datetime
        if self._debug_enabled:
            self._debug_enabled = False
            if self._debug_log_file:
                try:
                    self._debug_log_file.close()
                except Exception:
                    pass
                self._debug_log_file = None
            if self.device:
                self.device.debug_traffic = False
                self.device.debug_log_file = None
            self.debug_btn.config(text="DEBUG OFF")
            self.log_message("🐞 Debug logging stopped.")
        else:
            folder = get_debug_log_folder(self.config)
            filename = os.path.join(folder, f"isdt_debug_{datetime.datetime.now():%Y%m%d_%H%M%S}.log")
            try:
                self._debug_log_file = open(filename, "w", encoding="utf-8")
                self._debug_enabled = True
                if self.device:
                    self.device.debug_traffic = True
                    self.device.debug_log_file = self._debug_log_file
                self.debug_btn.config(text="!!DEBUG ON!!")
                self.log_message(f"🐞 Debug logging started: {filename}")
            except Exception as e:
                messagebox.showerror("Error", f"Could not open debug log file:\n{e}")
                self._debug_enabled = False
                self._debug_log_file = None

    # ------------------------------------------------------------------
    # Saved devices list and per-device settings
    # ------------------------------------------------------------------

    def _refresh_device_lists(self):
        """Reload the saved-device listbox and show the active device."""
        self.saved_devices_listbox.delete(0, tk.END)
        devices = self.config.get("devices", [])
        active_idx = self.config.get("active_device")
        for i, device in enumerate(devices):
            name = device.get("name", device.get("selected_model", "ISDT"))
            mac = device.get("mac_address", "")
            prefix = "▶ " if i == active_idx else "  "
            self.saved_devices_listbox.insert(tk.END, f"{prefix}{name} ({mac})")
        if active_idx is not None and 0 <= active_idx < len(devices):
            self._show_device_settings(active_idx)
            self.select_device_btn.config(state=tk.DISABLED)
            active = devices[active_idx]
            name = active.get("name") or active.get("selected_model", "ISDT")
            self.device_label.config(text=f"Selected: {name}")
        else:
            self._clear_device_settings()
            self.device_label.config(text="Selected: none")

    def _show_device_settings(self, index):
        """Fill the per-device settings panel with the given device's values."""
        devices = self.config.get("devices", [])
        if 0 <= index < len(devices):
            device = devices[index]
            model = device.get("selected_model", "C4 Air")

            self.settings_name.delete(0, tk.END)
            self.settings_name.insert(0, device.get("name", model))

            self.settings_mac.config(state="normal")
            self.settings_mac.delete(0, tk.END)
            self.settings_mac.insert(0, device.get("mac_address", ""))
            self.settings_mac.config(state="readonly")

            self.settings_model.set(model)

            # Poll interval is editable for all models; the valid range
            # (min/max) is enforced when saving.
            self.settings_interval.delete(0, tk.END)
            self.settings_interval.insert(
                0, str(device.get("poll_interval", get_poll_interval(model)))
            )
            self.settings_interval.config(state="normal", style="TEntry")

            self.settings_heartbeat.delete(0, tk.END)
            self.settings_heartbeat.insert(0, str(self.config.get("heartbeat_interval", 10)))
            self.settings_debug_folder.delete(0, tk.END)
            self.settings_debug_folder.insert(0, self.config.get("debug_log_folder", ""))
            self._selected_device_index = index

    def _clear_device_settings(self):
        """Reset the per-device settings panel to defaults."""
        self.settings_name.delete(0, tk.END)
        self.settings_mac.config(state="normal")
        self.settings_mac.delete(0, tk.END)
        self.settings_mac.config(state="readonly")
        self.settings_model.set("")
        self.settings_interval.delete(0, tk.END)
        self.settings_interval.insert(0, "10")
        self.settings_interval.config(state="normal", style="TEntry")
        self.settings_heartbeat.delete(0, tk.END)
        self.settings_heartbeat.insert(0, "10")
        self.settings_debug_folder.delete(0, tk.END)
        self._selected_device_index = None

    def _on_model_changed(self, event=None):
        """Called when the model dropdown is changed.

        The poll interval is editable for all models. If the currently entered
        value is outside the new model's allowed range, it is silently adjusted
        to the nearest bound so the user does not have to think about it.
        """
        model = self.settings_model.get().strip()
        min_i = get_min_poll_interval(model)
        max_i = get_max_poll_interval(model)

        try:
            current = int(self.settings_interval.get().strip())
        except ValueError:
            current = get_poll_interval(model)

        if current < min_i:
            current = min_i
        elif current > max_i:
            current = max_i

        self.settings_interval.delete(0, tk.END)
        self.settings_interval.insert(0, str(current))
        self.settings_interval.config(state="normal", style="TEntry")

    def add_selected_device(self):
        """Add the device selected in the scan list to the saved devices."""
        selection = self.device_listbox.curselection()
        if not selection:
            return
        idx = selection[0]
        device = self.scanned_devices[idx]
        if get_device_by_mac(self.config, device.address):
            messagebox.showinfo("Info", f"Device {device.name} is already in the list.")
            return
        index = add_device(self.config, device.address, "C4 Air", device.name)
        save_config(self.config)
        self.log_message(f"✅ Added: {device.name} ({device.address})")
        self._refresh_device_lists()
        self.saved_devices_listbox.selection_clear(0, tk.END)
        self.saved_devices_listbox.selection_set(index)
        self.saved_devices_listbox.see(index)
        self.on_saved_device_select()

    def on_saved_device_select(self, event=None):
        """Enable/disable the buttons based on the selected saved device."""
        if self._disconnect_future is not None:
            self.select_device_btn.config(state=tk.DISABLED)
            self.delete_device_btn.config(state=tk.DISABLED)
            return
        selection = self.saved_devices_listbox.curselection()
        if selection:
            idx = selection[0]
            devices = self.config.get("devices", [])
            if 0 <= idx < len(devices):
                self._show_device_settings(idx)
                self.select_device_btn.config(state=tk.NORMAL)
                self.delete_device_btn.config(state=tk.NORMAL)
                if self.config.get("active_device") == idx:
                    self.select_device_btn.config(state=tk.DISABLED, text="✅ Selected")
                else:
                    self.select_device_btn.config(state=tk.NORMAL, text="Select Device")
        else:
            self.select_device_btn.config(state=tk.DISABLED)
            self.delete_device_btn.config(state=tk.DISABLED)

    def select_saved_device(self):
        """Mark the selected saved device as active after full old-device teardown."""
        selection = self.saved_devices_listbox.curselection()
        if not selection:
            return
        idx = selection[0]
        devices = self.config.get("devices", [])
        if not (0 <= idx < len(devices)):
            return

        def finish_switch(success=True):
            if not success:
                messagebox.showerror(
                    "Device Switch",
                    "The previous BLE connection could not be fully released. "
                    "Please wait and try again."
                )
                return
            current_devices = self.config.get("devices", [])
            if not (0 <= idx < len(current_devices)):
                return
            set_active_device(self.config, idx)
            save_config(self.config)
            self.log_message(f"📱 Selected device: {current_devices[idx].get('name', '')}")
            self._refresh_device_lists()
            self.update_status("Disconnected", "gray")
            self.update_device_info(0, 0)
            self._set_table_placeholders()

        if self.device is not None:
            if self.device.connected:
                if not messagebox.askyesno(
                    "Device Switch",
                    "A device is currently connected. Disconnect and switch?"
                ):
                    self._refresh_device_lists()
                    return
            self.disconnect_device(on_complete=finish_switch)
            return

        finish_switch(True)

    def delete_saved_device(self):
        """Remove the selected saved device from the config."""
        selection = self.saved_devices_listbox.curselection()
        if not selection:
            return
        idx = selection[0]
        devices = self.config.get("devices", [])
        if 0 <= idx < len(devices):
            device = devices[idx]
            if not messagebox.askyesno("Delete Device", f"Delete '{device.get('name', '')}'?"):
                return
            def finish_delete(success=True):
                if not success:
                    messagebox.showerror(
                        "Delete Device",
                        "The BLE connection could not be fully released. Device was not deleted."
                    )
                    return
                remove_device(self.config, idx)
                save_config(self.config)
                self.log_message(f"🗑️ Deleted: {device.get('name', '')}")
                self._refresh_device_lists()
                self._clear_device_settings()
                self.update_status("Disconnected", "gray")
                self.update_device_info(0, 0)
                self._set_table_placeholders()

            if self.config.get("active_device") == idx and self.device is not None:
                self.disconnect_device(on_complete=finish_delete)
            else:
                finish_delete(True)

    def save_device_settings(self):
        """Save the per-device settings, validating the poll interval range."""
        if self._selected_device_index is None:
            messagebox.showerror("Error", "No device selected.")
            return

        name = self.settings_name.get().strip()
        selected_model = self.settings_model.get().strip()

        if not name:
            messagebox.showerror("Error", "Device name cannot be empty.")
            return

        # Poll interval – validate against model-specific min/max
        try:
            interval = int(self.settings_interval.get().strip())
        except ValueError:
            messagebox.showerror("Error", "Invalid poll interval (must be a number).")
            return

        min_i = get_min_poll_interval(selected_model)
        max_i = get_max_poll_interval(selected_model)
        if interval < min_i or interval > max_i:
            messagebox.showerror(
                "Error",
                f"Poll interval for {selected_model} must be between "
                f"{min_i} and {max_i} seconds."
            )
            return

        # Heartbeat interval
        try:
            heartbeat = int(self.settings_heartbeat.get().strip())
            if heartbeat < 1:
                raise ValueError
        except ValueError:
            messagebox.showerror("Error", "Invalid heartbeat interval (≥ 1 minute).")
            return

        devices = self.config.get("devices", [])
        if 0 <= self._selected_device_index < len(devices):
            devices[self._selected_device_index]["name"] = name
            devices[self._selected_device_index]["selected_model"] = selected_model
            devices[self._selected_device_index]["poll_interval"] = interval

        self.config["heartbeat_interval"] = heartbeat
        self.config["debug_log_folder"] = self.settings_debug_folder.get().strip()
        save_config(self.config)
        self.log_message(f"✅ Settings saved for {name}")
        self._refresh_device_lists()
        messagebox.showinfo("Success", "Device settings saved.")

    def update_gui_for_model(self):
        """Update title, slot dropdown and battery types after connect."""
        if not self.device or not self.device.connected:
            return
        model_config = self.device.model_config

        self.root.title(f"ISDT {model_config['display_name']} – Monitor & Control")

        num_slots = model_config["slots"]
        if self.slot_combo:
            self.slot_combo['values'] = [str(i) for i in range(1, num_slots + 1)]
            if int(self.slot_var.get()) > num_slots:
                self.slot_var.set("1")

        supported_types = model_config["battery_types"]
        if self.battery_combo:
            self.battery_combo['values'] = supported_types
            if self.battery_type_var.get() not in supported_types:
                self.battery_type_var.set(supported_types[0] if supported_types else "Auto")
                self._on_battery_type_changed()

        self.log_message(f"📊 Model: {model_config['display_name']} ({num_slots} slots, max {model_config['max_current_mA']}mA)")
        self.log_message(f"📊 Battery types: {', '.join(self.device.supported_battery_types)}")

        if self.device.supports_alarm:
            self.log_message("🔊 Alarm tone supported by this model")
            self.alarm_btn.config(state=tk.NORMAL)
        else:
            self.log_message("🔇 Alarm tone NOT supported by this model")
            self.alarm_btn.config(state=tk.DISABLED, text="🔇")

        self._set_table_placeholders(self.device)

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    def log_message(self, msg):
        """Append a timestamped message to the log box."""
        from datetime import datetime
        timestamp = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        self.log.config(state='normal')
        self.log.insert(tk.END, f"[{timestamp}] {msg}\n")
        self.log.see(tk.END)
        self.log.config(state='disabled')

    def update_status(self, text, color="black"):
        """Update the connection status label."""
        self.status_label.config(text=text, foreground=color)

    def update_device_info(self, input_voltage_mV, input_current_mA):
        """Update the input voltage / total power labels."""
        if input_voltage_mV > 0:
            self.input_voltage_label.config(
                text=f"🔌 Input voltage: {input_voltage_mV/1000:.1f} V"
            )
        else:
            self.input_voltage_label.config(text="🔌 Input voltage: -- V")

        if input_voltage_mV > 0 and input_current_mA > 0:
            total_power_W = (input_voltage_mV * input_current_mA) / 1_000_000
            self.total_power_label.config(text=f"⚡ Total power: {total_power_W:.1f} W")
        else:
            self.total_power_label.config(text="⚡ Total power: -- W")

    # ------------------------------------------------------------------
    # Slot settings field handling
    # ------------------------------------------------------------------

    def _on_battery_type_changed(self, event=None):
        """Enable/disable the current/capacity/cut-off fields based on type."""
        batt_str = self.battery_type_var.get()
        is_auto = (batt_str == "Auto")

        if is_auto:
            for w in (self.current_entry, self.capacity_entry, self.cutoff_entry):
                w.config(state='disabled', style="Gray.TEntry")
                w.delete(0, tk.END)
                w.insert(0, "Auto")
        else:
            for w in (self.current_entry, self.capacity_entry, self.cutoff_entry):
                w.config(state='normal', style="TEntry")

            limits = BATTERY_LIMITS.get(batt_str)
            if limits:
                if self.device and self.device.model_key:
                    default_current = get_default_current(self.device.model_key)
                    self.current_entry.delete(0, tk.END)
                    self.current_entry.insert(0, str(default_current))
                if self.device:
                    default_capacity = self.device.model_config.get("default_capacity_mAh", 2000)
                    self.capacity_entry.delete(0, tk.END)
                    self.capacity_entry.insert(0, str(default_capacity))
                default_cutoff = limits.get("cutoff_default", 0)
                self.cutoff_entry.delete(0, tk.END)
                self.cutoff_entry.insert(0, str(default_cutoff) if default_cutoff > 0 else "0")

        self._update_cutoff_delta_label()

    def _update_cutoff_delta_label(self, event=None):
        """Show 'Cut-off (ΔmV)' for small NiMh/NiCd values, else 'Cut-off (mV)'."""
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
            self.cutoff_label.config(text="Cut‑off (ΔmV):" if 0 < val < 6 else "Cut‑off (mV):")
        except ValueError:
            self.cutoff_label.config(text="Cut‑off (mV):")

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect_saved(self):
        """Connect to the currently active saved device."""
        if self._disconnect_future is not None:
            self.log_message("ℹ️ BLE disconnect is still in progress; connect is waiting.")
            return

        if self.device is not None:
            if self.device.connected:
                self.log_message("ℹ️ A device is already connected.")
                return
            # A disconnected instance may still have a poll task or pending
            # GATT cleanup. Finish it asynchronously before creating a new client.
            self.disconnect_device(on_complete=lambda success: self.connect_saved() if success else
                                   self.log_message("⚠️ Cannot connect until the previous BLE client is released."))
            return

        self._closing = False
        self._cancel_scheduled_reconnect()
        active = get_active_device(self.config)
        if not active:
            messagebox.showerror("Error", "No device selected.")
            return

        mac = active.get("mac_address")
        self.update_status("Connecting…", "orange")
        self.update_device_info(0, 0)
        self._set_table_placeholders()
        self.log_message(f"⏳ Connecting to {mac} ...")
        device = ISDTBLE(active, log_callback=self.log_message, config=self.config)
        if self._debug_enabled:
            device.debug_traffic = True
            device.debug_log_file = self._debug_log_file
        self.device = device
        asyncio.run_coroutine_threadsafe(self._connect_async(device), self.loop)

    async def _connect_async(self, device):
        """Connect one captured device instance; ignore stale completions."""
        try:
            success = await device.connect()
            if self.device is not device:
                await device.disconnect()
                return

            if success:
                self.config = load_config()
                device.config = self.config
                active = get_active_device(self.config)
                name = (active.get("name") or active.get("selected_model") or "ISDT") if active else "----"

                def update_connected_ui():
                    if self.device is not device:
                        return
                    self.log_message("✅ Connected!")
                    self.update_status(f"Connected to {name}", "green")
                    self.connect_btn.config(state=tk.DISABLED)
                    self.disconnect_btn.config(state=tk.NORMAL)
                    self.update_gui_for_model()

                self.root.after(0, update_connected_ui)
                self._connect_start_time = time.time()
                self._start_heartbeat()

                # A8Task once after connect (no retry).
                if device.is_a8:
                    try:
                        await device.request_a8_task()
                    except Exception:
                        pass

                if self.device is not device or not device.connected:
                    await device.disconnect()
                    return

                self.root.after(
                    START_POLL_DELAY_MS,
                    lambda d=device: self.start_polling(d)
                )
                self.root.after(
                    START_POLL_DELAY_MS + 500,
                    lambda d=device: self.update_settings_fields()
                    if self.device is d else None
                )
            else:
                self.log_message("⚠️ Connection failed.")
                await device.disconnect()
                if self.device is device:
                    self.device = None
                    self.root.after(0, lambda: self.connect_btn.config(state=tk.NORMAL))
                    self.root.after(0, lambda: self.disconnect_btn.config(state=tk.DISABLED))
                    self.root.after(0, lambda: self.update_status("Disconnected", "gray"))
                    self.root.after(0, self._set_table_placeholders)
                    self.root.after(0, lambda: self.update_device_info(0, 0))
        except Exception as e:
            err_msg = str(e).strip() or f"{type(e).__name__} (no details)"
            self.log_message(f"⚠️ Error: {err_msg}")
            try:
                await device.disconnect()
            except Exception:
                pass
            if self.device is device:
                self.device = None
                self.root.after(0, lambda: self.connect_btn.config(state=tk.NORMAL))
                self.root.after(0, lambda: self.disconnect_btn.config(state=tk.DISABLED))
                self.root.after(0, lambda: self.update_status("Disconnected", "gray"))
                self.root.after(0, self._set_table_placeholders)
                self.root.after(0, lambda: self.update_device_info(0, 0))

    async def _disconnect_device_async(self, device):
        """Stop polling and release the BLE client in a strictly ordered teardown."""
        task = device.poll_loop_task
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()

        # Disconnect first so a pending GATT operation in the poll task can be
        # interrupted by the backend. ISDTBLE.disconnect() retains its client
        # and task when WinRT does not finish, so no new device may take over.
        await device.disconnect()

        if task is not None and task is not asyncio.current_task() and not task.done():
            done, pending = await asyncio.wait({task}, timeout=5.0)
            if pending:
                raise TimeoutError("Polling task did not stop within 5 seconds")
            await asyncio.gather(task, return_exceptions=True)

    def _cancel_scheduled_reconnect(self):
        """Cancel a reconnect callback belonging to the previous connection."""
        self._reconnect_pending = False
        if self._reconnect_after_id is not None:
            try:
                self.root.after_cancel(self._reconnect_after_id)
            except Exception:
                pass
            self._reconnect_after_id = None

    def disconnect_device(self, on_complete=None):
        """Disconnect asynchronously while keeping the Tkinter UI responsive."""
        if self._disconnect_future is not None:
            self.log_message("ℹ️ BLE disconnect is already in progress.")
            return

        self._closing = True
        self.polling = False
        self._cancel_scheduled_reconnect()
        self._stop_heartbeat()

        device = self.device
        if device is None:
            self._finish_disconnect_ui(device, True)
            if on_complete:
                on_complete(True)
            return

        self._disconnect_device_ref = device
        self._disconnect_started_at = time.monotonic()
        self._disconnect_completion_callback = on_complete
        self._disconnect_last_log_second = 0
        self.connect_btn.config(state=tk.DISABLED)
        self.disconnect_btn.config(state=tk.DISABLED, text="Disconnecting…")
        self.select_device_btn.config(state=tk.DISABLED)
        self.delete_device_btn.config(state=tk.DISABLED)
        self.update_status("Disconnecting BLE…", "orange")
        self.log_message("⏳ Disconnect requested – waiting for BLE client release…")

        try:
            self._disconnect_future = asyncio.run_coroutine_threadsafe(
                self._disconnect_device_async(device), self.loop
            )
        except Exception as e:
            self._complete_disconnect(False, e)
            return

        self._poll_disconnect_progress()

    def _poll_disconnect_progress(self):
        """Refresh progress from Tk's event loop; never block the GUI thread."""
        future = self._disconnect_future
        if future is None:
            return

        if future.done():
            error = None
            try:
                future.result()
            except Exception as e:
                error = e
            self._complete_disconnect(error is None, error)
            return

        elapsed = int(time.monotonic() - self._disconnect_started_at)
        self.update_status(f"Disconnecting BLE… ({elapsed}s)", "orange")
        milestone = (elapsed // 5) * 5
        if milestone >= 5 and milestone > self._disconnect_last_log_second:
            self._disconnect_last_log_second = milestone
            self.log_message(f"⏳ BLE disconnect still running ({milestone}s); window remains active.")

        self._disconnect_progress_after_id = self.root.after(500, self._poll_disconnect_progress)

    def _complete_disconnect(self, success, error=None):
        """Finalize an asynchronous disconnect on the Tkinter main thread."""
        device = self._disconnect_device_ref
        callback = self._disconnect_completion_callback
        elapsed = (time.monotonic() - self._disconnect_started_at
                   if self._disconnect_started_at is not None else 0.0)

        if self._disconnect_progress_after_id is not None:
            try:
                self.root.after_cancel(self._disconnect_progress_after_id)
            except Exception:
                pass
            self._disconnect_progress_after_id = None

        self._disconnect_future = None
        self._disconnect_device_ref = None
        self._disconnect_started_at = None
        self._disconnect_completion_callback = None
        self._disconnect_last_log_second = 0

        if success:
            if self.device is device:
                self.device = None
            self._finish_disconnect_ui(device, True)
            self.log_message(f"✅ BLE disconnect completed ({elapsed:.1f}s).")
        else:
            if error is None:
                detail = "Unknown error"
            else:
                detail = str(error).strip() or type(error).__name__
            self.connect_btn.config(state=tk.DISABLED if self.device is not None else tk.NORMAL)
            self.disconnect_btn.config(state=tk.NORMAL, text="Retry Disconnect")
            self.update_status("Disconnect incomplete – retry required", "red")
            self.log_message(
                f"⚠️ BLE disconnect incomplete after {elapsed:.1f}s: {detail}. "
                "The previous client remains reserved; switching/connecting is blocked."
            )

        self.on_saved_device_select()
        if callback:
            callback(success)

    def _finish_disconnect_ui(self, device, success):
        """Reset connection widgets after the BLE client was safely released."""
        if not success:
            return
        self.connect_btn.config(state=tk.NORMAL)
        self.disconnect_btn.config(state=tk.DISABLED, text="Disconnect")
        self.update_status("Disconnected", "gray")
        self.update_device_info(0, 0)
        self.charge_start_times.clear()
        self._set_table_placeholders()
        self.alarm_btn.config(text="🔊")
        self.cutoff_entry.config(state='normal', style="TEntry")

    # ------------------------------------------------------------------
    # Polling and heartbeat
    # ------------------------------------------------------------------

    def _schedule_reconnect(self, device):
        """Schedule reconnect only while the same lost device remains selected."""
        if self._closing or self._reconnect_pending or self.device is not device:
            return
        self._reconnect_pending = True
        self._reconnect_after_id = self.root.after(
            RECONNECT_DELAY_S * 1000,
            lambda d=device: self._run_scheduled_reconnect(d)
        )

    def _run_scheduled_reconnect(self, device):
        """Reconnect only if the callback still belongs to the active device."""
        self._reconnect_after_id = None
        self._reconnect_pending = False
        if not self._closing and self.device is device:
            self.connect_saved()

    def _show_disconnected_device_ui(self, device):
        """Clear stale measurements after the active charger loses its link."""
        if self.device is not device:
            return
        self.update_status("Disconnected – reconnecting…", "orange")
        self.update_device_info(0, 0)
        self._set_table_placeholders(device)

    def start_polling(self, device=None):
        """Start polling the exact device instance that completed connection."""
        device = device or self.device
        if (device is None or self.device is not device
                or not device.connected or self.polling):
            return
        self.polling = True
        interval = device.poll_interval
        self.log_message(f"✅ Polling started (interval: {interval}s)")
        asyncio.run_coroutine_threadsafe(self._poll_loop(interval, device), self.loop)

    async def _poll_loop(self, interval, device):
        """Poll only the device instance captured when this task was started."""
        if self.device is not device or not device.connected:
            if self.device is device:
                self.polling = False
            return

        device.poll_loop_task = asyncio.current_task()
        failures = 0
        client_release_started = False

        try:
            while self.polling and self.device is device and device.connected:
                ok = await device.poll_data()
                if self.device is not device:
                    break
                self.root.after(0, lambda d=device: self.update_table() if self.device is d else None)

                if not device.connected:
                    self.polling = False
                    self._stop_heartbeat()
                    if not self._closing:
                        self.root.after(
                            0, lambda d=device: self._show_disconnected_device_ui(d)
                            if self.device is d else None
                        )
                        self.log_message(
                            f"⚠️ Link lost – releasing BLE client; reconnecting in {RECONNECT_DELAY_S}s"
                        )
                        client_release_started = True
                        await device.disconnect()
                        self._schedule_reconnect(device)
                    break

                if ok:
                    failures = 0
                else:
                    failures += 1
                    if failures >= MAX_POLL_FAILURES:
                        if device.is_a8 and device.connected:
                            await device.diagnose_a8_ble()
                        self.polling = False
                        self._stop_heartbeat()
                        self.log_message(
                            f"⚠️ {MAX_POLL_FAILURES} empty polls – releasing BLE client; "
                            f"reconnecting in {RECONNECT_DELAY_S}s"
                        )
                        client_release_started = True
                        await device.disconnect()
                        self.root.after(
                            0, lambda d=device: self._show_disconnected_device_ui(d)
                            if self.device is d else None
                        )
                        self._schedule_reconnect(device)
                        break
                    if device.is_a8:
                        continue

                await asyncio.sleep(interval)

        except asyncio.CancelledError:
            # Manual disconnect cancels this task and awaits its completion.
            pass

        finally:
            if device.poll_loop_task is asyncio.current_task():
                device.poll_loop_task = None
            if self.device is device:
                self.polling = False
                if not self._closing:
                    self._stop_heartbeat()
                    self.log_message("✅ Polling stopped")

                if (not self._closing and not device.connected
                        and not client_release_started):
                    client_release_started = True
                    try:
                        await device.disconnect()
                    except Exception as e:
                        self.log_message(f"⚠️ BLE cleanup after link loss failed: {e}")
                    self._schedule_reconnect(device)

    def _start_heartbeat(self):
        """Start (or restart) the heartbeat timer."""
        self._stop_heartbeat()
        interval = self.config.get("heartbeat_interval", 10)
        if interval > 0:
            self._heartbeat_timer_id = self.root.after(
                interval * 60000, self._heartbeat_loop
            )

    def _stop_heartbeat(self):
        """Cancel the heartbeat timer if one is pending."""
        if self._heartbeat_timer_id is not None:
            try:
                self.root.after_cancel(self._heartbeat_timer_id)
            except Exception:
                pass
            self._heartbeat_timer_id = None

    def _heartbeat_loop(self):
        """Log one heartbeat and schedule the next one."""
        self._heartbeat_timer_id = None
        if not self.device or not self.device.connected:
            return
        self._log_heartbeat()
        self._start_heartbeat()

    def _log_heartbeat(self):
        """Write one heartbeat line with active slot count and uptime."""
        if not self.device or not self.device.connected:
            self.log_message("💓 Heartbeat: disconnected")
            return
        uptime = time.time() - getattr(self, '_connect_start_time', time.time())
        active_slots = 0
        is_a8 = getattr(self.device, "model_key", "") == "A8 Air"

        for slot in range(1, self.device.num_slots + 1):
            work = self.device.latest_data.get(f"slot{slot}_workstate", {})
            if work.get("status_str", "idle") not in ("idle", "empty", "error"):
                active_slots += 1
        total_minutes = int(uptime // 60)
        hours = total_minutes // 60
        minutes = total_minutes % 60
        self.log_message(
            f"💓 Heartbeat: {active_slots}/{self.device.num_slots} slots active, "
            f"Connection time {hours}h {minutes}m"
        )

    def _format_time(self, seconds):
        """Compact time formatter (used for charge time)."""
        if seconds < 0:
            seconds = 0
        if seconds < 60:
            return f"{seconds:.0f}s"
        elif seconds < 3600:
            return f"{seconds//60:.0f}m {seconds%60:.0f}s"
        else:
            return f"{seconds//3600:.0f}h {(seconds%3600)//60:.0f}m"

    def _battery_bar(self, percent):
        """Return a textual battery bar for the given percentage."""
        percent = max(0, min(100, percent))
        filled = int(percent / 100 * 12)
        return "█" * filled + "░" * (12 - filled) + f" {percent:3d}%"

    # ------------------------------------------------------------------
    # Table update
    # ------------------------------------------------------------------

    def _set_table_placeholders(self, device=None):
        """Show the selected charger's slot layout without stale measurements."""
        model_key = None
        slot_count = 0

        if device is not None:
            model_key = getattr(device, "model_key", None)
            slot_count = getattr(device, "num_slots", 0)

        if not model_key:
            active = get_active_device(self.config)
            if active:
                model_key = active.get("selected_model", "C4 Air")

        model_config = ISDT_MODELS.get(model_key or "")
        if model_config:
            slot_count = model_config.get("slots", slot_count)

        placeholder = "—"
        rows = [
            (slot, placeholder, placeholder, placeholder, placeholder, placeholder,
             placeholder, placeholder, placeholder, placeholder, placeholder, placeholder)
            for slot in range(1, slot_count + 1)
        ]

        if rows == self._last_table_values:
            return

        for item in self.tree.get_children():
            self.tree.delete(item)
        for row in rows:
            self.tree.insert("", tk.END, values=row)
        self._last_table_values = rows

    def update_table(self):
        """Rebuild the data table from the latest poll results."""
        new_values = []
        if not self.device or not self.device.connected:
            self._set_table_placeholders(self.device)
            return

        input_voltage_mV = 0
        input_current_mA = 0
        now = time.time()

        # Input voltage comes from its own key (channel 0 electric)
        elec_in = self.device.latest_data.get("input_electric", {})
        if elec_in.get("input_voltage_mV", 0) > 0:
            input_voltage_mV = elec_in["input_voltage_mV"]
            input_current_mA = elec_in.get("input_current_mA", 0)

        is_a8 = getattr(self.device, "model_key", "") == "A8 Air"

        for slot in range(1, self.device.num_slots + 1):
            work = self.device.latest_data.get(f"slot{slot}_workstate", {})
            elec = self.device.latest_data.get(f"slot{slot}_electric", {})
            ir = self.device.latest_data.get(f"slot{slot}_ir", {})

            if not work:
                placeholder = "—"
                new_values.append((slot, placeholder, placeholder, placeholder, placeholder,
                                   placeholder, placeholder, placeholder, placeholder,
                                   placeholder, placeholder, placeholder))
                continue

            status = work.get("status_str", "idle")
            is_idle = status in ("idle", "empty") or work.get("status", 0) == 0

            batt_type = work.get("battery_type", -1)
            if batt_type >= 0:
                inv_map = {v: k for k, v in BATTERY_TYPE_STR_TO_INT.items()}
                batt_str = inv_map.get(batt_type, "Auto")
            else:
                batt_str = "Auto"
            battery_type = work.get("battery_type_str", batt_str)

            max_current = work.get("max_current_mA", 0)
            max_current_display = str(max_current) if max_current > 0 else "0"

            cap_limit = work.get("max_output_power_mW", 0)
            cap_limit_display = str(cap_limit) if cap_limit > 0 else "no limit"

            cutoff_mV = work.get("full_charged_volt_mV", 0)
            if cutoff_mV > 0:
                cutoff_display = f"{cutoff_mV} (ΔmV)" if cutoff_mV < 6 else str(cutoff_mV)
            else:
                cutoff_display = "0"

            if is_idle:
                voltage_V = current_A = 0.0
                capacity = capacity_percent = 0
                ir_val = "N/A" if is_a8 else 0
                charge_time_str = "--:--"
                battery_display = "—"
                if slot in self.charge_start_times:
                    del self.charge_start_times[slot]
            else:
                voltage_mV = elec.get("voltage_mV", 0) or work.get("voltage_mV", 0)
                voltage_V = voltage_mV / 1000.0

                if status == "done":
                    current_A = 0.0
                else:
                    # Charge current: prefer the Electric response, fall back
                    # to the work_current_mA field in the mega packet (A8 only
                    # sends Electric for slot 1, the other slots only have
                    # work_current_mA in the workstate packet).
                    current_mA = elec.get("charging_current_mA", 0)
                    if current_mA == 0:
                        current_mA = work.get("work_current_mA", 0)
                    current_A = current_mA / 1000.0

                capacity_percent = work.get("capacity_percent", 0)
                capacity = work.get("capacity_mAh", 0)
                if is_a8:
                    # The ISD Link app does not display IR for the A8 Air.
                    ir_val = "N/A"
                else:
                    ir_total = ir.get("ir_total_mohm")
                    if ir_total is None:
                        ir_val = "—"
                    else:
                        ir_val = round(ir_total)
                        # Flag high IR readings above 1000 mΩ.
                        # Keep the measured value visible; the warning is only a hint.
                        if ir_total > 1000:
                            ir_val = f"⚠ {ir_val} ⚠"

                work_period_ms = work.get("work_period_ms", 0)
                if work_period_ms > 0:
                    charge_time_str = self._format_time(work_period_ms / 1000.0)
                else:
                    is_charging = status in (
                        "Pre-charge / trickle", "CC constant current",
                        "Active charging", "CV constant voltage", "charging"
                    ) or work.get("status", 0) in (1, 2, 3, 4)
                    if is_charging:
                        if slot not in self.charge_start_times:
                            self.charge_start_times[slot] = now
                        charge_time_str = self._format_time(now - self.charge_start_times[slot])
                    else:
                        charge_time_str = "--:--"
                battery_display = self._battery_bar(capacity_percent)

            new_values.append((slot, status, battery_type, f"{voltage_V:.3f}", f"{current_A:.2f}",
                               max_current_display, capacity, ir_val, charge_time_str,
                               cutoff_display, cap_limit_display, battery_display))

        if new_values == self._last_table_values:
            return
        for item in self.tree.get_children():
            self.tree.delete(item)
        for row in new_values:
            self.tree.insert("", tk.END, values=row)
        self._last_table_values = new_values

        if input_voltage_mV > 0:
            self.root.after(0, lambda: self.update_device_info(input_voltage_mV, input_current_mA))

    def _on_tree_motion(self, event):
        """Show an English tooltip over IR cells."""
        region = self.tree.identify_region(event.x, event.y)
        column = self.tree.identify_column(event.x)
        if region != "cell" or column != "#8":
            self._hide_tree_tooltip()
            return

        is_a8 = bool(
            self.device and getattr(self.device, "model_key", "") == "A8 Air"
        )
        if is_a8:
            tooltip_text = "The A8 Air does not support IR measurement."
        else:
            tooltip_text = (
                "IR measurements are reliable only when the charging "
                "current is at least 300 mA."
            )

        # Recreate the tooltip only when its message changes.
        current_text = getattr(self, "_ir_tooltip_text", None)
        if self._ir_tooltip is None or current_text != tooltip_text:
            self._hide_tree_tooltip()
            self._ir_tooltip = tk.Toplevel(self.root)
            self._ir_tooltip.wm_overrideredirect(True)
            label = tk.Label(
                self._ir_tooltip,
                text=tooltip_text,
                background="#ffffe0",
                relief="solid",
                borderwidth=1,
                padx=6,
                pady=3,
                font=("Helvetica", 9),
            )
            label.pack()
            self._ir_tooltip_text = tooltip_text

        self._ir_tooltip.wm_geometry(f"+{event.x_root + 12}+{event.y_root + 12}")

    def _hide_tree_tooltip(self, event=None):
        """Hide the A8 IR tooltip."""
        if self._ir_tooltip is not None:
            self._ir_tooltip.destroy()
            self._ir_tooltip = None
        self._ir_tooltip_text = None

    def on_tree_select(self, event):
        """Copy the selected row's slot and battery type into the settings bar."""
        selection = self.tree.selection()
        if selection:
            values = self.tree.item(selection[0], 'values')
            if values and len(values) > 2:
                self.slot_var.set(str(values[0]))
                if values[2] and values[2] not in ("-", "—"):
                    if self.device and values[2] in getattr(self.device, "supported_battery_types", []):
                        self.battery_type_var.set(values[2])
                        self._on_battery_type_changed()
                self.update_settings_fields()

    def update_settings_fields(self):
        """Sync the slot settings bar with the currently selected slot."""
        if not self.device or not self.device.connected:
            return
        try:
            slot = int(self.slot_var.get()) - 1
            work = self.device.latest_data.get(f"slot{slot + 1}_workstate", {})
            batt_type = work.get("battery_type", -1) if work else -1
            if batt_type >= 0:
                inv_map = {v: k for k, v in BATTERY_TYPE_STR_TO_INT.items()}
                batt_str = inv_map.get(batt_type, "Auto")
            else:
                batt_str = "Auto"
            if batt_str in self.device.supported_battery_types:
                self.battery_type_var.set(batt_str)
            else:
                self.battery_type_var.set(
                    self.device.supported_battery_types[0] if self.device.supported_battery_types else "Auto"
                )
            self._on_battery_type_changed()
        except Exception as e:
            self.log_message(f"⚠️ update_settings_fields error: {e}")

    # ------------------------------------------------------------------
    # Alarm tone
    # ------------------------------------------------------------------

    def toggle_alarm_tone(self):
        """Toggle the charger's alarm tone."""
        if not self.device or not self.device.connected:
            messagebox.showerror("Error", "No device connected.")
            return
        if not self.device.supports_alarm:
            messagebox.showinfo("Info", "Alarm tone is NOT supported by this model.")
            return
        new_state = not self.device._alarm_tone_state
        asyncio.run_coroutine_threadsafe(self._toggle_alarm_async(new_state), self.loop)

    async def _toggle_alarm_async(self, new_state):
        success = await self.device.set_alarm_tone(new_state)
        if success:
            self.root.after(0, lambda: self.alarm_btn.config(text="🔊" if new_state else "🔇"))
            self.log_message(f"🔊 Alarm tone {'on' if new_state else 'off'}")
        else:
            self.log_message("⚠️ Failed to set alarm tone.")

    # ------------------------------------------------------------------
    # Scan for BLE devices
    # ------------------------------------------------------------------

    def scan_devices(self):
        """Start a BLE scan and populate the found-devices listbox."""
        if self.scanning:
            self.log_message("⚠️ Scan already in progress.")
            return
        self.scanning = True
        self.scan_btn.config(state=tk.DISABLED)
        self.scan_status.config(text="Searching...", foreground="orange")
        self.device_listbox.delete(0, tk.END)
        self.log_message("🔎 Scanning for BLE devices (10 seconds)...")
        self.root.update_idletasks()
        asyncio.run_coroutine_threadsafe(self._scan_async(), self.loop)
        self.root.after(15000, self._scan_timeout)

    def _scan_timeout(self):
        """Safety net in case the scan never finishes."""
        if self.scanning:
            self.scanning = False
            self.scan_btn.config(state=tk.NORMAL)
            self.scan_status.config(text="Scan timed out.", foreground="red")
            self.log_message("⚠️ Scan timed out.")

    async def _scan_async(self):
        """Run BleakScanner.discover() in the background thread."""
        from bleak import BleakScanner
        scanner = None
        try:
            scanner = BleakScanner()
            devices = await scanner.discover(timeout=10)
            self.scanned_devices = [d for d in devices if d.name]
        except Exception as e:
            self.log_message(f"⚠️ Scan error: {e}")
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
        """Push the scan results into the listbox (GUI thread)."""
        self.scan_btn.config(state=tk.NORMAL)
        self.device_listbox.delete(0, tk.END)
        for d in self.scanned_devices:
            self.device_listbox.insert(tk.END, f"{d.name} ({d.address})")
        if not self.scanned_devices:
            self.scan_status.config(text="No devices found.", foreground="orange")
        else:
            self.scan_status.config(text=f"{len(self.scanned_devices)} devices found", foreground="green")
            self.log_message(f"✅ {len(self.scanned_devices)} devices found.")
        self.add_device_btn.config(state=tk.NORMAL if self.scanned_devices else tk.DISABLED)
        self.scanning = False

    def on_device_select(self, event):
        """Enable the Add button when a scan result is selected."""
        selection = self.device_listbox.curselection()
        self.add_device_btn.config(state=tk.NORMAL if selection else tk.DISABLED)

    # ------------------------------------------------------------------
    # Apply slot settings
    # ------------------------------------------------------------------

    def apply_settings(self):
        """Send the slot settings to the charger (work task)."""
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
            current_mA = 0 if current_raw in ("Auto", "") else int(current_raw)
            if capacity_raw in ("no limit", "", "0"):
                capacity_mAh = 0
            else:
                capacity_mAh = int(capacity_raw)

            cutoff_raw = self.cutoff_entry.get().strip()
            if cutoff_raw in ("Auto", "") or self.cutoff_entry.cget('state') == "disabled":
                cutoff_mV = 0
            else:
                cutoff_mV = int(cutoff_raw)

            if batt_str == "Auto":
                self.log_message(f"⚡ Auto mode for Slot {slot+1}")
                asyncio.run_coroutine_threadsafe(
                    self.device.set_worktask(channel=slot, battery_type=6, work_current_mA=0,
                                             capacity_limit_mAh=0, full_charged_volt=0),
                    self.loop
                )
                messagebox.showinfo("Auto Mode", f"Slot {slot+1} set to Auto.")
                return

            if batt_str not in self.device.supported_battery_types:
                raise ValueError(f"Battery type {batt_str} not supported by {self.device.model_key}.")
            if current_mA > self.device.max_current_mA:
                raise ValueError(f"Current {current_mA}mA exceeds max {self.device.max_current_mA}mA")

            limits = BATTERY_LIMITS.get(batt_str)
            if limits is None:
                raise ValueError(f"Unknown battery type: {batt_str}")

            cap_min, cap_max = limits["capacity_min"], limits["capacity_max"]
            if (cap_min > 0 or cap_max > 0) and capacity_mAh != 0:
                if capacity_mAh < cap_min or capacity_mAh > cap_max:
                    raise ValueError(f"Capacity must be 0 or between {cap_min} and {cap_max} mAh.")

            if limits["cutoff_enabled"]:
                if cutoff_mV < limits["cutoff_min"] or cutoff_mV > limits["cutoff_max"]:
                    raise ValueError(f"Cut-off must be between {limits['cutoff_min']} and {limits['cutoff_max']} mV.")

            if current_mA < CURRENT_MIN_MA or current_mA > CURRENT_MAX_MA:
                raise ValueError(f"Current must be between {CURRENT_MIN_MA} and {CURRENT_MAX_MA} mA.")

            self.root.bell()
            asyncio.run_coroutine_threadsafe(
                self.device.set_worktask(channel=slot, battery_type=batt_int, work_current_mA=current_mA,
                                         capacity_limit_mAh=capacity_mAh, full_charged_volt=cutoff_mV),
                self.loop
            )
            self.log_message(f"⚡ Slot {slot+1}: {batt_str}, {current_mA}mA, {capacity_mAh}mAh, cut-off {cutoff_mV}mV")
            messagebox.showinfo("Sent", f"Settings for Slot {slot+1} sent.")

            if self.device.is_a8:
                self.root.after(2000, self._refresh_a8_task)

        except ValueError as e:
            messagebox.showerror("Input error", str(e))

    def _refresh_a8_task(self):
        """Re-request per-slot task settings after an Apply on the A8."""
        if self.device and self.device.connected and self.device.is_a8:
            asyncio.run_coroutine_threadsafe(self.device.request_a8_task(), self.loop)


if __name__ == "__main__":
    root = tk.Tk()
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