# Texture-set mesh maps

In Texture Paint, expand **Paint Layers > Mesh Maps & Baker**. Choose the size,
Cycles sample count, and UV margin, then use **Bake Mesh Maps** for all seven maps
or **Bake** beside a particular map. **View** opens a separate Image Editor window;
**Preview Baked Maps** also works while the baker settings are collapsed. Switching
maps reuses this window. Closing it leaves the painting editors intact. Preview
does not push a paint undo step.

Interactive baking shows a progress bar directly below the baker header, including
the current map, map count, and overall percentage. The bar stays visible when the
settings are collapsed. Click its X button or press Esc to cancel; existing maps
remain assigned. Preparing evaluated geometry and packing completed images run
on the main thread; the Cycles bake itself runs as a native background job.

Existing maps need to be rebaked to use the new path. Smart Masks use assigned
maps when **Use Baked Mesh Maps** is enabled; baking preserves this preference.
The unbaked, interactive procedural mask generators remain the approximate path.

## Evaluation

`bl_ui/tex_paint_baking.py` uses the native Cycles `object.bake` operator in a
temporary scene. It copies evaluated meshes from the current dependency graph,
including modifiers, active UVs, smooth/flat shading, and custom corner normals.
It creates temporary shader materials and render targets. Authored meshes,
material node trees, canvas/mask buffers, selection, render engine, and Cycles
device preferences are not borrowed for intermediate results.

| Map | Cycles evaluation |
| --- | --- |
| Normal | Native tangent-space normal bake, including selected-to-active high-poly transfer |
| World Normal | Shading normal encoded from -1..1 to 0..1 through emission |
| Color ID | Constant object/part/material color through emission |
| AO | Ambient Occlusion shader through emission, with controlled distance and scope |
| Curvature | Geometry Pointiness through emission; topology-dependent |
| Position | World XYZ normalized to the receiving assembly's bounds through emission |
| Thickness | Inward local AO, inverted so enclosed/thicker regions are white |

Thickness is a normalized occlusion proxy, not a physical distance measurement.
Curvature is Cycles pointiness, not a derivative across UV islands. Both reflect
mesh topology. AO Spread sets the ray distance relative to the longest dimension
of the receiving assembly. Self scope only intersects the shading object;
Material scope includes the receiving material's parts and explicit high-poly
sources; Global scope additionally includes visible, renderable mesh objects in
the current view layer. Other scenes and hidden unrelated objects do not leak in.

All maps evaluate at the requested output size, including rectangular Match Canvas
sizes. There is no 64/512/1024-pixel intermediate upscaling. Samples control Cycles
sampling; AO shaders use four rays per shading evaluation. UV Margin extends
island borders at output resolution using the native baker. Overlapping UV islands
still share texels and cannot represent different surface values independently.
High-poly projection uses the configured extrusion/ray distance and rejects missing
sources rather than silently falling back to low-poly geometry.

Auto device uses already-enabled Cycles GPU devices if available and CPU otherwise.
Explicit GPU requires devices enabled in Preferences > System. The baker never
changes or saves those preferences. Interactive invocation advances through native
Cycles jobs and reads their actual progress. Scripted `EXEC_DEFAULT` calls remain
synchronous and use the same scene snapshots, shaders, and bake steps.

## Publication and persistence

New maps are staged until every requested bake finishes. A failure removes temporary
scenes, objects, meshes, materials, and images, preserving previous slots. Other
material slots on a receiving mesh use a disposable image to avoid contaminating
the texture set. Multiple receivers accumulate into the same atlas.

Only one mesh-map bake can run at a time. Save, file load, undo, redo, and UI-module
unregistration cancel an active bake and join its native worker before deleting
temporary data. No bake progress is saved in the blend file. The WindowManager
RNA methods `object_bake_progress(scene)` and `object_bake_cancel(scene, wait=False)`
bridge the existing native object-bake job API; waiting is reserved for lifecycle
cleanup. Native bake failures dispatch the cancellation callback rather than the
success callback, preventing publication of failed jobs. Native job reports are
owned by the worker, and its modal handler tracks the scene that started the job
even when the caller restores a temporary context override. Waiting for worker
shutdown releases the Python lock so the Cycles render engine can finish.
The baker supplies no screen to native bake steps, preserving existing Image
Editors and avoiding a borrowed area pointer in the worker.

Successful maps are float images in Non-Color space, including Color ID. They are
packed as EXR and retain a fake user because the JSON slot names do not count as
datablock users. Marking packed float IDs as sRGB changes their values on reload;
the data color space prevents that conversion. Unreferenced generated maps from
previous bakes are released when replaced; referenced or imported images remain.
Normal wiring is an explicit saved option. Enabled masks re-evaluate after the
maps are published, absorbing any pending paint strokes first.

The previous implementation borrowed a layer mask buffer for AO/curvature, used
vertex-level external AO, interpolated mesh normals in Python, computed thickness
on a 64-pixel grid, and differentiated normals across UV edges. The MeetMat fixture
reproduced the reported AO blotches and triangle bands with that path. The Cycles
implementation replaces mesh-map baking; it does not remove the fast live generator.

See [the validation procedure](../tests/image_paint_edit/README.rst) for map checks,
save/reopen checks, and the separate native-window preview regression.
