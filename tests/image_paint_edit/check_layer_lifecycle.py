# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Layer duplication and channel-wide merges preserve pixels and mask authoring."""

import argparse
import json
import sys
import traceback
from pathlib import Path

import bpy
import numpy as np
from bl_ui import properties_tex_paint_layers as paint

sys.path.insert(0, str(Path(__file__).parent))
from check_mask_paint_stack import fixture, effect_pixels, sparse_stroke

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
    print('LAYER_LIFECYCLE_CHECK', name, condition, flush=True)
    assert condition, name


def authoring_controls(effects):
    # Native storage owns coverage. A compatibility snapshot in a runtime dict
    # may be omitted from the persisted authoring JSON without changing controls.
    return [{key: value for key, value in effect.items()
             if key not in {'paint_z64', 'paint_w', 'paint_h'}} for effect in effects]


def op(node, operator, **kwargs):
    with bpy.context.temp_override(node=node):
        return operator(**kwargs)


def image_pixels(node):
    result = np.empty(len(node.image.pixels), dtype=np.float32)
    node.image.pixels.foreach_get(result)
    return result.copy()


def raw_pixels(node, layer_uid, value=None):
    node.active_layer_index = next(i for i, layer in enumerate(node.layers) if layer.uid == layer_uid)
    node.paint_target = 'COLOR'
    if value is not None:
        values = np.tile(np.array(value, dtype=np.float32), node.canvas_width * node.canvas_height)
        node.paint_pixels.foreach_set(values)
    result = np.empty(len(node.paint_pixels), dtype=np.float32)
    node.paint_pixels.foreach_get(result)
    return result


def make_pair():
    material = bpy.data.materials.new('Lifecycle PBR')
    material.use_nodes = True
    bpy.context.object.data.materials.clear()
    bpy.context.object.data.materials.append(material)
    primary = material.node_tree.nodes.new('ShaderNodeTexPaint')
    primary.label = 'Base Color'
    op(primary, bpy.ops.node.tex_paint_canvas_new, name='Lifecycle Base Color', width=16, height=16,
       color=(.3, .2, .1, 1))
    companion = material.node_tree.nodes.new('ShaderNodeTexPaint')
    companion.label = 'Roughness'
    op(companion, bpy.ops.node.tex_paint_canvas_new, name='Lifecycle Roughness', width=16, height=16,
       color=(.8, .8, .8, 1))
    op(primary, bpy.ops.node.tex_paint_layer_add, type='PAINT')
    top_uid = primary.active_layer.uid
    below_uid = primary.layers[0].uid
    raw_pixels(primary, below_uid, (.3, .2, .1, 1))
    raw_pixels(primary, top_uid, (.7, .1, .4, .35))
    raw_pixels(companion, below_uid, (.8, .8, .8, 1))
    raw_pixels(companion, top_uid, (.2, .2, .2, .6))
    primary.active_layer_index = 1
    return primary, companion, below_uid, top_uid


def duplicate_masked_layer():
    node, generator_uid, painted_uid = fixture(size=64)
    original_uid = node.active_layer.uid
    sparse_stroke(node, painted_uid)
    state = paint._get_layer_grunge_state(node)
    generator = next(effect for effect in state['effects'] if effect['uid'] == generator_uid)
    generator['name'] = 'Authored generator settings'
    generator['gen_balance'] = .73
    paint._save_layer_grunge_state(node, node.active_layer, state, flush_json=True)
    paint._evaluate_layer_grunge_mask(node, node.active_layer, skip_absorb=True)
    source_pixels = effect_pixels(node, painted_uid)
    expected_state = json.loads(node['_grunge_layers_json'])[str(original_uid)]
    check('masked layer duplicate finishes', op(node, bpy.ops.node.tex_paint_layer_duplicate) == {'FINISHED'})
    duplicate_uid = node.active_layer.uid
    check('duplicated masked layer receives a new layer UID', duplicate_uid != original_uid)
    duplicate_state = paint._get_layer_grunge_state(node)
    check('duplicated native stack retains matching authoring controls',
          authoring_controls(duplicate_state['effects']) == authoring_controls(expected_state['effects']) and
          [effect.uid for effect in node.active_layer.mask_effects] ==
          [effect['uid'] for effect in duplicate_state['effects']])
    check('duplicated Paint coverage initially matches its source',
          np.array_equal(effect_pixels(node, painted_uid), source_pixels))
    changed = effect_pixels(node, painted_uid)
    changed[20:23, 21:24] = (1, 1, 1, 1)
    native = next(effect for effect in node.active_layer.mask_effects if effect.uid == painted_uid)
    native.pixels.foreach_set(changed.ravel())
    node.active_layer.mask_stack_evaluate()
    duplicate_state['effects'][0]['name'] = 'Independent duplicate'
    paint._save_layer_grunge_state(node, node.active_layer, duplicate_state, flush_json=True)
    node.active_layer_index = next(i for i, layer in enumerate(node.layers) if layer.uid == original_uid)
    check('duplicated Paint edits leave original coverage intact',
          np.array_equal(effect_pixels(node, painted_uid), source_pixels))
    check('duplicated generator authoring remains independent',
          paint._get_layer_grunge_state(node)['effects'][0]['name'] == 'Authored generator settings')


