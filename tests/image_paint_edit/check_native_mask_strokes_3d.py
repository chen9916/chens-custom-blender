# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Native per-dab 3D strokes against mask PAINT subeffects."""

import argparse
import json
import sys
import traceback
from pathlib import Path

import bpy
import numpy as np
from mathutils import Vector
from bpy_extras.view3d_utils import location_3d_to_region_2d

sys.path.insert(0, str(Path(__file__).parent))
from check_mask_paint_stack import fixture
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
    print('NATIVE_MASK_3D_CHECK', name, value, flush=True)
    assert value, name


def effect(uid):
    return next(e for e in node().active_layer.mask_effects if e.uid == uid)


def read_array(owner, property_name):
    prop = getattr(owner, property_name)
    values = np.empty(len(prop), dtype=np.float32)
    prop.foreach_get(values)
    return values


def final_mask():
    layer = node().active_layer
    values = read_array(layer, 'mask_pixels')
    # `mask_pixels` is the final evaluated mask as straight RGBA.
    return values.reshape(node().image.size[1], node().image.size[0], 4)[..., 0].copy()


def final_color():
    values = np.empty(len(node().image.pixels), dtype=np.float32)
    node().image.pixels.foreach_get(values)
    return values.reshape(node().image.size[1], node().image.size[0], 4).copy()


def target_color():
    values = np.empty(len(node().paint_pixels), dtype=np.float32)
    node().paint_pixels.foreach_get(values)
    return values.reshape(node().image.size[1], node().image.size[0], 4).copy()


def effect_pixels(uid):
    values = read_array(effect(uid), 'pixels')
    return values.reshape(node().image.size[1], node().image.size[0], 4).copy()


def point(offset):
    r = region()
    p = location_3d_to_region_2d(r, area.spaces.active.region_3d, Vector((offset, 0, 0)))
    return r.x + int(p.x), r.y + int(p.y)


def move(x, y, dx=0, ctrl=False):
    window.event_simulate(type='MOUSEMOVE', value='NOTHING', x=x + dx, y=y, ctrl=ctrl)


def stroke(offset=0.0, ctrl=False, hold=False):
    x, y = point(offset)
    if ctrl:
        window.event_simulate(type='LEFT_CTRL', value='PRESS', x=x, y=y)
        yield .05
    move(x, y, ctrl=ctrl)
    yield .2
    window.event_simulate(type='LEFTMOUSE', value='PRESS', x=x, y=y, ctrl=ctrl)
    yield .2
    # Inspect after a live dab, before release, to catch raw-buffer display and
    # delayed timer-based correction regressions.
    if hold:
        yield ('live',)
    for i in range(1, 5):
        move(x, y, i * 5, ctrl=ctrl)
        yield .06
    window.event_simulate(type='LEFTMOUSE', value='RELEASE', x=x + 20, y=y, ctrl=ctrl)
    if ctrl:
        window.event_simulate(type='LEFT_CTRL', value='RELEASE', x=x + 20, y=y)
    yield .5


def toggle(uid):
    return op(bpy.ops.node.tex_paint_mask_effect_toggle, 'EXEC_DEFAULT', True, effect_uid=uid)


def select(uid):
    return op(bpy.ops.node.tex_paint_mask_effect_select, 'EXEC_DEFAULT', True, effect_uid=uid)


def disable_legacy_timer():
    timer = getattr(paint, '_mask_paint_sync_timer', None)
    if timer and bpy.app.timers.is_registered(timer):
        bpy.app.timers.unregister(timer)
    return timer is None or not bpy.app.timers.is_registered(timer)


