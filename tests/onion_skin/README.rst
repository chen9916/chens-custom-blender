Native Mesh Onion Skin Validation
================================

This directory contains disposable fixture generation and interactive tests for
Blender's native mesh onion skin implementation. Python drives Blender operators,
UI properties and event simulation; mesh evaluation and rendering run in C++.
Run interactive tests in a separate Blender process. They change the loaded
session, delete test objects and quit automatically; they do not save the loaded
fixture. Keep the original production file separate from generated test copies.

Architecture and supported scope
--------------------------------

The public contract in ``source/blender/editors/include/ED_mesh_onion_skin.hh``
provides main-thread requests and immutable bundles containing owned positions,
triangles, sample frames and object transforms. The evaluator in
``source/blender/editors/animation/anim_mesh_onion_skin.cc`` builds a private,
copied and remapped dependency capsule. A native WM job serializes sampling;
Blender's existing dependency evaluation and geometry tasks provide internal
parallelism. Latest input replaces pending input instead of building a queue.
Completed coherent bundles can be published during continuous input even when a
newer request is pending, provided the result belongs to the current epoch and
is newer than the displayed result.

The pose adapter in
``source/blender/editors/transform/transform_convert_armature.cc`` captures
supported authored transform channels after native mirroring. Main-thread staging
in ``source/blender/editors/animation/anim_mesh_onion_skin_preview.cc`` copies the
active Action and uses Blender's native key inserter. Staging remains on the main
thread because key insertion reads user preferences. The worker applies the
resulting private curves, evaluates sample times and copies geometry; it never
inserts keys into the live scene or caches evaluated mesh pointers.

``source/blender/draw/engines/overlay/overlay_mesh_onion_skin.hh`` owns GPU batches
per viewport. Drawing queues requests and reads completed snapshots. Dynamic
vertex buffers are reused when vertex counts agree; index buffers are rebuilt
when actual tessellation changes. Ghosts use native uniform-color drawing with
alpha blending, optional depth testing, and no depth writes or selection picking.

The initial envelope is one active posed armature, direct Armature-deformed
meshes, an active Action without drivers or NLA, and authored standard IK with
supported transform constraints. Terminal Subdivision modifiers are shown as the
deformation cage. The UI exposes two past and two future samples by default,
step one, and opacity 0.15. Before/after counts are limited to four each.
Sampling is shared across enabled viewports showing the same scene and active
rig; opacity and in-front display remain local. The native RNA implementation is
in ``source/blender/makesrna/intern/rna_space.cc``.

Live temporary-key preview supports native translate, rotate, trackball and scale
of controls in one rig, including native mirrored controls. It requires one Action
layer and one strip. Visual keying, custom keying sets, targetless/automatic IK,
iTaSC, multi-rig transforms, other modifiers, simulations and instancing are
outside the initial envelope. Unsupported evaluation clears ghosts and reports a
status message. With auto-key disabled, unkeyed channels can affect ghosts;
authored animation curves continue to determine keyed channels at sample times.

Fixture preparation
-------------------

Replace ``<blender>`` and ``<blender_test>`` with the matching native build's
executables. Run commands from the source root and replace other angle-bracket
placeholders with absolute file paths or values. Create output directories first.

A deterministic standard-IK fixture::

  <blender> --factory-startup --background --python tests/onion_skin/make_live_fixture.py -- --vertices 100000 --output <ik-fixture.blend>

Import the supplied Mixamo FBX into a disposable posed test file::

  <blender> --factory-startup --background --python tests/onion_skin/make_mixamo_fixture.py -- --input <character.fbx> --output <mixamo-fixture.blend>

The generator writes a JSON sidecar with rig/control names, bones, key counts and
mesh sizes. Keep this untouched dense-key fixture as the primary imported-data
case. An additional ``--sparse-control`` copy retains five authored frames on the
selected control to make interpolation changes easier to observe. Report that
copy separately. Dense authored keys at neighboring sample frames can correctly
keep those ghosts unchanged when only the current frame's key is adjusted.

Native tests and lifecycle checks
--------------------------------

Build Blender's native test target with the project's normal test configuration,
then run the two native fixtures::

  <blender_test> --gtest_filter="MeshOnionEvaluationTest.*:MeshOnionPreviewTest.*"

They check source preservation, native key insertion policies, private provisional
curve replacement, authored IK deformation parity, terminal subdivision cages,
and rejection of unsupported evaluation. Run the UI defaults/file round-trip test
in a disposable interactive process::

  <blender> --factory-startup --python tests/onion_skin/check_settings.py -- <settings-roundtrip.blend>

Run native job lifecycle checks using the imported fixture's rig name::

  <blender> <mixamo-fixture.blend> --python tests/onion_skin/run_lifecycle_test.py -- --output <lifecycle-results> --rig <rig-name>