def duplicate_from_companion():
    primary, companion, below_uid, top_uid = make_pair()
    op(primary, bpy.ops.node.tex_paint_mask_effect_add, effect_type='PAINT')
    uid = paint._get_active_mask_effect(primary)['uid']
    state = paint._get_layer_grunge_state(primary)
    state['effects'][0]['name'] = 'Canonical mask authoring'
    paint._save_layer_grunge_state(primary, primary.active_layer, state)
    paint._evaluate_layer_grunge_mask(primary, skip_absorb=True)
    check('companion duplication starts with canonical metadata owner',
          paint._canonical_mask_owner_node(companion) == primary)
    check('duplicate from companion channel finishes',
          op(companion, bpy.ops.node.tex_paint_layer_duplicate) == {'FINISHED'})
    new_uid = companion.active_layer.uid
    primary.active_layer_index = next(i for i, layer in enumerate(primary.layers) if layer.uid == new_uid)
    check('companion duplicate remaps canonical authoring metadata',
          paint._get_layer_grunge_state(primary)['effects'][0]['name'] == 'Canonical mask authoring' and
          paint._get_layer_grunge_state(companion)['effects'][0]['uid'] == uid and
          primary.active_layer.mask_effects_active and companion.active_layer.mask_effects_active)


def merge_channels():
    primary, companion, below_uid, top_uid = make_pair()
    op(primary, bpy.ops.node.tex_paint_mask_effect_add, effect_type='PAINT')
    # An empty mask-paint effect preserves the top pixels and tests authoring cleanup.
    primary.paint_target = companion.paint_target = 'COLOR'
    before_primary = image_pixels(primary)
    before_companion = image_pixels(companion)
    primary.active_layer_index = 1
    check('Merge Down finishes across companion channels',
          op(primary, bpy.ops.node.tex_paint_layer_merge_down) == {'FINISHED'})
    check('Merge Down removes matching top UIDs in every channel',
          len(primary.layers) == len(companion.layers) == 1 and
          primary.layers[0].uid == companion.layers[0].uid == below_uid)
    check('primary merge preserves its own composited color',
          np.allclose(image_pixels(primary), before_primary, atol=2/255))
    check('companion merge preserves its own composited channel values',
          np.allclose(image_pixels(companion), before_companion, atol=2/255))
    check('merged layer has no stale native or authored mask effects',
          not primary.active_layer.mask_effects_active and not primary.active_layer.use_mask and
          not paint._get_layer_grunge_state(primary).get('effects') and
          str(top_uid) not in json.loads(primary.get('_grunge_layers_json', '{}')))


def merge_preflight():
    primary, companion, below_uid, top_uid = make_pair()
    companion.layers[0].lock = True
    before = {node.name: ([layer.uid for layer in node.layers], image_pixels(node))
              for node in (primary, companion)}
    check('Merge Down rejects an incompatible locked companion',
          op(primary, bpy.ops.node.tex_paint_layer_merge_down) == {'CANCELLED'})
    check('rejected Merge Down leaves every channel unchanged',
          all([layer.uid for layer in node.layers] == before[node.name][0] and
              np.array_equal(image_pixels(node), before[node.name][1])
              for node in (primary, companion)))
    companion.layers[0].lock = False
    # A malformed metadata payload must also reject before duplicating native layers.
    primary['_grunge_layers_json'] = '{broken'
    before_uids = [layer.uid for layer in primary.layers]
    check('duplicate rejects malformed authoring data without adding a layer',
          op(primary, bpy.ops.node.tex_paint_layer_duplicate) == {'CANCELLED'} and
          [layer.uid for layer in primary.layers] == before_uids)


