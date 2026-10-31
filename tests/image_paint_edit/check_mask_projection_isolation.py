# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""A grunge mask never changes the owning layer's color projection or pixels.

Run with --background --factory-startup --python this_file -- --output DIR.
Optional --source-ui FILE loads current helper code into the installed UI module
inside this disposable process, without registering duplicate operators.
"""

import argparse
import ctypes
import hashlib
import json
import sys
import traceback
from pathlib import Path

import bpy
import numpy as np
from bl_ui import properties_tex_paint_layers as paint

parser = argparse.ArgumentParser()
parser.add_argument('--output', type=Path, required=True)
parser.add_argument('--source-ui', type=Path)
args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
assert '--factory-startup' in sys.argv and bpy.app.background and not bpy.data.filepath
args.output.mkdir(parents=True, exist_ok=True)
if args.source_ui:
    exec(compile(args.source_ui.read_bytes(), str(args.source_ui), 'exec'), paint.__dict__)
source_path = args.source_ui or Path(paint.__file__)
report = {'build_hash': bpy.app.build_hash.decode(), 'runtime': bpy.app.binary_path,
          'module': str(source_path), 'module_sha256': hashlib.sha256(source_path.read_bytes()).hexdigest(),
          'checks': [], 'passed': False}
SIZE = 64


def check(name, condition):
    passed = bool(condition)
    report['checks'].append({'name': name, 'passed': passed})
    print('MASK_PROJECTION_ISOLATION_CHECK', name, passed, flush=True)
    assert passed, name


def op(node, operator, **kwargs):
    with bpy.context.temp_override(node=node):
        return operator(**kwargs)


def rgba(prop):
    values = np.empty(len(prop), dtype=np.float32)
    prop.foreach_get(values)
    return values.reshape(-1, 4).copy()


def color_settings(layer):
    return (layer.type, layer.proj_mode, layer.proj_image_name,
            tuple(layer.proj_location), tuple(layer.proj_rotation), tuple(layer.proj_scale),
            layer.proj_blend_falloff, layer.show_projection_widget, tuple(layer.fill_color))


def raw_color(layer):
    # Fill intentionally has no writable color-target RNA. Read its existing
    # buffer without allocating one or changing type/target to inspect it.
    native = paint._C_TexPaintLayer.from_address(layer.as_pointer())
    if not native.ibuf:
        return None
    buffer = paint._C_ImBuf.from_address(native.ibuf)
    count = int(buffer.x) * int(buffer.y) * 4
    if buffer.float_data:
        return np.ctypeslib.as_array((ctypes.c_float * count).from_address(buffer.float_data)).copy()
    return np.ctypeslib.as_array((ctypes.c_uint8 * count).from_address(buffer.byte_data)).copy()


def same_raw(layer, expected):
    actual = raw_color(layer)
    return actual is None if expected is None else actual is not None and np.array_equal(actual, expected)


def failed_projection_restores_state(node, label):
    layer = node.active_layer

    def borrowed_state():
        native = paint._C_TexPaintLayer.from_address(layer.as_pointer())
        return (native.uid, native.type, native.flag, native.ibuf, native.mask_ibuf,
                native.pf, native.mask_pf, color_settings(layer))

    state_before = borrowed_state()
    source_before = raw_color(layer)
    image_before = node.image
    class FailedProjectionCallback:
        def __getattr__(self, name):
            return getattr(layer, name)

        @property
        def proj_blend_falloff(self):
            return layer.proj_blend_falloff

        @proj_blend_falloff.setter
        def proj_blend_falloff(self, value):
            # Run the real RNA/native projection callback, then fail while
            # temporary type, buffer, source and UID are still assigned.
            layer.proj_blend_falloff = value
            raise RuntimeError('Injected failure after mask projection')

    failed = False
    try:
        paint._ensure_grunge_raw_u8(node, FailedProjectionCallback(), paint._get_active_mask_effect(node),
                                    SIZE, SIZE, context=bpy.context, force=True)
    except RuntimeError as error:
        failed = str(error) == 'Injected failure after mask projection'
    check(label + ' failed extraction restores UID type flags buffer ownership and color settings',
          failed and borrowed_state() == state_before and same_raw(layer, source_before) and
          node.image == image_before and not paint._INTERNAL_REPROJECTING)
    # Reevaluate the authoritative stack after the deliberately interrupted
    # scratch-mask projection so later checks read a completed mask.
    paint._evaluate_layer_grunge_mask(node, layer, context=bpy.context, force_reproject=True)


def image(name, grunge=False, is_float=False, size=SIZE):
    result = bpy.data.images.new(name, width=size, height=size, float_buffer=is_float)
    result.colorspace_settings.name = 'Non-Color'
    values = np.ones((size, size, 4), dtype=np.float32)
    if grunge:
        values[:, :size // 2, :3] = 0
    else:
        values[:, :, 1] = np.linspace(.65, 1, size, dtype=np.float32)[None, :]
        values[:, :, 2] = .8
    result.pixels.foreach_set(values.ravel())
    result.pack()
    result.use_fake_user = True
    return result


def create_mask(node, grunge_image):
    # Inject only fixture defaults so the real Add Mask operator evaluates our
    # generated image immediately and never opens an external grunge library.
    original_factory = paint._make_default_mask_effect

    def custom_defaults(*pos, **kwargs):
        effect = original_factory(*pos, **kwargs)
        if effect['type'] == 'GRUNGE':
            effect.update(category='CUSTOM', custom_image=grunge_image.name, channel='RED',
                          proj_mode='REPEAT', scale=1.0, scale_x=1.0, scale_y=1.0,
                          blend_mode='REPLACE', balance=.5, contrast=0.0)
        return effect

    paint._make_default_mask_effect = custom_defaults
    try:
        return op(node, bpy.ops.node.tex_paint_mask_effect_add, effect_type='GRUNGE')
    finally:
        paint._make_default_mask_effect = original_factory


def assert_isolated(node, settings, source, unmasked, label):
    layer = node.active_layer
    check(label + ' preserves every color projection setting', color_settings(layer) == settings)
    check(label + ' preserves existing color pixels and solid Fill absence', same_raw(layer, source))
    mask = rgba(layer.mask_pixels)[:, 0]
    master = rgba(node.image.pixels)
    zeros = mask <= 1 / 255
    expected = unmasked.copy()
    expected[:, :3] = unmasked[:, :3] * mask[:, None] + np.array((1, 1, 0)) * (1 - mask[:, None])
    check(label + ' mask has black regions revealing lower yellow Fill',
          np.count_nonzero(zeros) > SIZE and np.allclose(master[zeros, :3], (1, 1, 0), atol=1/255, rtol=0))
    check(label + ' composite applies mask independently of source color',
          np.allclose(master, expected, atol=1/255 + 1e-6, rtol=0))


def fixture(kind, is_float, grunge_image):
    label = ('Float ' if is_float else 'Byte ') + kind
    bpy.ops.mesh.primitive_plane_add(size=2)
    material = bpy.data.materials.new('Mask Isolation ' + label)
    material.use_nodes = True
    bpy.context.object.data.materials.append(material)
    node = material.node_tree.nodes.new('ShaderNodeTexPaint')
    material.node_tree.nodes.active = node
    op(node, bpy.ops.node.tex_paint_canvas_new, name=label, width=SIZE, height=SIZE,
       color=(0, 0, 0, 0), **{'float': is_float})
    op(node, bpy.ops.node.tex_paint_layer_add, type='FILL')
    node.active_layer.fill_color = (1, 1, 0, 1)
    op(node, bpy.ops.node.tex_paint_layer_add, type='PAINT' if kind == 'Paint' else 'FILL')
    layer = node.active_layer
    layer.fill_color = (1, 1, 1, 1)
    color_image = image('Color source ' + label, is_float=is_float)
    layer.proj_location = (.13, -.17, .09)
    layer.proj_rotation = (.11, .21, -.19)
    layer.proj_scale = (.7, .9, 1.1)
    layer.proj_blend_falloff = .37
    layer.show_projection_widget = True
    if kind == 'Textured Fill':
        layer.proj_image_name = color_image.name
        layer.proj_mode = 'UV_REPEAT'
    elif kind == 'Paint':
        node.paint_target = 'COLOR'
        node.paint_pixels.foreach_set(rgba(color_image.pixels).ravel())
        # Retain a dormant source name as part of the authored color state.
        layer.proj_image_name = color_image.name
    settings, source = color_settings(layer), raw_color(layer)
    unmasked = rgba(node.image.pixels)
    check(label + ' Add Grunge succeeds with generated custom fixture', create_mask(node, grunge_image) == {'FINISHED'})
    assert_isolated(node, settings, source, unmasked, label + ' Add Grunge')
    for mode in ('REPEAT', 'TRIPLANAR'):
        paint._update_active_mask_effect(node, layer, {'proj_mode': mode, 'offset_x': .07}, ensure_type='GRUNGE')
        paint._evaluate_layer_grunge_mask(node, layer, context=bpy.context, force_reproject=True)
        assert_isolated(node, settings, source, unmasked, label + ' ' + mode)
    mask_location, mask_rotation, mask_scale = paint._get_projection_transform(node, layer, bpy.context.object)
    moved_location = tuple(value + (.19 if index == 0 else 0) for index, value in enumerate(mask_location))
    paint._set_projection_transform(node, layer, location=moved_location, rotation=mask_rotation,
                                    scale=mask_scale, ob=bpy.context.object)
    paint._notify_gizmo_projection_changed(node, layer, bpy.context)
    mask_transform = paint._get_projection_transform(node, layer, bpy.context.object)
    check(label + ' mask gizmo stores an independent moved transform',
          np.allclose(mask_transform[0], moved_location, atol=1e-6))
    assert_isolated(node, settings, source, unmasked, label + ' mask gizmo')
    failed_projection_restores_state(node, label)
    for target in ('COLOR', 'MASK', 'COLOR', 'MASK'):
        node.paint_target = target
        transform = paint._get_projection_transform(node, layer, bpy.context.object)
        expected = (settings[3], settings[4], settings[5]) if target == 'COLOR' else mask_transform
        check(label + ' ' + target + ' switch retains independent transform',
              all(np.allclose(actual, saved, atol=1e-6) for actual, saved in zip(transform, expected)))
    assert_isolated(node, settings, source, unmasked, label + ' target round trip')
    return (material.name, node.name), {'settings': settings, 'source': source, 'unmasked': unmasked,
                                       'mask': rgba(layer.mask_pixels), 'transform': mask_transform}


def gutter_fixture(is_float, grunge_image):
    label = ('Float' if is_float else 'Byte') + ' UV gutters'
    bpy.ops.mesh.primitive_plane_add(size=2)
    uv_layer = bpy.context.object.data.uv_layers.active
    uvs = np.empty(len(uv_layer.data) * 2, dtype=np.float32)
    uv_layer.data.foreach_get('uv', uvs)
    uv_layer.data.foreach_set('uv', uvs * .8 + .1)
    bpy.context.object.data.update()
    material = bpy.data.materials.new('Mask Isolation ' + label)
    material.use_nodes = True
    bpy.context.object.data.materials.append(material)
    node = material.node_tree.nodes.new('ShaderNodeTexPaint')
    material.node_tree.nodes.active = node
    op(node, bpy.ops.node.tex_paint_canvas_new, name=label, width=SIZE, height=SIZE,
       color=(0, 0, 0, 0), **{'float': is_float})
    op(node, bpy.ops.node.tex_paint_layer_add, type='FILL')
    node.active_layer.fill_color = (1, 1, 0, 1)
    op(node, bpy.ops.node.tex_paint_layer_add, type='FILL')
    layer = node.active_layer
    layer.fill_color = (1, 1, 1, 1)
    create_mask(node, grunge_image)
    paint._update_active_mask_effect(node, layer, {'proj_mode': 'TRIPLANAR', 'balance': .65},
                                     ensure_type='GRUNGE')
    settings = color_settings(layer)

    def evaluate():
        paint._notify_gizmo_projection_changed(node, layer, bpy.context, interactive=False)
        return rgba(layer.mask_pixels), rgba(node.image.pixels)

    baseline_mask, baseline_master = evaluate()
    y, x = np.mgrid[:SIZE, :SIZE]
    gutters = ((x < 4) | (x >= SIZE - 4) | (y < 4) | (y >= SIZE - 4)).ravel()
    source = next(effect for effect in layer.mask_effects if effect.type == 0)
    check(label + ' uncovered raw black becomes nonzero after nonidentity balance',
          np.all(rgba(source.pixels)[gutters, 0] == 0) and np.all(baseline_mask[gutters, 0] > 0))
    for iteration in range(3):
        mask, master = evaluate()
        check(label + ' identical forced evaluation ' + str(iteration) + ' is byte-exact',
              np.array_equal(mask, baseline_mask) and np.array_equal(master, baseline_master) and
              color_settings(layer) == settings and raw_color(layer) is None)

    preview_image = image('Low resolution preview ' + label, grunge=True, size=16)
    original_resolver = paint._resolve_grunge_image
    preview_calls = []

    def preview_resolver(effect, interactive=False):
        if interactive and effect.get('custom_image') == grunge_image.name:
            preview_calls.append(True)
            return preview_image
        return original_resolver(effect, interactive=interactive)

    # Exercise the actual interactive/full-resolution control flow without a
    # dependency on files in the external grunge catalog.
    paint._resolve_grunge_image = preview_resolver
    try:
        paint._notify_gizmo_projection_changed(node, layer, bpy.context, interactive=True)
        refined_mask, refined_master = evaluate()
        key = (node.as_pointer(), layer.uid)
        check(label + ' low resolution preview then full refinement restores exact output',
              bool(preview_calls) and np.array_equal(refined_mask, baseline_mask) and
              np.array_equal(refined_master, baseline_master) and key not in paint._PENDING_HIRES_REFINE)

        snapshot = paint._capture_projection_edit(node, layer, bpy.context.object)
        location, rotation, scale = snapshot['transform']
        paint._set_projection_transform(node, layer, location=(location[0] + .27, *location[1:]),
                                        rotation=rotation, scale=scale, ob=bpy.context.object)
        paint._notify_gizmo_projection_changed(node, layer, bpy.context, interactive=True)
        changed_mask = rgba(layer.mask_pixels)
        restored = paint._restore_projection_edit(node, layer, snapshot)
        canceled_mask, canceled_master = evaluate()
        check(label + ' canceled changed transform restores exact metadata and evaluated pixels',
              restored and not np.array_equal(changed_mask, baseline_mask) and
              paint._get_active_mask_effect(node, layer) == snapshot['effect'] and
              np.array_equal(canceled_mask, baseline_mask) and np.array_equal(canceled_master, baseline_master) and
              key not in paint._PENDING_HIRES_REFINE)
        mask, master = evaluate()
        check(label + ' forced evaluation after cancel stays byte-exact',
              np.array_equal(mask, baseline_mask) and np.array_equal(master, baseline_master) and
              color_settings(layer) == settings and raw_color(layer) is None)
    finally:
        paint._resolve_grunge_image = original_resolver
    return (material.name, node.name), {'mask': baseline_mask, 'master': baseline_master, 'settings': settings}


def workflow():
    grunge_image = image('Deterministic split grunge', grunge=True)
    expected = dict(fixture(kind, is_float, grunge_image)
                    for is_float in (False, True) for kind in ('Solid Fill', 'Textured Fill', 'Paint'))
    gutter_expected = dict(gutter_fixture(is_float, grunge_image) for is_float in (False, True))
    path = args.output / 'mask-projection-isolation.blend'
    check('isolated projection fixtures save', bpy.ops.wm.save_as_mainfile(filepath=str(path)) == {'FINISHED'})
    check('isolated projection fixtures reopen', bpy.ops.wm.open_mainfile(filepath=str(path)) == {'FINISHED'})
    paint._clear_all_runtime_mask_caches(clear_raw_cache=True)
    for (material_name, node_name), saved in expected.items():
        node = bpy.data.materials[material_name].node_tree.nodes[node_name]
        layer = node.active_layer
        check(material_name + ' native mask survives reopening', np.array_equal(rgba(layer.mask_pixels), saved['mask']))
        paint._evaluate_layer_grunge_mask(node, layer, context=bpy.context, force_reproject=True)
        assert_isolated(node, saved['settings'], saved['source'], saved['unmasked'], material_name + ' reopen')
        transform = paint._get_projection_transform(node, layer)
        check(material_name + ' independent mask transform survives reopening',
              all(np.allclose(actual, value, atol=1e-6) for actual, value in zip(transform, saved['transform'])))
    for (material_name, node_name), saved in gutter_expected.items():
        node = bpy.data.materials[material_name].node_tree.nodes[node_name]
        layer = node.active_layer
        paint._notify_gizmo_projection_changed(node, layer, bpy.context, interactive=False)
        check(material_name + ' gutter output remains exact after reopening and forced evaluation',
              np.array_equal(rgba(layer.mask_pixels), saved['mask']) and
              np.array_equal(rgba(node.image.pixels), saved['master']) and
              color_settings(layer) == saved['settings'] and raw_color(layer) is None)
    report['passed'] = True


try:
    workflow()
except Exception:
    report['error'] = traceback.format_exc()
    print(report['error'], flush=True)
(args.output / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
if not report['passed']:
    raise RuntimeError('Mask projection isolation regression failed')
