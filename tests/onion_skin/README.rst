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
Supported modal pose edits stage their copied animation immediately after capture;
the job timer handles cold activation and changes to time or sampling settings.
This removes a producer timer wait while retaining one pending worker request.

The pose adapter in
``source/blender/editors/transform/transform_convert_armature.cc`` captures
supported authored transform channels after native mirroring. Main-thread staging
in ``source/blender/editors/animation/anim_mesh_onion_skin_preview.cc`` copies the
authored animation and uses Blender's native key inserter in a private owned rig.
This includes native NLA tweak-time mapping and an owned new-Action template when
the source has no active Action. Staging remains on the main
thread because key insertion reads user preferences. The worker applies the
resulting private curves, evaluates sample times and copies geometry; it never
inserts keys into the live scene or caches evaluated mesh pointers.

``source/blender/draw/engines/overlay/overlay_mesh_onion_skin.hh`` owns GPU batches
per viewport. Drawing queues requests and reads completed snapshots. Dynamic
vertex buffers are reused when vertex counts agree; index buffers are rebuilt
when actual tessellation changes. Ghosts use native uniform-color drawing with
alpha blending, optional depth testing, and no depth writes or selection picking.

The evaluation envelope is one active posed armature, directly skinned meshes,
native Action/NLA animation, authored standard IK and supported native transform
constraints. Modifier evaluation includes Armature, Mirror and simple Solidify;
terminal Subdivision modifiers are shown as the deformation cage. Conservative
preflight estimates account for constructive modifier expansion before admitting
work. Simulations, instancing, complex Solidify and arbitrary modifier stacks
remain outside the audited envelope.

Native driver evaluation supports Average, Sum, Minimum and Maximum drivers plus
scripted drivers handled by Blender's native simple expression evaluator. General
Python expressions and driver namespace
callbacks are rejected. Referenced object, armature, mesh, shape-key and Action
data are privately copied and remapped. Scene path inputs and driver-facing custom
properties are copied without copying the live scene's entire contents. View-layer
context properties must agree before different views share a CPU cache. These
checks support the supplied mannequin's rig and shape-key driver setup without
allowing arbitrary source references into the worker.
Drivers embedded in NLA strip controls and Scene paths outside the copied
driver-facing settings are rejected with an explicit status message.
Driver path auditing also rejects intermediate shading IDs and redirected Scene
reads through ID-valued properties. Safe invalid properties retain Blender's
native fallback behavior. Preview updates tag the edited rig and changed Actions;
native frame-change relations update the other animated dependencies.

The native Pose Mode header toggle opens a popover with two past and two future
samples by default, frame step one, and opacity 0.15. Before/after counts are
limited to four each. Keyframes Only selects the nearest authored key times,
including supported NLA time mapping, and disables Frame Step. Animated or
noninvertible NLA key-time mapping disables keyed-only sampling with a status
message; ordinary frame sampling can still evaluate that NLA animation. Loop
Frame Range wraps across the inclusive scene Start/End range. Each sample retains
its signed requested temporal offset so wrapped past samples keep past coloring
and distance fade uses their true distance from the playhead.

Sampling settings, including keyed-only and looping, are shared across enabled
viewports showing the same scene and active rig. Opacity, in-front display,
Distance Fade and Fade Strength are local viewport appearance settings. Fade is
disabled by default; strength defaults to 0.75. Enabled fade applies a smooth
monotonic falloff from the nearest to farthest actual temporal distance, shared
across meshes and both directions. Nearest samples retain configured opacity;
farthest samples retain 15 percent of it at maximum strength. Equal-distance
samples retain full opacity. Appearance changes redraw GPU batches without
restarting CPU evaluation. RNA lives in ``source/blender/makesrna/intern/rna_space.cc``.

Live temporary-key preview supports native translate, rotate, trackball and scale
of controls in one rig, including native mirrored controls, native NLA tweak
mapping and native creation of an active Action for NLA-only source animation.
The editable Action uses one layer and one strip. Visual keying, custom keying
sets, targetless/automatic IK, iTaSC and multi-rig transforms remain unsupported.
ID-valued custom properties that cannot be safely owned by the new-Action preview
template are rejected. Unsupported evaluation clears ghosts and reports a
status message. With auto-key disabled, unkeyed channels can affect ghosts;
authored animation curves continue to determine keyed channels at sample times.

Fixture preparation
-------------------

Replace ``<blender>`` and ``<blender_test>`` with the matching native build's
executables. Run commands from the source root and replace other angle-bracket
placeholders with absolute file paths or values. Create output directories first.

A deterministic standard-IK fixture::

  <blender> --factory-startup --background --python-exit-code 1 --python tests/onion_skin/make_live_fixture.py -- --vertices 100000 --output <ik-fixture.blend>

Import the supplied Mixamo FBX into a disposable posed test file::

  <blender> --factory-startup --background --python-exit-code 1 --python tests/onion_skin/make_mixamo_fixture.py -- --input <character.fbx> --output <mixamo-fixture.blend>

