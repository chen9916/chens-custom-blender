# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Cycles mesh-map integration, isolation, scope and persistence regressions."""

import argparse
import hashlib
import json
import sys
import traceback
from pathlib import Path

import bpy
import numpy as np
from bl_ui import properties_tex_paint_layers as paint
from bl_ui import tex_paint_baking as baker


parser = argparse.ArgumentParser()
parser.add_argument('--output', type=Path, required=True)
parser.add_argument('--model', type=Path)
args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
assert '--factory-startup' in sys.argv and bpy.app.background and not bpy.data.filepath
args.output.mkdir(parents=True, exist_ok=True)
report = {'build_hash': bpy.app.build_hash.decode(), 'build_time': bpy.app.build_time.decode(),
          'runtime': bpy.app.binary_path, 'checks': [], 'passed': False,
          'sources': {str(Path(m.__file__)): hashlib.sha256(Path(m.__file__).read_bytes()).hexdigest()
                      for m in (paint, baker)}}


def check(name, condition):
    result = bool(condition)
    report['checks'].append({'name': name, 'passed': result})
    print('MESH_BAKE_CHECK', name, result, flush=True)
    assert result, name


def pixels(image):
    array = np.empty(len(image.pixels), dtype=np.float32)
    image.pixels.foreach_get(array)
    return array.reshape(image.size[1], image.size[0], 4)


def clear_objects():
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob, do_unlink=True)


def receiver(name='Receiver', offset=0):
    bpy.ops.mesh.primitive_plane_add(size=2, location=(offset, 0, 0))
    ob = bpy.context.object
    ob.name = name
    return ob


def canvas(ob):
    material = bpy.data.materials.new('Bake Test')
    material.use_nodes = True
    ob.data.materials.append(material)
    node = material.node_tree.nodes.new('ShaderNodeTexPaint')
    material.node_tree.nodes.active = node
    with bpy.context.temp_override(node=node):
        bpy.ops.node.tex_paint_canvas_new(name='Bake Test', width=64, height=64, color=(.2, .4, .6, 1))
    paint._save_mesh_maps_state(node, updates={
        'bake_samples': 32, 'bake_margin': 4, 'bake_device': 'CPU', 'apply_normal_to_shader': False,
        'use_baked_maps': False, 'ao_dist': .5})
    return material, node


def bake(node, kind='ao'):
    with bpy.context.temp_override(node=node):
        result = bpy.ops.node.tex_paint_bake_mesh_maps(map_type=kind)
    assert result == {'FINISHED'}
    return bpy.data.images[paint._get_mesh_maps_state(node)['maps'][kind]] if kind != 'ALL' else None


def isolation():
    return {
        'scene': bpy.context.scene.name, 'engine': bpy.context.scene.render.engine,
        'selected': sorted(o.name for o in bpy.context.selected_objects),
        'active': bpy.context.view_layer.objects.active.name,
        'objects': sorted(o.name for o in bpy.data.objects),
        'meshes': sorted(o.name for o in bpy.data.meshes),
        'materials': sorted(o.name for o in bpy.data.materials),
        'scenes': sorted(o.name for o in bpy.data.scenes),
        'device': bpy.context.scene.cycles.device,
        'devices': [(d.name, d.type, d.use) for d in bpy.context.preferences.addons['cycles'].preferences.devices],
    }


