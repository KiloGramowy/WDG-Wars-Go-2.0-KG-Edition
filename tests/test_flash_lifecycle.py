"""Regression interleavings of the actual app methods; never flash hardware."""
from pathlib import Path
import subprocess
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_flash_preflight import FlashEnvironment, FakeSerial, FakeProcess, TABLES


class FlashLifecycleTests(unittest.TestCase):
    def environment(self,**kwargs):
        env=FlashEnvironment(**kwargs);self.addCleanup(env.close);return env

    def assert_released(self,env):
        self.assertFalse(env.game._flash_running)
        self.assertFalse(env.game._serial_busy)
        self.assertFalse(env.game._flash_job_lock.locked())

    def test_poll_send_and_reconnect_cannot_compete_with_live_flasher(self):
        env=self.environment()
        def interleave(g):
            self.assertTrue(g._flash_running);self.assertTrue(g._serial_busy)
            self.assertTrue(g._flash_job_lock.locked())
            before=list(env.events)
            g._poll_serial()
            self.assertFalse(g._try_reconnect_esp32())
            self.assertFalse(g._send('stop'))
            self.assertFalse(g._send('version'))
            self.assertFalse(g._send('scan_networks'))
            self.assertFalse(g._send('sniff'))
            self.assertEqual(env.events,before)
        env.during_flash=interleave
        env.flash()
        self.assert_released(env)
        self.assertEqual([x for x in env.events if x[0]=='serial-open'],[('serial-open','/dev/ttyACM1')])

    def test_preparing_operation_also_rejects_normal_commands(self):
        env=self.environment();g=env.game
        self.assertTrue(g._flash_running);self.assertFalse(g._serial_busy)
        before=list(env.events)
        self.assertFalse(g._send('scan_bt'));self.assertFalse(g._try_reconnect_esp32());g._poll_serial()
        self.assertEqual(env.events,before)

    def test_reopening_running_wizard_does_not_reset_operation_or_start_thread(self):
        env=self.environment()
        def interleave(g):
            g._start_flash_esp32()
            self.assertTrue(g._flash_running)
            env.buttons.add(env.px.KEY_RETURN);g._update_flash_screen()
            self.assertEqual(env.threads,[])
            self.assertEqual(len(env.flash_commands),1)
        env.during_flash=interleave;env.flash();self.assert_released(env)

    def test_escape_only_hides_progress_and_reopen_preserves_guard(self):
        env=self.environment();g=env.game
        g._flash_screen=True;env.buttons.add(env.px.KEY_ESCAPE);g._update_flash_screen()
        self.assertFalse(g._flash_screen);self.assertTrue(g._flash_running)
        env.buttons.clear();g._start_flash_esp32()
        self.assertTrue(g._flash_screen);self.assertTrue(g._flash_running)

    def test_second_worker_is_rejected_without_releasing_first_workers_guard(self):
        env=self.environment()
        def interleave(g):
            before=len(env.probe_calls)
            g._run_flash('wroom')
            self.assertEqual(len(env.probe_calls),before)
            self.assertEqual(len(env.flash_commands),1)
            self.assertTrue(g._flash_running);self.assertTrue(g._serial_busy)
            self.assertTrue(g._flash_job_lock.locked())
        env.during_flash=interleave;env.flash();self.assert_released(env)

    def test_wizard_cannot_admit_another_job_during_final_reconnect(self):
        env=self.environment();g=env.game
        def factory(port):
            serial=FakeSerial(env,port)
            original_setup=serial.setup
            def setup():
                self.assertTrue(g._flash_running);self.assertTrue(g._flash_job_lock.locked())
                g._start_flash_esp32();env.buttons.add(env.px.KEY_RETURN);g._update_flash_screen()
                self.assertEqual(env.threads,[]);self.assertEqual(len(env.flash_commands),1)
                original_setup()
            serial.setup=setup
            return serial
        env.namespace['SerialManager']=factory
        env.flash();self.assert_released(env)

    def test_enter_during_final_job_unlock_does_not_strand_active_flag(self):
        env=self.environment();g=env.game;underlying=threading.Lock();handoffs=[]
        class ReleaseHook:
            def acquire(self,blocking=True):return underlying.acquire(blocking=blocking)
            def locked(self):return underlying.locked()
            def release(self):
                # Finalization has cleared its active flag, but still owns
                # the job lock. An Enter in this exact gap must not reserve
                # another operation that cannot acquire the same lock.
                self_test.assertFalse(g._flash_running)
                self_test.assertTrue(underlying.locked())
                g._start_flash_esp32();env.buttons.add(env.px.KEY_RETURN)
                g._update_flash_screen()
                self_test.assertFalse(g._flash_running)
                self_test.assertEqual(env.threads,[])
                handoffs.append('checked');underlying.release()
        self_test=self
        g._flash_job_lock=ReleaseHook()
        env.flash()
        self.assertEqual(handoffs,['checked']);self.assert_released(env)

    def test_private_reconnect_override_is_not_usable_from_another_thread(self):
        env=self.environment();g=env.game;results=[]
        # The controlled worker records its own identifier. A separate caller
        # cannot borrow that bypass while normal access is suspended.
        g._flash_reconnect_thread=threading.get_ident()
        def outsider():results.append(g._try_reconnect_esp32(_flash_owner=True))
        thread=threading.Thread(target=outsider);thread.start();thread.join(2)
        self.assertFalse(thread.is_alive());self.assertEqual(results,[False])
        self.assertFalse(any(x[0] in ('detect-port','serial-open','serial-command') for x in env.events))

    def test_wroom_selects_correct_archive_even_when_xiao_is_first(self):
        env=self.environment(aio=False);env.reverse_assets=True;env.flash('wroom')
        downloads=[x[1] for x in env.events if x[0]=='download']
        self.assertIn('https://example.invalid/wroom.zip',downloads)
        self.assertNotIn('https://example.invalid/xiao.zip',downloads)
        self.assertEqual(Path(env.flash_commands[0][0][-1]).name,'projectZerobyLOCOSP.bin')

    def test_incomplete_current_archive_does_not_use_old_cache(self):
        env=self.environment();cache=env.root/'firmware_cache';cache.mkdir()
        old={name:b'OLD CACHED FILE' for name in TABLES['FLASH_BOARDS']['xiao']['offsets']}
        for name,data in old.items():(cache/name).write_bytes(data)
        env.firmware_entries={'bootloader.bin':b'new boot','partition-table.bin':b'new partitions'}
        env.flash()
        self.assertEqual(env.flash_commands,[])
        self.assertFalse(any(x[0] in ('serial-close','power') for x in env.events))
        self.assertTrue(env.game.serial.is_open);self.assertEqual(env.game._fw_version,'1.6.5')
        self.assertEqual({name:(cache/name).read_bytes() for name in old},old)
        self.assert_released(env)

    def test_cache_access_error_has_message_and_preserves_existing_connection(self):
        env=self.environment();original=Path.mkdir;cache=env.root/'firmware_cache'
        def denied(path,*args,**kwargs):
            if path==cache:raise PermissionError('fixture permission denied')
            return original(path,*args,**kwargs)
        with patch.object(Path,'mkdir',denied):env.flash()
        self.assertEqual(env.flash_commands,[]);self.assertTrue(env.game.serial.is_open)
        self.assertEqual(env.game._fw_version,'1.6.5');self.assert_released(env)
        self.assertIn('permission','\n'.join(env.output).lower());self.assertTrue(env.messages)

    def test_false_initial_aio_result_prevents_write(self):
        env=self.environment();env.power_result=False;env.flash()
        self.assertEqual(env.flash_commands,[]);self.assert_released(env)
        self.assertTrue(any('power' in text.lower() for text in env.output))

    def test_raising_initial_aio_result_prevents_write_and_reports_failure(self):
        env=self.environment();env.power_error=OSError('fake GPIO failure');env.flash()
        self.assertEqual(env.flash_commands,[]);self.assert_released(env)
        self.assertIn('GPIO','\n'.join(env.output))

    def test_nonzero_write_exit_recovers_once_without_reporting_firmware_success(self):
        env=self.environment();env.flash_code=2;env.flash();self.assert_released(env)
        self.assertEqual(len(env.flash_commands),1)
        self.assertEqual(len([x for x in env.events if x[0]=='serial-open']),1)
        self.assertFalse(env.game._flash_write_complete)
        self.assertFalse(any('confirmed' in text.lower() for text,color in env.messages))
        self.assertTrue(any('code 2' in text for text in env.output))

    def test_usb_write_timeout_recovers_only_after_process_is_stopped(self):
        env=self.environment();env.flash_error=env.helper.FlashError('fake USB write timeout')
        env.flash();self.assert_released(env)
        events=[x[0] for x in env.events]
        self.assertLess(events.index('flash'),events.index('serial-open'))
        self.assertFalse(env.game._flash_write_complete)
        self.assertIn('timeout','\n'.join(env.output))

    def test_unconfirmed_child_exit_keeps_port_and_files_reserved_until_exit(self):
        env=self.environment();checks=[]
        def stubborn(command,on_line,*,on_process=None,**kwargs):
            process=env.process=FakeProcess(env)
            on_process(process)
            raise env.helper.FlashError('termination not confirmed',process=process)
        env.helpers.run_esptool=stubborn
        original_sleep=env.sleep
        def advance(seconds):
            if env.process is not None and env.process.returncode is None:
                self.assertTrue(env.game._flash_running);self.assertTrue(env.game._serial_busy)
                self.assertIsNotNone(env.game._flash_workspace)
                self.assertFalse(any(x[0]=='serial-open' for x in env.events))
                checks.append(seconds)
                if len(checks)==2:env.process.returncode=2
            original_sleep(seconds)
        env.fake_time.sleep=advance
        env.flash();self.assertEqual(len(checks),2);self.assert_released(env)
        self.assertEqual(len([x for x in env.events if x[0]=='serial-open']),1)

    def test_usb_renumbering_uses_rediscovered_port_for_write_and_reconnect(self):
        env=self.environment();env.detect_port='/dev/ttyACM7';env.port_exists['/dev/ttyACM1']=False
        env.flash();command=env.flash_commands[0][0]
        self.assertEqual(command[command.index('-p')+1],'/dev/ttyACM7')
        self.assertIn(('serial-open','/dev/ttyACM7'),env.events);self.assert_released(env)

    def test_no_usb_port_fails_without_starting_child(self):
        env=self.environment();env.detect_port=None;env.port_exists['/dev/ttyACM1']=False
        original_serial=env.game.serial
        env.flash();self.assertEqual(env.flash_commands,[]);self.assert_released(env)
        self.assertIs(env.game.serial,original_serial);self.assertTrue(original_serial.is_open)
        self.assertTrue(env.game._esp32);self.assertTrue(env.game.state.connected);self.assertTrue(env.usb_on)
        self.assertEqual(env.game._fw_version,'1.6.5')
        self.assertFalse(any(x[0] in ('power','serial-close','serial-open') for x in env.events))
        self.assertTrue(any('port' in text.lower() for text in env.output))

    def test_serial_close_failure_preserves_still_open_manager_without_competitor(self):
        env=self.environment();old=env.game.serial;env.close_error=OSError('fixture native close failure')
        env.flash();self.assert_released(env)
        self.assertIs(env.game.serial,old);self.assertTrue(old.is_open)
        self.assertTrue(env.game._esp32);self.assertTrue(env.game.state.connected)
        self.assertEqual(env.game._fw_version,'1.6.5');self.assertEqual(env.flash_commands,[])
        self.assertFalse(any(x[0] in ('power','serial-open') for x in env.events))
        self.assertIn('native close failure','\n'.join(env.output))

    def test_serial_close_failure_after_handle_closed_allows_later_normal_recovery(self):
        env=self.environment();g=env.game;old=g.serial
        def failed_close():
            env.events.append(('serial-close',));old.is_open=False
            raise OSError('fixture close raised after handle was closed')
        old.close=failed_close
        env.flash();self.assert_released(env)
        self.assertIs(g.serial,old);self.assertFalse(g._esp32);self.assertFalse(g.state.connected)
        self.assertEqual(g._fw_version,'1.6.5');self.assertEqual(env.flash_commands,[])
        self.assertFalse(any(x[0] in ('power','serial-open') for x in env.events))
        g._poll_serial()
        self.assertIsNot(g.serial,old);self.assertTrue(g.serial.is_open);self.assertTrue(g.state.connected)

    def test_manual_usb_toggle_is_blocked_while_flash_owns_operation(self):
        env=self.environment();g=env.game
        g._toggle_usb()
        self.assertTrue(g._usb_enabled);self.assertTrue(env.usb_on)
        self.assertFalse(any(x[0]=='power' for x in env.events))
        self.assertTrue(any('Reserved' in text for text,color in env.messages))

    def test_shutdown_during_download_preserves_serial_version_and_usb(self):
        env=self.environment();g=env.game;old=g.serial
        def shutdown_on_archive(url):
            if url.endswith('/xiao.zip'):g._shutting_down=True
        env.during_download=shutdown_on_archive
        env.flash();self.assert_released(env)
        self.assertIs(g.serial,old);self.assertTrue(old.is_open);self.assertTrue(g.state.connected)
        self.assertEqual(g._fw_version,'1.6.5');self.assertTrue(env.usb_on);self.assertEqual(env.flash_commands,[])
        self.assertFalse(any(x[0] in ('power','serial-close','serial-open') for x in env.events))

    def test_old_firmware_check_cannot_publish_notice_after_device_generation_changes(self):
        env=self.environment();g=env.game
        del g._check_fw_update
        g._fw_generation=10;g._fw_update_available=False;g._fw_remote_version=''
        def changed_during_request(url):
            g._fw_generation+=1;g._fw_version='1.7.0'
        env.during_download=changed_during_request
        g._check_fw_update()
        self.assertFalse(g._fw_update_available);self.assertEqual(g._fw_remote_version,'')
        self.assertFalse(any('[FW]' in text for text,color in env.messages))

    def test_firmware_check_revalidates_generation_after_acquiring_serial_guard(self):
        env=self.environment();g=env.game
        del g._check_fw_update
        g._fw_generation=10;g._fw_update_available=False;g._fw_remote_version='';events=[]
        class GenerationChangesOnAcquire:
            def acquire(self):
                events.append('acquire');g._fw_generation+=1;g._fw_version='1.7.0';return True
            def release(self):events.append('release')
        g._esp_serial_lock=GenerationChangesOnAcquire()
        g._check_fw_update()
        self.assertEqual(events,['acquire','release'])
        self.assertFalse(g._fw_update_available);self.assertEqual(g._fw_remote_version,'')

    def test_inflight_old_serial_reply_is_processed_before_flash_invalidates_version(self):
        env=self.environment(aio=False);g=env.game;g._flash_running=False;g._fw_version=''
        g.loot=None;g._solo=SimpleNamespace(confirmed_wifi=False,confirmed_ble=False,
            handle_line=lambda line:True,disconnected=lambda:None)
        entered=threading.Event();release=threading.Event();errors=[]
        def old_read():
            entered.set()
            if not release.wait(3):raise AssertionError('Test did not release old serial read')
            env.events.append(('old-serial-reply',));return ['JanOS version: 1.6.5']
        g.serial.read_available=old_read
        def poll():
            try:g._poll_serial()
            except BaseException as error:errors.append(error)
        first=threading.Thread(target=poll);first.start();self.assertTrue(entered.wait(2))
        def flash():
            try:env.flash('wroom')
            except BaseException as error:errors.append(error)
        second=threading.Thread(target=flash);second.start()
        self.assertFalse(any(x[0]=='serial-close' for x in env.events))
        release.set();first.join(3);second.join(3)
        self.assertFalse(first.is_alive());self.assertFalse(second.is_alive());self.assertEqual(errors,[])
        names=[x[0] for x in env.events]
        self.assertLess(names.index('old-serial-reply'),names.index('serial-close'))
        self.assertEqual(g._fw_version,'');self.assertTrue(g._flash_fw_pending)
        self.assertFalse(any('confirmed' in text.lower() for text,color in env.messages))

    def test_successful_write_waits_for_fresh_firmware_response(self):
        env=self.environment();env.flash();g=env.game
        self.assertTrue(g._flash_write_complete);self.assertTrue(g._flash_fw_pending)
        self.assertEqual(g._fw_version,'');self.assertIn(('serial-command','version'),env.events)
        self.assertFalse(any('confirmed' in text.lower() for text,color in env.messages))
        g.loot=None;g._solo=SimpleNamespace(confirmed_wifi=False,confirmed_ble=False,handle_line=lambda line:True)
        g._handle_serial_line('JanOS version: 1.7.0')
        self.assertEqual(g._fw_version,'1.7.0');self.assertFalse(g._flash_fw_pending)
        self.assertTrue(any('1.7.0 confirmed' in text for text,color in env.messages))

    def test_fresh_ping_reply_can_confirm_firmware_after_write(self):
        env=self.environment();env.flash();g=env.game
        g.loot=None;g._solo=SimpleNamespace(confirmed_wifi=False,confirmed_ble=False,handle_line=lambda line:True)
        g._handle_serial_line('pong v1.7.0')
        self.assertEqual(g._fw_version,'1.7.0');self.assertFalse(g._flash_fw_pending)

    def test_missing_firmware_reply_remains_unconfirmed_after_deadline(self):
        env=self.environment();env.flash();g=env.game
        env.now+=16;g._poll_serial()
        self.assertEqual(g._fw_version,'');self.assertTrue(g._flash_fw_pending)
        self.assertTrue(any('not confirmed' in text.lower() for text,color in env.messages))
        self.assertFalse(any('v1.7.0 confirmed' in text for text,color in env.messages))

    def test_failed_download_preserves_device_and_old_version(self):
        env=self.environment();env.download_error=OSError('fake download failed');env.flash()
        self.assertTrue(env.game.serial.is_open);self.assertEqual(env.game._fw_version,'1.6.5')
        self.assertEqual(env.flash_commands,[]);self.assert_released(env)
        self.assertFalse(any(x[0] in ('serial-close','power','serial-open') for x in env.events))

    def test_reconnect_started_before_flash_finishes_before_serial_detach(self):
        env=self.environment(aio=False);g=env.game;g._flash_running=False;g.serial.is_open=False
        entered=threading.Event();release=threading.Event();errors=[];calls=[]
        def factory(port):
            serial=FakeSerial(env,port)
            if not calls:
                calls.append(port)
                def setup():
                    entered.set()
                    if not release.wait(3):raise AssertionError('Test did not release fake setup')
                    serial.is_open=True;env.events.append(('inflight-setup-complete',))
                serial.setup=setup
            return serial
        env.namespace['SerialManager']=factory
        def reconnect():
            try:g._try_reconnect_esp32()
            except BaseException as error:errors.append(error)
        first=threading.Thread(target=reconnect);first.start();self.assertTrue(entered.wait(2))
        def flash():
            try:env.flash('wroom')
            except BaseException as error:errors.append(error)
        second=threading.Thread(target=flash);second.start()
        self.assertFalse(any(x[0]=='serial-close' for x in env.events))
        release.set();first.join(3);second.join(3)
        self.assertFalse(first.is_alive());self.assertFalse(second.is_alive());self.assertEqual(errors,[])
        names=[x[0] for x in env.events]
        self.assertLess(names.index('inflight-setup-complete'),names.index('serial-close'))
        self.assertEqual(len(env.flash_commands),1);self.assert_released(env)


if __name__=='__main__':unittest.main()