The generator writes a JSON sidecar with rig/control names, bones, key counts and
mesh sizes. Keep this untouched dense-key fixture as the primary imported-data
case. An additional ``--sparse-control`` copy retains five authored frames on the
selected control to make interpolation changes easier to observe. Report that
copy separately. Dense authored keys at neighboring sample frames can correctly
keep those ghosts unchanged when only the current frame's key is adjusted.

Prepare the supplied Adjustable Mannequin as disposable authored-rig and NLA
fixtures. The generator rejects output equal to input, loads with scripts disabled,
preserves the original curves/keys and driver/modifier stacks, selects
``c_hand_ik.l`` on ``rig`` at frame 17, and sets a front orthographic viewport::

  <blender> --factory-startup --disable-autoexec --background --python-exit-code 1 --python tests/onion_skin/make_mannequin_fixture.py -- --input <original-mannequin.blend> --output <mannequin-active.blend> --variant active
  <blender> --factory-startup --disable-autoexec --background --python-exit-code 1 --python tests/onion_skin/make_mannequin_fixture.py -- --input <original-mannequin.blend> --output <mannequin-tweak.blend> --variant nla-tweak
  <blender> --factory-startup --disable-autoexec --background --python-exit-code 1 --python tests/onion_skin/make_mannequin_fixture.py -- --input <original-mannequin.blend> --output <mannequin-nla-only.blend> --variant nla-only

NLA variants place the original Action into a strip starting at frame 5 with scale
1.5. The tweak variant enters through Blender's native NLA operator; the NLA-only
variant has no active Action. Sidecars record actual strip mapping, retained key
times and rig/mesh metadata. ``--bone c_foot_ik.l`` selects the foot control instead.

Native tests and lifecycle checks
--------------------------------

Build Blender's native test target with the project's normal test configuration,
then run all native onion skin sampling, evaluation and preview fixtures::

  <blender_test> --gtest_filter="MeshOnion*"

They check source preservation, native key insertion policies, private provisional
curve replacement, authored IK deformation parity, constructive modifiers,
native drivers/NLA ownership, keyed/looped sampling and unsupported evaluation.
Run the UI defaults/file round-trip and split-view appearance/sampling test
in a disposable interactive process::

  <blender> --factory-startup --python-exit-code 1 --python tests/onion_skin/check_settings.py -- <settings-roundtrip.blend>

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

Run the mannequin active and NLA tweak cases with all authored Actions protected.
For NLA-only, the distinct confirmation policy checks native creation of exactly
one active Action and verifies undo restores the original animation stack::

  <blender> --disable-autoexec <mannequin-active.blend> --enable-event-simulate --python tests/onion_skin/run_live_test.py -- --output <mannequin-active-results> --rig rig --bone c_hand_ik.l --transform TRANSLATE --all-actions
  <blender> --disable-autoexec <mannequin-tweak.blend> --enable-event-simulate --python tests/onion_skin/run_live_test.py -- --output <mannequin-tweak-results> --rig rig --bone c_hand_ik.l --transform TRANSLATE --all-actions
  <blender> --disable-autoexec <mannequin-nla-only.blend> --enable-event-simulate --python tests/onion_skin/run_live_test.py -- --output <mannequin-nla-only-results> --rig rig --bone c_hand_ik.l --transform TRANSLATE --all-actions --confirm-policy new-action

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

Append ``--geometry-dump`` to a separate geometry correctness run to capture
actual submitted world-space positions and triangles at all six quiet states.
These bounded native captures live in the run's ``geometry`` directory. The
postprocessor requires matching mesh names, sample times, signed offsets and
triangle order, then checks a maximum Euclidean vertex displacement of 1e-5 world
units for cancel, undo and confirmation. It reports maximum and RMS displacement;
quantized hashes continue to prove movement during dragging. This avoids treating
an arbitrarily small change across a rounding boundary as a full geometry mismatch.
Missing or stale captures fail validation. These runs are ineligible for latency
claims because capture and file output add work.

For ``--confirm-policy new-action``, the disposable test disables the user
preference Only Insert Available so native confirmation can create new channels.
Other runs retain the native keying policy. The test also allows three seconds
after undo for a large control rig's private graph to be rebuilt.

The live driver accepts ``--keyed-only``, ``--loop-range``, ``--fade`` and
``--fade-strength`` to exercise the native controls. ``--frame`` sets the initial
playhead. For sampling correctness, provide fixture-authored explicit oracles
``--expected-frames`` and paired ``--expected-offsets``. With geometry diagnostics
enabled, the summarizer compares these against ``mesh_onion_skin_frames`` lines
emitted from actual native submitted batches at baseline, held poses, cancel,
confirm and undo. It also checks the submitted sample count, including meshes.
Missing diagnostics, stale phases, wrong wrapped offsets or wrong evaluated
frames fail validation; Python does not calculate onion skin geometry or generate
the expected sampling algorithm.

For the mannequin active fixture's retained times and scene range 1..30, frame 17
with two samples each side has the explicit keyed-only oracle::

  --keyed-only --frame 17 --expected-frames 12 15 18 22 --expected-offsets -5 -2 1 5

