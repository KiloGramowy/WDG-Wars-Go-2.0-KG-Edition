"""Real game lifecycle methods with fake transports; no radio, GPS or user data."""
import ast
import atexit
import copy
import logging
from pathlib import Path
from queue import Queue
import signal
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from watchdogs.aio_manager import AioPowerSession
from watchdogs.flipper_manager import FlipperManager
from watchdogs.subghz_manager import SubGhzManager
from watchdogs import race_attack

SOURCE=Path(__file__).resolve().parents[1]/'watchdogs/app.py'
TREE=ast.parse(SOURCE.read_text(encoding='utf-8'))
GAME=next(n for n in TREE.body if isinstance(n,ast.ClassDef) and n.name=='WatchDogsGame')
NAMES={'__init__','_cleanup','_guard_update','_guard_draw','_install_exit_handlers',
       '_restore_exit_handlers','_run_flash','_send','_try_reconnect_esp32',
       '_handle_esp_operation_state','_clear_esp_scan_state'}
ENV=dict(atexit=atexit,logging=logging,signal=signal,threading=threading,time=time,
         AioPowerSession=AioPowerSession,__name__='shutdown_test',
         pyxel=SimpleNamespace(quit=Mock()),C_ERROR=8)
NODE=ast.ClassDef(name='GameHarness',bases=[],keywords=[],decorator_list=[],
                 body=[copy.deepcopy(n) for n in GAME.body if isinstance(n,ast.FunctionDef) and n.name in NAMES])
exec(compile(ast.fix_missing_locations(ast.Module(body=[NODE],type_ignores=[])),str(SOURCE),'exec'),ENV)
GameHarness=ENV['GameHarness']


class PowerController:
    def __init__(self,events):self.events=events;self.states={k:False for k in ('gps','lora','usb','sdr')}
    def get_feature_state(self,feature):return self.states[feature]
    def toggle(self,feature,on):
        self.events.append(('power',feature,on));self.states[feature]=on;return True


def game():
    g=GameHarness.__new__(GameHarness)
    g.events=[]
    g.controller=PowerController(g.events)
    g._aio_power=AioPowerSession(g.controller)
    for feature in ('gps','lora','sdr','usb'):g._aio_power.toggle(feature,True)
    g.events.clear()
    g._shutting_down=g._cleanup_done=g._cleanup_running=g._exit_requested=False
    g._cleanup_errors=[];g._exit_handlers={}
    g._flash_running=False
    g._wardrive_selected={'wardriving','bt_scanning'}
    g._pending_cmd='scan_bt'
    g._cancel_wardrive_plan=lambda:g._wardrive_selected.clear()
    def action(label,retval=None):
        return Mock(side_effect=lambda *args, **kwargs:g.events.append(label) or retval)
    g.serial=SimpleNamespace(is_open=True,request_stop_for_shutdown=action('esp-stop',True),close=action('serial-close'))
    def final_serial():
        g.events.append('serial-drain')
        g._handle_esp_operation_state('all operations stopped.')
    g._poll_serial=Mock(side_effect=final_serial)
    g.gps=SimpleNamespace(close=action('gps-close'))
    for name in ('_mt','_lora','_sdr','_wd','_subghz','_dragon_drain','_mitm','_blueducky','_race'):
        setattr(g,name,SimpleNamespace(running=True,stop=action(name+'-stop',True)))
    g._subghz.shutdown=g._subghz.stop
    g._flipper=SimpleNamespace(close=action('flipper-close',True))
    g._watch=SimpleNamespace(disconnect=action('watch-close'))
    g._prox=SimpleNamespace(shutdown=action('prox-close'))
    g._plugins=[SimpleNamespace(on_unload=action('plugin-unload'))]
    g._mt_event_queue=Queue();g._mc_event_queue=Queue()
    g._poll_lora=action('mesh-drain')
    g._poll_sdr=action('sdr-drain')
    g._save_xp_if_dirty=action('xp-save')
    g.loot=SimpleNamespace(close=action('loot-close'))
    return g


