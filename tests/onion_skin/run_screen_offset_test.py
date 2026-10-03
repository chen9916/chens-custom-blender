# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Observe native screen offsets and playback using the disposable Mixamo fixture.

Python changes viewport controls and reads native draw diagnostics/pixels only.
It neither evaluates ghost geometry nor draws ghosts, and never saves the asset.
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
scene.frame_set(12)
scene.render.fps = 12
overlay.show_mesh_onion_skin = True
overlay.mesh_onion_skin_before = overlay.mesh_onion_skin_after = 2
overlay.mesh_onion_skin_step = 2
overlay.mesh_onion_skin_in_front = True
overlay.mesh_onion_skin_opacity = 0.7
overlay.mesh_onion_skin_screen_offset = True
overlay.mesh_onion_skin_screen_offset_reverse = False
overlay.mesh_onion_skin_screen_offset_spacing = 24.0
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
          'scope': 'native screen pixels, reversal, spacing/zoom, playhead scrolling, playback and loop boundary'}
state = {'phase': 'normal', 'ready': time.perf_counter() + 1.0,
         'deadline': time.perf_counter() + 8.0, 'pixels': {}}


def check(label, condition):
    report['checks'].append({'name': label, 'passed': bool(condition)})
    assert condition, label


def transition(phase, delay=0.35):
    state.update(phase=phase, ready=time.perf_counter() + delay,
                 deadline=time.perf_counter() + 5.0)
    print('ONION_SCREEN_PHASE', phase, flush=True)


def rows():
    text = args.log.read_text(encoding='utf-8', errors='replace') if args.log.exists() else ''
    marker = 'ONION_SCREEN_PHASE ' + state['phase']
    text = text.rsplit(marker, 1)[-1] if marker in text else text
    result = []
    for match in re.finditer(r'^mesh_onion_skin_screen (.+)$', text, re.MULTILINE):
        data = dict(re.findall(r'(\w+)=([^\s]+)', match.group(1)))
        for key in ('frames', 'offsets', 'pixels'):
            data[key] = [float(value) for value in data[key].split(',')]
        for key in ('generation', 'enabled', 'reverse'):
            data[key] = int(data[key])
        for key in ('playhead', 'spacing'):
            data[key] = float(data[key])
        result.append(data)
    return result


def observe_pixels():
    if bpy.context.area != area or state['phase'] not in ('normal', 'reverse'):
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
                                           'past_pixels': int(blue_x.size),
                                           'future_pixels': int(red_x.size)}


handler = bpy.types.SpaceView3D.draw_handler_add(observe_pixels, (), 'WINDOW', 'POST_PIXEL')


