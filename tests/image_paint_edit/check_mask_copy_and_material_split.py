# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Copy-mask ownership, material image isolation, and native mask metadata persistence."""

import argparse
import base64
import hashlib
import json
import sys
import traceback
from pathlib import Path
import zlib

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
    print('MASK_COPY_SPLIT_CHECK', name, result, flush=True)
    assert result, name


def op(node, operator, **kwargs):
    with bpy.context.temp_override(node=node):
        return operator(**kwargs)


def rgba(prop):
    values = np.empty(len(prop), dtype=np.float32)
    prop.foreach_get(values)
    return values.reshape(-1, 4)


def effect(layer, uid):
    return next(e for e in layer.mask_effects if e.uid == uid)


def native_snapshot(layer):
    return {e.uid: rgba(e.pixels).copy() for e in layer.mask_effects if e.type in {0, 1}}


def semantic_state(state):
    return json.loads(json.dumps(state), object_hook=lambda item: {
        key: value for key, value in item.items() if key not in {'paint_z64', 'paint_w', 'paint_h'}})


def image_set(node):
    return (node.image, node.pbr_image, node.emission_image)


def image_hashes(node):
    return tuple(hashlib.sha256(rgba(image.pixels).tobytes()).hexdigest() for image in image_set(node))


def pattern(size, seed):
    rng = np.random.default_rng(seed)
    value = rng.integers(0, 256, size=size * size, dtype=np.uint8)
    coverage = np.resize(np.array([0, 1, 63, 127, 128, 254, 255], dtype=np.uint8), size * size)
    pixels = np.empty((size * size, 4), dtype=np.float32)
    pixels[:, :3] = value[:, None].astype(np.float32) / 255
    pixels[:, 3] = coverage.astype(np.float32) / 255
    return pixels


def add_masked_fill(node):
    op(node, bpy.ops.node.tex_paint_layer_add, type='FILL')
    op(node, bpy.ops.node.tex_paint_mask_effect_add, effect_type='GENERATOR', gen_type='AO')
    op(node, bpy.ops.node.tex_paint_mask_effect_add, effect_type='PAINT')
    return node.active_layer


def paint_state(node, layer, uid):
    return next(e for e in paint._get_layer_grunge_state(node, layer)['effects'] if e['uid'] == uid)


def saved_state(node, layer):
    return json.loads(node['_grunge_layers_json'])[str(layer.uid)]


def snapshot_node(node):
    return {
        'images': tuple(image.name for image in image_set(node)),
        'layers': {layer.uid: {'mask': rgba(layer.mask_pixels).copy(),
                               'native': native_snapshot(layer),
                               'state': semantic_state(paint._get_layer_grunge_state(node, layer))}
                   for layer in node.layers if layer.use_mask},
    }


