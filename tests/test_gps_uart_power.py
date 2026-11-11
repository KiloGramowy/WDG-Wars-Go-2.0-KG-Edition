"""AIO UART startup/toggle must not probe the ESP32; fake serial/GPIO only."""
import ast
import copy
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from watchdogs import gps_manager as module


SOURCE = Path(__file__).resolve().parents[1] / "watchdogs" / "app.py"
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"))
APP = next(node for node in TREE.body if isinstance(node, ast.ClassDef)
           and node.name == "WatchDogsGame")
INITIALIZE = next(node for node in APP.body if isinstance(node, ast.FunctionDef)
                  and node.name == "_initialize")
OPEN = next(index for index, node in enumerate(INITIALIZE.body)
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute)
            and node.value.func.attr == "_setup_gps_connection")
STATUS = next(node for node in INITIALIZE.body if isinstance(node, ast.If)
              and any(isinstance(child, ast.Assign) and any(
                  isinstance(target, ast.Attribute) and target.attr == "_gps_enabled"
                  for target in child.targets) for child in node.body))
BOOT = ast.FunctionDef(name="boot_gps", args=ast.arguments(posonlyargs=[],
    args=[ast.arg(arg="self")], kwonlyargs=[], kw_defaults=[], defaults=[]),
    decorator_list=[], body=[copy.deepcopy(INITIALIZE.body[OPEN]),
                            copy.deepcopy(INITIALIZE.body[OPEN + 1]), copy.deepcopy(STATUS)])
CLOCK = SimpleNamespace(now=100.0)
AIO = SimpleNamespace(get_status=Mock(return_value={"gps": True}))
ENV = dict(AioManager=AIO, time=SimpleNamespace(monotonic=lambda: CLOCK.now),
           C_ERROR=8, C_WARNING=9, C_SUCCESS=11)
HARNESS = ast.ClassDef(name="GpsGame", bases=[], keywords=[], decorator_list=[],
    body=[BOOT] + [copy.deepcopy(node) for node in APP.body
        if isinstance(node, ast.FunctionDef)
        and node.name in ("_setup_gps_connection", "_toggle_gps", "_poll_gps")])
exec(compile(ast.fix_missing_locations(ast.Module(body=[HARNESS], type_ignores=[])),
             str(SOURCE), "exec"), ENV)
GpsGame = ENV["GpsGame"]


