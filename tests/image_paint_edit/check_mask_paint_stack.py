# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Fill-layer painted mask persistence, including sparse strokes and float canvases."""

import argparse
import json
import sys
import traceback
from pathlib import Path

import bpy
import numpy as np
from bl_ui import properties_tex_paint_layers as paint


def fixture(is_float=False, size=256):
    material = bpy.data.materials.new('Paint Mask Test')
    material.use_nodes = True
    bpy.context.object.data.materials.clear()
    bpy.context.object.data.materials.append(material)
    node = material.node_tree.nodes.new('ShaderNodeTexPaint')
    material.node_tree.nodes.active = node
    with bpy.context.temp_override(node=node):
        bpy.ops.node.tex_paint_canvas_new(name='Mask Canvas', width=size, height=size,
                                       color=(.2, .2, .2, 1), **{'float': is_float})
        bpy.ops.node.tex_paint_layer_add(type='FILL')
        node.active_layer.name = 'Masked Fill'
        node.active_layer.fill_color = (.9, .1, .05, 1)
        image = bpy.data.images.new('AO mask fixture', width=size, height=size, float_buffer=True)
        image.colorspace_settings.name = 'Non-Color'
        values = np.ones((size, size, 4), dtype=np.float32)
        values[..., :3] = .5
        image.pixels.foreach_set(values.ravel())
        image.pack()
        image.use_fake_user = True
        paint._save_mesh_maps_state(node, map_updates={'ao': image.name})
        bpy.ops.node.tex_paint_mask_effect_add(effect_type='GENERATOR', gen_type='AO')
        generator_uid = paint._get_active_mask_effect(node)['uid']
        bpy.ops.node.tex_paint_mask_effect_add(effect_type='PAINT')
        paint_uid = paint._get_active_mask_effect(node)['uid']
    return node, generator_uid, paint_uid


def pixels(node):
    prop = node.active_layer.mask_pixels
    out = np.empty(len(prop), dtype=np.float32)
    prop.foreach_get(out)
    return out.reshape(node.image.size[1], node.image.size[0], 4)


def effect_pixels(node, uid):
    effect = next(e for e in node.active_layer.mask_effects if e.uid == uid)
    out = np.empty(len(effect.pixels), dtype=np.float32)
    effect.pixels.foreach_get(out)
    return out.reshape(node.image.size[1], node.image.size[0], 4)


def native_authoring_omits_pixels(node, uid):
    paint._absorb_pending_brush_strokes(node, node.active_layer)
    state = json.loads(node.get('_grunge_layers_json', '{}'))[str(node.active_layer.uid)]
    metadata = next(e for e in state['effects'] if e['uid'] == uid)
    return not {'paint_z64', 'paint_w', 'paint_h'}.intersection(metadata)


def toggle(node, uid):
    with bpy.context.temp_override(node=node):
        bpy.ops.node.tex_paint_mask_effect_toggle(effect_uid=uid)


def sparse_stroke(node, uid, value=.1):
    effect = next(e for e in node.active_layer.mask_effects if e.uid == uid)
    out = effect_pixels(node, uid)
    # Write directly to independent source/coverage; no final-mask inference.
    # No changed pixel is on the old 1024-point sampling grid.
    out[10:13, 13:16, :3] = value
    out[10:13, 13:16, 3] = 1.0
    effect.pixels.foreach_set(out.ravel())
    node.active_layer.mask_stack_evaluate()
    return pixels(node)


