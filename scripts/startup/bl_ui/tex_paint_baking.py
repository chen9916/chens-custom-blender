# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Cycles mesh-map baking on disposable evaluated meshes and materials.

Nothing in the artist's scene is used as a render target or temporarily rewired.
Images are published by the caller only after the entire requested bake succeeds.
"""

import colorsys
from dataclasses import dataclass

import bpy
from mathutils import Vector


MAP_LABELS = {
    'normal': 'Normal',
    'normal_ws': 'WorldNormal',
    'id_map': 'ID',
    'ao': 'AO',
    'curvature': 'Curvature',
    'position': 'Position',
    'thickness': 'Thickness',
}


def _id_color(ob, part, slot):
    if any(abs(c - 1.0) > .001 for c in ob.color[:3]):
        return tuple(ob.color[:3])
    index = part * 7 + slot + 1
    return colorsys.hsv_to_rgb((index * .61803398875 + .13) % 1, .78, .92)


def _shader(material, kind, image, color, bounds_min, bounds_span, distance, scope):
    tree = material.node_tree
    tree.nodes.clear()
    output = tree.nodes.new('ShaderNodeOutputMaterial')
    emission = tree.nodes.new('ShaderNodeEmission')
    tree.links.new(emission.outputs[0], output.inputs['Surface'])
    geo = tree.nodes.new('ShaderNodeNewGeometry')

    if kind in {'ao', 'thickness'}:
        ao = tree.nodes.new('ShaderNodeAmbientOcclusion')
        ao.samples = 4
        ao.inside = kind == 'thickness'
        ao.only_local = scope == 'SELF' or kind == 'thickness'
        ao.inputs['Distance'].default_value = distance
        value = ao.outputs['AO']
        if kind == 'thickness':
            # Inward occlusion: white indicates a thicker, more enclosed region.
            invert = tree.nodes.new('ShaderNodeMath')
            invert.operation = 'SUBTRACT'
            invert.inputs[0].default_value = 1
            tree.links.new(value, invert.inputs[1])
            value = invert.outputs[0]
        tree.links.new(value, emission.inputs['Color'])
    elif kind in {'normal_ws', 'position'}:
        offset = tree.nodes.new('ShaderNodeVectorMath')
        scale = tree.nodes.new('ShaderNodeVectorMath')
        if kind == 'normal_ws':
            scale.operation = 'SCALE'
            scale.inputs['Scale'].default_value = .5
            tree.links.new(geo.outputs['Normal'], scale.inputs[0])
            offset.operation = 'ADD'
            offset.inputs[1].default_value = (.5, .5, .5)
            tree.links.new(scale.outputs['Vector'], offset.inputs[0])
            value = offset.outputs['Vector']
        else:
            offset.operation = 'SUBTRACT'
            offset.inputs[1].default_value = bounds_min
            tree.links.new(geo.outputs['Position'], offset.inputs[0])
            scale.operation = 'DIVIDE'
            scale.inputs[1].default_value = bounds_span
            tree.links.new(offset.outputs['Vector'], scale.inputs[0])
            value = scale.outputs['Vector']
        tree.links.new(value, emission.inputs['Color'])
    elif kind == 'curvature':
        # Cycles pointiness is topology-dependent, but never differentiates across UV seams.
        tree.links.new(geo.outputs['Pointiness'], emission.inputs['Color'])
    else:
        emission.inputs['Color'].default_value = (*color, 1)

    target = tree.nodes.new('ShaderNodeTexImage')
    target.image = image
    tree.nodes.active = target


@dataclass
class BakeStep:
    scene: object
    view_layer: object
    receiver: object
    selected: list
    options: dict
    label: str
    index: int
    total: int

    def run(self, context, execution='EXEC_DEFAULT'):
        # Native baking normally takes over an Image Editor on the invoking screen.
        # Keep artists' editors intact and avoid retaining an area in the worker.
        with context.temp_override(screen=None, area=None, region=None,
                                   scene=self.scene, view_layer=self.view_layer,
                                   active_object=self.receiver, object=self.receiver,
                                   selected_objects=self.selected, selected_editable_objects=self.selected):
            return bpy.ops.object.bake(execution, **self.options)


def run_sync(context, steps):
    """Drive the same bake steps synchronously for scripts and background callers."""
    try:
        while True:
            try:
                step = next(steps)
            except StopIteration as done:
                return done.value
            if step.run(context) != {'FINISHED'}:
                raise RuntimeError(f'Cycles {step.label} bake was cancelled')
    finally:
        steps.close()


def bake_maps(context, *args):
    return run_sync(context, bake_map_steps(context, *args))


def bake_map_steps(context, material, low_objects, high_objects, settings, slots, width, height):
    """Return new packed images; remove all temporary IDs even when Cycles fails."""
    if any(slot not in MAP_LABELS for slot in slots):
        raise ValueError('Unknown mesh-map type')
    if not low_objects:
        raise ValueError('No visible mesh uses this texture set in the current view layer')
    if context.mode == 'EDIT_MESH':
        raise ValueError('Leave Edit Mode before baking mesh maps')

    samples = max(1, min(int(settings.get('bake_samples', 64)), 4096))
    margin = max(0, min(int(settings.get('bake_margin', 16)), 128))
    scope = settings.get('ao_scope', 'GLOBAL')
    depsgraph = context.evaluated_depsgraph_get()
    temporary_objects, temporary_meshes, temporary_materials = [], [], []
    images = {}
    scene = None
    success = False
    try:
        scene = bpy.data.scenes.new('.TexPaint Bake')
        scene.render.engine = 'CYCLES'
        scene.cycles.samples = samples
        scene.cycles.use_adaptive_sampling = False
        scene.cycles.use_denoising = False
        scene.cycles.seed = 0
        scene.render.threads_mode = 'FIXED'
        scene.render.threads = min(8, max(1, context.scene.render.threads))
        prefs = context.preferences.addons['cycles'].preferences
        gpu = prefs.compute_device_type != 'NONE' and any(
            d.use and d.type != 'CPU' for d in prefs.devices)
        device = settings.get('bake_device', 'AUTO')
        if device == 'GPU' and not gpu:
            raise ValueError('Enable a Cycles GPU device in Preferences > System, or choose CPU')
        scene.cycles.device = 'GPU' if device != 'CPU' and gpu else 'CPU'

        originals = list(dict.fromkeys(low_objects + high_objects))
        if scope == 'GLOBAL':
            originals.extend(o for o in context.view_layer.objects
                             if o.type == 'MESH' and o.visible_get(view_layer=context.view_layer)
                             and not o.hide_render and o not in originals)
        copies = {}
        receivers = []
        material_info = []
        for part, original in enumerate(originals):
            evaluated = original.evaluated_get(depsgraph)
            mesh = bpy.data.meshes.new_from_object(
                evaluated, preserve_all_data_layers=True, depsgraph=depsgraph)
            temporary_meshes.append(mesh)
            ob = bpy.data.objects.new('.TexPaint Bake ' + original.name, mesh)
            temporary_objects.append(ob)
            scene.collection.objects.link(ob)
            ob.matrix_world = evaluated.matrix_world.copy()
            copies[original] = ob
            original_slots = [s.material for s in original.material_slots]
            if original in low_objects:
                if not mesh.uv_layers or not mesh.polygons:
                    raise ValueError(f"'{original.name}' needs a UV map and faces before baking")
                # Match the UV layer used by texture painting, even if a different render UV was saved.
                mesh.uv_layers.active.active_render = True
                receivers.append(ob)
            mesh.materials.clear()
            for index in range(max(1, len(original_slots))):
                mat = bpy.data.materials.new('.TexPaint Bake Material')
                temporary_materials.append(mat)
                mat.use_nodes = True
                mesh.materials.append(mat)
                is_target = original in low_objects and index < len(original_slots) and original_slots[index] == material
                material_info.append((mat, _id_color(original, part, index), is_target))

        view_layer = scene.view_layers[0]
        view_layer.update()
        corners = [ob.matrix_world @ Vector(corner) for ob in receivers for corner in ob.bound_box]
        bounds_min = tuple(min(p[i] for p in corners) for i in range(3))
        bounds_span = tuple(max(max(p[i] for p in corners) - bounds_min[i], 1e-5) for i in range(3))
        distance = max(max(bounds_span) * float(settings.get('ao_dist', .65)), 1e-5)
        # Non-target material faces bake into a disposable image instead of contaminating the texture set.
        scratch = bpy.data.images.new('.TexPaint Bake Scratch', width=width, height=height, alpha=True)
        images['_scratch'] = scratch
        source_copies = [copies[ob] for ob in high_objects]
        for map_index, slot in enumerate(slots):
            image = bpy.data.images.new(material.name + '_' + MAP_LABELS[slot],
                                        width=width, height=height, alpha=True, float_buffer=True)
            images[slot] = image
            # IDs are numeric data too. Packed float images use linear EXR; tagging them
            # sRGB would decode their values a second time when the blend file is reopened.
            image.colorspace_settings.name = 'Non-Color'
            image.generated_color = (.5, .5, 1, 0) if slot == 'normal' else (0, 0, 0, 0)
            for mat, color, is_target in material_info:
                _shader(mat, slot, image if is_target else scratch, color,
                        bounds_min, bounds_span, distance, scope)
            for index, receiver in enumerate(receivers):
                for ob in scene.objects:
                    ob.select_set(False, view_layer=view_layer)
                selected = source_copies + [receiver]
                for ob in selected:
                    ob.select_set(True, view_layer=view_layer)
                view_layer.objects.active = receiver
                # The low-poly receiver must not occlude its high-poly sources during projection.
                for ob in receivers:
                    ob.visible_shadow = not bool(source_copies)
                yield BakeStep(scene, view_layer, receiver, selected, dict(
                        type='NORMAL' if slot == 'normal' else 'EMIT',
                        normal_space='TANGENT', target='IMAGE_TEXTURES',
                        use_selected_to_active=bool(source_copies), use_clear=index == 0,
                        use_cage=False, cage_extrusion=float(settings.get('cage_dist', .08)),
                        max_ray_distance=float(settings.get('cage_dist', .08)) * 2.5,
                        margin=margin, margin_type='EXTEND',
                        uv_layer=receiver.data.uv_layers.active.name),
                    f'{MAP_LABELS[slot]} ({map_index + 1}/{len(slots)})',
                    map_index * len(receivers) + index, len(slots) * len(receivers))
            image.update()
            image.pack()
            # Slots store image names in JSON, which do not count as datablock users.
            image.use_fake_user = True
            image['tex_paint_bake_engine'] = 'CYCLES'
            image['tex_paint_bake_samples'] = samples
            image['tex_paint_bake_map'] = slot
        success = True
        return {slot: images[slot] for slot in slots}
    finally:
        for ob in temporary_objects:
            bpy.data.objects.remove(ob, do_unlink=True)
        for mesh in temporary_meshes:
            bpy.data.meshes.remove(mesh)
        for mat in temporary_materials:
            bpy.data.materials.remove(mat)
        if scene is not None:
            bpy.data.scenes.remove(scene)
        for slot, image in images.items():
            if not success or slot == '_scratch':
                bpy.data.images.remove(image)
