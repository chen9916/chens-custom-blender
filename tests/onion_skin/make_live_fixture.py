# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Create a deterministic authored-IK fixture for the native mesh overlay.

Run with a disposable Blender, --factory-startup -b --python this-file --
--vertices 100000 --output absolute-file.blend. This script creates test data;
the onion skin feature and evaluation remain native C++.
"""

import argparse
import json
import math
import sys
from pathlib import Path

import bpy
from mathutils import Quaternion, Vector


def create_fixture(vertex_target):
    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete(use_global=False)
    armature = bpy.data.armatures.new("OnionFixtureRig")
    rig = bpy.data.objects.new("OnionFixtureRig", armature)
    bpy.context.collection.objects.link(rig)
    bpy.context.view_layer.objects.active = rig
    rig.select_set(True)
    bpy.ops.object.mode_set(mode='EDIT')
    upper = armature.edit_bones.new("Upper")
    upper.head = (0, 0, 0)
    upper.tail = (0, 0, 1)
    lower = armature.edit_bones.new("Lower")
    lower.head = upper.tail
    lower.tail = (0.15, 0, 2)
    lower.parent = upper
    lower.use_connect = True
    control = armature.edit_bones.new("Control")
    control.head = lower.tail
    control.tail = (0.15, 0, 2.25)
    control.use_deform = False
    bpy.ops.object.mode_set(mode='OBJECT')
    constraint = rig.pose.bones["Lower"].constraints.new('IK')
    constraint.target = rig
    constraint.subtarget = "Control"
    constraint.chain_count = 2
    constraint.iterations = 64
    rig.show_in_front = True
    segments = 32
    rings = max(8, vertex_target // segments)
    positions = [
        (0.075 * math.cos(math.tau * j / segments),
         0.075 * math.sin(math.tau * j / segments),
         2 * i / (rings - 1))
        for i in range(rings) for j in range(segments)
    ]
    faces = [
        (i * segments + j, i * segments + (j + 1) % segments,
         (i + 1) * segments + (j + 1) % segments, (i + 1) * segments + j)
        for i in range(rings - 1) for j in range(segments)
    ]
    mesh = bpy.data.meshes.new("OnionFixtureMesh")
    mesh.from_pydata(positions, [], faces)
    mesh.update()
    body = bpy.data.objects.new("OnionFixtureMesh", mesh)
    bpy.context.collection.objects.link(body)
    upper_group = body.vertex_groups.new(name="Upper")
    lower_group = body.vertex_groups.new(name="Lower")
    for i in range(rings):
        weight = max(0, min(1, (2 * i / (rings - 1) - 0.8) / 0.4))
        indices = list(range(i * segments, (i + 1) * segments))
        if weight < 1:
            upper_group.add(indices, 1 - weight, 'REPLACE')
        if weight > 0:
            lower_group.add(indices, weight, 'REPLACE')
    modifier = body.modifiers.new("Armature", 'ARMATURE')
    modifier.object = rig
    body.shape_key_add(name="Basis")
    bulge = body.shape_key_add(name="Bulge")
    for point in bulge.data:
        scale = 1 + 0.2 * math.sin(math.pi * point.co.z / 2)
        point.co.x *= scale
        point.co.y *= scale
    bulge.value = 0.3
    control = rig.pose.bones["Control"]
    for frame, x in ((1, -0.55), (12, 0), (24, 0.55)):
        control.location = (x, 0, -0.25)
        control.keyframe_insert(data_path="location", frame=frame)
    scene = bpy.context.scene
    scene.frame_start = 1
    scene.frame_end = 24
    scene.frame_set(12)
    scene.tool_settings.use_keyframe_insert_auto = True
    for bone in rig.pose.bones:
        bone.select = False
    rig.pose.bones["Control"].select = True
    armature.bones.active = armature.bones["Control"]
    bpy.ops.object.mode_set(mode='POSE')
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type != 'VIEW_3D':
                continue
            view = area.spaces.active
            view.region_3d.view_rotation = Quaternion((1, 0, 0), math.pi / 2)
            view.region_3d.view_location = Vector((0, 0, 1))
            view.region_3d.view_distance = 3.5
            view.region_3d.view_perspective = 'ORTHO'
            if hasattr(view.overlay, "show_mesh_onion_skin"):
                view.overlay.show_mesh_onion_skin = True
                view.overlay.mesh_onion_skin_before = 2
                view.overlay.mesh_onion_skin_after = 2
                view.overlay.mesh_onion_skin_step = 2
                view.overlay.mesh_onion_skin_opacity = 0.2
    return rig, body


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--vertices', type=int, default=100000)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
    rig, body = create_fixture(args.vertices)
    bpy.ops.wm.save_as_mainfile(filepath=str(args.output.resolve()))
    print("ONION_FIXTURE " + json.dumps({
        "version": bpy.app.version_string,
        "build_hash": bpy.app.build_hash.decode(),
        "vertices": len(body.data.vertices),
        "bones": len(rig.data.bones),
        "authored_ik": True,
        "output": str(args.output.resolve()),
    }))


if __name__ == '__main__':
    main()
