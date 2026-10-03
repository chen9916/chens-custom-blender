# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Check that publication timing cannot masquerade as changed/restored mesh geometry."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class GeometrySummaryTest(unittest.TestCase):
    def summarize(self, expectation, *, moving=True, restored=True, diagnostics=True,
                  fresh_cancel=True, visible_samples=4, expected_visible_samples=4, source_passed=True):
        with tempfile.TemporaryDirectory(prefix='onion-geometry-summary-') as temporary:
            root = Path(temporary)
            run = root / 'run'
            run.mkdir()
            report = {'checks': [{'passed': source_passed}] * 13, 'draw_times': [],
                      'enabled': True, 'samples': 4, 'geometry_expectation': expectation,
                      'expected_visible_samples': expected_visible_samples,
                      'geometry_quantum': 1e-5}
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
            geometry(3 if moving else 1)
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


if __name__ == '__main__':
    unittest.main()
