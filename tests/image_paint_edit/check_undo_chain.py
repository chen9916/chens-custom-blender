# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Regression for replaying multiple paint steps across a memory-file undo boundary."""

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
bpy.context.preferences.edit.undo_steps = 64
bpy.context.preferences.view.show_splash = False
window = bpy.context.window
area = next(a for a in window.screen.areas if a.type == 'VIEW_3D')
area.type = 'IMAGE_EDITOR'
area.spaces.active.ui_mode = 'PAINT'
report = {'build_hash': bpy.app.build_hash.decode(), 'build_date': bpy.app.build_date.decode(),
          'build_time': bpy.app.build_time.decode(), 'runtime': bpy.app.binary_path,
          'checks': [], 'passed': False}


def node():
    return bpy.data.node_groups['.TexPaint_2D_Layers'].nodes['Texture Paint']


def region():
    return next(r for r in area.regions if r.type == 'WINDOW')


def op(operator, *pos, **kwargs):
    with bpy.context.temp_override(window=window, area=area, region=region()):
        return operator(*pos, **kwargs)


def pixels():
    out = np.empty(1024 * 1024 * 4, dtype=np.float32)
    node().paint_pixels.foreach_get(out)
    return out


def check(name, condition):
    passed = bool(condition)
    report['checks'].append({'name': name, 'passed': passed})
    print('UNDO_CHAIN_CHECK', name, passed, flush=True)
    assert passed, name


def stroke(x, y):
    a, b = region().view2d.view_to_region(x, y, clip=False)
    points = [dict(name='stroke', mouse=(a + i * 5, b), mouse_event=(a + i * 5, b),
                   location=(0, 0, 0), is_start=(i == 0), pressure=1,
                   time=1 + i * .1, size=30, x_tilt=0, y_tilt=0) for i in range(3)]
    return op(bpy.ops.paint.image_paint, 'EXEC_DEFAULT', True, stroke=points)


def workflow():
    yield 1.0
    op(bpy.ops.image.tex_paint_new_2d_canvas, 'EXEC_DEFAULT', True,
       preset='LOGO', resolution='1024', name='Undo Chain')
    op(bpy.ops.image.view_all, fit_view=True)
    op(bpy.ops.brush.asset_activate, asset_library_type='ESSENTIALS',
       relative_asset_identifier='brushes/essentials_brushes-mesh_texture.blend/Brush/Paint Hard')
    brush = bpy.context.tool_settings.image_paint.brush
    brush.color = (0.8, 0.2, 0.1)
    brush.strength = 1
    brush.size = 30
    brush.use_pressure_size = False
    brush.use_pressure_strength = False
    brush.use_unified_color = False
    brush.use_unified_size = False
    brush.use_unified_strength = False
    yield 0.5
    op(bpy.ops.ed.undo_push, message='Before multiple strokes')
    snapshots = [pixels()]
    for i in range(3):
        check(f'stroke {i + 1} completes', stroke(.25 + i * .2, .5) == {'FINISHED'})
        snapshots.append(pixels())
        check(f'stroke {i + 1} changes pixels', not np.array_equal(snapshots[-1], snapshots[-2]))
    # This reloads a Main before replaying the whole contiguous paint chain on undo.
    op(bpy.ops.image.paint_transform, 'EXEC_DEFAULT', True,
       translation=(8, 0), angle=0, scale=(1, 1))
    transformed = pixels()
    check('transform changes pixels', not np.array_equal(transformed, snapshots[-1]))
    print('UNDO_CHAIN_REPLAY_BEGIN', flush=True)
    op(bpy.ops.ed.undo)
    check('edit undo replays all three strokes', np.allclose(pixels(), snapshots[-1]))
    for i in range(2, -1, -1):
        op(bpy.ops.ed.undo)
        check(f'paint undo restores state {i}', np.allclose(pixels(), snapshots[i]))
    for i in range(1, 4):
        op(bpy.ops.ed.redo)
        check(f'paint redo restores state {i}', np.allclose(pixels(), snapshots[i]))
    op(bpy.ops.ed.redo)
    check('edit redo restores transform', np.allclose(pixels(), transformed))
    # A second trip must resolve pointers again after Main has been replaced.
    op(bpy.ops.ed.undo)
    check('repeated edit undo refreshes image references', np.allclose(pixels(), snapshots[-1]))
    op(bpy.ops.node.tex_paint_color_filter, 'EXEC_DEFAULT', True, action='ADD', type='INVERT')
    check('filter after paint leaves source intact', np.allclose(pixels(), snapshots[-1]) and
          len(node().active_layer.color_filters) == 1)
    op(bpy.ops.ed.undo)
    check('filter undo replays all brush steps', np.allclose(pixels(), snapshots[-1]) and
          len(node().active_layer.color_filters) == 0)
    op(bpy.ops.ed.redo)
    check('filter redo preserves brush pixels', np.allclose(pixels(), snapshots[-1]) and
          len(node().active_layer.color_filters) == 1)
    op(bpy.ops.ed.undo)
    op(bpy.ops.image.tex_paint_2d_color_adjust, 'EXEC_DEFAULT', True, brightness=.1)
    adjusted = pixels()
    check('color adjustment changes pixels', not np.array_equal(adjusted, snapshots[-1]))
    op(bpy.ops.ed.undo)
    check('color adjustment undo restores three strokes', np.allclose(pixels(), snapshots[-1]))
    op(bpy.ops.ed.redo)
    check('color adjustment redo restores result', np.allclose(pixels(), adjusted))
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
    print('UNDO_CHAIN_RESULT', json.dumps(report), flush=True)
    bpy.ops.wm.quit_blender()
    return None


bpy.app.timers.register(tick, first_interval=1.0)