The JSON report ``lifecycle_report.json`` must contain passed assertions and no
error. This test checks enable/off/re-enable, zero samples/restore, shared sampling
and local appearance in two View3Ds, global overlay hide/re-show, one versus last
subscriber cancellation, source preservation, reload during a worker, and deletion
of sampled meshes and the active rig. It uses real WM timers and the native
``bpy.app.is_job_running('MESH_ONION_SKIN')`` query. It resolves fresh RNA pointers
after reload. Closing windows and switching between two separate rigs remain
additional manual lifecycle cases.

The lifecycle driver also links a copied skinned mesh while the worker is active,
then removes it. To require proof that cooperative capsule replacement draws the
new membership, set ``BLENDER_MESH_ONION_SKIN_GEOMETRY_DIAGNOSTICS=1``, redirect
stdout to the sibling ``<lifecycle-results>.log``, and append
``--validate-membership``. This waits for native drawn sample counts to include the
new mesh and return to their baseline count after removal, then verifies source
curves, ID counts and users are restored. Without the flag, these same actions
exercise job liveness and source preservation without geometry instrumentation.

Interactive performance measurement
-----------------------------------

Set ``BLENDER_MESH_ONION_SKIN_DIAGNOSTICS=1`` in the launched process environment.
Capture stdout and stderr in a sibling log: output directory ``<case>`` and log
``<case>.log``. The following runs exercise real modal input and verify source
curves/users/datablock counts during cancel, native confirm and undo::

  <blender> <mixamo-fixture.blend> --enable-event-simulate --python tests/onion_skin/run_live_test.py -- --output <on-four> --enabled 1 --samples 4 --moves 120 --rig <rig-name> --bone <control-name> --transform ROTATE
  <blender> <mixamo-fixture.blend> --enable-event-simulate --python tests/onion_skin/run_live_test.py -- --output <off-four> --enabled 0 --samples 4 --moves 120 --rig <rig-name> --bone <control-name> --transform ROTATE

Repeat for two and eight samples, the standard-IK fixture, and the optional sparse
control copy. Use ``TRANSLATE`` with the standard-IK fixture's ``Control`` bone.
Keep GPU/backend, viewport size, rig, samples and input sequence matched between
on/off runs. Collect several independent runs to expose variance::

  python tests/onion_skin/summarize_benchmark.py <on-four> <off-four> --output <summary.json>

Telemetry includes staging, worker evaluation, input-to-publication and
input-to-native-draw-submission timing. The summary restricts these values to the
continuous modal dragging phases and reports median/p95/max and redraw intervals.
Enabled cases must have actual visible ghost updates and all source-preservation
checks must pass. Redraw cadence alone does not demonstrate ghost update cadence.
Native draw submission timing does not measure GPU completion or monitor
presentation; do not call it input-to-present latency.

Set ``BLENDER_MESH_ONION_SKIN_GEOMETRY_DIAGNOSTICS=1`` only for separate geometry
verification runs. Its optional geometry fingerprint can confirm changing ghost
positions in the sparse-control/IK cases, but hashing adds work and should be
disabled for the primary performance comparison. Disable normal diagnostics for
an additional uninstrumented on/off redraw comparison if logging overhead matters.

For geometry verification, append ``--geometry-expectation stable`` to the dense
Mixamo command and ``--geometry-expectation change`` to the sparse-control or
standard-IK command. Keep four samples and step two as configured by the driver.
The sparse Mixamo sidecar records the selected control's exact RNA prefix and
retained key times; its generator asserts the four sample frames lie between
authored control keys. The driver holds the final pose before cancel and confirm,
then records geometry markers after cancel, confirm and undo. The summarizer
compares world-position fingerprints of batches actually submitted by the native
overlay: cancel and undo must restore baseline geometry, confirm must match the
held provisional geometry, and all requested samples of each directly skinned
visible mesh must remain visible.
Sparse-control and IK runs must also show multiple geometry values during both
continuous drags. Dense Mixamo samples must remain stable. Failed source or
geometry checks cause a nonzero summarizer exit. The geometry checks use world
positions quantized to 1e-5 and do not measure framebuffer presentation.

Run the small postprocessor regression suite without launching Blender::

  python -m unittest discover -s tests/onion_skin -p test_summarize_benchmark.py -v

Record the Blender build hash, GPU/backend, fixture metadata, sampling settings,
source checks and actual ghost update counts with every performance claim. Evaluate
the proposed 33 ms p95 input-to-draw-submission goal against measured interaction;
this target is not a guarantee across rigs or hardware. CPU admission and bundle
size guards limit this initial feature, but private dependency state, concurrent
snapshots and per-viewport GPU buffers also consume memory. Report measured peak
memory when assessing a production rig.
