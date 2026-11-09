# Protocol reference: the Trio II's J8 serial line

Everything here comes from my own captures: 6,811 frames over about 70 minutes across three sessions, including IHD restarts and a midnight rollover, on a Geo Trio II (T2CO-A-PCB-001_3, © 2022) connected to SMETS2 electricity and gas meters in the UK. The numbers in the examples are real.

[`tools/decode_log.py`](../tools/decode_log.py) implements all of this in about 150 lines of Python, and [`firmware/trio_parser.h`](../firmware/trio_parser.h) is the C++ version running on the ESP32-C3.

## Physical layer

- J8 pin 1 is the TX of the JN5169 Zigbee chip's UART0.
- **115200 baud, 8N1, 3.3 V logic, idle high.**
- Only listen. Nothing needs to be sent to the IHD.

## Framing

```
01 | type (2) | length L (2) | checksum (1) | data (L + 2) | 03
```

- `01` starts a frame and `03` ends it.
- **Escaping:** any byte from 0x00 to 0x07 inside the frame is sent as `02` followed by `byte XOR 0x08`. In all my captures, only 0x08 to 0x0F ever follow a `02`, which confirms XOR 0x08 (not 0x10).
- After unescaping, the frame (without `01`/`03`) must be exactly **L + 7** bytes.
- **Checksum:** XOR of every unescaped byte except the checksum byte itself.

```python
def checksum_ok(u):            # u = unescaped bytes, without 01 / 03
    x = 0
    for i, b in enumerate(u):
        if i != 4:
            x ^= b
    return x == u[4] and len(u) == ((u[2] << 8) | u[3]) + 7
```

Every intact frame I captured (6,797 of 6,811) passes. The rest were split or merged frames, plus the debug text described below. Dropping frames that fail is what stops the occasional absurd reading: a damaged frame can still contain a pattern a parser recognises, with garbage after it.

## Message types

| Type | Count in captures | What it is |
|---|---|---|
| **0x8002** | most data | **Zigbee APS data indication**: everything from the meters |
| 0x8028 | rare | **plain-text debug log from the Zigbee chip** (see below) |
| 0x8008, 0x8009, 0x800A, 0x8012, 0x8014, 0x8023, 0x8024, 0x802A, 0x8071, 0x8073 | various | Zigbee stack status and events |
| 0x7FFF, 0x0072, 0x0514 | various | requests from the IHD side / housekeeping |

Only 0x8002 carries meter data. [`firmware/trio_known.h`](../firmware/trio_known.h) lists all 200 message *kinds* (type + endpoint + cluster + attribute or command) I've seen, which the firmware uses to flag anything new.

### 0x8028: the Zigbee chip's debug text

After the frame header and two more bytes, the data is plain ASCII. Examples from my captures:

```
**** R22Power Up! ****
Start Main: Stack ptr is 0x4007fdc usage 35
Beacon Epid ... Pjoin 0 Rcap 1 Zcap 1 LQI 58 PanId .... Chan 11 Countt 1
Initiating rejoin as MAC restored
```

**Some of these fail the checksum.** If you count bad checksums as "damaged frames", you'll think your wiring is flaky when it isn't. Mine did exactly that: two overnight "damaged frames" turned out to be `Initiating rejoin as MAC restored`, the IHD briefly losing and rejoining the meter's network. Check for type 0x8028 before the checksum test.

## Inside a 0x8002 frame

A real frame: live power plus the meter reading, received at 23:01 on 5 Nov 2026.

Raw, as it appears on the wire:

```
01 80 02 0A 02 08 3D C5 1E FF 02 08 AB 02 09 09 02 0F 02 0A 02 09 02 09 02 0A 0C D8 02 0A A4 58 43 02 08 08 A7
BD 02 08 28 18 BB 02 09 02 08 02 0C 02 08 2A 70 02 0A 02 08 02 08 02 08 02 08 25 C6 7A 19 02 09 02 08 02 08 02
09 02 08 02 08 25 0B 02 08 02 08 02 08 02 08 02 08 14 02 08 02 08 30 02 0A 02 08 02 0A 02 08 18 02 08 03
```

Unescaped and annotated:

```
80 02                      type 0x8002 (APS data indication)
00 3D                      length 61
C5                         checksum
1E FF 00 AB                status / sequence
01 09                      profile 0x0109 (Zigbee Smart Energy)
07 02                      cluster 0x0702 (Metering)
01 01                      source endpoint 1, destination endpoint 1  (1 = electricity)
02 0C D8 02 A4 58 ...      addressing: meter's short address 0x0CD8 -> IHD 0xA458, link quality etc.
00 28                      ZCL payload length 40
18 BB 01                   ZCL header: frame control 0x18 (server to client), sequence 0xBB,
                           command 0x01 (Read Attributes Response)
00 04 00 2A 70 02 00       attr 0x0400, status OK, int24  = 0x000270 =        624  -> 624 W
00 00 00 25 C6 7A 19 01 00 00
                           attr 0x0000, status OK, uint48 = 0x01197AC6 = 18447046 -> 18,447.046 kWh
01 00 00 25 0B 00 00 00 00 00
                           attr 0x0001, status OK, uint48 = 11 -> 0.011 kWh (export register)
14 00 00 30 02             attr 0x0014, enum8 = 2 (supply status)
00 02 00 18 00             attr 0x0200, bitmap8 = 0 (meter status)
```

Attribute IDs and values are **little-endian**. A practical way to find the ZCL header: locate `01 09` (the profile), read the cluster and endpoint after it, then scan forward for a frame-control byte that's preceded by a plausible 2-byte payload length. That's what both decoders do.

