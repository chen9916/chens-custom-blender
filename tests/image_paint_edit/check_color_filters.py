# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Validate non-destructive paint-layer filters in a disposable native window."""

import argparse
import json
import sys
import traceback
from pathlib import Path

import bpy
import numpy as np


parser = argparse.ArgumentParser()
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
assert '--factory-startup' in sys.argv and not bpy.app.background and not bpy.data.filepath
args.output.mkdir(parents=True, exist_ok=True)
bpy.context.preferences.filepaths.use_auto_save_temporary_files = False
bpy.context.preferences.filepaths.temporary_directory = str(args.output)
bpy.context.preferences.view.show_splash = False
bpy.context.preferences.edit.undo_steps = 64
window = bpy.context.window
area = next(a for a in window.screen.areas if a.type == 'VIEW_3D')
area.type = 'IMAGE_EDITOR'
area.spaces.active.ui_mode = 'PAINT'
area.spaces.active.show_region_ui = True
area.spaces.active.show_region_toolbar = True
area.spaces.active.show_region_tool_header = True
report = {'build_hash': bpy.app.build_hash.decode(), 'build_date': bpy.app.build_date.decode(),
          'build_time': bpy.app.build_time.decode(), 'runtime': bpy.app.binary_path,
          'checks': [], 'passed': False}


def node():
    return bpy.data.node_groups['.TexPaint_2D_Layers'].nodes['Texture Paint']


def layer():
    return node().active_layer


def region():
    return next(r for r in area.regions if r.type == 'WINDOW')


def op(operator, *pos, **kwargs):
    with bpy.context.temp_override(window=window, area=area, region=region()):
        return operator(*pos, **kwargs)


def pixels(composite=False):
    out = np.empty(512 * 512 * 4, dtype=np.float32)
    (node().image.pixels if composite else node().paint_pixels).foreach_get(out)
    return out.reshape(512, 512, 4)


def check(name, condition):
    passed = bool(condition)
    report['checks'].append({'name': name, 'passed': passed})
    print('COLOR_FILTER_CHECK', name, passed, flush=True)
    assert passed, name


def edit(action='ADD', kind='HSV'):
    return op(bpy.ops.node.tex_paint_color_filter, 'EXEC_DEFAULT', True, action=action, type=kind)


