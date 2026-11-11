"""Actual flash methods with private ZIP files and fake processes/hardware."""
import ast
import builtins
import copy
import io
import json
import logging
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from urllib.request import Request
import zipfile

ROOT=Path(__file__).resolve().parents[1]
SOURCE=ROOT/'watchdogs/app.py'
TREE=ast.parse(SOURCE.read_text(encoding='utf8'))
GAME=next(n for n in TREE.body if isinstance(n,ast.ClassDef) and n.name=='WatchDogsGame')
METHODS={'_run_flash','_start_flash_esp32','_update_flash_screen','_poll_serial',
         '_try_reconnect_esp32','_send','_handle_serial_line','_toggle_usb','_check_fw_update'}
METHODS.update(n.name for n in GAME.body if isinstance(n,ast.FunctionDef) and n.name.startswith('_flash'))
TABLES={}
for node in ast.parse((ROOT/'watchdogs/config.py').read_text(encoding='utf8')).body:
    if isinstance(node,ast.Assign):
        for target in node.targets:
            if isinstance(target,ast.Name) and target.id in {'FLASH_BOARDS','FIRMWARE_RELEASE_URL'}:
                TABLES[target.id]=ast.literal_eval(node.value)
GAME_PYTHON='/game/.venv/bin/python3'


class FakeProcess:
    def __init__(self,owner):self.owner,self.returncode=owner,None
    def poll(self):
        self.owner.events.append(('process-poll',self.returncode));return self.returncode
    def wait(self,timeout=None):
        if self.returncode is None:raise subprocess.TimeoutExpired('fake esptool',timeout)
        return self.returncode


class FakeSerial:
    def __init__(self,owner,port,opened=False):self.owner,self.device,self.is_open=owner,port,opened
    def setup(self):self.owner.events.append(('serial-open',self.device));self.is_open=True
    def close(self):
        self.owner.events.append(('serial-close',))
        if self.owner.close_error:raise self.owner.close_error
        self.is_open=False
    def send_command(self,command):
        self.owner.events.append(('serial-command',command));return self.owner.send_ok
    def read_available(self):self.owner.events.append(('serial-read',));return []


