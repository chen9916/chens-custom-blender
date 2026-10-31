Native image-edit regression checks
===================================

Procedural generator and viewport blend matrix
----------------------------------------------

``check_procedural_mask_matrix.py`` runs with ``--background --factory-startup``
and ``--output DIR``. It checks AO, Curvature, Cavity, Edge Wear, Normal Up and
Position Z from mesh generation and deterministic baked maps, byte/float canvases,
eight effect blends, inversion and opacity. Independent arithmetic expectations
cover 1,152 generator combinations and Paint coverage; additional cases cover
callback rollback, missing UVs, explicit map refresh, save/reopen and zero opacity.

``check_mask_blends_viewport.py`` runs in a disposable native window with
``--factory-startup --disable-autoexec --enable-event-simulate --python-exit-code 1``
and ``--output DIR``. It exercises the six generators plus projected grunge,
eight mask blends, 22 standard brush blends and 14 color-layer blends. It reads
the displayed image and selected Paint source during held strokes and Ctrl erase,
checks source isolation and toggles, and captures held-stroke screenshots. Non-Mix
brush modes receive existing coverage to exercise their normal Blender semantics.
The legacy absorption timer is disabled so no idle repair can hide a native
publication failure. Native mesh-generator strokes also run on a sloped receiver.

When the artist is running the default texture-paint executable, a fresh install
can be built without replacing it::

  $env:BLENDER_BUILD_PROFILE = 'tex-paint-layers'
  $env:BLENDER_TEX_PAINT_INSTALL_DIR = 'E:/dev/blender/blender/build_tex_paint_layers_ninja/bin-mask-matrix-dev'
  & E:/dev/blender/blender/mesh-onion-live-dev/build.cmd 4 install

The wrapper reconfigures when the explicit install prefix changes. Omitting that
override restores the normal ``bin-selection-dev`` destination on the next build.
The existing single-build, four-job and one-link-job limits still apply.

Mask/color projection isolation, 2026-10-31
------------------------------------------

Run ``check_mask_projection_isolation.py`` with ``--background --factory-startup``
and ``--output DIR``. Generated fixtures cover solid Fill, textured Fill and Paint
on byte/float canvases, UV and triplanar mask projection, independent color/mask
transforms, callback failure restoration, lower-layer visibility, and save/reopen.
UV-gutter cases use a nonidentity tone curve to detect feedback from the evaluated
mask into the next raw projection, including preview refinement and cancellation.

The Python-only follow-up to runtime ``4c880e7b7573`` (``13:16:45`` UTC) passed
216 checks from this suite and 20 existing native-window mask-paint checks. A
disposable copy of the reported artist scene passed 30 additional native-window
checks, including G/R/S cancel, actual free-move gizmo dragging and cancel, and
confirmed handle undo/redo. The report, script hash, copied fixtures and inspected
screenshots are under
``mesh-onion-live-dev/image-edit-validation/live-mask-color-leak/``.
The executable was unchanged; the installed UI module and the open artist
instance received the tested Python update. See ``summary.json`` there for exact
source/module hashes and evidence paths.

Native viewport/mask validation, 2026-10-30
------------------------------------------

The installed runtime reports ``4c880e7b7573``, build time ``11:46:35`` UTC,
with modified source and file subversion ``503.28``. The evidence is in
``E:/dev/blender/blender/mesh-onion-live-dev/image-edit-validation/native-paint-viewport/summary.json``.
It records the executable/source hashes and the build time for each suite.
All 32 native tests pass. The final executable passes 122 focused integration
checks for live Multiply/Ctrl strokes, blur partial/full equivalence, native mask
coverage transforms and the general canvas workflow. Another 117 integration
checks passed before the final blur-precision and transform corrections,
covering mask storage/migration, AO, adjustment filters, repeated undo and
reopened painting. These total 271 checks across the recorded suites.

The final 3D effect screenshot shows the Channels inspector's Layer Effects
stack, ordered Levels/Blur, strength and radius controls. Spatial mask filters
still require a full-mask evaluation once per dab; this validation does not
claim a measured frame rate or end-to-end display latency.

Running the checks
------------------

Use the active ``blender-tex-paint-layers`` worktree. Build only through the
workspace wrapper, one build at a time, at most four Ninja jobs::

  $env:BLENDER_BUILD_PROFILE = 'tex-paint-layers'
  & E:/dev/blender/blender/mesh-onion-live-dev/build.cmd 4 install
  & E:/dev/blender/blender/mesh-onion-live-dev/build.cmd 4 blender_test

