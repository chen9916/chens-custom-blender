# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Real 3D brush strokes on a fill layer's procedural + paint mask stack."""

import argparse
import json
import sys
import traceback
from pathlib import Path

import bpy
import numpy as np
from mathutils import Vector
from bpy_extras.view3d_utils import location_3d_to_region_2d
from bl_ui import properties_tex_paint_layers as paint

sys.path.insert(0, str(Path(__file__).parent))
from check_mask_paint_stack import fixture, pixels

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


def node():
    return bpy.data.materials['Paint Mask Test'].node_tree.nodes.get('Texture Paint')


def region():
    return next(r for r in area.regions if r.type == 'WINDOW')


def op(operator, *pos, **kwargs):
    with bpy.context.temp_override(window=window, area=area, region=region()):
        return operator(*pos, **kwargs)


def check(name, value):
    value = bool(value)
    report['checks'].append({'name': name, 'passed': value})
    print('MASK_3D_CHECK', name, value, flush=True)
    assert value, name


def effect(uid):
    return next(e for e in node().active_layer.mask_effects if e.uid == uid)


def metadata(uid):
    return next(e for e in paint._get_layer_grunge_state(node())['effects'] if e['uid'] == uid)


def effect_pixels(uid):
    p = effect(uid).pixels
    out = np.empty(len(p), dtype=np.float32)
    p.foreach_get(out)
    return out.reshape(node().image.size[1], node().image.size[0], 4).copy()


def native_authoring_omits_pixels(uid):
    """Settings JSON contains no second copy of native-owned Paint coverage."""
    paint._absorb_pending_brush_strokes(node(), node().active_layer)
    state = json.loads(node().get('_grunge_layers_json', '{}'))[str(node().active_layer.uid)]
    saved = next(e for e in state['effects'] if e['uid'] == uid)
    return not {'paint_z64', 'paint_w', 'paint_h'}.intersection(saved)


def stroke(offset=0):
    r = region()
    point = location_3d_to_region_2d(r, area.spaces.active.region_3d, Vector((offset, 0, 0)))
    x, y = r.x + int(point.x), r.y + int(point.y)
    window.event_simulate(type='MOUSEMOVE', value='NOTHING', x=x, y=y)
    yield .15
    window.event_simulate(type='LEFTMOUSE', value='PRESS', x=x, y=y)
    yield .1
    for i in range(1, 5):
        window.event_simulate(type='MOUSEMOVE', value='NOTHING', x=x + i * 5, y=y)
        yield .06
    window.event_simulate(type='LEFTMOUSE', value='RELEASE', x=x + 20, y=y)
    yield .5


def toggle(uid):
    return op(bpy.ops.node.tex_paint_mask_effect_toggle, 'EXEC_DEFAULT', True, effect_uid=uid)


