# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Native Paint Mask coverage and evaluated output survive .blend encoding/reopening."""

import argparse
import hashlib
import json
import sys
import traceback
from pathlib import Path

import bpy
import numpy as np


parser = argparse.ArgumentParser()
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
assert '--factory-startup' in sys.argv and bpy.app.background and not bpy.data.filepath
args.output.mkdir(parents=True, exist_ok=True)
report = {'build_hash': bpy.app.build_hash.decode(), 'build_time': bpy.app.build_time.decode(),
          'runtime': bpy.app.binary_path, 'checks': [], 'passed': False,
          'test_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}


def check(name, condition):
    result = bool(condition)
    report['checks'].append({'name': name, 'passed': result})
    print('NATIVE_MASK_STORAGE_CHECK', name, result, flush=True)
    assert result, name


def array(prop):
    values = np.empty(len(prop), dtype=np.float32)
    prop.foreach_get(values)
    return values.reshape(-1, 4)


def bytes_rgba(prop):
    return np.rint(np.clip(array(prop), 0, 1) * 255).astype(np.uint8)


def fixture(is_float):
    label = 'Float' if is_float else 'Byte'
    material = bpy.data.materials.new('Native Mask ' + label)
    material.use_nodes = True
    bpy.context.object.data.materials.append(material)
    node = material.node_tree.nodes.new('ShaderNodeTexPaint')
    material.node_tree.nodes.active = node
    with bpy.context.temp_override(node=node):
        bpy.ops.node.tex_paint_canvas_new(name='Native Mask ' + label, width=16, height=8,
                                       color=(.2, .3, .4, 1), **{'float': is_float})
        bpy.ops.node.tex_paint_layer_add(type='FILL')
    layer = node.active_layer
    layer.name = label + ' Masked Fill'
    layer.use_mask = True
    node.paint_target = 'MASK'
    source = layer.mask_effect_ensure(1, 0)
    source_values = np.ones((128, 4), dtype=np.float32)
    source_values[:, :3] = np.linspace(64 / 255, 224 / 255, 128, dtype=np.float32)[:, None]
    source.pixels.foreach_set(source_values.ravel())
    painted = layer.mask_effect_ensure(2, 1)
    painted.blend_mode = 3  # Native MULTIPLY: coverage controls contribution independently.
    values = np.zeros((128, 4), dtype=np.float32)
    coverage = np.array([0, 1, 63, 127, 128, 254, 255], dtype=np.float32) / 255
    values[:, 3] = np.resize(coverage, 128)
    grayscale = np.resize(np.array([0, 17, 64, 123, 192, 231, 255], dtype=np.float32), 128) / 255
    values[:, :3] = grayscale[:, None]
    painted.pixels.foreach_set(values.ravel())
    layer.mask_base_value = 255
    layer.mask_effects_active = True
    layer.active_mask_effect_uid = 2
    check(label + ' stack evaluates before saving', layer.mask_stack_evaluate())
    check(label + ' coverage includes every edge case',
          set(bytes_rgba(painted.pixels)[:, 3]) == {0, 1, 63, 127, 128, 254, 255})
    return material, node


def workflow():
    expected = {}
    for is_float in (False, True):
        material, node = fixture(is_float)
        layer = node.active_layer
        expected[material.name] = {
            'mask': bytes_rgba(layer.mask_pixels),
            'effects': {effect.uid: bytes_rgba(effect.pixels) for effect in layer.mask_effects},
            'native_order': [effect.uid for effect in layer.mask_effects],
        }
    path = args.output / 'native-mask-storage.blend'
    check('native mask fixture saves', bpy.ops.wm.save_as_mainfile(filepath=str(path)) == {'FINISHED'})
    check('native mask fixture reopens', bpy.ops.wm.open_mainfile(filepath=str(path)) == {'FINISHED'})
    for material_name, stored in expected.items():
        label = 'Float' if material_name.endswith('Float') else 'Byte'
        node = next(n for n in bpy.data.materials[material_name].node_tree.nodes
                    if n.bl_idname == 'ShaderNodeTexPaint')
        layer = node.active_layer
        check(label + ' native stack selection and order persist', layer.mask_effects_active and
              layer.active_mask_effect_uid == 2 and [e.uid for e in layer.mask_effects] == stored['native_order'])
        check(label + ' evaluated mask pixels survive reopening',
              np.array_equal(bytes_rgba(layer.mask_pixels), stored['mask']))
        for effect in layer.mask_effects:
            old = stored['effects'][effect.uid]
            current = bytes_rgba(effect.pixels)
            check(label + ' effect ' + str(effect.uid) + ' alpha survives exactly',
                  np.array_equal(current[:, 3], old[:, 3]))
            covered = old[:, 3] > 0
            check(label + ' effect ' + str(effect.uid) + ' visible values survive exactly',
                  np.array_equal(current[covered, :3], old[covered, :3]))
        check(label + ' loaded native stack evaluates without Python authoring cache', layer.mask_stack_evaluate())
        check(label + ' native reevaluation reproduces saved mask',
              np.array_equal(bytes_rgba(layer.mask_pixels), stored['mask']))
        painted = next(e for e in layer.mask_effects if e.uid == 2)
        erased = array(painted.pixels)
        erased[:, 3] = 0
        painted.pixels.foreach_set(erased.ravel())
        layer.mask_stack_evaluate()
        source = next(e for e in layer.mask_effects if e.uid == 1)
        check(label + ' erasing restored coverage reveals original generator values',
              np.array_equal(bytes_rgba(layer.mask_pixels)[:, 0], bytes_rgba(source.pixels)[:, 0]))
    report['passed'] = True


try:
    workflow()
except Exception:
    report['error'] = traceback.format_exc()
    print(report['error'], flush=True)
(args.output / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
if not report['passed']:
    raise RuntimeError('Native mask storage regression failed')
