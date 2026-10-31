# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Basic layer edits detach copied paint buffers and preserve canvas encoding."""

import argparse
import ctypes
import hashlib
import json
import sys
import traceback
from pathlib import Path

import bpy
import numpy as np
from mathutils import Color
from bl_ui import properties_tex_paint_layers as paint

parser = argparse.ArgumentParser()
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
assert '--factory-startup' in sys.argv and bpy.app.background and not bpy.data.filepath
args.output.mkdir(parents=True, exist_ok=True)
report = {'build_hash': bpy.app.build_hash.decode(), 'build_time': bpy.app.build_time.decode(),
          'runtime': bpy.app.binary_path, 'checks': [], 'shared_buffer_cases': [], 'passed': False}
actions = ('FILL_BRUSH', 'CLEAR', 'INVERT_RGB', 'FLIP_H', 'FLIP_V')
brush_color = (.0031308, .18, .7)


def check(name, condition):
    result = bool(condition)
    report['checks'].append({'name': name, 'passed': result})
    print('LAYER_PIXEL_ACTION_CHECK', name, result, flush=True)
    assert result, name


def pixels(node):
    previous = node.paint_target
    try:
        node.paint_target = 'COLOR'
        values = np.empty(len(node.paint_pixels), dtype=np.float32)
        node.paint_pixels.foreach_get(values)
        return values.reshape(8, 16, 4)
    finally:
        node.paint_target = previous


def native_buffer(layer):
    native = paint._C_TexPaintLayer.from_address(layer.as_pointer())
    return paint._C_ImBuf.from_address(native.ibuf)


def native_hash(layer):
    # Read-only evidence of the actual source buffer, including hidden RGB.
    image = native_buffer(layer)
    pointer = image.float_data or image.byte_data
    length = image.x * image.y * (16 if image.float_data else 4)
    return hashlib.sha256(ctypes.string_at(pointer, length)).hexdigest()


def op(node, action):
    with bpy.context.temp_override(node=node):
        return bpy.ops.node.tex_paint_layer_pixel_action(action=action)


def fixture(kind):
    material = bpy.data.materials.new('Pixel Actions ' + kind)
    material.use_nodes = True
    node = material.node_tree.nodes.new('ShaderNodeTexPaint')
    material.node_tree.nodes.active = node
    with bpy.context.temp_override(node=node):
        bpy.ops.node.tex_paint_canvas_new(name=material.name, width=16, height=8,
                                       color=(0, 0, 0, 0), **{'float': kind == 'Float'})
    if kind == 'Data':
        node.image.colorspace_settings.name = 'Non-Color'
    else:
        node.image.colorspace_settings.name = 'sRGB' if kind == 'Byte' else 'Linear Rec.709'
    source = np.empty((8, 16, 4), dtype=np.float32)
    source[..., 0] = np.linspace(.1, .8, 16)
    source[..., 1] = np.linspace(.2, .7, 8)[:, None]
    source[..., 2] = .35
    source[..., 3] = np.resize([.2, .5, 1, .7], (8, 16))
    if kind == 'Float':
        source[2, 3, :3] = (-.4, 2.3, 4.0)
    node.paint_pixels.foreach_set(source.ravel())
    return material, node


def expected(kind, before, action, lock_alpha=False):
    result = before.copy()
    if action == 'FILL_BRUSH':
        result[..., :3] = Color(brush_color).from_scene_linear_to_srgb() if kind == 'Byte' else brush_color
        if not lock_alpha:
            result[..., 3] = 1
    elif action == 'CLEAR':
        result[:] = 0
    elif action == 'INVERT_RGB':
        result[..., :3] = 1 - result[..., :3]
    elif action == 'FLIP_H':
        result[:] = before[:, ::-1, :]
    else:
        result[:] = before[::-1, :, :]
    # Fully transparent pixels have no defined straight RGB through native RNA.
    result[result[..., 3] == 0, :3] = 0
    return result


