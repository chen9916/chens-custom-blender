# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Responsive paint sidebar, explicit targets, and read-only material inspection."""

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
bpy.context.preferences.view.show_splash = False
window = bpy.context.window
area = next(a for a in window.screen.areas if a.type == 'VIEW_3D')
report = {'build_hash': bpy.app.build_hash.decode(), 'build_time': bpy.app.build_time.decode(),
          'checks': [], 'passed': False}


class VIEW3D_PT_paint_sidebar_regression(bpy.types.Panel):
    """Isolate the production drawing function from source-reload panel ordering."""
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Paint Test'
    bl_label = 'Paint Layers'

    def draw(self, context):
        paint.draw_tex_paint_layers(self.layout, context, paint.tex_paint_node_from_context(context))


bpy.utils.register_class(VIEW3D_PT_paint_sidebar_regression)


def node():
    return bpy.data.materials['Sidebar Test'].node_tree.nodes['Texture Paint']


def region(kind='WINDOW'):
    return next(r for r in area.regions if r.type == kind)


def op(operator, *pos, **kwargs):
    with bpy.context.temp_override(window=window, area=area, region=region()):
        return operator(*pos, **kwargs)


def check(name, condition):
    result = bool(condition)
    report['checks'].append({'name': name, 'passed': result})
    print('PAINT_SIDEBAR_CHECK', name, result, flush=True)
    assert result, name


def material_state():
    tree = node().id_data
    shader = tree.nodes['Principled BSDF']
    return (sorted((link.from_node.name, link.from_socket.name, link.to_node.name, link.to_socket.name)
                   for link in tree.links), float(shader.inputs['Emission Strength'].default_value))


def resize_sidebar(width):
    sidebar = region('UI')
    start_x, y = sidebar.x + 1, sidebar.y + sidebar.height // 2
    end_x = area.x + area.width - width
    window.event_simulate(type='MOUSEMOVE', value='NOTHING', x=start_x, y=y)
    yield .1
    window.event_simulate(type='LEFTMOUSE', value='PRESS', x=start_x, y=y)
    yield .1
    window.event_simulate(type='MOUSEMOVE', value='NOTHING', x=end_x, y=y)
    yield .15
    window.event_simulate(type='LEFTMOUSE', value='RELEASE', x=end_x, y=y)
    yield .4


def status():
    return paint._paint_target_status(node(), node().active_layer)


def raw_effect_pixels():
    return {effect.uid: np.asarray(effect.pixels[:], dtype=np.float32)
            for effect in node().active_layer.mask_effects}


def blocked_stroke(label):
    before = raw_effect_pixels()
    mask = np.asarray(node().active_layer.mask_pixels[:], dtype=np.float32)
    r = region()
    x, y = r.x + r.width // 2, r.y + r.height // 2
    window.event_simulate(type='MOUSEMOVE', value='NOTHING', x=x, y=y)
    yield .1
    window.event_simulate(type='LEFTMOUSE', value='PRESS', x=x, y=y)
    yield .1
    window.event_simulate(type='MOUSEMOVE', value='NOTHING', x=x + 20, y=y)
    yield .1
    window.event_simulate(type='LEFTMOUSE', value='RELEASE', x=x + 20, y=y)
    yield .2
    after = raw_effect_pixels()
    check(label + ' stroke preserves every raw effect and the evaluated mask',
          before.keys() == after.keys() and all(np.array_equal(before[uid], after[uid]) for uid in before) and
          np.array_equal(node().active_layer.mask_pixels[:], mask))


