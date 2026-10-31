# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Native-window bake progress, cancellation, lifecycle and publication regression."""

import argparse
import json
import sys
import time
import traceback
from pathlib import Path

import bpy
import numpy as np
from bl_ui import properties_tex_paint_layers as paint
from bl_ui import tex_paint_baking as baker


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
          'checks': [], 'progress': [], 'passed': False}


def check(name, condition):
    result = bool(condition)
    report['checks'].append({'name': name, 'passed': result})
    print('MESH_MAP_PROGRESS_CHECK', name, result, flush=True)
    assert result, name


def op(operator, execution='EXEC_DEFAULT', **kwargs):
    region = next(r for r in area.regions if r.type == 'WINDOW')
    with bpy.context.temp_override(window=window, area=area, region=region):
        return operator(execution, **kwargs)


def idle():
    return paint._MESH_MAP_BAKE_JOB is None


def wait_for(predicate, timeout=90):
    start = time.monotonic()
    while not predicate():
        assert time.monotonic() - start < timeout, 'Timed out waiting for bake'
        yield .1


def clean():
    return not any(x.name.startswith('.TexPaint Bake') for group in (
        bpy.data.objects, bpy.data.scenes, bpy.data.materials, bpy.data.images) for x in group)


def workflow():
    yield 1
    material = bpy.data.materials.new('Progress Test')
    material.use_nodes = True
    ob = bpy.context.object
    ob.data.materials.clear()
    ob.data.materials.append(material)
    node = material.node_tree.nodes.new('ShaderNodeTexPaint')
    material.node_tree.nodes.active = node
    with bpy.context.temp_override(node=node):
        bpy.ops.node.tex_paint_canvas_new(name='Progress Canvas', width=256, height=256, color=(1, 1, 1, 1))
    node.tex_paint_bake_samples = 16
    node.tex_paint_bake_device = 'CPU'
    node.tex_paint_bake_apply_normal = False
    node.tex_paint_use_baked_mesh_maps = False
    op(bpy.ops.node.tex_paint_bake_mesh_maps, map_type='ao')
    old_maps = paint._get_mesh_maps_state(node)['maps'].copy()
    old_image = bpy.data.images[old_maps['ao']]
    old_pixels = np.array(old_image.pixels[:])
    original_scene = bpy.context.scene
    image_area = next(a for a in window.screen.areas if a.type == 'DOPESHEET_EDITOR')
    image_area.type = 'IMAGE_EDITOR'
    image_area.spaces.active.image = node.image
    area.spaces.active.show_region_ui = True
    op(bpy.ops.object.mode_set, mode='TEXTURE_PAINT')
    yield .5
    for region in area.regions:
        if region.type == 'UI':
            region.active_panel_category = 'Tool'
    bpy.context.window_manager.tex_paint_show_mesh_maps_hub = True
    for method in ('button', 'escape', 'save'):
        node.tex_paint_bake_res = '1024'
        node.tex_paint_bake_samples = 512
        check(method + ' bake starts asynchronously', op(
            bpy.ops.node.tex_paint_bake_mesh_maps, 'INVOKE_DEFAULT', map_type='ao') == {'RUNNING_MODAL'})
        yield from wait_for(lambda: paint._MESH_MAP_BAKE_JOB is not None and
                            paint._MESH_MAP_BAKE_JOB.progress > .01)
        job = paint._MESH_MAP_BAKE_JOB
        check(method + ' reports native partial progress', 0 < job.progress < 1)
        check(method + ' prevents another bake', not bpy.ops.node.tex_paint_bake_mesh_maps.poll())
        check(method + ' keeps original scene active', bpy.context.scene == original_scene)
        if method == 'button':
            first = job.progress
            yield from wait_for(lambda: job.progress > first or idle())
            check('progress advances within a single map', first < job.progress < 1)
            report['progress'].extend([first, job.progress])
            op(bpy.ops.screen.screenshot, filepath=str(args.output / 'bake-progress.png'))
            check('Cancel button executes during the bake', op(bpy.ops.node.tex_paint_bake_cancel) == {'FINISHED'})
        elif method == 'escape':
            window.event_simulate(type='ESC', value='PRESS', x=area.x + 40, y=area.y + 40)
            window.event_simulate(type='ESC', value='RELEASE', x=area.x + 40, y=area.y + 40)
        else:
            op(bpy.ops.wm.save_as_mainfile, filepath=str(args.output / 'cancel-on-save.blend'))
        yield from wait_for(idle)
        yield .3
        check(method + ' preserves previous map assignment', paint._get_mesh_maps_state(node)['maps'] == old_maps)
        check(method + ' preserves previous image pixels', np.array_equal(old_pixels, np.array(old_image.pixels[:])))
        check(method + ' removes temporary data and releases bake guard', clean() and not paint._INTERNAL_REPROJECTING)
        check(method + ' preserves existing Image Editor', image_area.spaces.active.image == node.image)
    node.tex_paint_bake_res = 'MATCH'
    node.tex_paint_bake_samples = 16
    op(bpy.ops.ed.undo_push, message='Before mesh-map bake')
    observed_steps = set()
    started_steps = []
    completed_steps = []
    fractions = []
    original_run = baker.BakeStep.run

    def observed_run(step, context, execution='EXEC_DEFAULT'):
        # Fast maps can start and finish between the test's UI timer callbacks.
        # Observe real native dispatches without delaying or replacing the baker.
        job = paint._MESH_MAP_BAKE_JOB
        fractions.append(job.progress)
        result = original_run(step, context, execution)
        started_steps.append({'index': step.index, 'total': step.total,
                              'label': step.label, 'execution': execution, 'result': sorted(result)})
        return result

    def observed_complete(receiver, *_args):
        job = paint._MESH_MAP_BAKE_JOB
        if job is not None and job._step is not None and job._step.receiver == receiver:
            completed_steps.append(job._step.index)

    baker.BakeStep.run = observed_run
    bpy.app.handlers.object_bake_complete.append(observed_complete)
    try:
        check('all-map asynchronous bake starts', op(
            bpy.ops.node.tex_paint_bake_mesh_maps, 'INVOKE_DEFAULT', map_type='ALL') == {'RUNNING_MODAL'})
        start = time.monotonic()
        while not idle():
            job = paint._MESH_MAP_BAKE_JOB
            fractions.append(job.progress)
            if job._step is not None:
                observed_steps.add(job._step.index)
            assert time.monotonic() - start < 120
            yield .1
    finally:
        baker.BakeStep.run = original_run
        bpy.app.handlers.object_bake_complete.remove(observed_complete)
        report['all_map_dispatches'] = started_steps
        report['all_map_completions'] = completed_steps
        report['all_map_polled_steps'] = sorted(observed_steps)
        report['all_map_progress'] = fractions
    check('all seven native map jobs start once in order',
          [step['index'] for step in started_steps] == list(range(7)) and
          all(step['total'] == 7 and step['execution'] == 'INVOKE_DEFAULT' and
              step['result'] == ['RUNNING_MODAL'] for step in started_steps))
    check('all seven native map jobs complete once in order', completed_steps == list(range(7)))
    check('overall progress never moves backwards', all(a <= b for a, b in zip(fractions, fractions[1:])))
    maps = paint._get_mesh_maps_state(node)['maps']
    check('all seven maps publish with matching packed image IDs', set(maps) == set(baker.MAP_LABELS) and
          all(bpy.data.images.get(name) and bpy.data.images[name].packed_file and
              bpy.data.images[name].get('tex_paint_bake_map') == slot for slot, name in maps.items()))
    check('asynchronous AO matches synchronous AO exactly',
          np.array_equal(old_pixels, np.array(bpy.data.images[maps['ao']].pixels[:])))
    check('successful bake removes temporary data', clean() and not paint._INTERNAL_REPROJECTING)
    check('completed bake preserves existing Image Editor', image_area.spaces.active.image == node.image)
    check('completion releases bake button', bpy.ops.node.tex_paint_bake_mesh_maps.poll())
    final_maps = dict(maps)
    node_name = node.name
    check('completed bake supports undo', op(bpy.ops.ed.undo) == {'FINISHED'})
    node = bpy.data.materials['Progress Test'].node_tree.nodes.get(node_name)
    check('one undo restores previous map assignments', paint._get_mesh_maps_state(node)['maps'] == old_maps)
    check('completed bake supports redo', op(bpy.ops.ed.redo) == {'FINISHED'})
    node = bpy.data.materials['Progress Test'].node_tree.nodes.get(node_name)
    check('redo restores completed maps', paint._get_mesh_maps_state(node)['maps'] == final_maps)

    original_run = baker.BakeStep.run

    def missing_uv(step, context, execution='EXEC_DEFAULT'):
        step.receiver.data.uv_layers.remove(step.receiver.data.uv_layers.active)
        return original_run(step, context, execution)

    baker.BakeStep.run = missing_uv
    try:
        check('native failure starts asynchronously', op(
            bpy.ops.node.tex_paint_bake_mesh_maps, 'INVOKE_DEFAULT', map_type='ao') == {'RUNNING_MODAL'})
        yield from wait_for(idle)
    finally:
        baker.BakeStep.run = original_run
    check('native bake failure retains prior maps', paint._get_mesh_maps_state(node)['maps'] == final_maps)
    check('native bake failure removes temporary data', clean() and not paint._INTERNAL_REPROJECTING)
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
    paint._mesh_map_bake_abort()
    (args.output / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    bpy.ops.wm.quit_blender()
    return None


bpy.app.timers.register(tick, first_interval=1)
