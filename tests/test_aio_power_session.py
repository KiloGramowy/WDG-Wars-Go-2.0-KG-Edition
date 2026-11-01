"""Power ownership and shutdown tests with fake GPIO only; no real hardware."""

from types import SimpleNamespace
import threading
import unittest
from unittest.mock import patch

from watchdogs.aio_manager import AioManager, AioPowerSession, GPIO_MAP


class FakePowerController:
    def __init__(self, **states):
        self.states = dict.fromkeys(GPIO_MAP, False)
        self.states.update(states)
        self.reads = []
        self.writes = []
        self.before_write = None
        self.read_override = {}
        self.fail_writes = set()

    def get_feature_state(self, feature):
        self.reads.append(feature)
        return self.read_override.get(feature, self.states[feature])

    def toggle(self, feature, on):
        self.writes.append((feature, on))
        if self.before_write:
            self.before_write(feature, on)
        if (feature, on) in self.fail_writes:
            return False
        self.states[feature] = on
        return True


class ConfirmedPowerReadTests(unittest.TestCase):
    def test_reads_actual_level_instead_of_output_latch(self):
        for level, expected in (("hi", True), ("lo", False)):
            with self.subTest(level=level), patch("watchdogs.aio_manager.subprocess.run",
                    return_value=SimpleNamespace(returncode=0,
                        stdout=f"16: op dh pd | {level} // GPIO16 = output\n")):
                self.assertIs(AioManager.get_feature_state("lora"), expected)

    def test_failed_wrong_pin_or_ambiguous_read_does_not_prove_off(self):
        for code, output in (
                (1, "16: op dl pd | lo"),
                (0, "16: op dh pd | --"),
                (0, "17: op dl pd | lo"),
                (0, "permission denied"),
                (0, "16: op dh pd | lo\n16: op dh pd | hi"),
                (0, "16: op dh pd hi")):
            with self.subTest(code=code, output=output), patch(
                    "watchdogs.aio_manager.subprocess.run",
                    return_value=SimpleNamespace(returncode=code, stdout=output)):
                self.assertIsNone(AioManager.get_feature_state("lora"))
        with patch("watchdogs.aio_manager.subprocess.run", side_effect=OSError("missing")):
            self.assertIsNone(AioManager.get_feature_state("usb"))

    def test_old_toggle_keeps_its_existing_write_result_contract(self):
        with patch("watchdogs.aio_manager._pinctrl_set", return_value=True) as write, \
                patch("watchdogs.aio_manager._pinctrl_read_confirmed") as read:
            self.assertTrue(AioManager.toggle("usb", True))
        write.assert_called_once_with(23, True)
        read.assert_not_called()
        with patch("watchdogs.aio_manager.subprocess.run") as command:
            self.assertIsNone(AioManager.get_feature_state("not-a-feature"))
        command.assert_not_called()