**The profile is always at byte 9** (in all 1,257 meter-data frames I've captured). Search from there, not from the start of the frame: the checksum and status bytes before it can read `01 09` by coincidence. That happened once on my install, filing a normal reading under "endpoint 171, cluster 0x4100".

## Endpoints

| Endpoint | Clusters seen | Contents |
|---|---|---|
| **1** | 0x0702, 0x0705, 0x0700, 0x000A, 0x0800 | electricity: readings, costs, tariff; meter clock |
| **2** | 0x0700 | **gas tariff** only |
| **3** | 0x0702, 0x0705 | **gas** readings and costs |

## Metering (0x0702) and cost (0x0705) attributes

The meter tells you its own scaling: attribute 0x0300 (unit of measure) is 0 = kWh for electricity and 1 = m³ for gas, and 0x0302 (divisor) is 1000 for both.

| Attribute | Electricity (ep 1) | Gas (ep 3) | Type | Scale |
|---|---|---|---|---|
| Meter reading (import) | 0x0702 / 0x0000 | 0x0702 / 0x0000 | uint48 | ÷1000 → kWh / m³ |
| Exported | 0x0702 / 0x0001 | n/a | uint48 | ÷1000 → kWh |
| Live power | 0x0702 / 0x0400 | n/a | int24 | W |
| Today | 0x0702 / 0x0401 | 0x0702 / 0x0C01 | uint24 | ÷1000 → kWh |
| Yesterday | 0x0702 / 0x0403 | 0x0702 / 0x0C03 | uint24 | ÷1000 → kWh |
| 2 to 8 days ago | 0x0702 / 0x0420, 0x0422 … 0x042C | 0x0C20 … 0x0C2C | uint24 | ÷1000 → kWh |
| This week | 0x0702 / 0x0430 | 0x0C30 | uint24 | ÷1000 → kWh |
| 1 to 5 weeks ago | 0x0432 … 0x043A | 0x0C32 … 0x0C3A | uint24 | ÷1000 → kWh |
| This month | 0x0702 / 0x0440 | 0x0C40 | uint32 | ÷1000 → kWh |
| 1 to 13 months ago | 0x0442 … 0x045A | 0x0C42 … 0x0C5A | uint32 | ÷1000 → kWh |
| Cost today | 0x0705 / 0x051C | 0x0705 / 0x051C | uint48 | ÷100,000 → £ |
| Cost yesterday | 0x0705 / 0x051E | same | | ÷100,000 → £ |
| Cost history (days, weeks, months) | 0x0520 …, 0x0530 …, 0x0540 … | same | | ÷100,000 → £ |

Gas consumption is reported **in kWh** in the 0x0Cxx range (the "alternative unit" registers), already converted by the meter, while the gas meter reading (0x0000) is in m³.

Costs **include the standing charge**: electricity "cost today" was 60p a few seconds after midnight, which is exactly the daily standing charge.

### When things arrive

- **Electricity**: live power and meter reading roughly every 10 seconds.
- **Gas**: every couple of minutes, but the value only changes every 30 minutes (the gas meter's reporting interval).
- **Startup burst**: 15 to 30 seconds after the IHD powers up, it reads everything: history, formatting, tariff. If your listener boots *with* the IHD (powered from it), you catch this.
- **Midnight**: the "today" values reset (electricity first, gas when its next update arrives), and the **tariff is re-sent** between about 00:03 and 00:10.

## Price cluster commands

The tariff comes as Price cluster (0x0700) **server-to-client commands** (frame control bit 0 set), not attributes. Endpoint 1 is electricity, endpoint 2 is gas.

| Command | Name | What I use from it |
|---|---|---|
| 0x00 | Publish Price | unit rate |
| 0x02 | Publish Conversion Factor | gas volume correction factor |
| 0x03 | Publish Calorific Value | gas CV |
| 0x04 | Publish Tariff Information | standing charge |
| 0x09 | Publish Billing Period | (not decoded) |

The reliable anchor is the **currency code `3A 03`** (826, GBP, little-endian). Payloads below are the ZCL payload, after the 3-byte ZCL header.

### Publish Price (0x00): electricity

```
1F D5 B3 70                provider ID
07 52 61 74 65 20 30 31    rate label, length-prefixed: "Rate 01"
95 01 00 00                issuer event ID
E4 C8 7F 32                current time
00                         unit of measure
3A 03                      currency 826 = GBP
41                         trailing digits (high nibble) = 4, tier (low nibble) = 1
11                         number of tiers / register tier
C9 84 7E 32                start time
FF FF                      duration (until changed)
32 0A 00 00                PRICE = 0x0A32 = 2610 -> 2610 / 10^4 = 0.2610 GBP/kWh
FF FF ...                  price ratio, generation price, alternate cost etc. (unused)
```

### Publish Price (0x00): gas

```
... 3A 03  51  41  2C C5 54 32  FF FF  BF 1E 00 00 ...
           |   |   start time   dur.   PRICE = 0x1EBF = 7871, 5 digits -> 0.07871 GBP/kWh
           |   tiers
           trailing digits 5, tier 1
```

So: **price = uint32 at (currency offset + 10), decimal places = high nibble of (currency offset + 2).**

### Publish Tariff Information (0x04): standing charge

Electricity, label "Tariff":

```
... 06 54 61 72 69 66 66 ...  "Tariff"
... 3A 03  40  8F 17 00 00 ...
           |   STANDING CHARGE = 0x178F = 6031, 4 digits -> 0.6031 GBP/day
           trailing digits 4
```

Gas: `3A 03 50 9B 63 00 00` → 0x639B = 25499, 5 digits → **0.25499 GBP/day**.

So: **standing charge = uint32 at (currency offset + 3), decimal places = high nibble of (currency offset + 2).**

### Publish Conversion Factor (0x02), endpoint 2

```
A3 67 46 32  00 00 00 00  78 8F 01 00  50
issuer ID    start time   0x018F78     trailing digits 5 (high nibble)
                          = 102264 -> 1.02264
```

### Publish Calorific Value (0x03), endpoint 2

```
A3 67 46 32  00 00 00 00  93 01 00 00  01            10
issuer ID    start time   0x193 = 403  unit 01=MJ/m3  trailing digits 1 -> 40.3 MJ/m3
```

### Gas kWh from m³

```
kWh = m³ × volume correction × calorific value ÷ 3.6
    = m³ × 1.02264 × 40.3 ÷ 3.6
    = m³ × 11.448
```

This matches the meter's own kWh figures.

## Not yet confirmed

- ~~**Export with solar**~~: **confirmed** ([issue #1](https://github.com/gaza1994/Geo-Trio-ii-tap/issues/1)). Attribute 0x0001 on endpoint 1 is export and matches the Trio's display (6,700.787 → 6,703.116 kWh while import stayed at 14,524.833 kWh), and live power goes negative while exporting (−2,642 W). Export history registers (today, yesterday, months) haven't been captured yet.
- **Time-of-use tariffs**: mine has one rate (tier 1). Multi-rate tariffs presumably send several Publish Price commands or use the tier fields.
- **0x8073**: valid frame, empty payload, seen once. Probably an internal Zigbee stack event.
