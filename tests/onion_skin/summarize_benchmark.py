# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Summarize native telemetry and paired overlay-on/off modal redraw cadence."""

import argparse
from array import array
import json
import math
import re
import struct
import sys
from pathlib import Path


QUIET_STATES = ('baseline', 'hold_cancel', 'after_cancel',
                'hold_confirm', 'after_confirm', 'after_undo')
GEOMETRY_EPSILON = 1e-5  # Maximum Euclidean displacement per vertex, in world-space units.
MAX_DUMP_BYTES = 256 * 1024 * 1024


def read_geometry_dump(path, expected_phase):
    """Read native ONIONSK1 little-endian data with explicit bounds and topology checks."""
    size = path.stat().st_size
    if size > MAX_DUMP_BYTES:
        raise ValueError('Geometry dump exceeds the 256 MiB diagnostic limit')
    with path.open('rb') as stream:
        remaining = size

        def read(size):
            nonlocal remaining
            if size < 0 or size > remaining:
                raise ValueError('Truncated geometry dump')
            result = stream.read(size)
            remaining -= size
            if len(result) != size:
                raise ValueError('Truncated geometry dump')
            return result

        if read(8) != b'ONIONSK1':
            raise ValueError('Unsupported geometry dump format')
        phase, count, generation, epoch = struct.unpack('<IIQQ', read(24))
        if phase >= len(QUIET_STATES) or QUIET_STATES[phase] != expected_phase:
            raise ValueError('Geometry dump belongs to another quiet phase')
        if not 0 < count <= 4096:
            raise ValueError('Invalid submitted sample count')
        samples = []
        identities = set()
        for _ in range(count):
            uid, frame, offset, name_size, vertices, triangles = struct.unpack('<QffIQQ', read(36))
            if not math.isfinite(frame) or not math.isfinite(offset) or not 0 < name_size <= 1024:
                raise ValueError('Invalid sample identity')
            if not vertices or not triangles or 12 * (vertices + triangles) > remaining - name_size:
                raise ValueError('Invalid or truncated geometry counts')
            name = read(name_size).decode('utf-8')
            identity = (name, frame, offset)
            if identity in identities:
                raise ValueError('Duplicate submitted mesh/frame/offset sample')
            identities.add(identity)
            positions = array('f')
            positions.frombytes(read(vertices * 12))
            topology = array('I')
            topology.frombytes(read(triangles * 12))
            if sys.byteorder != 'little':
                positions.byteswap()
                topology.byteswap()
            if not all(math.isfinite(value) for value in positions):
                raise ValueError('Non-finite vertex coordinate')
            if any(index >= vertices for index in topology):
                raise ValueError('Triangle index exceeds vertex count')
            samples.append({'mesh': name, 'object_uid': uid, 'frame': frame, 'offset': offset,
                            'positions': positions, 'topology': topology})
        if remaining:
            raise ValueError('Unexpected trailing geometry data')
    return {'phase': expected_phase, 'generation': generation, 'epoch': epoch, 'samples': samples}


def compare_geometry_dumps(left, right):
    """Require identical sample identities/topology and at most 1e-5 world-unit vertex movement.

    Names survive undo's ID remapping. Vertex and triangle order remain exact; only sample
    ordering and source session UIDs may differ. Quantized hashes cannot measure a tolerance:
    arbitrarily small movements can cross their rounding boundaries.
    """
    result = {'epsilon': GEOMETRY_EPSILON, 'passed': False}
    identity = lambda sample: (sample['mesh'], sample['frame'], sample['offset'])
    a = {identity(sample): sample for sample in left['samples']}
    b = {identity(sample): sample for sample in right['samples']}
    if len(left['samples']) != len(right['samples']) or a.keys() != b.keys():
        return dict(result, error='Submitted sample count or mesh/frame/offset identity differs')
    count = 0
    sum_squared = 0.0
    maximum_squared = 0.0
    maximum_component = 0.0
    for key in a:
        before, after = a[key], b[key]
        if before['topology'] != after['topology']:
            return dict(result, error='Triangle topology or order differs', sample=list(key))
        x, y = before['positions'], after['positions']
        if len(x) != len(y):
            return dict(result, error='Vertex count differs', sample=list(key))
        for index in range(0, len(x), 3):
            delta = [float(y[index + axis]) - float(x[index + axis]) for axis in range(3)]
            squared = sum(value * value for value in delta)
            maximum_squared = max(maximum_squared, squared)
            maximum_component = max(maximum_component, *(abs(value) for value in delta))
            sum_squared += squared
            count += 1
    maximum = math.sqrt(maximum_squared)
    result.update(max_displacement=maximum,
                  rms_displacement=math.sqrt(sum_squared / count) if count else None,
                  max_component_delta=maximum_component, vertices=count, samples=len(a),
                  passed=bool(count) and maximum <= GEOMETRY_EPSILON)
    return result


