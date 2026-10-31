# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Ordered non-destructive blur/adjustments and 3D brush dirty halos."""

import argparse
import json
import sys
import traceback
from pathlib import Path

import bpy
import numpy as np
from bpy_extras.view3d_utils import location_3d_to_region_2d
from mathutils import Vector

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
size = 256


def node():
    return bpy.data.materials['Layer Effects Test'].node_tree.nodes['Texture Paint']


def layer():
    return node().active_layer


def region():
    return next(r for r in area.regions if r.type == 'WINDOW')


def op(operator, *pos, **kwargs):
    with bpy.context.temp_override(window=window, area=area, region=region()):
        return operator(*pos, **kwargs)


def pixels(composite=False):
    out = np.empty(size * size * 4, dtype=np.float32)
    (node().image.pixels if composite else node().paint_pixels).foreach_get(out)
    return out.reshape(size, size, 4)


def check(name, condition):
    result = bool(condition)
    report['checks'].append({'name': name, 'passed': result})
    print('LAYER_EFFECTS_CHECK', name, result, flush=True)
    assert result, name


def edit(action='ADD', kind='BLUR'):
    return op(bpy.ops.node.tex_paint_color_filter, 'EXEC_DEFAULT', True, action=action, type=kind)


def workflow():
    global window, area
    yield .5
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob, do_unlink=True)
    bpy.ops.mesh.primitive_plane_add(size=2)
    material = bpy.data.materials.new('Layer Effects Test')
    material.use_nodes = True
    bpy.context.object.data.materials.append(material)
    paint_node = material.node_tree.nodes.new('ShaderNodeTexPaint')
    material.node_tree.nodes.active = paint_node
    with bpy.context.temp_override(node=paint_node):
        bpy.ops.node.tex_paint_canvas_new(name='Layer Effects Canvas', width=size, height=size,
                                       color=(0, 0, 0, 0))
    source = np.zeros((size, size, 4), dtype=np.float32)
    source[80:176, 80:128] = (.8, .2, .1, 1)
    source[80:176, 128:176] = (.2, .8, .4, .5)
    node().paint_pixels.foreach_set(source.ravel())
    source = pixels()
    op(bpy.ops.ed.undo_push, message='Before layer blur')
    check('3D viewport can add native Blur effect', edit() == {'FINISHED'})
    layer().color_filters[0].blur_radius = 4
    blurred = pixels(True)
    check('Blur extends painted coverage outside the raw edge', blurred[100, 78, 3] > 0 and source[100, 78, 3] == 0)
    check('Blur retains color across a transparent edge', np.allclose(blurred[100, 78, :3], source[100, 80, :3], atol=2/255))
    check('Blur preserves raw painted pixels', np.array_equal(pixels(), source))
    layer().color_filters[0].enabled = False
    check('effect visibility restores original appearance', np.allclose(pixels(True), source, atol=1/255))
    layer().color_filters[0].enabled = True
    check('effect visibility restores Blur', np.array_equal(pixels(True), blurred))
    edit(kind='LEVELS')
    layer().color_filters[1].gamma = .5
    after_levels = pixels(True)
    op(bpy.ops.ed.undo_push, message='Layer effect settings')
    edit('UP')
    before_blur_levels = pixels(True)
    check('moving Levels before Blur changes evaluation order', not np.allclose(after_levels, before_blur_levels, atol=.002))
    op(bpy.ops.ed.undo)
    check('effect reorder undo restores parameters and output', layer().color_filters[0].type == 'BLUR' and
          np.array_equal(pixels(True), after_levels))
    op(bpy.ops.ed.redo)
    check('effect reorder redo restores output', layer().color_filters[0].type == 'LEVELS' and
          np.array_equal(pixels(True), before_blur_levels))
    layer().active_color_filter_index = 1
    edit('DUPLICATE')
    layer().color_filters[2].blur_radius = 7
    check('duplicated effects own independent parameters', layer().color_filters[1].blur_radius == 4)
    edit('REMOVE')
    check('removing an effect retains raw paint', np.array_equal(pixels(), source))
    area.spaces.active.region_3d.view_distance = 4
    area.spaces.active.region_3d.view_location = (0, 0, 0)
    area.spaces.active.region_3d.view_rotation = (1, 0, 0, 0)
    area.spaces.active.region_3d.view_perspective = 'ORTHO'
    area.spaces.active.show_region_ui = True
    area.spaces.active.shading.color_type = 'TEXTURE'
    bpy.context.preferences.experimental.use_3d_texture_paint = False
    op(bpy.ops.object.mode_set, mode='TEXTURE_PAINT')
    ip = bpy.context.tool_settings.image_paint
    ip.mode = 'IMAGE'
    ip.canvas = node().image
    op(bpy.ops.brush.asset_activate, asset_library_type='ESSENTIALS',
       relative_asset_identifier='brushes/essentials_brushes-mesh_texture.blend/Brush/Paint Hard')
    brush = ip.brush
    brush.color = (.1, .2, .9)
    brush.strength = 1
    brush.size = 12
    brush.use_pressure_size = brush.use_pressure_strength = False
    brush.use_unified_color = brush.use_unified_size = brush.use_unified_strength = False
    yield .5
    for r in area.regions:
        if r.type == 'UI':
            r.active_panel_category = 'Tool'
    op(bpy.ops.ed.undo_push, message='Before filtered 3D stroke')
    r = region()
    p = location_3d_to_region_2d(r, area.spaces.active.region_3d, Vector((-.65, 0, 0)))
    x, y = r.x + int(p.x), r.y + int(p.y)
    window.event_simulate(type='MOUSEMOVE', value='NOTHING', x=x, y=y)
    yield .1
    window.event_simulate(type='LEFTMOUSE', value='PRESS', x=x, y=y)
    yield .1
    window.event_simulate(type='MOUSEMOVE', value='NOTHING', x=x + 10, y=y)
    yield .1
    window.event_simulate(type='LEFTMOUSE', value='RELEASE', x=x + 10, y=y)
    yield .2
    painted = pixels()
    displayed = pixels(True)
    check('3D brush edits original pixels with ordered effects enabled', not np.array_equal(painted, source))
    changed_raw = np.any(painted != source, axis=2)
    old_display = before_blur_levels
    changed_display = np.any(abs(displayed - old_display) > 2/255, axis=2)
    check('live 3D update includes blur halo outside brush pixels', np.any(changed_display & ~changed_raw))
    # A neutral point-filter update forces a whole-image composite for comparison.
    layer().color_filters[0].strength = layer().color_filters[0].strength
    full_display = pixels(True)
    delta = np.abs(full_display - displayed)
    ys, xs = np.where(np.any(delta > 2/255, axis=2))
    report['partial_full_comparison'] = {
        'max_channel_error': delta.max(axis=(0, 1)).tolist(),
        'changed_pixels': len(xs),
        'bounds': [int(xs.min()), int(xs.max()), int(ys.min()), int(ys.max())] if len(xs) else None,
        'raw_stroke_bounds': [int(np.where(changed_raw)[1].min()), int(np.where(changed_raw)[1].max()),
                              int(np.where(changed_raw)[0].min()), int(np.where(changed_raw)[0].max())],
    }
    np.save(args.output / 'partial-display.npy', displayed)
    np.save(args.output / 'full-display.npy', full_display)
    np.save(args.output / 'raw-painted.npy', painted)
    check('partial brush update matches a forced full composite', np.allclose(full_display, displayed, atol=2/255))
    op(bpy.ops.ed.undo)
    check('filtered 3D stroke undo restores source', np.array_equal(pixels(), source))
    op(bpy.ops.ed.redo)
    check('filtered 3D stroke redo restores result', np.array_equal(pixels(), painted) and
          np.allclose(pixels(True), displayed, atol=2/255))
    yield .3
    op(bpy.ops.screen.screenshot, filepath=str(args.output / 'layer-effects-3d.png'))
    path = args.output / 'layer-effects.blend'
    op(bpy.ops.wm.save_as_mainfile, filepath=str(path))
    op(bpy.ops.wm.open_mainfile, filepath=str(path))
    yield .5
    window = bpy.context.window_manager.windows[0]
    area = next(a for a in window.screen.areas if a.type == 'VIEW_3D')
    check('file roundtrip retains ordered layer effects', [f.type for f in layer().color_filters] == ['LEVELS', 'BLUR'] and
          layer().color_filters[1].blur_radius == 4)
    check('file roundtrip retains raw paint and filtered output', np.array_equal(pixels(), painted) and
          np.allclose(pixels(True), displayed, atol=2/255))
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
