# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Check native Windows user resources and optionally render on one Cycles GPU.

Run with --background --factory-startup --disable-autoexec --python-exit-code 1.
The test reads the existing profile but never imports its add-ons or saves prefs.
"""

import argparse
import hashlib
import json
import math
import os
import re
import sys
import time
from pathlib import Path

import bpy


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def normalized_path(value):
    return os.path.normcase(str(Path(value).resolve()))


def normalized_name(value):
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def build_metadata():
    def text(value):
        return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value

    return {
        "version": bpy.app.version_string,
        "hash": text(bpy.app.build_hash),
        "branch": text(bpy.app.build_branch),
        "date": text(bpy.app.build_date),
        "time": text(bpy.app.build_time),
        "binary": bpy.app.binary_path,
    }


def profile_snapshot(root):
    # Fingerprint only preferences/startup files, without loading either into Blender.
    result = {}
    for name in ("userpref.blend", "startup.blend"):
        path = root / "config" / name
        result[name] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
    return result


def check_profile():
    require(os.name == 'nt', "This profile regression check targets native Windows AppData paths")
    require(bpy.app.version[:2] == (5, 3), "Expected a Blender 5.3 release candidate")
    require(os.environ.get("APPDATA"), "APPDATA is unavailable")
    root = Path(os.environ["APPDATA"]) / "Blender Foundation" / "Blender" / "5.3"
    require(root.is_dir(), "Existing Blender 5.3 AppData profile was not found")
    before = profile_snapshot(root)
    actual = {"USER": bpy.utils.resource_path('USER')}
    actual.update({name: bpy.utils.user_resource(name, create=False)
                   for name in ('CONFIG', 'SCRIPTS', 'EXTENSIONS')})
    expected = {"USER": root, "CONFIG": root / "config", "SCRIPTS": root / "scripts",
                "EXTENSIONS": root / "extensions"}
    for name, path in expected.items():
        require(actual[name] and normalized_path(actual[name]) == normalized_path(path),
                f"{name} uses {actual[name]!r}, expected native AppData path {str(path)!r}")
    scripts = [str(Path(path).resolve()) for path in bpy.utils.script_paths(user_pref=False)]
    require((root / "scripts").is_dir(), "Existing user scripts directory was not found")
    require(normalized_path(root / "scripts") in {normalized_path(path) for path in scripts},
            "Native script discovery omitted the existing user scripts directory")
    addons_root = root / "scripts" / "addons"
    addons = []
    addon_directories = []
    if addons_root.is_dir():
        for path in sorted(addons_root.iterdir(), key=lambda value: value.name.casefold()):
            if path.name.startswith('.') or path.name == "__pycache__":
                continue
            if path.is_dir():
                addon_directories.append(path.name)
            if (path.is_dir() and (path / "__init__.py").is_file()) or (
                    path.is_file() and path.suffix == '.py' and path.name != "__init__.py"):
                addons.append(path.name)
    addon_paths = bpy.utils.script_paths(subdir="addons", user_pref=False)
    if addons_root.is_dir():
        require(normalized_path(addons_root) in {normalized_path(path) for path in addon_paths},
                "Native add-on search omitted the existing user addons directory")
    extensions_root = root / "extensions"
    repositories = []
    if extensions_root.is_dir():
        repositories = sorted(path.name for path in extensions_root.iterdir()
                              if path.is_dir() and not path.name.startswith('.'))
    return root, before, {
        "native_resources": actual,
        "script_paths": scripts,
        "addon_paths": addon_paths,
        "existing_addons_directory": str(addons_root),
        "existing_addon_names": addons,
        "existing_addon_count": len(addons),
        "existing_addon_directory_names": addon_directories,
        "existing_addon_directory_count": len(addon_directories),
        "extension_repository_directories": repositories,
        "preferences_and_startup_sha256_before": before,
        "installed_addons_imported_by_test": False,
        "preferences_saved_by_test": False,
    }


def kernel_files():
    binary_root = Path(bpy.app.binary_path).resolve().parent
    # Cycles path_get("lib/...") resolves below the bundled addons_core/cycles directory.
    roots = [binary_root / "lib", binary_root / "blender.shared",
             Path(bpy.utils.system_resource('SCRIPTS')) / "addons_core" / "cycles" / "lib",
             Path(bpy.utils.system_resource('SCRIPTS')) / "addons" / "cycles" / "lib"]
    found = set()
    for root in roots:
        if root.is_dir():
            for pattern in ("*.cubin.zst", "*.ptx.zst", "*.cubin", "*.ptx"):
                found.update(path.resolve() for path in root.rglob(pattern) if path.is_file())
    return [{"path": str(path), "bytes": path.stat().st_size}
            for path in sorted(found, key=lambda value: str(value).casefold())]


def check_gpu(backend, requested_name, output):
    require('cycles' in bpy.context.preferences.addons, "Factory Cycles add-on is unavailable")
    preferences = bpy.context.preferences.addons['cycles'].preferences
    preferences.compute_device_type = backend
    # Exercise the shipped generic kernels rather than background adaptive compilation.
    preferences.kernel_optimization_level = 'OFF'
    # Query only the requested backend; this updates entries using the native _cycles API.
    native_devices = preferences.get_device_list(backend)
    inventory = [{"name": device[0], "backend": device[1], "id": device[2],
                  "meets_driver_requirement": bool(device[8])} for device in native_devices]
    selector = normalized_name(requested_name)
    require(selector, "A nonempty physical GPU name or native device ID is required")
    matches = [device for device in inventory if device["backend"] == backend and
               (selector in normalized_name(device["name"]) or
                selector == normalized_name(device["id"]))]
    require(len(matches) == 1,
            f"Expected exactly one {backend} GPU matching {requested_name!r}; found {matches!r}")
    chosen = matches[0]
    require(chosen["meets_driver_requirement"],
            f"{chosen['name']} does not meet the native {backend} driver requirement")
    for device in preferences.devices:
        device.use = device.type == backend and device.id == chosen["id"]

    def assert_selection():
        enabled = [(device.type, device.id) for device in preferences.devices if device.use]
        require(enabled == [(backend, chosen["id"])],
                f"CPU or an unrequested GPU was enabled: {enabled!r}")
        require(preferences.compute_device_type == backend and bpy.context.scene.cycles.device == 'GPU',
                "Cycles backend or scene GPU selection changed")

    files = kernel_files()
    require(any("kernel" in Path(item["path"]).name and
                (".cubin" in item["path"] or "compute_" in item["path"]) for item in files),
            "Bundled precompiled CUDA kernels are missing")
    if backend == 'OPTIX':
        require(any("optix" in Path(item["path"]).name.lower() for item in files),
                "Bundled precompiled OptiX kernels are missing")
    scene = bpy.context.scene
    require(scene.camera is not None and any(obj.type == 'MESH' for obj in scene.objects)
            and any(obj.type == 'LIGHT' for obj in scene.objects),
            "Use the native factory startup scene with its cube, camera and light")
    scene.render.engine = 'CYCLES'
    scene.cycles.device = 'GPU'
    scene.cycles.samples = 2
    scene.cycles.use_adaptive_sampling = False
    scene.cycles.use_denoising = False
    scene.cycles.seed = 0
    scene.render.resolution_x = scene.render.resolution_y = 32
    scene.render.resolution_percentage = 100
    scene.render.use_compositing = False
    scene.render.use_sequencer = False
    scene.render.image_settings.file_format = 'PNG'
    scene.render.filepath = str(output / "render.png")
    scene.render.use_file_extension = True
    require(not Path(scene.render.filepath).exists(), "Refusing to reuse a pre-existing render")
    assert_selection()
    print("ONION_RELEASE_GPU_BEGIN " + json.dumps(chosen), flush=True)
    started = time.perf_counter()
    result = bpy.ops.render.render(write_still=True)
    elapsed = time.perf_counter() - started
    require(result == {'FINISHED'}, f"Native Cycles render did not finish: {result!r}")
    assert_selection()
    image_path = Path(scene.render.filepath)
    require(image_path.is_file() and image_path.stat().st_size > 128,
            "Native render output is missing or empty")
    image = bpy.data.images.load(str(image_path), check_existing=False)
    try:
        require(tuple(image.size) == (32, 32), "Native render has unexpected image dimensions")
        pixels = list(image.pixels)
        colors = [value for index, value in enumerate(pixels) if index % 4 != 3]
        require(colors and all(math.isfinite(value) for value in pixels),
                "Native render has empty or non-finite pixels")
        require(max(colors) > 0.01 and max(colors) - min(colors) > 0.01,
                "Native render is blank or lacks meaningful scene contrast")
        pixel_check = {"rgb_min": min(colors), "rgb_max": max(colors),
                       "rgb_mean": sum(colors) / len(colors)}
    finally:
        bpy.data.images.remove(image)
    return {"backend": backend, "requested_device": requested_name, "selected_device": chosen,
            "kernel_optimization_level": preferences.kernel_optimization_level,
            "enumerated_devices": inventory, "precompiled_kernel_files": files,
            "cpu_render_device_enabled": False, "denoising_enabled": False,
            "render_seconds": elapsed, "samples": 2, "resolution": [32, 32],
            "image": str(image_path), "image_sha256": hashlib.sha256(image_path.read_bytes()).hexdigest(),
            "image_bytes": image_path.stat().st_size, "pixel_check": pixel_check,
            "device_evidence": "Native driver eligibility and exclusive GPU preferences checked before and after successful render",
            "runtime_device_query_available": False,
            "note": "Keep --debug-cycles logs for native kernel evidence; this smoke render is not a performance benchmark"}


def main():
    arguments = sys.argv[sys.argv.index('--') + 1:] if '--' in sys.argv else []
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--backend', choices=('CUDA', 'OPTIX'))
    parser.add_argument('--device', help="Unique physical GPU name fragment or exact native device ID")
    options = parser.parse_args(arguments)
    require(bool(options.backend) == bool(options.device), "Supply --backend and --device together")
    require(bpy.app.background, "Run the release check in background mode")
    require('--factory-startup' in sys.argv and '--disable-autoexec' in sys.argv,
            "Run with --factory-startup and --disable-autoexec to preserve user startup/preferences")
    output = options.output_dir.resolve()
    root, before, profile = check_profile()
    require(not output.is_relative_to(root.resolve()), "Write test output outside the user profile")
    output.mkdir(parents=True, exist_ok=True)
    require(not (output / 'result.json').exists(), "Refusing to overwrite a previous release result")
    result = {"build": build_metadata(), "profile": profile}
    if options.backend:
        result['gpu'] = check_gpu(options.backend, options.device, output)
    require(profile_snapshot(root) == before, "User preferences or startup file changed during the test")
    result['profile']['preferences_and_startup_unchanged'] = True
    result['passed'] = True
    (output / 'result.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print('ONION_RELEASE_RESULT ' + json.dumps(result), flush=True)


if __name__ == '__main__':
    main()