def workflow():
    clear_objects()
    ob = receiver()
    material, node = canvas(ob)
    node.active_layer.use_mask = True
    node.paint_target = 'MASK'
    authored_mask = np.ones((64, 64, 4), dtype=np.float32)
    authored_mask[..., :3] = .7
    authored_mask[16:32, 8:24, :3] = .2
    node.paint_pixels.foreach_set(authored_mask.ravel())
    authored_mask = np.array(node.paint_pixels[:], dtype=np.float32)
    blocker = receiver('Occluder')
    blocker.location.z = .1
    blocker.scale = (2, 2, 1)
    bpy.context.view_layer.objects.active = ob
    bpy.context.view_layer.update()
    baseline = isolation()
    canvas_before = pixels(node.image).copy()
    geometry_before = [tuple(v.co) for v in ob.data.vertices]
    global_ao = pixels(bake(node)).copy()
    check('nearby occluder darkens Cycles AO', global_ao[8:-8, 8:-8, 0].mean() < .2)
    check('baking preserves source scene and device settings', baseline == isolation())
    check('baking preserves source canvas and geometry', np.array_equal(canvas_before, pixels(node.image)) and
          geometry_before == [tuple(v.co) for v in ob.data.vertices])
    check('baking preserves disabled baked-map preference and mask state',
          not paint._get_mesh_maps_state(node)['use_baked_maps'] and node.active_layer.use_mask
          and np.array_equal(authored_mask, np.array(node.paint_pixels[:], dtype=np.float32)))
    node.tex_paint_bake_ao_scope = 'SELF'
    self_ao = pixels(bake(node)).copy()
    check('Self AO excludes surrounding geometry', self_ao[8:-8, 8:-8, 0].mean() > .98)
    node.tex_paint_bake_ao_scope = 'MATERIAL'
    material_ao = pixels(bake(node)).copy()
    check('Material AO excludes unrelated materials', material_ao[8:-8, 8:-8, 0].mean() > .98)
    blocker.data.materials.append(material)
    # Give the second receiver its own half of the same atlas.
    for loop in ob.data.uv_layers.active.data:
        loop.uv.x *= .45
    for loop in blocker.data.uv_layers.active.data:
        loop.uv.x = .55 + loop.uv.x * .45
    bpy.context.view_layer.update()
    shared = pixels(bake(node))
    check('Material AO includes shared-material parts', shared[8:-8, 4:24, 0].mean() < .2)
    check('shared atlas retains earlier receiver pixels', shared[8:-8, 40:58, 0].mean() > .98)
    check('rebaking releases unused prior generated maps',
          sum(i.get('tex_paint_bake_map') == 'ao' for i in bpy.data.images) == 1)
    blocker.data.materials.clear()

    # Evaluate modifiers on copies; the authored four-vertex plane remains untouched.
    modifier = ob.modifiers.new('Evaluate only for bake', 'SOLIDIFY')
    modifier.thickness = .25
    position = pixels(bake(node, 'position'))
    check('bake evaluates modifiers without applying them', len(ob.data.vertices) == 4 and modifier in ob.modifiers[:]
          and np.isfinite(position).all())
    ob.modifiers.remove(modifier)

    # Known high-poly slope: selected-to-active must transfer its non-neutral normal.
    high = receiver('High Source')
    high.location.z = .02
    high.rotation_euler.y = .1
    bpy.context.view_layer.objects.active = ob
    bpy.context.view_layer.update()
    node.tex_paint_bake_source_mode = 'HIGH_POLY_OB'
    node.tex_paint_bake_high_poly_ob = high.name
    node.tex_paint_bake_cage_dist = .3
    normal = pixels(bake(node, 'normal'))
    check('selected-to-active transfers high-poly normals', abs(float(normal[16:48, 6:23, 0].mean()) - .5) > .025)
    node.tex_paint_bake_high_poly_ob = 'Missing source'
    saved_slots = dict(paint._get_mesh_maps_state(node)['maps'])
    failed_baseline = isolation()
    ok, message = paint._bake_mesh_maps_for_node(node, bpy.context, 'normal')
    check('missing high-poly source fails without replacing maps', not ok and 'high-poly' in message and
          saved_slots == paint._get_mesh_maps_state(node)['maps'] and isolation() == failed_baseline)
    node.tex_paint_bake_source_mode = 'LOW_POLY'
    uv = ob.data.uv_layers.active
    ob.data.uv_layers.remove(uv)
    ob.data.update()
    bpy.context.view_layer.update()
    failed_baseline = isolation()
    count = len(bpy.data.images)
    ok, message = paint._bake_mesh_maps_for_node(node, bpy.context, 'ao')
    check('missing UVs clean up temporary data and retain prior maps', not ok and 'UV' in message and
          isolation() == failed_baseline and len(bpy.data.images) == count and
          saved_slots == paint._get_mesh_maps_state(node)['maps'])

    clear_objects()
    ob = receiver('Painted mask receiver')
    material, node = canvas(ob)
    node.tex_paint_use_baked_mesh_maps = True
    with bpy.context.temp_override(node=node):
        bpy.ops.node.tex_paint_mask_effect_add(effect_type='GENERATOR', gen_type='AO')
        bpy.ops.node.tex_paint_mask_effect_add(effect_type='PAINT')
    mask = np.array(node.paint_pixels[:], dtype=np.float32).reshape(64, 64, 4)
    mask[16:32, 8:24, :3] = .2
    mask[16:32, 8:24, 3] = 1.0
    node.paint_pixels.foreach_set(mask.ravel())
    painted = np.array(node.paint_pixels[:], dtype=np.float32).reshape(64, 64, 4)
    bake(node)
    rebaked = np.array(node.paint_pixels[:], dtype=np.float32).reshape(64, 64, 4)
    check('rebaking preserves native Paint values and coverage exactly', np.array_equal(rebaked, painted))
    evaluated = np.array(node.active_layer.mask_pixels[:], dtype=np.float32).reshape(64, 64, 4)
    check('rebaked AO evaluates underneath independent Paint coverage',
          np.allclose(evaluated[16:32, 8:24, :3], .2, atol=1 / 255) and
          evaluated[40:50, 40:50, 0].mean() > .98 and np.all(evaluated[..., 3] == 1))

    # Exercise full-resolution maps and file persistence on the user's disposable OBJ import.
    if args.model:
        clear_objects()
        bpy.ops.wm.obj_import(filepath=str(args.model))
        objects = sorted(bpy.context.selected_objects, key=lambda o: o.name)
        snapshots = []
        export_scene = bpy.data.scenes.new('Map PNG Export')
        export_scene.view_settings.view_transform = 'Raw'
        export_scene.render.image_settings.file_format = 'PNG'
        export_scene.render.image_settings.color_mode = 'RGB'
        export_scene.render.image_settings.color_depth = '16'
        for index, target in enumerate(objects):
            bpy.context.view_layer.objects.active = target
            mat = target.active_material
            mat.use_nodes = True
            node = mat.node_tree.nodes.new('ShaderNodeTexPaint')
            mat.node_tree.nodes.active = node
            with bpy.context.temp_override(node=node):
                bpy.ops.node.tex_paint_canvas_new(name=target.name, width=1024, height=1024, color=(1, 1, 1, 1))
            paint._save_mesh_maps_state(node, updates={
                'bake_samples': 128, 'bake_device': 'CPU', 'bake_res': '1024',
                'apply_normal_to_shader': False, 'use_baked_maps': False})
            original_mesh = target.data
            baseline = isolation()
            bake(node, 'ALL')
            check(target.name + ' retains mesh, normals and scene', target.data == original_mesh and
                  target.data.has_custom_normals and isolation() == baseline)
            for slot, name in paint._get_mesh_maps_state(node)['maps'].items():
                image = bpy.data.images[name]
                data = pixels(image)
                check(target.name + ' ' + slot + ' is finite, packed and full size',
                      tuple(image.size) == (1024, 1024) and np.isfinite(data).all() and
                      image.packed_file is not None and image.use_fake_user and
                      image.get('tex_paint_bake_engine') == 'CYCLES')
                image.save_render(str(args.output / (target.name + '-' + slot + '.png')), scene=export_scene)
                snapshots.append((name, data))
        bpy.data.scenes.remove(export_scene)
        fixture = args.output / 'meetmat-cycles-validated.blend'
        bpy.ops.wm.save_as_mainfile(filepath=str(fixture))
        bpy.ops.wm.open_mainfile(filepath=str(fixture))
        errors = {name: float(np.max(np.abs(pixels(bpy.data.images[name]) - data)))
                  if bpy.data.images.get(name) else None for name, data in snapshots}
        report['roundtrip_max_error'] = errors
        check('all 21 packed mesh maps survive save and reopen',
              all(error is not None and error < 1e-6 for error in errors.values()))


try:
    workflow()
    report['passed'] = True
except Exception:
    report['error'] = traceback.format_exc()
    print(report['error'], flush=True)
(args.output / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
if not report['passed']:
    raise RuntimeError('Mesh-map baking regression failed')
