```markdown
```
# ISDT Charger BLE Protocol

Technical documentation of the BLE protocol used by ISDT chargers (C4 Air, A8 Air, NP2 Air, LP2 Air, etc.).
Documented from BLE traffic analysis and verified against real device communication.

**Reading commands** are based on analysis by [mtheli/isdt_air_ble](https://github.com/mtheli/isdt_air_ble).  
**Writing/Control commands** were reverse‑engineered from the ISD Link Android app by [DittelHome/ISDT-Charge-Utility](https://github.com/DittelHome/ISDT-Charge-Utility).

---

## Overview

The charger exposes a single BLE service (`0000af00-...`) with two GATT characteristics.
Communication follows a simple command/response pattern: the client writes a command and
the charger responds via BLE notifications on the same characteristic.

After connecting, the client performs a **bind handshake** on AF02, optionally queries
**hardware info** on AF02, then enters a continuous **polling loop** on AF01.

## BLE Service & Characteristics

**Service UUID:** `0000af00-0000-1000-8000-00805f9b34fb`

| Characteristic | UUID | Properties | Purpose |
|----------------|------|------------|---------|
| AF01 | `0000af01-0000-1000-8000-00805f9b34fb` | Notify, Write | Data polling & control (read/write) |
| AF02 | `0000af02-0000-1000-8000-00805f9b34fb` | Notify, Write | Bind handshake & hardware info query |

---

## Connection Flow

```
Connect
  │
  ├── Enable notifications on AF01
  ├── Wait ~1.0s (let GATT settle)
  │
  ├── Bind Handshake (AF02)
  │     ├── Enable notifications on AF02
  │     ├── Write BindReq (0x18)
  │     ├── Wait for BindResp (0x19)
  │     └── Disable notifications on AF02
  │
  ├── Hardware Info Query (AF02, one-time)
  │     ├── Enable notifications on AF02
  │     ├── Write HardwareInfoReq (0xE0)
  │     ├── Wait for HardwareInfoResp (0xE1)
  │     └── Disable notifications on AF02
  │
  └── Polling/Control Loop (AF01)
        ├── Write command
        ├── Wait 100ms
        ├── Write next command
        ├── ... (19 commands per cycle for 6-channel devices)
        └── Collect & parse notification responses
```

---

## Bind Handshake (AF02)

After connecting, the client must register itself with the charger. The UUID is generated
once per client instance (random UUID, 16 bytes).

### BindReq (0x18)

Written to AF02. Total length: 19 bytes.

```
Offset  Length  Field
──────  ──────  ─────────────
0       1       Command: 0x18
1       16      Client UUID (random, 16 bytes)
17      1       Reserved: 0x00
18      1       Status: 0x00
```

### BindResp (0x19)

Received via AF02 notification.

```
Offset  Length  Field
──────  ──────  ─────────────
0       1       Command: 0x19
1       1       Bound status (0 = OK)
```

---

## Hardware Info Query (AF02)

One-time query after connect to retrieve firmware version, hardware version, and serial number.

### HardwareInfoReq (0xE0)

Written to AF02. Single byte.

```
Offset  Length  Field
──────  ──────  ─────────────
0       1       Command: 0xE0
```

### HardwareInfoResp (0xE1)

Received via AF02 notification. Total length: 13 bytes.

```
Offset  Length  Field
──────  ──────  ─────────────
0       1       Command: 0xE1
1       1       HW version major
2       1       HW version minor
3       1       FW version major
4       1       FW version minor
5       8       Device ID (uint64, little-endian) → serial number
```

---

## Response Frame Format

All AF01 notification responses from ISDT chargers share a common frame structure:

```
Byte 0: 0x31  Frame header (all charger and adapter models)
Byte 1: CMD   Command/response identifier
Byte 2+:      Command-specific payload
```

The `0x31` frame header is used by all known ISDT BLE devices (C4 Air, A8 Air, NP2 Air,
MASS2, etc.). It serves as a transport-layer framing byte. The parser does not need to
evaluate byte 0 — it is constant across all devices.

---

## Read Commands (AF01) – from mtheli/isdt_air_ble

All read commands are written to AF01. Responses arrive as AF01 notifications.
Commands are sent one at a time with a 100ms interval.

### Command Cycle

One full cycle consists of 1 + (N × 3) commands, where N is the number of channels:
- 6-channel devices (C4 Air, etc.): 19 commands total
- 8-channel devices (A8 Air): 25 commands total

| # | Command | Channel | Description |
|---|---------|---------|-------------|
| 1 | AlarmToneReq | — | Query alarm tone on/off |
| 2–4 | WorkState, Electric, IR | 0 | Slot 1 data |
| 5–7 | WorkState, Electric, IR | 1 | Slot 2 data |
| 8–10 | WorkState, Electric, IR | 2 | Slot 3 data |
| 11–13 | WorkState, Electric, IR | 3 | Slot 4 data |
| 14–16 | WorkState, Electric, IR | 4 | Slot 5 data |
| 17–19 | WorkState, Electric, IR | 5 | Slot 6 data |

At 100ms per command, one full cycle takes approximately **1.9 seconds**.

### AlarmToneReq (0x12 0x92)

Queries the current alarm tone status.

```
Write:    [0x12, 0x92]
Response: [0x31, 0x93, state]
```

`state`: 0 = off, non-zero = on.

### ElectricReq (0x12 0xE4)

Queries voltages, currents, and cell voltages for a channel.

```
Write:    [0x12, 0xE4, channel]
Response: [0x31, 0xE5, channel, ...]
```

**ElectricResp (0xE5)** — two formats depending on response length:

**Long format (> 35 bytes):** 4-byte voltages, up to 16 cells.

```
Offset  Length  Field               Unit
──────  ──────  ─────────────       ────
0       1       Frame header: 0x31
1       1       Command: 0xE5
2       1       Channel (0–5)
3       4       Input voltage       mV (LE) → ÷1000 = V
7       4       Input current       mA (LE) → ÷1000 = A
11      4       Output voltage      mV (LE) → ÷1000 = V
15      4       Charging current    mA (LE) → ÷1000 = A
19      32      Cell voltages ×16   mV (LE, 2 bytes each) → ÷1000 = V
```

**Short format (≤ 35 bytes):** 2-byte voltages, up to 8 cells.

```
Offset  Length  Field               Unit
──────  ──────  ─────────────       ────
0       1       Frame header: 0x31
1       1       Command: 0xE5
2       1       Channel (0–5)
3       2       Input voltage       mV (LE) → ÷1000 = V
5       4       Input current       mA (LE) → ÷1000 = A
9       2       Output voltage      mV (LE) → ÷1000 = V
11      4       Charging current    mA (LE) → ÷1000 = A
15      16      Cell voltages ×8    mV (LE, 2 bytes each) → ÷1000 = V
```

### WorkStateReq (0x13 0xE6)

Queries charge state, capacity, battery type, timing, and error info for a channel.

```
Write:    [0x13, 0xE6, channel]
Response: [0x31, 0xE7, channel, ...]
```

**WorkStateResp (0xE7):**

```
Offset  Length  Field                       Unit / Values
──────  ──────  ─────────────               ─────────────
0       1       Frame header: 0x31
1       1       Command: 0xE7
2       1       Channel (0–5)
3       1       Work state                  See table below
4       1       Capacity percentage         0–100 (%)
5       4       Capacity done               mAh (LE)
9       4       Energy done                 mWh (LE)
13      4       Work period                 ms (LE)
17      1       Battery type                See table below
18      1       Unit serials count
19      1       Link type
20      2       Full charged voltage        mV (LE) → ÷1000 = V  ← **Cut‑off / termination voltage**
22      4       Work current                mA (LE) → ÷1000 = A  ← **Charge current**
26      2       Battery count (whole)       LE
28      2       Battery count (current)     LE
30      2       Min input voltage           mV (LE) → ÷1000 = V
32      4       Max output power            mW (LE) → ÷1000 = W  ← **Capacity limit** (interpreted as such by the app)
36      2       Error code                  LE (0 = no error)
38      1       Parallel state (optional)   0 or 1
```

> **Note:** The fields `full_charged_volt_mV`, `work_current_mA`, and `max_output_power_mW` are used by the ISD Link app and this tool to display and **set** the charging parameters. They are read from this response and can be written back using `WorkTasksReq` (see below).

**Work State values:**

| Value | State | Description |
|-------|-------|-------------|
| 0 | idle | No activity |
| 1 | charging | Pre-charge / trickle phase |
| 2 | charging | CC (constant current) phase |
| 3 | charging | Active charging |
| 4 | charging | CV (constant voltage) / topping phase |
| 5 | error | Charging error |
| 6 | done | Fully charged |

**Battery Type values:**

| Value | Type | Description |
|-------|------|-------------|
| 0 | LiHV | 4.35V Lithium High Voltage |
| 1 | LiIon | 4.20V Standard Lithium-Ion |
| 2 | LiFe | 3.65V Lithium Iron Phosphate (LiFePO4) |
| 3 | NiZn | Nickel-Zinc |
| 4 | NiMH/Cd | Nickel Metal Hydride / Cadmium |
| 5 | LiIon | 1.50V Lithium-Ion (special variant) |
| 6 | Auto | Automatic detection |

### IRReq (0x13 0xFA)

Queries internal resistance per cell for a channel.

```
Write:    [0x13, 0xFA, channel]
Response: [0x31, 0xFB, channel, ...]
```

**IRResp (0xFB):**

```
Offset  Length  Field               Unit
──────  ──────  ─────────────       ────
0       1       Frame header: 0x31
1       1       Command: 0xFB
2       1       Channel (0–5)
3       N×2     IR per cell         0.1 mΩ (LE, 2 bytes each)
```

Number of cells is derived from response length:
- ≥ 20 bytes → 16 cells
- \> 15 bytes → 8 cells
- = 15 bytes → 6 cells
- else → (length − 3) / 2

Values of 0 or ≥ 10000 (1000 mΩ) are treated as invalid / no cell present.

---

## Write Commands (AF01) – developed by DittelHome/ISDT-Charge-Utility

These commands allow **setting** charging parameters on the device.
All write commands are written to AF01. The device usually acknowledges with a short response.

### WorkTasksReq (0x13 0xEA) – Set Charging Parameters

Sets battery type, charge current, capacity limit, and cut‑off voltage for a specific channel.

```
Write:    [0x13, 0xEA, channel, task_type, battery_type, linking_type,
           work_current (4B LE), cells, full_changed_volt (2B LE),
           capacity_limit (4B LE)]
Response: [0x31, 0xEB, channel, error_code]
```

**WorkTasksReq format:**

```
Offset  Length  Field               Unit / Values
──────  ──────  ─────────────       ─────────────
0       1       Command: 0x13
1       1       Sub-command: 0xEA
2       1       Channel (0–5)
3       1       Task type           0 = charge (default)
4       1       Battery type        See Battery Type values
5       1       Linking type        0 (default)
6       4       Work current        mA (LE) → ÷1000 = A
10      1       Cells               0 = auto, 1–16 for LiFe/LiIon/LiHV
11      2       Full changed volt   mV (LE) → ÷1000 = V (cut‑off voltage)
13      4       Capacity limit      mAh (LE) → 0 = unlimited
```

**WorkTasksResp (0xEB):**

```
Offset  Length  Field
──────  ──────  ─────────────
0       1       Frame header: 0x31
1       1       Command: 0xEB
2       1       Channel (0–5)
3       1       Error code          0 = OK, 0xFF = error
```

### AlarmToneSet (0x13 0x9C)

Sets the alarm tone on/off.

```
Write:    [0x13, 0x9C, task_type]
Response: [0x31, 0x9D, status]
```

`task_type`: 0 = off, 1 = on.

**AlarmToneTaskResp (0x9D):**

```
Offset  Length  Field
──────  ──────  ─────────────
0       1       Frame header: 0x31
1       1       Command: 0x9D
2       1       Status              0xFF = success, -1 = error
```

---

## Device Discovery

ISDT chargers advertise with manufacturer data using company ID `0xABBA` (43962).
The device model is identified from bytes 2–5 of the manufacturer data payload:

| Bytes [2:6] | Model |
|-------------|-------|
| `01030000` | C4 Air |
| `01040000` | C4 EVO |
| `01070000` | C4 Air |
| `010f00a8` | A8 Air |
| ... | ... |

---

## A8 Air Protocol Differences

The A8 Air (8-channel charger) uses an enhanced protocol with the following differences
from other ISDT chargers.

### WorkState Mega-Packet

Instead of individual per-channel WorkState responses, the A8 Air sends a single
**203-byte mega-packet**:

```
Format: [0x31, 0xE7, total_channels, channel_data × 8]
```

Byte 2 contains `total_channels` (= 8), not a single channel ID.

### Electric Responses

The A8 Air sends **9-byte Electric responses** (versus longer responses on other models):

```
Offset  Length  Field           Unit
──────  ──────  ───────────     ────
0       1       Frame header: 0x31
1       1       Command: 0xE5
2       1       Channel
3       2       Input voltage   mV (LE) → ÷1000 = V
5       4       Input current   mA (LE) → ÷1000 = A
```

### Feature Differences

| Feature | C4 Air | A8 Air |
|---------|--------|--------|
| Alarm tone | Yes | No |
| Cell voltages | Up to 16 per slot | Not available |
| IR per cell | Separate IRResp | Included in mega-packet |
| Write commands | Yes | Unknown |

---

## Timing

| Parameter | Value | Notes |
|-----------|-------|-------|
| Post-connect settle | 1.0s | Wait after GATT connection before bind |
| Post-notification setup | 0.5s | Wait after enabling AF01 notifications |
| Command interval | 100ms | Delay between individual polling commands |
| Full cycle (6-channel) | ~1.9s | 19 commands × 100ms (C4 Air) |
| Full cycle (8-channel) | ~2.5s | 25 commands × 100ms (A8 Air) |
| Bind timeout | 3.0s | Max wait for BindResp on AF02 |
| Hardware info timeout | 3.0s | Max wait for HardwareInfoResp on AF02 |

---

## Known Quirks

- **Disconnect after ~2 minutes:** The charger may randomly disconnect the BLE connection.
  This appears to be a firmware behavior, not caused by the client. The integration handles
  this by automatically reconnecting.

- **Long/short ElectricResp format:** The response length varies by device model. Devices
  supporting more cells use the long format with 4-byte voltage fields.

- **HardwareInfoResp offset:** Some devices include the 0x31 frame header before the
  command byte (0xE1), others send 0xE1 at position 0. The parser checks both positions.

- **Energy reset on WorkTasksReq:** When a WorkTasksReq is sent, the Energy (mWh) counter
  is reset to 0. This is normal device behavior.

- **NiMH charging protection:** NiMH/Cd batteries use Delta-Peak detection (-△V) for
  charge termination. The charger monitors for a small voltage drop (configurable 3–12 mV)
  indicating a full battery. An optional capacity limit (1000–4000 mAh) serves as a
  secondary safety cutoff.

---

## Source Attribution

| Part | Source |
|------|--------|
| **Read commands** (WorkState, Electric, IR, AlarmToneReq, HardwareInfo) | [mtheli/isdt_air_ble](https://github.com/mtheli/isdt_air_ble) |
| **Write commands** (WorkTasksReq, AlarmToneSet) | [DittelHome/ISDT-Charge-Utility](https://github.com/DittelHome/ISDT-Charge-Utility) – reverse‑engineered from ISD Link Android app |

---

## License

This documentation is provided under the **MIT License**. Feel free to use it in your own projects.
```
