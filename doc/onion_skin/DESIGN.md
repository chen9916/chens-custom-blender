# Native mesh onion skin: current implementation

This document describes the implemented feature in the `mesh-onion-live`
worktree. It is the architectural reference for further changes, rather than a
proposal. Workspace routing, build ownership and machine-specific commands live
in the applicable `AGENTS.md`. Validation commands and disposable fixture
instructions live in [the test README](../../tests/onion_skin/README.rst).

## Evaluation and ownership

The supported evaluation envelope is one active posed armature and its directly
skinned meshes. Native Action/NLA animation, authored standard IK, audited native
transform constraints and supported drivers determine each sampled pose.

Drawing posts main-thread requests through
[the editor API](../../source/blender/editors/include/ED_mesh_onion_skin.hh).
It never solves rigs or evaluates geometry in the draw caller. A native WM job
owns one private copied/remapped `Main`, Scene and dependency graph. The worker
serializes evaluation of different sample times; Blender's existing dependency
graph and geometry tasks provide internal parallelism. Separate concurrent
graphs per frame are not part of this implementation.

Dependency discovery, preflight and copying happen before worker access. Source
IDs are not edited to manufacture previews. Retained references in the private
workspace are remapped to owned copies; shading/display-only links are removed.
Scene driver inputs and relevant custom properties are copied without retaining
the live Scene's entire contents. Native key insertion staging stays on the
main thread because it reads user preferences.

The worker copies each evaluated mesh into owned positions, triangle indices
and an object-to-world matrix. It retains no evaluated mesh pointers across
graph evaluations. A complete immutable `Bundle` contains all samples for one
edit revision; shared `SampleGeometry` payloads can serve several view policies
and overlapping windows. Publication never mixes geometry from different edits.

## Scheduling and cache validity

Latest input replaces pending input in a bounded mailbox. Supported modal pose
input is staged after capture, without waiting for the job timer. A completed
same-epoch bundle can be published even if newer input is pending, provided its
generation is newer than the displayed bundle. This avoids starving publication
during continuous dragging. The next request still evaluates the latest input.

The frame cache belongs to the private evaluation capsule. Canonical frame
identity is reusable only while its epoch and geometry revision match. Timeline
or mixed-view operation admits at most 32 cached frames within the 256 MiB
budget; Relative-only operation retains the smaller eight-frame capacity.
Conservative workspace admission also accounts for copied data, constructive
modifier expansion and in-flight geometry. Budget or preflight failure reports
a status instead of silently substituting approximate geometry.

Changing time with unchanged animation reuses exact cached poses and evaluates
only missing frames. A pose proposal changes the geometry revision and clears
the previous exact-frame cache. Source dependency edits invalidate the relevant
workspace. An animation edit can affect every sampled frame; there is no
speculative reuse of supposedly unaffected poses and no vertex interpolation
standing in for a native rig solve.

Cancellation, confirmation, undo, file reload, ID remapping/deletion and editor
teardown stop or invalidate private evaluation before source lifetime changes.
Mutex-protected mailboxes and immutable shared payloads separate worker writes
from main-thread publication and drawing. Private temporary keys, Action
creation and NLA staging must preserve source keys, handles, users and animation
structure. Blender's ordinary confirmed transform/Auto Key operation remains
responsible for committing the user's edit.

## Sampling and continuous scrolling

`Settings` contains shared sampling choices: Before, After, Frame Step,
Keyframes Only and Loop Frame Range. Before/After are limited to four each.
Views showing the same Scene/rig share this sampling state; appearance remains
local to each viewport.

Relative sampling selects ordinary past/future samples around the current frame
and omits a centered ghost. Screen Space Offset selects Timeline sampling: a
stable frame grid anchored to Scene Start, or stable authored key indices,
including a centered sample and transparent outer guards. Each sample records
its canonical frame, signed temporal offset and unwrapped rank. Looping uses
the inclusive Scene Start/End period and deduplicates repeated canonical frames.
Supported NLA mapping supplies authored key times; animated or noninvertible
key-time mapping reports a keyed-only limitation while ordinary sampling can
still evaluate supported NLA animation.

