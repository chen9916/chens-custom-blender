# Native canvas editing

The Image Editor and material texture painting share `ShaderNodeTexPaint` storage,
private layer/mask `ImBuf` buffers, packing, dirty-region composition, and paint undo
buffer lookup. `blenkernel/intern/tex_paint_edit.cc` adds the common selection and
pixel transformation kernels. Python supplies native Blender UI definitions and
the existing higher-level filters; it does not implement the new wand, lasso, or
interactive pixel transform.

## Interaction

In Image Editor Paint mode, choose Magic Wand, Lasso, or Box in the left toolbar.
The tool header contains selection mode and wand tolerance, connectivity, and
sample-merged options. Shift extends and Ctrl subtracts. The Select menu includes
select all, deselect, invert, and the existing selection modifiers.

G moves, R rotates, and S scales the selected pixels on the active layer or mask.
X/Y constrain movement or scale; unconstrained 2D move reports `(dx, dy)` pixel deltas
in the status bar, while numeric movement is in pixels and rotation in
degrees. Enter/left click confirms; Esc/right click restores the original pixels
and selection. The transform tool also supports a mouse drag. Empty selections
cannot transform anything; clearing the selection removes the editing restriction.
Hidden/locked layers and unsupported targets are rejected and report a warning when invoked. Alpha must be unlocked
before moving a color layer's pixels. Mask movement keeps mask opacity opaque and
exposes black at the cutout.

Confirming a transform keeps the original selected content available for the next
G/R/S gesture. Pixels beneath a moved selection are restored when it moves away;
transparent holes do not pick them up. Consecutive rotations and scales resample
the original source through an accumulated affine transform. Content moved beyond
the canvas can return during this sequence. Undo/redo and save/reopen retain it.
Making or modifying the selection, or deselecting, anchors the current result.
Editing the target pixels between transforms starts a new source on the next
transform; the edits are preserved.

The main header identifies the active layer and Pixels/Mask target. Edit, Select,
Layer, and Filter menus hold document commands. The Layers sidebar contains the
stack and its inspector; the Image sidebar contains document setup. New canvas
presets create layered documents. Existing flat images can use Enable Layers.
UV projection mapping has an explicit Transform Projection tool, grouped with
Transform Pixels, so its handlers do not intercept ordinary pixel edits.

Mask fills added in the Image Editor default to canvas mapping. Their inspector
shows repeat/stamp, scale, offset, and rotation; mesh projection choices and 3D
falloff controls are confined to the 3D workflow. An existing fill with saved 3D
mapping retains that mapping until Use Canvas Mapping is chosen. This conversion
supports undo/redo and keeps the fill source and other settings.

Mask projection transforms belong to the selected mask effect. Layer projection
RNA fields belong exclusively to color. Mask G/R/S, alignment, snapping, reset,
and native viewport handles use the effect's transform and never reproject the
layer's color source. A Paint mask or generator selection cannot fall back to a
color projection gizmo. Native handles create one undo step; cancelling a 3D
transform restores the effect's complete authoring settings and final mask pixels.

The Python mask projector temporarily borrows the native layer projector and
restores its color fields, buffers, flags, and UID in `finally`. Each scratch
projection uses a fresh temporary negative UID so the native projector clears
UV gutters before sampling. Reusing the previous scratch UID would treat an
already adjusted final mask as raw input outside UV islands. A dedicated native
scratch-projection API remains a possible replacement for this compatibility path.

Procedural generator evaluation also restores the active paint target, mask flags,
generator parameters and internal evaluation guard after borrowing native mask
generation. Receivers without an active UV map contribute no raw texels; an old
evaluated mask is never reused as a failed generation result. UV-map removal or
selection invalidates the raw-source cache. Externally editing the pixels of an
existing baked-map image requires explicit refresh or rebake; ordinary balance
and opacity edits reuse the cached raw source.

