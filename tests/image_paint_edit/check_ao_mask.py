# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Check UV rasterization and procedural AO in a disposable background process."""

import argparse
import hashlib
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
assert '--factory-startup' in sys.argv and bpy.app.background and not bpy.data.filepath
args.output.mkdir(parents=True, exist_ok=True)
report = {
    'build_hash': bpy.app.build_hash.decode(),
    'build_date': bpy.app.build_date.decode(),
    'build_time': bpy.app.build_time.decode(),
    'runtime': bpy.app.binary_path,
    'module': paint.__file__,
    'module_sha256': hashlib.sha256(Path(paint.__file__).read_bytes()).hexdigest(),
    'checks': [],
    'passed': False,
}


def mesh_object(name, verts, faces, uvs=None):
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(verts, [], faces)
    if uvs is not None:
        uv_layer = mesh.uv_layers.new()
        uv_layer.data.foreach_set('uv', np.asarray(
            [uvs[loop.vertex_index] for loop in mesh.loops], dtype=np.float32).ravel())
    mesh.update()
    ob = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(ob)
    return ob


def clear_caches():
    paint._UV_TEXEL_GRID_CACHE.clear()
    paint._GLOBAL_AO_CACHE.clear()


def rectangle_map(width, height, bounds, reverse=False):
    x0, y0, x1, y1 = bounds
    uv = np.array([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], dtype=np.float32)
    uv /= (width, height)
    faces = [(0, 1, 2), (0, 2, 3)]
    if reverse:
        faces = [tuple(reversed(face)) for face in faces]
    ob = mesh_object('UV rectangle', [(u, v, 0) for u, v in uv], faces, uv)
    clear_caches()
    mapped = paint._build_uv_texel_map(ob, width, height)
    ys, xs = np.mgrid[:height, :width]
    expected = np.flatnonzero((xs + .5 >= x0) & (xs + .5 <= x1) &
                              (ys + .5 >= y0) & (ys + .5 <= y1))
    np.testing.assert_array_equal(mapped['pix_idx'], expected)
    weights = np.column_stack([mapped[key] for key in ('w0', 'w1', 'w2')])
    np.testing.assert_allclose(weights.sum(axis=1), 1, atol=1e-6)
    assert np.isfinite(weights).all() and (weights >= 0).all()
    # Interpolated UVs must locate the actual texel centers, including nonzero X/Y offsets.
    tri_uv = mapped['uvs'][mapped['tri_loops'][mapped['tri_idx']]]
    reconstructed = (tri_uv * weights[:, :, None]).sum(axis=1) * (width, height)
    centers = np.column_stack([expected % width + .5, expected // width + .5])
    # The rasterizer allows a small edge tolerance; check interpolation strictly inside triangles.
    interior = (weights > .025).all(axis=1)
    np.testing.assert_allclose(reconstructed[interior], centers[interior], atol=2e-5)
    assert paint._build_uv_texel_map(ob, width, height) is mapped


def chunked_map():
    # More than 4096 small triangles, with one independently known covered texel each.
    width, height = 65, 64
    verts, faces, uv = [], [], []
    for y in range(height):
        for x in range(width):
            start = len(verts)
            coords = [(x + .1, y + .1), (x + .9, y + .1), (x + .5, y + .9)]
            verts.extend((u, v, 0) for u, v in coords)
            uv.extend((u / width, v / height) for u, v in coords)
            faces.append((start, start + 1, start + 2))
    ob = mesh_object('Chunked UV triangles', verts, faces, uv)
    clear_caches()
    mapped = paint._build_uv_texel_map(ob, width, height)
    np.testing.assert_array_equal(mapped['pix_idx'], np.arange(width * height))
    np.testing.assert_array_equal(mapped['tri_idx'], np.arange(width * height))


def degenerate_map():
    ob = mesh_object('Degenerate UV', [(0, 0, 0), (1, 0, 0), (0, 1, 0)],
                     [(0, 1, 2)], [(0.5, 0.5)] * 3)
    clear_caches()
    assert len(paint._build_uv_texel_map(ob, 32, 24)['pix_idx']) == 0


def ao_operator():
    # Remove only this disposable process's generated fixtures and factory objects.
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob, do_unlink=True)
    clear_caches()
    verts, uv, faces = [], [], []
    steps = 8
    for y in range(steps + 1):
        for x in range(steps + 1):
            verts.append((2 * x / steps - 1, 2 * y / steps - 1, 0))
            uv.append((.1 + .8 * x / steps, .1 + .8 * y / steps))
    for y in range(steps):
        for x in range(steps):
            i = y * (steps + 1) + x
            faces.append((i, i + 1, i + steps + 2, i + steps + 1))
    target = mesh_object('AO receiver', verts, faces, uv)
    mesh_object('Nearby occluder', [(-2, -2, .1), (0, -2, .1), (0, 2, .1), (-2, 2, .1)],
                [(0, 1, 2, 3)])
    target.select_set(True)
    bpy.context.view_layer.objects.active = target
    bpy.context.view_layer.update()
    material = bpy.data.materials.new('AO regression')
    material.use_nodes = True
    target.data.materials.append(material)
    node = material.node_tree.nodes.new('ShaderNodeTexPaint')
    material.node_tree.nodes.active = node
    width, height = 64, 48
    with bpy.context.temp_override(node=node):
        assert bpy.ops.node.tex_paint_canvas_new(
            name='AO regression', width=width, height=height, color=(1, 1, 1, 1)) == {'FINISHED'}
        source_mesh = target.data
        source_image = node.image
        assert bpy.ops.node.tex_paint_mask_effect_add(effect_type='GENERATOR', gen_type='AO') == {'FINISHED'}
        assert node.active_layer.use_mask and node.paint_target == 'MASK'
        effect = paint._get_active_mask_effect(node)
        assert effect['gen_type'] == 'AO' and effect['gen_global_ao']

        def mask_pixels():
            pixels = np.empty(width * height * 4, dtype=np.float32)
            node.active_layer.mask_pixels.foreach_get(pixels)
            return pixels.reshape(height, width, 4)[..., 0].copy()

        with_occluder = mask_pixels()
        assert np.isfinite(with_occluder).all()
        assert with_occluder[12:36, 12:24].mean() + .05 < with_occluder[12:36, 42:50].mean()
        # Disabling global AO must remove the actual nearby-object contribution.
        node.tex_paint_mask_eff_gen_global_ao = False
        local_only = mask_pixels()
        assert local_only[12:36, 12:24].mean() > with_occluder[12:36, 12:24].mean() + .05
        node.tex_paint_mask_eff_gen_global_ao = True
        np.testing.assert_allclose(mask_pixels(), with_occluder, atol=1 / 255)
        # These generators share the same external-occlusion rasterizer.
        for kind in ('CAVITY', 'EDGE_WEAR'):
            clear_caches()
            assert bpy.ops.node.tex_paint_mask_effect_add(effect_type='GENERATOR', gen_type=kind) == {'FINISHED'}
            assert np.isfinite(mask_pixels()).all()
        assert target.data == source_mesh and node.image == source_image
        assert not paint._INTERNAL_REPROJECTING


cases = [
    ('small UV island', lambda: rectangle_map(32, 24, (3, 2, 12, 10))),
    ('reversed winding', lambda: rectangle_map(32, 24, (15, 12, 24, 20), True)),
    ('clipped UV island', lambda: rectangle_map(32, 24, (-2, -1, 5, 7))),
    ('single-column grid', lambda: rectangle_map(1, 12, (0, 2, 1, 10))),
    ('single-row grid', lambda: rectangle_map(12, 1, (2, 0, 10, 1))),
    ('large triangle fallback', lambda: rectangle_map(64, 48, (4, 3, 24, 23))),
    ('multiple triangle batches', chunked_map),
    ('degenerate UV triangle', degenerate_map),
    ('AO operator and nearby-object contribution', ao_operator),
]
for name, run in cases:
    result = {'name': name, 'passed': False}
    try:
        run()
        result['passed'] = True
    except Exception:
        result['error'] = traceback.format_exc()
        print(result['error'], flush=True)
    report['checks'].append(result)
    print('AO_MASK_CHECK', name, result['passed'], flush=True)
report['passed'] = all(result['passed'] for result in report['checks'])
(args.output / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
print('AO_MASK_RESULT', json.dumps(report), flush=True)
if not report['passed']:
    raise RuntimeError('AO mask regression failed; see report.json')
