# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""In-place Fill rasterization preserves masks, filters, channel output and companion identity."""

import argparse
import hashlib
import json
import sys
import traceback
from pathlib import Path

import bpy
import numpy as np
from bl_ui import properties_tex_paint_layers as paint

sys.path.insert(0, str(Path(__file__).parent))
from check_mask_paint_stack import fixture

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
    print('LAYER_RASTERIZE_CHECK', name, result, flush=True)
    assert result, name


def rgba(prop):
    values = np.empty(len(prop), dtype=np.float32)
    prop.foreach_get(values)
    return values.reshape(-1, 4)


def op(node, operator, **kwargs):
    with bpy.context.temp_override(node=node):
        return operator(**kwargs)


def channels(node):
    return tuple(rgba(image.pixels).copy() for image in (node.image, node.pbr_image, node.emission_image))


def filters(layer):
    return [(f.type, f.enabled, f.strength, f.gamma, f.blur_radius) for f in layer.color_filters]


def native_mask(layer):
    return {e.uid: rgba(e.pixels).copy() for e in layer.mask_effects if e.type in {0, 1}}


def matches_channels(node, expected, tolerance):
    return all(np.allclose(actual, saved, atol=tolerance, rtol=0)
               for actual, saved in zip(channels(node), expected))


def run_case(is_float):
    label = 'Float' if is_float else 'Byte'
    bpy.ops.mesh.primitive_cube_add()
    node, _, painted_uid = fixture(is_float, size=64)
    material = bpy.context.object.active_material
    material.name = 'Rasterize ' + label
    layer = node.active_layer
    layer.name = label + ' Rasterized Fill'
    layer.fill_color = (2.5, .5, 1.3, .6) if is_float else (.28, .49, .71, .65)
    layer.fill_roughness = .73
    layer.fill_metallic = .23
    layer.fill_height = .37
    layer.fill_specular = .61
    layer.fill_emission = (1.3, .5, .8) if is_float else (.4, .8, .2)
    layer.use_channel_specular = True
    layer.use_channel_emission = True
    painted = next(e for e in layer.mask_effects if e.uid == painted_uid)
    values = rgba(painted.pixels)
    values[10:240, :3] = .12
    values[10:240, 3] = np.resize(np.array([0, 1, 63, 127, 128, 254, 255], dtype=np.float32) / 255, 230)
    painted.pixels.foreach_set(values.ravel())
    layer.mask_stack_evaluate()
    node.paint_target = 'COLOR'
    op(node, bpy.ops.node.tex_paint_color_filter, action='ADD', type='LEVELS' if is_float else 'INVERT')
    layer.color_filters[0].gamma = .8 if is_float else 1
    layer.color_filters[0].strength = .5 if is_float else .25
    if is_float:
        image = bpy.data.images.new('Rasterize HDR Projection', width=64, height=64, float_buffer=True)
        image.colorspace_settings.name = 'Non-Color'
        pixels = np.ones((64 * 64, 4), dtype=np.float32)
        pixels[:, 0] = np.linspace(.2, 1.6, 64 * 64, dtype=np.float32)
        pixels[:, 1] = .7
        pixels[:, 2] = .4
        pixels[:, 3] = np.linspace(.2, .9, 64 * 64, dtype=np.float32)
        image.pixels.foreach_set(pixels.ravel())
        image.pack()
        image.use_fake_user = True
        layer.proj_image_name = image.name
        layer.proj_mode = 'UV_REPEAT'
        op(node, bpy.ops.node.tex_paint_color_filter, action='ADD', type='BLUR')
        layer.color_filters[-1].blur_radius = 2
        layer.color_filters[-1].strength = .5
    companion = node.id_data.nodes.new('ShaderNodeTexPaint')
    companion.label = 'Roughness'
    op(companion, bpy.ops.node.tex_paint_canvas_new, name=label + ' Companion', width=64, height=64,
       color=(.5, .5, .5, 1), **{'float': is_float})
    op(companion, bpy.ops.node.tex_paint_layer_add, type='FILL')
    companion_layer = next(item for item in companion.layers if item.uid == layer.uid)
    companion_layer.fill_color = (.4, .4, .4, .8)
    companion_layer.mask_stack_copy_from(layer)
    uid = layer.uid
    native_before = native_mask(layer)
    mask_before = rgba(layer.mask_pixels).copy()
    metadata_before = node['_grunge_layers_json']
    filters_before = filters(layer)
    channels_before = channels(node)
    companion_before = channels(companion)
    count_before = len(node.layers)

    companion_layer.lock = True
    check(label + ' locked companion rejects rasterization before any type changes', op(
        node, bpy.ops.node.tex_paint_layer_pixel_action, action='RASTERIZE_TO_PAINT') == {'CANCELLED'}
          and layer.type == 'FILL' and companion_layer.type == 'FILL' and len(node.layers) == count_before)
    companion_layer.lock = False
    companion_layer.proj_mode = 'FIT'
    check(label + ' unevaluated companion projection rejects before conversion', op(
        node, bpy.ops.node.tex_paint_layer_pixel_action, action='RASTERIZE_TO_PAINT') == {'CANCELLED'}
          and layer.type == 'FILL' and companion_layer.type == 'FILL')
    check(label + ' failed preflight preserves masks and derived image output',
          np.array_equal(rgba(layer.mask_pixels), mask_before) and
          matches_channels(node, channels_before, 0))
    companion_layer.proj_mode = 'NONE'
    check(label + ' real rasterize operator completes', op(
        node, bpy.ops.node.tex_paint_layer_pixel_action, action='RASTERIZE_TO_PAINT') == {'FINISHED'})
    check(label + ' conversion keeps matching companion layer identities',
          layer.type == 'PAINT' and companion_layer.type == 'PAINT' and layer.uid == uid and
          companion_layer.uid == uid and len(node.layers) == count_before)
    check(label + ' conversion preserves masks and native Paint coverage exactly',
          np.array_equal(rgba(layer.mask_pixels), mask_before) and layer.mask_effects_active and
          all(np.array_equal(rgba(next(e for e in layer.mask_effects if e.uid == eid).pixels), value)
              for eid, value in native_before.items()))
    check(label + ' conversion retains live filters and authoring metadata',
          filters(layer) == filters_before and node['_grunge_layers_json'] == metadata_before)
    check(label + ' conversion detaches color projection',
          layer.proj_mode == 'NONE' and not layer.proj_image_name and not layer.show_projection_widget)
    tolerance = 2e-5 if is_float else 1 / 255 + 1e-6
    check(label + ' all derived color PBR emission output is preserved',
          matches_channels(node, channels_before, tolerance) and
          matches_channels(companion, companion_before, tolerance))
    raw = rgba(node.paint_pixels).copy()
    check(label + ' rasterized paint pixels are editable in the native canvas format',
          raw.shape == (64 * 64, 4) and (not is_float or np.max(raw[:, :3]) > 1))
    layer.color_filters[0].enabled = False
    check(label + ' retained effects remain independently editable',
          not np.array_equal(channels(node)[0], channels_before[0]))
    layer.color_filters[0].enabled = True
    return (material.name, node.name), {
        'uid': uid, 'mask': mask_before, 'native': native_before, 'raw': raw,
        'filters': filters_before, 'metadata': metadata_before, 'channels': channels(node),
        'companion': companion.name, 'companion_channels': channels(companion),
        'tolerance': tolerance,
    }


