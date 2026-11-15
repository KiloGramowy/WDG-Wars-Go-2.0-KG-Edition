"""Production follows the own KG channel; writes require manual entry."""
import ast
import json
import os
import subprocess
import sys
from types import SimpleNamespace
import unittest
from watchdogs import config, game_updates, kg_firmware
from watchdogs.esp32_flash import FlashError
from test_flash_preflight import FlashEnvironment, ROOT, TREE


class FirmwarePolicyTests(unittest.TestCase):
    def environment(self):
        env=FlashEnvironment(allow_upstream=False);self.addCleanup(env.close)
        g=env.game;del g._check_fw_update
        g._flash_running=False;g._flash_sel=0;g.menu_open=True
        g._fw_remote_version='';g._fw_update_available=False;g._kg_fw_package=None;g._kg_fw_error=''
        return env

    def assert_board_untouched(self,env,*,threads=0,version='1.6.5'):
        self.assertEqual(env.events,[('thread-start',)]*threads)
        self.assertEqual(env.probe_calls,[]);self.assertEqual(env.flash_commands,[])
        self.assertEqual(len(env.threads),threads)
        self.assertTrue(env.game.serial.is_open);self.assertTrue(env.game._esp32)
        self.assertTrue(env.game.state.connected);self.assertTrue(env.usb_on)
        self.assertEqual(env.game._fw_version,version)
        self.assertFalse((env.root/'firmware_cache').exists())

    def test_production_uses_one_own_source_without_environment_override(self):
        self.assertIs(config.UPSTREAM_FIRMWARE_ENABLED,False)
        self.assertEqual(config.FIRMWARE_RELEASE_URL,kg_firmware.LATEST_URL)
        tree=ast.parse((ROOT/'watchdogs/config.py').read_text(encoding='utf-8'))
        definitions=[n for n in tree.body if isinstance(n,ast.Assign) and any(
            isinstance(t,ast.Name) and t.id=='FIRMWARE_RELEASE_URL' for t in n.targets)]
        self.assertEqual(len(definitions),1)
        environment=dict(os.environ,UPSTREAM_FIRMWARE_ENABLED='1',FIRMWARE_RELEASE_URL='https://example.invalid/unapproved')
        result=subprocess.run([sys.executable,'-B','-c',
            'from watchdogs import config,kg_firmware; assert config.UPSTREAM_FIRMWARE_ENABLED is False; '
            'assert config.FIRMWARE_RELEASE_URL==kg_firmware.LATEST_URL'],cwd=ROOT,env=environment,
            capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_menu_preserves_existing_kg_firmware_action(self):
        rows=[n for n in ast.walk(TREE) if isinstance(n,ast.Tuple) and len(n.elts)==5
            and isinstance(n.elts[3],ast.Constant) and n.elts[3].value=='_flash_esp']
        self.assertEqual(len(rows),1)
        self.assertEqual(ast.literal_eval(rows[0].elts[1]),'Firmware KG/SOLO')
        self.assertEqual(ast.literal_eval(rows[0].elts[2]),'_flash_esp32')

    def test_default_checker_reads_only_kg_metadata_and_announces_kg_version(self):
        env=self.environment();env.game._check_fw_update()
        self.assertEqual(env.kg_calls,[('metadata',)])
        self.assertEqual(env.game._fw_remote_version,'1.7.0');self.assertTrue(env.game._fw_update_available)
        self.assertIs(env.game._kg_fw_package,env.kg_package)
        self.assertIn('KG',env.messages[0][0]);self.assert_board_untouched(env)

    def test_missing_kg_package_clears_stock_notice_without_fallback(self):
        env=self.environment();g=env.game;g._fw_remote_version='1.7.3';g._fw_update_available=True
        env.kg_fetch_error=kg_firmware.KGFirmwareError('No compatible KG/SOLO package')
        g._check_fw_update()
        self.assertEqual(env.kg_calls,[('metadata',)]);self.assertFalse(g._fw_update_available)
        self.assertEqual(g._fw_remote_version,'');self.assertIn('No compatible',g._kg_fw_error)
        self.assertEqual(env.messages,[]);self.assert_board_untouched(env)

    def test_equal_reported_version_is_not_new_or_a_hash_certificate(self):
        env=self.environment();env.game._fw_version='1.7.0';env.game._check_fw_update()
        self.assertFalse(env.game._fw_update_available);self.assertEqual(env.game._fw_remote_version,'1.7.0')
        self.assertEqual(env.messages,[]);self.assert_board_untouched(env,version='1.7.0')

    def test_serial_version_starts_one_metadata_check_without_installing(self):
        env=self.environment();g=env.game;g._fw_version='';g.loot=None;handled=[]
        g._solo=SimpleNamespace(confirmed_wifi=False,confirmed_ble=False,
            handle_line=lambda line:handled.append(line) or True)
        g._handle_serial_line('JanOS version: 1.7.0');g._handle_serial_line('JanOS version: 1.7.0')
        self.assertEqual(handled,['JanOS version: 1.7.0']*2)
        self.assertEqual(env.threads[0].target.__name__,'_check_fw_update')
        self.assertTrue(env.threads[0].daemon);self.assertEqual(env.kg_calls,[])
        self.assert_board_untouched(env,threads=1,version='1.7.0')

    def test_open_and_refresh_only_schedule_metadata_enter_waits_for_it(self):
        env=self.environment();g=env.game;g._start_flash_esp32()
        self.assertTrue(g._flash_screen);self.assertFalse(g.menu_open);self.assertEqual(g._flash_boards,['xiao'])
        for key in (env.px.KEY_DOWN,env.px.KEY_UP,env.px.KEY_RETURN):
            env.buttons={key};g._update_flash_screen()
        self.assertFalse(g._flash_running);self.assertEqual(g._flash_sel,0)
        g._kg_fw_checking=True;env.buttons={env.px.KEY_R};g._update_flash_screen()
        self.assertEqual(len(env.threads),1)
        g._kg_fw_checking=False;g._update_flash_screen()
        self.assertTrue(all(t.target.__name__=='_check_fw_update' for t in env.threads))
        self.assert_board_untouched(env,threads=2)

    def test_busy_worker_cannot_release_existing_operation_guard(self):
        env=self.environment();g=env.game;g._flash_running=True;g._flash_job_lock.acquire()
        try:
            g._run_flash('xiao');self.assertTrue(g._flash_job_lock.locked());self.assertTrue(g._flash_running)
        finally:g._flash_job_lock.release()
        self.assertEqual(env.kg_calls,[]);self.assert_board_untouched(env)

    def test_unsupported_board_refused_before_tool_cache_transport_download(self):
        env=self.environment()
        with self.assertRaises(FlashError):env.game._flash_do('wroom')
        self.assertEqual(env.kg_calls,[]);self.assert_board_untouched(env)

    def test_renderer_shows_current_latest_source_target_and_manual_controls(self):
        env=self.environment();env.game._kg_fw_package=env.kg_package;env.game._draw_flash_screen()
        words=' '.join(args[2] for name,args in env.draw_calls if name=='text')
        for text in ('KG/SOLO FIRMWARE UPDATE','KG 1.6.5 (JanOS)','Latest KG: 1.7.0','Source: KiloGramowy',
                     'XIAO ESP32-C5 rev 1.0','8 MiB','application only','R check KG','ENTER install KG','ESC close'):
            self.assertIn(text,words)
        for text in ('disabled','No automatic firmware channel','Select Board','1.7.3','hash verified'):
            self.assertNotIn(text,words)
        for name,args in env.draw_calls:
            if name=='text':
                x,y,text,_=args;self.assertGreaterEqual(x,0);self.assertGreaterEqual(y,0)
                self.assertLessEqual(x+len(text)*5,640);self.assertLessEqual(y+8,360)
        self.assert_board_untouched(env)

    def test_escape_closes_window_without_starting_job(self):
        env=self.environment();g=env.game;g._kg_fw_package=env.kg_package;g._start_flash_esp32()
        env.buttons={env.px.KEY_ESCAPE};g._update_flash_screen()
        self.assertFalse(g._flash_screen);self.assertEqual(g._esc_consumed_frame,env.px.frame_count)
        self.assert_board_untouched(env)

    def test_game_updater_and_separate_piglet_keep_existing_identity(self):
        self.assertEqual(game_updates.REPOSITORY_ID,1442123109);self.assertEqual(game_updates.CHANNEL,'stable')
        piglet=json.loads((ROOT/'firmware/piglet-collection/manifest.json').read_text())
        self.assertEqual(piglet['target']['app_offset'],'0x10000')
        self.assertEqual(piglet['target']['board'],'Seeed XIAO ESP32-C5')
        self.assertEqual(piglet['provenance']['repository'],'KiloGramowy/Piglet-KG-Edition')


if __name__=='__main__':unittest.main()
