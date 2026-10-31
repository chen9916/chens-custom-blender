<!--
Keep this document short & concise,
linking to external resources instead of including content in-line.
See 'release/text/readme.html' for the end user read-me.
-->

Chen's Custom Blender
=====================

A unified Blender 5.3 development fork combining **Native Multi-Layer 3D Texture Painting & Krita 2D Image Studio** and **Native 3D Mesh Onion Skins** on `main`.

Custom Features
---------------

### 1. Multi-Layer 3D Texture Painting, Krita 2D Studio & Substance Mask Stack (`ShaderNodeTexPaint`)
- **Single-Node Multi-Channel PBR Layer Stack**: Native C++ `ShaderNodeTexPaint` node managing synchronized Base Color, Roughness, Metallic, Height/Bump, Specular, and Emission channels across Paint, Fill, and Adjustment layers, 16 blend modes, Clipping Masks, Alpha Lock, Layer Solo, and non-destructive per-layer Color & Blur Filter stacks.
- **Native Mask-Effect Stack**: Authoritative C++ per-effect mask evaluation combining **Hand-Painted Masks** (with live `Ctrl`-erase and independent coverage), **Smart 3D Mask Generators** (*Ambient Occlusion*, *Curvature*, *Cavity & Dirt*, *Edge Wear*, *Top-Down Dust*, *Vertical Height*), **Projected Grunge / Stencil Fills** (275 starter maps), **Levels**, and **Spatial Filters** (*Blur*, *Blur Slope*, *Sharpen*, *Grow/Shrink*, *Bevel*, *Threshold*, *Posterize*, *Highpass*).
- **Cycles Mesh Maps & High-Poly Baker Hub**: Asynchronous 7-map Cycles baker (*Tangent Normal*, *World Normal*, *Color ID*, *Ambient Occlusion*, *Curvature*, *Position*, *Thickness*) with live progress, cancellation, and 1-click preview.
- **Native 2D Image Editor Selection & Transform Suite**: Native C++ Magic Wand, Lasso, Box/Ellipse selection, marching-ants contour overlay, and stateful `G` / `R` / `S` affine pixel & mask transforms with HDR/alpha preservation and full undo/redo.
- **Documentation & Tests**: See [doc/paint_image_editing.md](doc/paint_image_editing.md), [doc/tex_paint_mesh_baking.md](doc/tex_paint_mesh_baking.md), [doc/tex_paint_readiness.md](doc/tex_paint_readiness.md), and [tests/image_paint_edit/README.rst](tests/image_paint_edit/README.rst).

### 2. Native 3D Mesh Onion Skins
- Native mesh onion skins for a posed armature and its directly skinned meshes.
- Live pose previews following Blender's native Auto Key policy.
- Keyed-frame sampling, frame-range looping, and smoothly scrolling ghost poses.
- Screen-pixel or camera-aware scene-distance spacing, with direction reversal.
- Per-viewport past/future colors, opacity, distance fade, and depth display.
- Same-frame key replacement that keeps the newest keyed value when keys collide.
- **Documentation & Tests**: See [doc/onion_skin/DESIGN.md](doc/onion_skin/DESIGN.md) and [tests/onion_skin/README.rst](tests/onion_skin/README.rst).

This repository contains Blender source code. Build instructions and upstream
project information are linked below.

Blender
-------

Blender is the free and open source 3D creation suite.
It supports the entirety of the 3D pipeline—modeling, rigging, animation, simulation, rendering, compositing,
motion tracking and video editing.

![Blender screenshot](https://code.blender.org/wp-content/uploads/2018/12/springrg.jpg "Blender screenshot")

Project Pages
-------------

- [Main Website](https://www.blender.org)
- [Reference Manual](https://docs.blender.org/manual/en/latest/index.html)
- [User Community](https://www.blender.org/community/)

Development
-----------

- [Build Instructions](https://developer.blender.org/docs/handbook/building_blender/)
- [Code Review & Bug Tracker](https://projects.blender.org)
- [Developer Forum](https://devtalk.blender.org)
- [Developer Documentation](https://developer.blender.org/docs/)


License
-------

Blender as a whole is licensed under the GNU General Public License, Version 3.
Individual files may have a different but compatible license.

See [blender.org/about/license](https://www.blender.org/about/license) for details.