The profile retains the CUDA/OptiX settings and installs into the separate
``build_tex_paint_layers_ninja/bin-selection-dev`` directory. It does not replace
an existing artist package or change preferences.

Run ``bin/tests/blender_test.exe --gtest_filter=TexPaintLayerTest.*:TexPaintMaskTest.*`` from the build
tree with the installed runtime, its ``blender.shared``, and its ``blender.crt``
directories on PATH for DLL dependencies. The tests cover
canvas storage/composition, target guards, empty selection, independent copies,
wand connectivity, polygon rasterization, premultiplied transforms, HDR/alpha
identity, and restricted update rectangles.

Run ``check_native_mask_storage.py`` with ``--background --factory-startup``
and ``--output DIR`` for byte/float canvas save/reopen checks, including very low
paint coverage and native reevaluation without Python caches. Run
``check_native_mask_legacy.py`` with the same flags and ``--fixture FILE`` using
a disposable pre-503.28 mask-stack file to verify migration of stored paint
values and coverage. Neither test overwrites the input fixture.

For operator integration, launch a disposable native window::

  blender.exe --factory-startup --disable-autoexec --enable-event-simulate \
    --python-exit-code 1 --python tests/image_paint_edit/check_workflow.py \
    -- --output E:/dev/blender/blender/mesh-onion-live-dev/image-edit-validation

The script creates its own small material canvas, drives toolbar gestures and
G/R/S via real window events, checks selection/transform/paint undo and redo,
cancel, target isolation, and save/load. It writes a JSON report with the actual
runtime build hash, a screenshot, and a disposable blend fixture, then quits.
It also changes the disposable area to a 3D viewport and checks that the same
coverage restricts projected painting on the material's canvas.
Require ``report.json`` to contain ``passed: true``; a clean process exit alone
is insufficient. Inspect the runtime log for Python registration/draw errors and
inspect the screenshot for toolbar/header/selection visibility.

Run ``check_undo_chain.py`` with the same disposable-window command and a separate
output directory to exercise three consecutive brush strokes followed by transform
and color-adjustment edits. It checks each undo/redo state and repeated Main
replacement. The former executable crashed at ``UNDO_CHAIN_REPLAY_BEGIN``; the
corrected undo implementation resolves every replayed image handle.

Run ``check_color_filters.py`` the same way (including ``--enable-event-simulate``)
to check native filter creation, bypass, strength, ordering, duplication, removal,
source/alpha/mask preservation, undo/redo, save/load, and the visible filter UI.
The native suite additionally checks HDR identity, owned copies, and merging
filters exactly once.

Run ``check_layer_effects_3d.py`` with the same disposable-window flags and its own
``--output`` directory to check the 3D Layer Effects stack. It validates Blur
coverage and transparent-edge color, bypass, adjustment ordering, independent
effect duplication, reorder undo/redo, actual 3D painting through effects, the
live dirty halo against a forced full composite, and packed file persistence.
Inspect ``layer-effects-3d.png`` and require the JSON report to pass. Native
``TexPaintLayerTest`` blur cases additionally exercise premultiplied HDR values,
source/mask isolation, multiple spatial effects, and below-active cache updates.
Color-layer Blur retains a support-expanded partial update. Spatial filters in the
mask-effect stack currently use a full-canvas evaluation fallback; pointwise mask
effects are checked with dirty-rectangle updates.

Run ``check_selection_contents.py`` with the same disposable-window command and
its own output directory. It reproduces moving a semi-transparent selection over
unselected pixels, confirming, then moving again. It checks background restoration,
transparent holes, repeated G events, undo/redo, cancel, combined rotations and
nonuniform scales, moving fully off canvas and back, save/load, explicit selection
changes, intervening pixel edits, and mask transforms. The native tests also check
independent ownership and release of the retained source. Require ``passed: true``.

For the procedural AO mask regression, run a disposable background process::

  blender.exe --background --factory-startup --disable-autoexec \
    --python-exit-code 1 --python tests/image_paint_edit/check_ao_mask.py \
    -- --output E:/dev/blender/blender/mesh-onion-live-dev/image-edit-validation/ao-mask

Require ``report.json`` to contain ``passed: true``. This checks UV coverage and
barycentric interpolation for small islands, reversed winding, clipped islands,
single-row/column grids, the large-triangle fallback, degenerate UVs, and more
than 4096 small triangles. The operator fixture creates an AO mask on a subdivided
receiver beside an occluder, checks the nearby-object contribution and Global
Scene AO toggle, and creates Cavity and Edge Wear masks through the shared path.
The report records the loaded Python module's SHA-256 as well as the executable's
build hash. These are computation/operator checks, not viewport validation.

