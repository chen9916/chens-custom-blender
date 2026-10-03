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
value.show_mesh_onion_skin = True
value.mesh_onion_skin_before = 3
value.mesh_onion_skin_after = 1
value.mesh_onion_skin_step = 4
value.mesh_onion_skin_opacity = 0.22
value.mesh_onion_skin_in_front = True
path = Path(sys.argv[sys.argv.index('--') + 1]).resolve()
bpy.ops.wm.save_as_mainfile(filepath=str(path))
bpy.ops.wm.open_mainfile(filepath=str(path))
value = overlay()
assert value.show_mesh_onion_skin
assert (value.mesh_onion_skin_before, value.mesh_onion_skin_after,
        value.mesh_onion_skin_step) == (3, 1, 4)
assert abs(value.mesh_onion_skin_opacity - 0.22) < 1e-6
assert value.mesh_onion_skin_in_front
print('ONION_SETTINGS_RESULT ' + json.dumps({'defaults': True, 'file_roundtrip': True}))
