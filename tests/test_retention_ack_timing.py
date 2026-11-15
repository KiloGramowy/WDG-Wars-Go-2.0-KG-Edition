"""Authentic ACK clocks and full normal representations; synthetic files only."""
import copy
import json
import os
import threading
from unittest.mock import patch
import unittest
from test_uploaded_log_retention_cleanup import UploadedLogRetentionCleanupTests,DAY,EXPORTS


class AckTimingTests(UploadedLogRetentionCleanupTests):
    def complete(self,data):
        return dict(ok=True,imported=len(data['networks']),aircraft_imported=len(data['aircraft']),
                    meshcore_imported=len(data['meshcore_nodes']))

    def test_first_full_ack_time_survives_disabled_policy_restart_and_late_seeding(self):
        data=self.data();first=self.now
        self.assertTrue(self.plugin._remember_receipt(data,self.complete(data)))
        raw=copy.deepcopy(self.plugin._upload_receipts)
        self.assertFalse(self.proof_rows())
        self.now+=6*DAY
        fresh=self.new_plugin();self.assertEqual(fresh._upload_receipts,raw)
        self.assertEqual(fresh._upload_ack_times[fresh._receipt_key(data)],first)
        self.assertTrue(fresh._set_retention_days(5));fresh._retention_seed_existing()
        self.assertTrue(all(p['confirmed_at']==first for p in self.proof_rows(fresh)))
        self.assertEqual(fresh.cleanup_uploaded_logs()['deleted'],4)

    def test_repeated_authentic_ack_and_reseed_do_not_restart_five_day_clock(self):
        data=self.data();first=self.now
        self.assertTrue(self.plugin._remember_receipt(data,self.complete(data)))
        self.now+=3*DAY
        self.assertTrue(self.plugin._remember_receipt(data,self.complete(data)))
        self.assertEqual(self.plugin._upload_ack_times[self.plugin._receipt_key(data)],first)
        self.assertTrue(self.plugin._set_retention_days(5));self.plugin._retention_seed_existing()
        self.now=first+5*DAY-.001;self.assertEqual(self.plugin.cleanup_uploaded_logs()['deleted'],0)
        self.now+=.001;self.assertEqual(self.plugin.cleanup_uploaded_logs()['deleted'],4)

    def test_old_session_and_mtime_do_not_age_a_recent_real_ack(self):
        for name in EXPORTS:os.utime(self.session/name,(1,1))
        self.uploaded_and_aged(days=5)
        self.now-=.001
        self.assertEqual(self.plugin.cleanup_uploaded_logs()['deleted'],0)
        self.now+=.001
        self.assertEqual(self.plugin.cleanup_uploaded_logs()['deleted'],4)

    def test_complete_batch_clock_starts_at_last_required_part_not_first_part_or_reseed(self):
        data=self.data();total=sum(self.plugin._payload_counts(data).values());batches=[]
        first=self.now
        for start,end in ((0,2),(2,total)):
            part=self.plugin._payload_slice(data,start,end)
            self.assertTrue(self.plugin._remember_receipt(part,self.complete(part)))
            batches.append(dict(start=start,end=end,key=self.plugin._receipt_key(part),counts=self.plugin._payload_counts(part)))
            self.now+=2*DAY
        last=first+2*DAY
        self.assertTrue(self.plugin._remember_coverage(data,batches))
        self.assertEqual(self.plugin._upload_ack_times[self.plugin._receipt_key(data)],last)
        self.assertTrue(self.plugin._set_retention_days(5))
        self.plugin._retention_confirm_files(self.plugin._retention_capture(self.session,data),data)
        self.now=last+5*DAY-.001
        self.assertEqual(self.plugin.cleanup_uploaded_logs()['deleted'],0)
        self.now+=.001;self.assertEqual(self.plugin.cleanup_uploaded_logs()['deleted'],4)

    def test_historical_batch_parts_without_clocks_start_at_complete_set_confirmation(self):
        data=self.data();total=sum(self.plugin._payload_counts(data).values());batches=[]
        for start,end in ((0,2),(2,total)):
            part=self.plugin._payload_slice(data,start,end)
            self.assertTrue(self.plugin._remember_receipt(part,self.complete(part)))
            batches.append(dict(start=start,end=end,key=self.plugin._receipt_key(part),counts=self.plugin._payload_counts(part)))
        self.plugin._upload_ack_times.clear();self.now+=10*DAY
        self.assertTrue(self.plugin._remember_coverage(data,batches))
        self.assertEqual(self.plugin._upload_ack_times[self.plugin._receipt_key(data)],self.now)

    def test_failed_receipt_or_coverage_save_rolls_back_their_ack_clock_together(self):
        data=self.data();before=self.state.read_bytes() if self.state.exists() else None
        with patch.object(self.plugin,'_save_state',return_value=False):
            self.assertFalse(self.plugin._remember_receipt(data,self.complete(data)))
        self.assertEqual(self.plugin._upload_receipts,{})
        self.assertEqual(self.plugin._upload_ack_times,{})
        self.assertEqual(self.state.read_bytes() if self.state.exists() else None,before)
        total=sum(self.plugin._payload_counts(data).values())
        part=self.plugin._payload_slice(data,0,total)
        self.assertTrue(self.plugin._remember_receipt(part,self.complete(part)))
        key=self.plugin._receipt_key(part)
        clocks=copy.deepcopy(self.plugin._upload_ack_times)
        with patch.object(self.plugin,'_save_state',return_value=False):
            self.assertFalse(self.plugin._remember_coverage(data,[dict(start=0,end=total,key=key,counts=self.plugin._payload_counts(part))]))
        self.assertEqual(self.plugin._upload_coverage,{})
        self.assertEqual(self.plugin._upload_ack_times,clocks)

    def test_receipt_and_coverage_mutations_hold_the_shared_state_writer_lock(self):
        data=self.data();observations=[]
        def save():
            def observe():
                lock=self.plugin._upload_queue_lock();acquired=lock.acquire(False)
                observations.append(acquired)
                if acquired:lock.release()
            thread=threading.Thread(target=observe);thread.start();thread.join(1)
            self.assertFalse(thread.is_alive())
            return True
        with patch.object(self.plugin,'_save_state',side_effect=save):
            self.assertTrue(self.plugin._remember_receipt(data,self.complete(data)))
            total=sum(self.plugin._payload_counts(data).values())
            self.assertTrue(self.plugin._remember_coverage(data,[dict(start=0,end=total,
                key=self.plugin._receipt_key(data),counts=self.plugin._payload_counts(data))]))
        self.assertEqual(observations,[False,False])

    def test_ack_metadata_roundtrip_preserves_unknown_state_and_raw_server_dictionary(self):
        data=self.data();response=self.complete(data);response['server_extra']='synthetic'
        self.assertTrue(self.plugin._remember_receipt(data,response))
        state=json.loads(self.state.read_text());state['unrelated_extension']={'kept':[1,2,3]}
        self.state.write_text(json.dumps(state))
        fresh=self.new_plugin();self.assertTrue(fresh._save_state())
        after=json.loads(self.state.read_text())
        self.assertEqual(after['unrelated_extension'],state['unrelated_extension'])
        self.assertEqual(after['receipts'],state['receipts'])
        self.assertEqual(after['upload_ack_times_v1'],state['upload_ack_times_v1'])

    def test_malformed_timestamp_map_is_preserved_without_blocking_authentic_upload_receipts(self):
        self.state.write_text(json.dumps({'upload_ack_times_v1':['unrecognized-old-metadata'],
                                         'unrelated_extension':{'kept':True}}))
        fresh=self.new_plugin();data=self.data(fresh)
        self.assertTrue(fresh._remember_receipt(data,self.complete(data)))
        saved=json.loads(self.state.read_text())
        self.assertEqual(saved['upload_ack_times_v1'],['unrecognized-old-metadata'])
        self.assertEqual(saved['unrelated_extension'],{'kept':True})
        self.assertIn(fresh._receipt_key(data),saved['receipts'])
        self.assertTrue(fresh._set_retention_days(5))
        fresh._retention_confirm_files(fresh._retention_capture(self.session,data),data)
        self.assertEqual(self.proof_rows(fresh),[])

    def test_invalid_timestamp_map_keys_or_values_cannot_block_unrelated_receipt_writes(self):
        for metadata in ({'invalid-key':1_700_000_000},{'a'*64:float('nan')},{'a'*64:True}):
            with self.subTest(metadata_type=str(type(next(iter(metadata.values()))))):
                self.state.write_text(json.dumps({'upload_ack_times_v1':metadata,'unrelated_extension':'kept'}))
                fresh=self.new_plugin();data=self.data(fresh)
                self.assertFalse(fresh._upload_ack_times_valid)
                self.assertTrue(fresh._remember_receipt(data,self.complete(data)))
                saved=json.loads(self.state.read_text())
                self.assertEqual(saved['unrelated_extension'],'kept')
                self.assertIn(fresh._receipt_key(data),saved['receipts'])
                if isinstance(next(iter(metadata.values())),float):
                    self.assertIsInstance(saved['upload_ack_times_v1'],str)
                    self.assertIn('NaN',saved['upload_ack_times_v1'])
                else:self.assertEqual(saved['upload_ack_times_v1'],metadata)

    def test_missing_expired_export_prunes_only_its_proof_and_preserves_progress(self):
        self.uploaded_and_aged();(self.session/'wardriving.csv').unlink()
        progress=self.progress();receipts=copy.deepcopy(self.plugin._upload_receipts)
        result=self.plugin.cleanup_uploaded_logs()
        self.assertEqual(result['pruned'],1);self.assertEqual(result['deleted'],3)
        self.assertEqual(self.proof_rows(),[])
        self.assertEqual(self.progress(),progress)
        self.assertEqual(self.plugin._upload_receipts,receipts)

    def test_pure_no_gps_file_never_gets_a_timer_proof_from_other_group_ack(self):
        path=self.session/'wardriving.csv'
        lines=path.read_text().splitlines(keepends=True)
        from watchdogs.loot_manager import LootManager
        import csv,io
        output=lines[:2]
        for line in lines[2:]:
            row=LootManager._wardrive_fields(line);row[6:8]=['0','0']
            buf=io.StringIO();csv.writer(buf).writerow(row);output.append(buf.getvalue())
        path.write_text(''.join(output))
        data=self.data();self.assertEqual(data['networks'],[])
        self.assertTrue(self.plugin._remember_receipt(data,self.complete(data)))
        self.assertTrue(self.plugin._set_retention_days(5));self.plugin._retention_seed_existing()
        self.assertNotIn('wardriving.csv',{p['filename'] for p in self.proof_rows()})
        self.now+=5*DAY
        self.assertEqual(self.plugin.cleanup_uploaded_logs()['deleted'],3)
        self.assertTrue(path.exists())

    def test_invalid_retention_timestamp_never_falls_back_to_a_fabricated_age(self):
        data=self.data();self.assertTrue(self.plugin._remember_receipt(data,self.complete(data)))
        key=self.plugin._receipt_key(data)
        for stamp in (float('nan'),self.now+DAY,10**1000):
            with self.subTest(stamp_type=type(stamp).__name__):
                self.plugin._upload_ack_times[key]=stamp
                self.plugin._retention_confirm_files(self.plugin._retention_capture(self.session,data),data)
                self.assertEqual(self.proof_rows(),[])

    def test_structurally_short_identity_only_no_gps_tail_keeps_the_mixed_export(self):
        path=self.session/'wardriving.csv'
        path.write_text(path.read_text()+'02:00:00:00:00:FF,truncated\n')
        self.uploaded_and_aged_except('wardriving.csv')

    def test_invalid_raw_wifi_type_cannot_be_reinterpreted_as_a_no_gps_residual(self):
        path=self.session/'wardriving.csv'
        path.write_text(path.read_text()+'02:00:00:00:00:FF,Malformed,WPA2,now,6,-60,12.5,34.5,0,0,BROKEN\n')
        self.uploaded_and_aged_except('wardriving.csv')


def load_tests(loader,tests,pattern):
    # Reuse the real fixture without executing its existing cases a second time.
    return unittest.TestSuite(AckTimingTests(name) for name in AckTimingTests.__dict__ if name.startswith('test_'))
