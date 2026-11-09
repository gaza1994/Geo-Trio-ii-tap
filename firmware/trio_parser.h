// Geo Trio II J8 decoder - NXP JN5169 serial frames -> Zigbee Smart Energy data.
// Frame: 0x01 .. 0x03; bytes 0x00-0x07 escaped as 0x02,(b^0x08); length + XOR checksum checked.
// Pure C++ (no ESPHome dependencies) so it can be unit-tested on a PC.
#pragma once
#include <vector>
#include <string>
#include <cstdint>
#include <cstddef>
#include <cstdio>

struct TrioAttr { uint8_t endpoint; uint16_t cluster; uint16_t attr; int64_t raw; };

struct TrioFrame {
  bool ok = false;            // well-formed frame
  uint16_t msg_type = 0;      // e.g. 0x8002 = incoming Zigbee data
  bool zcl = false;           // a Smart Energy (0x0109) ZCL payload was found
  uint8_t endpoint = 0;
  uint16_t cluster = 0;
  uint8_t frame_ctrl = 0;
  uint8_t cmd = 0;
  bool cluster_specific = false;
  std::vector<uint8_t> payload;   // ZCL payload after the command byte
  std::vector<TrioAttr> attrs;    // decoded attributes (global read-response / report)
  std::vector<uint8_t> unescaped;
};

static inline int trio_type_size(uint8_t t) {
  switch (t) {
    case 0x08: case 0x10: case 0x18: case 0x20: case 0x28: case 0x30: return 1;
    case 0x09: case 0x19: case 0x21: case 0x29: case 0x31: return 2;
    case 0x0a: case 0x1a: case 0x22: case 0x2a: return 3;
    case 0x0b: case 0x1b: case 0x23: case 0x2b: case 0xe2: return 4;
    case 0x0c: case 0x1c: case 0x24: case 0x2c: return 5;
    case 0x0d: case 0x1d: case 0x25: case 0x2d: return 6;
    case 0x0e: case 0x1e: case 0x26: case 0x2e: return 7;
    case 0x0f: case 0x1f: case 0x27: case 0x2f: return 8;
    default: return 0;
  }
}
static inline bool trio_type_signed(uint8_t t) { return t >= 0x28 && t <= 0x2f; }

static inline uint32_t trio_u32(const std::vector<uint8_t> &b, size_t i) {
  return uint32_t(b[i]) | (uint32_t(b[i+1]) << 8) | (uint32_t(b[i+2]) << 16) | (uint32_t(b[i+3]) << 24);
}

static inline TrioFrame trio_decode(const std::vector<uint8_t> &raw) {
  TrioFrame f;
  if (raw.size() < 4 || raw.front() != 0x01 || raw.back() != 0x03) return f;
  auto &u = f.unescaped; u.reserve(raw.size());
  bool esc = false;
  for (size_t i = 1; i + 1 < raw.size(); i++) {
    uint8_t b = raw[i];
    if (esc) { u.push_back(b ^ 0x08); esc = false; }
    else if (b == 0x02) esc = true;
    else u.push_back(b);
  }
  // NXP frame: type(2) length(2) checksum(1) data(length+2). Checksum = XOR of every other byte.
  // Verified on 6,797 captured frames; rejects frames damaged by dropped or merged bytes.
  if (u.size() < 7) return f;
  uint16_t len = (uint16_t(u[2]) << 8) | u[3];
  if (u.size() != size_t(len) + 7) return f;
  uint8_t x = 0;
  for (size_t i = 0; i < u.size(); i++) if (i != 4) x ^= u[i];
  if (x != u[4]) return f;
  f.ok = true;
  f.msg_type = (uint16_t(u[0]) << 8) | u[1];
  if (f.msg_type != 0x8002) return f;
  size_t p = 0; bool found = false;
  // Profile 0x0109 is always at byte 9 (1,257 of 1,257 captured frames). Starting the search there stops
  // the checksum + status bytes being mistaken for it when they happen to read 01 09 (seen 9 Nov 2026).
  for (size_t i = 9; i + 7 < u.size(); i++)
    if (u[i] == 0x01 && u[i + 1] == 0x09) { p = i; found = true; break; }
  if (!found) return f;
  f.cluster = (uint16_t(u[p + 2]) << 8) | u[p + 3];
  f.endpoint = u[p + 4];
  // ZCL header: frame control, seq, command; preceded by a 2-byte big-endian payload length.
  size_t z = 0; bool zf = false;
  for (size_t i = p + 10; i + 3 <= u.size(); i++) {
    uint8_t fc = u[i];
    if ((fc & 0xF4) != 0x00 && (fc & 0xF4) != 0x10) continue;   // server->client, no mfr code
    if (i < 2) continue;
    uint16_t len = (uint16_t(u[i - 2]) << 8) | u[i - 1];
    if (len >= 3 && i + len <= u.size() + 2) { z = i; zf = true; break; }
  }
  if (!zf) return f;
  f.zcl = true;
  f.frame_ctrl = u[z];
  f.cluster_specific = (u[z] & 0x03) == 0x01;
  f.cmd = u[z + 2];
  f.payload.assign(u.begin() + z + 3, u.end());
  if (f.cluster_specific || (f.cmd != 0x01 && f.cmd != 0x0a)) return f;
  // Global read-attributes response (0x01, has status byte) or report (0x0a)
  auto &pl = f.payload; size_t i = 0;
  while (i + 3 <= pl.size()) {
    uint16_t attr = uint16_t(pl[i]) | (uint16_t(pl[i + 1]) << 8); i += 2;
    if (f.cmd == 0x01) { uint8_t st = pl[i++]; if (st != 0x00) continue; }
    if (i >= pl.size()) break;
    uint8_t type = pl[i++];
    int sz = trio_type_size(type);
    if (sz == 0 || i + sz > pl.size()) break;
    uint64_t v = 0;
    for (int k = sz - 1; k >= 0; k--) v = (v << 8) | pl[i + k];
    i += sz;
    int64_t sv = int64_t(v);
    if (trio_type_signed(type) && sz < 8 && ((v >> (sz * 8 - 1)) & 1)) sv = int64_t(v | (~0ULL << (sz * 8)));
    f.attrs.push_back({f.endpoint, f.cluster, attr, sv});
  }
  return f;
}

