#pragma once
#include <Arduino.h>
#include <ArduinoJson.h>

struct CollectionFileList {
  struct Entry { char path[288]; };
  Entry* files = nullptr;
  uint32_t count = 0;
  ~CollectionFileList();
  CollectionFileList() = default;
  CollectionFileList(const CollectionFileList&) = delete;
  CollectionFileList& operator=(const CollectionFileList&) = delete;
};

bool collectionArchivePrepare();
String collectionArchiveCollector();
bool collectionArchiveSetCollector(const String& id);
bool collectionArchiveList(CollectionFileList& list, bool pendingOnly = true,
                            bool includeSourceObligations = false);
bool collectionArchiveAnnounceSources(const CollectionFileList& list);
bool collectionArchiveFinishSources(const CollectionFileList& list);
bool collectionArchiveObserve(const String& path, const String& sha, uint32_t size);
bool collectionArchivePlanMove(const String& source, const String& destination,
                                const String& sha, uint32_t size);
bool collectionArchiveMoved(const String& source, const String& destination,
                             const String& sha, uint32_t size);
bool collectionArchiveCanDelete(const String& path);
bool collectionArchiveRetentionEligible(const String& path);
bool collectionArchiveReceived(const String& path);
// A confirmed path may disappear during earlier items' existing retention.
// This returns only its own durable acknowledged/handover snapshot, not bytes.
bool collectionArchiveDeliveredSnapshot(const String& path, String& sha, uint32_t& size);
bool collectionArchiveLegacyMove(const String& source);
void collectionArchiveRecover();
void collectionArchiveRefresh(bool verifyBodies = false);
void collectionArchiveStatus(JsonObject status);
