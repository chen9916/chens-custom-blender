# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Exercise the native overlay in a disposable interactive Blender process.

Open make_live_fixture.py's saved file with --enable-event-simulate, then
--python this-file -- --output DIR [--enabled 0] [--samples 4]. The test
simulates real modal pose transforms. Python only drives the native UI and
collects observations; it performs no onion skin evaluation or drawing.
"""

import argparse
import hashlib
import json
import math
import os
import sys
import time
import traceback
from pathlib import Path

import bpy
import gpu


parser = argparse.ArgumentParser()
parser.add_argument('--output', type=Path, required=True)
parser.add_argument('--enabled', type=int, default=1)
parser.add_argument('--samples', type=int, default=4)
parser.add_argument('--moves', type=int, default=120)
parser.add_argument('--rig', default='OnionFixtureRig')
parser.add_argument('--bone', default='Control')
parser.add_argument('--transform', choices=('TRANSLATE', 'ROTATE'), default='TRANSLATE')
parser.add_argument('--geometry-expectation', choices=('off', 'change', 'stable'), default='off',
                    help='Validate opt-in native geometry diagnostics separately from timing runs')
args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
args.output.mkdir(parents=True, exist_ok=True)
if args.geometry_expectation != 'off':
    assert args.enabled, 'Geometry validation requires visible native ghosts'
    assert os.environ.get('BLENDER_MESH_ONION_SKIN_GEOMETRY_DIAGNOSTICS'), \
        'Launch with BLENDER_MESH_ONION_SKIN_GEOMETRY_DIAGNOSTICS=1 for geometry validation'
bpy.context.preferences.filepaths.use_auto_save_temporary_files = False
window = bpy.context.window
area = next(area for area in window.screen.areas if area.type == 'VIEW_3D')
region = next(region for region in area.regions if region.type == 'WINDOW')
view = area.spaces.active
rig_name = args.rig
native_overlay = hasattr(view.overlay, 'show_mesh_onion_skin')
if args.enabled:
    assert native_overlay, 'This Blender does not contain the native mesh onion skin feature'
if native_overlay:
    view.overlay.show_mesh_onion_skin = bool(args.enabled)
    view.overlay.mesh_onion_skin_before = args.samples // 2
    view.overlay.mesh_onion_skin_after = args.samples - args.samples // 2
    view.overlay.mesh_onion_skin_step = 2
    view.overlay.mesh_onion_skin_in_front = True
    view.overlay.mesh_onion_skin_opacity = 0.2
sampled_meshes = [obj.name for obj in bpy.context.scene.objects
                 if obj.type == 'MESH' and obj.visible_get(view_layer=bpy.context.view_layer) and
                 any(modifier.type == 'ARMATURE' and modifier.object == bpy.data.objects[rig_name] and
                     modifier.show_viewport for modifier in obj.modifiers)]
if args.enabled:
    assert sampled_meshes, 'The fixture has no visible mesh directly deformed by the selected rig'
report = {'enabled': bool(args.enabled), 'samples': args.samples,
          'sampled_meshes': sampled_meshes,
          'expected_visible_samples': args.samples * len(sampled_meshes),
          'version': bpy.app.version_string, 'build_hash': bpy.app.build_hash.decode(),
          'rig': args.rig, 'bone': args.bone,
          'geometry_expectation': args.geometry_expectation,
          'geometry_diagnostics_enabled': bool(os.environ.get(
              'BLENDER_MESH_ONION_SKIN_GEOMETRY_DIAGNOSTICS')),
          'geometry_quantum': 1e-5,
          'geometry_marks': [],
          'gpu': {'renderer': gpu.platform.renderer_get(),
                  'vendor': gpu.platform.vendor_get(),
                  'version': gpu.platform.version_get()},
          'checks': [], 'draw_times': []}
state = {'phase': 'warmup', 'deadline': time.perf_counter() + 3, 'move': 0}


def rig():
    return bpy.data.objects[rig_name]


def curves_digest():
    action = rig().animation_data.action
    values = []
    for layer in action.layers:
        for strip in layer.strips:
            bag = strip.channelbag(rig().animation_data.action_slot)
            if bag:
                for curve in bag.fcurves:
                    values.append((curve.data_path, curve.array_index,
                                   [(tuple(key.co), tuple(key.handle_left),
                                     tuple(key.handle_right), key.interpolation)
                                    for key in curve.keyframe_points]))
    return hashlib.sha256(repr(values).encode()).hexdigest()


def source_state():
    bone = rig().pose.bones[args.bone]
    return {'curves': curves_digest(),
            'pose': tuple(bone.location) + tuple(bone.rotation_euler) +
                    tuple(bone.rotation_quaternion) + tuple(bone.rotation_axis_angle) +
                    tuple(bone.scale),
            'users': (rig().users, rig().data.users, rig().animation_data.action.users),
            'counts': (len(bpy.data.objects), len(bpy.data.actions), len(bpy.data.meshes))}


def check(name, condition):
    report['checks'].append({'name': name, 'passed': bool(condition)})
    print('ONION_CHECK', name, bool(condition), flush=True)
    if not condition:
        raise AssertionError(name)


def draw_observation():
    report['draw_times'].append((state['phase'], time.perf_counter()))


handler = bpy.types.SpaceView3D.draw_handler_add(draw_observation, (), 'WINDOW', 'POST_PIXEL')


def invoke_transform():
    with bpy.context.temp_override(window=window, area=area, region=region):
        if args.transform == 'ROTATE':
            result = bpy.ops.transform.rotate('INVOKE_DEFAULT',
                                              constraint_axis=(False, True, False),
                                              orient_type='GLOBAL')
        else:
            result = bpy.ops.transform.translate('INVOKE_DEFAULT',
                                                 constraint_axis=(True, False, False),
                                                 orient_type='GLOBAL')
        check('modal transform invoked', result == {'RUNNING_MODAL'})


def phase_update(phase, **values):
    state.update(phase=phase, **values)
    print('ONION_PHASE ' + phase, flush=True)


def geometry_mark(name):
    # Native diagnostics describe batches actually submitted by the overlay. The postprocessor
    # associates the latest completed draw with this quiet-state marker, rather than treating
    # every generation number or an unrelated viewport redraw as changed mesh geometry.
    report['geometry_marks'].append({'name': name, 'phase': state['phase'],
                                     'frame': bpy.context.scene.frame_current,
                                     'pose': source_state()['pose']})
    print('ONION_GEOMETRY_MARK ' + name, flush=True)


def mouse_move(x):
    window.event_simulate(type='MOUSEMOVE', value='NOTHING',
                          x=region.x + region.width // 2 + x,
                          y=region.y + region.height // 2)


def finish():
    report['final_state'] = source_state()
    report['draw_count'] = len(report['draw_times'])
    (args.output / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    bpy.types.SpaceView3D.draw_handler_remove(handler, 'WINDOW')
    print('ONION_TEST_RESULT', json.dumps({key: value for key, value in report.items()
                                         if key != 'draw_times'}), flush=True)
    bpy.ops.wm.quit_blender()
    return None


def tick():
    try:
        return tick_impl()
    except Exception:
        report['error'] = traceback.format_exc()
        print(report['error'], flush=True)
        return finish()


def tick_impl():
    now = time.perf_counter()
    area.tag_redraw()
    phase = state['phase']
    if phase == 'warmup' and now >= state['deadline']:
        report['initial_state'] = source_state()
        with bpy.context.temp_override(window=window, area=area, region=region):
            bpy.ops.ed.undo_push(message='Native Mesh Onion Skin Baseline')
            bpy.ops.screen.screenshot(filepath=str(args.output / 'warmup.png'))
        geometry_mark('baseline')
        mouse_move(0)
        phase_update('start_cancel', deadline=now + 0.2)
    elif phase in ('start_cancel', 'start_confirm') and now >= state['deadline']:
        invoke_transform()
        phase_update('move_cancel' if phase == 'start_cancel' else 'move_confirm',
                     move=0, deadline=now + 0.1)
    elif phase in ('move_cancel', 'move_confirm') and now >= state['deadline']:
        move = state['move']
        if move < args.moves:
            mouse_move(round(75 * math.sin(move * 0.045) + 20))
            state['move'] += 1
        else:
            current = source_state()
            initial = report['initial_state']
            check(phase + ' leaves source curves unchanged', current['curves'] == initial['curves'])
            check(phase + ' leaves source users unchanged', current['users'] == initial['users'])
            check(phase + ' leaves source datablocks unchanged', current['counts'] == initial['counts'])
            check(phase + ' changes authored control', current['pose'] != initial['pose'])
            # Keep the final authored pose fixed while outstanding latest-input work settles.
            # This isolates geometric comparisons from intermediate mouse-event generations.
            phase_update('hold_cancel' if phase == 'move_cancel' else 'hold_confirm',
                         deadline=now + 1)
    elif phase in ('hold_cancel', 'hold_confirm') and now >= state['deadline']:
        geometry_mark(phase)
        if args.geometry_expectation != 'off':
            with bpy.context.temp_override(window=window, area=area, region=region):
                bpy.ops.screen.screenshot(filepath=str(args.output / (phase + '.png')))
        key = 'ESC' if phase == 'hold_cancel' else 'RET'
        window.event_simulate(type=key, value='PRESS')
        window.event_simulate(type=key, value='RELEASE')
        phase_update('after_cancel' if key == 'ESC' else 'after_confirm', deadline=now + 1)
    elif phase == 'after_cancel' and now >= state['deadline']:
        check('cancel restores source state', source_state() == report['initial_state'])
        geometry_mark('after_cancel')
        mouse_move(0)
        phase_update('start_confirm', deadline=now + 0.2)
    elif phase == 'after_confirm' and now >= state['deadline']:
        check('native confirm inserts authored keys', curves_digest() != report['initial_state']['curves'])
        geometry_mark('after_confirm')
        with bpy.context.temp_override(window=window, area=area, region=region):
            bpy.ops.ed.undo()
        phase_update('after_undo', deadline=now + 1)
    elif phase == 'after_undo' and now >= state['deadline']:
        check('undo restores source curves', curves_digest() == report['initial_state']['curves'])
        geometry_mark('after_undo')
        if native_overlay:
            view.overlay.show_mesh_onion_skin = False
        phase_update('disabled', deadline=now + 0.5)
    elif phase == 'disabled' and now >= state['deadline']:
        return finish()
    return 1 / 60


bpy.app.timers.register(tick, first_interval=0.1)
