# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Check that publication timing cannot masquerade as changed/restored mesh geometry."""

import json
import importlib.util
from pathlib import Path
import subprocess
import struct
import sys
import tempfile
import unittest

_spec = importlib.util.spec_from_file_location(
    'onion_benchmark_summary', Path(__file__).with_name('summarize_benchmark.py'))
_summary = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_summary)


def write_dump(path, phase, *, coordinate=0.0, uid=1, mesh='Mesh', count=1, topology=(0, 1, 2)):
    """Independent fixture encoder for the documented native binary format."""
    output = bytearray(b'ONIONSK1')
    output.extend(struct.pack('<IIQQ', _summary.QUIET_STATES.index(phase), count, 7, 3))
    for index in range(count):
        name = (mesh + str(index)).encode('utf-8')
        output.extend(struct.pack('<QffIQQ', uid + index, 13.0, -4.0, len(name), 3, 1))
        output.extend(name)
        output.extend(struct.pack('<9f', coordinate, 0, 0, 1, 0, 0, 0, 1, 0))
        output.extend(struct.pack('<3I', *topology))
    path.write_bytes(output)


class NumericGeometryTest(unittest.TestCase):
    def compare(self, left_coordinate=0, right_coordinate=0, **right_options):
        with tempfile.TemporaryDirectory(prefix='onion-vertices-') as temporary:
            left = Path(temporary) / 'hold_confirm.bin'
            right = Path(temporary) / 'after_confirm.bin'
            write_dump(left, 'hold_confirm', coordinate=left_coordinate)
            write_dump(right, 'after_confirm', coordinate=right_coordinate, **right_options)
            return _summary.compare_geometry_dumps(
                _summary.read_geometry_dump(left, 'hold_confirm'),
                _summary.read_geometry_dump(right, 'after_confirm'))

    def test_sub_epsilon_quantization_boundary_passes(self):
        before, after = 4.9e-6, 5.1e-6
        self.assertNotEqual(round(before / 1e-5), round(after / 1e-5))
        result = self.compare(before, after)
        self.assertTrue(result['passed'])
        self.assertLess(result['max_displacement'], 3e-7)
        self.assertAlmostEqual(result['rms_displacement'], result['max_displacement'] / 3 ** 0.5)

    def test_large_vertex_delta_fails(self):
        result = self.compare(0, 2e-5)
        self.assertFalse(result['passed'])
        self.assertGreater(result['max_displacement'], result['epsilon'])

    def test_missing_submitted_sample_fails(self):
        result = self.compare(count=2)
        self.assertFalse(result['passed'])
        self.assertIn('sample count', result['error'])

    def test_triangle_order_change_fails(self):
        result = self.compare(topology=(0, 2, 1))
        self.assertFalse(result['passed'])
        self.assertIn('topology', result['error'])

    def test_undo_uid_remapping_preserves_named_mesh_identity(self):
        self.assertTrue(self.compare(uid=999)['passed'])
        self.assertFalse(self.compare(mesh='Different')['passed'])

    def test_wrong_phase_and_truncation_are_rejected(self):
        with tempfile.TemporaryDirectory(prefix='onion-vertices-') as temporary:
            path = Path(temporary) / 'data.bin'
            write_dump(path, 'baseline')
            with self.assertRaisesRegex(ValueError, 'another quiet phase'):
                _summary.read_geometry_dump(path, 'after_undo')
            path.write_bytes(path.read_bytes()[:-1])
            with self.assertRaisesRegex(ValueError, 'truncated'):
                _summary.read_geometry_dump(path, 'baseline')