Effects at zero opacity are ignored when choosing the stack's initial value, just
like disabled effects. This applies to native per-stroke evaluation and Python
authoring: a first Add, Screen or Max generator at zero opacity cannot silently
replace a white mask base with black.

## Non-destructive layer effects

The active paint or fill layer has a Layer Effects stack under Channels in the
3D painting sidebar and Pixels in the Image Editor. Add effects there or through
Filter > Add Layer Filter. The stack supports Blur, HSV offsets,
brightness/contrast, levels/gamma, invert, and grayscale.
Filters evaluate from top to bottom, with individual enable toggles and strength;
they can be reordered, duplicated, renamed, or removed.

Native layer composition applies these operations before layer blending, opacity,
and masking. Color adjustments evaluate straight RGB. Blur evaluates a separable
box filter on premultiplied RGBA, softening color and coverage together without
pulling hidden colors into transparent edges. Its radius is in canvas pixels,
with clamped canvas edges. Source pixels, alpha, and mask pixels remain intact.
Painting and G/R/S continue editing the source buffer, with filters applied to the
result. These are whole-layer color operations, independent of the temporary
selection. The same compositor supplies material textures and the Image Editor.
The existing Adjustment layer continues to adjust the composite below it.

Each layer owns its persisted filter list; duplication copies it independently,
and file subversion 503.26 initializes older layers with no filters. Merge Down
bakes filters once into its result and clears the baked filter stack. Levels
preserve values above one for float documents; filters use the existing canvas
buffer color space. Filter animation and per-filter masks are not implemented.

Layers with spatial effects keep an owned derived RGBA runtime buffer. Initial
evaluation computes the full result; later brush changes evaluate only the dirty
region and its filter support. The summed blur radii expand both below-layer cache
updates and the master image's partial GPU update region. Multiple blur and color
adjustments retain exact stack order. Explicit full recomposition rebuilds the
derived pixels, and the derived cache is neither saved nor used as a brush target.

## Native mask-effect painting

Each mask effect owns its source or paint pixels on the `TexPaintLayer`. A Paint
effect stores straight grayscale RGB and independent alpha coverage in an owned
RGBA buffer. The evaluated `mask_ibuf` is the final mask presented to the layer;
the native painter never edits it when a Paint effect is selected. `mask_pixels`
reads that final result, while `TexPaintMaskEffect.pixels` reads or writes the
selected effect's source and coverage. `paint_pixels` follows the active native
paint target.

The Python UI still owns procedural authoring controls and saved generator
metadata. It publishes the ordered effect stack, source maps, blend settings, and
base value to native storage. Native effect buffers and evaluation are then the
authoritative painting state. Authoring settings are persisted immediately so
native duplication and undo see current controls. Native-owned Paint pixels are
omitted from the authoring JSON; legacy `paint_z64` data remains supported during
migration. Transient compatibility snapshots can still be used by older effect
operations. Brush strokes are not reconstructed by diffing the final mask or
waiting for an idle timer.

Only the explicitly selected, enabled Paint effect accepts brush input. Selecting
a generator or filter clears the native paint-effect target. The sidebar reports
that state and provides Select Paint and Add Paint actions. Previewing the whole
layer mask does not select a different effect. Sidebar drawing only inspects the
material graph; Connect Material is an explicit undoable action.

Every projected dab writes to the selected Paint effect, evaluates the affected
mask region, composites the result into the layer mask and master image, then
updates the display buffer. Ctrl erasing attenuates the selected effect's alpha,
so lower effects become visible again even when the Paint effect uses Multiply.
Ordinary color painting keeps its native layer target as well: erasing a Multiply
Paint layer removes its contribution and reveals the lower color layer.

