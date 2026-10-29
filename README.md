<!--
Keep this document short & concise,
linking to external resources instead of including content in-line.
See 'release/text/readme.html' for the end user read-me.
-->

Chen's Custom Blender
=====================

A personal Blender 5.3 development fork combining native Substance-style **Multi-Layer Texture Painting & Mask Studio** and **Native 3D Mesh Onion Skins** on `main`.

Custom Features
---------------

### 1. Multi-Layer Texture Painting & Substance Mask Studio (`ShaderNodeTexPaint`)
- Native C++ multi-layer canvas node (`ShaderNodeTexPaint`) with Paint, Fill, and Adjustment layers, 16 blend modes, Clipping Masks, Alpha Lock, Layer Solo, and grayscale Mask Preview (`Alt`-click style).
- **Substance-Style Mask Stack**: Non-destructive stack of **Mask Fill (Custom Image / Grunge)**, **Smart 3D Mask Generators** (*Edge Wear*, *Cavity & Dirt*, *Curvature*, *Ambient Occlusion*, *Top-Down Dust*, *Vertical Height*), **Hand-Painted Mask**, and **Levels** sub-layers.
- **6 Projection Methods**: *UV Projection*, *Triplanar*, *Planar / Decal*, *Warp (Surface-Hugging)*, *Spherical*, and *Cylindrical* with *Repeat (Tile)* and *Clamp (Stamp)* wrap modes.
- **Interactive 3D & 2D UV Projection Gizmos**: Manipulate projections in the 3D Viewport or directly over UV islands in the 2D Image Editor (`G` / `R` / `S` shortcuts and interactive handles).

### 2. Native 3D Mesh Onion Skins
- Native mesh onion skins for a posed armature and its directly skinned meshes.
- Live pose previews following Blender's native Auto Key policy.
- Keyed-frame sampling, frame-range looping, and smoothly scrolling ghost poses.
- Screen-pixel or camera-aware scene-distance spacing, with direction reversal.
- Per-viewport past/future colors, opacity, distance fade, and depth display.
- Same-frame key replacement that keeps the newest keyed value when keys collide.

Enable **Mesh Onion Skin** from the 3D Viewport header in Pose Mode and use its
popover to configure sampling and appearance. See the
[implemented design](doc/onion_skin/DESIGN.md) for supported behavior and limits,
and the [validation guide](tests/onion_skin/README.rst) for reproducible checks.

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
