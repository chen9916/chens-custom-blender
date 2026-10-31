# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Actual viewport strokes, Ctrl erase and toggles across procedural mask blends."""
import argparse
import hashlib
import json
import sys
import traceback
from pathlib import Path

import bpy
import numpy as np
from mathutils import Vector
from bpy_extras.view3d_utils import location_3d_to_region_2d
from bl_ui import properties_tex_paint_layers as paint

parser = argparse.ArgumentParser()
parser.add_argument('--output', type=Path, required=True)
parser.add_argument('--group', choices=('masks', 'brushes', 'layers', 'all'), default='all')
args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
assert '--factory-startup' in sys.argv and not bpy.app.background and not bpy.data.filepath
args.output.mkdir(parents=True, exist_ok=True)
bpy.context.preferences.filepaths.use_auto_save_temporary_files = False
bpy.context.preferences.filepaths.temporary_directory = str(args.output)
bpy.context.preferences.view.show_splash = False
window = bpy.context.window
area = next(a for a in window.screen.areas if a.type == 'VIEW_3D')
SIZE = 128
BLENDS = ('REPLACE', 'ADD', 'SUBTRACT', 'MULTIPLY', 'SCREEN', 'OVERLAY', 'MAX', 'MIN')
GENERATORS = ('AO', 'CURVATURE', 'CAVITY', 'EDGE_WEAR', 'NORMAL_UP', 'POSITION_Z', 'GRUNGE')
report = {'build_hash': bpy.app.build_hash.decode(), 'build_time': bpy.app.build_time.decode(),
          'module_sha256': hashlib.sha256(Path(paint.__file__).read_bytes()).hexdigest(),
          'checks': [], 'cases': [], 'passed': False}


def node():
    return bpy.data.materials['Mask Viewport Matrix'].node_tree.nodes['Texture Paint']


def op(fn, *pos, **kwargs):
    region = next(r for r in area.regions if r.type == 'WINDOW')
    with bpy.context.temp_override(window=window, area=area, region=region, node=node()):
        return fn(*pos, **kwargs)


def check(name, value):
    value = bool(value)
    report['checks'].append({'name': name, 'passed': value})
    print('MASK_BLEND_VIEWPORT', name, value, flush=True)
    assert value, name


def rgba(owner, name):
    prop = getattr(owner, name)
    result = np.empty(len(prop), dtype=np.float32)
    prop.foreach_get(result)
    return result.reshape(-1, 4)


def effect(uid):
    return next(e for e in node().active_layer.mask_effects if e.uid == uid)


def mask():
    return rgba(node().active_layer, 'mask_pixels')[:, 0]


def color():
    return rgba(node().image, 'pixels')


def write_metadata(uid, **values):
    state = paint._get_layer_grunge_state(node())
    next(e for e in state['effects'] if e['uid'] == uid).update(values)
    paint._save_layer_grunge_state(node(), node().active_layer, state)
    paint._evaluate_layer_grunge_mask(node(), node().active_layer, context=bpy.context)


def toggle(uid):
    return op(bpy.ops.node.tex_paint_mask_effect_toggle, 'EXEC_DEFAULT', True, effect_uid=uid)


def painted_expectation(base, source, blend, opacity, invert):
    # Paint modes use source coverage; ADD/SUB attenuate before clamping.
    value = 1.0 - source[:, 0] if invert else source[:, 0]
    alpha = source[:, 3] * opacity
    if blend == 'ADD':
        return np.minimum(base + value * alpha, 1)
    if blend == 'SUBTRACT':
        return np.maximum(base - value * alpha, 0)
    if blend == 'MULTIPLY':
        blended = base * value
    elif blend == 'SCREEN':
        blended = 1 - (1 - base) * (1 - value)
    elif blend == 'OVERLAY':
        blended = np.where(base <= .5, 2 * base * value, 1 - 2 * (1 - base) * (1 - value))
    elif blend == 'MAX':
        blended = np.maximum(base, value)
    elif blend == 'MIN':
        blended = np.minimum(base, value)
    else:
        blended = value
    return base + (blended - base) * alpha


