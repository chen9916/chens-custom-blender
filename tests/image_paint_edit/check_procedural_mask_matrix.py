# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Independent generator/blend oracle on byte/float canvases with native Paint coverage."""

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
report = {'build_hash': bpy.app.build_hash.decode(), 'runtime': bpy.app.binary_path,
          'test_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
          'checks': [], 'matrix_cases': 0, 'passed': False}
SIZE = 24
COUNT = SIZE * SIZE
GENERATORS = ('AO', 'CURVATURE', 'CAVITY', 'EDGE_WEAR', 'NORMAL_UP', 'POSITION_Z')
BLENDS = ('REPLACE', 'ADD', 'SUBTRACT', 'MULTIPLY', 'SCREEN', 'OVERLAY', 'MAX', 'MIN')


def check(name, condition):
    passed = bool(condition)
    report['checks'].append({'name': name, 'passed': passed})
    print('PROCEDURAL_MASK_MATRIX_CHECK', name, passed, flush=True)
    assert passed, name


def op(node, operator, **kwargs):
    with bpy.context.temp_override(node=node):
        return operator(**kwargs)


def rgba(prop):
    result = np.empty(len(prop), dtype=np.float32)
    prop.foreach_get(result)
    return result.reshape(-1, 4).copy()


def byte(value):
    return np.floor(np.clip(value, 0, 1) * 255 + .5).astype(np.uint8)


def effect_pixels(layer, uid):
    return byte(rgba(next(effect for effect in layer.mask_effects if effect.uid == uid).pixels))


def mask(layer):
    return byte(rgba(layer.mask_pixels)[:, 0])


def source_settings(layer):
    fields = ('type', 'proj_mode', 'proj_image_name', 'proj_blend_falloff', 'show_projection_widget',
              'fill_roughness', 'fill_metallic', 'fill_height', 'fill_specular',
              'use_channel_color', 'use_channel_roughness', 'use_channel_metallic',
              'use_channel_height', 'use_channel_specular', 'use_channel_emission',
              'mask_gen_type', 'mask_gen_blend', 'mask_gen_balance', 'mask_gen_contrast',
              'mask_gen_radius', 'mask_gen_noise', 'mask_gen_noise_scale', 'mask_gen_invert')
    arrays = ('proj_location', 'proj_rotation', 'proj_scale', 'fill_color', 'fill_emission')
    return tuple(getattr(layer, key) for key in fields) + tuple(tuple(getattr(layer, key)) for key in arrays)


def source_buffers(layer):
    native = paint._C_TexPaintLayer.from_address(layer.as_pointer())
    result = []
    for address in (native.ibuf, native.pbr_ibuf):
        if not address:
            result.append(None)
            continue
        buffer = paint._C_ImBuf.from_address(address)
        count = buffer.x * buffer.y * 4
        kind, address = (ctypes.c_float, buffer.float_data) if buffer.float_data else (ctypes.c_uint8, buffer.byte_data)
        result.append(np.ctypeslib.as_array((kind * count).from_address(address)).copy())
    return result


def buffers_equal(layer, saved):
    return all(actual is None if expected is None else actual is not None and np.array_equal(actual, expected)
               for actual, expected in zip(source_buffers(layer), saved))


def blend(base, value, mode, opacity):
    b, s = base.astype(np.float64) / 255, np.asarray(value, dtype=np.float64) / 255
    if mode == 'ADD':
        target = np.minimum(b + s, 1)
    elif mode == 'SUBTRACT':
        target = np.maximum(b - s, 0)
    elif mode == 'MULTIPLY':
        target = b * s
    elif mode == 'SCREEN':
        target = b + s - b * s
    elif mode == 'OVERLAY':
        target = np.where(b <= .5, 2 * b * s, 1 - 2 * (1 - b) * (1 - s))
    elif mode == 'MAX':
        target = np.maximum(b, s)
    elif mode == 'MIN':
        target = np.minimum(b, s)
    else:
        target = s
    return byte(b + (target - b) * opacity)


