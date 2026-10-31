# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Flatten layer appearances without altering linked copies or losing material channels."""

import argparse
import json
import sys
import traceback
from pathlib import Path

import bpy
import numpy as np
from bl_ui import properties_tex_paint_layers as paint

parser = argparse.ArgumentParser()
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
assert '--factory-startup' in sys.argv and bpy.app.background and not bpy.data.filepath
args.output.mkdir(parents=True, exist_ok=True)
report = {'build_hash': bpy.app.build_hash.decode(), 'build_time': bpy.app.build_time.decode(),
          'checks': [], 'passed': False}


def check(name, condition):
    condition = bool(condition)
    report['checks'].append({'name': name, 'passed': condition})
    print('LAYER_FLATTEN_CHECK', name, condition, flush=True)
    assert condition, name


def op(node, operator, **kwargs):
    with bpy.context.temp_override(node=node):
        return operator(**kwargs)


def pixels(image):
    result = np.empty(len(image.pixels), dtype=np.float32)
    image.pixels.foreach_get(result)
    return result.copy()


def layer_pixels(node, uid, values=None):
    node.active_layer_index = next(i for i, layer in enumerate(node.layers) if layer.uid == uid)
    node.paint_target = 'COLOR'
    if values is not None:
        node.paint_pixels.foreach_set(values.ravel())
    result = np.empty(len(node.paint_pixels), dtype=np.float32)
    node.paint_pixels.foreach_get(result)
    return result.copy()


def fixture(is_float):
    material = bpy.data.materials.new('Flatten Channels')
    material.use_nodes = True
    bpy.context.object.data.materials.clear()
    bpy.context.object.data.materials.append(material)
    nodes = []
    for label in ('Base Color', 'Roughness'):
        node = material.node_tree.nodes.new('ShaderNodeTexPaint')
        node.label = label
        op(node, bpy.ops.node.tex_paint_canvas_new, name='Flatten ' + label,
           width=16, height=8, color=(.3, .2, .1, .75), **{'float': is_float})
        nodes.append(node)
    primary, companion = nodes
    op(primary, bpy.ops.node.tex_paint_layer_add, type='PAINT')
    base_uid, top_uid = [layer.uid for layer in primary.layers]
    for index, node in enumerate(nodes):
        base = np.full((8, 16, 4), (.3, .2, .1, .75), dtype=np.float32)
        top = np.full((8, 16, 4), (.7, .1, .4, .35), dtype=np.float32)
        if index:
            base[..., :3] = .8
            top[..., :3] = .2
        elif is_float:
            base[..., 0] = 2.0
        top[:, :8, 3] = .7
        layer_pixels(node, base_uid, base)
        layer_pixels(node, top_uid, top)
    return primary, companion, base_uid, top_uid


def flatten_appearance(is_float):
    label = 'float' if is_float else 'byte'
    primary, companion, base_uid, top_uid = fixture(is_float)
    op(primary, bpy.ops.node.tex_paint_color_filter, action='ADD', type='INVERT')
    primary.active_layer.color_filters[0].strength = .5
    op(primary, bpy.ops.node.tex_paint_mask_effect_add, effect_type='PAINT')
    primary.active_layer.preview_mask = False
    before = [pixels(node.image) for node in (primary, companion)]
    primary.active_layer.preview_mask = True
    check(label + ' flatten ignores a temporary mask preview',
          op(primary, bpy.ops.node.tex_paint_layers_flatten, visible_only=False) == {'FINISHED'} and
          np.allclose(pixels(primary.image), before[0], atol=2/255 if not is_float else 2e-6))
    check(label + ' flattened companion preserves its own pixels',
          np.allclose(pixels(companion.image), before[1], atol=2/255 if not is_float else 2e-6))
    check(label + ' flattened layers are fresh paint without baked effects remaining',
          all(len(node.layers) == 1 and node.active_layer.type == 'PAINT' and
              not node.active_layer.color_filters and not node.active_layer.mask_effects and
              not node.active_layer.use_mask and not node.active_layer.preview_mask and
              node.active_layer.proj_mode == 'NONE' and node.active_layer.opacity == 1
              for node in (primary, companion)))
    check(label + ' companion flattened UIDs stay synchronized',
          primary.active_layer.uid == companion.active_layer.uid)


