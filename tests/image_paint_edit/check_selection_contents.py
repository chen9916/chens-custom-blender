# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Repeated selection transforms must not pick up destination pixels."""
import argparse
import json
import math
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
bpy.context.preferences.view.show_splash = False
bpy.context.preferences.edit.undo_steps = 64
window = bpy.context.window
area = next(a for a in window.screen.areas if a.type == 'VIEW_3D')
area.type = 'IMAGE_EDITOR'
area.spaces.active.ui_mode = 'PAINT'
tree = bpy.data.node_groups.new('Selection Contents', 'ShaderNodeTree')
tree.use_fake_user = True
canvas = tree.nodes.new('ShaderNodeTexPaint')
canvas.name = 'Canvas'
with bpy.context.temp_override(node=canvas):
    bpy.ops.node.tex_paint_canvas_new(name='Selection Contents', width=32, height=32,
                                    color=(0, 0, 0, 0), **{'float': True})
area.spaces.active.image = canvas.image
report = {'passed': False, 'checks': [], 'build_hash': bpy.app.build_hash.decode(),
          'build_date': bpy.app.build_date.decode(), 'build_time': bpy.app.build_time.decode(),
          'runtime': bpy.app.binary_path}

def node():
    return bpy.data.node_groups['Selection Contents'].nodes['Canvas']

def region():
    return next(r for r in area.regions if r.type == 'WINDOW')

def op(operator, *pos, **kw):
    with bpy.context.temp_override(window=window, area=area, region=region()):
        return operator(*pos, **kw)

def pixels():
    p = np.empty(32 * 32 * 4, dtype=np.float32)
    node().paint_pixels.foreach_get(p)
    return p.reshape(32, 32, 4)

def same_visible_pixels(a, b, tolerance=2e-5):
    # Tiny interpolation residuals can have arbitrary straight RGB at nearly zero alpha.
    a, b = a.copy(), b.copy()
    a[..., :3] *= a[..., 3:4]
    b[..., :3] *= b[..., 3:4]
    return np.allclose(a, b, atol=tolerance)

def check(name, condition):
    report['checks'].append({'name': name, 'passed': bool(condition)})
    print('SELECTION_CONTENT_CHECK', name, bool(condition), flush=True)
    assert condition, name

selection = np.zeros((32, 32), dtype=np.float32)
selection[8:14, 2:8] = 1
source = np.zeros((32, 32, 4), dtype=np.float32)
source[8:12, 2:6] = (1, 0, 0, .5)
source[8:14, 10:16] = (0, 0, 1, 1)
source[8:14, 22:26] = (0, 1, 0, 1)

def baseline():
    node().paint_target = 'COLOR'
    node().paint_pixels.foreach_set(source.ravel())
    node().selection_mask.foreach_set(selection.ravel())
    op(bpy.ops.ed.undo_push, message='Selected red patch')

def expected(dx):
    base = source.copy()
    base[..., :3] *= base[..., 3:4]
    lifted = base * selection[..., None]
    base *= (1 - selection[..., None])
    moved = np.zeros_like(lifted)
    if 0 <= dx < 32:
        moved[:, dx:] = lifted[:, :32-dx]
    elif -32 < dx < 0:
        moved[:, :dx] = lifted[:, -dx:]
    out = moved + base * (1 - moved[..., 3:4])
    np.divide(out[..., :3], out[..., 3:4], out=out[..., :3], where=out[..., 3:4] > 0)
    return out

def key(code, unicode=''):
    r = region()
    x, y = r.x + r.width // 2, r.y + r.height // 2
    extra = {'unicode': unicode} if unicode else {}
    window.event_simulate(type=code, value='PRESS', x=x, y=y, **extra)
    window.event_simulate(type=code, value='RELEASE', x=x, y=y)

def move_eight(confirm=True):
    key('G')
    yield .15
    key('X')
    key('EIGHT', '8')
    yield .2
    key('RET' if confirm else 'ESC')
    yield .25

def transform(dx=0, angle=0, scale=(1, 1)):
    return op(bpy.ops.image.paint_transform, 'EXEC_DEFAULT', True,
              translation=(dx, 0), angle=angle, scale=scale)