Native effect buffers use synthetic undo paths of the form
`<tex_paint_layer>{layer_uid}:paint:{effect_uid}`. Image tile undo resolves those
paths against the current Main, while packed effect pixels and settings are saved
with the document. File subversion 503.28 clears native effect records in older
files while preserving their Python-authored metadata and evaluated mask. The
load handler republishes enabled legacy mask stacks to native storage, including
their saved independent paint coverage. Native Blur, Sharpen, Highpass,
Dilate/Erode, and Bevel evaluate dirty regions with the complete input halo for
the ordered filter stack. Each spatial stage consumes its own halo before the
next stage. Slope Blur retains a full-canvas fallback because its warp can sample
distant pixels; morphology also falls back where its wrapped support crosses a
canvas edge. The projected painter coalesces Slope Blur updates once per dab.
Color-layer Blur retains its separate support-expanded partial update path.

## Selection and pixels

`NodeTexPaint` owns a persisted float coverage mask and an explicit active flag.
An active all-zero mask is deliberately different from no restriction. Native
2D brush dabs, bucket/gradient fill, and 3D projected brush masks consume the same
coverage. Masks are canvas-wide and are retained when switching layers or paint
targets. Resizing the canvas clears the selection.
The 3D projection cache retains geometry coverage separately and refreshes canvas
selection coverage when rebinding each stroke. Changing selections therefore
does not require rebuilding the cached projected geometry.

File subversion 503.25 initializes older documents without a selection. Owned
copies retain coverage; evaluated copies omit it. Cached outline endpoints belong
to the document runtime, merge contiguous boundary edges, and rebuild only when
coverage changes. Zooming and panning project cached endpoints.

Pixel kernels use premultiplied float RGBA in the buffer's color space. Float
buffers preserve HDR values; byte buffers convert from/to straight alpha. The
transaction reads an immutable source once, samples from that source for every
preview, and writes/composites the union of old and new affected bounds. Identity
transforms preserve feathered pixels exactly. Bilinear samples outside the image
are transparent. The transformed selection follows the pixels.

File subversion 503.27 adds owned source pixels, original coverage, and the
accumulated transform to `NodeTexPaint`. These arrays are copied for owned data,
omitted from evaluated copies, and serialized for file and memory-file undo.
Reuse requires matching dimensions, layer UID, paint target, and a 64-bit hash of
the committed raw pixel buffer. This catches intervening edits through both native
and legacy pixel-writing paths without discarding those edits. Selection changes
explicitly release the stored source. Layer filters do not change source pixels.

The `paint_pixels` and `selection_mask` RNA arrays provide owned-buffer access for
the remaining Python operations, replacing direct pointer writes for the common
2D read/write helpers. These arrays represent straight RGBA and scalar coverage,
respectively. Access targets the active editable layer or mask and does not fall
back to modifying the composite when the target is locked or unavailable.

## Undo and validation

Layer pixel actions write through the native RNA pixel service, which detaches
shared storage before mutation. Float edits preserve HDR and alpha; brush-color
fills use the canvas color space. Copy Mask uses the native mask-copy service for
both plain masks and effect stacks, preserving float precision and independent
ownership. Both single-object and all-shared-objects material splits create
independent derived image IDs named for each PBR channel, keep the active object
on the source material during multi-part splits, use object material slots so
linked mesh duplicates can keep sharing geometry, and roll back all slot
assignments, created materials/images, and source node pixels if any part's
procedural regeneration fails. Layer locking, visibility, opacity, naming, mask
state, active-layer selection, mask brush strokes, and layer-stack operators
synchronize across 3D material channels while keeping standalone Image Editor
canvases in `.TexPaint_2D_Layers` completely isolated (`BKE_tex_paint_tree_shares_channel_layers`)
even when their layer or mask-effect UIDs coincide or their dimensions differ.

Fill rasterization converts the existing layer in place. Its UID, authored mask,
native Paint coverage, channel parameters, and editable color effects survive.
Projected fill color and texture-driven height are converted to owned pixels.
All matching channel layers are checked before conversion. Missing projection
pixels and clipping bases whose distinct coverage cannot fit in one Paint buffer
are rejected before replacing source data.

