"""KG routing through real app methods, with fake network/USB/processes only."""
import threading
from types import SimpleNamespace
import unittest
from watchdogs.kg_firmware import KGFirmwareError
from test_flash_preflight import FlashEnvironment


class KGFirmwareIntegrationTests(unittest.TestCase):
    def environment(self):
        env=FlashEnvironment(allow_upstream=False);self.addCleanup(env.close)
        return env

    def assert_released(self,env):
        self.assertFalse(env.game._flash_running);self.assertFalse(env.game._serial_busy)
        self.assertFalse(env.game._flash_job_lock.locked())

    def assert_no_device_change(self,env):
        self.assertEqual(env.flash_commands,[])
        self.assertFalse(any(event[0] in ('serial-close','serial-open','power','flash') for event in env.events))
        self.assertTrue(env.game.serial.is_open);self.assertTrue(env.game.state.connected)
        self.assertTrue(env.usb_on);self.assert_released(env)

    def test_manual_positive_kg_path_writes_only_one_exact_app_without_header_overrides(self):
        env=self.environment();env.detect_port='/dev/ttyACM7';env.flash()
        self.assertEqual(len(env.flash_commands),1)
        command=env.flash_commands[0][0];tail=command[command.index('write-flash')+1:]
        self.assertEqual(len(tail),2);self.assertEqual(tail[0],'0x20000')
        self.assertTrue(tail[1].endswith('janos-1.7.0-solo-xiao-c5-app.bin'))
        self.assertEqual(command[command.index('-p')+1],'/dev/ttyACM7')
        for option in ('--force','--flash-mode','--flash-freq','--flash-size','erase-flash'):
            self.assertNotIn(option,command)
        self.assertEqual(env.kg_verify_calls,2)
        self.assertIn(('preflight','/dev/ttyACM7','xiao'),env.kg_calls)
        self.assertFalse(any(event[0] in ('download','detect-port') for event in env.events))
        self.assertIn(('serial-open','/dev/ttyACM7'),env.events)
        self.assertEqual(env.game._kg_fw_expected_version,'1.7.0')
        self.assertTrue(env.game._flash_fw_pending);self.assert_released(env)

    def test_metadata_download_and_image_errors_preserve_connected_board(self):
        for kind in ('metadata','download','verify'):
            with self.subTest(stage=kind):
                env=self.environment();error=KGFirmwareError('Synthetic KG validation failed')
                if kind=='metadata':env.kg_fetch_error=error
                elif kind=='download':env.download_error=error
                else:env.kg_verify_error=error
                env.flash();self.assert_no_device_change(env)

    def test_unknown_usb_identity_refuses_before_serial_close_or_gpio(self):
        env=self.environment();env.kg_bind_error=KGFirmwareError('Synthetic USB identity unavailable')
        env.flash();self.assert_no_device_change(env)
        self.assertFalse(any(event[0]=='preflight' for event in env.kg_calls))

    def test_active_esp32_operations_refuse_without_auto_stopping_or_preparing(self):
        for flag in ('wifi_scanning','_esp_stop_waiting','_wardrive_selected','solo'):
            with self.subTest(flag=flag):
                env=self.environment()
                if flag=='solo':env.game._solo=SimpleNamespace(busy=True)
                else:setattr(env.game,flag,True)
                env.flash();self.assertEqual(env.events,[]);self.assertEqual(env.kg_calls,[])
                self.assert_no_device_change(env)

    def test_shutdown_during_readonly_preflight_never_reaches_the_write(self):
        env=self.environment();env.during_kg_preflight=lambda game:setattr(game,'_shutting_down',True)
        env.flash();self.assertEqual(env.flash_commands,[])
        self.assertTrue(any(event[0]=='preflight' for event in env.kg_calls))
        self.assertEqual(env.game._kg_fw_expected_version,'')
        self.assertEqual(len(env.recovery_commands),1)
        self.assertEqual(env.recovery_commands[0][0][-1],'get-security-info')
        self.assertFalse(any(event[0]=='serial-open' for event in env.events));self.assert_released(env)

    def test_changed_usb_target_after_preflight_never_falls_back_to_another_device(self):
        env=self.environment()
        def changed(game):env.kg_bound_missing=True;env.detect_port='/dev/ttyACM99'
        env.during_kg_preflight=changed;env.flash()
        self.assertEqual(env.flash_commands,[])
        self.assertEqual(env.recovery_commands,[])
        self.assertFalse(any(event[0] in ('detect-port','serial-open') for event in env.events))
        self.assertIs(env.game._kg_fw_usb_identity,env.kg_identity)
        self.assertFalse(env.game._try_reconnect_esp32())
        self.assertFalse(any(event[0] in ('detect-port','serial-open') for event in env.events))
        self.assert_released(env)

    def test_preflight_rejection_preserves_binding_for_safe_later_reconnect(self):
        env=self.environment();env.kg_preflight_error=KGFirmwareError('Synthetic layout mismatch')
        env.flash();self.assertEqual(env.flash_commands,[])
        self.assertIn(('serial-open','/dev/ttyACM1'),env.events)
        self.assertEqual(len(env.recovery_commands),1)
        order=[event[0] for event in env.events]
        self.assertLess(order.index('recovery'),order.index('serial-open'))
        self.assertFalse(any(event[0]=='detect-port' for event in env.events));self.assert_released(env)

    def test_image_changed_after_preflight_cannot_be_written(self):
        env=self.environment();env.kg_verify_error=KGFirmwareError('Synthetic image changed')
        env.kg_verify_error_after=2;env.flash()
        self.assertEqual(env.flash_commands,[]);self.assertEqual(env.kg_verify_calls,2)
        self.assert_released(env)

    def test_same_version_manual_reinstall_allowed_but_older_package_refused(self):
        env=self.environment();env.game._fw_version='1.7.0';env.flash()
        self.assertEqual(len(env.flash_commands),1)
        env=self.environment();env.game._fw_version='1.7.1';env.flash()
        self.assert_no_device_change(env)

    def test_only_matching_fresh_report_can_confirm_a_kg_install(self):
        env=self.environment();env.flash();game=env.game;game.loot=None
        game._solo=SimpleNamespace(confirmed_wifi=False,confirmed_ble=False,handle_line=lambda line:True)
        game._handle_serial_line('JanOS version: 1.6.5')
        self.assertEqual(game._fw_version,'');self.assertTrue(game._flash_fw_pending)
        self.assertEqual(game._kg_fw_expected_version,'1.7.0')
        self.assertIs(game._kg_fw_usb_identity,env.kg_identity)
        game._handle_serial_line('JanOS version: 1.7.0')
        self.assertEqual(game._fw_version,'1.7.0');self.assertFalse(game._flash_fw_pending)
        self.assertEqual(game._kg_fw_expected_version,'');self.assertIsNone(game._kg_fw_usb_identity)

    def test_checker_generation_and_shutdown_prevent_late_notice_or_cache_commit(self):
        for change in ('generation','shutdown','already_shutting_down'):
            with self.subTest(change=change):
                env=self.environment();game=env.game;del game._check_fw_update
                game._kg_fw_package=None;game._fw_update_available=False;game._fw_remote_version=''
                if change=='already_shutting_down':game._shutting_down=True
                def mutate(g):
                    if change=='generation':g._fw_generation=getattr(g,'_fw_generation',0)+1
                    else:g._shutting_down=True
                env.during_kg_fetch=mutate;game._check_fw_update()
                self.assertIsNone(game._kg_fw_package);self.assertFalse(game._fw_update_available)
                self.assertEqual(game._fw_remote_version,'');self.assertEqual(env.output,[])
                self.assertEqual(env.messages,[])
                self.assertEqual(env.events,[('thread-start',)] if change=='generation' else [])
                if change=='already_shutting_down':self.assertEqual(env.kg_calls,[])

    def test_checker_revalidates_generation_under_the_serial_guard(self):
        env=self.environment();game=env.game;del game._check_fw_update
        game._fw_generation=10;game._fw_update_available=False;game._fw_remote_version='';calls=[]
        class ChangedOnAcquire:
            def acquire(self):game._fw_generation+=1;calls.append('acquire');return True
            def release(self):calls.append('release')
        game._esp_serial_lock=ChangedOnAcquire();game._check_fw_update()
        self.assertEqual(calls,['acquire','release']);self.assertFalse(game._fw_update_available)
        self.assertEqual(game._fw_remote_version,'');self.assertEqual(env.output,[])
        self.assertEqual(len(env.threads),1)
        self.assertEqual(env.threads[0].target.__name__,'_check_fw_update')

    def test_checker_busy_guard_does_not_duplicate_requests(self):
        env=self.environment();game=env.game;del game._check_fw_update
        lock=game._kg_fw_check_lock=threading.Lock();lock.acquire();game._kg_fw_checking=True
        try:
            game._check_fw_update();self.assertEqual(env.kg_calls,[]);self.assertTrue(game._kg_fw_checking)
        finally:lock.release()

    def test_enter_is_explicit_manual_worker_and_escape_does_not_start_another(self):
        env=self.environment();game=env.game
        game._flash_running=False;game._kg_fw_package=env.kg_package;game._kg_fw_error=''
        game._start_flash_esp32();self.assertEqual(env.threads,[])
        env.buttons={env.px.KEY_RETURN};game._update_flash_screen()
        self.assertEqual(len(env.threads),1);self.assertEqual(env.threads[0].target.__name__,'_run_flash')
        self.assertEqual(env.threads[0].args,('xiao',));self.assertTrue(game._flash_running)
        env.buttons={env.px.KEY_ESCAPE};game._update_flash_screen()
        self.assertFalse(game._flash_screen);self.assertTrue(game._flash_running)
        self.assertEqual(env.kg_calls,[]);self.assertEqual(env.flash_commands,[])

    def test_recovery_requires_matching_previous_version_before_claiming_responsiveness(self):
        env=self.environment();env.kg_preflight_error=KGFirmwareError('Synthetic layout refusal')
        env.flash();game=env.game;game.loot=None
        self.assertTrue(game._kg_fw_recovery_needed)
        self.assertEqual(game._kg_fw_recovery_version,'1.6.5')
        self.assertIn('boot not confirmed',game._flash_status)
        game._solo=SimpleNamespace(confirmed_wifi=False,confirmed_ble=False,handle_line=lambda line:True)
        game._handle_serial_line('JanOS version: 1.7.0')
        self.assertEqual(game._fw_version,'');self.assertTrue(game._kg_fw_recovery_needed)
        game._handle_serial_line('JanOS version: 1.6.5')
        self.assertEqual(game._fw_version,'1.6.5');self.assertFalse(game._kg_fw_recovery_needed)
        self.assertEqual(game._kg_fw_recovery_version,'');self.assertIsNone(game._kg_fw_usb_identity)
        self.assertIn('previous firmware KG 1.6.5 responsive',game._flash_status)
        self.assertFalse(game._flash_write_complete);self.assertEqual(env.flash_commands,[])

    def test_ambiguous_recovery_target_cannot_reset_or_borrow_another_device(self):
        env=self.environment()
        def ambiguous(game):env.kg_bound_error=KGFirmwareError('Synthetic USB ambiguity');env.detect_port='/dev/ttyACM99'
        env.during_kg_preflight=ambiguous;env.flash()
        self.assertEqual(env.recovery_commands,[]);self.assertEqual(env.flash_commands,[])
        self.assertFalse(any(event[0] in ('detect-port','serial-open') for event in env.events))
        self.assertFalse(env.game._try_reconnect_esp32());self.assert_released(env)

    def test_recovery_child_timeout_retains_workspace_and_serial_until_observed_exit(self):
        env=self.environment();env.kg_preflight_error=KGFirmwareError('Synthetic layout refusal')
        env.recovery_code=None;env.recovery_error=env.helper.FlashError('Synthetic recovery timeout')
        original_sleep=env.sleep;waiting=[]
        def advance(seconds):
            if env.recovery_commands and env.process is not None and env.process.returncode is None:
                self.assertTrue(env.game._flash_running);self.assertTrue(env.game._serial_busy)
                self.assertTrue(env.game._flash_job_lock.locked())
                self.assertIsNotNone(env.game._flash_workspace)
                self.assertFalse(any(event[0]=='serial-open' for event in env.events))
                waiting.append(seconds)
                if len(waiting)==2:env.process.returncode=2
            original_sleep(seconds)
        env.fake_time.sleep=advance;env.flash()
        self.assertEqual(len(waiting),2);self.assertEqual(env.flash_commands,[])
        self.assertEqual(len(env.recovery_commands),1);self.assert_released(env)
        self.assertTrue(env.game._kg_fw_recovery_needed)

    def test_recovery_reset_and_restore_follow_bound_usb_port_after_renumbering(self):
        env=self.environment();env.kg_bound_port='/dev/ttyACM7';env.detect_port='/dev/ttyACM99'
        env.kg_preflight_error=KGFirmwareError('Synthetic layout refusal');env.flash()
        self.assertEqual(env.flash_commands,[]);self.assertEqual(len(env.recovery_commands),1)
        command,options=env.recovery_commands[0]
        self.assertEqual(command[command.index('-p')+1],'/dev/ttyACM7')
        self.assertEqual(command[command.index('--before')+1],'no-reset')
        self.assertEqual(command[command.index('--after')+1],'hard-reset')
        self.assertEqual(options['total_timeout'],30);self.assertEqual(options['idle_timeout'],10)
        self.assertIn(('serial-open','/dev/ttyACM7'),env.events)
        self.assertFalse(any(event[0]=='detect-port' for event in env.events));self.assert_released(env)

    def test_normal_reconnect_invalidates_known_version_and_requests_fresh_kg_check(self):
        env=self.environment();game=env.game;del game._check_fw_update
        game._flash_running=False;game.serial=None;game._esp32=False;game.state.connected=False
        game._fw_generation=10;game._fw_version='1.6.5';game._fw_remote_version='1.7.3';game._fw_update_available=True
        self.assertTrue(game._try_reconnect_esp32())
        self.assertEqual(game._fw_generation,11);self.assertEqual(game._fw_version,'')
        self.assertEqual(game._fw_remote_version,'');self.assertFalse(game._fw_update_available)
        self.assertIn(('serial-command','version'),env.events)
        self.assertEqual(env.threads,[]);self.assertEqual(env.kg_calls,[])
        game.loot=None;game._solo=SimpleNamespace(confirmed_wifi=False,confirmed_ble=False,handle_line=lambda line:True)
        game._handle_serial_line('JanOS version: 1.7.0')
        self.assertEqual(game._fw_version,'1.7.0');self.assertEqual(len(env.threads),1)
        self.assertEqual(env.threads[0].target.__name__,'_check_fw_update')
        self.assertEqual(env.flash_commands,[]);self.assertFalse(any(event[0]=='power' for event in env.events))

    def test_stale_check_success_or_error_retries_current_connection_only_after_unlock(self):
        for outcome in ('success','error'):
            with self.subTest(outcome=outcome):
                env=self.environment();game=env.game;del game._check_fw_update
                game._fw_generation=10;game._fw_remote_version='';game._fw_update_available=False
                game._kg_fw_package=None;game._kg_fw_error=''
                def reconnect_during_fetch(g):
                    g._fw_generation+=1;g._fw_version='1.7.0'
                    # A check for the new version arrives while the old
                    # request still owns admission and cannot run yet.
                    g._check_fw_update()
                env.during_kg_fetch=reconnect_during_fetch
                if outcome=='error':env.kg_fetch_error=KGFirmwareError('Synthetic old-connection failure')
                thread_class=env.namespace['threading'].Thread;original_start=thread_class.start;started=[]
                def start_after_unlock(thread):
                    self.assertFalse(game._kg_fw_check_lock.locked())
                    started.append('unlocked');original_start(thread)
                thread_class.start=start_after_unlock
                game._check_fw_update()
                self.assertEqual(env.kg_calls,[('metadata',)])
                self.assertIsNone(game._kg_fw_package);self.assertEqual(game._kg_fw_error,'')
                self.assertFalse(game._fw_update_available);self.assertEqual(game._fw_remote_version,'')
                self.assertEqual(env.messages,[]);self.assertEqual(env.output,[])
                self.assertEqual(started,['unlocked']);self.assertEqual(len(env.threads),1)
                self.assertEqual(env.threads[0].target.__name__,'_check_fw_update')


if __name__=='__main__':unittest.main()
