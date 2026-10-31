# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Read the disposable 503.24 fixture with the current runtime, without saving it."""

import json
from pathlib import Path
import sys

import bpy
import numpy as np

assert bpy.app.background and '--factory-startup' in sys.argv
node = bpy.data.node_groups['Legacy Canvas'].nodes['Canvas']
pixels = np.empty(16 * 16 * 4, dtype=np.float32)
node.paint_pixels.foreach_get(pixels)
pixels.shape = (16, 16, 4)
assert not node.selection_active
assert np.allclose(pixels, (1, 0, 0, 1))
assert len(node.selection_mask) == 16 * 16
assert all(len(layer.color_filters) == 0 for layer in node.layers)
report = {'passed': True, 'build_hash': bpy.app.build_hash.decode(),
          'checks': ['legacy selection defaults unrestricted', 'legacy layer pixels preserved',
                     'legacy layers default to no color filters']}
output = Path(sys.argv[sys.argv.index('--') + 1])
output.write_text(json.dumps(report, indent=2), encoding='utf-8')
print('IMAGE_EDIT_LEGACY_RESULT', json.dumps(report))
