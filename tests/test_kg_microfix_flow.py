"""KG repair/migration UI with real controller and fake USB/process boundaries.

No test opens a serial device, installs a dependency or executes esptool.
"""
import ast
import copy
from dataclasses import replace
import json
import re
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from watchdogs import kg_firmware as kg
from watchdogs.wardrive_solo import WardriveSoloController
from test_flash_preflight import FlashEnvironment, GAME, GAME_PYTHON, SOURCE


def supported_caps():
    return '@SOLO ' + json.dumps(dict(v=1, event='caps', boot_id='0123456789abcdef',
        emitted_monotonic_us=1_000_000, clock_monotonic_us=1_000_000,
        supported=True, contract=1, firmware='1.7.0', sdk='synthetic-sdk',
        max_line_bytes=4096, channels=[1, 6, 11], skipped=[], country={}))


class KGMicrofixFlowTests(unittest.TestCase):
    def environment(self, probe_results=None):
        env = FlashEnvironment(probe_results, aio=False, allow_upstream=False)
        self.addCleanup(env.close)
        clock = patch('watchdogs.kg_firmware.time.monotonic', lambda: env.now)
        clock.start()
        self.addCleanup(clock.stop)
        g = env.game
        g._flash_running = False
        g._flash_screen = True
        g._fw_version = '1.7.5'
        g._fw_generation = 7
        g._kg_fw_package = env.kg_package
        g._kg_fw_error = g._kg_esptool_error = ''
        g._kg_esptool_version = '5.4.0'
        g._kg_probe = None
        g._kg_probe_requested = False
        g._kg_migration_confirm = False
        g._kg_fw_migration_authorization = None
        g._kg_fw_migration_serial_token = None
        g._wardrive_selected = ()
        g.loot = None
        g.state.portal_running = g.state.evil_twin_running = False
        g.net_mgr = SimpleNamespace(parse_network_line=lambda _: None)
        env.namespace['_AIRTAG_COUNT_RE'] = re.compile(r'(?!)')
        env.namespace['_BLE_RE'] = re.compile(r'(?!)')
        # Load the unchanged production STOP dispatch/TX methods; only their
        # external arbiter callback is fake, like the shared hardware harness.
        for name in ('_begin_esp_scan', '_dispatch_pending_esp_command',
                     '_clear_esp_scan_state', '_handle_esp_operation_state',
                     '_update_gameupd_screen'):
            node = copy.deepcopy(next(n for n in GAME.body
                if isinstance(n, ast.FunctionDef) and n.name == name))
            exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])),
                         str(SOURCE), 'exec'), env.namespace)
            setattr(type(g), name, env.namespace[name])
            if name in g.__dict__:
                delattr(g, name)
        g._pending_cmd = None
        g._pending_cmd_state = g._pending_cmd_name = ''

        def queue_after_stop(command, state, label):
            env.events.append(('stop-barrier', command))
            g._pending_cmd, g._pending_cmd_state, g._pending_cmd_name = command, state, label
            g._esp_stop_waiting, g._esp_stop_ack = True, False
            g._esp_stop_requested_at = env.now
            g._esp_stop_timed_out = False
            g._pending_cmd_frame = env.px.frame_count
            return True

        g._solo = WardriveSoloController(queue_after_stop, lambda: True, lambda _: None,
            monotonic=lambda: env.now, utc_clock=lambda: env.now)
        g.serial.in_waiting = 0
        g.serial.line_buffer = SimpleNamespace(_buf=b'')
        return env

    def key(self, env, key):
        env.buttons = {key}
        env.game._update_flash_screen()
        env.buttons.clear()

    def dispatch_caps(self, env, *, buffer=b'', waiting=0, barrier=True):
        g = env.game
        # A successful fake write/reconnect retains the real STOP barrier.
        # Deliver its ACK and advance the required frame delay before probing.
        if g._esp_stop_waiting and g._pending_cmd is None:
            g._handle_serial_line('All operations stopped.')
            env.px.frame_count += 15
            g._dispatch_pending_esp_command()
        g._kg_request_probe()
        g._kg_probe_tick(dispatch=True)
        self.assertEqual(g._solo.state, 'queued_caps')
        self.assertFalse(any(e == ('serial-command', 'wardrive_solo_caps') for e in env.events))
        g.serial.line_buffer = SimpleNamespace(_buf=buffer)
        g.serial.in_waiting = waiting
        if barrier:
            g._esp_stop_ack = True
            g._dispatch_pending_esp_command()
        else:
            g._esp_stop_waiting = False
            g._pending_cmd = None
            g._begin_esp_scan('wardrive_solo_caps', 'solo_control', 'Check SOLO interface')
        self.assertEqual(g._solo.state, 'await_caps')

    def unsupported(self, env, **kwargs):
        self.dispatch_caps(env, **kwargs)
        env.now += .1
        env.game._handle_serial_line('Unrecognized command')
        env.game._kg_probe_tick(dispatch=True)
        return env.game._solo.capability_status

    def migration_ready(self, env):
        self.assertEqual(self.unsupported(env), 'unsupported')
        self.assertEqual(env.game._kg_probe['phase'], 'version')
        env.now += .1
        env.game._handle_serial_line('JanOS version: 1.7.5')
        self.assertEqual(env.game._kg_probe['phase'], 'ready')

    def authorize_from_ui(self, env):
        self.migration_ready(env)
        self.key(env, env.px.KEY_M)
        self.assertTrue(env.game._kg_migration_confirm)
        self.key(env, env.px.KEY_Y)
        self.assertIsInstance(env.game._kg_fw_migration_authorization, kg.MigrationAuthorization)

    def assert_no_write(self, env):
        self.assertEqual(env.flash_commands, [])
        self.assertFalse(getattr(env.game, '_flash_write_started', False))

    def test_explicit_p_repairs_only_game_python_and_never_flashes(self):
        env = self.environment([1, 0, 0])
        env.helpers.repair_esptool = lambda executable, **kwargs: env.helper.repair_esptool(
            executable, run=env.run, **kwargs)
        self.key(env, env.px.KEY_P)
        self.assertEqual(len(env.threads), 1)
        worker = env.threads[0]
        self.assertEqual(worker.kwargs, {'repair': True})
        worker.target(**worker.kwargs)
        commands = [command for command, _ in env.probe_calls]
        self.assertEqual(commands[0], [GAME_PYTHON, '-m', 'esptool', 'version'])
        self.assertTrue(any(command[:3] == [GAME_PYTHON, '-m', 'pip'] for command in commands))
        self.assertTrue(all(command[0] == GAME_PYTHON for command in commands))
        self.assertEqual(env.game._kg_esptool_version, '5.4.0')
        self.assertEqual(env.kg_calls, [])
        self.assertFalse(any(e[0] in ('power', 'serial-close') for e in env.events))
        self.assert_no_write(env)

    def test_dependency_failure_blocks_enter_without_job_or_device_change(self):
        env = self.environment([1])
        env.game._kg_check_python()
        self.assertTrue(env.game._kg_esptool_error)
        self.key(env, env.px.KEY_RETURN)
        self.assertEqual(env.threads, [])
        self.assertEqual(env.kg_calls, [])
        self.assert_no_write(env)

    def test_delayed_python_repair_worker_refuses_after_shutdown(self):
        env = self.environment()
        repair_calls = []
        env.helpers.repair_esptool = lambda *args, **kwargs: repair_calls.append(args)
        env.game._kg_start_python_check(repair=True)
        self.assertEqual(len(env.threads), 1)
        worker = env.threads[0]
        self.assertTrue(env.game._kg_dependency_running)
        env.game._shutting_down = True
        worker.target(*worker.args, **worker.kwargs)
        self.assertEqual(repair_calls, [])
        self.assertEqual(env.probe_calls, [])
        self.assertFalse(env.game._kg_dependency_running)
        self.assertFalse(env.game._flash_job_lock.locked())
        self.assert_no_write(env)

    def test_shutdown_prevents_scheduling_new_python_repair(self):
        env = self.environment()
        env.game._shutting_down = True
        env.game._kg_start_python_check(repair=True)
        self.assertEqual(env.threads, [])
        self.assertEqual(env.probe_calls, [])
        self.assertFalse(getattr(env.game, '_kg_dependency_running', False))
        self.assert_no_write(env)

    def test_python_repair_blocks_game_update_cleanup_until_worker_releases_job(self):
        env = self.environment()
        g = env.game
        entered, repair_resume = threading.Event(), threading.Event()
        finalizing, unlock_resume = threading.Event(), threading.Event()
        actions, errors = [], []

        class FinalizationLock:
            def __init__(self):
                self.inner = threading.Lock()

            def acquire(self, blocking=True):
                return self.inner.acquire(blocking=blocking)

            def locked(self):
                return self.inner.locked()

            def release(self):
                # The worker clears dependency_running before releasing its
                # operation lock. Exercise this real finalization ordering too.
                finalizing.set()
                if not unlock_resume.wait(5):
                    self.inner.release()
                    raise RuntimeError('Test did not release the Python job lock')
                self.inner.release()

        g._flash_job_lock = FinalizationLock()

        def repair(executable, **kwargs):
            self.assertEqual(executable, GAME_PYTHON)
            entered.set()
            if not repair_resume.wait(5):
                raise RuntimeError('Test did not release the fake dependency repair')
            return '5.4.0'

        def run_worker():
            try:
                g._kg_check_python(repair=True)
            except BaseException as exc:
                errors.append(exc)

        env.helpers.repair_esptool = repair
        plan = object()
        g._gameupd_plan = plan
        g._gameupd_open_frame = env.px.frame_count - 1
        g._gameupd_install_frame = -1
        g._gameupd_running = g._gameupd_done = g._gameupd_closed_for_install = False
        g._gameupd_restart_at = None
        g._gameupd_confirm = True
        g._cleanup_errors = []
        g._gameupd_log_add = lambda line: env.output.append(line)
        g._cleanup = lambda: actions.append('cleanup')
        g._gameupd_do = lambda: actions.append('install')
        g._gameupd_restart_process = lambda: actions.append('restart')
        g._gameupd_discard_plan = lambda: actions.append('discard')

        def press_y():
            env.buttons = {env.px.KEY_Y}
            g._update_gameupd_screen()
            env.buttons.clear()

        worker = threading.Thread(target=run_worker, name='kg-test-python-repair')
        worker.start()
        try:
            self.assertTrue(entered.wait(5))
            self.assertTrue(g._kg_dependency_running)
            self.assertFalse(g._flash_running)
            press_y()
            self.assertEqual(actions, [])
            self.assertTrue(g._gameupd_confirm)
            self.assertIs(g._gameupd_plan, plan)
            self.assertFalse(g._gameupd_closed_for_install)
            repair_resume.set()
            self.assertTrue(finalizing.wait(5))
            self.assertFalse(g._kg_dependency_running)
            self.assertTrue(g._flash_job_lock.locked())
            press_y()
            self.assertEqual(actions, [])
            self.assertTrue(g._gameupd_confirm)
            self.assertIs(g._gameupd_plan, plan)
        finally:
            repair_resume.set()
            unlock_resume.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertFalse(g._flash_job_lock.locked())
        press_y()
        self.assertEqual(actions, ['cleanup'])
        self.assertFalse(g._gameupd_confirm)
        self.assertTrue(g._gameupd_closed_for_install)
        env.px.frame_count += 1
        g._update_gameupd_screen()
        self.assertEqual(actions, ['cleanup', 'install'])
        self.assert_no_write(env)

    def test_m_dialog_n_and_escape_create_no_job(self):
        for key_name in ('KEY_N', 'KEY_ESCAPE'):
            with self.subTest(key=key_name):
                env = self.environment()
                self.migration_ready(env)
                jobs_before = len(env.threads)
                self.key(env, env.px.KEY_M)
                self.assertTrue(env.game._kg_migration_confirm)
                self.key(env, getattr(env.px, key_name))
                self.assertFalse(env.game._kg_migration_confirm)
                self.assertEqual(len(env.threads), jobs_before)
                self.assertIsNone(env.game._kg_fw_migration_authorization)
                self.assert_no_write(env)

    def test_expired_unknown_and_wrong_usb_evidence_never_opens_m(self):
        for invalid in ('expired', 'unknown', 'usb', 'connection', 'version'):
            with self.subTest(invalid=invalid):
                env = self.environment()
                self.migration_ready(env)
                if invalid == 'expired': env.now += 31
                elif invalid == 'unknown': env.game._solo.capability_status = 'unknown'
                elif invalid == 'usb': env.kg_identity = replace(env.kg_identity, serial_number='OTHER-UNIT')
                elif invalid == 'connection': env.game._fw_generation += 1
                else: env.game._fw_version = '1.7.4'
                before = len(env.threads)
                self.key(env, env.px.KEY_M)
                self.assertFalse(env.game._kg_migration_confirm)
                self.key(env, env.px.KEY_Y)
                self.assertEqual(len(env.threads), before)
                self.assertIsNone(env.game._kg_fw_migration_authorization)
                self.assert_no_write(env)

    def test_y_revalidates_evidence_expired_or_usb_changed_inside_dialog(self):
        for invalid in ('expired', 'usb'):
            with self.subTest(invalid=invalid):
                env = self.environment()
                self.migration_ready(env)
                self.key(env, env.px.KEY_M)
                if invalid == 'expired': env.now += 31
                else: env.kg_identity = replace(env.kg_identity, serial_number='OTHER-UNIT')
                before = len(env.threads)
                self.key(env, env.px.KEY_Y)
                self.assertEqual(len(env.threads), before)
                self.assertIsNone(env.game._kg_fw_migration_authorization)
                self.assert_no_write(env)

    def test_new_reported_version_invalidates_previous_negative_before_m(self):
        for line in ('JanOS version: 1.7.4', '> WatchDogsGo version: v1.7.4'):
            with self.subTest(line=line):
                env = self.environment()
                self.migration_ready(env)
                env.game._handle_serial_line(line)
                self.assertEqual(env.game._fw_version, '1.7.4')
                self.assertIsNone(env.game._kg_probe)
                self.assertEqual(env.game._solo.capability_status, 'unknown')
                self.assertIsNone(env.game._solo.capability_context)
                before = len(env.threads)
                self.key(env, env.px.KEY_M)
                self.assertFalse(env.game._kg_migration_confirm)
                self.assertEqual(len(env.threads), before)
                self.assertIsNone(env.game._kg_fw_migration_authorization)
                self.assert_no_write(env)

    def test_boot_banner_during_m_dialog_cannot_reuse_negative_on_y(self):
        env = self.environment()
        self.migration_ready(env)
        self.key(env, env.px.KEY_M)
        self.assertTrue(env.game._kg_migration_confirm)
        # Even an unchanged numeric version belongs to a new boot, not to
        # the previously measured negative interface response.
        env.game._handle_serial_line('APP_MAIN START (v1.7.5)')
        self.assertEqual(env.game._fw_version, '1.7.5')
        self.assertIsNone(env.game._kg_probe)
        self.assertEqual(env.game._solo.capability_status, 'unknown')
        self.assertIsNone(env.game._solo.capability_context)
        before = len(env.threads)
        self.key(env, env.px.KEY_Y)
        self.assertFalse(env.game._kg_migration_confirm)
        self.assertEqual(len(env.threads), before)
        self.assertIsNone(env.game._kg_fw_migration_authorization)
        self.assert_no_write(env)

    def test_actual_fresh_negative_and_version_authorize_exactly_one_job(self):
        env = self.environment()
        self.authorize_from_ui(env)
        self.assertEqual(env.game._kg_fw_migration_authorization.reported_version, '1.7.5')
        self.assertEqual(env.game._kg_fw_migration_authorization.package_version, '1.7.0')
        self.assertEqual(len(env.threads), 1)
        self.assertEqual(env.threads[0].args, ('xiao',))
        self.key(env, env.px.KEY_Y)
        self.assertEqual(len(env.threads), 1)
        self.assert_no_write(env)

    def test_unrecognized_needs_stop_barrier_and_empty_rx(self):
        for buffer, waiting, barrier in ((b'partial', 0, True), (b'', 1, True), (b'', 0, False)):
            with self.subTest(buffer=buffer, waiting=waiting, barrier=barrier):
                env = self.environment()
                self.assertNotEqual(self.unsupported(env, buffer=buffer, waiting=waiting,
                                                     barrier=barrier), 'unsupported')
                self.key(env, env.px.KEY_M)
                self.assertFalse(env.game._kg_migration_confirm)
                self.assert_no_write(env)

    def test_bare_prompt_rx_is_safe_but_caps_timeout_is_unknown(self):
        env = self.environment()
        self.assertEqual(self.unsupported(env, buffer=b'> '), 'unsupported')
        env = self.environment()
        self.dispatch_caps(env)
        env.now += 6
        env.game._solo.tick()
        self.assertEqual(env.game._solo.capability_status, 'unknown')
        env.game._kg_probe_tick(dispatch=True)
        self.key(env, env.px.KEY_M)
        self.assertFalse(env.game._kg_migration_confirm)
        self.assert_no_write(env)

    def test_negative_reply_needs_new_exact_version_not_previous_banner(self):
        env = self.environment()
        self.assertEqual(self.unsupported(env), 'unsupported')
        self.key(env, env.px.KEY_M)
        self.assertFalse(env.game._kg_migration_confirm)
        env.game._handle_serial_line('boot noise JanOS version: 1.7.5')
        self.assertEqual(env.game._kg_probe['phase'], 'version')
        env.now += 6
        env.game._handle_serial_line('JanOS version: 1.7.5')
        self.assertNotEqual(env.game._kg_probe['phase'], 'ready')
        self.assert_no_write(env)

    def test_normal_downgrade_refuses_before_download_or_serial_close(self):
        env = self.environment()
        env.game._run_flash('xiao')
        self.assertIn(('metadata',), env.kg_calls)
        self.assertFalse(any(e[0] == 'download' for e in env.kg_calls))
        self.assertFalse(any(e[0] == 'serial-close' for e in env.events))
        self.assert_no_write(env)

    def test_authorized_migration_backs_up_before_one_exact_app_write(self):
        env = self.environment()
        self.authorize_from_ui(env)
        def backup(*args, **kwargs):
            self.assertTrue(kwargs['check_current_device']())
            env.events.append(('backup-verified',))
            return SimpleNamespace(directory=env.root / 'private-recovery')
        env.kg_helpers.backup_migration_device = backup
        env.game._run_flash('xiao')
        self.assertEqual(len(env.flash_commands), 1)
        order = [e[0] for e in env.events]
        self.assertLess(order.index('backup-verified'), order.index('flash'))
        command = env.flash_commands[0][0]
        self.assertEqual(command[command.index('write-flash') + 1], '0x20000')
        self.assertEqual(len(command[command.index('write-flash') + 1:]), 2)
        self.assertEqual(command[0], GAME_PYTHON)
        self.assertTrue(env.game._flash_fw_pending)

    def test_cancel_failed_backup_or_wrong_device_never_writes(self):
        for failure in ('cancel', 'backup', 'usb'):
            with self.subTest(failure=failure):
                env = self.environment()
                self.authorize_from_ui(env)
                def backup(*args, **kwargs):
                    env.events.append(('backup-attempt',))
                    if failure == 'backup': raise kg.KGFirmwareError('Synthetic backup failure')
                    if failure == 'cancel': env.game._kg_cancel_requested = True
                    else: env.kg_bound_missing = True
                    return SimpleNamespace(directory=env.root / 'private-recovery')
                env.kg_helpers.backup_migration_device = backup
                env.game._run_flash('xiao')
                self.assertIn(('backup-attempt',), env.events)
                self.assert_no_write(env)

    def test_explicit_c_cancels_queued_migration_before_any_device_acquisition(self):
        env = self.environment()
        self.authorize_from_ui(env)
        self.key(env, env.px.KEY_C)
        self.assertTrue(env.game._kg_cancel_requested)
        env.game._run_flash('xiao')
        self.assertFalse(any(e[0] in ('serial-close', 'power') for e in env.events))
        self.assert_no_write(env)

    def test_cancel_and_write_claim_are_atomic_in_both_thread_orderings(self):
        for first in ('cancel', 'claim'):
            with self.subTest(first=first):
                env = self.environment()
                self.authorize_from_ui(env)
                g = env.game
                g._flash_write_started = False
                owned, contender, resume = threading.Event(), threading.Event(), threading.Event()
                results, errors = {}, []
                first_name = 'kg-test-' + first

                class PausingLock:
                    """Pause the first operation *inside* the real shared lock."""
                    def __init__(self):
                        self.inner = threading.Lock()

                    def acquire(self, blocking=True, timeout=-1):
                        name = threading.current_thread().name
                        if name != first_name:
                            contender.set()
                        acquired = self.inner.acquire(blocking, timeout)
                        if acquired and name == first_name:
                            owned.set()
                            if not resume.wait(5):
                                self.inner.release()
                                raise RuntimeError('Test did not release the controlled lock')
                        return acquired

                    def release(self):
                        self.inner.release()

                    def locked(self):
                        return self.inner.locked()

                    def __enter__(self):
                        self.acquire()
                        return self

                    def __exit__(self, *args):
                        self.release()

                g._kg_cancel_lock = PausingLock()

                def perform(operation):
                    try:
                        action = (g._kg_cancel_migration if operation == 'cancel'
                                  else g._kg_claim_firmware_write)
                        results[operation] = action()
                    except BaseException as exc:
                        errors.append(exc)

                second = 'claim' if first == 'cancel' else 'cancel'
                winner = threading.Thread(target=perform, args=(first,), name=first_name)
                loser = threading.Thread(target=perform, args=(second,), name='kg-test-' + second)
                winner.start()
                try:
                    self.assertTrue(owned.wait(5), 'First operation did not acquire shared lock')
                    loser.start()
                    self.assertTrue(contender.wait(5), 'Second operation did not attempt shared lock')
                    self.assertNotIn(second, results, 'Contender bypassed the shared lock')
                finally:
                    resume.set()
                    winner.join(5)
                    if loser.ident is not None:
                        loser.join(5)
                self.assertFalse(winner.is_alive())
                self.assertFalse(loser.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(results, {first: True, second: False})
                self.assertEqual(g._kg_cancel_requested, first == 'cancel')
                self.assertEqual(g._flash_write_started, first == 'claim')
                self.assertFalse(g._kg_cancel_lock.locked())
                self.assertEqual(env.flash_commands, [])

    def test_usb_session_changed_after_y_refuses_before_backup_and_rom(self):
        env = self.environment()
        self.authorize_from_ui(env)
        env.game._fw_generation += 1
        env.kg_helpers.backup_migration_device = lambda *args, **kwargs: self.fail(
            'A changed session must not enter backup/ROM')
        env.game._run_flash('xiao')
        self.assertFalse(any(e[0] in ('serial-close', 'power') for e in env.events))
        self.assertFalse(any(e[0] == 'preflight' for e in env.kg_calls))
        self.assert_no_write(env)

    def test_matching_version_alone_keeps_pending_then_fresh_caps_confirms(self):
        env = self.environment()
        env.game._fw_version = '1.6.9'
        env.game._run_flash('xiao')
        self.assertEqual(len(env.flash_commands), 1)
        env.game._handle_serial_line('JanOS version: 1.7.0')
        self.assertTrue(env.game._flash_fw_pending)
        self.assertTrue(env.game._kg_probe_requested)
        self.dispatch_caps(env)
        env.now += .1
        env.game._handle_serial_line(supported_caps())
        env.game._kg_probe_tick()
        self.assertFalse(env.game._flash_fw_pending)
        self.assertIn('SOLO interface confirmed', env.game._flash_status)
        self.assertFalse(env.game._solo.busy)

    def test_stale_caps_wrong_context_and_wrong_usb_do_not_confirm_write(self):
        for failure in ('stale', 'context', 'usb'):
            with self.subTest(failure=failure):
                env = self.environment()
                g = env.game
                g._fw_version = '1.6.9'
                g._run_flash('xiao')
                g._handle_serial_line('JanOS version: 1.7.0')
                self.dispatch_caps(env)
                if failure == 'stale':
                    g._solo.handle_line(supported_caps(), monotonic=env.now - 1, utc=env.now - 1)
                else:
                    env.now += .1
                    g._handle_serial_line(supported_caps())
                    if failure == 'context': g._fw_generation += 1
                    else: g._kg_fw_usb_identity = replace(env.kg_identity, serial_number='OTHER-UNIT')
                g._kg_probe_tick()
                self.assertTrue(g._flash_fw_pending)


if __name__ == '__main__':
    unittest.main()