def workflow():
    global area, window
    yield .5
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob, do_unlink=True)
    bpy.ops.mesh.primitive_plane_add(size=2)
    _, generator_uid, first_uid = fixture(size=256)
    layer = node().active_layer
    check('native mask effect collection is published', len(layer.mask_effects) >= 2)
    check('active PAINT effect matches UI selection', layer.active_mask_effect_uid == first_uid)
    first = effect(first_uid)
    check('PAINT pixels own independent RGBA coverage', len(first.pixels) == 256 * 256 * 4)

    # The legacy idle callback used to absorb edits to the evaluated mask. Disable
    # it so the test proves the native stroke path updates the selected subeffect.
    check('legacy mask absorption timer is disabled',
          disable_legacy_timer())

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
    brush.size = 12
    brush.use_pressure_size = brush.use_pressure_strength = False
    brush.use_unified_color = brush.use_unified_size = brush.use_unified_strength = False
    # Multiply ensures paint darkens the procedural base; Ctrl must remove
    # this effect coverage and reveal the generator below it.
    st = paint._get_layer_grunge_state(node(), layer)
    metadata_first = next(e for e in st['effects'] if int(e['uid']) == first_uid)
    metadata_first['blend_mode'] = 'MULTIPLY'
    paint._save_layer_grunge_state(node(), layer, st, flush_json=True)
    first.blend_mode = 3  # TEXPAINT_MASK_EFFECT_MULTIPLY
    first.enabled = True
    layer.mask_effects_active = True
    layer.active_mask_effect_uid = first_uid
    layer.mask_stack_evaluate()
    yield .5

    base = final_mask()
    baseline_effect = effect_pixels(first_uid)
    baseline_revision = first.revision
    op(bpy.ops.ed.undo_push, message='Before native mask stroke')
    steps = stroke(-.3, hold=True)
    while True:
        item = next(steps)
        if item == ('live',):
            live_raw = effect_pixels(first_uid)
            live_final = final_mask()
            live_alpha = live_raw[..., 3] * float(first.opacity)
            live_expected = base * (1.0 - live_alpha + live_raw[..., 0] * live_alpha)
            check('live 3D dab writes directly to selected PAINT pixels',
                  np.any(live_raw[..., 3] > baseline_effect[..., 3]))
            check('live 3D dab publishes fully composited final mask',
                  not np.array_equal(live_final, base) and
                  np.allclose(live_final, live_expected, atol=1/255))
            check('live 3D dab increments native effect revision',
                  first.revision > baseline_revision)
            break
        yield item
    yield from steps
    painted = final_mask()
    painted_effect = effect_pixels(first_uid)
    paint_alpha = painted_effect[..., 3] * float(first.opacity)
    expected_painted = base * (1.0 - paint_alpha + painted_effect[..., 0] * paint_alpha)
    check('released stroke remains in native PAINT coverage',
          np.any(painted_effect[..., 3] > baseline_effect[..., 3]))
    check('no idle snapback without absorption timer', not np.array_equal(painted, base))
    check('idle final mask matches native Multiply coverage',
          np.allclose(painted, expected_painted, atol=1/255))
    check('Multiply PAINT output matches native blend formula',
          np.allclose(painted, expected_painted, atol=1/255) and np.any(painted < base - 1e-3))
    yield .5
    check('second idle interval leaves native mask composite unchanged',
          np.array_equal(final_mask(), painted) and
          np.array_equal(effect_pixels(first_uid), painted_effect))

    # Ctrl reverses the brush operation. The erase must clear/attenuate this
    # effect's alpha and reveal the unchanged generated result underneath.
    yield from stroke(-.3, ctrl=True)
    erased = final_mask()
    erased_effect = effect_pixels(first_uid)
    check('Ctrl eraser reduces selected PAINT coverage',
          np.sum(erased_effect[..., 3]) < np.sum(painted_effect[..., 3]))
    check('Ctrl erase reveals underlying generator',
          np.mean(np.abs(erased - base)) < np.mean(np.abs(painted - base)))

    # A second Paint subeffect receives later strokes without modifying the first.
    first_after_erase = effect_pixels(first_uid)
    op(bpy.ops.node.tex_paint_mask_effect_add, 'EXEC_DEFAULT', True, effect_type='PAINT')
    second_uid = node().active_layer.active_mask_effect_uid
    check('second PAINT effect is independently addressable', second_uid != first_uid)
    second_before = effect_pixels(second_uid)
    yield from stroke(.3)
    second_after = effect_pixels(second_uid)
    check('second 3D stroke changes only active PAINT effect',
          np.any(second_after[..., 3] != second_before[..., 3]) and
          np.array_equal(effect_pixels(first_uid), first_after_erase))
    second_final = final_mask()
    toggle(second_uid)
    check('hiding second Paint preserves first effect output',
          np.allclose(final_mask(), erased, atol=2/255))
    toggle(second_uid)
    check('showing second Paint restores its stroke', np.allclose(final_mask(), second_final, atol=2/255))

    # Undo/redo must restore the native target buffer and its evaluated output.
    # Undo both effect-toggle operators and then the projected brush stroke.
    op(bpy.ops.ed.undo)
    op(bpy.ops.ed.undo)
    op(bpy.ops.ed.undo)
    check('native image undo restores state before second stroke',
          np.allclose(final_mask(), erased, atol=2/255))
    op(bpy.ops.ed.redo)
    check('native image redo restores second stroke', np.allclose(final_mask(), second_final, atol=2/255))

    op(bpy.ops.screen.screenshot, filepath=str(args.output / 'native-mask-strokes-3d.png'))
    path = args.output / 'native-mask-strokes.blend'
    op(bpy.ops.wm.save_as_mainfile, filepath=str(path))
    op(bpy.ops.wm.open_mainfile, filepath=str(path))
    yield .5
    window = bpy.context.window_manager.windows[0]
    area = next(a for a in window.screen.areas if a.type == 'VIEW_3D')
    disable_legacy_timer()
    check('save and reopen retain native effect pixels',
          np.array_equal(effect_pixels(second_uid), second_after) and
          np.allclose(final_mask(), second_final, atol=2/255))
    toggle(second_uid)
    toggle(second_uid)
    check('reopened effect toggles preserve native pixels',
          np.allclose(final_mask(), second_final, atol=2/255))

    # Spatial mask effects must publish their expanded dirty region during the
    # projected stroke, without waiting for an idle evaluation to repair it.
    op(bpy.ops.node.tex_paint_mask_effect_add, 'EXEC_DEFAULT', True,
       effect_type='FILTER', filter_type='BLUR')
    blur_metadata = paint._get_active_mask_effect(node())
    blur_uid = int(blur_metadata['uid'])
    select(second_uid)
    layer = node().active_layer
    check('Blur follows Paint while metadata and native stroke target select Paint',
          effect(blur_uid).type == 3 and effect(blur_uid).filter_type == 0 and
          effect(blur_uid).enabled and list(layer.mask_effects)[-1].uid == blur_uid and
          int(paint._get_active_mask_effect(node())['uid']) == second_uid and
          layer.active_mask_effect_uid == second_uid and node().paint_target == 'MASK')
    blur_paint_before = effect_pixels(second_uid)
    steps = stroke(0.0, hold=True)
    while True:
        item = next(steps)
        if item == ('live',):
            blur_live_raw = effect_pixels(second_uid)
            blur_live_mask = final_mask()
            blur_live_color = final_color()
            layer.mask_stack_evaluate()
            check('held Blur stroke writes Paint coverage without reference mutating raw pixels',
                  np.any(blur_live_raw[..., 3] > blur_paint_before[..., 3]) and
                  np.array_equal(effect_pixels(second_uid), blur_live_raw))
            check('held Blur stroke mask and master match full native evaluation',
                  np.allclose(blur_live_mask, final_mask(), atol=1/255, rtol=0) and
                  np.allclose(blur_live_color, final_color(), atol=1/255, rtol=0))
            break
        yield item
    yield from steps
    blur_painted_raw = effect_pixels(second_uid)
    blur_painted_mask = final_mask()
    blur_painted_color = final_color()
    yield .5
    check('released Blur stroke has no idle mask or master snapback',
          np.array_equal(effect_pixels(second_uid), blur_painted_raw) and
          np.array_equal(final_mask(), blur_painted_mask) and
          np.array_equal(final_color(), blur_painted_color))
    yield from stroke(0.0, ctrl=True)
    blur_erased_raw = effect_pixels(second_uid)
    blur_erased_mask = final_mask()
    blur_erased_color = final_color()
    check('Ctrl with Blur erases selected raw Paint coverage',
          np.sum(blur_erased_raw[..., 3]) < np.sum(blur_painted_raw[..., 3]))
    layer.mask_stack_evaluate()
    check('Ctrl Blur stroke mask and master match full evaluation without changing raw Paint',
          np.array_equal(effect_pixels(second_uid), blur_erased_raw) and
          np.allclose(blur_erased_mask, final_mask(), atol=1/255, rtol=0) and
          np.allclose(blur_erased_color, final_color(), atol=1/255, rtol=0))

    # Exercise the ordinary color-layer projector too: a Multiply Paint layer
    # must reveal its lower Fill layer when Ctrl erases the paint alpha.
    lower = node().active_layer
    lower.use_mask = False
    lower.fill_color = (.55, .35, .2, 1.0)
    lower.blend_mode = 'MIX'
    op(bpy.ops.node.tex_paint_layer_add, 'EXEC_DEFAULT', True, type='PAINT')
    color_layer = node().active_layer
    color_layer.blend_mode = 'MUL'
    node().paint_target = 'COLOR'
    ip = bpy.context.tool_settings.image_paint
    ip.mode = 'IMAGE'
    ip.canvas = node().image
    brush = ip.brush
    brush.color = (0, 0, 0)
    brush.strength = 1
    brush.size = 12
    yield .5
    lower_composite = final_color()
    lower_layer_pixels = target_color()
    check('Multiply color Paint layer starts transparent over lower Fill',
          np.allclose(lower_composite, lower_composite[0, 0], atol=2/255) and
          np.all(lower_layer_pixels[..., 3] == 0))
    op(bpy.ops.ed.undo_push, message='Before Multiply color stroke')
    yield from stroke(.05)
    multiplied = final_color()
    multiplied_raw = target_color()
    check('3D Multiply color stroke changes active raw Paint layer',
          np.any(multiplied_raw[..., 3] > lower_layer_pixels[..., 3]) and
          np.any(multiplied[..., :3] < lower_composite[..., :3] - 1e-3))
    yield from stroke(.05, ctrl=True)
    erased_color = final_color()
    erased_raw = target_color()
    check('Ctrl eraser reduces color Paint alpha',
          np.sum(erased_raw[..., 3]) < np.sum(multiplied_raw[..., 3]))
    opaque_centers = multiplied_raw[..., 3] >= .99
    check('Ctrl erasing Multiply reveals lower Fill layer',
          np.any(opaque_centers) and
          np.all(erased_raw[..., 3][opaque_centers] <= 2/255) and
          np.allclose(erased_color[opaque_centers], lower_composite[opaque_centers], atol=2/255) and
          np.mean(np.abs(erased_color - lower_composite)) <
          np.mean(np.abs(multiplied - lower_composite)))
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