def expected_color(mask_values):
    result = np.ones((SIZE * SIZE, 4), dtype=np.float32)
    result[:, 2] = mask_values
    return result


def stroke(ctrl=False, label='', held_check=None):
    region = next(r for r in area.regions if r.type == 'WINDOW')
    point = location_3d_to_region_2d(region, area.spaces.active.region_3d, Vector((0, 0, 0)))
    x, y = region.x + int(point.x), region.y + int(point.y)
    if ctrl:
        window.event_simulate(type='LEFT_CTRL', value='PRESS', x=x, y=y)
        yield .05
    window.event_simulate(type='MOUSEMOVE', value='NOTHING', x=x, y=y, ctrl=ctrl)
    yield .06
    window.event_simulate(type='LEFTMOUSE', value='PRESS', x=x, y=y, ctrl=ctrl)
    yield .16
    if held_check:
        held_check(label + ' held dab')
    window.event_simulate(type='MOUSEMOVE', value='NOTHING', x=x+18, y=y, ctrl=ctrl)
    yield .1
    window.event_simulate(type='LEFTMOUSE', value='RELEASE', x=x+18, y=y, ctrl=ctrl)
    if ctrl:
        window.event_simulate(type='LEFT_CTRL', value='RELEASE', x=x+18, y=y)
    yield .12


def setup():
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob, do_unlink=True)
    bpy.ops.mesh.primitive_plane_add(size=2)
    mat = bpy.data.materials.new('Mask Viewport Matrix')
    mat.use_nodes = True
    bpy.context.object.data.materials.append(mat)
    n = mat.node_tree.nodes.new('ShaderNodeTexPaint')
    mat.node_tree.nodes.active = n
    op(bpy.ops.node.tex_paint_canvas_new, name='Viewport Matrix', width=SIZE, height=SIZE,
       color=(1, 1, 0, 1))
    op(bpy.ops.node.tex_paint_layer_add, type='FILL')
    n.active_layer.fill_color = (1, 1, 1, 1)
    n.active_layer.opacity = 1
    maps = {}
    for key in ('ao', 'curvature', 'normal_ws', 'position', 'grunge'):
        image = bpy.data.images.new('Matrix ' + key, width=SIZE, height=SIZE, float_buffer=True)
        image.colorspace_settings.name = 'Non-Color'
        values = np.ones((SIZE, SIZE, 4), dtype=np.float32)
        values[..., :3] = np.linspace(.25, .75, SIZE)[None, :, None]
        image.pixels.foreach_set(values.ravel())
        image.pack()
        image.use_fake_user = True
        maps[key] = image.name
    paint._save_mesh_maps_state(n, map_updates=maps)
    region = area.spaces.active.region_3d
    region.view_distance, region.view_location = 4, (0, 0, 0)
    region.view_rotation, region.view_perspective = (1, 0, 0, 0), 'ORTHO'
    area.spaces.active.show_region_ui = False
    area.spaces.active.shading.type = 'SOLID'
    area.spaces.active.shading.color_type = 'TEXTURE'
    bpy.context.preferences.experimental.use_3d_texture_paint = False
    op(bpy.ops.object.mode_set, mode='TEXTURE_PAINT')
    ip = bpy.context.tool_settings.image_paint
    ip.mode, ip.canvas = 'IMAGE', n.image
    op(bpy.ops.brush.asset_activate, asset_library_type='ESSENTIALS',
       relative_asset_identifier='brushes/essentials_brushes-mesh_texture.blend/Brush/Paint Hard')
    brush = ip.brush
    brush.strength, brush.size = 1, 28
    brush.use_pressure_size = brush.use_pressure_strength = False
    brush.use_unified_color = brush.use_unified_size = brush.use_unified_strength = False
    if bpy.app.timers.is_registered(paint._mask_paint_sync_timer):
        bpy.app.timers.unregister(paint._mask_paint_sync_timer)
    return maps