def finish():
    if window.screen.is_animation_playing:
        with bpy.context.temp_override(window=window, area=area, region=region):
            bpy.ops.screen.animation_cancel(restore_frame=False)
    overlay.show_mesh_onion_skin = False
    bpy.types.SpaceView3D.draw_handler_remove(handler, 'WINDOW')
    report['passed'] = bool(report['checks']) and all(c['passed'] for c in report['checks']) and 'error' not in report
    (args.output / 'result.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print('ONION_SCREEN_RESULT', json.dumps(report), flush=True)
    bpy.ops.wm.quit_blender()
    return None


def tick():
    try:
        area.tag_redraw()
        now = time.perf_counter()
        if now < state['ready']:
            return 0.04
        candidates = rows()
        phase = state['phase']
        if phase == 'playback':
            frames = {r['playhead'] for r in candidates}
            if len(frames) < 3 and now < state['deadline']:
                return 0.04
            check('playback produces advancing native ghost draws', len(frames) >= 3)
            check('native worker remains active during playback', bpy.app.is_job_running('MESH_ONION_SKIN'))
            check('playback screen spacing follows displayed frame distance', all(
                abs(p - d * 43.5) < 1e-4 for r in candidates for p, d in zip(r['pixels'], r['offsets'])))
            with bpy.context.temp_override(window=window, area=area, region=region):
                bpy.ops.screen.animation_cancel(restore_frame=False)
            scene.frame_set(1)
            overlay.mesh_onion_skin_loop_frame_range = True
            transition('loop_start')
            return 0.04
        expected_frames = {'normal': [8, 10, 14, 16], 'reverse': [8, 10, 14, 16],
                           'spacing': [8, 10, 14, 16], 'zoom': [8, 10, 14, 16],
                           'keyed': [10, 11, 13, 14], 'advance': [11, 12, 14, 15],
                           'loop_start': [21, 22, 2, 3], 'loop_end': [20, 21, 1, 2],
                           'disabled': [20, 21, 1, 2]}[phase]
        matching = [r for r in candidates if r['playhead'] == scene.frame_current
                    and r['frames'] == expected_frames and
                    r['enabled'] == int(overlay.mesh_onion_skin_screen_offset) and
                    r['reverse'] == int(overlay.mesh_onion_skin_screen_offset_reverse) and
                    r['spacing'] == overlay.mesh_onion_skin_screen_offset_spacing]
        if not matching:
            assert now < state['deadline'], 'No matching native screen-offset draw: ' + phase
            return 0.04
        row = matching[-1]
        sign = -1 if overlay.mesh_onion_skin_screen_offset_reverse else 1
        spacing = overlay.mesh_onion_skin_screen_offset_spacing if row['enabled'] else 0
        check(phase + ' submits correct native signed pixel offsets', all(
            abs(p - d * spacing * sign) < 1e-4 for p, d in zip(row['pixels'], row['offsets'])))
        report['observations'][phase] = row
        if phase in ('normal', 'reverse'):
            image = state['pixels'].get(phase)
            if image is None:
                assert now < state['deadline'], 'Colored native ghosts were not observed: ' + phase
                return 0.04
            check(phase + ' rendered past/future ghosts on correct screen sides',
                  image['past_x'] < image['future_x'] if phase == 'normal' else image['past_x'] > image['future_x'])
            report['observations'][phase]['viewport_pixels'] = image
            with bpy.context.temp_override(window=window, area=area, region=region):
                bpy.ops.screen.screenshot(filepath=str(args.output / (phase + '.png')))
        if phase == 'normal':
            overlay.mesh_onion_skin_screen_offset_reverse = True
            transition('reverse')
        elif phase == 'reverse':
            check('side switch reuses the cached geometry generation', row['generation'] == report['observations']['normal']['generation'])
            overlay.mesh_onion_skin_screen_offset_reverse = False
            overlay.mesh_onion_skin_screen_offset_spacing = 43.5
            transition('spacing')
        elif phase == 'spacing':
            check('spacing reuses cached geometry', row['generation'] == report['observations']['normal']['generation'])
            view.region_3d.view_distance *= 1.3
            transition('zoom')
        elif phase == 'zoom':
            check('zoom preserves pixel displacement', row['pixels'] == report['observations']['spacing']['pixels'])
            overlay.mesh_onion_skin_keyed_frames_only = True
            transition('keyed')
        elif phase == 'keyed':
            scene.frame_set(13)
            transition('advance')
        elif phase == 'advance':
            prior = report['observations']['keyed']
            check('same future pose scrolls left as playhead advances',
                  abs(row['pixels'][row['frames'].index(14)] - prior['pixels'][prior['frames'].index(14)] + 43.5) < 1e-4)
            with bpy.context.temp_override(window=window, area=area, region=region):
                bpy.ops.screen.animation_play()
            transition('playback', delay=0.7)
        elif phase == 'loop_start':
            check('looped past ghosts remain on left', row['pixels'][0] < 0 and row['pixels'][1] < 0)
            scene.frame_set(22)
            transition('loop_end')
        elif phase == 'loop_end':
            check('looped future ghosts remain on right', row['pixels'][2] > 0 and row['pixels'][3] > 0)
            overlay.mesh_onion_skin_screen_offset = False
            transition('disabled')
        else:
            check('disabling offset restores zero screen displacement', all(p == 0 for p in row['pixels']))
            check('source animation and ID users remain unchanged', source_state() == original)
            return finish()
        return 0.04
    except Exception:
        report['error'] = traceback.format_exc()
        print(report['error'], flush=True)
        return finish()


print('ONION_SCREEN_PHASE normal', flush=True)
bpy.app.timers.register(tick, first_interval=0.1)
