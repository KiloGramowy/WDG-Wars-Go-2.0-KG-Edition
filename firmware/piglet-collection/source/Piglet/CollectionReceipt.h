#pragma once
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

// Protocol-only helpers stay independent of Arduino, Wi-Fi and the SD card.
namespace PigletCollection {
inline uint32_t remainingBudget(uint32_t started, uint32_t now, uint32_t budget) {
  const uint32_t elapsed = now - started; // millis() wrap is intentional.
  return elapsed >= budget ? 0 : budget - elapsed;
}

inline bool lowerHex(const char* text, size_t length) {
  if (!text || strlen(text) != length) return false;
  for (size_t i = 0; i < length; ++i)
    if (!((text[i] >= '0' && text[i] <= '9') ||
          (text[i] >= 'a' && text[i] <= 'f'))) return false;
  return true;
}

inline bool safeIdentifier(const char* text) {
  if (!text) return false;
  const size_t size = strlen(text);
  if (size < 12 || size > 64) return false;
  for (size_t i = 0; i < size; ++i) {
    const char c = text[i];
    if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
          (c >= '0' && c <= '9') || c == '_' || c == '-')) return false;
  }
  return true;
}

inline bool countsValid(uint64_t records, uint64_t wifi, uint64_t ble,
                        uint64_t duplicates) {
  // Subtraction avoids accepting an overflowing sum from a corrupt receipt.
  return wifi <= records && ble <= records - wifi &&
         duplicates == records - wifi - ble;
}

inline bool constantEqual(const char* left, const char* right, size_t size) {
  if (!left || !right || strlen(left) != size || strlen(right) != size) return false;
  unsigned char difference = 0;
  for (size_t i = 0; i < size; ++i) difference |= left[i] ^ right[i];
  return difference == 0;
}

inline bool receiptMessage(char* output, size_t capacity,
                           const char* collector, const char* digest,
                           uint64_t size, const char* importId,
                           uint64_t records, uint64_t wifi, uint64_t ble,
                           uint64_t duplicates) {
  if (!lowerHex(collector, 32) || !lowerHex(digest, 64) ||
      !lowerHex(importId, 32) || size > 2147483648ULL ||
      !countsValid(records, wifi, ble, duplicates)) return false;
  const int used = snprintf(output, capacity,
      "PC1\n%s\n%s\n%llu\n%s\n%llu\n%llu\n%llu\n%llu\n",
      collector, digest, static_cast<unsigned long long>(size), importId,
      static_cast<unsigned long long>(records),
      static_cast<unsigned long long>(wifi), static_cast<unsigned long long>(ble),
      static_cast<unsigned long long>(duplicates));
  return used > 0 && static_cast<size_t>(used) < capacity;
}
}  // namespace PigletCollection
