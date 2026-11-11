#pragma once
#include "CollectionReceipt.h"

namespace PigletCollection {
enum class FileProof { Missing, Matches, Different, Unreadable };
enum class HandoverAction { Retry, Rename, RemoveDuplicate, AnotherName, Complete };

// Used by the real SD handover as well as the portable fault/retry tests.
inline HandoverAction planHandover(FileProof source, FileProof destination) {
  if (source == FileProof::Missing)
    return destination == FileProof::Matches ? HandoverAction::Complete : HandoverAction::Retry;
  if (source != FileProof::Matches || destination == FileProof::Unreadable)
    return HandoverAction::Retry;
  if (destination == FileProof::Missing) return HandoverAction::Rename;
  if (destination == FileProof::Matches) return HandoverAction::RemoveDuplicate;
  return HandoverAction::AnotherName;
}

inline bool handoverCollisionName(char* output, size_t capacity,
                                  const char* digest, unsigned attempt) {
  if (!lowerHex(digest, 64) || attempt >= 32) return false;
  const int size = attempt == 0
      ? snprintf(output, capacity, "pc_%s.csv", digest)
      : snprintf(output, capacity, "pc_%s_%u.csv", digest, attempt);
  return size > 0 && static_cast<size_t>(size) < capacity;
}
} // namespace PigletCollection
