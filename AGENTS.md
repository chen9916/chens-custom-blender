# Chen's Custom Blender — Repository Guide

This repository (`main` branch) unifies two native C++ feature sets in Blender 5.3:

1. **Multi-Layer Texture Painting, Krita 2D Image Editing & Substance 3D Mask Studio (`ShaderNodeTexPaint`)**
   - Architecture and behavior: [doc/paint_image_editing.md](doc/paint_image_editing.md)
   - Cycles Mesh Maps & High-Poly Baker: [doc/tex_paint_mesh_baking.md](doc/tex_paint_mesh_baking.md)
   - Production readiness record: [doc/tex_paint_readiness.md](doc/tex_paint_readiness.md)
   - Native & Python regression suites: [tests/image_paint_edit/README.rst](tests/image_paint_edit/README.rst)

2. **Native 3D Mesh Onion Skins**
   - Architecture and supported scope: [doc/onion_skin/DESIGN.md](doc/onion_skin/DESIGN.md)
   - Validation guide and test suites: [tests/onion_skin/README.rst](tests/onion_skin/README.rst)

When working in the local development workspace (`E:/dev/blender/blender`), also follow the parent workspace guide `../AGENTS.md` for build limits, GPU toolchain paths, and validation procedures.
