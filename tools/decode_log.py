#!/usr/bin/env python3
"""Decode a Geo Trio II J8 serial capture.

Reads an ESPHome log (lines containing "<<< 01 ... 03", as produced by the stage-1 raw
logging config in ../docs/hardware.md) or any text file with one hex frame per line,
and prints what each frame means: meter readings, costs, tariff, and the Zigbee chip's
own status messages. Mirrors firmware/trio_parser.h exactly.

    python3 decode_log.py capture.txt            # readable events
    python3 decode_log.py capture.txt --summary  # last value of everything + frame stats
"""
import re, sys
from collections import Counter, OrderedDict

ATTRS = {  # (endpoint, cluster, attribute): (name, scale, unit)
    (1, 0x0702, 0x0000): ("Electricity meter (import)", 1000, "kWh"),
    (1, 0x0702, 0x0001): ("Electricity exported", 1000, "kWh"),
    (1, 0x0702, 0x0400): ("Electricity power", 1, "W"),
    (1, 0x0702, 0x0401): ("Electricity today", 1000, "kWh"),
    (1, 0x0702, 0x0403): ("Electricity yesterday", 1000, "kWh"),
    (1, 0x0702, 0x0430): ("Electricity this week", 1000, "kWh"),
    (1, 0x0702, 0x0440): ("Electricity this month", 1000, "kWh"),
    (1, 0x0705, 0x051C): ("Electricity cost today", 100000, "GBP"),
    (1, 0x0705, 0x051E): ("Electricity cost yesterday", 100000, "GBP"),
    (1, 0x0705, 0x0530): ("Electricity cost this week", 100000, "GBP"),
    (1, 0x0705, 0x0540): ("Electricity cost this month", 100000, "GBP"),
    (3, 0x0702, 0x0000): ("Gas meter", 1000, "m3"),
    (3, 0x0702, 0x0C01): ("Gas today", 1000, "kWh"),
    (3, 0x0702, 0x0C03): ("Gas yesterday", 1000, "kWh"),
    (3, 0x0702, 0x0C30): ("Gas this week", 1000, "kWh"),
    (3, 0x0702, 0x0C40): ("Gas this month", 1000, "kWh"),
    (3, 0x0705, 0x051C): ("Gas cost today", 100000, "GBP"),
    (3, 0x0705, 0x051E): ("Gas cost yesterday", 100000, "GBP"),
    (3, 0x0705, 0x0530): ("Gas cost this week", 100000, "GBP"),
    (3, 0x0705, 0x0540): ("Gas cost this month", 100000, "GBP"),
}
FORMAT = {0x0300: "unit of measure", 0x0301: "multiplier", 0x0302: "divisor", 0x0303: "summation formatting", 0x0306: "metering device type"}


def attr_name(ep, cl, at):
    """Names for everything the IHD reads, including the history registers."""
    if (ep, cl, at) in ATTRS: return ATTRS[(ep, cl, at)]
    if cl == 0x000A and at in (0x0000, 0x0007): return (("Meter clock UTC" if at == 0 else "Meter clock local") + " (seconds since 2000-01-01)", 1, "")
    fuel = {1: "Electricity", 3: "Gas"}.get(ep)
    if cl == 0x0702 and at in FORMAT: return (f"{fuel or 'ep%d' % ep} meter {FORMAT[at]}", 1, "")
    if fuel and cl == 0x0702:
        base = 0x0400 if ep == 1 else 0x0C00
        o = at - base
        if 0x20 <= o <= 0x2C and o % 2 == 0: return (f"{fuel} {o // 2 - 14} days ago", 1000, "kWh")
        if 0x32 <= o <= 0x3A and o % 2 == 0: return (f"{fuel} {(o - 0x30) // 2} weeks ago", 1000, "kWh")
        if 0x42 <= o <= 0x5A and o % 2 == 0: return (f"{fuel} {(o - 0x40) // 2} months ago", 1000, "kWh")
    if fuel and cl == 0x0705:
        o = at - 0x0500
        if 0x20 <= o <= 0x2C and o % 2 == 0: return (f"{fuel} cost {o // 2 - 14} days ago", 100000, "GBP")
        if 0x32 <= o <= 0x3A and o % 2 == 0: return (f"{fuel} cost {(o - 0x30) // 2} weeks ago", 100000, "GBP")
        if 0x42 <= o <= 0x5A and o % 2 == 0: return (f"{fuel} cost {(o - 0x40) // 2} months ago", 100000, "GBP")
    return (f"ep{ep} cluster {cl:04X} attr {at:04X}", 1, "raw")


TYPE_SIZE = {**{t: 1 for t in (0x08, 0x10, 0x18, 0x20, 0x28, 0x30)}, **{t: 2 for t in (0x09, 0x19, 0x21, 0x29, 0x31)},
             **{t: 3 for t in (0x0a, 0x1a, 0x22, 0x2a)}, **{t: 4 for t in (0x0b, 0x1b, 0x23, 0x2b, 0xe2)},
             **{t: 5 for t in (0x0c, 0x1c, 0x24, 0x2c)}, **{t: 6 for t in (0x0d, 0x1d, 0x25, 0x2d)},
             **{t: 7 for t in (0x0e, 0x1e, 0x26, 0x2e)}, **{t: 8 for t in (0x0f, 0x1f, 0x27, 0x2f)}}


def unescape(raw):
    out, esc = [], False
    for b in raw[1:-1]:
        if esc: out.append(b ^ 0x08); esc = False
        elif b == 0x02: esc = True
        else: out.append(b)
    return out


