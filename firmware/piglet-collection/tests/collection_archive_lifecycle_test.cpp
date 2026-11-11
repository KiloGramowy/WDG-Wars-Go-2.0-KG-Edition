// Portable lifecycle MODEL using the actual planner. This does not execute
// Arduino SD/FAT, persistence guarantees, or firmware HMAC implementation.
#include "../source/Piglet/CollectionHandover.h"
#include <cassert>
#include <map>
#include <set>
#include <string>

using PigletCollection::FileProof;
using PigletCollection::HandoverAction;

struct Model {
  std::map<std::string, std::string> files;
  std::set<std::string> unreadable;
  bool verifiedReceipt = true;
  bool failIntent = false, failRename = false, failRemove = false;
  bool crashAfterRename = false, failMetadata = false;
  bool intent = false, done = false;
  std::string plannedSource, plannedDestination;
  const std::string digest = std::string(64, 'a');

  FileProof proof(const std::string& path, const std::string& expected) const {
    if (unreadable.count(path)) return FileProof::Unreadable;
    const auto file = files.find(path);
    if (file == files.end()) return FileProof::Missing;
    return file->second == expected ? FileProof::Matches : FileProof::Different;
  }

  bool plan(const std::string& source, const std::string& destination) {
    if (failIntent) return false;
    intent = true; done = false;
    plannedSource = source; plannedDestination = destination;
    return true;
  }

  bool complete() {
    if (failMetadata) return false;
    done = true;
    return true;
  }

  bool handover(const std::string& source, const std::string& basename,
                const std::string& expected) {
    // Caller precondition, represented as a boolean in this model. The real
    // firmware verifies collector identity, digest, size and receipt HMAC.
    if (!verifiedReceipt) return false;
    const FileProof original = proof(source, expected);
    std::string destination = "/uploaded/" + basename;
    // The real overload has an explicit already-uploaded shortcut: it commits
    // metadata, never coalesces a file with itself by deleting it.
    if (source == destination) return original == FileProof::Matches && complete();
    for (unsigned attempt = 0; attempt <= 32; ++attempt) {
      const auto action = PigletCollection::planHandover(original, proof(destination, expected));
      if (action == HandoverAction::Retry) return false;
      if (action == HandoverAction::Complete) return complete();
      if (action == HandoverAction::Rename) {
        if (!plan(source, destination) || failRename) return false;
        files[destination] = files.at(source); files.erase(source);
        if (crashAfterRename) return false;
        assert(proof(destination, expected) == FileProof::Matches);
        return complete();
      }
      if (action == HandoverAction::RemoveDuplicate) {
        if (!plan(source, destination) || failRemove) return false;
        files.erase(source);
        return complete();
      }
      char alternative[96];
      if (!PigletCollection::handoverCollisionName(alternative, sizeof alternative,
                                                  digest.c_str(), attempt)) return false;
      destination = "/uploaded/" + std::string(alternative);
    }
    return false;
  }

  bool canDelete(const std::string& path, const std::string& expected,
                 bool active = false) const {
    return !active && verifiedReceipt && done && proof(path, expected) == FileProof::Matches;
  }
};

// Source-obligation MODEL only: flags stand for already persisted, schema-valid
// journal records, while receipt/handed sets stand for the firmware's verified
// HMAC and handover preconditions. This does not execute ArduinoJson or FAT.
struct SourceObligationsModel {
  bool uconsole = true, negotiated = true;
  std::set<std::string> obligations, verifiedReceipts, handed, offered;
  unsigned imports = 0;

  void announce(const std::set<std::string>& paths) {
    if (uconsole && negotiated) obligations.insert(paths.begin(), paths.end());
  }

  std::set<std::string> resume(const std::set<std::string>& pending) const {
    std::set<std::string> paths = pending;
    if (uconsole && negotiated) paths.insert(obligations.begin(), obligations.end());
    return paths;
  }

