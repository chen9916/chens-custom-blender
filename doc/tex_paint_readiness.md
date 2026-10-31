# Texture painting readiness

This is a development hardening record for the `tex-paint-layers` worktree, not a
release certification. The installed executable, installed Python modules, source
revision, dirty-source hashes, and report results must be recorded together.
Blender's build information uses the upstream tracking revision on this branch;
the embedded hash alone does not identify an uncommitted development build.

## Acceptance areas

| Area | Required evidence |
| --- | --- |
| Brush routing | Actual 3D strokes address the selected Paint effect; generator/filter/disabled/locked targets cannot redirect painting. Ctrl erasure reveals the lower mask or color layer in Multiply mode. |
| Persistence | Independent Paint values and coverage survive undo, save/reopen, visibility changes, duplication, and supported legacy migration. Native Paint pixels have one persisted owner. |
| Layer operations | Copies detach shared buffers; material copies own independent derived images; channel operations preflight before mutation; unsupported conversions cancel without losing data. |
| Filters | Ordered local updates equal a full recomposition, including edge regions and float storage. Large-canvas measurements report work and wall time separately from viewport presentation. |
| Interface | Narrow/wide sidebars remain readable, identify the target, and preserve custom material links during redraw. Preview actions preserve edit context. |
| Baking | Cycles map generation, cancellation/progress, previews, temporary-data cleanup, saved pixels, and representative model quality retain their existing regression evidence. |

## Validation procedure

Use the single throttled profile build from the workspace guide and disposable
factory-startup processes. Run the relevant native tests and Python checks in
[the regression guide](../tests/image_paint_edit/README.rst). Native-window reports
and inspected screenshots are required for interaction claims. A passing
background process does not establish viewport behavior.

Before calling a candidate validated, require every selected report to contain
`passed: true`, inspect the native test summary and runtime logs, and record any
unsupported behavior below. Do not overwrite the artist's original model or
blend files. Packaging is a separate step with a matching tested source revision.

## Boundaries to retain in release notes

- The shared brush integration covers the existing projected texture painter,
  not Blender's experimental 3D texture-paint pipeline.
- Slope Blur and wrapped morphology near canvas edges can require full-canvas
  evaluation. Local filter timing is not a frame-rate or GPU completion metric.
- Procedural authoring and some legacy operations still use Python/NumPy.
  Selection transforms retain full-sized source and preview arrays.
- Curvature uses topology-dependent pointiness; thickness is an inward-occlusion
  proxy. See [mesh-map semantics](tex_paint_mesh_baking.md).
- Layer filters have no animation or individual masks. Color-space behavior
  follows the canvas storage; byte rasterization has finite precision.
- Flattening rejects spatial specular and nonzero emission that cannot be encoded
  by one Paint layer. Merge Down checks its relationship to surrounding layers.
  Use texture-set export when those channels must be baked together.
- A legacy Paint entry missing from an otherwise native-active mask stack retains
  its import payload; explicit stack evaluation imports it. Ordinary native-only
  saved stacks do not require this compatibility path.
- Externally changing pixels in the same baked-map image needs explicit mask
  refresh or rebake. Balance/opacity edits intentionally reuse cached raw maps.

## Candidate evidence

The 2026-10-31 production-hardening update in
`build_tex_paint_layers_ninja/bin-selection-dev/blender.exe` (embedded hash
`4c880e7b7573`, build time `2026-10-30 20:15:05` UTC) resolves the remaining
standalone 2D canvas same-UID cross-talk (`BKE_tex_paint_tree_shares_channel_layers`
across RNA property callbacks, native mask stroke updates, C++ layer operators,
and Python mask effect selection) and makes multi-part `ALL_SHARED_OBJECTS`
material splitting (`NODE_OT_tex_paint_make_material_unique`) atomic with full
rollback on regeneration failure, active-object retention, and per-channel image
naming. It also adds 2D `(dx, dy)` modal pixel-move status readouts, blocked-target
warning reports, top-level `Enable Layers` in `IMAGE_MT_paint_layer` for flat
images, and full native mask stack synchronization via `mask_stack_copy_from`.
This build passed all 54 native GoogleTests (`30` in `TexPaintLayerTest`, `15` in
`TexPaintMaskTest`, `9` in `TexPaintFlattenTest`), 1,158 headless checks across
12 background suites (`552` across the 10 readiness suites plus `216` in
`mask_projection_isolation` and `390` in `procedural_mask_matrix`), and 314
native-window checks across 11 interactive suites (`295` across the 10 readiness
suites plus `19` in `selection_contents`), with zero Python context nesting
errors. Evidence is recorded in
`mesh-onion-live-dev/image-edit-validation/production-hardening/integrated-20261031/summary.json`.