def painted(base, pixels, mode, opacity, invert=False):
    b = base.astype(np.int64)
    value = pixels[:, 0].astype(np.int64)
    if invert:
        value = 255 - value
    alpha = (pixels[:, 3].astype(np.int64) * round(opacity * 256)) >> 8
    if mode == 'ADD':
        return np.minimum(b + (value * alpha + 127) // 255, 255).astype(np.uint8)
    if mode == 'SUBTRACT':
        return np.maximum(b - (value * alpha + 127) // 255, 0).astype(np.uint8)
    if mode in {'SCREEN', 'OVERLAY'}:
        return blend(base, value, mode, alpha / 255)
    if mode == 'MULTIPLY':
        value = (b * value + 127) // 255
    elif mode == 'MAX':
        value = np.maximum(b, value)
    elif mode == 'MIN':
        value = np.minimum(b, value)
    return ((b * (255 - alpha) + value * alpha + 127) // 255).astype(np.uint8)


def generated_image(name, values):
    image = bpy.data.images.new(name, width=SIZE, height=SIZE, float_buffer=True)
    image.colorspace_settings.name = 'Non-Color'
    image.pixels.foreach_set(values.ravel())
    image.pack()
    image.use_fake_user = True
    return image


def baked_maps():
    x = np.arange(COUNT, dtype=np.float32) / (COUNT - 1)
    data = {}
    names = {}
    for key, value in (('ao', .2 + .7 * x), ('curvature', .1 + .8 * x),
                       ('normal_ws', .15 + .7 * x[::-1]), ('position', .05 + .9 * x)):
        values = np.ones((COUNT, 4), dtype=np.float32)
        values[:, :3] = value[:, None]
        data[key] = value
        names[key] = generated_image('Matrix map ' + key, values).name
    concave = np.maximum(.5 - data['curvature'], 0) * 2
    convex = np.maximum(data['curvature'] - .5, 0) * 2
    expected = {'AO': data['ao'], 'CURVATURE': data['curvature'],
                'CAVITY': np.clip(concave * 1.6 + (1 - data['ao']) * .55, 0, 1),
                'EDGE_WEAR': np.clip(convex * 1.85 * (.35 + .65 * data['ao']), 0, 1),
                'NORMAL_UP': data['normal_ws'], 'POSITION_Z': data['position']}
    return names, {key: byte(value) for key, value in expected.items()}


def fixture(is_float, route, generator, map_names):
    label = ('Float' if is_float else 'Byte') + ' ' + route + ' ' + generator
    bpy.ops.mesh.primitive_cube_add()
    uv_layer = bpy.context.object.data.uv_layers.active
    uvs = np.empty(len(uv_layer.data) * 2, dtype=np.float32)
    uv_layer.data.foreach_get('uv', uvs)
    uv_layer.data.foreach_set('uv', uvs * .8 + .1)
    bpy.context.object.data.update()
    material = bpy.data.materials.new('Procedural ' + label)
    material.use_nodes = True
    bpy.context.object.data.materials.append(material)
    node = material.node_tree.nodes.new('ShaderNodeTexPaint')
    material.node_tree.nodes.active = node
    op(node, bpy.ops.node.tex_paint_canvas_new, name=label, width=SIZE, height=SIZE,
       color=(1, 1, 0, 1), **{'float': is_float})
    op(node, bpy.ops.node.tex_paint_layer_add, type='FILL')
    layer = node.active_layer
    layer.fill_color = (.8, .7, .6, .9)
    layer.fill_roughness, layer.fill_metallic, layer.fill_height, layer.fill_specular = .67, .23, .31, .58
    layer.fill_emission = (.2, .4, .7)
    layer.use_channel_emission = layer.use_channel_specular = True
    layer.proj_location, layer.proj_rotation, layer.proj_scale = (.13, .17, .21), (.1, .2, .3), (.7, .9, 1.1)
    values = np.ones((COUNT, 4), dtype=np.float32)
    values[:, :3] = np.linspace(.3, 1, COUNT, dtype=np.float32)[:, None]
    layer.proj_image_name = generated_image('Matrix color ' + label, values).name
    layer.proj_mode = 'UV_REPEAT'
    paint._save_mesh_maps_state(node, updates={'use_baked_maps': route == 'Baked'}, map_updates=map_names)
    lower = paint._make_default_mask_effect('PAINT', 1, is_first=True)
    lower.update(opacity=.6, blend_mode='REPLACE')
    generated = paint._make_default_mask_effect('GENERATOR', 2, is_first=False, gen_type=generator)
    generated.update(gen_radius=0.0, gen_noise=0.0, gen_global_ao=False, gen_balance=.5, gen_contrast=0.0)
    upper = paint._make_default_mask_effect('PAINT', 3, is_first=False)
    upper.update(opacity=.73, blend_mode='MULTIPLY')
    state = paint._get_layer_grunge_state(node, layer)
    state.update(effects=[lower, generated, upper], active=True, active_effect_uid=2, next_effect_uid=4, base_value=191)
    layer.use_mask = True
    node.paint_target = 'MASK'
    paint._save_layer_grunge_state(node, layer, state)
    for uid, offset in ((1, 17), (3, 83)):
        effect = layer.mask_effect_ensure(uid, 1)
        pixels = np.ones((COUNT, 4), dtype=np.float32)
        pixels[:, :3] = ((np.arange(COUNT) * 37 + offset) % 256)[:, None] / 255
        pixels[:, 3] = np.resize(np.array([0, 1, 63, 127, 128, 254, 255], dtype=np.float32) / 255, COUNT)
        effect.pixels.foreach_set(pixels.ravel())
    layer.mask_effects_active = True
    return label, material, node, generated, upper


def run_case(is_float, route, generator, map_names, expected_raw):
    label, material, node, generated, upper = fixture(is_float, route, generator, map_names)
    layer = node.active_layer
    settings, buffers = source_settings(layer), source_buffers(layer)
    saved_paint = {uid: effect_pixels(layer, uid) for uid in (1, 3)}

    def evaluate(force=False):
        paint._evaluate_layer_grunge_mask(node, layer, context=bpy.context, force_reproject=force, skip_absorb=True)
        return mask(layer)

    evaluate(True)
    raw = effect_pixels(layer, 2)[:, 0]
    if route == 'Baked':
        check(label + ' generated baked route matches independent map formula',
              np.max(np.abs(raw.astype(int) - expected_raw[generator].astype(int))) <= 1)
    baseline = painted(np.full(COUNT, 191, dtype=np.uint8), saved_paint[1], 'REPLACE', .6)
    for mode in BLENDS:
        for invert in (False, True):
            for opacity in (0.0, .37, 1.0):
                generated.update(blend_mode=mode, invert=invert, opacity=opacity)
                paint._save_layer_grunge_state(node, layer)
                actual = evaluate()
                source = 255 - raw if invert else raw
                wanted = painted(blend(baseline, source, mode, opacity), saved_paint[3], 'MULTIPLY', .73)
                error = int(np.max(np.abs(actual.astype(int) - wanted.astype(int))))
                report['matrix_cases'] += 1
                if error > 1:
                    check(label + ' ' + mode + ' invert=' + str(invert) + ' opacity=' + str(opacity) +
                          ' independent blend error=' + str(error), False)
    check(label + ' eight blends invert and opacity match independent formulas', True)
    # The same eight modes also receive sparse painted coverage, rather than
    # merely testing fully opaque procedural sources.
    generated.update(blend_mode='REPLACE', invert=False, opacity=.37)
    for mode in BLENDS:
        upper.update(blend_mode=mode, invert=True)
        paint._save_layer_grunge_state(node, layer)
        actual = evaluate()
        wanted = painted(blend(baseline, raw, 'REPLACE', .37), saved_paint[3], mode, .73, invert=True)
        check(label + ' painted ' + mode + ' preserves coverage-aware blend',
              np.max(np.abs(actual.astype(int) - wanted.astype(int))) <= 1)
    upper.update(blend_mode='MULTIPLY', invert=False)
    generated.update(gen_balance=.65, opacity=.71)
    paint._save_layer_grunge_state(node, layer)
    stable = evaluate(True)
    for iteration in range(2):
        check(label + ' repeated raw evaluation ' + str(iteration) + ' is idempotent with UV gutters',
              np.array_equal(evaluate(True), stable) and np.array_equal(effect_pixels(layer, 2)[:, 0], raw))
    for uid in (2, 1, 3):
        op(node, bpy.ops.node.tex_paint_mask_effect_toggle, effect_uid=uid)
        op(node, bpy.ops.node.tex_paint_mask_effect_toggle, effect_uid=uid)
    check(label + ' toggles retain both Paint sources and final output',
          all(np.array_equal(effect_pixels(layer, uid), values) for uid, values in saved_paint.items()) and
          np.array_equal(mask(layer), stable))
    check(label + ' generator evaluation preserves color and PBR source settings and pixels',
          source_settings(layer) == settings and buffers_equal(layer, buffers))
    return (material.name, node.name), {'mask': stable, 'paint': saved_paint, 'settings': settings,
                                       'buffers': buffers, 'state': json.loads(node['_grunge_layers_json'])}


def edge_cases(map_names):
    _, _, node, generated, _ = fixture(False, 'Local', 'AO', map_names)
    layer = node.active_layer
    paint._evaluate_layer_grunge_mask(node, layer, force_reproject=True, skip_absorb=True)
    settings, buffers = source_settings(layer), source_buffers(layer)
    painted_before = {uid: effect_pixels(layer, uid) for uid in (1, 3)}
    node.paint_target = 'COLOR'
    paint._evaluate_layer_grunge_mask(node, layer, force_reproject=True, skip_absorb=True)
    check('local generator forced evaluation preserves explicit COLOR target and source data',
          node.paint_target == 'COLOR' and source_settings(layer) == settings and buffers_equal(layer, buffers))
    native = paint._C_TexPaintLayer.from_address(layer.as_pointer())
    state_before = (native.uid, native.type, native.flag, native.ibuf, native.mask_ibuf, node.image)

    class FailedGeneratorCallback:
        def __getattr__(self, name):
            return getattr(layer, name)

        @property
        def mask_gen_noise_scale(self):
            return layer.mask_gen_noise_scale

        @mask_gen_noise_scale.setter
        def mask_gen_noise_scale(self, value):
            layer.mask_gen_noise_scale = value
            raise RuntimeError('Injected failure after native generator')

    previous_internal = paint._INTERNAL_REPROJECTING
    paint._INTERNAL_REPROJECTING = True
    failed = False
    try:
        paint._ensure_generator_raw_u8(node, FailedGeneratorCallback(), generated, SIZE, SIZE, force=True)
    except RuntimeError as error:
        failed = str(error) == 'Injected failure after native generator'
    restored_internal = paint._INTERNAL_REPROJECTING
    paint._INTERNAL_REPROJECTING = previous_internal
    native = paint._C_TexPaintLayer.from_address(layer.as_pointer())
    check('generator callback failure restores borrowed state target prior guard and both Paint sources',
          failed and restored_internal and node.paint_target == 'COLOR' and
          (native.uid, native.type, native.flag, native.ibuf, native.mask_ibuf, node.image) == state_before and
          source_settings(layer) == settings and buffers_equal(layer, buffers) and
          all(np.array_equal(effect_pixels(layer, uid), pixels) for uid, pixels in painted_before.items()))
    node.paint_target = 'MASK'
    for uv_layer in list(bpy.context.object.data.uv_layers):
        bpy.context.object.data.uv_layers.remove(uv_layer)
    bpy.context.object.data.update()
    # No force: removal must invalidate the previously cached valid receiver.
    paint._evaluate_layer_grunge_mask(node, layer, force_reproject=False, skip_absorb=True)
    check('removing receiver UVs cannot feed prior evaluated mask back into generator raw pixels',
          np.all(effect_pixels(layer, 2)[:, 0] == 0) and
          all(np.array_equal(effect_pixels(layer, uid), pixels) for uid, pixels in painted_before.items()))

    _, material, node, generated, _ = fixture(True, 'Local', 'CURVATURE', map_names)
    layer = node.active_layer
    paint._evaluate_layer_grunge_mask(node, layer, force_reproject=True, skip_absorb=True)
    valid_raw = effect_pixels(layer, 2)
    valid_object = bpy.context.object
    invalid_mesh = valid_object.data.copy()
    for uv_layer in list(invalid_mesh.uv_layers):
        invalid_mesh.uv_layers.remove(uv_layer)
    invalid_object = bpy.data.objects.new('Shared receiver without UVs', invalid_mesh)
    bpy.context.collection.objects.link(invalid_object)
    check('shared no-UV fixture retains its material assignment', invalid_object.active_material == material)
    paint._evaluate_layer_grunge_mask(node, layer, force_reproject=True, skip_absorb=True)
    check('a shared no-UV receiver cannot corrupt another receiver raw generator map',
          np.array_equal(effect_pixels(layer, 2), valid_raw))

    # Explicit refresh must reread an edited baked image even when its ID/name
    # is unchanged; ordinary LUT-only edits may reuse the last raw evaluation.
    _, _, baked_node, _, _ = fixture(False, 'Baked', 'AO', map_names)
    baked_layer = baked_node.active_layer
    paint._evaluate_layer_grunge_mask(baked_node, baked_layer, force_reproject=True, skip_absorb=True)
    ao_image = bpy.data.images[map_names['ao']]
    original_ao = rgba(ao_image.pixels)
    edited_ao = original_ao.copy()
    edited_ao[:, :3] = .91
    ao_image.pixels.foreach_set(edited_ao.ravel())
    try:
        paint._evaluate_layer_grunge_mask(baked_node, baked_layer, force_reproject=True, skip_absorb=True)
        check('explicit refresh reads same-ID baked map pixel edits',
              np.all(effect_pixels(baked_layer, 2)[:, 0] == 232))
    finally:
        ao_image.pixels.foreach_set(original_ao.ravel())

    for edit in ('geometry', 'uv'):
        _, _, changed_node, _, _ = fixture(False, 'Local', 'POSITION_Z', map_names)
        changed_layer = changed_node.active_layer

        def refreshed_raw():
            paint._evaluate_layer_grunge_mask(changed_node, changed_layer,
                                              force_reproject=True, skip_absorb=True)
            return effect_pixels(changed_layer, 2)

        before = refreshed_raw()
        mesh = bpy.context.object.data
        if edit == 'geometry':
            values = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
            mesh.vertices.foreach_get('co', values)
            values.reshape(-1, 3)[0, 2] -= 2
            mesh.vertices.foreach_set('co', values)
        else:
            values = np.empty(len(mesh.uv_layers.active.data) * 2, dtype=np.float32)
            mesh.uv_layers.active.data.foreach_get('uv', values)
            mesh.uv_layers.active.data.foreach_set('uv', values * .6 + .1)
        mesh.update()
        edited = refreshed_raw()
        bpy.context.object.data = mesh.copy()
        check('forced ' + edit + ' edit matches a fresh mesh rebuild and changes raw map',
              np.any(edited != before) and np.array_equal(edited, refreshed_raw()))

    state = paint._get_layer_grunge_state(node, layer)
    state['effects'] = [generated]
    state['base_value'] = 255
    for mode in BLENDS:
        generated.update(blend_mode=mode, opacity=0.0, enabled=True)
        paint._save_layer_grunge_state(node, layer, state)
        paint._evaluate_layer_grunge_mask(node, layer, skip_absorb=True)
        transparent = mask(layer)
        generated['enabled'] = False
        paint._save_layer_grunge_state(node, layer, state)
        paint._evaluate_layer_grunge_mask(node, layer, skip_absorb=True)
        check('first SOURCE ' + mode + ' zero opacity equals disabled over explicit white base',
              np.all(transparent == 255) and np.array_equal(mask(layer), transparent))


def workflow():
    names, expected_raw = baked_maps()
    expected = dict(run_case(is_float, route, generator, names, expected_raw)
                    for is_float in (False, True) for route in ('Local', 'Baked') for generator in GENERATORS)
    path = args.output / 'procedural-mask-matrix.blend'
    check('procedural matrix saves', bpy.ops.wm.save_as_mainfile(filepath=str(path)) == {'FINISHED'})
    check('procedural matrix reopens', bpy.ops.wm.open_mainfile(filepath=str(path)) == {'FINISHED'})
    paint._clear_all_runtime_mask_caches(clear_raw_cache=True)
    for (material_name, node_name), saved in expected.items():
        node = bpy.data.materials[material_name].node_tree.nodes[node_name]
        layer = node.active_layer
        check(material_name + ' persistent native Paint coverage and metadata survive reopen',
              all(np.array_equal(effect_pixels(layer, uid), pixels) for uid, pixels in saved['paint'].items()) and
              json.loads(node['_grunge_layers_json']) == saved['state'])
        paint._evaluate_layer_grunge_mask(node, layer, context=bpy.context, force_reproject=True, skip_absorb=True)
        check(material_name + ' forced reopened evaluation preserves mask and source data',
              np.array_equal(mask(layer), saved['mask']) and source_settings(layer) == saved['settings'] and
              buffers_equal(layer, saved['buffers']))
    edge_cases(names)
    report['passed'] = True


try:
    workflow()
except Exception:
    report['error'] = traceback.format_exc()
    print(report['error'], flush=True)
(args.output / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
if not report['passed']:
    raise RuntimeError('Procedural mask matrix regression failed')