For Cycles mesh-map baking, run ``check_mesh_baking.py`` in a disposable background
process with ``--output`` and optionally ``--model E:/dev/blender/blender/test_model/MeetMat.obj``.
It covers scene/material/canvas/mask isolation, distinct AO scopes, shared atlas
accumulation, evaluated modifiers, selected-to-active normal transfer, failure
cleanup, and pending mask strokes. The optional OBJ import bakes all seven maps
on all three parts at 1024 pixels and 128 samples, exports raw PNG inspection
copies, and checks all packed float values after save/reopen. The original OBJ
is read only. Require ``report.json`` to contain ``passed: true``.

Run ``check_mesh_map_preview.py`` in a disposable native window with the same
window flags as ``check_workflow.py``. It checks the Cycles operator and controls,
opens the preview menu, opens a dedicated image window without an existing Image
Editor, switches all seven maps, closes/reopens the window, and preserves Texture
Paint mode. Inspect ``baker-controls.png`` and ``map-preview.png`` and require the
JSON report to pass. See ``doc/tex_paint_mesh_baking.md`` for map semantics.

Run ``check_mesh_map_progress.py`` in a separate disposable native window with
``--factory-startup --disable-autoexec --enable-event-simulate --python-exit-code 1``
and ``--output``. It checks live within-map progress, all-seven-map sequencing,
monotonic overall progress, Cancel/Esc/save cancellation, previous-map retention,
temporary data cleanup, Image Editor preservation, undo/redo, native failure
callbacks, and exact equality with the synchronous AO result.
Require ``report.json`` to pass and inspect ``bake-progress.png``.

Run ``check_mask_paint_stack.py`` with ``--background --factory-startup`` and an
``--output`` directory for native fill-layer mask effects on byte/float canvases.
It writes independent Paint RGBA value/coverage, reads the final mask through
``layer.mask_pixels``, and checks generated output, visibility, reload handling,
Levels evaluation, and companion PBR stack copies. Save checks verify that native
Paint arrays are not duplicated in JSON, pending Python edits are published, and
stale pointer/revision caches cannot overwrite newer native strokes.
Run ``check_mask_paint_3d.py`` in a disposable native window with
``--factory-startup --disable-autoexec --enable-event-simulate --python-exit-code 1``
and ``--output``. It paints real strokes on a UV plane with a generated mask and
two Paint effects, checks native per-effect coverage, Image/Material paint modes,
visibility, undo/redo, save/reopen, and the first stroke after reload. Require
both JSON reports to pass.

Run ``check_native_mask_strokes_3d.py`` in a separate disposable native window
with the same flags and its own ``--output`` directory. It disables the legacy
absorption timer, checks a live projected Multiply mask stroke against the native
blend formula, Ctrl erasure revealing the generator, independent Paint effects,
undo/redo, and save/reopen. It also paints an ordinary color Paint layer in
Multiply over a lower Fill layer and checks that Ctrl erasing restores the Fill.
Require ``report.json`` to contain ``passed: true``. This regression passed on the
2026-10-30 11:46:35 development runtime; rerun it for each integrated candidate.

Production hardening checks
---------------------------

The current hardening procedure is tracked in ``doc/tex_paint_readiness.md``.
Run ``TexPaintLayerTest.*:TexPaintMaskTest.*:TexPaintFlattenTest.*`` with the native
test executable. The disabled ``TexPaintMaskTest.DISABLED_BenchmarkSpatialFilterRegions``
requires ``--gtest_also_run_disabled_tests`` and its exact filter. It prints 2K/4K
local/full evaluation wall times and pixel work counters; these exclude projected
brush input, other layers, GPU upload/completion, and display presentation.

Run these disposable background scripts with ``--factory-startup --disable-autoexec
--python-exit-code 1`` and ``-- --output DIR``:

* ``check_layer_pixel_actions.py``: shared-copy isolation, byte/data/HDR color
  actions, alpha, target restoration, and editability guards.
* ``check_mask_copy_and_material_split.py``: native/plain mask copies, authoring
  settings, cold caches, shared-mesh material isolation, and save/reopen.
* ``check_layer_lifecycle.py``: duplicate authoring, matching companion merges,
  rejection before mutation, mask reset, and canvas UID reuse.
* ``check_layer_rasterize.py``: native Fill conversion with masks/effects, byte and
  projected float HDR data, all-channel preflight, and save/reopen.
* ``check_layer_flatten.py``: visible/all merges, hidden-layer retention, material
  channels, mask-preview isolation, and unsupported-channel rejection.
