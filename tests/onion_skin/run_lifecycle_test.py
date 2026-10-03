# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Exercise native worker lifecycle in a disposable interactive Mixamo process.

Open the supplied .blend with --python this-file -- --output DIR [--rig NAME].
The loaded file is never saved. Native WM jobs do all evaluation; Python only
changes UI settings, checks source preservation, and performs lifecycle actions.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
import traceback
from pathlib import Path

import bpy
from bpy.app.handlers import persistent


parser = argparse.ArgumentParser()
parser.add_argument('--output', type=Path, required=True)
parser.add_argument('--rig', default=None)
parser.add_argument('--validate-membership', action='store_true',
                    help='Require native geometry diagnostics in the sibling output log')
args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
args.output.mkdir(parents=True, exist_ok=True)
if args.validate_membership:
    assert os.environ.get('BLENDER_MESH_ONION_SKIN_GEOMETRY_DIAGNOSTICS'), \
        'Membership geometry validation requires BLENDER_MESH_ONION_SKIN_GEOMETRY_DIAGNOSTICS=1'
report = {'version': bpy.app.version_string,
          'build_hash': bpy.app.build_hash.decode(), 'checks': [], 'phases': [],
          'membership_geometry_validation': args.validate_membership,
          'scope': 'enable, mesh membership restart, zero samples, shared viewports, reload, mesh/rig deletion'}
state = {'phase': 'setup', 'ready': 0, 'timeout': 0}


def check(name, condition):
    report['checks'].append({'name': name, 'passed': bool(condition)})
    print('ONION_LIFECYCLE_CHECK', name, bool(condition), flush=True)
    if not condition:
        raise AssertionError(name)


def running():
    return bpy.app.is_job_running('MESH_ONION_SKIN')


def areas():
    # Resolve after every file reload; no RNA pointers survive lifecycle actions.
    return sorted((area for area in bpy.context.window.screen.areas
                   if area.type == 'VIEW_3D'), key=lambda area: (area.x, area.y))


def overlays():
    return [area.spaces.active.overlay for area in areas()]


def rig():
    return bpy.data.objects[state['rig_name']]


def sampling(value):
    return (value.mesh_onion_skin_before, value.mesh_onion_skin_after,
            value.mesh_onion_skin_step)


def set_sampling(value, before, after, step):
    value.mesh_onion_skin_before = before
    value.mesh_onion_skin_after = after
    value.mesh_onion_skin_step = step


def source_digest():
    obj = rig()
    action = obj.animation_data.action
    values = []
    for layer in action.layers:
        for strip in layer.strips:
            bag = strip.channelbag(obj.animation_data.action_slot)
            if bag:
                for curve in bag.fcurves:
                    values.append((curve.data_path, curve.array_index,
                                   [(tuple(key.co), tuple(key.handle_left),
                                     tuple(key.handle_right), key.interpolation)
                                    for key in curve.keyframe_points]))
    return {'curves': hashlib.sha256(repr(values).encode()).hexdigest(),
            'users': (obj.users, obj.data.users, action.users),
            'counts': (len(bpy.data.objects), len(bpy.data.meshes), len(bpy.data.actions))}


def transition(phase, delay=0.3, timeout=8):
    now = time.perf_counter()
    state.update(phase=phase, ready=now + delay, timeout=now + timeout)
    report['phases'].append({'phase': phase, 'time': now})
    print('ONION_LIFECYCLE_PHASE', phase, flush=True)


def expect_job(expected, label, next_phase):
    now = time.perf_counter()
    if now < state['ready']:
        return
    if running() == expected:
        check(label, True)
        transition(next_phase)
    elif now >= state['timeout']:
        check(label, False)


def visible_skinned_meshes():
    obj = rig()
    return [mesh for mesh in bpy.context.scene.objects if mesh.type == 'MESH' and
            mesh.visible_get(view_layer=bpy.context.view_layer) and
            any(mod.type == 'ARMATURE' and mod.object == obj and mod.show_viewport
                for mod in mesh.modifiers)]


def expect_membership(expected_samples, label, next_phase):
    if not running():
        if time.perf_counter() >= state['timeout']:
            check(label + ' retains native worker', False)
        return
    if args.validate_membership:
        # Diagnostics are flushed by native draw submission. The phase marker prevents an
        # old bundle drawn before the membership change from satisfying this assertion.
        log = args.output.with_suffix('.log')
        text = log.read_text(encoding='utf-8', errors='replace') if log.exists() else ''
        marker = 'ONION_LIFECYCLE_PHASE ' + state['phase']
        current = text.rsplit(marker, 1)[-1] if marker in text else ''
        counts = [int(value) for value in re.findall(
            r'mesh_onion_skin_geometry[^\n]*\bvisible_samples=(\d+)', current)]
        if expected_samples not in counts:
            if time.perf_counter() >= state['timeout']:
                check(label + ' draws expected mesh membership', False)
            return
        check(label + ' draws expected mesh membership', True)
    check(label + ' retains native worker', True)
    transition(next_phase)