The earlier 2026-10-31 procedural/blend audit produced a separate native runtime:
`build_tex_paint_layers_ninja/bin-mask-matrix-dev/blender.exe`, embedded hash
`4c880e7b7573`, build time `2026-10-30 18:48:23` UTC. This fixes zero-opacity
effects changing the initial mask value. Python also restores the paint target
and borrowed generator state and rejects missing-UV receivers as raw sources.
Its UI module SHA-256 is
`eeae912b9fd42b7e29b2546cfc89705143cd79967d87d7d322933e4795e4cb4a`.

This pass passed 15 native mask tests, 390 generator checks spanning 1,152
independent blend combinations, 2,319 viewport assertions across 104 stroke
cases, and all 216 projection-isolation regression checks. Viewport cases cover
six generators plus grunge, eight mask blends, 22 standard brush blends and 14
color-layer blends, with held strokes, Ctrl erase, alpha lock, effect/mask/layer
toggles, preview/inversion and blocked targets. Local mesh-generator strokes run
on a sloped receiver as well as controlled baked-map sources. Held-stroke
screenshots were inspected. This validates data flow and interaction, not a new
assessment of Cycles bake quality or measured frame rate.

The manifest is
`mesh-onion-live-dev/image-edit-validation/procedural-viewport-matrix/summary.json`.
CUDA/OptiX and the required kernel coverage are retained. The original artist
runtime was left running at `13:16:45`; its Python helpers were updated without
changing the scene. The native zero-opacity fix requires opening the separate
new executable. No release package or commit was made.

The 2026-10-31 Python follow-up fixes mask images leaking into Fill color
projection and repeated adjustment of UV gutters during mask reprojection. It
also separates 3D mask/color transforms and enables native-handle undo. The
executable below was unchanged at that step; its UI module SHA-256 was
`8845b41070fef0abc0ba62bc2ecd55a5e6f84ee003160d74749625ec93e7aa3d`.
The follow-up passed 216 isolation checks, 30 native-window checks on a copy of
the reported artist scene, and 20 existing native mask-paint interaction checks.
Evidence is recorded in
`mesh-onion-live-dev/image-edit-validation/live-mask-color-leak/summary.json`.
The current artist scene was backed up and its confirmed accidental color
projection repaired while preserving native Paint pixels and mask authoring.
Existing files with both color and mask projections are not automatically
rewritten: intentional color textures cannot be inferred safely from image names.

The 2026-10-30 development candidate reports runtime hash `4c880e7b7573` and build
time `13:16:45` UTC. Its source is branch `tex-paint-layers`, HEAD
`44f4d2e0e99fc6bc1bcc8ad1c40d9e077c4d15d8`, with uncommitted changes. The workspace
manifest `mesh-onion-live-dev/image-edit-validation/production-hardening/summary.json`
records the executable and source hashes, exact report paths, and installed-script
comparison. Earlier evidence is historical and remains in the regression guide.

The filter microbenchmark uses a 16-by-16 dirty region, radius-six Blur, eight
local evaluations and three full evaluations after initialization. On this
machine, average evaluation times were 0.129 ms local versus 26.405 ms full at
2048 pixels, and 0.133 ms versus 88.960 ms at 4096 pixels. The local path gathers
4096 pixels and writes 1600 pixels at either size. These measurements exclude
projected brush input, other layer composition, GPU work, and presentation.

The installed native-window tests additionally compare a held projected stroke
and Ctrl erasure with Blur enabled against a full evaluation, checking both the
final mask and the displayed image data. The window-based transaction suite
covers duplicate, mask copy, rasterize, Merge Down, Flatten, and locks through
undo, redo, and save/reopen. Actual job start/completion instrumentation verifies
all seven asynchronous bake stages; UI polling alone can miss short stages.
The preceding candidate exposed a Python context nesting race during asynchronous
baking despite passing all pixel checks. Counter updates and cleanup now occur
under the GIL. The runner rejects that diagnostic in its logs, and the progress
suite is repeated in fresh processes to exercise concurrent render/UI callbacks.

Final validation passed 50 native tests, 456 background checks, and 293 native-window
checks on this candidate. Two additional fresh-process progress runs passed 42
checks each; all three progress runs and the complete suite were free of Python
context nesting errors. Narrow/wide sidebar, layer-effect, preview, and bake-progress
screenshots were inspected. CUDA, OptiX, CUDA binaries, and the configured GPU
architecture coverage remain enabled. This is a tested development installation;
no release package or source commit was created by this hardening pass.