* ``check_native_mask_legacy.py``: add ``--fixture LEGACY_BLEND`` using a disposable
  pre-native file containing a painted mask. Checks migration and a second native
  save/reopen, while verifying that the original fixture was not changed.

Run ``check_paint_sidebar.py`` in a disposable native window with
``--enable-event-simulate``. It checks narrow/wide layouts, read-only material
inspection, explicit reconnection/undo, and real rejected strokes for a generator,
filter, and disabled Paint effect while other Paint effects exist. Inspect its
screenshots as well as the JSON report. Run ``check_layer_transactions.py`` with
the same native-window flags for layer-operation undo/redo and persistence.

The workspace helper ``mesh-onion-live-dev/test-paint-readiness.ps1`` runs the
selected installed-runtime suites sequentially and refuses to reuse an existing
report directory. It records both process exit status and JSON result; neither
alone is sufficient to declare an interactive suite passed.
It also rejects Python context nesting errors in the runtime log. Consecutive
asynchronous map jobs reproduced a lost-increment race between the render worker
and UI callbacks even when all pixel assertions passed. Repeat the progress
suite in fresh disposable processes after changing Python context ownership.

Do not run the script in an artist's existing session. Do not overwrite original
assets. Record the source revision and dirty status beside the runtime hash when
testing uncommitted work. These checks do not establish large-canvas latency,
GPU completion time, full application feature parity, or all 3D paint behavior.

Recorded development validation
-------------------------------

The 2026-10-31 production-hardening candidate reports runtime hash
``4c880e7b7573`` and build time ``20:15:05`` UTC in
``build_tex_paint_layers_ninja/bin-selection-dev/blender.exe``. All 54 native
GoogleTests (``30`` in ``TexPaintLayerTest``, ``15`` in ``TexPaintMaskTest``,
``9`` in ``TexPaintFlattenTest``), 1,158 headless checks across 12 background
suites (``552`` across the 10 readiness suites plus ``216`` in
``check_mask_projection_isolation.py`` and ``390`` in
``check_procedural_mask_matrix.py``), and 314 native-window checks across 11
interactive suites (``295`` across the 10 readiness suites plus ``19`` in
``check_selection_contents.py``) passed with zero Python context nesting errors.
This pass covers standalone 2D canvas isolation across shared layer/effect UIDs
and differing canvas dimensions, atomic multi-part ``ALL_SHARED_OBJECTS``
material split rollback, ``IMAGE_MT_paint_layer`` flat-image ``Enable Layers``
conversion, and native ``NODE_OT_tex_paint_mask_paint_action`` operations. The
evidence manifest is
``mesh-onion-live-dev/image-edit-validation/production-hardening/integrated-20261031/summary.json``.

The earlier production-hardening candidate on 2026-10-30 reports runtime hash
``4c880e7b7573`` and build time ``13:16:45`` UTC. All 50 native tests, 456 background
checks, and 293 native-window checks passed. Two extra fresh-process bake-progress
runs passed 42 checks each, without the Python context error reproduced on the
preceding candidate. The workspace evidence manifest is
``mesh-onion-live-dev/image-edit-validation/production-hardening/summary.json``.
See ``doc/tex_paint_readiness.md`` for measured filter work, remaining limitations,
and the distinction between this tested development installation and a release
package. All records below describe earlier candidates.

On 2026-10-30, all 18 ``TexPaintLayerTest`` tests, 54 selection/transform workflow
checks, 22 undo-chain checks, 35 color-filter checks, and three legacy-file
migration checks passed. The layer filter inspector screenshot was inspected.
The tested runtime reports build hash ``4c880e7b7573``, build time
``2026-10-30 06:31:34``. The earlier undo crash was reproduced on the preceding
runtime before applying the image-reference fix.
The source is branch ``tex-paint-layers`` at
``44f4d2e0e99fc6bc1bcc8ad1c40d9e077c4d15d8`` plus uncommitted changes. The
embedded build hash follows the upstream tracking revision, so it does not
identify those changes by itself.

The workspace record
``mesh-onion-live-dev/image-edit-validation/filters-undo-summary.json`` contains
the executable and changed-source SHA-256 hashes and links to the native,
interactive, migration, and screenshot evidence. This is a tested development
installation, not a finalized release package.

The later Python UI update for 2D mask fills is recorded separately in
``mesh-onion-live-dev/image-edit-validation/mask-ui-2d/report.json``. Eight focused
native-window checks cover canvas defaults and scale, explicit conversion with
undo/redo, and retention of 3D material mapping. The canvas and existing-mapping
screenshots were inspected. The native executable is unchanged by this UI update.