def workflow():
    original_brush_colors = paint._get_current_brush_colors
    paint._get_current_brush_colors = lambda context: (brush_color, (0, 0, 0))
    try:
        for kind in ('Byte', 'Data', 'Float'):
            material, source_node = fixture(kind)
            source = pixels(source_node)
            source_hash = native_hash(source_node.layers[0])
            for mode in ('Layer Copy', 'Material Copy'):
                for action in actions:
                    source_node.active_layer_index = 0
                    if mode == 'Layer Copy':
                        with bpy.context.temp_override(node=source_node):
                            bpy.ops.node.tex_paint_layer_duplicate()
                        target = source_node
                    else:
                        copied = material.copy()
                        target = next(n for n in copied.node_tree.nodes if n.bl_idname == 'ShaderNodeTexPaint')
                        target.active_layer_index = 0
                    before = pixels(target)
                    label = f'{kind} {mode} {action}'
                    target_buffer = native_buffer(target.active_layer)
                    source_buffer = native_buffer(source_node.layers[0])
                    if (target_buffer.float_data or target_buffer.byte_data) == (source_buffer.float_data or source_buffer.byte_data):
                        report['shared_buffer_cases'].append(label)
                    check(label + ' begins with identical source pixels', np.array_equal(before, source))
                    target.active_layer.use_mask = True
                    target.paint_target = 'MASK'
                    mask_before = np.asarray(target.active_layer.mask_pixels[:], dtype=np.float32)
                    check(label + ' completes and restores mask target', op(target, action) == {'FINISHED'} and
                          target.paint_target == 'MASK' and
                          np.array_equal(target.active_layer.mask_pixels[:], mask_before))
                    check(label + ' has expected pixels', np.allclose(pixels(target), expected(kind, before, action),
                                                                     atol=1e-5 if kind == 'Float' else 1 / 255))
                    source_node.active_layer_index = 0
                    check(label + ' preserves original native buffer and source pixels',
                          native_hash(source_node.layers[0]) == source_hash and np.array_equal(pixels(source_node), source))

            source_node.active_layer_index = 0
            with bpy.context.temp_override(node=source_node):
                bpy.ops.node.tex_paint_layer_duplicate()
            source_node.active_layer.lock_alpha = True
            before = pixels(source_node)
            check(kind + ' alpha-locked fill completes', op(source_node, 'FILL_BRUSH') == {'FINISHED'})
            check(kind + ' alpha-locked fill preserves coverage', np.array_equal(pixels(source_node)[..., 3], before[..., 3]) and
                  np.allclose(pixels(source_node), expected(kind, before, 'FILL_BRUSH', True), atol=1 / 255))
            for flag in ('hide', 'lock'):
                layer = source_node.active_layer
                stable = native_hash(layer)
                setattr(layer, flag, True)
                check(kind + ' ' + flag + ' blocks all basic pixel actions', all(op(source_node, action) == {'CANCELLED'} for action in actions))
                check(kind + ' ' + flag + ' preserves storage', native_hash(layer) == stable)
                setattr(layer, flag, False)
            with bpy.context.temp_override(node=source_node):
                bpy.ops.node.tex_paint_layer_add(type='FILL')
            fill = source_node.active_layer
            fill.fill_color = (.2, .3, .4, .6)
            check(kind + ' Fill layer retains direct color fill', op(source_node, 'FILL_BRUSH') == {'FINISHED'} and
                  np.allclose(fill.fill_color, (*brush_color, .6)))
            check(kind + ' Fill layer invert preserves alpha', op(source_node, 'INVERT_RGB') == {'FINISHED'} and
                  np.allclose(fill.fill_color, (*(1 - c for c in brush_color), .6)))
    finally:
        paint._get_current_brush_colors = original_brush_colors


try:
    workflow()
    report['passed'] = True
except Exception:
    report['error'] = traceback.format_exc()
    raise
finally:
    (args.output / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