def native_mask_reset():
    for mode in ('WHITE', 'BLACK', 'REMOVE'):
        primary, companion, below_uid, top_uid = make_pair()
        op(primary, bpy.ops.node.tex_paint_mask_effect_add, effect_type='PAINT')
        uid = paint._get_active_mask_effect(primary)['uid']
        native = next(effect for effect in primary.active_layer.mask_effects if effect.uid == uid)
        values = np.tile(np.array((.25, .25, .25, 1), dtype=np.float32), 16 * 16)
        native.pixels.foreach_set(values)
        primary.active_layer.mask_stack_evaluate()
        paint._sync_layer_mask_to_companion_channels(primary)
        check(mode + ' native mask action finishes',
              op(primary, bpy.ops.node.tex_paint_layer_mask_init, mode=mode) == {'FINISHED'})
        check(mode + ' reset clears native and authoring effects in every channel',
              all(not node.active_layer.mask_effects_active and not node.active_layer.mask_effects and
                  not paint._get_layer_grunge_state(node).get('effects')
                  for node in (primary, companion)))
        if mode == 'REMOVE':
            check('Remove resets channels to an unmasked layer',
                  all(not node.active_layer.use_mask for node in (primary, companion)))
        else:
            base = 0 if mode == 'BLACK' else 255
            check(mode + ' reset preserves the authored base value',
                  all(paint._get_layer_grunge_state(node)['base_value'] == base
                      for node in (primary, companion)))
        material_name = paint._get_material_for_node(primary).name
        path = args.output / ('native-reset-' + mode.lower() + '.blend')
        bpy.ops.wm.save_as_mainfile(filepath=str(path))
        bpy.ops.wm.open_mainfile(filepath=str(path))
        tree = bpy.data.materials[material_name].node_tree
        primary = next(node for node in tree.nodes
                       if node.bl_idname == 'ShaderNodeTexPaint' and node.label == 'Base Color')
        companion = next(node for node in tree.nodes
                         if node.bl_idname == 'ShaderNodeTexPaint' and node.label == 'Roughness')
        check(mode + ' reset cannot resurrect old effects after reopening',
              all(not node.active_layer.mask_effects_active and not node.active_layer.mask_effects and
                  not paint._get_layer_grunge_state(node).get('effects')
                  for node in (primary, companion)))
        if mode == 'BLACK':
            op(primary, bpy.ops.node.tex_paint_mask_effect_add, effect_type='PAINT')
            check('Paint added after a Black reset starts over the black base',
                  primary.active_layer.mask_base_value == 0 and
                  np.allclose(primary.active_layer.mask_pixels[:][::4], 0))

    primary, companion, below_uid, top_uid = make_pair()
    op(primary, bpy.ops.node.tex_paint_mask_effect_add, effect_type='PAINT')
    original_state = json.loads(primary['_grunge_layers_json'])[str(top_uid)]
    native_uids = [effect.uid for effect in primary.active_layer.mask_effects]
    check('native Invert preserves authored and native mask effects',
          op(primary, bpy.ops.node.tex_paint_layer_mask_init, mode='INVERT') == {'FINISHED'} and
          primary.active_layer.invert_mask and companion.active_layer.invert_mask and
          [effect.uid for effect in primary.active_layer.mask_effects] == native_uids and
          json.loads(primary['_grunge_layers_json'])[str(top_uid)] == original_state)


def replace_canvas():
    node, generator_uid, painted_uid = fixture(size=64)
    # Place authoring records under UID1 as well, because a replacement restarts the layer UIDs.
    node.active_layer_index = 0
    op(node, bpy.ops.node.tex_paint_mask_effect_add, effect_type='PAINT')
    check('canvas replacement fixture has authored effects on its base UID',
          bool(paint._get_layer_grunge_state(node).get('effects')))
    check('new canvas replaces the layer stack',
          op(node, bpy.ops.node.tex_paint_canvas_new, name='Replacement Canvas', width=32, height=32,
             color=(.2, .4, .6, 1)) == {'FINISHED'} and len(node.layers) == 1)
    check('new canvas cannot inherit old mask authoring through reused layer UIDs',
          not node.get('_grunge_layers_json') and not node.active_layer.mask_effects_active and
          not node.active_layer.use_mask and not paint._get_layer_grunge_state(node).get('effects'))
    # Creating a companion canvas must not clear the primary owner's existing authoring map.
    op(node, bpy.ops.node.tex_paint_mask_effect_add, effect_type='PAINT')
    authored = node['_grunge_layers_json']
    companion = node.id_data.nodes.new('ShaderNodeTexPaint')
    companion.label = 'Roughness'
    op(companion, bpy.ops.node.tex_paint_canvas_new, name='Replacement Roughness', width=32, height=32,
       color=(.5, .5, .5, 1))
    check('new companion canvas preserves the canonical owner authoring map',
          node['_grunge_layers_json'] == authored)