def check_native_save_pre(node, uid, label, check):
    layer = node.active_layer
    metadata = next(e for e in paint._get_layer_grunge_state(node)['effects'] if e['uid'] == uid)
    pbuf = paint._ensure_paint_effect_buffer(node.as_pointer(), layer.uid, metadata, 256, 256)
    pbuf['val_u8'][10 * 256 + 13] = 51
    pbuf['alpha_u8'][10 * 256 + 13] = 128
    paint._sync_paint_effect_z64(metadata, pbuf)
    compressions = []
    original_sync = paint._sync_paint_effect_z64

    def tracked_sync(*args, **kwargs):
        compressions.append(args[0]['uid'])
        return original_sync(*args, **kwargs)

    paint._sync_paint_effect_z64 = tracked_sync
    try:
        paint._on_blend_save_pre(None)
    finally:
        paint._sync_paint_effect_z64 = original_sync
    saved = effect_pixels(node, uid)[10, 13]
    check(label + ' save publishes pending Python Paint edits',
          np.allclose(saved, (51/255, 51/255, 51/255, 128/255), atol=1/255) and not pbuf['native_dirty'])
    check(label + ' native save does not compress legacy Paint snapshots', not compressions)

    pbuf['val_u8'][:] = 0
    paint._sync_paint_effect_z64(metadata, pbuf)
    native = next(e for e in layer.mask_effects if e.uid == uid)
    newer = effect_pixels(node, uid)
    newer[10, 13] = (.9, .9, .9, 1)
    native.pixels.foreach_set(newer.ravel())
    layer.mask_stack_evaluate()
    newer = effect_pixels(node, uid)
    paint._on_blend_save_pre(None)
    check(label + ' save keeps a newer native stroke over a stale pending cache',
          np.array_equal(effect_pixels(node, uid), newer) and
          paint._PAINT_EFFECT_BUFFERS[(node.as_pointer(), layer.uid, uid)]['native_revision'] == native.revision)

    pbuf = paint._PAINT_EFFECT_BUFFERS[(node.as_pointer(), layer.uid, uid)]
    pbuf['val_u8'][:] = 0
    pbuf['native_pointer'] += 8
    paint._sync_paint_effect_z64(metadata, pbuf)
    paint._on_blend_save_pre(None)
    check(label + ' save discards a pending cache from replaced native storage',
          np.array_equal(effect_pixels(node, uid), newer) and
          paint._PAINT_EFFECT_BUFFERS[(node.as_pointer(), layer.uid, uid)]['native_pointer'] == native.as_pointer())

    state = paint._get_layer_grunge_state(node)
    legacy = paint._make_default_mask_effect(effect_type='PAINT', uid=999, is_first=False)
    state['effects'].append(legacy)
    legacy_buffer = {'w': 256, 'h': 256, 'val_u8': np.full(256 * 256, 64, dtype=np.uint8),
                     'alpha_u8': np.full(256 * 256, 128, dtype=np.uint8), 'rev': 1}
    paint._PAINT_EFFECT_BUFFERS[(node.as_pointer(), layer.uid, 999)] = legacy_buffer
    paint._sync_paint_effect_z64(legacy, legacy_buffer)
    paint._on_blend_save_pre(None)
    saved_state = json.loads(node['_grunge_layers_json'])[str(layer.uid)]
    saved_legacy = next(e for e in saved_state['effects'] if e['uid'] == 999)
    check(label + ' save retains an unpublished legacy Paint import payload',
          bool(saved_legacy.get('paint_z64')) and saved_legacy['paint_w'] == saved_legacy['paint_h'] == 256)
    state['effects'].remove(legacy)
    paint._PAINT_EFFECT_BUFFERS.pop((node.as_pointer(), layer.uid, 999))
    paint._save_layer_grunge_state(node, layer, state)


