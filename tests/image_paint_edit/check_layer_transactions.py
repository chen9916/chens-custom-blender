# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Native-window layer transactions survive memfile undo/redo and save/reopen."""

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
assert '--factory-startup' in sys.argv and not bpy.app.background and not bpy.data.filepath
args.output.mkdir(parents=True, exist_ok=True)
bpy.context.preferences.filepaths.use_auto_save_temporary_files = False
bpy.context.preferences.filepaths.temporary_directory = str(args.output)
bpy.context.preferences.edit.undo_steps = 64
bpy.context.preferences.view.show_splash = False
report = {'build_hash': bpy.app.build_hash.decode(), 'build_time': bpy.app.build_time.decode(),
          'checks': [], 'passed': False}
material_name = ''


def check(name, condition):
    condition = bool(condition)
    report['checks'].append({'name': name, 'passed': condition})
    print('LAYER_TRANSACTION_CHECK', name, condition, flush=True)
    assert condition, name


def nodes():
    tree = bpy.data.materials[material_name].node_tree
    return [next(node for node in tree.nodes if node.bl_idname == 'ShaderNodeTexPaint' and node.label == label)
            for label in ('Base Color', 'Roughness')]


def invoke(operator, *positional, node=None, **kwargs):
    window = bpy.context.window_manager.windows[0]
    area = next(area for area in window.screen.areas if area.type == 'VIEW_3D')
    region = next(region for region in area.regions if region.type == 'WINDOW')
    override = {'window': window, 'area': area, 'region': region}
    if node is not None:
        override['node'] = node
    with bpy.context.temp_override(**override):
        return operator(*positional, **kwargs)


def node_op(operator, *, undo=False, **kwargs):
    return invoke(operator, 'EXEC_DEFAULT', undo, node=nodes()[0], **kwargs)


def array(prop):
    values = np.empty(len(prop), dtype=np.float32)
    prop.foreach_get(values)
    return values.copy()


def clean_authoring(raw):
    states = json.loads(raw or '{}')
    for state in states.values():
        for effect in state.get('effects', []):
            for key in ('paint_z64', 'paint_w', 'paint_h'):
                effect.pop(key, None)
    return states


def snapshot():
    result = {}
    layer_properties = (
        'uid', 'name', 'type', 'blend_mode', 'opacity', 'hide', 'lock', 'lock_alpha',
        'use_clipping', 'use_mask', 'invert_mask', 'preview_mask', 'mask_base_value',
        'mask_effects_active', 'active_mask_effect_uid', 'proj_mode', 'proj_image_name',
        'fill_roughness', 'fill_metallic', 'fill_height', 'fill_specular',
        'use_channel_color', 'use_channel_roughness', 'use_channel_metallic',
        'use_channel_height', 'use_channel_normal', 'use_channel_specular', 'use_channel_emission')
    for node in nodes():
        layers = []
        for layer in node.layers:
            item = {name: getattr(layer, name) for name in layer_properties}
            item['fill_color'] = tuple(layer.fill_color)
            item['fill_emission'] = tuple(layer.fill_emission)
            item['mask_pixels'] = array(layer.mask_pixels)
            item['effects'] = [
                {'uid': effect.uid, 'type': effect.type, 'enabled': effect.enabled,
                 'blend_mode': effect.blend_mode, 'opacity': effect.opacity, 'invert': effect.invert,
                 'curve': tuple(effect.curve), 'filter_type': effect.filter_type,
                 'filter_radius': effect.filter_radius, 'filter_strength': effect.filter_strength,
                 'filter_scale': effect.filter_scale, 'pixels': array(effect.pixels)}
                for effect in layer.mask_effects]
            item['color_filters'] = [
                {'name': effect.name, 'type': effect.type, 'enabled': effect.enabled,
                 'strength': effect.strength, 'hsv': tuple(effect.hsv),
                 'bright_contrast': tuple(effect.bright_contrast), 'input_black': effect.input_black,
                 'input_white': effect.input_white, 'gamma': effect.gamma,
                 'output_black': effect.output_black, 'output_white': effect.output_white,
                 'blur_radius': effect.blur_radius}
                for effect in layer.color_filters]
            layers.append(item)
        result[node.label] = {
            'active_uid': node.active_layer.uid, 'paint_target': node.paint_target, 'layers': layers,
            'authoring': clean_authoring(node.get('_grunge_layers_json', '{}')),
            'color': array(node.image.pixels), 'pbr': array(node.pbr_image.pixels),
            'emission': array(node.emission_image.pixels)}
    return result


def same(before, after):
    if isinstance(before, np.ndarray):
        return (isinstance(after, np.ndarray) and before.shape == after.shape and
                np.allclose(before, after, atol=2e-6, rtol=1e-6))
    if isinstance(before, dict):
        return before.keys() == after.keys() and all(same(before[key], after[key]) for key in before)
    if isinstance(before, (list, tuple)):
        return len(before) == len(after) and all(same(a, b) for a, b in zip(before, after))
    if isinstance(before, float):
        return abs(before - after) <= 2e-6
    return before == after