class GpsUartPowerTests(unittest.TestCase):
    def setUp(self):
        CLOCK.now = 100.0
        AIO.get_status.return_value = {"gps": True}
        AIO.get_status.reset_mock()
        self.g = GpsGame()
        self.g._aio_available = True
        self.g._aio_power = SimpleNamespace(toggle=Mock(return_value=True), claim=Mock())
        self.g.gps = module.GpsManager(device="/dev/ttyAMA0")
        self.addCleanup(self.g.gps.close)
        self.g._gps_enabled = False
        self.g.gps_fix = False
        self.g.msg, self.g._term_add = Mock(), Mock()
        self.g._poll_radio_profiles = Mock()
        self.g.player_lat, self.g.player_lon = 12.5, 34.5
        self.conn = SimpleNamespace(in_waiting=0, reset_input_buffer=Mock(), close=Mock())
        self.serial = Mock(return_value=self.conn)
        for name, value in (("exists", True), ("access", True)):
            patched = patch.object(module.os.path, name, return_value=value) if name == "exists" else \
                patch.object(module.os, name, return_value=value)
            patched.start()
            self.addCleanup(patched.stop)
        patched = patch.object(module.serial, "Serial", self.serial)
        patched.start()
        self.addCleanup(patched.stop)

    def prohibit_setup_probe(self):
        self.g.gps.setup = Mock(side_effect=AssertionError("USB GPS probing must not run"))

    def test_boot_opens_only_configured_uart_even_before_nmea_arrives(self):
        self.prohibit_setup_probe()
        self.g.boot_gps()
        self.g.gps.setup.assert_not_called()
        self.serial.assert_called_once_with(port="/dev/ttyAMA0", baudrate=9600, timeout=0)
        self.assertTrue(self.g._gps_enabled)
        self.assertTrue(self.g.gps.available)
        self.assertFalse(self.g.gps.fix.valid)
        self.g._poll_gps()
        self.assertFalse(self.g.gps_fix)
        self.assertEqual(self.serial.call_count, 1)

    def test_toggle_on_keeps_silent_uart_open_and_does_not_claim_a_fix(self):
        self.prohibit_setup_probe()
        self.g._toggle_gps()
        self.g._aio_power.toggle.assert_called_once_with("gps", True)
        self.assertTrue(self.g._gps_enabled)
        self.assertTrue(self.g.gps.available)
        self.assertFalse(self.g.gps_fix)
        self.g.gps.setup.assert_not_called()
        self.serial.assert_called_once_with(port="/dev/ttyAMA0", baudrate=9600, timeout=0)

    def test_failed_open_keeps_power_enabled_and_existing_retry_recovers(self):
        self.prohibit_setup_probe()
        self.serial.side_effect = [OSError("synthetic UART not ready"), self.conn]
        self.g._toggle_gps()
        self.assertTrue(self.g._gps_enabled)
        self.assertFalse(self.g.gps.available)
        self.g._poll_gps()
        self.assertTrue(self.g._gps_enabled)
        self.assertTrue(self.g.gps.available)
        self.assertFalse(self.g.gps_fix)
        self.assertEqual(self.serial.call_count, 2)
        self.g.gps.setup.assert_not_called()
        self.assertIn("waiting for UART", self.g.msg.call_args_list[0].args[0])

    def test_failed_boot_open_remains_pending_and_uart_retries_are_throttled(self):
        self.prohibit_setup_probe()
        self.serial.side_effect = OSError("synthetic UART not ready")
        self.g.boot_gps()
        self.assertTrue(self.g._gps_enabled)
        self.g._poll_gps()
        self.assertEqual(self.serial.call_count, 2)
        CLOCK.now = 104.9
        self.g._poll_gps()
        self.assertEqual(self.serial.call_count, 2)
        CLOCK.now = 105.0
        self.g._poll_gps()
        self.assertEqual(self.serial.call_count, 3)
        self.assertTrue(self.g._gps_enabled)
        self.g.gps.setup.assert_not_called()

    def test_gpio_failure_does_not_open_uart_or_change_disabled_state(self):
        self.prohibit_setup_probe()
        self.g._aio_power.toggle.return_value = False
        self.g._toggle_gps()
        self.assertFalse(self.g._gps_enabled)
        self.serial.assert_not_called()
        self.g.gps.setup.assert_not_called()

    def test_toggle_off_closes_uart_clears_fix_and_disables_retry(self):
        self.prohibit_setup_probe()
        self.g._toggle_gps()
        self.g.gps_fix = True
        self.g.gps_sats, self.g.gps_sats_vis = 8, 12
        self.g._toggle_gps()
        self.conn.close.assert_called_once_with()
        self.assertFalse(self.g._gps_enabled)
        self.assertFalse(self.g.gps.available)
        self.assertFalse(self.g.gps_fix)
        self.assertEqual((self.g.gps_sats, self.g.gps_sats_vis), (0, 0))
        before = self.serial.call_count
        CLOCK.now += 10
        self.g._poll_gps()
        self.assertEqual(self.serial.call_count, before)

    def test_non_aio_and_usb_gps_keep_the_existing_setup_path(self):
        for aio, device in ((False, "/dev/ttyAMA0"), (True, "/dev/ttyUSB0"),
                            (True, "/dev/serial/by-id/synthetic-gps")):
            with self.subTest(aio=aio, device=device):
                self.g._aio_available = aio
                self.g.gps.device = device
                self.g.gps._available = False
                self.g._gps_enabled = False
                def setup():
                    self.g.gps._available = True
                    return True
                self.g.gps.setup = Mock(side_effect=setup)
                self.g._toggle_gps()
                self.g.gps.setup.assert_called_once_with()
                self.assertTrue(self.g._gps_enabled)
                self.serial.assert_not_called()

    def test_failed_non_aio_or_usb_setup_does_not_enable_uart_retry(self):
        for aio, device in ((False, "/dev/ttyAMA0"), (True, "/dev/ttyUSB0")):
            with self.subTest(aio=aio, device=device):
                self.g._aio_available = aio
                self.g.gps.device = device
                self.g.gps._available = False
                self.g._gps_enabled = False
                self.g.gps.setup = Mock(return_value=False)
                self.g._toggle_gps()
                self.assertFalse(self.g._gps_enabled)
                self.g._poll_gps()
                self.serial.assert_not_called()


if __name__ == "__main__":
    unittest.main()