def visible_hidden_layers():
    primary, companion, base_uid, top_uid = fixture(True)
    primary.active_layer_index = 0
    op(primary, bpy.ops.node.tex_paint_layer_add, type='PAINT')
    hidden_uid = primary.active_layer.uid
    raw = np.full((8, 16, 4), (.9, .8, .7, .6), dtype=np.float32)
    hidden_sources = []
    for node in (primary, companion):
        hidden_sources.append(layer_pixels(node, hidden_uid, raw))
    # Visibility/lock changes can synchronize to companion channels. Finish both
    # editable source buffers before protecting either channel's layer.
    for node in (primary, companion):
        layer = next(layer for layer in node.layers if layer.uid == hidden_uid)
        layer.hide = True
    for node in (primary, companion):
        layer = next(layer for layer in node.layers if layer.uid == hidden_uid)
        layer.lock = True
    before = [pixels(node.image) for node in (primary, companion)]
    check('Merge Visible retains hidden locked layers',
          op(primary, bpy.ops.node.tex_paint_layers_flatten, visible_only=True) == {'FINISHED'})
    check('Merge Visible inserts below the retained hidden layer consistently',
          all(len(node.layers) == 2 and node.layers[1].uid == hidden_uid and
              node.layers[1].hide and node.layers[1].lock for node in (primary, companion)))
    check('Merge Visible preserves the current channel appearances',
          all(np.allclose(pixels(node.image), values, atol=2e-6)
              for node, values in zip((primary, companion), before)))
    # Inspect the retained source through the public editable-buffer API after
    # explicitly showing and unlocking it, as an artist would do before editing.
    for node in (primary, companion):
        layer = next(layer for layer in node.layers if layer.uid == hidden_uid)
        layer.hide = False
    for node in (primary, companion):
        layer = next(layer for layer in node.layers if layer.uid == hidden_uid)
        layer.lock = False
    check('retained hidden source pixels remain exact',
          all(np.array_equal(layer_pixels(node, hidden_uid), source)
              for node, source in zip((primary, companion), hidden_sources)))


def material_channels():
    primary, companion, base_uid, top_uid = fixture(True)
    primary.active_layer_index = 1
    top = primary.active_layer
    top.use_channel_roughness = top.use_channel_metallic = top.use_channel_height = True
    top.use_channel_specular = True
    top.fill_roughness = .1
    top.fill_metallic = .8
    top.fill_height = .35
    # The existing canvas has default Specular .5; retaining that value keeps it uniform.
    top.fill_specular = .5
    before = pixels(primary.pbr_image)
    check('flatten preserves representable PBR channels',
          op(primary, bpy.ops.node.tex_paint_layers_flatten) == {'FINISHED'} and
          np.allclose(pixels(primary.pbr_image), before, atol=2e-6))
    primary, companion, base_uid, top_uid = fixture(True)
    primary.active_layer_index = 1
    primary.active_layer.use_channel_specular = True
    primary.active_layer.fill_specular = 1
    before_uids = [[layer.uid for layer in node.layers] for node in (primary, companion)]
    before = [pixels(node.image) for node in (primary, companion)]
    check('spatial Specular rejection leaves every channel intact',
          op(primary, bpy.ops.node.tex_paint_layers_flatten) == {'CANCELLED'} and
          all([layer.uid for layer in node.layers] == uids and np.array_equal(pixels(node.image), values)
              for node, uids, values in zip((primary, companion), before_uids, before)))
    primary.active_layer.use_channel_specular = False
    primary.active_layer.use_channel_emission = True
    check('nonzero Emission rejection leaves layers intact',
          op(primary, bpy.ops.node.tex_paint_layers_flatten) == {'CANCELLED'} and
          all([layer.uid for layer in node.layers] == uids
              for node, uids in zip((primary, companion), before_uids)))


def locked_preflight():
    primary, companion, base_uid, top_uid = fixture(False)
    companion.active_layer.lock = True
    before = [[layer.uid for layer in node.layers] for node in (primary, companion)]
    check('locked companion prevents any flatten mutation',
          op(primary, bpy.ops.node.tex_paint_layers_flatten) == {'CANCELLED'} and
          all([layer.uid for layer in node.layers] == uids
              for node, uids in zip((primary, companion), before)))


for name, run in (
        ('byte_appearance', lambda: flatten_appearance(False)),
        ('float_appearance', lambda: flatten_appearance(True)),
        ('visible_hidden_layers', visible_hidden_layers),
        ('material_channels', material_channels),
        ('locked_preflight', locked_preflight)):
    try:
        run()
    except Exception:
        report.setdefault('errors', []).append({'case': name, 'traceback': traceback.format_exc()})
        print(report['errors'][-1]['traceback'], flush=True)
report['passed'] = not report.get('errors') and all(item['passed'] for item in report['checks'])
(args.output / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
if not report['passed']:
    raise RuntimeError('Layer flatten regression failed; see report.json')