class PowerSessionTests(unittest.TestCase):
    def test_preserves_preexisting_on_and_unknown_rails(self):
        controller = FakePowerController(lora=False, usb=True, gps=None)
        session = AioPowerSession(controller)
        for feature in ("usb", "gps", "lora"):
            self.assertTrue(session.toggle(feature, True))
        controller.writes.clear()
        self.assertEqual(session.shutdown(), {"lora": True})
        self.assertEqual(controller.writes, [("lora", False)])
        self.assertTrue(controller.states["usb"])
        self.assertTrue(controller.states["gps"])

    def test_nonboolean_state_cannot_grant_ownership(self):
        for state in (None, 0, "lo"):
            with self.subTest(state=state):
                controller = FakePowerController(usb=state)
                session = AioPowerSession(controller)
                self.assertTrue(session.toggle("usb", True))
                self.assertEqual(session.shutdown(), {})
                self.assertEqual(controller.writes, [("usb", True)])

    def test_explicit_claim_adopts_confirmed_on_without_hardware_write(self):
        controller = FakePowerController(lora=True, usb=True)
        session = AioPowerSession(controller)
        self.assertTrue(session.claim("lora"))
        self.assertEqual(controller.writes, [])
        self.assertEqual(session.shutdown(), {"lora": True})
        self.assertEqual(controller.writes, [("lora", False)])
        self.assertTrue(controller.states["usb"])

    def test_claim_rejects_off_unknown_invalid_or_closing_without_writing(self):
        for state in (False, None, 1, "hi"):
            with self.subTest(state=state):
                controller = FakePowerController(lora=state)
                session = AioPowerSession(controller)
                self.assertFalse(session.claim("lora"))
                self.assertEqual(session.shutdown(), {})
                self.assertEqual(controller.writes, [])
        controller = FakePowerController(lora=True)
        session = AioPowerSession(controller)
        self.assertFalse(session.claim("invalid"))
        session.begin_shutdown()
        self.assertFalse(session.claim("lora"))
        self.assertEqual(controller.reads, [])
        self.assertEqual(controller.writes, [])

    def test_failed_read_does_not_claim_a_preexisting_rail(self):
        controller = FakePowerController(usb=True)
        session = AioPowerSession(controller)
        with patch.object(controller, "get_feature_state", side_effect=OSError("read failed")):
            self.assertTrue(session.toggle("usb", True))
        self.assertEqual(session.shutdown(), {})

    def test_partial_failed_on_write_is_owned_before_the_write(self):
        controller = FakePowerController()
        session = AioPowerSession(controller)

        def partly_enable(feature, on):
            if on:
                self.assertIn(feature, session._owned)
                controller.states[feature] = True
                raise OSError("write applied but command failed")

        controller.before_write = partly_enable
        self.assertFalse(session.toggle("lora", True))
        self.assertTrue(controller.states["lora"])
        controller.before_write = None
        self.assertEqual(session.shutdown(), {"lora": True})
        self.assertFalse(controller.states["lora"])

    def test_interruption_after_partial_on_write_keeps_cleanup_ownership(self):
        controller = FakePowerController()
        session = AioPowerSession(controller)

        def interrupted(feature, on):
            self.assertIn(feature, session._owned)
            controller.states[feature] = True
            raise KeyboardInterrupt()

        controller.before_write = interrupted
        with self.assertRaises(KeyboardInterrupt):
            session.toggle("usb", True)
        controller.before_write = None
        self.assertEqual(session.shutdown(), {"usb": True})

    def test_confirmed_manual_off_drops_claim_and_shutdown_is_idempotent(self):
        controller = FakePowerController()
        session = AioPowerSession(controller)
        self.assertTrue(session.toggle("lora", True))
        self.assertTrue(session.toggle("lora", False))
        writes = list(controller.writes)
        self.assertEqual(session.shutdown(), {})
        self.assertEqual(session.shutdown(), {})
        self.assertEqual(controller.writes, writes)

    def test_failed_off_keeps_claim_for_retry(self):
        controller = FakePowerController()
        session = AioPowerSession(controller)
        self.assertTrue(session.toggle("lora", True))
        controller.fail_writes.add(("lora", False))
        self.assertFalse(session.toggle("lora", False))
        self.assertEqual(session.shutdown(), {"lora": False})
        controller.fail_writes.clear()
        self.assertEqual(session.shutdown(), {"lora": True})

    def test_off_requires_confirmed_low_before_releasing_claim(self):
        for level in (None, True):
            with self.subTest(level=level):
                controller = FakePowerController()
                session = AioPowerSession(controller)
                self.assertTrue(session.toggle("lora", True))
                controller.read_override["lora"] = level
                self.assertEqual(session.shutdown(), {"lora": False})
                controller.read_override.clear()
                self.assertEqual(session.shutdown(), {"lora": True})

    def test_shutdown_reports_blocks_and_orders_usb_last(self):
        controller = FakePowerController()
        session = AioPowerSession(controller)
        for feature in ("usb", "gps", "sdr", "lora"):
            self.assertTrue(session.toggle(feature, True))
        controller.writes.clear()
        self.assertEqual(session.shutdown(blocked_features=("lora",)),
                         {"lora": False, "sdr": True, "gps": True, "usb": True})
        self.assertEqual(controller.writes, [("sdr", False), ("gps", False), ("usb", False)])
        self.assertEqual(session.shutdown(), {"lora": True})

    def test_one_failed_power_off_does_not_skip_other_owned_rails(self):
        controller = FakePowerController()
        session = AioPowerSession(controller)
        for feature in ("usb", "lora"):
            self.assertTrue(session.toggle(feature, True))
        controller.fail_writes.add(("lora", False))
        self.assertEqual(session.shutdown(), {"lora": False, "usb": True})

    def test_begin_shutdown_rejects_late_on_without_touching_hardware(self):
        controller = FakePowerController()
        session = AioPowerSession(controller)
        session.begin_shutdown()
        self.assertFalse(session.toggle("usb", True))
        self.assertEqual(controller.reads, [])
        self.assertEqual(controller.writes, [])

    def test_shutdown_waits_for_inflight_on_then_switches_it_off(self):
        controller = FakePowerController()
        session = AioPowerSession(controller)
        write_started, release_write = threading.Event(), threading.Event()
        shutdown_started, shutdown_done = threading.Event(), threading.Event()
        errors, results = [], []

        def blocked_write(feature, on):
            if on:
                write_started.set()
                if not release_write.wait(2):
                    raise RuntimeError("test did not release ON write")

        def enable():
            try:
                results.append(session.toggle("usb", True))
            except BaseException as exc:
                errors.append(exc)

        def shutdown():
            shutdown_started.set()
            try:
                results.append(session.shutdown())
            except BaseException as exc:
                errors.append(exc)
            finally:
                shutdown_done.set()

        controller.before_write = blocked_write
        enabled = threading.Thread(target=enable)
        stopped = threading.Thread(target=shutdown)
        enabled.start()
        try:
            self.assertTrue(write_started.wait(1))
            stopped.start()
            self.assertTrue(shutdown_started.wait(1))
            self.assertFalse(shutdown_done.wait(0.05))
        finally:
            release_write.set()
            enabled.join(2)
            if stopped.ident is not None:
                stopped.join(2)
        self.assertFalse(enabled.is_alive())
        self.assertFalse(stopped.is_alive())
        self.assertEqual(errors, [])
        self.assertIn(True, results)
        self.assertIn({"usb": True}, results)
        self.assertEqual(controller.writes, [("usb", True), ("usb", False)])
        self.assertFalse(controller.states["usb"])
        self.assertFalse(session.toggle("usb", True))

    def test_independent_session_cannot_turn_off_another_sessions_power(self):
        controller = FakePowerController()
        first, second = AioPowerSession(controller), AioPowerSession(controller)
        self.assertTrue(first.toggle("lora", True))
        self.assertTrue(second.toggle("lora", True))
        self.assertEqual(second.shutdown(), {})
        self.assertTrue(controller.states["lora"])
        self.assertEqual(first.shutdown(), {"lora": True})


if __name__ == "__main__":
    unittest.main()