class ShutdownTests(unittest.TestCase):
    def test_stop_drain_save_then_power_usb_last_and_idempotent(self):
        g=game();g._cleanup();g._cleanup()
        for entry in ('esp-stop','serial-close','mesh-drain','xp-save','loot-close'):
            self.assertEqual(g.events.count(entry),1)
        self.assertLess(g.events.index('_mt-stop'),g.events.index('mesh-drain'))
        self.assertLess(g.events.index('mesh-drain'),g.events.index('xp-save'))
        self.assertLess(g.events.index('xp-save'),g.events.index('loot-close'))
        self.assertLess(g.events.index('loot-close'),g.events.index(('power','lora',False)))
        self.assertEqual(g.events[-1],('power','usb',False))
        self.assertFalse(g._wardrive_selected);self.assertIsNone(g._pending_cmd)
        self.assertEqual(g._cleanup_errors,[])

    def test_serial_close_failure_still_saves_and_stops_other_devices(self):
        g=game();g.serial.close.side_effect=OSError('port failed')
        with self.assertLogs(level='WARNING'):g._cleanup()
        self.assertIn('xp-save',g.events);self.assertIn('loot-close',g.events)
        self.assertFalse(g.controller.states['lora'])
        self.assertTrue(g.controller.states['usb'])
        self.assertIn('serial close',g._cleanup_errors)

    def test_failed_radio_stop_keeps_lora_power_but_saves_and_releases_usb(self):
        g=game();g._mt.stop.return_value=False;g._mt.stop.side_effect=None
        with self.assertLogs(level='WARNING'):g._cleanup()
        self.assertTrue(g.controller.states['lora'])
        self.assertFalse(g.controller.states['usb'])
        self.assertIn('loot-close',g.events)

    def test_failed_sdr_join_keeps_sdr_and_usb_power(self):
        g=game();g._sdr.stop.side_effect=None;g._sdr.stop.return_value=False
        with self.assertLogs(level='WARNING'):g._cleanup()
        self.assertTrue(g.controller.states['sdr']);self.assertTrue(g.controller.states['usb'])
        self.assertFalse(g.controller.states['gps'])

    def test_one_plugin_failure_does_not_skip_later_plugins_or_data(self):
        g=game();g._plugins.insert(0,SimpleNamespace(on_unload=Mock(side_effect=RuntimeError('bad plugin'))))
        with self.assertLogs(level='WARNING'):g._cleanup()
        self.assertIn('plugin-unload',g.events);self.assertIn('loot-close',g.events)
        self.assertFalse(g.controller.states['usb'])

    def test_partial_constructor_only_usb_still_releases_it(self):
        g=GameHarness.__new__(GameHarness);g._cleanup_errors=[]
        controller=PowerController([]);g._aio_power=AioPowerSession(controller)
        g._aio_power.toggle('usb',True);g._cleanup()
        self.assertFalse(controller.states['usb'])

    def test_last_stop_events_are_persisted_before_loot_close(self):
        g=game();records=[]
        g._mt.stop.side_effect=lambda:g._mt_event_queue.put(('node','!1234abcd')) or True
        g._poll_lora.side_effect=lambda:records.append(g._mt_event_queue.get_nowait())
        g.loot.close.side_effect=lambda:self.assertEqual(records,[('node','!1234abcd')])
        g._cleanup()

    def test_shutdown_rejects_pending_commands_reconnect_and_power_on(self):
        g=game();g._cleanup()
        self.assertFalse(g._send('scan_networks'))
        self.assertFalse(g._try_reconnect_esp32())
        with self.assertLogs(level='WARNING'):self.assertFalse(g._aio_power.toggle('usb',True))

    def test_native_atexit_callback_is_registered_before_initialization(self):
        seen=[]
        def initialize(g,*args):
            seen.append('initialize')
            self.assertTrue(register.called)
            # Model Pyxel invoking Python atexit while inside native run.
            register.call_args.args[0]()
        with patch.object(GameHarness,'_initialize',initialize,create=True), \
             patch.object(atexit,'register') as register, patch.object(atexit,'unregister') as unregister:
            g=GameHarness()
        unregister.assert_called_once();self.assertTrue(g._cleanup_done)

    def test_init_exception_unwinds_and_releases_acquired_usb(self):
        controller=PowerController([])
        def initialize(g,*args):
            g._aio_power.toggle('usb',True)
            raise RuntimeError('startup failed')
        with patch.dict(ENV,AioPowerSession=lambda:AioPowerSession(controller)), \
             patch.object(GameHarness,'_initialize',initialize,create=True):
            with self.assertRaisesRegex(RuntimeError,'startup failed'):GameHarness()
        self.assertFalse(controller.states['usb'])

    def test_callback_exception_cleans_up_before_original_error(self):
        for method,callback in (('_guard_update','update'),('_guard_draw','draw')):
            with self.subTest(method=method):
                g=game();setattr(g,callback,Mock(side_effect=RuntimeError('callback failed')))
                with self.assertRaisesRegex(RuntimeError,'callback failed'):getattr(g,method)()
                self.assertTrue(g._cleanup_done);self.assertFalse(g.controller.states['usb'])

    def test_exit_signal_request_runs_cleanup_before_quit(self):
        g=game();g._exit_requested=True;g.update=Mock();ENV['pyxel'].quit.reset_mock()
        g._guard_update();g.update.assert_not_called();ENV['pyxel'].quit.assert_called_once()
        self.assertTrue(g._cleanup_done)

    def test_flash_guard_keeps_power_and_does_not_stop_its_serial(self):
        g=game();g._flash_running=True
        with self.assertLogs(level='WARNING'):g._cleanup()
        self.assertTrue(g.controller.states['usb']);g.serial.request_stop_for_shutdown.assert_not_called()
        g.serial.close.assert_not_called()
        self.assertIn('ESP32 flash still active', g._cleanup_errors)

    def test_initial_stop_notice_is_not_final_ack(self):
        g=game();calls=[]
        def final_serial():
            if not calls:
                g._handle_esp_operation_state('stop command received - stopping all operations...')
                self.assertFalse(g._esp_stop_ack)
                calls.append('notice')
            else:
                g._handle_esp_operation_state('all operations stopped.')
                calls.append('final')
        g._poll_serial.side_effect=final_serial
        g._cleanup()
        self.assertEqual(calls,['notice','final']);self.assertTrue(g._esp_stop_ack)
        self.assertNotIn('ESP32 stop acknowledgement',g._cleanup_errors)

    def test_missing_final_ack_is_reported_before_owned_usb_is_disabled(self):
        g=game();clock=iter([0.0,0.0,0.1,6.0])
        g._poll_serial.side_effect=lambda:None
        fake_clock=SimpleNamespace(monotonic=lambda:next(clock),sleep=lambda seconds:None)
        with patch.dict(ENV,time=fake_clock),self.assertLogs(level='WARNING'):
            g._cleanup()
        self.assertIn('ESP32 stop acknowledgement',g._cleanup_errors)
        self.assertIn('loot-close',g.events);self.assertFalse(g.controller.states['usb'])

    def test_unused_attack_is_not_stopped_or_allowed_to_start_services(self):
        g=game();g._race=race_attack.RACEAttack()
        with patch.object(race_attack.os,'system') as system:
            g._cleanup()
        system.assert_not_called()

    def test_flash_worker_clears_busy_flag_even_on_error(self):
        g=game();g._flash_running=True;g._flash_do=Mock(side_effect=RuntimeError('flash failed'))
        g._term_add=Mock();g.msg=Mock()
        g._run_flash('test')
        self.assertTrue(any('flash failed' in str(c) for c in g._term_add.call_args_list))
        self.assertFalse(g._flash_running)
        self.assertFalse(g._serial_busy)