def workflow():
    yield .5
    material = bpy.data.materials.new('Sidebar Test')
    material.use_nodes = True
    bpy.context.object.data.materials.clear()
    bpy.context.object.data.materials.append(material)
    texture = material.node_tree.nodes.new('ShaderNodeTexPaint')
    material.node_tree.nodes.active = texture
    with bpy.context.temp_override(node=texture):
        bpy.ops.node.tex_paint_canvas_new(name='Sidebar Canvas', width=64, height=64, color=(.3, .1, .05, 1))
        bpy.ops.node.tex_paint_layer_add(type='FILL')
        texture.active_layer.name = 'Painted Surface'
        bpy.ops.node.tex_paint_mask_effect_add(effect_type='GENERATOR', gen_type='NORMAL_UP')
        generator = paint._get_active_mask_effect(texture)['uid']
        bpy.ops.node.tex_paint_mask_effect_add(effect_type='PAINT')
        painted = paint._get_active_mask_effect(texture)['uid']
        bpy.ops.node.tex_paint_mask_effect_add(effect_type='PAINT')
        second = paint._get_active_mask_effect(texture)['uid']
        bpy.ops.node.tex_paint_mask_effect_select(effect_uid=painted)
    custom = material.node_tree.nodes.new('ShaderNodeRGB')
    custom.name = 'Artist Color'
    material.node_tree.links.new(custom.outputs[0], material.node_tree.nodes['Principled BSDF'].inputs['Base Color'])
    material.node_tree.nodes['Principled BSDF'].inputs['Emission Strength'].default_value = 0
    untouched = material_state()
    area.spaces.active.show_region_ui = True
    area.spaces.active.shading.color_type = 'TEXTURE'
    op(bpy.ops.object.mode_set, mode='TEXTURE_PAINT')
    bpy.context.tool_settings.image_paint.mode = 'IMAGE'
    bpy.context.tool_settings.image_paint.canvas = texture.image
    op(bpy.ops.brush.asset_activate, asset_library_type='ESSENTIALS',
       relative_asset_identifier='brushes/essentials_brushes-mesh_texture.blend/Brush/Paint Hard')
    brush = bpy.context.tool_settings.image_paint.brush
    brush.color = (0, 0, 0)
    brush.size = 20
    brush.strength = 1
    brush.use_unified_color = brush.use_unified_size = brush.use_unified_strength = False
    yield .4
    region('UI').active_panel_category = 'Paint Test'
    bpy.context.window_manager.tex_paint_show_mesh_maps_hub = False
    yield .5
    with bpy.context.temp_override(window=window, area=area, region=region('UI')):
        check('production paint panel is available in this 3D context', paint.VIEW3D_PT_tex_paint_layers.poll(bpy.context))
    check('panel drawing preserves custom material links and values', material_state() == untouched)
    check('material connection inspection is read-only', paint._material_layer_links_missing(material, node()) and material_state() == untouched)
    check('selected Paint effect is the explicit brush target', status()[0] == 'PAINT_EFFECT' and status()[2])
    scale = bpy.context.preferences.system.ui_scale
    yield from resize_sidebar(int(300 * scale))
    with bpy.context.temp_override(window=window, area=area, region=region('UI')):
        narrow = paint._paint_sidebar_is_narrow(bpy.context)
    report['narrow_width'] = region('UI').width
    check('native sidebar uses the narrow layout', narrow)
    op(bpy.ops.screen.screenshot, filepath=str(args.output / 'sidebar-narrow.png'))
    op(bpy.ops.node.tex_paint_mask_effect_select, effect_uid=generator)
    check('generator selection explicitly blocks brushing', status()[0] == 'SELECT_PAINT' and
          not status()[2] and node().active_layer.active_mask_effect_uid == 0 and len(node().paint_pixels) == 0)
    yield from blocked_stroke('generator selected with two enabled Paint effects')
    yield .3
    op(bpy.ops.screen.screenshot, filepath=str(args.output / 'sidebar-generator.png'))
    op(bpy.ops.node.tex_paint_mask_effect_add, effect_type='FILTER', filter_type='BLUR')
    filtered = paint._get_active_mask_effect(node())['uid']
    check('filter selection explicitly blocks brushing with two enabled Paint effects',
          status()[0] == 'SELECT_PAINT' and not status()[2] and
          node().active_layer.active_mask_effect_uid == 0 and len(node().paint_pixels) == 0 and
          all(next(e for e in node().active_layer.mask_effects if e.uid == uid).enabled for uid in (painted, second)))
    yield from blocked_stroke('filter selected with two enabled Paint effects')
    op(bpy.ops.screen.screenshot, filepath=str(args.output / 'sidebar-filter.png'))
    op(bpy.ops.node.tex_paint_mask_effect_remove, effect_uid=filtered)
    op(bpy.ops.node.tex_paint_mask_effect_select, effect_uid=painted)
    check('Select Paint restores only the requested target', status()[0] == 'PAINT_EFFECT' and
          node().active_layer.active_mask_effect_uid == painted and len(node().paint_pixels) > 0)
    op(bpy.ops.node.tex_paint_mask_effect_toggle, effect_uid=painted)
    check('hidden Paint effect is reported and cannot redirect painting', status()[0] == 'DISABLED_PAINT' and
          node().active_layer.active_mask_effect_uid == painted and len(node().paint_pixels) == 0)
    yield from blocked_stroke('disabled selected Paint with another enabled Paint effect')
    yield .3
    op(bpy.ops.screen.screenshot, filepath=str(args.output / 'sidebar-disabled-paint.png'))
    op(bpy.ops.node.tex_paint_mask_effect_toggle, effect_uid=painted)
    saved_mask = np.array(node().active_layer.mask_pixels[:])
    saved_effects = raw_effect_pixels()
    node().active_layer.preview_mask = True
    yield .2
    node().active_layer.preview_mask = False
    check('View Layer Mask changes inspection without changing paint',
          np.array_equal(saved_mask, np.array(node().active_layer.mask_pixels[:])) and
          all(np.array_equal(saved_effects[uid], values) for uid, values in raw_effect_pixels().items()))
    yield from resize_sidebar(int(570 * scale))
    with bpy.context.temp_override(window=window, area=area, region=region('UI')):
        narrow = paint._paint_sidebar_is_narrow(bpy.context)
    report['wide_width'] = region('UI').width
    check('native sidebar uses the wide layout', not narrow)
    op(bpy.ops.screen.screenshot, filepath=str(args.output / 'sidebar-wide.png'))
    op(bpy.ops.node.tex_paint_layer_target_select, target='COLOR')
    check('fill settings clearly distinguish an unpaintable color target', status()[0] == 'FILL' and not status()[2])
    node().active_layer.lock = True
    check('locked target has an explicit status', status()[0] == 'LOCKED' and not status()[2])
    node().active_layer.lock = False
    node().active_layer.hide = True
    check('hidden target has an explicit status', status()[0] == 'HIDDEN' and not status()[2])
    node().active_layer.hide = False
    yield .2
    op(bpy.ops.screen.screenshot, filepath=str(args.output / 'sidebar-fill-wide.png'))
    yield from resize_sidebar(int(300 * scale))
    op(bpy.ops.screen.screenshot, filepath=str(args.output / 'sidebar-fill-narrow.png'))
    check('target navigation and both widths preserve the artist material', material_state() == untouched)
    op(bpy.ops.ed.undo_push, message='Before explicit material connection')
    check('explicit Connect Material action completes', op(
        bpy.ops.node.tex_paint_reconnect_live_layers, 'EXEC_DEFAULT', True, remove_baked_nodes=False) == {'FINISHED'})
    check('explicit Connect Material wires the layer outputs', not paint._material_layer_links_missing(material, node()))
    op(bpy.ops.ed.undo)
    check('material connection undo restores custom links', material_state() == untouched)
    area.type = 'IMAGE_EDITOR'
    area.spaces.active.image = node().image
    area.spaces.active.ui_mode = 'PAINT'
    area.spaces.active.show_region_ui = True
    yield .4
    region('UI').active_panel_category = 'Layers'
    yield .3
    with bpy.context.temp_override(window=window, area=area, region=region()):
        check('Image Editor remains scoped to the same layered image',
              paint.tex_paint_node_from_context(bpy.context) == node() and
              not paint._paint_sidebar_is_narrow(bpy.context))
    op(bpy.ops.screen.screenshot, filepath=str(args.output / 'sidebar-image-editor.png'))
    check('Image Editor drawing also preserves custom material links', material_state() == untouched)
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


bpy.app.timers.register(tick, first_interval=1)
