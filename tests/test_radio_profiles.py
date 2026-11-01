"""Offline GPS policy tests with injected coordinates, clock and area resolver.

Synthetic fixes remain in temporary configuration directories and never reach
player discoveries, upload, GPIO or a radio manager.
"""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from watchdogs.radio_profiles import PROFILES, RadioProfileController


POINT = (36.47, -6.2)
ES = {'code': 'ES', 'label': 'Spain', 'region': 'EU_868', 'boundary_distance_km': 100.0,
      'land_boundary_distance_km': 6.0}


class Clock:
    def __init__(self): self.now = 0.0
    def __call__(self): return self.now


class RadioProfilePolicyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.clock = Clock()
        self.area = dict(ES)
        self.controller = self.new_controller()

    def new_controller(self):
        return RadioProfileController(home=self.directory.name,
            resolver=lambda lat, lon: deepcopy(self.area), clock=self.clock)

    def fix(self, point=POINT, **changes):
        value = dict(lat=point[0], lon=point[1], observed_at=self.clock.now,
                     quality=1, sats=8, hdop=0.8)
        value.update(changes)
        return value

    def stable(self, point=POINT, controller=None):
        controller = controller or self.controller
        start = self.clock.now
        for delta in (0, 5, 10, 15, 20):
            self.clock.now = start + delta
            controller.update(self.fix(point))
        self.assertIsNotNone(controller.preview_area('meshtastic'))

    def approve(self, protocol='meshtastic', key='sfnarrow'):
        self.controller.remember(protocol, key, self.controller.preview_area(protocol))
        self.controller.set_auto(protocol)

    def move(self, km):
        return POINT[0] + km / 111.2, POINT[1]

    def test_default_manual_and_auto_without_fix_do_not_infer_country(self):
        self.assertEqual(self.controller.target('meshcore'), 'eu_uk_narrow')
        self.controller.set_auto('meshtastic')
        for _ in range(3): self.controller.update(None)
        self.assertIsNone(self.controller.target('meshtastic'))
        self.assertEqual(self.controller.status('meshtastic')['reason'], 'AUTO requires a GPS fix')
        self.assertEqual(self.controller.status('meshtastic')['active_profile'], 'sfnarrow')
        self.assertEqual(self.controller.data['bindings'], [])

    def test_country_and_documented_profile_do_not_create_automatic_approval(self):
        self.controller.set_auto('meshtastic')
        self.stable()
        self.assertIsNone(self.controller.target('meshtastic'))
        self.assertIn('No approved profile', self.controller.status('meshtastic')['reason'])
        self.assertEqual(self.controller.data['bindings'], [])

    def test_explicit_approval_uses_the_displayed_two_km_circle(self):
        self.stable()
        preview = self.controller.preview_area('meshtastic')
        self.assertEqual((preview['lat'], preview['lon']), POINT)
        self.assertEqual(preview['radius_km'], 2.0)
        self.approve()
        self.assertEqual(self.controller.target('meshtastic'), 'sfnarrow')
        binding = self.controller.data['bindings'][0]
        self.assertIs(binding['approved_by_user'], True)
        for field in ('id', 'lat', 'lon', 'radius_km', 'country', 'region'):
            self.assertEqual(binding[field], preview[field])

    def test_five_distinct_samples_and_twenty_seconds_are_both_required(self):
        self.controller.set_auto('meshtastic')
        initial = self.fix()
        for _ in range(100): self.controller.update(initial)
        self.assertIsNone(self.controller.preview_area('meshtastic'))
        self.assertEqual(self.controller.status('meshtastic')['gps_status'], 'Stabilizing GPS (20 s)')
        for second in (1, 2, 3, 4):
            self.clock.now = second
            self.controller.update(self.fix())
        self.assertIsNone(self.controller.preview_area('meshtastic'))
        self.clock.now = 20
        self.controller.update(self.fix())
        self.assertIsNotNone(self.controller.preview_area('meshtastic'))
        self.assertEqual(self.controller.status('meshtastic')['gps_status'], 'GPS fix confirmed')

    def test_elapsed_time_without_five_samples_is_not_stable(self):
        for second in (0, 20, 25):
            self.clock.now = second
            self.controller.update(self.fix())
        self.assertIsNone(self.controller.preview_area('meshcore'))

    def test_unreliable_stale_future_or_malformed_fix_never_approves(self):
        invalid = [None, {}, self.fix(quality=0), self.fix(quality=3), self.fix(sats=3),
                   self.fix(hdop=0), self.fix(hdop=3.1), self.fix(hdop=float('nan')),
                   self.fix(lat=91), self.fix(lon=-181), self.fix(lat=float('inf')),
                   self.fix(observed_at=-11), self.fix(observed_at=1), self.fix(lat=None)]
        self.controller.set_auto('meshtastic')
        for value in invalid:
            with self.subTest(value=value):
                self.controller.update(value)
                self.assertIsNone(self.controller.target('meshtastic'))
                self.assertIsNone(self.controller.preview_area('meshtastic'))

    def test_loss_of_fix_keeps_applied_profile_and_requires_stabilization_again(self):
        self.stable()
        self.approve()
        self.controller.applied('meshtastic', 'sfnarrow')
        self.controller.update(None)
        state = self.controller.status('meshtastic')
        self.assertEqual(state['active_profile'], 'sfnarrow')
        self.assertIn('lost', state['reason'])
        self.assertIsNone(self.controller.target('meshtastic'))
        self.clock.now += 5
        self.controller.update(self.fix())
        self.assertIsNone(self.controller.target('meshtastic'))
        self.stable()
        self.assertEqual(self.controller.target('meshtastic'), 'sfnarrow')

    def test_expired_fix_status_cannot_still_claim_confirmation_without_update(self):
        self.stable()
        self.approve()
        self.clock.now += 11
        self.assertIsNone(self.controller.target('meshtastic'))
        state = self.controller.status('meshtastic')
        self.assertIn('lost', state['reason'])
        self.assertNotIn('confirmed', state['gps_status'])

    def test_unknown_country_and_country_border_keep_active_profile(self):
        self.controller.set_auto('meshtastic')
        for area, message in ((None, 'Unsupported area; select MANUAL'),
                              (dict(ES, boundary_distance_km=0.1), 'Near area boundary; profile kept')):
            with self.subTest(area=area):
                self.area = area
                self.clock.now += 30
                self.controller.update(self.fix())
                self.assertIsNone(self.controller.target('meshtastic'))
                self.assertIsNone(self.controller.preview_area('meshtastic'))
                self.assertEqual(self.controller.status('meshtastic')['active_profile'], 'sfnarrow')
                self.assertEqual(self.controller.status('meshtastic')['gps_status'], message)

    def test_country_transition_requires_new_stable_samples(self):
        self.stable()
        self.approve()
        self.area = dict(code='GB', label='United Kingdom', region='EU_868', boundary_distance_km=100,
                         land_boundary_distance_km=6.0)
        self.clock.now += 5
        self.controller.update(self.fix())
        self.assertIsNone(self.controller.preview_area('meshtastic'))
        self.assertIsNone(self.controller.target('meshtastic'))
        self.stable()
        self.assertIsNone(self.controller.target('meshtastic'))  # ES circle cannot apply in GB.

    def test_single_impossible_jump_and_backwards_timestamp_are_rejected(self):
        self.stable()
        self.approve()
        self.clock.now += 5
        self.controller.update(self.fix(self.move(20)))
        self.assertIsNone(self.controller.target('meshtastic'))
        self.assertIn('rejected', self.controller.status('meshtastic')['reason'])
        self.controller.update(self.fix(observed_at=19))
        self.assertIsNone(self.controller.preview_area('meshtastic'))
        self.assertEqual(self.controller.status('meshtastic')['active_profile'], 'sfnarrow')

    def test_real_mapped_us_canada_border_is_blocked_after_twenty_stable_seconds(self):
        from watchdogs.radio_geography import resolve_area
        point = (48.9900, -100.0)
        mapped = resolve_area(*point)
        self.assertEqual(mapped['code'], 'US')
        self.assertLess(mapped['land_boundary_distance_km'], 2)
        controller = RadioProfileController(home=Path(self.directory.name) / 'us-border',
                                            resolver=resolve_area, clock=self.clock)
        controller.set_auto('meshtastic')
        for second in (0, 5, 10, 15, 20):
            self.clock.now = second
            controller.update(self.fix(point))
        self.assertIsNone(controller.preview_area('meshtastic'))
        self.assertIsNone(controller.target('meshtastic'))
        self.assertIn('boundary', controller.status('meshtastic')['reason'])
        self.assertEqual(controller.status('meshtastic')['active_profile'], 'sfnarrow')

    def test_real_san_fernando_can_offer_local_approval_after_twenty_seconds(self):
        from watchdogs.radio_geography import resolve_area
        point = (36.4666, -6.1988)
        mapped = resolve_area(*point)
        self.assertGreater(mapped['boundary_distance_km'], .25)
        self.assertLess(mapped['boundary_distance_km'], 2)
        self.assertGreater(mapped['land_boundary_distance_km'], 2)
        controller = RadioProfileController(home=Path(self.directory.name) / 'san-fernando',
                                            resolver=resolve_area, clock=self.clock)
        self.stable(point, controller)
        preview = controller.preview_area('meshtastic')
        self.assertEqual(preview['country'], 'ES')
        self.assertEqual(preview['radius_km'], 2)

    def test_missing_land_metadata_fails_closed_but_manual_remains_available(self):
        for missing in (False, True):
            with self.subTest(missing=missing):
                self.area = dict(ES, land_boundary_distance_km=None)
                if missing: del self.area['land_boundary_distance_km']
                self.controller = self.new_controller()
                self.controller.set_auto('meshtastic')
                for delta in (0, 5, 10, 15, 20):
                    self.clock.now += delta
                    self.controller.update(self.fix())
                self.assertIsNone(self.controller.target('meshtastic'))
                self.assertIsNone(self.controller.preview_area('meshtastic'))
                self.controller.set_manual('meshtastic', 'longfast')
                self.assertEqual(self.controller.target('meshtastic'), 'longfast')

    def test_land_margin_above_two_km_allows_explicit_local_approval(self):
        self.area = dict(ES, land_boundary_distance_km=2.1)
        self.stable()
        self.approve()
        self.assertEqual(self.controller.target('meshtastic'), 'sfnarrow')

    def test_conflicting_coordinates_at_same_epoch_invalidate_stable_fix(self):
        self.stable()
        self.approve()
        same_epoch = self.fix(self.move(1))
        self.controller.update(same_epoch)
        self.assertIsNone(self.controller.target('meshtastic'))
        self.assertIsNone(self.controller.preview_area('meshtastic'))
        self.assertIn('rejected', self.controller.status('meshtastic')['reason'])

    def test_local_circle_border_and_outside_do_not_retune(self):
        self.stable()
        self.approve()
        for distance in (1.9, 2.05, 2.4):
            with self.subTest(distance=distance):
                self.clock.now += 60
                self.stable(self.move(distance))
                self.assertIsNone(self.controller.target('meshtastic'))
                self.assertEqual(self.controller.status('meshtastic')['active_profile'], 'sfnarrow')

    def test_ten_metre_gps_jitter_during_confirmation_keeps_displayed_circle(self):
        self.stable()
        preview = deepcopy(self.controller.preview_area('meshtastic'))
        self.clock.now += 2
        self.controller.update(self.fix(self.move(.01)))
        self.assertNotEqual(self.controller.preview_area('meshtastic')['lat'], preview['lat'])
        self.controller.remember('meshtastic', 'sfnarrow', preview)
        binding, = self.controller.data['bindings']
        for field in ('id', 'label', 'lat', 'lon', 'radius_km', 'country', 'region'):
            self.assertEqual(binding[field], preview[field])
        self.assertEqual(binding['radius_km'], 2.0)
        restored = self.new_controller()
        self.assertEqual(restored.data['bindings'], self.controller.data['bindings'])

    def test_confirmation_after_leaving_circle_country_or_fix_is_rejected(self):
        for change in ('outside', 'country', 'region', 'lost', 'expired'):
            with self.subTest(change=change):
                self.area = dict(ES)
                self.controller = self.new_controller()
                self.stable()
                preview = deepcopy(self.controller.preview_area('meshtastic'))
                if change == 'outside':
                    self.clock.now += 60
                    self.controller.update(self.fix(self.move(2.1)))
                    self.assertIsNotNone(self.controller.preview_area('meshtastic'))
                elif change == 'country':
                    self.area = dict(ES, code='GB', label='United Kingdom')
                    self.clock.now += 5
                    self.stable()
                    self.assertIsNotNone(self.controller.preview_area('meshtastic'))
                elif change == 'region':
                    self.area = dict(ES, region='US')
                    self.clock.now += 2
                    self.controller.update(self.fix())
                elif change == 'lost':
                    self.controller.update(None)
                else:
                    self.clock.now += 11
                with self.assertRaises(ValueError):
                    self.controller.remember('meshtastic', 'sfnarrow', preview)
                self.assertEqual(self.controller.data['bindings'], [])

    def test_existing_binding_cannot_be_resized_or_recentered_by_reapproval(self):
        self.stable()
        self.approve()
        original = deepcopy(self.controller.data['bindings'])
        preview = self.controller.preview_area('meshtastic')
        for changes in (dict(lat=self.move(.01)[0]), dict(radius_km=3.0), dict(label='Moved circle')):
            with self.subTest(changes=changes):
                altered = dict(preview, **changes)
                with self.assertRaises(ValueError):
                    self.controller.remember('meshtastic', 'longfast', altered)
                self.assertEqual(self.controller.data['bindings'], original)

    def test_reapproval_does_not_creep_the_original_circle(self):
        self.stable()
        self.approve()
        original = deepcopy(self.controller.data['bindings'][0])
        self.clock.now += 10
        self.controller.update(self.fix(self.move(.5)))
        preview = self.controller.preview_area('meshtastic')
        self.controller.remember('meshtastic', 'longfast', preview)
        changed = self.controller.data['bindings'][0]
        self.assertEqual(changed['profile'], 'longfast')
        for field in ('id', 'lat', 'lon', 'radius_km'):
            self.assertEqual(changed[field], original[field])

    def test_conflicting_overlap_is_manual_and_same_profile_overlap_is_unambiguous(self):
        self.stable()
        self.approve()
        self.clock.now += 60
        self.stable(self.move(3))
        self.approve(key='longfast')
        self.clock.now += 60
        self.stable(self.move(1.5))
        self.assertIsNone(self.controller.target('meshtastic'))
        self.assertIn('Conflicting assignments', self.controller.status('meshtastic')['reason'])
        self.controller.data['bindings'][1]['profile'] = 'sfnarrow'
        self.assertEqual(self.controller.target('meshtastic'), 'sfnarrow')

    def test_another_circle_boundary_prevents_inner_circle_auto_selection(self):
        self.stable()
        self.approve()
        overlapping = dict(self.controller.data['bindings'][0], id='second', lat=self.move(2)[0])
        self.controller.data['bindings'].append(overlapping)
        self.assertIsNone(self.controller.target('meshtastic'))
        self.assertIn('Local area boundary', self.controller.status('meshtastic')['reason'])

    def test_tampered_us_profile_with_es_country_cannot_apply_in_europe(self):
        self.stable()
        self.approve()
        stored = deepcopy(self.controller.data)
        stored['bindings'][0].update(profile='us_longfast', region='US', country='ES')
        self.controller.path.write_text(json.dumps(stored))
        restored = self.new_controller()
        self.stable(controller=restored)
        self.assertIsNone(restored.target('meshtastic'))
        self.assertEqual(restored.status('meshtastic')['active_profile'], 'sfnarrow')

    def test_manual_is_always_available_without_fix_and_survives_restart(self):
        self.controller.set_manual('meshtastic', 'longfast')
        self.assertEqual(self.controller.target('meshtastic'), 'longfast')
        self.controller.applied('meshtastic', 'longfast')
        restored = self.new_controller()
        self.assertEqual(restored.target('meshtastic'), 'longfast')
        self.assertEqual(restored.status('meshtastic')['mode'], 'manual')
        self.assertEqual(restored.status('meshtastic')['active_profile'], 'longfast')

    def test_auto_to_manual_is_not_overwritten_by_new_gps_updates(self):
        self.stable()
        self.approve()
        self.controller.set_manual('meshtastic', 'longfast')
        self.clock.now += 5
        self.controller.update(self.fix())
        self.assertEqual(self.controller.target('meshtastic'), 'longfast')
        self.controller.update(None)
        self.assertEqual(self.controller.target('meshtastic'), 'longfast')

    def test_mode_and_approval_persist_but_restart_does_not_reuse_old_fix(self):
        self.stable()
        self.approve()
        restored = self.new_controller()
        self.assertEqual(restored.status('meshtastic')['mode'], 'auto')
        self.assertEqual(len(restored.data['bindings']), 1)
        self.assertIsNone(restored.target('meshtastic'))
        self.stable(controller=restored)
        self.assertEqual(restored.target('meshtastic'), 'sfnarrow')

    def test_ui_translation_preserves_saved_values_and_user_area_label(self):
        self.stable()
        label = 'Mój prywatny obszar — kanał użytkownika'
        preview = dict(self.controller.preview_area('meshtastic'), label=label)
        self.controller.remember('meshtastic', 'sfnarrow', preview)
        self.controller.set_auto('meshtastic')
        stored = json.loads(self.controller.path.read_text())
        self.assertEqual(stored['protocols']['meshtastic']['mode'], 'auto')
        self.assertEqual(stored['protocols']['meshcore']['mode'], 'manual')
        self.assertEqual(stored['protocols']['meshtastic']['manual_profile'], 'sfnarrow')
        self.assertEqual(stored['bindings'][0]['profile'], 'sfnarrow')
        self.assertEqual(stored['bindings'][0]['label'], label)
        restored = self.new_controller()
        self.assertEqual(restored.data, stored)
        self.stable(controller=restored)
        self.assertIn(label, restored.status('meshtastic')['reason'])
        self.assertTrue(restored.status('meshtastic')['reason'].startswith('Approved area: '))
        self.assertEqual(restored.target('meshtastic'), 'sfnarrow')

    def test_meshcore_and_meshtastic_approvals_and_manual_choices_are_separate(self):
        self.stable()
        self.approve('meshtastic', 'sfnarrow')
        self.approve('meshcore', 'eu_uk_narrow_v2')
        self.assertEqual(self.controller.target('meshtastic'), 'sfnarrow')
        self.assertEqual(self.controller.target('meshcore'), 'eu_uk_narrow_v2')
        self.controller.set_manual('meshcore', 'eu_uk_narrow')
        self.assertEqual(self.controller.target('meshcore'), 'eu_uk_narrow')
        self.assertEqual(self.controller.target('meshtastic'), 'sfnarrow')
        with self.assertRaises(ValueError): self.controller.set_manual('meshcore', 'sfnarrow')

    def test_gps_region_cannot_approve_a_profile_from_a_different_band(self):
        self.stable()
        with self.assertRaisesRegex(ValueError, 'RF profile does not match the GPS region'):
            self.controller.remember('meshtastic', 'us_longfast', self.controller.preview_area('meshtastic'))
        self.assertEqual(self.controller.data['bindings'], [])

    def test_backend_acknowledgment_is_separate_from_preferred_profile(self):
        self.controller.set_manual('meshtastic', 'longfast')
        self.assertEqual(self.controller.status('meshtastic')['active_profile'], 'sfnarrow')
        self.controller.applied('meshtastic', 'longfast')
        self.assertEqual(self.controller.status('meshtastic')['active_profile'], 'longfast')

    def test_removing_local_approval_does_not_modify_active_radio_preference(self):
        self.stable()
        self.approve()
        self.controller.forget_area('meshtastic', self.controller.preview_area('meshtastic'))
        self.assertIsNone(self.controller.target('meshtastic'))
        self.assertEqual(self.controller.status('meshtastic')['active_profile'], 'sfnarrow')

    def test_corrupted_configuration_is_preserved_and_cannot_be_overwritten(self):
        self.controller.path.write_text('[]')
        restored = self.new_controller()
        self.assertTrue(restored.error)
        with self.assertRaises(ValueError): restored.set_auto('meshtastic')
        self.assertEqual(self.controller.path.read_text(), '[]')

    def test_failed_persistence_rolls_back_policy_in_memory(self):
        before = deepcopy(self.controller.data)
        with patch.object(self.controller, '_save', side_effect=OSError('synthetic disk failure')):
            with self.assertRaises(OSError): self.controller.set_auto('meshtastic')
        self.assertEqual(self.controller.data, before)

    def test_catalog_entries_are_protocol_scoped_and_have_verification_metadata(self):
        for protocol in ('meshcore', 'meshtastic'):
            for key in self.controller.choices(protocol):
                profile = PROFILES[key]
                self.assertEqual(profile['protocol'], protocol)
                self.assertTrue(profile['source'])
                self.assertEqual(profile['verified_on'], '2026-10-29')


if __name__ == '__main__': unittest.main()
