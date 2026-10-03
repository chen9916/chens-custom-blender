# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Summarize native telemetry and paired overlay-on/off modal redraw cadence."""

import argparse
import json
import math
import re
import sys
from pathlib import Path


def percentiles(values):
    if not values:
        return None
    values = sorted(values)
    def percentile(fraction):
        return values[min(len(values) - 1, math.ceil(len(values) * fraction) - 1)]
    return {'count': len(values), 'median': percentile(0.5), 'p95': percentile(0.95),
            'max': max(values)}


def summarize(directory):
    report = json.loads((directory / 'report.json').read_text(encoding='utf-8'))
    text = directory.with_suffix('.log').read_text(encoding='utf-8', errors='replace')
    phase = 'warmup'
    drag_rows = []
    last_geometry = None
    geometry_marks = {}
    drag_geometry = {'move_cancel': set(), 'move_confirm': set()}
    for row in text.splitlines():
        if row.startswith('ONION_PHASE '):
            phase = row.split(' ', 1)[1]
        if phase in ('move_cancel', 'move_confirm'):
            drag_rows.append(row)
        if row.startswith('mesh_onion_skin_geometry '):
            fingerprint = re.search(r'\bgeometry_hash=([0-9a-fA-F]+)', row)
            visible = re.search(r'\bvisible_samples=(\d+)', row)
            generation = re.search(r'\bgeneration=(\d+)', row)
            if fingerprint and visible and int(visible.group(1)) > 0:
                last_geometry = {'hash': fingerprint.group(1).lower(),
                                 'visible_samples': int(visible.group(1)),
                                 'generation': int(generation.group(1)) if generation else None,
                                 'phase': phase}
                if phase in drag_geometry:
                    drag_geometry[phase].add(last_geometry['hash'])
        if row.startswith('ONION_GEOMETRY_MARK '):
            geometry_marks[row.split()[1]] = last_geometry
    drag_text = '\n'.join(drag_rows)
    metrics = {}
    for name in ('staging_ms', 'worker_ms', 'input_to_publish_ms', 'input_to_draw_ms'):
        metrics[name] = percentiles([float(value) for value in
                                    re.findall(r'\b' + name + r'=([\d.]+)', drag_text)])
    for phase in ('move_cancel', 'move_confirm'):
        times = [value for event_phase, value in report['draw_times'] if event_phase == phase]
        intervals = [(right - left) * 1000 for left, right in zip(times, times[1:])]
        metrics[phase + '_redraw_interval_ms'] = percentiles(intervals)
    source_checks_passed = len(report['checks']) >= 13 and all(
        check['passed'] for check in report['checks']) and not report.get('error')
    metrics['ghost_updates_observed'] = bool(metrics['input_to_draw_ms'])
    metrics['ghost_geometry_changes_observed'] = any(len(hashes) > 1
                                                    for hashes in drag_geometry.values())
    expectation = report.get('geometry_expectation', 'off')
    geometry_checks = {}
    if expectation != 'off':
        required = ('baseline', 'hold_cancel', 'after_cancel',
                    'hold_confirm', 'after_confirm', 'after_undo')
        accepted_phases = {'baseline': {'warmup'},
                           'hold_cancel': {'move_cancel', 'hold_cancel'},
                           'hold_confirm': {'move_confirm', 'hold_confirm'},
                           'after_cancel': {'after_cancel'},
                           'after_confirm': {'after_confirm'},
                           'after_undo': {'after_undo'}}
        geometry_checks['all_quiet_states_drawn'] = all(
            geometry_marks.get(name) is not None and
            geometry_marks[name]['phase'] in accepted_phases[name] for name in required)
        if geometry_checks['all_quiet_states_drawn']:
            baseline = geometry_marks['baseline']['hash']
            expected_visible_samples = report.get('expected_visible_samples', report['samples'])
            geometry_checks['sample_visibility_preserved'] = all(
                geometry_marks[name]['visible_samples'] == expected_visible_samples for name in required)
            geometry_checks['cancel_restores_baseline_geometry'] = \
                geometry_marks['after_cancel']['hash'] == baseline
            geometry_checks['undo_restores_baseline_geometry'] = \
                geometry_marks['after_undo']['hash'] == baseline
            geometry_checks['confirmed_geometry_matches_provisional_geometry'] = \
                geometry_marks['after_confirm']['hash'] == geometry_marks['hold_confirm']['hash']
            if expectation == 'change':
                geometry_checks['cancel_drag_changes_ghost_geometry'] = \
                    geometry_marks['hold_cancel']['hash'] != baseline
                geometry_checks['confirm_drag_changes_ghost_geometry'] = \
                    geometry_marks['hold_confirm']['hash'] != baseline
                geometry_checks['continuous_cancel_drag_changes_geometry'] = \
                    len(drag_geometry['move_cancel']) > 1
                geometry_checks['continuous_confirm_drag_changes_geometry'] = \
                    len(drag_geometry['move_confirm']) > 1
            else:
                geometry_checks['fully_keyed_samples_remain_unchanged'] = all(
                    geometry_marks[name]['hash'] == baseline for name in required)
                geometry_checks['continuous_drag_samples_remain_unchanged'] = all(
                    len(hashes) <= 1 and (not hashes or baseline in hashes)
                    for hashes in drag_geometry.values())
    geometry_passed = bool(geometry_checks) and all(geometry_checks.values())
    metrics['geometry_validation'] = {'expectation': expectation,
                                      'quantum': report.get('geometry_quantum'),
                                      'checks': geometry_checks,
                                      'marks': geometry_marks,
                                      'distinct_drag_hashes': {phase: len(hashes)
                                                               for phase, hashes in drag_geometry.items()},
                                      'passed': geometry_passed if expectation != 'off' else None}
    metrics['checks_passed'] = source_checks_passed and (expectation == 'off' or geometry_passed)
    metrics['performance_measurement_eligible'] = expectation == 'off' and not report.get(
        'geometry_diagnostics_enabled', False)
    metrics['latency_scope'] = 'continuous modal dragging, through native draw submission'
    metrics['error'] = report.get('error')
    metrics['enabled'] = report['enabled']
    metrics['samples'] = report['samples']
    return metrics


parser = argparse.ArgumentParser()
parser.add_argument('directories', type=Path, nargs='+')
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
result = {str(directory): summarize(directory) for directory in args.directories}
args.output.write_text(json.dumps(result, indent=2), encoding='utf-8')
print(json.dumps(result, indent=2))
if any(not metrics['checks_passed'] for metrics in result.values()):
    sys.exit(1)