def make_mask(generator, maps):
    op(bpy.ops.node.tex_paint_mask_stack_init, mode='WHITE')
    if generator == 'GRUNGE':
        factory = paint._make_default_mask_effect
        def defaults(*pos, **kwargs):
            result = factory(*pos, **kwargs)
            if result['type'] == 'GRUNGE':
                result.update(category='CUSTOM', custom_image=maps['grunge'], channel='RED',
                              proj_mode='TRIPLANAR', scale=1.0, balance=.5, contrast=0)
            return result
        paint._make_default_mask_effect = defaults
        try:
            op(bpy.ops.node.tex_paint_mask_effect_add, effect_type='GRUNGE')
        finally:
            paint._make_default_mask_effect = factory
    else:
        op(bpy.ops.node.tex_paint_mask_effect_add, effect_type='GENERATOR', gen_type=generator)
    source_uid = paint._get_active_mask_effect(node())['uid']
    write_metadata(source_uid, gen_balance=.5, gen_contrast=0, gen_radius=0, gen_noise=0)
    op(bpy.ops.node.tex_paint_mask_effect_add, effect_type='PAINT')
    painted_uid = paint._get_active_mask_effect(node())['uid']
    node().active_layer.show_projection_widget = False
    return source_uid, painted_uid


def mask_case(generator, blend, index, source_uid, painted_uid, brush_mode='MIX', seed_existing=False):
    label = generator + '/' + blend + '/brush ' + brush_mode
    report['cases'].append(label)
    layer = node().active_layer
    layer.lock_alpha = bool(index % 2)  # Color alpha lock must not prevent mask erasure.
    opacity, invert = (.63 if index % 2 else 1.0), bool(index % 3 == 1)
    write_metadata(painted_uid, blend_mode=blend, opacity=opacity, invert=invert)
    effect(painted_uid).pixels.foreach_set(np.zeros(SIZE * SIZE * 4, dtype=np.float32))
    layer.mask_stack_evaluate()
    base = mask()
    if seed_existing:
        # Blender's non-Mix brush modes modify existing paint. Give them owned
        # coverage; empty transparent pixels alone do not exercise these modes.
        seed = np.tile(np.array((.65, .65, .65, .8), dtype=np.float32), (SIZE * SIZE, 1))
        effect(painted_uid).pixels.foreach_set(seed.ravel())
        layer.mask_stack_evaluate()
    source_before = rgba(effect(source_uid), 'pixels')
    brush = bpy.context.tool_settings.image_paint.brush
    bright = blend in {'ADD', 'SCREEN', 'MAX'}
    value = .85 if bright else .15
    value = 1 - value if invert else value
    brush.color, brush.blend = (value,) * 3, brush_mode

    def verify(stage):
        raw = rgba(effect(painted_uid), 'pixels')
        expected = painted_expectation(base, raw, blend, opacity, invert)
        check(label + ' ' + stage + ' final mask equals coverage blend', np.allclose(mask(), expected, atol=2/255))
        check(label + ' ' + stage + ' viewport image includes underlying yellow',
              np.allclose(color(), expected_color(mask()), atol=1/255 + 1e-6))
        check(label + ' ' + stage + ' generator pixels unchanged',
              np.array_equal(rgba(effect(source_uid), 'pixels'), source_before))
        if index == 0 and stage.endswith('held dab') and 'Ctrl' not in stage:
            op(bpy.ops.screen.screenshot, filepath=str(args.output / (generator + '-held-dab.png')))
    yield from stroke(label=label, held_check=verify)
    painted_raw, painted_mask = rgba(effect(painted_uid), 'pixels'), mask()
    check(label + ' brush writes selected Paint coverage', np.any(painted_raw[:, 3] > 0))
    verify('released')
    yield .08
    check(label + ' no idle snapback', np.array_equal(mask(), painted_mask))
    if index == 0:
        op(bpy.ops.screen.screenshot, filepath=str(args.output / (generator + '-paint.png')))
    toggle(painted_uid)
    check(label + ' hiding Paint reveals procedural result', np.array_equal(mask(), base))
    toggle(painted_uid)
    toggle(source_uid)
    toggle(source_uid)
    check(label + ' effect toggles preserve stroke and source',
          np.array_equal(mask(), painted_mask) and np.array_equal(rgba(effect(painted_uid), 'pixels'), painted_raw))
    layer.use_mask = False
    check(label + ' disabled layer mask reveals complete white Fill', np.allclose(color(), 1, atol=1/255))
    layer.use_mask = True
    layer.hide = True
    check(label + ' hiding Fill reveals yellow base', np.allclose(color(), (1, 1, 0, 1), atol=1/255))
    layer.hide = False
    layer.preview_mask = True
    check(label + ' mask preview shows evaluated mask', np.allclose(color()[:, :3], mask()[:, None], atol=1/255))
    layer.preview_mask = False
    layer.invert_mask = True
    check(label + ' layer mask inversion keeps underlying yellow',
          np.allclose(color(), expected_color(1 - painted_mask), atol=1/255 + 1e-6))
    layer.invert_mask = False
    check(label + ' viewport toggle round trip preserves native Paint',
          np.array_equal(mask(), painted_mask) and np.array_equal(rgba(effect(painted_uid), 'pixels'), painted_raw))
    yield from stroke(ctrl=True, label=label + ' Ctrl', held_check=verify)
    erased = rgba(effect(painted_uid), 'pixels')
    check(label + ' Ctrl erase reduces coverage even with color alpha lock', erased[:, 3].sum() < painted_raw[:, 3].sum())
    verify('erased')
    check(label + ' Ctrl erase approaches unchanged procedural base',
          np.abs(mask() - base).sum() <= np.abs(painted_mask - base).sum() + 1/255)
    after_erase = mask()
    toggle(painted_uid)
    toggle(painted_uid)
    check(label + ' erase survives toggles', np.array_equal(mask(), after_erase) and
          np.array_equal(rgba(effect(painted_uid), 'pixels'), erased))