Flatten and Merge Visible prepare owned color and material-channel snapshots
before removing layers in any channel. They ignore temporary mask preview,
clear baked effects, and preserve hidden layers for Merge Visible. The new result
occupies the bottommost removed position; surviving hidden layers retain their
relative order. Re-enabling a hidden layer after merging can therefore give a
different relationship to the merged result than before the operation.

The current Paint buffer format cannot store spatially varying specular or
arbitrary emission in a single flattened layer. Those combinations cancel with
an explanation and can instead be exported as a texture set. Merge Down also
requires a representable combination relative to the rest of the stack; it does
not silently discard material channels or change context-dependent blending.

Selection and transform operators push native undo steps. A confirmed transform
serializes both pixels and coverage through the existing layer packing path;
cancel restores its private snapshot without adding a completed operation. The
packed-buffer loader detects EXR's premultiplied and PNG's straight alpha conventions,
preventing float colors from being multiplied by alpha again on load or undo. The
owning node tree and embedded owner are tagged for undo without recompiling the
material. Brush strokes continue to use native image tile undo, including the
existing layer-buffer lookup across memory-file boundaries.
Replaying a chain of brush steps resolves every image by its saved name and library
in the current Main. Resolving only the entry step leaves neighboring references
null or stale after a memory-file reload; the three-stroke regression reproduces
the reported undo crash in the previous build and checks the corrected replay.

See [the regression procedure](../tests/image_paint_edit/README.rst). UI behavior
must be checked in a disposable interactive process; background calls do not
verify event routing or overlays.

The shared Python UV rasterizer used by nearby-object AO and mesh-map baking
retains singleton coordinate axes while broadcasting triangle coverage. Gathering
covered texels indexes those singleton axes at zero; using the broadcast coverage
indices on both coordinate arrays caused AO mask creation to raise `IndexError`
on small UV triangles. The focused AO regression also checks the shared Cavity
and Edge Wear generator path.

The texture-set Mesh Maps & Baker now uses an isolated Cycles bake rather than
the live procedural generator. See [mesh-map baking](tex_paint_mesh_baking.md) for
the seven map definitions, quality controls, preview workflow, and limitations.
Asynchronous Cycles callbacks and main-thread UI callbacks can enter Python while
the renderer releases the GIL. The shared Python context nesting counter now
changes only while holding the GIL, and zero-depth cleanup completes before
release. This prevents lost increments and persistent negative-depth context
errors during consecutive map jobs. Nested file-load context refresh retains its
existing behavior while using the same protected entry path.

Native mask painting is validated against owned per-effect RGBA coverage and the
separately evaluated final mask. Persistence checks compare native data across
save/reopen and verify that authoring JSON does not duplicate native Paint arrays.
Companion PBR nodes receive the same native effect stack and
coverage. The projected painter evaluates and publishes each dab before its image
buffer update, which avoids displaying an intermediate raw mask as the final
composite.

## Current boundaries

This establishes a common native selection/transform path, not complete feature
parity with a standalone illustration application. Legacy color adjustments,
selection modifiers, generators, and projection authoring still contain Python
and NumPy work. They use the common 2D pixel/selection bridge where applicable.
Separate canvases populated by a baked projection remain separate documents.
The shared brush integration here covers the existing projected texture painter;
Blender's experimental 3D texture-paint pipeline is outside this validation.
Flat-image resize, reload, and transform commands are hidden for layered documents
because they operate on the derived display image rather than the layer stack.

The transaction keeps full-size source/preview arrays, retains the original source
between transforms, and uses memory-file undo for edits. Hashing the target,
coverage copying, and outline rebuilding still scale with canvas
size when the selection changes. Tile snapshots, asynchronous previews, GPU
composition, brush-selection performance, and color-management consistency across
byte/float documents need measured follow-up work. No frame-rate or GPU-completion
claim is made by this change.
