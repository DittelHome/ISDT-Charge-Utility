```markdown
``` 
# ISDT Charge Utility

**Cross-platform GUI for ISDT C4 Air charger**
- Direct Bluetooth Low Energy (BLE) connection.  
- Works under Windows and Linux.

Monitor and control your charger with ease.

![Python Version](https://img.shields.io/badge/python-3.10+-blue.svg)
![License](https://img.shields.io/badge/license-MIT-green.svg)
![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux-lightgrey.svg)

---

## 📸 Screenshots

| Main View | Settings |
|-----------|----------|
| ![Main View](images/screenshot_main.png) | ![Settings](images/screenshot_settings.png) |

*(Replace with actual screenshots)*

---

## ✨ Features

### 📊 Monitoring
- **BLE Connection** –  Direct communication with ISDT C4/A4/A8/NP2 Air
- **Auto-Connect** – Saved MAC address is used on startup
- **Automatic Model Detection** – Detects C4 Air (6 slots), A4 Air (4 slots), A8 Air (8 slots)
- **Detailed Status** – Pre‑charge, CC, CV, done, error
- **Charge Time** – Read directly from the device
- **Battery Bar** – Visual representation of charge level
- **Input Voltage & Total Power** – Clear overview
- **Hardware Info** – Firmware and hardware version displayed on connect
- **Automatic Polling** – Regular data updates (interval adjustable)
- **Timeout Detection** – Detects when the device is powered off

### ⚡ Control
- **Set Battery Type** – Model-dependent:
  - C4 Air: LiHV, LiIon, LiFe, NiZn, NiMh/NiCd, LiIon(1.5V), Auto
  - A4 Air: NiMh/NiCd, LiIon, LiFe, Auto
  - A8 Air: LiHV, NiMh/NiCd, LiIon, LiFe, Auto
- **Set Charge Current** – 100–2000 mA (model-dependent maximum)
- **Set Capacity Limit** – Battery‑specific ranges (0 = unlimited)
- **Set Cut‑off Voltage** – Battery‑specific ranges (0 = default)
- **Alarm Tone** – Toggle on/off with a single click (🔊/🔇)
- **Persistent Settings** – MAC, device name, interval, Bind UUID are saved

---

## 🖥️ Supported Devices

| Model       | Slots | Max Current | Status                    |
|-------------|-------|-------------|---------------------------|
| ISDT C4 Air | 6     | 2000mA      | ✅ Fully supported        |
| ISDT A4 Air | 4     | 1000mA      | ❓ Test outstanding (need testers) |
| ISDT A8 Air | 8     | 1000mA      | ❓ Test outstanding (need testers) |
| ISDT NP2 Air| 2     | 1500mA      | ❓ Test outstanding (need testers) |

---

## 📋 Requirements

### Windows
- **Windows 10 or 11** (with Bluetooth 4.0+)
- **Python 3.10 or higher**
- **Bluetooth adapter** (built-in or USB)

### Linux
- **Linux** (tested on Ubuntu/Debian, should work on other distributions)
- **Python 3.10 or higher**
- **Bluetooth adapter** (built-in or USB)
- **BlueZ** – Linux Bluetooth stack

---

## 🔧 Installation

### 1. Clone or download the repository

```bash
git clone https://github.com/DittelHome/ISDT-Charge-Utility.git
cd ISDT-Charge-Utility
```

### 2. Install Python dependencies

#### Windows
```cmd
pip install bleak
```

#### Linux
```bash
# Create and activate virtual environment (optional)
python3 -m venv .isdt
source .isdt/bin/activate

# Install required packages
pip install bleak
```

or:

```bash
sudo apt install python3-bleak
```

### 3. Run the application

#### Windows
- Double-click `isdt.py` (if `.py` files are associated with Python)
- Or create a shortcut:
  - Right-click on desktop → New → Shortcut
  - Move the shortcut to `Start Menu` or `Taskbar`


#### Linux

- python3 isdt.py
- Or Create a *.Starter


---

## 🚀 Usage

### First Start

## 🔹 Linux
1. **Pair the device in Blueman** (one-time)
   - Open Blueman Manager
   - Scan for devices
   - Select your ISDT device
   - Click "Pair"
   - Enter the PIN: `000000`

2. **Start the app**
   ```bash
   python3 isdt.py
   ```

3. **Scan and save your device**
   - Go to the **"Settings"** tab
   - Click **"Scan"**
   - Select your ISDT device from the list
   - Click **"Save selected device"**

4. **Connect**
   - Switch to the **"Device"** tab
   - Click **"Connect (saved)"**
   - The connection will be established and polling starts automatically

## 🔹 Windows
> **⚠️ Important:** On Windows, the ISDT C4 Air must **NOT** be paired via Windows Bluetooth settings!
> Do **NOT** use "Add Bluetooth device" in Windows to pair the charger.
> The app handles the BLE connection directly.

1. **Start the app**
   - Double-click `isdt.py` or use the shortcut

2. **Scan and save your device**
   - Go to the **"Settings"** tab
   - Click **"Scan"**
   - Select your ISDT device from the list
   - Click **"Save selected device"**

3. **Connect**
   - Switch to the **"Device"** tab
   - Click **"Connect (saved)"**
   - The connection will be established and polling starts automatically
---
### Controlling Charging Parameters

1. **Select a slot**
   - Click on a row in the table, or
   - Use the **"Slot"** dropdown in the settings panel

2. **Adjust values**
   - **Battery type** – Choose from the dropdown (model-dependent)
   - **Current** – Enter value in mA (100–2000, model-dependent maximum)
   - **Capacity limit** – Enter value in mAh (0 = unlimited)
   - **Cut‑off** – Enter value in mV (0 = default; only if supported for the selected battery type)

3. **Apply**
   - Click the red **"Apply"** button
   - A **beep** confirms that the settings were sent
   - The charger will apply the new parameters immediately

4. **Alarm Tone**
   - Click the **speaker icon** (🔊/🔇) in the top bar to toggle the alarm on/off

---
### Battery‑specific Validation

The tool validates your input based on the selected battery type. Invalid values will show an error message.

| Battery Type | Capacity Limit (mAh) | Cut‑off (mV) |
|--------------|----------------------|--------------|
| LiHV         | 0 or 2000–7000       | 4250–4450    |
| LiIon        | 0 or 2000–7000       | 4100–4300    |
| LiFe         | 0 or 2000–7000       | 3550–3750    |
| NiZn         | 0 or 2000–7000       | 1800–2000    |
| NiMh/Cd         | 0 or 1000–4000       | 3–12 (Delta‑Peak) |
| LiIon(1.5V)  | 0 or 1000–4000       | none (0)     |
| Auto         | 0 or 2000–7000       | none (0)     |

---

## 📊 Displayed Data

| Column | Description |
|--------|-------------|
| **Slot** | Channel number (1-6) |
| **Status** | Charge state (idle, pre-charge, CC, CV, done, error) |
| **Type** | Battery chemistry (NiMh/NiCd, LiIon, LiFe, etc.) |
| **Voltage** | Current battery voltage in volts |
| **Current** | Charge current in amperes |
| **Capacity** | Charged capacity in mAh |
| **IR** | Internal resistance in mΩ |
| **Charge Time** | Elapsed charging time (from device) |
| **Charge Level** | Battery bar with percentage |

---

### 📁 Project Structure

| File | Description |
|------|-------------|
| `isdt.py` | Main program (tkinter GUI) |
| `isdt_ble.py` | BLE communication (connection, polling, control) |
| `isdt_protocol.py` | Protocol definitions and parsers |
| `isdt_config.py` | Settings load/save |
| `isdt_limits.py` | Battery‑specific validation limits (customizable) |
| `isdt_models.py`	|Model definitions (C4/A4/A8 Air)|
| `PROTOCOL.md` | Technical documentation of the BLE protocol |


---

## ⚙️ Configuration

Settings are stored in `~/.isdt_gui_config.json` or  `C:\Users\username\.isdt_gui_config.json:`

```json
{
    "mac_address": "50:54:7B:63:4B:A3",
    "device_name": "ISDT C4 Air",
    "poll_interval": 5,
    "bind_uuid": "3c7a0e1ea9fb4919bb268c617e0ff89c"
}
```

---

## 🐛 Troubleshooting

### "Device with address ... was not found"

#### Windows
- **Do NOT pair** the device via Windows Bluetooth settings

#### Linux
- Pair the device in Blueman (PIN: `000000`)

### Connection Timeout
- Make sure the charger is within 1-2 meters of your PC
- **Close the ISD Link app** on your smartphone or on a second PC (IMPORTANT!)


### "Device is rebooting sometimes"
- Make sure you have a good USB‑C power adapter.  
- The ISDT Charger requires 12 V, otherwise it won't charge Li‑ion batteries.

---

## 🔍 Debug Mode

To enable debug mode, set `debug=True` when initializing `ISDTBLE` in `isdt.py`:

```python
self.device = ISDTBLE(mac, log_callback=self.log_message, debug=True)
```

---

## 📝 Developer Notes

### Protocol

The BLE protocol is based on the documentation from the [Home Assistant Integration](https://github.com/mtheli/isdt_air_ble) for reading, and reverse‑engineered from the ISD Link Android app for writing. See [Protocol description](https://github.com/DittelHome/ISDT-Charge-Utility/blob/main/PROTOCOL.md)

---

## 💻 Platform Support

| Platform | Support | Notes |
|----------|---------|-------|
| **Linux** | ✅ Full support | Tested on Ubuntu/Debian |
| **Windows 10/11** | ✅ Full support | Requires Python 3.10+
| **macOS** | ❌ Not tested | - |

>Windows 11 Test was performed with Python 3.13.15 successfully.
Python 3.14.7 (newest one) had problems (no BLE connect).

The program works natively on both Linux and Windows.


---
## ⚠️ DISCLAIMER / HAFTUNGSAUSSCHLUSS

**IMPORTANT - PLEASE READ CAREFULLY**

This software is provided "AS IS" and "WITH ALL FAULTS" without warranty of any kind, express or implied. 

**By using this software, you agree that:**

1. **USE AT YOUR OWN RISK** – The author assumes NO responsibility or liability for any damages, injuries, or losses resulting from the use of this software.
2. **BATTERY SAFETY** – Charging batteries incorrectly can cause fire, explosion, or personal injury. Always follow the battery manufacturer's safety guidelines.
3. **NO GUARANTEE** – The software may contain bugs or errors. It is your responsibility to verify all settings before starting a charge.
4. **HARDWARE DAMAGE** – The author is not liable for any damage to your charger, batteries, or other equipment.
5. **NO WARRANTY** – This software comes with no warranty, express or implied. The entire risk of using the software is with you.

**Du verwendest diese Software auf eigene Gefahr!**  
Der Autor übernimmt keinerlei Haftung für Schäden an Geräten, Batterien oder Personen.


---

## 📜 License

MIT License – see [LICENSE](LICENSE) file.

---

## 🙏 Acknowledgments

- [mtheli/isdt_air_ble](https://github.com/mtheli/isdt_air_ble) – Home Assistant integration as protocol reference (read commands)
- [bleak](https://github.com/hbldh/bleak) – BLE library for Python

---

**Note:** This is an independent community project and is not affiliated with, endorsed by, or sponsored by ISDT.
