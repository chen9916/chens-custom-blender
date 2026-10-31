# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Exercise canvas selection, native events, painting and undo in a disposable UI process.

blender --factory-startup --enable-event-simulate --python check_workflow.py -- --output DIR
Never run this in an artist's working session.
"""

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
assert '--factory-startup' in sys.argv and '--enable-event-simulate' in sys.argv
assert not bpy.app.background and not bpy.data.filepath
args.output.mkdir(parents=True, exist_ok=True)
bpy.context.preferences.filepaths.use_auto_save_temporary_files = False
bpy.context.preferences.filepaths.temporary_directory = str(args.output)
bpy.context.preferences.edit.undo_steps = 64
bpy.context.preferences.view.show_splash = False
window = bpy.context.window
area = next(a for a in window.screen.areas if a.type == 'VIEW_3D')
area.type = 'IMAGE_EDITOR'
area.spaces.active.ui_mode = 'PAINT'
area.spaces.active.show_region_toolbar = True
area.spaces.active.show_region_tool_header = True
area.spaces.active.show_region_ui = True
area.spaces.active.show_region_asset_shelf = False
material = bpy.data.materials.new('Selection Test Material')
material.use_nodes = True
node_initial = material.node_tree.nodes.new('ShaderNodeTexPaint')
node_initial.name = 'Canvas'
bpy.context.object.data.materials.clear()
bpy.context.object.data.materials.append(material)
with bpy.context.temp_override(node=node_initial):
    bpy.ops.node.tex_paint_canvas_new(name='Selection Test', width=32, height=32,
                                    color=(0, 0, 0, 0), **{'float': True})
area.spaces.active.image = node_initial.image
report = {'build_hash': bpy.app.build_hash.decode(), 'version': bpy.app.version_string,
          'build_date': bpy.app.build_date.decode(), 'build_time': bpy.app.build_time.decode(),
          'runtime': bpy.app.binary_path,
          'checks': [], 'passed': False}


def node():
    return bpy.data.materials['Selection Test Material'].node_tree.nodes['Canvas']


def region():
    return next(r for r in area.regions if r.type == 'WINDOW')


def op(operator, *pos, **kwargs):
    with bpy.context.temp_override(window=window, area=area, region=region()):
        return operator(*pos, **kwargs)


def pixels():
    out = np.empty(32 * 32 * 4, dtype=np.float32)
    node().paint_pixels.foreach_get(out)
    return out.reshape(32, 32, 4)


def mask():
    out = np.empty(32 * 32, dtype=np.float32)
    node().selection_mask.foreach_get(out)
    return out.reshape(32, 32)


def check(name, condition):
    passed = bool(condition)
    report['checks'].append({'name': name, 'passed': passed})
    print('IMAGE_EDIT_CHECK', name, passed, flush=True)
    if not passed:
        raise AssertionError(name)


def key(code):
    x, y = screen_pixel(16, 16)
    digits = dict(ZERO='0', TWO='2', FOUR='4', SIX='6', NINE='9')
    extra = {'unicode': digits[code]} if code in digits else {}
    window.event_simulate(type=code, value='PRESS', x=x, y=y, **extra)
    window.event_simulate(type=code, value='RELEASE', x=x, y=y)


def screen_pixel(x, y):
    r = region()
    a, b = r.view2d.view_to_region(x / 32, y / 32, clip=False)
    return r.x + a, r.y + b


def mouse(x, y, event='MOUSEMOVE', value='NOTHING', **modifiers):
    x, y = screen_pixel(x, y)
    window.event_simulate(type=event, value=value, x=x, y=y, **modifiers)


def baseline():
    p = np.zeros((32, 32, 4), dtype=np.float32)
    p[8:16, 8:12] = (1, 0, 0, 1)
    p[22:25, 22:25] = (0, 1, 0, 1)
    node().paint_pixels.foreach_set(p.ravel())
    node().selection_active = False
    return p


def paint_stroke(x, y):
    a, b = region().view2d.view_to_region(x / 32, y / 32, clip=False)
    stroke = [dict(name='stroke', mouse=(a + i, b), mouse_event=(a + i, b),
                   location=(0, 0, 0), is_start=(i == 0), pressure=1.0,
                   time=1.0 + i * 0.1, size=20, x_tilt=0, y_tilt=0) for i in range(3)]
    return op(bpy.ops.paint.image_paint, 'EXEC_DEFAULT', True, stroke=stroke)


def workflow():
    global window, area
    yield 1.0
    original = baseline()
    op(bpy.ops.image.view_all, fit_view=True)
    op(bpy.ops.ed.undo_push, message='Canvas baseline')
    yield 0.5

    check('wand operator registered', bpy.ops.image.paint_select_wand.get_rna_type().identifier ==
          'IMAGE_OT_paint_select_wand')
    check('wand selects connected red patch', op(bpy.ops.image.paint_select_wand, 'EXEC_DEFAULT', True,
          location=(9, 9), tolerance=0.01) == {'FINISHED'} and mask().sum() == 32)
    selected = mask().copy()
    op(bpy.ops.ed.undo)
    check('selection undo clears restriction', not node().selection_active)
    op(bpy.ops.ed.redo)
    check('selection redo restores coverage', np.array_equal(mask(), selected))

    # Drive the actual active wand tool and the default keymap, not a Python modal substitute.
    node().selection_active = False
    op(bpy.ops.wm.tool_set_by_id, name='builtin.image_select_wand')
    mouse(9.5, 9.5)
    yield 0.2
    mouse(9.5, 9.5, 'LEFTMOUSE', 'PRESS')
    mouse(9.5, 9.5, 'LEFTMOUSE', 'RELEASE')
    yield 0.3
    check('toolbar wand click selects pixels', node().selection_active and np.array_equal(mask(), selected))

    mouse(23, 23, 'LEFTMOUSE', 'PRESS', shift=True)
    mouse(23, 23, 'LEFTMOUSE', 'RELEASE', shift=True)
    yield 0.2
    check('Shift wand adds to selection', mask().sum() == 41 and mask()[9, 9] == 1)
    mouse(23, 23, 'LEFTMOUSE', 'PRESS', ctrl=True)
    mouse(23, 23, 'LEFTMOUSE', 'RELEASE', ctrl=True)
    yield 0.2
    check('Ctrl wand subtracts from selection', np.array_equal(mask(), selected))

    key('G')
    yield 0.15
    key('X')
    key('FOUR')
    yield 0.25
    moved = pixels().copy()
    check('G X 4 previews translation', moved[9, 13, 0] > 0.99 and moved[9, 9, 3] == 0)
    key('RET')
    yield 0.3
    moved_mask = mask().copy()
    check('selection follows moved pixels', np.allclose(moved_mask, np.roll(selected, 4, axis=1)))
    op(bpy.ops.ed.undo)
    check('transform undo restores pixels and coverage', np.allclose(pixels(), original) and
          np.array_equal(mask(), selected))
    op(bpy.ops.ed.redo)
    check('transform redo restores pixels and coverage', np.allclose(pixels(), moved) and
          np.array_equal(mask(), moved_mask))

    key('G')
    yield 0.15
    key('Y')
    key('SIX')
    yield 0.2
    check('cancel test has a live preview', not np.allclose(pixels(), moved))
    key('ESC')
    yield 0.3
    check('Esc restores pixels and selection', np.allclose(pixels(), moved) and
          np.array_equal(mask(), moved_mask))

    key('R')
    yield 0.15
    key('NINE')
    key('ZERO')
    yield 0.2
    rotated = np.zeros_like(moved)
    rotated[10:14, 10:18] = (1, 0, 0, 1)
    rotated[22:25, 22:25] = (0, 1, 0, 1)
    check('R 90 rotates around selection center without stale translation',
          np.allclose(pixels(), rotated, atol=1e-5))
    key('RET')
    yield 0.3
    op(bpy.ops.ed.undo)
    check('rotation undo', np.allclose(pixels(), moved))

    key('S')
    yield 0.15
    key('TWO')
    yield 0.2
    check('S 2 previews scaling', mask().sum() > moved_mask.sum() * 3.5)
    key('ESC')
    yield 0.3
    check('scale cancel', np.allclose(pixels(), moved) and np.array_equal(mask(), moved_mask))

    op(bpy.ops.wm.tool_set_by_id, name='builtin.image_transform')
    mouse(14, 12)
    yield 0.15
    mouse(14, 12, 'LEFTMOUSE', 'PRESS')
    yield 0.15
    mouse(16, 14)
    yield 0.2
    check('transform toolbar drag previews pixels', pixels()[17, 17, 0] > 0.9 and
          pixels()[9, 13, 3] < 0.1)
    mouse(16, 14, 'LEFTMOUSE', 'RELEASE')
    yield 0.2
    op(bpy.ops.ed.undo)
    check('toolbar drag release commits one undo step', np.allclose(pixels(), moved) and
          np.array_equal(mask(), moved_mask))

    # Empty selection is a real restriction, not permission to edit everything.
    node().selection_mask.foreach_set(np.zeros(32 * 32, dtype=np.float32))
    check('empty selection prevents transform', op(bpy.ops.image.paint_transform,
          translation=(3, 0)) == {'CANCELLED'} and np.array_equal(pixels(), moved))
    from bl_ui.properties_tex_paint_layers import _get_2d_selection_mask
    with bpy.context.temp_override(window=window, area=area, region=region()):
        empty = _get_2d_selection_mask(node().image, 32, 32)
    check('Python editing bridge retains empty selection', empty is not None and not empty.any())

    # Execute the native lasso with region-space points; then exercise its gesture tool as well.
    polygon = [(6, 6), (18, 6), (18, 18), (6, 18)]
    path = []
    for x, y in polygon:
        a, b = region().view2d.view_to_region(x / 32, y / 32, clip=False)
        path.append({'name': 'point', 'loc': (a, b), 'time': 0.0})
    check('native lasso exec completes', op(bpy.ops.image.paint_select_lasso, 'EXEC_DEFAULT', True,
          path=path, mode='SET') == {'FINISHED'})
    check('lasso selects interior only', mask()[10, 10] == 1 and mask()[24, 24] == 0)
    op(bpy.ops.wm.tool_set_by_id, name='builtin.image_select_lasso')
    mouse(20, 20)
    yield 0.2
    mouse(20, 20, 'LEFTMOUSE', 'PRESS')
    yield 0.15
    for point in [(26, 20), (27, 23), (26, 27), (20, 27), (19, 23), (20, 20)]:
        mouse(*point)
        yield 0.15
    mouse(20, 20, 'LEFTMOUSE', 'RELEASE')
    yield 0.4
    check('toolbar lasso gesture selects new region', mask()[23, 23] == 1 and mask()[10, 10] == 0)

    op(bpy.ops.wm.tool_set_by_id, name='builtin.image_select_box')
    node().selection_mask.foreach_set(np.zeros(32 * 32, dtype=np.float32))
    mouse(20, 20)
    yield 0.15
    mouse(20, 20, 'LEFTMOUSE', 'PRESS')
    yield 0.15
    mouse(22, 22)
    yield 0.15
    mouse(27, 27)
    yield 0.2
    mouse(27, 27, 'LEFTMOUSE', 'RELEASE')
    yield 0.3
    check('toolbar box gesture confirms through native keymap',
          mask()[23, 23] == 1 and mask()[10, 10] == 0)

    op(bpy.ops.brush.asset_activate, asset_library_type='ESSENTIALS',
       relative_asset_identifier='brushes/essentials_brushes-mesh_texture.blend/Brush/Paint Hard')
    brush = bpy.context.tool_settings.image_paint.brush
    brush.color = (0, 0, 1)
    brush.strength = 1
    brush.size = 100
    brush.use_pressure_size = False
    brush.use_pressure_strength = False
    brush.use_unified_color = False
    brush.use_unified_size = False
    brush.use_unified_strength = False
    before_brush = pixels().copy()
    brush_selection = mask().copy()
    op(bpy.ops.ed.undo_push, message='Before selected paint')
    check('native brush stroke completes', paint_stroke(23, 23) == {'FINISHED'})
    painted = pixels().copy()
    check('selection clips native brush', not np.allclose(painted, before_brush) and
          np.array_equal(painted[brush_selection == 0], before_brush[brush_selection == 0]))
    op(bpy.ops.image.paint_transform, 'EXEC_DEFAULT', True, translation=(-2, 0))
    op(bpy.ops.ed.undo)
    check('edit undo after paint restores stroke', np.allclose(pixels(), painted))
    op(bpy.ops.ed.undo)
    check('mixed paint undo restores prior pixels', np.allclose(pixels(), before_brush))
    op(bpy.ops.ed.redo)
    check('mixed paint redo restores stroke', np.allclose(pixels(), painted))
    node().selection_mask.foreach_set(np.zeros(32 * 32, dtype=np.float32))
    paint_stroke(23, 23)
    check('empty selection blocks native brush writes', np.array_equal(pixels(), painted))

    # Layer and mask targeting share the same native buffer service as brush strokes.
    color_before = pixels().copy()
    op(bpy.ops.node.tex_paint_layer_mask_init, mode='WHITE')
    node().paint_target = 'MASK'
    mask_pixels = pixels().copy()
    check('mask target is editable', mask_pixels.shape == (32, 32, 4))
    node().selection_active = False
    check('mask transform executes', op(bpy.ops.image.paint_transform, 'EXEC_DEFAULT', True,
          translation=(2, 0), angle=0, scale=(1, 1)) == {'FINISHED'})
    check('mask transform exposes black with opaque alpha', np.allclose(pixels()[:, :2, :3], 0) and
          np.allclose(pixels()[:, 2:, :3], 1) and np.allclose(pixels()[:, :, 3], 1))
    node().paint_target = 'COLOR'
    check('mask transform leaves layer color unchanged', np.array_equal(pixels(), color_before))
    node().active_layer.lock = True
    check('locked layer blocks transform', not op(bpy.ops.image.paint_transform.poll))
    node().active_layer.lock = False

    # Exact half-selection identity regression, including HDR values through the safe RNA bridge.
    identity = pixels().copy()
    identity[9, 13] = (2.0, 0.25, 0.125, 0.75)
    node().paint_pixels.foreach_set(identity.ravel())
    check('RNA bridge preserves straight alpha and HDR', np.allclose(pixels(), identity))
    node().selection_mask.foreach_set(np.full(32 * 32, 0.5, dtype=np.float32))
    op(bpy.ops.image.paint_transform, 'EXEC_DEFAULT', True, translation=(0, 0), angle=0, scale=(1, 1))
    check('feathered identity preserves alpha and HDR', np.allclose(pixels(), identity))
    op(bpy.ops.ed.undo_push, message='Translucent HDR baseline')
    op(bpy.ops.image.paint_transform, 'EXEC_DEFAULT', True, translation=(1, 0), angle=0, scale=(1, 1))
    hdr_moved = pixels().copy()
    op(bpy.ops.ed.undo)
    np.save(args.output / 'hdr-before.npy', identity)
    np.save(args.output / 'hdr-undo.npy', pixels())
    check('undo preserves translucent HDR pixels', np.allclose(pixels(), identity, atol=1e-6))
    op(bpy.ops.ed.redo)
    check('redo preserves translucent HDR pixels', np.allclose(pixels(), hdr_moved, atol=1e-6))
    op(bpy.ops.ed.undo)

    # Persistent state survives a real write/read; compare by names after pointers are remapped.
    saved_pixels = pixels().copy()
    saved_mask = mask().copy()
    np.save(args.output / 'pixels-before-save.npy', saved_pixels)
    fixture = args.output / 'selection-roundtrip.blend'
    op(bpy.ops.wm.save_as_mainfile, filepath=str(fixture), check_existing=False)
    bpy.ops.wm.open_mainfile(filepath=str(fixture), load_ui=False)
    # File loading remaps window-manager data even when the visible screen is retained.
    window = bpy.context.window_manager.windows[0]
    area = next(a for a in window.screen.areas if a.type == 'IMAGE_EDITOR')
    np.save(args.output / 'pixels-after-load.npy', pixels())
    check('file roundtrip preserves native pixels', np.allclose(pixels(), saved_pixels, atol=1e-6))
    check('file roundtrip preserves selection', node().selection_active and np.array_equal(mask(), saved_mask))
    # Let Blender finish the queued screen/dependency refresh after reopening the file.
    yield 0.5

    area.type = 'VIEW_3D'
    area.spaces.active.region_3d.view_distance = 5
    area.spaces.active.region_3d.view_location = (0, 0, 0)
    area.spaces.active.region_3d.view_rotation = (1, 0, 0, 0)
    area.spaces.active.region_3d.view_perspective = 'ORTHO'
    bpy.context.preferences.experimental.use_3d_texture_paint = False
    op(bpy.ops.object.mode_set, mode='TEXTURE_PAINT')
    ip = bpy.context.tool_settings.image_paint
    ip.mode = 'IMAGE'
    ip.canvas = node().image
    op(bpy.ops.brush.asset_activate, asset_library_type='ESSENTIALS',
       relative_asset_identifier='brushes/essentials_brushes-mesh_texture.blend/Brush/Paint Hard')
    ip.brush.color = (0.1, 0.2, 1.0)
    ip.brush.strength = 1
    ip.brush.size = 150
    ip.brush.use_pressure_size = False
    ip.brush.use_pressure_strength = False
    ip.brush.use_alpha = True
    ip.brush.use_accumulate = False
    ip.brush.use_unified_color = False
    ip.brush.use_unified_size = False
    ip.brush.use_unified_strength = False
    yield 0.6
    before_3d = pixels().copy()
    node().selection_mask.foreach_set(np.zeros(32 * 32, dtype=np.float32))

    def paint_3d():
        r = region()
        from bpy_extras.view3d_utils import location_3d_to_region_2d
        from mathutils import Vector
        p = location_3d_to_region_2d(r, area.spaces.active.region_3d, Vector((0, 0, 1)))
        x, y = r.x + int(p.x), r.y + int(p.y)
        window.event_simulate(type='MOUSEMOVE', value='NOTHING', x=x, y=y)
        yield 0.15
        window.event_simulate(type='LEFTMOUSE', value='PRESS', x=x, y=y)
        yield 0.15
        for i in range(1, 5):
            window.event_simulate(type='MOUSEMOVE', value='NOTHING', x=x + i * 10, y=y)
            yield 0.1
        window.event_simulate(type='LEFTMOUSE', value='RELEASE', x=x + 40, y=y)
        yield 0.3

    check('3D brush context targets shared canvas', op(bpy.ops.paint.image_paint.poll) and
          ip.canvas == node().image)
    yield from paint_3d()
    check('empty canvas selection blocks 3D painting', np.array_equal(pixels(), before_3d))
    checker = (np.indices((32, 32)).sum(axis=0) % 2).astype(np.float32)
    node().selection_mask.foreach_set(checker.ravel())
    yield from paint_3d()
    painted_3d = pixels().copy()
    np.save(args.output / '3d-before.npy', before_3d)
    np.save(args.output / '3d-after.npy', painted_3d)
    check('3D painting uses the same coverage and layer pixels',
          not np.allclose(painted_3d, before_3d) and
          np.array_equal(painted_3d[checker == 0], before_3d[checker == 0]))
    op(bpy.ops.ed.undo)
    check('3D stroke undo restores shared layer', np.allclose(pixels(), before_3d))
    op(bpy.ops.ed.redo)
    check('3D stroke redo restores shared layer', np.allclose(pixels(), painted_3d))
    node().selection_mask.foreach_set((1 - checker).ravel())
    bpy.context.tool_settings.image_paint.brush.color = (1, 0.1, 0.2)
    yield from paint_3d()
    check('cached 3D stroke refreshes changed selection coverage',
          not np.allclose(pixels(), painted_3d) and
          np.array_equal(pixels()[checker == 1], painted_3d[checker == 1]))
    op(bpy.ops.object.mode_set, mode='OBJECT')
    area.type = 'IMAGE_EDITOR'
    area.spaces.active.ui_mode = 'PAINT'
    area.spaces.active.image = node().image
    node().selection_mask.foreach_set(selected.ravel())
    op(bpy.ops.wm.tool_set_by_id, name='builtin.image_select_wand')
    yield 0.5
    op(bpy.ops.image.view_all, fit_view=True)
    yield 0.2
    for menu in ('IMAGE_MT_paint_edit', 'IMAGE_MT_paint_select', 'IMAGE_MT_paint_layer',
                 'IMAGE_MT_paint_filter', 'IMAGE_MT_image', 'TEXPAINT_MT_layer_specials'):
        check(menu + ' opens', op(bpy.ops.wm.call_menu, name=menu) == {'INTERFACE'})
        yield 0.2
        key('ESC')
        yield 0.15
    orig_image = area.spaces.active.image
    flat_img = bpy.data.images.new('FlatMenuCheck', width=8, height=8, alpha=True)
    area.spaces.active.image = flat_img
    check('IMAGE_MT_paint_layer opens with Enable Layers on flat image',
          op(bpy.ops.wm.call_menu, name='IMAGE_MT_paint_layer') == {'INTERFACE'})
    yield 0.15
    key('ESC')
    yield 0.1
    check('Enable Layers action converts flat image into 2D layered canvas',
          op(bpy.ops.image.tex_paint_2d_layer_mode, action='ENABLE_LAYERS') == {'FINISHED'} and
          any(n.image == area.spaces.active.image for n in bpy.data.node_groups['.TexPaint_2D_Layers'].nodes
              if n.bl_idname == 'ShaderNodeTexPaint'))
    area.spaces.active.image = orig_image
    op(bpy.ops.screen.screenshot, filepath=str(args.output / 'native-image-editor.png'))
    report['passed'] = True


steps = workflow()


def finish():
    (args.output / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print('IMAGE_EDIT_RESULT', json.dumps(report), flush=True)
    bpy.ops.wm.quit_blender()
    return None


def tick():
    try:
        area.tag_redraw()
        return next(steps)
    except StopIteration:
        return finish()
    except Exception:
        report['error'] = traceback.format_exc()
        print(report['error'], flush=True)
        try:
            np.save(args.output / 'failure-pixels.npy', pixels())
            np.save(args.output / 'failure-selection.npy', mask())
            op(bpy.ops.screen.screenshot, filepath=str(args.output / 'failure.png'))
            report['area'] = area.type
            report['selection_sum'] = float(mask().sum())
        except Exception:
            pass
        return finish()


bpy.app.timers.register(tick, first_interval=1.0, persistent=True)