At frame 1, normal step-two looping and keyed-only looping respectively use::

  --loop-range --frame 1 --expected-frames 27 29 3 5 --expected-offsets -4 -2 2 4
  --keyed-only --loop-range --frame 1 --expected-frames 23 30 5 8 --expected-offsets -8 -1 4 7

Confirm the sidecar's scene range before applying these particular oracles to any
other fixture. Key-only sampling may produce fewer than the requested count when
there are insufficient authored times; the explicit oracle specifies that count.

Run the small postprocessor regression suite without launching Blender::

  python -m unittest discover -s tests/onion_skin -p test_summarize_benchmark.py -v

Record the Blender build hash, GPU/backend, fixture metadata, sampling settings,
source checks and actual ghost update counts with every performance claim. Evaluate
the proposed 33 ms p95 input-to-draw-submission goal against measured interaction;
this target is not a guarantee across rigs or hardware. CPU admission and bundle
size guards limit this initial feature, but private dependency state, concurrent
snapshots and per-viewport GPU buffers also consume memory. Report measured peak
memory when assessing a production rig.

Measured development build
--------------------------

The final control-rig candidate was measured on a Ryzen 9 7900X with an
RTX 5070 Ti (driver 616.92), using 120 continuous native modal moves before
cancel and another 120 before confirm. Warm-cache latency from native pose input
to native draw submission was:

============================= ======= ========= =========
Fixture                       Ghosts  Median ms P95 ms
============================= ======= ========= =========
Mixamo Running                4       7.431     13.791
Adjustable Mannequin          4       75.276    100.347
Adjustable Mannequin          2       71.720    98.586
Adjustable Mannequin NLA tweak 4       84.637    108.924
============================= ======= ========= =========

These are individual runs with latency diagnostics enabled and geometry
instrumentation disabled. They measure CPU-side submission, excluding operating
system event queuing, GPU completion and monitor presentation. The Mixamo fixture
meets the 33 ms p95 target on this machine; the 344-bone mannequin does not. Its
four-sample native graph solve takes about 51 ms median and dominates worker
cost. Reducing its samples improves worker cost but does not guarantee low
end-to-end latency through viewport redraw scheduling.

The tests validated the uncommitted extension with embedded checkpoint hash
``0d6467bd605c``; the final package rebuild updates the commit metadata. The
package's ``ONION_SKIN_VALIDATION.json`` records the fixture metadata, source
assertions, exact measured timings, process memory and supported limitations.
Whole-process memory includes the asset, undo history and evaluation capsule;
GPU memory was not measured.

GPU release and existing user profile
------------------------------------

An ordinary Windows package must use the existing
``%APPDATA%\Blender Foundation\Blender\5.3`` profile. A directory named
``portable`` beside ``blender.exe`` selects a separate profile and hides the
existing user add-ons. Preserve any portable profile under another directory
name when restoring ordinary AppData discovery. The release check observes
existing add-on names and native search paths without importing those add-ons
or saving preferences. It fingerprints the user's preferences and startup file
before and after the check.

Build CUDA devices, OptiX devices and precompiled CUDA kernels with
``WITH_CYCLES_DEVICE_CUDA=ON``, ``WITH_CYCLES_DEVICE_OPTIX=ON`` and
``WITH_CYCLES_CUDA_BINARIES=ON``. The local GPU release uses CUDA 13.4.1/12.9.1,
OptiX 8.0.0, and ``CYCLES_CUDA_BINARIES_ARCH=sm_86;sm_120;compute_75`` to cover
the RTX 3080 and RTX 5070 Ti plus PTX fallback. Record the actual CMake cache and
installed kernel inventory with the release results; these settings alone do
not prove that a backend can render.

Run each backend/device combination in a fresh background process, using a
distinct workspace output directory. For example::

  blender.exe --background --factory-startup --disable-autoexec --debug-cycles --python-exit-code 1 --python tests/onion_skin/check_release.py -- --output-dir E:/dev/blender/blender/mesh-onion-live-dev/release-cuda-5070 --backend CUDA --device "RTX 5070 Ti"
  blender.exe --background --factory-startup --disable-autoexec --debug-cycles --python-exit-code 1 --python tests/onion_skin/check_release.py -- --output-dir E:/dev/blender/blender/mesh-onion-live-dev/release-optix-3080 --backend OPTIX --device "RTX 3080"

Omit ``--backend`` and ``--device`` for a profile-only check. A GPU check requires
one unambiguous eligible native device, disables all CPUs and other GPUs, and
renders the factory scene at 32 by 32 pixels with two samples and no denoising.
Adaptive kernel optimization is disabled in memory to exercise the bundled
generic kernels without requesting background scene-specific compilation.
It checks meaningful PNG pixels and records the physical device ID, build hash,
precompiled kernel inventory and render duration in ``result.json`` and
``ONION_RELEASE_RESULT``. Native selection and driver eligibility are checked
before and after rendering to reject the preference-level CPU fallback. Cycles
does not expose a Python query for the render session's actual device; retain
the debug log as additional native kernel evidence. This checks backend startup
and rendering, and supplies no new onion-skin performance claim.