def create_fixture():
    global material_name
    material = bpy.data.materials.new('Layer Transaction')
    material_name = material.name
    material.use_nodes = True
    bpy.context.object.data.materials.clear()
    bpy.context.object.data.materials.append(material)
    for label in ('Base Color', 'Roughness'):
        node = material.node_tree.nodes.new('ShaderNodeTexPaint')
        node.label = label
        invoke(bpy.ops.node.tex_paint_canvas_new, node=node, name='Transaction ' + label,
               width=16, height=8, color=(.25, .4, .6, .8), **{'float': True})
    node_op(bpy.ops.node.tex_paint_layer_add, type='PAINT')
    for index, node in enumerate(nodes()):
        values = np.full((8, 16, 4), (.8, .2, .1, .6), dtype=np.float32)
        values[:, :8, :3] = (2.0, .3, .2) if index == 0 else (.15, .15, .15)
        node.paint_pixels.foreach_set(values.ravel())
    primary = nodes()[0]
    image = bpy.data.images.new('Transaction AO', width=16, height=8, float_buffer=True)
    image.colorspace_settings.name = 'Non-Color'
    values = np.ones((8, 16, 4), dtype=np.float32)
    values[..., :3] = .65
    image.pixels.foreach_set(values.ravel())
    image.pack()
    image.use_fake_user = True
    paint._save_mesh_maps_state(primary, map_updates={'ao': image.name})


def add_mask(layer_index):
    primary = nodes()[0]
    primary.active_layer_index = layer_index
    node_op(bpy.ops.node.tex_paint_mask_effect_add, effect_type='GENERATOR', gen_type='AO')
    node_op(bpy.ops.node.tex_paint_mask_effect_add, effect_type='PAINT')
    effect = next(effect for effect in primary.active_layer.mask_effects if effect.type == 1)
    values = np.zeros((8, 16, 4), dtype=np.float32)
    values[2:5, 3:7] = (.1, .1, .1, .7)
    effect.pixels.foreach_set(values.ravel())
    primary.active_layer.mask_stack_evaluate()
    paint._sync_layer_mask_to_companion_channels(primary)


def setup_case(name):
    create_fixture()
    if name == 'mask_copy':
        add_mask(0)
        nodes()[0].active_layer_index = 1
        node_op(bpy.ops.node.tex_paint_mask_effect_add, effect_type='PAINT')
    elif name == 'rasterize':
        node_op(bpy.ops.node.tex_paint_layer_add, type='FILL')
        for node in nodes():
            node.active_layer.fill_color = (.35, .65, .9, .6)
            for channel in ('roughness', 'metallic', 'height', 'normal', 'specular', 'emission'):
                setattr(node.active_layer, 'use_channel_' + channel, False)
        add_mask(2)
        nodes()[0].paint_target = 'COLOR'
        node_op(bpy.ops.node.tex_paint_color_filter, action='ADD', type='INVERT')
        nodes()[0].active_layer.color_filters[0].strength = .25
    else:
        add_mask(1)
        if name == 'flatten':
            nodes()[0].paint_target = 'COLOR'
            node_op(bpy.ops.node.tex_paint_color_filter, action='ADD', type='INVERT')
            nodes()[0].active_layer.color_filters[0].strength = .25


def execute_case(name):
    if name == 'lock':
        companion = nodes()[1]
        path = ('active_object.active_material.node_tree.nodes[' + repr(companion.name) +
                '].active_layer.lock')
        return invoke(bpy.ops.wm.context_toggle, 'EXEC_DEFAULT', True, data_path=path)
    if name == 'duplicate':
        return node_op(bpy.ops.node.tex_paint_layer_duplicate, undo=True)
    if name == 'merge_down':
        return node_op(bpy.ops.node.tex_paint_layer_merge_down, undo=True)
    action = {'mask_copy': 'COPY_MASK_BELOW', 'rasterize': 'RASTERIZE_TO_PAINT',
              'flatten': 'FLATTEN_ALL'}[name]
    return node_op(bpy.ops.node.tex_paint_layer_pixel_action, undo=True, action=action)


def workflow():
    yield .5
    for name in ('duplicate', 'mask_copy', 'rasterize', 'merge_down', 'flatten', 'lock'):
        setup_case(name)
        yield .1
        invoke(bpy.ops.ed.undo_push, message=name + ' baseline')
        before = snapshot()
        check(name + ' executes as one native transaction', execute_case(name) == {'FINISHED'})
        yield .1
        after = snapshot()
        check(name + ' changes the intended layer state', not same(before, after))
        if name == 'lock':
            check('companion lock transaction protects both channels',
                  all(node.active_layer.lock for node in nodes()))
        invoke(bpy.ops.ed.undo)
        yield .1
        check(name + ' undo restores both channels and native mask authoring', same(before, snapshot()))
        invoke(bpy.ops.ed.redo)
        yield .1
        check(name + ' redo restores both channels and native mask authoring', same(after, snapshot()))
        path = args.output / (name + '.blend')
        invoke(bpy.ops.wm.save_as_mainfile, filepath=str(path))
        invoke(bpy.ops.wm.open_mainfile, filepath=str(path))
        yield .2
        check(name + ' save and reopen preserve the completed transaction', same(after, snapshot()))
    report['passed'] = True


steps = workflow()


def tick():
    try:
        return next(steps)
    except StopIteration:
        pass
    except Exception:
        report['error'] = traceback.format_exc()
        print(report['error'], flush=True)
    (args.output / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    bpy.ops.wm.quit_blender()
    return None


bpy.app.timers.register(tick, first_interval=1, persistent=True)
