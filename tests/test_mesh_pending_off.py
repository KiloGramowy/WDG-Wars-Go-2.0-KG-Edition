"""Queued mesh receptions belong to the map snapshot taken after OFF.

Compile native callbacks, queue polling and mapping methods; use temporary real
contact persistence. These are software regressions without a radio or upload.
"""
import ast
import copy
from queue import Queue
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from test_mesh_map_history import (
    ENV, MapGame, NATIVE_GAME, SOURCE, XP, game_for, legacy, loot_at,
)
from watchdogs.mesh_rules import MESH_NEW_CONTACT_XP


METHODS = {"_active_lora_protocol", "_on_mc_node", "_on_mt_node",
           "_poll_lora", "_poll_meshtastic"}
namespace = dict(ENV, MapGame=MapGame, pyxel=SimpleNamespace(frame_count=42),
                 C_HACK_CYAN=12)
harness = ast.ClassDef(name="PendingGame", bases=[ast.Name(id="MapGame", ctx=ast.Load())],
    keywords=[], decorator_list=[], body=[copy.deepcopy(node) for node in NATIVE_GAME.body
        if isinstance(node, ast.FunctionDef) and node.name in METHODS])
exec(compile(ast.fix_missing_locations(ast.Module(body=[harness], type_ignores=[])),
             str(SOURCE), "exec"), namespace)
PendingGame = namespace["PendingGame"]


def pending_game(loot, gps=None):
    game = PendingGame()
    game.__dict__.update(game_for(loot).__dict__)
    game.gps = gps
    game.player_lat, game.player_lon = 51.1, 17.9
    game._mc_event_queue, game._mt_event_queue = Queue(), Queue()
    game._mc_bubbles = []
    for radio, protocol in ((game._lora, "meshcore"), (game._mt, "meshtastic")):
        radio.mode, radio.queue = protocol, Queue()
        radio.running = radio.ready = False
    return game


def set_active(game, protocol):
    for radio, kind in ((game._lora, "meshcore"), (game._mt, "meshtastic")):
        radio.running = radio.ready = kind == protocol


def node_id(protocol, index):
    return ("!" if protocol == "meshtastic" else "") + f"{index:08x}"


def enqueue(game, protocol, index, positioned=True):
    callback = game._on_mc_node if protocol == "meshcore" else game._on_mt_node
    callback(node_id(protocol, index), "Client", f"Synthetic {index}",
             51.1 + index * 0.1 if positioned else 0,
             17.9 if positioned else 0, -90, -3)