def run_case(is_float):
    label = 'Float' if is_float else 'Byte'
    size = 64
    bpy.ops.mesh.primitive_cube_add()
    original = bpy.context.object
    original.name = label + ' Original Part'
    node, generated_uid, painted_uid = fixture(is_float, size=size)
    material = original.active_material
    material.name = 'Copy Split ' + label
    source = node.active_layer
    source.name = 'Source Native Mask'
    source_values = pattern(size, 11 if is_float else 7)
    effect(source, painted_uid).pixels.foreach_set(source_values.ravel())
    state = paint._get_layer_grunge_state(node, source)
    metadata = paint_state(node, source, painted_uid)
    metadata.update({'name': 'Source Painted Detail', 'blend_mode': 'MULTIPLY', 'opacity': .73})
    # A valid legacy payload verifies that native authoring JSON removes duplicate pixels.
    visible = rgba(effect(source, painted_uid).pixels)
    values = np.rint(visible[:, 0] * 255).astype(np.uint8)
    alpha = np.rint(visible[:, 3] * 255).astype(np.uint8)
    metadata['paint_z64'] = base64.b64encode(zlib.compress(values.tobytes() + alpha.tobytes())).decode('ascii')
    metadata['paint_w'] = metadata['paint_h'] = size
    paint._save_layer_grunge_state(node, source, state, flush_json=False)
    serialized = next(e for e in saved_state(node, source)['effects'] if e['uid'] == painted_uid)
    check(label + ' nonflushed authoring settings persist immediately',
          serialized['name'] == 'Source Painted Detail' and serialized['opacity'] == .73)
    check(label + ' native Paint pixels are omitted from authoring JSON',
          not {'paint_z64', 'paint_w', 'paint_h'}.intersection(serialized))
    paint._evaluate_layer_grunge_mask(node, source, force_reproject=False)

    target = add_masked_fill(node)
    target.name = 'Target Native Mask'
    target_values = pattern(size, 41)
    source_effect = effect(source, painted_uid)
    target_effect = effect(target, painted_uid)
    target_effect.pixels.foreach_set(target_values.ravel())
    # Match revisions deliberately, reproducing the stale-cache identity collision.
    for _ in range(32):
        if source_effect.revision == target_effect.revision:
            break
        if source_effect.revision < target_effect.revision:
            source_effect.pixels.foreach_set(source_values.ravel())
        else:
            target_effect.pixels.foreach_set(target_values.ravel())
    check(label + ' copy fixture has matching effect IDs and revisions',
          source_effect.uid == target_effect.uid and source_effect.revision == target_effect.revision)
    target.mask_stack_evaluate()
    paint._ensure_paint_effect_buffer(node.as_pointer(), target.uid,
                                      paint_state(node, target, painted_uid), size, size)
    expected_native = native_snapshot(source)
    expected_mask = rgba(source.mask_pixels).copy()
    expected_state = semantic_state(paint._get_layer_grunge_state(node, source))
    check(label + ' Copy Mask Below completes', op(
        node, bpy.ops.node.tex_paint_layer_pixel_action, action='COPY_MASK_BELOW') == {'FINISHED'})
    check(label + ' native copied RGB and coverage match exactly',
          all(np.array_equal(rgba(effect(target, uid).pixels), values)
              for uid, values in expected_native.items()))
    check(label + ' copied evaluated mask and authoring metadata match',
          np.array_equal(rgba(target.mask_pixels), expected_mask) and
          semantic_state(paint._get_layer_grunge_state(node, target)) == expected_state)
    buffer = paint._ensure_paint_effect_buffer(node.as_pointer(), target.uid,
                                               paint_state(node, target, painted_uid), size, size)
    expected = expected_native[painted_uid]
    check(label + ' warmed target cache receives copied native coverage',
          np.array_equal(buffer['alpha_u8'], np.rint(expected[:, 3] * 255).astype(np.uint8)))
    changed = rgba(effect(target, painted_uid).pixels)
    changed[:, :3] = 1 - changed[:, :3]
    changed[:, 3] = 1 - changed[:, 3]
    effect(target, painted_uid).pixels.foreach_set(changed.ravel())
    target.mask_stack_evaluate()
    target_mask = rgba(target.mask_pixels).copy()
    check(label + ' copied Paint edits are independent of the source layer',
          np.array_equal(rgba(source.mask_pixels), expected_mask) and
          np.array_equal(rgba(source_effect.pixels), expected_native[painted_uid]))
    op(node, bpy.ops.node.tex_paint_mask_effect_toggle, effect_uid=painted_uid)
    op(node, bpy.ops.node.tex_paint_mask_effect_toggle, effect_uid=painted_uid)
    check(label + ' copied Paint toggles preserve independent edits',
          np.array_equal(rgba(target.mask_pixels), target_mask) and
          np.array_equal(rgba(source.mask_pixels), expected_mask))

    op(node, bpy.ops.node.tex_paint_layer_add, type='FILL')
    plain = node.active_layer
    plain.name = 'Source Plain Mask'
    plain.use_mask = True
    plain.mask_effects_clear()
    plain.invert_mask = True
    node.paint_target = 'MASK'
    plain_values = np.ones((size * size, 4), dtype=np.float32)
    plain_values[:, :3] = np.linspace(.1234567, .9876543, size * size, dtype=np.float32)[:, None]
    node.paint_pixels.foreach_set(plain_values.ravel())
    paint._save_layer_grunge_state(node, plain, paint._get_layer_grunge_state(node, plain))
    plain_before = rgba(plain.mask_pixels).copy()
    plain_target = add_masked_fill(node)
    plain_target.name = 'Target Plain Mask'
    check(label + ' plain Copy Mask Below completes', op(
        node, bpy.ops.node.tex_paint_layer_pixel_action, action='COPY_MASK_BELOW') == {'FINISHED'})
    check(label + ' plain mask copies values and flags without a native stack',
          not plain_target.mask_effects_active and len(plain_target.mask_effects) == 0 and
          plain_target.use_mask and plain_target.invert_mask and
          np.array_equal(rgba(plain_target.mask_pixels), plain_before))
    independent = plain_before.copy()
    independent[:, :3] *= .5
    node.paint_pixels.foreach_set(independent.ravel())
    check(label + ' copied plain mask has independent pixel ownership',
          np.array_equal(rgba(plain.mask_pixels), plain_before) and
          not np.array_equal(rgba(plain_target.mask_pixels), plain_before))

    split = bpy.data.objects.new(label + ' Split Part', original.data)
    bpy.context.collection.objects.link(split)
    original.select_set(False)
    split.select_set(True)
    bpy.context.view_layer.objects.active = split
    original_images = image_set(node)
    original_users = tuple(image.users for image in original_images)
    original_hashes = image_hashes(node)
    check(label + ' split fixture shares exactly one material between two objects',
          split.active_material == material and split.data == original.data and material.users == 1)
    check(label + ' Active Object material split completes', op(
        node, bpy.ops.node.tex_paint_make_material_unique, mode='ACTIVE_OBJECT') == {'FINISHED'})
    unique_material = split.active_material
    unique_node = next(n for n in unique_material.node_tree.nodes if n.bl_idname == 'ShaderNodeTexPaint')
    copied_images = image_set(unique_node)
    check(label + ' split retains separate material assignments',
          unique_material != material and original.active_material == material and
          material.users == 1 and unique_material.users == 1)
    check(label + ' split retains shared geometry with an object material override',
          split.data == original.data and split.material_slots[0].link == 'OBJECT' and
          original.data.materials[0] == material)
    check(label + ' all three derived image IDs are independent',
          all(image is not None for image in original_images + copied_images) and
          len({image.as_pointer() for image in original_images + copied_images}) == 6)
    check(label + ' split preserves original image users',
          tuple(image.users for image in original_images) == original_users and
          all(image.users >= 1 for image in copied_images))
    check(label + ' splitting does not change source derived pixels', image_hashes(node) == original_hashes)
    copied_layer = next(layer for layer in unique_node.layers if layer.uid == source.uid)
    unique_node.active_layer_index = next(i for i, layer in enumerate(unique_node.layers) if layer == copied_layer)
    unique_node.paint_target = 'MASK'
    copy_values = rgba(effect(copied_layer, painted_uid).pixels)
    copy_values[:, :3] = .1
    copy_values[:, 3] = 1
    effect(copied_layer, painted_uid).pixels.foreach_set(copy_values.ravel())
    copied_state = paint._get_layer_grunge_state(unique_node, copied_layer)
    paint_state(unique_node, copied_layer, painted_uid)['name'] = 'Independent Split Detail'
    paint._save_layer_grunge_state(unique_node, copied_layer, copied_state, flush_json=False)
    paint._evaluate_layer_grunge_mask(unique_node, copied_layer, force_reproject=False)
    unique_node.layers[-1].fill_color = (.05, .9, .3, 1)
    unique_node.layers[-1].fill_roughness = .11
    unique_node.layers[-1].fill_emission = (.2, .6, .9)
    check(label + ' editing the split leaves all original image hashes unchanged',
          image_hashes(node) == original_hashes and
          np.array_equal(rgba(source_effect.pixels), expected_native[painted_uid]))
    check(label + ' editing the split changes its independent derived output',
          image_hashes(unique_node) != original_hashes)
    check(label + ' split settings publish before any idle timer',
          next(e for e in saved_state(unique_node, copied_layer)['effects']
               if e['uid'] == painted_uid)['name'] == 'Independent Split Detail')
    # Drop Python caches before saving: native coverage and eager settings must stand alone.
    paint._clear_all_runtime_mask_caches(clear_raw_cache=True)
    check(label + ' cache clearing preserves independently authored split metadata',
          paint_state(unique_node, copied_layer, painted_uid)['name'] == 'Independent Split Detail')

    # Multi-part ALL_SHARED_OBJECTS atomic rollback and clean split across multiple parts.
    shared_b = bpy.data.objects.new(label + ' Shared Part B', original.data)
    shared_c = bpy.data.objects.new(label + ' Shared Part C', original.data)
    bpy.context.collection.objects.link(shared_b)
    bpy.context.collection.objects.link(shared_c)
    bpy.context.view_layer.objects.active = original
    mats_before = set(bpy.data.materials)
    imgs_before = set(bpy.data.images)
    op_cls = paint.NODE_OT_tex_paint_make_material_unique
    real_rebake = op_cls._rebake_node_layers
    calls = {'count': 0}

    @staticmethod
    def fail_on_second_part(target_node, ctx):
        calls['count'] += 1
        if calls['count'] >= 2:
            raise RuntimeError('Injected second-part rebake failure')
        return real_rebake(target_node, ctx)

    op_cls._rebake_node_layers = fail_on_second_part
    try:
        res = op(node, bpy.ops.node.tex_paint_make_material_unique, mode='ALL_SHARED_OBJECTS')
    finally:
        op_cls._rebake_node_layers = staticmethod(real_rebake)
    check(label + ' multi-part split failure on second part cancels cleanly',
          res == {'CANCELLED'} and calls['count'] == 2)
    check(label + ' multi-part split rollback restores all shared object slots and links',
          original.active_material == material and
          shared_b.active_material == material and shared_b.material_slots[0].link == 'DATA' and
          shared_c.active_material == material and shared_c.material_slots[0].link == 'DATA')
    check(label + ' multi-part split rollback leaves zero orphan materials or images',
          set(bpy.data.materials) == mats_before and set(bpy.data.images) == imgs_before and
          image_hashes(node) == original_hashes)

    check(label + ' All Shared Objects multi-part split completes cleanly',
          op(node, bpy.ops.node.tex_paint_make_material_unique, mode='ALL_SHARED_OBJECTS') == {'FINISHED'})
    mat_b = shared_b.active_material
    mat_c = shared_c.active_material
    node_b = next(n for n in mat_b.node_tree.nodes if n.bl_idname == 'ShaderNodeTexPaint')
    node_c = next(n for n in mat_c.node_tree.nodes if n.bl_idname == 'ShaderNodeTexPaint')
    check(label + ' All Shared Objects split assigns distinct materials and images per part',
          original.active_material == material and
          len({material, mat_b, mat_c}) == 3 and
          shared_b.material_slots[0].link == 'OBJECT' and
          shared_c.material_slots[0].link == 'OBJECT' and
          len({img.as_pointer() for n in (node, node_b, node_c) for img in image_set(n)}) == 9)

    return {(material.name, node.name): snapshot_node(node),
            (unique_material.name, unique_node.name): snapshot_node(unique_node),
            (mat_b.name, node_b.name): snapshot_node(node_b),
            (mat_c.name, node_c.name): snapshot_node(node_c)}