  bool offerRetired(const std::string& path, bool hostCommitted) {
    if (!uconsole || !negotiated || !verifiedReceipts.count(path) ||
        !handed.count(path) || !hostCommitted) return false;
    offered.insert(path); // Count source identity without importing bytes again.
    return true;
  }

  void finish(bool http200, bool isBoolean, bool finished) {
    if (uconsole && negotiated && http200 && isBoolean && finished) obligations.clear();
  }
};

int main() {
  using PigletCollection::planHandover;
  assert(planHandover(FileProof::Missing, FileProof::Matches) == HandoverAction::Complete);
  assert(planHandover(FileProof::Missing, FileProof::Missing) == HandoverAction::Retry);
  assert(planHandover(FileProof::Matches, FileProof::Missing) == HandoverAction::Rename);
  assert(planHandover(FileProof::Matches, FileProof::Matches) == HandoverAction::RemoveDuplicate);
  assert(planHandover(FileProof::Matches, FileProof::Different) == HandoverAction::AnotherName);
  assert(planHandover(FileProof::Different, FileProof::Matches) == HandoverAction::Retry);
  assert(planHandover(FileProof::Unreadable, FileProof::Missing) == HandoverAction::Retry);
  assert(planHandover(FileProof::Matches, FileProof::Unreadable) == HandoverAction::Retry);

  const std::string source = "/logs/capture.csv", destination = "/uploaded/capture.csv";
  const std::string bytes = "SOURCE-A";
  {
    Model m; m.files[source] = bytes; m.verifiedReceipt = false;
    assert(!m.handover(source, "capture.csv", bytes));
    assert(m.files.at(source) == bytes && !m.intent && !m.done);
  }
  {
    Model m; m.files[source] = bytes; m.failIntent = true;
    assert(!m.handover(source, "capture.csv", bytes));
    assert(m.files.at(source) == bytes && !m.files.count(destination));
  }
  {
    Model m; m.files[source] = bytes; m.failRename = true;
    assert(!m.handover(source, "capture.csv", bytes));
    assert(m.intent && !m.done && m.files.at(source) == bytes);
    m.failRename = false;
    assert(m.handover(source, "capture.csv", bytes) && m.done);
    assert(m.files.at(destination) == bytes && !m.files.count(source));
  }
  {
    Model m; m.files[source] = bytes; m.crashAfterRename = true;
    assert(!m.handover(source, "capture.csv", bytes));
    assert(m.intent && !m.done && !m.files.count(source));
    assert(m.files.at(m.plannedDestination) == bytes);
    m.crashAfterRename = false;
    assert(m.handover(source, "capture.csv", bytes) && m.done);
    assert(m.files.size() == 1); // Recovery is local, with no second copy/import.
  }
  {
    Model m; m.files[source] = bytes; m.failMetadata = true;
    assert(!m.handover(source, "capture.csv", bytes));
    assert(m.intent && !m.done && m.files.at(destination) == bytes);
    m.failMetadata = false;
    assert(m.handover(source, "capture.csv", bytes) && m.done);
  }
  {
    Model m; m.files[destination] = bytes; m.failMetadata = true;
    assert(!m.handover(destination, "capture.csv", bytes));
    assert(m.files.at(destination) == bytes && !m.done);
    m.failMetadata = false;
    assert(m.handover(destination, "capture.csv", bytes) && m.done);
    assert(m.files.size() == 1 && m.files.at(destination) == bytes);
  }
  {
    Model m; m.files[source] = bytes; m.files[destination] = "OTHER-B";
    assert(m.handover(source, "capture.csv", bytes));
    assert(m.files.at(destination) == "OTHER-B");
    assert(m.files.at("/uploaded/pc_" + m.digest + ".csv") == bytes);
  }
  {
    Model m; m.files[source] = bytes; m.files[destination] = bytes; m.failRemove = true;
    assert(!m.handover(source, "capture.csv", bytes));
    assert(m.files.size() == 2 && m.intent && !m.done);
    m.failRemove = false;
    assert(m.handover(source, "capture.csv", bytes) && m.files.size() == 1);
  }
  {
    Model m; m.files[source] = "CHANGED!"; // Same byte count as SOURCE-A.
    assert(!m.handover(source, "capture.csv", bytes));
    assert(m.files.at(source) == "CHANGED!" && !m.done);
  }
  {
    Model m; m.files[source] = bytes; m.unreadable.insert(destination);
    assert(!m.handover(source, "capture.csv", bytes));
    assert(m.files.at(source) == bytes && !m.intent);
  }
  {
    Model m; m.files[source] = bytes; m.files[destination] = "occupied";
    for (unsigned i = 0; i < 32; ++i) {
      char name[96];
      assert(PigletCollection::handoverCollisionName(name, sizeof name, m.digest.c_str(), i));
      m.files["/uploaded/" + std::string(name)] = "occupied";
    }
    assert(!m.handover(source, "capture.csv", bytes));
    assert(m.files.at(source) == bytes && m.files.size() == 34);
    char name[96];
    assert(!PigletCollection::handoverCollisionName(name, sizeof name, m.digest.c_str(), 32));
  }
  {
    Model m; m.files[source] = bytes;
    assert(m.handover(source, "capture.csv", bytes));
    assert(m.canDelete(destination, bytes));
    assert(!m.canDelete(destination, bytes, true));
    m.verifiedReceipt = false; // New pairing/collector is not the old receipt.
    assert(!m.canDelete(destination, bytes));
    m.verifiedReceipt = true; m.files[destination] = "CHANGED!";
    assert(!m.canDelete(destination, bytes));
    m.files[destination] = bytes;
    const bool simulatedDeleteFailure = true;
    if (!simulatedDeleteFailure) m.files.erase(destination);
    assert(m.files.count(destination) && m.canDelete(destination, bytes));
  }
  {
    // Two announced paths share bytes. A's ACK permits coalescing/retention,
    // then power is lost before B is offered. The durable inventory survives.
    Model sd; sd.files[source] = bytes; sd.files[destination] = bytes;
    SourceObligationsModel journal;
    journal.announce({source, destination}); // Persist BEFORE begin(expected=2).
    journal.offered.insert(source); journal.imports = 1;
    assert(sd.handover(source, "capture.csv", bytes));
    journal.verifiedReceipts = {source, destination};
    journal.handed = {source, destination};
    assert(sd.canDelete(destination, bytes));
    sd.files.erase(destination); // Existing retention after valid ACK/handover.
    assert(sd.files.empty() && journal.obligations.size() == 2);
    SourceObligationsModel reboot = journal; // Only durable state is restored.
    reboot.offered.clear();
    const auto restored = reboot.resume({});
    assert(restored.size() == 2 && restored.count(source) && restored.count(destination));
    assert(!reboot.offerRetired(destination, false)); // No raw bytes: no fake ACK.
    assert(reboot.offerRetired(source, true) && reboot.offerRetired(destination, true));
    assert(reboot.offered.size() == 2 && reboot.imports == 1);
    reboot.finish(false, true, true); // Lost HTTP finish response.
    assert(reboot.obligations.size() == 2);
    reboot.finish(true, true, false); // Collector still has unfinished sources.
    assert(reboot.obligations.size() == 2);
    reboot.finish(true, false, true); // A string/truthy field is not bool true.
    assert(reboot.obligations.size() == 2);
    reboot.finish(true, true, true);
    assert(reboot.obligations.empty() && reboot.resume({}).empty());
  }
  {
    SourceObligationsModel normal; normal.uconsole = false;
    normal.announce({source, destination});
    assert(normal.obligations.empty() && normal.resume({source}).size() == 1);
    SourceObligationsModel legacy; legacy.negotiated = false;
    legacy.announce({source, destination});
    assert(legacy.obligations.empty() && legacy.resume({destination}).size() == 1);
    assert(!legacy.offerRetired(source, true));
  }
  return 0;
}
