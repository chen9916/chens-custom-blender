# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Check native overlay defaults and persistent settings with factory Blender."""

import json
import sys
from pathlib import Path

import bpy


def overlay():
    return next(area.spaces.active.overlay for area in bpy.context.screen.areas
                if area.type == 'VIEW_3D')


value = overlay()
assert not value.show_mesh_onion_skin
assert (value.mesh_onion_skin_before, value.mesh_onion_skin_after,
        value.mesh_onion_skin_step) == (2, 2, 1)
assert abs(value.mesh_onion_skin_opacity - 0.15) < 1e-6
assert tuple(round(channel, 6) for channel in value.mesh_onion_skin_past_color) == (
    0.16, 0.55, 1.0)
assert tuple(round(channel, 6) for channel in value.mesh_onion_skin_future_color) == (
    1.0, 0.25, 0.14)
assert not value.mesh_onion_skin_keyed_frames_only
assert not value.mesh_onion_skin_loop_frame_range
assert not value.mesh_onion_skin_distance_fade
assert abs(value.mesh_onion_skin_fade_strength - 0.75) < 1e-6
assert not value.mesh_onion_skin_screen_offset
assert not value.mesh_onion_skin_screen_offset_reverse
assert abs(value.mesh_onion_skin_screen_offset_spacing - 24.0) < 1e-6
assert not value.mesh_onion_skin_screen_offset_world_space
assert abs(value.mesh_onion_skin_screen_offset_distance - 0.1) < 1e-6
value.show_mesh_onion_skin = True
value.mesh_onion_skin_before = 3
value.mesh_onion_skin_after = 1
value.mesh_onion_skin_step = 4
value.mesh_onion_skin_opacity = 0.22
value.mesh_onion_skin_past_color = (0.05, 0.35, 0.9)
value.mesh_onion_skin_future_color = (0.95, 0.12, 0.05)
value.mesh_onion_skin_in_front = True
value.mesh_onion_skin_keyed_frames_only = True
value.mesh_onion_skin_loop_frame_range = True
value.mesh_onion_skin_distance_fade = True
value.mesh_onion_skin_fade_strength = 0.61
value.mesh_onion_skin_screen_offset = True
value.mesh_onion_skin_screen_offset_reverse = True
value.mesh_onion_skin_screen_offset_spacing = 43.5
value.mesh_onion_skin_screen_offset_world_space = True
value.mesh_onion_skin_screen_offset_distance = 0.375
path = Path(sys.argv[sys.argv.index('--') + 1]).resolve()
bpy.ops.wm.save_as_mainfile(filepath=str(path))
bpy.ops.wm.open_mainfile(filepath=str(path))
value = overlay()
assert value.show_mesh_onion_skin
assert (value.mesh_onion_skin_before, value.mesh_onion_skin_after,
        value.mesh_onion_skin_step) == (3, 1, 4)
assert abs(value.mesh_onion_skin_opacity - 0.22) < 1e-6
assert tuple(round(channel, 6) for channel in value.mesh_onion_skin_past_color) == (
    0.05, 0.35, 0.9)
assert tuple(round(channel, 6) for channel in value.mesh_onion_skin_future_color) == (
    0.95, 0.12, 0.05)
assert value.mesh_onion_skin_in_front
assert value.mesh_onion_skin_keyed_frames_only
assert value.mesh_onion_skin_loop_frame_range
assert value.mesh_onion_skin_distance_fade
assert abs(value.mesh_onion_skin_fade_strength - 0.61) < 1e-6
assert value.mesh_onion_skin_screen_offset
assert value.mesh_onion_skin_screen_offset_reverse
assert abs(value.mesh_onion_skin_screen_offset_spacing - 43.5) < 1e-6
assert value.mesh_onion_skin_screen_offset_world_space
assert abs(value.mesh_onion_skin_screen_offset_distance - 0.375) < 1e-6