The AO mask regression on 2026-10-30 reproduced the reported small-triangle
``IndexError`` through ``node.tex_paint_mask_effect_add`` before the fix. After
correcting the singleton-axis coordinate indexing and running the profile's
install build, all nine focused cases in ``check_ao_mask.py`` passed, including
the nearby-object contribution, Global Scene AO toggle, and shared Cavity/Edge
Wear path. The installed runtime reports build hash ``4c880e7b7573`` and build
time ``2026-10-30 07:27:03``. Evidence is in
``mesh-onion-live-dev/image-edit-validation/ao-mask/report.json`` and
``ao-mask/summary.json``; the latter records the dirty source revision and hashes.
The background regression does not establish viewport or modal behavior.

The repeated-selection fix was validated on 2026-10-30 with runtime build hash
``4c880e7b7573`` and build time ``07:54:17``. All 20 native tests, 19 retained-content
checks, 54 workflow checks, 22 undo-chain checks, 35 color-filter checks, and three
legacy-file checks passed. The preceding executable reproduced destination pixels
being picked up on the second move. A broader regression also caught a tiny
interpolation fringe on exact 90-degree rotations; the corrected build passes the
original pixel comparison. The editor and filter inspector screenshots were
inspected. Evidence and source/executable hashes are recorded in
``mesh-onion-live-dev/image-edit-validation/selection-contents-summary.json``.
These checks cover correctness on small canvases, not large-canvas performance.

The Cycles mesh-map update was validated on 2026-10-30 with runtime build hash
``4c880e7b7573`` and build time ``08:34:56``. All 39 bake/isolation/persistence
checks, 16 native preview checks, and nine live-AO regressions passed. MeetMat's
head, body, and base each produced seven 1024-pixel maps at 128 samples; all 21
packed maps retained identical numeric pixels after reopening. A separate
512-pixel, 64-sample AO comparison on the RTX 5070 Ti passed CPU/CUDA comparison
(mean absolute channel error ``7.45e-8``, maximum ``1/256``).

The map contact sheet, native controls/preview screenshots, and head AO applied
to the model were inspected. The head comparison uses a 512-pixel legacy bake
and a 1024-pixel/128-sample Cycles bake; it is quality evidence, not a timing or
equal-settings benchmark. Large dark blotches and triangle bands in the former
were replaced by continuous groove shading. Curvature remains topology-dependent
pointiness and thickness remains an inward-occlusion proxy.

Evidence, the disposable model with packed maps, source/executable hashes, and
the unchanged CUDA/OptiX configuration are recorded under
``mesh-onion-live-dev/image-edit-validation/mesh-baking/summary.json``. The OBJ
source was not modified. Runtime logs also record the OBJ's missing external MTL
and sandbox thumbnail-cache write warnings; all map exports and blend roundtrips
completed. This is a development installation, not a finalized release package.

The bake-progress update was validated on 2026-10-30 with runtime build hash
``4c880e7b7573`` and build time ``10:17:26``. All 41 native-window progress,
cancellation, editor preservation, undo/redo, and failure checks passed, as did
all 14 focused synchronous baking regressions. The progress screenshot was
inspected; it shows the map name/count, actual percentage, and Cancel button below
the baker header. Asynchronous and synchronous AO pixels matched exactly.

The test reproduced and fixed a cancellation deadlock caused by retaining the
Python lock while joining Cycles, and an asynchronous report lifetime crash when
the invoking scene override ended. Job reports now have native worker ownership,
and modal tracking uses the bake's original scene. Evidence and executable/source
hashes are in ``mesh-onion-live-dev/image-edit-validation/mesh-baking-progress/summary.json``.
The full MeetMat quality evidence above predates this progress-only update; the
new checks verify unchanged map values and interactive behavior on a small fixture.

The earlier painted-mask persistence path was validated on 2026-10-30 with runtime hash
``4c880e7b7573`` and build time ``10:46:10``. The previous installed UI reproduced
stroke loss on both byte and float masks when toggling a Paint effect. The fixed
installation passed 16 mask-stack checks, 16 native 3D paint checks, all 54 existing
painting workflow checks, and nine AO regressions. Native-window checks include
actual narrow strokes that entirely miss the old 1024-point sample grid, separate
Paint effects, generator/layer visibility, undo/redo, save/reopen, and new strokes
after reopening. These results cover the former deferred Python capture path and
do not validate the current native per-effect stroke routing. The 3D screenshot
was inspected. Evidence and source/executable hashes are recorded in
``mesh-onion-live-dev/image-edit-validation/mask-paint-stack/summary.json``.