class AuxiliaryCloseTests(unittest.TestCase):
    def test_monster_stops_firmware_then_closes_and_joins_own_reader(self):
        m=SubGhzManager();events=[]
        m.connected=True
        m._ser=SimpleNamespace(write=lambda data:events.append(data) or len(data),
            cancel_read=lambda:events.append('cancel'),close=lambda:events.append('close'))
        m._reader=SimpleNamespace(join=lambda **kw:events.append('join'),is_alive=lambda:False)
        self.assertTrue(m.stop());self.assertEqual(events,[b'subghz_stop\r\n','cancel','close','join'])
        self.assertFalse(m.connected);self.assertIsNone(m._ser)

    def test_monster_stuck_reader_does_not_claim_release(self):
        m=SubGhzManager();m._reader=SimpleNamespace(join=Mock(),is_alive=lambda:True)
        self.assertFalse(m.stop())

    def test_flipper_stops_its_rx_before_serial_close(self):
        m=FlipperManager();events=[];m._subghz_active=True
        m._ser=SimpleNamespace(write=lambda data:events.append(data),
            cancel_read=lambda:events.append('cancel'),close=lambda:events.append('close'))
        m._rx_thread=SimpleNamespace(join=lambda **kw:events.append('join'),is_alive=lambda:False)
        self.assertTrue(m.close());self.assertLess(events.index(b'\r\n'),events.index('close'))
        self.assertEqual(events[-1],'join');self.assertIsNone(m._ser)


if __name__=='__main__':unittest.main()