def decode(raw):
    """Return a dict describing one frame (raw = list of ints including 01 ... 03)."""
    f = {"ok": False}
    if len(raw) < 4 or raw[0] != 0x01 or raw[-1] != 0x03: return f
    u = unescape(raw); f["u"] = u
    if len(u) >= 2: f["type"] = (u[0] << 8) | u[1]
    if len(u) >= 7 and u[0] == 0x80 and u[1] == 0x28:   # Zigbee chip debug text
        f["text"] = "".join(chr(c) for c in u[7:] if 0x20 <= c < 0x7f).strip()
    if len(u) < 7: return f
    if len(u) != ((u[2] << 8) | u[3]) + 7: return f
    x = 0
    for i, b in enumerate(u):
        if i != 4: x ^= b
    if x != u[4]: return f
    f["ok"] = True
    if f["type"] != 0x8002: return f
    p = next((i for i in range(9, len(u) - 7) if u[i] == 0x01 and u[i + 1] == 0x09), None)
    if p is None: return f
    f["cluster"] = (u[p + 2] << 8) | u[p + 3]; f["endpoint"] = u[p + 4]
    z = None
    for i in range(p + 10, len(u) - 2):
        fc = u[i]
        if (fc & 0xF4) not in (0x00, 0x10): continue
        ln = (u[i - 2] << 8) | u[i - 1]
        if ln >= 3 and i + ln <= len(u) + 2: z = i; break
    if z is None: return f
    f["specific"] = (u[z] & 0x03) == 0x01; f["cmd"] = u[z + 2]; pl = u[z + 3:]; f["payload"] = pl
    f["attrs"] = []
    if f["specific"] or f["cmd"] not in (0x01, 0x0a): return f
    i = 0
    while i + 3 <= len(pl):
        attr = pl[i] | (pl[i + 1] << 8); i += 2
        if f["cmd"] == 0x01:
            st = pl[i]; i += 1
            if st != 0: continue
        if i >= len(pl): break
        t = pl[i]; i += 1; sz = TYPE_SIZE.get(t, 0)
        if not sz or i + sz > len(pl): break
        v = int.from_bytes(bytes(pl[i:i + sz]), "little"); i += sz
        if 0x28 <= t <= 0x2f and v >> (sz * 8 - 1): v -= 1 << (sz * 8)
        f["attrs"].append((attr, v))
    return f


def tariff(f):
    """Price-cluster commands: returns (label, value) or None."""
    if f.get("cluster") != 0x0700 or not f.get("specific"): return None
    pl, cmd, ep = f["payload"], f["cmd"], f["endpoint"]
    fuel = {1: "Electricity", 2: "Gas"}.get(ep, f"ep{ep}")
    def gbp(start):
        return next((i for i in range(start, len(pl) - 1) if pl[i] == 0x3a and pl[i + 1] == 0x03), -1)
    u32 = lambda i: int.from_bytes(bytes(pl[i:i + 4]), "little")
    if cmd == 0x00:
        g = gbp(9)
        if g >= 0 and g + 14 <= len(pl): return (f"{fuel} unit rate", f"{u32(g + 10) / 10 ** (pl[g + 2] >> 4):.5f} GBP/kWh")
    if cmd == 0x04:
        g = gbp(16)
        if g >= 0 and g + 7 <= len(pl): return (f"{fuel} standing charge", f"{u32(g + 3) / 10 ** (pl[g + 2] >> 4):.5f} GBP/day")
    if cmd == 0x02 and len(pl) >= 13: return ("Gas volume correction factor", f"{u32(8) / 10 ** (pl[12] >> 4):.5f}")
    if cmd == 0x03 and len(pl) >= 14 and pl[12] == 0x01: return ("Gas calorific value", f"{u32(8) / 10 ** (pl[13] >> 4):.1f} MJ/m3")
    return None


def main():
    if len(sys.argv) < 2: sys.exit(__doc__)
    summary = "--summary" in sys.argv
    last, stats = OrderedDict(), Counter()
    for line in open(sys.argv[1], errors="replace"):
        line = re.sub(r"\x1b\[[0-9;]*m", "", line)
        m = re.search(r"((?:01)(?: [0-9A-Fa-f]{2})+ 03)\s*$", line.split("<<<")[-1].strip())
        if not m: continue
        ts = (re.search(r"\[(\d\d:\d\d:\d\d)", line) or [None, ""])[1]
        f = decode([int(h, 16) for h in m.group(1).split()])
        stats["frames"] += 1
        if "text" in f:
            stats["ihd text"] += 1
            if not summary: print(f"{ts} IHD says: {f['text']}")
            continue
        if not f["ok"]: stats["damaged"] += 1; continue
        stats[f"type {f['type']:04X}"] += 1
        t = tariff(f)
        if t:
            last[t[0]] = t[1]
            if not summary: print(f"{ts} TARIFF {t[0]}: {t[1]}")
        for attr, v in f.get("attrs", []):
            key = (f["endpoint"], f["cluster"], attr)
            name, scale, unit = attr_name(*key)
            val = (f"{v / scale:.3f} {unit}" if scale != 1 else f"{v} {unit}").strip()
            last[name] = val
            if not summary: print(f"{ts} {name}: {val}")
    if summary:
        print("Latest values:"); [print(f"  {k}: {v}") for k, v in last.items()]
        print("Frames:", dict(stats))


if __name__ == "__main__":
    main()
