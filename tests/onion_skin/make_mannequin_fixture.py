# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Prepare disposable authored-rig and NLA variants without changing the source file."""

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
parser.add_argument('--variant', choices=('active', 'nla-tweak', 'nla-only'), default='active')
parser.add_argument('--rig', default='rig')
parser.add_argument('--bone', default='c_hand_ik.l')
parser.add_argument('--frame', type=int, default=17)
args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
assert args.input.resolve() != args.output.resolve(), 'Never overwrite the original asset'
bpy.ops.wm.open_mainfile(filepath=str(args.input.resolve()), use_scripts=False)
bpy.context.preferences.filepaths.use_auto_save_temporary_files = False
rig = bpy.data.objects[args.rig]
assert rig.type == 'ARMATURE' and rig.pose and args.bone in rig.pose.bones
adt = rig.animation_data
assert adt and adt.action, 'The original rig must have an active authored Action'
action = adt.action
slot = adt.action_slot
curves = [curve for layer in action.layers for strip in layer.strips
          for bag in strip.channelbags for curve in bag.fcurves]
original_times = sorted({float(key.co.x) for curve in curves for key in curve.keyframe_points})
original_keys = sum(len(curve.keyframe_points) for curve in curves)
if bpy.context.object and bpy.context.object.mode != 'OBJECT':
    bpy.ops.object.mode_set(mode='OBJECT')
for obj in bpy.context.selected_objects:
    obj.select_set(False)
rig.hide_set(False)
rig.hide_viewport = False
rig.select_set(True)
bpy.context.view_layer.objects.active = rig
rig.show_in_front = True
for pose_bone in rig.pose.bones:
    pose_bone.select = False
control = rig.pose.bones[args.bone]
control.bone.hide = False
for collection in control.bone.collections:
    while collection:
        collection.is_visible = True
        collection = collection.parent
control.select = True
rig.data.bones.active = control.bone
bpy.ops.object.mode_set(mode='POSE')
scene = bpy.context.scene
scene.tool_settings.use_keyframe_insert_auto = True
scene.tool_settings.auto_keying_mode = 'ADD_REPLACE_KEYS'
scene.tool_settings.use_keyframe_insert_keyingset = False
if args.variant != 'active':
    assert not adt.use_tweak_mode and not adt.nla_tracks, 'Create NLA variants from the active-Action original'
    track = adt.nla_tracks.new()
    track.name = 'OnionFixtureOffsetScaled'
    track.select = True
    strip = track.strips.new('OnionFixtureRun', 5, action)
    strip.action_slot = slot
    strip.scale = 1.5
    strip.blend_type = 'REPLACE'
    strip.influence = 1.0
    strip.extrapolation = 'HOLD'
    strip.select = True
    adt.action = None
    scene.frame_end = max(scene.frame_end, math.ceil(strip.frame_end))
    if args.variant == 'nla-tweak':
        area = next(area for area in bpy.context.screen.areas if area.type == 'VIEW_3D')
        previous_type = area.type
        try:
            area.type = 'NLA_EDITOR'
            region = next(region for region in area.regions if region.type == 'WINDOW')
            with bpy.context.temp_override(window=bpy.context.window, area=area, region=region):
                result = bpy.ops.nla.tweakmode_enter(isolate_action=False)
            assert result == {'FINISHED'} and adt.use_tweak_mode and adt.action == action
        finally:
            area.type = previous_type
    else:
        assert adt.action is None and not adt.use_tweak_mode
scene.frame_set(args.frame)
bpy.context.view_layer.update()
meshes = [obj for obj in scene.objects if obj.type == 'MESH' and
          any(modifier.type == 'ARMATURE' and modifier.object == rig for modifier in obj.modifiers)]
assert meshes, 'The rig must deform a character mesh'
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
            if hasattr(view.overlay, 'show_mesh_onion_skin'):
                view.overlay.show_mesh_onion_skin = False
                for property_name in ('mesh_onion_skin_keyed_frames_only',
                                      'mesh_onion_skin_loop_frame_range',
                                      'mesh_onion_skin_distance_fade'):
                    if hasattr(view.overlay, property_name):
                        setattr(view.overlay, property_name, False)
assert sum(len(curve.keyframe_points) for curve in curves) == original_keys
assert sorted({float(key.co.x) for curve in curves for key in curve.keyframe_points}) == original_times
metadata = {'input': str(args.input.resolve()), 'variant': args.variant, 'rig': rig.name,
            'bone': control.name, 'frame': scene.frame_current,
            'range': [scene.frame_start, scene.frame_end], 'bones': len(rig.pose.bones),
            'drivers': len(adt.drivers), 'action': action.name, 'curves': len(curves),
            'keys': original_keys, 'key_times': original_times,
            'active_action': adt.action.name if adt.action else None,
            'use_tweak_mode': adt.use_tweak_mode,
            'nla': [{'track': nla_track.name, 'strip': nla_strip.name,
                     'action': nla_strip.action.name if nla_strip.action else None,
                     'start': nla_strip.frame_start, 'end': nla_strip.frame_end,
                     'scale': nla_strip.scale, 'repeat': nla_strip.repeat}
                    for nla_track in adt.nla_tracks for nla_strip in nla_track.strips],
            'meshes': [{'name': obj.name, 'vertices': len(obj.data.vertices),
                        'modifiers': [modifier.type for modifier in obj.modifiers]}
                       for obj in meshes]}
args.output.parent.mkdir(parents=True, exist_ok=True)
bpy.ops.wm.save_as_mainfile(filepath=str(args.output.resolve()))
args.output.with_suffix('.json').write_text(json.dumps(metadata, indent=2), encoding='utf-8')
print('ONION_MANNEQUIN_FIXTURE ' + json.dumps(metadata), flush=True)