The overlay keeps sampled pose identities as the presentation clock advances.
Each pose moves from future through the current model to past, with smooth edge
entry/exit and color blending through the center. Guard samples, short-loop
envelopes and one-sided visibility avoid abrupt alias changes. At zero temporal
offset, the evaluated pose and transform align with the current model.

The native playback timer supplies a fractional clock without changing Scene
time/subframes or forcing extra rig solves.
Enabled scrolling views request redraws at up to 60 Hz during native playback.
That is presentation cadence, not a guarantee of 60 Hz geometry evaluation or
live rig updates. Geometry arrives asynchronously in complete bundles; heavy
rigs retain their actual evaluation latency.

## Overlay and native controls

The Pose Mode header exposes a native Mesh Onion Skin toggle and popover.
Defaults are two ghosts before/after, step one and opacity 0.15. Controls include
Keyframes Only, inclusive Loop Frame Range, distance fade with strength, In
Front, Screen Space Offset, Reverse Direction and Scene Distance.

Distance Fade is off by default and its strength defaults to 0.75. Ordinary
sampling normalizes temporal distance between the nearest and farthest samples;
equal-distance samples retain full opacity. At maximum strength, the farthest
samples retain 15 percent of configured opacity. Timeline sampling instead uses
stable rank boundaries together with smooth outer guards, avoiding opacity
resets as poses move between slots. Fade, opacity, depth display and spacing
settings are local to the viewport and do not change shared sampling.

Fresh `View3DOverlay` flag bits 20 through 27 and explicit fields persist these
settings. Blender file version 503.28 initializes scene-distance settings for
older files; earlier migrations initialize the other controls. Existing pixel
spacing is not reinterpreted as scene distance.

Pixel spacing defaults to 24 pixels per frame and translates clip-space X after
projection, preserving apparent spacing across zoom and camera distance. Scene
Distance defaults to 0.1 scene units per frame and translates along the viewer's
normalized horizontal world axis before projection. Normal projection then
accounts for zoom, depth and perspective; displaced world positions also drive
clipping. The native distance RNA property respects Scene units. Reverse
Direction swaps the screen sides without swapping the past/future colors.

Appearance changes update overlay uniforms and redraw; they do not invalidate
pose geometry. Each viewport owns its GPU buffers and batches. Retained sample
payloads reuse geometry buffers; tessellation changes rebuild triangle indices.
The overlay alpha blends ghosts with optional depth testing and no depth writes
or selection picking. CPU publication never owns GPU resources.

Past and Future Color are local per-viewport appearance controls, defaulting to
the familiar blue/orange pair used by the native overlay. Changing either color
updates draw uniforms only; it does not rebuild the private evaluation capsule,
invalidate frame caches or trigger a new rig solve. Sampling counts, frame step,
keyed-only mode and looping remain shared between enabled viewports for the
active scene/rig, while colors, opacity, fade, depth and spacing remain local.

## Supported edits and explicit limits

Modal translation, rotation, trackball and scale capture authored control values
after native mirroring. Auto Key preview uses owned private Action curves,
native insertion rules, NLA tweak mapping and private new-Action templates where
necessary. The editable Action envelope is one layer and one strip.

With Auto Key disabled, unkeyed channels can affect ghosts. Existing animated
curves remain authoritative at sampled times; ghosts do not simulate a manual
key insertion in that mode.
Dense keys on neighboring sample frames can therefore correctly keep those
ghosts unchanged when only the current frame's key changes.

Native Auto Key and manual insertion share same-time replacement rules. Re-keying
a channel that already contains colliding keys keeps one key with the newly
inserted value, including with Only Insert Needed enabled. Collision detection
uses Blender's existing key-time tolerance; distinct subframes, other channels
and collisions at unrelated times are not cleaned up. Ordinary replacement
preserves the chosen key's handles and interpolation. Private preview staging
uses the same rule without repairing or otherwise editing source curves before
native confirmation. Files are not automatically deduplicated on load.