def color_layer_cases():
    op(bpy.ops.node.tex_paint_layer_add, type='PAINT')
    n = node()
    n.layers[-2].hide = True
    n.paint_target = 'COLOR'
    layer = n.active_layer
    brush = bpy.context.tool_settings.image_paint.brush
    brush.blend, brush.color = 'MIX', (.35, .7, .15)
    for blend in [item.identifier for item in layer.bl_rna.properties['blend_mode'].enum_items]:
        label = 'Color layer/' + blend
        report['cases'].append(label)
        layer.blend_mode, layer.opacity = blend, .72
        n.paint_pixels.foreach_set(np.zeros(SIZE * SIZE * 4, dtype=np.float32))
        layer.opacity = layer.opacity
        baseline = color()
        def verify(stage):
            live = color()
            layer.opacity = layer.opacity  # Independent full composition after capturing live upload data.
            check(label + ' ' + stage + ' dirty composite equals full composite', np.allclose(live, color(), atol=1/255))
        yield from stroke(label=label, held_check=verify)
        painted = rgba(n, 'paint_pixels')
        painted_master = color()
        check(label + ' stroke writes color coverage', painted[:, 3].sum() > 0)
        layer.hide = True
        check(label + ' hiding Paint retains lower yellow', np.array_equal(color(), baseline))
        layer.hide = False
        check(label + ' visibility restores stroke', np.array_equal(color(), painted_master))
        yield from stroke(ctrl=True, label=label + ' Ctrl', held_check=verify)
        check(label + ' Ctrl erases in this layer blend', rgba(n, 'paint_pixels')[:, 3].sum() < painted[:, 3].sum())
        verify('released Ctrl')