class MeshPendingOffTests(unittest.TestCase):
    def test_off_ranks_the_whole_queued_batch_and_remembers_partial_expiry_on_resume(self):
        for protocol in ("meshcore", "meshtastic"):
            for resume in (False, True):
                with self.subTest(protocol=protocol, resume=resume), \
                        tempfile.TemporaryDirectory() as root, \
                        patch("time.monotonic", return_value=1000) as clock:
                    legacy(root)
                    with loot_at(root) as loot:
                        game = pending_game(loot)
                        set_active(game, protocol)
                        game._sync_mesh_map_visibility()
                        for index in range(1, 71):
                            enqueue(game, protocol, index)
                        enqueue(game, protocol, 64)  # Old duplicate still in the original batch.
                        set_active(game, "")
                        game._sync_mesh_map_visibility()
                        game._poll_lora()
                        self.assertEqual(len(game.markers), 32)
                        clock.return_value = 1350
                        game._sync_mesh_map_visibility()
                        # The farther 38 queued nodes participate in ranking,
                        # although none has been persisted or drawn yet.
                        self.assertEqual(len(game.markers), 32)
                        clock.return_value = 1450
                        if resume:
                            set_active(game, protocol)
                        game._sync_mesh_map_visibility()
                        game._poll_lora()
                        game._sync_mesh_map_visibility()
                        self.assertEqual(len(game.markers), 35)
                        game._poll_lora()
                        game._sync_mesh_map_visibility()
                        self.assertEqual({marker.mesh_key for marker in game.markers},
                            {f"{protocol}:{node_id(protocol, index)}" for index in range(1, 36)})
                        self.assertEqual(len(game._mesh_session_nodes), 70)
                        self.assertEqual(game.xp, XP + 69 * MESH_NEW_CONTACT_XP)
                        if resume:
                            enqueue(game, protocol, 64)  # A genuinely later observation may restore it.
                            game._poll_lora()
                            self.assertEqual(len(game.markers), 36)
                            self.assertTrue(any(marker.mesh_key == f"{protocol}:{node_id(protocol, 64)}"
                                                for marker in game.markers))
                        else:
                            clock.return_value = 1600
                            game._sync_mesh_map_visibility()
                            self.assertEqual(game.markers, [])

    def test_off_preview_uses_stored_node_or_recorded_receiver_position_without_persistence(self):
        for protocol in ("meshcore", "meshtastic"):
            with self.subTest(protocol=protocol), tempfile.TemporaryDirectory() as root, \
                    patch("time.monotonic", return_value=1000) as clock, \
                    patch("time.time", return_value=1000):
                legacy(root)
                gps = SimpleNamespace(radio_fix=lambda **kwargs: dict(lat=51.2, lon=17.9,
                    observed_at=kwargs["now"] - 0.1, quality=1, sats=8, hdop=0.8))
                with loot_at(root, gps) as loot:
                    game = pending_game(loot, gps)
                    set_active(game, protocol)
                    game._sync_mesh_map_visibility()
                    enqueue(game, protocol, 1, positioned=False)  # Retain canonical saved coordinates.
                    enqueue(game, protocol, 2, positioned=False)  # Use recorded RX GPS.
                    enqueue(game, protocol, 3)  # Genuine node position.
                    before = loot._db_path.read_bytes()
                    set_active(game, "")
                    game._sync_mesh_map_visibility()
                    self.assertEqual(game.markers, [])
                    self.assertEqual(game._mesh_session_nodes, set())
                    self.assertEqual(loot._db_path.read_bytes(), before)
                    game.player_lat, game.player_lon = 60, 17.9  # OFF origin must stay frozen.
                    clock.return_value = 1350
                    game._poll_lora()
                    self.assertEqual(len(game.markers), 2)
                    known = next(marker for marker in game.markers
                                 if marker.mesh_key == f"{protocol}:{node_id(protocol, 1)}")
                    self.assertEqual((known.lat, known.lon), (51.1, 17.9))
                    fallback = next(marker for marker in game.markers if marker is not known)
                    self.assertTrue(fallback.label.endswith("[RX GPS]"))
                    clock.return_value = 1450
                    game._sync_mesh_map_visibility()
                    self.assertEqual(game.markers, [known])

    def test_busy_snapshot_mutex_is_retried_without_waiting_or_moving_off_time(self):
        class BusyLock:
            def acquire(self, blocking):
                self.blocking = blocking
                return False

        for protocol in ("meshcore", "meshtastic"):
            with self.subTest(protocol=protocol), tempfile.TemporaryDirectory() as root, \
                    patch("time.monotonic", return_value=1000) as clock:
                legacy(root)
                with loot_at(root) as loot:
                    game = pending_game(loot)
                    set_active(game, protocol)
                    game._sync_mesh_map_visibility()
                    enqueue(game, protocol, 1)
                    game._poll_lora()
                    enqueue(game, protocol, 2)
                    set_active(game, "")
                    queue = game._mc_event_queue if protocol == "meshcore" else game._mt_event_queue
                    original, busy = queue.mutex, BusyLock()
                    queue.mutex = busy
                    try:
                        game._sync_mesh_map_visibility()
                        self.assertFalse(busy.blocking)
                    finally:
                        queue.mutex = original
                    clock.return_value = 1001
                    game._sync_mesh_map_visibility()
                    game._poll_lora()
                    clock.return_value = 1600
                    game._sync_mesh_map_visibility()
                    self.assertEqual(game.markers, [])

    def test_late_first_snapshot_removes_expired_visible_marker_in_the_same_refresh(self):
        for protocol in ("meshcore", "meshtastic"):
            with self.subTest(protocol=protocol), tempfile.TemporaryDirectory() as root, \
                    patch("time.monotonic", return_value=1000) as clock:
                legacy(root)
                with loot_at(root) as loot:
                    game = pending_game(loot)
                    set_active(game, protocol)
                    enqueue(game, protocol, 1)
                    game._poll_lora()
                    self.assertEqual(len(game.markers), 1)
                    set_active(game, "")
                    queue = game._mc_event_queue if protocol == "meshcore" else game._mt_event_queue
                    original = queue.mutex
                    queue.mutex = SimpleNamespace(acquire=lambda _blocking: False)
                    try:
                        game._sync_mesh_map_visibility()
                    finally:
                        queue.mutex = original
                    clock.return_value = 1601
                    game._sync_mesh_map_visibility()
                    self.assertEqual(game.markers, [])
                    self.assertNotIn((protocol, node_id(protocol, 1)), game._mesh_map_eligible_nodes)

    def test_long_pause_expires_pending_batch_but_still_saves_and_counts_queued_rx(self):
        for protocol in ("meshcore", "meshtastic"):
            for resume in (False, True):
                with self.subTest(protocol=protocol, resume=resume), \
                        tempfile.TemporaryDirectory() as root, \
                        patch("time.monotonic", return_value=1000) as clock, \
                        patch("time.time", return_value=1000):
                    legacy(root)
                    with loot_at(root) as loot:
                        game = pending_game(loot)
                        set_active(game, protocol)
                        game._sync_mesh_map_visibility()
                        for index in range(1, 71):
                            enqueue(game, protocol, index)
                        set_active(game, "")
                        game._sync_mesh_map_visibility()
                        game._poll_lora()
                        self.assertEqual(len(game.markers), 32)
                        clock.return_value = 1600
                        if resume:
                            set_active(game, protocol)
                            enqueue(game, protocol, 1)  # New observation of a saved identity.
                        game._sync_mesh_map_visibility()
                        self.assertEqual(game.markers, [])
                        self.assertEqual(game._mesh_map_eligible_nodes, set())
                        game._poll_lora()
                        game._sync_mesh_map_visibility()
                        self.assertEqual(game.markers, [])
                        game._poll_lora()
                        game._sync_mesh_map_visibility()
                        expected = [f"{protocol}:{node_id(protocol, 1)}"] if resume else []
                        self.assertEqual([marker.mesh_key for marker in game.markers], expected)
                        self.assertEqual(len(game._mesh_session_nodes), 70)
                        contacts = (loot.load_contacts() if protocol == "meshcore"
                                    else loot.load_meshtastic_contacts())
                        self.assertEqual(len(contacts), 70)
                        self.assertEqual(game.xp, XP + 69 * MESH_NEW_CONTACT_XP)

    def test_later_gps_does_not_restore_expired_nodes_still_in_pending_batch(self):
        for protocol in ("meshcore", "meshtastic"):
            with self.subTest(protocol=protocol), tempfile.TemporaryDirectory() as root, \
                    patch("time.monotonic", return_value=1000) as clock, \
                    patch("time.time", return_value=1000):
                legacy(root, positioned=False)
                gps = SimpleNamespace(radio_fix=Mock(return_value=None))
                with loot_at(root, gps) as loot:
                    game = pending_game(loot, gps)
                    set_active(game, protocol)
                    game._sync_mesh_map_visibility()
                    for index in range(1, 35):
                        enqueue(game, protocol, index, positioned=False)
                    set_active(game, "")
                    game._sync_mesh_map_visibility()
                    game._poll_lora()
                    clock.return_value = 1600
                    gps.radio_fix.side_effect = lambda **kwargs: dict(lat=51.2, lon=18,
                        observed_at=kwargs["now"] - 0.1, quality=1, sats=8, hdop=0.8)
                    game._assign_missing_mesh_gps()
                    self.assertEqual(game.markers, [])
                    game._poll_lora()  # Persist the remaining pre-OFF observations.
                    game._sync_mesh_map_visibility()
                    clock.return_value = 1602
                    game._assign_missing_mesh_gps()
                    self.assertEqual(game.markers, [])
                    self.assertEqual(game._mesh_map_eligible_nodes, set())
                    self.assertEqual(len(game._mesh_session_nodes), 34)
                    self.assertEqual(game.xp, XP + 33 * MESH_NEW_CONTACT_XP)

    def test_large_off_batch_is_processed_over_frames_with_original_off_deadline(self):
        for protocol in ("meshcore", "meshtastic"):
            with self.subTest(protocol=protocol), tempfile.TemporaryDirectory() as root, \
                    patch("time.monotonic", return_value=1000) as clock:
                legacy(root)
                with loot_at(root) as loot:
                    game = pending_game(loot)
                    set_active(game, protocol)
                    game._sync_mesh_map_visibility()
                    for index in range(1, 71):
                        enqueue(game, protocol, index)
                    set_active(game, "")
                    game._sync_mesh_map_visibility()
                    game._poll_lora()
                    game._sync_mesh_map_visibility()
                    self.assertEqual(len(game.markers), 32)
                    self.assertTrue(game._mesh_map_decay[protocol].active)
                    clock.return_value = 1001
                    game._poll_lora()
                    game._sync_mesh_map_visibility()
                    self.assertEqual(len(game.markers), 64)
                    clock.return_value = 1002
                    game._poll_lora()
                    game._sync_mesh_map_visibility()
                    self.assertEqual(len(game.markers), 70)
                    clock.return_value = 1300
                    game._sync_mesh_map_visibility()
                    self.assertEqual(len(game.markers), 69)
                    self.assertFalse(any(marker.mesh_key.endswith(node_id(protocol, 70))
                                         for marker in game.markers))
                    clock.return_value = 1600
                    game._sync_mesh_map_visibility()
                    self.assertEqual(game.markers, [])

    def test_replenished_queue_cannot_block_poll_or_extend_off_batch(self):
        class ReplenishedQueue(Queue):
            def __init__(self, replacement):
                super().__init__()
                self.replacement, self.reads = replacement, 0

            def get_nowait(self):
                self.reads += 1
                if self.reads > 100:
                    raise AssertionError("poll waited for a queue that never empties")
                event = super().get_nowait()
                self.put(self.replacement)
                return event

        for protocol in ("meshcore", "meshtastic"):
            with self.subTest(protocol=protocol), tempfile.TemporaryDirectory() as root, \
                    patch("time.monotonic", return_value=1000) as clock:
                legacy(root)
                with loot_at(root) as loot:
                    game = pending_game(loot)
                    game._mc_dm_ack_map, game._radio_protocol = {}, ""
                    replacement = (("dm_ack", "synthetic") if protocol == "meshcore"
                                   else ("status", "stopped", ""))
                    queue = ReplenishedQueue(replacement)
                    setattr(game, "_mc_event_queue" if protocol == "meshcore"
                            else "_mt_event_queue", queue)
                    set_active(game, protocol)
                    game._sync_mesh_map_visibility()
                    enqueue(game, protocol, 1)
                    enqueue(game, protocol, 2)
                    set_active(game, "")
                    game._sync_mesh_map_visibility()
                    game._poll_lora()
                    self.assertEqual(queue.reads, 2)
                    self.assertEqual(queue.qsize(), 2)
                    game._sync_mesh_map_visibility()
                    self.assertEqual(len(game.markers), 2)
                    self.assertTrue(game._mesh_map_decay[protocol].active)
                    clock.return_value = 1300
                    game._sync_mesh_map_visibility()
                    self.assertEqual(len(game.markers), 1)
                    clock.return_value = 1600
                    game._sync_mesh_map_visibility()
                    self.assertEqual(game.markers, [])

    def test_telemetry_poll_also_has_a_finite_frame_budget(self):
        with tempfile.TemporaryDirectory() as root:
            legacy(root)
            with loot_at(root) as loot:
                game = pending_game(loot)
                for radio in (game._lora, game._mt):
                    for _ in range(100):
                        radio.queue.put(("Synthetic telemetry", None))
                game._poll_lora()
                self.assertEqual(game._lora.queue.qsize(), 36)
                self.assertEqual(game._mt.queue.qsize(), 36)

    def test_off_or_switch_includes_entire_last_batch_and_preserves_distance_order(self):
        for protocol in ("meshcore", "meshtastic"):
            other = "meshtastic" if protocol == "meshcore" else "meshcore"
            for target in ("", other):
                with self.subTest(protocol=protocol, target=target), \
                        tempfile.TemporaryDirectory() as root, \
                        patch("time.monotonic", return_value=1000) as clock, \
                        patch("time.time", return_value=1000):
                    legacy(root)
                    with loot_at(root) as loot:
                        game = pending_game(loot)
                        set_active(game, protocol)
                        game._sync_mesh_map_visibility()
                        for index in (1, 2, 3):  # Saved contact and two new nodes.
                            enqueue(game, protocol, index)
                        self.assertEqual(game.markers, [])
                        set_active(game, target)
                        if target:
                            enqueue(game, target, 1)
                        # Draw/GPS can synchronize before the normal queue drain.
                        game._sync_mesh_map_visibility()
                        game._poll_lora()
                        game._sync_mesh_map_visibility()
                        expected_other = 1 if target else 0
                        self.assertEqual(len(game.markers), 3 + expected_other)
                        self.assertEqual(game.xp, XP + 2 * MESH_NEW_CONTACT_XP)
                        saved = loot._db_path.read_bytes()
                        # Unread chat/status events cannot extend an existing schedule.
                        if protocol == "meshcore":
                            game._mc_event_queue.put(("msg", "public", "synthetic", -90, 0))
                        else:
                            game._mt_event_queue.put(("status", "stopped", ""))
                        for moment, remaining in ((1299.999, 3), (1300, 2), (1450, 1), (1600, 0)):
                            clock.return_value = moment
                            game._sync_mesh_map_visibility()
                            old = [marker for marker in game.markers if marker.type == protocol]
                            self.assertEqual(len(old), remaining)
                            self.assertEqual({marker.mesh_key for marker in old},
                                {f"{protocol}:{node_id(protocol, index)}"
                                 for index in range(1, remaining + 1)})
                            self.assertEqual(len(game.markers), remaining + expected_other)
                        self.assertEqual(loot._db_path.read_bytes(), saved)

    def test_first_rx_then_off_before_first_ready_sample_still_expires(self):
        for protocol in ("meshcore", "meshtastic"):
            with self.subTest(protocol=protocol), tempfile.TemporaryDirectory() as root, \
                    patch("time.monotonic", return_value=1000) as clock:
                legacy(root)
                with loot_at(root) as loot:
                    game = pending_game(loot)
                    set_active(game, protocol)
                    enqueue(game, protocol, 1)
                    set_active(game, "")
                    game._sync_mesh_map_visibility()
                    game._poll_lora()
                    game._sync_mesh_map_visibility()
                    self.assertEqual(len(game.markers), 1)
                    clock.return_value = 1600
                    game._sync_mesh_map_visibility()
                    self.assertEqual(game.markers, [])
                    self.assertEqual(game.xp, XP)

    def test_queued_unlocated_node_remains_eligible_for_later_receiver_fix(self):
        for protocol in ("meshcore", "meshtastic"):
            with self.subTest(protocol=protocol), tempfile.TemporaryDirectory() as root, \
                    patch("time.monotonic", return_value=1000) as clock, \
                    patch("time.time", return_value=1000):
                legacy(root, positioned=False)
                gps = SimpleNamespace(radio_fix=Mock(return_value=None))
                with loot_at(root, gps) as loot:
                    game = pending_game(loot, gps)
                    set_active(game, protocol)
                    game._sync_mesh_map_visibility()
                    enqueue(game, protocol, 1, positioned=False)
                    set_active(game, "")
                    game._sync_mesh_map_visibility()
                    game._poll_lora()
                    game._sync_mesh_map_visibility()
                    self.assertEqual(game.markers, [])
                    self.assertIn((protocol, node_id(protocol, 1)), game._mesh_map_eligible_nodes)
                    clock.return_value = 1001
                    gps.radio_fix.side_effect = lambda **kwargs: dict(lat=51.2, lon=18,
                        observed_at=kwargs["now"] - 0.1, quality=1, sats=8, hdop=0.8)
                    game._assign_missing_mesh_gps()
                    self.assertEqual(len(game.markers), 1)
                    self.assertTrue(game.markers[0].label.endswith("[RX GPS]"))
                    contacts = loot.load_contacts() if protocol == "meshcore" else loot.load_meshtastic_contacts()
                    self.assertEqual(contacts[node_id(protocol, 1)]["last_seen"], 1000)
                    self.assertEqual(game.xp, XP)
                    clock.return_value = 1600
                    game._sync_mesh_map_visibility()
                    self.assertEqual(game.markers, [])

    def test_ready_protocol_keeps_new_and_known_nodes_beyond_off_interval(self):
        for protocol in ("meshcore", "meshtastic"):
            with self.subTest(protocol=protocol), tempfile.TemporaryDirectory() as root, \
                    patch("time.monotonic", return_value=1000) as clock:
                legacy(root)
                with loot_at(root) as loot:
                    game = pending_game(loot)
                    set_active(game, protocol)
                    for index in (1, 2):
                        enqueue(game, protocol, index)
                    game._poll_lora()
                    game._sync_mesh_map_visibility()
                    clock.return_value = 1700
                    game._sync_mesh_map_visibility()
                    enqueue(game, protocol, 1)
                    game._poll_lora()
                    game._sync_mesh_map_visibility()
                    self.assertEqual(len(game.markers), 2)
                    self.assertEqual(game.xp, XP + MESH_NEW_CONTACT_XP)

    def test_rapid_resume_rx_off_cancels_old_decay_and_keeps_new_batch(self):
        for protocol in ("meshcore", "meshtastic"):
            with self.subTest(protocol=protocol), tempfile.TemporaryDirectory() as root, \
                    patch("time.monotonic", return_value=1000) as clock:
                legacy(root)
                with loot_at(root) as loot:
                    game = pending_game(loot)
                    set_active(game, protocol)
                    enqueue(game, protocol, 1)
                    enqueue(game, protocol, 2)
                    game._poll_lora()
                    game._sync_mesh_map_visibility()
                    set_active(game, "")
                    game._sync_mesh_map_visibility()
                    clock.return_value = 1400  # Farther node has already expired.
                    set_active(game, protocol)
                    enqueue(game, protocol, 3)
                    set_active(game, "")  # Neither ready transition was sampled.
                    game._sync_mesh_map_visibility()
                    game._poll_lora()
                    game._sync_mesh_map_visibility()
                    self.assertEqual({marker.mesh_key for marker in game.markers},
                        {f"{protocol}:{node_id(protocol, index)}" for index in (1, 3)})
                    clock.return_value = 1699
                    game._sync_mesh_map_visibility()
                    self.assertEqual(len(game.markers), 2)
                    clock.return_value = 1700
                    game._sync_mesh_map_visibility()
                    self.assertEqual([marker.mesh_key for marker in game.markers],
                                     [f"{protocol}:{node_id(protocol, 1)}"])
                    clock.return_value = 2000
                    game._sync_mesh_map_visibility()
                    self.assertEqual(game.markers, [])


if __name__ == "__main__":
    unittest.main()