def run(args):
    assert '--factory-startup' in sys.argv and bpy.app.background and not bpy.data.filepath
    args.output.mkdir(parents=True, exist_ok=True)
    report = {'build_hash': bpy.app.build_hash.decode(), 'build_time': bpy.app.build_time.decode(),
              'checks': [], 'passed': False}

    def check(name, condition):
        result = bool(condition)
        report['checks'].append({'name': name, 'passed': result})
        print('MASK_STACK_CHECK', name, result, flush=True)
        assert result, name

    for is_float in (False, True):
        label = 'float' if is_float else 'byte'
        try:
            node, generator, painted = fixture(is_float)
            base = pixels(node)
            stroke = sparse_stroke(node, painted)
            check_native_save_pre(node, painted, label, check)
            stroke = pixels(node)
            native_stroke = effect_pixels(node, painted)
            toggle(node, painted)
            check(label + ' hiding Paint restores unmodified generator', np.allclose(pixels(node), base, atol=1/255))
            toggle(node, painted)
            check(label + ' showing Paint restores sparse stroke', np.allclose(pixels(node), stroke, atol=1/255))
            check(label + ' stroke belongs to native Paint coverage',
                  np.array_equal(effect_pixels(node, painted), native_stroke))
            check(label + ' native authoring JSON omits duplicate Paint pixels',
                  native_authoring_omits_pixels(node, painted))
            toggle(node, generator)
            toggle(node, generator)
            check(label + ' generator toggles preserve native Paint data',
                  np.array_equal(effect_pixels(node, painted), native_stroke) and
                  np.allclose(pixels(node), stroke, atol=1/255) and
                  native_authoring_omits_pixels(node, painted))
            material_name = next(m.name for m in bpy.data.materials if m.node_tree == node.id_data)
            path = args.output / (label + '-native-painted-mask.blend')
            check(label + ' native mask fixture saves',
                  bpy.ops.wm.save_as_mainfile(filepath=str(path)) == {'FINISHED'})
            check(label + ' native mask fixture reopens',
                  bpy.ops.wm.open_mainfile(filepath=str(path)) == {'FINISHED'})
            node = next(n for n in bpy.data.materials[material_name].node_tree.nodes
                        if n.bl_idname == 'ShaderNodeTexPaint')
            check(label + ' native saved coverage survives without duplicate JSON pixels',
                  np.array_equal(effect_pixels(node, painted), native_stroke) and
                  np.allclose(pixels(node), stroke, atol=1/255) and
                  native_authoring_omits_pixels(node, painted))
            paint._clear_all_runtime_mask_caches(clear_raw_cache=True)
            check(label + ' restored native stack evaluates without Python pixel caches',
                  node.active_layer.mask_stack_evaluate() and
                  np.array_equal(effect_pixels(node, painted), native_stroke) and
                  np.allclose(pixels(node), stroke, atol=1/255))
            next_stroke = sparse_stroke(node, painted, .9)
            next_native = effect_pixels(node, painted)
            toggle(node, painted)
            toggle(node, painted)
            check(label + ' first stroke after load survives toggles',
                  np.allclose(pixels(node), next_stroke, atol=2/255) and
                  np.array_equal(effect_pixels(node, painted), next_native))
            # A fused generator/levels top must retain the baseline immediately.
            with bpy.context.temp_override(node=node):
                bpy.ops.node.tex_paint_mask_effect_add(effect_type='LEVELS')
            levels_output = pixels(node)
            check(label + ' native stack evaluates with Paint coverage below Levels',
                  np.array_equal(effect_pixels(node, painted), next_native) and
                  levels_output.shape == (256, 256, 4) and np.all(np.isfinite(levels_output)))
            # PBR channel nodes share a mask stack, so mirroring one stroke must
            # not be mistaken for a second stroke during save/channel switching.
            node, generator, painted = fixture(is_float)
            other = node.id_data.nodes.new('ShaderNodeTexPaint')
            with bpy.context.temp_override(node=other):
                bpy.ops.node.tex_paint_canvas_new(name='Roughness', width=256, height=256,
                                                color=(.5, .5, .5, 1), **{'float': is_float})
                bpy.ops.node.tex_paint_layer_add(type='FILL')
            paint._evaluate_layer_grunge_mask(node, node.active_layer)
            stroke = sparse_stroke(node, painted)
            stroke_native = effect_pixels(node, painted)
            paint._sync_layer_mask_to_companion_channels(node, node.active_layer)
            other_layer = next(layer for layer in other.layers if layer.uid == node.active_layer.uid)
            check(label + ' companion receives native stack coverage without a second stroke',
                  np.array_equal(effect_pixels(other, painted), stroke_native) and
                  np.allclose(pixels(other), stroke, atol=1/255))
            toggle(node, painted)
            toggle(node, painted)
            check(label + ' companion mask stack remains identical after toggling',
                  np.allclose(pixels(node), stroke, atol=2/255) and
                  np.allclose(pixels(other), pixels(node), atol=1/255) and
                  other_layer.mask_effects_active)
            # Native Paint coverage remains transparent when edited through the
            # shared image transform operators, unlike an opaque scalar mask.
            node, generator, painted = fixture(is_float, size=64)
            base = pixels(node).copy()
            sparse_stroke(node, painted, .1)
            original = effect_pixels(node, painted)
            selection = np.zeros((64, 64), dtype=np.float32)
            selection[10:13, 13:16] = 1
            node.selection_mask.foreach_set(selection.ravel())
            area = bpy.context.screen.areas[0]
            old_type = area.type
            try:
                area.type = 'IMAGE_EDITOR'
                area.spaces.active.ui_mode = 'PAINT'
                area.spaces.active.image = node.image
                region = next(r for r in area.regions if r.type == 'WINDOW')
                with bpy.context.temp_override(area=area, region=region):
                    result = bpy.ops.image.paint_transform(translation=(5, 0), angle=0, scale=(1, 1))
                moved = effect_pixels(node, painted)
                check(label + ' paint-effect transform preserves independent coverage',
                      result == {'FINISHED'} and np.all(moved[10:13, 13:16, 3] == 0) and
                      np.array_equal(moved[10:13, 18:21], original[10:13, 13:16]) and
                      np.sum(moved[..., 3]) == np.sum(original[..., 3]))
                check(label + ' moved Paint reveals its unchanged generated mask',
                      np.array_equal(pixels(node)[10:13, 13:16], base[10:13, 13:16]))
            finally:
                area.type = old_type
        except Exception:
            report.setdefault('errors', []).append(traceback.format_exc())
            print(report['errors'][-1], flush=True)
    report['passed'] = not report.get('errors') and all(c['passed'] for c in report['checks'])
    (args.output / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    if not report['passed']:
        raise RuntimeError('Paint mask regression failed')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    run(parser.parse_args(sys.argv[sys.argv.index('--') + 1:]))