def workflow():
    global area, window
    yield .5
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob, do_unlink=True)
    bpy.ops.mesh.primitive_plane_add(size=2)
    _, generator, painted = fixture(size=1024)
    if hasattr(paint, '_mask_paint_sync_timer') and bpy.app.timers.is_registered(paint._mask_paint_sync_timer):
        bpy.app.timers.unregister(paint._mask_paint_sync_timer)
    area.spaces.active.region_3d.view_distance = 4
    area.spaces.active.region_3d.view_location = (0, 0, 0)
    area.spaces.active.region_3d.view_rotation = (1, 0, 0, 0)
    area.spaces.active.region_3d.view_perspective = 'ORTHO'
    area.spaces.active.show_region_ui = True
    area.spaces.active.shading.type = 'SOLID'
    area.spaces.active.shading.color_type = 'TEXTURE'
    bpy.context.preferences.experimental.use_3d_texture_paint = False
    op(bpy.ops.object.mode_set, mode='TEXTURE_PAINT')
    ip = bpy.context.tool_settings.image_paint
    ip.mode = 'IMAGE'
    ip.canvas = node().image
    op(bpy.ops.brush.asset_activate, asset_library_type='ESSENTIALS',
       relative_asset_identifier='brushes/essentials_brushes-mesh_texture.blend/Brush/Paint Hard')
    brush = ip.brush
    brush.color = (0, 0, 0)
    brush.strength = 1
    brush.size = 10
    brush.use_pressure_size = brush.use_pressure_strength = False
    brush.use_unified_color = brush.use_unified_size = brush.use_unified_strength = False
    yield .5
    for r in area.regions:
        if r.type == 'UI':
            r.active_panel_category = 'Tool'
    before = pixels(node())
    before_native = effect_pixels(painted)
    op(bpy.ops.ed.undo_push, message='Before mask stroke')
    yield from stroke(-.3)
    after = pixels(node())
    after_native = effect_pixels(painted)
    check('3D brush changes fill-layer mask', not np.array_equal(before, after))
    check('3D brush writes directly to selected native Paint coverage',
          np.any(after_native[..., 3] != before_native[..., 3]))
    check('native Paint coverage is not duplicated in authoring JSON', native_authoring_omits_pixels(painted))
    check('sparse stroke avoids old sampling grid', np.array_equal(before.reshape(-1, 4)[::1024], after.reshape(-1, 4)[::1024]))
    saved_paint = effect_pixels(painted)
    toggle(painted)
    check('hiding Paint reveals unchanged generated mask', np.allclose(pixels(node()), before, atol=1/255))
    toggle(painted)
    check('showing Paint restores actual 3D stroke', np.allclose(pixels(node()), after, atol=1/255))
    toggle(generator)
    toggle(generator)
    check('generator toggles retain separate native painted coverage',
          np.array_equal(effect_pixels(painted), saved_paint) and
          np.allclose(pixels(node()), after, atol=1/255))
    # Undo the four toggles, then undo and redo the native brush stroke.
    for _ in range(4):
        op(bpy.ops.ed.undo)
    op(bpy.ops.ed.undo)
    check('native stroke undo restores generated mask', np.allclose(pixels(node()), before, atol=1/255))
    op(bpy.ops.ed.redo)
    check('native stroke redo restores painted mask', np.allclose(pixels(node()), after, atol=1/255))
    toggle(painted)
    toggle(painted)
    check('stroke redo remains stable after toggling', np.allclose(pixels(node()), after, atol=2/255))
    node().active_layer.hide = True
    node().active_layer.hide = False
    check('fill-layer visibility retains mask pixels', np.allclose(pixels(node()), after, atol=2/255))
    op(bpy.ops.node.tex_paint_mask_effect_add, 'EXEC_DEFAULT', True, effect_type='PAINT')
    second = paint._get_active_mask_effect(node())['uid']
    first_data = effect_pixels(painted)
    ip = bpy.context.tool_settings.image_paint
    ip.mode = 'MATERIAL'
    ip.brush.color = (0, 0, 0)
    yield .3
    yield from stroke(.3)
    second_pixels = pixels(node())
    second_after = effect_pixels(second)
    check('material-mode stroke goes to second native Paint effect',
          np.any(second_after[..., 3] > 0) and
          np.array_equal(effect_pixels(painted), first_data) and
          not np.array_equal(second_pixels, after))
    toggle(second)
    check('hiding second Paint retains first stroke', np.allclose(pixels(node()), after, atol=2/255))
    toggle(second)
    check('second Paint has independent native coverage without duplicate JSON pixels',
          native_authoring_omits_pixels(second))
    op(bpy.ops.screen.screenshot, filepath=str(args.output / 'paint-mask-3d.png'))
    path = args.output / 'paint-mask.blend'
    op(bpy.ops.wm.save_as_mainfile, filepath=str(path))
    op(bpy.ops.wm.open_mainfile, filepath=str(path))
    yield .5
    window = bpy.context.window_manager.windows[0]
    area = next(a for a in window.screen.areas if a.type == 'VIEW_3D')
    if hasattr(paint, '_mask_paint_sync_timer') and bpy.app.timers.is_registered(paint._mask_paint_sync_timer):
        bpy.app.timers.unregister(paint._mask_paint_sync_timer)
    check('save and reopen retain painted mask', np.allclose(pixels(node()), second_pixels, atol=2/255))
    check('save and reopen retain native Paint coverage without duplicate JSON pixels',
          np.array_equal(effect_pixels(second), second_after) and native_authoring_omits_pixels(second))
    toggle(second)
    toggle(second)
    check('reopened paint effects survive toggles', np.allclose(pixels(node()), second_pixels, atol=2/255))
    yield from stroke(.05)
    reopened_stroke = pixels(node())
    reopened_native = effect_pixels(second)
    check('first stroke after reopen changes mask', not np.array_equal(reopened_stroke, second_pixels))
    check('first stroke after reopen updates native coverage',
          not np.array_equal(reopened_native, second_after))
    toggle(second)
    toggle(second)
    check('first stroke after reopen survives toggles',
          np.allclose(pixels(node()), reopened_stroke, atol=2/255) and
          np.array_equal(effect_pixels(second), reopened_native))
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
