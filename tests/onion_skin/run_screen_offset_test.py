# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Observe stable native ghost identities, rendered offsets and native playback.

Python changes viewport controls and reads native draw diagnostics/pixels only.
It neither evaluates ghost geometry nor draws ghosts, and never saves the asset.
Playback phases deliberately do not request redraws from Python: fractional
presentation must be driven by Blender's own viewport timer.
"""

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import time
import traceback

import bpy
import gpu
import numpy as np
from bpy_extras import view3d_utils
from mathutils import Quaternion, Vector

parser = argparse.ArgumentParser()
parser.add_argument('--output', type=Path, required=True)
parser.add_argument('--log', type=Path, required=True)
args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
args.output.mkdir(parents=True, exist_ok=True)
window = bpy.context.window
area = next(a for a in window.screen.areas if a.type == 'VIEW_3D')
region = next(r for r in area.regions if r.type == 'WINDOW')
view = area.spaces.active
overlay = view.overlay
scene = bpy.context.scene
rig = bpy.data.objects['Armature']
assert rig.mode == 'POSE'
bpy.context.preferences.filepaths.use_auto_save_temporary_files = False
scene.frame_start, scene.frame_end = 1, 22
scene.frame_set(13)
scene.render.fps = 12
scene.render.fps_base = 1.0
overlay.show_mesh_onion_skin = True
overlay.mesh_onion_skin_before = overlay.mesh_onion_skin_after = 2
overlay.mesh_onion_skin_step = 2
overlay.mesh_onion_skin_keyed_frames_only = False
overlay.mesh_onion_skin_loop_frame_range = False
overlay.mesh_onion_skin_distance_fade = False
overlay.mesh_onion_skin_in_front = True
overlay.mesh_onion_skin_opacity = 0.7
overlay.mesh_onion_skin_screen_offset = True
overlay.mesh_onion_skin_screen_offset_reverse = False
overlay.mesh_onion_skin_screen_offset_spacing = 24.0
overlay.mesh_onion_skin_screen_offset_world_space = False
overlay.show_bones = False
overlay.show_floor = overlay.show_axis_x = overlay.show_axis_y = False
overlay.show_extras = overlay.show_relationship_lines = False
view.show_gizmo = False
view.shading.type = 'SOLID'
view.shading.color_type = 'SINGLE'
view.shading.single_color = (0.5, 0.5, 0.5)
view.region_3d.view_distance *= 1.5


def source_state():
    action = rig.animation_data.action
    bag = action.layers[0].strips[0].channelbag(rig.animation_data.action_slot)
    values = [(f.data_path, f.array_index, [(tuple(k.co), tuple(k.handle_left),
                                           tuple(k.handle_right), k.interpolation)
                                          for k in f.keyframe_points]) for f in bag.fcurves]
    return (hashlib.sha256(repr(values).encode()).hexdigest(), rig.users,
            rig.data.users, action.users, len(bpy.data.objects), len(bpy.data.actions))


original = source_state()
report = {'build_hash': bpy.app.build_hash.decode(), 'checks': [], 'observations': {},
          'scope': 'native stable poses, center alignment, rendered sides, fractional scrolling, '
                   'camera-dependent scene-distance spacing, forward/backward native playback '
                   'and mixed viewport sampling'}
started = time.perf_counter()
state = {'phase': 'normal', 'ready': started + 1.0, 'deadline': started + 8.0, 'pixels': {}}


def check(label, condition):
    report['checks'].append({'name': label, 'passed': bool(condition)})
    assert condition, label


def transition(phase, delay=0.35):
    now = time.perf_counter()
    state.update(phase=phase, ready=now + delay, deadline=now + 8.0)
    print('ONION_SCREEN_PHASE', phase, flush=True)


def rows():
    text = args.log.read_text(encoding='utf-8', errors='replace') if args.log.exists() else ''
    marker = 'ONION_SCREEN_PHASE ' + state['phase']
    text = text.rsplit(marker, 1)[-1] if marker in text else text
    result = []
    for match in re.finditer(r'^mesh_onion_skin_screen (.+)$', text, re.MULTILINE):
        data = dict(re.findall(r'(\w+)=([^\s]+)', match.group(1)))
        if not {'native_frame', 'alpha', 'ranks', 'payloads', 'submitted', 'world_space',
                'distance', 'world_offsets'} <= data.keys():
            continue
        for key in ('frames', 'offsets', 'pixels', 'world_offsets', 'alpha', 'ranks'):
            data[key] = [float(value) for value in data[key].split(',')]
        for key in ('payloads', 'submitted'):
            data[key] = [int(value) for value in data[key].split(',')]
        for key in ('generation', 'epoch', 'enabled', 'reverse', 'vbo_uploads', 'batch_creates', 'world_space'):
            data[key] = int(data[key])
        for key in ('playhead', 'native_frame', 'center', 'spacing', 'distance'):
            data[key] = float(data[key])
        if all(len(data[key]) == len(data['frames']) for key in
               ('offsets', 'pixels', 'world_offsets', 'alpha', 'ranks', 'payloads', 'submitted')):
            result.append(data)
    return result


def samples(row):
    return {frame: {'offset': row['offsets'][i], 'pixel': row['pixels'][i],
                    'world_offset': row['world_offsets'][i],
                    'alpha': row['alpha'][i], 'rank': row['ranks'][i],
                    'payload': row['payloads'][i], 'submitted': row['submitted'][i]}
            for i, frame in enumerate(row['frames'])}


def observe_pixels():
    if bpy.context.area != area or state['phase'] not in (
            'normal', 'reverse', 'world_normal', 'world_reverse', 'world_zoom',
            'world_perspective', 'world_perspective_far', 'world_orbit'):
        return
    x, y, width, height = gpu.state.viewport_get()
    buffer = gpu.state.active_framebuffer_get().read_color(x, y, width, height, 4, 0, 'FLOAT')
    pixels = np.asarray(buffer)
    blue = (pixels[..., 2] > pixels[..., 0] + 0.08) & (pixels[..., 2] > pixels[..., 1] + 0.04)
    red = (pixels[..., 0] > pixels[..., 2] + 0.08) & (pixels[..., 0] > pixels[..., 1] + 0.04)
    blue_y, blue_x = np.nonzero(blue)
    red_y, red_x = np.nonzero(red)
    if blue_x.size > 100 and red_x.size > 100:
        state['pixels'][state['phase']] = {'past_x': float(blue_x.mean()),
                                           'future_x': float(red_x.mean()),
                                           'separation': float(abs(red_x.mean() - blue_x.mean())),
                                           'past_pixels': int(blue_x.size),
                                           'future_pixels': int(red_x.size)}


handler = bpy.types.SpaceView3D.draw_handler_add(observe_pixels, (), 'WINDOW', 'POST_PIXEL')


def cancel_playback():
    if window.screen.is_animation_playing:
        with bpy.context.temp_override(window=window, area=area, region=region):
            bpy.ops.screen.animation_cancel(restore_frame=False)


def finish():
    cancel_playback()
    for viewport in window.screen.areas:
        if viewport.type == 'VIEW_3D':
            viewport.spaces.active.overlay.show_mesh_onion_skin = False
    bpy.types.SpaceView3D.draw_handler_remove(handler, 'WINDOW')
    report['elapsed_seconds'] = time.perf_counter() - started
    report['passed'] = bool(report['checks']) and all(c['passed'] for c in report['checks']) and 'error' not in report
    (args.output / 'result.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print('ONION_SCREEN_RESULT', json.dumps(report), flush=True)
    bpy.ops.wm.quit_blender()
    return None


def pixel_law(row):
    sign = -1 if row['reverse'] else 1
    spacing = row['spacing'] if row['enabled'] and not row['world_space'] else 0.0
    return all(abs(pixel - offset * spacing * sign) < 2e-4
               for pixel, offset in zip(row['pixels'], row['offsets']))


def world_law(row):
    sign = -1 if row['reverse'] else 1
    distance = row['distance'] if row['enabled'] and row['world_space'] else 0.0
    return all(abs(world_offset - offset * distance * sign) < 2e-4
               for world_offset, offset in zip(row['world_offsets'], row['offsets']))


def center_visible(row, frame):
    value = samples(row).get(float(frame))
    return value is not None and abs(value['offset']) < 1e-5 and value['alpha'] > 0 and value['submitted']


def same_payloads(left, right):
    return {frame: value['payload'] for frame, value in samples(left).items()} == {
        frame: value['payload'] for frame, value in samples(right).items()}


def playback_observation(candidates, direction):
    phase = state['phase']
    valid = [row for row in candidates if row['enabled'] and not row['reverse']
             and abs(row['spacing'] - 43.5) < 1e-5]
    check(phase + ' receives native presentation draws', len(valid) >= 20)
    check(phase + ' uses signed screen pixels throughout', all(pixel_law(row) for row in valid))
    check(phase + ' displays subframe motion between animation ticks', sum(
        abs(row['playhead'] - row['native_frame']) > 1e-3 for row in valid) >= 8)
    held = {}
    for row in valid:
        held.setdefault(row['native_frame'], set()).add(round(row['playhead'], 4))
    check(phase + ' native redraws move ghosts between unchanged scene frames',
          sum(len(values) > 1 for values in held.values()) >= 4)
    check(phase + ' keeps boundary guards invisible', any(
        alpha == 0 and not submitted for row in valid
        for alpha, submitted in zip(row['alpha'], row['submitted'])))
    check(phase + ' fades boundary poses continuously', any(
        1e-3 < alpha < 0.699 for row in valid for alpha in row['alpha']))
    period = scene.frame_end - scene.frame_start + 1
    check(phase + ' crosses the inclusive scene loop boundary', any(
        abs(current['native_frame'] - previous['native_frame']) > period / 2
        for previous, current in zip(valid, valid[1:])))
    comparisons = publications = movements = centers = 0
    identities_ok = offsets_ok = pixels_ok = alpha_ok = direction_ok = True
    visits = {}
    for row in valid:
        for frame, value in samples(row).items():
            if value['alpha'] > 0 and value['submitted']:
                position = 'future' if value['offset'] > 0.3 else 'past' if value['offset'] < -0.3 else 'center'
                visits.setdefault(frame, set()).add(position)
                centers += position == 'center'
    for previous, current in zip(valid, valid[1:]):
        delta = current['playhead'] - previous['playhead']
        delta -= round(delta / period) * period
        if abs(delta) > 3 or previous['epoch'] != current['epoch']:
            continue
        if abs(delta) > 1e-4:
            direction_ok &= direction * delta >= -1e-3
        old = samples(previous)
        new = samples(current)
        for frame in old.keys() & new.keys():
            left, right = old[frame], new[frame]
            identities_ok &= left['payload'] == right['payload']
            # A canonical pose's distant alias can change laps outside the visible window.
            if abs(right['offset'] - left['offset']) > period / 2:
                continue
            comparisons += 1
            publications += previous['generation'] != current['generation']
            offsets_ok &= abs(right['offset'] - left['offset'] + delta) < 2e-4
            pixels_ok &= abs(right['pixel'] - left['pixel'] + delta * 43.5) < 0.01
            # Smoothstep's maximum derivative is 1.5, with configured alpha 0.7.
            alpha_ok &= abs(right['alpha'] - left['alpha']) <= 1.05 * abs(right['rank'] - left['rank']) + 0.002
            movements += abs(delta) > 1e-4 and left['alpha'] > 0 and right['alpha'] > 0
    check(phase + ' retains fixed-pose payloads across publications', identities_ok and publications >= 6)
    check(phase + ' scrolls fixed poses continuously across publications',
          offsets_ok and pixels_ok and comparisons >= 60 and movements >= 20)
    check(phase + ' pixel motion follows playback direction', direction_ok)
    check(phase + ' has no abrupt opacity changes for retained poses', alpha_ok)
    check(phase + ' retains poses through future-center-past alignment',
          centers > 0 and any({'future', 'center', 'past'} <= positions for positions in visits.values()))
    return {'rows': len(valid), 'native_frames': len(held),
            'frames_with_fractional_redraws': sum(len(values) > 1 for values in held.values()),
            'shared_pose_comparisons': comparisons, 'cross_publication_comparisons': publications,
            'visible_pose_movements': movements, 'near_center_draws': centers,
            'first': valid[0], 'last': valid[-1]}


def start_playback(direction):
    with bpy.context.temp_override(window=window, area=area, region=region):
        bpy.ops.screen.animation_play(reverse=direction < 0)
    transition('forward' if direction > 0 else 'backward', delay=2.2)


def tick():
    global region
    try:
        now = time.perf_counter()
        assert now - started < 30.0, 'Focused native scrolling test exceeded 30 seconds'
        phase = state['phase']
        # Playback must supply its own 60 Hz presentation redraws.
        if phase not in ('forward', 'backward'):
            area.tag_redraw()
        if now < state['ready']:
            return 0.04
        candidates = rows()
        if phase in ('forward', 'backward'):
            report['observations'][phase] = playback_observation(candidates, 1 if phase == 'forward' else -1)
            cancel_playback()
            scene.frame_set(13)
            if phase == 'forward':
                transition('backward_ready')
            else:
                with bpy.context.temp_override(window=window, area=area):
                    assert bpy.ops.screen.area_split(direction='VERTICAL', factor=0.5) == {'FINISHED'}
                viewports = [viewport for viewport in window.screen.areas if viewport.type == 'VIEW_3D']
                assert len(viewports) >= 2
                second = next(viewport for viewport in viewports if viewport != area)
                second.spaces.active.overlay.show_mesh_onion_skin = True
                second.spaces.active.overlay.mesh_onion_skin_screen_offset = False
                region = next(r for r in area.regions if r.type == 'WINDOW')
                state['second'] = second
                transition('mixed')
            return 0.04
        target = scene.frame_current + scene.frame_subframe
        matching = [row for row in candidates if abs(row['native_frame'] - target) < 1e-5
                    and abs(row['playhead'] - target) < 1e-5
                    and row['enabled'] == int(overlay.mesh_onion_skin_screen_offset)
                    and row['reverse'] == int(overlay.mesh_onion_skin_screen_offset_reverse)
                    and abs(row['spacing'] - overlay.mesh_onion_skin_screen_offset_spacing) < 1e-5
                    and row['world_space'] == int(overlay.mesh_onion_skin_screen_offset_world_space)
                    and abs(row['distance'] - overlay.mesh_onion_skin_screen_offset_distance) < 1e-5]
        if phase == 'mixed':
            state['second'].tag_redraw()
            matching = [row for row in matching if center_visible(row, 13)]
            relative = [row for row in candidates if not row['enabled'] and row['native_frame'] == 13
                        and row['frames'] == [9, 11, 15, 17]]
            if not matching or not relative:
                assert now < state['deadline'], 'Mixed viewports did not publish both sampling modes'
                return 0.04
            check('mixed viewport retains original relative cohort', all(pixel == 0 for pixel in relative[-1]['pixels']))
            check('mixed timeline viewport retains aligned center pose', center_visible(matching[-1], 13))
            report['observations']['mixed'] = {'timeline': matching[-1], 'relative': relative[-1]}
            check('source animation and ID users remain unchanged', source_state() == original)
            return finish()
        if phase == 'keyed':
            matching = [row for row in matching if center_visible(row, 13) and 12 in row['frames']]
        elif phase in ('forward_ready', 'backward_ready'):
            matching = [row for row in matching if abs(row['center'] - target) < 1e-5]
        elif phase in ('fractional', 'world_fractional'):
            matching = [row for row in matching if 13 in row['frames']]
        else:
            matching = [row for row in matching if center_visible(row, 13)]
        if not matching:
            assert now < state['deadline'], 'No matching native stable-timeline draw: ' + phase
            return 0.04
        row = matching[-1]
        if phase in ('normal', 'reverse', 'world_normal', 'world_reverse', 'world_zoom',
                     'world_perspective', 'world_perspective_far', 'world_orbit'):
            image = state['pixels'].get(phase)
            if image is None:
                assert now < state['deadline'], 'Colored native ghosts were not observed: ' + phase
                return 0.04
            check(phase + ' renders past/future ghosts on correct screen sides',
                  image['past_x'] > image['future_x'] if row['reverse'] else image['past_x'] < image['future_x'])
            row['viewport_pixels'] = image
            if phase in ('normal', 'reverse', 'world_normal', 'world_orbit'):
                with bpy.context.temp_override(window=window, area=area, region=region):
                    bpy.ops.screen.screenshot(filepath=str(args.output / (phase + '.png')))
        check(phase + ' submits correct signed native pixels', pixel_law(row))
        check(phase + ' submits correct signed scene distances', world_law(row))
        if phase.startswith('world_') and phase != 'world_setup':
            initial = report['observations']['zoom']
            check(phase + ' reuses immutable geometry', same_payloads(initial, row))
            check(phase + ' does not upload geometry', row['vbo_uploads'] == initial['vbo_uploads'])
            check(phase + ' does not request another evaluation', row['generation'] == initial['generation'])
            if phase != 'world_fractional':
                check(phase + ' keeps centered geometry undisplaced',
                      center_visible(row, 13) and samples(row)[13]['world_offset'] == 0)
        report['observations'][phase] = row
        if phase == 'normal':
            check('timeline retains the aligned zero-offset current pose', center_visible(row, 13))
            check('timeline retains transparent entry and exit guards', sum(
                alpha == 0 and not submitted for alpha, submitted in zip(row['alpha'], row['submitted'])) >= 2)
            overlay.mesh_onion_skin_screen_offset_reverse = True
            transition('reverse')
        elif phase == 'reverse':
            initial = report['observations']['normal']
            check('Reverse Direction reuses immutable geometry', same_payloads(initial, row))
            check('Reverse Direction does not upload geometry', row['vbo_uploads'] == initial['vbo_uploads'])
            overlay.mesh_onion_skin_screen_offset_reverse = False
            overlay.mesh_onion_skin_screen_offset_spacing = 43.5
            transition('spacing')
        elif phase == 'spacing':
            initial = report['observations']['normal']
            check('spacing reuses immutable geometry', same_payloads(initial, row))
            check('spacing does not upload geometry', row['vbo_uploads'] == initial['vbo_uploads'])
            view.region_3d.view_distance *= 1.3
            transition('zoom')
        elif phase == 'zoom':
            check('zoom preserves pixel displacement', row['pixels'] == report['observations']['spacing']['pixels'])
            rv3d = view.region_3d
            state['world_view'] = {'rotation': rv3d.view_rotation.copy(),
                                   'perspective': rv3d.view_perspective,
                                   'distance': rv3d.view_distance}
            overlay.mesh_onion_skin_screen_offset_world_space = True
            rv3d.view_perspective = 'ORTHO'
            transition('world_setup')
        elif phase == 'world_setup':
            rv3d = view.region_3d
            location = rv3d.view_location.copy()
            right = (rv3d.view_rotation @ Vector((1, 0, 0))).normalized()
            center = view3d_utils.location_3d_to_region_2d(region, rv3d, location)
            displaced = view3d_utils.location_3d_to_region_2d(region, rv3d, location + right)
            check('native viewer horizontal projection is available',
                  center is not None and displaced is not None and displaced.x > center.x)
            overlay.mesh_onion_skin_screen_offset_distance = 75.0 / (displaced.x - center.x)
            transition('world_normal')
        elif phase == 'world_normal':
            overlay.mesh_onion_skin_screen_offset_reverse = True
            transition('world_reverse')
        elif phase == 'world_reverse':
            overlay.mesh_onion_skin_screen_offset_reverse = False
            view.region_3d.view_distance *= 2.0
            transition('world_zoom')
        elif phase == 'world_zoom':
            ratio = row['viewport_pixels']['separation'] / report['observations']['world_normal']['viewport_pixels']['separation']
            check('orthographic zoom halves rendered scene-distance separation', 0.4 < ratio < 0.6)
            row['separation_ratio'] = ratio
            view.region_3d.view_distance = state['world_view']['distance']
            view.region_3d.view_perspective = 'PERSP'
            transition('world_perspective')
        elif phase == 'world_perspective':
            view.region_3d.view_distance *= 2.0
            transition('world_perspective_far')
        elif phase == 'world_perspective_far':
            ratio = row['viewport_pixels']['separation'] / report['observations']['world_perspective']['viewport_pixels']['separation']
            check('perspective camera distance reduces rendered separation', 0.25 < ratio < 0.8)
            row['separation_ratio'] = ratio
            view.region_3d.view_distance = state['world_view']['distance']
            view.region_3d.view_rotation = Quaternion((0, 0, 1), 0.65) @ state['world_view']['rotation']
            transition('world_orbit')
        elif phase == 'world_orbit':
            scene.frame_set(13, subframe=0.25)
            transition('world_fractional')
        elif phase == 'world_fractional':
            pose = samples(row)[13]
            check('fractional scene-distance scrolling retains the frame13 pose',
                  pose['payload'] == samples(report['observations']['world_normal'])[13]['payload'])
            check('fractional scene-distance scrolling moves by a quarter-frame',
                  abs(pose['offset'] + 0.25) < 1e-5
                  and abs(pose['world_offset'] + 0.25 * row['distance']) < 2e-4)
            scene.frame_set(13)
            rv3d = view.region_3d
            rv3d.view_distance = state['world_view']['distance']
            rv3d.view_rotation = state['world_view']['rotation']
            rv3d.view_perspective = state['world_view']['perspective']
            overlay.mesh_onion_skin_screen_offset_world_space = False
            scene.frame_set(13, subframe=0.25)
            transition('fractional')
        elif phase == 'fractional':
            pose = samples(row)[13]
            check('fractional advance retains the same frame13 pose',
                  pose['payload'] == samples(report['observations']['spacing'])[13]['payload'])
            check('fractional advance scrolls frame13 by a quarter-frame',
                  abs(pose['offset'] + 0.25) < 1e-5 and abs(pose['pixel'] + 0.25 * 43.5) < 2e-4)
            scene.frame_set(13)
            overlay.mesh_onion_skin_keyed_frames_only = True
            transition('keyed')
        elif phase == 'keyed':
            check('keyed-only timeline retains aligned current authored pose', center_visible(row, 13))
            overlay.mesh_onion_skin_keyed_frames_only = False
            overlay.mesh_onion_skin_loop_frame_range = True
            scene.frame_set(8)
            transition('forward_ready')
        elif phase == 'forward_ready':
            start_playback(1)
        elif phase == 'backward_ready':
            start_playback(-1)
        return 0.04
    except Exception:
        report['error'] = traceback.format_exc()
        print(report['error'], flush=True)
        return finish()


print('ONION_SCREEN_PHASE normal', flush=True)
bpy.app.timers.register(tick, first_interval=0.1)
