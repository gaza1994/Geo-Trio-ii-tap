# Changelog

## 2026-11-09

- **Firmware: decoder fix.** The Smart Energy marker (`01 09`) is now searched for from byte 9, where it always is. Previously the checksum and status bytes could match by coincidence and mis-file a normal reading as a new message kind. Same fix in `tools/decode_log.py`.
- **Firmware: Gas power** (kW) sensor, estimated from how far the gas reading moves between the meter's half-hourly reports. Drops to 0 after 40 minutes with no gas. Use it with `Gas unit rate` for gas cost per hour.
- **Firmware: live electricity power every reading** (~10 s) again, rather than a 30-second average, so it keeps pace with the IHD.
- **Docs: quick-start section** at the top of the README.
- **Docs: solar export confirmed** by [issue #1](https://github.com/gaza1994/Geo-Trio-ii-tap/issues/1): attribute 0x0001, and live power goes negative while exporting.

## 2026-11-07

- First publication.
