# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Check native same-frame key replacement in a disposable interactive process.

Run with --factory-startup --enable-event-simulate. This creates a small rig,
drives real modal transforms, checks keys/reevaluation/undo/redo, and quits without
saving. It must not be run in the user's working Blender session.
"""

import argparse
import json
import sys
import time
import traceback
from pathlib import Path

import bpy


parser = argparse.ArgumentParser()
parser.add_argument('--output', type=Path, required=True)
parser.add_argument('--method', choices=('auto', 'manual'), default='auto')
parser.add_argument('--stacked', action='store_true')
args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
assert '--factory-startup' in sys.argv and '--enable-event-simulate' in sys.argv
assert not bpy.app.background and not bpy.data.filepath
args.output.mkdir(parents=True, exist_ok=True)
bpy.context.preferences.filepaths.use_auto_save_temporary_files = False
bpy.context.preferences.filepaths.temporary_directory = str(args.output)
bpy.context.preferences.edit.use_keyframe_insert_available = False
bpy.context.preferences.edit.use_auto_keyframe_insert_needed = True
bpy.context.preferences.edit.use_keyframe_insert_needed = False
bpy.ops.object.select_all(action='SELECT')
bpy.ops.object.delete(use_global=False)
bpy.ops.object.armature_add(enter_editmode=True)
rig = bpy.context.object
rig.name = 'KeyReplacementRig'
rig.data.edit_bones[0].name = 'Control'
bpy.ops.object.mode_set(mode='POSE')
control = rig.pose.bones['Control']
control.select = True
rig.data.bones.active = control.bone
for frame, value in ((1, 0.0), (5, 0.25), (10, 0.5)):
    control.location.x = value
    control.keyframe_insert(data_path='location', frame=frame)
bag = rig.animation_data.action.layers[0].strips[0].channelbag(rig.animation_data.action_slot)
for curve in bag.fcurves:
    for key in curve.keyframe_points:
        key.interpolation = 'LINEAR'
if args.stacked:
    curve = bag.fcurves.find(control.path_from_id() + '.location', index=0)
    curve.keyframe_points.add(1)
    key = curve.keyframe_points[-1]
    key.co = (5.0, 0.75)
    key.interpolation = 'LINEAR'
    key.handle_left_type = key.handle_right_type = 'VECTOR'
    curve.keyframe_points.sort()
    curve.keyframe_points.handles_recalc()

# A directly skinned mesh exercises private preview staging alongside native keying.
mesh = bpy.data.meshes.new('KeyReplacementMesh')
mesh.from_pydata([(-0.1, 0.0, 0.0), (0.1, 0.0, 0.0), (0.0, 0.0, 1.0)], [], [(0, 1, 2)])
obj = bpy.data.objects.new('KeyReplacementMesh', mesh)
bpy.context.scene.collection.objects.link(obj)
obj.vertex_groups.new(name='Control').add([0, 1, 2], 1.0, 'REPLACE')
obj.modifiers.new(name='Armature', type='ARMATURE').object = rig
scene = bpy.context.scene
scene.frame_set(5)
scene.tool_settings.use_keyframe_insert_auto = args.method == 'auto'
scene.tool_settings.auto_keying_mode = 'ADD_REPLACE_KEYS'
scene.tool_settings.use_keyframe_insert_keyingset = False
window = bpy.context.window
area = next(area for area in window.screen.areas if area.type == 'VIEW_3D')
region = next(region for region in area.regions if region.type == 'WINDOW')
view = area.spaces.active
view.region_3d.view_perspective = 'ORTHO'
view.region_3d.view_distance = 6.0
view.region_3d.view_location = (0.0, 0.0, 0.5)
view.overlay.show_mesh_onion_skin = True
view.overlay.mesh_onion_skin_before = 1
view.overlay.mesh_onion_skin_after = 1
view.overlay.mesh_onion_skin_in_front = True
report = {'build_hash': bpy.app.build_hash.decode(), 'version': bpy.app.version_string,
          'method': args.method, 'stacked': args.stacked, 'checks': []}
state = {'phase': 'warmup', 'deadline': time.perf_counter() + 1.0, 'round': 0}


def rig_get():
    return bpy.data.objects['KeyReplacementRig']


def control_get():
    return rig_get().pose.bones['Control']


def curves_get():
    adt = rig_get().animation_data
    return adt.action.layers[0].strips[0].channelbag(adt.action_slot).fcurves


def target_curve():
    return curves_get().find(control_get().path_from_id() + '.location', index=0)


def animation_snapshot():
    return [(curve.data_path, curve.array_index,
             [(tuple(key.co), tuple(key.handle_left), tuple(key.handle_right), key.interpolation)
              for key in curve.keyframe_points]) for curve in curves_get()]


def check(name, condition):
    report['checks'].append({'name': name, 'passed': bool(condition)})
    print('KEY_REPLACEMENT_CHECK', name, bool(condition), flush=True)
    if not condition:
        raise AssertionError(name)


def advance(phase, delay=0.3):
    state.update(phase=phase, deadline=time.perf_counter() + delay)


def mouse_move(offset):
    window.event_simulate(type='MOUSEMOVE', value='NOTHING',
                          x=region.x + region.width // 2 + offset,
                          y=region.y + region.height // 2)


def finish():
    (args.output / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print('KEY_REPLACEMENT_RESULT', json.dumps(report), flush=True)
    view.overlay.show_mesh_onion_skin = False
    bpy.ops.wm.quit_blender()
    return None


def tick_impl():
    now = time.perf_counter()
    area.tag_redraw()
    if now < state['deadline']:
        return 0.05
    phase = state['phase']
    if phase == 'warmup':
        state['baseline'] = animation_snapshot()
        state['baseline_pose'] = tuple(control_get().location)
        with bpy.context.temp_override(window=window, area=area, region=region):
            bpy.ops.ed.undo_push(message='Key replacement baseline')
        state['cancel'] = True
        mouse_move(0)
        advance('invoke')
    elif phase == 'invoke':
        state['before'] = animation_snapshot()
        with bpy.context.temp_override(window=window, area=area, region=region):
            result = bpy.ops.transform.translate('INVOKE_DEFAULT',
                                                 constraint_axis=(True, False, False),
                                                 orient_type='GLOBAL')
        report['last_invocation'] = {'result': list(result), 'mode': bpy.context.mode,
                                     'selected_bones': [bone.name for bone in
                                                        (bpy.context.selected_pose_bones or [])]}
        check('native modal transform starts', result == {'RUNNING_MODAL'})
        advance('move')
    elif phase == 'move':
        mouse_move(50)
        advance('hold')
    elif phase == 'hold':
        check('modal preview leaves authored keys unchanged',
              animation_snapshot() == state['before'])
        state['held_x'] = control_get().location.x
        check('modal input changes the control',
              abs(state['held_x'] - state['baseline_pose'][0]) > 1e-4)
        key = 'ESC' if state['cancel'] else 'RET'
        window.event_simulate(type=key, value='PRESS')
        window.event_simulate(type=key, value='RELEASE')
        advance('confirmed', 0.5)
    elif phase == 'confirmed':
        if state['cancel']:
            check('cancel preserves all source keys', animation_snapshot() == state['baseline'])
            check('cancel restores the control',
                  max(abs(a - b) for a, b in zip(control_get().location, state['baseline_pose'])) < 1e-5)
            state['cancel'] = False
            mouse_move(0)
            advance('invoke')
        else:
            if args.method == 'manual':
                check('Auto Key off leaves source keys unchanged',
                      animation_snapshot() == state['before'])
                with bpy.context.temp_override(window=window, area=area, region=region):
                    # Python calls default to undo=False; match the native I-key undo behavior.
                    check('manual native key insertion completes',
                          bpy.ops.anim.keyframe_insert('EXEC_DEFAULT', True) == {'FINISHED'})
            curve = target_curve()
            keys = list(curve.keyframe_points)
            check('re-keying retains one key at frame five',
                  len(keys) == 3 and [key.co.x for key in keys] == [1.0, 5.0, 10.0])
            check('the newest pose replaces the old key value', abs(keys[1].co.y - state['held_x']) < 1e-5)
            check('neighboring authored keys are preserved', keys[0].co.y == 0.0 and keys[2].co.y == 0.5)
            expected = state['held_x'] + (0.5 - state['held_x']) * 0.1 / 5.0
            check('interpolation after the key does not snap to a stale duplicate',
                  abs(curve.evaluate(5.1) - expected) < 1e-5)
            bpy.context.scene.frame_set(6)
            bpy.context.scene.frame_set(5)
            check('leaving and revisiting the frame retains the newest pose',
                  abs(control_get().location.x - state['held_x']) < 1e-5)
            state['round'] += 1
            if state['round'] == 1:
                state['first'] = animation_snapshot()
                mouse_move(0)
                advance('invoke')
            else:
                state['second'] = animation_snapshot()
                with bpy.context.temp_override(window=window, area=area, region=region):
                    check('native undo completes', bpy.ops.ed.undo() == {'FINISHED'})
                advance('undo', 0.5)
    elif phase == 'undo':
        check('undo restores the previous keyed pose', animation_snapshot() == state['first'])
        with bpy.context.temp_override(window=window, area=area, region=region):
            check('native redo completes', bpy.ops.ed.redo() == {'FINISHED'})
        advance('redo', 0.5)
    elif phase == 'redo':
        check('redo restores the newest key without duplicates', animation_snapshot() == state['second'])
        return finish()
    return 0.05


def tick():
    try:
        return tick_impl()
    except Exception:
        report['error'] = traceback.format_exc()
        print(report['error'], flush=True)
        return finish()


bpy.app.timers.register(tick, first_interval=0.1)