def shared_layer_locks():
    primary, companion, below_uid, top_uid = make_pair()
    before_pixels = [image_pixels(node) for node in (primary, companion)]
    before_sources = [raw_pixels(node, top_uid) for node in (primary, companion)]
    primary.active_layer.lock = True
    check('locking the primary layer protects every matching channel',
          all(node.active_layer.lock for node in (primary, companion)))
    check('locking layers leaves the channel composites unchanged',
          all(np.array_equal(image_pixels(node), before)
              for node, before in zip((primary, companion), before_pixels)))
    companion.active_layer.hide = True
    primary.active_layer.opacity = .35
    check('visibility and opacity changes cannot undo a shared layer lock',
          all(node.active_layer.lock for node in (primary, companion)))
    companion.active_layer.lock = False
    check('unlocking the companion layer unlocks every matching channel',
          not any(node.active_layer.lock for node in (primary, companion)))
    primary.active_layer.hide = False
    companion.active_layer.lock = True
    check('locking the companion layer protects the primary layer',
          all(node.active_layer.lock for node in (primary, companion)))
    primary.active_layer.opacity = .8
    primary.active_layer.hide = True
    primary.active_layer.hide = False
    check('later primary visibility edits retain the companion-authored lock',
          all(node.active_layer.lock for node in (primary, companion)))
    primary.active_layer.lock = False
    check('unlocking the primary layer unlocks every matching channel',
          not any(node.active_layer.lock for node in (primary, companion)))
    check('lock and visibility transactions preserve the raw painted sources',
          all(np.array_equal(raw_pixels(node, top_uid), before)
              for node, before in zip((primary, companion), before_sources)))


