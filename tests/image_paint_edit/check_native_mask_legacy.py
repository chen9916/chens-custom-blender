# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Migrate a pre-native mask stack without losing its independently stored paint."""

import argparse
import base64
import hashlib
import json
import sys
import traceback
import zlib
from pathlib import Path

import bpy
import numpy as np
from bl_ui import properties_tex_paint_layers as paint

parser = argparse.ArgumentParser()
parser.add_argument('--fixture', type=Path, required=True)
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
assert '--factory-startup' in sys.argv and bpy.app.background and not bpy.data.filepath
args.output.mkdir(parents=True, exist_ok=True)
report = {'build_hash': bpy.app.build_hash.decode(), 'build_time': bpy.app.build_time.decode(),
          'fixture': str(args.fixture), 'checks': [], 'passed': False}


def check(name, condition):
    report['checks'].append({'name': name, 'passed': bool(condition)})
    print('LEGACY_MASK_CHECK', name, bool(condition), flush=True)
    assert condition, name


try:
    fixture_hash = hashlib.sha256(args.fixture.read_bytes()).hexdigest()
    bpy.ops.wm.open_mainfile(filepath=str(args.fixture))
    checked = 0
    stored = []
    for material in bpy.data.materials:
        if not material.node_tree:
            continue
        for node in material.node_tree.nodes:
            if node.bl_idname != 'ShaderNodeTexPaint':
                continue
            for layer in node.layers:
                state = paint._get_layer_grunge_state(node, layer)
                if not layer.use_mask or not state.get('effects'):
                    continue
                check('legacy stack publishes native effects on load', layer.mask_effects_active)
                for effect in state['effects']:
                    if effect.get('type') != 'PAINT' or not effect.get('paint_z64'):
                        continue
                    native = next(e for e in layer.mask_effects if e.uid == int(effect['uid']))
                    raw = np.frombuffer(zlib.decompress(base64.b64decode(effect['paint_z64'])), dtype=np.uint8)
                    count = len(raw) // 2
                    rgba = np.empty(count * 4, dtype=np.float32)
                    native.pixels.foreach_get(rgba)
                    rgba = np.rint(np.clip(rgba.reshape(-1, 4), 0, 1) * 255).astype(np.uint8)
                    check('legacy coverage survives migration exactly', np.array_equal(rgba[:, 3], raw[count:]))
                    visible = raw[count:] > 0
                    check('legacy visible paint values survive migration exactly',
                          np.array_equal(rgba[visible, 0], raw[:count][visible]))
                    stored.append((material.name, node.name, layer.uid, native.uid, rgba.copy()))
                    checked += 1
                before = np.array(layer.mask_pixels[:], dtype=np.float32)
                paint._clear_all_runtime_mask_caches(clear_raw_cache=True)
                layer.mask_stack_evaluate()
                check('native evaluation survives clearing Python caches',
                      np.allclose(before, layer.mask_pixels[:], atol=1/255))
    check('fixture contains saved painted coverage', checked > 0)
    migrated = args.output / 'migrated-native-mask.blend'
    check('migrated native mask saves to a disposable copy',
          bpy.ops.wm.save_as_mainfile(filepath=str(migrated)) == {'FINISHED'})
    check('migrated native mask reopens', bpy.ops.wm.open_mainfile(filepath=str(migrated)) == {'FINISHED'})
    for material_name, node_name, layer_uid, effect_uid, expected in stored:
        node = bpy.data.materials[material_name].node_tree.nodes[node_name]
        layer = next(l for l in node.layers if l.uid == layer_uid)
        native = next(e for e in layer.mask_effects if e.uid == effect_uid)
        values = np.asarray(native.pixels[:], dtype=np.float32).reshape(-1, 4)
        actual = np.rint(np.clip(values, 0, 1) * 255).astype(np.uint8)
        check('migrated native Paint coverage persists exactly', np.array_equal(actual, expected))
        saved = json.loads(node['_grunge_layers_json'])[str(layer_uid)]
        effect = next(e for e in saved['effects'] if e['uid'] == effect_uid)
        check('migrated authoring JSON omits duplicate native Paint pixels',
              not {'paint_z64', 'paint_w', 'paint_h'}.intersection(effect))
    check('legacy input fixture remains unchanged', hashlib.sha256(args.fixture.read_bytes()).hexdigest() == fixture_hash)
    report['passed'] = True
except Exception:
    report['error'] = traceback.format_exc()
    print(report['error'], flush=True)
finally:
    (args.output / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
if not report['passed']:
    raise RuntimeError('Legacy native-mask migration failed')