# Use a native posed armature to exercise sampling synchronization between enabled View3Ds.
# No fixture mesh or worker is needed to check these RNA update semantics.
bpy.ops.object.armature_add()
rig = bpy.context.object
bpy.ops.object.mode_set(mode='POSE')
area = next(area for area in bpy.context.screen.areas if area.type == 'VIEW_3D')
with bpy.context.temp_override(window=bpy.context.window, area=area):
    assert bpy.ops.screen.area_split(direction='VERTICAL', factor=0.5) == {'FINISHED'}
views = [area.spaces.active for area in bpy.context.screen.areas if area.type == 'VIEW_3D']
assert len(views) >= 2
first, second = [view.overlay for view in views[:2]]
first.show_mesh_onion_skin = True
second.show_mesh_onion_skin = True
first.mesh_onion_skin_keyed_frames_only = False
assert not second.mesh_onion_skin_keyed_frames_only
first.mesh_onion_skin_loop_frame_range = False
assert not second.mesh_onion_skin_loop_frame_range
second.mesh_onion_skin_keyed_frames_only = True
second.mesh_onion_skin_loop_frame_range = True
assert first.mesh_onion_skin_keyed_frames_only and first.mesh_onion_skin_loop_frame_range
before = (second.mesh_onion_skin_distance_fade, second.mesh_onion_skin_fade_strength)
first.mesh_onion_skin_distance_fade = not before[0]
first.mesh_onion_skin_fade_strength = 0.13
assert (second.mesh_onion_skin_distance_fade, second.mesh_onion_skin_fade_strength) == before
colors_before = (tuple(second.mesh_onion_skin_past_color),
                 tuple(second.mesh_onion_skin_future_color))
first.mesh_onion_skin_past_color = (0.2, 0.4, 0.8)
first.mesh_onion_skin_future_color = (0.9, 0.15, 0.05)
assert (tuple(second.mesh_onion_skin_past_color),
        tuple(second.mesh_onion_skin_future_color)) == colors_before
screen_offset_before = (second.mesh_onion_skin_screen_offset,
                        second.mesh_onion_skin_screen_offset_reverse,
                        second.mesh_onion_skin_screen_offset_spacing,
                        second.mesh_onion_skin_screen_offset_world_space,
                        second.mesh_onion_skin_screen_offset_distance)
first.mesh_onion_skin_screen_offset = not screen_offset_before[0]
first.mesh_onion_skin_screen_offset_reverse = not screen_offset_before[1]
first.mesh_onion_skin_screen_offset_spacing = 12.5
first.mesh_onion_skin_screen_offset_world_space = not screen_offset_before[3]
first.mesh_onion_skin_screen_offset_distance = 0.25
assert (second.mesh_onion_skin_screen_offset,
        second.mesh_onion_skin_screen_offset_reverse,
        second.mesh_onion_skin_screen_offset_spacing,
        second.mesh_onion_skin_screen_offset_world_space,
        second.mesh_onion_skin_screen_offset_distance) == screen_offset_before
assert first.mesh_onion_skin_screen_offset != second.mesh_onion_skin_screen_offset
assert first.mesh_onion_skin_screen_offset_reverse != second.mesh_onion_skin_screen_offset_reverse
assert first.mesh_onion_skin_screen_offset_spacing != second.mesh_onion_skin_screen_offset_spacing
assert first.mesh_onion_skin_screen_offset_world_space != second.mesh_onion_skin_screen_offset_world_space
assert first.mesh_onion_skin_screen_offset_distance != second.mesh_onion_skin_screen_offset_distance
for view in views:
    view.overlay.show_mesh_onion_skin = False
print('ONION_SETTINGS_RESULT ' + json.dumps({'defaults': True, 'file_roundtrip': True,
                                           'shared_sampling': True, 'local_fade': True,
                                           'local_colors': True, 'local_screen_offset': True,
                                           'local_scene_distance': True}))