class FlashEnvironment:
    """Run production methods, never a physical device or an external request."""
    def __init__(self,probe_results=None,aio=True):
        from watchdogs import esp32_flash as helper
        self.helper=helper
        self.temporary=tempfile.TemporaryDirectory(prefix='wdg-flash-test-')
        self.root=Path(self.temporary.name)
        self.events=[];self.probe_calls=[];self.flash_commands=[];self.messages=[];self.output=[];self.threads=[]
        self.probe_results=list(probe_results or [])
        self.usb_on=self.send_ok=True
        self.power_error=self.close_error=self.flash_error=None
        self.power_result=True;self.flash_code=0;self.during_flash=None;self.process=None
        self.now=100.;self.detect_port='/dev/ttyACM1';self.port_exists={}
        self.firmware_entries=None;self.reverse_assets=False;self.download_error=None;self.during_download=None;self.buttons=set()
        owner=self
        class FakeThread:
            def __init__(self,target,args=(),kwargs=None,daemon=False,**unused):
                self.target,self.args,self.kwargs=target,args,kwargs or {};owner.threads.append(self)
            def start(self):owner.events.append(('thread-start',))
        self.fake_time=SimpleNamespace(time=lambda:self.now,monotonic=lambda:self.now,sleep=self.sleep)
        self.px=SimpleNamespace(frame_count=90,KEY_UP=1,KEY_DOWN=2,KEY_RETURN=3,KEY_ESCAPE=4)
        self.px.btnp=lambda key:key in self.buttons
        fake_threading=SimpleNamespace(Thread=FakeThread,Lock=threading.Lock,RLock=threading.RLock,
                                       get_ident=threading.get_ident)
        def path(value,*rest):
            candidate=Path(value,*rest)
            if str(candidate).replace('\\','/').startswith('/dev/'):
                return SimpleNamespace(exists=lambda:self.port_exists.get(str(candidate).replace('\\','/'),True))
            return candidate
        def check_tool(executable,**kwargs):
            kwargs['run']=self.run;return helper.check_esptool(executable,**kwargs)
        self.helpers=SimpleNamespace(**{key:getattr(helper,key) for key in dir(helper) if not key.startswith('__')})
        self.helpers.check_esptool=check_tool;self.helpers.run_esptool=self.fake_flash_process
        modules={'config':SimpleNamespace(**TABLES),'esp32_flash':self.helpers,
                 'json':json,'zipfile':zipfile,'subprocess':SimpleNamespace(run=self.run,Popen=self.forbidden_popen,
                     PIPE=subprocess.PIPE,STDOUT=subprocess.STDOUT,DEVNULL=subprocess.DEVNULL,TimeoutExpired=subprocess.TimeoutExpired),
                 'tempfile':tempfile,'shutil':shutil,'threading':fake_threading,'time':self.fake_time,'re':re,'queue':queue,
                 'os':os,'logging':logging,'urllib.request':SimpleNamespace(Request=Request,urlopen=self.urlopen),
                 'pathlib':SimpleNamespace(Path=path)}
        def fake_import(name,globals=None,locals=None,fromlist=(),level=0):
            if name not in modules or (level and name not in ('config','esp32_flash')):
                raise AssertionError('Unexpected flash method import: '+name)
            return modules[name]
        env=dict(__name__='flash_harness',__package__='flash_harness',
                 __builtins__=dict(vars(builtins),__import__=fake_import),sys=SimpleNamespace(executable=GAME_PYTHON),
                 time=self.fake_time,threading=fake_threading,pyxel=self.px,re=re,logging=logging,queue=queue,
                 detect_esp32_port=self.detect,SerialManager=lambda port:FakeSerial(self,port),
                 C_ERROR=1,C_DIM=2,C_WARNING=3,C_SUCCESS=4,C_TEXT=5)
        node=ast.ClassDef(name='GameHarness',bases=[],keywords=[],decorator_list=[],
                          body=[copy.deepcopy(n) for n in GAME.body if isinstance(n,ast.FunctionDef) and n.name in METHODS])
        exec(compile(ast.fix_missing_locations(ast.Module(body=[node],type_ignores=[])),str(SOURCE),'exec'),env)
        self.namespace=env;g=self.game=env['GameHarness']()
        g._app_dir=str(self.root);g._boot_serial_port='/dev/ttyACM1';g._aio_available=aio
        g._aio_power=SimpleNamespace(toggle=self.toggle);g._esp32=True;g.state=SimpleNamespace(connected=True)
        g.serial=FakeSerial(self,g._boot_serial_port,True)
        g._term_add=lambda text,**kw:self.output.append(text);g.msg=lambda text,color:self.messages.append((text,color))
        g._flash_running=True;g._serial_busy=False;g._esp_serial_lock=threading.RLock();g._flash_job_lock=threading.Lock()
        g._flash_process=None;g._flash_workspace=None;g._flash_device_acquired=False;g._flash_write_complete=False
        g._flash_fw_pending=False;g._flash_fw_deadline=0.;g._fw_version='1.6.5';g._fw_update_available=True;g._fw_remote_version='1.7.0'
        g._shutting_down=False;g._pending_cmd_frame=0;g._esp_stop_waiting=g._esp_stop_ack=False;g._usb_enabled=True
        g._clear_esp_scan_state=lambda **kw:None;g._handle_esp_operation_state=lambda line:None;g._check_fw_update=lambda:None
    def close(self):self.temporary.cleanup()
    def sleep(self,seconds):self.events.append(('sleep',seconds));self.now+=seconds
    def detect(self):self.events.append(('detect-port',));return self.detect_port
    def run(self,command,**kwargs):
        self.events.append(('probe',tuple(command)));self.probe_calls.append((command,kwargs))
        outcome=self.probe_results.pop(0) if self.probe_results else 0
        if isinstance(outcome,Exception):raise outcome
        if callable(outcome):outcome=outcome(command)
        if isinstance(outcome,dict):return SimpleNamespace(**outcome)
        return SimpleNamespace(returncode=outcome,stdout='esptool v5.4.0\n5.4.0\n' if outcome==0 else '',stderr='No module named esptool' if outcome else '')
    def forbidden_popen(self,*args,**kwargs):raise AssertionError('An actual flash subprocess is forbidden in app tests')
    def fake_flash_process(self,command,on_line,*,on_process=None,**kwargs):
        self.events.append(('flash',tuple(command)));self.flash_commands.append((command,kwargs));self.process=FakeProcess(self)
        if on_process:on_process(self.process)
        if self.during_flash:self.during_flash(self.game)
        on_line('Fake write progress');self.process.returncode=self.flash_code
        if self.flash_error:raise self.flash_error
        if self.flash_code:raise self.helper.FlashError('esptool exited with code '+str(self.flash_code))
    def urlopen(self,request,**kwargs):
        url=getattr(request,'full_url',getattr(request,'url',str(request)));self.events.append(('download',url))
        if self.download_error:raise self.download_error
        if self.during_download:self.during_download(url)
        if url==TABLES['FIRMWARE_RELEASE_URL']:
            assets=[{'name':'projectZerobyLOCOSP-1.7.0.zip','browser_download_url':'https://example.invalid/wroom.zip'},
                    {'name':'projectZerobyLOCOSP-xiao-1.7.0.zip','browser_download_url':'https://example.invalid/xiao.zip'}]
            return io.BytesIO(json.dumps({'tag_name':'v1.7.0','assets':assets[::-1] if self.reverse_assets else assets}).encode())
        board='xiao' if 'xiao' in url else 'wroom'
        entries=self.firmware_entries
        if entries is None:entries={name:b'\xe9FAKE TEST IMAGE' for name in TABLES['FLASH_BOARDS'][board]['offsets']}
        output=io.BytesIO()
        with zipfile.ZipFile(output,'w') as archive:
            for name,content in entries.items():archive.writestr(name,content)
        return io.BytesIO(output.getvalue())
    def toggle(self,feature,on):
        self.events.append(('power',feature,on))
        if self.power_error:raise self.power_error
        if self.power_result:self.usb_on=on
        return self.power_result
    def flash(self,board='xiao'):self.game._run_flash(board)


