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
parser.add_argument('--frame', type=int, default=None, help='Set the initial scene playhead')
parser.add_argument('--keyed-only', action='store_true')
parser.add_argument('--loop-range', action='store_true')
parser.add_argument('--fade', action='store_true')
parser.add_argument('--fade-strength', type=float, default=0.75)
parser.add_argument('--expected-frames', type=float, nargs='+', default=None,
                    help='Explicit authored fixture oracle for native submitted sample frames')
parser.add_argument('--expected-offsets', type=float, nargs='+', default=None,
                    help='Signed requested offsets paired with --expected-frames')
parser.add_argument('--all-actions', action='store_true',
                    help='Protect all authored Actions, including NLA-only source animation')
parser.add_argument('--confirm-policy', choices=('authored', 'new-action'), default='authored',
                    help='Expect native confirmation to modify the authored Action or create one')
parser.add_argument('--geometry-expectation', choices=('off', 'change', 'stable'), default='off',
                    help='Validate opt-in native geometry diagnostics separately from timing runs')
parser.add_argument('--geometry-dump', action='store_true',
                    help='Capture native quiet-state world vertices for numeric parity checks')
args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
args.output.mkdir(parents=True, exist_ok=True)
dump_environment = 'BLENDER_MESH_ONION_SKIN_GEOMETRY_DUMP_DIRECTORY'
dump_phase_environment = 'BLENDER_MESH_ONION_SKIN_GEOMETRY_DUMP_PHASE'
quiet_states = ('baseline', 'hold_cancel', 'after_cancel', 'hold_confirm',
                'after_confirm', 'after_undo')
dump_directory = None
if args.geometry_dump or os.environ.get(dump_environment):
    assert args.geometry_expectation != 'off', 'Geometry dumps are correctness instrumentation'
    dump_directory = Path(os.environ.get(dump_environment, str(args.output / 'geometry'))).resolve()
    assert dump_directory.is_relative_to(args.output.resolve()), 'Dump directory must stay inside this run'
    dump_directory.mkdir(parents=True, exist_ok=True)
    for name in quiet_states:
        for suffix in ('.bin', '.bin.tmp'):
            (dump_directory / (name + suffix)).unlink(missing_ok=True)
    os.environ[dump_environment] = str(dump_directory)
    os.environ[dump_phase_environment] = 'baseline'
assert 0.0 <= args.fade_strength <= 1.0
if args.expected_frames is not None or args.expected_offsets is not None:
    assert args.enabled and args.expected_frames is not None and args.expected_offsets is not None
    assert len(args.expected_frames) == len(args.expected_offsets)
    assert os.environ.get('BLENDER_MESH_ONION_SKIN_GEOMETRY_DIAGNOSTICS'), \
        'Native sampling validation requires BLENDER_MESH_ONION_SKIN_GEOMETRY_DIAGNOSTICS=1'
if args.confirm_policy == 'new-action':
    assert args.all_actions, 'New Action confirmation requires --all-actions source protection'
    # Available-only is a user preference and survives loading any fixture. Native
    # confirmation must be allowed to create channels for this particular case.
    bpy.context.preferences.edit.use_keyframe_insert_available = False
if args.geometry_expectation != 'off':
    assert args.enabled, 'Geometry validation requires visible native ghosts'
    assert os.environ.get('BLENDER_MESH_ONION_SKIN_GEOMETRY_DIAGNOSTICS'), \
        'Launch with BLENDER_MESH_ONION_SKIN_GEOMETRY_DIAGNOSTICS=1 for geometry validation'
bpy.context.preferences.filepaths.use_auto_save_temporary_files = False
if args.frame is not None:
    bpy.context.scene.frame_set(args.frame)
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
    for name, value in (('mesh_onion_skin_keyed_frames_only', args.keyed_only),
                        ('mesh_onion_skin_loop_frame_range', args.loop_range),
                        ('mesh_onion_skin_distance_fade', args.fade),
                        ('mesh_onion_skin_fade_strength', args.fade_strength)):
        if hasattr(view.overlay, name):
            setattr(view.overlay, name, value)
        elif args.keyed_only or args.loop_range or args.fade:
            raise AssertionError('This Blender lacks the requested native sampling/fade settings')
sampled_meshes = [obj.name for obj in bpy.context.scene.objects
                 if obj.type == 'MESH' and obj.visible_get(view_layer=bpy.context.view_layer) and
                 any(modifier.type == 'ARMATURE' and modifier.object == bpy.data.objects[rig_name] and
                     modifier.show_viewport for modifier in obj.modifiers)]
if args.enabled:
    assert sampled_meshes, 'The fixture has no visible mesh directly deformed by the selected rig'
