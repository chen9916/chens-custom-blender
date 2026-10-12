<!--
Keep this document short & concise,
linking to external resources instead of including content in-line.
See 'release/text/readme.html' for the end user read-me.
-->

Chen's Custom Blender
=====================

A personal Blender 5.3 development fork with native mesh onion skins and animation
editing improvements. Custom development lives on the `mesh-onion-live` branch.

Custom Features
---------------

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