def workflow():
    expected = {}
    expected.update(run_case(False))
    expected.update(run_case(True))
    path = args.output / 'mask-copy-and-split.blend'
    check('copy and material split fixture saves', bpy.ops.wm.save_as_mainfile(filepath=str(path)) == {'FINISHED'})
    check('copy and material split fixture reopens', bpy.ops.wm.open_mainfile(filepath=str(path)) == {'FINISHED'})
    for (material_name, node_name), stored in expected.items():
        node = bpy.data.materials[material_name].node_tree.nodes[node_name]
        label = material_name
        check(label + ' derived image references survive reopening',
              tuple(image.name for image in image_set(node)) == stored['images'])
        for uid, values in stored['layers'].items():
            layer = next(layer for layer in node.layers if layer.uid == uid)
            check(label + ' layer ' + str(uid) + ' mask survives reopening',
                  np.array_equal(rgba(layer.mask_pixels), values['mask']))
            check(label + ' layer ' + str(uid) + ' native coverage survives reopening',
                  all(np.array_equal(rgba(effect(layer, eid).pixels), pixels)
                      for eid, pixels in values['native'].items()))
            check(label + ' layer ' + str(uid) + ' authoring metadata survives reopening',
                  semantic_state(paint._get_layer_grunge_state(node, layer)) == values['state'])
            serialized = saved_state(node, layer)
            check(label + ' layer ' + str(uid) + ' has no duplicate native Paint payload',
                  all(not {'paint_z64', 'paint_w', 'paint_h'}.intersection(e)
                      for e in serialized.get('effects', []) if e.get('type') == 'PAINT'))
    report['passed'] = True


try:
    workflow()
except Exception:
    report['error'] = traceback.format_exc()
    print(report['error'], flush=True)
(args.output / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
if not report['passed']:
    raise RuntimeError('Mask copy and material split regression failed')
