# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Exercise the native mesh-map preview window and baker UI in an isolated session."""

import argparse
import json
import sys
import traceback
from pathlib import Path

import bpy
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


def check(name, value):
    value = bool(value)
    report['checks'].append({'name': name, 'passed': value})
    print('MESH_MAP_PREVIEW_CHECK', name, value, flush=True)
    assert value, name


def op(operator, **kwargs):
    region = next(r for r in area.regions if r.type == 'WINDOW')
    with bpy.context.temp_override(window=window, area=area, region=region):
        return operator(**kwargs)


def workflow():
    yield 1.0
    material = bpy.data.materials.new('Preview Test')
    material.use_nodes = True
    bpy.context.object.data.materials.clear()
    bpy.context.object.data.materials.append(material)
    node = material.node_tree.nodes.new('ShaderNodeTexPaint')
    material.node_tree.nodes.active = node
    with bpy.context.temp_override(node=node):
        bpy.ops.node.tex_paint_canvas_new(name='Preview Canvas', width=64, height=64, color=(1, 1, 1, 1))
    node.tex_paint_bake_samples = 16
    node.tex_paint_bake_device = 'CPU'
    node.tex_paint_bake_apply_normal = False
    check('Cycles bake operator works from a native 3D editor',
          op(bpy.ops.node.tex_paint_bake_mesh_maps, map_type='ALL') == {'FINISHED'})
    maps = paint._get_mesh_maps_state(node)['maps']
    check('sample and margin controls retain settings', node.tex_paint_bake_samples == 16 and
          node.tex_paint_bake_margin == 16)
    area.spaces.active.show_region_ui = True
    op(bpy.ops.object.mode_set, mode='TEXTURE_PAINT')
    yield .5
    for region in area.regions:
        if region.type == 'UI':
            region.active_panel_category = 'Tool'
    bpy.context.window_manager.tex_paint_show_mesh_maps_hub = True
    yield .5
    op(bpy.ops.screen.screenshot, filepath=str(args.output / 'baker-controls.png'))
    check('preview menu opens', op(bpy.ops.wm.call_menu, name='TEXPAINT_MT_preview_mesh_maps') == {'INTERFACE'})
    yield .3
    window.event_simulate(type='ESC', value='PRESS', x=area.x + 40, y=area.y + 40)
    window.event_simulate(type='ESC', value='RELEASE', x=area.x + 40, y=area.y + 40)
    yield .3
    initial_windows = len(bpy.context.window_manager.windows)
    check('preview opens without an existing Image Editor', op(
        bpy.ops.node.tex_paint_mesh_map_preview, slot='ao') == {'FINISHED'})
    yield .5
    preview = next(w for w in bpy.context.window_manager.windows if w != window)
    preview_area = next(a for a in preview.screen.areas if a.type == 'IMAGE_EDITOR')
    check('preview displays the requested map in view mode', preview_area.spaces.active.image.name == maps['ao']
          and preview_area.spaces.active.mode == 'VIEW')
    check('preview preserves the 3D painting editor', area.type == 'VIEW_3D' and bpy.context.object.mode == 'TEXTURE_PAINT')
    for slot in maps:
        check('preview switches to ' + slot, op(
            bpy.ops.node.tex_paint_mesh_map_preview, slot=slot) == {'FINISHED'}
            and preview_area.spaces.active.image.name == maps[slot])
    check('map changes reuse one preview window', len(bpy.context.window_manager.windows) == initial_windows + 1)
    with bpy.context.temp_override(window=preview, area=preview_area):
        bpy.ops.screen.screenshot(filepath=str(args.output / 'map-preview.png'))
        bpy.ops.wm.window_close()
    yield .5
    check('closing preview retains painting session', len(bpy.context.window_manager.windows) == initial_windows
          and bpy.context.object.mode == 'TEXTURE_PAINT')
    check('preview reopens after closing', op(
        bpy.ops.node.tex_paint_mesh_map_preview, slot='ao') == {'FINISHED'})
    yield .5
    report['passed'] = True


steps = workflow()


def tick():
    try:
        area.tag_redraw()
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