def workflow():
    global window, area
    yield 1.0
    op(bpy.ops.image.tex_paint_new_2d_canvas, 'EXEC_DEFAULT', True,
       preset='LOGO', resolution='512', name='Filter Test')
    source = np.empty((512, 512, 4), dtype=np.float32)
    source[:] = (.2, .4, .8, .6)
    node().paint_pixels.foreach_set(source.ravel())
    original = pixels()
    op(bpy.ops.ed.undo_push, message='Unfiltered layer')
    check('add native color filter', edit(kind='INVERT') == {'FINISHED'})
    check('filter changes composite RGB', np.allclose(pixels(True)[..., :3], 1 - original[..., :3], atol=.005))
    check('filter preserves painted pixels', np.array_equal(pixels(), original))
    check('filter preserves alpha', np.allclose(pixels(True)[..., 3], original[..., 3], atol=.005))
    op(bpy.ops.ed.undo)
    check('undo removes filter without changing pixels', len(layer().color_filters) == 0 and
          np.array_equal(pixels(), original))
    op(bpy.ops.ed.redo)
    check('redo restores filter', len(layer().color_filters) == 1 and
          np.allclose(pixels(True)[..., :3], 1 - original[..., :3], atol=.005))
    layer().color_filters[0].enabled = False
    check('bypass restores source appearance', np.allclose(pixels(True), original, atol=.005))
    layer().color_filters[0].enabled = True
    layer().color_filters[0].strength = .5
    check('filter strength mixes input and output', np.allclose(pixels(True)[..., :3], .5, atol=.005))
    layer().color_filters[0].strength = 1
    edit(kind='BRIGHT_CONTRAST')
    layer().color_filters[1].bright_contrast = (.1, 0)
    after = pixels(True)
    check('second filter receives first filter output', np.allclose(after[..., :3], 1.1 - original[..., :3], atol=.005))
    # Python property assignment does not push the undo step that an interactive field edit does.
    op(bpy.ops.ed.undo_push, message='Filter settings')
    edit('UP')
    check('moving filter changes evaluation order', np.allclose(pixels(True)[..., :3], .9 - original[..., :3], atol=.005))
    op(bpy.ops.ed.undo)
    check('undo restores filter order and settings', layer().color_filters[0].type == 'INVERT' and
          np.allclose(pixels(True), after, atol=.005))
    op(bpy.ops.ed.redo)
    check('redo restores moved filter', layer().color_filters[0].type == 'BRIGHT_CONTRAST')
    edit('DUPLICATE')
    check('duplicate filter is independent', len(layer().color_filters) == 3)
    layer().color_filters[1].bright_contrast = (.25, 0)
    check('duplicate settings do not change original', abs(layer().color_filters[0].bright_contrast[0] - .1) < 1e-6)
    edit('REMOVE')
    check('remove filter preserves source', len(layer().color_filters) == 2 and np.array_equal(pixels(), original))
    while layer().color_filters:
        edit('REMOVE')
    edit(kind='HSV')
    layer().color_filters[0].hsv = (.1, -.2, .05)
    check('HSV filter changes appearance only', not np.allclose(pixels(True), original) and np.array_equal(pixels(), original))
    edit('REMOVE')
    edit(kind='LEVELS')
    check('neutral levels preserve appearance', np.allclose(pixels(True), original, atol=.005))
    layer().color_filters[0].gamma = 2
    check('gamma adjusts RGB', np.allclose(pixels(True)[..., :3], np.sqrt(original[..., :3]), atol=.005))
    edit(kind='GRAYSCALE')
    gray = pixels(True)
    check('grayscale channels match', np.allclose(gray[..., 0], gray[..., 1]) and np.allclose(gray[..., 1], gray[..., 2]))
    check('all adjustments retain original paint', np.array_equal(pixels(), original))

    op(bpy.ops.image.view_all, fit_view=True)
    op(bpy.ops.brush.asset_activate, asset_library_type='ESSENTIALS',
       relative_asset_identifier='brushes/essentials_brushes-mesh_texture.blend/Brush/Paint Hard')
    brush = bpy.context.tool_settings.image_paint.brush
    brush.color = (.9, .1, .2)
    brush.strength = 1
    brush.size = 30
    brush.use_pressure_size = False
    brush.use_pressure_strength = False
    brush.use_unified_color = False
    brush.use_unified_size = False
    brush.use_unified_strength = False
    yield .3
    op(bpy.ops.ed.undo_push, message='Filtered paint baseline')
    a, b = region().view2d.view_to_region(.5, .5, clip=False)
    stroke = [dict(name='stroke', mouse=(a + i * 5, b), mouse_event=(a + i * 5, b),
                   location=(0, 0, 0), is_start=(i == 0), pressure=1,
                   time=1 + i * .1, size=30, x_tilt=0, y_tilt=0) for i in range(3)]
    check('paint through enabled filters completes',
          op(bpy.ops.paint.image_paint, 'EXEC_DEFAULT', True, stroke=stroke) == {'FINISHED'})
    painted = pixels()
    check('painting updates original color buffer', not np.array_equal(painted, original))
    visible = pixels(True)
    check('new paint is displayed through filters', np.allclose(visible[..., 0], visible[..., 1]) and
          np.allclose(visible[..., 1], visible[..., 2]) and not np.allclose(visible, painted))
    op(bpy.ops.ed.undo)
    check('filtered brush undo restores pixels and filters', np.array_equal(pixels(), original) and
          len(layer().color_filters) == 2)
    op(bpy.ops.ed.redo)
    check('filtered brush redo restores pixels and appearance', np.array_equal(pixels(), painted) and
          np.allclose(pixels(True), visible))
    op(bpy.ops.ed.undo)

    op(bpy.ops.node.tex_paint_layer_mask_init, 'EXEC_DEFAULT', True, mode='WHITE')
    node().paint_target = 'MASK'
    mask = np.empty_like(original)
    mask[:] = (.5, .5, .5, 1)
    node().paint_pixels.foreach_set(mask.ravel())
    mask_before = pixels()
    node().paint_target = 'COLOR'
    layer().color_filters[0].gamma = 1.5
    check('layer mask still controls filtered alpha', np.allclose(pixels(True)[..., 3], original[..., 3] * .5, atol=.005))
    node().paint_target = 'MASK'
    check('color filters leave mask pixels intact', np.array_equal(pixels(), mask_before))
    check('mask target cannot add color filters', not op(bpy.ops.node.tex_paint_color_filter.poll))
    node().paint_target = 'COLOR'
    layer().lock = True
    check('locked layer blocks filter stack operators', not op(bpy.ops.node.tex_paint_color_filter.poll))
    layer().lock = False

    saved = pixels(True)
    op(bpy.ops.wm.save_as_mainfile, filepath=str(args.output / 'color-filters.blend'), check_existing=False)
    bpy.ops.wm.open_mainfile(filepath=str(args.output / 'color-filters.blend'), load_ui=False)
    window = bpy.context.window_manager.windows[0]
    area = next(a for a in window.screen.areas if a.type == 'IMAGE_EDITOR')
    yield .5
    check('file roundtrip preserves filter stack', [f.type for f in layer().color_filters] == ['LEVELS', 'GRAYSCALE'] and
          layer().color_filters[0].gamma == 1.5)
    check('file roundtrip preserves filtered result', np.allclose(pixels(True), saved, atol=.005))
    check('file roundtrip preserves original pixels', np.array_equal(pixels(), original))
    op(bpy.ops.node.tex_paint_layer_duplicate, 'EXEC_DEFAULT', True)
    check('layer duplication preserves filters', len(node().layers) == 2 and len(layer().color_filters) == 2)
    layer().color_filters[0].gamma = 3
    check('duplicated layer filter settings are independent', node().layers[0].color_filters[0].gamma == 1.5)
    op(bpy.ops.node.tex_paint_layer_remove, 'EXEC_DEFAULT', True)
    # Leave the native sidebar open with an editable Levels filter for visual inspection.
    layer().active_color_filter_index = 0
    for r in area.regions:
        if r.type == 'UI':
            r.active_panel_category = 'Layers'
    op(bpy.ops.image.view_all, fit_view=True)
    yield .5
    check('filter menu opens', op(bpy.ops.wm.call_menu, name='TEXPAINT_MT_add_color_filter') == {'INTERFACE'})
    yield .3
    x, y = region().x + 50, region().y + 50
    window.event_simulate(type='ESC', value='PRESS', x=x, y=y)
    window.event_simulate(type='ESC', value='RELEASE', x=x, y=y)
    yield .4
    op(bpy.ops.screen.screenshot, filepath=str(args.output / 'color-filters.png'))
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
    print('COLOR_FILTER_RESULT', json.dumps(report), flush=True)
    bpy.ops.wm.quit_blender()
    return None


bpy.app.timers.register(tick, first_interval=1.0, persistent=True)