def workflow():
    expected = dict((run_case(False), run_case(True)))
    path = args.output / 'rasterized-layers.blend'
    check('rasterized fixtures save', bpy.ops.wm.save_as_mainfile(filepath=str(path)) == {'FINISHED'})
    check('rasterized fixtures reopen', bpy.ops.wm.open_mainfile(filepath=str(path)) == {'FINISHED'})
    for (material_name, node_name), saved in expected.items():
        node = bpy.data.materials[material_name].node_tree.nodes[node_name]
        companion = node.id_data.nodes[saved['companion']]
        layer = next(item for item in node.layers if item.uid == saved['uid'])
        check(material_name + ' paint layer identity and source pixels survive reopening',
              layer.type == 'PAINT' and np.array_equal(rgba(node.paint_pixels), saved['raw']))
        check(material_name + ' native mask coverage survives reopening',
              np.array_equal(rgba(layer.mask_pixels), saved['mask']) and
              all(np.array_equal(rgba(next(e for e in layer.mask_effects if e.uid == eid).pixels), value)
                  for eid, value in saved['native'].items()))
        check(material_name + ' live effects and metadata survive reopening',
              filters(layer) == saved['filters'] and node['_grunge_layers_json'] == saved['metadata'])
        check(material_name + ' all channel output survives reopening',
              matches_channels(node, saved['channels'], saved['tolerance']) and
              matches_channels(companion, saved['companion_channels'], saved['tolerance']))
    report['passed'] = True


try:
    workflow()
except Exception:
    report['error'] = traceback.format_exc()
    print(report['error'], flush=True)
(args.output / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
if not report['passed']:
    raise RuntimeError('Layer rasterization regression failed')
