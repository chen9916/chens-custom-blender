# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Import a supplied Mixamo FBX into disposable native onion skin test data."""

import argparse
import json
import math
import sys
from pathlib import Path

import bpy
from mathutils import Quaternion


parser = argparse.ArgumentParser()
parser.add_argument('--input', type=Path, required=True)
parser.add_argument('--output', type=Path, required=True)
parser.add_argument('--sparse-control', action='store_true',
                    help='Retain five authored keyframes on the selected arm control only')
args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
bpy.ops.object.select_all(action='SELECT')
bpy.ops.object.delete(use_global=False)
bpy.ops.import_scene.fbx(filepath=str(args.input.resolve()))
rigs = [obj for obj in bpy.context.scene.objects if obj.type == 'ARMATURE']
assert len(rigs) == 1, 'The fixture expects one Mixamo armature'
rig = rigs[0]
meshes = [obj for obj in bpy.context.scene.objects if obj.type == 'MESH']
assert meshes, 'The supplied FBX must include a character mesh'
action = rig.animation_data.action
start, end = action.frame_range
scene = bpy.context.scene
scene.frame_start = int(start)
scene.frame_end = int(end)
scene.frame_set(round((start + end) / 2))
scene.tool_settings.use_keyframe_insert_auto = True
bone = next((bone for bone in rig.pose.bones if bone.name.endswith('LeftForeArm')),
            next(iter(rig.pose.bones)))
for obj in bpy.context.selected_objects:
    obj.select_set(False)
rig.select_set(True)
bpy.context.view_layer.objects.active = rig
for pose_bone in rig.pose.bones:
    pose_bone.select = False
bone.select = True
rig.data.bones.active = bone.bone
rig.show_in_front = True
bpy.ops.object.mode_set(mode='POSE')
coords = [obj.matrix_world @ vertex.co for obj in meshes for vertex in obj.data.vertices]
minimum = [min(point[axis] for point in coords) for axis in range(3)]
maximum = [max(point[axis] for point in coords) for axis in range(3)]
center = [(minimum[axis] + maximum[axis]) / 2 for axis in range(3)]
extent = max(maximum[axis] - minimum[axis] for axis in range(3))
for screen in bpy.data.screens:
    for area in screen.areas:
        if area.type == 'VIEW_3D':
            view = area.spaces.active
            view.region_3d.view_rotation = Quaternion((1, 0, 0), math.pi / 2)
            view.region_3d.view_location = center
            view.region_3d.view_distance = max(extent * 1.4, 1)
            view.region_3d.view_perspective = 'ORTHO'
bags = [strip.channelbag(rig.animation_data.action_slot)
        for layer in action.layers for strip in layer.strips]
curves = [curve for bag in bags if bag for curve in bag.fcurves]
control_prefix = bone.path_from_id() + '.'
control_curves = [curve for curve in curves if curve.data_path.startswith(control_prefix)]
assert control_curves, 'The selected control has no authored animation channels'
if args.sparse_control:
    retained = {int(start), int(end), round(start + (end - start) * 0.25),
                scene.frame_current, round(start + (end - start) * 0.75)}
    for curve in control_curves:
        # Removal reallocates the underlying BezTriple array. Fetch each wrapper afresh and
        # walk backward so earlier indices remain valid; never retain wrappers across removal.
        for index in range(len(curve.keyframe_points) - 1, -1, -1):
            key = curve.keyframe_points[index]
            if round(key.co.x) not in retained:
                curve.keyframe_points.remove(key, fast=True)
        curve.update()
        assert {round(key.co.x) for key in curve.keyframe_points} == retained, \
            'Each sparse control channel must retain exactly the five authored test frames'
control_frames = sorted({round(key.co.x, 4)
                         for curve in control_curves for key in curve.keyframe_points})
ghost_frames = [scene.frame_current + offset for offset in (-4, -2, 2, 4)]
if args.sparse_control:
    assert int(start) <= min(ghost_frames) and max(ghost_frames) <= int(end), \
        'The sparse-control fixture needs four surrounding samples within the animation'
    assert all(abs(sample - key) > 0.01 for sample in ghost_frames for key in control_frames), \
        'Sparse-control ghost samples must lie between authored control keys'
    assert all(any(key < sample for key in control_frames) and
               any(key > sample for key in control_frames) for sample in ghost_frames), \
        'Sparse-control samples must be bracketed by authored keys'
frames = sorted({round(key.co.x, 4) for curve in curves for key in curve.keyframe_points})
metadata = {'input': str(args.input.resolve()), 'rig': rig.name, 'bone': bone.name,
            'sparse_control_test_copy': args.sparse_control,
            'start': start, 'end': end, 'inclusive_frame_count': round(end - start) + 1,
            'unique_keyframes': frames, 'curves': len(curves),
            'selected_control_prefix': control_prefix,
            'selected_control_keyframes': control_frames,
            'selected_control_channels': [curve.data_path for curve in control_curves],
            'ghost_sample_frames': ghost_frames,
            'keys': sum(len(curve.keyframe_points) for curve in curves),
            'bones': len(rig.pose.bones),
            'meshes': [{'name': obj.name, 'vertices': len(obj.data.vertices),
                        'polygons': len(obj.data.polygons),
                        'modifiers': [modifier.type for modifier in obj.modifiers]}
                       for obj in meshes]}
args.output.parent.mkdir(parents=True, exist_ok=True)
bpy.ops.wm.save_as_mainfile(filepath=str(args.output.resolve()))
args.output.with_suffix('.json').write_text(json.dumps(metadata, indent=2), encoding='utf-8')
print('ONION_MIXAMO_FIXTURE ' + json.dumps(metadata), flush=True)