class GeometrySummaryTest(unittest.TestCase):
    def summarize(self, expectation, *, moving=True, restored=True, diagnostics=True,
                  fresh_cancel=True, visible_samples=4, expected_visible_samples=4, source_passed=True,
                  sampling=False, correct_offsets=True, numeric=False, numeric_delta=0.0,
                  numeric_missing=False, numeric_hash_mismatch=True):
        with tempfile.TemporaryDirectory(prefix='onion-geometry-summary-') as temporary:
            root = Path(temporary)
            run = root / 'run'
            run.mkdir()
            report = {'checks': [{'passed': source_passed}] * 13, 'draw_times': [],
                      'enabled': True, 'samples': 4, 'geometry_expectation': expectation,
                      'expected_visible_samples': expected_visible_samples,
                      'geometry_quantum': 1e-5}
            if sampling:
                report.update(expected_frames=[27, 29, 3, 5], expected_offsets=[-4, -2, 2, 4])
            if numeric:
                report.update(geometry_dump_enabled=True, geometry_dump_directory='geometry')
                geometry = run / 'geometry'
                geometry.mkdir()
                held = 0.1 if moving else 0.0
                values = {'baseline': 0.0, 'hold_cancel': held, 'after_cancel': 0.0,
                          'hold_confirm': held, 'after_confirm': held + numeric_delta, 'after_undo': 0.0}
                for phase_name, coordinate in values.items():
                    if numeric_missing and phase_name == 'after_confirm':
                        continue
                    write_dump(geometry / (phase_name + '.bin'), phase_name,
                               coordinate=coordinate, count=visible_samples)
            (run / 'report.json').write_text(json.dumps(report), encoding='utf-8')
            rows = []
            generation = 0

            def phase(name):
                rows.append('ONION_PHASE ' + name)

            def geometry(value):
                nonlocal generation
                generation += 1
                if diagnostics:
                    rows.append(f'mesh_onion_skin_geometry generation={generation} '
                                f'geometry_hash={value:016x} visible_samples={visible_samples}')
                    if sampling:
                        offsets = '-4,-2,2,4' if correct_offsets else '26,28,2,4'
                        rows.append(f'mesh_onion_skin_frames generation={generation} '
                                    f'frames=27,29,3,5 offsets={offsets}')

            def mark(name):
                rows.append('ONION_GEOMETRY_MARK ' + name)

            geometry(1)
            mark('baseline')
            phase('move_cancel')
            geometry(2 if moving else 1)
            geometry(3 if moving else 1)
            phase('hold_cancel')
            mark('hold_cancel')
            phase('after_cancel')
            if fresh_cancel:
                geometry(1 if restored else 4)
            mark('after_cancel')
            phase('move_confirm')
            geometry(2 if moving else 1)
            geometry(3 if moving else 1)
            phase('hold_confirm')
            mark('hold_confirm')
            phase('after_confirm')
            geometry(4 if numeric and numeric_hash_mismatch else 3 if moving else 1)
            mark('after_confirm')
            phase('after_undo')
            geometry(1)
            mark('after_undo')
            run.with_suffix('.log').write_text('\n'.join(rows), encoding='utf-8')
            output = root / 'summary.json'
            result = subprocess.run([sys.executable,
                                     str(Path(__file__).with_name('summarize_benchmark.py')),
                                     str(run), '--output', str(output)],
                                    capture_output=True, text=True, check=False)
            summary = json.loads(output.read_text(encoding='utf-8'))[str(run)]
            return result.returncode, summary

    def test_sparse_changes_and_restoration_pass(self):
        code, summary = self.summarize('change')
        self.assertEqual(code, 0)
        self.assertTrue(summary['checks_passed'])
        self.assertTrue(summary['ghost_geometry_changes_observed'])

    def test_fully_keyed_static_samples_pass(self):
        code, summary = self.summarize('stable', moving=False)
        self.assertEqual(code, 0)
        self.assertTrue(summary['geometry_validation']['passed'])

    def test_unchanged_sparse_samples_fail(self):
        code, summary = self.summarize('change', moving=False)
        self.assertEqual(code, 1)
        self.assertFalse(summary['geometry_validation']['passed'])

    def test_failed_cancel_restoration_fails(self):
        code, summary = self.summarize('change', restored=False)
        self.assertEqual(code, 1)
        self.assertFalse(summary['geometry_validation']['checks']['cancel_restores_baseline_geometry'])

    def test_missing_diagnostics_fail(self):
        code, summary = self.summarize('change', diagnostics=False)
        self.assertEqual(code, 1)
        self.assertFalse(summary['geometry_validation']['checks']['all_quiet_states_drawn'])

    def test_old_baseline_observation_does_not_prove_cancel_drew(self):
        code, summary = self.summarize('stable', moving=False, fresh_cancel=False)
        self.assertEqual(code, 1)
        self.assertFalse(summary['geometry_validation']['checks']['all_quiet_states_drawn'])

    def test_missing_requested_samples_fail(self):
        code, summary = self.summarize('change', visible_samples=3)
        self.assertEqual(code, 1)
        self.assertFalse(summary['geometry_validation']['checks']['sample_visibility_preserved'])

    def test_source_failure_causes_nonzero_exit(self):
        code, summary = self.summarize('off', source_passed=False)
        self.assertEqual(code, 1)
        self.assertFalse(summary['checks_passed'])

    def test_multiple_mesh_fixture_counts_all_submitted_samples(self):
        code, summary = self.summarize('change', visible_samples=8, expected_visible_samples=8)
        self.assertEqual(code, 0)
        self.assertTrue(summary['geometry_validation']['checks']['sample_visibility_preserved'])

    def test_geometry_run_is_ineligible_for_performance_comparison(self):
        code, summary = self.summarize('change')
        self.assertEqual(code, 0)
        self.assertFalse(summary['performance_measurement_eligible'])

    def test_wrapped_sampling_uses_explicit_temporal_offset_oracle(self):
        code, summary = self.summarize('stable', moving=False, sampling=True)
        self.assertEqual(code, 0)
        self.assertTrue(summary['sampling_validation']['passed'])

    def test_evaluated_frame_difference_is_not_wrapped_temporal_offset(self):
        code, summary = self.summarize('stable', moving=False, sampling=True, correct_offsets=False)
        self.assertEqual(code, 1)
        self.assertFalse(summary['sampling_validation']['passed'])

    def test_native_sampling_diagnostics_are_required_for_oracle_checks(self):
        code, summary = self.summarize('off', sampling=True, diagnostics=False)
        self.assertEqual(code, 1)
        self.assertFalse(summary['sampling_validation']['passed'])

    def test_numeric_parity_accepts_small_delta_despite_different_quantized_hash(self):
        code, summary = self.summarize('change', numeric=True, numeric_delta=2e-7)
        self.assertEqual(code, 0)
        validation = summary['geometry_validation']
        self.assertNotEqual(validation['marks']['hold_confirm']['hash'],
                            validation['marks']['after_confirm']['hash'])
        self.assertTrue(validation['checks']['confirmed_geometry_matches_provisional_geometry'])
        self.assertFalse(summary['performance_measurement_eligible'])

    def test_numeric_parity_rejects_large_vertex_delta(self):
        code, summary = self.summarize('change', numeric=True, numeric_delta=2e-5)
        self.assertEqual(code, 1)
        self.assertFalse(summary['geometry_validation']['checks'][
            'confirmed_geometry_matches_provisional_geometry'])

    def test_missing_numeric_state_cannot_fall_back_to_matching_hashes(self):
        code, summary = self.summarize('change', numeric=True, numeric_missing=True,
                                       numeric_hash_mismatch=False)
        self.assertEqual(code, 1)
        self.assertFalse(summary['geometry_validation']['numeric']['complete'])


if __name__ == '__main__':
    unittest.main()