@persistent
def loaded(_unused):
    if state['phase'] == 'after_reload':
        state['old_job_present_at_load_post'] = running()


bpy.app.handlers.load_post.append(loaded)


def finish():
    report['passed'] = bool(report['checks']) and all(item['passed'] for item in report['checks'])
    report['passed'] = report['passed'] and 'error' not in report
    try:
        if loaded in bpy.app.handlers.load_post:
            bpy.app.handlers.load_post.remove(loaded)
        for value in overlays():
            value.show_mesh_onion_skin = False
    except Exception:
        report['cleanup_error'] = traceback.format_exc()
        report['passed'] = False
    (args.output / 'lifecycle_report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print('ONION_LIFECYCLE_RESULT', json.dumps(report), flush=True)
    bpy.ops.wm.quit_blender()
    return None


def tick():
    try:
        for area in areas():
            area.tag_redraw()
        return tick_impl()
    except Exception:
        report['error'] = traceback.format_exc()
        print(report['error'], flush=True)
        return finish()


def tick_impl():
    phase = state['phase']
    now = time.perf_counter()
    if phase == 'setup':
        state['filepath'] = bpy.data.filepath
        check('fixture is saved for disposable reload', bool(state['filepath']))
        candidates = [obj for obj in bpy.context.scene.objects if obj.type == 'ARMATURE']
        check('fixture has requested armature', bool(candidates))
        state['rig_name'] = args.rig or candidates[0].name
        check('active authored action exists', rig().animation_data is not None and
              rig().animation_data.action is not None)
        check('fixture opens with active posed rig', bpy.context.view_layer.objects.active == rig()
              and rig().mode == 'POSE')
        check('native UI is present', bool(areas()) and
              hasattr(overlays()[0], 'show_mesh_onion_skin'))
        report['rig'] = state['rig_name']
        state['baseline'] = source_digest()
        for value in overlays():
            value.show_mesh_onion_skin = False
            value.show_overlays = True
        transition('initial_disabled', delay=0.5)
    elif phase == 'initial_disabled':
        expect_job(False, 'initial worker stopped', 'enable')
    elif now < state['ready']:
        return 1 / 60
    elif phase == 'enable':
        value = overlays()[0]
        set_sampling(value, 2, 2, 1)
        value.show_mesh_onion_skin = True
        transition('enabled')
    elif phase == 'enabled':
        if args.validate_membership:
            expect_membership(4 * len(visible_skinned_meshes()),
                              'initial mesh membership', 'add_mesh')
        else:
            expect_job(True, 'enable starts native worker', 'add_mesh')
    elif phase == 'add_mesh':
        meshes = visible_skinned_meshes()
        check('membership fixture has visible directly skinned mesh', bool(meshes))
        state['membership_baseline'] = source_digest()
        state['membership_mesh_count'] = len(meshes)
        duplicate = meshes[0].copy()
        duplicate.name = 'OnionLifecycleMembershipCopy'
        bpy.context.scene.collection.objects.link(duplicate)
        duplicate.select_set(False)
        state['membership_copy_name'] = duplicate.name
        transition('mesh_added', delay=0.5)
    elif phase == 'mesh_added':
        expect_membership(4 * (state['membership_mesh_count'] + 1),
                          'linked mesh replacement', 'remove_added_mesh')
    elif phase == 'remove_added_mesh':
        bpy.data.objects.remove(bpy.data.objects[state['membership_copy_name']], do_unlink=True)
        transition('added_mesh_removed', delay=0.5)
    elif phase == 'added_mesh_removed':
        expect_membership(4 * state['membership_mesh_count'],
                          'removed mesh replacement', 'membership_preserved')
    elif phase == 'membership_preserved':
        check('mesh membership restart preserves source animation and users',
              source_digest() == state['membership_baseline'])
        transition('disable')
    elif phase == 'disable':
        overlays()[0].show_mesh_onion_skin = False
        transition('disabled')
    elif phase == 'disabled':
        expect_job(False, 'disable stops native worker', 'reenable')
    elif phase == 'reenable':
        overlays()[0].show_mesh_onion_skin = True
        transition('reenabled')
    elif phase == 'reenabled':
        expect_job(True, 're-enable restarts native worker', 'zero')
    elif phase == 'zero':
        set_sampling(overlays()[0], 0, 0, 1)
        transition('zeroed')
    elif phase == 'zeroed':
        if not running() and now >= state['ready']:
            check('zero samples retain enabled setting', overlays()[0].show_mesh_onion_skin)
        expect_job(False, 'zero samples stop native worker', 'restore')
    elif phase == 'restore':
        set_sampling(overlays()[0], 1, 1, 3)
        transition('restored')
    elif phase == 'restored':
        expect_job(True, 'restoring samples restarts worker', 'split')
    elif phase == 'split':
        if len(areas()) < 2:
            area = areas()[0]
            with bpy.context.temp_override(window=bpy.context.window, area=area):
                result = bpy.ops.screen.area_split(direction='VERTICAL', factor=0.5)
            check('native viewport split succeeds', result == {'FINISHED'})
        transition('shared')
    elif phase == 'shared':
        check('two native viewports exist', len(overlays()) >= 2)
        first, second = overlays()[:2]
        second.show_mesh_onion_skin = True
        set_sampling(first, 3, 1, 2)
        check('sampling synchronized across viewports', sampling(second) == (3, 1, 2))
        appearance = (second.mesh_onion_skin_opacity, second.mesh_onion_skin_in_front)
        first.mesh_onion_skin_opacity = 0.37
        first.mesh_onion_skin_in_front = not appearance[1]
        check('appearance remains per viewport',
              (second.mesh_onion_skin_opacity, second.mesh_onion_skin_in_front) == appearance)
        second.show_overlays = False
        set_sampling(first, 4, 0, 5)
        second.show_overlays = True
        check('global overlay re-show joins shared sampling', sampling(second) == (4, 0, 5))
        # Keep exactly two subscribers regardless of the fixture screen layout.
        for value in overlays()[2:]:
            value.show_mesh_onion_skin = False
        first.show_mesh_onion_skin = False
        transition('one_subscriber')
    elif phase == 'one_subscriber':
        expect_job(True, 'disabling one viewport retains shared worker', 'disable_last')
    elif phase == 'disable_last':
        overlays()[1].show_mesh_onion_skin = False
        transition('last_disabled')
    elif phase == 'last_disabled':
        expect_job(False, 'disabling last viewport stops worker', 'before_reload')
    elif phase == 'before_reload':
        check('UI lifecycle preserves source animation and users', source_digest() == state['baseline'])
        overlays()[0].show_mesh_onion_skin = True
        transition('reload_running')
    elif phase == 'reload_running':
        expect_job(True, 'worker active before reload', 'reload')
    elif phase == 'reload':
        transition('after_reload', delay=0.5)
        bpy.ops.wm.open_mainfile(filepath=state['filepath'])
        # Persistent timer resumes with fresh RNA references on its next tick.
    elif phase == 'after_reload':
        check('load_post observed old worker joined',
              state.get('old_job_present_at_load_post') is False)
        check('reload restores source animation and users', source_digest() == state['baseline'])
        for value in overlays():
            value.show_mesh_onion_skin = False
            value.show_overlays = True
        value = overlays()[0]
        set_sampling(value, 2, 2, 1)
        value.show_mesh_onion_skin = True
        transition('reloaded_worker')
    elif phase == 'reloaded_worker':
        expect_job(True, 'worker restarts after file reload', 'delete_mesh')
    elif phase == 'delete_mesh':
        obj = rig()
        meshes = [mesh for mesh in bpy.context.scene.objects if mesh.type == 'MESH' and
                  any(mod.type == 'ARMATURE' and mod.object == obj for mod in mesh.modifiers)]
        check('fixture has directly skinned meshes', bool(meshes))
        for mesh in meshes:
            bpy.data.objects.remove(mesh, do_unlink=True)
        transition('mesh_deleted')
    elif phase == 'mesh_deleted':
        expect_job(False, 'deleting all sampled meshes joins worker safely', 'reload_for_rig')
    elif phase == 'reload_for_rig':
        transition('after_rig_reload', delay=0.5)
        bpy.ops.wm.open_mainfile(filepath=state['filepath'])
    elif phase == 'after_rig_reload':
        for value in overlays():
            value.show_mesh_onion_skin = False
            value.show_overlays = True
        set_sampling(overlays()[0], 2, 2, 1)
        overlays()[0].show_mesh_onion_skin = True
        transition('rig_worker')
    elif phase == 'rig_worker':
        expect_job(True, 'worker active before rig deletion', 'delete_rig')
    elif phase == 'delete_rig':
        bpy.data.objects.remove(rig(), do_unlink=True)
        transition('rig_deleted')
    elif phase == 'rig_deleted':
        expect_job(False, 'deleting active rig joins worker safely', 'finished')
    elif phase == 'finished':
        return finish()
    return 1 / 60


bpy.app.timers.register(tick, first_interval=0.1, persistent=True)