def numeric_geometry_validation(directory, report):
    result = {'enabled': bool(report.get('geometry_dump_enabled')), 'complete': False,
              'epsilon': GEOMETRY_EPSILON, 'comparisons': {}}
    if not result['enabled']:
        return result, {}
    dumps = {}
    try:
        folder = (directory / report['geometry_dump_directory']).resolve()
        if not folder.is_relative_to(directory.resolve()):
            raise ValueError('Geometry directory is outside this run')
        for phase in QUIET_STATES:
            dumps[phase] = read_geometry_dump(folder / (phase + '.bin'), phase)
        pairs = {'cancel_restores_baseline_geometry': ('baseline', 'after_cancel'),
                 'undo_restores_baseline_geometry': ('baseline', 'after_undo'),
                 'confirmed_geometry_matches_provisional_geometry': ('hold_confirm', 'after_confirm')}
        for name, (left, right) in pairs.items():
            result['comparisons'][name] = compare_geometry_dumps(dumps[left], dumps[right])
        if report.get('geometry_expectation') == 'stable':
            for phase in QUIET_STATES[1:]:
                result['comparisons']['stable_' + phase] = compare_geometry_dumps(
                    dumps['baseline'], dumps[phase])
        result['complete'] = True
        result['states'] = {phase: {'generation': data['generation'], 'epoch': data['epoch'],
                                    'samples': len(data['samples'])} for phase, data in dumps.items()}
    except (OSError, KeyError, TypeError, ValueError, struct.error) as error:
        result['error'] = str(error)
    return result, dumps


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
    last_sampling = None
    sampling_marks = {}
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
        if row.startswith('mesh_onion_skin_frames '):
            frames = re.search(r'\bframes=([^\s]+)', row)
            offsets = re.search(r'\boffsets=([^\s]+)', row)
            if frames and offsets:
                try:
                    frame_values = [float(value) for value in frames.group(1).split(',')]
                    offset_values = [float(value) for value in offsets.group(1).split(',')]
                    if len(frame_values) == len(offset_values) and frame_values:
                        last_sampling = {'pairs': list(zip(frame_values, offset_values)), 'phase': phase}
                except ValueError:
                    last_sampling = None
        if row.startswith('ONION_GEOMETRY_MARK '):
            geometry_marks[row.split()[1]] = last_geometry
            sampling_marks[row.split()[1]] = last_sampling
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
    numeric_validation, geometry_dumps = numeric_geometry_validation(directory, report)
    if numeric_validation['complete']:
        sampling_marks = {name: {'pairs': [(sample['frame'], sample['offset'])
                                           for sample in data['samples']],
                                 'phase': 'warmup' if name == 'baseline' else name,
                                 'source': 'native_geometry_dump'}
                          for name, data in geometry_dumps.items()}
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
        if numeric_validation['enabled']:
            geometry_checks['all_numeric_quiet_states_available'] = numeric_validation['complete']
            geometry_checks['all_quiet_states_drawn'] = numeric_validation['complete'] and all(
                geometry_marks.get(name) is not None for name in required)
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
            if numeric_validation['complete']:
                geometry_checks['sample_visibility_preserved'] = all(
                    len(geometry_dumps[name]['samples']) == expected_visible_samples for name in required)
                for name, comparison in numeric_validation['comparisons'].items():
                    if not name.startswith('stable_'):
                        geometry_checks[name] = comparison['passed']
                if expectation == 'stable':
                    geometry_checks['fully_keyed_samples_remain_unchanged'] = all(
                        comparison['passed'] for name, comparison in
                        numeric_validation['comparisons'].items() if name.startswith('stable_'))
    geometry_passed = bool(geometry_checks) and all(geometry_checks.values())
    sampling_checks = {}
    if report.get('expected_frames') is not None:
        required = ('baseline', 'hold_cancel', 'after_cancel',
                    'hold_confirm', 'after_confirm', 'after_undo')
        phases = {'baseline': {'warmup'}, 'hold_cancel': {'move_cancel', 'hold_cancel'},
                  'hold_confirm': {'move_confirm', 'hold_confirm'},
                  'after_cancel': {'after_cancel'}, 'after_confirm': {'after_confirm'},
                  'after_undo': {'after_undo'}}
        sampling_checks['all_quiet_states_submitted_samples'] = all(
            sampling_marks.get(name) is not None and sampling_marks[name]['phase'] in phases[name]
            for name in required)
        if sampling_checks['all_quiet_states_submitted_samples']:
            expected = {(round(frame, 5), round(offset, 5)) for frame, offset in
                        zip(report['expected_frames'], report['expected_offsets'])}
            sampling_checks['native_frames_and_offsets_match_fixture_oracle'] = all(
                {(round(frame, 5), round(offset, 5)) for frame, offset in sampling_marks[name]['pairs']}
                == expected for name in required)
            expected_count = report.get('expected_visible_samples', report['samples'])
            sampling_checks['native_submitted_sample_count_matches'] = all(
                len(sampling_marks[name]['pairs']) == expected_count for name in required)
    sampling_passed = bool(sampling_checks) and all(sampling_checks.values())
    metrics['sampling_validation'] = {'checks': sampling_checks, 'marks': sampling_marks,
                                      'passed': sampling_passed if sampling_checks else None}
    metrics['geometry_validation'] = {'expectation': expectation,
                                      'quantum': report.get('geometry_quantum'),
                                      'checks': geometry_checks,
                                      'marks': geometry_marks,
                                      'numeric': numeric_validation,
                                      'distinct_drag_hashes': {phase: len(hashes)
                                                               for phase, hashes in drag_geometry.items()},
                                      'passed': geometry_passed if expectation != 'off' else None}
    metrics['checks_passed'] = source_checks_passed and (expectation == 'off' or geometry_passed) and (
        report.get('expected_frames') is None or sampling_passed)
    metrics['performance_measurement_eligible'] = expectation == 'off' and not report.get(
        'geometry_diagnostics_enabled', False) and not numeric_validation['enabled']
    metrics['latency_scope'] = 'continuous modal dragging, through native draw submission'
    metrics['error'] = report.get('error')
    metrics['enabled'] = report['enabled']
    metrics['samples'] = report['samples']
    return metrics


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('directories', type=Path, nargs='+')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = {str(directory): summarize(directory) for directory in args.directories}
    args.output.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result, indent=2))
    if any(not metrics['checks_passed'] for metrics in result.values()):
        sys.exit(1)


if __name__ == '__main__':
    main()