def blocked_target_cases(maps):
    source_uid, painted_uid = make_mask('AO', maps)
    layer = node().active_layer
    for guard in ('generator selected', 'Paint disabled', 'layer hidden', 'layer locked', 'Fill mask disabled'):
        if guard == 'generator selected':
            op(bpy.ops.node.tex_paint_mask_effect_select, effect_uid=source_uid)
        elif guard == 'Paint disabled':
            toggle(painted_uid)
        elif guard == 'layer hidden':
            layer.hide = True
        elif guard == 'layer locked':
            layer.lock = True
        else:
            layer.use_mask = False
        source_before, paint_before, master_before = rgba(effect(source_uid), 'pixels'), rgba(effect(painted_uid), 'pixels'), color()
        yield from stroke()
        yield from stroke(ctrl=True)
        check(guard + ' blocks paint and Ctrl without fallback to generator or composite',
              np.array_equal(rgba(effect(source_uid), 'pixels'), source_before) and
              np.array_equal(rgba(effect(painted_uid), 'pixels'), paint_before) and
              np.array_equal(color(), master_before))
        if guard == 'generator selected':
            op(bpy.ops.node.tex_paint_mask_effect_select, effect_uid=painted_uid)
        elif guard == 'Paint disabled':
            toggle(painted_uid)
        elif guard == 'layer hidden':
            layer.hide = False
        elif guard == 'layer locked':
            layer.lock = False
        else:
            layer.use_mask = True


def workflow():
    yield .5
    maps = setup()
    yield .5
    if args.group in {'masks', 'all'}:
        for generator in GENERATORS:
            source_uid, painted_uid = make_mask(generator, maps)
            for mode in BLENDS:
                write_metadata(source_uid, blend_mode=mode, opacity=0)
                zero_mask, zero_color = mask(), color()
                toggle(source_uid)
                check(generator + '/' + mode + ' zero-opacity source equals disabled in viewport',
                      np.array_equal(mask(), zero_mask) and np.array_equal(color(), zero_color))
                toggle(source_uid)
            write_metadata(source_uid, blend_mode='REPLACE', opacity=1)
            for index, blend in enumerate(BLENDS):
                bpy.context.tool_settings.image_paint.mode = 'MATERIAL' if index % 2 else 'IMAGE'
                yield from mask_case(generator, blend, index, source_uid, painted_uid)
        # Exercise native mesh generation too, on a sloped receiver with a real
        # height range. The full blend grid above uses controlled baked sources.
        paint._save_mesh_maps_state(node(), updates={'use_baked_maps': False})
        for vertex in bpy.context.object.data.vertices:
            vertex.co.z = vertex.co.x * .3
        bpy.context.object.data.update()
        for generator in GENERATORS[:-1]:
            source_uid, painted_uid = make_mask(generator, maps)
            for index, blend in enumerate(('REPLACE', 'MULTIPLY')):
                yield from mask_case('Local ' + generator, blend, index, source_uid, painted_uid)
        paint._save_mesh_maps_state(node(), updates={'use_baked_maps': True})
        yield from blocked_target_cases(maps)
    if args.group in {'brushes', 'all'}:
        source_uid, painted_uid = make_mask('AO', maps)
        brushes = [item.identifier for item in bpy.types.Brush.bl_rna.properties['blend'].enum_items
                   if item.identifier not in {'ERASE_ALPHA', 'ADD_ALPHA'}]
        for index, brush in enumerate(brushes):
            yield from mask_case('Brush modes', 'MULTIPLY', index, source_uid, painted_uid,
                                 brush_mode=brush, seed_existing=True)
    if args.group in {'layers', 'all'}:
        yield from color_layer_cases()
    op(bpy.ops.screen.screenshot, filepath=str(args.output / 'final-viewport.png'))
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