report = {'enabled': bool(args.enabled), 'samples': args.samples,
          'sampled_meshes': sampled_meshes,
          'expected_visible_samples': (len(args.expected_frames) if args.expected_frames is not None
                                       else args.samples) * len(sampled_meshes),
          'frame': bpy.context.scene.frame_current, 'keyed_only': args.keyed_only,
          'loop_range': args.loop_range, 'fade': args.fade, 'fade_strength': args.fade_strength,
          'expected_frames': args.expected_frames, 'expected_offsets': args.expected_offsets,
          'version': bpy.app.version_string, 'build_hash': bpy.app.build_hash.decode(),
          'build_date': bpy.app.build_date.decode(), 'build_time': bpy.app.build_time.decode(),
          'build_branch': bpy.app.build_branch.decode(),
          'rig': args.rig, 'bone': args.bone,
          'all_actions_digest': args.all_actions, 'confirm_policy': args.confirm_policy,
          'geometry_expectation': args.geometry_expectation,
          'geometry_diagnostics_enabled': bool(os.environ.get(
              'BLENDER_MESH_ONION_SKIN_GEOMETRY_DIAGNOSTICS')),
          'geometry_quantum': 1e-5,
          'geometry_dump_enabled': dump_directory is not None,
          'geometry_dump_directory': str(dump_directory.relative_to(args.output.resolve()))
                                     if dump_directory else None,
          'geometry_epsilon': 1e-5,
          'geometry_marks': [],
          'gpu': {'renderer': gpu.platform.renderer_get(),
                  'vendor': gpu.platform.vendor_get(),
                  'version': gpu.platform.version_get()},
          'checks': [], 'draw_times': []}
state = {'phase': 'warmup', 'deadline': time.perf_counter() + 3, 'move': 0}


def rig():
    return bpy.data.objects[rig_name]


def curves_digest():
    values = []
    action = rig().animation_data.action
    actions = sorted(bpy.data.actions, key=lambda value: value.name) if args.all_actions else [action]
    for action in actions:
        if action is None:
            continue
        values.append(('action', action.name))
        for layer in action.layers:
            for strip in layer.strips:
                bags = list(strip.channelbags) if args.all_actions else [
                    strip.channelbag(rig().animation_data.action_slot)]
                for bag in bags:
                    if bag:
                        for curve in bag.fcurves:
                            values.append((curve.data_path, curve.array_index,
                                           [(tuple(key.co), tuple(key.handle_left),
                                             tuple(key.handle_right), key.interpolation)
                                            for key in curve.keyframe_points]))
    return hashlib.sha256(repr(values).encode()).hexdigest()


def animation_state():
    adt = rig().animation_data
    return {'active_action': adt.action.name if adt.action else None,
            'active_slot': adt.action_slot.identifier if adt.action_slot else None,
            'use_nla': adt.use_nla, 'use_tweak_mode': adt.use_tweak_mode,
            'blend_type': adt.action_blend_type, 'influence': adt.action_influence,
            'tracks': [(track.name, track.mute, track.is_solo,
                        [(strip.name, strip.action.name if strip.action else None,
                          strip.action_slot.identifier if strip.action_slot else None,
                          strip.frame_start, strip.frame_end, strip.action_frame_start,
                          strip.action_frame_end, strip.scale, strip.repeat, strip.blend_type,
                          strip.influence, strip.mute, strip.extrapolation)
                         for strip in track.strips]) for track in adt.nla_tracks]}


def source_state():
    bone = rig().pose.bones[args.bone]
    action = rig().animation_data.action
    return {'curves': curves_digest(),
            'animation': animation_state(),
            'pose': tuple(bone.location) + tuple(bone.rotation_euler) +
                    tuple(bone.rotation_quaternion) + tuple(bone.rotation_axis_angle) +
                    tuple(bone.scale),
            'users': (rig().users, rig().data.users,
                      tuple((value.name, value.users) for value in sorted(
                          bpy.data.actions, key=lambda value: value.name)) if args.all_actions else
                      action.users if action else None),
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
    if dump_directory:
        os.environ[dump_phase_environment] = phase if phase in quiet_states else ''
        area.tag_redraw()
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
        check('initial Action assignment matches confirmation policy',
              (rig().animation_data.action is None) == (args.confirm_policy == 'new-action'))
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
            check(phase + ' leaves source animation stack unchanged',
                  current['animation'] == initial['animation'])
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
        check('native confirm preserves NLA strips and tweak mode',
              animation_state()['tracks'] == report['initial_state']['animation']['tracks'] and
              animation_state()['use_tweak_mode'] == report['initial_state']['animation']['use_tweak_mode'])
        if args.confirm_policy == 'new-action':
            check('native confirm creates one active Action', rig().animation_data.action is not None and
                  len(bpy.data.actions) == report['initial_state']['counts'][1] + 1)
        geometry_mark('after_confirm')
        with bpy.context.temp_override(window=window, area=area, region=region):
            bpy.ops.ed.undo()
        # Undo can rebuild a complete private graph for a large driver/control rig.
        phase_update('after_undo', deadline=now + 3)
    elif phase == 'after_undo' and now >= state['deadline']:
        check('undo restores source curves', curves_digest() == report['initial_state']['curves'])
        check('undo restores source animation stack',
              animation_state() == report['initial_state']['animation'])
        geometry_mark('after_undo')
        if native_overlay:
            view.overlay.show_mesh_onion_skin = False
        phase_update('disabled', deadline=now + 0.5)
    elif phase == 'disabled' and now >= state['deadline']:
        return finish()
    return 1 / 60


bpy.app.timers.register(tick, first_interval=0.1)
