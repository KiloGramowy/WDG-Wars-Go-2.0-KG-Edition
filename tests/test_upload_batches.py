"""Lossless upload partitioning/receipts/duplicate cleanup; fake API only."""
import base64
import csv
import io
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import Mock,patch
from urllib.error import HTTPError,URLError
from plugins.wardrive_upload import WardriveUpload,_upload_receipt
from watchdogs.piglet_csv import NATIVE_HEADER,PREAMBLE
import test_upload_recovery as recovery
DUPLICATE_CACHE_RESPONSE=recovery.DUPLICATE_CACHE_RESPONSE

class UploadBatchTests(unittest.TestCase):
    setUp=recovery.UploadRecoveryTests.setUp
    new_plugin=recovery.UploadRecoveryTests.new_plugin
    write_mesh=recovery.UploadRecoveryTests.write_mesh
    def network_csv(self,n,*,collection=False):
        for path in self.session.glob("*.csv"):path.unlink()
        path=self.session/"wardriving.csv"
        with path.open("w",encoding="utf8",newline="") as stream:
            stream.write(PREAMBLE+"\n")
            writer=csv.writer(stream);writer.writerow(NATIVE_HEADER)
            for number in range(n):
                writer.writerow([f"02:00:00:00:{number>>8:02X}:{number&255:02X}",f"fixture-{number}","[ESS]",
                                 "2026-11-08 12:00:00",1,-50,12,34,0,1,"WIFI"])
        if collection:(self.session/"collection_source.json").write_text("{}",encoding="utf8")
        return path
    @staticmethod
    def decode(request):return json.loads(base64.b64decode(json.loads(request.data)["data"]))
    @staticmethod
    def reply(data,*,new=0):
        return dict(ok=True,imported=new,updated=0,duplicates=0,cooldown=len(data["networks"])-new,
                    aircraft_imported=0,aircraft_already_seen=len(data["aircraft"]),
                    meshcore_imported=0,meshcore_already_seen=len(data["meshcore_nodes"]))
    def fake_api(self,callback=None):
        calls=[]
        def opened(request,timeout=90):
            data=self.decode(request);calls.append(data)
            response=callback(data,len(calls)) if callback else self.reply(data)
            if isinstance(response,BaseException):raise response
            return io.BytesIO(json.dumps(response).encode())
        return calls,opened
    def run_worker(self,opened,plugin=None):
        plugin=plugin or self.plugin
        with patch("plugins.wardrive_upload._open",side_effect=opened):plugin._upload_worker([self.session])
    def test_planner_limits_signed_bytes_and_records_without_dropping_mixed_groups(self):
        data=dict(networks=[dict(ssid="x"*120,ordinal=n) for n in range(5)],
                  aircraft=[dict(ordinal=n) for n in range(3)],meshcore_nodes=[dict(ordinal=n) for n in range(2)])
        with patch("plugins.wardrive_upload.UPLOAD_BATCH_RECORDS",3),patch("plugins.wardrive_upload.UPLOAD_BATCH_BYTES",700):
            spans=list(self.plugin._plan_upload_batches(data))
            parts=[self.plugin._payload_slice(data,*span) for span in spans]
            self.assertTrue(all(sum(len(v) for v in part.values())<=3 for part in parts))
            self.assertTrue(all(len(self.plugin._sign_payload(part))<=700 for part in parts))
        for key in data:self.assertEqual([r for part in parts for r in part[key]],data[key])
    def test_all_batches_store_real_receipts_and_separate_complete_coverage(self):
        self.network_csv(5);data=self.plugin._session_upload_data(self.session)
        calls,opened=self.fake_api()
        with patch("plugins.wardrive_upload.UPLOAD_BATCH_RECORDS",2):self.run_worker(opened)
        self.assertEqual([len(p["networks"]) for p in calls],[2,2,1])
        self.assertIsNotNone(self.plugin._cached_coverage(data))
        self.assertNotIn(self.plugin._receipt_key(data),self.plugin._upload_receipts)
        self.assertEqual(len(self.plugin._upload_receipts),3)
        self.assertIn(self.session.name,self.plugin._uploaded_sessions)
        self.assertIsNotNone(self.new_plugin()._cached_coverage(data))
    def test_restart_skips_only_confirmed_batches_and_retries_remaining_records(self):
        self.network_csv(5)
        calls,opened=self.fake_api(lambda data,n:self.reply(data) if n==1 else URLError("fixture disconnect"))
        with patch("plugins.wardrive_upload.UPLOAD_BATCH_RECORDS",2):self.run_worker(opened)
        self.assertEqual(len(self.plugin._upload_receipts),1)
        self.assertNotIn(self.session.name,self.plugin._uploaded_sessions)
        reloaded=self.new_plugin();calls,opened=self.fake_api()
        with patch("plugins.wardrive_upload.UPLOAD_BATCH_RECORDS",2):self.run_worker(opened,reloaded)
        self.assertEqual([len(p["networks"]) for p in calls],[2,1])
        self.assertEqual(calls[0]["networks"][0]["ssid"],"fixture-2")
        self.assertIn(self.session.name,reloaded._uploaded_sessions)
    def test_413_halves_until_real_child_ack_and_single_record_failure_stays_pending(self):
        self.network_csv(5)
        data=self.plugin._session_upload_data(self.session)
        def response(data,n):
            if len(data["networks"])>2:return HTTPError("https://example.invalid",413,"too large",{},io.BytesIO(b"{}"))
            return self.reply(data)
        calls,opened=self.fake_api(response);self.run_worker(opened)
        self.assertEqual([len(p["networks"]) for p in calls],[5,2,3,1,2])
        self.assertIsNotNone(self.plugin._cached_coverage(data))
        self.assertTrue(self.plugin._upload_receipts)
    def test_bare_cache_halves_require_full_actual_child_ack_then_duplicate_csv_can_delete(self):
        path=self.network_csv(4,collection=True);original=path.read_bytes()
        raw=self.base/"raw-original.csv";raw.write_bytes(original)
        data=self.plugin._session_upload_data(self.session)
        calls,opened=self.fake_api(lambda data,n:DUPLICATE_CACHE_RESPONSE if n==1 else self.reply(data))
        self.run_worker(opened)
        self.assertEqual([len(p["networks"]) for p in calls],[4,2,2])
        self.assertIsNotNone(self.plugin._cached_coverage(data))
        self.assertFalse(path.exists());self.assertEqual(raw.read_bytes(),original)
        self.assertTrue((self.session/"collection_source.json").is_file())
    def test_singleton_bare_cache_and_incomplete_accounting_never_delete_or_confirm(self):
        path=self.network_csv(1,collection=True)
        calls,opened=self.fake_api(lambda data,n:DUPLICATE_CACHE_RESPONSE);self.run_worker(opened)
        self.assertEqual(len(calls),1);self.assertTrue(path.exists())
        self.assertNotIn(self.session.name,self.plugin._uploaded_sessions)
        self.network_csv(4,collection=True)
        calls,opened=self.fake_api(lambda data,n:dict(ok=True,duplicates=3))
        self.run_worker(opened)
        self.assertEqual(len(calls),2);self.assertTrue(path.exists())
        self.assertEqual(self.plugin._upload_coverage,{})
    def test_prior_real_import_or_update_protects_export_even_after_known_child_receipts(self):
        path=self.network_csv(4,collection=True);data=self.plugin._session_upload_data(self.session)
        calls,opened=self.fake_api(lambda data,n:dict(ok=True,imported=1,duplicates=2))
        self.run_worker(opened)
        self.assertIn(self.plugin._receipt_key(data),self.plugin._upload_keep_exports)
        reloaded=self.new_plugin()
        calls,opened=self.fake_api(lambda data,n:DUPLICATE_CACHE_RESPONSE if n==1 else self.reply(data))
        self.run_worker(opened,reloaded)
        self.assertTrue(path.exists());self.assertIsNotNone(reloaded._cached_coverage(data))
    def test_changed_source_or_account_never_marks_current_file_uploaded(self):
        path=self.network_csv(5);data=self.plugin._session_upload_data(self.session)
        def response(part,n):
            if n==3:path.write_bytes(path.read_bytes()+b"\n")
            return self.reply(part)
        calls,opened=self.fake_api(response)
        with patch("plugins.wardrive_upload.UPLOAD_BATCH_RECORDS",2):self.run_worker(opened)
        self.assertNotIn(self.session.name,self.plugin._uploaded_sessions)
        other=self.new_plugin(key="different-fixture-account")
        self.assertIsNone(other._cached_coverage(data))
    def test_coverage_save_failure_and_missing_receipt_never_confirm_full_file(self):
        self.network_csv(3);data=self.plugin._session_upload_data(self.session)
        calls,opened=self.fake_api()
        original=self.plugin._save_state
        def fail_coverage():return False if self.plugin._upload_coverage else original()
        with patch("plugins.wardrive_upload.UPLOAD_BATCH_RECORDS",2),patch.object(self.plugin,"_save_state",side_effect=fail_coverage):self.run_worker(opened)
        self.assertEqual(len(self.plugin._upload_receipts),2);self.assertIsNone(self.plugin._cached_coverage(data))
        self.assertNotIn(self.session.name,self.plugin._uploaded_sessions)
    def test_duplicate_cleanup_rechecks_identity_after_preserve(self):
        path=self.network_csv(2,collection=True);data=self.plugin._session_upload_data(self.session)
        self.assertTrue(self.plugin._remember_receipt(data,self.reply(data)))
        snapshots=self.plugin._retention_capture(self.session,data)
        loot=self.plugin.app.loot
        def preserve(*args):
            self.plugin.app.loot=Mock(_base=str(self.base));return True
        loot.prepare_upload_log_discard=preserve
        self.assertEqual(self.plugin._remove_duplicate_exports(snapshots,data,account=(self.plugin._api_url,self.plugin._api_key)),0)
        self.assertTrue(path.exists())
    def test_duplicate_cleanup_rechecks_receipt_and_active_session_after_preserve(self):
        for change in ("receipt","active","account"):
            with self.subTest(change=change):
                path=self.network_csv(2,collection=True);data=self.plugin._session_upload_data(self.session)
                self.assertTrue(self.plugin._remember_receipt(data,self.reply(data)))
                snapshots=self.plugin._retention_capture(self.session,data)
                old_key=self.plugin._api_key
                def preserve(*args):
                    if change=="receipt":self.plugin._upload_receipts.clear()
                    elif change=="active":self.plugin.app.loot.session_dir=str(self.session)
                    else:self.plugin._api_key="changed-account"
                    return True
                self.plugin.app.loot.prepare_upload_log_discard=preserve
                self.assertEqual(self.plugin._remove_duplicate_exports(snapshots,data,account=(self.plugin._api_url,old_key)),0)
                self.assertTrue(path.exists())
                self.plugin._api_key=old_key;self.plugin.app.loot.session_dir=str(self.base/"active")
    def test_stop_aware_wait_returns_without_network_or_delay(self):
        self.plugin._sync_stop.set()
        self.assertFalse(WardriveUpload._wait_upload(self.plugin,6.5,lambda:True))
        with patch("plugins.wardrive_upload._open") as opened:
            self.assertEqual(self.plugin._post_upload_batch(dict(networks=[],aircraft=[],meshcore_nodes=[]),
                (self.plugin._api_url,self.plugin._api_key),lambda:False),(None,None))
        opened.assert_not_called()

    def test_duplicate_cleanup_rechecks_loot_identity_when_entering_writer_lock(self):
        path=self.network_csv(2,collection=True);data=self.plugin._session_upload_data(self.session)
        self.assertTrue(self.plugin._remember_receipt(data,self.reply(data)))
        snapshots=self.plugin._retention_capture(self.session,data)
        old=self.plugin.app.loot
        old.prepare_upload_log_discard=Mock(return_value=True)
        plugin=self.plugin
        class SwitchLock:
            def __enter__(self):plugin.app.loot=Mock(_base=str(self.base))
            def __exit__(self,*args):return False
        switch=SwitchLock();switch.base=self.base
        with patch.object(self.plugin,"_upload_queue_lock",return_value=switch):
            self.assertEqual(self.plugin._remove_duplicate_exports(snapshots,data,
                account=(self.plugin._api_url,self.plugin._api_key)),0)
        old.prepare_upload_log_discard.assert_not_called();self.assertTrue(path.exists())

    def test_duplicate_cleanup_can_explicitly_revisit_real_ack_closed_collection_session(self):
        path=self.network_csv(2,collection=True);data=self.plugin._session_upload_data(self.session)
        self.assertTrue(self.plugin._remember_receipt(data,self.reply(data)))
        self.assertTrue(self.plugin._mark_uploaded(self.session))
        self.assertEqual(self.plugin._session_upload_data(self.session)["networks"],[])
        self.assertEqual(self.plugin.cleanup_collection_duplicate_export(self.session),1)
        self.assertFalse(path.exists())

    def test_timeout_and_wrapped_timeout_halve_but_other_connection_errors_do_not(self):
        for wrapped in (False,True):
            with self.subTest(wrapped=wrapped):
                self.plugin=self.new_plugin()
                self.plugin._uploaded_sessions.clear();self.plugin._upload_receipts.clear();self.plugin._upload_coverage.clear()
                self.network_csv(5)
                data=self.plugin._session_upload_data(self.session)
                def response(part,n):
                    if len(part["networks"])>2:
                        error=TimeoutError("fixture request timeout")
                        return URLError(error) if wrapped else error
                    return self.reply(part)
                calls,opened=self.fake_api(response);self.run_worker(opened)
                self.assertEqual([len(p["networks"]) for p in calls],[5,2,3,1,2])
                self.assertIsNotNone(self.plugin._cached_coverage(data))
        self.plugin=self.new_plugin();self.plugin._uploaded_sessions.clear();self.plugin._upload_receipts.clear();self.plugin._upload_coverage.clear()
        self.network_csv(5)
        calls,opened=self.fake_api(lambda part,n:URLError("fixture DNS failure"));self.run_worker(opened)
        self.assertEqual(len(calls),1)
        self.assertNotIn(self.session.name,self.plugin._uploaded_sessions)

    def test_timed_out_single_record_never_fabricates_a_receipt(self):
        path=self.network_csv(1,collection=True)
        calls,opened=self.fake_api(lambda part,n:TimeoutError("fixture timeout"));self.run_worker(opened)
        self.assertEqual(len(calls),1)
        self.assertEqual(self.plugin._upload_receipts,{})
        self.assertTrue(path.exists());self.assertNotIn(self.session.name,self.plugin._uploaded_sessions)

    def test_default_target_is_ten_thousand_records_with_six_mib_wire_bound(self):
        import plugins.wardrive_upload as upload
        self.assertEqual(upload.UPLOAD_BATCH_RECORDS,10000)
        self.assertEqual(upload.UPLOAD_BATCH_BYTES,6*1024*1024)
        data=dict(networks=[dict(ordinal=n) for n in range(10001)],aircraft=[],meshcore_nodes=[])
        self.assertEqual(list(self.plugin._plan_upload_batches(data)),[(0,10000),(10000,10001)])

    def test_actual_post_slots_are_at_least_six_and_half_seconds_apart(self):
        now=[100.0];delays=[]
        def waited(delay,current):
            delays.append(delay);now[0]+=delay;return current()
        with patch("plugins.wardrive_upload.time.monotonic",side_effect=lambda:now[0]), \
                patch.object(self.plugin,"_wait_upload",side_effect=waited):
            self.assertTrue(self.plugin._wait_upload_slot(lambda:True))
            self.assertTrue(self.plugin._wait_upload_slot(lambda:True))
            self.assertTrue(self.plugin._wait_upload_slot(lambda:True))
        self.assertEqual(delays,[0,6.5,6.5])

    def test_captured_gap_gets_one_identical_retry_then_actual_child_receipts(self):
        self.network_csv(4)
        data=self.plugin._session_upload_data(self.session)
        def response(part,number):
            if number==1:return dict(ok=True,imported=0,updated=0,duplicates=1,cooldown=2,captured=1)
            if number==2:return DUPLICATE_CACHE_RESPONSE
            return self.reply(part)
        calls,opened=self.fake_api(response);self.run_worker(opened)
        self.assertEqual([len(part["networks"]) for part in calls],[4,4,2,2])
        self.assertEqual(calls[0],calls[1])
        self.assertIsNotNone(self.plugin._cached_coverage(data))
        self.assertNotIn(self.plugin._receipt_key(data),self.plugin._upload_receipts)

    def test_repeated_incomplete_is_bounded_and_rejected_job_invalid_responses_are_not_retried(self):
        self.network_csv(4)
        data=self.plugin._session_upload_data(self.session)
        cases=[(dict(ok=True,duplicates=3,captured=1),2),
               (dict(ok=True,duplicates=3,rejected=1),1),
               (dict(ok=True,duplicates=3,job_id=123),1),
               (dict(ok=True,duplicates="3"),1),
               (dict(ok=True,duplicates=3,error="fixture error"),1)]
        for response,expected in cases:
            with self.subTest(response=response):
                self.plugin=self.new_plugin();self.plugin._uploaded_sessions.clear()
                self.plugin._upload_receipts.clear();self.plugin._upload_coverage.clear()
                calls,opened=self.fake_api(lambda part,n:response)
                self.run_worker(opened)
                self.assertEqual(len(calls),expected)
                self.assertIsNone(self.plugin._cached_coverage(data))
                self.assertNotIn(self.session.name,self.plugin._uploaded_sessions)

if __name__=="__main__":unittest.main()
