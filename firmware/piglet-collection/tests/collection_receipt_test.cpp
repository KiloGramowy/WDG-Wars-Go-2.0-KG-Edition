#include "../source/Piglet/CollectionReceipt.h"
#include <assert.h>
#include <limits.h>
#include <string>

int main() {
  using namespace PigletCollection;
  assert(remainingBudget(1000, 1000, 10000) == 10000);
  assert(remainingBudget(1000, 10999, 10000) == 1);
  assert(remainingBudget(1000, 11000, 10000) == 0);
  assert(remainingBudget(1000, 11001, 10000) == 0);
  assert(remainingBudget(UINT32_MAX - 100, 50, 10000) == 9849);
  const std::string collector(32, 'a'), digest(64, 'b'), importId(32, 'c');
  assert(lowerHex(collector.c_str(), 32));
  assert(!lowerHex("AAAA", 4));
  assert(!lowerHex("0/12", 4));
  assert(!lowerHex(nullptr, 0));
  assert(!lowerHex(collector.c_str(), 31));
  assert(safeIdentifier("piglet-0123456789ab"));
  assert(!safeIdentifier("short"));
  assert(!safeIdentifier("piglet-../../private"));
  assert(!safeIdentifier(std::string(65, 'x').c_str()));
  assert(countsValid(3, 1, 1, 1));
  assert(countsValid(0, 0, 0, 0));
  assert(!countsValid(3, 2, 2, 0));
  assert(!countsValid(3, 4, 0, 0));
  assert(!countsValid(0, UINT64_MAX, 1, 0));
  assert(!countsValid(UINT64_MAX, UINT64_MAX, 1, UINT64_MAX));
  assert(constantEqual("0123", "0123", 4));
  assert(!constantEqual("0123", "0124", 4));
  assert(!constantEqual("0123", "0123extra", 4));
  char message[512];
  assert(receiptMessage(message, sizeof(message), collector.c_str(),
                        digest.c_str(), 123, importId.c_str(), 3, 1, 1, 1));
  const std::string expected = "PC1\n" + collector + "\n" + digest +
      "\n123\n" + importId + "\n3\n1\n1\n1\n";
  assert(expected == message);
  assert(!receiptMessage(message, 10, collector.c_str(), digest.c_str(),
                         123, importId.c_str(), 3, 1, 1, 1));
  assert(!receiptMessage(message, sizeof(message), collector.c_str(), digest.c_str(),
                         2147483649ULL, importId.c_str(), 3, 1, 1, 1));
  assert(!receiptMessage(message, sizeof(message), collector.c_str(), "bad",
                         123, importId.c_str(), 3, 1, 1, 1));
  assert(!receiptMessage(message, sizeof(message), collector.c_str(), digest.c_str(),
                         123, importId.c_str(), 3, 1, 1, 2));
  return 0;
}
