# How it was worked out

The order things happened in, and the reasoning at each step. If you're decoding something similar, the method matters more than the answers.

## 1. The cloud goes away

October 2026: Geo retired the Geo Home app and its API. Home Assistant's Geo integration started failing as accounts were shut off. My Trio II also feeds Drayton Wiser Insights+ (via its built-in WiFi), which kept working, and I briefly wrote a custom integration against that cloud instead. But the point was to stop depending on anyone's cloud.

## 2. Someone's already been inside

[nadnerb33/geotrio2mqtt](https://github.com/nadnerb33/geotrio2mqtt) had found that the Zigbee chip's UART is exposed on **J8** on the 2017 board, with an Arduino sketch that pattern-matches a few readings. Two things were different for me:

- **My board is the 2022 revision** (T2CO-A-PCB-001_3), with integrated WiFi and no ground pad where the docs said.
- The sketch matches byte patterns in the raw stream. I wanted every reading, validated, and the tariff.

## 3. Find the points on an undocumented board

Multimeter only: ground by continuity to the USB shell (the BT2 battery contact is the easiest pad), 5 V on TP69, and on J8 the pad whose voltage *flickers*, since an idle-high UART dips with every byte. Details in [hardware.md](hardware.md).

## 4. Hold-a-wire test, then capture

An ESP32-C3 on ESPHome, [stage-1 config](../firmware/stage1-raw-logger.yaml): log every frame as hex, split on the `0x03` end byte. Jumpers held on J8 TX and ground, the C3 powered from a laptop. Frames appeared immediately.

Then three captures:

| Capture | Length | Frames | Why |
|---|---|---|---|
| 1 | 43 min | 3,020 | steady state: what arrives, how often |
| 2 | IHD restart | 884 | **the startup burst**: history, formatting, tariff |
| 3 | 23 min | 2,907 | more steady state, to confirm |

Later, deliberately: a capture across **midnight** (resets, tariff re-sent) and several IHD and C3 restarts.

## 5. Framing: confirm, don't assume

geotrio2mqtt unescapes with `XOR 0x08`. I briefly suspected it should be `XOR 0x10`, since that's common in similar protocols. The capture settled it: across 6,811 frames, the byte after an `02` escape was only ever 0x08 to 0x0F. With `XOR 0x08`, those decode to exactly 0x00 to 0x07, the bytes that need escaping. `XOR 0x08` is right.

Then length: after unescaping, every intact frame is exactly *length field + 7* bytes. Then the checksum: I tried XOR of everything except byte 4, and it matched **6,797 of 6,811** frames. Most of the 14 failures were visibly broken: two frames merged, or one cut short. (Some, it turned out much later, weren't broken at all; see step 9.)

That last point mattered: people using pattern-matching had reported occasional absurd gas readings. A merged frame can contain a valid-looking pattern followed by bytes from the next frame. Validating length and checksum makes that impossible.

## 6. Structure: Zigbee, not a custom protocol

Message type 0x8002 frames contain `01 09`, Zigbee's Smart Energy profile ID. After it come a cluster ID and endpoints, and further on a standard ZCL header (frame control, sequence number, command) carrying **Read Attributes Responses**. So the meat is plain ZCL, documented in the Smart Energy spec. From there it's identifying which cluster/attribute is which:

- 0x0702 Metering: 0x0000 meter reading, 0x0400 live power, 0x0401 today...
- 0x0705 Prepayment: costs, in units of £0.00001.

And the surprise: **gas is on endpoint 3**, not 2. Endpoint 2 only carries the gas *tariff*.

Every decoded value was checked against the IHD's own screen: power, today, yesterday, this month, costs. All matched.

## 7. The tariff

The startup burst included Price cluster frames that weren't attribute reports, but **commands** (ZCL frame control with the cluster-specific bit set): Publish Price, Publish Tariff Information, Publish Conversion Factor, Publish Calorific Value.

The way in was the currency code, **826 (GBP) = `3A 03`**. In Publish Price for electricity, ten bytes after it sat `32 0A 00 00` = 2610, and the byte just after the currency had 4 in its high nibble: 2610 ÷ 10⁴ = **£0.2610/kWh**, my unit rate. The gas price, standing charges, volume correction factor (1.02264) and calorific value (40.3 MJ/m³) fell out the same way, each confirmed against my bill. Byte-by-byte layouts are in [protocol.md](protocol.md#price-cluster-commands).

Watching across midnight showed the meter **re-sends the tariff daily**, between about 00:03 and 00:10. So a tariff change reaches Home Assistant within a day, even with no restart.

## 8. Building the firmware in stages

- **Stage 1**: raw hex logging only (still in the repo).
- **Stage 2**: decoded sensors, verified against the screen.
- **Hardening**: checksum validation; a list of every message kind seen (198 at first, 200 now) with alerts for anything new; tariff persisted in flash and debounced; midnight rollover for "yesterday"; yesterday values remembered across restarts but only if saved the same day.
- **Testing restarts deliberately**: OTA update, pulling the C3's power, pulling the IHD's power, both together. The tariff and yesterday values must come back; they did.
- **Home Assistant correctness**: state classes chosen so long-term statistics can't be corrupted; the gas kWh running total.
- **Lightening the load**: filters cut database writes by about 80%.

## 9. The "damaged frames" that weren't

After soldering, a few frames a night still failed the checksum. Bad joint? I added a sensor logging the raw bytes of each one. Decoded, they were type **0x8028**, plain ASCII from the Zigbee chip:

```
Initiating rejoin as MAC restored
```

The IHD briefly losing and rejoining the meter's network, and the chip logging it. These debug frames don't always carry a valid checksum. Now they're shown as **IHD status messages** and aren't counted as damage; the damaged-frame counter sits at zero.

Lesson: when a counter says something's wrong, log *what* it's counting before believing it.

## 10. A false match

A "new message kind" alert arrived for *endpoint 171, cluster 0x4100*. Decoded by hand, it was an ordinary electricity report. The decoder searched for the Smart Energy marker `01 09` from the start of the frame, and in this one the checksum was `01` and the next status byte `09`. Across every captured meter frame the real marker is at byte 9, so the search now starts there. All 6,811 captured frames decode identically before and after.

## 11. Confirmed by others

A solar user confirmed export is attribute 0x0001 and that live power goes negative while exporting ([issue #1](https://github.com/gaza1994/Geo-Trio-ii-tap/issues/1)).

## 12. What's still unknown

See [protocol.md](protocol.md#not-yet-confirmed): time-of-use tariffs, export history registers, and message type 0x8073.