def independent_2d_locks():
    tree = bpy.data.node_groups.get('.TexPaint_2D_Layers')
    if tree is None:
        tree = bpy.data.node_groups.new('.TexPaint_2D_Layers', 'ShaderNodeTree')
    canvases = []
    for name, (w, h) in (('Independent 2D One', (8, 8)), ('Independent 2D Two', (12, 6))):
        node = tree.nodes.new('ShaderNodeTexPaint')
        op(node, bpy.ops.node.tex_paint_canvas_new, name=name, width=w, height=h,
           color=(.3, .5, .7, 1))
        canvases.append(node)
    first, second = canvases
    first.active_layer.lock = True
    check('separate 2D canvases keep locks independent despite matching layer UIDs',
          first.active_layer.uid == second.active_layer.uid and
          first.active_layer.lock and not second.active_layer.lock)
    second.active_layer.hide = True
    second.active_layer.opacity = .35
    second.active_layer.name = 'Canvas Two Base'
    check('another 2D canvas visibility, opacity, and rename cannot bleed or clear a layer lock',
          first.active_layer.lock and not second.active_layer.lock and
          not first.active_layer.hide and abs(first.active_layer.opacity - 1.0) < 1e-6 and
          first.active_layer.name != 'Canvas Two Base')
    second.active_layer.hide = False
    second.active_layer.opacity = 1.0
    second.active_layer.lock = True
    first.active_layer.lock = False
    check('unlocking one 2D canvas leaves the other canvas locked',
          not first.active_layer.lock and second.active_layer.lock)
    second.active_layer.lock = False

    # Mask init, use_mask, and invert_mask isolation across matching layer UIDs.
    check('2D mask init succeeds on first canvas',
          op(first, bpy.ops.node.tex_paint_layer_mask_init, mode='BLACK') == {'FINISHED'})
    check('2D mask init on first canvas does not add a mask to second canvas',
          first.active_layer.use_mask and not second.active_layer.use_mask)
    check('2D mask init succeeds on second canvas',
          op(second, bpy.ops.node.tex_paint_layer_mask_init, mode='WHITE') == {'FINISHED'})
    first.active_layer.invert_mask = True
    first.active_layer.use_mask = False
    check('2D use_mask and invert_mask stay isolated between canvases',
          not first.active_layer.use_mask and second.active_layer.use_mask and
          first.active_layer.invert_mask and not second.active_layer.invert_mask)
    first.active_layer.use_mask = True

    # Native mask effect selection isolation across matching layer UIDs.
    check('2D mask Paint effect adds on first canvas',
          op(first, bpy.ops.node.tex_paint_mask_effect_add, effect_type='PAINT') == {'FINISHED'})
    check('2D mask Paint effect adds on second canvas',
          op(second, bpy.ops.node.tex_paint_mask_effect_add, effect_type='PAINT') == {'FINISHED'})
    second_eff_uid = second.active_layer.active_mask_effect_uid
    second.paint_target = 'COLOR'
    first_eff_uid = first.active_layer.active_mask_effect_uid
    check('selecting mask Paint effect on first 2D canvas succeeds',
          op(first, bpy.ops.node.tex_paint_mask_effect_select, effect_uid=first_eff_uid) == {'FINISHED'})
    check('selecting mask Paint effect on first 2D canvas does not switch second canvas target',
          first.paint_target == 'MASK' and second.paint_target == 'COLOR' and
          second.active_layer.active_mask_effect_uid == second_eff_uid)

    # Native mask paint actions (FILL_WHITE, INVERT, CLEAR) and companion native mask stack sync.
    check('mask Paint effect FILL_WHITE action succeeds on second 2D canvas',
          op(second, bpy.ops.node.tex_paint_mask_paint_action, action='FILL_WHITE') == {'FINISHED'} and
          np.allclose(second.layers[0].mask_pixels[:][::4], 1.0))
    check('mask Paint effect INVERT_STROKES action succeeds on second 2D canvas',
          op(second, bpy.ops.node.tex_paint_mask_paint_action, action='INVERT_STROKES') == {'FINISHED'} and
          np.allclose(second.layers[0].mask_pixels[:][::4], 0.0))
    check('mask Paint effect CLEAR action succeeds on second 2D canvas',
          op(second, bpy.ops.node.tex_paint_mask_paint_action, action='CLEAR') == {'FINISHED'})

    # Layer stack operators (add, active_layer_index, duplicate, move, merge_down, flatten) isolation.
    check('adding a layer on first 2D canvas does not add to second canvas',
          op(first, bpy.ops.node.tex_paint_layer_add, type='PAINT') == {'FINISHED'} and
          len(first.layers) == 2 and len(second.layers) == 1)
    check('adding a layer on second 2D canvas keeps matching UIDs',
          op(second, bpy.ops.node.tex_paint_layer_add, type='PAINT') == {'FINISHED'} and
          first.layers[1].uid == second.layers[1].uid)
    first.active_layer_index = 0
    check('changing active_layer_index on first 2D canvas leaves second canvas at top layer',
          first.active_layer_index == 0 and second.active_layer_index == 1)
    first.active_layer_index = 1
    check('duplicating a layer on first 2D canvas does not duplicate on second canvas',
          op(first, bpy.ops.node.tex_paint_layer_duplicate) == {'FINISHED'} and
          len(first.layers) == 3 and len(second.layers) == 2)
    second_order_before = [l.uid for l in second.layers]
    check('moving a layer on first 2D canvas does not reorder second canvas',
          op(first, bpy.ops.node.tex_paint_layer_move, direction='DOWN') == {'FINISHED'} and
          [l.uid for l in second.layers] == second_order_before)
    first.active_layer_index = 2
    check('merge down on first 2D canvas succeeds despite second canvas having different size and layer count',
          op(first, bpy.ops.node.tex_paint_layer_merge_down) == {'FINISHED'} and
          len(first.layers) == 2 and len(second.layers) == 2)
    check('flattening first 2D canvas leaves second 2D canvas multi-layer stack intact',
          op(first, bpy.ops.node.tex_paint_layers_flatten) == {'FINISHED'} and
          len(first.layers) == 1 and len(second.layers) == 2)


for name, run in (
        ('duplicate_masked_layer', duplicate_masked_layer),
        ('duplicate_from_companion', duplicate_from_companion),
        ('merge_channels', merge_channels),
        ('merge_preflight', merge_preflight),
        ('native_mask_reset', native_mask_reset),
        ('replace_canvas', replace_canvas),
        ('shared_layer_locks', shared_layer_locks),
        ('independent_2d_locks', independent_2d_locks)):
    try:
        run()
    except Exception:
        report.setdefault('errors', []).append({'case': name, 'traceback': traceback.format_exc()})
        print(report['errors'][-1]['traceback'], flush=True)
report['passed'] = not report.get('errors') and all(item['passed'] for item in report['checks'])
(args.output / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
if not report['passed']:
    raise RuntimeError('Layer lifecycle regression failed; see report.json')