// ---- IHD debug text (message type 0x8028) ----------------------------------------------
// The Zigbee chip prints status lines ("Initiating rejoin as MAC restored", "Beacon ... LQI 58").
// Layout after unescaping: type(2) length(2) checksum(1) 2 header bytes, then ASCII text.
// Some of these carry a checksum that doesn't validate, so they're read even when f.ok is false.
static inline bool trio_is_ihd_text(const TrioFrame &f) {
  return f.unescaped.size() >= 7 && f.unescaped[0] == 0x80 && f.unescaped[1] == 0x28;
}
static inline std::string trio_ihd_text(const TrioFrame &f) {
  std::string t;
  for (size_t i = 7; i < f.unescaped.size(); i++) { uint8_t ch = f.unescaped[i]; if (ch >= 0x20 && ch < 0x7f) t += (char) ch; }
  while (!t.empty() && t.back() == ' ') t.pop_back();
  while (!t.empty() && t.front() == ' ') t.erase(0, 1);
  return t;
}

// ---- Price cluster (0x0700) commands --------------------------------------------------
// Anchored on the currency code (0x033A = 826 = GBP), which sits at a fixed position
// relative to the fields we need in both Publish Price and Publish Tariff Information.
static inline int trio_find_gbp(const std::vector<uint8_t> &p, size_t from) {
  for (size_t i = from; i + 1 < p.size(); i++) if (p[i] == 0x3a && p[i + 1] == 0x03) return int(i);
  return -1;
}
static inline double trio_pow10(int n) { double d = 1; while (n-- > 0) d *= 10; return d; }

// Publish Price (cmd 0x00): returns unit price in GBP/kWh, or -1.
static inline double trio_publish_price(const TrioFrame &f) {
  if (!f.zcl || f.cluster != 0x0700 || !f.cluster_specific || f.cmd != 0x00) return -1;
  int g = trio_find_gbp(f.payload, 9);
  if (g < 0 || size_t(g) + 2 + 1 + 1 + 4 + 2 + 4 > f.payload.size()) return -1;
  int digits = f.payload[g + 2] >> 4;
  uint32_t price = trio_u32(f.payload, g + 2 + 1 + 1 + 4 + 2);
  if (price == 0 || price == 0xFFFFFFFF) return -1;
  return price / trio_pow10(digits);
}
// Publish Tariff Information (cmd 0x04): returns standing charge in GBP/day, or -1.
static inline double trio_standing_charge(const TrioFrame &f) {
  if (!f.zcl || f.cluster != 0x0700 || !f.cluster_specific || f.cmd != 0x04) return -1;
  int g = trio_find_gbp(f.payload, 16);
  if (g < 0 || size_t(g) + 2 + 1 + 4 > f.payload.size()) return -1;
  int digits = f.payload[g + 2] >> 4;
  uint32_t sc = trio_u32(f.payload, g + 3);
  if (sc == 0xFFFFFFFF) return -1;
  return sc / trio_pow10(digits);
}
// Publish Conversion Factor (cmd 0x02) / Publish Calorific Value (cmd 0x03)
static inline double trio_conversion_factor(const TrioFrame &f) {
  if (!f.zcl || f.cluster != 0x0700 || !f.cluster_specific || f.cmd != 0x02 || f.payload.size() < 13) return -1;
  return trio_u32(f.payload, 8) / trio_pow10(f.payload[12] >> 4);
}
static inline double trio_calorific_value(const TrioFrame &f) {
  if (!f.zcl || f.cluster != 0x0700 || !f.cluster_specific || f.cmd != 0x03 || f.payload.size() < 14) return -1;
  if (f.payload[12] != 0x01) return -1;   // only MJ/m3 understood
  return trio_u32(f.payload, 8) / trio_pow10(f.payload[13] >> 4);
}

// ---- "Have we seen this kind of message before?" ---------------------------------------
// Signature strings for anything not in the known list get reported to HA.
static inline std::string trio_hex(const std::vector<uint8_t> &b, size_t max_bytes = 120) {
  std::string s; char t[4];
  for (size_t i = 0; i < b.size() && i < max_bytes; i++) { snprintf(t, sizeof t, "%02X", b[i]); s += t; }
  if (b.size() > max_bytes) s += "...";
  return s;
}
