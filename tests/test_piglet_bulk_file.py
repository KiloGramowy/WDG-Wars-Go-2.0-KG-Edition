"""Whole-CSV settlement, recovery and cancellation; no hardware or benchmarks."""
import csv
import json
import os
import sqlite3
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from watchdogs.loot_manager import LootManager
from watchdogs.piglet_collection import CollectionError,PigletCollectionStore
from watchdogs.piglet_csv import NATIVE_HEADER
from tests.test_piglet_collection import BLE,contents,digest,row

class BulkFileTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        with patch.object(LootManager,"_periodic_backup_loop",lambda self:None):
            self.loot=LootManager(self.directory.name,SimpleNamespace(fix=None))
        self.addCleanup(self.close_loot)
        self.store=self.new_store()
        self.addCleanup(lambda:self.store.close())
        self.wifi,self.ble,self.xp=set(),set(),100
        self.loot.save_xp(self.xp)
    def close_loot(self):
        with patch.object(os,"sync",lambda:None,create=True):self.loot.close()
    def new_store(self):
        # Drive real worker stages explicitly, avoiding scheduler timing.
        with patch.object(PigletCollectionStore,"_worker_main",lambda self:None):return PigletCollectionStore(self.loot)
    def transfer(self,rows,extra=""):
        data=contents(rows,extra)
        session=self.store.begin("piglet-123456789abc",1)["session_id"]
        sha=digest(data)
        self.store.offer(session,"source.csv",len(data),sha)
        for offset in range(0,len(data),16384):
            block=data[offset:offset+16384]
            self.store.chunk(session,sha,offset,block,digest(block))
        self.store.commit(session,sha)
        return session,sha,data
    def file(self,sha):return self.store._sql.execute("SELECT * FROM files WHERE sha=?",(sha,)).fetchone()
    def ready(self,sha):
        self.store._prepare(self.file(sha))
        self.store._prepare_bulk(self.file(sha))
    def settle(self):
        result=self.store.process_pending(self.xp,self.wifi,self.ble)
        if result["committed"]:self.xp=result["xp_total"]
        return result
    def exported(self,sha):
        target=Path(self.loot._base)/self.file(sha)["session_name"]
        with (target/"wardriving.csv").open(encoding="utf8",newline="") as source:
            self.assertIn("source=PigletCollection1",source.readline())
            reader=csv.reader(source)
            self.assertEqual(tuple(next(reader)),NATIVE_HEADER)
            return list(reader)
    def test_validation_and_export_do_not_pay_or_enqueue_observations(self):
        session,sha,data=self.transfer([row(),row(BLE,"BLE")])
        self.store._prepare(self.file(sha))
        self.assertFalse(self.settle()["committed"])
        self.store._prepare_bulk(self.file(sha))
        self.assertEqual(self.loot.load_xp(),100)
        self.assertEqual(self.store.pending_events(),[])
        self.assertNotIn("receipt",self.store.status(session,sha))
        self.assertEqual(self.store.status(session,sha)["import_percent"],99)
        self.assertFalse((Path(self.loot._base)/self.file(sha)["session_name"]).exists())
        self.assertTrue(self.settle()["committed"])
        self.assertEqual(self.xp,125)
        self.assertEqual(self.store.pending_events(),[])
        self.assertEqual(self.store.status(session,sha)["state"],"committed")
        self.assertEqual(len(self.exported(sha)),2)
        self.assertEqual(self.store._path(sha).read_bytes(),data)
    def test_exact_duplicates_do_not_score_but_every_source_row_exports(self):
        source=[row(),row(),row(rssi="-60"),row(BLE,"BLE"),row(BLE,"BLE")]
        session,sha,_=self.transfer(source)
        self.ready(sha);self.settle()
        state=self.store.status(session,sha)
        self.assertEqual((state["wifi"],state["ble"],state["duplicates"],state["base_xp"]),(2,1,2,26))
        self.assertEqual((state["new_wifi"],state["new_ble"],self.xp),(1,1,126))
        exported=self.exported(sha)
        self.assertEqual(len(exported),len(source))
        for original,converted in zip(source,exported):
            self.assertEqual(converted[3],original[3]);self.assertEqual(converted[6:10],original[7:11])
        self.assertFalse(self.settle()["committed"])
    def test_partial_legacy_preserves_paid_rows_and_outbox_and_exports_all(self):
        _,sha,_=self.transfer([row(),row(rssi="-60"),row(BLE,"BLE")])
        self.store._prepare(self.file(sha))
        first=self.store._sql.execute("SELECT ordinal,record FROM prepared WHERE sha=? ORDER BY ordinal LIMIT 1",(sha,)).fetchone()
        batch="a"*32
        paid=self.loot.commit_collection_batch(batch,sha,[dict(ordinal=first["ordinal"],record=json.loads(first["record"]))],self.xp,self.wifi,self.ble)
        self.xp=paid["xp_total"]
        self.store._recover_batch(batch,self.loot.collection_pending_batches()[batch])
        events=self.store.pending_events()
        self.store._prepare_bulk(self.file(sha))
        self.assertFalse(self.settle()["committed"])
        self.assertEqual(self.store.pending_events(),events)
        self.assertEqual(self.loot.load_xp(),115)
        self.store.acknowledge_events([e["event_id"] for e in events])
        self.settle()
        self.assertEqual(self.xp,126);self.assertEqual(len(self.exported(sha)),3)
        self.assertEqual(self.file(sha)["wifi"],2)
        self.assertEqual(self.store._sql.execute("SELECT COUNT(*) FROM prepared WHERE sha=? AND done=1",(sha,)).fetchone()[0],3)
    def test_extension_is_same_candidate_and_returned_total_is_final(self):
        _,sha,_=self.transfer([row(),row(BLE,"BLE")]);self.ready(sha)
        calls=[]
        def extend(candidate,context):
            self.assertTrue(candidate["collection_import_batches"][context["file_commit_id"]]["whole_file"])
            self.assertEqual(len(context["groups"]),2);self.assertEqual(self.loot.load_xp(),100)
            candidate["xp"]+=7;candidate["test_extension_receipt"]=context["file_sha256"];calls.append(context)
        with patch.object(self.loot,"_extend_collection_file_commit",extend):self.settle()
        self.assertEqual((len(calls),self.xp,self.loot.load_xp()),(1,132,132))
        self.assertEqual(self.loot._db["test_extension_receipt"],sha)
        self.settle();self.assertEqual(self.xp,132)
    def test_save_failure_keeps_authoritative_state_and_known_ids(self):
        _,sha,_=self.transfer([row()]);self.ready(sha)
        with patch.object(self.loot,"_save_db",return_value=False):
            with self.assertRaises(OSError):self.settle()
        self.assertEqual((self.loot.load_xp(),self.wifi,self.ble),(100,set(),set()))
        self.assertEqual(self.store._sql.execute("SELECT COUNT(*) FROM prepared WHERE done=1").fetchone()[0],0)
        self.settle();self.assertEqual(self.xp,115)
    def test_crash_after_canonical_save_recovers_without_reaward(self):
        session,sha,_=self.transfer([row(),row(BLE,"BLE")]);self.ready(sha)
        with patch.object(self.store,"_recover_file_batch",side_effect=OSError("injected")):result=self.settle()
        self.assertTrue(result["committed"]);self.assertEqual(self.xp,125)
        self.assertEqual(len(self.loot.collection_pending_batches()),1)
        self.store.close();self.store=self.new_store();self.settle()
        self.assertEqual(self.xp,125);self.assertEqual(self.store.status(session,sha)["state"],"committed")
        self.assertEqual(len(self.exported(sha)),2);self.assertEqual(self.loot.collection_pending_batches(),{})
    def test_cancel_reprocesses_only_unpaid_work_and_keeps_raw(self):
        session,sha,data=self.transfer([row(),row(BLE,"BLE")]);self.ready(sha)
        self.store.cancel_processing();self.assertTrue(self.store.cancelled)
        self.assertFalse(self.settle()["committed"])
        with self.assertRaises(CollectionError):self.store.commit(session,sha)
        self.assertTrue(self.store.close());self.assertEqual(self.loot.load_xp(),100)
        self.store=self.new_store();self.assertEqual(self.store._path(sha).read_bytes(),data)
        self.assertEqual(self.file(sha)["bulk_ready"],0)
        self.assertEqual(self.store._sql.execute("SELECT COUNT(*) FROM prepared WHERE done=3").fetchone()[0],0)
        self.store._prepare_bulk(self.file(sha));self.settle();self.assertEqual(self.xp,125)
    def test_cancel_preserves_completed_export_and_paid_rewards(self):
        session,sha,data=self.transfer([row()]);self.ready(sha);self.settle();exported=self.exported(sha)
        self.store.cancel_processing();self.assertTrue(self.store.close());self.store=self.new_store()
        self.assertEqual(self.store.status(session,sha)["state"],"committed")
        self.assertEqual(self.loot.load_xp(),115);self.assertEqual(self.store._path(sha).read_bytes(),data)
        self.assertEqual(self.exported(sha),exported)
    def test_next_file_exact_records_are_not_paid_again(self):
        _,sha,_=self.transfer([row()]);self.ready(sha);self.settle()
        _,other,_=self.transfer([row(),row(BLE,"BLE")],extra=",differentFile=1")
        self.ready(other);self.settle();self.assertEqual(self.xp,125)
        self.assertEqual((self.file(other)["wifi"],self.file(other)["ble"],self.file(other)["duplicates"]),(0,1,1))
        self.assertEqual(len(self.exported(other)),2)

    def test_receipt_failure_returns_already_committed_xp_and_retry_does_not_rescore(self):
        session,sha,_=self.transfer([row()]);self.ready(sha)
        with patch.object(self.loot,"complete_collection_file",return_value=False):
            result=self.settle()
        self.assertTrue(result["committed"])
        self.assertEqual(self.xp,115)
        self.assertIn("error",result)
        self.assertNotIn("receipt",self.store.status(session,sha))
        self.assertFalse(self.settle()["committed"])
        self.assertEqual(self.store.status(session,sha)["state"],"committed")
        self.assertEqual(self.xp,115)

    def test_cancel_during_export_stops_before_settlement_and_keeps_old_paid_rows(self):
        _,sha,data=self.transfer([row(),row(rssi="-60"),row(BLE,"BLE")])
        self.store._prepare(self.file(sha))
        from watchdogs import piglet_collection
        original=piglet_collection.piglet_records
        def interrupted(path):
            for index,record in enumerate(original(path)):
                yield record
                if index==0:self.store.cancel_processing()
        with patch.object(piglet_collection,"piglet_records",interrupted):
            with self.assertRaises(InterruptedError):self.store._prepare_bulk(self.file(sha))
        self.assertEqual(self.loot.load_xp(),100)
        self.assertFalse(self.settle()["committed"])
        self.store.close();self.store=self.new_store()
        self.assertEqual(self.store._path(sha).read_bytes(),data)
        self.store._prepare_bulk(self.file(sha));self.settle()
        self.assertEqual(self.xp,126)

    def test_cancelled_legacy_partial_retry_preserves_old_payment(self):
        _,sha,_=self.transfer([row(),row(rssi="-60"),row(BLE,"BLE")])
        self.store._prepare(self.file(sha))
        first=self.store._sql.execute("SELECT ordinal,record FROM prepared WHERE sha=? ORDER BY ordinal LIMIT 1",(sha,)).fetchone()
        batch="a"*32
        paid=self.loot.commit_collection_batch(batch,sha,[dict(ordinal=first["ordinal"],record=json.loads(first["record"]))],self.xp,self.wifi,self.ble)
        self.xp=paid["xp_total"]
        self.store._recover_batch(batch,self.loot.collection_pending_batches()[batch])
        self.store.acknowledge_events([e["event_id"] for e in self.store.pending_events()])
        self.store._prepare_bulk(self.file(sha))
        self.store.cancel_processing();self.store.close();self.store=self.new_store()
        self.assertEqual(self.loot.load_xp(),115)
        self.assertEqual(self.store._sql.execute("SELECT COUNT(*) FROM prepared WHERE done=1").fetchone()[0],1)
        self.store._prepare_bulk(self.file(sha));self.settle()
        self.assertEqual(self.xp,126)
        self.assertEqual(len(self.exported(sha)),3)

    def test_publish_failure_after_rename_recovers_paid_csv_without_duplicate_reward(self):
        session,sha,_=self.transfer([row()]);self.ready(sha)
        from watchdogs import piglet_collection
        original=piglet_collection._fsync_directory
        def fail_after_rename(path):
            if Path(path)==Path(self.loot._base):raise OSError("injected parent fsync failure")
            return original(path)
        with patch.object(piglet_collection,"_fsync_directory",fail_after_rename):result=self.settle()
        self.assertTrue(result["committed"]);self.assertEqual(self.xp,115)
        self.assertIn("error",result)
        self.store.close();self.store=self.new_store();self.settle()
        self.assertEqual(self.xp,115)
        self.assertEqual(self.store.status(session,sha)["state"],"committed")
        self.assertEqual(len(self.exported(sha)),1)

    def test_post_commit_sql_read_failure_returns_durable_xp_to_caller(self):
        _,sha,_=self.transfer([row()]);self.ready(sha)
        connection=self.store._sql
        loot=self.loot
        class FaultConnection:
            failed=False
            def execute(self,query,*args):
                if (not self.failed and query=="SELECT * FROM files WHERE sha=?"
                        and loot.load_xp()==115 and not loot.collection_pending_batches()):
                    self.failed=True
                    raise sqlite3.OperationalError("injected post-commit read failure")
                return connection.execute(query,*args)
            def __getattr__(self,name):return getattr(connection,name)
            def __enter__(self):return connection.__enter__()
            def __exit__(self,*args):return connection.__exit__(*args)
        self.store._sql=FaultConnection()
        result=self.settle()
        self.assertTrue(result["committed"])
        self.assertEqual((self.xp,self.loot.load_xp()),(115,115))
        self.assertIn("error",result)
        self.assertFalse(self.settle()["committed"])
        self.assertEqual(self.xp,115)

if __name__=="__main__":unittest.main()