Drivers support native Average, Sum, Minimum and Maximum operations and scripted
drivers accepted by Blender's simple-expression evaluator. Arbitrary Python
namespace expressions/callbacks, drivers embedded in NLA strip controls and
unaudited Scene/shading reads are rejected. View Layer context driver inputs
must agree before views share evaluation.

The modifier envelope is Armature, Mirror, simple Solidify and terminal
Subdivision displayed as the deformation cage. Simulations, instancing,
arbitrary modifier stacks, complex Solidify and edit-mode meshes are excluded.
Visual keying, custom keying sets, automatic/targetless IK, iTaSC and multi-rig
transforms are outside the audited preview envelope. Unsafe ID-valued custom
properties in new-Action templates are rejected. Unsupported requests clear
ghosts and expose a status rather than claim broader support.

## Source map

- [Evaluation, preflight, cache and WM job](../../source/blender/editors/animation/anim_mesh_onion_skin.cc).
- [Preview input contract](../../source/blender/editors/include/ED_mesh_onion_skin_preview.hh), [private animation staging](../../source/blender/editors/animation/anim_mesh_onion_skin_preview.cc), [pose transform adapter](../../source/blender/editors/transform/transform_convert_armature.cc).
- [Frame/key/NLA selection](../../source/blender/editors/animation/anim_mesh_onion_skin_frames.cc).
- [Overlay resources, clock and fades](../../source/blender/draw/engines/overlay/overlay_mesh_onion_skin.hh), [spacing shader](../../source/blender/draw/engines/overlay/shaders/overlay_mesh_onion_skin_screen_vert.glsl).
- [Persisted DNA](../../source/blender/makesdna/DNA_view3d_types.h), [RNA/update policies](../../source/blender/makesrna/intern/rna_space.cc), [native UI](../../scripts/startup/bl_ui/space_view3d.py), [file migrations](../../source/blender/blenloader/intern/versioning_503.cc).
- [Lifetime callbacks](../../source/blender/editors/util/ed_util.cc), [ID remapping hook](../../source/blender/windowmanager/intern/wm_event_system.cc).
- [Native evaluation tests](../../source/blender/editors/animation/anim_mesh_onion_skin_test.cc), [preview tests](../../source/blender/editors/animation/anim_mesh_onion_skin_preview_test.cc), [frame selection tests](../../source/blender/editors/animation/anim_mesh_onion_skin_frames_test.cc), [key replacement tests](../../source/blender/blenkernel/intern/fcurve_test.cc).

## Release evidence and performance interpretation

The current tested feature release is source commit
`e7763b5893b28918bad78d63f019a959e2bea36a`, packaged as
`blender-mesh-onion-skin-native-5.3-camera-distance`. It passed 72 native checks
and 109 viewport checks on the supplied Running animation; the interactive
viewport test run took 12.22 seconds. Checks cover spacing, camera/zoom behavior,
reversal, continuous playback, loop seams, buffer reuse and source preservation.
This test duration is not an interaction-latency benchmark.
The package preserves CUDA/OptiX kernels and normal AppData 5.3 add-on discovery.
Its four GPU render checks are historical release evidence from commit
`9fc3cde71ce8841e93d757401e8c3987a00f48ed`; they were not repeated for the
camera-distance package. Package smoke checks verify the current executable,
embedded source hash, native settings and normal profile routing.
Historical warm four-ghost CPU input-to-submission measurements were median/p95
7.431/13.791 ms for Mixamo and 75.276/100.347 ms for the heavier Adjustable
Mannequin. These were measured on earlier implementations, not a new benchmark
of the current release or GPU-visible latency guarantees. Cache reuse improves
unchanged playback; an edited heavy rig still requires native reevaluation.
Use [the test README](../../tests/onion_skin/README.rst) for reproducible checks
and keep historical evidence distinguished from current measurements.