def workflow():
    global window, area
    yield 1
    baseline()
    op(bpy.ops.image.view_all, fit_view=True)
    yield .5
    yield from move_eight()
    check('first move composites selected red over blue', np.allclose(pixels(), expected(8), atol=1e-5))
    yield from move_eight()
    check('second move restores originally unselected blue', np.allclose(pixels()[:, 10:16], source[:, 10:16], atol=1e-5))
    check('second move retains only original selection contents', np.allclose(pixels(), expected(16), atol=1e-5))
    op(bpy.ops.ed.undo)
    check('undo restores first placement', np.allclose(pixels(), expected(8), atol=1e-5))
    op(bpy.ops.ed.redo)
    check('redo restores second placement', np.allclose(pixels(), expected(16), atol=1e-5))
    transform(-8)
    check('move after redo still excludes background', np.allclose(pixels(), expected(8), atol=1e-5))
    yield from move_eight(confirm=False)
    check('cancel preserves lifted contents and placement', np.allclose(pixels(), expected(8), atol=1e-5))
    yield from move_eight()
    check('move after cancel still excludes background', np.allclose(pixels(), expected(16), atol=1e-5))

    for angle, scale in [(math.pi/4, (1, 1)), (0, (2, 1)), (-math.pi/4, (1, 1)),
                         (math.pi/4, (1, 1)), (0, (.5, 1)), (-math.pi/4, (1, 1))]:
        transform(angle=angle, scale=scale)
    check('mixed rotation and nonuniform scale retain immutable contents', same_visible_pixels(pixels(), expected(16)))
    transform(-16)
    check('returning to origin restores original image', same_visible_pixels(pixels(), source))

    baseline()
    transform(40)
    check('selection can move fully off canvas', np.allclose(pixels(), expected(40), atol=1e-5))
    transform(-32)
    check('off-canvas contents return without losing pixels', np.allclose(pixels(), expected(8), atol=1e-5))
    fixture = args.output / 'lifted-selection.blend'
    op(bpy.ops.wm.save_as_mainfile, filepath=str(fixture), check_existing=False)
    bpy.ops.wm.open_mainfile(filepath=str(fixture), load_ui=False)
    window = bpy.context.window_manager.windows[0]
    area = next(a for a in window.screen.areas if a.type == 'IMAGE_EDITOR')
    yield .5
    transform(8)
    check('save-load retains selected contents separately from background', np.allclose(pixels(), expected(16), atol=1e-5))

    baseline()
    transform(8)
    node().selection_mask.foreach_set(np.roll(selection, 8, axis=1).ravel())
    transform(8)
    check('explicit new selection starts from current pixels', pixels()[9, 12, 3] == 0)

    baseline()
    transform(8)
    edited = pixels()
    edited[25, 25] = (1, 1, 0, 1)
    node().paint_pixels.foreach_set(edited.ravel())
    transform(8)
    check('intervening pixel edits remain intact', np.allclose(pixels()[25, 25], (1, 1, 0, 1)))
    check('intervening pixel edits establish a new transform source', pixels()[9, 12, 3] == 0)

    op(bpy.ops.node.tex_paint_layer_mask_init, 'EXEC_DEFAULT', True, mode='WHITE')
    node().paint_target = 'MASK'
    gray = np.full((32, 32, 4), .2, dtype=np.float32)
    gray[..., 3] = 1
    gray[8:14, 2:8, :3] = .9
    gray[8:14, 10:16, :3] = .6
    node().paint_pixels.foreach_set(gray.ravel())
    node().selection_mask.foreach_set(selection.ravel())
    transform(8)
    transform(8)
    check('repeated mask transforms preserve underlying mask pixels', np.allclose(pixels()[8:14, 10:16], gray[8:14, 10:16]))
    check('mask cutout remains opaque black', np.allclose(pixels()[8:14, 2:8, :3], 0) and np.allclose(pixels()[..., 3], 1))

    baseline()
    node().selection_active = False
    transform(40)
    fixture = args.output / 'whole-layer-transform.blend'
    op(bpy.ops.wm.save_as_mainfile, filepath=str(fixture), check_existing=False)
    bpy.ops.wm.open_mainfile(filepath=str(fixture), load_ui=False)
    window = bpy.context.window_manager.windows[0]
    area = next(a for a in window.screen.areas if a.type == 'IMAGE_EDITOR')
    yield .5
    transform(-40)
    check('unrestricted whole-layer source survives save-load', np.allclose(pixels(), source, atol=1e-5))
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
    print('SELECTION_CONTENT_RESULT', json.dumps(report), flush=True)
    bpy.ops.wm.quit_blender()
    return None
bpy.app.timers.register(tick, first_interval=1, persistent=True)