class FlashPreflightTests(unittest.TestCase):
    def environment(self,*args,**kwargs):
        env=FlashEnvironment(*args,**kwargs);self.addCleanup(env.close);return env
    def assert_no_hardware_changes(self,env):
        self.assertEqual([event[0] for event in env.events],['probe'])
        self.assertTrue(env.game.serial.is_open);self.assertTrue(env.game._esp32);self.assertTrue(env.game.state.connected)
        self.assertTrue(env.usb_on);self.assertFalse(env.game._flash_running)
        self.assertEqual(env.game._fw_version,'1.6.5');self.assertEqual(env.flash_commands,[])
    def test_available_module_uses_game_interpreter_before_any_work(self):
        env=self.environment();env.flash();command,kwargs=env.probe_calls[0]
        self.assertEqual(command,[GAME_PYTHON,'-m','esptool','version']);self.assertEqual(kwargs['timeout'],10)
        self.assertFalse(kwargs['check']);self.assertEqual(env.events[0][0],'probe')
        self.assertTrue(env.flash_commands);self.assertFalse(env.game._flash_running)
    def test_missing_module_returns_actionable_error_before_any_changes(self):
        env=self.environment([1]);env.flash();self.assert_no_hardware_changes(env)
        text='\n'.join(env.output);self.assertIn('esptool',text);self.assertIn(GAME_PYTHON,text);self.assertTrue(env.messages)
    def test_system_python_install_does_not_pass_game_venv_check(self):
        env=self.environment([lambda command:0 if command[0]=='/usr/bin/python3' else 1]);env.flash()
        self.assert_no_hardware_changes(env);self.assertEqual(len(env.probe_calls),1)
        self.assertEqual(env.probe_calls[0][0][0],GAME_PYTHON)
    def test_broken_environment_and_timeout_fail_safely(self):
        for failure in [2,FileNotFoundError('no interpreter'),PermissionError('not executable'),ValueError('invalid executable'),
                        TypeError('invalid executable type'),subprocess.TimeoutExpired([GAME_PYTHON],10)]:
            with self.subTest(failure=repr(failure)):
                env=self.environment([failure]);env.flash();self.assert_no_hardware_changes(env);self.assertTrue(env.output)
    def test_missing_dependency_keeps_serial_and_connection_state(self):
        for board in ('xiao','wroom'):
            with self.subTest(board=board):
                env=self.environment([1]);serial=env.game.serial;env.flash(board)
                self.assert_no_hardware_changes(env);self.assertIs(env.game.serial,serial)
    def test_missing_dependency_never_cycles_usb_with_or_without_aio(self):
        for board in ('xiao','wroom'):
            for aio in (False,True):
                with self.subTest(board=board,aio=aio):
                    env=self.environment([1],aio=aio);env.flash(board);self.assert_no_hardware_changes(env)
    def test_retry_rechecks_dependency_and_can_continue_after_install(self):
        env=self.environment([1,0]);env.flash();self.assert_no_hardware_changes(env)
        env.game._flash_running=True;env.flash();self.assertEqual(len(env.probe_calls),2)
        self.assertEqual(len(env.flash_commands),1);self.assertFalse(env.game._flash_running)
        self.assertIn(('serial-command','version'),env.events)
    def test_existing_board_commands_and_power_sequences_are_unchanged(self):
        for board in ('xiao','wroom'):
            for aio in (False,True):
                with self.subTest(board=board,aio=aio):
                    env=self.environment(aio=aio);env.flash(board);command=env.flash_commands[0][0];xiao_aio=board=='xiao' and aio
                    fixed=[GAME_PYTHON,'-m','esptool','-p','/dev/ttyACM1','-b','460800','--before',
                           'usb-reset' if board=='xiao' else 'default-reset','--after','no-reset' if xiao_aio else 'hard-reset',
                           '--chip','esp32c5','--no-stub','write-flash','--flash-mode','dio','--flash-freq','80m','--flash-size','detect']
                    self.assertEqual(command[:len(fixed)],fixed);pairs=command[len(fixed):]
                    self.assertEqual(pairs[::2],['0x2000','0x8000','0x20000'])
                    self.assertEqual([Path(p).name for p in pairs[1::2]],list(TABLES['FLASH_BOARDS'][board]['offsets']))
                    self.assertEqual([event for event in env.events if event[0]=='power'],
                                     [('power','usb',False),('power','usb',True)]*(2 if xiao_aio else 0))
    def test_unsupported_esptool_version_keeps_existing_connection(self):
        env=self.environment([dict(returncode=0,stdout='esptool v4.9.0\n4.9.0',stderr='')]);env.flash()
        self.assert_no_hardware_changes(env);self.assertIn('5.0.0','\n'.join(env.output))
    def test_permission_diagnostic_distinguishes_interpreter_access_failure(self):
        env=self.environment([PermissionError('execution permission denied')]);env.flash()
        self.assert_no_hardware_changes(env);self.assertIn('permission','\n'.join(env.output).lower())

if __name__=='__main__':unittest.main()
