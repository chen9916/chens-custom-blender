# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Layer stack panels, Substance-style Layered Mask Stack (Procedural + Paint + Grunge + Levels), and 3D Viewport projection widgets for ``ShaderNodeTexPaint``."""

import base64
import ctypes
import json
import math
import os
import random
import zlib

import bpy
from bpy.types import GizmoGroup, Menu, Operator, Panel, UIList
from bpy.props import BoolProperty, EnumProperty, FloatProperty, FloatVectorProperty, IntProperty, StringProperty
from bpy.app.translations import pgettext_iface as iface_
from mathutils import Euler, Matrix, Vector


# ---------------------------------------------------------------------------
# C Struct Mirror for Direct Real-Time Layer Mask Buffer Evaluation
# ---------------------------------------------------------------------------

class _C_TexPaintLayer(ctypes.Structure):
    _fields_ = [
        ("next", ctypes.c_void_p),
        ("prev", ctypes.c_void_p),
        ("name", ctypes.c_char * 64),
        ("uid", ctypes.c_int),
        ("type", ctypes.c_int),
        ("blend_mode", ctypes.c_int),
        ("flag", ctypes.c_int),
        ("opacity", ctypes.c_float),
        ("_pad0", ctypes.c_char * 4),
        ("fill_color", ctypes.c_float * 4),
        ("adjust_hsv", ctypes.c_float * 3),
        ("adjust_bright_contrast", ctypes.c_float * 2),
        ("_pad1", ctypes.c_char * 12),
        ("proj_image_name", ctypes.c_char * 64),
        ("proj_mode", ctypes.c_int),
        ("proj_blend_falloff", ctypes.c_float),
        ("proj_location", ctypes.c_float * 3),
        ("proj_rotation", ctypes.c_float * 3),
        ("proj_scale", ctypes.c_float * 3),
        ("_pad_proj", ctypes.c_char * 4),
        ("mask_gen_type", ctypes.c_int),
        ("mask_gen_blend", ctypes.c_int),
        ("mask_gen_balance", ctypes.c_float),
        ("mask_gen_contrast", ctypes.c_float),
        ("mask_gen_radius", ctypes.c_float),
        ("mask_gen_noise", ctypes.c_float),
        ("mask_gen_noise_scale", ctypes.c_float),
        ("mask_gen_invert", ctypes.c_int),
        ("pf", ctypes.c_void_p),
        ("mask_pf", ctypes.c_void_p),
        ("ibuf", ctypes.c_void_p),
        ("mask_ibuf", ctypes.c_void_p),
        ("runtime_dirty", ctypes.c_int),
        ("_pad2", ctypes.c_char * 4),
        ("channel_flags", ctypes.c_int),
        ("fill_roughness", ctypes.c_float),
        ("fill_metallic", ctypes.c_float),
        ("fill_height", ctypes.c_float),
        ("fill_specular", ctypes.c_float),
        ("fill_emission", ctypes.c_float * 3),
        ("pbr_pf", ctypes.c_void_p),
        ("pbr_ibuf", ctypes.c_void_p),
    ]


class _C_ImBuf(ctypes.Structure):
    _fields_ = [
        ("x", ctypes.c_int),
        ("y", ctypes.c_int),
        ("display_size", ctypes.c_int * 2),
        ("data_offset", ctypes.c_int * 2),
        ("display_offset", ctypes.c_int * 2),
        ("channels", ctypes.c_int),
        ("color_mode", ctypes.c_int),
        ("flags", ctypes.c_int),
        ("_pad_imb", ctypes.c_int),
        ("byte_data", ctypes.c_void_p),
        ("byte_sharing", ctypes.c_void_p),
        ("byte_colorspace", ctypes.c_void_p),
        ("float_data", ctypes.c_void_p),
        ("float_sharing", ctypes.c_void_p),
        ("float_colorspace", ctypes.c_void_p),
    ]


# ---------------------------------------------------------------------------
# Substance Grunge Library Discovery & Preview Collection
# ---------------------------------------------------------------------------

_GRUNGE_LIBRARY_ROOT = r"E:\dev\blender\blender\substance_starter_blender_assets\textures\Grunges_and_Procedurals"

_GRUNGE_CATEGORIES = (
    ('ALL', "All Library Maps (275)", "Browse or search across all 275 Substance Painter grunge, noise, pattern, and fabric maps", 'VIEWZOOM', 0),
    ('BITMAP_GRUNGES', "Bitmap Grunges (86)", "Scanned & photographic Substance Painter grunge maps (rust, dirt, concrete, leaks, scratches, wipes)", 'TEXTURE', 1),
    ('PROC_GRUNGES', "Procedural Grunges (30)", "Substance procedural grunge maps (spots, leaky paint, damas, galet, dirty)", 'MOD_OCEAN', 2),
    ('NOISES', "Noises & Dirt (64)", "Substance procedural noise, dirt, clouds, cells, moisture, and Voronoi maps", 'FORCE_TURBULENCE', 3),
    ('PATTERNS', "Patterns & Gradients (49)", "Substance geometric patterns, scratches, fibers, tiles, and gradients", 'MESH_GRID', 4),
    ('FABRICS', "Fabrics & Weaves (46)", "Substance fabric weaves, carbon fiber, canvas, leather, and mesh patterns", 'MATCLOTH', 5),
    ('CUSTOM', "Custom Image", "Use any loaded Blender image or import a custom texture file from disk", 'FILE_IMAGE', 6),
)

_GRUNGE_CATEGORY_FOLDERS = {
    'BITMAP_GRUNGES': ("Grunges Bitmap",),
    'PROC_GRUNGES': ("Grunges",),
    'NOISES': ("Noises", "Noises Advanced"),
    'PATTERNS': ("Patterns", "Geometric", "Gradient", "Dynamic"),
    'FABRICS': ("Fabric",),
}

_PREVIEW_COLLECTION = None
_GRUNGE_CATALOG = {}       # category -> list of dicts: {id, label, tooltip, png_path, preview_path}
_GRUNGE_ENUM_CACHE = {}    # (category, search_query) -> list of Blender EnumProperty tuples
_GRUNGE_LOOKUP = {}        # id -> dict


def _clean_grunge_label(stem):
    s = stem
    for prefix in ("grunge_concrete_", "grunge_", "noise_", "pattern_", "fabric_"):
        if s.lower().startswith(prefix):
            if prefix == "grunge_concrete_":
                s = "Concrete " + s[len(prefix):]
            else:
                s = s[len(prefix):]
            break
    return s.replace("_", " ").strip().title()


def _ensure_grunge_catalog():
    global _PREVIEW_COLLECTION
    if _GRUNGE_CATALOG:
        return
    if _PREVIEW_COLLECTION is None:
        try:
            from bpy.utils import previews
            _PREVIEW_COLLECTION = previews.new()
        except Exception:
            _PREVIEW_COLLECTION = None

    all_entries = []
    for cat_id, folders in _GRUNGE_CATEGORY_FOLDERS.items():
        entries = []
        for folder in folders:
            folder_path = os.path.join(_GRUNGE_LIBRARY_ROOT, folder)
            if not os.path.isdir(folder_path):
                continue
            preview_dir = os.path.join(folder_path, "_previews")
            for fname in sorted(os.listdir(folder_path)):
                if not fname.lower().endswith(".png"):
                    continue
                stem = os.path.splitext(fname)[0]
                png_path = os.path.join(folder_path, fname)
                prev_candidate = os.path.join(preview_dir, f"{stem}_preview.png")
                prev_path = prev_candidate if os.path.isfile(prev_candidate) else png_path
                label = _clean_grunge_label(stem)
                item_id = f"{cat_id}::{stem}"
                info = {
                    "id": item_id,
                    "stem": stem,
                    "label": label,
                    "tooltip": f"{stem.replace('_', ' ').title()} ({folder})",
                    "png_path": png_path,
                    "preview_path": prev_path,
                    "category": cat_id,
                }
                entries.append(info)
                all_entries.append(info)
                _GRUNGE_LOOKUP[item_id] = info
                _GRUNGE_LOOKUP.setdefault(stem, info)
        _GRUNGE_CATALOG[cat_id] = entries
    _GRUNGE_CATALOG['ALL'] = all_entries


def _get_grunge_enum_items_for_category(cat_id, search_filter=""):
    _ensure_grunge_catalog()
    q = (search_filter or "").strip().lower()
    cache_key = (cat_id, q)
    if cache_key in _GRUNGE_ENUM_CACHE:
        return _GRUNGE_ENUM_CACHE[cache_key]

    entries = _GRUNGE_CATALOG.get(cat_id, [])
    if q:
        entries = [
            e for e in entries
            if q in e["label"].lower() or q in e["stem"].lower() or q in e["category"].lower()
        ]
    if not entries:
        return [('NONE', "No Grunge Found", "No grunge textures match the current category or search filter", 'TEXTURE', 0)]

    items = []
    for idx, info in enumerate(entries):
        icon_id = 'TEXTURE'
        if _PREVIEW_COLLECTION is not None:
            key = info["id"]
            if key in _PREVIEW_COLLECTION:
                icon_id = _PREVIEW_COLLECTION[key].icon_id
            else:
                try:
                    thumb = _PREVIEW_COLLECTION.load(key, info["preview_path"], 'IMAGE')
                    icon_id = thumb.icon_id
                except Exception:
                    icon_id = 'TEXTURE'
        items.append((info["id"], info["label"], info["tooltip"], icon_id, idx))
    _GRUNGE_ENUM_CACHE[cache_key] = items
    return items


# ---------------------------------------------------------------------------
# Substance-Painter-Style Multi-Layer Mask Stack & High-Speed LUT Compositor
# ---------------------------------------------------------------------------

_MASK_GEN_TYPE_TO_INT = {
    'NONE': 0,
    'AO': 1,
    'CURVATURE': 2,
    'CAVITY': 3,
    'EDGE_WEAR': 4,
    'NORMAL_UP': 5,
    'POSITION_Z': 6,
}

_MASK_GEN_LABELS = {
    'EDGE_WEAR': "Edge Wear",
    'CURVATURE': "Curvature",
    'CAVITY': "Cavity Dirt",
    'AO': "Ambient Occlusion",
    'NORMAL_UP': "Top Dust",
    'POSITION_Z': "Height Gradient",
}

_MASK_FILTER_LABELS = {
    'BLUR': "Filter: Blur",
    'BLUR_SLOPE': "Filter: Blur Slope",
    'SHARPEN': "Filter: Sharpen",
    'DILATE_ERODE': "Filter: Grow / Shrink",
    'BEVEL': "Filter: Bevel",
    'THRESHOLD': "Filter: Threshold",
    'POSTERIZE': "Filter: Posterize",
    'HIGHPASS': "Filter: Highpass",
}

_BLEND_SHORT_LABELS = {
    'REPLACE': "Norm",
    'MULTIPLY': "Mul",
    'SUBTRACT': "Sub",
    'ADD': "Add",
    'SCREEN': "Scrn",
    'OVERLAY': "Ovly",
    'MAX': "Max",
    'MIN': "Min",
}

_LAYER_BLEND_SHORT_LABELS = {
    'MIX': "",
    'DARKEN': "Dark",
    'MUL': "Mul",
    'LIGHTEN': "Lite",
    'SCREEN': "Scrn",
    'ADD': "Add",
    'OVERLAY': "Ovly",
    'SOFTLIGHT': "Soft",
    'DIFFERENCE': "Diff",
    'SUB': "Sub",
    'HUE': "Hue",
    'SATURATION': "Sat",
    'COLOR': "Col",
    'LUMINOSITY': "Lum",
}


def _make_default_mask_effect(
    effect_type='GRUNGE',
    uid=1,
    is_first=True,
    gen_type='EDGE_WEAR',
    fill_source='GRUNGE',
    filter_type='BLUR',
):
    if effect_type == 'GENERATOR':
        default_blend = 'REPLACE' if is_first else 'ADD'
        name = _MASK_GEN_LABELS.get(gen_type, "Generator")
    elif effect_type == 'PAINT':
        default_blend = 'REPLACE'  # Normal (Paint White = Reveal, Paint Black = Hide)
        name = "Paint Mask"
    elif effect_type == 'LEVELS':
        default_blend = 'REPLACE'
        name = "Levels"
    elif effect_type == 'FILTER':
        default_blend = 'REPLACE'
        name = _MASK_FILTER_LABELS.get(filter_type, "Filter: Blur")
    elif fill_source == 'CUSTOM_IMAGE':
        default_blend = 'REPLACE' if is_first else 'ADD'
        name = "Bitmap Fill"
    else:
        default_blend = 'REPLACE' if is_first else 'MULTIPLY'
        name = "Grunge Fill"

    is_custom_img = (effect_type == 'GRUNGE' and fill_source == 'CUSTOM_IMAGE')
    def_scale = 1.0 if is_custom_img else 2.0
    def_contrast = 0.0 if is_custom_img else 0.30

    return {
        "uid": int(uid),
        "name": name,
        "type": effect_type,         # 'GENERATOR' | 'PAINT' | 'GRUNGE' | 'LEVELS' | 'FILTER'
        "enabled": True,
        "blend_mode": default_blend, # 'REPLACE' | 'MULTIPLY' | 'SUBTRACT' | 'ADD' | 'SCREEN' | 'OVERLAY' | 'MAX' | 'MIN'
        "opacity": 1.0,
        "invert": False,

        # GENERATOR settings
        "gen_type": gen_type,
        "gen_balance": 0.50,
        "gen_contrast": 0.45,
        "gen_radius": 0.35,
        "gen_noise": 0.0,
        "gen_noise_scale": 8.0,
        "gen_global_ao": True,

        # GRUNGE / IMAGE FILL (Substance Mask Fill) settings
        "category": "CUSTOM" if is_custom_img else "BITMAP_GRUNGES",
        "texture": "BITMAP_GRUNGES::grunge_concrete_cracked",
        "custom_image": "",
        "channel": "AUTO",           # 'AUTO' | 'LUMINANCE' | 'ALPHA' | 'RED' | 'GREEN' | 'BLUE'
        "proj_mode": "REPEAT" if is_custom_img else "TRIPLANAR",
        "uv_wrap": "REPEAT",         # 'REPEAT' (Tile) | 'CLAMP' (Single Stamp)
        "scale": def_scale,
        "scale_x": def_scale,
        "scale_y": def_scale,
        "lock_scale": True,
        "balance": 0.50,
        "contrast": def_contrast,
        "rotation": 0.0,
        "offset_x": 0.0,
        "offset_y": 0.0,
        "box_blend": 0.50,

        # LEVELS settings
        "lvl_balance": 0.50,
        "lvl_contrast": 0.0,

        # FILTER (Substance Mask Filter: Blur, Blur Slope, Sharpen, Grow/Shrink, Bevel, Threshold, Posterize, Highpass)
        "filter_type": filter_type,
        "flt_radius": 6.0,
        "flt_strength": 1.0,
        "flt_scale": 8.0,
    }


_DEFAULT_LAYER_MASK_STATE = {
    "ui_mode": "GRUNGE",
    "ui_mode_set": False,
    "active": False,
    "base_value": 255,
    "active_effect_uid": 0,
    "next_effect_uid": 1,
    "effects": [],
}

# Runtime caches for zero-allocation / sub-15ms evaluation
_RUNTIME_LAYER_STATES = {}       # (node_ptr, layer_uid) -> state dict
_NODE_JSON_CACHE = {}            # node_ptr -> dict of {str(layer_uid): state_dict}
_NODE_LAST_JSON_STR = {}         # node_ptr -> str (detects Undo/Redo/Reload changes to node['_grunge_layers_json'])
_DIRTY_JSON_NODES = {}           # node_ptr -> node (debounced JSON serialization for slider drags)
_EFFECT_RAW_CACHE = {}           # (node_ptr, layer_uid, effect_uid) -> {"sig": tuple, "raw_u8": ndarray}
_PAINT_EFFECT_BUFFERS = {}       # (node_ptr, layer_uid, effect_uid) -> {"w": int, "h": int, "val_u8": ndarray, "alpha_u8": ndarray, "rev": int}
_STACK_BELOW_CACHE = {}          # (node_ptr, layer_uid) -> {"sig": tuple, "below_u8": ndarray}
_PAIR_IDX_CACHE = {}             # (node_ptr, layer_uid) -> {"w": int, "h": int, "below_sig": tuple, "raw_id": int, "idx_u16": ndarray}
_LAST_COMPOSITED_MASK = {}       # (node_ptr, layer_uid) -> ndarray (uint8[w*h]) or None when deferred
_LAST_MASK_SAMPLE = {}           # (node_ptr, layer_uid) -> ndarray (256-byte strided sample for 5us stroke detection)
_PENDING_STROKE_SNAPSHOT = {}    # (node_ptr, layer_uid) -> int hash (detects when brush stroke finishes)
_PENDING_HIRES_REFINE = {}       # (node_ptr, layer_uid) -> (node, layer, float_timestamp)
_GLOBAL_AO_CACHE = {}            # (ob_ptr, w, h, radius_q, scene_sig) -> ndarray uint8[w*h]
_UV_TEXEL_GRID_CACHE = {}        # (mesh_ptr, n_v, n_p, w, h) -> dict
_LUT_THREAD_POOL = None
_INTERNAL_REPROJECTING = False


def _get_lut_thread_pool():
    global _LUT_THREAD_POOL
    if _LUT_THREAD_POOL is None:
        from concurrent.futures import ThreadPoolExecutor
        _LUT_THREAD_POOL = ThreadPoolExecutor(max_workers=8)
    return _LUT_THREAD_POOL


def _parallel_take_u32(lut_u32, idx_arr, dst_u32, raw_u8=None, below_u8=None, idx_pairs=None):
    """Run GIL-free np.take across 8 worker threads directly into dst_u32 (and optionally update idx_pairs)."""
    import numpy as np
    n = len(dst_u32)
    if n < 262144:
        if idx_pairs is not None:
            if below_u8 is not None:
                idx_pairs[:, 1] = below_u8
            if raw_u8 is not None:
                idx_pairs[:, 0] = raw_u8
        np.take(lut_u32, idx_arr, out=dst_u32)
        return

    pool = _get_lut_thread_pool()
    step = (n + 7) // 8
    chunks = [(i * step, min((i + 1) * step, n)) for i in range(8) if i * step < n]

    def _worker(ab):
        a, b = ab
        if idx_pairs is not None:
            if below_u8 is not None:
                idx_pairs[a:b, 1] = below_u8[a:b]
            if raw_u8 is not None:
                idx_pairs[a:b, 0] = raw_u8[a:b]
        np.take(lut_u32, idx_arr[a:b], out=dst_u32[a:b])

    list(pool.map(_worker, chunks))


def _parallel_take_u8(lut_u8, idx_arr, dst_u8, raw_u8=None, below_u8=None, idx_pairs=None):
    """Run GIL-free np.take across 8 worker threads into dst_u8."""
    import numpy as np
    n = len(dst_u8)
    if n < 262144:
        if idx_pairs is not None:
            if below_u8 is not None:
                idx_pairs[:, 1] = below_u8
            if raw_u8 is not None:
                idx_pairs[:, 0] = raw_u8
        np.take(lut_u8, idx_arr, out=dst_u8)
        return

    pool = _get_lut_thread_pool()
    step = (n + 7) // 8
    chunks = [(i * step, min((i + 1) * step, n)) for i in range(8) if i * step < n]

    def _worker(ab):
        a, b = ab
        if idx_pairs is not None:
            if below_u8 is not None:
                idx_pairs[a:b, 1] = below_u8[a:b]
            if raw_u8 is not None:
                idx_pairs[a:b, 0] = raw_u8[a:b]
        np.take(lut_u8, idx_arr[a:b], out=dst_u8[a:b])

    list(pool.map(_worker, chunks))


def _layer_uid(layer):
    if layer is None:
        return 0
    try:
        return int(_C_TexPaintLayer.from_address(layer.as_pointer()).uid)
    except Exception:
        return 0


def _migrate_legacy_state_if_needed(st, layer=None):
    """Upgrade single-grunge state dicts or existing Smart Masks into the multi-effect stack format."""
    if "effects" not in st or not isinstance(st["effects"], list):
        st["effects"] = []
    if "next_effect_uid" not in st:
        st["next_effect_uid"] = 1
    if "base_value" not in st:
        st["base_value"] = 255

    # Migrate legacy single-grunge state if present
    if not st["effects"] and st.get("active") and "texture" in st:
        uid = st["next_effect_uid"]
        st["next_effect_uid"] = uid + 1
        eff = _make_default_mask_effect('GRUNGE', uid=uid, is_first=True)
        for k in ("category", "texture", "custom_image", "proj_mode", "blend_mode", "scale", "balance", "contrast", "rotation", "offset_x", "offset_y", "box_blend", "invert"):
            if k in st:
                eff[k] = st[k]
        info = _GRUNGE_LOOKUP.get(eff["texture"])
        if info:
            eff["name"] = info["label"]
        st["effects"].append(eff)
        st["active_effect_uid"] = uid

    # If the layer already had a C++ Smart Mask generator set on it and effects is empty, adopt it
    if not st["effects"] and layer is not None:
        gen_t = getattr(layer, "mask_gen_type", 'NONE')
        if gen_t != 'NONE':
            uid = st["next_effect_uid"]
            st["next_effect_uid"] = uid + 1
            eff = _make_default_mask_effect('GENERATOR', uid=uid, is_first=True, gen_type=gen_t)
            eff["gen_balance"] = float(getattr(layer, "mask_gen_balance", 0.5))
            eff["gen_contrast"] = float(getattr(layer, "mask_gen_contrast", 0.45))
            eff["gen_radius"] = float(getattr(layer, "mask_gen_radius", 0.35))
            eff["gen_noise"] = float(getattr(layer, "mask_gen_noise", 0.0))
            eff["gen_noise_scale"] = float(getattr(layer, "mask_gen_noise_scale", 8.0))
            eff["invert"] = bool(getattr(layer, "mask_gen_invert", False))
            st["effects"].append(eff)
            st["active_effect_uid"] = uid
            st["active"] = True

    if st["effects"]:
        valid_uids = {e["uid"] for e in st["effects"]}
        if st.get("active_effect_uid") not in valid_uids:
            st["active_effect_uid"] = st["effects"][-1]["uid"]
    else:
        st["active_effect_uid"] = 0
    return st


def _find_layer_by_uid(node, target_uid):
    if node is None or target_uid <= 0:
        return None
    for l_item in getattr(node, "layers", ()):
        if _layer_uid(l_item) == target_uid:
            return l_item
    return None


def _canonical_mask_owner_node(node):
    """Return the primary Base Color ShaderNodeTexPaint in a 3D material node tree so all PBR channels share one Mask Stack per layer UID."""
    if node is None:
        return None
    ntree = getattr(node, "id_data", None)
    if ntree is None or getattr(ntree, "name", "") == ".TexPaint_2D_Layers":
        return node
    try:
        fallback = None
        for cand in ntree.nodes:
            if cand.bl_idname == "ShaderNodeTexPaint" and cand.image is not None:
                if fallback is None:
                    fallback = cand
                out_c = cand.outputs.get("Color")
                if out_c and out_c.is_linked:
                    for lk in out_c.links:
                        if lk.to_node.bl_idname == "ShaderNodeBsdfPrincipled" and lk.to_socket and lk.to_socket.name == "Base Color":
                            return cand
                lbl = (cand.label or cand.image.name or cand.name).lower().replace("_", "").replace(" ", "")
                if "base" in lbl or lbl == "paintedtexture":
                    return cand
        return fallback or node
    except Exception:
        return node


def _sync_layer_mask_to_companion_channels(node, layer=None):
    """Mirror `layer.mask_ibuf` bytes and mask flags to all companion PBR channel nodes in the same material tree that share `layer`'s UID."""
    if node is None:
        return
    if layer is None:
        layer = getattr(node, "active_layer", None)
    if layer is None:
        return
    ntree = getattr(node, "id_data", None)
    if ntree is None or getattr(ntree, "name", "") == ".TexPaint_2D_Layers":
        return
    target_uid = _layer_uid(layer)
    if target_uid <= 0:
        return
    c_src = _C_TexPaintLayer.from_address(layer.as_pointer())
    use_m = bool(getattr(layer, "use_mask", False))
    inv_m = bool(getattr(layer, "invert_mask", False))

    for other_node in ntree.nodes:
        if other_node == node or other_node.bl_idname != "ShaderNodeTexPaint" or other_node.image is None:
            continue
        other_layer = _find_layer_by_uid(other_node, target_uid)
        if other_layer is None:
            continue
        if not use_m:
            if getattr(other_layer, "use_mask", False):
                other_layer.use_mask = False
            continue
        if not getattr(other_layer, "use_mask", False):
            other_layer.use_mask = True
        if getattr(other_layer, "invert_mask", False) != inv_m:
            other_layer.invert_mask = inv_m
        c_dst = _C_TexPaintLayer.from_address(other_layer.as_pointer())
        if not c_dst.mask_ibuf:
            other_layer.opacity = other_layer.opacity
            c_dst = _C_TexPaintLayer.from_address(other_layer.as_pointer())
        if c_src.mask_ibuf and c_dst.mask_ibuf:
            imb_s = _C_ImBuf.from_address(c_src.mask_ibuf)
            imb_d = _C_ImBuf.from_address(c_dst.mask_ibuf)
            w, h = int(imb_s.x), int(imb_s.y)
            if int(imb_d.x) == w and int(imb_d.y) == h and w > 0 and h > 0:
                if imb_s.byte_data and imb_d.byte_data:
                    ctypes.memmove(imb_d.byte_data, imb_s.byte_data, w * h * 4)
                    c_dst.runtime_dirty = 1
                elif imb_s.float_data and imb_d.float_data:
                    ctypes.memmove(imb_d.float_data, imb_s.float_data, w * h * 16)
                    c_dst.runtime_dirty = 1
                other_layer.opacity = other_layer.opacity


def _get_layer_grunge_state(node, layer=None):
    """Read-only lookup of the Mask Stack state for (node, layer), safe to call inside UI draw()."""
    if node is None:
        return dict(_DEFAULT_LAYER_MASK_STATE, effects=[])
    if layer is None:
        layer = getattr(node, "active_layer", None)
    uid = _layer_uid(layer)
    if uid <= 0:
        return dict(_DEFAULT_LAYER_MASK_STATE, effects=[])

    owner_node = _canonical_mask_owner_node(node)
    node_ptr = owner_node.as_pointer()
    raw_json = owner_node.get("_grunge_layers_json", "")
    if node_ptr not in _DIRTY_JSON_NODES and _NODE_LAST_JSON_STR.get(node_ptr) != raw_json:
        _NODE_LAST_JSON_STR[node_ptr] = raw_json
        _NODE_JSON_CACHE.pop(node_ptr, None)
        for k in [k for k in _RUNTIME_LAYER_STATES if k[0] == node_ptr]:
            _RUNTIME_LAYER_STATES.pop(k, None)
        for k in [k for k in _PAINT_EFFECT_BUFFERS if k[0] == node_ptr]:
            _PAINT_EFFECT_BUFFERS.pop(k, None)

    key = (node_ptr, uid)
    st = _RUNTIME_LAYER_STATES.get(key)
    if st is not None:
        return st

    st = dict(_DEFAULT_LAYER_MASK_STATE, effects=[])
    all_data = _NODE_JSON_CACHE.get(node_ptr)
    if all_data is None:
        if raw_json:
            try:
                all_data = json.loads(raw_json)
            except Exception:
                all_data = {}
        if not isinstance(all_data, dict):
            all_data = {}
        _NODE_JSON_CACHE[node_ptr] = all_data
        _NODE_LAST_JSON_STR[node_ptr] = raw_json

    saved = all_data.get(str(uid))
    if isinstance(saved, dict):
        st.update(saved)

    _migrate_legacy_state_if_needed(st, layer)
    _RUNTIME_LAYER_STATES[key] = st
    return st


def _flush_dirty_node_json(node=None):
    """Write pending Mask Stack state dicts into node['_grunge_layers_json']."""
    if node is not None:
        owner_node = _canonical_mask_owner_node(node)
        node_ptr = owner_node.as_pointer()
        all_data = _NODE_JSON_CACHE.get(node_ptr)
        if all_data is not None:
            try:
                dumped = json.dumps(all_data)
                owner_node["_grunge_layers_json"] = dumped
                _NODE_LAST_JSON_STR[node_ptr] = dumped
            except Exception:
                pass
        _DIRTY_JSON_NODES.pop(node_ptr, None)
        return

    for node_ptr, n_ref in list(_DIRTY_JSON_NODES.items()):
        all_data = _NODE_JSON_CACHE.get(node_ptr)
        if all_data is not None and n_ref is not None:
            try:
                dumped = json.dumps(all_data)
                n_ref["_grunge_layers_json"] = dumped
                _NODE_LAST_JSON_STR[node_ptr] = dumped
            except Exception:
                pass
        _DIRTY_JSON_NODES.pop(node_ptr, None)


def _save_layer_grunge_state(node, layer, updates=None, flush_json=True):
    """Persist Mask Stack state updates for (node, layer) into runtime cache and (optionally debounced) IDProperty."""
    if node is None:
        return None
    if layer is None:
        layer = getattr(node, "active_layer", None)
    uid = _layer_uid(layer)
    if uid <= 0:
        return None

    owner_node = _canonical_mask_owner_node(node)
    node_ptr = owner_node.as_pointer()
    st = _get_layer_grunge_state(owner_node, layer)
    if updates and updates is not st:
        st.update(updates)
    _migrate_legacy_state_if_needed(st, layer)
    _RUNTIME_LAYER_STATES[(node_ptr, uid)] = st

    all_data = _NODE_JSON_CACHE.get(node_ptr)
    if not isinstance(all_data, dict):
        try:
            raw_json = owner_node.get("_grunge_layers_json", "")
            all_data = json.loads(raw_json) if raw_json else {}
            if not isinstance(all_data, dict):
                all_data = {}
        except Exception:
            all_data = {}
        _NODE_JSON_CACHE[node_ptr] = all_data

    all_data[str(uid)] = st
    if flush_json or bpy.app.background:
        _flush_dirty_node_json(owner_node)
    else:
        _DIRTY_JSON_NODES[node_ptr] = owner_node
    return None


def _get_active_mask_effect(node, layer=None):
    """Return the currently selected mask effect dict in the layer's Mask Stack, or None."""
    st = _get_layer_grunge_state(node, layer)
    effects = st.get("effects", [])
    if not effects:
        return None
    act_uid = st.get("active_effect_uid", 0)
    for eff in effects:
        if eff["uid"] == act_uid:
            return eff
    return effects[-1]


def _update_active_mask_effect(node, layer, updates, ensure_type=None, flush_json=True):
    """Update fields on the active mask effect (or create one if stack is empty) and save."""
    if layer is None and node is not None:
        layer = getattr(node, "active_layer", None)
    st = _get_layer_grunge_state(node, layer)
    effects = st.get("effects", [])
    eff = _get_active_mask_effect(node, layer)
    if eff is None or (ensure_type is not None and eff.get("type") != ensure_type):
        if layer is not None and not getattr(layer, "use_mask", False):
            layer.use_mask = True
        uid = int(st.get("next_effect_uid", 1))
        st["next_effect_uid"] = uid + 1
        eff = _make_default_mask_effect(
            effect_type=ensure_type or 'GRUNGE',
            uid=uid,
            is_first=(len(effects) == 0),
        )
        effects.append(eff)
        st["effects"] = effects
        st["active_effect_uid"] = uid

    eff.update(updates)
    if eff["type"] == 'GRUNGE' and "texture" in updates:
        _ensure_grunge_catalog()
        info = _GRUNGE_LOOKUP.get(eff["texture"])
        if info:
            eff["name"] = info["label"]
    elif eff["type"] == 'GRUNGE' and "custom_image" in updates and "name" not in updates:
        im_name = str(updates["custom_image"] or "").strip()
        if im_name:
            eff["name"] = os.path.splitext(im_name)[0][:28]
    elif eff["type"] == 'GENERATOR' and "gen_type" in updates:
        eff["name"] = _MASK_GEN_LABELS.get(eff["gen_type"], "Generator")
    elif eff["type"] == 'FILTER' and "filter_type" in updates:
        eff["name"] = _MASK_FILTER_LABELS.get(eff["filter_type"], "Filter: Blur")

    st["active"] = True
    _save_layer_grunge_state(node, layer, st, flush_json=flush_json)
    return eff


def _ensure_paint_effect_buffer(node_ptr, layer_uid, eff, w, h):
    """Ensure the runtime (val_u8, alpha_u8) arrays exist for a PAINT mask effect."""
    import numpy as np
    pkey = (node_ptr, layer_uid, int(eff["uid"]))
    buf = _PAINT_EFFECT_BUFFERS.get(pkey)
    if buf is not None and buf["w"] == w and buf["h"] == h:
        return buf

    val_u8 = np.zeros(w * h, dtype=np.uint8)
    alpha_u8 = np.zeros(w * h, dtype=np.uint8)

    # Restore from compressed base64 if saved in effect dict
    b64_data = eff.get("paint_z64", "")
    if b64_data and eff.get("paint_w") == w and eff.get("paint_h") == h:
        try:
            raw_bytes = zlib.decompress(base64.b64decode(b64_data))
            if len(raw_bytes) == w * h * 2:
                arr = np.frombuffer(raw_bytes, dtype=np.uint8)
                val_u8[:] = arr[:w * h]
                alpha_u8[:] = arr[w * h:]
        except Exception:
            pass

    buf = {
        "w": w,
        "h": h,
        "val_u8": val_u8,
        "alpha_u8": alpha_u8,
        "rev": 1,
    }
    _PAINT_EFFECT_BUFFERS[pkey] = buf
    return buf


def _sync_paint_effect_z64(eff, pbuf):
    """Compress the PAINT sub-layer (val_u8, alpha_u8) buffers into eff['paint_z64'] for Undo/Redo and .blend persistence."""
    import numpy as np
    if eff is None or pbuf is None:
        return
    val_u8 = pbuf.get("val_u8")
    alpha_u8 = pbuf.get("alpha_u8")
    if val_u8 is None or alpha_u8 is None:
        return
    if not np.any(alpha_u8):
        eff.pop("paint_z64", None)
        eff.pop("paint_w", None)
        eff.pop("paint_h", None)
        return
    combined = np.concatenate((val_u8, alpha_u8)).tobytes()
    eff["paint_w"] = int(pbuf["w"])
    eff["paint_h"] = int(pbuf["h"])
    eff["paint_z64"] = base64.b64encode(zlib.compress(combined, level=1)).decode("ascii")


def _absorb_pending_brush_strokes(node, layer=None):
    """Detect if the user painted into layer.mask_ibuf with a brush and absorb the stroke delta into the PAINT effect."""
    if _INTERNAL_REPROJECTING or node is None:
        return False
    if layer is None:
        layer = getattr(node, "active_layer", None)
    if layer is None or not getattr(layer, "use_mask", False):
        return False

    c_layer = _C_TexPaintLayer.from_address(layer.as_pointer())
    if not c_layer.mask_ibuf:
        return False
    c_imb = _C_ImBuf.from_address(c_layer.mask_ibuf)
    w, h = int(c_imb.x), int(c_imb.y)
    if w <= 0 or h <= 0 or not c_imb.byte_data:
        return False

    node_ptr = node.as_pointer()
    layer_uid = int(c_layer.uid)
    key = (node_ptr, layer_uid)

    import numpy as np
    buf = (ctypes.c_uint8 * (w * h * 4)).from_address(c_imb.byte_data)
    cur_u8 = np.frombuffer(buf, dtype=np.uint8).reshape(-1, 4)[:, 0]

    # Ultra-fast 1024-point strided sample check (~0.008 ms) before scanning 4.2M pixels
    step = max(1, (w * h) // 1024)
    sample_prev = _LAST_MASK_SAMPLE.get(key)
    if sample_prev is not None and len(sample_prev) == len(cur_u8[::step]):
        if np.array_equal(cur_u8[::step], sample_prev):
            return False

    last_u8 = _LAST_COMPOSITED_MASK.get(key)
    if last_u8 is None or len(last_u8) != w * h:
        _LAST_COMPOSITED_MASK[key] = cur_u8.copy()
        _LAST_MASK_SAMPLE[key] = cur_u8[::step].copy()
        return False

    if np.array_equal(cur_u8, last_u8):
        _LAST_MASK_SAMPLE[key] = cur_u8[::step].copy()
        return False

    st = _get_layer_grunge_state(node, layer)
    effects = st.get("effects", [])
    if not effects:
        return False

    # Find target PAINT effect: active effect if it's PAINT, otherwise topmost enabled PAINT effect, otherwise auto-create one on top
    target_eff = None
    target_idx = -1
    act_uid = st.get("active_effect_uid", 0)
    for idx, eff in enumerate(effects):
        if eff["uid"] == act_uid and eff["type"] == 'PAINT' and eff.get("enabled", True):
            target_eff = eff
            target_idx = idx
            break

    if target_eff is None:
        for idx in range(len(effects) - 1, -1, -1):
            if effects[idx]["type"] == 'PAINT' and effects[idx].get("enabled", True):
                target_eff = effects[idx]
                target_idx = idx
                break

    if target_eff is None:
        uid = int(st.get("next_effect_uid", 1))
        st["next_effect_uid"] = uid + 1
        target_eff = _make_default_mask_effect('PAINT', uid=uid, is_first=False)
        effects.append(target_eff)
        target_idx = len(effects) - 1
        st["active_effect_uid"] = uid
        _save_layer_grunge_state(node, layer, st)

    canon_ptr = _canonical_mask_owner_node(node).as_pointer()
    pbuf = _ensure_paint_effect_buffer(canon_ptr, layer_uid, target_eff, w, h)
    val_u8 = pbuf["val_u8"]
    alpha_u8 = pbuf["alpha_u8"]

    diff_idx = np.flatnonzero(cur_u8 != last_u8)
    if len(diff_idx) == 0:
        return False

    c = cur_u8[diff_idx].astype(np.float32)
    l = last_u8[diff_idx].astype(np.float32)

    brighter = c > l
    s_alpha = np.where(
        brighter,
        (c - l) / np.maximum(255.0 - l, 1.0),
        (l - c) / np.maximum(l, 1.0),
    )
    s_alpha = np.clip(s_alpha, 0.0, 1.0)
    s_val = np.where(brighter, 1.0, 0.0)

    old_a = alpha_u8[diff_idx].astype(np.float32) * (1.0 / 255.0)
    old_v = val_u8[diff_idx].astype(np.float32) * (1.0 / 255.0)

    out_a = s_alpha + old_a * (1.0 - s_alpha)
    out_v = np.where(
        out_a > 1e-5,
        (s_val * s_alpha + old_v * old_a * (1.0 - s_alpha)) / np.maximum(out_a, 1e-5),
        0.0,
    )

    val_u8[diff_idx] = (np.clip(out_v, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)
    alpha_u8[diff_idx] = (np.clip(out_a, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)
    pbuf["rev"] = int(pbuf.get("rev", 1)) + 1
    _sync_paint_effect_z64(target_eff, pbuf)
    _save_layer_grunge_state(node, layer, st, flush_json=True)

    # Check if there are enabled effects ABOVE target_idx or if target_eff has non-default opacity/invert/blend
    has_upper_effects = any(e.get("enabled", True) for e in effects[target_idx + 1:])
    needs_recomposite = (
        has_upper_effects or
        target_eff.get("blend_mode", "REPLACE") != 'REPLACE' or
        abs(float(target_eff.get("opacity", 1.0)) - 1.0) > 1e-4 or
        bool(target_eff.get("invert", False))
    )

    if needs_recomposite:
        _evaluate_layer_grunge_mask(node, layer, force_reproject=False, skip_absorb=True)
    else:
        last_u8[diff_idx] = cur_u8[diff_idx]
        _LAST_MASK_SAMPLE[key] = cur_u8[::step].copy()
        _sync_layer_mask_to_companion_channels(node, layer)
    return True


_OBJECT_DATA_PTR_OFFSET = None


def _get_object_data_ptr_slot(ob):
    """Return (ctypes.c_void_p slot, orig_value) for `Object::data` on `ob`."""
    global _OBJECT_DATA_PTR_OFFSET
    if ob is None or ob.data is None:
        return None, None
    ob_ptr = ob.as_pointer()
    mesh_ptr = ob.data.as_pointer()
    if _OBJECT_DATA_PTR_OFFSET is not None:
        slot = ctypes.c_void_p.from_address(ob_ptr + _OBJECT_DATA_PTR_OFFSET)
        if slot.value == mesh_ptr:
            return slot, mesh_ptr
    for off in range(0, 1024, 8):
        slot = ctypes.c_void_p.from_address(ob_ptr + off)
        if slot.value == mesh_ptr:
            _OBJECT_DATA_PTR_OFFSET = off
            return slot, mesh_ptr
    return None, None


def _find_all_mesh_objects_for_node(node, context=None):
    """Return `(ordered_mesh_objects, first_c_ob)` for all mesh objects using `node`'s material.
    `ordered_mesh_objects` places the active object LAST so its UV islands win when UVs overlap.
    `first_c_ob` is the first mesh object in `bpy.data.objects` that C++ `find_mesh_for_ntree` resolves.
    """
    active_ob = None
    if context is not None:
        active_ob = getattr(context, "active_object", None) or getattr(context, "image_paint_object", None)
    if active_ob is None and bpy.context is not None:
        active_ob = getattr(bpy.context, "active_object", None) or getattr(bpy.context, "image_paint_object", None)
    if active_ob is not None and (active_ob.type != 'MESH' or active_ob.data is None):
        active_ob = None

    mat = _get_material_for_node(node) if node is not None else None
    first_c_ob = None
    fallback_c_ob = None
    sharing_obs = []

    for ob in bpy.data.objects:
        if ob.type != 'MESH' or ob.data is None:
            continue
        if fallback_c_ob is None:
            fallback_c_ob = ob
        uses_mat = False
        if mat is not None:
            for slot in ob.material_slots:
                if slot.material == mat:
                    uses_mat = True
                    break
        if uses_mat:
            if first_c_ob is None:
                first_c_ob = ob
            if len(ob.data.vertices) > 0 and len(ob.data.polygons) > 0:
                sharing_obs.append(ob)

    if first_c_ob is None:
        first_c_ob = fallback_c_ob

    if not sharing_obs:
        if active_ob is not None and len(active_ob.data.vertices) > 0 and len(active_ob.data.polygons) > 0:
            sharing_obs = [active_ob]
        elif first_c_ob is not None and len(first_c_ob.data.vertices) > 0 and len(first_c_ob.data.polygons) > 0:
            sharing_obs = [first_c_ob]

    if active_ob is not None and active_ob in sharing_obs and len(sharing_obs) > 1:
        sharing_obs = [o for o in sharing_obs if o != active_ob] + [active_ob]

    return sharing_obs, first_c_ob


def _find_mesh_object_for_node(node, context=None):
    sharing_obs, first_c_ob = _find_all_mesh_objects_for_node(node, context)
    if sharing_obs:
        return sharing_obs[-1]
    return first_c_ob


def _transform_widget_between_objects(loc_src, rot_src, scl_src, src_ob, dst_ob):
    """Transform a 3D projection widget (loc, rot_euler, scale) from src_ob local space into dst_ob local space."""
    if src_ob is None or dst_ob is None or src_ob == dst_ob:
        return tuple(loc_src), tuple(rot_src), tuple(scl_src)
    rel_mat = dst_ob.matrix_world.inverted_safe() @ src_ob.matrix_world
    loc_v = rel_mat @ Vector(loc_src)
    rot_m = Euler(rot_src, 'XYZ').to_matrix()
    rel_3x3 = rel_mat.to_3x3()
    dst_3x3 = rel_3x3 @ rot_m
    sx_sign = -1.0 if scl_src[0] < 0.0 else 1.0
    sy_sign = -1.0 if scl_src[1] < 0.0 else 1.0
    sz_sign = -1.0 if scl_src[2] < 0.0 else 1.0
    sx = max(abs(float(scl_src[0])) * max(dst_3x3.col[0].length, 1e-6), 1e-4) * sx_sign
    sy = max(abs(float(scl_src[1])) * max(dst_3x3.col[1].length, 1e-6), 1e-4) * sy_sign
    sz = max(abs(float(scl_src[2])) * max(dst_3x3.col[2].length, 1e-6), 1e-4) * sz_sign
    rot_dst = dst_3x3.normalized().to_euler('XYZ')
    return (
        (float(loc_v.x), float(loc_v.y), float(loc_v.z)),
        (float(rot_dst.x), float(rot_dst.y), float(rot_dst.z)),
        (sx, sy, sz),
    )


def _resolve_grunge_image(eff, interactive=False):
    cat = eff.get("category", "BITMAP_GRUNGES")
    if cat == 'CUSTOM':
        im_name = eff.get("custom_image", "")
        return bpy.data.images.get(im_name) if im_name else None

    _ensure_grunge_catalog()
    tex_id = eff.get("texture", "")
    info = _GRUNGE_LOOKUP.get(tex_id)
    if info is None or info.get("category") != cat:
        entries = _GRUNGE_CATALOG.get(cat, [])
        if not entries:
            return None
        info = entries[0]
        eff["texture"] = info["id"]

    # During interactive Scale/Tile/Rotation/Gizmo dragging, use the 512x512 L2-cache-resident 8-bit preview PNG
    # if the full-res 2K image isn't already loaded, or for instant L2 cache locality; idle timer refines to 2K.
    if interactive:
        prev_path = info.get("preview_path", "")
        if prev_path and os.path.isfile(prev_path):
            try:
                return bpy.data.images.load(prev_path, check_existing=True)
            except Exception:
                pass

    png_path = info.get("png_path", "")
    if not png_path or not os.path.isfile(png_path):
        return None
    try:
        return bpy.data.images.load(png_path, check_existing=True)
    except Exception:
        return None


def _read_mask_ibuf_r_u8(mask_ibuf_ptr, w, h):
    import numpy as np
    if not mask_ibuf_ptr:
        return np.zeros(w * h, dtype=np.uint8)
    c_imb = _C_ImBuf.from_address(mask_ibuf_ptr)
    if c_imb.byte_data:
        buf = (ctypes.c_uint8 * (w * h * 4)).from_address(c_imb.byte_data)
        return np.frombuffer(buf, dtype=np.uint8).reshape(-1, 4)[:, 0].copy()
    if c_imb.float_data:
        buf = (ctypes.c_float * (w * h * 4)).from_address(c_imb.float_data)
        arr_f = np.frombuffer(buf, dtype=np.float32).reshape(-1, 4)[:, 0]
        return (np.clip(arr_f, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)
    return np.zeros(w * h, dtype=np.uint8)


_MESH_MAP_SLOTS = (
    ("normal", "Normal (Tangent)", "NORMALS_FACE"),
    ("normal_ws", "World Space Normal", "ORIENTATION_GLOBAL"),
    ("id_map", "ID (Color / Part)", "COLOR"),
    ("ao", "Ambient Occlusion", "SHADING_RENDERED"),
    ("curvature", "Curvature", "SMOOTHCURVE"),
    ("position", "Position", "EMPTY_AXIS"),
    ("thickness", "Thickness", "MOD_SOLIDIFY"),
)

_DEFAULT_MESH_MAPS_STATE = {
    "use_baked_maps": True,
    "source_mode": "LOW_POLY",      # 'LOW_POLY' | 'HIGH_POLY_OB' | 'HIGH_POLY_COL'
    "high_poly_ob": "",
    "high_poly_col": "",
    "ao_scope": "GLOBAL",           # 'GLOBAL' (Surrounding Scene Objects) | 'MATERIAL' (Shared Parts) | 'SELF' (Isolated)
    "cage_dist": 0.08,
    "ao_dist": 0.65,
    "bake_res": "MATCH",            # 'MATCH' | '512' | '1024' | '2048' | '4096'
    "apply_normal_to_shader": True,
    "maps": {
        "normal": "",
        "normal_ws": "",
        "id_map": "",
        "ao": "",
        "curvature": "",
        "position": "",
        "thickness": "",
    },
}


def _get_mesh_maps_state(node):
    """Return the Substance-Painter-style Mesh Maps & High-Poly Baker state dict for `node`'s material."""
    st = dict(_DEFAULT_MESH_MAPS_STATE)
    st["maps"] = dict(_DEFAULT_MESH_MAPS_STATE["maps"])
    if node is None:
        return st
    owner = _canonical_mask_owner_node(node)
    raw = owner.get("_mesh_maps_json", "")
    if raw:
        try:
            saved = json.loads(raw)
            if isinstance(saved, dict):
                saved_maps = saved.get("maps")
                for k, v in saved.items():
                    if k != "maps":
                        st[k] = v
                if isinstance(saved_maps, dict):
                    for mk in st["maps"]:
                        if mk in saved_maps:
                            st["maps"][mk] = str(saved_maps[mk] or "")
        except Exception:
            pass
    return st


def _save_mesh_maps_state(node, updates=None, map_updates=None):
    """Persist Mesh Maps & High-Poly Baker state onto the canonical base-color `ShaderNodeTexPaint`."""
    if node is None:
        return None
    owner = _canonical_mask_owner_node(node)
    st = _get_mesh_maps_state(owner)
    if updates:
        for k, v in updates.items():
            if k != "maps":
                st[k] = v
    if map_updates:
        for mk, mv in map_updates.items():
            st["maps"][mk] = str(mv or "")
    try:
        owner["_mesh_maps_json"] = json.dumps(st)
    except Exception:
        pass
    return None


def _read_baked_image_channel_f32(ima, w, h, channel=0):
    """Read a channel (or grayscale) from `bpy.data.images` `ima` resized to `(h, w)` float32[0..1]."""
    import numpy as np
    if ima is None:
        return None
    iw, ih = int(ima.size[0]), int(ima.size[1])
    if iw <= 0 or ih <= 0:
        return None
    arr = np.empty(iw * ih * 4, dtype=np.float32)
    try:
        ima.pixels.foreach_get(arr)
    except Exception:
        return None
    ch_2d = arr.reshape(ih, iw, 4)[:, :, channel]
    if iw == w and ih == h:
        return np.clip(ch_2d, 0.0, 1.0)
    ys = np.clip((np.linspace(0.0, ih - 1.0, h)).astype(np.int32), 0, ih - 1)
    xs = np.clip((np.linspace(0.0, iw - 1.0, w)).astype(np.int32), 0, iw - 1)
    return np.clip(ch_2d[ys[:, None], xs[None, :]], 0.0, 1.0)


def _build_uv_texel_map(ob, grid_w, grid_h):
    """Vectorized UV triangle rasterizer returning covered texels `(pix_idx, tri_idx, w0, w1, w2, tri_verts, tri_loops)` at `(grid_w, grid_h)`.
    Cached by mesh pointer & topology so subsequent evaluations take 0.0ms.
    """
    import numpy as np
    if ob is None or ob.type != 'MESH' or ob.data is None:
        return None
    mesh = ob.data
    if not mesh.uv_layers or len(mesh.vertices) == 0 or len(mesh.polygons) == 0:
        return None

    ckey = (mesh.as_pointer(), len(mesh.vertices), len(mesh.polygons), int(grid_w), int(grid_h))
    cached = _UV_TEXEL_GRID_CACHE.get(ckey)
    if cached is not None:
        return cached

    mesh.calc_loop_triangles()
    n_tris = len(mesh.loop_triangles)
    n_loops = len(mesh.loops)
    if n_tris == 0 or n_loops == 0:
        return None

    tri_loops = np.empty(n_tris * 3, dtype=np.int32)
    tri_verts = np.empty(n_tris * 3, dtype=np.int32)
    mesh.loop_triangles.foreach_get("loops", tri_loops)
    mesh.loop_triangles.foreach_get("vertices", tri_verts)
    tri_loops = tri_loops.reshape(n_tris, 3)
    tri_verts = tri_verts.reshape(n_tris, 3)

    uv_layer = mesh.uv_layers.active or mesh.uv_layers[0]
    uvs = np.empty(n_loops * 2, dtype=np.float32)
    uv_layer.data.foreach_get("uv", uvs)
    uvs = uvs.reshape(n_loops, 2)

    uv0 = uvs[tri_loops[:, 0]] * np.array([grid_w, grid_h], dtype=np.float32)
    uv1 = uvs[tri_loops[:, 1]] * np.array([grid_w, grid_h], dtype=np.float32)
    uv2 = uvs[tri_loops[:, 2]] * np.array([grid_w, grid_h], dtype=np.float32)

    e0 = uv1 - uv0
    e1 = uv2 - uv0
    denom = e0[:, 0] * e1[:, 1] - e0[:, 1] * e1[:, 0]
    valid_tri = np.abs(denom) > 1e-6
    inv_denom = np.where(valid_tri, 1.0 / np.where(valid_tri, denom, 1.0), 0.0)

    xmin = np.clip(np.floor(np.minimum(uv0[:, 0], np.minimum(uv1[:, 0], uv2[:, 0])) - 0.5).astype(np.int32), 0, grid_w - 1)
    xmax = np.clip(np.ceil(np.maximum(uv0[:, 0], np.maximum(uv1[:, 0], uv2[:, 0])) + 0.5).astype(np.int32), 0, grid_w - 1)
    ymin = np.clip(np.floor(np.minimum(uv0[:, 1], np.minimum(uv1[:, 1], uv2[:, 1])) - 0.5).astype(np.int32), 0, grid_h - 1)
    ymax = np.clip(np.ceil(np.maximum(uv0[:, 1], np.maximum(uv1[:, 1], uv2[:, 1])) + 0.5).astype(np.int32), 0, grid_h - 1)

    span_x = xmax - xmin + 1
    span_y = ymax - ymin + 1
    valid_tri &= (span_x > 0) & (span_y > 0)

    buf_tri = np.full(grid_h * grid_w, -1, dtype=np.int32)
    buf_w1 = np.zeros(grid_h * grid_w, dtype=np.float32)
    buf_w2 = np.zeros(grid_h * grid_w, dtype=np.float32)

    # 1. Batch-vectorized rasterization for all small triangles (span <= 14x14)
    MAX_S = 14
    small_mask = valid_tri & (span_x <= MAX_S) & (span_y <= MAX_S)
    small_ids = np.flatnonzero(small_mask)
    if len(small_ids) > 0:
        # Process in chunks of 4096 triangles to stay in L2 cache
        oy = np.arange(MAX_S, dtype=np.int32)[None, :, None]
        ox = np.arange(MAX_S, dtype=np.int32)[None, None, :]
        for c_start in range(0, len(small_ids), 4096):
            s_idx = small_ids[c_start:c_start + 4096]
            px = xmin[s_idx, None, None] + ox
            py = ymin[s_idx, None, None] + oy
            in_box = (px <= xmax[s_idx, None, None]) & (py <= ymax[s_idx, None, None])
            dx = (px.astype(np.float32) + 0.5) - uv0[s_idx, 0, None, None]
            dy = (py.astype(np.float32) + 0.5) - uv0[s_idx, 1, None, None]
            inv_d = inv_denom[s_idx, None, None]
            w1 = (dx * e1[s_idx, 1, None, None] - dy * e1[s_idx, 0, None, None]) * inv_d
            w2 = (e0[s_idx, 0, None, None] * dy - e0[s_idx, 1, None, None] * dx) * inv_d
            w0 = 1.0 - w1 - w2
            hit = in_box & (w0 >= -0.02) & (w1 >= -0.02) & (w2 >= -0.02)
            t_loc, y_loc, x_loc = np.nonzero(hit)
            if len(t_loc) > 0:
                p_flat = py[t_loc, y_loc, x_loc] * grid_w + px[t_loc, y_loc, x_loc]
                buf_tri[p_flat] = s_idx[t_loc]
                buf_w1[p_flat] = np.clip(w1[t_loc, y_loc, x_loc], 0.0, 1.0)
                buf_w2[p_flat] = np.clip(w2[t_loc, y_loc, x_loc], 0.0, 1.0)

    # 2. Vectorized 2D meshgrid slice for any larger triangles (span > 14)
    large_ids = np.flatnonzero(valid_tri & (~small_mask))
    for ti in large_ids:
        x0, x1 = int(xmin[ti]), int(xmax[ti])
        y0, y1 = int(ymin[ti]), int(ymax[ti])
        ys, xs = np.mgrid[y0:y1 + 1, x0:x1 + 1]
        dx = (xs.astype(np.float32) + 0.5) - uv0[ti, 0]
        dy = (ys.astype(np.float32) + 0.5) - uv0[ti, 1]
        inv_d = float(inv_denom[ti])
        w1 = (dx * float(e1[ti, 1]) - dy * float(e1[ti, 0])) * inv_d
        w2 = (float(e0[ti, 0]) * dy - float(e0[ti, 1]) * dx) * inv_d
        w0 = 1.0 - w1 - w2
        hit = (w0 >= -0.015) & (w1 >= -0.015) & (w2 >= -0.015)
        if np.any(hit):
            p_flat = (ys[hit] * grid_w + xs[hit]).ravel()
            buf_tri[p_flat] = int(ti)
            buf_w1[p_flat] = np.clip(w1[hit].ravel(), 0.0, 1.0)
            buf_w2[p_flat] = np.clip(w2[hit].ravel(), 0.0, 1.0)

    cov_idx = np.flatnonzero(buf_tri >= 0)
    t_idx = buf_tri[cov_idx]
    w1_c = buf_w1[cov_idx]
    w2_c = buf_w2[cov_idx]
    w0_c = np.maximum(0.0, 1.0 - w1_c - w2_c)
    w_sum = np.maximum(w0_c + w1_c + w2_c, 1e-6)
    w0_c /= w_sum
    w1_c /= w_sum
    w2_c /= w_sum

    res = {
        "grid_w": int(grid_w),
        "grid_h": int(grid_h),
        "pix_idx": cov_idx,
        "tri_idx": t_idx,
        "w0": w0_c,
        "w1": w1_c,
        "w2": w2_c,
        "tri_verts": tri_verts,
        "tri_loops": tri_loops,
        "uvs": uvs,
    }
    _UV_TEXEL_GRID_CACHE[ckey] = res
    return res


def _dilate_2d_buffer_into_gutter(img_2d, cov_mask_2d, iterations=3):
    """Dilate covered UV island values by `iterations` pixels into the UV gutter to eliminate seam bleeding."""
    import numpy as np
    out = img_2d.copy()
    mask = cov_mask_2d.copy()
    for _ in range(iterations):
        unfilled = ~mask
        if not np.any(unfilled):
            break
        for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            nbr_m = np.roll(np.roll(mask, dy, axis=0), dx, axis=1)
            fill_here = unfilled & nbr_m
            if np.any(fill_here):
                nbr_v = np.roll(np.roll(out, dy, axis=0), dx, axis=1)
                out[fill_here] = nbr_v[fill_here]
                mask[fill_here] = True
                unfilled = ~mask
    return out


_HEMI_DIRS_10 = (
    (0.0, 0.0, 1.0),
    (0.5877, 0.0, 0.8090),
    (-0.5877, 0.0, 0.8090),
    (0.0, 0.5877, 0.8090),
    (0.0, -0.5877, 0.8090),
    (0.6881, 0.5000, 0.5257),
    (-0.6881, 0.5000, 0.5257),
    (0.6881, -0.5000, 0.5257),
    (-0.6881, -0.5000, 0.5257),
    (0.0, 0.8506, 0.5257),
)


def _build_world_bvh_for_objects(obs):
    """Build a single combined World-Space C++ `BVHTree` from a list of mesh objects `obs`."""
    import numpy as np
    from mathutils.bvhtree import BVHTree

    all_verts = []
    all_tris = []
    v_offset = 0
    for ob in obs:
        if ob is None or ob.type != 'MESH' or ob.data is None:
            continue
        mesh = ob.data
        nv = len(mesh.vertices)
        if nv == 0 or len(mesh.polygons) == 0:
            continue
        mesh.calc_loop_triangles()
        nt = len(mesh.loop_triangles)
        if nt == 0:
            continue
        v_co = np.empty(nv * 3, dtype=np.float32)
        mesh.vertices.foreach_get("co", v_co)
        v_co = v_co.reshape(nv, 3)
        mw = np.array(ob.matrix_world, dtype=np.float32)
        v_w = v_co @ mw[:3, :3].T + mw[:3, 3]
        t_v = np.empty(nt * 3, dtype=np.int32)
        mesh.loop_triangles.foreach_get("vertices", t_v)
        t_v = t_v.reshape(nt, 3) + v_offset
        all_verts.append(v_w)
        all_tris.append(t_v)
        v_offset += nv

    if not all_verts:
        return None
    V = np.vstack(all_verts).tolist()
    T = np.vstack(all_tris).tolist()
    return BVHTree.FromPolygons(V, T)


def _compute_part_external_scene_occlusion(ob_part, other_obs, w, h, radius):
    """Compute World-Space inter-object Ambient Occlusion (0..255 occlusion amount) cast by `other_obs` onto `ob_part`."""
    import numpy as np
    if ob_part is None or ob_part.data is None or not other_obs:
        return None

    # Filter other_obs to those whose world bounding box is within ao_dist of ob_part
    mw = ob_part.matrix_world
    pts = [mw @ Vector(co) for co in ob_part.bound_box]
    bmin = Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
    bmax = Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
    bsize = bmax - bmin
    max_ext = max(float(bsize.x), float(bsize.y), float(bsize.z), 1e-3)
    ao_dist = max((0.20 + 0.55 * float(radius)) * max_ext, 0.02)
    contact_dist = max(0.22 * max_ext, 0.01)

    nearby_obs = []
    for o in other_obs:
        if o == ob_part or o.type != 'MESH' or o.data is None or len(o.data.vertices) == 0:
            continue
        omw = o.matrix_world
        opts = [omw @ Vector(co) for co in o.bound_box]
        obmin = Vector((min(p.x for p in opts), min(p.y for p in opts), min(p.z for p in opts)))
        obmax = Vector((max(p.x for p in opts), max(p.y for p in opts), max(p.z for p in opts)))
        if (
            obmax.x >= bmin.x - ao_dist and obmin.x <= bmax.x + ao_dist and
            obmax.y >= bmin.y - ao_dist and obmin.y <= bmax.y + ao_dist and
            obmax.z >= bmin.z - ao_dist and obmin.z <= bmax.z + ao_dist
        ):
            nearby_obs.append(o)

    if not nearby_obs:
        return None

    scene_sig = tuple(
        (o.name, o.data.as_pointer(), len(o.data.vertices), tuple(round(v, 3) for row in o.matrix_world for v in row))
        for o in [ob_part] + nearby_obs
    )
    ckey = (ob_part.as_pointer(), int(w), int(h), round(float(radius), 2), scene_sig)
    cached = _GLOBAL_AO_CACHE.get(ckey)
    if cached is not None:
        return cached

    bvh = _build_world_bvh_for_objects(nearby_obs)
    if bvh is None:
        return None

    mesh = ob_part.data
    nv = len(mesh.vertices)
    v_co = np.empty(nv * 3, dtype=np.float32)
    v_no = np.empty(nv * 3, dtype=np.float32)
    mesh.vertices.foreach_get("co", v_co)
    mesh.vertices.foreach_get("normal", v_no)
    v_co = v_co.reshape(nv, 3)
    v_no = v_no.reshape(nv, 3)

    mw_np = np.array(mw, dtype=np.float32)
    rot_3x3 = mw_np[:3, :3]
    inv_t = np.linalg.inv(rot_3x3).T
    V_w = v_co @ rot_3x3.T + mw_np[:3, 3]
    N_w = v_no @ inv_t.T
    n_len = np.maximum(np.linalg.norm(N_w, axis=1, keepdims=True), 1e-6)
    N_w /= n_len

    v_occ = np.zeros(nv, dtype=np.float32)
    eps = 0.0025 * max_ext
    inv_ao_dist = 1.0 / ao_dist
    inv_contact = 1.0 / contact_dist

    # Evaluate hemisphere raycast + contact proximity per vertex against surrounding objects' BVH
    ray_cast = bvh.ray_cast
    find_nearest = bvh.find_nearest
    for vi in range(nv):
        px, py, pz = float(V_w[vi, 0]), float(V_w[vi, 1]), float(V_w[vi, 2])
        nx, ny, nz = float(N_w[vi, 0]), float(N_w[vi, 1]), float(N_w[vi, 2])
        orig = Vector((px + nx * eps, py + ny * eps, pz + nz * eps))

        # Orthonormal basis around vertex normal N
        if abs(nz) < 0.9:
            tx, ty, tz = -ny, nx, 0.0
        else:
            tx, ty, tz = 0.0, -nz, ny
        t_inv = 1.0 / max(math.hypot(tx, ty, tz), 1e-6)
        tx, ty, tz = tx * t_inv, ty * t_inv, tz * t_inv
        bx = ny * tz - nz * ty
        by = nz * tx - nx * tz
        bz = nx * ty - ny * tx

        occ_sum = 0.0
        for hx, hy, hz in _HEMI_DIRS_10:
            dx = tx * hx + bx * hy + nx * hz
            dy = ty * hx + by * hy + ny * hz
            dz = tz * hx + bz * hy + nz * hz
            hit_loc, _hit_no, _hit_idx, hit_d = ray_cast(orig, Vector((dx, dy, dz)), ao_dist)
            if hit_loc is not None and hit_d is not None:
                occ_sum += (1.0 - float(hit_d) * inv_ao_dist)

        occ_val = (occ_sum / float(len(_HEMI_DIRS_10))) * 1.35

        # Contact / Proximity occlusion for 90-degree seams & touching modular parts
        near_loc, _near_no, _near_idx, near_d = find_nearest(Vector((px, py, pz)), contact_dist)
        if near_loc is not None and near_d is not None:
            to_near = near_loc - Vector((px, py, pz))
            # Only occlude if the nearby surface is not strictly behind the back-face
            if near_d <= contact_dist * 0.25 or (to_near.x * nx + to_near.y * ny + to_near.z * nz) >= -0.25 * near_d:
                c_fall = max(0.0, 1.0 - float(near_d) * inv_contact)
                occ_val = max(occ_val, c_fall * c_fall * 0.88)

        v_occ[vi] = min(occ_val, 1.0)

    if float(np.max(v_occ)) <= 1e-3:
        _GLOBAL_AO_CACHE[ckey] = None
        return None

    # Smooth vertex occlusion 1 pass across mesh edges for soft gradients
    grid_w = min(int(w), 512)
    grid_h = min(int(h), 512)
    tmap = _build_uv_texel_map(ob_part, grid_w, grid_h)
    if tmap is None:
        return None

    tv = tmap["tri_verts"][tmap["tri_idx"]]
    pix_occ = (
        v_occ[tv[:, 0]] * tmap["w0"]
        + v_occ[tv[:, 1]] * tmap["w1"]
        + v_occ[tv[:, 2]] * tmap["w2"]
    )
    occ_grid = np.zeros((grid_h, grid_w), dtype=np.float32)
    cov_grid = np.zeros((grid_h, grid_w), dtype=bool)
    occ_grid.ravel()[tmap["pix_idx"]] = pix_occ
    cov_grid.ravel()[tmap["pix_idx"]] = True
    occ_grid = _dilate_2d_buffer_into_gutter(occ_grid, cov_grid, iterations=2)
    occ_grid = _fast_mask_box_blur_2d_f32(occ_grid, 1)

    if grid_w != w or grid_h != h:
        ys = np.clip((np.linspace(0.0, grid_h - 1.0, h)).astype(np.int32), 0, grid_h - 1)
        xs = np.clip((np.linspace(0.0, grid_w - 1.0, w)).astype(np.int32), 0, grid_w - 1)
        occ_full = occ_grid[ys[:, None], xs[None, :]].ravel()
    else:
        occ_full = occ_grid.ravel()

    occ_u8 = (np.clip(occ_full, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)
    _GLOBAL_AO_CACHE[ckey] = occ_u8
    return occ_u8


def _ensure_generator_raw_u8(node, layer, eff, w, h, force=False):
    """Evaluate C++ BKE_tex_paint_layer_generate_mask in raw mode (balance=0.5, contrast=0.0) across all mesh objects sharing the material,
    incorporating Baked Mesh Maps (Normal, AO, Curvature, World Normal, Position) and Global Scene AO across surrounding objects.
    """
    global _INTERNAL_REPROJECTING
    import numpy as np

    owner_node = _canonical_mask_owner_node(node)
    node_ptr = node.as_pointer()
    c_layer = _C_TexPaintLayer.from_address(layer.as_pointer())
    layer_uid = int(c_layer.uid)
    eff_uid = int(eff["uid"])
    rkey = (node_ptr, layer_uid, eff_uid)

    gen_type_str = eff.get("gen_type", "EDGE_WEAR")
    gen_type_int = _MASK_GEN_TYPE_TO_INT.get(gen_type_str, 4)
    radius = float(np.clip(eff.get("gen_radius", 0.35), 0.0, 1.0))
    noise = float(np.clip(eff.get("gen_noise", 0.0), 0.0, 1.0))
    noise_scale = max(float(eff.get("gen_noise_scale", 8.0)), 0.1)
    use_global_ao = bool(eff.get("gen_global_ao", True))

    mst = _get_mesh_maps_state(owner_node)
    use_baked = bool(mst.get("use_baked_maps", True))
    maps_dict = mst.get("maps", {})
    baked_sig = tuple((k, maps_dict.get(k, "")) for k in ("ao", "curvature", "normal_ws", "position")) if use_baked else ()

    sharing_obs, first_c_ob = _find_all_mesh_objects_for_node(node)
    scene_obs = [
        o for o in bpy.data.objects
        if o.type == 'MESH' and o.data is not None and not o.hide_get() and len(o.data.vertices) > 0 and len(o.data.polygons) > 0
    ]
    obs_sig = tuple(
        (o.name, o.data.as_pointer(), len(o.data.vertices), len(o.data.polygons), tuple(round(v, 3) for row in o.matrix_world for v in row))
        for o in (scene_obs if use_global_ao else sharing_obs)
    )

    sig = ("GEN", gen_type_int, round(radius, 4), round(noise, 4), round(noise_scale, 4), use_global_ao, baked_sig, w, h, obs_sig)
    cached = _EFFECT_RAW_CACHE.get(rkey)
    if not force and cached is not None and cached.get("sig") == sig:
        return cached["raw_u8"]

    # 1. Check if Baked Mesh Maps in the Hub can directly supply or enrich this generator
    if use_baked:
        ao_ima = bpy.data.images.get(maps_dict.get("ao", "")) if maps_dict.get("ao") else None
        curv_ima = bpy.data.images.get(maps_dict.get("curvature", "")) if maps_dict.get("curvature") else None
        nws_ima = bpy.data.images.get(maps_dict.get("normal_ws", "")) if maps_dict.get("normal_ws") else None
        pos_ima = bpy.data.images.get(maps_dict.get("position", "")) if maps_dict.get("position") else None

        baked_2d = None
        r_blur = max(1, int(round(radius * 12.0)))
        if gen_type_str == 'AO' and ao_ima is not None:
            ao_f = _read_baked_image_channel_f32(ao_ima, w, h, 0)
            if ao_f is not None:
                baked_2d = ao_f if radius <= 0.05 else (ao_f * (1.0 - radius * 0.65) + _fast_mask_box_blur_2d_f32(ao_f, r_blur) * (radius * 0.65))
        elif gen_type_str == 'CURVATURE' and curv_ima is not None:
            c_f = _read_baked_image_channel_f32(curv_ima, w, h, 0)
            if c_f is not None:
                baked_2d = c_f if radius <= 0.05 else (c_f * (1.0 - radius * 0.65) + _fast_mask_box_blur_2d_f32(c_f, r_blur) * (radius * 0.65))
        elif gen_type_str == 'CAVITY' and (curv_ima is not None or ao_ima is not None):
            c_f = _read_baked_image_channel_f32(curv_ima, w, h, 0) if curv_ima is not None else np.full((h, w), 0.5, dtype=np.float32)
            a_f = _read_baked_image_channel_f32(ao_ima, w, h, 0) if ao_ima is not None else np.ones((h, w), dtype=np.float32)
            if c_f is not None and a_f is not None:
                if radius > 0.05:
                    c_f = c_f * (1.0 - radius * 0.6) + _fast_mask_box_blur_2d_f32(c_f, r_blur) * (radius * 0.6)
                conc = np.maximum(0.5 - c_f, 0.0) * 2.0
                baked_2d = np.clip(conc * 1.6 + (1.0 - a_f) * 0.55, 0.0, 1.0)
        elif gen_type_str == 'EDGE_WEAR' and (curv_ima is not None or ao_ima is not None):
            c_f = _read_baked_image_channel_f32(curv_ima, w, h, 0) if curv_ima is not None else np.full((h, w), 0.5, dtype=np.float32)
            a_f = _read_baked_image_channel_f32(ao_ima, w, h, 0) if ao_ima is not None else np.ones((h, w), dtype=np.float32)
            if c_f is not None and a_f is not None:
                if radius > 0.05:
                    c_f = c_f * (1.0 - radius * 0.6) + _fast_mask_box_blur_2d_f32(c_f, r_blur) * (radius * 0.6)
                conv = np.maximum(c_f - 0.5, 0.0) * 2.0
                baked_2d = np.clip(conv * 1.85 * (0.35 + 0.65 * a_f), 0.0, 1.0)
        elif gen_type_str == 'NORMAL_UP' and nws_ima is not None:
            baked_2d = _read_baked_image_channel_f32(nws_ima, w, h, 2)
        elif gen_type_str == 'POSITION_Z' and pos_ima is not None:
            baked_2d = _read_baked_image_channel_f32(pos_ima, w, h, 2)

        if baked_2d is not None:
            if noise > 1e-4:
                ys = np.linspace(0.0, noise_scale * 2.0 * math.pi, h, dtype=np.float32)[:, None]
                xs = np.linspace(0.0, noise_scale * 2.0 * math.pi, w, dtype=np.float32)[None, :]
                g = np.sin(xs + np.cos(ys * 1.3)) * 0.5 + np.cos(ys * 1.7 - xs * 0.8) * 0.5
                baked_2d = np.clip(baked_2d + g * (noise * 0.42), 0.0, 1.0)
            raw_u8 = (np.clip(baked_2d.ravel(), 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)
            _EFFECT_RAW_CACHE[rkey] = {"sig": sig, "raw_u8": raw_u8}
            return raw_u8

    orig_gen_type = c_layer.mask_gen_type
    orig_gen_blend = c_layer.mask_gen_blend
    orig_gen_balance = c_layer.mask_gen_balance
    orig_gen_contrast = c_layer.mask_gen_contrast
    orig_gen_radius = c_layer.mask_gen_radius
    orig_gen_noise = c_layer.mask_gen_noise
    orig_gen_noise_scale = c_layer.mask_gen_noise_scale
    orig_gen_invert = c_layer.mask_gen_invert

    # Temporarily clear bNode.id (offset 216) so rna_TexPaintLayer_mask_gen_update skips the redundant
    # intermediate BKE_tex_paint_composite_rect (~8.4ms saved) before Python evaluates the full mask stack.
    c_node_id = ctypes.c_void_p.from_address(node_ptr + 216)
    orig_node_id = c_node_id.value

    c_data_slot, orig_mesh_ptr = _get_object_data_ptr_slot(first_c_ob) if first_c_ob is not None else (None, None)
    eval_obs = sharing_obs if sharing_obs else ([first_c_ob] if first_c_ob is not None else [])
    raw_u8 = np.zeros(w * h, dtype=np.uint8)

    # Precompute assembly world Z span for continuous multi-part POSITION_Z when use_global_ao is True
    asm_zmin, asm_zmax = 0.0, 1.0
    if use_global_ao and gen_type_str == 'POSITION_Z' and len(eval_obs) > 1:
        all_wz = [float((o.matrix_world @ Vector(co)).z) for o in eval_obs for co in o.bound_box]
        if all_wz:
            asm_zmin, asm_zmax = min(all_wz), max(all_wz)

    _INTERNAL_REPROJECTING = True
    try:
        c_node_id.value = 0
        c_layer.mask_gen_blend = 0  # REPLACE
        c_layer.mask_gen_balance = 0.5
        c_layer.mask_gen_contrast = 0.0
        c_layer.mask_gen_radius = radius
        c_layer.mask_gen_noise = noise
        c_layer.mask_gen_invert = 0

        for idx, ob_part in enumerate(eval_obs):
            if ob_part is None or ob_part.data is None:
                continue
            if c_data_slot is not None:
                c_data_slot.value = ob_part.data.as_pointer()
            c_layer.mask_gen_type = gen_type_int
            layer.mask_gen_noise_scale = noise_scale
            part_u8 = _read_mask_ibuf_r_u8(c_layer.mask_ibuf, w, h)

            # Apply Global Scene AO (interaction with surrounding scene objects & shared modular parts)
            if use_global_ao and gen_type_str in {'AO', 'CAVITY', 'EDGE_WEAR'} and len(scene_obs) > 1:
                ext_occ_u8 = _compute_part_external_scene_occlusion(ob_part, scene_obs, w, h, radius)
                if ext_occ_u8 is not None:
                    p16 = part_u8.astype(np.uint16)
                    o16 = ext_occ_u8.astype(np.uint16)
                    if gen_type_str == 'AO':
                        part_u8 = ((p16 * (255 - o16)) // 255).astype(np.uint8)
                    elif gen_type_str == 'CAVITY':
                        part_u8 = np.minimum(255, p16 + (o16 * 150) // 255).astype(np.uint8)
                    elif gen_type_str == 'EDGE_WEAR':
                        part_u8 = ((p16 * (255 - (o16 * 185) // 255)) // 255).astype(np.uint8)
            elif use_global_ao and gen_type_str == 'POSITION_Z' and len(eval_obs) > 1 and (asm_zmax - asm_zmin) > 1e-5:
                pwz = [float((ob_part.matrix_world @ Vector(co)).z) for co in ob_part.bound_box]
                pz_min, pz_max = min(pwz), max(pwz)
                part_f = part_u8.astype(np.float32) * (1.0 / 255.0)
                world_z = pz_min + part_f * (pz_max - pz_min)
                part_u8 = (np.clip((world_z - asm_zmin) / (asm_zmax - asm_zmin), 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)

            if idx == 0:
                raw_u8 = part_u8
            else:
                c_layer.mask_gen_type = 99
                layer.mask_gen_noise_scale = noise_scale
                cov_u8 = _read_mask_ibuf_r_u8(c_layer.mask_ibuf, w, h)
                mask_sel = cov_u8 > 0
                raw_u8[mask_sel] = part_u8[mask_sel]
    finally:
        if c_data_slot is not None and orig_mesh_ptr is not None:
            c_data_slot.value = orig_mesh_ptr
        c_node_id.value = orig_node_id
        _INTERNAL_REPROJECTING = False
        c_layer.mask_gen_type = orig_gen_type
        c_layer.mask_gen_blend = orig_gen_blend
        c_layer.mask_gen_balance = orig_gen_balance
        c_layer.mask_gen_contrast = orig_gen_contrast
        c_layer.mask_gen_radius = orig_gen_radius
        c_layer.mask_gen_noise = orig_gen_noise
        c_layer.mask_gen_noise_scale = orig_gen_noise_scale
        c_layer.mask_gen_invert = orig_gen_invert

    _EFFECT_RAW_CACHE[rkey] = {"sig": sig, "raw_u8": raw_u8}
    return raw_u8


def _ensure_grunge_raw_u8(node, layer, eff, w, h, context=None, force=False, from_gizmo=False, interactive=False):
    """Evaluate C++ BKE_tex_paint_layer_reproject into mask_ibuf and cache uint8[w*h] for this Mask Fill / Grunge effect."""
    global _INTERNAL_REPROJECTING
    import numpy as np

    src_ima = _resolve_grunge_image(eff, interactive=interactive)
    if src_ima is None:
        # If in CUSTOM image mode and no image is chosen yet, default to 255 (or 0) so the user sees a clean canvas
        return np.full(w * h, 255, dtype=np.uint8)

    node_ptr = node.as_pointer()
    c_layer = _C_TexPaintLayer.from_address(layer.as_pointer())
    layer_uid = int(c_layer.uid)
    eff_uid = int(eff["uid"])
    rkey = (node_ptr, layer_uid, eff_uid)

    proj_mode_str = eff.get("proj_mode", "TRIPLANAR")
    uv_wrap = eff.get("uv_wrap", "REPEAT")
    is_repeat = (uv_wrap != 'CLAMP')

    if proj_mode_str in {'REPEAT', 'UV', 'FIT'}:
        proj_mode_int = 1 if (proj_mode_str == 'FIT' or not is_repeat) else 2
    else:
        proj_mode_map = {
            'TRIPLANAR': 3,
            'DECAL': 4,
            'WARP': 5,
            'SPHERICAL': 6,
            'CYLINDRICAL': 7,
        }
        proj_mode_int = proj_mode_map.get(proj_mode_str, 3)

    lock_scale = bool(eff.get("lock_scale", True))
    base_scale = max(float(eff.get("scale", 2.0)), 0.02)
    sx = base_scale if lock_scale else max(float(eff.get("scale_x", base_scale)), 0.02)
    sy = base_scale if lock_scale else max(float(eff.get("scale_y", base_scale)), 0.02)

    rot_z = float(eff.get("rotation", 0.0))
    off_x = float(eff.get("offset_x", 0.0))
    off_y = float(eff.get("offset_y", 0.0))
    box_blend = float(np.clip(eff.get("box_blend", 0.5), 0.0, 1.0))
    channel_mode = eff.get("channel", "AUTO")

    sharing_obs, first_c_ob = _find_all_mesh_objects_for_node(node, context)
    ob = sharing_obs[-1] if sharing_obs else first_c_ob

    def _compute_ob_bbox(target_ob):
        bc = Vector((0.0, 0.0, 0.0))
        me = 2.0
        if target_ob is not None and getattr(target_ob, "bound_box", None):
            pts = [Vector(co) for co in target_ob.bound_box]
            bmin = Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
            bmax = Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
            bc = (bmin + bmax) * 0.5
            bsize = bmax - bmin
            me = max(bsize.x, bsize.y, bsize.z, 1e-3)
        return bc, me

    bbox_center, max_extent = _compute_ob_bbox(ob)

    if from_gizmo and getattr(layer, "show_projection_widget", False) and proj_mode_int >= 3:
        eff["gizmo_loc"] = [float(c_layer.proj_location[0]), float(c_layer.proj_location[1]), float(c_layer.proj_location[2])]
        eff["gizmo_rot"] = [float(c_layer.proj_rotation[0]), float(c_layer.proj_rotation[1]), float(c_layer.proj_rotation[2])]
        gx = max(abs(float(c_layer.proj_scale[0])), 1e-3)
        gy = max(abs(float(c_layer.proj_scale[1])), 1e-3)
        gz = max(abs(float(c_layer.proj_scale[2])), 1e-3)
        eff["gizmo_scale"] = [gx, gy, gz]
        eff["gizmo_override"] = True
        new_sx = round(max(max_extent * 0.5 / gx, 0.02), 3)
        new_sy = round(max(max_extent * 0.5 / gy, 0.02), 3)
        eff["scale"] = new_sx
        eff["scale_x"] = new_sx
        eff["scale_y"] = new_sy
        eff["offset_x"] = 0.0
        eff["offset_y"] = 0.0
        eff["rotation"] = 0.0
        sx, sy = new_sx, new_sy
        off_x, off_y, rot_z = 0.0, 0.0, 0.0

    def _compute_proj_xform(bc, me):
        if proj_mode_int in {1, 2}:
            l_out = (off_x, off_y, 0.0)
            r_out = (0.0, 0.0, rot_z)
            s_out = (sx, sy, 1.0) if proj_mode_int == 2 else (1.0 / sx, 1.0 / sy, 1.0)
            return l_out, r_out, s_out
        if eff.get("gizmo_override") and eff.get("gizmo_loc") and eff.get("gizmo_rot"):
            base_loc = Vector(eff["gizmo_loc"])
            base_rot_mat = Euler(eff["gizmo_rot"], 'XYZ').to_matrix()
        elif proj_mode_str in {'DECAL', 'WARP'}:
            base_loc = Vector((bc.x, bc.y + me * 0.5, bc.z))
            base_rot_mat = Euler((math.pi * 0.5, 0.0, 0.0), 'XYZ').to_matrix()
        else:
            base_loc = bc.copy()
            base_rot_mat = Matrix.Identity(3)

        ax_x = base_rot_mat.col[0].normalized()
        ax_y = base_rot_mat.col[1].normalized()
        final_loc = base_loc + ax_x * (off_x * me * 0.5) + ax_y * (off_y * me * 0.5)
        final_rot_mat = base_rot_mat @ Matrix.Rotation(rot_z, 3, 'Z')
        final_rot = final_rot_mat.to_euler('XYZ')

        half_x = max(me * 0.5 / sx, 1e-3)
        half_y = max(me * 0.5 / sy, 1e-3)
        half_z = (
            max(me * 0.5 / ((sx + sy) * 0.5), 1e-3)
            if proj_mode_str == 'TRIPLANAR'
            else max(me * 0.6, 1e-3)
        )
        z_signed = (half_z if is_repeat else -half_z) if proj_mode_str == 'TRIPLANAR' else (-half_z if is_repeat else half_z)
        return (
            (float(final_loc.x), float(final_loc.y), float(final_loc.z)),
            (float(final_rot.x), float(final_rot.y), float(final_rot.z)),
            (half_x, half_y, z_signed),
        )

    loc, rot, scl = _compute_proj_xform(bbox_center, max_extent)
    obs_sig = (
        tuple((o.name, o.data.as_pointer(), len(o.data.vertices), len(o.data.polygons)) for o in sharing_obs)
        if proj_mode_int >= 3
        else ()
    )

    sig = (
        "GRUNGE",
        src_ima.name,
        proj_mode_int,
        channel_mode,
        tuple(round(v, 4) for v in loc),
        tuple(round(v, 4) for v in rot),
        tuple(round(v, 4) for v in scl),
        round(box_blend, 4),
        w,
        h,
        obs_sig,
    )
    cached = _EFFECT_RAW_CACHE.get(rkey)
    if not force and cached is not None and cached.get("sig") == sig:
        return cached["raw_u8"]

    orig_type = c_layer.type
    orig_ibuf = c_layer.ibuf
    orig_mask_ibuf = c_layer.mask_ibuf
    orig_flag = c_layer.flag
    orig_proj_mode = c_layer.proj_mode
    orig_proj_name = bytes(c_layer.proj_image_name)

    # Temporarily clear bNode.id (offset 216) so rna_TexPaintLayer_projection_update skips the redundant
    # intermediate BKE_tex_paint_composite_rect (~8.4ms saved) before Python evaluates the full mask stack.
    c_node_id = ctypes.c_void_p.from_address(node_ptr + 216)
    orig_node_id = c_node_id.value

    def _extract_projected_u8_and_cov():
        c_imb = _C_ImBuf.from_address(orig_mask_ibuf)
        if c_imb.byte_data:
            buf = (ctypes.c_uint8 * (w * h * 4)).from_address(c_imb.byte_data)
            rgba = np.frombuffer(buf, dtype=np.uint8).reshape(-1, 4)
            cov_m = rgba[:, 3] > 0
            if (
                channel_mode == 'AUTO' and
                eff.get("category", "BITMAP_GRUNGES") != 'CUSTOM' and
                proj_mode_int in {2, 3} and
                is_repeat
            ):
                p_u8 = rgba[:, 0].copy()
            elif channel_mode == 'ALPHA':
                p_u8 = rgba[:, 3].copy()
            elif channel_mode in {'RED', 'GREEN', 'BLUE'}:
                ch_i = {'RED': 0, 'GREEN': 1, 'BLUE': 2}[channel_mode]
                a_u16 = rgba[:, 3].astype(np.uint16)
                p_u8 = ((rgba[:, ch_i].astype(np.uint16) * a_u16 + 127) >> 8).astype(np.uint8)
            elif channel_mode == 'LUMINANCE':
                lum_u16 = (
                    rgba[:, 0].astype(np.uint16) * 77 +
                    rgba[:, 1].astype(np.uint16) * 150 +
                    rgba[:, 2].astype(np.uint16) * 29
                ) >> 8
                p_u8 = lum_u16.astype(np.uint8)
            else:
                a_u16 = rgba[:, 3].astype(np.uint16)
                lum_u16 = (
                    rgba[:, 0].astype(np.uint16) * 77 +
                    rgba[:, 1].astype(np.uint16) * 150 +
                    rgba[:, 2].astype(np.uint16) * 29
                ) >> 8
                if int(lum_u16.max()) == 0 and int(a_u16.max()) > 0:
                    p_u8 = rgba[:, 3].copy()
                else:
                    p_u8 = ((lum_u16 * a_u16 + 127) >> 8).astype(np.uint8)
            return p_u8, cov_m.copy()
        if c_imb.float_data:
            buf = (ctypes.c_float * (w * h * 4)).from_address(c_imb.float_data)
            arr = np.frombuffer(buf, dtype=np.float32).reshape(-1, 4)
            cov = np.clip(arr[:, 3], 0.0, 1.0)
            cov_m = cov > 1e-5
            if channel_mode == 'ALPHA':
                raw_f = cov
            elif channel_mode in {'RED', 'GREEN', 'BLUE'}:
                ch_i = {'RED': 0, 'GREEN': 1, 'BLUE': 2}[channel_mode]
                raw_f = np.clip(arr[:, ch_i], 0.0, 1.0)
            elif channel_mode == 'LUMINANCE':
                lum = 0.299 * arr[:, 0] + 0.587 * arr[:, 1] + 0.114 * arr[:, 2]
                raw_f = np.where(cov > 1e-5, np.clip(lum / np.maximum(cov, 1e-5), 0.0, 1.0), 0.0)
            else:
                lum = 0.299 * arr[:, 0] + 0.587 * arr[:, 1] + 0.114 * arr[:, 2]
                raw_f = np.clip(lum, 0.0, 1.0)
            return (raw_f * 255.0 + 0.5).astype(np.uint8), cov_m.copy()
        return np.zeros(w * h, dtype=np.uint8), np.zeros(w * h, dtype=bool)

    c_data_slot, orig_mesh_ptr = _get_object_data_ptr_slot(first_c_ob) if (proj_mode_int >= 3 and first_c_ob is not None) else (None, None)
    eval_obs = sharing_obs if (proj_mode_int >= 3 and sharing_obs) else ([ob] if ob is not None else [])
    raw_u8 = np.zeros(w * h, dtype=np.uint8)

    _INTERNAL_REPROJECTING = True
    try:
        c_node_id.value = 0
        c_layer.type = 0  # TEXPAINT_LAYER_PAINT
        c_layer.ibuf = orig_mask_ibuf
        c_layer.proj_mode = proj_mode_int
        c_layer.proj_image_name = src_ima.name.encode('utf-8')[:63]

        if proj_mode_int < 3 or len(eval_obs) <= 1:
            if proj_mode_int >= 3 and eval_obs and c_data_slot is not None and eval_obs[0].data is not None:
                c_data_slot.value = eval_obs[0].data.as_pointer()
            loc_0, rot_0, scl_0 = (
                _transform_widget_between_objects(loc, rot, scl, ob, eval_obs[0])
                if (proj_mode_int >= 3 and eval_obs and eval_obs[0] != ob)
                else (loc, rot, scl)
            )
            for i in range(3):
                c_layer.proj_location[i] = loc_0[i]
                c_layer.proj_rotation[i] = rot_0[i]
                c_layer.proj_scale[i] = scl_0[i]
            orig_uid = c_layer.uid
            c_layer.uid = orig_uid + 10000
            layer.proj_blend_falloff = box_blend
            c_layer.uid = orig_uid
            raw_u8, _ = _extract_projected_u8_and_cov()
        else:
            use_world_widget = bool(eff.get("gizmo_override") or proj_mode_str in {'DECAL', 'WARP'})
            for idx, ob_part in enumerate(eval_obs):
                if ob_part.data is None:
                    continue
                if c_data_slot is not None:
                    c_data_slot.value = ob_part.data.as_pointer()
                if use_world_widget:
                    loc_p, rot_p, scl_p = _transform_widget_between_objects(loc, rot, scl, ob, ob_part)
                else:
                    bc_p, me_p = _compute_ob_bbox(ob_part)
                    loc_p, rot_p, scl_p = _compute_proj_xform(bc_p, me_p)
                for i in range(3):
                    c_layer.proj_location[i] = loc_p[i]
                    c_layer.proj_rotation[i] = rot_p[i]
                    c_layer.proj_scale[i] = scl_p[i]
                # Reset last_proj_layer_uid via temporary uid flip so C++ clears between objects
                orig_uid = c_layer.uid
                c_layer.uid = orig_uid + 10000 + idx
                layer.proj_blend_falloff = box_blend
                c_layer.uid = orig_uid
                part_u8, cov_m = _extract_projected_u8_and_cov()
                if idx == 0:
                    raw_u8 = part_u8
                elif not is_repeat:
                    raw_u8[cov_m] = np.maximum(raw_u8[cov_m], part_u8[cov_m])
                else:
                    raw_u8[cov_m] = part_u8[cov_m]
    finally:
        if c_data_slot is not None and orig_mesh_ptr is not None:
            c_data_slot.value = orig_mesh_ptr
        c_node_id.value = orig_node_id
        _INTERNAL_REPROJECTING = False
        c_layer.type = orig_type
        c_layer.ibuf = orig_ibuf
        c_layer.mask_ibuf = orig_mask_ibuf
        c_layer.flag = orig_flag | (1 << 4)
        if orig_type == 0:
            c_layer.proj_mode = orig_proj_mode
            c_layer.proj_image_name = orig_proj_name
        if proj_mode_int >= 3 and not from_gizmo and getattr(layer, "show_projection_widget", False):
            c_layer.proj_location[0], c_layer.proj_location[1], c_layer.proj_location[2] = loc[0], loc[1], loc[2]
            c_layer.proj_rotation[0], c_layer.proj_rotation[1], c_layer.proj_rotation[2] = rot[0], rot[1], rot[2]
            c_layer.proj_scale[0], c_layer.proj_scale[1], c_layer.proj_scale[2] = abs(scl[0]), abs(scl[1]), abs(scl[2])

    _EFFECT_RAW_CACHE[rkey] = {"sig": sig, "raw_u8": raw_u8}
    return raw_u8


def _compute_blend_matrix_or_curve(s_curve_256, blend_mode, opacity, const_base_val=None):
    """Compute either a 256-entry 1D float32 array (if const_base_val is given) or a 256x256 2D float32 matrix."""
    import numpy as np
    opacity = float(np.clip(opacity, 0.0, 1.0))
    if const_base_val is not None:
        b = np.float32(np.clip(const_base_val / 255.0, 0.0, 1.0))
        s = np.clip(s_curve_256, 0.0, 1.0)
    else:
        b = np.linspace(0.0, 1.0, 256, dtype=np.float32)[:, None]
        s = np.clip(s_curve_256, 0.0, 1.0)[None, :]

    if blend_mode == 'MULTIPLY':
        m = b * s
    elif blend_mode == 'SUBTRACT':
        m = np.clip(b - s, 0.0, 1.0)
    elif blend_mode == 'ADD':
        m = np.clip(b + s, 0.0, 1.0)
    elif blend_mode == 'SCREEN':
        m = b + s - b * s
    elif blend_mode == 'OVERLAY':
        m = np.where(b <= 0.5, 2.0 * b * s, 1.0 - 2.0 * (1.0 - b) * (1.0 - s))
    elif blend_mode == 'MAX':
        m = np.maximum(b, s)
    elif blend_mode == 'MIN':
        m = np.minimum(b, s)
    else:
        m = s

    if opacity < 0.9999:
        m = b + (m - b) * opacity
    return m


def _pack_u8_lut_to_rgba_u32(m_float):
    import numpy as np
    v = (np.clip(m_float.ravel(), 0.0, 1.0) * 255.0 + 0.5).astype(np.uint32)
    return (255 << 24) | (v << 16) | (v << 8) | v


def _blend_1d_curve_onto_acc_u8(acc_u8, raw_u8, s_curve_256, blend_mode, opacity, is_first_effect=False):
    """Blend a 256-entry float32 [0..1] curve applied to raw_u8 onto acc_u8 in-place using an 8-thread LUT."""
    import numpy as np
    opacity = float(np.clip(opacity, 0.0, 1.0))
    if opacity <= 1e-5:
        return acc_u8

    if blend_mode == 'REPLACE' and opacity >= 0.9999:
        lut_256 = (np.clip(s_curve_256, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)
        _parallel_take_u8(lut_256, raw_u8, acc_u8)
        return acc_u8

    if acc_u8 is raw_u8 and blend_mode == 'REPLACE':
        b_1d = np.linspace(0.0, 1.0, 256, dtype=np.float32)
        s_1d = np.clip(s_curve_256, 0.0, 1.0)
        m_1d = b_1d + (s_1d - b_1d) * opacity
        lut_256 = (np.clip(m_1d, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)
        _parallel_take_u8(lut_256, acc_u8, acc_u8)
        return acc_u8

    m = _compute_blend_matrix_or_curve(s_curve_256, blend_mode, opacity, const_base_val=None)
    lut_64k_u8 = (np.clip(m.ravel(), 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)
    idx_u16 = np.empty(len(acc_u8), dtype=np.uint16)
    idx_pairs = idx_u16.view(np.uint8).reshape(-1, 2)
    _parallel_take_u8(lut_64k_u8, idx_u16, acc_u8, raw_u8=raw_u8, below_u8=acc_u8, idx_pairs=idx_pairs)
    return acc_u8


def _blend_paint_effect_onto_acc_u8(acc_u8, pbuf, blend_mode, opacity, invert):
    """Composite a hand-painted (val_u8, alpha_u8) mask effect onto acc_u8 in-place."""
    import numpy as np
    opacity = float(np.clip(opacity, 0.0, 1.0))
    if opacity <= 1e-5:
        return acc_u8

    val_u8 = pbuf["val_u8"]
    alpha_u8 = pbuf["alpha_u8"]
    v = (255 - val_u8).astype(np.uint16) if invert else val_u8.astype(np.uint16)
    op_u16 = int(round(opacity * 256.0))
    eff_a = (alpha_u8.astype(np.uint16) * op_u16) >> 8

    acc_u16 = acc_u8.astype(np.uint16)
    if blend_mode == 'ADD':
        delta = (v * eff_a + 127) // 255
        res = np.minimum(acc_u16 + delta, 255)
    elif blend_mode == 'SUBTRACT':
        delta = (v * eff_a + 127) // 255
        res = np.maximum(acc_u16.astype(np.int16) - delta.astype(np.int16), 0)
    elif blend_mode == 'MULTIPLY':
        mul_v = (acc_u16 * v + 127) // 255
        res = (acc_u16 * (255 - eff_a) + mul_v * eff_a + 127) // 255
    elif blend_mode == 'MAX':
        target = np.maximum(acc_u16, v)
        res = (acc_u16 * (255 - eff_a) + target * eff_a + 127) // 255
    elif blend_mode == 'MIN':
        target = np.minimum(acc_u16, v)
        res = (acc_u16 * (255 - eff_a) + target * eff_a + 127) // 255
    else:
        # REPLACE / Normal (White = Reveal toward 255, Black = Hide toward 0, alpha=0 passes acc_u8 through)
        res = (acc_u16 * (255 - eff_a) + v * eff_a + 127) // 255

    acc_u8[:] = res.astype(np.uint8)
    return acc_u8


def _effect_curve_256(eff):
    """Build the 256-entry float32 [0..1] tone curve for a GENERATOR, GRUNGE, or LEVELS effect."""
    import numpy as np
    x = np.linspace(0.0, 1.0, 256, dtype=np.float32)
    etype = eff.get("type", "GRUNGE")
    invert = bool(eff.get("invert", False))

    if etype == 'GENERATOR':
        balance = float(np.clip(eff.get("gen_balance", 0.5), 0.0, 1.0))
        contrast = float(np.clip(eff.get("gen_contrast", 0.45), 0.0, 1.0))
        pivot = 1.0 - balance
        slope = 1.0 / max(1.0 - contrast * 0.96, 0.025)
        s = np.clip((x - pivot) * slope + 0.5, 0.0, 1.0)
        if contrast > 0.001:
            s_smooth = s * s * (3.0 - 2.0 * s)
            s = s + (s_smooth - s) * contrast
    elif etype == 'LEVELS':
        balance = float(np.clip(eff.get("lvl_balance", 0.5), 0.0, 1.0))
        contrast = float(np.clip(eff.get("lvl_contrast", 0.0), 0.0, 1.0))
        shift = (balance - 0.5) * 2.0
        slope = 1.0 / max(1.0 - contrast * 0.98, 0.02)
        s = np.clip((x + shift - 0.5) * slope + 0.5, 0.0, 1.0)
        if contrast > 0.01:
            s_smooth = s * s * (3.0 - 2.0 * s)
            s = s + (s_smooth - s) * contrast
    else:
        # GRUNGE
        balance = float(np.clip(eff.get("balance", 0.5), 0.0, 1.0))
        contrast = float(np.clip(eff.get("contrast", 0.30), 0.0, 1.0))
        shift = (balance - 0.5) * 2.0
        slope = 1.0 / max(1.0 - contrast * 0.98, 0.02)
        s = np.clip((x + shift - 0.5) * slope + 0.5, 0.0, 1.0)
        if contrast > 0.01:
            s_smooth = s * s * (3.0 - 2.0 * s)
            s = s + (s_smooth - s) * contrast

    if invert:
        s = 1.0 - s
    return s


def _effect_state_sig(node_ptr, layer_uid, eff):
    """Build a cache signature tuple for one mask effect's full state."""
    etype = eff.get("type", "GRUNGE")
    common = (
        int(eff["uid"]),
        etype,
        bool(eff.get("enabled", True)),
        eff.get("blend_mode", "REPLACE"),
        round(float(eff.get("opacity", 1.0)), 4),
        bool(eff.get("invert", False)),
    )
    if etype == 'GENERATOR':
        return common + (
            eff.get("gen_type", "EDGE_WEAR"),
            round(float(eff.get("gen_balance", 0.5)), 4),
            round(float(eff.get("gen_contrast", 0.45)), 4),
            round(float(eff.get("gen_radius", 0.35)), 4),
            round(float(eff.get("gen_noise", 0.0)), 4),
            round(float(eff.get("gen_noise_scale", 8.0)), 4),
            bool(eff.get("gen_global_ao", True)),
        )
    elif etype == 'PAINT':
        pbuf = _PAINT_EFFECT_BUFFERS.get((node_ptr, layer_uid, int(eff["uid"])))
        rev = pbuf.get("rev", 0) if pbuf else 0
        return common + (rev,)
    elif etype == 'LEVELS':
        return common + (
            round(float(eff.get("lvl_balance", 0.5)), 4),
            round(float(eff.get("lvl_contrast", 0.0)), 4),
        )
    elif etype == 'FILTER':
        return common + (
            eff.get("filter_type", "BLUR"),
            round(float(eff.get("flt_radius", 6.0)), 3),
            round(float(eff.get("flt_strength", 1.0)), 4),
            round(float(eff.get("flt_scale", 8.0)), 3),
        )
    else:
        return common + (
            eff.get("category", "BITMAP_GRUNGES"),
            eff.get("texture", ""),
            eff.get("custom_image", ""),
            eff.get("channel", "AUTO"),
            eff.get("proj_mode", "TRIPLANAR"),
            eff.get("uv_wrap", "REPEAT"),
            bool(eff.get("lock_scale", True)),
            round(float(eff.get("scale", 2.0)), 4),
            round(float(eff.get("scale_x", eff.get("scale", 2.0))), 4),
            round(float(eff.get("scale_y", eff.get("scale", 2.0))), 4),
            round(float(eff.get("balance", 0.5)), 4),
            round(float(eff.get("contrast", 0.3)), 4),
            round(float(eff.get("rotation", 0.0)), 4),
            round(float(eff.get("offset_x", 0.0)), 4),
            round(float(eff.get("offset_y", 0.0)), 4),
            round(float(eff.get("box_blend", 0.5)), 4),
        )


def _fast_mask_box_blur_2d_f32(img_2d, radius_px):
    """Separable 2-pass cumulative box blur on float32[H, W] (~2ms at 2K)."""
    import numpy as np
    r = max(int(round(radius_px)), 1)
    arr = img_2d
    for _ in range(2):
        pad_y = np.pad(arr, ((r, r), (0, 0)), mode='edge')
        cs_y = np.cumsum(pad_y, axis=0, dtype=np.float32)
        arr = (cs_y[2 * r:] - cs_y[:-2 * r]) * (1.0 / float(2 * r))
        pad_x = np.pad(arr, ((0, 0), (r, r)), mode='edge')
        cs_x = np.cumsum(pad_x, axis=1, dtype=np.float32)
        arr = (cs_x[:, 2 * r:] - cs_x[:, :-2 * r]) * (1.0 / float(2 * r))
    return arr


def _evaluate_filter_effect_onto_acc_u8(acc_u8, w, h, eff, blend_mode, opacity, invert):
    """Evaluate a Substance-Painter-style Mask Filter (Blur, Blur Slope, Sharpen, Grow/Shrink, Bevel, Threshold, Posterize, Highpass)."""
    import numpy as np
    if opacity <= 1e-5:
        return acc_u8

    ftype = eff.get("filter_type", "BLUR")
    rad = float(eff.get("flt_radius", 6.0))
    strength = float(eff.get("flt_strength", 1.0))
    scale = max(float(eff.get("flt_scale", 8.0)), 0.5)

    src_2d = acc_u8.reshape(h, w).astype(np.float32) * (1.0 / 255.0)
    r_px = max(1, min(int(round(rad)), 64))

    if ftype == 'BLUR':
        blurred = _fast_mask_box_blur_2d_f32(src_2d, r_px)
        out_2d = src_2d + (blurred - src_2d) * min(max(strength, 0.0), 1.0)

    elif ftype == 'BLUR_SLOPE':
        # Substance Painter Blur Slope / Directional Noise Warp:
        # Multi-octave smooth procedural slope field warps and feathers mask edges organically!
        ys = np.linspace(0.0, scale * 2.0 * math.pi, h, dtype=np.float32)[:, None]
        xs = np.linspace(0.0, scale * 2.0 * math.pi, w, dtype=np.float32)[None, :]
        dx = (
            np.sin(xs * 1.0 + np.cos(ys * 1.3 + 0.7)) * 0.65
            + np.cos(xs * 2.3 - ys * 1.9 + 1.4) * 0.35
        )
        dy = (
            np.cos(ys * 1.0 + np.sin(xs * 1.3 - 0.5)) * 0.65
            + np.sin(ys * 2.1 + xs * 1.7 - 1.1) * 0.35
        )
        amp = rad * strength * (max(w, h) / 1024.0)
        iy_base = np.arange(h, dtype=np.float32)[:, None]
        ix_base = np.arange(w, dtype=np.float32)[None, :]
        # 3-tap directional slope accumulation (matches Substance Blur Slope directional smear)
        acc_slope = np.zeros((h, w), dtype=np.float32)
        for t_step in (0.35, 0.70, 1.0):
            sy = np.clip((iy_base + dy * (amp * t_step)).astype(np.int32), 0, h - 1)
            sx = np.clip((ix_base + dx * (amp * t_step)).astype(np.int32), 0, w - 1)
            acc_slope += src_2d[sy, sx]
        out_2d = acc_slope * (1.0 / 3.0)

    elif ftype == 'SHARPEN':
        blurred = _fast_mask_box_blur_2d_f32(src_2d, max(r_px // 2, 1))
        out_2d = np.clip(src_2d + (src_2d - blurred) * (strength * 2.5), 0.0, 1.0)

    elif ftype == 'DILATE_ERODE':
        # strength > 0.5 = Grow (Dilate), strength < 0.5 = Shrink (Erode)
        signed = (strength - 0.5) * 2.0
        steps = max(1, int(round(abs(signed) * r_px)))
        cur = src_2d.copy()
        if signed >= 0.0:
            for s in range(1, steps + 1):
                cur = np.maximum(cur, np.roll(src_2d, s, axis=0))
                cur = np.maximum(cur, np.roll(src_2d, -s, axis=0))
                cur = np.maximum(cur, np.roll(cur, s, axis=1))
                cur = np.maximum(cur, np.roll(cur, -s, axis=1))
        else:
            for s in range(1, steps + 1):
                cur = np.minimum(cur, np.roll(src_2d, s, axis=0))
                cur = np.minimum(cur, np.roll(src_2d, -s, axis=0))
                cur = np.minimum(cur, np.roll(cur, s, axis=1))
                cur = np.minimum(cur, np.roll(cur, -s, axis=1))
        out_2d = _fast_mask_box_blur_2d_f32(cur, 1) if steps > 1 else cur

    elif ftype == 'BEVEL':
        cur = src_2d.copy()
        for s in range(1, max(r_px // 2, 1) + 1):
            cur = np.maximum(cur, np.roll(src_2d, s, axis=0))
            cur = np.maximum(cur, np.roll(src_2d, -s, axis=0))
            cur = np.maximum(cur, np.roll(cur, s, axis=1))
            cur = np.maximum(cur, np.roll(cur, -s, axis=1))
        out_2d = _fast_mask_box_blur_2d_f32(cur, r_px)
        if abs(strength - 1.0) > 1e-3:
            out_2d = np.clip((out_2d - 0.5) * (0.5 + strength) + 0.5, 0.0, 1.0)

    elif ftype == 'THRESHOLD':
        thr = min(max(1.0 - strength * 0.5, 0.02), 0.98)
        soft = max(rad / 128.0, 0.005)
        out_2d = np.clip((src_2d - (thr - soft)) / (2.0 * soft), 0.0, 1.0)

    elif ftype == 'POSTERIZE':
        bands = float(max(2, min(int(round(rad)), 16)))
        out_2d = np.round(src_2d * bands) / bands

    else:  # HIGHPASS
        blurred = _fast_mask_box_blur_2d_f32(src_2d, r_px)
        out_2d = np.clip(0.5 + (src_2d - blurred) * (strength * 2.0), 0.0, 1.0)

    if invert:
        out_2d = 1.0 - out_2d

    flt_u8 = (np.clip(out_2d, 0.0, 1.0).ravel() * 255.0 + 0.5).astype(np.uint8)
    id_curve = np.linspace(0.0, 1.0, 256, dtype=np.float32)
    _blend_1d_curve_onto_acc_u8(acc_u8, flt_u8, id_curve, blend_mode, opacity, is_first_effect=False)
    return acc_u8


def _evaluate_single_effect_onto_acc(node, layer, eff, acc_u8, w, h, context=None, force_reproject=False, from_gizmo=False, is_first=False, interactive=False):
    import numpy as np
    if not eff.get("enabled", True):
        return acc_u8

    etype = eff.get("type", "GRUNGE")
    blend_mode = eff.get("blend_mode", "REPLACE")
    opacity = float(np.clip(eff.get("opacity", 1.0), 0.0, 1.0))
    invert = bool(eff.get("invert", False))

    if etype == 'GENERATOR':
        raw_u8 = _ensure_generator_raw_u8(node, layer, eff, w, h, force=force_reproject)
        s_curve = _effect_curve_256(eff)
        _blend_1d_curve_onto_acc_u8(acc_u8, raw_u8, s_curve, blend_mode, opacity, is_first_effect=is_first)
    elif etype == 'GRUNGE':
        raw_u8 = _ensure_grunge_raw_u8(node, layer, eff, w, h, context=context, force=force_reproject, from_gizmo=from_gizmo, interactive=interactive)
        s_curve = _effect_curve_256(eff)
        _blend_1d_curve_onto_acc_u8(acc_u8, raw_u8, s_curve, blend_mode, opacity, is_first_effect=is_first)
    elif etype == 'LEVELS':
        s_curve = _effect_curve_256(eff)
        _blend_1d_curve_onto_acc_u8(acc_u8, acc_u8, s_curve, 'REPLACE', opacity, is_first_effect=False)
    elif etype == 'FILTER':
        _evaluate_filter_effect_onto_acc_u8(acc_u8, w, h, eff, blend_mode, opacity, invert)
    elif etype == 'PAINT':
        node_ptr = _canonical_mask_owner_node(node).as_pointer()
        layer_uid = _layer_uid(layer)
        pbuf = _ensure_paint_effect_buffer(node_ptr, layer_uid, eff, w, h)
        _blend_paint_effect_onto_acc_u8(acc_u8, pbuf, blend_mode, opacity, invert)

    return acc_u8


def _evaluate_layer_grunge_mask(node, layer=None, context=None, force_reproject=False, from_gizmo=False, skip_absorb=False, interactive=False):
    """Evaluate the entire Substance-style Multi-Layer Mask Stack into layer.mask_ibuf in real time."""
    global _INTERNAL_REPROJECTING
    if _INTERNAL_REPROJECTING or node is None:
        return False
    if layer is None:
        layer = getattr(node, "active_layer", None)
    if layer is None:
        return False

    if not skip_absorb:
        _absorb_pending_brush_strokes(node, layer)

    import time
    import numpy as np

    st = _get_layer_grunge_state(node, layer)
    effects = st.get("effects", [])
    if not effects:
        return False

    # 1. Ensure the layer mask buffer is allocated (or unpacked from .blend) in C++
    if not layer.use_mask:
        layer.use_mask = True
    c_layer = _C_TexPaintLayer.from_address(layer.as_pointer())
    if not c_layer.mask_ibuf:
        layer.opacity = layer.opacity
        c_layer = _C_TexPaintLayer.from_address(layer.as_pointer())
    if not c_layer.mask_ibuf:
        layer.use_mask = False
        layer.use_mask = True
        c_layer = _C_TexPaintLayer.from_address(layer.as_pointer())
    if not c_layer.mask_ibuf:
        return False

    c_imb = _C_ImBuf.from_address(c_layer.mask_ibuf)
    w, h = int(c_imb.x), int(c_imb.y)
    if w <= 0 or h <= 0:
        return False

    node_ptr = node.as_pointer()
    layer_uid = int(c_layer.uid)
    key = (node_ptr, layer_uid)

    # Determine initial base mask value (0 or 255) before effect 0
    enabled_indices = [i for i, e in enumerate(effects) if e.get("enabled", True)]
    first_enabled = effects[enabled_indices[0]] if enabled_indices else None
    if first_enabled is None:
        init_val = int(st.get("base_value", 255))
    elif first_enabled["type"] != 'PAINT' and first_enabled.get("blend_mode", "REPLACE") in {'ADD', 'SCREEN', 'MAX'}:
        init_val = 0
    elif first_enabled["type"] != 'PAINT' and first_enabled.get("blend_mode", "REPLACE") in {'MULTIPLY', 'SUBTRACT', 'MIN'}:
        init_val = 255
    else:
        init_val = int(st.get("base_value", 255))

    # Find active effect index for Below-Active Stack Caching
    act_uid = st.get("active_effect_uid", 0)
    act_idx = 0
    for idx, eff in enumerate(effects):
        if eff["uid"] == act_uid:
            act_idx = idx
            break

    # Pre-warm any missing raw buffers BEFORE reading/writing mask_ibuf so C++ reprojection doesn't clobber acc_u8
    for idx in enabled_indices:
        eff = effects[idx]
        force_this = force_reproject and (idx == act_idx)
        gizmo_this = from_gizmo and (idx == act_idx)
        inter_this = interactive and (idx == act_idx)
        if eff["type"] == 'GENERATOR':
            _ensure_generator_raw_u8(node, layer, eff, w, h, force=force_this)
        elif eff["type"] == 'GRUNGE':
            _ensure_grunge_raw_u8(
                node, layer, eff, w, h,
                context=context, force=force_this, from_gizmo=gizmo_this, interactive=inter_this,
            )
            if inter_this and force_this:
                _PENDING_HIRES_REFINE[key] = (node, layer, time.perf_counter())

    c_imb = _C_ImBuf.from_address(c_layer.mask_ibuf)
    has_byte_dst = bool(c_imb.byte_data)
    if has_byte_dst:
        buf_u32 = (ctypes.c_uint32 * (w * h)).from_address(c_imb.byte_data)
        dst_u32 = np.frombuffer(buf_u32, dtype=np.uint32)

    last_enabled_idx = enabled_indices[-1] if enabled_indices else -1
    top_eff = effects[last_enabled_idx] if last_enabled_idx >= 0 else None
    top_opacity = float(np.clip(top_eff.get("opacity", 1.0), 0.0, 1.0)) if top_eff else 0.0

    # ULTRA-FAST FUSED PATH: When writing to an 8-bit mask_ibuf and the topmost enabled effect is GRUNGE,
    # GENERATOR, or LEVELS with opacity > 0, fuse the final effect's LUT + RGBA uint32 packing into ONE
    # multi-threaded pass directly into c_imb.byte_data (eliminates acc_u8 copy + second np.take pass).
    top_rkey = (node_ptr, layer_uid, int(top_eff["uid"])) if top_eff is not None else None
    can_fuse_top = (
        has_byte_dst and
        top_eff is not None and
        top_eff.get("type") in {'GRUNGE', 'GENERATOR', 'LEVELS'} and
        top_opacity > 1e-5 and
        (top_eff.get("type") == 'LEVELS' or top_rkey in _EFFECT_RAW_CACHE)
    )

    if can_fuse_top:
        # Evaluate all enabled effects strictly below last_enabled_idx into below_u8
        below_top_sig = (
            init_val,
            w,
            h,
            tuple(_effect_state_sig(node_ptr, layer_uid, e) for e in effects[:last_enabled_idx]),
        )
        enabled_below = [i for i in enabled_indices if i < last_enabled_idx]
        if not enabled_below:
            below_u8 = None  # Constant init_val base!
        else:
            below_cached = _STACK_BELOW_CACHE.get(key)
            if below_cached is not None and below_cached.get("sig") == below_top_sig:
                below_u8 = below_cached["below_u8"]
            else:
                below_u8 = np.full(w * h, init_val, dtype=np.uint8)
                for i in enabled_below:
                    _evaluate_single_effect_onto_acc(
                        node, layer, effects[i], below_u8, w, h,
                        context=context, force_reproject=False, from_gizmo=False,
                        is_first=(i == enabled_indices[0]), interactive=False,
                    )
                _STACK_BELOW_CACHE[key] = {"sig": below_top_sig, "below_u8": below_u8}

        etype = top_eff.get("type", "GRUNGE")
        blend_mode = 'REPLACE' if etype == 'LEVELS' else top_eff.get("blend_mode", "REPLACE")
        s_curve = _effect_curve_256(top_eff)

        if etype == 'LEVELS':
            src_1d = below_u8 if below_u8 is not None else np.full(w * h, init_val, dtype=np.uint8)
            b_1d = np.linspace(0.0, 1.0, 256, dtype=np.float32)
            s_1d = np.clip(s_curve, 0.0, 1.0)
            m_1d = b_1d + (s_1d - b_1d) * top_opacity
            lut_256_u32 = _pack_u8_lut_to_rgba_u32(m_1d)
            _parallel_take_u32(lut_256_u32, src_1d, dst_u32)
        else:
            rkey = (node_ptr, layer_uid, int(top_eff["uid"]))
            raw_u8 = _EFFECT_RAW_CACHE[rkey]["raw_u8"]

            if below_u8 is None or (blend_mode == 'REPLACE' and top_opacity >= 0.9999):
                # 1D 256-entry uint32 LUT (L1-cache resident, zero 16-bit pair index needed!)
                m_1d = _compute_blend_matrix_or_curve(
                    s_curve, blend_mode, top_opacity,
                    const_base_val=(init_val if below_u8 is None else None),
                )
                lut_256_u32 = _pack_u8_lut_to_rgba_u32(m_1d)
                _parallel_take_u32(lut_256_u32, raw_u8, dst_u32)
            else:
                # 2D 64K-entry uint32 LUT with cached 16-bit pair index (zero shifts, zero allocations on Balance/Contrast drag!)
                m_2d = _compute_blend_matrix_or_curve(s_curve, blend_mode, top_opacity, const_base_val=None)
                lut_64k_u32 = _pack_u8_lut_to_rgba_u32(m_2d)

                pcache = _PAIR_IDX_CACHE.get(key)
                raw_id = id(raw_u8)
                if pcache is None or pcache["w"] != w or pcache["h"] != h:
                    idx_u16 = np.empty(w * h, dtype=np.uint16)
                    pcache = {"w": w, "h": h, "below_sig": None, "raw_id": None, "idx_u16": idx_u16}
                    _PAIR_IDX_CACHE[key] = pcache
                else:
                    idx_u16 = pcache["idx_u16"]

                need_below = below_u8 if pcache["below_sig"] != below_top_sig else None
                need_raw = raw_u8 if pcache["raw_id"] != raw_id else None
                idx_pairs = idx_u16.view(np.uint8).reshape(-1, 2) if (need_below is not None or need_raw is not None) else None

                _parallel_take_u32(
                    lut_64k_u32, idx_u16, dst_u32,
                    raw_u8=need_raw, below_u8=need_below, idx_pairs=idx_pairs,
                )
                pcache["below_sig"] = below_top_sig
                pcache["raw_id"] = raw_id

        # Record 1024-point strided sample for 8us brush stroke detection; defer full 4MB copy to idle timer
        step = max(1, (w * h) // 1024)
        cur_u8 = dst_u32.view(np.uint8).reshape(-1, 4)[:, 0]
        _LAST_MASK_SAMPLE[key] = cur_u8[::step].copy()
        if bpy.app.background and any(e.get("type") == 'PAINT' for e in effects):
            _LAST_COMPOSITED_MASK[key] = cur_u8.copy()
        else:
            _LAST_COMPOSITED_MASK[key] = None
    else:
        # General fallback path (e.g. topmost effect is PAINT or float_data buffer)
        below_sig = (
            init_val,
            w,
            h,
            tuple(_effect_state_sig(node_ptr, layer_uid, e) for e in effects[:act_idx]),
        )
        below_cached = _STACK_BELOW_CACHE.get(key)
        if act_idx > 0 and below_cached is not None and below_cached.get("sig") == below_sig:
            acc_u8 = below_cached["below_u8"].copy()
            start_idx = act_idx
        else:
            acc_u8 = np.full(w * h, init_val, dtype=np.uint8)
            for i in range(act_idx):
                _evaluate_single_effect_onto_acc(
                    node, layer, effects[i], acc_u8, w, h,
                    context=context, force_reproject=False, from_gizmo=False, is_first=(i == 0),
                )
            if act_idx > 0:
                _STACK_BELOW_CACHE[key] = {"sig": below_sig, "below_u8": acc_u8.copy()}
            start_idx = act_idx

        for i in range(start_idx, len(effects)):
            _evaluate_single_effect_onto_acc(
                node, layer, effects[i], acc_u8, w, h,
                context=context,
                force_reproject=False,
                from_gizmo=(from_gizmo and i == act_idx),
                is_first=(i == 0),
                interactive=(interactive and i == act_idx),
            )

        _LAST_COMPOSITED_MASK[key] = acc_u8.copy()
        step = max(1, (w * h) // 1024)
        _LAST_MASK_SAMPLE[key] = acc_u8[::step].copy()

        if has_byte_dst:
            v_u32 = np.arange(256, dtype=np.uint32)
            lut_rgba = (255 << 24) | (v_u32 << 16) | (v_u32 << 8) | v_u32
            _parallel_take_u32(lut_rgba, acc_u8, dst_u32)
        elif c_imb.float_data:
            buf_f32 = (ctypes.c_float * (w * h * 4)).from_address(c_imb.float_data)
            dst_f32 = np.frombuffer(buf_f32, dtype=np.float32).reshape(-1, 4)
            g = acc_u8.astype(np.float32) * (1.0 / 255.0)
            dst_f32[:, 0] = g
            dst_f32[:, 1] = g
            dst_f32[:, 2] = g
            dst_f32[:, 3] = 1.0

    c_layer.runtime_dirty = 1
    st["active"] = True
    _save_layer_grunge_state(node, layer, st, flush_json=not interactive)
    if getattr(node, "paint_target", None) != 'MASK':
        node.paint_target = 'MASK'
    layer.invert_mask = layer.invert_mask
    _sync_layer_mask_to_companion_channels(node, layer)
    return True


# ---------------------------------------------------------------------------
# Context Lookup & 3D Projection Helpers
# ---------------------------------------------------------------------------

def _find_any_tex_paint_node(ntree):
    if ntree is None:
        return None
    fallback = None
    for node in ntree.nodes:
        if node.bl_idname == "ShaderNodeTexPaint":
            if node.image is not None:
                return node
            if fallback is None:
                fallback = node
    return fallback


_2D_STANDALONE_NG_NAME = ".TexPaint_2D_Layers"


def _is_standalone_2d_node(node):
    if node is None:
        return False
    id_data = getattr(node, "id_data", None)
    return id_data is not None and getattr(id_data, "name", "") == _2D_STANDALONE_NG_NAME


def _find_tex_paint_node_for_image(target_image, context=None):
    if target_image is None:
        return None
    ob = getattr(context, "active_object", None) if context else None
    mat = ob.active_material if ob else None
    if mat and mat.node_tree:
        for node in mat.node_tree.nodes:
            if node.bl_idname == "ShaderNodeTexPaint" and node.image == target_image:
                return node
    for mat in bpy.data.materials:
        if mat.node_tree:
            for node in mat.node_tree.nodes:
                if node.bl_idname == "ShaderNodeTexPaint" and node.image == target_image:
                    return node
    for ng in bpy.data.node_groups:
        for node in ng.nodes:
            if node.bl_idname == "ShaderNodeTexPaint" and node.image == target_image:
                return node
    return None


def _find_active_flat_paint_slot_node(mat):
    """If mat.paint_active_slot points to a legacy flat ShaderNodeTexImage (and not a ShaderNodeTexPaint), return (flat_node, slot_image)."""
    if mat is None or mat.node_tree is None:
        return None, None
    if not mat.texture_paint_slots or mat.paint_active_slot < 0 or mat.paint_active_slot >= len(mat.texture_paint_slots):
        return None, None
    slot_ima = (
        mat.texture_paint_images[mat.paint_active_slot]
        if mat.texture_paint_images and mat.paint_active_slot < len(mat.texture_paint_images)
        else None
    )
    if slot_ima is None:
        return None, None
    active_node = mat.node_tree.nodes.active
    if active_node is not None and active_node.bl_idname == "ShaderNodeTexPaint":
        return None, None
    for n in mat.node_tree.nodes:
        if n.bl_idname == "ShaderNodeTexPaint" and n.image == slot_ima:
            return None, None
    for n in mat.node_tree.nodes:
        if n.bl_idname == "ShaderNodeTexImage" and n.image == slot_ima:
            return n, slot_ima
    return None, None


def _find_3d_object_tex_paint_node(context):
    """Find the active 3D mesh object's ShaderNodeTexPaint node (ignoring Image Editor space)."""
    if context is None:
        return None
    ob = getattr(context, "active_object", None) or getattr(context, "image_paint_object", None)
    mat = ob.active_material if (ob and ob.type == 'MESH') else None
    if mat is None or mat.node_tree is None:
        return None
    node = mat.node_tree.nodes.active
    if node and node.bl_idname == "ShaderNodeTexPaint" and node.image is None:
        return node
    if mat.texture_paint_slots and 0 <= mat.paint_active_slot < len(mat.texture_paint_slots):
        image = (
            mat.texture_paint_images[mat.paint_active_slot]
            if mat.texture_paint_images and mat.paint_active_slot < len(mat.texture_paint_images)
            else None
        )
        if image is not None:
            for candidate in mat.node_tree.nodes:
                if candidate.bl_idname == "ShaderNodeTexPaint" and candidate.image == image:
                    return candidate
    if node and node.bl_idname == "ShaderNodeTexPaint":
        return node
    return _find_any_tex_paint_node(mat.node_tree)


def tex_paint_node_from_context(context):
    """Find the layered paint node edited in the current context, or None."""
    if context is None:
        return None
    ctx_node = getattr(context, "node", None)
    if ctx_node is not None and getattr(ctx_node, "bl_idname", "") == "ShaderNodeTexPaint":
        return ctx_node

    space = getattr(context, "space_data", None)
    if space and space.type == 'NODE_EDITOR':
        node = getattr(context, "active_node", None)
        if node and node.bl_idname == "ShaderNodeTexPaint":
            return node
        return _find_any_tex_paint_node(getattr(space, "edit_tree", None))

    if space and space.type == 'IMAGE_EDITOR':
        # Strictly match the Image Editor's selected image! If a normal single-layer image (e.g. a Logo/Pattern)
        # or no image is chosen in the Image Editor, return None so 3D texture painting tools are hidden.
        target_image = getattr(space, "image", None)
        if target_image is None:
            return None
        return _find_tex_paint_node_for_image(target_image, context)

    return _find_3d_object_tex_paint_node(context)


_3D_GIZMO_PROJ_MODES = {'TRIPLANAR', 'DECAL', 'WARP', 'SPHERICAL', 'CYLINDRICAL'}
_UV_GIZMO_PROJ_MODES = {'REPEAT', 'UV', 'FIT', 'UV_REPEAT'}


def _is_layer_grunge_gizmo_eligible(node, layer):
    if node is None or layer is None or not layer.use_mask:
        return False
    eff = _get_active_mask_effect(node, layer)
    if eff is None or eff.get("type") != 'GRUNGE' or not eff.get("enabled", True):
        return False
    return eff.get("proj_mode", "TRIPLANAR") in _3D_GIZMO_PROJ_MODES


def _active_uv_projection_from_context(context):
    """Return (ob, node, layer, target_kind, eff) when the active 3D multi-layer node has an active UV Projection."""
    if context is None:
        return None, None, None, None, None
    ob = getattr(context, "active_object", None) or getattr(context, "image_paint_object", None)
    node = tex_paint_node_from_context(context)
    if node is None:
        node = _find_3d_object_tex_paint_node(context)
    if node is None or node.image is None or _is_standalone_2d_node(node):
        return None, None, None, None, None
    if ob is None or ob.type != 'MESH':
        ob = _find_mesh_object_for_node(node, context)
    layer = node.active_layer
    if layer is None or layer.hide or layer.lock:
        return None, None, None, None, None

    p_target = getattr(node, "paint_target", 'COLOR')
    has_color_uv = (layer.type in {'PAINT', 'FILL'} and getattr(layer, "proj_mode", 'NONE') in {'FIT', 'UV_REPEAT'})
    eff = _get_active_mask_effect(node, layer) if getattr(layer, "use_mask", False) else None
    has_mask_uv = (
        eff is not None and
        eff.get("type") == 'GRUNGE' and
        eff.get("enabled", True) and
        eff.get("proj_mode", "TRIPLANAR") in {'REPEAT', 'UV', 'FIT'}
    )

    if p_target == 'MASK' and has_mask_uv:
        return ob, node, layer, 'MASK_FILL', eff
    if has_color_uv:
        return ob, node, layer, 'COLOR_LAYER', None
    if has_mask_uv:
        return ob, node, layer, 'MASK_FILL', eff
    return None, None, None, None, None


def _get_uv_gizmo_transform(node, layer, target_kind, eff):
    """Return (center_u, center_v, half_u, half_v, rot_rad, is_repeat) in [0, 1] UV space."""
    if target_kind == 'MASK_FILL' and eff is not None:
        off_x = float(eff.get("offset_x", 0.0))
        off_y = float(eff.get("offset_y", 0.0))
        lock_s = bool(eff.get("lock_scale", True))
        base_s = max(float(eff.get("scale", 2.0)), 0.02)
        sx = base_s if lock_s else max(float(eff.get("scale_x", base_s)), 0.02)
        sy = base_s if lock_s else max(float(eff.get("scale_y", base_s)), 0.02)
        rot_rad = float(eff.get("rotation", 0.0))
        is_repeat = (eff.get("uv_wrap", "REPEAT") != 'CLAMP')
        return (0.5 + off_x, 0.5 + off_y, 0.5 / sx, 0.5 / sy, rot_rad, is_repeat)

    off_x = float(layer.proj_location[0])
    off_y = float(layer.proj_location[1])
    rot_rad = float(layer.proj_rotation[2])
    raw_sx = max(abs(float(layer.proj_scale[0])), 0.01)
    raw_sy = max(abs(float(layer.proj_scale[1])), 0.01)
    is_repeat = (getattr(layer, "proj_mode", 'FIT') == 'UV_REPEAT')
    if is_repeat:
        half_u, half_v = 0.5 / raw_sx, 0.5 / raw_sy
    else:
        half_u, half_v = 0.5 * raw_sx, 0.5 * raw_sy
    return (0.5 + off_x, 0.5 + off_y, half_u, half_v, rot_rad, is_repeat)


def _set_uv_gizmo_transform(context, node, layer, target_kind, eff, center_u, center_v, half_u, half_v, rot_rad, lock_aspect=None, interactive=True):
    """Apply updated (center_u, center_v, half_u, half_v, rot_rad) from the 2D UV Gizmo back to the layer or mask fill."""
    import time
    off_x = max(min(float(center_u) - 0.5, 10.0), -10.0)
    off_y = max(min(float(center_v) - 0.5, 10.0), -10.0)
    half_u = max(float(half_u), 0.008)
    half_v = max(float(half_v), 0.008)

    if target_kind == 'MASK_FILL' and eff is not None:
        sx = max(min(0.5 / half_u, 64.0), 0.05)
        sy = max(min(0.5 / half_v, 64.0), 0.05)
        eff["offset_x"] = round(off_x, 4)
        eff["offset_y"] = round(off_y, 4)
        eff["rotation"] = float(rot_rad)
        eff["scale"] = round(sx, 4)
        eff["scale_x"] = round(sx, 4)
        eff["scale_y"] = round(sy, 4)
        if lock_aspect is not None:
            eff["lock_scale"] = bool(lock_aspect)
        _save_layer_grunge_state(node, layer, flush_json=not interactive)
        _evaluate_layer_grunge_mask(node, layer, context=context, force_reproject=True, interactive=interactive, skip_absorb=True)
        if interactive:
            _PENDING_HIRES_REFINE[(node.as_pointer(), _layer_uid(layer))] = (node, layer, time.perf_counter())
    elif target_kind == 'COLOR_LAYER' and layer is not None:
        is_repeat = (getattr(layer, "proj_mode", 'FIT') == 'UV_REPEAT')
        if is_repeat:
            new_sx = max(min(0.5 / half_u, 64.0), 0.05)
            new_sy = max(min(0.5 / half_v, 64.0), 0.05)
        else:
            new_sx = max(min(half_u * 2.0, 20.0), 0.01)
            new_sy = max(min(half_v * 2.0, 20.0), 0.01)
        layer.proj_location = (off_x, off_y, float(layer.proj_location[2]))
        layer.proj_rotation = (float(layer.proj_rotation[0]), float(layer.proj_rotation[1]), float(rot_rad))
        layer.proj_scale = (new_sx, new_sy, float(layer.proj_scale[2]))


def _active_projection_layer_from_context(context):
    """Return (ob, node, layer) when the active mesh has an active editable 3D Projection layer or Mask Fill."""
    if context is None:
        return None, None, None
    ob = getattr(context, "active_object", None) or getattr(context, "image_paint_object", None)
    if ob is None or ob.type != 'MESH':
        return None, None, None
    node = tex_paint_node_from_context(context)
    if node is None or node.image is None:
        return None, None, None
    layer = node.active_layer
    if layer is None or layer.hide or layer.lock:
        return None, None, None
    if layer.type in {'PAINT', 'FILL'} and getattr(layer, "proj_mode", 'NONE') in _3D_GIZMO_PROJ_MODES:
        return ob, node, layer
    if _is_layer_grunge_gizmo_eligible(node, layer):
        return ob, node, layer
    return None, None, None


def _effective_proj_mode(node, layer):
    if layer is not None and layer.type in {'PAINT', 'FILL'} and getattr(layer, "proj_mode", 'NONE') in _3D_GIZMO_PROJ_MODES:
        return layer.proj_mode
    if _is_layer_grunge_gizmo_eligible(node, layer):
        eff = _get_active_mask_effect(node, layer)
        return eff.get("proj_mode", "TRIPLANAR") if eff else 'TRIPLANAR'
    return 'NONE'


_LAST_COLOR_PROJ_SIG = {}  # (node_ptr, layer_uid) -> tuple


def _reproject_color_layer_multi_object(node, layer, context=None, force=True):
    """Project a 3D Decal / Triplanar / Warp color/fill layer across ALL mesh objects sharing the material."""
    global _INTERNAL_REPROJECTING
    import numpy as np
    if _INTERNAL_REPROJECTING or node is None or layer is None:
        return False
    if layer.type not in {'PAINT', 'FILL'}:
        return False
    pmode = getattr(layer, "proj_mode", 'NONE')
    im_name = getattr(layer, "proj_image_name", "")
    if pmode not in _3D_GIZMO_PROJ_MODES or not im_name:
        return False

    sharing_obs, first_c_ob = _find_all_mesh_objects_for_node(node, context)
    if not sharing_obs or first_c_ob is None:
        return False
    active_ob = sharing_obs[-1]

    c_layer = _C_TexPaintLayer.from_address(layer.as_pointer())
    orig_loc = (float(c_layer.proj_location[0]), float(c_layer.proj_location[1]), float(c_layer.proj_location[2]))
    orig_rot = (float(c_layer.proj_rotation[0]), float(c_layer.proj_rotation[1]), float(c_layer.proj_rotation[2]))
    orig_scl = (float(c_layer.proj_scale[0]), float(c_layer.proj_scale[1]), float(c_layer.proj_scale[2]))
    falloff = float(layer.proj_blend_falloff)

    key = (node.as_pointer(), _layer_uid(layer))
    sig = (
        pmode,
        im_name,
        tuple(round(v, 4) for v in orig_loc),
        tuple(round(v, 4) for v in orig_rot),
        tuple(round(v, 4) for v in orig_scl),
        round(falloff, 4),
        active_ob.name,
        tuple((o.name, o.data.as_pointer(), len(o.data.vertices)) for o in sharing_obs),
    )
    if not force and _LAST_COLOR_PROJ_SIG.get(key) == sig:
        return False

    if len(sharing_obs) == 1 and sharing_obs[0] == first_c_ob:
        _LAST_COLOR_PROJ_SIG[key] = sig
        return False

    c_data_slot, orig_mesh_ptr = _get_object_data_ptr_slot(first_c_ob)
    if c_data_slot is None:
        return False

    w = max(int(node.canvas_width), 1)
    h = max(int(node.canvas_height), 1)
    c_node_id = ctypes.c_void_p.from_address(node.as_pointer() + 216)
    orig_node_id = c_node_id.value
    acc_u8 = None
    acc_f32 = None

    _INTERNAL_REPROJECTING = True
    try:
        c_node_id.value = 0
        for idx, ob_part in enumerate(sharing_obs):
            if ob_part.data is None:
                continue
            c_data_slot.value = ob_part.data.as_pointer()
            loc_p, rot_p, scl_p = _transform_widget_between_objects(orig_loc, orig_rot, orig_scl, active_ob, ob_part)
            for i in range(3):
                c_layer.proj_location[i] = loc_p[i]
                c_layer.proj_rotation[i] = rot_p[i]
                c_layer.proj_scale[i] = scl_p[i]
            orig_uid = c_layer.uid
            c_layer.uid = orig_uid + 20000 + idx
            layer.proj_blend_falloff = falloff
            c_layer.uid = orig_uid

            if c_layer.ibuf:
                c_imb = _C_ImBuf.from_address(c_layer.ibuf)
                if c_imb.byte_data:
                    buf = (ctypes.c_uint8 * (w * h * 4)).from_address(c_imb.byte_data)
                    part_u8 = np.frombuffer(buf, dtype=np.uint8).reshape(-1, 4)
                    if acc_u8 is None:
                        acc_u8 = part_u8.copy()
                    else:
                        hit = (part_u8[:, 3] > 0) & (part_u8[:, 3] >= acc_u8[:, 3])
                        acc_u8[hit] = part_u8[hit]
                elif c_imb.float_data:
                    buf = (ctypes.c_float * (w * h * 4)).from_address(c_imb.float_data)
                    part_f = np.frombuffer(buf, dtype=np.float32).reshape(-1, 4)
                    if acc_f32 is None:
                        acc_f32 = part_f.copy()
                    else:
                        hit = (part_f[:, 3] > 1e-5) & (part_f[:, 3] >= acc_f32[:, 3])
                        acc_f32[hit] = part_f[hit]

        if c_layer.ibuf:
            c_imb = _C_ImBuf.from_address(c_layer.ibuf)
            if acc_u8 is not None and c_imb.byte_data:
                buf = (ctypes.c_uint8 * (w * h * 4)).from_address(c_imb.byte_data)
                np.frombuffer(buf, dtype=np.uint8).reshape(-1, 4)[:] = acc_u8
                c_layer.runtime_dirty = 1
            elif acc_f32 is not None and c_imb.float_data:
                buf = (ctypes.c_float * (w * h * 4)).from_address(c_imb.float_data)
                np.frombuffer(buf, dtype=np.float32).reshape(-1, 4)[:] = acc_f32
                c_layer.runtime_dirty = 1
    finally:
        c_data_slot.value = orig_mesh_ptr
        for i in range(3):
            c_layer.proj_location[i] = orig_loc[i]
            c_layer.proj_rotation[i] = orig_rot[i]
            c_layer.proj_scale[i] = orig_scl[i]
        c_node_id.value = orig_node_id
        _INTERNAL_REPROJECTING = False

    _LAST_COLOR_PROJ_SIG[key] = sig
    layer.opacity = layer.opacity
    return True


def _notify_gizmo_projection_changed(node, layer, context=None):
    if _is_layer_grunge_gizmo_eligible(node, layer):
        _evaluate_layer_grunge_mask(node, layer, context=context, force_reproject=True, from_gizmo=True, interactive=True)
    elif layer is not None and layer.type in {'PAINT', 'FILL'} and getattr(layer, "proj_mode", 'NONE') in _3D_GIZMO_PROJ_MODES:
        _reproject_color_layer_multi_object(node, layer, context=context, force=True)


def _rotation_from_normal_and_up(normal_vec, up_hint):
    """Construct an Object-space Euler XYZ rotation where +Z aligns with normal_vec."""
    z_axis = normal_vec.normalized()
    if z_axis.length_squared < 1e-8:
        z_axis = Vector((0.0, 0.0, 1.0))
    up = up_hint - z_axis * up_hint.dot(z_axis)
    if up.length_squared < 1e-6:
        fallback = Vector((0.0, 0.0, 1.0)) if abs(z_axis.z) < 0.9 else Vector((0.0, 1.0, 0.0))
        up = fallback - z_axis * fallback.dot(z_axis)
    y_axis = up.normalized()
    x_axis = y_axis.cross(z_axis).normalized()
    y_axis = z_axis.cross(x_axis).normalized()
    rot_mat = Matrix((
        (x_axis.x, y_axis.x, z_axis.x),
        (x_axis.y, y_axis.y, z_axis.y),
        (x_axis.z, y_axis.z, z_axis.z),
    ))
    return rot_mat.to_euler('XYZ')


def _axis_basis_matrix(axis_dir, origin):
    """Build a 4x4 world matrix whose local +Z (column 2) points along axis_dir at origin."""
    z_axis = axis_dir.normalized()
    if z_axis.length_squared < 1e-8:
        z_axis = Vector((0.0, 0.0, 1.0))
    ref = Vector((0.0, 0.0, 1.0)) if abs(z_axis.z) < 0.9 else Vector((0.0, 1.0, 0.0))
    x_axis = ref.cross(z_axis).normalized()
    y_axis = z_axis.cross(x_axis).normalized()
    mat = Matrix.Identity(4)
    mat[0][0], mat[1][0], mat[2][0] = x_axis.x, x_axis.y, x_axis.z
    mat[0][1], mat[1][1], mat[2][1] = y_axis.x, y_axis.y, y_axis.z
    mat[0][2], mat[1][2], mat[2][2] = z_axis.x, z_axis.y, z_axis.z
    mat[0][3], mat[1][3], mat[2][3] = origin.x, origin.y, origin.z
    return mat


def _tag_view3d_gizmo_refresh(context):
    if context is None:
        return
    _ob, _node, layer = _active_projection_layer_from_context(context)
    if layer is not None and hasattr(layer, "show_projection_widget"):
        layer.show_projection_widget = layer.show_projection_widget
    if getattr(context, "screen", None) is not None:
        for area in context.screen.areas:
            if area.type in {'VIEW_3D', 'IMAGE_EDITOR', 'NODE_EDITOR'}:
                area.tag_redraw()


# ---------------------------------------------------------------------------
# Operators for 3D Projection Widget & Interactive Modal Shortcuts (G / R / S)
# ---------------------------------------------------------------------------

_MODAL_TRANSFORM_STATE = {
    "active": False,
    "mode": 'TRANSLATE',
    "world_origin": None,
    "world_current": None,
    "axis": None,
    "local_space": True,
    "axes_world": (),
}

_PROJECTION_KEYMAPS = []


class NODE_OT_tex_paint_gizmo_mode(Operator):
    """Interactively Move (G), Rotate (R), or Scale (S) the 3D Projection Widget, or switch gizmo handles"""
    bl_idname = "node.tex_paint_gizmo_mode"
    bl_label = "Transform 3D Projection Widget"
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}

    mode: EnumProperty(
        name="Mode",
        items=(
            ('TRANSLATE', "Move (G)", "Interactively move the 3D projection box (Shortcut: G)"),
            ('ROTATE', "Rotate (R)", "Interactively rotate the 3D projection box (Shortcut: R)"),
            ('SCALE', "Scale (S)", "Interactively scale the 3D projection box (Shortcut: S)"),
            ('ALL', "All", "Show Move, Rotate, and Scale 3D gizmo handles together"),
            ('RESET_LOC', "Reset Location", "Reset projection location to mesh surface/center (Alt+G)"),
            ('RESET_ROT', "Reset Rotation", "Reset projection rotation (Alt+R)"),
            ('RESET_SCALE', "Reset Scale", "Reset projection scale (Alt+S)"),
        ),
        default='TRANSLATE',
    )

    @classmethod
    def poll(cls, context):
        _ob, _node, layer = _active_projection_layer_from_context(context)
        if layer is not None:
            return True
        _uob, _unode, ulayer, _tk, _eff = _active_uv_projection_from_context(context)
        return ulayer is not None

    def execute(self, context):
        ob, node, layer = _active_projection_layer_from_context(context)
        if layer is None:
            _uob, unode, ulayer, target_kind, eff = _active_uv_projection_from_context(context)
            if ulayer is None:
                return {'PASS_THROUGH'}
            if not ulayer.show_projection_widget:
                ulayer.show_projection_widget = True
            wm = context.window_manager
            if self.mode in {'TRANSLATE', 'ROTATE', 'SCALE', 'ALL'}:
                current = getattr(wm, "tex_paint_gizmo_mode", 'ALL')
                wm.tex_paint_gizmo_mode = 'ALL' if (current == self.mode and self.mode != 'ALL') else self.mode
            else:
                cu, cv, hu, hv, rot_r, _rep = _get_uv_gizmo_transform(unode, ulayer, target_kind, eff)
                if self.mode == 'RESET_LOC':
                    cu, cv = 0.5, 0.5
                elif self.mode == 'RESET_ROT':
                    rot_r = 0.0
                elif self.mode == 'RESET_SCALE':
                    hu, hv = 0.5, 0.5
                _set_uv_gizmo_transform(context, unode, ulayer, target_kind, eff, cu, cv, hu, hv, rot_r, interactive=False)
            _tag_view3d_gizmo_refresh(context)
            return {'FINISHED'}

        if not layer.show_projection_widget:
            layer.show_projection_widget = True

        eff_mode = _effective_proj_mode(node, layer)
        wm = context.window_manager
        if self.mode in {'TRANSLATE', 'ROTATE', 'SCALE', 'ALL'}:
            current = getattr(wm, "tex_paint_gizmo_mode", 'ALL')
            if current == self.mode and self.mode != 'ALL':
                wm.tex_paint_gizmo_mode = 'ALL'
            else:
                wm.tex_paint_gizmo_mode = self.mode
        elif self.mode == 'RESET_LOC' and ob is not None:
            bbox = [Vector(co) for co in ob.bound_box]
            center = sum(bbox, Vector((0.0, 0.0, 0.0))) / 8.0
            if eff_mode == 'DECAL':
                ok, hit_loc, hit_no, _ = ob.closest_point_on_mesh(center)
                if ok:
                    center = hit_loc
                    layer.proj_rotation = _rotation_from_normal_and_up(hit_no, Vector((0.0, 0.0, 1.0)))
            layer.proj_location = center
            _notify_gizmo_projection_changed(node, layer, context)
        elif self.mode == 'RESET_ROT':
            layer.proj_rotation = (0.0, 0.0, 0.0)
            _notify_gizmo_projection_changed(node, layer, context)
        elif self.mode == 'RESET_SCALE' and ob is not None:
            dims = ob.dimensions
            avg_ext = max((dims.x + dims.y + dims.z) / 6.0, 0.1)
            if eff_mode == 'DECAL':
                layer.proj_scale = (avg_ext * 0.5, avg_ext * 0.5, avg_ext * 0.8)
            else:
                layer.proj_scale = (avg_ext, avg_ext, avg_ext)
            _notify_gizmo_projection_changed(node, layer, context)

        _tag_view3d_gizmo_refresh(context)
        return {'FINISHED'}

    def _init_modal_basis(self, context, ob, layer, event):
        from bpy_extras import view3d_utils

        self._init_loc = Vector(layer.proj_location)
        self._init_rot = Euler(layer.proj_rotation, 'XYZ')
        self._init_rot_mat = self._init_rot.to_matrix()
        self._init_scale = Vector(layer.proj_scale)

        self._ob_mat = ob.matrix_world.copy()
        self._ob_inv = self._ob_mat.inverted_safe()
        self._ob_3x3 = self._ob_mat.to_3x3()
        self._ob_inv_3x3 = self._ob_inv.to_3x3()
        self._init_world_pos = self._ob_mat @ self._init_loc

        self._local_axes_world = []
        self._obj_axes_world = []
        for i in range(3):
            lw = self._ob_3x3 @ self._init_rot_mat.col[i]
            self._local_axes_world.append(lw.normalized() if lw.length > 1e-6 else Vector((i == 0, i == 1, i == 2)))
            ow = self._ob_3x3.col[i]
            self._obj_axes_world.append(ow.normalized() if ow.length > 1e-6 else Vector((i == 0, i == 1, i == 2)))

        self._start_mouse = Vector((float(event.mouse_region_x), float(event.mouse_region_y)))
        self._cur_mouse = self._start_mouse.copy()
        self._prev_mouse = self._start_mouse.copy()
        self._prev_angle = None
        self._accum_angle = 0.0
        self._trackball_phi = [0.0, 0.0]
        self._num_str = ""

        region = context.region
        rv3d = context.region_data
        center_2d = None
        if region is not None and rv3d is not None:
            center_2d = view3d_utils.location_3d_to_region_2d(region, rv3d, self._init_world_pos)
        if center_2d is None and region is not None:
            center_2d = Vector((region.width * 0.5, region.height * 0.5))
        elif center_2d is None:
            center_2d = self._start_mouse.copy()
        self._center_2d = Vector((float(center_2d[0]), float(center_2d[1])))

        d0 = (self._start_mouse - self._center_2d).length
        self._start_dist = max(d0, 25.0)
        v0 = self._start_mouse - self._center_2d
        self._start_angle = math.atan2(v0.y, v0.x) if v0.length > 1e-3 else 0.0
        self._prev_angle = self._start_angle

    def _get_active_axes_world(self):
        return self._local_axes_world if self._local_space else self._obj_axes_world

    def _update_modal_overlay_and_header(self, context, info_str):
        axes_w = self._get_active_axes_world()
        _MODAL_TRANSFORM_STATE["active"] = True
        _MODAL_TRANSFORM_STATE["mode"] = self._active_mode
        _MODAL_TRANSFORM_STATE["world_origin"] = self._init_world_pos
        _MODAL_TRANSFORM_STATE["axis"] = self._axis
        _MODAL_TRANSFORM_STATE["local_space"] = self._local_space
        _MODAL_TRANSFORM_STATE["axes_world"] = axes_w

        if context.area:
            space_lbl = "Local" if self._local_space else "Object"
            if self._active_mode == 'TRACKBALL':
                mode_title = "Trackball Rotate (R R)"
                axis_lbl = " [Free 3D]"
            else:
                mode_title = self._active_mode.title()
                axis_lbl = f" [{space_lbl} {self._axis}]" if self._axis else " [View]"
            num_lbl = f" | Input: {self._num_str}" if self._num_str else ""
            context.area.header_text_set(
                f"3D Projection {mode_title}{axis_lbl}: {info_str}{num_lbl}  |  "
                f"G/R/S: Mode (R R: Trackball)  |  X/Y/Z: Axis (2x=Object, Shift=Plane)  |  Shift: Precision  |  Ctrl: Snap  |  LMB/Enter: Confirm  |  RMB/Esc: Cancel"
            )

    def _apply_modal_transform(self, context, event):
        from bpy_extras import view3d_utils

        ob, node, layer = _active_projection_layer_from_context(context)
        region = context.region
        rv3d = context.region_data
        if ob is None or layer is None or region is None or rv3d is None:
            return

        axes_w = self._get_active_axes_world()
        axis_map = {'X': 0, 'Y': 1, 'Z': 2}
        plane_normal_map = {'YZ': 0, 'XZ': 1, 'XY': 2}

        typed_val = None
        if self._num_str and self._num_str not in {"-", ".", "-."}:
            try:
                typed_val = float(self._num_str)
            except ValueError:
                typed_val = None

        if self._active_mode == 'TRANSLATE':
            p_start = view3d_utils.region_2d_to_location_3d(
                region, rv3d, (self._start_mouse.x, self._start_mouse.y), self._init_world_pos
            )
            p_cur = view3d_utils.region_2d_to_location_3d(
                region, rv3d, (self._cur_mouse.x, self._cur_mouse.y), self._init_world_pos
            )
            delta_w = p_cur - p_start
            if event.shift:
                delta_w *= 0.2

            if self._axis in axis_map:
                ax_idx = axis_map[self._axis]
                ax_dir = axes_w[ax_idx]
                dist = typed_val if typed_val is not None else float(delta_w.dot(ax_dir))
                if event.ctrl and typed_val is None:
                    dist = round(dist * 10.0) * 0.1
                delta_w = ax_dir * dist
                info_str = f"D{self._axis} = {dist:+.3f}"
            elif self._axis in plane_normal_map:
                n_idx = plane_normal_map[self._axis]
                n_dir = axes_w[n_idx]
                delta_w = delta_w - n_dir * float(delta_w.dot(n_dir))
                if event.ctrl and typed_val is None:
                    delta_w = Vector((round( c * 10.0) * 0.1 for c in delta_w))
                info_str = f"D = ({delta_w.x:+.3f}, {delta_w.y:+.3f}, {delta_w.z:+.3f})"
            else:
                if typed_val is not None:
                    view_right = Vector(rv3d.view_matrix.inverted_safe().col[0][:3]).normalized()
                    delta_w = view_right * typed_val
                elif event.ctrl:
                    delta_w = Vector((round(c * 10.0) * 0.1 for c in delta_w))
                info_str = f"Dx={delta_w.x:+.3f}  Dy={delta_w.y:+.3f}  Dz={delta_w.z:+.3f}"

            new_world_pos = self._init_world_pos + delta_w
            _MODAL_TRANSFORM_STATE["world_current"] = new_world_pos
            layer.proj_location = self._ob_inv @ new_world_pos
            _notify_gizmo_projection_changed(node, layer, context)
            self._update_modal_overlay_and_header(context, info_str)

        elif self._active_mode == 'ROTATE':
            v_cur = self._cur_mouse - self._center_2d
            if v_cur.length > 1e-3:
                cur_ang = math.atan2(v_cur.y, v_cur.x)
                diff = cur_ang - self._prev_angle
                while diff > math.pi:
                    diff -= 2.0 * math.pi
                while diff < -math.pi:
                    diff += 2.0 * math.pi
                self._accum_angle += diff * (0.2 if event.shift else 1.0)
                self._prev_angle = cur_ang

            angle_rad = math.radians(typed_val) if typed_val is not None else self._accum_angle
            if event.ctrl and typed_val is None:
                step = math.radians(5.0 if event.shift else 15.0)
                angle_rad = round(angle_rad / step) * step

            if self._axis in axis_map:
                ax_idx = axis_map[self._axis]
                ax_w = axes_w[ax_idx]
            elif self._axis in plane_normal_map:
                ax_idx = plane_normal_map[self._axis]
                ax_w = axes_w[ax_idx]
            else:
                view_inv = rv3d.view_matrix.inverted_safe()
                ax_w = Vector(view_inv.col[2][:3]).normalized()

            # Flip sign if camera looks opposite to rotation axis so screen motion matches rotation sense
            view_inv = rv3d.view_matrix.inverted_safe()
            view_z_w = Vector(view_inv.col[2][:3]).normalized()
            sign = -1.0 if ax_w.dot(view_z_w) < 0.0 else 1.0

            ax_obj = (self._ob_inv_3x3 @ ax_w).normalized()
            if ax_obj.length_squared < 1e-8:
                ax_obj = Vector((0.0, 0.0, 1.0))

            rot_delta = Matrix.Rotation(angle_rad * sign, 3, ax_obj)
            new_rot_mat = rot_delta @ self._init_rot_mat
            layer.proj_rotation = new_rot_mat.to_euler('XYZ', self._init_rot)
            _MODAL_TRANSFORM_STATE["world_current"] = self._init_world_pos
            _notify_gizmo_projection_changed(node, layer, context)
            info_str = f"Angle = {math.degrees(angle_rad):+.1f}°"
            self._update_modal_overlay_and_header(context, info_str)

        elif self._active_mode == 'TRACKBALL':
            dx = self._cur_mouse.x - self._prev_mouse.x
            dy = self._cur_mouse.y - self._prev_mouse.y
            fac = 0.01 * (0.2 if event.shift else 1.0)
            self._trackball_phi[0] += -dy * fac
            self._trackball_phi[1] += dx * fac

            phi0 = math.radians(typed_val) if typed_val is not None else self._trackball_phi[0]
            phi1 = 0.0 if typed_val is not None else self._trackball_phi[1]
            if event.ctrl and typed_val is None:
                step = math.radians(1.0 if event.shift else 5.0)
                phi0 = round(phi0 / step) * step
                phi1 = round(phi1 / step) * step

            view_inv = rv3d.view_matrix.inverted_safe()
            axis1_w = Vector(view_inv.col[0][:3]).normalized()
            axis2_w = Vector(view_inv.col[1][:3]).normalized()
            rot_vec_w = axis1_w * phi0 + axis2_w * phi1
            angle_rad = rot_vec_w.length

            if angle_rad > 1e-7:
                ax_w = rot_vec_w / angle_rad
                ax_obj = (self._ob_inv_3x3 @ ax_w).normalized()
                if ax_obj.length_squared < 1e-8:
                    ax_obj = Vector((0.0, 0.0, 1.0))
                rot_delta = Matrix.Rotation(angle_rad, 3, ax_obj)
                new_rot_mat = rot_delta @ self._init_rot_mat
            else:
                new_rot_mat = self._init_rot_mat.copy()

            layer.proj_rotation = new_rot_mat.to_euler('XYZ', self._init_rot)
            _MODAL_TRANSFORM_STATE["world_current"] = self._init_world_pos
            _notify_gizmo_projection_changed(node, layer, context)
            info_str = f"Trackball: {math.degrees(phi0):+.2f}°  {math.degrees(phi1):+.2f}°"
            self._update_modal_overlay_and_header(context, info_str)

        elif self._active_mode == 'SCALE':
            d_cur = (self._cur_mouse - self._center_2d).length
            raw_ratio = d_cur / self._start_dist
            if event.shift:
                raw_ratio = 1.0 + (raw_ratio - 1.0) * 0.2

            ratio = typed_val if typed_val is not None else raw_ratio
            if event.ctrl and typed_val is None:
                step = 0.02 if event.shift else 0.1
                ratio = round(ratio / step) * step
            ratio = max(0.005, abs(ratio))

            new_scale = list(self._init_scale)
            if self._axis in axis_map:
                ax_idx = axis_map[self._axis]
                new_scale[ax_idx] = max(0.005, self._init_scale[ax_idx] * ratio)
                info_str = f"Scale {self._axis} = {ratio:.3f}x"
            elif self._axis in plane_normal_map:
                n_idx = plane_normal_map[self._axis]
                for i in range(3):
                    if i != n_idx:
                        new_scale[i] = max(0.005, self._init_scale[i] * ratio)
                info_str = f"Scale {self._axis} = {ratio:.3f}x"
            else:
                new_scale = [max(0.005, s * ratio) for s in self._init_scale]
                info_str = f"Scale = {ratio:.3f}x"

            layer.proj_scale = new_scale
            _MODAL_TRANSFORM_STATE["world_current"] = self._init_world_pos
            _notify_gizmo_projection_changed(node, layer, context)
            self._update_modal_overlay_and_header(context, info_str)

        self._prev_mouse = self._cur_mouse.copy()
        _tag_view3d_gizmo_refresh(context)

    def _restore_initial_state(self, context):
        _ob, node, layer = _active_projection_layer_from_context(context)
        if layer is not None:
            layer.proj_location = self._init_loc
            layer.proj_rotation = self._init_rot
            layer.proj_scale = self._init_scale
            _notify_gizmo_projection_changed(node, layer, context)

    def _finish_modal(self, context):
        _MODAL_TRANSFORM_STATE["active"] = False
        _MODAL_TRANSFORM_STATE["world_origin"] = None
        _MODAL_TRANSFORM_STATE["world_current"] = None
        _MODAL_TRANSFORM_STATE["axis"] = None
        try:
            context.window.cursor_modal_restore()
        except Exception:
            pass
        if getattr(context, "area", None):
            context.area.header_text_set(None)
        _flush_dirty_node_json()
        _tag_view3d_gizmo_refresh(context)

    def invoke(self, context, event):
        ob, node, layer = _active_projection_layer_from_context(context)
        if ob is None or layer is None:
            return {'PASS_THROUGH'}

        # If invoked from a UI button (N-panel / Properties), just switch gizmo mode or run reset
        if (
            context.space_data is None or
            context.space_data.type != 'VIEW_3D' or
            context.region is None or
            context.region.type != 'WINDOW' or
            context.region_data is None
        ):
            return self.execute(context)

        # In the 3D Viewport window, only intercept G/R/S when in Texture Paint mode!
        if getattr(context, "mode", "") != 'PAINT_TEXTURE':
            return {'PASS_THROUGH'}

        widget_visible = bool(getattr(layer, "show_projection_widget", False))
        if not widget_visible:
            # In Texture Paint mode, pressing G or R on a 3D projection layer auto-shows the widget and starts moving/rotating
            if self.mode in {'TRANSLATE', 'ROTATE'}:
                layer.show_projection_widget = True
            else:
                return {'PASS_THROUGH'}

        if self.mode in {'ALL', 'RESET_LOC', 'RESET_ROT', 'RESET_SCALE'}:
            return self.execute(context)

        self._active_mode = self.mode
        self._axis = None
        self._local_space = True
        context.window_manager.tex_paint_gizmo_mode = self._active_mode

        self._init_modal_basis(context, ob, layer, event)
        context.window.cursor_modal_set('SCROLL_XY')
        self._update_modal_overlay_and_header(context, "Move mouse to adjust")
        self._tag_redraw(context)
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def _tag_redraw(self, context):
        if getattr(context, "area", None):
            context.area.tag_redraw()

    def modal(self, context, event):
        ob, node, layer = _active_projection_layer_from_context(context)
        if ob is None or layer is None:
            self._finish_modal(context)
            return {'CANCELLED'}

        # Allow middle-mouse viewport navigation during transform
        if event.type in {'MIDDLEMOUSE', 'WHEELUPMOUSE', 'WHEELDOWNMOUSE'} or (
            event.alt and event.type in {'LEFTMOUSE', 'MIDDLEMOUSE', 'RIGHTMOUSE'}
        ):
            return {'PASS_THROUGH'}

        if event.type == 'MOUSEMOVE':
            self._cur_mouse = Vector((float(event.mouse_region_x), float(event.mouse_region_y)))
            self._apply_modal_transform(context, event)
            return {'RUNNING_MODAL'}

        # Confirm on Left-Click, Enter, or Space
        if event.type in {'LEFTMOUSE', 'RET', 'NUMPAD_ENTER', 'SPACE'} and event.value == 'PRESS':
            self._finish_modal(context)
            return {'FINISHED'}

        # Cancel on Right-Click or Esc
        if event.type in {'RIGHTMOUSE', 'ESC'} and event.value == 'PRESS':
            self._restore_initial_state(context)
            self._finish_modal(context)
            return {'CANCELLED'}

        # Switch between G (Move), R (Rotate), R->R (Trackball), and S (Scale) mid-modal just like Blender!
        if event.value == 'PRESS' and event.type in {'G', 'R', 'S'}:
            if event.type == 'R' and self._active_mode == 'ROTATE':
                new_mode = 'TRACKBALL'
            elif event.type == 'R' and self._active_mode == 'TRACKBALL':
                new_mode = 'ROTATE'
            else:
                new_mode = {'G': 'TRANSLATE', 'R': 'ROTATE', 'S': 'SCALE'}[event.type]

            if new_mode != self._active_mode:
                self._restore_initial_state(context)
                self._active_mode = new_mode
                self._axis = None
                self._local_space = True
                context.window_manager.tex_paint_gizmo_mode = (
                    'ROTATE' if self._active_mode == 'TRACKBALL' else self._active_mode
                )
                self._init_modal_basis(context, ob, layer, event)
                self._apply_modal_transform(context, event)
            return {'RUNNING_MODAL'}

        # Axis and Plane Locking: X / Y / Z (press once = Local, twice = Object, third = Free; Shift+X/Y/Z = Plane)
        if event.value == 'PRESS' and event.type in {'X', 'Y', 'Z'}:
            if self._active_mode == 'TRACKBALL':
                self._active_mode = 'ROTATE'
            plane_for = {'X': 'YZ', 'Y': 'XZ', 'Z': 'XY'}
            if event.shift:
                target_plane = plane_for[event.type]
                if self._axis == target_plane and self._local_space:
                    self._local_space = False
                elif self._axis == target_plane and not self._local_space:
                    self._axis = None
                    self._local_space = True
                else:
                    self._axis = target_plane
                    self._local_space = True
            else:
                if self._axis == event.type and self._local_space:
                    self._local_space = False
                elif self._axis == event.type and not self._local_space:
                    self._axis = None
                    self._local_space = True
                else:
                    self._axis = event.type
                    self._local_space = True
            self._restore_initial_state(context)
            self._apply_modal_transform(context, event)
            return {'RUNNING_MODAL'}

        # Numeric keyboard input (0-9, '.', '-', Backspace)
        if event.value == 'PRESS':
            digit_map = {
                'ZERO': '0', 'ONE': '1', 'TWO': '2', 'THREE': '3', 'FOUR': '4',
                'FIVE': '5', 'SIX': '6', 'SEVEN': '7', 'EIGHT': '8', 'NINE': '9',
                'NUMPAD_0': '0', 'NUMPAD_1': '1', 'NUMPAD_2': '2', 'NUMPAD_3': '3', 'NUMPAD_4': '4',
                'NUMPAD_5': '5', 'NUMPAD_6': '6', 'NUMPAD_7': '7', 'NUMPAD_8': '8', 'NUMPAD_9': '9',
            }
            if event.type in digit_map:
                self._num_str += digit_map[event.type]
                self._apply_modal_transform(context, event)
                return {'RUNNING_MODAL'}
            elif event.type in {'PERIOD', 'NUMPAD_PERIOD'} and '.' not in self._num_str:
                self._num_str += '.'
                self._apply_modal_transform(context, event)
                return {'RUNNING_MODAL'}
            elif event.type in {'MINUS', 'NUMPAD_MINUS'}:
                if self._num_str.startswith('-'):
                    self._num_str = self._num_str[1:]
                else:
                    self._num_str = '-' + self._num_str
                self._apply_modal_transform(context, event)
                return {'RUNNING_MODAL'}
            elif event.type == 'BACK_SPACE' and self._num_str:
                self._num_str = self._num_str[:-1]
                self._apply_modal_transform(context, event)
                return {'RUNNING_MODAL'}

        return {'RUNNING_MODAL'}


class NODE_OT_tex_paint_layer_align_view(Operator):
    """Align the active Decal / 3D Projection widget to face the current 3D Viewport camera"""
    bl_idname = "node.tex_paint_layer_align_view"
    bl_label = "Align Projection to View"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        _ob, _node, layer = _active_projection_layer_from_context(context)
        return layer is not None and context.region_data is not None

    def execute(self, context):
        ob, node, layer = _active_projection_layer_from_context(context)
        rv3d = context.region_data
        if ob is None or layer is None or rv3d is None:
            return {'CANCELLED'}

        ob_inv_3x3 = ob.matrix_world.inverted_safe().to_3x3()
        view_inv = rv3d.view_matrix.inverted_safe()
        view_z_obj = (ob_inv_3x3 @ Vector(view_inv.col[2][:3])).normalized()
        view_y_obj = (ob_inv_3x3 @ Vector(view_inv.col[1][:3])).normalized()

        layer.proj_rotation = _rotation_from_normal_and_up(view_z_obj, view_y_obj)

        bbox = [Vector(co) for co in ob.bound_box]
        center_obj = sum(bbox, Vector((0.0, 0.0, 0.0))) / 8.0
        radius = max((bbox[6] - bbox[0]).length, 1.0)
        ray_start = center_obj + view_z_obj * (radius * 2.0)
        hit, hit_loc, _hit_no, _ = ob.ray_cast(ray_start, -view_z_obj, distance=radius * 4.0)
        if hit:
            layer.proj_location = hit_loc

        layer.show_projection_widget = True
        _notify_gizmo_projection_changed(node, layer, context)
        _tag_view3d_gizmo_refresh(context)
        return {'FINISHED'}


class NODE_OT_tex_paint_layer_snap_decal(Operator):
    """Click or drag on the 3D mesh surface to snap the projection position and normal (Wheel = Scale, Shift = Rotate)"""
    bl_idname = "node.tex_paint_layer_snap_decal"
    bl_label = "Place Projection on Surface"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        _ob, _node, layer = _active_projection_layer_from_context(context)
        return (
            layer is not None and
            context.space_data is not None and
            context.space_data.type == 'VIEW_3D'
        )

    def _raycast_surface(self, context, event):
        from bpy_extras import view3d_utils
        ob, node, layer = _active_projection_layer_from_context(context)
        region = context.region
        rv3d = context.region_data
        if ob is None or layer is None or region is None or rv3d is None:
            return False

        coord = (event.mouse_region_x, event.mouse_region_y)
        ray_origin_world = view3d_utils.region_2d_to_origin_3d(region, rv3d, coord)
        ray_dir_world = view3d_utils.region_2d_to_vector_3d(region, rv3d, coord).normalized()

        sharing_obs, _first_c = _find_all_mesh_objects_for_node(node, context)
        cand_obs = sharing_obs if sharing_obs else [ob]

        best_dist_sq = 1e30
        best_world_loc = None
        best_world_no = None

        for cand_ob in cand_obs:
            c_inv = cand_ob.matrix_world.inverted_safe()
            r_org = c_inv @ ray_origin_world
            r_dir = (c_inv.to_3x3() @ ray_dir_world).normalized()
            hit, h_loc, h_no, _f_idx = cand_ob.ray_cast(r_org, r_dir)
            if hit:
                w_loc = cand_ob.matrix_world @ h_loc
                dist_sq = (w_loc - ray_origin_world).length_squared
                if dist_sq < best_dist_sq:
                    best_dist_sq = dist_sq
                    best_world_loc = w_loc
                    best_world_no = (c_inv.to_3x3().transposed() @ h_no).normalized()

        if best_world_loc is None or best_world_no is None:
            return False

        ob_inv = ob.matrix_world.inverted_safe()
        hit_loc = ob_inv @ best_world_loc
        hit_no = (ob_inv.to_3x3() @ best_world_no).normalized()

        view_inv = rv3d.view_matrix.inverted_safe()
        up_hint_obj = (ob_inv.to_3x3() @ Vector(view_inv.col[1][:3])).normalized()
        base_euler = _rotation_from_normal_and_up(hit_no, up_hint_obj)
        if self._spin_angle != 0.0:
            rot_mat = base_euler.to_matrix() @ Matrix.Rotation(self._spin_angle, 3, 'Z')
            base_euler = rot_mat.to_euler('XYZ')

        layer.proj_location = hit_loc
        layer.proj_rotation = base_euler
        _notify_gizmo_projection_changed(node, layer, context)
        return True

    def invoke(self, context, event):
        ob, _node, layer = _active_projection_layer_from_context(context)
        if ob is None or layer is None:
            return {'CANCELLED'}

        self._init_loc = Vector(layer.proj_location)
        self._init_rot = Euler(layer.proj_rotation, 'XYZ')
        self._init_scale = Vector(layer.proj_scale)
        self._spin_angle = 0.0
        self._last_mouse_x = event.mouse_region_x
        self._dragging = False
        layer.show_projection_widget = True

        context.window.cursor_modal_set('CROSSHAIR')
        context.area.header_text_set(
            "Place on Surface: LMB click/drag on mesh | Wheel: Scale | Shift+Move: Spin | Enter/RMB: Confirm | Esc: Cancel"
        )
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        _ob, node, layer = _active_projection_layer_from_context(context)
        if layer is None:
            self._finish(context)
            return {'CANCELLED'}

        if event.type in {'MIDDLEMOUSE'} or (event.alt and event.type in {'LEFTMOUSE', 'MIDDLEMOUSE', 'RIGHTMOUSE'}):
            return {'PASS_THROUGH'}

        if event.type in {'WHEELUPMOUSE', 'NUMPAD_PLUS'}:
            fac = 1.1
            layer.proj_scale = [max(0.005, s * fac) for s in layer.proj_scale]
            _notify_gizmo_projection_changed(node, layer, context)
            _tag_view3d_gizmo_refresh(context)
            return {'RUNNING_MODAL'}

        if event.type in {'WHEELDOWNMOUSE', 'NUMPAD_MINUS'}:
            fac = 1.0 / 1.1
            layer.proj_scale = [max(0.005, s * fac) for s in layer.proj_scale]
            _notify_gizmo_projection_changed(node, layer, context)
            _tag_view3d_gizmo_refresh(context)
            return {'RUNNING_MODAL'}

        if event.type == 'LEFTMOUSE':
            if event.value == 'PRESS':
                self._dragging = True
                self._raycast_surface(context, event)
                _tag_view3d_gizmo_refresh(context)
                return {'RUNNING_MODAL'}
            elif event.value == 'RELEASE':
                self._dragging = False
                self._finish(context)
                return {'FINISHED'}

        if event.type == 'MOUSEMOVE':
            if event.shift:
                dx = event.mouse_region_x - self._last_mouse_x
                self._spin_angle -= dx * 0.015
                rot_mat = Euler(layer.proj_rotation, 'XYZ').to_matrix() @ Matrix.Rotation(-dx * 0.015, 3, 'Z')
                layer.proj_rotation = rot_mat.to_euler('XYZ')
                _notify_gizmo_projection_changed(node, layer, context)
                _tag_view3d_gizmo_refresh(context)
            elif self._dragging:
                self._raycast_surface(context, event)
                _tag_view3d_gizmo_refresh(context)
            self._last_mouse_x = event.mouse_region_x
            return {'RUNNING_MODAL'}

        if event.type in {'RET', 'NUMPAD_ENTER', 'SPACE', 'RIGHTMOUSE'}:
            self._finish(context)
            return {'FINISHED'}

        if event.type == 'ESC':
            layer.proj_location = self._init_loc
            layer.proj_rotation = self._init_rot
            layer.proj_scale = self._init_scale
            _notify_gizmo_projection_changed(node, layer, context)
            self._finish(context)
            return {'CANCELLED'}

        return {'RUNNING_MODAL'}

    def _finish(self, context):
        context.window.cursor_modal_restore()
        if context.area:
            context.area.header_text_set(None)
        _tag_view3d_gizmo_refresh(context)


class NODE_OT_tex_paint_layer_bake_projection(Operator):
    """Freeze the projected pixels into a standard hand-painted layer and hide the 3D projection widget"""
    bl_idname = "node.tex_paint_layer_bake_projection"
    bl_label = "Bake Projection to Paint Layer"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        node = tex_paint_node_from_context(context)
        layer = node.active_layer if node else None
        if layer is None:
            return False
        if layer.type == 'PAINT' and getattr(layer, "proj_mode", 'NONE') != 'NONE':
            return True
        return _is_layer_grunge_gizmo_eligible(node, layer)

    def execute(self, context):
        node = tex_paint_node_from_context(context)
        layer = node.active_layer if node else None
        if layer is None:
            return {'CANCELLED'}
        layer.show_projection_widget = False
        if layer.type == 'PAINT':
            layer.proj_mode = 'NONE'
        _tag_view3d_gizmo_refresh(context)
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# 3D Viewport Interactive GizmoGroup & Wireframe Box Overlay
# ---------------------------------------------------------------------------

class VIEW3D_GGT_tex_paint_projection(GizmoGroup):
    """Substance-Painter-style 3D Projection Widget for Decal, Triplanar, and Grunge Mask Layers."""
    bl_idname = "VIEW3D_GGT_tex_paint_projection"
    bl_label = "Texture Paint 3D Projection Widget"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'WINDOW'
    bl_options = {'3D', 'PERSISTENT', 'SHOW_MODAL_ALL'}

    @classmethod
    def poll(cls, context):
        if context is None or context.space_data is None or context.space_data.type != 'VIEW_3D':
            return False
        if getattr(context, "mode", "") != 'PAINT_TEXTURE':
            return False
        _ob, _node, layer = _active_projection_layer_from_context(context)
        if layer is None:
            return False
        return bool(getattr(layer, "show_projection_widget", False))

    def _get_world_state(self):
        context = bpy.context
        ob, node, layer = _active_projection_layer_from_context(context)
        if ob is None or layer is None:
            return None
        ob_mat = ob.matrix_world
        ob_3x3 = ob_mat.to_3x3()
        loc_obj = Vector(layer.proj_location)
        rot_obj = Euler(layer.proj_rotation, 'XYZ').to_matrix()
        scale_obj = Vector(layer.proj_scale)
        world_pos = ob_mat @ loc_obj

        axes_obj = [rot_obj.col[i].normalized() for i in range(3)]
        axes_world = []
        axis_scales = []
        for i in range(3):
            v_w = ob_3x3 @ axes_obj[i]
            length = max(v_w.length, 1e-6)
            axes_world.append(v_w / length)
            axis_scales.append(length)

        return ob, node, layer, loc_obj, rot_obj, scale_obj, world_pos, axes_obj, axes_world, axis_scales

    def _make_move_get(self, axis):
        def get_fn():
            state = self._get_world_state()
            if state is None:
                return 0.0
            _ob, _node, _layer, _loc_obj, _rot_obj, _scale_obj, world_pos, _axes_obj, axes_world, _axis_scales = state
            return float(world_pos.dot(axes_world[axis]))
        return get_fn

    def _make_move_set(self, axis):
        def set_fn(value):
            state = self._get_world_state()
            if state is None:
                return
            ob, node, layer, _loc_obj, _rot_obj, _scale_obj, world_pos, _axes_obj, axes_world, _axis_scales = state
            axis_w = axes_world[axis]
            cur_proj = float(world_pos.dot(axis_w))
            new_world_pos = world_pos + axis_w * (float(value) - cur_proj)
            layer.proj_location = ob.matrix_world.inverted_safe() @ new_world_pos
            _notify_gizmo_projection_changed(node, layer)
        return set_fn

    def _make_scale_get(self, axis):
        def get_fn():
            state = self._get_world_state()
            if state is None:
                return 0.5
            _ob, _node, _layer, _loc_obj, _rot_obj, scale_obj, _world_pos, _axes_obj, _axes_world, axis_scales = state
            return float(scale_obj[axis] * axis_scales[axis])
        return get_fn

    def _make_scale_set(self, axis):
        def set_fn(value):
            state = self._get_world_state()
            if state is None:
                return
            _ob, node, layer, _loc_obj, _rot_obj, _scale_obj, _world_pos, _axes_obj, _axes_world, axis_scales = state
            new_s = max(0.005, abs(float(value)) / max(axis_scales[axis], 1e-6))
            s_vec = list(layer.proj_scale)
            s_vec[axis] = new_s
            layer.proj_scale = s_vec
            _notify_gizmo_projection_changed(node, layer)
        return set_fn

    def _uni_scale_get(self):
        state = self._get_world_state()
        if state is None:
            return 0.5
        _ob, _node, _layer, _loc_obj, _rot_obj, scale_obj, _world_pos, _axes_obj, _axes_world, axis_scales = state
        return float((scale_obj[0] * axis_scales[0] + scale_obj[1] * axis_scales[1]) * 0.5)

    def _uni_scale_set(self, value):
        state = self._get_world_state()
        if state is None:
            return
        _ob, node, layer, _loc_obj, _rot_obj, scale_obj, _world_pos, _axes_obj, _axes_world, axis_scales = state
        cur_avg = max((scale_obj[0] * axis_scales[0] + scale_obj[1] * axis_scales[1]) * 0.5, 1e-5)
        ratio = max(0.005, float(value)) / cur_avg
        layer.proj_scale = [max(0.005, s * ratio) for s in scale_obj]
        _notify_gizmo_projection_changed(node, layer)

    def _make_rot_get(self, axis):
        def get_fn():
            return float(self._rot_modal_angle[axis])
        return get_fn

    def _make_rot_set(self, axis):
        def set_fn(value):
            state = self._get_world_state()
            if state is None:
                return
            _ob, node, layer, _loc_obj, _rot_obj, _scale_obj, _world_pos, _axes_obj, _axes_world, _axis_scales = state
            self._rot_modal_angle[axis] = float(value)
            axis_char = ('X', 'Y', 'Z')[axis]
            rot_new = self._rot_start_mat @ Matrix.Rotation(-float(value), 3, axis_char)
            layer.proj_rotation = rot_new.to_euler('XYZ', Euler(layer.proj_rotation, 'XYZ'))
            _notify_gizmo_projection_changed(node, layer)
        return set_fn

    def setup(self, context):
        self.gizmos.clear()
        self._rot_start_mat = Matrix.Identity(3)
        self._rot_modal_angle = [0.0, 0.0, 0.0]

        axis_colors = (
            ((1.0, 0.25, 0.25), (1.0, 0.55, 0.55)),
            ((0.25, 0.95, 0.25), (0.55, 1.0, 0.55)),
            ((0.25, 0.55, 1.0), (0.55, 0.78, 1.0)),
        )

        gz_free = self.gizmos.new("GIZMO_GT_move_3d")
        gz_free.draw_style = 'RING_2D'
        gz_free.draw_options = {'FILL_SELECT', 'ALIGN_VIEW'}
        gz_free.color = (1.0, 0.95, 0.4)
        gz_free.alpha = 0.9
        gz_free.color_highlight = (1.0, 1.0, 0.8)
        gz_free.alpha_highlight = 1.0
        gz_free.scale_basis = 0.18
        gz_free.line_width = 2.2
        gz_free.use_draw_modal = True
        self.gz_free_move = gz_free

        self.move_arrows = []
        for i in range(3):
            gz = self.gizmos.new("GIZMO_GT_arrow_3d")
            gz.draw_style = 'NORMAL'
            gz.length = 0.85
            gz.color, gz.color_highlight = axis_colors[i]
            gz.alpha = 0.92
            gz.alpha_highlight = 1.0
            gz.scale_basis = 1.0
            gz.line_width = 2.5
            gz.use_draw_modal = True
            gz.target_set_handler(
                "offset",
                get=self._make_move_get(i),
                set=self._make_move_set(i),
            )
            self.move_arrows.append(gz)

        self.scale_boxes = []
        for i in range(3):
            gz = self.gizmos.new("GIZMO_GT_arrow_3d")
            gz.draw_style = 'BOX'
            gz.length = 0.15
            gz.color, gz.color_highlight = axis_colors[i]
            gz.alpha = 0.92
            gz.alpha_highlight = 1.0
            gz.scale_basis = 1.0
            gz.line_width = 2.2
            gz.use_draw_modal = True
            gz.target_set_handler(
                "offset",
                get=self._make_scale_get(i),
                set=self._make_scale_set(i),
            )
            self.scale_boxes.append(gz)

        gz_uni = self.gizmos.new("GIZMO_GT_arrow_3d")
        gz_uni.draw_style = 'BOX'
        gz_uni.length = 0.0
        gz_uni.color = (0.95, 0.95, 0.95)
        gz_uni.alpha = 0.9
        gz_uni.color_highlight = (1.0, 1.0, 0.5)
        gz_uni.alpha_highlight = 1.0
        gz_uni.scale_basis = 1.25
        gz_uni.use_draw_modal = True
        gz_uni.target_set_handler(
            "offset",
            get=self._uni_scale_get,
            set=self._uni_scale_set,
        )
        self.gz_uni_scale = gz_uni

        self.rot_dials = []
        for i in range(3):
            gz = self.gizmos.new("GIZMO_GT_dial_3d")
            gz.draw_options = {'CLIP'}
            gz.color, gz.color_highlight = axis_colors[i]
            gz.alpha = 0.88
            gz.alpha_highlight = 1.0
            gz.scale_basis = 0.95 if i == 2 else 0.82
            gz.line_width = 2.8 if i == 2 else 2.0
            gz.use_draw_modal = True
            gz.use_draw_value = True
            gz.incremental_angle = math.radians(15.0)
            gz.target_set_handler(
                "offset",
                get=self._make_rot_get(i),
                set=self._make_rot_set(i),
            )
            self.rot_dials.append(gz)

        self.refresh(context)

    def refresh(self, context):
        state = self._get_world_state()
        if state is None:
            return
        ob, _node, layer, _loc_obj, rot_obj, scale_obj, world_pos, _axes_obj, axes_world, axis_scales = state

        wm = getattr(context, "window_manager", None)
        gizmo_mode = getattr(wm, "tex_paint_gizmo_mode", 'ALL') if wm else 'ALL'
        show_move = gizmo_mode in {'TRANSLATE', 'ALL'}
        show_rot = gizmo_mode in {'ROTATE', 'ALL'}
        show_scale = gizmo_mode in {'SCALE', 'ALL'}

        self.gz_free_move.hide = not show_move
        if not self.gz_free_move.is_modal:
            self.gz_free_move.matrix_space = ob.matrix_world
            self.gz_free_move.matrix_basis = Matrix.Identity(4)
            self.gz_free_move.target_set_prop("offset", layer, "proj_location")

        for i, gz in enumerate(self.move_arrows):
            gz.hide = not show_move
            axis_w = axes_world[i]
            perp_origin = world_pos - axis_w * float(world_pos.dot(axis_w))
            gz.matrix_space = Matrix.Identity(4)
            gz.matrix_basis = _axis_basis_matrix(axis_w, perp_origin)

        for i, gz in enumerate(self.scale_boxes):
            gz.hide = not show_scale
            gz.length = 0.35 if gizmo_mode == 'ALL' else 0.15
            gz.matrix_space = Matrix.Identity(4)
            gz.matrix_basis = _axis_basis_matrix(axes_world[i], world_pos)

        self.gz_uni_scale.hide = (gizmo_mode != 'SCALE')
        if not self.gz_uni_scale.is_modal:
            rv3d = getattr(context, "region_data", None)
            if rv3d is not None:
                view_inv = rv3d.view_matrix.inverted_safe()
                uni_dir = (Vector(view_inv.col[0][:3]) + Vector(view_inv.col[1][:3])).normalized()
            else:
                uni_dir = (axes_world[0] + axes_world[1]).normalized()
            avg_s = float((scale_obj[0] * axis_scales[0] + scale_obj[1] * axis_scales[1]) * 0.5)
            uni_perp = world_pos - uni_dir * avg_s
            self.gz_uni_scale.matrix_space = Matrix.Identity(4)
            self.gz_uni_scale.matrix_basis = _axis_basis_matrix(uni_dir, uni_perp)

        any_rot_modal = any(gz.is_modal for gz in self.rot_dials)
        if not any_rot_modal:
            self._rot_start_mat = rot_obj.copy()
            self._rot_modal_angle = [0.0, 0.0, 0.0]
            for i, gz in enumerate(self.rot_dials):
                gz.hide = not show_rot
                gz.matrix_space = Matrix.Identity(4)
                gz.matrix_basis = _axis_basis_matrix(axes_world[i], world_pos)
        else:
            for gz in self.rot_dials:
                gz.hide = not show_rot


_draw_handler_handle = None


def _draw_projection_wireframe_3d():
    if bpy.app.background:
        return
    context = bpy.context
    if not VIEW3D_GGT_tex_paint_projection.poll(context):
        return
    ob, node, layer = _active_projection_layer_from_context(context)
    if ob is None or layer is None:
        return

    import gpu
    from gpu_extras.batch import batch_for_shader

    loc = Vector(layer.proj_location)
    rot = Euler(layer.proj_rotation, 'XYZ').to_matrix().to_4x4()
    sx = max(layer.proj_scale[0], 0.001)
    sy = max(layer.proj_scale[1], 0.001)
    sz = max(layer.proj_scale[2], 0.001)
    scale_mat = Matrix.Diagonal((sx, sy, sz, 1.0))
    widget_world_mat = ob.matrix_world @ Matrix.Translation(loc) @ rot @ scale_mat

    def xform(pt):
        v = widget_world_mat @ Vector((pt[0], pt[1], pt[2], 1.0))
        return (v.x, v.y, v.z)

    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('LESS_EQUAL')

    eff_mode = _effective_proj_mode(node, layer)
    if eff_mode in {'DECAL', 'WARP'}:
        is_warp = (eff_mode == 'WARP')
        main_col = (0.78, 0.42, 1.0, 0.95) if is_warp else (0.2, 0.92, 1.0, 0.95)
        sub_col = (0.78, 0.42, 1.0, 0.35) if is_warp else (0.2, 0.92, 1.0, 0.32)
        frame_lines = [
            xform((-1, -1, 0)), xform((1, -1, 0)),
            xform((1, -1, 0)), xform((1, 1, 0)),
            xform((1, 1, 0)), xform((-1, 1, 0)),
            xform((-1, 1, 0)), xform((-1, -1, 0)),
            xform((-0.85, -0.7, 0)), xform((-0.85, -0.85, 0)),
            xform((-0.85, -0.85, 0)), xform((-0.7, -0.85, 0)),
            xform((0.85, -0.7, 0)), xform((0.85, -0.85, 0)),
            xform((0.85, -0.85, 0)), xform((0.7, -0.85, 0)),
            xform((0.85, 0.7, 0)), xform((0.85, 0.85, 0)),
            xform((0.85, 0.85, 0)), xform((0.7, 0.85, 0)),
            xform((-0.85, 0.7, 0)), xform((-0.85, 0.85, 0)),
            xform((-0.85, 0.85, 0)), xform((-0.7, 0.85, 0)),
            xform((-0.18, 1.0, 0)), xform((0.0, 1.22, 0)),
            xform((0.0, 1.22, 0)), xform((0.18, 1.0, 0)),
            xform((0, 0, 0.5)), xform((0, 0, -0.65)),
            xform((0, 0, -0.65)), xform((-0.12, 0, -0.45)),
            xform((0, 0, -0.65)), xform((0.12, 0, -0.45)),
            xform((0, 0, -0.65)), xform((0, -0.12, -0.45)),
            xform((0, 0, -0.65)), xform((0, 0.12, -0.45)),
        ]
        if is_warp:
            # Add curved warp grid lines indicating surface-hugging warp projection
            for t_i in range(-3, 4):
                u = t_i / 3.0
                z_curve = -0.28 * (u * u)
                frame_lines.extend([xform((u, -1.0, z_curve)), xform((u, 1.0, z_curve))])
                frame_lines.extend([xform((-1.0, u, z_curve)), xform((1.0, u, z_curve))])
        gpu.state.line_width_set(2.2)
        shader.bind()
        shader.uniform_float("color", main_col)
        batch_for_shader(shader, 'LINES', {"pos": frame_lines}).draw(shader)

        box_corners = [
            (-1, -1, -1), (1, -1, -1), (1, 1, -1), (-1, 1, -1),
            (-1, -1, 1), (1, -1, 1), (1, 1, 1), (-1, 1, 1),
        ]
        box_edges = [
            (0, 1), (1, 2), (2, 3), (3, 0),
            (4, 5), (5, 6), (6, 7), (7, 4),
            (0, 4), (1, 5), (2, 6), (3, 7),
        ]
        depth_lines = []
        for a, b in box_edges:
            depth_lines.append(xform(box_corners[a]))
            depth_lines.append(xform(box_corners[b]))
        gpu.state.line_width_set(1.0)
        shader.uniform_float("color", sub_col)
        batch_for_shader(shader, 'LINES', {"pos": depth_lines}).draw(shader)

    elif eff_mode in {'TRIPLANAR', 'SPHERICAL', 'CYLINDRICAL'}:
        box_corners = [
            (-1, -1, -1), (1, -1, -1), (1, 1, -1), (-1, 1, -1),
            (-1, -1, 1), (1, -1, 1), (1, 1, 1), (-1, 1, 1),
        ]
        box_edges = [
            (0, 1), (1, 2), (2, 3), (3, 0),
            (4, 5), (5, 6), (6, 7), (7, 4),
            (0, 4), (1, 5), (2, 6), (3, 7),
        ]
        cube_lines = []
        for a, b in box_edges:
            cube_lines.append(xform(box_corners[a]))
            cube_lines.append(xform(box_corners[b]))
        cube_lines.extend([
            xform((-0.25, 0, 1)), xform((0.25, 0, 1)),
            xform((0, -0.25, 1)), xform((0, 0.25, 1)),
            xform((1, -0.25, 0)), xform((1, 0.25, 0)),
            xform((1, 0, -0.25)), xform((1, 0, 0.25)),
            xform((-0.25, 1, 0)), xform((0.25, 1, 0)),
            xform((0, 1, -0.25)), xform((0, 1, 0.25)),
        ])
        gpu.state.line_width_set(2.0)
        shader.bind()
        shader.uniform_float("color", (1.0, 0.78, 0.22, 0.9))
        batch_for_shader(shader, 'LINES', {"pos": cube_lines}).draw(shader)

    # Draw active modal transform axis constraint lines & guide line (G / R / S)
    if _MODAL_TRANSFORM_STATE.get("active"):
        origin_w = _MODAL_TRANSFORM_STATE.get("world_origin")
        axes_w = _MODAL_TRANSFORM_STATE.get("axes_world")
        axis_mode = _MODAL_TRANSFORM_STATE.get("axis")
        cur_w = _MODAL_TRANSFORM_STATE.get("world_current")
        if origin_w is not None and axes_w and len(axes_w) == 3:
            axis_colors_rgba = (
                (1.0, 0.28, 0.28, 0.95),
                (0.28, 0.95, 0.28, 0.95),
                (0.28, 0.58, 1.0, 0.95),
            )
            active_indices = {
                'X': (0,), 'Y': (1,), 'Z': (2,),
                'YZ': (1, 2), 'XZ': (0, 2), 'XY': (0, 1),
            }.get(axis_mode, ())
            gpu.state.depth_test_set('NONE')
            gpu.state.line_width_set(2.4)
            shader.bind()
            for ax_i in active_indices:
                d = axes_w[ax_i] * 50.0
                p0 = origin_w - d
                p1 = origin_w + d
                shader.uniform_float("color", axis_colors_rgba[ax_i])
                batch_for_shader(shader, 'LINES', {"pos": [(p0.x, p0.y, p0.z), (p1.x, p1.y, p1.z)]}).draw(shader)
            if cur_w is not None and (cur_w - origin_w).length > 1e-4:
                gpu.state.line_width_set(1.8)
                shader.uniform_float("color", (1.0, 1.0, 1.0, 0.85))
                batch_for_shader(
                    shader, 'LINES',
                    {"pos": [(origin_w.x, origin_w.y, origin_w.z), (cur_w.x, cur_w.y, cur_w.z)]},
                ).draw(shader)

    gpu.state.line_width_set(1.0)
    gpu.state.depth_test_set('NONE')
    gpu.state.blend_set('NONE')


# ---------------------------------------------------------------------------
# 2D Image Editor Interactive UV Projection Gizmo & Mesh UV Overlay
# ---------------------------------------------------------------------------

_draw_handler_image_handle = None
_MESH_UV_WIRE_CACHE = {}  # (mesh_ptr, n_loops, n_polys, uv_name) -> ndarray(N, 2) float32


def _get_cached_mesh_uv_wire_segments(ob):
    """Extract and cache 2D UV wireframe line segment endpoints for `ob` in [0, 1] UV space."""
    import numpy as np
    if ob is None or ob.type != 'MESH' or ob.data is None:
        return None
    mesh = ob.data
    uv_layer = mesh.uv_layers.active
    if uv_layer is None or len(mesh.loops) == 0 or len(mesh.polygons) == 0:
        return None
    m_key = (mesh.as_pointer(), len(mesh.loops), len(mesh.polygons), uv_layer.name)
    cached = _MESH_UV_WIRE_CACHE.get(m_key)
    if cached is not None:
        return cached

    n_loops = len(mesh.loops)
    uvs = np.empty(n_loops * 2, dtype=np.float32)
    uv_layer.data.foreach_get("uv", uvs)
    uvs = uvs.reshape(-1, 2)

    # Build loop -> next_loop_in_poly index mapping vectorized
    loop_starts = np.empty(len(mesh.polygons), dtype=np.int32)
    loop_totals = np.empty(len(mesh.polygons), dtype=np.int32)
    mesh.polygons.foreach_get("loop_start", loop_starts)
    mesh.polygons.foreach_get("loop_total", loop_totals)

    idx_a = np.arange(n_loops, dtype=np.int32)
    idx_b = idx_a + 1
    poly_ends = loop_starts + loop_totals - 1
    idx_b[poly_ends] = loop_starts

    seg_pts = np.empty((n_loops * 2, 2), dtype=np.float32)
    seg_pts[0::2] = uvs[idx_a]
    seg_pts[1::2] = uvs[idx_b]

    if len(_MESH_UV_WIRE_CACHE) > 8:
        _MESH_UV_WIRE_CACHE.clear()
    _MESH_UV_WIRE_CACHE[m_key] = seg_pts
    return seg_pts


def _compute_uv_gizmo_handles_px(region, center_u, center_v, half_u, half_v, rot_rad):
    """Compute screen-pixel coordinates of the 2D UV Projection frame and interactive handles in `region`."""
    v2d = region.view2d
    x0, y0 = v2d.view_to_region(0.0, 0.0, clip=False)
    x1, y1 = v2d.view_to_region(1.0, 1.0, clip=False)
    sx_px = float(x1 - x0)
    sy_px = float(y1 - y0)

    cos_r = math.cos(rot_rad)
    sin_r = math.sin(rot_rad)

    def local_to_px(lu, lv):
        du = lu * half_u
        dv = lv * half_v
        u = center_u + du * cos_r - dv * sin_r
        v = center_v + du * sin_r + dv * cos_r
        return Vector((x0 + u * sx_px, y0 + v * sy_px))

    c_px = local_to_px(0.0, 0.0)
    bl_px = local_to_px(-1.0, -1.0)
    br_px = local_to_px(1.0, -1.0)
    tr_px = local_to_px(1.0, 1.0)
    tl_px = local_to_px(-1.0, 1.0)
    el_px = local_to_px(-1.0, 0.0)
    er_px = local_to_px(1.0, 0.0)
    eb_px = local_to_px(0.0, -1.0)
    et_px = local_to_px(0.0, 1.0)

    up_dir = et_px - c_px
    if up_dir.length > 1e-4:
        up_dir_norm = up_dir.normalized()
    else:
        up_dir_norm = Vector((-sin_r, cos_r))
    rot_px = et_px + up_dir_norm * 28.0

    return {
        "x0": x0,
        "y0": y0,
        "sx_px": sx_px,
        "sy_px": sy_px,
        "local_to_px": local_to_px,
        "CENTER": c_px,
        "CORNER_BL": bl_px,
        "CORNER_BR": br_px,
        "CORNER_TR": tr_px,
        "CORNER_TL": tl_px,
        "EDGE_L": el_px,
        "EDGE_R": er_px,
        "EDGE_B": eb_px,
        "EDGE_T": et_px,
        "ROTATE": rot_px,
        "UP_DIR": up_dir_norm,
    }


_2D_SELECTION_STATE = {}  # image_name -> {"mask": ndarray(H,W,float32), "contour_uv": ndarray(N,2,float32), "pct": float, "bbox": (u0,v0,u1,v1)}
_2D_MODAL_DRAG_PREVIEW = {"active": False, "kind": "", "pts_uv": []}


def _draw_2d_selection_and_tool_overlay(context, region):
    """Draw the Krita-style 2D Selection Contour (marching-ants outline) and active modal tool drag wire in the Image Editor."""
    space = getattr(context, "space_data", None)
    ima = getattr(space, "image", None) if space else None
    if ima is None:
        return
    sel_info = _2D_SELECTION_STATE.get(ima.name)
    has_sel = sel_info is not None and sel_info.get("contour_uv") is not None and len(sel_info["contour_uv"]) > 0
    has_drag = bool(_2D_MODAL_DRAG_PREVIEW.get("active") and _2D_MODAL_DRAG_PREVIEW.get("pts_uv"))
    if not has_sel and not has_drag:
        return

    import gpu
    import numpy as np
    from gpu_extras.batch import batch_for_shader

    v2d = region.view2d
    x0, y0 = v2d.view_to_region(0.0, 0.0, clip=False)
    x1, y1 = v2d.view_to_region(1.0, 1.0, clip=False)
    sx_px = float(x1 - x0)
    sy_px = float(y1 - y0)

    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    gpu.state.blend_set('ALPHA')

    if has_sel:
        c_uv = sel_info["contour_uv"]
        px_pts = np.empty_like(c_uv)
        px_pts[:, 0] = x0 + c_uv[:, 0] * sx_px
        px_pts[:, 1] = y0 + c_uv[:, 1] * sy_px

        # Dark backdrop pass + bright cyan/white Krita selection contour pass
        gpu.state.line_width_set(3.0)
        shader.bind()
        shader.uniform_float("color", (0.04, 0.08, 0.14, 0.78))
        batch_for_shader(shader, 'LINES', {"pos": px_pts}).draw(shader)

        gpu.state.line_width_set(1.6)
        shader.uniform_float("color", (0.22, 0.96, 1.0, 0.96))
        batch_for_shader(shader, 'LINES', {"pos": px_pts}).draw(shader)

        # Subtle selection bounding-box corner brackets
        bbox = sel_info.get("bbox")
        if bbox is not None:
            bu0, bv0, bu1, bv1 = bbox
            bx0, by0 = x0 + bu0 * sx_px, y0 + bv0 * sy_px
            bx1, by1 = x0 + bu1 * sx_px, y0 + bv1 * sy_px
            b_lines = [
                (bx0, by0), (bx1, by0),
                (bx1, by0), (bx1, by1),
                (bx1, by1), (bx0, by1),
                (bx0, by1), (bx0, by0),
            ]
            gpu.state.line_width_set(1.0)
            shader.uniform_float("color", (1.0, 0.88, 0.25, 0.45))
            batch_for_shader(shader, 'LINES', {"pos": b_lines}).draw(shader)

    if has_drag:
        pts_uv = _2D_MODAL_DRAG_PREVIEW["pts_uv"]
        if len(pts_uv) >= 2:
            d_pts = [(x0 + u * sx_px, y0 + v * sy_px) for (u, v) in pts_uv]
            gpu.state.line_width_set(2.2)
            shader.bind()
            shader.uniform_float("color", (1.0, 0.85, 0.20, 0.95))
            batch_for_shader(shader, 'LINES', {"pos": d_pts}).draw(shader)

    gpu.state.line_width_set(1.0)
    gpu.state.blend_set('NONE')


def _draw_uv_projection_gizmo_2d():
    """Draw the 3D Mesh UV layout overlay, Krita 2D Selection contour, and interactive 2D UV Projection Gizmo in the Image Editor."""
    if bpy.app.background:
        return
    context = bpy.context
    if context is None or context.space_data is None or context.space_data.type != 'IMAGE_EDITOR':
        return
    region = context.region
    if region is None or region.type != 'WINDOW':
        return

    _draw_2d_selection_and_tool_overlay(context, region)

    ob, node, layer, target_kind, eff = _active_uv_projection_from_context(context)
    if node is None or layer is None or not getattr(layer, "show_projection_widget", False):
        return

    import gpu
    import numpy as np
    from gpu_extras.batch import batch_for_shader

    cu, cv, hu, hv, rot_rad, is_repeat = _get_uv_gizmo_transform(node, layer, target_kind, eff)
    geom = _compute_uv_gizmo_handles_px(region, cu, cv, hu, hv, rot_rad)
    x0, y0, sx_px, sy_px = geom["x0"], geom["y0"], geom["sx_px"], geom["sy_px"]
    local_to_px = geom["local_to_px"]

    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    gpu.state.blend_set('ALPHA')

    # 1. Draw subtle [0, 1] UV tile border & active 3D Mesh UV Island Wireframe so the artist sees UVs clearly
    uv_segs = _get_cached_mesh_uv_wire_segments(ob)
    if uv_segs is not None and len(uv_segs) > 0:
        px_segs = np.empty_like(uv_segs)
        px_segs[:, 0] = x0 + uv_segs[:, 0] * sx_px
        px_segs[:, 1] = y0 + uv_segs[:, 1] * sy_px
        gpu.state.line_width_set(1.0)
        shader.bind()
        shader.uniform_float("color", (0.68, 0.86, 1.0, 0.24))
        batch_for_shader(shader, 'LINES', {"pos": px_segs}).draw(shader)

    # 2. If Repeat (Tile) is enabled, draw faint neighboring tile outlines (-1..+1)
    is_mask = (target_kind == 'MASK_FILL')
    main_col = (0.20, 0.92, 1.0, 0.95) if is_mask else (1.0, 0.78, 0.22, 0.95)
    sub_col = (0.20, 0.92, 1.0, 0.30) if is_mask else (1.0, 0.78, 0.22, 0.30)

    if is_repeat:
        tile_lines = []
        for tx in (-2.0, 0.0, 2.0):
            for ty in (-2.0, 0.0, 2.0):
                if tx == 0.0 and ty == 0.0:
                    continue
                p0 = local_to_px(tx - 1.0, ty - 1.0)
                p1 = local_to_px(tx + 1.0, ty - 1.0)
                p2 = local_to_px(tx + 1.0, ty + 1.0)
                p3 = local_to_px(tx - 1.0, ty + 1.0)
                tile_lines.extend([
                    (p0.x, p0.y), (p1.x, p1.y),
                    (p1.x, p1.y), (p2.x, p2.y),
                    (p2.x, p2.y), (p3.x, p3.y),
                    (p3.x, p3.y), (p0.x, p0.y),
                ])
        gpu.state.line_width_set(1.0)
        shader.bind()
        shader.uniform_float("color", (main_col[0], main_col[1], main_col[2], 0.16))
        batch_for_shader(shader, 'LINES', {"pos": tile_lines}).draw(shader)

    # 3. Draw internal rule-of-thirds alignment grid
    grid_lines = []
    for t in (-1.0 / 3.0, 1.0 / 3.0):
        pa, pb = local_to_px(t, -1.0), local_to_px(t, 1.0)
        pc, pd = local_to_px(-1.0, t), local_to_px(1.0, t)
        grid_lines.extend([(pa.x, pa.y), (pb.x, pb.y), (pc.x, pc.y), (pd.x, pd.y)])
    gpu.state.line_width_set(1.0)
    shader.bind()
    shader.uniform_float("color", sub_col)
    batch_for_shader(shader, 'LINES', {"pos": grid_lines}).draw(shader)

    # 4. Draw primary UV Projection Bounding Box + Corner Brackets + "UP" Orientation Triangle
    bl_px, br_px, tr_px, tl_px = geom["CORNER_BL"], geom["CORNER_BR"], geom["CORNER_TR"], geom["CORNER_TL"]
    et_px, rot_px, up_dir = geom["EDGE_T"], geom["ROTATE"], geom["UP_DIR"]
    right_dir = Vector((up_dir.y, -up_dir.x))

    frame_lines = [
        (bl_px.x, bl_px.y), (br_px.x, br_px.y),
        (br_px.x, br_px.y), (tr_px.x, tr_px.y),
        (tr_px.x, tr_px.y), (tl_px.x, tl_px.y),
        (tl_px.x, tl_px.y), (bl_px.x, bl_px.y),
    ]
    # Top orientation chevron
    tip = et_px + up_dir * 10.0
    ch_l = et_px - right_dir * 8.0
    ch_r = et_px + right_dir * 8.0
    frame_lines.extend([(ch_l.x, ch_l.y), (tip.x, tip.y), (tip.x, tip.y), (ch_r.x, ch_r.y)])

    gpu.state.line_width_set(2.2)
    shader.bind()
    shader.uniform_float("color", main_col)
    batch_for_shader(shader, 'LINES', {"pos": frame_lines}).draw(shader)

    wm = getattr(context, "window_manager", None)
    gizmo_mode = getattr(wm, "tex_paint_gizmo_mode", 'ALL') if wm else 'ALL'
    show_move = gizmo_mode in {'TRANSLATE', 'ALL'}
    show_rot = gizmo_mode in {'ROTATE', 'ALL'}
    show_scale = gizmo_mode in {'SCALE', 'ALL'}

    def _circle_pts(center_v, rad, segs=20):
        pts = []
        for i in range(segs):
            a0 = (2.0 * math.pi * i) / segs
            a1 = (2.0 * math.pi * (i + 1)) / segs
            pts.append((center_v.x + math.cos(a0) * rad, center_v.y + math.sin(a0) * rad))
            pts.append((center_v.x + math.cos(a1) * rad, center_v.y + math.sin(a1) * rad))
        return pts

    def _quad_tris(center_v, half_sz):
        x, y = center_v.x, center_v.y
        s = half_sz
        return [
            (x - s, y - s), (x + s, y - s), (x + s, y + s),
            (x - s, y - s), (x + s, y + s), (x - s, y + s),
        ]

    # 5. Rotation Stem & Knob above Top Edge
    if show_rot:
        gpu.state.line_width_set(2.0)
        shader.uniform_float("color", (0.35, 0.78, 1.0, 0.95))
        stem_and_ring = [(et_px.x, et_px.y), (rot_px.x, rot_px.y)] + _circle_pts(rot_px, 8.0, 20)
        batch_for_shader(shader, 'LINES', {"pos": stem_and_ring}).draw(shader)
        shader.uniform_float("color", (0.25, 0.65, 1.0, 0.85))
        batch_for_shader(shader, 'TRIS', {"pos": _quad_tris(rot_px, 4.0)}).draw(shader)

    # 6. Corner & Edge Scale Handles
    if show_scale:
        # 4 Corner Uniform Scale Squares
        corner_tris = []
        for cp in (bl_px, br_px, tr_px, tl_px):
            corner_tris.extend(_quad_tris(cp, 5.5))
        shader.uniform_float("color", (1.0, 1.0, 1.0, 0.95))
        batch_for_shader(shader, 'TRIS', {"pos": corner_tris}).draw(shader)

        # Left/Right X-Scale Handles (Red) & Top/Bottom Y-Scale Handles (Green)
        lr_tris = _quad_tris(geom["EDGE_L"], 4.8) + _quad_tris(geom["EDGE_R"], 4.8)
        shader.uniform_float("color", (1.0, 0.32, 0.32, 0.95))
        batch_for_shader(shader, 'TRIS', {"pos": lr_tris}).draw(shader)

        tb_tris = _quad_tris(geom["EDGE_B"], 4.8) + _quad_tris(geom["EDGE_T"], 4.8)
        shader.uniform_float("color", (0.32, 0.95, 0.38, 0.95))
        batch_for_shader(shader, 'TRIS', {"pos": tb_tris}).draw(shader)

    # 7. Center Move Ring & Crosshair Handle
    if show_move:
        c_px = geom["CENTER"]
        move_lines = _circle_pts(c_px, 11.0, 24) + [
            (c_px.x - 15.0, c_px.y), (c_px.x + 15.0, c_px.y),
            (c_px.x, c_px.y - 15.0), (c_px.x, c_px.y + 15.0),
        ]
        gpu.state.line_width_set(2.2)
        shader.uniform_float("color", (1.0, 0.92, 0.35, 0.98))
        batch_for_shader(shader, 'LINES', {"pos": move_lines}).draw(shader)

    gpu.state.line_width_set(1.0)
    gpu.state.blend_set('NONE')


class IMAGE_OT_tex_paint_uv_gizmo_transform(Operator):
    """Drag the 2D UV Projection Gizmo handles (Move, Rotate, Scale) directly on the UVs in the Image Editor"""
    bl_idname = "image.tex_paint_uv_gizmo_transform"
    bl_label = "Transform 2D UV Projection Gizmo"
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}

    mode: EnumProperty(
        name="Mode",
        items=(
            ('HIT_TEST', "Interactive Handle Drag", "Click and drag any 2D UV Gizmo handle (Center=Move, Corners=Scale, Edges=Scale X/Y, Top Knob=Rotate)"),
            ('TRANSLATE', "Move on UVs (G)", "Move the UV projection across the UV layout (Shortcut: G)"),
            ('ROTATE', "Rotate on UVs (R)", "Rotate the UV projection on the UV layout (Shortcut: R)"),
            ('SCALE', "Scale on UVs (S)", "Scale the UV projection on the UV layout (Shortcut: S)"),
            ('SCALE_X', "Scale Width (U)", "Stretch/squash horizontal U width on the UV layout"),
            ('SCALE_Y', "Scale Height (V)", "Stretch/squash vertical V height on the UV layout"),
            ('RESET_LOC', "Reset UV Offset", "Center the UV projection at (0.5, 0.5) (Alt+G)"),
            ('RESET_ROT', "Reset UV Rotation", "Reset UV rotation to 0° (Alt+R)"),
            ('RESET_SCALE', "Reset UV Scale", "Reset UV scale to 1.0 (Alt+S)"),
        ),
        default='HIT_TEST',
    )

    @classmethod
    def poll(cls, context):
        if context is None:
            return False
        if context.space_data is not None and context.space_data.type != 'IMAGE_EDITOR':
            return False
        _ob, _node, layer, _tk, _eff = _active_uv_projection_from_context(context)
        return layer is not None

    def execute(self, context):
        _ob, node, layer, target_kind, eff = _active_uv_projection_from_context(context)
        if node is None or layer is None:
            return {'CANCELLED'}
        if self.mode in {'RESET_LOC', 'RESET_ROT', 'RESET_SCALE'}:
            cu, cv, hu, hv, rot_r, _rep = _get_uv_gizmo_transform(node, layer, target_kind, eff)
            if self.mode == 'RESET_LOC':
                cu, cv = 0.5, 0.5
            elif self.mode == 'RESET_ROT':
                rot_r = 0.0
            elif self.mode == 'RESET_SCALE':
                hu, hv = 0.5, 0.5
            _set_uv_gizmo_transform(context, node, layer, target_kind, eff, cu, cv, hu, hv, rot_r, interactive=False)
            _tag_view3d_gizmo_refresh(context)
            return {'FINISHED'}
        return {'CANCELLED'}

    def _hit_test_handle(self, context, event, node, layer, target_kind, eff):
        region = context.region
        if region is None or region.type != 'WINDOW':
            return None
        cu, cv, hu, hv, rot_rad, _rep = _get_uv_gizmo_transform(node, layer, target_kind, eff)
        geom = _compute_uv_gizmo_handles_px(region, cu, cv, hu, hv, rot_rad)
        m_pt = Vector((float(event.mouse_region_x), float(event.mouse_region_y)))

        wm = getattr(context, "window_manager", None)
        gmode = getattr(wm, "tex_paint_gizmo_mode", 'ALL') if wm else 'ALL'
        show_move = gmode in {'TRANSLATE', 'ALL'}
        show_rot = gmode in {'ROTATE', 'ALL'}
        show_scale = gmode in {'SCALE', 'ALL'}

        if show_rot and (m_pt - geom["ROTATE"]).length <= 15.0:
            return 'ROTATE'
        if show_scale:
            for ck in ("CORNER_BL", "CORNER_BR", "CORNER_TR", "CORNER_TL"):
                if (m_pt - geom[ck]).length <= 13.0:
                    return 'SCALE'
            for ek in ("EDGE_L", "EDGE_R"):
                if (m_pt - geom[ek]).length <= 13.0:
                    return 'SCALE_X'
            for ek in ("EDGE_B", "EDGE_T"):
                if (m_pt - geom[ek]).length <= 13.0:
                    return 'SCALE_Y'
        if show_move and (m_pt - geom["CENTER"]).length <= 19.0:
            return 'TRANSLATE'
        # If in dedicated Move (G) gizmo mode, clicking anywhere inside the UV frame drags it
        if gmode == 'TRANSLATE':
            u_click, v_click = region.view2d.region_to_view(m_pt.x, m_pt.y)
            cos_r = math.cos(rot_rad)
            sin_r = math.sin(rot_rad)
            du = u_click - cu
            dv = v_click - cv
            lu = du * cos_r + dv * sin_r
            lv = -du * sin_r + dv * cos_r
            if abs(lu) <= hu and abs(lv) <= hv:
                return 'TRANSLATE'
        return None

    def invoke(self, context, event):
        _ob, node, layer, target_kind, eff = _active_uv_projection_from_context(context)
        if node is None or layer is None or context.region is None or context.region.type != 'WINDOW':
            return {'PASS_THROUGH'}

        if self.mode == 'HIT_TEST':
            if not getattr(layer, "show_projection_widget", False):
                return {'PASS_THROUGH'}
            hit_mode = self._hit_test_handle(context, event, node, layer, target_kind, eff)
            if hit_mode is None:
                return {'PASS_THROUGH'}
            self._active_mode = hit_mode
            self._drag_release_confirm = True
        elif self.mode in {'RESET_LOC', 'RESET_ROT', 'RESET_SCALE'}:
            cu, cv, hu, hv, rot_r, _rep = _get_uv_gizmo_transform(node, layer, target_kind, eff)
            if self.mode == 'RESET_LOC':
                cu, cv = 0.5, 0.5
            elif self.mode == 'RESET_ROT':
                rot_r = 0.0
            elif self.mode == 'RESET_SCALE':
                hu, hv = 0.5, 0.5
            _set_uv_gizmo_transform(context, node, layer, target_kind, eff, cu, cv, hu, hv, rot_r, interactive=False)
            _tag_view3d_gizmo_refresh(context)
            return {'FINISHED'}
        else:
            if not getattr(layer, "show_projection_widget", False):
                layer.show_projection_widget = True
            self._active_mode = self.mode
            self._drag_release_confirm = False

        self._init_state = _get_uv_gizmo_transform(node, layer, target_kind, eff)
        cu, cv, hu, hv, rot_r, _rep = self._init_state
        geom = _compute_uv_gizmo_handles_px(context.region, cu, cv, hu, hv, rot_r)
        self._center_px = geom["CENTER"]
        self._start_mouse = Vector((float(event.mouse_region_x), float(event.mouse_region_y)))
        self._start_uv = Vector(context.region.view2d.region_to_view(self._start_mouse.x, self._start_mouse.y))
        self._start_dist = max((self._start_mouse - self._center_px).length, 15.0)
        v0 = self._start_mouse - self._center_px
        self._start_angle = math.atan2(v0.y, v0.x) if v0.length > 1e-3 else 0.0
        self._axis = None

        context.window.cursor_modal_set('SCROLL_XY')
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def _apply(self, context, event, node, layer, target_kind, eff):
        cu0, cv0, hu0, hv0, rot0, _rep = self._init_state
        cur_mouse = Vector((float(event.mouse_region_x), float(event.mouse_region_y)))
        cur_uv = Vector(context.region.view2d.region_to_view(cur_mouse.x, cur_mouse.y))
        cu, cv, hu, hv, rot_r = cu0, cv0, hu0, hv0, rot0
        lock_aspect = None

        if self._active_mode == 'TRANSLATE':
            delta_uv = (cur_uv - self._start_uv) * (0.2 if event.shift else 1.0)
            if self._axis == 'X':
                delta_uv.y = 0.0
            elif self._axis == 'Y':
                delta_uv.x = 0.0
            cu = cu0 + delta_uv.x
            cv = cv0 + delta_uv.y
            if event.ctrl:
                cu = round(cu * 20.0) * 0.05
                cv = round(cv * 20.0) * 0.05
        elif self._active_mode == 'ROTATE':
            v_cur = cur_mouse - self._center_px
            if v_cur.length > 1e-3:
                ang_diff = math.atan2(v_cur.y, v_cur.x) - self._start_angle
                rot_r = rot0 + ang_diff * (0.2 if event.shift else 1.0)
                if event.ctrl:
                    step = math.radians(15.0)
                    rot_r = round(rot_r / step) * step
        elif self._active_mode in {'SCALE', 'SCALE_X', 'SCALE_Y'}:
            cos_r = math.cos(rot0)
            sin_r = math.sin(rot0)
            du = cur_uv.x - cu0
            dv = cur_uv.y - cv0
            lu = abs(du * cos_r + dv * sin_r)
            lv = abs(-du * sin_r + dv * cos_r)

            if self._active_mode == 'SCALE_X' or self._axis == 'X':
                hu = max(lu, 0.008)
                lock_aspect = False
            elif self._active_mode == 'SCALE_Y' or self._axis == 'Y':
                hv = max(lv, 0.008)
                lock_aspect = False
            elif event.shift:
                # Shift + Corner drag = Free 2D non-uniform corner sizing
                hu = max(lu, 0.008)
                hv = max(lv, 0.008)
                lock_aspect = False
            else:
                ratio = max((cur_mouse - self._center_px).length / self._start_dist, 0.02)
                if event.ctrl:
                    ratio = round(ratio * 10.0) * 0.1
                hu = max(hu0 * ratio, 0.008)
                hv = max(hv0 * ratio, 0.008)

        _set_uv_gizmo_transform(
            context, node, layer, target_kind, eff, cu, cv, hu, hv, rot_r,
            lock_aspect=lock_aspect, interactive=True,
        )
        if context.area:
            context.area.header_text_set(
                f"UV Gizmo ({self._active_mode}): Pos=({cu:.2f}, {cv:.2f})  |  "
                f"Tile Scale=({0.5 / max(hu, 1e-3):.2f}, {0.5 / max(hv, 1e-3):.2f})  |  "
                f"Rot={math.degrees(rot_r):+.1f}°  |  X/Y: Axis  |  Shift: Free/Fine  |  Ctrl: Snap"
            )
        _tag_view3d_gizmo_refresh(context)

    def _finish(self, context, node, layer, interactive_refine=True):
        try:
            context.window.cursor_modal_restore()
        except Exception:
            pass
        if getattr(context, "area", None):
            context.area.header_text_set(None)
        if interactive_refine and node is not None and layer is not None and getattr(layer, "use_mask", False):
            _evaluate_layer_grunge_mask(node, layer, context=context, force_reproject=True, interactive=False, skip_absorb=True)
        _flush_dirty_node_json()
        _tag_view3d_gizmo_refresh(context)

    def modal(self, context, event):
        _ob, node, layer, target_kind, eff = _active_uv_projection_from_context(context)
        if node is None or layer is None:
            self._finish(context, node, layer, interactive_refine=False)
            return {'CANCELLED'}

        if event.type in {'MIDDLEMOUSE', 'WHEELUPMOUSE', 'WHEELDOWNMOUSE'}:
            return {'PASS_THROUGH'}

        if event.type == 'MOUSEMOVE':
            self._apply(context, event, node, layer, target_kind, eff)
            return {'RUNNING_MODAL'}

        if self._drag_release_confirm and event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
            self._finish(context, node, layer, interactive_refine=True)
            return {'FINISHED'}

        if event.type in {'LEFTMOUSE', 'RET', 'NUMPAD_ENTER', 'SPACE'} and event.value == 'PRESS':
            self._finish(context, node, layer, interactive_refine=True)
            return {'FINISHED'}

        if event.type in {'RIGHTMOUSE', 'ESC'} and event.value == 'PRESS':
            cu0, cv0, hu0, hv0, rot0, _rep = self._init_state
            _set_uv_gizmo_transform(context, node, layer, target_kind, eff, cu0, cv0, hu0, hv0, rot0, interactive=False)
            self._finish(context, node, layer, interactive_refine=False)
            return {'CANCELLED'}

        if event.value == 'PRESS' and event.type in {'G', 'R', 'S'}:
            self._active_mode = {'G': 'TRANSLATE', 'R': 'ROTATE', 'S': 'SCALE'}[event.type]
            self._axis = None
            self._apply(context, event, node, layer, target_kind, eff)
            return {'RUNNING_MODAL'}

        if event.value == 'PRESS' and event.type in {'X', 'Y'}:
            self._axis = None if self._axis == event.type else event.type
            self._apply(context, event, node, layer, target_kind, eff)
            return {'RUNNING_MODAL'}

        return {'RUNNING_MODAL'}


class IMAGE_OT_tex_paint_focus_uv_gizmo(Operator):
    """Show the interactive 2D UV Projection Gizmo in the Image Editor (opening an Image Editor pane if needed) so you can place the projection directly on the mesh UVs"""
    bl_idname = "image.tex_paint_focus_uv_gizmo"
    bl_label = "Place on UVs (Image Editor Gizmo)"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        _ob, node, layer, _tk, _eff = _active_uv_projection_from_context(context)
        return node is not None and layer is not None

    def execute(self, context):
        _ob, node, layer, _tk, _eff = _active_uv_projection_from_context(context)
        if node is None or layer is None:
            return {'CANCELLED'}

        layer.show_projection_widget = True

        img_area = None
        if getattr(context, "screen", None):
            for area in context.screen.areas:
                if area.type == 'IMAGE_EDITOR':
                    img_area = area
                    break

            if img_area is None and not bpy.app.background and context.area and context.area.type == 'VIEW_3D':
                prev_areas = set(context.screen.areas)
                try:
                    with context.temp_override(area=context.area):
                        bpy.ops.screen.area_split(direction='VERTICAL', factor=0.45)
                    new_areas = [a for a in context.screen.areas if a not in prev_areas]
                    if new_areas:
                        img_area = min(new_areas, key=lambda a: a.x)
                        img_area.type = 'IMAGE_EDITOR'
                except Exception:
                    img_area = None

            if img_area is not None:
                for sp in img_area.spaces:
                    if sp.type == 'IMAGE_EDITOR':
                        sp.image = node.image
                        break
                img_area.tag_redraw()

        _tag_view3d_gizmo_refresh(context)
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Substance-Style Multi-Layer Mask Stack & Grunge Operators
# ---------------------------------------------------------------------------

_BRUSH_COLOR_MEMORY = {}  # "rgb": (primary_rgb_tuple, secondary_rgb_tuple)
_LAST_SEEN_PAINT_TARGET = {}  # node_ptr -> 'COLOR' | 'MASK'
_SOLO_LAYER_STATE = {}  # node_ptr -> {"solo_uid": int, "saved_hide": {uid: bool}}


def _get_current_brush_colors(context):
    ts = getattr(context, "tool_settings", None)
    if ts is None:
        return None, None
    ip = getattr(ts, "image_paint", None)
    ups = getattr(ip, "unified_paint_settings", None) or getattr(ts, "unified_paint_settings", None)
    brush = getattr(ip, "brush", None) if ip is not None else None
    if ups is not None and (brush is None or getattr(brush, "use_unified_color", True)):
        return tuple(ups.color), tuple(getattr(ups, "secondary_color", (0.0, 0.0, 0.0)))
    if brush is not None:
        return tuple(brush.color), tuple(getattr(brush, "secondary_color", (0.0, 0.0, 0.0)))
    return None, None


def _is_pure_bw_color(col):
    if col is None or len(col) < 3:
        return False
    return (
        abs(col[0] - col[1]) < 1e-4 and
        abs(col[1] - col[2]) < 1e-4 and
        (col[0] < 1e-4 or col[0] > 0.9999)
    )


def _save_rgb_brush_color_before_mask(context):
    col, sec = _get_current_brush_colors(context)
    if col is None:
        return
    if "rgb" not in _BRUSH_COLOR_MEMORY or not _is_pure_bw_color(col):
        _BRUSH_COLOR_MEMORY["rgb"] = (col, sec if sec is not None else (1.0, 1.0, 1.0))


def _restore_rgb_brush_color_after_mask(context):
    saved = _BRUSH_COLOR_MEMORY.pop("rgb", None)
    if saved is None:
        return
    col, sec = saved
    ts = getattr(context, "tool_settings", None)
    if ts is not None:
        ip = getattr(ts, "image_paint", None)
        ups = getattr(ip, "unified_paint_settings", None) or getattr(ts, "unified_paint_settings", None)
        if ups is not None:
            ups.color = col
            if hasattr(ups, "secondary_color") and sec is not None:
                ups.secondary_color = sec
        if ip is not None and ip.brush is not None:
            ip.brush.color = col
            if hasattr(ip.brush, "secondary_color") and sec is not None:
                ip.brush.secondary_color = sec


def _configure_paint_mask_brush(context, value=1.0):
    _save_rgb_brush_color_before_mask(context)
    val = float(value)
    opp = 1.0 - val
    col = (val, val, val)
    sec_col = (opp, opp, opp)
    ts = getattr(context, "tool_settings", None)
    if ts is not None:
        ip = getattr(ts, "image_paint", None)
        ups = getattr(ip, "unified_paint_settings", None) or getattr(ts, "unified_paint_settings", None)
        if ups is not None:
            ups.color = col
            if hasattr(ups, "secondary_color"):
                ups.secondary_color = sec_col
        if ip is not None and ip.brush is not None:
            ip.brush.color = col
            if hasattr(ip.brush, "secondary_color"):
                ip.brush.secondary_color = sec_col
            ip.brush.blend = 'MIX'


class NODE_OT_tex_paint_mask_effect_add(Operator):
    """Add a new Mask Sub-Layer (Mask Fill, Procedural Smart Mask, Hand-Painted Mask, Grunge Fill, or Levels) to the active layer's Mask Stack"""
    bl_idname = "node.tex_paint_mask_effect_add"
    bl_label = "Add Mask Sub-Layer"
    bl_options = {'REGISTER', 'UNDO'}

    effect_type: EnumProperty(
        name="Effect Type",
        items=(
            ('GENERATOR', "Generator", "Add a 3D geometry generator (Edge Wear, Curvature, Cavity, AO, Top Dust, Height)", 'MODIFIER', 0),
            ('PAINT', "Paint", "Add a non-destructive hand-painted mask layer (paint White to Reveal, Black to Hide)", 'BRUSH_DATA', 1),
            ('GRUNGE', "Fill (Image / Grunge)", "Fill the mask with any custom grayscale/alpha image or Substance Grunge texture with 2D/3D projection", 'TEXTURE', 2),
            ('LEVELS', "Levels", "Add a non-destructive Balance / Contrast / Invert remapping over the mask stack below", 'SEQ_LUMA_WAVEFORM', 3),
            ('FILTER', "Filter (Blur / Warp / Bevel)", "Add a Substance-style spatial filter (Blur, Blur Slope, Sharpen, Grow/Shrink, Bevel, Threshold, Posterize, Highpass)", 'SHADERFX', 4),
        ),
        default='GRUNGE',
    )
    fill_source: EnumProperty(
        name="Fill Source",
        items=(
            ('GRUNGE', "Grunge Library", "Initialize the Mask Fill with the built-in 275 Substance Grunge & Procedural library"),
            ('CUSTOM_IMAGE', "Custom Image", "Initialize the Mask Fill with a custom image slot (any image, alpha stencil, logo, or heightmap)"),
        ),
        default='GRUNGE',
        options={'SKIP_SAVE'},
    )
    gen_type: StringProperty(
        name="Generator Preset",
        default="EDGE_WEAR",
        options={'SKIP_SAVE'},
    )
    filter_type: StringProperty(
        name="Filter Type",
        default="BLUR",
        options={'SKIP_SAVE'},
    )
    blend_override: StringProperty(
        name="Blend Mode Override",
        default="",
        options={'SKIP_SAVE'},
    )

    @classmethod
    def poll(cls, context):
        node = tex_paint_node_from_context(context)
        return node is not None and node.active_layer is not None

    def execute(self, context):
        node = tex_paint_node_from_context(context)
        layer = node.active_layer if node else None
        if node is None or layer is None:
            return {'CANCELLED'}

        _absorb_pending_brush_strokes(node, layer)

        # If adding the very first effect onto an existing hand-painted mask buffer, preserve existing mask base
        st = _get_layer_grunge_state(node, layer)
        effects = st.get("effects", [])
        is_first = (len(effects) == 0)

        if not layer.use_mask:
            layer.use_mask = True
            if self.effect_type == 'PAINT':
                st["base_value"] = 255

        uid = int(st.get("next_effect_uid", 1))
        st["next_effect_uid"] = uid + 1

        eff = _make_default_mask_effect(
            effect_type=self.effect_type,
            uid=uid,
            is_first=is_first,
            gen_type=self.gen_type or 'EDGE_WEAR',
            fill_source=self.fill_source or 'GRUNGE',
            filter_type=self.filter_type or 'BLUR',
        )
        if self.blend_override:
            eff["blend_mode"] = self.blend_override

        # Insert above active effect (or at top of stack)
        insert_idx = len(effects)
        act_uid = st.get("active_effect_uid", 0)
        for idx, e in enumerate(effects):
            if e["uid"] == act_uid:
                insert_idx = idx + 1
                break
        effects.insert(insert_idx, eff)

        st["effects"] = effects
        st["active_effect_uid"] = uid
        st["active"] = True
        _save_layer_grunge_state(node, layer, st)

        node.paint_target = 'MASK'
        if self.effect_type == 'PAINT':
            _configure_paint_mask_brush(context, 1.0)
        elif eff.get("proj_mode") in {'REPEAT', 'UV', 'FIT'}:
            layer.show_projection_widget = True

        _evaluate_layer_grunge_mask(node, layer, context=context, force_reproject=True, skip_absorb=True)
        _tag_view3d_gizmo_refresh(context)
        return {'FINISHED'}


class NODE_OT_tex_paint_mask_effect_remove(Operator):
    """Remove a Mask Sub-Layer from the active layer's Mask Stack"""
    bl_idname = "node.tex_paint_mask_effect_remove"
    bl_label = "Remove Mask Sub-Layer"
    bl_options = {'REGISTER', 'UNDO'}

    effect_uid: IntProperty(name="Effect UID", default=0, options={'SKIP_SAVE'})

    @classmethod
    def poll(cls, context):
        node = tex_paint_node_from_context(context)
        if node is None or node.active_layer is None:
            return False
        st = _get_layer_grunge_state(node, node.active_layer)
        return bool(st.get("effects"))

    def execute(self, context):
        node = tex_paint_node_from_context(context)
        layer = node.active_layer if node else None
        if node is None or layer is None:
            return {'CANCELLED'}

        _absorb_pending_brush_strokes(node, layer)
        st = _get_layer_grunge_state(node, layer)
        effects = st.get("effects", [])
        target_uid = int(self.effect_uid) if self.effect_uid > 0 else int(st.get("active_effect_uid", 0))

        new_effects = [e for e in effects if int(e["uid"]) != target_uid]
        node_ptr = node.as_pointer()
        layer_uid = _layer_uid(layer)
        _EFFECT_RAW_CACHE.pop((node_ptr, layer_uid, target_uid), None)
        _PAINT_EFFECT_BUFFERS.pop((node_ptr, layer_uid, target_uid), None)

        st["effects"] = new_effects
        if new_effects:
            st["active_effect_uid"] = new_effects[-1]["uid"]
            _save_layer_grunge_state(node, layer, st)
            _evaluate_layer_grunge_mask(node, layer, context=context, force_reproject=False, skip_absorb=True)
        else:
            st["active_effect_uid"] = 0
            st["active"] = False
            _save_layer_grunge_state(node, layer, st)
            bpy.ops.node.tex_paint_layer_mask_init(mode='WHITE')
        _tag_view3d_gizmo_refresh(context)
        return {'FINISHED'}


class NODE_OT_tex_paint_mask_effect_move(Operator):
    """Move the active Mask Sub-Layer up or down in the Mask Stack"""
    bl_idname = "node.tex_paint_mask_effect_move"
    bl_label = "Move Mask Sub-Layer"
    bl_options = {'REGISTER', 'UNDO'}

    direction: EnumProperty(
        name="Direction",
        items=(
            ('UP', "Up", "Move the mask sub-layer higher in the stack (evaluated later)"),
            ('DOWN', "Down", "Move the mask sub-layer lower in the stack (evaluated earlier)"),
        ),
        default='UP',
    )

    @classmethod
    def poll(cls, context):
        node = tex_paint_node_from_context(context)
        if node is None or node.active_layer is None:
            return False
        st = _get_layer_grunge_state(node, node.active_layer)
        return len(st.get("effects", [])) > 1

    def execute(self, context):
        node = tex_paint_node_from_context(context)
        layer = node.active_layer if node else None
        if node is None or layer is None:
            return {'CANCELLED'}

        _absorb_pending_brush_strokes(node, layer)
        st = _get_layer_grunge_state(node, layer)
        effects = st.get("effects", [])
        act_uid = st.get("active_effect_uid", 0)
        cur_idx = -1
        for i, e in enumerate(effects):
            if e["uid"] == act_uid:
                cur_idx = i
                break
        if cur_idx < 0:
            return {'CANCELLED'}

        new_idx = cur_idx + (1 if self.direction == 'UP' else -1)
        if not (0 <= new_idx < len(effects)):
            return {'CANCELLED'}

        effects[cur_idx], effects[new_idx] = effects[new_idx], effects[cur_idx]
        st["effects"] = effects
        _save_layer_grunge_state(node, layer, st)
        _evaluate_layer_grunge_mask(node, layer, context=context, force_reproject=False, skip_absorb=True)
        return {'FINISHED'}


class NODE_OT_tex_paint_mask_effect_duplicate(Operator):
    """Duplicate the active Mask Sub-Layer in the Mask Stack"""
    bl_idname = "node.tex_paint_mask_effect_duplicate"
    bl_label = "Duplicate Mask Sub-Layer"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        node = tex_paint_node_from_context(context)
        return node is not None and _get_active_mask_effect(node) is not None

    def execute(self, context):
        node = tex_paint_node_from_context(context)
        layer = node.active_layer if node else None
        if node is None or layer is None:
            return {'CANCELLED'}

        _absorb_pending_brush_strokes(node, layer)
        st = _get_layer_grunge_state(node, layer)
        effects = st.get("effects", [])
        src = _get_active_mask_effect(node, layer)
        if src is None:
            return {'CANCELLED'}

        new_uid = int(st.get("next_effect_uid", 1))
        st["next_effect_uid"] = new_uid + 1
        dup = dict(src)
        dup["uid"] = new_uid
        dup["name"] = f"{src.get('name', 'Mask')} Copy"

        node_ptr = node.as_pointer()
        layer_uid = _layer_uid(layer)
        if src.get("type") == 'PAINT':
            src_buf = _PAINT_EFFECT_BUFFERS.get((node_ptr, layer_uid, int(src["uid"])))
            if src_buf is not None:
                _PAINT_EFFECT_BUFFERS[(node_ptr, layer_uid, new_uid)] = {
                    "w": src_buf["w"],
                    "h": src_buf["h"],
                    "val_u8": src_buf["val_u8"].copy(),
                    "alpha_u8": src_buf["alpha_u8"].copy(),
                    "rev": 1,
                }

        insert_idx = len(effects)
        for i, e in enumerate(effects):
            if e["uid"] == src["uid"]:
                insert_idx = i + 1
                break
        effects.insert(insert_idx, dup)
        st["effects"] = effects
        st["active_effect_uid"] = new_uid
        _save_layer_grunge_state(node, layer, st)
        _evaluate_layer_grunge_mask(node, layer, context=context, force_reproject=False, skip_absorb=True)
        return {'FINISHED'}


class NODE_OT_tex_paint_mask_effect_select(Operator):
    """Select a Mask Sub-Layer in the Mask Stack to edit its settings or paint on it"""
    bl_idname = "node.tex_paint_mask_effect_select"
    bl_label = "Select Mask Sub-Layer"
    bl_options = {'REGISTER', 'UNDO'}

    effect_uid: IntProperty(name="Effect UID", default=0)

    def execute(self, context):
        node = tex_paint_node_from_context(context)
        layer = node.active_layer if node else None
        if node is None or layer is None:
            return {'CANCELLED'}

        _absorb_pending_brush_strokes(node, layer)
        st = _get_layer_grunge_state(node, layer)
        st["active_effect_uid"] = int(self.effect_uid)
        _save_layer_grunge_state(node, layer, st)

        eff = _get_active_mask_effect(node, layer)
        if eff is not None and eff.get("type") == 'PAINT':
            node.paint_target = 'MASK'
        _tag_view3d_gizmo_refresh(context)
        return {'FINISHED'}


class NODE_OT_tex_paint_mask_effect_toggle(Operator):
    """Toggle visibility (Eye) or Invert on a Mask Sub-Layer in the Mask Stack"""
    bl_idname = "node.tex_paint_mask_effect_toggle"
    bl_label = "Toggle Mask Sub-Layer Property"
    bl_options = {'REGISTER', 'UNDO'}

    effect_uid: IntProperty(name="Effect UID", default=0)
    prop: EnumProperty(
        name="Property",
        items=(
            ('ENABLED', "Visibility", "Mute or unmute this mask sub-layer"),
            ('INVERT', "Invert", "Invert this mask sub-layer's values"),
        ),
        default='ENABLED',
    )

    def execute(self, context):
        node = tex_paint_node_from_context(context)
        layer = node.active_layer if node else None
        if node is None or layer is None:
            return {'CANCELLED'}

        _absorb_pending_brush_strokes(node, layer)
        st = _get_layer_grunge_state(node, layer)
        for eff in st.get("effects", []):
            if int(eff["uid"]) == int(self.effect_uid):
                if self.prop == 'ENABLED':
                    eff["enabled"] = not bool(eff.get("enabled", True))
                elif self.prop == 'INVERT':
                    eff["invert"] = not bool(eff.get("invert", False))
                break
        _save_layer_grunge_state(node, layer, st)
        _evaluate_layer_grunge_mask(node, layer, context=context, force_reproject=False, skip_absorb=True)
        return {'FINISHED'}


class NODE_OT_tex_paint_mask_paint_action(Operator):
    """Clear, Fill White, Fill Black, or Invert the painted strokes on the active Paint Mask Sub-Layer"""
    bl_idname = "node.tex_paint_mask_paint_action"
    bl_label = "Paint Mask Sub-Layer Action"
    bl_options = {'REGISTER', 'UNDO'}

    action: EnumProperty(
        name="Action",
        items=(
            ('CLEAR', "Clear Strokes", "Erase all hand-painted strokes on this Paint sub-layer so underlying mask layers pass through"),
            ('FILL_WHITE', "Fill White", "Fill this Paint sub-layer with 100% White (Reveal)"),
            ('FILL_BLACK', "Fill Black", "Fill this Paint sub-layer with 100% Black (Hide)"),
            ('INVERT_STROKES', "Invert Strokes", "Swap painted White and Black strokes on this Paint sub-layer"),
        ),
        default='CLEAR',
    )

    @classmethod
    def poll(cls, context):
        node = tex_paint_node_from_context(context)
        eff = _get_active_mask_effect(node) if node else None
        return eff is not None and eff.get("type") == 'PAINT'

    def execute(self, context):
        node = tex_paint_node_from_context(context)
        layer = node.active_layer if node else None
        if node is None or layer is None:
            return {'CANCELLED'}

        _absorb_pending_brush_strokes(node, layer)
        eff = _get_active_mask_effect(node, layer)
        if eff is None or eff.get("type") != 'PAINT':
            return {'CANCELLED'}

        c_layer = _C_TexPaintLayer.from_address(layer.as_pointer())
        if not c_layer.mask_ibuf:
            return {'CANCELLED'}
        c_imb = _C_ImBuf.from_address(c_layer.mask_ibuf)
        w, h = int(c_imb.x), int(c_imb.y)
        pbuf = _ensure_paint_effect_buffer(node.as_pointer(), int(c_layer.uid), eff, w, h)

        if self.action == 'CLEAR':
            pbuf["val_u8"].fill(0)
            pbuf["alpha_u8"].fill(0)
        elif self.action == 'FILL_WHITE':
            pbuf["val_u8"].fill(255)
            pbuf["alpha_u8"].fill(255)
        elif self.action == 'FILL_BLACK':
            pbuf["val_u8"].fill(0)
            pbuf["alpha_u8"].fill(255)
        elif self.action == 'INVERT_STROKES':
            mask = pbuf["alpha_u8"] > 0
            pbuf["val_u8"][mask] = 255 - pbuf["val_u8"][mask]

        pbuf["rev"] = int(pbuf.get("rev", 1)) + 1
        _sync_paint_effect_z64(eff, pbuf)
        st = _get_layer_grunge_state(node, layer)
        _save_layer_grunge_state(node, layer, st, flush_json=True)
        _evaluate_layer_grunge_mask(node, layer, context=context, force_reproject=False, skip_absorb=True)
        return {'FINISHED'}


class NODE_OT_tex_paint_mask_brush_color(Operator):
    """Set the active Texture Paint brush to White (Reveal) or Black (Hide) and switch target to the active Paint Mask sub-layer"""
    bl_idname = "node.tex_paint_mask_brush_color"
    bl_label = "Set Mask Brush Value"
    bl_options = {'REGISTER', 'UNDO'}

    value: FloatProperty(
        name="Mask Value",
        description="1.0 = White (Reveal), 0.0 = Black (Hide)",
        default=1.0,
        min=0.0,
        max=1.0,
    )

    def execute(self, context):
        node = tex_paint_node_from_context(context)
        if node is not None:
            node.paint_target = 'MASK'
            layer = node.active_layer
            if layer is not None:
                if not layer.use_mask:
                    layer.use_mask = True
                _absorb_pending_brush_strokes(node, layer)
                st = _get_layer_grunge_state(node, layer)
                effects = st.get("effects", [])
                eff = _get_active_mask_effect(node, layer)
                if eff is None or eff.get("type") != 'PAINT':
                    # Select topmost PAINT sub-layer if one exists, or add one on top
                    paint_eff = next((e for e in reversed(effects) if e.get("type") == 'PAINT'), None)
                    if paint_eff is not None:
                        st["active_effect_uid"] = paint_eff["uid"]
                        _save_layer_grunge_state(node, layer, st)
                    else:
                        bpy.ops.node.tex_paint_mask_effect_add(effect_type='PAINT')

        _configure_paint_mask_brush(context, self.value)
        return {'FINISHED'}


class NODE_OT_tex_paint_grunge_apply(Operator):
    """Apply or add a Substance Grunge texture mask sub-layer on the active layer"""
    bl_idname = "node.tex_paint_grunge_apply"
    bl_label = "Apply Grunge Mask"
    bl_options = {'REGISTER', 'UNDO'}

    blend_override: StringProperty(
        name="Blend Mode Override",
        default="",
        options={'SKIP_SAVE'},
    )

    @classmethod
    def poll(cls, context):
        node = tex_paint_node_from_context(context)
        return node is not None and node.active_layer is not None

    def execute(self, context):
        node = tex_paint_node_from_context(context)
        layer = node.active_layer if node else None
        if node is None or layer is None:
            return {'CANCELLED'}

        _absorb_pending_brush_strokes(node, layer)
        eff = _get_active_mask_effect(node, layer)
        if eff is None or eff.get("type") != 'GRUNGE':
            return bpy.ops.node.tex_paint_mask_effect_add(
                effect_type='GRUNGE',
                blend_override=self.blend_override,
            )

        if self.blend_override:
            _update_active_mask_effect(node, layer, {"blend_mode": self.blend_override})
        _evaluate_layer_grunge_mask(node, layer, context=context, force_reproject=True, skip_absorb=True)
        _tag_view3d_gizmo_refresh(context)
        return {'FINISHED'}


class NODE_OT_tex_paint_grunge_step(Operator):
    """Cycle to the previous or next Substance Grunge map in the active category"""
    bl_idname = "node.tex_paint_grunge_step"
    bl_label = "Step Grunge Texture"
    bl_options = {'REGISTER', 'UNDO'}

    step: IntProperty(name="Step", default=1)

    @classmethod
    def poll(cls, context):
        node = tex_paint_node_from_context(context)
        return node is not None and node.active_layer is not None

    def execute(self, context):
        node = tex_paint_node_from_context(context)
        layer = node.active_layer if node else None
        if node is None or layer is None:
            return {'CANCELLED'}

        _ensure_grunge_catalog()
        eff = _get_active_mask_effect(node, layer)
        if eff is None or eff.get("type") != 'GRUNGE':
            eff = _update_active_mask_effect(node, layer, {}, ensure_type='GRUNGE')

        cat = eff.get("category", "BITMAP_GRUNGES")
        entries = _GRUNGE_CATALOG.get(cat, [])
        if not entries:
            return {'CANCELLED'}

        cur_id = eff.get("texture", "")
        cur_idx = 0
        for idx, item in enumerate(entries):
            if item["id"] == cur_id:
                cur_idx = idx
                break
        new_idx = (cur_idx + int(self.step)) % len(entries)
        new_id = entries[new_idx]["id"]
        _update_active_mask_effect(node, layer, {"texture": new_id}, ensure_type='GRUNGE')
        _evaluate_layer_grunge_mask(node, layer, context=context, force_reproject=True)
        return {'FINISHED'}


class NODE_OT_tex_paint_grunge_randomize(Operator):
    """Pick a random Substance Grunge texture or randomize its 3D offset and rotation seed"""
    bl_idname = "node.tex_paint_grunge_randomize"
    bl_label = "Randomize Grunge Mask"
    bl_options = {'REGISTER', 'UNDO'}

    random_map: BoolProperty(
        name="Randomize Grunge Map",
        description="Pick a random grunge texture from the current category in addition to randomizing placement",
        default=True,
    )

    @classmethod
    def poll(cls, context):
        node = tex_paint_node_from_context(context)
        return node is not None and node.active_layer is not None

    def execute(self, context):
        node = tex_paint_node_from_context(context)
        layer = node.active_layer if node else None
        if node is None or layer is None:
            return {'CANCELLED'}

        _ensure_grunge_catalog()
        eff = _get_active_mask_effect(node, layer)
        if eff is None or eff.get("type") != 'GRUNGE':
            eff = _update_active_mask_effect(node, layer, {}, ensure_type='GRUNGE')

        cat = eff.get("category", "BITMAP_GRUNGES")
        updates = {
            "offset_x": round(random.uniform(-0.5, 0.5), 3),
            "offset_y": round(random.uniform(-0.5, 0.5), 3),
            "rotation": round(random.uniform(-math.pi, math.pi), 3),
        }
        if self.random_map and cat != 'CUSTOM':
            entries = _GRUNGE_CATALOG.get(cat, [])
            if entries:
                updates["texture"] = random.choice(entries)["id"]

        _update_active_mask_effect(node, layer, updates, ensure_type='GRUNGE')
        _evaluate_layer_grunge_mask(node, layer, context=context, force_reproject=True)
        return {'FINISHED'}


class NODE_OT_tex_paint_grunge_open_image(Operator):
    """Import any image file from disk and fill it into the active layer's Mask Stack"""
    bl_idname = "node.tex_paint_grunge_open_image"
    bl_label = "Open Image into Mask Fill"
    bl_options = {'REGISTER', 'UNDO'}

    filepath: StringProperty(subtype='FILE_PATH')
    filter_image: BoolProperty(default=True, options={'HIDDEN'})
    filter_folder: BoolProperty(default=True, options={'HIDDEN'})

    @classmethod
    def poll(cls, context):
        node = tex_paint_node_from_context(context)
        return node is not None and node.active_layer is not None

    def invoke(self, context, _event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        node = tex_paint_node_from_context(context)
        layer = node.active_layer if node else None
        if node is None or layer is None or not self.filepath:
            return {'CANCELLED'}
        try:
            ima = bpy.data.images.load(self.filepath, check_existing=True)
        except Exception as ex:
            self.report({'ERROR'}, f"Could not load image: {ex}")
            return {'CANCELLED'}

        if not layer.use_mask:
            layer.use_mask = True

        eff = _get_active_mask_effect(node, layer)
        if eff is None or eff.get("type") != 'GRUNGE':
            bpy.ops.node.tex_paint_mask_effect_add(
                effect_type='GRUNGE',
                fill_source='CUSTOM_IMAGE',
            )

        _update_active_mask_effect(
            node,
            layer,
            {
                "category": "CUSTOM",
                "custom_image": ima.name,
                "name": os.path.splitext(os.path.basename(self.filepath))[0][:28],
            },
            ensure_type='GRUNGE',
        )
        _evaluate_layer_grunge_mask(node, layer, context=context, force_reproject=True)
        _tag_view3d_gizmo_refresh(context)
        return {'FINISHED'}


class NODE_OT_tex_paint_fill_transform_action(Operator):
    """Quick transform helper for the active Mask Fill sub-layer (Reset, Center, Rotate 90°, Match Image Aspect)"""
    bl_idname = "node.tex_paint_fill_transform_action"
    bl_label = "Mask Fill Transform"
    bl_options = {'REGISTER', 'UNDO'}

    action: EnumProperty(
        name="Action",
        items=(
            ('RESET', "Reset Transform", "Reset Scale, Offset, Rotation, and 3D Gizmo placement of the active Mask Fill"),
            ('CENTER', "Center Offset", "Reset Offset X and Offset Y to (0, 0)"),
            ('ROT_90_CCW', "Rotate -90°", "Rotate the Mask Fill 90 degrees counter-clockwise"),
            ('ROT_90_CW', "Rotate +90°", "Rotate the Mask Fill 90 degrees clockwise"),
            ('MATCH_ASPECT', "Match Image Aspect", "Adjust Scale X / Scale Y to preserve the source image's aspect ratio without stretching"),
        ),
        default='RESET',
    )

    @classmethod
    def poll(cls, context):
        node = tex_paint_node_from_context(context)
        eff = _get_active_mask_effect(node) if node else None
        return eff is not None and eff.get("type") == 'GRUNGE'

    def execute(self, context):
        node = tex_paint_node_from_context(context)
        layer = node.active_layer if node else None
        eff = _get_active_mask_effect(node, layer) if (node and layer) else None
        if eff is None or eff.get("type") != 'GRUNGE':
            return {'CANCELLED'}

        updates = {}
        if self.action == 'RESET':
            def_s = 1.0 if eff.get("category") == 'CUSTOM' else 2.0
            updates = {
                "scale": def_s,
                "scale_x": def_s,
                "scale_y": def_s,
                "lock_scale": True,
                "offset_x": 0.0,
                "offset_y": 0.0,
                "rotation": 0.0,
                "gizmo_override": False,
            }
            if getattr(layer, "show_projection_widget", False):
                bpy.ops.node.tex_paint_gizmo_mode(mode='RESET_LOC')
                bpy.ops.node.tex_paint_gizmo_mode(mode='RESET_ROT')
                bpy.ops.node.tex_paint_gizmo_mode(mode='RESET_SCALE')
        elif self.action == 'CENTER':
            updates = {"offset_x": 0.0, "offset_y": 0.0}
        elif self.action in {'ROT_90_CCW', 'ROT_90_CW'}:
            step = -math.pi * 0.5 if self.action == 'ROT_90_CCW' else math.pi * 0.5
            cur_rot = float(eff.get("rotation", 0.0)) + step
            while cur_rot > math.pi:
                cur_rot -= 2.0 * math.pi
            while cur_rot < -math.pi:
                cur_rot += 2.0 * math.pi
            updates = {"rotation": round(cur_rot, 4)}
        elif self.action == 'MATCH_ASPECT':
            ima = _resolve_grunge_image(eff, interactive=False)
            if ima is not None and ima.size[0] > 0 and ima.size[1] > 0:
                iw, ih = float(ima.size[0]), float(ima.size[1])
                base_s = max(0.05, float(eff.get("scale", 1.0)))
                if iw >= ih:
                    sx = base_s
                    sy = round(base_s * (iw / ih), 4)
                else:
                    sx = round(base_s * (ih / iw), 4)
                    sy = base_s
                updates = {
                    "lock_scale": False,
                    "scale_x": sx,
                    "scale_y": sy,
                }

        if updates:
            _update_active_mask_effect(node, layer, updates, ensure_type='GRUNGE')
            _evaluate_layer_grunge_mask(node, layer, context=context, force_reproject=True)
            _tag_view3d_gizmo_refresh(context)
        return {'FINISHED'}


class NODE_OT_tex_paint_mask_gen_preset(Operator):
    """Switch the active Smart Mask Generator sub-layer to a specific procedural geometry preset"""
    bl_idname = "node.tex_paint_mask_gen_preset"
    bl_label = "Set Smart Mask Generator Preset"
    bl_options = {'REGISTER', 'UNDO'}

    gen_type: EnumProperty(
        name="Generator",
        items=(
            ('EDGE_WEAR', "Edge Wear", "Highlight convex sharp edges attenuated by ambient occlusion"),
            ('CURVATURE', "Curvature", "Full curvature map (concave to convex)"),
            ('CAVITY', "Cavity", "Highlight concave crevices, seams, and recessed dirt areas"),
            ('AO', "Ambient Occlusion", "Ambient occlusion shading across the mesh"),
            ('NORMAL_UP', "Top Dust", "Top-facing world/object normal for dust, snow, or moss"),
            ('POSITION_Z', "Height", "Bottom-to-top vertical height gradient"),
        ),
        default='EDGE_WEAR',
    )

    @classmethod
    def poll(cls, context):
        node = tex_paint_node_from_context(context)
        return node is not None and node.active_layer is not None

    def execute(self, context):
        node = tex_paint_node_from_context(context)
        layer = node.active_layer if node else None
        if node is None or layer is None:
            return {'CANCELLED'}

        _absorb_pending_brush_strokes(node, layer)
        eff = _get_active_mask_effect(node, layer)
        if eff is None or eff.get("type") != 'GENERATOR':
            return bpy.ops.node.tex_paint_mask_effect_add(
                effect_type='GENERATOR',
                gen_type=self.gen_type,
            )

        _update_active_mask_effect(node, layer, {"gen_type": self.gen_type}, ensure_type='GENERATOR')
        _evaluate_layer_grunge_mask(node, layer, context=context, force_reproject=True, skip_absorb=True)
        return {'FINISHED'}


class NODE_OT_tex_paint_mask_stack_init(Operator):
    """Initialize, reset, or remove the layer's Mask Stack (White, Black, or Remove Mask)"""
    bl_idname = "node.tex_paint_mask_stack_init"
    bl_label = "Initialize Layer Mask Stack"
    bl_options = {'REGISTER', 'UNDO'}

    mode: EnumProperty(
        name="Mode",
        items=(
            ('WHITE', "White (Reveal All)", "Reset the mask to 100% White (Reveal) and clear the mask sub-layer stack"),
            ('BLACK', "Black (Hide All)", "Reset the mask to 100% Black (Hide) and clear the mask sub-layer stack"),
            ('REMOVE', "Remove Mask", "Remove the layer mask and all mask sub-layers"),
        ),
        default='WHITE',
    )

    @classmethod
    def poll(cls, context):
        node = tex_paint_node_from_context(context)
        return node is not None and node.active_layer is not None

    def execute(self, context):
        node = tex_paint_node_from_context(context)
        layer = node.active_layer if node else None
        if node is None or layer is None:
            return {'CANCELLED'}

        node_ptr = node.as_pointer()
        layer_uid = _layer_uid(layer)
        st = _get_layer_grunge_state(node, layer)
        for eff in st.get("effects", []):
            e_uid = int(eff["uid"])
            _EFFECT_RAW_CACHE.pop((node_ptr, layer_uid, e_uid), None)
            _PAINT_EFFECT_BUFFERS.pop((node_ptr, layer_uid, e_uid), None)
        _STACK_BELOW_CACHE.pop((node_ptr, layer_uid), None)
        _LAST_COMPOSITED_MASK.pop((node_ptr, layer_uid), None)

        st["effects"] = []
        st["active_effect_uid"] = 0
        st["active"] = False
        st["base_value"] = 0 if self.mode == 'BLACK' else 255
        _save_layer_grunge_state(node, layer, st)

        bpy.ops.node.tex_paint_layer_mask_init(mode=self.mode)
        _sync_layer_mask_to_companion_channels(node, layer)
        if self.mode == 'BLACK':
            # Black mask hides all -> set brush to White (1.0) so painting immediately reveals!
            _configure_paint_mask_brush(context, 1.0)
        elif self.mode == 'WHITE':
            # White mask reveals all -> set brush to Black (0.0) so painting immediately hides!
            _configure_paint_mask_brush(context, 0.0)
        elif self.mode == 'REMOVE':
            _restore_rgb_brush_color_after_mask(context)
        _tag_view3d_gizmo_refresh(context)
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Heavy-User Workflow Operators: Target Switch, Solo, PBR Channels, Specials,
# Smart Weathering Recipes, Viewport Shading, and Quick UV Unwrap
# ---------------------------------------------------------------------------

class NODE_OT_tex_paint_layer_target_select(Operator):
    """Select a layer and switch painting focus between its Color buffer and its Mask Stack (preserving brush colors)"""
    bl_idname = "node.tex_paint_layer_target_select"
    bl_label = "Select Layer & Paint Target"
    bl_options = {'REGISTER', 'UNDO'}

    layer_index: IntProperty(name="Layer Index", default=-1, options={'SKIP_SAVE'})
    target: EnumProperty(
        name="Paint Target",
        items=(
            ('COLOR', "Color", "Paint RGB color on the selected layer"),
            ('MASK', "Mask", "Paint or edit the layer mask on the selected layer"),
        ),
        default='COLOR',
    )

    @classmethod
    def poll(cls, context):
        return tex_paint_node_from_context(context) is not None

    def execute(self, context):
        node = tex_paint_node_from_context(context)
        if node is None:
            return {'CANCELLED'}

        if 0 <= self.layer_index < len(node.layers):
            node.active_layer_index = self.layer_index

        layer = node.active_layer
        if layer is None:
            return {'CANCELLED'}

        if self.target == 'MASK':
            node.paint_target = 'MASK'
            _LAST_SEEN_PAINT_TARGET[node.as_pointer()] = 'MASK'
            if getattr(layer, "use_mask", False):
                st = _get_layer_grunge_state(node, layer)
                col, _ = _get_current_brush_colors(context)
                if not _is_pure_bw_color(col):
                    default_val = 1.0 if (int(st.get("base_value", 255)) == 0 and not st.get("effects")) else 0.0
                    _configure_paint_mask_brush(context, default_val)
        else:
            if getattr(layer, "preview_mask", False):
                layer.preview_mask = False
            node.paint_target = 'COLOR'
            _LAST_SEEN_PAINT_TARGET[node.as_pointer()] = 'COLOR'
            _restore_rgb_brush_color_after_mask(context)

        _tag_view3d_gizmo_refresh(context)
        return {'FINISHED'}


class NODE_OT_tex_paint_fill_layer_texture(Operator):
    """Switch a Fill or Paint layer between Solid Value and Texture / 275 Grunge Projection, or step/randomize the projected Grunge map"""
    bl_idname = "node.tex_paint_fill_layer_texture"
    bl_label = "Fill Layer Texture / Grunge"
    bl_options = {'REGISTER', 'UNDO'}

    action: EnumProperty(
        name="Action",
        items=(
            ('SOLID', "Solid Value", "Use uniform values per channel without a projected texture"),
            ('GRUNGE', "Texture / Grunge (275)", "Project a 275 Substance Grunge or Custom Image across this layer"),
            ('PREV', "Previous Grunge", "Select the previous Grunge map in the library"),
            ('NEXT', "Next Grunge", "Select the next Grunge map in the library"),
            ('RANDOM', "Random Grunge", "Pick a random Grunge map from the 275 Substance library"),
        ),
        default='GRUNGE',
    )

    @classmethod
    def poll(cls, context):
        node = tex_paint_node_from_context(context)
        layer = node.active_layer if node else None
        return layer is not None and layer.type in {'FILL', 'PAINT'}

    def execute(self, context):
        import random
        node = tex_paint_node_from_context(context)
        layer = node.active_layer if node else None
        if node is None or layer is None:
            return {'CANCELLED'}

        if self.action == 'SOLID':
            layer.proj_mode = 'NONE'
            layer.proj_image_name = ""
            layer.show_projection_widget = False
            _tag_view3d_gizmo_refresh(context)
            return {'FINISHED'}

        _ensure_grunge_catalog()
        all_entries = []
        for cat_id in ('BITMAP_GRUNGES', 'PROC_GRUNGES', 'NOISES', 'PATTERNS'):
            all_entries.extend(_GRUNGE_CATALOG.get(cat_id, []))
        if not all_entries:
            return {'CANCELLED'}

        cur_img_name = getattr(layer, "proj_image_name", "")
        cur_idx = 0
        for idx, info in enumerate(all_entries):
            if cur_img_name and (info["stem"] in cur_img_name or cur_img_name in info["stem"]):
                cur_idx = idx
                break

        if self.action == 'PREV':
            target_info = all_entries[(cur_idx - 1) % len(all_entries)]
        elif self.action == 'NEXT':
            target_info = all_entries[(cur_idx + 1) % len(all_entries)]
        elif self.action == 'RANDOM':
            target_info = random.choice(all_entries)
        else:
            target_info = all_entries[cur_idx]

        png_path = target_info.get("png_path", "")
        if png_path and os.path.isfile(png_path):
            ima = bpy.data.images.load(png_path, check_existing=True)
            if getattr(layer, "proj_mode", 'NONE') == 'NONE':
                is_2d = _is_standalone_2d_node(node)
                layer.proj_mode = 'UV_REPEAT' if is_2d else 'TRIPLANAR'
                if is_2d:
                    layer.proj_scale = (2.0, 2.0, 1.0)
            layer.proj_image_name = ima.name
            _reproject_color_layer_multi_object(node, layer, context=context, force=True)

        _tag_view3d_gizmo_refresh(context)
        return {'FINISHED'}


def _is_layer_soloed(node, layer):
    if node is None or layer is None:
        return False
    info = _SOLO_LAYER_STATE.get(node.as_pointer())
    if not info:
        return False
    return int(info.get("solo_uid", -1)) == _layer_uid(layer)


class NODE_OT_tex_paint_layer_solo(Operator):
    """Isolate (Solo) a single layer to inspect its contribution, or restore all layers' previous visibility"""
    bl_idname = "node.tex_paint_layer_solo"
    bl_label = "Solo / Isolate Layer"
    bl_options = {'REGISTER', 'UNDO'}

    layer_index: IntProperty(name="Layer Index", default=-1, options={'SKIP_SAVE'})

    @classmethod
    def poll(cls, context):
        node = tex_paint_node_from_context(context)
        return node is not None and len(node.layers) > 0

    def execute(self, context):
        node = tex_paint_node_from_context(context)
        if node is None or len(node.layers) == 0:
            return {'CANCELLED'}

        idx = self.layer_index if (0 <= self.layer_index < len(node.layers)) else node.active_layer_index
        if not (0 <= idx < len(node.layers)):
            return {'CANCELLED'}

        target_layer = node.layers[idx]
        target_uid = _layer_uid(target_layer)
        node_ptr = node.as_pointer()
        cur_solo = _SOLO_LAYER_STATE.get(node_ptr)

        if cur_solo is not None and int(cur_solo.get("solo_uid", -1)) == target_uid:
            # Un-solo and restore previous visibility
            saved_hide = cur_solo.get("saved_hide", {})
            for l_item in node.layers:
                uid = _layer_uid(l_item)
                if uid in saved_hide:
                    l_item.hide = bool(saved_hide[uid])
                else:
                    l_item.hide = False
            _SOLO_LAYER_STATE.pop(node_ptr, None)
        else:
            # Save current hide state if not already in solo mode
            if cur_solo is not None and "saved_hide" in cur_solo:
                saved_hide = cur_solo["saved_hide"]
            else:
                saved_hide = {_layer_uid(l_item): bool(l_item.hide) for l_item in node.layers}
            for l_item in node.layers:
                l_item.hide = (_layer_uid(l_item) != target_uid)
            _SOLO_LAYER_STATE[node_ptr] = {
                "solo_uid": target_uid,
                "saved_hide": saved_hide,
            }
            node.active_layer_index = idx

        _tag_view3d_gizmo_refresh(context)
        return {'FINISHED'}


class NODE_OT_tex_paint_viewport_shading(Operator):
    """Quickly switch 3D Viewport lighting for Texture Painting (Flat Albedo, Studio Lit, Material Preview, or Wireframe)"""
    bl_idname = "node.tex_paint_viewport_shading"
    bl_label = "Set Paint Viewport Shading"
    bl_options = {'REGISTER'}

    mode: EnumProperty(
        name="Shading Preset",
        items=(
            ('FLAT', "Flat (Unlit Albedo)", "View pure unlit texture colors without lighting shadows (ideal for hand-painted / base color work)"),
            ('STUDIO', "Studio Lit", "View texture colors with Studio lighting in Solid mode"),
            ('MATERIAL', "Material Preview (PBR)", "View full PBR shader channels (Base Color, Roughness, Metallic, Bump) in Material Preview"),
            ('WIREFRAME', "Toggle Wireframe", "Toggle wireframe overlay on the active model to inspect topology and UV seams while painting"),
        ),
        default='FLAT',
    )

    @classmethod
    def poll(cls, context):
        space = getattr(context, "space_data", None)
        return space is not None and space.type == 'VIEW_3D'

    def execute(self, context):
        space = context.space_data
        shading = getattr(space, "shading", None)
        overlay = getattr(space, "overlay", None)
        if shading is None:
            return {'CANCELLED'}

        if self.mode == 'FLAT':
            shading.type = 'SOLID'
            shading.light = 'FLAT'
            shading.color_type = 'TEXTURE'
        elif self.mode == 'STUDIO':
            shading.type = 'SOLID'
            shading.light = 'STUDIO'
            shading.color_type = 'TEXTURE'
        elif self.mode == 'MATERIAL':
            shading.type = 'MATERIAL'
        elif self.mode == 'WIREFRAME' and overlay is not None:
            overlay.show_wireframes = not overlay.show_wireframes

        return {'FINISHED'}


_PBR_CHANNEL_DEFS = (
    ('BASE_COLOR', "Base Color", "Base Color", 'COLOR', (0.75, 0.75, 0.75, 1.0), False),
    ('ROUGHNESS', "Roughness", "Roughness", 'MATSPHERE', (0.5, 0.5, 0.5, 1.0), True),
    ('METALLIC', "Metallic", "Metallic", 'SHADING_RENDERED', (0.0, 0.0, 0.0, 1.0), True),
    ('SPECULAR', "Specular", "Specular IOR Level", 'NODE_MATERIAL', (0.5, 0.5, 0.5, 1.0), True),
    ('NORMAL', "Normal", "Normal", 'NORMALS_VERTEX_FACE', (0.5, 0.5, 1.0, 1.0), True),
    ('BUMP', "Bump / Height", "Normal", 'MOD_NORMALEDIT', (0.5, 0.5, 0.5, 1.0), True),
    ('EMISSION', "Emission", "Emission Color", 'LIGHT_SUN', (0.0, 0.0, 0.0, 1.0), False),
    ('ALPHA', "Alpha / Opacity", "Alpha", 'IMAGE_ALPHA', (1.0, 1.0, 1.0, 1.0), True),
    ('DISPLACEMENT', "Displacement", "Displacement", 'MOD_DISPLACE', (0.5, 0.5, 0.5, 1.0), True),
)


def _detect_node_pbr_channel(node):
    """Return (channel_id, short_label, icon) for a ShaderNodeTexPaint or ShaderNodeTexImage based on its outgoing links or name."""
    if node is None:
        return ('CUSTOM', "Layer Canvas", 'NODE_TEXTURE')
    out_sock = node.outputs.get("Color")
    if out_sock and out_sock.is_linked:
        # First pass: check intermediate nodes and specific non-Base-Color sockets (so accidental dual Base Color links don't mask a Roughness/Bump node)
        has_base_color_link = False
        for link in out_sock.links:
            to_node = link.to_node
            to_sock = link.to_socket.name if link.to_socket else ""
            if to_node.bl_idname == "ShaderNodeBump":
                return ('BUMP', "Bump", 'MOD_NORMALEDIT')
            if to_node.bl_idname == "ShaderNodeNormalMap":
                return ('NORMAL', "Normal", 'NORMALS_VERTEX_FACE')
            if to_node.bl_idname in {"ShaderNodeDisplacement", "ShaderNodeOutputMaterial"}:
                return ('DISPLACEMENT', "Displacement", 'MOD_DISPLACE')
            if to_node.bl_idname == "ShaderNodeBsdfPrincipled":
                if to_sock == "Roughness":
                    return ('ROUGHNESS', "Roughness", 'MATSPHERE')
                if to_sock == "Metallic":
                    return ('METALLIC', "Metallic", 'SHADING_RENDERED')
                if to_sock in {"Specular IOR Level", "Specular"}:
                    return ('SPECULAR', "Specular", 'NODE_MATERIAL')
                if to_sock == "Normal":
                    return ('NORMAL', "Normal", 'NORMALS_VERTEX_FACE')
                if to_sock in {"Emission Color", "Emission"}:
                    return ('EMISSION', "Emission", 'LIGHT_SUN')
                if to_sock == "Alpha":
                    return ('ALPHA', "Alpha", 'IMAGE_ALPHA')
                if to_sock == "Base Color":
                    has_base_color_link = True
        if has_base_color_link:
            return ('BASE_COLOR', "Base Color", 'COLOR')
    alpha_sock = node.outputs.get("Alpha")
    if alpha_sock and alpha_sock.is_linked:
        for link in alpha_sock.links:
            to_node = link.to_node
            to_sock = link.to_socket.name if link.to_socket else ""
            if to_node.bl_idname == "ShaderNodeBsdfPrincipled" and to_sock == "Alpha":
                return ('ALPHA', "Alpha", 'IMAGE_ALPHA')
    label_str = (node.label or (node.image.name if node.image else node.name)).lower()
    norm_str = label_str.replace("_", "").replace(" ", "")
    for ch_id, ch_lbl, _sock, ch_icon, _col, _nc in _PBR_CHANNEL_DEFS:
        key = ch_id.lower().replace("_", "")
        short_key = ch_lbl.split(" ")[0].lower()
        if key in norm_str or short_key in norm_str:
            return (ch_id, ch_lbl.split(" /")[0], ch_icon)
    if norm_str == "paintedtexture":
        return ('BASE_COLOR', "Base Color", 'COLOR')
    return ('CUSTOM', node.label or (node.image.name if node.image else node.name), 'NODE_TEXTURE')


def _get_material_tex_paint_channels(mat):
    """Return list of (node, channel_id, label, icon) for all ShaderNodeTexPaint nodes in `mat`."""
    if mat is None or mat.node_tree is None:
        return []
    order_map = {ch[0]: idx for idx, ch in enumerate(_PBR_CHANNEL_DEFS)}
    result = []
    for n in mat.node_tree.nodes:
        if n.bl_idname == "ShaderNodeTexPaint":
            ch_id, lbl, icon = _detect_node_pbr_channel(n)
            result.append((n, ch_id, lbl, icon))
    result.sort(key=lambda item: (order_map.get(item[1], 99), item[0].name))
    return result


def _wire_tex_paint_node_to_pbr_channel(ntree, bsdf, target_node, ch_id, sock_name):
    """Ensure `target_node` (`ShaderNodeTexPaint`) is cleanly wired into `bsdf` / `Material Output` for `ch_id`."""
    if ntree is None or target_node is None:
        return
    out_color = target_node.outputs.get("Color")
    if out_color is None:
        return
    # Remove any accidental link from non-Base-Color channel to BSDF Base Color
    if ch_id != 'BASE_COLOR' and bsdf is not None and "Base Color" in bsdf.inputs:
        bc_sock = bsdf.inputs["Base Color"]
        for lk in list(ntree.links):
            if lk.from_node == target_node and lk.to_socket == bc_sock:
                ntree.links.remove(lk)

    if ch_id == 'BUMP' and bsdf is not None:
        bump_node = next((n for n in ntree.nodes if n.bl_idname == "ShaderNodeBump"), None)
        if bump_node is None:
            bump_node = ntree.nodes.new("ShaderNodeBump")
            bump_node.location = (bsdf.location.x - 170, bsdf.location.y - 340)
            bump_node.inputs["Distance"].default_value = 0.05
            bump_node.inputs["Strength"].default_value = 0.65
        ntree.links.new(out_color, bump_node.inputs["Height"])
        if "Normal" in bsdf.inputs:
            norm_sock = bsdf.inputs["Normal"]
            if norm_sock.is_linked and norm_sock.links[0].from_node.bl_idname == "ShaderNodeNormalMap":
                nmap_node = norm_sock.links[0].from_node
                ntree.links.new(nmap_node.outputs["Normal"], bump_node.inputs["Normal"])
            ntree.links.new(bump_node.outputs["Normal"], norm_sock)
    elif ch_id == 'NORMAL' and bsdf is not None:
        nmap_node = next((n for n in ntree.nodes if n.bl_idname == "ShaderNodeNormalMap"), None)
        if nmap_node is None:
            nmap_node = ntree.nodes.new("ShaderNodeNormalMap")
            nmap_node.location = (bsdf.location.x - 170, bsdf.location.y - 240)
        ntree.links.new(out_color, nmap_node.inputs["Color"])
        if "Normal" in bsdf.inputs:
            norm_sock = bsdf.inputs["Normal"]
            if norm_sock.is_linked and norm_sock.links[0].from_node.bl_idname == "ShaderNodeBump":
                bump_node = norm_sock.links[0].from_node
                ntree.links.new(nmap_node.outputs["Normal"], bump_node.inputs["Normal"])
            else:
                ntree.links.new(nmap_node.outputs["Normal"], norm_sock)
    elif ch_id == 'DISPLACEMENT':
        out_mat = next((n for n in ntree.nodes if n.bl_idname == "ShaderNodeOutputMaterial"), None)
        if out_mat is not None and "Displacement" in out_mat.inputs:
            disp_node = next((n for n in ntree.nodes if n.bl_idname == "ShaderNodeDisplacement"), None)
            if disp_node is None:
                disp_node = ntree.nodes.new("ShaderNodeDisplacement")
                disp_node.location = (out_mat.location.x - 200, out_mat.location.y - 180)
            ntree.links.new(out_color, disp_node.inputs["Height"])
            ntree.links.new(disp_node.outputs["Displacement"], out_mat.inputs["Displacement"])
    elif ch_id == 'EMISSION' and bsdf is not None:
        sock = bsdf.inputs.get("Emission Color") or bsdf.inputs.get("Emission")
        if sock is not None:
            ntree.links.new(out_color, sock)
        em_str = bsdf.inputs.get("Emission Strength")
        if em_str is not None and em_str.default_value <= 0.0:
            em_str.default_value = 1.0
    elif ch_id == 'SPECULAR' and bsdf is not None:
        sock = bsdf.inputs.get("Specular IOR Level") or bsdf.inputs.get("Specular")
        if sock is not None:
            ntree.links.new(out_color, sock)
    elif bsdf is not None:
        sock = bsdf.inputs.get(sock_name)
        if sock is not None:
            ntree.links.new(out_color, sock)


def _upgrade_flat_tex_image_to_layered(context, mat, ntree, flat_node, ch_def=None):
    """Convert a legacy flat ShaderNodeTexImage into a multi-layer ShaderNodeTexPaint in-place, preserving pixels and links."""
    import numpy as np
    if mat is None or ntree is None or flat_node is None or flat_node.bl_idname != "ShaderNodeTexImage":
        return None

    if ch_def is None:
        det_id, _det_lbl, _det_icon = _detect_node_pbr_channel(flat_node)
        ch_def = next((d for d in _PBR_CHANNEL_DEFS if d[0] == det_id), _PBR_CHANNEL_DEFS[0])
    ch_id, ch_label, sock_name, _icon, def_color, is_non_color = ch_def

    old_ima = flat_node.image
    w, h = 2048, 2048
    is_float = False
    old_pixels = None
    old_cs = None
    if old_ima is not None:
        try:
            old_cs = old_ima.colorspace_settings.name
        except Exception:
            old_cs = None
        if old_ima.size[0] > 0 and old_ima.size[1] > 0:
            w, h = int(old_ima.size[0]), int(old_ima.size[1])
            is_float = bool(getattr(old_ima, "is_float", False))
            try:
                old_pixels = np.empty(w * h * 4, dtype=np.float32)
                old_ima.pixels.foreach_get(old_pixels)
            except Exception:
                old_pixels = None

    loc = (float(flat_node.location.x), float(flat_node.location.y))
    in_links = []
    for in_s in flat_node.inputs:
        for lk in in_s.links:
            in_links.append((lk.from_socket, in_s.name))
    out_links = []
    for out_s in flat_node.outputs:
        for lk in out_s.links:
            out_links.append((out_s.name, lk.to_socket))

    short_suffix = ch_label.split(" ")[0]
    desired_name = f"{mat.name}_{short_suffix}"
    ntree.nodes.remove(flat_node)
    if old_ima is not None and old_ima.users == 0:
        try:
            bpy.data.images.remove(old_ima)
        except Exception:
            pass

    new_node = ntree.nodes.new("ShaderNodeTexPaint")
    new_node.label = ch_label.split(" /")[0]
    new_node.location = loc

    with context.temp_override(node=new_node):
        bpy.ops.node.tex_paint_canvas_new(
            name=desired_name,
            width=w,
            height=h,
            float=is_float,
            color=def_color,
        )

    if new_node.image is not None:
        if is_non_color or old_cs == 'Non-Color':
            try:
                new_node.image.colorspace_settings.name = 'Non-Color'
            except Exception:
                pass
        if old_pixels is not None and len(new_node.layers) > 0:
            base_layer = new_node.layers[0]
            try:
                c_layer = _C_TexPaintLayer.from_address(base_layer.as_pointer())
                if c_layer.ibuf:
                    c_imb = _C_ImBuf.from_address(c_layer.ibuf)
                    if int(c_imb.x) == w and int(c_imb.y) == h:
                        if c_imb.byte_data:
                            buf = (ctypes.c_uint8 * (w * h * 4)).from_address(c_imb.byte_data)
                            np.frombuffer(buf, dtype=np.uint8)[:] = np.clip(old_pixels * 255.0 + 0.5, 0.0, 255.0).astype(np.uint8)
                        elif c_imb.float_data:
                            buf = (ctypes.c_float * (w * h * 4)).from_address(c_imb.float_data)
                            np.frombuffer(buf, dtype=np.float32)[:] = old_pixels
                        c_layer.runtime_dirty = 1
                        base_layer.opacity = base_layer.opacity
            except Exception:
                pass

    for from_sock, in_name in in_links:
        dst_sock = new_node.inputs.get(in_name)
        if dst_sock is not None and from_sock is not None:
            try:
                ntree.links.new(from_sock, dst_sock)
            except Exception:
                pass

    if out_links:
        for out_name, to_sock in out_links:
            src_sock = new_node.outputs.get(out_name)
            if src_sock is not None and to_sock is not None:
                try:
                    ntree.links.new(src_sock, to_sock)
                except Exception:
                    pass
    else:
        bsdf = next((n for n in ntree.nodes if n.bl_idname == "ShaderNodeBsdfPrincipled"), None)
        _wire_tex_paint_node_to_pbr_channel(ntree, bsdf, new_node, ch_id, sock_name)

    return new_node


class NODE_OT_tex_paint_convert_slot_to_layers(Operator):
    """Convert a legacy flat Image Texture slot into a multi-layer Paint Canvas in-place, preserving its pixels and shader links"""
    bl_idname = "node.tex_paint_convert_slot_to_layers"
    bl_label = "Convert Flat Texture Slot to Layered Canvas"
    bl_options = {'REGISTER', 'UNDO'}

    slot_index: IntProperty(name="Slot Index", default=-1, options={'SKIP_SAVE'})
    convert_all: BoolProperty(name="Convert All Flat Slots", default=False, options={'SKIP_SAVE'})

    @classmethod
    def poll(cls, context):
        ob = getattr(context, "active_object", None) or getattr(context, "image_paint_object", None)
        mat = ob.active_material if (ob and ob.type == 'MESH') else None
        return mat is not None and mat.node_tree is not None

    def execute(self, context):
        ob = getattr(context, "active_object", None) or getattr(context, "image_paint_object", None)
        mat = ob.active_material if (ob and ob.type == 'MESH') else None
        if mat is None or mat.node_tree is None:
            return {'CANCELLED'}
        ntree = mat.node_tree

        flat_nodes = []
        if self.convert_all:
            flat_nodes = [n for n in ntree.nodes if n.bl_idname == "ShaderNodeTexImage"]
        else:
            idx = self.slot_index if self.slot_index >= 0 else mat.paint_active_slot
            target_ima = (
                mat.texture_paint_images[idx]
                if mat.texture_paint_images and 0 <= idx < len(mat.texture_paint_images)
                else None
            )
            if target_ima is not None:
                for n in ntree.nodes:
                    if n.bl_idname == "ShaderNodeTexImage" and n.image == target_ima:
                        flat_nodes.append(n)
                        break
            if not flat_nodes:
                fn, _fima = _find_active_flat_paint_slot_node(mat)
                if fn is not None:
                    flat_nodes.append(fn)
            if not flat_nodes:
                for n in ntree.nodes:
                    if n.bl_idname == "ShaderNodeTexImage":
                        flat_nodes.append(n)
                        break

        if not flat_nodes:
            self.report({'INFO'}, "All texture slots in this material are already multi-layer canvases")
            return {'CANCELLED'}

        last_new_node = None
        converted_count = 0
        for fn in list(flat_nodes):
            det_id, _det_lbl, _det_icon = _detect_node_pbr_channel(fn)
            existing_layered = next(
                (n for n, cid, _l, _i in _get_material_tex_paint_channels(mat) if cid == det_id and cid != 'CUSTOM'),
                None,
            )
            if existing_layered is not None:
                # A layered canvas for this exact channel already exists; remove the duplicate flat node and select the layered one!
                old_ima = fn.image
                ntree.nodes.remove(fn)
                if old_ima is not None and old_ima.users == 0:
                    try:
                        bpy.data.images.remove(old_ima)
                    except Exception:
                        pass
                bsdf = next((n for n in ntree.nodes if n.bl_idname == "ShaderNodeBsdfPrincipled"), None)
                ch_def = next((d for d in _PBR_CHANNEL_DEFS if d[0] == det_id), _PBR_CHANNEL_DEFS[0])
                _wire_tex_paint_node_to_pbr_channel(ntree, bsdf, existing_layered, ch_def[0], ch_def[2])
                last_new_node = existing_layered
                converted_count += 1
            else:
                new_node = _upgrade_flat_tex_image_to_layered(context, mat, ntree, fn)
                if new_node is not None:
                    last_new_node = new_node
                    converted_count += 1

        if last_new_node is not None:
            for n in ntree.nodes:
                n.select = (n == last_new_node)
            ntree.nodes.active = last_new_node
            if last_new_node.image is not None and hasattr(mat, "texture_paint_images"):
                for slot_i, slot_ima in enumerate(mat.texture_paint_images):
                    if slot_ima == last_new_node.image:
                        mat.paint_active_slot = slot_i
                        break

        _tag_view3d_gizmo_refresh(context)
        self.report({'INFO'}, f"Converted {converted_count} texture slot(s) to multi-layer canvas")
        return {'FINISHED'}


class NODE_OT_tex_paint_pbr_channel(Operator):
    """Switch to or create a layered PBR Material Channel (Base Color, Roughness, Metallic, Specular, Normal, Bump/Height, Emission, Alpha, Displacement)"""
    bl_idname = "node.tex_paint_pbr_channel"
    bl_label = "Select or Create PBR Paint Channel"
    bl_options = {'REGISTER', 'UNDO'}

    channel: EnumProperty(
        name="PBR Channel",
        items=(
            ('BASE_COLOR', "Base Color (Albedo)", "Create or switch to the Base Color / Albedo layered canvas", 'COLOR', 0),
            ('ROUGHNESS', "Roughness", "Create or switch to a Non-Color Roughness layered canvas (0.5 mid-grey base)", 'MATSPHERE', 1),
            ('METALLIC', "Metallic", "Create or switch to a Non-Color Metallic layered canvas (0.0 black base)", 'SHADING_RENDERED', 2),
            ('SPECULAR', "Specular IOR Level", "Create or switch to a Non-Color Specular IOR Level layered canvas (0.5 mid-grey base)", 'NODE_MATERIAL', 3),
            ('NORMAL', "Normal", "Create or switch to a Non-Color Normal Map layered canvas auto-wired through a Normal Map node", 'NORMALS_VERTEX_FACE', 4),
            ('BUMP', "Bump / Height", "Create or switch to a Non-Color Height/Bump layered canvas auto-wired through a Bump node", 'MOD_NORMALEDIT', 5),
            ('EMISSION', "Emission", "Create or switch to an Emission Color layered canvas", 'LIGHT_SUN', 6),
            ('ALPHA', "Alpha / Opacity", "Create or switch to an Opacity / Alpha layered canvas", 'IMAGE_ALPHA', 7),
            ('DISPLACEMENT', "Displacement", "Create or switch to a Non-Color Displacement layered canvas auto-wired to Material Output", 'MOD_DISPLACE', 8),
        ),
        default='BASE_COLOR',
    )
    node_name: StringProperty(name="Target Node Name", default="", options={'SKIP_SAVE'})

    @classmethod
    def poll(cls, context):
        ob = getattr(context, "active_object", None) or getattr(context, "image_paint_object", None)
        return ob is not None and ob.type == 'MESH'

    def execute(self, context):
        ob = getattr(context, "active_object", None) or getattr(context, "image_paint_object", None)
        if ob is None or ob.type != 'MESH':
            return {'CANCELLED'}

        mat = ob.active_material
        if mat is None:
            mat = bpy.data.materials.new(name=f"{ob.name}_Material")
            if ob.data.materials:
                ob.data.materials[0] = mat
            else:
                ob.data.materials.append(mat)
        if mat.node_tree is None:
            mat.use_nodes = True

        ntree = mat.node_tree
        if ntree is None:
            return {'CANCELLED'}

        ch_def = next((d for d in _PBR_CHANNEL_DEFS if d[0] == self.channel), _PBR_CHANNEL_DEFS[0])
        ch_id, ch_label, sock_name, _icon, def_color, is_non_color = ch_def

        # Find or create Principled BSDF
        bsdf = next((n for n in ntree.nodes if n.bl_idname == "ShaderNodeBsdfPrincipled"), None)
        if bsdf is None:
            bsdf = ntree.nodes.new("ShaderNodeBsdfPrincipled")
            bsdf.location = (0, 300)
            out_node = next((n for n in ntree.nodes if n.bl_idname == "ShaderNodeOutputMaterial"), None)
            if out_node is not None and "Surface" in out_node.inputs:
                ntree.links.new(bsdf.outputs["BSDF"], out_node.inputs["Surface"])

        target_node = None
        if self.node_name:
            cand = ntree.nodes.get(self.node_name)
            if cand and cand.bl_idname == "ShaderNodeTexPaint":
                target_node = cand

        if target_node is None:
            for n, cid, _lbl, _icon in _get_material_tex_paint_channels(mat):
                if cid == self.channel:
                    target_node = n
                    break

        # Check if any legacy flat ShaderNodeTexImage exists for this channel
        matching_flat = [
            n for n in ntree.nodes
            if n.bl_idname == "ShaderNodeTexImage" and _detect_node_pbr_channel(n)[0] == self.channel
        ]
        if target_node is not None and matching_flat:
            # Remove orphaned duplicate flat ShaderNodeTexImage nodes on the same channel so Texture Slots stays clean!
            for fn in matching_flat:
                old_ima = fn.image
                ntree.nodes.remove(fn)
                if old_ima is not None and old_ima.users == 0:
                    try:
                        bpy.data.images.remove(old_ima)
                    except Exception:
                        pass
            _wire_tex_paint_node_to_pbr_channel(ntree, bsdf, target_node, ch_id, sock_name)
        elif target_node is None and matching_flat:
            # Upgrade the existing flat ShaderNodeTexImage in-place to a multi-layer ShaderNodeTexPaint!
            target_node = _upgrade_flat_tex_image_to_layered(context, mat, ntree, matching_flat[0], ch_def=ch_def)
            for fn in matching_flat[1:]:
                ntree.nodes.remove(fn)

        if target_node is None:
            # Match resolution to existing canvas if present
            ref_node = _find_any_tex_paint_node(ntree)
            w = int(ref_node.canvas_width) if (ref_node and ref_node.image) else 2048
            h = int(ref_node.canvas_height) if (ref_node and ref_node.image) else 2048
            existing_count = len([n for n in ntree.nodes if n.bl_idname == "ShaderNodeTexPaint"])

            target_node = ntree.nodes.new("ShaderNodeTexPaint")
            target_node.label = ch_label.split(" /")[0]
            target_node.location = (bsdf.location.x - 380, bsdf.location.y - existing_count * 260)

            short_suffix = ch_label.split(" ")[0]
            with context.temp_override(node=target_node):
                bpy.ops.node.tex_paint_canvas_new(
                    name=f"{mat.name}_{short_suffix}",
                    width=w,
                    height=h,
                    color=def_color,
                )

            if target_node.image is not None and is_non_color:
                try:
                    target_node.image.colorspace_settings.name = 'Non-Color'
                except Exception:
                    pass

            _wire_tex_paint_node_to_pbr_channel(ntree, bsdf, target_node, ch_id, sock_name)

        primary_node = _canonical_mask_owner_node(target_node)
        if primary_node is not None and primary_node != target_node:
            _ensure_companion_node_layers_synced(context, mat, primary_node, target_node, self.channel)

        # If the user is currently on a Fill Layer and adds/clicks a PBR channel, enable that channel
        # on the Fill Layer and keep the unified Primary Node active so all channels stay in one panel!
        act_p_layer = primary_node.active_layer if primary_node is not None else None
        if act_p_layer is not None and act_p_layer.type == 'FILL' and primary_node is not None:
            _set_fill_channel_enabled(context, mat, primary_node, act_p_layer, self.channel, True)
            focus_node = primary_node
        else:
            focus_node = target_node

        # Activate focus_node in node_tree and sync paint_active_slot
        for n in ntree.nodes:
            n.select = (n == focus_node)
        ntree.nodes.active = focus_node

        if focus_node.image is not None and hasattr(mat, "texture_paint_images"):
            for slot_i, slot_ima in enumerate(mat.texture_paint_images):
                if slot_ima == focus_node.image:
                    mat.paint_active_slot = slot_i
                    break

        # If we added a non-Base-Color PBR channel while in Solid shading, switch to Material Preview so the artist sees it!
        if self.channel in {'ROUGHNESS', 'METALLIC', 'SPECULAR', 'NORMAL', 'BUMP', 'EMISSION', 'DISPLACEMENT'}:
            space = getattr(context, "space_data", None)
            if space and space.type == 'VIEW_3D' and getattr(space, "shading", None):
                if space.shading.type == 'SOLID':
                    space.shading.type = 'MATERIAL'

        _tag_view3d_gizmo_refresh(context)
        return {'FINISHED'}


class TEXPAINT_MT_add_pbr_channel(Menu):
    """Add or switch to a PBR Material Channel (Base Color, Roughness, Metallic, Specular, Normal, Bump/Height, Emission, Alpha, Displacement)"""
    bl_idname = "TEXPAINT_MT_add_pbr_channel"
    bl_label = "PBR Material Channels"

    def draw(self, _context):
        layout = self.layout
        for ch_id, ch_lbl, _sock, ch_icon, _col, _nc in _PBR_CHANNEL_DEFS:
            op = layout.operator("node.tex_paint_pbr_channel", text=ch_lbl, icon=ch_icon)
            op.channel = ch_id
            op.node_name = ""


# ---------------------------------------------------------------------------
# Substance Painter Multi-Channel Fill Layer Sync & Channel Toggles
# ---------------------------------------------------------------------------

_FILL_CHANNEL_PILLS = (
    ('BASE_COLOR', "Color", 'COLOR', True),
    ('ROUGHNESS', "Rough", 'MATSPHERE', True),
    ('METALLIC', "Metal", 'SHADING_RENDERED', True),
    ('BUMP', "Height", 'MOD_NORMALEDIT', True),
    ('EMISSION', "Emis", 'LIGHT_SUN', True),
    ('NORMAL', "Nrm", 'NORMALS_VERTEX_FACE', False),
    ('SPECULAR', "Spec", 'NODE_MATERIAL', False),
    ('ALPHA', "Alpha", 'IMAGE_ALPHA', False),
    ('DISPLACEMENT', "Disp", 'MOD_DISPLACE', False),
)

_FILL_CHANNEL_DEFAULT_RGB = {
    'BASE_COLOR': (0.80, 0.80, 0.80),
    'ROUGHNESS': (0.45, 0.45, 0.45),
    'METALLIC': (0.00, 0.00, 0.00),
    'BUMP': (0.50, 0.50, 0.50),
    'EMISSION': (1.00, 0.45, 0.10),
    'NORMAL': (0.50, 0.50, 1.00),
    'SPECULAR': (0.50, 0.50, 0.50),
    'ALPHA': (1.00, 1.00, 1.00),
    'DISPLACEMENT': (0.50, 0.50, 0.50),
}


def _get_material_for_node(node, context=None):
    if node is None or _is_standalone_2d_node(node):
        return None
    ntree = getattr(node, "id_data", None)
    if ntree is None:
        return None
    if context is not None:
        ob = getattr(context, "active_object", None) or getattr(context, "image_paint_object", None)
        if ob is not None and ob.type == 'MESH' and ob.active_material and ob.active_material.node_tree == ntree:
            return ob.active_material
    for m in bpy.data.materials:
        if m.node_tree == ntree:
            return m
    return None


def _get_material_channel_node(mat, ch_id):
    if mat is None or mat.node_tree is None:
        return None
    for n, cid, _lbl, _icon in _get_material_tex_paint_channels(mat):
        if cid == ch_id:
            return n
    return None


def _resolve_primary_node_and_layer(node, layer=None, context=None):
    """Return (mat, primary_node, primary_layer) for any channel node & layer in a 3D material."""
    if node is None:
        return None, None, None
    if layer is None:
        layer = getattr(node, "active_layer", None)
    mat = _get_material_for_node(node, context)
    if mat is None:
        return None, node, layer
    p_node = _canonical_mask_owner_node(node) or node
    if p_node == node or layer is None:
        return mat, p_node, layer
    p_layer = _find_layer_by_uid(p_node, _layer_uid(layer))
    if p_layer is None and 0 <= p_node.active_layer_index < len(p_node.layers):
        p_layer = p_node.layers[p_node.active_layer_index]
    return mat, p_node, p_layer


def _ensure_companion_node_layers_synced(context, mat, primary_node, comp_node, ch_id):
    """Ensure `comp_node` has the exact same layer stack (UIDs, order, names, flags, masks) as `primary_node`."""
    if primary_node is None or comp_node is None or primary_node == comp_node:
        return
    p_layers = list(primary_node.layers)
    if not p_layers:
        return
    orig_active_idx = int(primary_node.active_layer_index)

    # 1. Sync base layer UID if comp_node only has its initial Base layer
    if len(comp_node.layers) >= 1:
        c0 = _C_TexPaintLayer.from_address(comp_node.layers[0].as_pointer())
        p0_uid = _layer_uid(p_layers[0])
        if c0.uid != p0_uid and _find_layer_by_uid(comp_node, p0_uid) is None:
            c0.uid = p0_uid
            comp_node.layers[0].name = p_layers[0].name

    def_rgb = _FILL_CHANNEL_DEFAULT_RGB.get(ch_id, (0.5, 0.5, 0.5))

    # 2. Ensure every layer in primary_node exists in comp_node with matching UID, type, flags, and mask
    for idx, p_layer in enumerate(p_layers):
        p_uid = _layer_uid(p_layer)
        c_layer = _find_layer_by_uid(comp_node, p_uid)
        if c_layer is None:
            comp_node.active_layer_index = max(0, min(idx - 1, len(comp_node.layers) - 1))
            with context.temp_override(node=comp_node):
                bpy.ops.node.tex_paint_layer_add(type=p_layer.type)
            c_layer = comp_node.active_layer
            if c_layer is not None:
                c_struct = _C_TexPaintLayer.from_address(c_layer.as_pointer())
                c_struct.uid = p_uid
                c_layer.name = p_layer.name
                c_layer.opacity = p_layer.opacity
                c_layer.hide = p_layer.hide
                c_layer.use_clipping = p_layer.use_clipping
                if p_layer.type == 'FILL':
                    c_layer.fill_color = (def_rgb[0], def_rgb[1], def_rgb[2], 0.0)

        if c_layer is not None and getattr(p_layer, "use_mask", False):
            if not c_layer.use_mask:
                c_layer.use_mask = True
            c_layer.invert_mask = p_layer.invert_mask
            c_src = _C_TexPaintLayer.from_address(p_layer.as_pointer())
            c_dst = _C_TexPaintLayer.from_address(c_layer.as_pointer())
            if not c_dst.mask_ibuf:
                c_layer.opacity = c_layer.opacity
                c_dst = _C_TexPaintLayer.from_address(c_layer.as_pointer())
            if c_src.mask_ibuf and c_dst.mask_ibuf:
                imb_s = _C_ImBuf.from_address(c_src.mask_ibuf)
                imb_d = _C_ImBuf.from_address(c_dst.mask_ibuf)
                w, h = int(imb_s.x), int(imb_s.y)
                if int(imb_d.x) == w and int(imb_d.y) == h and w > 0 and h > 0:
                    if imb_s.byte_data and imb_d.byte_data:
                        ctypes.memmove(imb_d.byte_data, imb_s.byte_data, w * h * 4)
                        c_dst.runtime_dirty = 1
                    elif imb_s.float_data and imb_d.float_data:
                        ctypes.memmove(imb_d.float_data, imb_s.float_data, w * h * 16)
                        c_dst.runtime_dirty = 1

    if 0 <= orig_active_idx < len(primary_node.layers):
        primary_node.active_layer_index = orig_active_idx
    if 0 <= orig_active_idx < len(comp_node.layers):
        comp_node.active_layer_index = orig_active_idx
    if comp_node.active_layer is not None:
        comp_node.active_layer.opacity = comp_node.active_layer.opacity


def _ensure_material_pbr_channel_node(context, mat, primary_node, ch_id, keep_primary_active=True):
    """Find or create the `ShaderNodeTexPaint` node for `ch_id` in `mat`, wire it to Principled BSDF, and sync its layers."""
    if mat is None or mat.node_tree is None:
        return None
    if ch_id == 'BASE_COLOR' and primary_node is not None:
        return primary_node

    comp_node = _get_material_channel_node(mat, ch_id)
    if comp_node is None:
        bpy.ops.node.tex_paint_pbr_channel(channel=ch_id, node_name="")
        comp_node = _get_material_channel_node(mat, ch_id)

    if comp_node is not None and primary_node is not None and comp_node != primary_node:
        _ensure_companion_node_layers_synced(context, mat, primary_node, comp_node, ch_id)

    if keep_primary_active and primary_node is not None and mat.node_tree is not None:
        for n in mat.node_tree.nodes:
            n.select = (n == primary_node)
        mat.node_tree.nodes.active = primary_node
        if primary_node.image is not None and hasattr(mat, "texture_paint_images"):
            for slot_i, slot_ima in enumerate(mat.texture_paint_images):
                if slot_ima == primary_node.image:
                    mat.paint_active_slot = slot_i
                    break
    return comp_node


_CHANNEL_RNA_PROP_MAP = {
    'BASE_COLOR': "use_channel_color",
    'ROUGHNESS': "use_channel_roughness",
    'METALLIC': "use_channel_metallic",
    'BUMP': "use_channel_height",
    'HEIGHT': "use_channel_height",
    'NORMAL': "use_channel_normal",
    'EMISSION': "use_channel_emission",
    'SPECULAR': "use_channel_specular",
}


def _ensure_single_node_pbr_links(mat, node):
    """Ensure the single multi-channel `ShaderNodeTexPaint` node has all PBR outputs wired to Principled BSDF."""
    if mat is None or mat.node_tree is None or node is None or node.bl_idname != "ShaderNodeTexPaint":
        return
    if bool(node.get("_tex_paint_baked_set_active", False)):
        return
    ntree = mat.node_tree
    bsdf = next((n for n in ntree.nodes if n.bl_idname == "ShaderNodeBsdfPrincipled"), None)
    if bsdf is None:
        return
    socket_pairs = (
        ("Color", "Base Color"),
        ("Roughness", "Roughness"),
        ("Metallic", "Metallic"),
        ("Normal", "Normal"),
        ("Emission", "Emission Color"),
    )
    for out_name, in_name in socket_pairs:
        out_sock = node.outputs.get(out_name)
        in_sock = bsdf.inputs.get(in_name)
        if out_sock is not None and in_sock is not None:
            already_linked = any(lk.from_socket == out_sock for lk in in_sock.links)
            if not already_linked:
                try:
                    ntree.links.new(out_sock, in_sock)
                except Exception:
                    pass
    em_str = bsdf.inputs.get("Emission Strength")
    if em_str is not None and not em_str.is_linked and float(em_str.default_value) <= 0.0:
        em_str.default_value = 1.0


def _get_fill_channel_layer(mat, primary_node, primary_layer, ch_id):
    if primary_layer is None or primary_layer.type not in {'FILL', 'PAINT'}:
        return None
    return primary_layer


def _is_fill_channel_enabled(mat, primary_node, primary_layer, ch_id):
    if primary_layer is None or primary_layer.type not in {'FILL', 'PAINT'}:
        return False
    prop_name = _CHANNEL_RNA_PROP_MAP.get(ch_id)
    if prop_name and hasattr(primary_layer, prop_name):
        return bool(getattr(primary_layer, prop_name))
    return ch_id == 'BASE_COLOR'


def _set_fill_channel_enabled(context, mat, primary_node, primary_layer, ch_id, enabled):
    if primary_layer is None or primary_layer.type not in {'FILL', 'PAINT'}:
        return None
    prop_name = _CHANNEL_RNA_PROP_MAP.get(ch_id)
    if prop_name and hasattr(primary_layer, prop_name):
        setattr(primary_layer, prop_name, bool(enabled))
    if enabled and primary_node is not None:
        _ensure_single_node_pbr_links(mat, primary_node)
        if ch_id != 'BASE_COLOR' and context is not None:
            space = getattr(context, "space_data", None)
            if space and space.type == 'VIEW_3D' and getattr(space, "shading", None):
                if space.shading.type == 'SOLID':
                    space.shading.type = 'MATERIAL'
    return primary_layer


def _get_enabled_fill_channel_badges(mat, primary_node, primary_layer):
    """Return short badge string (e.g. 'C·R·M·H') for channels enabled on a Layer."""
    if primary_layer is None or primary_layer.type not in {'FILL', 'PAINT'}:
        return ""
    short_codes = (
        ('BASE_COLOR', "C"),
        ('ROUGHNESS', "R"),
        ('METALLIC', "M"),
        ('BUMP', "H"),
        ('NORMAL', "N"),
        ('EMISSION', "E"),
        ('SPECULAR', "S"),
    )
    active_codes = [code for cid, code in short_codes if _is_fill_channel_enabled(mat, primary_node, primary_layer, cid)]
    if primary_layer.type == 'PAINT' and active_codes == ["C"]:
        return "C"
    return "·".join(active_codes)


class NODE_OT_tex_paint_fill_channel_toggle(Operator):
    """Toggle individual PBR channels (Base Color, Roughness, Metallic, Height, Normal, Emission) ON or OFF on the active layer, or apply a Substance PBR Material Preset"""
    bl_idname = "node.tex_paint_fill_channel_toggle"
    bl_label = "Toggle Layer PBR Channel"
    bl_options = {'REGISTER', 'UNDO'}

    channel: EnumProperty(
        name="Channel",
        items=(
            ('BASE_COLOR', "Base Color", "Toggle Base Color (Albedo) on this layer", 'COLOR', 0),
            ('ROUGHNESS', "Roughness", "Toggle Roughness (0.0 Shiny to 1.0 Rough) on this layer", 'MATSPHERE', 1),
            ('METALLIC', "Metallic", "Toggle Metallic (0.0 Dielectric to 1.0 Metal) on this layer", 'SHADING_RENDERED', 2),
            ('BUMP', "Height", "Toggle Height / Bump (-1.0 Indented to +1.0 Raised, Blend: Add) on this layer", 'MOD_NORMALEDIT', 3),
            ('NORMAL', "Normal", "Toggle Normal channel (Flattens or replaces underlying height bump) on this layer", 'NORMALS_VERTEX_FACE', 4),
            ('EMISSION', "Emission", "Toggle Emission Glow on this layer", 'LIGHT_SUN', 5),
            ('SPECULAR', "Specular", "Toggle Specular IOR Level on this layer", 'NODE_MATERIAL', 6),
        ),
        default='ROUGHNESS',
    )
    action: EnumProperty(
        name="Action",
        items=(
            ('TOGGLE', "Toggle Channel", "Turn the specified channel ON or OFF on this layer"),
            ('ENABLE_CORE_PBR', "Enable Color + Rough + Metal + Height", "Turn on all 4 core Substance Painter PBR channels on this layer"),
            ('ONLY_COLOR', "Color Only", "Enable only Base Color and turn off other channels on this layer"),
            ('ONLY_HEIGHT', "Height Stamp Only (No Color)", "Turn off Base Color and enable only Height / Bump for pure surface carving or embossing"),
            ('PRESET_GOLD', "Preset: Pure Gold", "Metallic gold fill (Color + Roughness 0.18 + Metallic 1.0 + Height +0.05)"),
            ('PRESET_STEEL', "Preset: Brushed Steel", "Raw steel fill (Color + Roughness 0.28 + Metallic 1.0)"),
            ('PRESET_COPPER', "Preset: Burnished Copper", "Warm copper fill (Color + Roughness 0.24 + Metallic 1.0 + Height +0.02)"),
            ('PRESET_RUST', "Preset: Coarse Rust", "Matte oxidized rust fill (Color + Roughness 0.88 + Metallic 0.0 + Normal Flat)"),
            ('PRESET_PAINTED_METAL', "Preset: Glossy Paint Coat", "Glossy painted coat (Color + Roughness 0.20 + Metallic 0.0 + Height +0.08)"),
            ('PRESET_PLASTIC', "Preset: Smooth Plastic", "Smooth dielectric plastic (Color + Roughness 0.22 + Metallic 0.0)"),
            ('PRESET_RUBBER', "Preset: Matte Rubber", "Dark high-roughness rubber (Color + Roughness 0.85 + Metallic 0.0)"),
            ('PRESET_CARVE', "Preset: Carved Groove (-Height)", "Height-only indentation (-0.45 Height, Color & Roughness OFF)"),
            ('PRESET_EMBOSS', "Preset: Raised Emboss (+Height)", "Height-only raised detail (+0.45 Height, Color & Roughness OFF)"),
        ),
        default='TOGGLE',
        options={'SKIP_SAVE'},
    )

    @classmethod
    def poll(cls, context):
        node = tex_paint_node_from_context(context)
        layer = node.active_layer if node else None
        return layer is not None and layer.type in {'FILL', 'PAINT'}

    def execute(self, context):
        node = tex_paint_node_from_context(context)
        if node is None:
            return {'CANCELLED'}
        p_layer = node.active_layer
        if p_layer is None or p_layer.type not in {'FILL', 'PAINT'}:
            return {'CANCELLED'}
        mat = _get_material_for_node(node, context)

        if self.action == 'TOGGLE':
            cur_on = _is_fill_channel_enabled(mat, node, p_layer, self.channel)
            _set_fill_channel_enabled(context, mat, node, p_layer, self.channel, not cur_on)
            _tag_view3d_gizmo_refresh(context)
            return {'FINISHED'}

        all_chans = ('BASE_COLOR', 'ROUGHNESS', 'METALLIC', 'BUMP', 'NORMAL', 'EMISSION', 'SPECULAR')
        if self.action == 'ENABLE_CORE_PBR':
            for cid in all_chans:
                _set_fill_channel_enabled(context, mat, node, p_layer, cid, cid in {'BASE_COLOR', 'ROUGHNESS', 'METALLIC', 'BUMP'})
            _tag_view3d_gizmo_refresh(context)
            return {'FINISHED'}

        if self.action == 'ONLY_COLOR':
            for cid in all_chans:
                _set_fill_channel_enabled(context, mat, node, p_layer, cid, cid == 'BASE_COLOR')
            _tag_view3d_gizmo_refresh(context)
            return {'FINISHED'}

        if self.action in {'ONLY_HEIGHT', 'PRESET_CARVE', 'PRESET_EMBOSS'}:
            for cid in all_chans:
                _set_fill_channel_enabled(context, mat, node, p_layer, cid, cid == 'BUMP')
            if self.action == 'PRESET_CARVE':
                p_layer.fill_height = -0.45
            elif self.action == 'PRESET_EMBOSS':
                p_layer.fill_height = 0.45
            _tag_view3d_gizmo_refresh(context)
            return {'FINISHED'}

        presets = {
            'PRESET_GOLD': ((0.94, 0.74, 0.24), 0.18, 1.0, 0.05, False),
            'PRESET_STEEL': ((0.66, 0.70, 0.74), 0.28, 1.0, 0.00, False),
            'PRESET_COPPER': ((0.88, 0.42, 0.26), 0.24, 1.0, 0.02, False),
            'PRESET_RUST': ((0.35, 0.12, 0.05), 0.80, 0.0, 0.00, True),
            'PRESET_PAINTED_METAL': ((0.14, 0.36, 0.68), 0.20, 0.0, 0.08, False),
            'PRESET_PLASTIC': ((0.82, 0.16, 0.14), 0.22, 0.0, 0.00, False),
            'PRESET_RUBBER': ((0.09, 0.09, 0.10), 0.85, 0.0, 0.00, False),
        }
        if self.action in presets:
            col_rgb, rough_v, metal_v, height_v, flat_norm = presets[self.action]
            _set_fill_channel_enabled(context, mat, node, p_layer, 'BASE_COLOR', True)
            _set_fill_channel_enabled(context, mat, node, p_layer, 'ROUGHNESS', True)
            _set_fill_channel_enabled(context, mat, node, p_layer, 'METALLIC', metal_v > 0.001)
            _set_fill_channel_enabled(context, mat, node, p_layer, 'BUMP', abs(height_v) > 0.001)
            _set_fill_channel_enabled(context, mat, node, p_layer, 'NORMAL', flat_norm)
            p_layer.fill_color = (col_rgb[0], col_rgb[1], col_rgb[2], 1.0)
            p_layer.fill_roughness = rough_v
            p_layer.fill_metallic = metal_v
            p_layer.fill_height = height_v
            _tag_view3d_gizmo_refresh(context)
            return {'FINISHED'}

        return {'FINISHED'}


class TEXPAINT_MT_fill_channels_and_presets(Menu):
    """Toggle PBR channels (Base Color, Roughness, Metallic, Height, Normal, Emission, Specular) or apply 1-click Substance PBR Material Presets on the active layer"""
    bl_idname = "TEXPAINT_MT_fill_channels_and_presets"
    bl_label = "Layer Channels & PBR Presets"

    def draw(self, context):
        layout = self.layout
        node = tex_paint_node_from_context(context)
        p_layer = node.active_layer if node else None
        mat = _get_material_for_node(node, context) if node else None

        layout.label(text="Toggle Layer Channels (ON / OFF)", icon='RENDERLAYERS')
        channel_menu_items = (
            ('BASE_COLOR', "Base Color", 'COLOR'),
            ('ROUGHNESS', "Roughness", 'MATSPHERE'),
            ('METALLIC', "Metallic", 'SHADING_RENDERED'),
            ('BUMP', "Height (Blend: Add)", 'MOD_NORMALEDIT'),
            ('NORMAL', "Normal (Flat / Replace)", 'NORMALS_VERTEX_FACE'),
            ('EMISSION', "Emission", 'LIGHT_SUN'),
            ('SPECULAR', "Specular", 'NODE_MATERIAL'),
        )
        for cid, ch_lbl, ch_icon in channel_menu_items:
            is_on = _is_fill_channel_enabled(mat, node, p_layer, cid)
            op = layout.operator(
                "node.tex_paint_fill_channel_toggle",
                text=f"{ch_lbl} ({'ON' if is_on else 'OFF'})",
                icon='CHECKBOX_HLT' if is_on else ch_icon,
            )
            op.channel = cid
            op.action = 'TOGGLE'

        layout.separator()
        layout.operator("node.tex_paint_fill_channel_toggle", text="Enable Core PBR (Color + Rough + Metal + Height)", icon='SHADING_RENDERED').action = 'ENABLE_CORE_PBR'
        layout.operator("node.tex_paint_fill_channel_toggle", text="Height Stamp Only (Turn Off Color)", icon='MOD_NORMALEDIT').action = 'ONLY_HEIGHT'
        layout.operator("node.tex_paint_fill_channel_toggle", text="Color Only (Turn Off Other Channels)", icon='COLOR').action = 'ONLY_COLOR'

        layout.separator()
        layout.label(text="Substance PBR Material Presets", icon='MATERIAL')
        preset_items = (
            ('PRESET_GOLD', "Pure Gold (Metal + Shiny + +Height)", 'SOLO_ON'),
            ('PRESET_STEEL', "Brushed Steel (Metal + Semi-Rough)", 'SHADING_RENDERED'),
            ('PRESET_COPPER', "Burnished Copper (Metal + Warm)", 'COLOR'),
            ('PRESET_RUST', "Coarse Rust (Rust Color + Rough 0.8 + Normal Flat)", 'FORCE_TURBULENCE'),
            ('PRESET_PAINTED_METAL', "Glossy Paint Coat (Shiny + +Height)", 'BRUSH_DATA'),
            ('PRESET_PLASTIC', "Smooth Plastic (Glossy Dielectric)", 'MATSPHERE'),
            ('PRESET_RUBBER', "Matte Rubber (Dark Rough Dielectric)", 'MESH_CIRCLE'),
            ('PRESET_CARVE', "Carved Groove (Height -0.45 Only)", 'REMOVE'),
            ('PRESET_EMBOSS', "Raised Emboss (Height +0.45 Only)", 'ADD'),
        )
        for act_id, lbl, ic in preset_items:
            layout.operator("node.tex_paint_fill_channel_toggle", text=lbl, icon=ic).action = act_id


class NODE_OT_tex_paint_layer_pixel_action(Operator):
    """Perform fast pixel operations on the active layer (Fill, Clear, Invert, Flip, Rasterize, Copy Mask, Flatten)"""
    bl_idname = "node.tex_paint_layer_pixel_action"
    bl_label = "Layer Pixel Action"
    bl_options = {'REGISTER', 'UNDO'}

    action: EnumProperty(
        name="Action",
        items=(
            ('FILL_BRUSH', "Fill with Brush Color", "Fill the active layer with the current brush color (respecting Lock Alpha on Paint layers)"),
            ('CLEAR', "Clear to Transparent", "Clear the active Paint layer to 100% transparent"),
            ('INVERT_RGB', "Invert Layer Colors", "Invert the RGB colors of the active Paint or Fill layer"),
            ('FLIP_H', "Flip Layer Horizontally", "Mirror the active Paint layer horizontally"),
            ('FLIP_V', "Flip Layer Vertically", "Mirror the active Paint layer vertically"),
            ('RASTERIZE_TO_PAINT', "Rasterize to Paint Layer", "Convert a Fill layer or 3D Decal/Triplanar projection layer into a standard hand-paintable Paint layer"),
            ('COPY_MASK_BELOW', "Copy Mask from Layer Below", "Copy the mask buffer and Substance Mask Stack from the layer immediately below"),
            ('MERGE_VISIBLE', "Merge Visible Layers", "Merge all currently visible layers into a single Paint layer"),
            ('FLATTEN_ALL', "Flatten All Layers", "Flatten the entire layer stack into a single Base Paint layer"),
        ),
        default='FILL_BRUSH',
    )

    @classmethod
    def poll(cls, context):
        node = tex_paint_node_from_context(context)
        return node is not None and node.active_layer is not None

    def execute(self, context):
        import numpy as np
        node = tex_paint_node_from_context(context)
        layer = node.active_layer if node else None
        if node is None or layer is None:
            return {'CANCELLED'}

        col_rgb, _ = _get_current_brush_colors(context)
        if col_rgb is None:
            col_rgb = (1.0, 1.0, 1.0)

        if self.action == 'FILL_BRUSH':
            if layer.type == 'FILL':
                layer.fill_color = (col_rgb[0], col_rgb[1], col_rgb[2], layer.fill_color[3])
                return {'FINISHED'}
            if layer.type != 'PAINT':
                self.report({'WARNING'}, "Switch to a Paint or Fill layer to fill with color")
                return {'CANCELLED'}
            c_layer = _C_TexPaintLayer.from_address(layer.as_pointer())
            if not c_layer.ibuf:
                return {'CANCELLED'}
            c_imb = _C_ImBuf.from_address(c_layer.ibuf)
            w, h = int(c_imb.x), int(c_imb.y)
            lock_a = bool(layer.lock_alpha)
            if c_imb.byte_data:
                # Convert scene linear brush color to sRGB byte slightly approximated via gamma 1/2.2 or color management
                srgb = [int(round(min(max(pow(max(c, 0.0), 1.0 / 2.2), 0.0), 1.0) * 255.0)) for c in col_rgb[:3]]
                buf = (ctypes.c_uint8 * (w * h * 4)).from_address(c_imb.byte_data)
                arr = np.frombuffer(buf, dtype=np.uint8).reshape(-1, 4)
                arr[:, 0] = srgb[0]
                arr[:, 1] = srgb[1]
                arr[:, 2] = srgb[2]
                if not lock_a:
                    arr[:, 3] = 255
            elif c_imb.float_data:
                buf = (ctypes.c_float * (w * h * 4)).from_address(c_imb.float_data)
                arr = np.frombuffer(buf, dtype=np.float32).reshape(-1, 4)
                if lock_a:
                    a = arr[:, 3:4]
                    arr[:, 0:1] = col_rgb[0] * a
                    arr[:, 1:2] = col_rgb[1] * a
                    arr[:, 2:3] = col_rgb[2] * a
                else:
                    arr[:, 0] = col_rgb[0]
                    arr[:, 1] = col_rgb[1]
                    arr[:, 2] = col_rgb[2]
                    arr[:, 3] = 1.0
            c_layer.runtime_dirty = 1
            layer.opacity = layer.opacity
            return {'FINISHED'}

        if self.action == 'CLEAR':
            if layer.type != 'PAINT':
                return {'CANCELLED'}
            c_layer = _C_TexPaintLayer.from_address(layer.as_pointer())
            if not c_layer.ibuf:
                return {'CANCELLED'}
            c_imb = _C_ImBuf.from_address(c_layer.ibuf)
            w, h = int(c_imb.x), int(c_imb.y)
            if c_imb.byte_data:
                ctypes.memset(c_imb.byte_data, 0, w * h * 4)
            elif c_imb.float_data:
                ctypes.memset(c_imb.float_data, 0, w * h * 16)
            c_layer.runtime_dirty = 1
            layer.opacity = layer.opacity
            return {'FINISHED'}

        if self.action == 'INVERT_RGB':
            if layer.type == 'FILL':
                fc = layer.fill_color
                layer.fill_color = (1.0 - fc[0], 1.0 - fc[1], 1.0 - fc[2], fc[3])
                return {'FINISHED'}
            if layer.type != 'PAINT':
                return {'CANCELLED'}
            c_layer = _C_TexPaintLayer.from_address(layer.as_pointer())
            if not c_layer.ibuf:
                return {'CANCELLED'}
            c_imb = _C_ImBuf.from_address(c_layer.ibuf)
            w, h = int(c_imb.x), int(c_imb.y)
            if c_imb.byte_data:
                buf = (ctypes.c_uint8 * (w * h * 4)).from_address(c_imb.byte_data)
                arr = np.frombuffer(buf, dtype=np.uint8).reshape(-1, 4)
                arr[:, :3] = 255 - arr[:, :3]
            elif c_imb.float_data:
                buf = (ctypes.c_float * (w * h * 4)).from_address(c_imb.float_data)
                arr = np.frombuffer(buf, dtype=np.float32).reshape(-1, 4)
                arr[:, :3] = np.clip(arr[:, 3:4] - arr[:, :3], 0.0, 1.0)
            c_layer.runtime_dirty = 1
            layer.opacity = layer.opacity
            return {'FINISHED'}

        if self.action in {'FLIP_H', 'FLIP_V'}:
            if layer.type != 'PAINT':
                return {'CANCELLED'}
            c_layer = _C_TexPaintLayer.from_address(layer.as_pointer())
            if not c_layer.ibuf:
                return {'CANCELLED'}
            c_imb = _C_ImBuf.from_address(c_layer.ibuf)
            w, h = int(c_imb.x), int(c_imb.y)
            if c_imb.byte_data:
                buf = (ctypes.c_uint8 * (w * h * 4)).from_address(c_imb.byte_data)
                arr = np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 4)
                arr[:] = arr[:, ::-1, :].copy() if self.action == 'FLIP_H' else arr[::-1, :, :].copy()
            elif c_imb.float_data:
                buf = (ctypes.c_float * (w * h * 4)).from_address(c_imb.float_data)
                arr = np.frombuffer(buf, dtype=np.float32).reshape(h, w, 4)
                arr[:] = arr[:, ::-1, :].copy() if self.action == 'FLIP_H' else arr[::-1, :, :].copy()
            c_layer.runtime_dirty = 1
            layer.opacity = layer.opacity
            return {'FINISHED'}

        if self.action == 'RASTERIZE_TO_PAINT':
            if layer.type == 'PAINT' and getattr(layer, "proj_mode", 'NONE') != 'NONE':
                with context.temp_override(node=node):
                    bpy.ops.node.tex_paint_layer_bake_projection()
                return {'FINISHED'}
            if layer.type == 'FILL':
                fc = tuple(layer.fill_color)
                orig_name = layer.name
                orig_blend = layer.blend_mode
                orig_opacity = layer.opacity
                orig_clip = layer.use_clipping
                orig_use_mask = layer.use_mask
                orig_inv_mask = layer.invert_mask
                st_copy = dict(_get_layer_grunge_state(node, layer))
                mask_bytes = None
                c_old = _C_TexPaintLayer.from_address(layer.as_pointer())
                if orig_use_mask and c_old.mask_ibuf:
                    c_m = _C_ImBuf.from_address(c_old.mask_ibuf)
                    mw, mh = int(c_m.x), int(c_m.y)
                    if c_m.byte_data and mw > 0 and mh > 0:
                        mbuf = (ctypes.c_uint8 * (mw * mh * 4)).from_address(c_m.byte_data)
                        mask_bytes = np.frombuffer(mbuf, dtype=np.uint8).copy()

                with context.temp_override(node=node):
                    bpy.ops.node.tex_paint_layer_add(type='PAINT')
                new_layer = node.active_layer
                new_layer.name = orig_name
                new_layer.blend_mode = orig_blend
                new_layer.opacity = orig_opacity
                new_layer.use_clipping = orig_clip

                # Fill new paint layer with the fill_color
                c_new = _C_TexPaintLayer.from_address(new_layer.as_pointer())
                if c_new.ibuf:
                    c_imb = _C_ImBuf.from_address(c_new.ibuf)
                    w, h = int(c_imb.x), int(c_imb.y)
                    if c_imb.byte_data:
                        srgb = [int(round(min(max(pow(max(c, 0.0), 1.0 / 2.2), 0.0), 1.0) * 255.0)) for c in fc[:3]]
                        a_u8 = int(round(min(max(fc[3], 0.0), 1.0) * 255.0))
                        buf = (ctypes.c_uint8 * (w * h * 4)).from_address(c_imb.byte_data)
                        arr = np.frombuffer(buf, dtype=np.uint8).reshape(-1, 4)
                        arr[:, 0] = srgb[0]
                        arr[:, 1] = srgb[1]
                        arr[:, 2] = srgb[2]
                        arr[:, 3] = a_u8
                    c_new.runtime_dirty = 1

                if orig_use_mask:
                    new_layer.use_mask = True
                    new_layer.invert_mask = orig_inv_mask
                    _save_layer_grunge_state(node, new_layer, st_copy)
                    c_new = _C_TexPaintLayer.from_address(new_layer.as_pointer())
                    if mask_bytes is not None and c_new.mask_ibuf:
                        c_nm = _C_ImBuf.from_address(c_new.mask_ibuf)
                        if c_nm.byte_data:
                            dst_m = np.frombuffer(
                                (ctypes.c_uint8 * len(mask_bytes)).from_address(c_nm.byte_data),
                                dtype=np.uint8,
                            )
                            dst_m[:] = mask_bytes
                            c_new.runtime_dirty = 1

                # Remove old Fill layer (which is now immediately below new_layer)
                old_idx = node.active_layer_index - 1
                if 0 <= old_idx < len(node.layers):
                    node.active_layer_index = old_idx
                    with context.temp_override(node=node):
                        bpy.ops.node.tex_paint_layer_remove()
                    node.active_layer_index = old_idx
                if node.active_layer is not None:
                    node.active_layer.name = orig_name
                    node.active_layer.opacity = node.active_layer.opacity
                return {'FINISHED'}
            return {'CANCELLED'}

        if self.action == 'COPY_MASK_BELOW':
            idx = node.active_layer_index
            if idx <= 0 or idx >= len(node.layers):
                self.report({'WARNING'}, "No layer below to copy mask from")
                return {'CANCELLED'}
            below = node.layers[idx - 1]
            if not below.use_mask:
                self.report({'WARNING'}, "Layer below does not have a mask")
                return {'CANCELLED'}
            layer.use_mask = True
            layer.invert_mask = below.invert_mask
            c_below = _C_TexPaintLayer.from_address(below.as_pointer())
            c_cur = _C_TexPaintLayer.from_address(layer.as_pointer())
            if c_below.mask_ibuf and c_cur.mask_ibuf:
                cb_imb = _C_ImBuf.from_address(c_below.mask_ibuf)
                cc_imb = _C_ImBuf.from_address(c_cur.mask_ibuf)
                w, h = int(cb_imb.x), int(cb_imb.y)
                if cb_imb.byte_data and cc_imb.byte_data and int(cc_imb.x) == w and int(cc_imb.y) == h:
                    ctypes.memmove(cc_imb.byte_data, cb_imb.byte_data, w * h * 4)
                    c_cur.runtime_dirty = 1
            below_st = _get_layer_grunge_state(node, below)
            st_dup = json.loads(json.dumps(below_st))
            _save_layer_grunge_state(node, layer, st_dup)
            layer.opacity = layer.opacity
            _sync_layer_mask_to_companion_channels(node, layer)
            return {'FINISHED'}

        if self.action in {'MERGE_VISIBLE', 'FLATTEN_ALL'}:
            if node.image is None or len(node.layers) <= 1:
                return {'FINISHED'}
            w, h = int(node.canvas_width), int(node.canvas_height)
            comp_pixels = np.empty(w * h * 4, dtype=np.float32)
            node.image.pixels.foreach_get(comp_pixels)

            if self.action == 'FLATTEN_ALL':
                while len(node.layers) > 1:
                    node.active_layer_index = len(node.layers) - 1
                    with context.temp_override(node=node):
                        bpy.ops.node.tex_paint_layer_remove()
                base = node.active_layer
                if base.type != 'PAINT':
                    with context.temp_override(node=node):
                        bpy.ops.node.tex_paint_layer_add(type='PAINT')
                    node.active_layer_index = 0
                    with context.temp_override(node=node):
                        bpy.ops.node.tex_paint_layer_remove()
                    base = node.active_layer
                base.name = "Merged Base"
                base.use_mask = False
                base.use_clipping = False
                base.blend_mode = 'MIX'
                base.opacity = 1.0
                target_layer = base
            else:
                # MERGE_VISIBLE: remove all visible layers except the bottom-most visible one (or create a Paint layer)
                vis_indices = [i for i, l in enumerate(node.layers) if not l.hide]
                if len(vis_indices) <= 1:
                    return {'FINISHED'}
                for i in reversed(vis_indices):
                    node.active_layer_index = i
                    with context.temp_override(node=node):
                        bpy.ops.node.tex_paint_layer_remove()
                with context.temp_override(node=node):
                    bpy.ops.node.tex_paint_layer_add(type='PAINT')
                target_layer = node.active_layer
                target_layer.name = "Merged Visible"

            c_t = _C_TexPaintLayer.from_address(target_layer.as_pointer())
            if c_t.ibuf:
                c_imb = _C_ImBuf.from_address(c_t.ibuf)
                if c_imb.byte_data:
                    buf = (ctypes.c_uint8 * (w * h * 4)).from_address(c_imb.byte_data)
                    dst = np.frombuffer(buf, dtype=np.uint8).reshape(-1)
                    dst[:] = (np.clip(comp_pixels, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)
                elif c_imb.float_data:
                    buf = (ctypes.c_float * (w * h * 4)).from_address(c_imb.float_data)
                    dst = np.frombuffer(buf, dtype=np.float32).reshape(-1)
                    dst[:] = comp_pixels
                c_t.runtime_dirty = 1
                target_layer.opacity = target_layer.opacity
            return {'FINISHED'}

        return {'CANCELLED'}


class NODE_OT_tex_paint_export_texture_set(Operator):
    """Bake & export the Multilayer Texture node into standard PBR texture maps (BaseColor, Roughness, Metallic, Normal, Height, ORM, Emission) on disk and/or wire standard Image Texture nodes into the material"""
    bl_idname = "node.tex_paint_export_texture_set"
    bl_label = "Export / Bake PBR Texture Set"
    bl_options = {'REGISTER', 'UNDO'}

    mode: EnumProperty(
        name="Output Mode",
        items=(
            (
                'BOTH',
                "Save Files + Create Material Nodes",
                "Export PNG textures to disk AND wire standard Image Texture nodes into Principled BSDF (ready for FBX / glTF export)",
            ),
            (
                'NODES_ONLY',
                "Create Blender Texture Set Nodes Only",
                "Unpack into regular Blender Image datablocks and connect standard Image Texture nodes to Principled BSDF",
            ),
            (
                'FILES_ONLY',
                "Export Texture Files to Folder Only",
                "Save PBR texture maps to disk while keeping the live Multilayer Texture node connected",
            ),
        ),
        default='BOTH',
    )
    export_dir: StringProperty(
        name="Export Folder",
        description="Folder where exported PNG texture maps are written",
        default="//textures/",
        subtype='DIR_PATH',
    )
    prefix: StringProperty(
        name="Texture Prefix",
        description="Filename and datablock prefix for the exported PBR texture set",
        default="",
    )
    normal_convention: EnumProperty(
        name="Normal Format",
        items=(
            ('OPENGL', "OpenGL (+Y) — Blender / glTF / Unity", "Standard right-handed tangent-space normal map (+Y green channel)"),
            ('DIRECTX', "DirectX (-Y) — Unreal Engine", "Inverted green channel (-Y) tangent-space normal map"),
        ),
        default='OPENGL',
    )
    export_base_color: BoolProperty(name="Base Color", default=True)
    export_roughness: BoolProperty(name="Roughness", default=True)
    export_metallic: BoolProperty(name="Metallic", default=True)
    export_normal: BoolProperty(name="Normal Map (from Height)", default=True)
    export_height: BoolProperty(name="Height (Displacement)", default=False)
    export_orm: BoolProperty(
        name="Packed ORM (R=AO, G=Rough, B=Metal)",
        description="Pack Ambient Occlusion (1.0), Roughness, and Metallic into a single RGB map for Unreal / glTF",
        default=False,
    )
    export_emission: BoolProperty(name="Emission", default=True)

    @classmethod
    def poll(cls, context):
        node = tex_paint_node_from_context(context)
        return node is not None and getattr(node, "image", None) is not None

    def invoke(self, context, _event):
        node = tex_paint_node_from_context(context)
        ob = getattr(context, "active_object", None)
        mat = ob.active_material if ob else None
        if not self.prefix:
            raw_name = mat.name if mat else (node.image.name if node and node.image else "Material")
            self.prefix = "".join(c if (c.isalnum() or c in "_-") else "_" for c in raw_name).strip("_") or "Material"
        if not bpy.data.is_saved and self.export_dir.startswith("//"):
            import tempfile
            self.export_dir = os.path.join(tempfile.gettempdir(), f"{self.prefix}_textures")
        wm = getattr(context, "window_manager", None)
        if wm:
            return wm.invoke_props_dialog(self, width=380)
        return self.execute(context)

    def draw(self, _context):
        layout = self.layout
        col = layout.column(align=True)
        col.prop(self, "mode", text="Target")
        col.prop(self, "prefix", text="Set Name")
        if self.mode in {'BOTH', 'FILES_ONLY'}:
            col.prop(self, "export_dir", text="Folder")
        col.prop(self, "normal_convention", text="Normals")

        layout.separator(factor=0.4)
        box = layout.box()
        bcol = box.column(align=True)
        bcol.label(text="Channels to Generate:", icon='TEXTURE')
        r1 = bcol.row(align=True)
        r1.prop(self, "export_base_color", toggle=True)
        r1.prop(self, "export_roughness", toggle=True)
        r1.prop(self, "export_metallic", toggle=True)
        r2 = bcol.row(align=True)
        r2.prop(self, "export_normal", toggle=True)
        r2.prop(self, "export_height", toggle=True)
        r2.prop(self, "export_emission", toggle=True)
        bcol.prop(self, "export_orm", toggle=True, icon=' NODE_COMPOSITING' if False else 'RENDERLAYERS')

    def execute(self, context):
        import numpy as np

        node = tex_paint_node_from_context(context)
        if node is None or node.image is None:
            self.report({'WARNING'}, "No active Multilayer Texture Canvas found")
            return {'CANCELLED'}

        ob = getattr(context, "active_object", None)
        mat = (ob.active_material if ob else None) or _get_material_for_node(node)

        # Force a fresh composite by touching active layer opacity
        if node.active_layer is not None:
            node.active_layer.opacity = node.active_layer.opacity

        w = int(node.canvas_width or node.image.size[0] or 1024)
        h = int(node.canvas_height or node.image.size[1] or 1024)
        prefix = (self.prefix or (mat.name if mat else node.image.name)).strip() or "Material"

        # Read composited Base Color (RGBA)
        color_arr = np.zeros((h, w, 4), dtype=np.float32)
        if node.image and len(node.image.pixels) == w * h * 4:
            node.image.pixels.foreach_get(color_arr.ravel())

        # Read composited Packed PBR (R=Roughness, G=Metallic, B=Height, A=Specular)
        pbr_ima = getattr(node, "pbr_image", None)
        pbr_arr = np.zeros((h, w, 4), dtype=np.float32)
        pbr_arr[:, :, 0] = 0.5
        pbr_arr[:, :, 2] = 0.5
        pbr_arr[:, :, 3] = 0.5
        if pbr_ima and len(pbr_ima.pixels) == w * h * 4:
            pbr_ima.pixels.foreach_get(pbr_arr.ravel())

        # Read composited Emission (RGBA)
        em_ima = getattr(node, "emission_image", None)
        em_arr = np.zeros((h, w, 4), dtype=np.float32)
        has_any_emission = any(getattr(l, "use_channel_emission", False) for l in node.layers)
        if em_ima and len(em_ima.pixels) == w * h * 4:
            em_ima.pixels.foreach_get(em_arr.ravel())

        def _make_or_update_image(img_name, rgba_float, is_data=False):
            ima = bpy.data.images.get(img_name)
            if ima is not None and (ima.size[0] != w or ima.size[1] != h):
                bpy.data.images.remove(ima)
                ima = None
            if ima is None:
                ima = bpy.data.images.new(name=img_name, width=w, height=h, alpha=True, float_buffer=False)
            try:
                ima.colorspace_settings.name = 'Non-Color' if is_data else 'sRGB'
            except Exception:
                pass
            ima.pixels.foreach_set(np.clip(rgba_float, 0.0, 1.0).astype(np.float32).ravel())
            ima.update()
            return ima

        generated_images = {}

        if self.export_base_color:
            generated_images["BaseColor"] = _make_or_update_image(f"{prefix}_BaseColor", color_arr, is_data=False)

        if self.export_roughness:
            r_rgba = np.ones((h, w, 4), dtype=np.float32)
            r_rgba[:, :, 0] = pbr_arr[:, :, 0]
            r_rgba[:, :, 1] = pbr_arr[:, :, 0]
            r_rgba[:, :, 2] = pbr_arr[:, :, 0]
            generated_images["Roughness"] = _make_or_update_image(f"{prefix}_Roughness", r_rgba, is_data=True)

        if self.export_metallic:
            m_rgba = np.ones((h, w, 4), dtype=np.float32)
            m_rgba[:, :, 0] = pbr_arr[:, :, 1]
            m_rgba[:, :, 1] = pbr_arr[:, :, 1]
            m_rgba[:, :, 2] = pbr_arr[:, :, 1]
            generated_images["Metallic"] = _make_or_update_image(f"{prefix}_Metallic", m_rgba, is_data=True)

        if self.export_height:
            h_rgba = np.ones((h, w, 4), dtype=np.float32)
            h_rgba[:, :, 0] = pbr_arr[:, :, 2]
            h_rgba[:, :, 1] = pbr_arr[:, :, 2]
            h_rgba[:, :, 2] = pbr_arr[:, :, 2]
            generated_images["Height"] = _make_or_update_image(f"{prefix}_Height", h_rgba, is_data=True)

        if self.export_normal:
            h_field = pbr_arr[:, :, 2]
            bump_str = float(getattr(node, "bump_strength", 1.0)) * 8.0
            dh_dx = (np.roll(h_field, -1, axis=1) - np.roll(h_field, 1, axis=1)) * 0.5 * bump_str
            dh_dy = (np.roll(h_field, -1, axis=0) - np.roll(h_field, 1, axis=0)) * 0.5 * bump_str
            nx = -dh_dx
            ny = -dh_dy if self.normal_convention == 'OPENGL' else dh_dy
            nz = np.ones_like(h_field)
            inv_len = 1.0 / np.maximum(np.sqrt(nx * nx + ny * ny + nz * nz), 1e-6)
            n_rgba = np.ones((h, w, 4), dtype=np.float32)
            n_rgba[:, :, 0] = (nx * inv_len) * 0.5 + 0.5
            n_rgba[:, :, 1] = (ny * inv_len) * 0.5 + 0.5
            n_rgba[:, :, 2] = (nz * inv_len) * 0.5 + 0.5
            generated_images["Normal"] = _make_or_update_image(f"{prefix}_Normal", n_rgba, is_data=True)

        if self.export_orm:
            orm_rgba = np.ones((h, w, 4), dtype=np.float32)
            orm_rgba[:, :, 0] = 1.0
            orm_rgba[:, :, 1] = pbr_arr[:, :, 0]
            orm_rgba[:, :, 2] = pbr_arr[:, :, 1]
            generated_images["ORM"] = _make_or_update_image(f"{prefix}_ORM", orm_rgba, is_data=True)

        if self.export_emission and has_any_emission:
            em_out = em_arr.copy()
            em_out[:, :, 3] = 1.0
            generated_images["Emission"] = _make_or_update_image(f"{prefix}_Emission", em_out, is_data=False)

        saved_paths = []
        if self.mode in {'BOTH', 'FILES_ONLY'}:
            out_dir = bpy.path.abspath(self.export_dir)
            if not out_dir or out_dir.startswith("//"):
                import tempfile
                out_dir = os.path.join(tempfile.gettempdir(), f"{prefix}_textures")
            os.makedirs(out_dir, exist_ok=True)
            for ch_key, ima in generated_images.items():
                fpath = os.path.join(out_dir, f"{prefix}_{ch_key}.png")
                ima.filepath_raw = fpath
                ima.file_format = 'PNG'
                ima.save()
                saved_paths.append(fpath)

        if self.mode in {'BOTH', 'NODES_ONLY'} and mat is not None and mat.use_nodes and mat.node_tree is not None:
            ntree = mat.node_tree
            bsdf = next((n for n in ntree.nodes if n.bl_idname == "ShaderNodeBsdfPrincipled"), None)
            if bsdf is not None:
                node["_tex_paint_baked_set_active"] = True
                # Remove existing generated texture set nodes from previous bakes
                for old_n in [n for n in ntree.nodes if n.get("_tex_paint_exported_set_node")]:
                    ntree.nodes.remove(old_n)

                base_x = node.location.x
                base_y = node.location.y - 340.0
                row_idx = 0

                def _spawn_tex_node(label, ima):
                    nonlocal row_idx
                    tn = ntree.nodes.new("ShaderNodeTexImage")
                    tn.name = f"TexSet_{label}"
                    tn.label = f"{prefix} {label}"
                    tn.image = ima
                    tn["_tex_paint_exported_set_node"] = True
                    tn.location = (base_x, base_y - row_idx * 260.0)
                    row_idx += 1
                    return tn

                if "BaseColor" in generated_images:
                    tn_c = _spawn_tex_node("Base Color", generated_images["BaseColor"])
                    sock_bc = bsdf.inputs.get("Base Color")
                    if sock_bc:
                        ntree.links.new(tn_c.outputs["Color"], sock_bc)

                if "Roughness" in generated_images:
                    tn_r = _spawn_tex_node("Roughness", generated_images["Roughness"])
                    sock_r = bsdf.inputs.get("Roughness")
                    if sock_r:
                        ntree.links.new(tn_r.outputs["Color"], sock_r)

                if "Metallic" in generated_images:
                    tn_m = _spawn_tex_node("Metallic", generated_images["Metallic"])
                    sock_m = bsdf.inputs.get("Metallic")
                    if sock_m:
                        ntree.links.new(tn_m.outputs["Color"], sock_m)

                if "Normal" in generated_images:
                    tn_n = _spawn_tex_node("Normal", generated_images["Normal"])
                    nmap = ntree.nodes.new("ShaderNodeNormalMap")
                    nmap.label = f"{prefix} Normal Map"
                    nmap["_tex_paint_exported_set_node"] = True
                    nmap.location = (base_x + 260.0, tn_n.location.y)
                    ntree.links.new(tn_n.outputs["Color"], nmap.inputs["Color"])
                    sock_n = bsdf.inputs.get("Normal")
                    if sock_n:
                        ntree.links.new(nmap.outputs["Normal"], sock_n)

                if "Emission" in generated_images:
                    tn_e = _spawn_tex_node("Emission", generated_images["Emission"])
                    sock_e = bsdf.inputs.get("Emission Color") or bsdf.inputs.get("Emission")
                    if sock_e:
                        ntree.links.new(tn_e.outputs["Color"], sock_e)
                    sock_es = bsdf.inputs.get("Emission Strength")
                    if sock_es and not sock_es.is_linked and sock_es.default_value == 0.0:
                        sock_es.default_value = 1.0

        ch_names = ", ".join(generated_images.keys())
        if saved_paths:
            self.report({'INFO'}, f"Exported {len(saved_paths)} PBR maps ({ch_names}) to {os.path.dirname(saved_paths[0])}")
        else:
            self.report({'INFO'}, f"Created standard PBR Texture Set nodes ({ch_names}) in '{mat.name if mat else prefix}'")
        return {'FINISHED'}


class NODE_OT_tex_paint_reconnect_live_layers(Operator):
    """Switch the material back from baked Image Texture nodes to the live Multilayer Texture node"""
    bl_idname = "node.tex_paint_reconnect_live_layers"
    bl_label = "Reconnect Live Paint Layers"
    bl_options = {'REGISTER', 'UNDO'}

    remove_baked_nodes: BoolProperty(
        name="Remove Baked Image Nodes",
        default=True,
    )

    @classmethod
    def poll(cls, context):
        return tex_paint_node_from_context(context) is not None

    def execute(self, context):
        node = tex_paint_node_from_context(context)
        ob = getattr(context, "active_object", None)
        mat = (ob.active_material if ob else None) or _get_material_for_node(node)
        if node is None or mat is None or not mat.use_nodes or mat.node_tree is None:
            return {'CANCELLED'}
        if "_tex_paint_baked_set_active" in node:
            del node["_tex_paint_baked_set_active"]
        if self.remove_baked_nodes:
            for old_n in [n for n in mat.node_tree.nodes if n.get("_tex_paint_exported_set_node")]:
                mat.node_tree.nodes.remove(old_n)
        _ensure_single_node_pbr_links(mat, node)
        self.report({'INFO'}, "Reconnected live Multilayer Texture node to Principled BSDF")
        return {'FINISHED'}


class NODE_OT_tex_paint_make_material_unique(Operator):
    """Re-bake all shared modular models onto the shared material, or split the shared material into independent per-part materials"""
    bl_idname = "node.tex_paint_make_material_unique"
    bl_label = "Modular Shared Material Action"
    bl_options = {'REGISTER', 'UNDO'}

    mode: EnumProperty(
        name="Action",
        items=(
            ('REBAKE_ALL', "Re-Bake All Shared Models", "Re-evaluate all Mask Generators and 3D Projections across every mesh object sharing this material"),
            ('ACTIVE_OBJECT', "Make Material Unique (Active Part)", "Duplicate this material and its canvas images for the active object so this part has its own independent material"),
            ('ALL_SHARED_OBJECTS', "Split Material Across All Parts", "Give each modular mesh object sharing this material its own unique material & canvas copy and re-bake each part"),
        ),
        default='REBAKE_ALL',
    )

    @classmethod
    def poll(cls, context):
        return tex_paint_node_from_context(context) is not None

    @staticmethod
    def _rebake_node_layers(node, context):
        if node is None:
            return
        for l_item in node.layers:
            if getattr(l_item, "use_mask", False):
                st = _get_layer_grunge_state(node, l_item)
                if st.get("effects"):
                    _evaluate_layer_grunge_mask(node, l_item, context=context, force_reproject=True, skip_absorb=True)
            if l_item.type in {'PAINT', 'FILL'} and getattr(l_item, "proj_mode", 'NONE') in _3D_GIZMO_PROJ_MODES:
                _reproject_color_layer_multi_object(node, l_item, context=context, force=True)

    @staticmethod
    def _clone_material_for_object(src_mat, target_ob, context):
        new_mat = src_mat.copy()
        new_mat.name = f"{src_mat.name}_{target_ob.name}"
        if new_mat.node_tree is not None:
            for n in new_mat.node_tree.nodes:
                if n.bl_idname == "ShaderNodeTexPaint" and n.image is not None:
                    new_ima = n.image.copy()
                    new_ima.name = f"{new_mat.name}_BaseColor"
                    new_ima.use_fake_user = True
                    n_ptr = n.as_pointer()
                    # Assign bNode::id (offset 216) to the new per-part master image
                    ctypes.c_void_p.from_address(n_ptr + 216).value = new_ima.as_pointer()
                    # Clear aux image pointers in NodeTexPaint (offsets 1072, 1080) so C++ allocates fresh per-part PBR/Emission images
                    c_storage_ptr = ctypes.c_void_p.from_address(n_ptr + 224).value
                    if c_storage_ptr:
                        ctypes.c_void_p.from_address(c_storage_ptr + 1072).value = 0
                        ctypes.c_void_p.from_address(c_storage_ptr + 1080).value = 0
                    if len(n.layers) > 0:
                        n.layers[0].opacity = n.layers[0].opacity
        for slot in target_ob.material_slots:
            if slot.material == src_mat:
                slot.material = new_mat
        if new_mat.node_tree is not None:
            for n in new_mat.node_tree.nodes:
                if n.bl_idname == "ShaderNodeTexPaint":
                    NODE_OT_tex_paint_make_material_unique._rebake_node_layers(n, context)
        return new_mat

    def execute(self, context):
        node = tex_paint_node_from_context(context)
        if node is None:
            return {'CANCELLED'}
        mat = _get_material_for_node(node, context)
        sharing_obs, _first_c = _find_all_mesh_objects_for_node(node, context)

        if self.mode == 'REBAKE_ALL' or mat is None or len(sharing_obs) <= 1:
            self._rebake_node_layers(node, context)
            _tag_view3d_gizmo_refresh(context)
            self.report({'INFO'}, f"Re-baked Generators & 3D Projections across {max(len(sharing_obs), 1)} model(s)")
            return {'FINISHED'}

        active_ob = getattr(context, "active_object", None) or sharing_obs[-1]
        if self.mode == 'ACTIVE_OBJECT' and active_ob in sharing_obs:
            new_mat = self._clone_material_for_object(mat, active_ob, context)
            self._rebake_node_layers(node, context)
            _tag_view3d_gizmo_refresh(context)
            self.report({'INFO'}, f"Created unique material '{new_mat.name}' for '{active_ob.name}'")
            return {'FINISHED'}

        if self.mode == 'ALL_SHARED_OBJECTS':
            # Keep the original material on the first object, and clone unique materials for all other objects
            for ob_part in sharing_obs[1:]:
                self._clone_material_for_object(mat, ob_part, context)
            self._rebake_node_layers(node, context)
            _tag_view3d_gizmo_refresh(context)
            self.report({'INFO'}, f"Split shared material across {len(sharing_obs)} modular objects")
            return {'FINISHED'}

        return {'CANCELLED'}


# ---------------------------------------------------------------------------
# Substance-Painter-Style Mesh Maps & High-Poly to Low-Poly Baker Hub
# ---------------------------------------------------------------------------

def _apply_baked_normal_map_to_shader(mat, node, normal_ima):
    """Wire the baked Tangent-Space Normal Map (`normal_ima`) into `mat`'s Principled BSDF Normal input
    (chaining cleanly with any existing Height/Bump node).
    """
    if mat is None or mat.node_tree is None or normal_ima is None:
        return False
    ntree = mat.node_tree
    bsdf = None
    for n in ntree.nodes:
        if n.bl_idname == "ShaderNodeBsdfPrincipled":
            bsdf = n
            break
    if bsdf is None:
        return False

    try:
        normal_ima.colorspace_settings.name = 'Non-Color'
    except Exception:
        pass

    tex_node = ntree.nodes.get("TexPaint_BakedNormal_Tex")
    if tex_node is None or tex_node.bl_idname != "ShaderNodeTexImage":
        tex_node = ntree.nodes.new("ShaderNodeTexImage")
        tex_node.name = "TexPaint_BakedNormal_Tex"
        tex_node.label = "Baked Mesh Normal Map"
        tex_node.location = (bsdf.location.x - 680, bsdf.location.y - 420)
    tex_node.image = normal_ima

    nmap_node = ntree.nodes.get("TexPaint_BakedNormal_Map")
    if nmap_node is None or nmap_node.bl_idname != "ShaderNodeNormalMap":
        nmap_node = ntree.nodes.new("ShaderNodeNormalMap")
        nmap_node.name = "TexPaint_BakedNormal_Map"
        nmap_node.label = "Baked Normal Map"
        nmap_node.location = (bsdf.location.x - 380, bsdf.location.y - 420)

    ntree.links.new(tex_node.outputs["Color"], nmap_node.inputs["Color"])

    norm_in = bsdf.inputs.get("Normal")
    if norm_in is not None:
        # If a Bump node is already connected to Principled BSDF Normal, feed Baked Normal into Bump.Normal!
        existing_bump = None
        if norm_in.is_linked:
            for lk in norm_in.links:
                if lk.from_node.bl_idname == "ShaderNodeBump":
                    existing_bump = lk.from_node
                    break
        if existing_bump is not None and "Normal" in existing_bump.inputs:
            ntree.links.new(nmap_node.outputs["Normal"], existing_bump.inputs["Normal"])
        else:
            ntree.links.new(nmap_node.outputs["Normal"], norm_in)
    return True


def _ensure_baked_map_image(mat_prefix, slot_key, w, h, is_color=False):
    """Create or resize a `bpy.data.images` target for a Mesh Map slot (`normal`, `ao`, `curvature`, etc.)."""
    suffix_map = {
        "normal": "Normal",
        "normal_ws": "WorldNormal",
        "id_map": "ID",
        "ao": "AO",
        "curvature": "Curvature",
        "position": "Position",
        "thickness": "Thickness",
    }
    img_name = f"{mat_prefix}_{suffix_map.get(slot_key, slot_key)}"
    ima = bpy.data.images.get(img_name)
    if ima is None:
        ima = bpy.data.images.new(name=img_name, width=int(w), height=int(h), alpha=False, float_buffer=False)
    elif int(ima.size[0]) != int(w) or int(ima.size[1]) != int(h):
        try:
            ima.scale(int(w), int(h))
        except Exception:
            pass
    try:
        ima.colorspace_settings.name = 'sRGB' if is_color else 'Non-Color'
    except Exception:
        pass
    return ima


def _write_rgb_f32_to_image(ima, rgb_f32, w, h):
    """Write float32[H, W, 3] into `ima.pixels` and pack for persistence."""
    import numpy as np
    rgba = np.ones((h, w, 4), dtype=np.float32)
    rgba[:, :, :3] = np.clip(rgb_f32, 0.0, 1.0)
    ima.pixels.foreach_set(rgba.ravel())
    ima.update()
    try:
        ima.pack()
    except Exception:
        pass


def _golden_ratio_color_rgb(idx):
    """Generate a distinct saturated RGB color for Color ID map index `idx`."""
    import colorsys
    h = (float(idx) * 0.61803398875 + 0.13) % 1.0
    s = 0.78 if (idx % 2 == 0) else 0.62
    v = 0.92 if (idx % 3 != 0) else 0.76
    return colorsys.hsv_to_rgb(h, s, v)


def _build_high_poly_bvh_and_data(hp_obs, context=None):
    """Build a combined World-Space C++ `BVHTree` plus per-triangle vertex normals, positions, and Color IDs
    from High-Poly source objects `hp_obs` (evaluating modifiers when depsgraph is available).
    """
    import numpy as np
    from mathutils.bvhtree import BVHTree

    depsgraph = None
    if context is not None:
        try:
            depsgraph = context.evaluated_depsgraph_get()
        except Exception:
            depsgraph = None

    all_verts = []
    all_vnorms = []
    all_tris = []
    all_tri_colors = []
    v_offset = 0

    for ob_idx, ob in enumerate(hp_obs):
        if ob is None or ob.type != 'MESH' or ob.data is None:
            continue
        eval_ob = ob.evaluated_get(depsgraph) if depsgraph is not None else ob
        mesh = eval_ob.to_mesh() if depsgraph is not None else ob.data
        if mesh is None:
            continue
        try:
            nv = len(mesh.vertices)
            if nv == 0 or len(mesh.polygons) == 0:
                continue
            mesh.calc_loop_triangles()
            nt = len(mesh.loop_triangles)
            if nt == 0:
                continue

            v_co = np.empty(nv * 3, dtype=np.float32)
            v_no = np.empty(nv * 3, dtype=np.float32)
            mesh.vertices.foreach_get("co", v_co)
            mesh.vertices.foreach_get("normal", v_no)
            v_co = v_co.reshape(nv, 3)
            v_no = v_no.reshape(nv, 3)

            mw = np.array(eval_ob.matrix_world, dtype=np.float32)
            rot = mw[:3, :3]
            inv_t = np.linalg.inv(rot).T
            v_w = v_co @ rot.T + mw[:3, 3]
            n_w = v_no @ inv_t.T
            n_len = np.maximum(np.linalg.norm(n_w, axis=1, keepdims=True), 1e-6)
            n_w /= n_len

            t_v = np.empty(nt * 3, dtype=np.int32)
            t_m = np.empty(nt, dtype=np.int32)
            mesh.loop_triangles.foreach_get("vertices", t_v)
            mesh.loop_triangles.foreach_get("material_index", t_m)
            t_v = t_v.reshape(nt, 3) + v_offset

            # Per-triangle Color ID based on High-Poly object + material index (or object color)
            ob_col = getattr(ob, "color", None)
            use_ob_col = (
                ob_col is not None and
                (abs(float(ob_col[0]) - 1.0) > 1e-3 or abs(float(ob_col[1]) - 1.0) > 1e-3 or abs(float(ob_col[2]) - 1.0) > 1e-3)
            )
            tri_cols = np.empty((nt, 3), dtype=np.float32)
            if use_ob_col:
                tri_cols[:] = (float(ob_col[0]), float(ob_col[1]), float(ob_col[2]))
            else:
                unique_mats = np.unique(t_m)
                for m_idx in unique_mats:
                    rgb = _golden_ratio_color_rgb(ob_idx * 7 + int(m_idx) + 1)
                    tri_cols[t_m == m_idx] = rgb

            all_verts.append(v_w)
            all_vnorms.append(n_w)
            all_tris.append(t_v)
            all_tri_colors.append(tri_cols)
            v_offset += nv
        finally:
            if depsgraph is not None and mesh is not ob.data:
                try:
                    eval_ob.to_mesh_clear()
                except Exception:
                    pass

    if not all_verts:
        return None

    V = np.vstack(all_verts)
    N = np.vstack(all_vnorms)
    T = np.vstack(all_tris)
    C = np.vstack(all_tri_colors)
    bvh = BVHTree.FromPolygons(V.tolist(), T.tolist())
    return {
        "bvh": bvh,
        "verts": V,
        "vnorms": N,
        "tris": T,
        "tri_colors": C,
    }


def _bake_mesh_maps_for_node(node, context=None, target_map='ALL'):
    """Bake Substance-Painter-style Mesh Maps (`normal`, `normal_ws`, `id_map`, `ao`, `curvature`, `position`, `thickness`)
    across all Low-Poly mesh objects sharing `node`'s material, optionally transferring details from High-Poly mesh(es)
    and evaluating Global Scene AO across surrounding objects.
    """
    global _INTERNAL_REPROJECTING
    import numpy as np

    if node is None:
        return False, "No active Layered Texture Paint node"

    owner_node = _canonical_mask_owner_node(node)
    mat = _get_material_for_node(owner_node, context)
    st = _get_mesh_maps_state(owner_node)

    sharing_obs, first_c_ob = _find_all_mesh_objects_for_node(owner_node, context)
    low_obs = sharing_obs if sharing_obs else ([first_c_ob] if first_c_ob is not None else [])
    low_obs = [o for o in low_obs if o is not None and o.type == 'MESH' and o.data is not None and len(o.data.vertices) > 0 and len(o.data.polygons) > 0]
    if not low_obs:
        return False, "No mesh objects with UVs found for this material"

    # Determine output resolution
    res_mode = st.get("bake_res", "MATCH")
    if res_mode == 'MATCH' and owner_node.image is not None:
        out_w = max(int(owner_node.image.size[0]), 64)
        out_h = max(int(owner_node.image.size[1]), 64)
    else:
        try:
            r_val = int(res_mode)
        except Exception:
            r_val = 1024
        out_w, out_h = r_val, r_val

    # Evaluation grid resolution for raycast maps (up to 1024x1024 for fast raycasting, upsampled to out_w x out_h)
    eval_w = min(out_w, 1024)
    eval_h = min(out_h, 1024)

    source_mode = st.get("source_mode", "LOW_POLY")
    ao_scope = st.get("ao_scope", "GLOBAL")
    cage_dist = max(float(st.get("cage_dist", 0.08)), 1e-4)
    ao_dist_factor = max(float(st.get("ao_dist", 0.65)), 0.02)

    hp_obs = []
    if source_mode == 'HIGH_POLY_OB':
        hp_ob = bpy.data.objects.get(st.get("high_poly_ob", ""))
        if hp_ob is not None and hp_ob.type == 'MESH':
            hp_obs = [hp_ob]
    elif source_mode == 'HIGH_POLY_COL':
        hp_col = bpy.data.collections.get(st.get("high_poly_col", ""))
        if hp_col is not None:
            hp_obs = [o for o in hp_col.all_objects if o.type == 'MESH' and o.data is not None]

    hp_data = _build_high_poly_bvh_and_data(hp_obs, context=context) if hp_obs else None

    all_slots = ("normal", "normal_ws", "id_map", "ao", "curvature", "position", "thickness")
    bake_slots = all_slots if target_map == 'ALL' else (target_map,)

    # Compute assembly world-space bounding box across all low_obs
    all_w_pts = [o.matrix_world @ Vector(co) for o in low_obs for co in o.bound_box]
    asm_bmin = np.array([min(p.x for p in all_w_pts), min(p.y for p in all_w_pts), min(p.z for p in all_w_pts)], dtype=np.float32)
    asm_bmax = np.array([max(p.x for p in all_w_pts), max(p.y for p in all_w_pts), max(p.z for p in all_w_pts)], dtype=np.float32)
    asm_span = np.maximum(asm_bmax - asm_bmin, 1e-5)
    asm_extent = float(np.max(asm_span))

    # Accumulator buffers at (eval_h, eval_w)
    acc_normal = np.dstack([
        np.full((eval_h, eval_w), 0.5, dtype=np.float32),
        np.full((eval_h, eval_w), 0.5, dtype=np.float32),
        np.ones((eval_h, eval_w), dtype=np.float32),
    ])
    acc_normal_ws = np.dstack([
        np.full((eval_h, eval_w), 0.5, dtype=np.float32),
        np.full((eval_h, eval_w), 0.5, dtype=np.float32),
        np.ones((eval_h, eval_w), dtype=np.float32),
    ])
    acc_id = np.zeros((eval_h, eval_w, 3), dtype=np.float32)
    acc_pos = np.zeros((eval_h, eval_w, 3), dtype=np.float32)
    acc_thick = np.ones((eval_h, eval_w), dtype=np.float32)

    # Scene objects for Global AO
    scene_obs = [
        o for o in bpy.data.objects
        if o.type == 'MESH' and o.data is not None and not o.hide_get() and o not in hp_obs and len(o.data.vertices) > 0 and len(o.data.polygons) > 0
    ]

    need_surf = any(s in bake_slots for s in ("normal", "normal_ws", "id_map", "position", "curvature"))
    need_thick = ("thickness" in bake_slots)

    for part_idx, ob_part in enumerate(low_obs):
        tmap = _build_uv_texel_map(ob_part, eval_w, eval_h)
        if tmap is None or len(tmap["pix_idx"]) == 0:
            continue

        mesh = ob_part.data
        nv = len(mesh.vertices)
        nt = len(mesh.loop_triangles)
        v_co = np.empty(nv * 3, dtype=np.float32)
        v_no = np.empty(nv * 3, dtype=np.float32)
        mesh.vertices.foreach_get("co", v_co)
        mesh.vertices.foreach_get("normal", v_no)
        v_co = v_co.reshape(nv, 3)
        v_no = v_no.reshape(nv, 3)

        mw = np.array(ob_part.matrix_world, dtype=np.float32)
        rot = mw[:3, :3]
        inv_t = np.linalg.inv(rot).T
        V_w = v_co @ rot.T + mw[:3, 3]
        N_w = v_no @ inv_t.T
        N_w /= np.maximum(np.linalg.norm(N_w, axis=1, keepdims=True), 1e-6)

        pix_idx = tmap["pix_idx"]
        tri_idx = tmap["tri_idx"]
        w0, w1, w2 = tmap["w0"], tmap["w1"], tmap["w2"]
        tv = tmap["tri_verts"][tri_idx]
        cov_2d = np.zeros((eval_h, eval_w), dtype=bool)
        cov_2d.ravel()[pix_idx] = True

        if need_surf:
            # Interpolate Low-Poly world position & normal at each covered UV texel
            P_low = V_w[tv[:, 0]] * w0[:, None] + V_w[tv[:, 1]] * w1[:, None] + V_w[tv[:, 2]] * w2[:, None]
            N_low = N_w[tv[:, 0]] * w0[:, None] + N_w[tv[:, 1]] * w1[:, None] + N_w[tv[:, 2]] * w2[:, None]
            N_low /= np.maximum(np.linalg.norm(N_low, axis=1, keepdims=True), 1e-6)

            # Per-triangle Tangent & Bitangent in World Space from UV derivatives
            tl = tmap["tri_loops"]
            uvs = tmap["uvs"]
            dp1 = V_w[tmap["tri_verts"][:, 1]] - V_w[tmap["tri_verts"][:, 0]]
            dp2 = V_w[tmap["tri_verts"][:, 2]] - V_w[tmap["tri_verts"][:, 0]]
            duv1 = uvs[tl[:, 1]] - uvs[tl[:, 0]]
            duv2 = uvs[tl[:, 2]] - uvs[tl[:, 0]]
            det = duv1[:, 0] * duv2[:, 1] - duv1[:, 1] * duv2[:, 0]
            inv_det = np.where(np.abs(det) > 1e-8, 1.0 / np.where(np.abs(det) > 1e-8, det, 1.0), 1.0)[:, None]
            T_tri = (dp1 * duv2[:, 1:2] - dp2 * duv1[:, 1:2]) * inv_det
            t_len = np.linalg.norm(T_tri, axis=1, keepdims=True)
            T_tri = np.where(t_len > 1e-6, T_tri / np.maximum(t_len, 1e-6), np.array([1.0, 0.0, 0.0], dtype=np.float32))

            T_low = T_tri[tri_idx]
            # Gram-Schmidt orthogonalize T_low against N_low
            T_low = T_low - N_low * np.sum(T_low * N_low, axis=1, keepdims=True)
            T_low /= np.maximum(np.linalg.norm(T_low, axis=1, keepdims=True), 1e-6)
            B_low = np.cross(N_low, T_low)

            if hp_data is not None:
                # Raycast from Low-Poly UV texels onto High-Poly BVHTree!
                hp_bvh = hp_data["bvh"]
                hp_V = hp_data["verts"]
                hp_N = hp_data["vnorms"]
                hp_T = hp_data["tris"]
                hp_C = hp_data["tri_colors"]
                ray_cast = hp_bvh.ray_cast
                find_nearest = hp_bvh.find_nearest

                N_surf = N_low.copy()
                P_surf = P_low.copy()
                ID_surf = np.full((len(pix_idx), 3), _golden_ratio_color_rgb(part_idx + 1), dtype=np.float32)
                max_ray = cage_dist * 2.5

                for k in range(len(pix_idx)):
                    px, py, pz = float(P_low[k, 0]), float(P_low[k, 1]), float(P_low[k, 2])
                    nx, ny, nz = float(N_low[k, 0]), float(N_low[k, 1]), float(N_low[k, 2])
                    orig = Vector((px + nx * cage_dist, py + ny * cage_dist, pz + nz * cage_dist))
                    rdir = Vector((-nx, -ny, -nz))
                    hit_loc, _hit_no, hit_i, _hit_d = ray_cast(orig, rdir, max_ray)
                    if hit_loc is None or hit_i is None:
                        hit_loc, _hit_no, hit_i, _hit_d = find_nearest(Vector((px, py, pz)), max_ray)
                    if hit_loc is not None and hit_i is not None:
                        P_surf[k] = (float(hit_loc.x), float(hit_loc.y), float(hit_loc.z))
                        ID_surf[k] = hp_C[hit_i]
                        # Barycentric smooth normal interpolation on High-Poly triangle hit_i
                        iv0, iv1, iv2 = int(hp_T[hit_i, 0]), int(hp_T[hit_i, 1]), int(hp_T[hit_i, 2])
                        a = hp_V[iv0]
                        b = hp_V[iv1]
                        c = hp_V[iv2]
                        v0_hp = b - a
                        v1_hp = c - a
                        v2_hp = P_surf[k] - a
                        d00 = float(np.dot(v0_hp, v0_hp))
                        d01 = float(np.dot(v0_hp, v1_hp))
                        d11 = float(np.dot(v1_hp, v1_hp))
                        d20 = float(np.dot(v2_hp, v0_hp))
                        d21 = float(np.dot(v2_hp, v1_hp))
                        den = d00 * d11 - d01 * d01
                        if abs(den) > 1e-10:
                            bv = max(0.0, min(1.0, (d11 * d20 - d01 * d21) / den))
                            bw = max(0.0, min(1.0 - bv, (d00 * d21 - d01 * d20) / den))
                            bu = max(0.0, 1.0 - bv - bw)
                            nh = hp_N[iv0] * bu + hp_N[iv1] * bv + hp_N[iv2] * bw
                        else:
                            nh = hp_N[iv0]
                        n_l = max(float(np.linalg.norm(nh)), 1e-6)
                        N_surf[k] = nh / n_l

                # Project High-Poly world normal N_surf into Low-Poly tangent space (T_low, B_low, N_low)
                tan_x = np.sum(N_surf * T_low, axis=1)
                tan_y = np.sum(N_surf * B_low, axis=1)
                tan_z = np.maximum(np.sum(N_surf * N_low, axis=1), 0.05)
                tan_len = np.maximum(np.sqrt(tan_x * tan_x + tan_y * tan_y + tan_z * tan_z), 1e-6)
                N_tan = np.stack([
                    0.5 + 0.5 * (tan_x / tan_len),
                    0.5 + 0.5 * (tan_y / tan_len),
                    0.5 + 0.5 * (tan_z / tan_len),
                ], axis=1)
            else:
                # Low-Poly mode: smooth bevel normal variations across edges + part/material Color ID
                P_surf = P_low
                N_surf = N_low
                N_tan = np.column_stack([
                    np.full(len(pix_idx), 0.5, dtype=np.float32),
                    np.full(len(pix_idx), 0.5, dtype=np.float32),
                    np.ones(len(pix_idx), dtype=np.float32),
                ])
                t_m = np.empty(nt, dtype=np.int32)
                mesh.loop_triangles.foreach_get("material_index", t_m)
                ob_col = getattr(ob_part, "color", None)
                use_ob_col = (
                    ob_col is not None and
                    (abs(float(ob_col[0]) - 1.0) > 1e-3 or abs(float(ob_col[1]) - 1.0) > 1e-3 or abs(float(ob_col[2]) - 1.0) > 1e-3)
                )
                if use_ob_col:
                    ID_surf = np.full((len(pix_idx), 3), (float(ob_col[0]), float(ob_col[1]), float(ob_col[2])), dtype=np.float32)
                else:
                    tri_cols = np.empty((nt, 3), dtype=np.float32)
                    for m_idx in np.unique(t_m):
                        tri_cols[t_m == m_idx] = _golden_ratio_color_rgb(part_idx * 7 + int(m_idx) + 1)
                    ID_surf = tri_cols[tri_idx]

            # Write part texels into dilated 2D maps
            for c_i in range(3):
                ch_n = acc_normal[:, :, c_i].copy()
                ch_n.ravel()[pix_idx] = N_tan[:, c_i]
                acc_normal[:, :, c_i] = _dilate_2d_buffer_into_gutter(ch_n, cov_2d, iterations=3)

                ch_ws = acc_normal_ws[:, :, c_i].copy()
                ch_ws.ravel()[pix_idx] = 0.5 + 0.5 * N_surf[:, c_i]
                acc_normal_ws[:, :, c_i] = _dilate_2d_buffer_into_gutter(ch_ws, cov_2d, iterations=3)

                ch_id = acc_id[:, :, c_i].copy()
                ch_id.ravel()[pix_idx] = ID_surf[:, c_i]
                acc_id[:, :, c_i] = _dilate_2d_buffer_into_gutter(ch_id, cov_2d, iterations=3)

                ch_pos = acc_pos[:, :, c_i].copy()
                ch_pos.ravel()[pix_idx] = np.clip((P_surf[:, c_i] - asm_bmin[c_i]) / asm_span[c_i], 0.0, 1.0)
                acc_pos[:, :, c_i] = _dilate_2d_buffer_into_gutter(ch_pos, cov_2d, iterations=3)

        if need_thick:
            # Interior cone raycast at texel resolution (up to 64x64 per part) so flat low-poly faces
            # still capture thin edges/corners vs thick face interiors, as well as High-Poly thickness.
            bvh_thick = hp_data["bvh"] if hp_data is not None else _build_world_bvh_for_objects([ob_part])
            if bvh_thick is not None:
                th_w = min(eval_w, 64)
                th_h = min(eval_h, 64)
                tmap_th = _build_uv_texel_map(ob_part, th_w, th_h)
                if tmap_th is not None and len(tmap_th["pix_idx"]) > 0:
                    p_th = tmap_th["pix_idx"]
                    tv_th = tmap_th["tri_verts"][tmap_th["tri_idx"]]
                    w0_t, w1_t, w2_t = tmap_th["w0"], tmap_th["w1"], tmap_th["w2"]
                    P_th = V_w[tv_th[:, 0]] * w0_t[:, None] + V_w[tv_th[:, 1]] * w1_t[:, None] + V_w[tv_th[:, 2]] * w2_t[:, None]
                    # Use geometric triangle normal blended with vertex normal for accurate inward cone
                    e1_t = V_w[tv_th[:, 1]] - V_w[tv_th[:, 0]]
                    e2_t = V_w[tv_th[:, 2]] - V_w[tv_th[:, 0]]
                    FN_th = np.cross(e1_t, e2_t)
                    FN_th /= np.maximum(np.linalg.norm(FN_th, axis=1, keepdims=True), 1e-6)
                    VN_th = N_w[tv_th[:, 0]] * w0_t[:, None] + N_w[tv_th[:, 1]] * w1_t[:, None] + N_w[tv_th[:, 2]] * w2_t[:, None]
                    N_th = FN_th * 0.65 + VN_th * 0.35
                    N_th /= np.maximum(np.linalg.norm(N_th, axis=1, keepdims=True), 1e-6)

                    max_thick = max(1.15 * asm_extent, 0.05)
                    eps_t = 0.004 * asm_extent
                    rc_t = bvh_thick.ray_cast
                    pix_th = np.ones(len(p_th), dtype=np.float32)
                    cone_dirs = (
                        (0.0, 0.0, 1.0),
                        (0.65, 0.0, 0.76),
                        (-0.65, 0.0, 0.76),
                        (0.0, 0.65, 0.76),
                        (0.0, -0.65, 0.76),
                    )
                    for k in range(len(p_th)):
                        px, py, pz = float(P_th[k, 0]), float(P_th[k, 1]), float(P_th[k, 2])
                        inx, iny, inz = -float(N_th[k, 0]), -float(N_th[k, 1]), -float(N_th[k, 2])
                        in_vec = Vector((inx, iny, inz))
                        up = Vector((0.0, 0.0, 1.0)) if abs(inz) < 0.92 else Vector((1.0, 0.0, 0.0))
                        tan = in_vec.cross(up).normalized()
                        bitan = in_vec.cross(tan).normalized()
                        orig = Vector((px + inx * eps_t, py + iny * eps_t, pz + inz * eps_t))
                        d_sum = 0.0
                        for lx, ly, lz in cone_dirs:
                            rdir = tan * lx + bitan * ly + in_vec * lz
                            hit_loc, _hit_no, _hit_i, hit_d = rc_t(orig, rdir, max_thick)
                            if hit_loc is not None and hit_d is not None:
                                d_sum += min(max(float(hit_d) / max_thick, 0.0), 1.0)
                            else:
                                d_sum += 1.0
                        pix_th[k] = d_sum / float(len(cone_dirs))

                    # Normalize part thickness contrast so thin vs thick regions span [0.05..0.98]
                    t_min, t_max = float(np.min(pix_th)), float(np.max(pix_th))
                    if (t_max - t_min) > 1e-4:
                        pix_th = 0.05 + 0.93 * ((pix_th - t_min) / (t_max - t_min))

                    th_small = np.ones((th_h, th_w), dtype=np.float32)
                    cov_small = np.zeros((th_h, th_w), dtype=bool)
                    th_small.ravel()[p_th] = pix_th
                    cov_small.ravel()[p_th] = True
                    th_small = _dilate_2d_buffer_into_gutter(th_small, cov_small, iterations=2)
                    th_small = _fast_mask_box_blur_2d_f32(th_small, 1)

                    if th_w != eval_w or th_h != eval_h:
                        ys_t = np.clip((np.linspace(0.0, th_h - 1.0, eval_h)).astype(np.int32), 0, th_h - 1)
                        xs_t = np.clip((np.linspace(0.0, th_w - 1.0, eval_w)).astype(np.int32), 0, th_w - 1)
                        th_up = th_small[ys_t[:, None], xs_t[None, :]]
                    else:
                        th_up = th_small
                    acc_thick[cov_2d] = th_up[cov_2d]
                    acc_thick = _dilate_2d_buffer_into_gutter(acc_thick, cov_2d, iterations=3)

    # Evaluate AO and Curvature at full (out_w, out_h) using C++ generator + Global Scene AO + High-Poly Normal Curvature
    ao_2d = None
    curv_2d = None
    act_layer = owner_node.active_layer or (owner_node.layers[0] if len(owner_node.layers) > 0 else None)

    if ("ao" in bake_slots or "curvature" in bake_slots) and act_layer is not None:
        c_layer = _C_TexPaintLayer.from_address(act_layer.as_pointer())
        saved_mask_bytes = None
        orig_use_mask = bool(act_layer.use_mask)
        if not orig_use_mask:
            act_layer.use_mask = True
            c_layer = _C_TexPaintLayer.from_address(act_layer.as_pointer())
        if c_layer.mask_ibuf:
            c_imb = _C_ImBuf.from_address(c_layer.mask_ibuf)
            mw_i, mh_i = int(c_imb.x), int(c_imb.y)
            if c_imb.byte_data and mw_i > 0 and mh_i > 0:
                saved_mask_bytes = ctypes.string_at(c_imb.byte_data, mw_i * mh_i * 4)
        else:
            mw_i, mh_i = out_w, out_h

        try:
            # Temporarily disable use_baked_maps so _ensure_generator_raw_u8 computes fresh geometry + Global AO
            _save_mesh_maps_state(owner_node, updates={"use_baked_maps": False})
            if "ao" in bake_slots:
                dummy_ao_eff = {
                    "uid": -9991,
                    "gen_type": "AO",
                    "gen_radius": min(max(ao_dist_factor * 0.6, 0.1), 1.0),
                    "gen_noise": 0.0,
                    "gen_noise_scale": 8.0,
                    "gen_global_ao": (ao_scope in {'GLOBAL', 'MATERIAL'}),
                }
                ao_u8 = _ensure_generator_raw_u8(owner_node, act_layer, dummy_ao_eff, mw_i, mh_i, force=True)
                ao_raw_2d = ao_u8.reshape(mh_i, mw_i).astype(np.float32) * (1.0 / 255.0)
                if mw_i != out_w or mh_i != out_h:
                    ys = np.clip((np.linspace(0.0, mh_i - 1.0, out_h)).astype(np.int32), 0, mh_i - 1)
                    xs = np.clip((np.linspace(0.0, mw_i - 1.0, out_w)).astype(np.int32), 0, mw_i - 1)
                    ao_2d = ao_raw_2d[ys[:, None], xs[None, :]]
                else:
                    ao_2d = ao_raw_2d

            if "curvature" in bake_slots:
                dummy_curv_eff = {
                    "uid": -9992,
                    "gen_type": "CURVATURE",
                    "gen_radius": 0.35,
                    "gen_noise": 0.0,
                    "gen_noise_scale": 8.0,
                    "gen_global_ao": False,
                }
                curv_u8 = _ensure_generator_raw_u8(owner_node, act_layer, dummy_curv_eff, mw_i, mh_i, force=True)
                curv_raw_2d = curv_u8.reshape(mh_i, mw_i).astype(np.float32) * (1.0 / 255.0)
                if mw_i != out_w or mh_i != out_h:
                    ys = np.clip((np.linspace(0.0, mh_i - 1.0, out_h)).astype(np.int32), 0, mh_i - 1)
                    xs = np.clip((np.linspace(0.0, mw_i - 1.0, out_w)).astype(np.int32), 0, mw_i - 1)
                    curv_2d = curv_raw_2d[ys[:, None], xs[None, :]]
                else:
                    curv_2d = curv_raw_2d
        finally:
            _save_mesh_maps_state(owner_node, updates={"use_baked_maps": True})
            if saved_mask_bytes is not None and c_layer.mask_ibuf:
                c_imb = _C_ImBuf.from_address(c_layer.mask_ibuf)
                if c_imb.byte_data:
                    ctypes.memmove(c_imb.byte_data, saved_mask_bytes, len(saved_mask_bytes))
            if not orig_use_mask:
                act_layer.use_mask = False

    # Upsample eval_w x eval_h maps to out_w x out_h if needed
    def _upsample_to_out(arr_2d_or_3d):
        if eval_w == out_w and eval_h == out_h:
            return arr_2d_or_3d
        ys = np.clip((np.linspace(0.0, eval_h - 1.0, out_h)).astype(np.int32), 0, eval_h - 1)
        xs = np.clip((np.linspace(0.0, eval_w - 1.0, out_w)).astype(np.int32), 0, eval_w - 1)
        return arr_2d_or_3d[ys[:, None], xs[None, :]]

    # Fuse Tangent-Space Normal Map UV derivatives into Curvature map (captures High-Poly sculpted bevels & grooves!)
    if curv_2d is not None:
        n_full = _upsample_to_out(acc_normal)
        nx_2d = n_full[:, :, 0] * 2.0 - 1.0
        ny_2d = n_full[:, :, 1] * 2.0 - 1.0
        dnx_dx = (np.roll(nx_2d, -1, axis=1) - np.roll(nx_2d, 1, axis=1)) * 0.5
        dny_dy = (np.roll(ny_2d, -1, axis=0) - np.roll(ny_2d, 1, axis=0)) * 0.5
        normal_curv_delta = (dnx_dx + dny_dy) * 1.45
        curv_2d = np.clip(curv_2d + normal_curv_delta, 0.0, 1.0)
        # Also enrich AO crevices with deep concave High-Poly normal cavities
        if ao_2d is not None and hp_data is not None:
            hp_cav = np.maximum(-normal_curv_delta, 0.0) * 0.65
            ao_2d = np.clip(ao_2d - hp_cav, 0.0, 1.0)

    mat_prefix = (mat.name if mat else (owner_node.image.name if owner_node.image else "TexSet")).replace(" ", "_")
    baked_names = {}

    if "normal" in bake_slots:
        ima = _ensure_baked_map_image(mat_prefix, "normal", out_w, out_h, is_color=False)
        _write_rgb_f32_to_image(ima, _upsample_to_out(acc_normal), out_w, out_h)
        baked_names["normal"] = ima.name
        if bool(st.get("apply_normal_to_shader", True)):
            _apply_baked_normal_map_to_shader(mat, owner_node, ima)

    if "normal_ws" in bake_slots:
        ima = _ensure_baked_map_image(mat_prefix, "normal_ws", out_w, out_h, is_color=False)
        _write_rgb_f32_to_image(ima, _upsample_to_out(acc_normal_ws), out_w, out_h)
        baked_names["normal_ws"] = ima.name

    if "id_map" in bake_slots:
        ima = _ensure_baked_map_image(mat_prefix, "id_map", out_w, out_h, is_color=True)
        _write_rgb_f32_to_image(ima, _upsample_to_out(acc_id), out_w, out_h)
        baked_names["id_map"] = ima.name

    if "ao" in bake_slots and ao_2d is not None:
        ima = _ensure_baked_map_image(mat_prefix, "ao", out_w, out_h, is_color=False)
        _write_rgb_f32_to_image(ima, np.dstack([ao_2d, ao_2d, ao_2d]), out_w, out_h)
        baked_names["ao"] = ima.name

    if "curvature" in bake_slots and curv_2d is not None:
        ima = _ensure_baked_map_image(mat_prefix, "curvature", out_w, out_h, is_color=False)
        _write_rgb_f32_to_image(ima, np.dstack([curv_2d, curv_2d, curv_2d]), out_w, out_h)
        baked_names["curvature"] = ima.name

    if "position" in bake_slots:
        ima = _ensure_baked_map_image(mat_prefix, "position", out_w, out_h, is_color=False)
        _write_rgb_f32_to_image(ima, _upsample_to_out(acc_pos), out_w, out_h)
        baked_names["position"] = ima.name

    if "thickness" in bake_slots:
        ima = _ensure_baked_map_image(mat_prefix, "thickness", out_w, out_h, is_color=False)
        th_out = _upsample_to_out(acc_thick)
        _write_rgb_f32_to_image(ima, np.dstack([th_out, th_out, th_out]), out_w, out_h)
        baked_names["thickness"] = ima.name

    _save_mesh_maps_state(owner_node, updates={"use_baked_maps": True}, map_updates=baked_names)

    # Clear generator raw cache and re-evaluate active layer masks so Smart Masks immediately reflect the baked maps
    _EFFECT_RAW_CACHE.clear()
    _GLOBAL_AO_CACHE.clear()
    for l_item in owner_node.layers:
        if getattr(l_item, "use_mask", False):
            st_l = _get_layer_grunge_state(owner_node, l_item)
            if st_l.get("effects"):
                _evaluate_layer_grunge_mask(owner_node, l_item, context=context, force_reproject=True, skip_absorb=True)

    return True, f"Baked {len(baked_names)} Mesh Map(s) ({out_w}×{out_h}) across {len(low_obs)} part(s)"


class NODE_OT_tex_paint_bake_mesh_maps(Operator):
    """Bake Substance-Painter-style Mesh Maps (Normal, World Space Normal, ID, Ambient Occlusion, Curvature, Position, Thickness) from Low-Poly + Global Scene or High-Poly source mesh(es)"""
    bl_idname = "node.tex_paint_bake_mesh_maps"
    bl_label = "Bake Mesh Maps"
    bl_options = {'REGISTER', 'UNDO'}

    map_type: EnumProperty(
        name="Map to Bake",
        items=(
            ('ALL', "All Mesh Maps", "Bake all 7 Mesh Maps (Normal, World Normal, ID, AO, Curvature, Position, Thickness)"),
            ('normal', "Normal (Tangent Space)", "Bake Tangent-Space Normal Map"),
            ('normal_ws', "World Space Normal", "Bake World-Space Normal Map"),
            ('id_map', "ID (Color / Part)", "Bake Part / Material Color ID Map"),
            ('ao', "Ambient Occlusion (Global / Self)", "Bake Ambient Occlusion Map (including surrounding scene objects when Global AO is active)"),
            ('curvature', "Curvature", "Bake Curvature Map (geometry + normal-map derivatives)"),
            ('position', "Position", "Bake normalized World-Space XYZ Position Map"),
            ('thickness', "Thickness", "Bake interior raycast Thickness Map"),
        ),
        default='ALL',
    )

    @classmethod
    def poll(cls, context):
        return tex_paint_node_from_context(context) is not None

    def execute(self, context):
        node = tex_paint_node_from_context(context)
        ok, msg = _bake_mesh_maps_for_node(node, context=context, target_map=self.map_type)
        if not ok:
            self.report({'WARNING'}, msg)
            return {'CANCELLED'}
        _tag_view3d_gizmo_refresh(context)
        self.report({'INFO'}, msg)
        return {'FINISHED'}


class NODE_OT_tex_paint_mesh_map_action(Operator):
    """Preview, Clear, or Connect a baked Mesh Map in the Texture Set Mesh Maps Hub"""
    bl_idname = "node.tex_paint_mesh_map_action"
    bl_label = "Mesh Map Action"
    bl_options = {'REGISTER', 'UNDO'}

    action: EnumProperty(
        name="Action",
        items=(
            ('PREVIEW_2D', "Preview in 2D Image Editor", "Display this Mesh Map in an open 2D Image Editor"),
            ('APPLY_NORMAL', "Connect Normal Map to Shader", "Wire the baked Tangent-Space Normal Map into the material's Principled BSDF Normal input"),
            ('CLEAR_SLOT', "Clear Mesh Map Slot", "Unlink this Mesh Map slot"),
            ('CLEAR_ALL', "Clear All Mesh Map Slots", "Unlink all 7 Mesh Map slots on this material"),
        ),
        default='PREVIEW_2D',
    )
    slot: StringProperty(name="Slot", default="ao")

    @classmethod
    def poll(cls, context):
        return tex_paint_node_from_context(context) is not None

    def execute(self, context):
        node = tex_paint_node_from_context(context)
        if node is None:
            return {'CANCELLED'}
        owner = _canonical_mask_owner_node(node)
        st = _get_mesh_maps_state(owner)
        maps = st.get("maps", {})

        if self.action == 'CLEAR_ALL':
            _save_mesh_maps_state(owner, map_updates={k: "" for k in maps})
            _EFFECT_RAW_CACHE.clear()
            for l_item in owner.layers:
                if getattr(l_item, "use_mask", False):
                    _evaluate_layer_grunge_mask(owner, l_item, context=context, force_reproject=True, skip_absorb=True)
            self.report({'INFO'}, "Cleared all Mesh Map slots")
            return {'FINISHED'}

        if self.action == 'CLEAR_SLOT':
            _save_mesh_maps_state(owner, map_updates={self.slot: ""})
            _EFFECT_RAW_CACHE.clear()
            for l_item in owner.layers:
                if getattr(l_item, "use_mask", False):
                    _evaluate_layer_grunge_mask(owner, l_item, context=context, force_reproject=True, skip_absorb=True)
            return {'FINISHED'}

        im_name = maps.get(self.slot, "")
        ima = bpy.data.images.get(im_name) if im_name else None
        if ima is None:
            self.report({'WARNING'}, f"No image assigned to '{self.slot}' slot")
            return {'CANCELLED'}

        if self.action == 'APPLY_NORMAL':
            mat = _get_material_for_node(owner, context)
            if _apply_baked_normal_map_to_shader(mat, owner, ima):
                self.report({'INFO'}, f"Connected '{ima.name}' to Principled BSDF Normal")
                return {'FINISHED'}
            return {'CANCELLED'}

        if self.action == 'PREVIEW_2D':
            wm = getattr(context, "window_manager", None)
            shown = False
            if wm:
                for win in wm.windows:
                    if win.screen:
                        for area in win.screen.areas:
                            if area.type == 'IMAGE_EDITOR':
                                for sp in area.spaces:
                                    if sp.type == 'IMAGE_EDITOR':
                                        sp.image = ima
                                        shown = True
                                area.tag_redraw()
            if shown:
                self.report({'INFO'}, f"Displaying '{ima.name}' in 2D Image Editor")
            else:
                self.report({'INFO'}, f"Mesh Map '{ima.name}' ready (open an Image Editor to view 2D)")
            return {'FINISHED'}

        return {'CANCELLED'}


class NODE_OT_tex_paint_import_mesh_map(Operator):
    """Import an external baked image file (Normal, AO, Curvature, ID, Position, Thickness) from disk into a Mesh Map slot"""
    bl_idname = "node.tex_paint_import_mesh_map"
    bl_label = "Open Mesh Map Image..."
    bl_options = {'REGISTER', 'UNDO'}

    filepath: StringProperty(
        name="File Path",
        description="Image file to load into the selected Mesh Map slot",
        maxlen=1024,
        subtype='FILE_PATH',
    )
    filter_image: BoolProperty(default=True, options={'HIDDEN', 'SKIP_SAVE'})
    filter_folder: BoolProperty(default=True, options={'HIDDEN', 'SKIP_SAVE'})
    slot: StringProperty(name="Mesh Map Slot", default="ao")

    @classmethod
    def poll(cls, context):
        return tex_paint_node_from_context(context) is not None

    def invoke(self, context, _event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        node = tex_paint_node_from_context(context)
        if node is None or not self.filepath or not os.path.isfile(self.filepath):
            return {'CANCELLED'}
        try:
            ima = bpy.data.images.load(self.filepath, check_existing=True)
        except Exception as ex:
            self.report({'ERROR'}, f"Failed to load image: {ex}")
            return {'CANCELLED'}

        try:
            ima.colorspace_settings.name = 'sRGB' if self.slot == 'id_map' else 'Non-Color'
        except Exception:
            pass

        owner = _canonical_mask_owner_node(node)
        _save_mesh_maps_state(owner, updates={"use_baked_maps": True}, map_updates={self.slot: ima.name})
        if self.slot == 'normal':
            st = _get_mesh_maps_state(owner)
            if st.get("apply_normal_to_shader", True):
                mat = _get_material_for_node(owner, context)
                _apply_baked_normal_map_to_shader(mat, owner, ima)

        _EFFECT_RAW_CACHE.clear()
        for l_item in owner.layers:
            if getattr(l_item, "use_mask", False):
                _evaluate_layer_grunge_mask(owner, l_item, context=context, force_reproject=True, skip_absorb=True)

        self.report({'INFO'}, f"Loaded '{ima.name}' into '{self.slot}' Mesh Map slot")
        return {'FINISHED'}


class TEXPAINT_MT_layer_specials(Menu):
    """Special layer operations (Fill, Clear, Invert, Flip, Rasterize, Copy Mask, Merge Visible, Flatten, Export Texture Set)"""
    bl_idname = "TEXPAINT_MT_layer_specials"
    bl_label = "Layer Specials"

    def draw(self, _context):
        layout = self.layout
        layout.operator("node.tex_paint_export_texture_set", text="Export / Bake PBR Texture Set...", icon='EXPORT')
        layout.separator()
        layout.operator("node.tex_paint_make_material_unique", text="Re-Bake All Shared Models", icon='FILE_REFRESH').mode = 'REBAKE_ALL'
        layout.operator("node.tex_paint_make_material_unique", text="Make Material Unique (Active Part)", icon='DUPLICATE').mode = 'ACTIVE_OBJECT'
        layout.operator("node.tex_paint_make_material_unique", text="Split Shared Material Across All Parts", icon='MOD_EXPLODE').mode = 'ALL_SHARED_OBJECTS'
        layout.separator()
        layout.operator("node.tex_paint_layer_pixel_action", text="Fill Layer with Brush Color", icon='COLOR').action = 'FILL_BRUSH'
        layout.operator("node.tex_paint_layer_pixel_action", text="Clear Layer to Transparent", icon='TRASH').action = 'CLEAR'
        layout.operator("node.tex_paint_layer_pixel_action", text="Invert Layer RGB", icon='ARROW_LEFTRIGHT').action = 'INVERT_RGB'
        layout.separator()
        layout.operator("node.tex_paint_layer_pixel_action", text="Flip Layer Horizontally", icon='MOD_MIRROR').action = 'FLIP_H'
        layout.operator("node.tex_paint_layer_pixel_action", text="Flip Layer Vertically", icon='SORT_ASC').action = 'FLIP_V'
        layout.separator()
        layout.operator("node.tex_paint_layer_pixel_action", text="Rasterize Fill / 3D Projection to Paint", icon='BRUSH_DATA').action = 'RASTERIZE_TO_PAINT'
        layout.operator("node.tex_paint_layer_pixel_action", text="Copy Mask from Layer Below", icon='PASTEDOWN').action = 'COPY_MASK_BELOW'
        layout.separator()
        layout.operator("node.tex_paint_layer_duplicate", text="Duplicate Layer", icon='DUPLICATE')
        layout.operator("node.tex_paint_layer_merge_down", text="Merge Down", icon='TRIA_DOWN_BAR')
        layout.operator("node.tex_paint_layer_pixel_action", text="Merge Visible Layers", icon='RENDERLAYERS').action = 'MERGE_VISIBLE'
        layout.operator("node.tex_paint_layer_pixel_action", text="Flatten All Layers", icon='FULLSCREEN_EXIT').action = 'FLATTEN_ALL'
        layout.separator()
        layout.operator("node.tex_paint_canvas_resize", text="Resize Canvas...", icon='FULLSCREEN_ENTER')


class NODE_OT_tex_paint_smart_mask_recipe(Operator):
    """Apply a 1-click Substance Painter multi-layer Smart Weathering Mask Preset (Generator + Grunge + Filter + Levels/Paint)"""
    bl_idname = "node.tex_paint_smart_mask_recipe"
    bl_label = "Apply Smart Mask Preset"
    bl_options = {'REGISTER', 'UNDO'}

    recipe: EnumProperty(
        name="Smart Mask Preset",
        items=(
            ('EDGE_SCRATCHES', "Edge Wear + Scratches", "Sharp edge wear broken up by concrete/scratch grunge + Paint touchup layer", 'MOD_BEVEL', 0),
            ('SLOPE_CHIPPED_EDGES', "Chipped Edges (Blur Slope)", "Edge Wear + Blur Slope directional edge warp + sharp threshold levels", 'MOD_NOISE', 1),
            ('HEAVY_CHIPPING', "Heavy Paint Chipping", "Broad edge wear chipped away with high-contrast procedural spots", 'MOD_EXPLODE', 2),
            ('CAVITY_DIRT', "Cavity Dirt & Grime", "Concave crevices & seams multiplied with organic dirt grunge", 'SHARPCURVE', 3),
            ('SOFT_BLURRED_AO', "Soft Crevice Dust (Blurred AO)", "Ambient Occlusion crevices softened with a Blur filter and dust breakup", 'SHADING_RENDERED', 4),
            ('TOP_DUST', "Top-Down Dust / Moss", "Upward-facing surface normals broken up with soft procedural noise", 'NORMALS_FACE', 5),
            ('RUST_LEAKS', "Rust & Vertical Streaks", "Occluded crevices combined with vertical leak grunge and contrast boost", 'MOD_FLUIDSIM', 6),
            ('PAINT_PEEL', "Weathered Surface Peel", "Curvature base + Overlay cracked concrete grunge + Bevel rim + Paint layer", 'BRUSH_DATA', 7),
        ),
        default='EDGE_SCRATCHES',
    )

    @classmethod
    def poll(cls, context):
        node = tex_paint_node_from_context(context)
        return node is not None and node.active_layer is not None

    def execute(self, context):
        node = tex_paint_node_from_context(context)
        layer = node.active_layer if node else None
        if node is None or layer is None:
            return {'CANCELLED'}

        _ensure_grunge_catalog()
        if not layer.use_mask:
            layer.use_mask = True

        st = _get_layer_grunge_state(node, layer)
        effects = []

        def _pick_grunge(cat_id, substring_hint):
            entries = _GRUNGE_CATALOG.get(cat_id, [])
            for info in entries:
                if substring_hint in info["stem"].lower():
                    return info["id"]
            return entries[0]["id"] if entries else "BITMAP_GRUNGES::grunge_concrete_cracked"

        if self.recipe == 'EDGE_SCRATCHES':
            e1 = _make_default_mask_effect('GENERATOR', uid=1, is_first=True, gen_type='EDGE_WEAR')
            e1.update({"name": "Edge Wear Base", "gen_balance": 0.56, "gen_contrast": 0.52, "gen_radius": 0.35, "gen_noise": 0.25})
            e2 = _make_default_mask_effect('GRUNGE', uid=2, is_first=False)
            e2.update({
                "name": "Scratches Breakup",
                "blend_mode": 'MULTIPLY',
                "opacity": 0.78,
                "category": 'BITMAP_GRUNGES',
                "texture": _pick_grunge('BITMAP_GRUNGES', "scratch"),
                "scale": 2.5,
                "balance": 0.58,
                "contrast": 0.42,
            })
            e3 = _make_default_mask_effect('PAINT', uid=3, is_first=False)
            e3.update({"name": "Hand Paint Touchup"})
            effects = [e1, e2, e3]

        elif self.recipe == 'SLOPE_CHIPPED_EDGES':
            e1 = _make_default_mask_effect('GENERATOR', uid=1, is_first=True, gen_type='EDGE_WEAR')
            e1.update({"name": "Edge Wear Base", "gen_balance": 0.58, "gen_contrast": 0.45, "gen_radius": 0.42, "gen_noise": 0.20})
            e2 = _make_default_mask_effect('FILTER', uid=2, is_first=False, filter_type='BLUR_SLOPE')
            e2.update({"name": "Filter: Blur Slope", "flt_radius": 7.5, "flt_strength": 1.15, "flt_scale": 10.0})
            e3 = _make_default_mask_effect('LEVELS', uid=3, is_first=False)
            e3.update({"name": "Crisp Chipped Threshold", "lvl_balance": 0.52, "lvl_contrast": 0.58})
            effects = [e1, e2, e3]

        elif self.recipe == 'CAVITY_DIRT':
            e1 = _make_default_mask_effect('GENERATOR', uid=1, is_first=True, gen_type='CAVITY')
            e1.update({"name": "Cavity Crevices", "gen_balance": 0.58, "gen_contrast": 0.45, "gen_radius": 0.42, "gen_noise": 0.20})
            e2 = _make_default_mask_effect('GRUNGE', uid=2, is_first=False)
            e2.update({
                "name": "Dirt Grunge",
                "blend_mode": 'MULTIPLY',
                "opacity": 0.72,
                "category": 'BITMAP_GRUNGES',
                "texture": _pick_grunge('BITMAP_GRUNGES', "dirt"),
                "scale": 2.2,
                "balance": 0.55,
                "contrast": 0.35,
            })
            effects = [e1, e2]

        elif self.recipe == 'SOFT_BLURRED_AO':
            e1 = _make_default_mask_effect('GENERATOR', uid=1, is_first=True, gen_type='AO')
            e1.update({"name": "Ambient Occlusion", "invert": True, "gen_balance": 0.55, "gen_contrast": 0.42, "gen_radius": 0.45})
            e2 = _make_default_mask_effect('FILTER', uid=2, is_first=False, filter_type='BLUR')
            e2.update({"name": "Filter: Soft Blur", "flt_radius": 8.0, "flt_strength": 1.0})
            e3 = _make_default_mask_effect('GRUNGE', uid=3, is_first=False)
            e3.update({
                "name": "Fine Dust Grain",
                "blend_mode": 'MULTIPLY',
                "opacity": 0.65,
                "category": 'NOISES',
                "texture": _pick_grunge('NOISES', "clouds"),
                "scale": 3.2,
                "balance": 0.58,
                "contrast": 0.25,
            })
            effects = [e1, e2, e3]

        elif self.recipe == 'TOP_DUST':
            e1 = _make_default_mask_effect('GENERATOR', uid=1, is_first=True, gen_type='NORMAL_UP')
            e1.update({"name": "Top-Facing Normal", "gen_balance": 0.52, "gen_contrast": 0.40, "gen_radius": 0.30, "gen_noise": 0.30})
            e2 = _make_default_mask_effect('GRUNGE', uid=2, is_first=False)
            e2.update({
                "name": "Dust Clouds",
                "blend_mode": 'MULTIPLY',
                "opacity": 0.68,
                "category": 'NOISES',
                "texture": _pick_grunge('NOISES', "clouds"),
                "scale": 3.0,
                "balance": 0.56,
                "contrast": 0.28,
            })
            effects = [e1, e2]

        elif self.recipe == 'RUST_LEAKS':
            e1 = _make_default_mask_effect('GENERATOR', uid=1, is_first=True, gen_type='AO')
            e1.update({"name": "Occluded Rust Base", "invert": True, "gen_balance": 0.54, "gen_contrast": 0.48, "gen_radius": 0.45})
            e2 = _make_default_mask_effect('GRUNGE', uid=2, is_first=False)
            e2.update({
                "name": "Vertical Leaks",
                "blend_mode": 'ADD',
                "opacity": 0.65,
                "category": 'BITMAP_GRUNGES',
                "texture": _pick_grunge('BITMAP_GRUNGES', "leak"),
                "scale": 1.8,
                "balance": 0.46,
                "contrast": 0.45,
            })
            e3 = _make_default_mask_effect('LEVELS', uid=3, is_first=False)
            e3.update({"name": "Crisp Rust Edges", "lvl_balance": 0.52, "lvl_contrast": 0.35})
            effects = [e1, e2, e3]

        elif self.recipe == 'HEAVY_CHIPPING':
            e1 = _make_default_mask_effect('GENERATOR', uid=1, is_first=True, gen_type='EDGE_WEAR')
            e1.update({"name": "Broad Edge Wear", "gen_balance": 0.62, "gen_contrast": 0.55, "gen_radius": 0.55, "gen_noise": 0.35})
            e2 = _make_default_mask_effect('GRUNGE', uid=2, is_first=False)
            e2.update({
                "name": "Chipping Spots",
                "blend_mode": 'SUBTRACT',
                "opacity": 0.75,
                "category": 'PROC_GRUNGES',
                "texture": _pick_grunge('PROC_GRUNGES', "spots"),
                "scale": 2.6,
                "balance": 0.42,
                "contrast": 0.65,
            })
            e3 = _make_default_mask_effect('LEVELS', uid=3, is_first=False)
            e3.update({"name": "Sharp Chipping", "lvl_balance": 0.54, "lvl_contrast": 0.55})
            effects = [e1, e2, e3]

        else:  # PAINT_PEEL
            e1 = _make_default_mask_effect('GENERATOR', uid=1, is_first=True, gen_type='CURVATURE')
            e1.update({"name": "Surface Curvature", "gen_balance": 0.52, "gen_contrast": 0.45, "gen_radius": 0.38})
            e2 = _make_default_mask_effect('GRUNGE', uid=2, is_first=False)
            e2.update({
                "name": "Cracked Concrete",
                "blend_mode": 'OVERLAY',
                "opacity": 0.85,
                "category": 'BITMAP_GRUNGES',
                "texture": _pick_grunge('BITMAP_GRUNGES', "cracked"),
                "scale": 2.0,
                "balance": 0.50,
                "contrast": 0.45,
            })
            e3 = _make_default_mask_effect('PAINT', uid=3, is_first=False)
            e3.update({"name": "Manual Paint Strokes"})
            effects = [e1, e2, e3]

        st["effects"] = effects
        st["active_effect_uid"] = effects[-1]["uid"]
        st["next_effect_uid"] = len(effects) + 1
        st["active"] = True
        st["base_value"] = 0
        _save_layer_grunge_state(node, layer, st)

        node.paint_target = 'MASK'
        _configure_paint_mask_brush(context, 1.0)
        _evaluate_layer_grunge_mask(node, layer, context=context, force_reproject=True, skip_absorb=True)
        _tag_view3d_gizmo_refresh(context)
        return {'FINISHED'}


class TEXPAINT_MT_smart_mask_recipes(Menu):
    """Substance-Painter-style Smart Masks preset menu (multi-effect stacks: Generator + Grunge + Filter + Levels)"""
    bl_idname = "TEXPAINT_MT_smart_mask_recipes"
    bl_label = "Smart Masks (Multi-Effect Presets)"

    def draw(self, _context):
        layout = self.layout
        layout.label(text="Edge Wear & Chipping Presets", icon='MOD_BEVEL')
        for rec_id, rec_lbl, rec_icon in (
            ('EDGE_SCRATCHES', "Edge Wear + Scratches", 'MOD_BEVEL'),
            ('SLOPE_CHIPPED_EDGES', "Chipped Edges (Blur Slope)", 'MOD_NOISE'),
            ('HEAVY_CHIPPING', "Heavy Paint Chipping", 'MOD_EXPLODE'),
            ('PAINT_PEEL', "Weathered Surface Peel", 'BRUSH_DATA'),
        ):
            op = layout.operator("node.tex_paint_smart_mask_recipe", text=rec_lbl, icon=rec_icon)
            op.recipe = rec_id

        layout.separator()
        layout.label(text="Dirt, Dust & Rust Presets", icon='SHADING_RENDERED')
        for rec_id, rec_lbl, rec_icon in (
            ('CAVITY_DIRT', "Cavity Dirt & Grime", 'SHARPCURVE'),
            ('SOFT_BLURRED_AO', "Soft Crevice Dust (Blurred AO)", 'SHADING_RENDERED'),
            ('TOP_DUST', "Top-Down Dust / Moss", 'NORMALS_FACE'),
            ('RUST_LEAKS', "Rust & Vertical Streaks", 'MOD_FLUIDSIM'),
        ):
            op = layout.operator("node.tex_paint_smart_mask_recipe", text=rec_lbl, icon=rec_icon)
            op.recipe = rec_id


class TEXPAINT_MT_add_mask_generator(Menu):
    """Substance-Painter-style Add Generator submenu inside + Add Effect"""
    bl_idname = "TEXPAINT_MT_add_mask_generator"
    bl_label = "Add Generator"

    def draw(self, _context):
        layout = self.layout
        gen_items = (
            ('EDGE_WEAR', "Edge Wear (Convex Sharp Edges)", 'MOD_BEVEL'),
            ('CAVITY', "Cavity & Dirt (Concave Crevices)", 'SHARPCURVE'),
            ('CURVATURE', "Curvature (Concave to Convex)", 'SMOOTHCURVE'),
            ('AO', "Ambient Occlusion (Mesh Shading)", 'SHADING_RENDERED'),
            ('NORMAL_UP', "Top-Down Dust (World Space Normal)", 'NORMALS_FACE'),
            ('POSITION_Z', "Vertical Height (Bottom-to-Top Gradient)", 'EMPTY_SINGLE_ARROW'),
        )
        for g_id, g_lbl, g_icon in gen_items:
            op_g = layout.operator("node.tex_paint_mask_effect_add", text=g_lbl, icon=g_icon)
            op_g.effect_type = 'GENERATOR'
            op_g.gen_type = g_id


class TEXPAINT_MT_add_mask_filter(Menu):
    """Substance-Painter-style Add Filter submenu inside + Add Effect (Blur, Blur Slope, Sharpen, Bevel, Dilate/Erode, etc.)"""
    bl_idname = "TEXPAINT_MT_add_mask_filter"
    bl_label = "Add Filter"

    def draw(self, _context):
        layout = self.layout
        filter_items = (
            ('BLUR', "Blur (Gaussian / Box Softening)", 'MOD_SMOOTH'),
            ('BLUR_SLOPE', "Blur Slope (Directional Edge Warp)", 'MOD_NOISE'),
            ('SHARPEN', "Sharpen (Unsharp Mask Crispness)", 'SHARPCURVE'),
            ('DILATE_ERODE', "Grow / Shrink (Dilate / Erode)", 'FULLSCREEN_ENTER'),
            ('BEVEL', "Bevel (Edge Distance Ramp)", 'MOD_BEVEL'),
            ('THRESHOLD', "Threshold (Hard Binary Stencil)", 'IPO_CONSTANT'),
            ('POSTERIZE', "Posterize (Stepped Contour Bands)", 'LINENUMBERS_ON'),
            ('HIGHPASS', "Highpass (Edge Accentuation)", 'SEQ_HISTOGRAM'),
        )
        for f_id, f_lbl, f_icon in filter_items:
            op_f = layout.operator("node.tex_paint_mask_effect_add", text=f_lbl, icon=f_icon)
            op_f.effect_type = 'FILTER'
            op_f.filter_type = f_id


class TEXPAINT_MT_add_layer(Menu):
    """Dropdown menu for the + button between the Up/Down arrows on the main Layer Stack"""
    bl_idname = "TEXPAINT_MT_add_layer"
    bl_label = "Add Layer or Projection"

    def draw(self, context):
        layout = self.layout
        space = getattr(context, "space_data", None)
        node = tex_paint_node_from_context(context)
        is_2d_only = bool((space and space.type == 'IMAGE_EDITOR') or _is_standalone_2d_node(node))

        op_p = layout.operator("node.tex_paint_layer_add", text="Add Paint Layer", icon='BRUSH_DATA')
        op_p.type = 'PAINT'
        op_f = layout.operator(
            "node.tex_paint_layer_add",
            text="Add Fill Layer (Color)" if is_2d_only else "Add Fill Layer (Multi-Channel PBR)",
            icon='COLOR',
        )
        op_f.type = 'FILL'
        op_a = layout.operator("node.tex_paint_layer_add", text="Add Adjustment Layer (HSV / Contrast)", icon='MODIFIER')
        op_a.type = 'ADJUSTMENT'

        layout.separator()
        layout.operator("node.tex_paint_layer_import_image", text="Import Image as Layer...", icon='FILE_IMAGE')

        if not is_2d_only:
            op_dec = layout.operator("node.tex_paint_layer_project_image", text="Project 3D Decal...", icon='MOD_UVPROJECT')
            op_dec.projection_mode = 'DECAL'
            op_dec.new_layer = True
            op_dec.clear_layer = True

            op_wrp = layout.operator("node.tex_paint_layer_project_image", text="Project 3D Warp...", icon='MOD_WARP')
            op_wrp.projection_mode = 'WARP'
            op_wrp.new_layer = True
            op_wrp.clear_layer = True

            op_tri = layout.operator("node.tex_paint_layer_project_image", text="Project 3D Triplanar...", icon='MESH_CUBE')
            op_tri.projection_mode = 'TRIPLANAR'
            op_tri.new_layer = True
            op_tri.clear_layer = True

        layout.separator()
        op_bm = layout.operator("node.tex_paint_mask_stack_init", text="Add Black Mask (Hide All)", icon='SHADING_SOLID')
        op_bm.mode = 'BLACK'
        op_wm = layout.operator("node.tex_paint_mask_stack_init", text="Add White Mask (Reveal All)", icon='LIGHT_SUN')
        op_wm.mode = 'WHITE'
        layout.operator("node.tex_paint_grunge_open_image", text="Open Image into Mask...", icon='FILE_FOLDER')
        if not is_2d_only:
            layout.menu("TEXPAINT_MT_smart_mask_recipes", text="Smart Masks (Presets)", icon='PRESET')


class TEXPAINT_MT_add_mask_effect(Menu):
    """Substance-Painter-style + Add Effect menu (Generator, Paint, Fill, Filter, Levels)"""
    bl_idname = "TEXPAINT_MT_add_mask_effect"
    bl_label = "Add Mask Effect"

    def draw(self, context):
        layout = self.layout
        space = getattr(context, "space_data", None)
        node = tex_paint_node_from_context(context)
        is_2d_only = bool((space and space.type == 'IMAGE_EDITOR') or _is_standalone_2d_node(node))

        if not is_2d_only:
            layout.menu("TEXPAINT_MT_add_mask_generator", text="Add Generator", icon='MODIFIER')

        op_pnt = layout.operator("node.tex_paint_mask_effect_add", text="Add Paint", icon='BRUSH_DATA')
        op_pnt.effect_type = 'PAINT'

        op_grn = layout.operator("node.tex_paint_mask_effect_add", text="Add Fill (Grunge / Procedural)", icon='TEXTURE')
        op_grn.effect_type = 'GRUNGE'
        op_grn.fill_source = 'GRUNGE'

        op_fill = layout.operator("node.tex_paint_mask_effect_add", text="Add Bitmap Fill (Image / Stencil)", icon='FILE_IMAGE')
        op_fill.effect_type = 'GRUNGE'
        op_fill.fill_source = 'CUSTOM_IMAGE'

        layout.menu("TEXPAINT_MT_add_mask_filter", text="Add Filter (Blur / Slope / Bevel)", icon='SHADERFX')

        op_lvl = layout.operator("node.tex_paint_mask_effect_add", text="Add Levels", icon='SEQ_LUMA_WAVEFORM')
        op_lvl.effect_type = 'LEVELS'

        layout.separator()
        layout.operator("node.tex_paint_grunge_open_image", text="Open Image File into Mask...", icon='FILE_FOLDER')
        layout.operator("node.tex_paint_mask_effect_duplicate", text="Duplicate Active Effect", icon='DUPLICATE')
        layout.operator("node.tex_paint_layer_pixel_action", text="Copy Mask from Layer Below", icon='PASTEDOWN').action = 'COPY_MASK_BELOW'

        layout.separator()
        op_rb = layout.operator("node.tex_paint_mask_stack_init", text="Reset to Black Mask", icon='SHADING_SOLID')
        op_rb.mode = 'BLACK'
        op_rw = layout.operator("node.tex_paint_mask_stack_init", text="Reset to White Mask", icon='LIGHT_SUN')
        op_rw.mode = 'WHITE'
        op_rm = layout.operator("node.tex_paint_mask_stack_init", text="Remove Layer Mask", icon='X')
        op_rm.mode = 'REMOVE'


class NODE_OT_tex_paint_quick_uv(Operator):
    """Automatically generate non-overlapping UVs for the active mesh and return to Texture Paint mode"""
    bl_idname = "node.tex_paint_quick_uv"
    bl_label = "Quick UV Unwrap for Painting"
    bl_options = {'REGISTER', 'UNDO'}

    method: EnumProperty(
        name="Unwrap Method",
        items=(
            ('SMART', "Smart UV Project", "Unwrap the mesh by angle threshold with island margin (best general-purpose paint UVs)"),
            ('CUBE', "Cube Projection", "Project UVs onto 6 box faces"),
        ),
        default='SMART',
    )

    @classmethod
    def poll(cls, context):
        ob = getattr(context, "active_object", None)
        return ob is not None and ob.type == 'MESH'

    def execute(self, context):
        ob = context.active_object
        prev_mode = getattr(context, "mode", 'PAINT_TEXTURE')
        mesh = ob.data
        if not mesh.uv_layers:
            mesh.uv_layers.new(name="UVMap")
        try:
            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.mesh.select_all(action='SELECT')
            if self.method == 'CUBE':
                bpy.ops.uv.cube_project(cube_size=1.0, correct_aspect=True)
            else:
                bpy.ops.uv.smart_project(angle_limit=math.radians(66.0), island_margin=0.02)
        finally:
            target_mode = 'TEXTURE_PAINT' if 'PAINT' in prev_mode else 'OBJECT'
            try:
                bpy.ops.object.mode_set(mode=target_mode)
            except Exception:
                pass
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Layer Stack UIList & Substance-Painter-Style Multi-Layer Mask Stack UI
# ---------------------------------------------------------------------------

class TEXPAINT_UL_layers(UIList):
    """Layer list, drawn from top to bottom with inline Blend Mode dropdown and Opacity slider."""

    def draw_item(self, _context, layout, data, item, _icon, _active_data, _active_propname, index):
        layer = item
        node = data
        is_active = (getattr(node, "active_layer_index", -1) == index)
        paint_target = getattr(node, "paint_target", 'COLOR')
        row = layout.row(align=True)
        if layer.type == 'PAINT':
            proj_mode = getattr(layer, "proj_mode", 'NONE')
            if proj_mode == 'DECAL':
                type_icon = 'MOD_UVPROJECT'
            elif proj_mode == 'TRIPLANAR':
                type_icon = 'MESH_CUBE'
            elif proj_mode in {'FIT', 'UV_REPEAT'}:
                type_icon = 'TEXTURE'
            else:
                type_icon = 'BRUSH_DATA'
        else:
            type_icon = {'FILL': 'COLOR', 'ADJUSTMENT': 'MODIFIER'}[layer.type]

        if layer.use_clipping:
            row.label(text="", icon='TRIA_DOWN_BAR')

        # Clickable Layer Slot -> selects this layer and switches Inspector to Layer Channels (paint_target = COLOR)
        op_col = row.operator(
            "node.tex_paint_layer_target_select",
            text="",
            icon=type_icon,
            emboss=(is_active and paint_target == 'COLOR'),
            depress=(is_active and paint_target == 'COLOR'),
        )
        op_col.layer_index = index
        op_col.target = 'COLOR'

        # Clickable Mask Slot (shown when layer has a mask or when Mask tab is active on this layer)
        if layer.use_mask or (is_active and paint_target == 'MASK'):
            if layer.use_mask:
                st = _get_layer_grunge_state(node, layer)
                effects = st.get("effects", [])
                if len(effects) > 1:
                    mask_icon = 'RENDERLAYERS'
                elif len(effects) == 1:
                    et = effects[0].get("type", "GRUNGE")
                    mask_icon = {
                        'GENERATOR': 'MODIFIER',
                        'PAINT': 'BRUSH_DATA',
                        'GRUNGE': 'TEXTURE',
                        'FILTER': 'SHADERFX',
                        'LEVELS': 'SEQ_LUMA_WAVEFORM',
                    }.get(et, 'MOD_MASK')
                else:
                    mask_icon = 'MOD_MASK'
            else:
                mask_icon = 'MOD_MASK'

            op_msk = row.operator(
                "node.tex_paint_layer_target_select",
                text="",
                icon=mask_icon,
                emboss=(is_active and paint_target == 'MASK') or layer.use_mask,
                depress=(is_active and paint_target == 'MASK'),
            )
            op_msk.layer_index = index
            op_msk.target = 'MASK'

        mat_ul = _get_material_for_node(node)
        if layer.type == 'FILL' and _is_fill_channel_enabled(mat_ul, node, layer, 'BASE_COLOR'):
            swatch = row.row(align=True)
            swatch.scale_x = 0.35
            swatch.prop(layer, "fill_color", text="")

        row.prop(layer, "name", text="", emboss=False)

        # Inline Blend Mode Dropdown & Opacity Slider on each layer row
        blend_sub = row.row(align=True)
        blend_sub.ui_units_x = 3.1
        blend_sub.prop(layer, "blend_mode", text="")

        op_sub = row.row(align=True)
        op_sub.ui_units_x = 2.9
        op_sub.prop(layer, "opacity", text="", slider=True)

        if layer.lock:
            row.prop(layer, "lock", text="", emboss=False)
        row.prop(layer, "hide", text="", emboss=False)

    def filter_items(self, _context, data, propname):
        layers = getattr(data, propname)
        flt_flags = [self.bitflag_filter_item] * len(layers)
        flt_neworder = list(reversed(range(len(layers))))
        return flt_flags, flt_neworder


def _draw_mask_brush_bar(layout, context, node, layer, is_paint_sublayer=False):
    """Draw the unified single-row Mask Brush toolbar (Reveal White, Hide Black, Flip, Clear, Invert)."""
    col, _ = _get_current_brush_colors(context)
    is_white = bool(col and col[0] > 0.5 and col[1] > 0.5 and col[2] > 0.5)
    is_black = bool(col and col[0] <= 0.5 and col[1] <= 0.5 and col[2] <= 0.5)

    b_row = layout.row(align=True)
    op_rev = b_row.operator(
        "node.tex_paint_mask_brush_color",
        text="Reveal",
        icon='LIGHT_SUN',
        depress=is_white,
    )
    op_rev.value = 1.0
    op_hid = b_row.operator(
        "node.tex_paint_mask_brush_color",
        text="Hide",
        icon='SHADING_SOLID',
        depress=is_black,
    )
    op_hid.value = 0.0
    b_row.operator("paint.brush_colors_flip", text="", icon='FILE_REFRESH')
    if is_paint_sublayer:
        b_row.separator()
        op_clr = b_row.operator("node.tex_paint_mask_paint_action", text="Clear", icon='LOOP_BACK')
        op_clr.action = 'CLEAR'
        op_inv = b_row.operator("node.tex_paint_mask_paint_action", text="", icon='ARROW_LEFTRIGHT')
        op_inv.action = 'INVERT_STROKES'
    elif hasattr(layer, "preview_mask"):
        b_row.prop(layer, "preview_mask", text="Inspect", toggle=True, icon='HIDE_OFF')


def _draw_mask_effect_stack(layout, context, node, layer, is_2d_only=False):
    """Draw the Substance-Painter-style Multi-Layer Mask Stack (Fill + Grunge + Generator + Paint + Levels)."""
    st = _get_layer_grunge_state(node, layer)
    effects = st.get("effects", [])
    act_uid = st.get("active_effect_uid", 0)
    act_eff = _get_active_mask_effect(node, layer)
    wm = getattr(context, "window_manager", None)

    # Show the Reveal/Hide Mask Brush Bar at the top only when painting a plain base mask (no sub-layers yet)
    if (getattr(node, "paint_target", 'COLOR') == 'MASK' or layer.type != 'PAINT') and not effects:
        _draw_mask_brush_bar(layout, context, node, layer, is_paint_sublayer=False)
        layout.separator(factor=0.25)

    # 1. Mask Sub-Layer Stack List + Right-Side Column (Up ▲, Add +, Down ▼)
    stack_row = layout.row(align=False)
    list_box = stack_row.box()
    list_col = list_box.column(align=True)

    type_icons = {
        'GENERATOR': 'MODIFIER',
        'PAINT': 'BRUSH_DATA',
        'GRUNGE': 'TEXTURE',
        'FILTER': 'SHADERFX',
        'LEVELS': 'SEQ_LUMA_WAVEFORM',
    }

    if not effects:
        base_is_black = int(st.get("base_value", 255)) < 128
        empty_row = list_col.row(align=True)
        empty_row.label(
            text="Base Mask (Black)" if base_is_black else "Base Mask (White)",
            icon='SHADING_SOLID' if base_is_black else 'LIGHT_SUN',
        )
        empty_row.menu("TEXPAINT_MT_add_mask_effect", text="Add Sub-Layer", icon='ADD')
    else:
        for eff in reversed(effects):
            e_uid = int(eff["uid"])
            is_sel = (e_uid == act_uid)
            enabled = bool(eff.get("enabled", True))
            etype = eff.get("type", "GRUNGE")
            if etype == 'GRUNGE' and eff.get("category") == 'CUSTOM':
                icon = 'FILE_IMAGE'
            else:
                icon = type_icons.get(etype, 'MOD_MASK')
            b_short = _BLEND_SHORT_LABELS.get(eff.get("blend_mode", "REPLACE"), "Norm")
            op_pct = int(round(float(eff.get("opacity", 1.0)) * 100.0))

            erow = list_col.row(align=True)
            sel_op = erow.operator(
                "node.tex_paint_mask_effect_select",
                text=f"{eff.get('name', 'Mask')}  [{b_short} {op_pct}%]",
                icon=icon,
                depress=is_sel,
                emboss=True,
            )
            sel_op.effect_uid = e_uid

            inv_op = erow.operator(
                "node.tex_paint_mask_effect_toggle",
                text="",
                icon='ARROW_LEFTRIGHT',
                depress=bool(eff.get("invert", False)),
            )
            inv_op.effect_uid = e_uid
            inv_op.prop = 'INVERT'

            vis_op = erow.operator(
                "node.tex_paint_mask_effect_toggle",
                text="",
                icon='HIDE_OFF' if enabled else 'HIDE_ON',
                depress=not enabled,
            )
            vis_op.effect_uid = e_uid
            vis_op.prop = 'ENABLED'

            rem_op = erow.operator("node.tex_paint_mask_effect_remove", text="", icon='X')
            rem_op.effect_uid = e_uid

    # Side Column: Up Arrow (▲), + Add Dropdown in the middle, Down Arrow (▼)
    side_col = stack_row.column(align=True)
    op_up = side_col.operator("node.tex_paint_mask_effect_move", text="", icon='TRIA_UP')
    op_up.direction = 'UP'
    side_col.menu("TEXPAINT_MT_add_mask_effect", text="", icon='ADD')
    op_dn = side_col.operator("node.tex_paint_mask_effect_move", text="", icon='TRIA_DOWN')
    op_dn.direction = 'DOWN'

    # 2. Contextual Inspector for the Active Mask Sub-Layer
    if act_eff is None:
        return

    layout.separator(factor=0.35)
    etype = act_eff.get("type", "GRUNGE")

    # Compact Single-Row Combine Mode & Opacity Bar (Substance Painter style)
    comb_row = layout.row(align=True)
    comb_split = comb_row.split(factor=0.56, align=True)
    comb_split.prop(node, "tex_paint_grunge_blend_mode", text="")
    comb_split.prop(node, "tex_paint_mask_eff_opacity", text="Opacity", slider=True)

    layout.separator(factor=0.3)

    if etype == 'GRUNGE':
        cat = act_eff.get("category", "BITMAP_GRUNGES")
        pmode = act_eff.get("proj_mode", "TRIPLANAR")
        lock_s = bool(act_eff.get("lock_scale", True))

        # -------------------------------------------------------------------
        # STEP 1 (Substance Painter Order): Projection & Transform
        # -------------------------------------------------------------------
        p_col = layout.column(align=True)

        # Row 1: Projection Method + Repeat/Clamp Wrap + Reset Icon (all on 1 clean row!)
        proj_row = p_col.row(align=True)
        proj_split = proj_row.split(factor=0.64, align=True)
        proj_split.prop(node, "tex_paint_grunge_proj_mode", text="")
        proj_split.prop(node, "tex_paint_fill_wrap_mode", text="")
        op_rst = proj_row.operator("node.tex_paint_fill_transform_action", text="", icon='LOOP_BACK')
        op_rst.action = 'RESET'

        # Row 2: Scale (Uniform vs Non-Uniform X/Y + Lock Aspect + Match Image Aspect)
        s_row = p_col.row(align=True)
        if lock_s:
            s_row.prop(node, "tex_paint_grunge_scale", text="Scale", slider=True)
            s_row.prop(node, "tex_paint_grunge_lock_scale", text="", toggle=True, icon='LOCKED')
        else:
            s_row.prop(node, "tex_paint_grunge_scale_x", text="Scale X", slider=True)
            s_row.prop(node, "tex_paint_grunge_scale_y", text="Y", slider=True)
            s_row.prop(node, "tex_paint_grunge_lock_scale", text="", toggle=True, icon='UNLOCKED')
        op_asp = s_row.operator("node.tex_paint_fill_transform_action", text="", icon='FULLSCREEN_ENTER')
        op_asp.action = 'MATCH_ASPECT'

        # Row 3: Offset X & Offset Y + Center Button
        off_row = p_col.row(align=True)
        off_row.prop(node, "tex_paint_grunge_offset_x", text="Offset X", slider=True)
        off_row.prop(node, "tex_paint_grunge_offset_y", text="Y", slider=True)
        op_ctr = off_row.operator("node.tex_paint_fill_transform_action", text="", icon='PIVOT_CURSOR')
        op_ctr.action = 'CENTER'

        # Row 4: Rotation + 90-degree step buttons
        rot_row = p_col.row(align=True)
        rot_row.prop(node, "tex_paint_grunge_rotation", text="Rotation", slider=True)
        op_ccw = rot_row.operator("node.tex_paint_fill_transform_action", text="-90°")
        op_ccw.action = 'ROT_90_CCW'
        op_cw = rot_row.operator("node.tex_paint_fill_transform_action", text="+90°")
        op_cw.action = 'ROT_90_CW'

        # Row 5: Falloff / Warp Depth + 3D Viewport Manipulator or 2D UV Image Editor Gizmo on one row
        has_falloff = pmode in {'TRIPLANAR', 'DECAL', 'WARP'}
        has_3d_gizmo = (not is_2d_only) and (pmode in _3D_GIZMO_PROJ_MODES)
        has_uv_gizmo = (not is_2d_only) and (pmode in {'REPEAT', 'UV', 'FIT'})
        if has_falloff or has_3d_gizmo or has_uv_gizmo:
            fg_row = p_col.row(align=True)
            if has_falloff:
                falloff_labels = {
                    'TRIPLANAR': "Blend",
                    'DECAL': "Falloff",
                    'WARP': "Warp Depth",
                }
                fg_row.prop(node, "tex_paint_grunge_box_blend", text=falloff_labels.get(pmode, "Falloff"), slider=True)
            if has_3d_gizmo:
                fg_row.prop(
                    layer,
                    "show_projection_widget",
                    text="3D Gizmo" if not has_falloff else "",
                    toggle=True,
                    icon='GIZMO',
                )
                fg_row.operator("node.tex_paint_layer_snap_decal", text="Surface" if not has_falloff else "", icon='SNAP_FACE_NEAREST')
                fg_row.operator("node.tex_paint_layer_align_view", text="View" if not has_falloff else "", icon='RESTRICT_VIEW_OFF')
            elif has_uv_gizmo:
                fg_row.prop(
                    layer,
                    "show_projection_widget",
                    text="2D UV Gizmo",
                    toggle=True,
                    icon='GIZMO',
                )
                fg_row.operator(
                    "image.tex_paint_focus_uv_gizmo",
                    text="Place on UVs",
                    icon='UV',
                )

            if (has_3d_gizmo or has_uv_gizmo) and getattr(layer, "show_projection_widget", False) and wm and hasattr(wm, "tex_paint_gizmo_mode"):
                gm_row = p_col.row(align=True)
                gm_row.prop(wm, "tex_paint_gizmo_mode", expand=True)

        layout.separator(factor=0.35)

        # -------------------------------------------------------------------
        # STEP 2 (Substance Painter Order): Grayscale Image / Grunge Input
        # -------------------------------------------------------------------
        i_col = layout.column(align=True)

        # Source Selector + Open File Icon on one row
        src_row = i_col.row(align=True)
        src_row.prop(node, "tex_paint_fill_source", expand=True)
        src_row.operator("node.tex_paint_grunge_open_image", text="", icon='FILE_FOLDER')

        i_col.separator(factor=0.2)

        if cat == 'CUSTOM':
            cust_row = i_col.row(align=True)
            cust_split = cust_row.split(factor=0.62, align=True)
            cust_split.prop_search(node, "tex_paint_grunge_custom_image", bpy.data, "images", text="", icon='IMAGE_DATA')
            cust_split.prop(node, "tex_paint_fill_channel", text="")

            im_name = act_eff.get("custom_image", "")
            ima = bpy.data.images.get(im_name) if im_name else None
            if ima is not None:
                try:
                    ima.preview_ensure()
                except Exception:
                    pass
                info_row = i_col.row(align=True)
                if getattr(ima, "preview", None) and ima.preview.icon_id:
                    info_row.label(text=f"{ima.name} ({int(ima.size[0])}×{int(ima.size[1])})", icon_value=ima.preview.icon_id)
                else:
                    info_row.label(text=f"{ima.name} ({int(ima.size[0])}×{int(ima.size[1])})", icon='FILE_IMAGE')
                op_ma = info_row.operator("node.tex_paint_fill_transform_action", text="Match Aspect", icon='FULLSCREEN_ENTER')
                op_ma.action = 'MATCH_ASPECT'
            else:
                emp_row = i_col.row(align=True)
                emp_row.operator("node.tex_paint_grunge_open_image", text="Open Image File from Disk...", icon='FILE_IMAGE')
        else:
            # Category + Search on one single row!
            cs_row = i_col.row(align=True)
            cs_split = cs_row.split(factor=0.48, align=True)
            cs_split.prop(node, "tex_paint_grunge_category", text="")
            if hasattr(node, "tex_paint_grunge_search"):
                cs_split.prop(node, "tex_paint_grunge_search", text="", icon='VIEWZOOM', placeholder="Search...")

            i_col.template_icon_view(
                node,
                "tex_paint_grunge_texture",
                show_labels=True,
                scale=3.0,
                scale_popup=4.8,
            )
            nav_row = i_col.row(align=True)
            op_prev = nav_row.operator("node.tex_paint_grunge_step", text="", icon='TRIA_LEFT')
            op_prev.step = -1
            nav_row.prop(node, "tex_paint_grunge_texture", text="")
            op_next = nav_row.operator("node.tex_paint_grunge_step", text="", icon='TRIA_RIGHT')
            op_next.step = 1
            op_rnd = nav_row.operator("node.tex_paint_grunge_randomize", text="", icon='FILE_REFRESH')
            op_rnd.random_map = True

        # Grayscale Balance, Contrast, Invert & Random Offset on one compact row!
        i_col.separator(factor=0.25)
        adj_row = i_col.row(align=True)
        adj_row.prop(node, "tex_paint_grunge_balance", text="Balance", slider=True)
        adj_row.prop(node, "tex_paint_grunge_contrast", text="Contrast", slider=True)
        adj_row.prop(node, "tex_paint_grunge_invert", text="", toggle=True, icon='ARROW_LEFTRIGHT')
        op_seed = adj_row.operator("node.tex_paint_grunge_randomize", text="", icon='RNDCURVE')
        op_seed.random_map = False

    elif etype == 'GENERATOR':
        gen_col = layout.column(align=True)
        # Single-row Generator Type dropdown + Invert toggle (replaces 6-button grid)
        gt_row = gen_col.row(align=True)
        gt_row.prop(node, "tex_paint_mask_eff_gen_type", text="")
        gt_row.prop(node, "tex_paint_grunge_invert", text="", toggle=True, icon='ARROW_LEFTRIGHT')

        bc_row = gen_col.row(align=True)
        bc_row.prop(node, "tex_paint_mask_eff_gen_balance", text="Balance", slider=True)
        bc_row.prop(node, "tex_paint_mask_eff_gen_contrast", text="Contrast", slider=True)

        rn_row = gen_col.row(align=True)
        rn_row.prop(node, "tex_paint_mask_eff_gen_radius", text="Radius", slider=True)
        rn_row.prop(node, "tex_paint_mask_eff_gen_noise", text="3D Noise", slider=True)
        if float(act_eff.get("gen_noise", 0.0)) > 0.001:
            rn_row.prop(node, "tex_paint_mask_eff_gen_noise_scale", text="Scale", slider=True)

        if not is_2d_only:
            g_type = act_eff.get("gen_type", "EDGE_WEAR")
            ao_row = gen_col.row(align=True)
            ao_lbl = "Global Scene AO (Nearby Objects)" if g_type in {'AO', 'CAVITY', 'EDGE_WEAR'} else "Global World-Space Assembly"
            if hasattr(node, "tex_paint_mask_eff_gen_global_ao"):
                ao_row.prop(node, "tex_paint_mask_eff_gen_global_ao", text=ao_lbl, toggle=True, icon='WORLD')
            if wm and hasattr(wm, "tex_paint_show_mesh_maps_hub"):
                ao_row.prop(wm, "tex_paint_show_mesh_maps_hub", text="Mesh Maps", toggle=True, icon='SHADING_RENDERED')

    elif etype == 'PAINT':
        _draw_mask_brush_bar(layout, context, node, layer, is_paint_sublayer=True)

    elif etype == 'FILTER':
        flt_col = layout.column(align=True)
        ft = act_eff.get("filter_type", "BLUR")
        ft_row = flt_col.row(align=True)
        ft_row.prop(node, "tex_paint_mask_eff_filter_type", text="")
        ft_row.prop(node, "tex_paint_grunge_invert", text="", toggle=True, icon='ARROW_LEFTRIGHT')

        rs_row = flt_col.row(align=True)
        if ft not in {'THRESHOLD', 'POSTERIZE'}:
            rs_row.prop(node, "tex_paint_mask_eff_flt_radius", text="Radius", slider=True)
        str_labels = {
            'BLUR': "Intensity",
            'BLUR_SLOPE': "Slope Warp",
            'SHARPEN': "Sharpen",
            'DILATE_ERODE': "Grow (+) / Shrink (-)",
            'BEVEL': "Bevel Width",
            'THRESHOLD': "Threshold",
            'POSTERIZE': "Steps Banding",
            'HIGHPASS': "Strength",
        }
        rs_row.prop(node, "tex_paint_mask_eff_flt_strength", text=str_labels.get(ft, "Strength"), slider=True)
        if ft == 'BLUR_SLOPE':
            sc_row = flt_col.row(align=True)
            sc_row.prop(node, "tex_paint_mask_eff_flt_scale", text="Slope Noise Scale", slider=True)

    elif etype == 'LEVELS':
        lvl_row = layout.row(align=True)
        lvl_row.prop(node, "tex_paint_mask_eff_lvl_balance", text="Balance", slider=True)
        lvl_row.prop(node, "tex_paint_mask_eff_lvl_contrast", text="Contrast", slider=True)
        lvl_row.prop(node, "tex_paint_grunge_invert", text="", toggle=True, icon='ARROW_LEFTRIGHT')


def _draw_mesh_maps_hub(layout, _context, node, wm):
    """Draw the collapsible Substance-Painter-style Mesh Maps & High-Poly Baker Hub."""
    owner = _canonical_mask_owner_node(node)
    st = _get_mesh_maps_state(owner)
    maps = st.get("maps", {})
    linked_count = sum(1 for k, _lbl, _ic in _MESH_MAP_SLOTS if maps.get(k) and bpy.data.images.get(maps.get(k)) is not None)
    expanded = bool(getattr(wm, "tex_paint_show_mesh_maps_hub", False)) if wm else False

    hub_box = layout.box()
    hub_col = hub_box.column(align=True)
    hdr = hub_col.row(align=True)
    if wm and hasattr(wm, "tex_paint_show_mesh_maps_hub"):
        hdr.prop(
            wm,
            "tex_paint_show_mesh_maps_hub",
            text=f"Mesh Maps & Baker ({linked_count}/7)",
            icon='TRIA_DOWN' if expanded else 'TRIA_RIGHT',
            emboss=True,
        )
    else:
        hdr.label(text=f"Mesh Maps & Baker ({linked_count}/7)", icon='SHADING_RENDERED')

    op_bake_all = hdr.operator("node.tex_paint_bake_mesh_maps", text="Bake Mesh Maps", icon='RENDER_STILL')
    op_bake_all.map_type = 'ALL'
    if linked_count > 0 and hasattr(owner, "tex_paint_use_baked_mesh_maps"):
        hdr.prop(owner, "tex_paint_use_baked_mesh_maps", text="", toggle=True, icon='CHECKMARK')

    if not expanded:
        return

    hub_col.separator(factor=0.3)

    # 1. High-Poly / Low-Poly Source & Global AO Baker Settings
    src_mode = st.get("source_mode", "LOW_POLY")
    b_row1 = hub_col.row(align=True)
    b_row1.prop(owner, "tex_paint_bake_source_mode", text="Source")

    if src_mode == 'HIGH_POLY_OB':
        hp_row = hub_col.row(align=True)
        hp_row.prop_search(owner, "tex_paint_bake_high_poly_ob", bpy.data, "objects", text="High-Poly", icon='MESH_ICOSPHERE')
        hp_row.prop(owner, "tex_paint_bake_cage_dist", text="Cage Dist", slider=True)
    elif src_mode == 'HIGH_POLY_COL':
        hp_row = hub_col.row(align=True)
        hp_row.prop_search(owner, "tex_paint_bake_high_poly_col", bpy.data, "collections", text="High-Poly Col", icon='OUTLINER_COLLECTION')
        hp_row.prop(owner, "tex_paint_bake_cage_dist", text="Cage Dist", slider=True)

    b_row2 = hub_col.row(align=True)
    b_row2.prop(owner, "tex_paint_bake_ao_scope", text="AO Scope")
    b_row2.prop(owner, "tex_paint_bake_ao_dist", text="AO Spread", slider=True)

    b_row3 = hub_col.row(align=True)
    b_row3.prop(owner, "tex_paint_bake_res", text="Size")
    b_row3.prop(owner, "tex_paint_bake_apply_normal", text="Wire Normal to BSDF", toggle=True, icon='NODE_SEL')
    if linked_count > 0:
        op_ca = b_row3.operator("node.tex_paint_mesh_map_action", text="", icon='TRASH')
        op_ca.action = 'CLEAR_ALL'

    hub_col.separator(factor=0.35)

    # 2. The 7 Substance Painter Mesh Map Slots (Normal, World Normal, ID, AO, Curvature, Position, Thickness)
    for slot_key, slot_label, slot_icon in _MESH_MAP_SLOTS:
        s_row = hub_col.row(align=True)
        lbl_sub = s_row.row(align=True)
        lbl_sub.ui_units_x = 5.2
        lbl_sub.label(text=slot_label, icon=slot_icon)

        prop_name = f"tex_paint_mesh_map_{slot_key}"
        if hasattr(owner, prop_name):
            s_row.prop_search(owner, prop_name, bpy.data, "images", text="", icon='IMAGE_DATA')

        op_bk = s_row.operator("node.tex_paint_bake_mesh_maps", text="", icon='RENDER_STILL')
        op_bk.map_type = slot_key

        op_im = s_row.operator("node.tex_paint_import_mesh_map", text="", icon='FILE_FOLDER')
        op_im.slot = slot_key

        im_name = maps.get(slot_key, "")
        has_im = bool(im_name and bpy.data.images.get(im_name) is not None)
        if has_im:
            if slot_key == "normal":
                op_con = s_row.operator("node.tex_paint_mesh_map_action", text="", icon='NODE_SEL')
                op_con.action = 'APPLY_NORMAL'
                op_con.slot = slot_key
            op_prv = s_row.operator("node.tex_paint_mesh_map_action", text="", icon='HIDE_OFF')
            op_prv.action = 'PREVIEW_2D'
            op_prv.slot = slot_key
            op_clr = s_row.operator("node.tex_paint_mesh_map_action", text="", icon='X')
            op_clr.action = 'CLEAR_SLOT'
            op_clr.slot = slot_key


def draw_tex_paint_layers(layout, context, node, compact=False):
    ob = getattr(context, "active_object", None)
    mat = ob.active_material if ob else None
    space = getattr(context, "space_data", None)
    is_view3d = bool(space and space.type == 'VIEW_3D')
    is_image_editor = bool(space and space.type == 'IMAGE_EDITOR')
    is_2d_only = _is_standalone_2d_node(node)
    wm = getattr(context, "window_manager", None)

    # 0. Actionable Diagnostic Alerts (Missing UVs, Non-Textured Solid Viewport)
    if is_view3d and ob and ob.type == 'MESH':
        ts = getattr(context, "tool_settings", None)
        ip = getattr(ts, "image_paint", None) if ts else None
        if ip and getattr(ip, "missing_uvs", False):
            uv_box = layout.box()
            uv_row = uv_box.row(align=True)
            uv_row.alert = True
            uv_row.operator("node.tex_paint_quick_uv", text="Missing UVs — Click to Auto-Unwrap (Smart UV)", icon='UV').method = 'SMART'
        shading = getattr(space, "shading", None)
        if shading and shading.type == 'SOLID' and shading.color_type != 'TEXTURE':
            sh_row = layout.row(align=True)
            sh_row.alert = True
            sh_row.operator("node.tex_paint_viewport_shading", text="Viewport Hiding Textures — Click to Show", icon='TEXTURE').mode = 'STUDIO'

    if not compact and is_view3d:
        if ob and len(ob.material_slots) > 1:
            layout.template_list(
                "MATERIAL_UL_matslots", "tex_paint_mats",
                ob, "material_slots",
                ob, "active_material_index", rows=2,
            )

    # 0b. Single Multilayer Texture Node Header + Viewport Lighting Switcher
    flat_slot_node, flat_slot_ima = _find_active_flat_paint_slot_node(mat) if not is_2d_only else (None, None)
    if not is_2d_only and mat is not None:
        if node is not None and node.image is not None:
            _ensure_single_node_pbr_links(mat, node)

        if is_view3d and not compact and getattr(space, "shading", None):
            sh = space.shading
            ov = getattr(space, "overlay", None)
            top_bar = layout.row(align=True)
            top_bar.label(text="Multilayer Material", icon='MATERIAL')
            v_sub = top_bar.row(align=True)
            v_sub.operator(
                "node.tex_paint_viewport_shading",
                text="Flat",
                icon='SHADING_TEXTURE',
                depress=(sh.type == 'SOLID' and sh.light == 'FLAT' and sh.color_type == 'TEXTURE'),
            ).mode = 'FLAT'
            v_sub.operator(
                "node.tex_paint_viewport_shading",
                text="Studio",
                icon='SHADING_SOLID',
                depress=(sh.type == 'SOLID' and sh.light == 'STUDIO' and sh.color_type == 'TEXTURE'),
            ).mode = 'STUDIO'
            v_sub.operator(
                "node.tex_paint_viewport_shading",
                text="PBR",
                icon='SHADING_RENDERED',
                depress=(sh.type == 'MATERIAL'),
            ).mode = 'MATERIAL'
            if ov is not None:
                v_sub.operator(
                    "node.tex_paint_viewport_shading",
                    text="",
                    icon='SHADING_WIRE',
                    depress=bool(ov.show_wireframes),
                ).mode = 'WIREFRAME'

        if flat_slot_node is not None:
            flat_box = layout.box()
            flat_col = flat_box.column(align=True)
            slot_lbl = flat_slot_ima.name if flat_slot_ima else flat_slot_node.name
            flat_col.label(text=f"Selected Slot '{slot_lbl}' is a Flat Image", icon='IMAGE_DATA')
            op_conv = flat_col.operator(
                "node.tex_paint_convert_slot_to_layers",
                text=f"Convert '{slot_lbl}' to Multilayer Node",
                icon='RENDERLAYERS',
            )
            op_conv.slot_index = int(mat.paint_active_slot)
            op_conv.convert_all = False
            return

    if node is not None:
        layout.context_pointer_set("node", node)

    if node is None or node.image is None:
        col = layout.column(align=True)
        col.operator("node.tex_paint_canvas_new", text="New Multilayer Texture Canvas", icon='ADD')
        return

    if not is_2d_only and not compact:
        sharing_obs, _first_c = _find_all_mesh_objects_for_node(node, context)
        if len(sharing_obs) > 1:
            sh_row = layout.row(align=True)
            sh_row.label(text=f"Shared ({len(sharing_obs)} Parts):", icon='LINKED')
            sh_row.operator("node.tex_paint_make_material_unique", text="Re-Bake All", icon='FILE_REFRESH').mode = 'REBAKE_ALL'
            sh_row.operator("node.tex_paint_make_material_unique", text="Unique Part", icon='DUPLICATE').mode = 'ACTIVE_OBJECT'
            sh_row.operator("node.tex_paint_make_material_unique", text="Split All", icon='MOD_EXPLODE').mode = 'ALL_SHARED_OBJECTS'

        _draw_mesh_maps_hub(layout, context, node, wm)

    # 1. Layer Stack List + Right-Side Column (Up ▲, Add + Dropdown in the middle, Down ▼, Remove -, Specials ▾)
    row = layout.row()
    row.template_list(
        "TEXPAINT_UL_layers", "",
        node, "layers",
        node, "active_layer_index",
        rows=4 if compact else 5,
        sort_lock=True,
    )

    col = row.column(align=True)
    col.operator("node.tex_paint_layer_move", icon='TRIA_UP', text="").direction = 'UP'
    col.menu("TEXPAINT_MT_add_layer", icon='ADD', text="")
    col.operator("node.tex_paint_layer_move", icon='TRIA_DOWN', text="").direction = 'DOWN'
    col.separator()
    col.operator("node.tex_paint_layer_remove", icon='REMOVE', text="")
    col.menu("TEXPAINT_MT_layer_specials", icon='DOWNARROW_HLT', text="")

    layer = node.active_layer
    if layer is None:
        return

    # Alert if the active layer is Hidden or Locked
    if layer.hide or layer.lock:
        warn_row = layout.row(align=True)
        warn_row.alert = True
        if layer.hide:
            warn_row.prop(layer, "hide", text="Layer Hidden — Click to Unhide", toggle=True, icon='HIDE_OFF')
        if layer.lock:
            warn_row.prop(layer, "lock", text="Layer Locked — Click to Unlock", toggle=True, icon='UNLOCKED')

    # Determine Mask tab label
    paint_target = getattr(node, "paint_target", 'COLOR')
    if not layer.use_mask:
        mask_tab_lbl = "Mask"
    else:
        st_m = _get_layer_grunge_state(node, layer)
        effs_m = st_m.get("effects", [])
        mask_tab_lbl = f"Mask ({len(effs_m)})" if len(effs_m) > 1 else "Mask"

    # 2. Clean Single-Bar Inspector Header: [ Channels | Mask ] + Quick Layer Flags (Clip, Alpha, Solo, Lock, Presets)
    insp_box = layout.box()
    insp_col = insp_box.column(align=True)

    hdr_bar = insp_col.row(align=True)
    tabs_sub = hdr_bar.row(align=True)
    op_tab_col = tabs_sub.operator(
        "node.tex_paint_layer_target_select",
        text="Channels",
        icon='COLOR' if layer.type == 'FILL' else ('MODIFIER' if layer.type == 'ADJUSTMENT' else 'BRUSH_DATA'),
        depress=(paint_target == 'COLOR'),
    )
    op_tab_col.layer_index = node.active_layer_index
    op_tab_col.target = 'COLOR'

    op_tab_msk = tabs_sub.operator(
        "node.tex_paint_layer_target_select",
        text=mask_tab_lbl,
        icon='MOD_MASK',
        depress=(paint_target == 'MASK'),
    )
    op_tab_msk.layer_index = node.active_layer_index
    op_tab_msk.target = 'MASK'

    hdr_bar.separator(factor=0.5)
    flags_sub = hdr_bar.row(align=True)
    if layer.type == 'PAINT':
        flags_sub.prop(layer, "lock_alpha", text="", toggle=True, icon='IMAGE_ALPHA')
    flags_sub.prop(layer, "use_clipping", text="", toggle=True, icon='TRIA_DOWN_BAR')
    is_solo = _is_layer_soloed(node, layer)
    op_solo_btn = flags_sub.operator(
        "node.tex_paint_layer_solo",
        text="",
        icon='SOLO_ON' if is_solo else 'SOLO_OFF',
        depress=is_solo,
    )
    op_solo_btn.layer_index = node.active_layer_index
    flags_sub.prop(layer, "lock", text="", toggle=True, icon='LOCKED' if layer.lock else 'UNLOCKED')
    if layer.type in {'FILL', 'PAINT'} and not is_2d_only:
        flags_sub.menu("TEXPAINT_MT_fill_channels_and_presets", text="", icon='PRESET')

    # =======================================================================
    # TAB A: LAYER CHANNELS INSPECTOR
    # =======================================================================
    if paint_target == 'COLOR':
        if layer.type in {'FILL', 'ADJUSTMENT'}:
            insp_col.separator(factor=0.3)
            guard_row = insp_col.row(align=True)
            guard_lbl = "Fill Layer — Direct Brush Guarded" if layer.type == 'FILL' else "Adjustment — Direct Brush Guarded"
            guard_row.label(text=guard_lbl, icon='LOCKED')
            op_add_p = guard_row.operator("node.tex_paint_layer_add", text="Paint Layer", icon='ADD')
            op_add_p.type = 'PAINT'
            if layer.use_mask:
                op_pm = guard_row.operator("node.tex_paint_layer_target_select", text="Paint Mask", icon='MOD_MASK')
                op_pm.layer_index = node.active_layer_index
                op_pm.target = 'MASK'
            else:
                op_pm = guard_row.operator("node.tex_paint_mask_stack_init", text="Add Mask", icon='MOD_MASK')
                op_pm.mode = 'WHITE'

        if layer.type in {'FILL', 'PAINT'} and not is_2d_only:
            insp_col.separator(factor=0.35)

            # Compact, Non-Truncating Channel Pill Strip: [ Color | Rough | Metal | Height | Normal | Emit ]
            strip_row = insp_col.row(align=True)
            diagram_channels = (
                ('BASE_COLOR', "Color"),
                ('ROUGHNESS', "Rough"),
                ('METALLIC', "Metal"),
                ('BUMP', "Height"),
                ('NORMAL', "Normal"),
                ('EMISSION', "Emit"),
            )
            for cid, clbl in diagram_channels:
                is_on = _is_fill_channel_enabled(mat, node, layer, cid)
                op_c = strip_row.operator(
                    "node.tex_paint_fill_channel_toggle",
                    text=clbl,
                    depress=is_on,
                )
                op_c.channel = cid
                op_c.action = 'TOGGLE'

            insp_col.separator(factor=0.35)

            # Clean Two-Column Property Sheet for Enabled Channels
            ch_val_col = insp_col.column(align=True)
            ch_val_col.use_property_split = True
            ch_val_col.use_property_decorate = False
            any_ch_on = False
            non_color_ch_on = False

            if _is_fill_channel_enabled(mat, node, layer, 'BASE_COLOR'):
                any_ch_on = True
                if layer.type == 'FILL':
                    c_split = ch_val_col.split(factor=0.4, align=True)
                    c_lbl = c_split.row(align=True)
                    c_lbl.alignment = 'RIGHT'
                    c_lbl.label(text="Base Color")
                    r_sub = c_split.row(align=True)
                    r_sub.prop(layer, "fill_color", text="")
                    has_tex = getattr(layer, "proj_mode", 'NONE') != 'NONE'
                    op_tex = r_sub.operator(
                        "node.tex_paint_fill_layer_texture",
                        text="",
                        icon='TEXTURE',
                        depress=has_tex,
                    )
                    op_tex.action = 'SOLID' if has_tex else 'GRUNGE'
                else:
                    c_split = ch_val_col.split(factor=0.4, align=True)
                    c_lbl = c_split.row(align=True)
                    c_lbl.alignment = 'RIGHT'
                    c_lbl.label(text="Base Color")
                    act_sub = c_split.row(align=True)
                    act_sub.operator("node.tex_paint_layer_pixel_action", text="Fill", icon='COLOR').action = 'FILL_BRUSH'
                    act_sub.operator("node.tex_paint_layer_pixel_action", text="Clear", icon='TRASH').action = 'CLEAR'

            if _is_fill_channel_enabled(mat, node, layer, 'ROUGHNESS'):
                any_ch_on = True
                non_color_ch_on = True
                ch_val_col.prop(layer, "fill_roughness", text="Roughness", slider=True)

            if _is_fill_channel_enabled(mat, node, layer, 'METALLIC'):
                any_ch_on = True
                non_color_ch_on = True
                ch_val_col.prop(layer, "fill_metallic", text="Metallic", slider=True)

            if _is_fill_channel_enabled(mat, node, layer, 'BUMP'):
                any_ch_on = True
                non_color_ch_on = True
                ch_val_col.prop(layer, "fill_height", text="Height", slider=True)
                if hasattr(node, "bump_strength"):
                    ch_val_col.prop(node, "bump_strength", text="Bump Strength", slider=True)

            if _is_fill_channel_enabled(mat, node, layer, 'NORMAL'):
                any_ch_on = True
                non_color_ch_on = True
                n_split = ch_val_col.split(factor=0.4, align=True)
                n_lbl = n_split.row(align=True)
                n_lbl.alignment = 'RIGHT'
                n_lbl.label(text="Normal")
                has_h = _is_fill_channel_enabled(mat, node, layer, 'BUMP')
                n_split.label(
                    text="Replace Height Bump" if has_h else "Flat (Replaces Below)",
                    icon='NORMALS_VERTEX_FACE',
                )

            if _is_fill_channel_enabled(mat, node, layer, 'EMISSION'):
                any_ch_on = True
                non_color_ch_on = True
                ch_val_col.prop(layer, "fill_emission", text="Emission")

            if _is_fill_channel_enabled(mat, node, layer, 'SPECULAR'):
                any_ch_on = True
                non_color_ch_on = True
                ch_val_col.prop(layer, "fill_specular", text="Specular", slider=True)

            if not any_ch_on:
                off_row = insp_col.row(align=True)
                off_row.enabled = False
                off_row.label(text="All channels OFF — passes through from below", icon='INFO')
            elif non_color_ch_on and is_view3d and getattr(space, "shading", None) and space.shading.type == 'SOLID':
                insp_col.separator(factor=0.25)
                sh_hint = insp_col.row(align=True)
                sh_hint.operator(
                    "node.tex_paint_viewport_shading",
                    text="Switch to PBR Viewport to Preview Channels",
                    icon='SHADING_RENDERED',
                ).mode = 'MATERIAL'

        elif layer.type == 'FILL' and is_2d_only:
            insp_col.separator(factor=0.25)
            insp_col.prop(layer, "fill_color", text="Fill Color")

        elif layer.type == 'ADJUSTMENT':
            insp_col.separator(factor=0.25)
            adj_col = insp_col.column(align=True)
            adj_col.use_property_split = True
            adj_col.use_property_decorate = False
            adj_col.prop(layer, "adjust_hsv", index=0, text="Hue", slider=True)
            adj_col.prop(layer, "adjust_hsv", index=1, text="Saturation", slider=True)
            adj_col.prop(layer, "adjust_hsv", index=2, text="Value", slider=True)
            adj_col.prop(layer, "adjust_bright_contrast", index=0, text="Brightness", slider=True)
            adj_col.prop(layer, "adjust_bright_contrast", index=1, text="Contrast", slider=True)

        # Compact Texture / Grunge Projection Sub-Box (shown only when Texture is enabled on the layer)
        has_color_proj = (layer.type in {'PAINT', 'FILL'} and getattr(layer, "proj_mode", 'NONE') != 'NONE')
        if has_color_proj:
            insp_col.separator(factor=0.35)
            proj_box = insp_col.box()
            proj_col = proj_box.column(align=True)

            tex_row = proj_col.row(align=True)
            op_prv = tex_row.operator("node.tex_paint_fill_layer_texture", text="", icon='TRIA_LEFT')
            op_prv.action = 'PREV'
            tex_row.prop_search(layer, "proj_image_name", bpy.data, "images", text="", icon='TEXTURE')
            op_nxt = tex_row.operator("node.tex_paint_fill_layer_texture", text="", icon='TRIA_RIGHT')
            op_nxt.action = 'NEXT'
            op_rnd = tex_row.operator("node.tex_paint_fill_layer_texture", text="", icon='FILE_REFRESH')
            op_rnd.action = 'RANDOM'
            op_clr = tex_row.operator("node.tex_paint_fill_layer_texture", text="", icon='X')
            op_clr.action = 'SOLID'

            is_uv_color_proj = layer.proj_mode in {'FIT', 'UV_REPEAT'}
            hdr = proj_col.row(align=True)
            hdr.prop(node, "tex_paint_color_proj_mode", text="")
            if layer.proj_mode in {'DECAL', 'TRIPLANAR', 'WARP'}:
                lbl_map = {'DECAL': "Falloff", 'TRIPLANAR': "Blend", 'WARP': "Warp"}
                hdr.prop(layer, "proj_blend_falloff", text=lbl_map.get(layer.proj_mode, "Falloff"), slider=True)
            hdr.prop(
                layer,
                "show_projection_widget",
                text="Gizmo",
                toggle=True,
                icon='GIZMO',
            )
            if layer.type == 'PAINT':
                hdr.operator("node.tex_paint_layer_bake_projection", text="Bake", icon='BRUSH_DATA')

            sc_row = proj_col.row(align=True)
            sc_row.prop(layer, "proj_scale", index=0, text="Scale X", slider=True)
            sc_row.prop(layer, "proj_scale", index=1, text="Y", slider=True)
            if not is_uv_color_proj:
                sc_row.prop(layer, "proj_scale", index=2, text="Z", slider=True)

            if getattr(layer, "show_projection_widget", False):
                if is_uv_color_proj:
                    of_row = proj_col.row(align=True)
                    of_row.prop(layer, "proj_location", index=0, text="Offset X", slider=True)
                    of_row.prop(layer, "proj_location", index=1, text="Y", slider=True)
                    of_row.prop(layer, "proj_rotation", index=2, text="Rot", slider=True)
                act_row = proj_col.row(align=True)
                if wm and hasattr(wm, "tex_paint_gizmo_mode"):
                    act_row.prop(wm, "tex_paint_gizmo_mode", expand=True)
                act_row.separator()
                if not is_uv_color_proj:
                    act_row.operator("node.tex_paint_layer_snap_decal", text="", icon='SNAP_FACE_NEAREST')
                    act_row.operator("node.tex_paint_layer_align_view", text="", icon='RESTRICT_VIEW_OFF')
                act_row.operator("node.tex_paint_gizmo_mode", text="", icon='LOOP_BACK').mode = 'RESET_LOC'

    # =======================================================================
    # TAB B: LAYER MASK STACK INSPECTOR
    # =======================================================================
    else:
        insp_col.separator(factor=0.35)
        if not layer.use_mask:
            init_r1 = insp_col.row(align=True)
            op_bm2 = init_r1.operator("node.tex_paint_mask_stack_init", text="Black Mask (Hide)", icon='SHADING_SOLID')
            op_bm2.mode = 'BLACK'
            op_wm2 = init_r1.operator("node.tex_paint_mask_stack_init", text="White Mask (Reveal)", icon='LIGHT_SUN')
            op_wm2.mode = 'WHITE'

            insp_col.separator(factor=0.2)
            init_r2 = insp_col.row(align=True)
            init_r2.menu("TEXPAINT_MT_add_mask_effect", text="Add Mask Effect", icon='ADD')
            if not is_2d_only:
                init_r2.menu("TEXPAINT_MT_smart_mask_recipes", text="Smart Masks", icon='PRESET')
        else:
            hdr_row = insp_col.row(align=True)
            hdr_row.menu("TEXPAINT_MT_add_mask_effect", text="Add Effect", icon='ADD')
            if not is_2d_only:
                hdr_row.menu("TEXPAINT_MT_smart_mask_recipes", text="Smart Masks", icon='PRESET')
            act_sub = hdr_row.row(align=True)
            if hasattr(layer, "preview_mask"):
                act_sub.prop(layer, "preview_mask", text="", toggle=True, icon='HIDE_OFF')
            act_sub.prop(layer, "invert_mask", text="", toggle=True, icon='ARROW_LEFTRIGHT')
            op_rem = act_sub.operator("node.tex_paint_mask_stack_init", text="", icon='X')
            op_rem.mode = 'REMOVE'

            insp_col.separator(factor=0.3)
            _draw_mask_effect_stack(insp_col, context, node, layer, is_2d_only=is_2d_only)

    if not compact:
        if bool(node.get("_tex_paint_baked_set_active", False)):
            baked_row = layout.row(align=True)
            baked_row.alert = True
            baked_row.operator(
                "node.tex_paint_reconnect_live_layers",
                text="Baked Texture Set Active — Switch Back to Live Layers",
                icon='FILE_REFRESH',
            )
        foot_row = layout.row(align=True)
        foot_row.label(
            text=iface_("{:d}x{:d} {:s}").format(
                node.canvas_width, node.canvas_height, iface_("Float") if node.use_float else iface_("Byte"),
            ),
            icon='IMAGE_RGB_ALPHA',
        )
        foot_row.operator("node.tex_paint_canvas_resize", text="Resize", icon='FULLSCREEN_ENTER')
        foot_row.operator("node.tex_paint_export_texture_set", text="Export Set", icon='EXPORT')


class VIEW3D_PT_tex_paint_layers(Panel):
    """Paint Layers panel in the 3D Viewport N-toolbar (Tool tab) when using a Texture Paint brush."""
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Tool"
    bl_label = "Paint Layers"
    bl_order = 0

    @classmethod
    def poll(cls, context):
        if context.space_data is None:
            return False
        if context.region and context.region.type in {'TOOL_HEADER', 'HEADER'}:
            return False
        from bl_ui.properties_paint_common import UnifiedPaintPanel
        return UnifiedPaintPanel.get_brush_mode(context) == 'PAINT_TEXTURE'

    def draw(self, context):
        draw_tex_paint_layers(self.layout, context, tex_paint_node_from_context(context))


class VIEW3D_PT_tex_paint_layers_popover(Panel):
    """Compact Paint Layers & Mask Stack popover accessible from the 3D Viewport & Image Editor headers when the N-panel is closed."""
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'HEADER'
    bl_label = "Paint Layers"
    bl_ui_units_x = 18

    @classmethod
    def poll(cls, _context):
        return True

    def draw(self, context):
        draw_tex_paint_layers(self.layout, context, tex_paint_node_from_context(context), compact=True)


class IMAGE_PT_tex_paint_layers(Panel):
    """Paint Layers panel in the 2D Image Editor — ONLY shown when the chosen image is a multi-layer canvas."""
    bl_space_type = 'IMAGE_EDITOR'
    bl_region_type = 'UI'
    bl_category = "Tool"
    bl_label = "Paint Layers"
    bl_order = 0

    @classmethod
    def poll(cls, context):
        if context.space_data is None or context.space_data.type != 'IMAGE_EDITOR':
            return False
        if context.region and context.region.type in {'TOOL_HEADER', 'HEADER'}:
            return False
        # Do NOT display multi-layer texture painting tools if the user did not choose a multi-layer image!
        return tex_paint_node_from_context(context) is not None

    def draw(self, context):
        draw_tex_paint_layers(self.layout, context, tex_paint_node_from_context(context))


class NODE_PT_tex_paint_layers(Panel):
    bl_space_type = 'NODE_EDITOR'
    bl_region_type = 'UI'
    bl_category = "Node"
    bl_label = "Paint Layers"

    @classmethod
    def poll(cls, context):
        return tex_paint_node_from_context(context) is not None

    def draw(self, context):
        draw_tex_paint_layers(self.layout, context, tex_paint_node_from_context(context))


# ---------------------------------------------------------------------------
# 2D Image Editor Studio (Krita / Photoshop Tools + Send to 3D Model Bridge)
# ---------------------------------------------------------------------------

def _sync_2d_image_to_3d_projections(context, ima):
    """If any 3D mesh material ShaderNodeTexPaint layer projects `ima`, reproject it in real time."""
    if ima is None:
        return
    ima_name = ima.name
    for mat in bpy.data.materials:
        if not mat.node_tree:
            continue
        for node_3d in mat.node_tree.nodes:
            if node_3d.bl_idname != "ShaderNodeTexPaint" or node_3d.image is None:
                continue
            for l_3d in node_3d.layers:
                if l_3d.type == 'PAINT' and getattr(l_3d, "proj_mode", 'NONE') != 'NONE':
                    if getattr(l_3d, "proj_image_name", "") == ima_name:
                        if not _reproject_color_layer_multi_object(node_3d, l_3d, context=context, force=True):
                            l_3d.proj_blend_falloff = l_3d.proj_blend_falloff
                if getattr(l_3d, "use_mask", False):
                    st = _get_layer_grunge_state(node_3d, l_3d)
                    for eff in st.get("effects", []):
                        if eff.get("type") == 'GRUNGE' and eff.get("custom_image") == ima_name:
                            _evaluate_layer_grunge_mask(node_3d, l_3d, context=context, force_reproject=True)
                            break
    _tag_view3d_gizmo_refresh(context)


def _read_active_2d_rgba_f32(context):
    """Read RGBA float32[H, W, 4] from either the active 2D Paint layer (if layered) or space.image (if flat)."""
    import numpy as np
    space = getattr(context, "space_data", None)
    ima = getattr(space, "image", None) if (space and space.type == 'IMAGE_EDITOR') else None
    if ima is None:
        return None, None, None, 0, 0, None

    node = _find_tex_paint_node_for_image(ima, context)
    layer = node.active_layer if node is not None else None
    if node is not None and layer is not None and layer.type == 'PAINT':
        c_layer = _C_TexPaintLayer.from_address(layer.as_pointer())
        if not c_layer.ibuf:
            try:
                layer.opacity = layer.opacity
            except Exception:
                pass
            c_layer = _C_TexPaintLayer.from_address(layer.as_pointer())
        if c_layer.ibuf:
            c_imb = _C_ImBuf.from_address(c_layer.ibuf)
            w, h = int(c_imb.x), int(c_imb.y)
            if w > 0 and h > 0:
                if c_imb.byte_data:
                    buf = (ctypes.c_uint8 * (w * h * 4)).from_address(c_imb.byte_data)
                    arr = np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 4).astype(np.float32) * (1.0 / 255.0)
                    return ima, node, layer, w, h, arr
                elif c_imb.float_data:
                    buf = (ctypes.c_float * (w * h * 4)).from_address(c_imb.float_data)
                    arr = np.frombuffer(buf, dtype=np.float32).reshape(h, w, 4).copy()
                    return ima, node, layer, w, h, arr

    w, h = int(ima.size[0]), int(ima.size[1])
    if w <= 0 or h <= 0:
        return ima, node, layer, 0, 0, None
    arr = np.empty(w * h * 4, dtype=np.float32)
    ima.pixels.foreach_get(arr)
    return ima, None, None, w, h, arr.reshape(h, w, 4)


def _write_active_2d_rgba_f32(context, ima, node, layer, rgba_f32):
    """Write RGBA float32[H, W, 4] back to the active 2D layer or flat image and sync any 3D projections."""
    import numpy as np
    rgba_f32 = np.clip(rgba_f32, 0.0, 1.0).astype(np.float32, copy=False)
    h, w, _ = rgba_f32.shape

    if node is not None and layer is not None and layer.type == 'PAINT':
        c_layer = _C_TexPaintLayer.from_address(layer.as_pointer())
        if c_layer.ibuf:
            c_imb = _C_ImBuf.from_address(c_layer.ibuf)
            if c_imb.byte_data:
                buf = (ctypes.c_uint8 * (w * h * 4)).from_address(c_imb.byte_data)
                dst = np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 4)
                dst[:] = (rgba_f32 * 255.0 + 0.5).astype(np.uint8)
            elif c_imb.float_data:
                buf = (ctypes.c_float * (w * h * 4)).from_address(c_imb.float_data)
                dst = np.frombuffer(buf, dtype=np.float32).reshape(h, w, 4)
                dst[:] = rgba_f32
            c_layer.runtime_dirty = 1
            layer.opacity = layer.opacity
            _sync_2d_image_to_3d_projections(context, ima)
            return

    ima.pixels.foreach_set(rgba_f32.ravel())
    ima.update()
    _sync_2d_image_to_3d_projections(context, ima)


# ---------------------------------------------------------------------------
# Krita-Style 2D Selection Engine (Magic Wand, Marquee, Lasso & Contour Cache)
# ---------------------------------------------------------------------------

def _get_2d_selection_mask(ima, w=0, h=0):
    """Return the active float32[H, W] selection mask in [0..1] for `ima`, or None if no selection is active."""
    if ima is None:
        return None
    info = _2D_SELECTION_STATE.get(ima.name)
    if info is None:
        return None
    m = info.get("mask")
    if m is None:
        return None
    if w > 0 and h > 0 and m.shape != (h, w):
        return None
    return m


def _compute_selection_contour_uv(mask_f32):
    """Extract vectorized pixel-boundary line segments in [0, 1] UV space where mask crosses 0.5."""
    import numpy as np
    h, w = mask_f32.shape
    b = mask_f32 >= 0.5
    if not np.any(b):
        return None, 0.0, None

    ys_all, xs_all = np.nonzero(b)
    pct = float(len(ys_all)) * 100.0 / float(max(w * h, 1))
    bbox = (
        float(xs_all.min()) / float(w),
        float(ys_all.min()) / float(h),
        float(xs_all.max() + 1) / float(w),
        float(ys_all.max() + 1) / float(h),
    )

    segs_u0 = []
    segs_v0 = []
    segs_u1 = []
    segs_v1 = []

    # Vertical edges between col x and col x+1
    vy, vx = np.nonzero(b[:, :-1] != b[:, 1:])
    if len(vx) > 0:
        u = (vx.astype(np.float32) + 1.0) / float(w)
        segs_u0.append(u)
        segs_u1.append(u)
        segs_v0.append(vy.astype(np.float32) / float(h))
        segs_v1.append((vy.astype(np.float32) + 1.0) / float(h))

    # Horizontal edges between row y and row y+1
    hy, hx = np.nonzero(b[:-1, :] != b[1:, :])
    if len(hy) > 0:
        v = (hy.astype(np.float32) + 1.0) / float(h)
        segs_u0.append(hx.astype(np.float32) / float(w))
        segs_u1.append((hx.astype(np.float32) + 1.0) / float(w))
        segs_v0.append(v)
        segs_v1.append(v)

    # Border edges where selection touches canvas edge
    bx0 = np.nonzero(b[:, 0])[0]
    if len(bx0) > 0:
        z = np.zeros(len(bx0), dtype=np.float32)
        segs_u0.append(z)
        segs_u1.append(z)
        segs_v0.append(bx0.astype(np.float32) / float(h))
        segs_v1.append((bx0.astype(np.float32) + 1.0) / float(h))

    bx1 = np.nonzero(b[:, -1])[0]
    if len(bx1) > 0:
        o = np.ones(len(bx1), dtype=np.float32)
        segs_u0.append(o)
        segs_u1.append(o)
        segs_v0.append(bx1.astype(np.float32) / float(h))
        segs_v1.append((bx1.astype(np.float32) + 1.0) / float(h))

    by0 = np.nonzero(b[0, :])[0]
    if len(by0) > 0:
        z = np.zeros(len(by0), dtype=np.float32)
        segs_u0.append(by0.astype(np.float32) / float(w))
        segs_u1.append((by0.astype(np.float32) + 1.0) / float(w))
        segs_v0.append(z)
        segs_v1.append(z)

    by1 = np.nonzero(b[-1, :])[0]
    if len(by1) > 0:
        o = np.ones(len(by1), dtype=np.float32)
        segs_u0.append(by1.astype(np.float32) / float(w))
        segs_u1.append((by1.astype(np.float32) + 1.0) / float(w))
        segs_v0.append(o)
        segs_v1.append(o)

    if not segs_u0:
        return None, pct, bbox

    u0_cat = np.concatenate(segs_u0)
    v0_cat = np.concatenate(segs_v0)
    u1_cat = np.concatenate(segs_u1)
    v1_cat = np.concatenate(segs_v1)

    n_segs = len(u0_cat)
    if n_segs > 12000:
        step = max(1, n_segs // 12000)
        u0_cat = u0_cat[::step]
        v0_cat = v0_cat[::step]
        u1_cat = u1_cat[::step]
        v1_cat = v1_cat[::step]
        n_segs = len(u0_cat)

    pts = np.empty((n_segs * 2, 2), dtype=np.float32)
    pts[0::2, 0] = u0_cat
    pts[0::2, 1] = v0_cat
    pts[1::2, 0] = u1_cat
    pts[1::2, 1] = v1_cat
    return pts, pct, bbox


def _set_2d_selection_mask(ima, mask_f32, context=None):
    """Store `mask_f32` (float32[H, W] in [0..1]) as the active 2D selection for `ima` and refresh the Image Editor."""
    import numpy as np
    if ima is None:
        return
    if mask_f32 is None:
        _2D_SELECTION_STATE.pop(ima.name, None)
        _tag_view3d_gizmo_refresh(context)
        return

    m = np.clip(mask_f32, 0.0, 1.0).astype(np.float32, copy=False)
    if float(m.max()) < 0.01:
        _2D_SELECTION_STATE.pop(ima.name, None)
        _tag_view3d_gizmo_refresh(context)
        return

    contour_uv, pct, bbox = _compute_selection_contour_uv(m)
    if contour_uv is None:
        _2D_SELECTION_STATE.pop(ima.name, None)
    else:
        _2D_SELECTION_STATE[ima.name] = {
            "active": True,
            "mask": m,
            "contour_uv": contour_uv,
            "pct": pct,
            "bbox": bbox,
        }
    _tag_view3d_gizmo_refresh(context)


def _combine_2d_selection(old_mask, new_mask, combine_mode):
    import numpy as np
    if old_mask is None or old_mask.shape != new_mask.shape or combine_mode == 'REPLACE':
        return new_mask
    if combine_mode == 'ADD':
        return np.maximum(old_mask, new_mask)
    if combine_mode == 'SUBTRACT':
        return np.clip(old_mask - new_mask, 0.0, 1.0)
    if combine_mode == 'INTERSECT':
        return np.minimum(old_mask, new_mask)
    return new_mask


def _apply_2d_edit_with_selection(orig_rgba, new_rgba, ima):
    """If `ima` has an active 2D selection mask, blend `new_rgba` only inside the selection mask."""
    h, w, _ = orig_rgba.shape
    sel_m = _get_2d_selection_mask(ima, w, h)
    if sel_m is None:
        return new_rgba
    w_m = sel_m[:, :, None]
    return orig_rgba * (1.0 - w_m) + new_rgba * w_m


def _flood_fill_connected_2d(within_bool, seed_y, seed_x):
    """Fast pure-NumPy 4-connected flood-fill component containing `(seed_y, seed_x)` (< 2ms at 1K-4K)."""
    import numpy as np
    h, w = within_bool.shape
    sy = max(0, min(int(seed_y), h - 1))
    sx = max(0, min(int(seed_x), w - 1))
    if not within_bool[sy, sx]:
        within_bool = within_bool.copy()
        within_bool[sy, sx] = True

    def _dilate_flood(grid_bool, start_y, start_x, max_iters=512):
        gh, gw = grid_bool.shape
        vis = np.zeros((gh, gw), dtype=bool)
        vis[start_y, start_x] = True
        for _ in range(max_iters):
            nxt = vis.copy()
            nxt[1:, :] |= vis[:-1, :]
            nxt[:-1, :] |= vis[1:, :]
            nxt[:, 1:] |= vis[:, :-1]
            nxt[:, :-1] |= vis[:, 1:]
            nxt &= grid_bool
            if np.array_equal(nxt, vis):
                break
            vis = nxt
        return vis

    # Multi-grid acceleration for large canvases (> 256px) so interactive dragging stays > 60 FPS
    stride = max(1, max(h, w) // 192)
    if stride <= 1:
        return _dilate_flood(within_bool, sy, sx, max_iters=max(h, w) * 2)

    coarse = within_bool[::stride, ::stride].copy()
    cy = min(sy // stride, coarse.shape[0] - 1)
    cx = min(sx // stride, coarse.shape[1] - 1)
    coarse[cy, cx] = True
    c_vis = _dilate_flood(coarse, cy, cx, max_iters=coarse.shape[0] + coarse.shape[1] + 64)

    # Dilate coarse mask by 1 coarse cell to cover boundary transitions cleanly, then upsample & refine
    c_pad = c_vis.copy()
    c_pad[1:, :] |= c_vis[:-1, :]
    c_pad[:-1, :] |= c_vis[1:, :]
    c_pad[:, 1:] |= c_vis[:, :-1]
    c_pad[:, :-1] |= c_vis[:, 1:]

    up = np.repeat(np.repeat(c_pad, stride, axis=0), stride, axis=1)[:h, :w]
    fine = up & within_bool
    fine[sy, sx] = True
    return _dilate_flood(c_vis if stride == 1 else fine, sy, sx, max_iters=stride * 2 + 4) if stride <= 2 else fine


def _compute_magic_wand_mask(rgba_f32, seed_x, seed_y, tolerance, contiguous=True, feather_px=0):
    """Compute a [0..1] float32 Magic Wand selection mask from `rgba_f32` around `(seed_x, seed_y)`."""
    import numpy as np
    h, w, _ = rgba_f32.shape
    sx = max(0, min(int(seed_x), w - 1))
    sy = max(0, min(int(seed_y), h - 1))
    seed_col = rgba_f32[sy, sx]
    tol = max(float(tolerance), 0.002)

    # Perceptual weighted max + Euclidean RGB/Alpha distance
    diff = np.abs(rgba_f32 - seed_col[None, None, :])
    # Weight RGB by alpha if both seed and pixel are transparent, otherwise include alpha difference
    if float(seed_col[3]) < 0.02:
        dist = diff[:, :, 3]
    else:
        rgb_dist = np.sqrt((diff[:, :, 0] ** 2 + diff[:, :, 1] ** 2 + diff[:, :, 2] ** 2) * (1.0 / 3.0))
        dist = np.maximum(rgb_dist, diff[:, :, 3] * 0.85)

    within = dist <= tol
    within[sy, sx] = True

    if contiguous:
        conn = _flood_fill_connected_2d(within, sy, sx)
        mask_f = conn.astype(np.float32)
    else:
        soft = max(tol * 0.15, 0.005)
        mask_f = np.clip((tol + soft - dist) / soft, 0.0, 1.0).astype(np.float32)

    if int(feather_px) > 0:
        mask_f = IMAGE_OT_tex_paint_2d_fx._box_blur_2d(mask_f, int(feather_px))
    return mask_f


class IMAGE_OT_tex_paint_new_2d_canvas(Operator):
    """Create a new 2D image canvas optimized for Logos, Decals, Seamless Patterns, or Krita/PS 2D Layers"""
    bl_idname = "image.tex_paint_new_2d_canvas"
    bl_label = "New 2D Logo / Pattern Canvas"
    bl_options = {'REGISTER', 'UNDO'}

    preset: EnumProperty(
        name="Preset",
        items=(
            ('LOGO', "Transparent Logo / Decal", "Create a transparent 2D RGBA canvas for painting a logo, icon, or decal"),
            ('PATTERN', "Seamless Tile Pattern", "Create a 2D canvas and enable X/Y wrap-around tiling for seamless patterns"),
            ('LAYERED_2D', "2D Multi-Layer Document (Krita / PS)", "Create a standalone 2D multi-layer document with Paint, Fill, Adjust & Mask layers"),
        ),
        default='LOGO',
    )
    name: StringProperty(name="Name", default="Logo_Decal")
    resolution: EnumProperty(
        name="Resolution",
        items=(
            ('512', "512 x 512", "Fast 512p stamp / icon canvas"),
            ('1024', "1024 x 1024 (1K)", "Standard 1K logo / pattern canvas"),
            ('2048', "2048 x 2048 (2K)", "High-resolution 2K canvas"),
            ('4096', "4096 x 4096 (4K)", "Ultra-high-resolution 4K canvas"),
        ),
        default='1024',
    )
    bg_color: FloatVectorProperty(
        name="Background",
        subtype='COLOR',
        size=4,
        min=0.0,
        max=1.0,
        default=(0.0, 0.0, 0.0, 0.0),
    )

    @classmethod
    def poll(cls, context):
        return context.space_data is not None and context.space_data.type == 'IMAGE_EDITOR'

    def execute(self, context):
        space = context.space_data
        res = int(self.resolution)
        base_name = self.name.strip() or ("Pattern_Tile" if self.preset == 'PATTERN' else "Logo_Decal")

        if self.preset == 'LAYERED_2D':
            ng = bpy.data.node_groups.get(_2D_STANDALONE_NG_NAME)
            if ng is None:
                ng = bpy.data.node_groups.new(_2D_STANDALONE_NG_NAME, "ShaderNodeTree")
                ng.use_fake_user = True
            node = ng.nodes.new("ShaderNodeTexPaint")
            with context.temp_override(node=node):
                bpy.ops.node.tex_paint_canvas_new(
                    name=base_name,
                    width=res,
                    height=res,
                    color=tuple(self.bg_color),
                )
            ima = node.image
        else:
            ima = bpy.data.images.new(
                name=base_name,
                width=res,
                height=res,
                alpha=True,
                float_buffer=False,
            )
            ima.generated_color = tuple(self.bg_color)

        space.image = ima
        space.ui_mode = 'PAINT'

        ip = getattr(context.tool_settings, "image_paint", None) if context.tool_settings else None
        if ip is not None:
            if self.preset == 'PATTERN':
                ip.tile_x = True
                ip.tile_y = True
                if hasattr(space, "show_repeat"):
                    space.show_repeat = True
            else:
                ip.tile_x = False
                ip.tile_y = False
                if hasattr(space, "show_repeat"):
                    space.show_repeat = False

        _tag_view3d_gizmo_refresh(context)
        return {'FINISHED'}


class IMAGE_OT_tex_paint_2d_layer_mode(Operator):
    """Convert the current 2D image in the Image Editor to a Krita/PS multi-layer document, or flatten back to a single image"""
    bl_idname = "image.tex_paint_2d_layer_mode"
    bl_label = "Toggle 2D Layered Document Mode"
    bl_options = {'REGISTER', 'UNDO'}

    action: EnumProperty(
        name="Action",
        items=(
            ('ENABLE_LAYERS', "Enable 2D Layers (Krita / PS Mode)", "Wrap this 2D image in a standalone multi-layer stack without affecting 3D materials"),
            ('FLATTEN', "Flatten 2D Layers to Single Image", "Bake all 2D layers into a flat standalone image and exit multi-layer mode"),
        ),
        default='ENABLE_LAYERS',
    )

    @classmethod
    def poll(cls, context):
        space = getattr(context, "space_data", None)
        return space is not None and space.type == 'IMAGE_EDITOR' and getattr(space, "image", None) is not None

    def execute(self, context):
        import numpy as np
        space = context.space_data
        ima = space.image
        if ima is None:
            return {'CANCELLED'}

        existing_node = _find_tex_paint_node_for_image(ima, context)

        if self.action == 'ENABLE_LAYERS':
            if existing_node is not None:
                return {'FINISHED'}
            w, h = int(ima.size[0]), int(ima.size[1])
            if w <= 0 or h <= 0:
                w, h = 1024, 1024
                orig_rgba = None
            else:
                orig_rgba = np.empty(w * h * 4, dtype=np.float32)
                ima.pixels.foreach_get(orig_rgba)
                orig_rgba = orig_rgba.reshape(h, w, 4)

            orig_name = ima.name
            ima.name = f"{orig_name}_flat_src"

            ng = bpy.data.node_groups.get(_2D_STANDALONE_NG_NAME)
            if ng is None:
                ng = bpy.data.node_groups.new(_2D_STANDALONE_NG_NAME, "ShaderNodeTree")
                ng.use_fake_user = True
            node = ng.nodes.new("ShaderNodeTexPaint")
            with context.temp_override(node=node):
                bpy.ops.node.tex_paint_canvas_new(
                    name=orig_name,
                    width=w,
                    height=h,
                    color=(0.0, 0.0, 0.0, 0.0),
                )
            new_ima = node.image
            new_ima.name = orig_name
            space.image = new_ima
            space.ui_mode = 'PAINT'

            if orig_rgba is not None and node.active_layer is not None:
                _write_active_2d_rgba_f32(context, new_ima, node, node.active_layer, orig_rgba)
            if ima.users == 0:
                bpy.data.images.remove(ima)

        elif self.action == 'FLATTEN':
            if existing_node is None or not _is_standalone_2d_node(existing_node):
                return {'CANCELLED'}
            w, h = int(ima.size[0]), int(ima.size[1])
            flat_pixels = np.empty(w * h * 4, dtype=np.float32)
            ima.pixels.foreach_get(flat_pixels)

            ng = existing_node.id_data
            ng.nodes.remove(existing_node)
            ima.pixels.foreach_set(flat_pixels)
            ima.update()

        _tag_view3d_gizmo_refresh(context)
        return {'FINISHED'}


class IMAGE_OT_tex_paint_2d_edit(Operator):
    """Krita / Photoshop 2D Canvas & Layer Transform Tools (Flip, Rotate, Wrap Offset 50%, Auto-Center Logo)"""
    bl_idname = "image.tex_paint_2d_edit"
    bl_label = "2D Image / Layer Transform"
    bl_options = {'REGISTER', 'UNDO'}

    action: EnumProperty(
        name="Action",
        items=(
            ('FLIP_H', "Flip Horizontal", "Mirror the 2D image or active paint layer horizontally"),
            ('FLIP_V', "Flip Vertical", "Mirror the 2D image or active paint layer vertically"),
            ('ROT_CW', "Rotate 90° CW", "Rotate the 2D image or active paint layer 90° clockwise"),
            ('ROT_CCW', "Rotate 90° CCW", "Rotate the 2D image or active paint layer 90° counter-clockwise"),
            ('ROT_180', "Rotate 180°", "Rotate the 2D image or active paint layer 180°"),
            ('WRAP_50', "Offset 50% (Check Seams)", "Shift image by (W/2, H/2) with wrap-around to inspect and paint out tile seams"),
            ('CENTER_CONTENT', "Auto-Center Logo", "Center the non-transparent painted logo/shape in the middle of the canvas"),
            ('SYMMETRY_H', "Mirror Left -> Right", "Mirror the left half of the canvas onto the right half for symmetrical logos"),
        ),
        default='FLIP_H',
    )

    @classmethod
    def poll(cls, context):
        space = getattr(context, "space_data", None)
        return space is not None and space.type == 'IMAGE_EDITOR' and getattr(space, "image", None) is not None

    def execute(self, context):
        import numpy as np
        ima, node, layer, w, h, rgba = _read_active_2d_rgba_f32(context)
        if rgba is None:
            return {'CANCELLED'}

        if self.action == 'FLIP_H':
            rgba = rgba[:, ::-1, :].copy()
        elif self.action == 'FLIP_V':
            rgba = rgba[::-1, :, :].copy()
        elif self.action == 'ROT_CW':
            if w == h:
                rgba = np.rot90(rgba, k=1, axes=(0, 1)).copy()
            else:
                rgba = rgba[::-1, ::-1, :].copy()
        elif self.action == 'ROT_CCW':
            if w == h:
                rgba = np.rot90(rgba, k=-1, axes=(0, 1)).copy()
            else:
                rgba = rgba[::-1, ::-1, :].copy()
        elif self.action == 'ROT_180':
            rgba = rgba[::-1, ::-1, :].copy()
        elif self.action == 'WRAP_50':
            rgba = np.roll(np.roll(rgba, h // 2, axis=0), w // 2, axis=1)
        elif self.action == 'SYMMETRY_H':
            mid = w // 2
            rgba[:, w - mid:, :] = rgba[:, :mid, :][:, ::-1, :]
        elif self.action == 'CENTER_CONTENT':
            mask = rgba[:, :, 3] > 0.01
            if np.any(mask):
                ys, xs = np.nonzero(mask)
                cy = (int(ys.min()) + int(ys.max())) // 2
                cx = (int(xs.min()) + int(xs.max())) // 2
                dy = (h // 2) - cy
                dx = (w // 2) - cx
                rgba = np.roll(np.roll(rgba, dy, axis=0), dx, axis=1)

        _write_active_2d_rgba_f32(context, ima, node, layer, rgba)
        return {'FINISHED'}


class IMAGE_OT_tex_paint_2d_shape(Operator):
    """Stamp a parametric 2D vector-style shape (Circle, Ring, Box, Polygon, Star, Crosshair, Stripes) onto the 2D canvas"""
    bl_idname = "image.tex_paint_2d_shape"
    bl_label = "Stamp 2D Shape / Pattern"
    bl_options = {'REGISTER', 'UNDO'}

    shape: EnumProperty(
        name="Shape",
        items=(
            ('CIRCLE', "Circle / Ring", "Crisp anti-aliased circle or ring"),
            ('BOX', "Rounded Box / Frame", "Rounded rectangle or border frame"),
            ('POLYGON', "Polygon / Badge", "Regular polygon badge (Triangle, Hexagon, Octagon)"),
            ('STAR', "Star Emblem", "Multi-pointed star emblem"),
            ('CROSSHAIR', "Crosshair / Reticle", "Tech crosshair reticle with ring and ticks"),
            ('STRIPES', "Hazard / Diagonal Stripes", "Repeating diagonal or linear stripes"),
            ('GRID', "Tech Grid", "Repeating 2D grid pattern"),
        ),
        default='CIRCLE',
    )
    radius: FloatProperty(name="Size / Radius", min=0.02, max=1.0, default=0.38, subtype='FACTOR')
    thickness: FloatProperty(name="Outline Thickness (0 = Filled)", min=0.0, max=0.5, default=0.0, subtype='FACTOR')
    corner_radius: FloatProperty(name="Corner Rounding", min=0.0, max=0.5, default=0.08, subtype='FACTOR')
    sides: bpy.props.IntProperty(name="Sides / Points / Count", min=3, max=24, default=6)
    rotation: FloatProperty(name="Rotation", min=-math.pi, max=math.pi, default=0.0, subtype='ANGLE')
    softness: FloatProperty(name="Edge Feather", min=0.001, max=0.25, default=0.006, subtype='FACTOR')
    erase: BoolProperty(name="Erase (Cutout Alpha)", default=False)

    @classmethod
    def poll(cls, context):
        space = getattr(context, "space_data", None)
        return space is not None and space.type == 'IMAGE_EDITOR' and getattr(space, "image", None) is not None

    def execute(self, context):
        import numpy as np
        ima, node, layer, w, h, rgba = _read_active_2d_rgba_f32(context)
        if rgba is None:
            return {'CANCELLED'}
        orig_rgba = rgba.copy()

        # Read active brush color
        col_rgb, _ = _get_current_brush_colors(context)
        if col_rgb is None:
            col_rgb = (1.0, 1.0, 1.0)

        y_lin = np.linspace(-1.0, 1.0, h, dtype=np.float32)[:, None]
        x_lin = np.linspace(-1.0, 1.0, w, dtype=np.float32)[None, :]
        ca, sa = math.cos(-self.rotation), math.sin(-self.rotation)
        px = x_lin * ca - y_lin * sa
        py = x_lin * sa + y_lin * ca

        r = max(self.radius * 2.0, 0.02)
        if self.shape == 'CIRCLE':
            dist = np.hypot(px, py) - r * 0.5
        elif self.shape == 'BOX':
            cr = min(self.corner_radius, r * 0.45)
            qx = np.abs(px) - (r * 0.5 - cr)
            qy = np.abs(py) - (r * 0.5 - cr)
            dist = np.hypot(np.maximum(qx, 0.0), np.maximum(qy, 0.0)) + np.minimum(np.maximum(qx, qy), 0.0) - cr
        elif self.shape == 'POLYGON':
            n = max(int(self.sides), 3)
            ang = np.arctan2(py, px)
            seg = 2.0 * math.pi / n
            a_mod = np.mod(ang + seg * 0.5, seg) - seg * 0.5
            dist = np.hypot(px, py) * np.cos(a_mod) - r * 0.5
        elif self.shape == 'STAR':
            n = max(int(self.sides), 3)
            ang = np.arctan2(py, px)
            seg = math.pi / n
            a_mod = np.abs(np.mod(ang + seg, 2.0 * seg) - seg) / seg
            rad_star = r * (0.24 + 0.32 * (1.0 - a_mod))
            dist = np.hypot(px, py) - rad_star
        elif self.shape == 'CROSSHAIR':
            d_ring = np.abs(np.hypot(px, py) - r * 0.45) - max(self.thickness, 0.025)
            d_bar_x = np.maximum(np.abs(py) - 0.018, np.abs(np.abs(px) - r * 0.42) - r * 0.22)
            d_bar_y = np.maximum(np.abs(px) - 0.018, np.abs(np.abs(py) - r * 0.42) - r * 0.22)
            d_dot = np.hypot(px, py) - 0.035
            dist = np.minimum(np.minimum(d_ring, d_dot), np.minimum(d_bar_x, d_bar_y))
        elif self.shape == 'STRIPES':
            freq = max(int(self.sides), 2)
            wave = np.sin((px + py) * freq * math.pi)
            dist = -wave * 0.1
        else:  # GRID
            freq = max(int(self.sides), 2)
            gx = np.abs(np.mod(px * freq * 0.5 + 0.5, 1.0) - 0.5)
            gy = np.abs(np.mod(py * freq * 0.5 + 0.5, 1.0) - 0.5)
            th = max(self.thickness, 0.04)
            dist = np.minimum(gx, gy) - th

        if self.thickness > 0.001 and self.shape in {'CIRCLE', 'BOX', 'POLYGON', 'STAR'}:
            dist = np.abs(dist) - self.thickness * 0.5

        aa = max(self.softness * 2.0, 2.0 / max(w, h))
        mask = np.clip(0.5 - dist / aa, 0.0, 1.0)[:, :, None]

        if self.erase:
            rgba[:, :, 3:4] *= (1.0 - mask)
        else:
            src_rgb = np.array(col_rgb, dtype=np.float32).reshape(1, 1, 3)
            out_a = mask + rgba[:, :, 3:4] * (1.0 - mask)
            safe_a = np.maximum(out_a, 1e-6)
            rgba[:, :, :3] = (src_rgb * mask + rgba[:, :, :3] * rgba[:, :, 3:4] * (1.0 - mask)) / safe_a
            rgba[:, :, 3:4] = out_a

        rgba = _apply_2d_edit_with_selection(orig_rgba, rgba, ima)
        _write_active_2d_rgba_f32(context, ima, node, layer, rgba)
        return {'FINISHED'}


class IMAGE_OT_tex_paint_2d_fx(Operator):
    """Krita / Photoshop 2D Image & Logo FX (Outline Stroke, Drop Shadow, Blur, Sharpen, Color to Alpha, Fill, Clear)"""
    bl_idname = "image.tex_paint_2d_fx"
    bl_label = "Apply 2D Image / Logo FX"
    bl_options = {'REGISTER', 'UNDO'}

    effect: EnumProperty(
        name="Effect",
        items=(
            ('OUTLINE', "Outline Alpha (Stroke)", "Draw a colored stroke outline around painted opaque shapes"),
            ('SHADOW', "Drop Shadow / Glow", "Add a soft outer shadow or glow behind transparent logo shapes"),
            ('BLUR', "Gaussian Blur", "Smoothly blur the 2D image or active layer (or active selection)"),
            ('SHARPEN', "Sharpen (Unsharp Mask)", "Enhance crispness and edge contrast"),
            ('COLOR_TO_ALPHA_WHITE', "White BG -> Transparent", "Convert white background into clean alpha transparency (Krita Color-to-Alpha)"),
            ('COLOR_TO_ALPHA_BLACK', "Black BG -> Transparent", "Convert black background into clean alpha transparency"),
            ('INVERT_RGB', "Invert Colors", "Invert RGB channels"),
            ('AUTO_CONTRAST', "Auto Levels / Contrast", "Stretch RGB luminance histogram across [0..1]"),
            ('THRESHOLD', "Threshold (High-Contrast B&W)", "Convert image to crisp black-and-white stencil around midpoint"),
            ('POSTERIZE', "Posterize (Quantize Colors)", "Quantize tonal steps for stylized / cel-shaded decals"),
            ('PIXELATE', "Pixelate / Mosaic", "Pixelate the image or selection into crisp blocks"),
            ('CLEAR_TRANSPARENT', "Clear to Transparent", "Clear the active 2D layer (or active selection) to 100% transparent"),
            ('FILL_BRUSH', "Fill with Brush Color", "Fill the active 2D layer (or active selection) with the current brush color"),
        ),
        default='OUTLINE',
    )
    radius_px: bpy.props.IntProperty(name="Radius / Width (px)", min=1, max=64, default=6)
    strength: FloatProperty(name="Strength / Opacity", min=0.0, max=2.0, default=1.0)

    @classmethod
    def poll(cls, context):
        space = getattr(context, "space_data", None)
        return space is not None and space.type == 'IMAGE_EDITOR' and getattr(space, "image", None) is not None

    @staticmethod
    def _box_blur_2d(arr, radius):
        import numpy as np
        r = max(int(radius), 1)
        for _ in range(2):
            pad_y = np.pad(arr, ((r, r), (0, 0)) if arr.ndim == 2 else ((r, r), (0, 0), (0, 0)), mode='edge')
            cs_y = np.cumsum(pad_y, axis=0, dtype=np.float32)
            arr = (cs_y[2 * r:] - cs_y[:-2 * r]) / float(2 * r)
            pad_x = np.pad(arr, ((0, 0), (r, r)) if arr.ndim == 2 else ((0, 0), (r, r), (0, 0)), mode='edge')
            cs_x = np.cumsum(pad_x, axis=1, dtype=np.float32)
            arr = (cs_x[:, 2 * r:] - cs_x[:, :-2 * r]) / float(2 * r)
        return arr

    def execute(self, context):
        import numpy as np
        ima, node, layer, w, h, rgba = _read_active_2d_rgba_f32(context)
        if rgba is None:
            return {'CANCELLED'}
        orig_rgba = rgba.copy()

        col_rgb, _ = _get_current_brush_colors(context)
        if col_rgb is None:
            col_rgb = (1.0, 1.0, 1.0)

        if self.effect == 'CLEAR_TRANSPARENT':
            rgba[:] = 0.0
        elif self.effect == 'FILL_BRUSH':
            rgba[:, :, 0] = col_rgb[0]
            rgba[:, :, 1] = col_rgb[1]
            rgba[:, :, 2] = col_rgb[2]
            rgba[:, :, 3] = 1.0
        elif self.effect == 'INVERT_RGB':
            rgba[:, :, :3] = 1.0 - rgba[:, :, :3]
        elif self.effect == 'AUTO_CONTRAST':
            op_mask = rgba[:, :, 3] > 0.01
            if np.any(op_mask):
                lo = float(np.percentile(rgba[:, :, :3][op_mask], 1.0))
                hi = float(np.percentile(rgba[:, :, :3][op_mask], 99.0))
                if hi - lo > 1e-4:
                    rgba[:, :, :3] = np.clip((rgba[:, :, :3] - lo) / (hi - lo), 0.0, 1.0)
        elif self.effect == 'THRESHOLD':
            lum = 0.2126 * rgba[:, :, 0] + 0.7152 * rgba[:, :, 1] + 0.0722 * rgba[:, :, 2]
            thr = max(0.05, min(0.95, 1.0 - self.strength * 0.5))
            bw = (lum >= thr).astype(np.float32)
            rgba[:, :, 0] = bw
            rgba[:, :, 1] = bw
            rgba[:, :, 2] = bw
        elif self.effect == 'POSTERIZE':
            steps = float(max(2, min(16, int(self.radius_px))))
            rgba[:, :, :3] = np.round(rgba[:, :, :3] * steps) / steps
        elif self.effect == 'PIXELATE':
            blk = max(2, min(64, int(self.radius_px)))
            small = rgba[::blk, ::blk, :]
            rgba = np.repeat(np.repeat(small, blk, axis=0), blk, axis=1)[:h, :w, :].copy()
        elif self.effect == 'COLOR_TO_ALPHA_WHITE':
            lum = np.max(rgba[:, :, :3], axis=2)
            new_a = np.clip((1.0 - lum) * self.strength, 0.0, 1.0)
            rgba[:, :, 3] *= new_a
        elif self.effect == 'COLOR_TO_ALPHA_BLACK':
            lum = np.max(rgba[:, :, :3], axis=2)
            new_a = np.clip(lum * self.strength, 0.0, 1.0)
            rgba[:, :, 3] *= new_a
        elif self.effect == 'BLUR':
            rgba = self._box_blur_2d(rgba, self.radius_px)
        elif self.effect == 'SHARPEN':
            blurred = self._box_blur_2d(rgba, max(self.radius_px // 2, 1))
            rgba[:, :, :3] = np.clip(rgba[:, :, :3] + (rgba[:, :, :3] - blurred[:, :, :3]) * self.strength, 0.0, 1.0)
        elif self.effect == 'OUTLINE':
            a = rgba[:, :, 3]
            dilated = a.copy()
            r = max(int(self.radius_px), 1)
            for shift in range(1, r + 1):
                dilated = np.maximum(dilated, np.roll(a, shift, axis=0))
                dilated = np.maximum(dilated, np.roll(a, -shift, axis=0))
                dilated = np.maximum(dilated, np.roll(dilated, shift, axis=1))
                dilated = np.maximum(dilated, np.roll(dilated, -shift, axis=1))
            dilated = self._box_blur_2d(dilated, 1)
            ring_a = np.clip((dilated - a) * min(self.strength * 2.0, 2.0), 0.0, 1.0)[:, :, None]
            stroke_rgb = np.array(col_rgb, dtype=np.float32).reshape(1, 1, 3)
            out_a = rgba[:, :, 3:4] + ring_a * (1.0 - rgba[:, :, 3:4])
            safe_a = np.maximum(out_a, 1e-6)
            rgba[:, :, :3] = (rgba[:, :, :3] * rgba[:, :, 3:4] + stroke_rgb * ring_a * (1.0 - rgba[:, :, 3:4])) / safe_a
            rgba[:, :, 3:4] = out_a
        elif self.effect == 'SHADOW':
            a = rgba[:, :, 3]
            r = max(int(self.radius_px), 2)
            shifted = np.roll(np.roll(a, r // 2, axis=0), r // 2, axis=1)
            sh_a = np.clip(self._box_blur_2d(shifted, r) * min(self.strength, 1.5), 0.0, 1.0)[:, :, None]
            out_a = rgba[:, :, 3:4] + sh_a * (1.0 - rgba[:, :, 3:4])
            safe_a = np.maximum(out_a, 1e-6)
            rgba[:, :, :3] = (rgba[:, :, :3] * rgba[:, :, 3:4]) / safe_a
            rgba[:, :, 3:4] = out_a

        rgba = _apply_2d_edit_with_selection(orig_rgba, rgba, ima)
        _write_active_2d_rgba_f32(context, ima, node, layer, rgba)
        return {'FINISHED'}


class IMAGE_OT_tex_paint_2d_magic_wand(Operator):
    """Krita Magic Wand / Contiguous Color Selection Tool (Click pixel in 2D Image Editor, drag left/right to adjust tolerance live; Shift=Add, Alt=Subtract)"""
    bl_idname = "image.tex_paint_2d_magic_wand"
    bl_label = "Magic Wand Select (2D)"
    bl_options = {'REGISTER', 'UNDO'}

    tolerance: FloatProperty(name="Tolerance", min=0.002, max=1.0, default=0.14, subtype='FACTOR')
    contiguous: BoolProperty(name="Contiguous (Flood Fill)", default=True)
    sample_merged: BoolProperty(name="Sample Merged Canvas", default=True)
    feather_px: IntProperty(name="Feather (px)", min=0, max=32, default=0)
    combine_mode: EnumProperty(
        name="Mode",
        items=(
            ('REPLACE', "Replace Selection", "Replace active selection"),
            ('ADD', "Add (Shift)", "Add to active selection"),
            ('SUBTRACT', "Subtract (Alt)", "Subtract from active selection"),
            ('INTERSECT', "Intersect", "Intersect with active selection"),
        ),
        default='REPLACE',
    )
    seed_u: FloatProperty(name="Seed U", min=0.0, max=1.0, default=0.5)
    seed_v: FloatProperty(name="Seed V", min=0.0, max=1.0, default=0.5)

    @classmethod
    def poll(cls, context):
        space = getattr(context, "space_data", None)
        return space is not None and space.type == 'IMAGE_EDITOR' and getattr(space, "image", None) is not None

    def _compute_and_apply(self, context):
        sx = int(round(self.seed_u * (self._w - 1)))
        sy = int(round(self.seed_v * (self._h - 1)))
        new_mask = _compute_magic_wand_mask(
            self._sample_rgba, sx, sy, self.tolerance, contiguous=self.contiguous, feather_px=self.feather_px
        )
        combined = _combine_2d_selection(self._prev_sel, new_mask, self.combine_mode)
        _set_2d_selection_mask(self._ima, combined, context)

    def execute(self, context):
        import numpy as np
        ima, _node, _layer, w, h, rgba = _read_active_2d_rgba_f32(context)
        if rgba is None:
            return {'CANCELLED'}
        self._ima, self._w, self._h = ima, w, h
        if self.sample_merged and int(ima.size[0]) == w and int(ima.size[1]) == h:
            buf = np.empty(w * h * 4, dtype=np.float32)
            ima.pixels.foreach_get(buf)
            self._sample_rgba = buf.reshape(h, w, 4)
        else:
            self._sample_rgba = rgba
        self._prev_sel = _get_2d_selection_mask(ima, w, h)
        self._compute_and_apply(context)
        return {'FINISHED'}

    def invoke(self, context, event):
        import numpy as np
        wm = getattr(context, "window_manager", None)
        if wm is not None:
            self.tolerance = float(getattr(wm, "tex_paint_2d_wand_tolerance", self.tolerance))
            self.contiguous = bool(getattr(wm, "tex_paint_2d_wand_contiguous", self.contiguous))
            self.sample_merged = bool(getattr(wm, "tex_paint_2d_wand_merged", self.sample_merged))
            self.feather_px = int(getattr(wm, "tex_paint_2d_sel_feather", self.feather_px))

        if event.shift and event.alt:
            self.combine_mode = 'INTERSECT'
        elif event.shift:
            self.combine_mode = 'ADD'
        elif event.alt:
            self.combine_mode = 'SUBTRACT'

        ima, _node, _layer, w, h, rgba = _read_active_2d_rgba_f32(context)
        if rgba is None:
            return {'CANCELLED'}
        self._ima, self._w, self._h = ima, w, h
        if self.sample_merged and int(ima.size[0]) == w and int(ima.size[1]) == h:
            buf = np.empty(w * h * 4, dtype=np.float32)
            ima.pixels.foreach_get(buf)
            self._sample_rgba = buf.reshape(h, w, 4)
        else:
            self._sample_rgba = rgba
        self._prev_sel = _get_2d_selection_mask(ima, w, h)
        if self._prev_sel is not None:
            self._prev_sel = self._prev_sel.copy()

        self._waiting_for_click = True
        if context.region and context.region.type == 'WINDOW':
            u, v = context.region.view2d.region_to_view(event.mouse_region_x, event.mouse_region_y)
            if 0.0 <= u <= 1.0 and 0.0 <= v <= 1.0 and event.type == 'LEFTMOUSE' and event.value == 'PRESS':
                self.seed_u = float(u)
                self.seed_v = float(v)
                self._start_x = event.mouse_region_x
                self._start_tol = self.tolerance
                self._waiting_for_click = False
                self._compute_and_apply(context)

        context.window.cursor_modal_set('EYEDROPPER')
        context.area.header_text_set(
            "Magic Wand (2D): Click image to select color region | Drag Left/Right = Adjust Tolerance | Shift=Add, Alt=Subtract | Esc=Cancel"
        )
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type in {'RIGHTMOUSE', 'ESC'}:
            _set_2d_selection_mask(self._ima, self._prev_sel, context)
            context.window.cursor_modal_restore()
            context.area.header_text_set(None)
            return {'CANCELLED'}

        if self._waiting_for_click:
            if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
                u, v = context.region.view2d.region_to_view(event.mouse_region_x, event.mouse_region_y)
                self.seed_u = min(max(float(u), 0.0), 1.0)
                self.seed_v = min(max(float(v), 0.0), 1.0)
                if event.shift and event.alt:
                    self.combine_mode = 'INTERSECT'
                elif event.shift:
                    self.combine_mode = 'ADD'
                elif event.alt:
                    self.combine_mode = 'SUBTRACT'
                self._start_x = event.mouse_region_x
                self._start_tol = self.tolerance
                self._waiting_for_click = False
                self._compute_and_apply(context)
                return {'RUNNING_MODAL'}
            return {'RUNNING_MODAL'}

        if event.type == 'MOUSEMOVE':
            dx = (event.mouse_region_x - self._start_x) / 280.0
            if abs(dx) > 0.008:
                self.tolerance = min(max(self._start_tol + dx, 0.005), 0.98)
                wm = getattr(context, "window_manager", None)
                if wm is not None and hasattr(wm, "tex_paint_2d_wand_tolerance"):
                    wm.tex_paint_2d_wand_tolerance = self.tolerance
                self._compute_and_apply(context)
                context.area.header_text_set(
                    f"Magic Wand Tolerance: {self.tolerance * 100.0:.1f}% ({'Contiguous' if self.contiguous else 'Global Color'}) — Release LMB to confirm"
                )
            return {'RUNNING_MODAL'}

        if event.type in {'LEFTMOUSE', 'RET', 'NUMPAD_ENTER'} and event.value == 'RELEASE':
            context.window.cursor_modal_restore()
            context.area.header_text_set(None)
            return {'FINISHED'}

        return {'RUNNING_MODAL'}


class IMAGE_OT_tex_paint_2d_marquee_select(Operator):
    """Krita Rectangular, Elliptical & Freehand Lasso Selection Tool in the 2D Image Editor (Shift=Add, Alt=Subtract)"""
    bl_idname = "image.tex_paint_2d_marquee_select"
    bl_label = "Marquee / Lasso Select (2D)"
    bl_options = {'REGISTER', 'UNDO'}

    shape: EnumProperty(
        name="Shape",
        items=(
            ('BOX', "Rectangle Select", "Drag a rectangular marquee selection"),
            ('ELLIPSE', "Ellipse / Circle Select", "Drag an elliptical or circular marquee selection"),
            ('LASSO', "Freehand Lasso Select", "Draw a freehand lasso polygon selection"),
        ),
        default='BOX',
    )
    combine_mode: EnumProperty(
        name="Mode",
        items=(
            ('REPLACE', "Replace Selection", "Replace active selection"),
            ('ADD', "Add (Shift)", "Add to active selection"),
            ('SUBTRACT', "Subtract (Alt)", "Subtract from active selection"),
            ('INTERSECT', "Intersect", "Intersect with active selection"),
        ),
        default='REPLACE',
    )
    feather_px: IntProperty(name="Feather (px)", min=0, max=32, default=0)
    u0: FloatProperty(default=0.2)
    v0: FloatProperty(default=0.2)
    u1: FloatProperty(default=0.8)
    v1: FloatProperty(default=0.8)

    @classmethod
    def poll(cls, context):
        space = getattr(context, "space_data", None)
        return space is not None and space.type == 'IMAGE_EDITOR' and getattr(space, "image", None) is not None

    def _rasterize_and_commit(self, context, pts_uv=None):
        import numpy as np
        ima = context.space_data.image
        w, h = int(ima.size[0]), int(ima.size[1])
        if w <= 0 or h <= 0:
            return
        prev_sel = getattr(self, "_prev_sel", _get_2d_selection_mask(ima, w, h))

        ys = (np.arange(h, dtype=np.float32) + 0.5) / float(h)
        xs = (np.arange(w, dtype=np.float32) + 0.5) / float(w)

        if self.shape == 'BOX':
            u_min, u_max = min(self.u0, self.u1), max(self.u0, self.u1)
            v_min, v_max = min(self.v0, self.v1), max(self.v0, self.v1)
            mask = ((xs[None, :] >= u_min) & (xs[None, :] <= u_max) & (ys[:, None] >= v_min) & (ys[:, None] <= v_max)).astype(np.float32)
        elif self.shape == 'ELLIPSE':
            cu = 0.5 * (self.u0 + self.u1)
            cv = 0.5 * (self.v0 + self.v1)
            ru = max(0.5 * abs(self.u1 - self.u0), 1.0 / w)
            rv = max(0.5 * abs(self.v1 - self.v0), 1.0 / h)
            d = ((xs[None, :] - cu) / ru) ** 2 + ((ys[:, None] - cv) / rv) ** 2
            mask = (d <= 1.0).astype(np.float32)
        else:  # LASSO polygon rasterization via vectorized ray-crossing
            poly = pts_uv if pts_uv and len(pts_uv) >= 3 else [(self.u0, self.v0), (self.u1, self.v0), (self.u1, self.v1), (self.u0, self.v1)]
            inside = np.zeros((h, w), dtype=bool)
            gx = xs[None, :]
            gy = ys[:, None]
            n = len(poly)
            for i in range(n):
                x1, y1 = poly[i]
                x2, y2 = poly[(i + 1) % n]
                cond = (y1 > gy) != (y2 > gy)
                dy = y2 - y1
                if abs(dy) > 1e-7:
                    x_int = (x2 - x1) * (gy - y1) / dy + x1
                    inside ^= cond & (gx < x_int)
            mask = inside.astype(np.float32)

        if int(self.feather_px) > 0:
            mask = IMAGE_OT_tex_paint_2d_fx._box_blur_2d(mask, int(self.feather_px))
        combined = _combine_2d_selection(prev_sel, mask, self.combine_mode)
        _set_2d_selection_mask(ima, combined, context)

    def execute(self, context):
        self._rasterize_and_commit(context)
        return {'FINISHED'}

    def invoke(self, context, event):
        wm = getattr(context, "window_manager", None)
        if wm is not None:
            self.feather_px = int(getattr(wm, "tex_paint_2d_sel_feather", self.feather_px))
        if event.shift and event.alt:
            self.combine_mode = 'INTERSECT'
        elif event.shift:
            self.combine_mode = 'ADD'
        elif event.alt:
            self.combine_mode = 'SUBTRACT'

        ima = context.space_data.image
        w, h = int(ima.size[0]), int(ima.size[1])
        self._prev_sel = _get_2d_selection_mask(ima, w, h)
        if self._prev_sel is not None:
            self._prev_sel = self._prev_sel.copy()

        self._dragging = False
        self._lasso_pts = []
        if context.region and context.region.type == 'WINDOW' and event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            u, v = context.region.view2d.region_to_view(event.mouse_region_x, event.mouse_region_y)
            if 0.0 <= u <= 1.0 and 0.0 <= v <= 1.0:
                self.u0 = self.u1 = float(u)
                self.v0 = self.v1 = float(v)
                self._lasso_pts = [(float(u), float(v))]
                self._dragging = True

        context.window.cursor_modal_set('CROSSHAIR')
        context.area.header_text_set(
            f"2D {self.shape.title()} Select: Click & drag on image | Shift=Add, Alt=Subtract | Esc=Cancel"
        )
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type in {'RIGHTMOUSE', 'ESC'}:
            _2D_MODAL_DRAG_PREVIEW["active"] = False
            context.window.cursor_modal_restore()
            context.area.header_text_set(None)
            _tag_view3d_gizmo_refresh(context)
            return {'CANCELLED'}

        if not self._dragging:
            if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
                u, v = context.region.view2d.region_to_view(event.mouse_region_x, event.mouse_region_y)
                if event.shift and event.alt:
                    self.combine_mode = 'INTERSECT'
                elif event.shift:
                    self.combine_mode = 'ADD'
                elif event.alt:
                    self.combine_mode = 'SUBTRACT'
                self.u0 = self.u1 = float(u)
                self.v0 = self.v1 = float(v)
                self._lasso_pts = [(float(u), float(v))]
                self._dragging = True
            return {'RUNNING_MODAL'}

        if event.type == 'MOUSEMOVE':
            u, v = context.region.view2d.region_to_view(event.mouse_region_x, event.mouse_region_y)
            self.u1 = float(u)
            self.v1 = float(v)
            if self.shape == 'LASSO':
                if not self._lasso_pts or math.hypot(u - self._lasso_pts[-1][0], v - self._lasso_pts[-1][1]) > 0.004:
                    self._lasso_pts.append((float(u), float(v)))
                pts = self._lasso_pts + [self._lasso_pts[0]]
            elif self.shape == 'ELLIPSE':
                cu, cv = 0.5 * (self.u0 + self.u1), 0.5 * (self.v0 + self.v1)
                ru, rv = 0.5 * (self.u1 - self.u0), 0.5 * (self.v1 - self.v0)
                pts = [(cu + ru * math.cos(a), cv + rv * math.sin(a)) for a in [i * 2.0 * math.pi / 40.0 for i in range(41)]]
            else:
                pts = [(self.u0, self.v0), (self.u1, self.v0), (self.u1, self.v1), (self.u0, self.v1), (self.u0, self.v0)]
            _2D_MODAL_DRAG_PREVIEW["active"] = True
            _2D_MODAL_DRAG_PREVIEW["mode"] = self.shape
            _2D_MODAL_DRAG_PREVIEW["points_uv"] = pts
            if context.area:
                context.area.tag_redraw()
            return {'RUNNING_MODAL'}

        if event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
            _2D_MODAL_DRAG_PREVIEW["active"] = False
            context.window.cursor_modal_restore()
            context.area.header_text_set(None)
            self._rasterize_and_commit(context, self._lasso_pts)
            return {'FINISHED'}

        return {'RUNNING_MODAL'}


class IMAGE_OT_tex_paint_2d_select_action(Operator):
    """Krita 2D Selection Modifiers & Pixel Actions (Invert, Grow, Shrink, Feather, Delete Pixels, Fill, Stroke, Extract to Layer, Convert to Mask)"""
    bl_idname = "image.tex_paint_2d_select_action"
    bl_label = "2D Selection Action"
    bl_options = {'REGISTER', 'UNDO'}

    action: EnumProperty(
        name="Action",
        items=(
            ('SELECT_ALL', "Select All", "Select the entire 2D canvas"),
            ('DESELECT', "Deselect (Clear Selection)", "Clear the active 2D selection"),
            ('INVERT_SEL', "Invert Selection", "Invert the active 2D selection"),
            ('SELECT_OPAQUE', "Select Opaque Pixels (Alpha > 0)", "Select all non-transparent pixels on the active layer or image"),
            ('GROW', "Grow / Expand Selection", "Expand the active selection outward by Radius px"),
            ('SHRINK', "Shrink / Contract Selection", "Contract the active selection inward by Radius px"),
            ('FEATHER', "Feather / Smooth Selection", "Soften the edges of the active selection by Radius px"),
            ('BORDER', "Border Selection (Ring)", "Convert the active selection into a border ring of width Radius px"),
            ('DELETE_PIXELS', "Delete / Erase Selected Pixels", "Erase pixels inside the active selection to transparent"),
            ('CROP_TO_SEL', "Keep Only Selection (Erase Outside)", "Erase all pixels outside the active selection (Cutout / Crop)"),
            ('FILL_SEL', "Fill Selection with Brush Color", "Fill the active selection with the current brush color"),
            ('STROKE_SEL', "Stroke Selection Outline", "Draw a colored stroke outline along the active selection border"),
            ('EXTRACT_LAYER', "Duplicate Selection to New 2D Layer", "Copy the selected pixels onto a new 2D layer above"),
            ('CUT_TO_LAYER', "Cut Selection to New 2D Layer", "Move (cut) the selected pixels onto a new 2D layer above"),
            ('SEL_TO_MASK', "Convert Selection to Layer Mask", "Apply the active 2D selection as the layer mask on the active layer"),
        ),
        default='INVERT_SEL',
    )
    radius_px: IntProperty(name="Radius / Width (px)", min=1, max=64, default=4)

    @classmethod
    def poll(cls, context):
        space = getattr(context, "space_data", None)
        return space is not None and space.type == 'IMAGE_EDITOR' and getattr(space, "image", None) is not None

    @staticmethod
    def _dilate_2d(mask, r):
        import numpy as np
        out = mask.copy()
        for shift in range(1, max(int(r), 1) + 1):
            out = np.maximum(out, np.roll(mask, shift, axis=0))
            out = np.maximum(out, np.roll(mask, -shift, axis=0))
            out = np.maximum(out, np.roll(out, shift, axis=1))
            out = np.maximum(out, np.roll(out, -shift, axis=1))
        return out

    @staticmethod
    def _erode_2d(mask, r):
        import numpy as np
        out = mask.copy()
        for shift in range(1, max(int(r), 1) + 1):
            out = np.minimum(out, np.roll(mask, shift, axis=0))
            out = np.minimum(out, np.roll(mask, -shift, axis=0))
            out = np.minimum(out, np.roll(out, shift, axis=1))
            out = np.minimum(out, np.roll(out, -shift, axis=1))
        return out

    def execute(self, context):
        import numpy as np
        ima, node, layer, w, h, rgba = _read_active_2d_rgba_f32(context)
        if rgba is None:
            return {'CANCELLED'}
        sel = _get_2d_selection_mask(ima, w, h)

        if self.action == 'SELECT_ALL':
            _set_2d_selection_mask(ima, np.ones((h, w), dtype=np.float32), context)
            return {'FINISHED'}
        if self.action == 'DESELECT':
            _set_2d_selection_mask(ima, None, context)
            return {'FINISHED'}
        if self.action == 'SELECT_OPAQUE':
            _set_2d_selection_mask(ima, np.clip(rgba[:, :, 3], 0.0, 1.0).copy(), context)
            return {'FINISHED'}

        if sel is None:
            if self.action == 'INVERT_SEL':
                _set_2d_selection_mask(ima, np.ones((h, w), dtype=np.float32), context)
                return {'FINISHED'}
            # Default to opaque pixels if user clicks a selection modifier without an active selection
            sel = (rgba[:, :, 3] > 0.02).astype(np.float32)
            if not np.any(sel > 0.01):
                sel = np.ones((h, w), dtype=np.float32)

        if self.action == 'INVERT_SEL':
            _set_2d_selection_mask(ima, 1.0 - sel, context)
            return {'FINISHED'}
        if self.action == 'GROW':
            _set_2d_selection_mask(ima, self._dilate_2d(sel, self.radius_px), context)
            return {'FINISHED'}
        if self.action == 'SHRINK':
            _set_2d_selection_mask(ima, self._erode_2d(sel, self.radius_px), context)
            return {'FINISHED'}
        if self.action == 'FEATHER':
            _set_2d_selection_mask(ima, IMAGE_OT_tex_paint_2d_fx._box_blur_2d(sel, self.radius_px), context)
            return {'FINISHED'}
        if self.action == 'BORDER':
            dil = self._dilate_2d(sel, max(self.radius_px // 2, 1))
            ero = self._erode_2d(sel, max(self.radius_px - self.radius_px // 2, 1))
            _set_2d_selection_mask(ima, np.clip(dil - ero, 0.0, 1.0), context)
            return {'FINISHED'}

        col_rgb, _ = _get_current_brush_colors(context)
        if col_rgb is None:
            col_rgb = (1.0, 1.0, 1.0)
        m3 = np.clip(sel, 0.0, 1.0)[:, :, None]

        if self.action == 'DELETE_PIXELS':
            rgba[:, :, 3:4] *= (1.0 - m3)
            _write_active_2d_rgba_f32(context, ima, node, layer, rgba)
        elif self.action == 'CROP_TO_SEL':
            rgba[:, :, 3:4] *= m3
            _write_active_2d_rgba_f32(context, ima, node, layer, rgba)
        elif self.action == 'FILL_SEL':
            src_rgb = np.array(col_rgb, dtype=np.float32).reshape(1, 1, 3)
            out_a = m3 + rgba[:, :, 3:4] * (1.0 - m3)
            safe_a = np.maximum(out_a, 1e-6)
            rgba[:, :, :3] = (src_rgb * m3 + rgba[:, :, :3] * rgba[:, :, 3:4] * (1.0 - m3)) / safe_a
            rgba[:, :, 3:4] = out_a
            _write_active_2d_rgba_f32(context, ima, node, layer, rgba)
        elif self.action == 'STROKE_SEL':
            dil = self._dilate_2d(sel, max(self.radius_px, 1))
            ero = self._erode_2d(sel, 1)
            ring = np.clip(dil - ero, 0.0, 1.0)[:, :, None]
            src_rgb = np.array(col_rgb, dtype=np.float32).reshape(1, 1, 3)
            out_a = ring + rgba[:, :, 3:4] * (1.0 - ring)
            safe_a = np.maximum(out_a, 1e-6)
            rgba[:, :, :3] = (src_rgb * ring + rgba[:, :, :3] * rgba[:, :, 3:4] * (1.0 - ring)) / safe_a
            rgba[:, :, 3:4] = out_a
            _write_active_2d_rgba_f32(context, ima, node, layer, rgba)
        elif self.action in {'EXTRACT_LAYER', 'CUT_TO_LAYER'}:
            extracted = rgba.copy()
            extracted[:, :, 3:4] *= m3
            if self.action == 'CUT_TO_LAYER':
                rgba[:, :, 3:4] *= (1.0 - m3)
                _write_active_2d_rgba_f32(context, ima, node, layer, rgba)
            if node is None:
                bpy.ops.image.tex_paint_2d_layer_mode(action='ENABLE_LAYERS')
                ima, node, layer, w, h, _ = _read_active_2d_rgba_f32(context)
            if node is not None:
                with context.temp_override(node=node):
                    bpy.ops.node.tex_paint_layer_add(type='PAINT')
                new_layer = node.active_layer
                if new_layer is not None:
                    new_layer.name = "Extracted Selection"
                    _write_active_2d_rgba_f32(context, node.image, node, new_layer, extracted)
        elif self.action == 'SEL_TO_MASK':
            if node is None:
                bpy.ops.image.tex_paint_2d_layer_mode(action='ENABLE_LAYERS')
                ima, node, layer, w, h, _ = _read_active_2d_rgba_f32(context)
            if node is not None and layer is not None:
                if not getattr(layer, "use_mask", False):
                    with context.temp_override(node=node):
                        bpy.ops.node.tex_paint_layer_mask_init(mode='BLACK')
                c_layer = _C_TexPaintLayer.from_address(layer.as_pointer())
                if c_layer.mask_ibuf:
                    c_imb = _C_ImBuf.from_address(c_layer.mask_ibuf)
                    mw, mh = int(c_imb.x), int(c_imb.y)
                    if mw == w and mh == h and c_imb.byte_data:
                        u8 = (np.clip(sel, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8).ravel()
                        rgba_m = np.empty((w * h, 4), dtype=np.uint8)
                        rgba_m[:, 0] = u8
                        rgba_m[:, 1] = u8
                        rgba_m[:, 2] = u8
                        rgba_m[:, 3] = 255
                        ctypes.memmove(c_imb.byte_data, rgba_m.ctypes.data, w * h * 4)
                        c_layer.runtime_dirty = 1
                        layer.opacity = layer.opacity

        return {'FINISHED'}


class IMAGE_OT_tex_paint_2d_bucket_fill(Operator):
    """Krita Contiguous Paint Bucket / Flood Fill Tool in the 2D Image Editor (Click to flood-fill region with Brush Color; drag to adjust tolerance)"""
    bl_idname = "image.tex_paint_2d_bucket_fill"
    bl_label = "Paint Bucket Flood Fill (2D)"
    bl_options = {'REGISTER', 'UNDO'}

    tolerance: FloatProperty(name="Tolerance", min=0.002, max=1.0, default=0.15, subtype='FACTOR')
    contiguous: BoolProperty(name="Contiguous", default=True)
    grow_px: IntProperty(name="Grow / Anti-Gap (px)", min=0, max=8, default=1)
    feather_px: IntProperty(name="Feather (px)", min=0, max=16, default=1)
    erase: BoolProperty(name="Erase to Transparent", default=False)
    seed_u: FloatProperty(default=0.5)
    seed_v: FloatProperty(default=0.5)

    @classmethod
    def poll(cls, context):
        space = getattr(context, "space_data", None)
        return space is not None and space.type == 'IMAGE_EDITOR' and getattr(space, "image", None) is not None

    def _apply_bucket(self, context):
        import numpy as np
        sx = int(round(self.seed_u * (self._w - 1)))
        sy = int(round(self.seed_v * (self._h - 1)))
        fill_mask = _compute_magic_wand_mask(
            self._orig_rgba, sx, sy, self.tolerance, contiguous=self.contiguous, feather_px=0
        )
        if self.grow_px > 0:
            fill_mask = IMAGE_OT_tex_paint_2d_select_action._dilate_2d(fill_mask, self.grow_px)
        if self.feather_px > 0:
            fill_mask = IMAGE_OT_tex_paint_2d_fx._box_blur_2d(fill_mask, self.feather_px)

        sel = _get_2d_selection_mask(self._ima, self._w, self._h)
        if sel is not None:
            fill_mask = fill_mask * np.clip(sel, 0.0, 1.0)

        m3 = fill_mask[:, :, None]
        rgba = self._orig_rgba.copy()
        if self.erase:
            rgba[:, :, 3:4] *= (1.0 - m3)
        else:
            col_rgb, _ = _get_current_brush_colors(context)
            if col_rgb is None:
                col_rgb = (1.0, 1.0, 1.0)
            src_rgb = np.array(col_rgb, dtype=np.float32).reshape(1, 1, 3)
            out_a = m3 + rgba[:, :, 3:4] * (1.0 - m3)
            safe_a = np.maximum(out_a, 1e-6)
            rgba[:, :, :3] = (src_rgb * m3 + rgba[:, :, :3] * rgba[:, :, 3:4] * (1.0 - m3)) / safe_a
            rgba[:, :, 3:4] = out_a

        _write_active_2d_rgba_f32(context, self._ima, self._node, self._layer, rgba)

    def execute(self, context):
        ima, node, layer, w, h, rgba = _read_active_2d_rgba_f32(context)
        if rgba is None:
            return {'CANCELLED'}
        self._ima, self._node, self._layer, self._w, self._h = ima, node, layer, w, h
        self._orig_rgba = rgba.copy()
        self._apply_bucket(context)
        return {'FINISHED'}

    def invoke(self, context, event):
        wm = getattr(context, "window_manager", None)
        if wm is not None:
            self.tolerance = float(getattr(wm, "tex_paint_2d_wand_tolerance", self.tolerance))
            self.contiguous = bool(getattr(wm, "tex_paint_2d_wand_contiguous", self.contiguous))

        ima, node, layer, w, h, rgba = _read_active_2d_rgba_f32(context)
        if rgba is None:
            return {'CANCELLED'}
        self._ima, self._node, self._layer, self._w, self._h = ima, node, layer, w, h
        self._orig_rgba = rgba.copy()
        self._waiting_for_click = True

        if context.region and context.region.type == 'WINDOW' and event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            u, v = context.region.view2d.region_to_view(event.mouse_region_x, event.mouse_region_y)
            if 0.0 <= u <= 1.0 and 0.0 <= v <= 1.0:
                self.seed_u, self.seed_v = float(u), float(v)
                self._start_x = event.mouse_region_x
                self._start_tol = self.tolerance
                self._waiting_for_click = False
                self._apply_bucket(context)

        context.window.cursor_modal_set('PAINT_CROSS')
        context.area.header_text_set(
            "Paint Bucket Fill (2D): Click region to flood-fill with Brush Color | Drag Left/Right = Adjust Tolerance | Esc=Cancel"
        )
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type in {'RIGHTMOUSE', 'ESC'}:
            _write_active_2d_rgba_f32(context, self._ima, self._node, self._layer, self._orig_rgba)
            context.window.cursor_modal_restore()
            context.area.header_text_set(None)
            return {'CANCELLED'}

        if self._waiting_for_click:
            if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
                u, v = context.region.view2d.region_to_view(event.mouse_region_x, event.mouse_region_y)
                self.seed_u = min(max(float(u), 0.0), 1.0)
                self.seed_v = min(max(float(v), 0.0), 1.0)
                self._start_x = event.mouse_region_x
                self._start_tol = self.tolerance
                self._waiting_for_click = False
                self._apply_bucket(context)
            return {'RUNNING_MODAL'}

        if event.type == 'MOUSEMOVE':
            dx = (event.mouse_region_x - self._start_x) / 280.0
            if abs(dx) > 0.008:
                self.tolerance = min(max(self._start_tol + dx, 0.005), 0.98)
                self._apply_bucket(context)
                context.area.header_text_set(f"Paint Bucket Fill Tolerance: {self.tolerance * 100.0:.1f}% — Release LMB to confirm")
            return {'RUNNING_MODAL'}

        if event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
            context.window.cursor_modal_restore()
            context.area.header_text_set(None)
            return {'FINISHED'}

        return {'RUNNING_MODAL'}


class IMAGE_OT_tex_paint_2d_gradient(Operator):
    """Krita Interactive 2D Gradient Tool (Click & drag start->end line in the Image Editor; respects active 2D selection)"""
    bl_idname = "image.tex_paint_2d_gradient"
    bl_label = "Gradient Fill Tool (2D)"
    bl_options = {'REGISTER', 'UNDO'}

    gradient_type: EnumProperty(
        name="Gradient Type",
        items=(
            ('LINEAR', "Linear Gradient", "Linear ramp from start point to end point"),
            ('RADIAL', "Radial Gradient", "Circular ramp from center point to outer radius"),
            ('REFLECTED', "Reflected (Mirrored)", "Symmetric linear gradient mirrored across the start line"),
            ('ANGLE', "Conical / Angle", "Angular sweep around the start point"),
        ),
        default='LINEAR',
    )
    blend_target: EnumProperty(
        name="Colors",
        items=(
            ('FG_TO_TRANSPARENT', "Brush Color -> Transparent", "Fade from Primary Brush Color to Transparent"),
            ('FG_TO_BG', "Primary -> Secondary Brush Color", "Blend from Primary Brush Color to Secondary Brush Color"),
        ),
        default='FG_TO_TRANSPARENT',
    )
    opacity: FloatProperty(name="Opacity", min=0.0, max=1.0, default=1.0, subtype='FACTOR')
    reverse: BoolProperty(name="Reverse Gradient", default=False)
    u0: FloatProperty(default=0.2)
    v0: FloatProperty(default=0.5)
    u1: FloatProperty(default=0.8)
    v1: FloatProperty(default=0.5)

    @classmethod
    def poll(cls, context):
        space = getattr(context, "space_data", None)
        return space is not None and space.type == 'IMAGE_EDITOR' and getattr(space, "image", None) is not None

    def _render_gradient(self, context):
        import numpy as np
        w, h = self._w, self._h
        ys = (np.arange(h, dtype=np.float32) + 0.5) / float(h)
        xs = (np.arange(w, dtype=np.float32) + 0.5) / float(w)
        du = self.u1 - self.u0
        dv = self.v1 - self.v0
        dist_sq = max(du * du + dv * dv, 1e-6)
        dist = math.sqrt(dist_sq)

        px = xs[None, :] - self.u0
        py = ys[:, None] - self.v0

        if self.gradient_type == 'LINEAR':
            t = np.clip((px * du + py * dv) / dist_sq, 0.0, 1.0)
        elif self.gradient_type == 'REFLECTED':
            t = np.clip(np.abs((px * du + py * dv) / dist_sq), 0.0, 1.0)
        elif self.gradient_type == 'RADIAL':
            t = np.clip(np.hypot(px, py) / dist, 0.0, 1.0)
        else:  # ANGLE
            ang0 = math.atan2(dv, du)
            ang = np.arctan2(py, px) - ang0
            t = np.mod(ang / (2.0 * math.pi), 1.0)

        if self.reverse:
            t = 1.0 - t

        col1, col2 = _get_current_brush_colors(context)
        if col1 is None:
            col1 = (1.0, 1.0, 1.0)
        if col2 is None:
            col2 = (0.0, 0.0, 0.0)

        c1 = np.array(col1, dtype=np.float32).reshape(1, 1, 3)
        t3 = t[:, :, None]
        if self.blend_target == 'FG_TO_TRANSPARENT':
            grad_rgb = np.broadcast_to(c1, (h, w, 3)).copy()
            grad_a = (1.0 - t3) * self.opacity
        else:
            c2 = np.array(col2, dtype=np.float32).reshape(1, 1, 3)
            grad_rgb = c1 * (1.0 - t3) + c2 * t3
            grad_a = np.full((h, w, 1), self.opacity, dtype=np.float32)

        sel = _get_2d_selection_mask(self._ima, w, h)
        if sel is not None:
            grad_a = grad_a * np.clip(sel, 0.0, 1.0)[:, :, None]

        rgba = self._orig_rgba.copy()
        out_a = grad_a + rgba[:, :, 3:4] * (1.0 - grad_a)
        safe_a = np.maximum(out_a, 1e-6)
        rgba[:, :, :3] = (grad_rgb * grad_a + rgba[:, :, :3] * rgba[:, :, 3:4] * (1.0 - grad_a)) / safe_a
        rgba[:, :, 3:4] = out_a
        _write_active_2d_rgba_f32(context, self._ima, self._node, self._layer, rgba)

    def execute(self, context):
        ima, node, layer, w, h, rgba = _read_active_2d_rgba_f32(context)
        if rgba is None:
            return {'CANCELLED'}
        self._ima, self._node, self._layer, self._w, self._h = ima, node, layer, w, h
        self._orig_rgba = rgba.copy()
        self._render_gradient(context)
        return {'FINISHED'}

    def invoke(self, context, event):
        ima, node, layer, w, h, rgba = _read_active_2d_rgba_f32(context)
        if rgba is None:
            return {'CANCELLED'}
        self._ima, self._node, self._layer, self._w, self._h = ima, node, layer, w, h
        self._orig_rgba = rgba.copy()
        self._dragging = False
        if context.region and context.region.type == 'WINDOW' and event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            u, v = context.region.view2d.region_to_view(event.mouse_region_x, event.mouse_region_y)
            if 0.0 <= u <= 1.0 and 0.0 <= v <= 1.0:
                self.u0 = self.u1 = float(u)
                self.v0 = self.v1 = float(v)
                self._dragging = True

        context.window.cursor_modal_set('CROSSHAIR')
        context.area.header_text_set("Gradient Tool (2D): Click & drag start -> end point on image | Esc=Cancel")
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type in {'RIGHTMOUSE', 'ESC'}:
            _2D_MODAL_DRAG_PREVIEW["active"] = False
            _write_active_2d_rgba_f32(context, self._ima, self._node, self._layer, self._orig_rgba)
            context.window.cursor_modal_restore()
            context.area.header_text_set(None)
            return {'CANCELLED'}

        if not self._dragging:
            if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
                u, v = context.region.view2d.region_to_view(event.mouse_region_x, event.mouse_region_y)
                self.u0 = self.u1 = float(u)
                self.v0 = self.v1 = float(v)
                self._dragging = True
            return {'RUNNING_MODAL'}

        if event.type == 'MOUSEMOVE':
            u, v = context.region.view2d.region_to_view(event.mouse_region_x, event.mouse_region_y)
            self.u1, self.v1 = float(u), float(v)
            _2D_MODAL_DRAG_PREVIEW["active"] = True
            _2D_MODAL_DRAG_PREVIEW["mode"] = 'GRADIENT'
            _2D_MODAL_DRAG_PREVIEW["points_uv"] = [(self.u0, self.v0), (self.u1, self.v1)]
            self._render_gradient(context)
            return {'RUNNING_MODAL'}

        if event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
            _2D_MODAL_DRAG_PREVIEW["active"] = False
            context.window.cursor_modal_restore()
            context.area.header_text_set(None)
            self._render_gradient(context)
            return {'FINISHED'}

        return {'RUNNING_MODAL'}


class IMAGE_OT_tex_paint_2d_free_transform(Operator):
    """Krita Free Transform Tool (Interactive Move, Rotate, and Scale of selected 2D pixels or active 2D layer)"""
    bl_idname = "image.tex_paint_2d_free_transform"
    bl_label = "Free Transform Selection / Layer (2D)"
    bl_options = {'REGISTER', 'UNDO'}

    mode: EnumProperty(
        name="Transform Mode",
        items=(
            ('TRANSLATE', "Move / Translate", "Drag to move selected pixels or layer"),
            ('ROTATE', "Rotate", "Drag to rotate selected pixels or layer around pivot"),
            ('SCALE', "Scale", "Drag to scale selected pixels or layer around pivot"),
        ),
        default='TRANSLATE',
    )
    offset_u: FloatProperty(name="Offset X", min=-1.0, max=1.0, default=0.0)
    offset_v: FloatProperty(name="Offset Y", min=-1.0, max=1.0, default=0.0)
    angle: FloatProperty(name="Rotation", min=-math.pi * 2.0, max=math.pi * 2.0, default=0.0, subtype='ANGLE')
    scale_x: FloatProperty(name="Scale X", min=0.05, max=8.0, default=1.0)
    scale_y: FloatProperty(name="Scale Y", min=0.05, max=8.0, default=1.0)

    @classmethod
    def poll(cls, context):
        space = getattr(context, "space_data", None)
        return space is not None and space.type == 'IMAGE_EDITOR' and getattr(space, "image", None) is not None

    def _apply_transform(self, context):
        import numpy as np
        w, h = self._w, self._h
        ys = (np.arange(h, dtype=np.float32) + 0.5) / float(h)
        xs = (np.arange(w, dtype=np.float32) + 0.5) / float(w)

        du = xs[None, :] - self._piv_u - self.offset_u
        dv = ys[:, None] - self._piv_v - self.offset_v
        ca, sa = math.cos(self.angle), math.sin(self.angle)
        sx = max(self.scale_x, 0.02)
        sy = max(self.scale_y, 0.02)

        src_u = (du * ca + dv * sa) / sx + self._piv_u
        src_v = (-du * sa + dv * ca) / sy + self._piv_v

        valid = (src_u >= 0.0) & (src_u <= 1.0) & (src_v >= 0.0) & (src_v <= 1.0)
        ix = np.clip((src_u * w).astype(np.int32), 0, w - 1)
        iy = np.clip((src_v * h).astype(np.int32), 0, h - 1)

        warped = self._floating_rgba[iy, ix].copy()
        warped[~valid] = 0.0

        out = self._bg_rgba.copy()
        wa = warped[:, :, 3:4]
        out_a = wa + out[:, :, 3:4] * (1.0 - wa)
        safe_a = np.maximum(out_a, 1e-6)
        out[:, :, :3] = (warped[:, :, :3] * wa + out[:, :, :3] * out[:, :, 3:4] * (1.0 - wa)) / safe_a
        out[:, :, 3:4] = out_a

        _write_active_2d_rgba_f32(context, self._ima, self._node, self._layer, out)
        if self._has_sel and self._orig_sel is not None:
            warped_sel = self._orig_sel[iy, ix].copy()
            warped_sel[~valid] = 0.0
            _set_2d_selection_mask(self._ima, warped_sel, context)

    def _init_buffers(self, context):
        import numpy as np
        ima, node, layer, w, h, rgba = _read_active_2d_rgba_f32(context)
        if rgba is None:
            return False
        self._ima, self._node, self._layer, self._w, self._h = ima, node, layer, w, h
        self._orig_rgba = rgba.copy()
        sel = _get_2d_selection_mask(ima, w, h)
        self._orig_sel = sel.copy() if sel is not None else None
        self._has_sel = sel is not None and bool(np.any(sel > 0.01))

        if self._has_sel:
            m3 = np.clip(sel, 0.0, 1.0)[:, :, None]
            self._floating_rgba = rgba.copy()
            self._floating_rgba[:, :, 3:4] *= m3
            self._bg_rgba = rgba.copy()
            self._bg_rgba[:, :, 3:4] *= (1.0 - m3)
            ys, xs = np.nonzero(sel > 0.05)
            self._piv_u = float(xs.mean() / w) if len(xs) else 0.5
            self._piv_v = float(ys.mean() / h) if len(ys) else 0.5
        else:
            self._floating_rgba = rgba.copy()
            self._bg_rgba = np.zeros_like(rgba)
            op = rgba[:, :, 3] > 0.02
            if np.any(op):
                ys, xs = np.nonzero(op)
                self._piv_u = float(xs.mean() / w)
                self._piv_v = float(ys.mean() / h)
            else:
                self._piv_u, self._piv_v = 0.5, 0.5
        return True

    def execute(self, context):
        if not self._init_buffers(context):
            return {'CANCELLED'}
        self._apply_transform(context)
        return {'FINISHED'}

    def invoke(self, context, event):
        if not self._init_buffers(context):
            return {'CANCELLED'}
        self.offset_u = 0.0
        self.offset_v = 0.0
        self.angle = 0.0
        self.scale_x = 1.0
        self.scale_y = 1.0
        self._start_u, self._start_v = context.region.view2d.region_to_view(event.mouse_region_x, event.mouse_region_y)

        context.window.cursor_modal_set('SCROLL_XY')
        context.area.header_text_set(
            f"2D Free Transform ({self.mode.title()}): Drag mouse | Press G=Move, R=Rotate, S=Scale | LMB/Enter=Confirm | Esc=Cancel"
        )
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type in {'RIGHTMOUSE', 'ESC'}:
            _write_active_2d_rgba_f32(context, self._ima, self._node, self._layer, self._orig_rgba)
            if self._orig_sel is not None:
                _set_2d_selection_mask(self._ima, self._orig_sel, context)
            context.window.cursor_modal_restore()
            context.area.header_text_set(None)
            return {'CANCELLED'}

        if event.value == 'PRESS':
            if event.type == 'G':
                self.mode = 'TRANSLATE'
                self._start_u, self._start_v = context.region.view2d.region_to_view(event.mouse_region_x, event.mouse_region_y)
                return {'RUNNING_MODAL'}
            if event.type == 'R':
                self.mode = 'ROTATE'
                self._start_u, self._start_v = context.region.view2d.region_to_view(event.mouse_region_x, event.mouse_region_y)
                return {'RUNNING_MODAL'}
            if event.type == 'S':
                self.mode = 'SCALE'
                self._start_u, self._start_v = context.region.view2d.region_to_view(event.mouse_region_x, event.mouse_region_y)
                return {'RUNNING_MODAL'}

        if event.type == 'MOUSEMOVE':
            u, v = context.region.view2d.region_to_view(event.mouse_region_x, event.mouse_region_y)
            if self.mode == 'TRANSLATE':
                self.offset_u = float(u - self._start_u)
                self.offset_v = float(v - self._start_v)
            elif self.mode == 'ROTATE':
                a0 = math.atan2(self._start_v - self._piv_v, self._start_u - self._piv_u)
                a1 = math.atan2(v - self._piv_v, u - self._piv_u)
                self.angle = float(a1 - a0)
            elif self.mode == 'SCALE':
                d0 = max(math.hypot(self._start_u - self._piv_u, self._start_v - self._piv_v), 0.02)
                d1 = max(math.hypot(u - self._piv_u, v - self._piv_v), 0.01)
                s = min(max(d1 / d0, 0.05), 6.0)
                self.scale_x = s
                self.scale_y = s
            self._apply_transform(context)
            return {'RUNNING_MODAL'}

        if event.type in {'LEFTMOUSE', 'RET', 'NUMPAD_ENTER'} and event.value in {'PRESS', 'RELEASE'}:
            context.window.cursor_modal_restore()
            context.area.header_text_set(None)
            return {'FINISHED'}

        return {'RUNNING_MODAL'}


class IMAGE_OT_tex_paint_2d_color_adjust(Operator):
    """Krita Color Adjustment Dialog (Hue / Saturation / Value, Brightness / Contrast, Gamma & RGB Balance; respects active 2D selection)"""
    bl_idname = "image.tex_paint_2d_color_adjust"
    bl_label = "Krita 2D Color Adjustments (HSV / Levels / Balance)"
    bl_options = {'REGISTER', 'UNDO'}

    hue_shift: FloatProperty(name="Hue Shift", min=-0.5, max=0.5, default=0.0, subtype='FACTOR')
    saturation: FloatProperty(name="Saturation", min=0.0, max=2.5, default=1.0, subtype='FACTOR')
    value_mult: FloatProperty(name="Value / Exposure", min=0.0, max=2.5, default=1.0, subtype='FACTOR')
    brightness: FloatProperty(name="Brightness", min=-1.0, max=1.0, default=0.0, subtype='FACTOR')
    contrast: FloatProperty(name="Contrast", min=-0.95, max=2.0, default=0.0, subtype='FACTOR')
    gamma: FloatProperty(name="Gamma", min=0.2, max=3.0, default=1.0)
    red_gain: FloatProperty(name="Red Balance", min=0.0, max=2.0, default=1.0, subtype='FACTOR')
    green_gain: FloatProperty(name="Green Balance", min=0.0, max=2.0, default=1.0, subtype='FACTOR')
    blue_gain: FloatProperty(name="Blue Balance", min=0.0, max=2.0, default=1.0, subtype='FACTOR')

    @classmethod
    def poll(cls, context):
        space = getattr(context, "space_data", None)
        return space is not None and space.type == 'IMAGE_EDITOR' and getattr(space, "image", None) is not None

    def invoke(self, context, _event):
        return context.window_manager.invoke_props_dialog(self, width=320)

    def draw(self, _context):
        layout = self.layout
        col = layout.column(align=True)
        col.label(text="HSV Adjustment:", icon='COLOR')
        col.prop(self, "hue_shift", slider=True)
        col.prop(self, "saturation", slider=True)
        col.prop(self, "value_mult", slider=True)
        col.separator()
        col.label(text="Levels / Tone:", icon='SEQ_LUMA_WAVEFORM')
        col.prop(self, "brightness", slider=True)
        col.prop(self, "contrast", slider=True)
        col.prop(self, "gamma", slider=True)
        col.separator()
        col.label(text="RGB Color Balance:", icon='IMAGE_RGB')
        row = col.row(align=True)
        row.prop(self, "red_gain", text="R", slider=True)
        row.prop(self, "green_gain", text="G", slider=True)
        row.prop(self, "blue_gain", text="B", slider=True)

    def execute(self, context):
        import numpy as np
        ima, node, layer, _w, _h, rgba = _read_active_2d_rgba_f32(context)
        if rgba is None:
            return {'CANCELLED'}
        orig_rgba = rgba.copy()
        rgb = rgba[:, :, :3].copy()

        # 1. Hue / Saturation / Value (Vectorized RGB <-> HSV)
        if abs(self.hue_shift) > 1e-4 or abs(self.saturation - 1.0) > 1e-4 or abs(self.value_mult - 1.0) > 1e-4:
            r, g, b = rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2]
            cmax = np.maximum(np.maximum(r, g), b)
            cmin = np.minimum(np.minimum(r, g), b)
            delta = cmax - cmin

            h_map = np.zeros_like(cmax)
            nz = delta > 1e-6
            mr = nz & (cmax == r)
            mg = nz & (cmax == g) & ~mr
            mb = nz & ~mr & ~mg
            h_map[mr] = np.mod((g[mr] - b[mr]) / delta[mr], 6.0) / 6.0
            h_map[mg] = ((b[mg] - r[mg]) / delta[mg] + 2.0) / 6.0
            h_map[mb] = ((r[mb] - g[mb]) / delta[mb] + 4.0) / 6.0

            s_map = np.where(cmax > 1e-6, delta / np.maximum(cmax, 1e-6), 0.0)
            h_map = np.mod(h_map + self.hue_shift, 1.0)
            s_map = np.clip(s_map * self.saturation, 0.0, 1.0)
            v_map = np.clip(cmax * self.value_mult, 0.0, 1.0)

            h6 = h_map * 6.0
            i_sec = np.floor(h6).astype(np.int32) % 6
            f_sec = h6 - np.floor(h6)
            p = v_map * (1.0 - s_map)
            q = v_map * (1.0 - f_sec * s_map)
            t = v_map * (1.0 - (1.0 - f_sec) * s_map)

            rgb[:, :, 0] = np.select([i_sec == 0, i_sec == 1, i_sec == 2, i_sec == 3, i_sec == 4], [v_map, q, p, p, t], default=v_map)
            rgb[:, :, 1] = np.select([i_sec == 0, i_sec == 1, i_sec == 2, i_sec == 3, i_sec == 4], [t, v_map, v_map, q, p], default=p)
            rgb[:, :, 2] = np.select([i_sec == 0, i_sec == 1, i_sec == 2, i_sec == 3, i_sec == 4], [p, p, t, v_map, v_map], default=q)

        # 2. RGB Balance
        rgb[:, :, 0] *= self.red_gain
        rgb[:, :, 1] *= self.green_gain
        rgb[:, :, 2] *= self.blue_gain

        # 3. Brightness / Contrast / Gamma
        if abs(self.brightness) > 1e-4 or abs(self.contrast) > 1e-4:
            c_fac = 1.0 + self.contrast if self.contrast >= 0.0 else 1.0 / (1.0 - self.contrast)
            rgb = (rgb - 0.5) * c_fac + 0.5 + self.brightness
        if abs(self.gamma - 1.0) > 1e-4:
            rgb = np.power(np.clip(rgb, 0.0, 1.0), 1.0 / max(self.gamma, 0.05))

        rgba[:, :, :3] = np.clip(rgb, 0.0, 1.0)
        rgba = _apply_2d_edit_with_selection(orig_rgba, rgba, ima)
        _write_active_2d_rgba_f32(context, ima, node, layer, rgba)
        return {'FINISHED'}


class TEXPAINT_MT_2d_select_menu(Menu):
    """Krita 2D Selection & Pixel Actions Menu in the Image Editor."""
    bl_label = "Krita 2D Selection & Pixel Actions"

    def draw(self, context):
        layout = self.layout
        wm = getattr(context, "window_manager", None)
        fx_rad = getattr(wm, "tex_paint_2d_fx_radius", 6) if wm else 6

        layout.operator("image.tex_paint_2d_magic_wand", text="Magic Wand (Contiguous Color)", icon='EYEDROPPER')
        layout.operator("image.tex_paint_2d_marquee_select", text="Rectangle Marquee Select", icon='SELECT_SET').shape = 'BOX'
        layout.operator("image.tex_paint_2d_marquee_select", text="Ellipse / Circle Select", icon='MESH_CIRCLE').shape = 'ELLIPSE'
        layout.operator("image.tex_paint_2d_marquee_select", text="Freehand Lasso Select", icon='GP_SELECT_POINTS').shape = 'LASSO'
        layout.separator()
        layout.operator("image.tex_paint_2d_select_action", text="Select Opaque Pixels (Alpha > 0)", icon='IMAGE_ALPHA').action = 'SELECT_OPAQUE'
        layout.operator("image.tex_paint_2d_select_action", text="Select All", icon='SELECT_EXTEND').action = 'SELECT_ALL'
        layout.operator("image.tex_paint_2d_select_action", text="Invert Selection", icon='ARROW_LEFTRIGHT').action = 'INVERT_SEL'
        layout.operator("image.tex_paint_2d_select_action", text="Deselect (Clear)", icon='X').action = 'DESELECT'
        layout.separator()
        op_gr = layout.operator("image.tex_paint_2d_select_action", text=f"Grow Selection (+{fx_rad} px)", icon='FULLSCREEN_ENTER')
        op_gr.action = 'GROW'
        op_gr.radius_px = fx_rad
        op_sh = layout.operator("image.tex_paint_2d_select_action", text=f"Shrink Selection (-{fx_rad} px)", icon='FULLSCREEN_EXIT')
        op_sh.action = 'SHRINK'
        op_sh.radius_px = fx_rad
        op_fe = layout.operator("image.tex_paint_2d_select_action", text=f"Feather Selection ({fx_rad} px)", icon='SMOOTHCURVE')
        op_fe.action = 'FEATHER'
        op_fe.radius_px = fx_rad
        op_bd = layout.operator("image.tex_paint_2d_select_action", text=f"Border Ring ({fx_rad} px)", icon='MOD_SKIN')
        op_bd.action = 'BORDER'
        op_bd.radius_px = fx_rad
        layout.separator()
        layout.operator("image.tex_paint_2d_select_action", text="Delete / Erase Selected Pixels", icon='TRASH').action = 'DELETE_PIXELS'
        layout.operator("image.tex_paint_2d_select_action", text="Keep Only Selection (Crop Outside)", icon='PIVOT_BOUNDBOX').action = 'CROP_TO_SEL'
        layout.operator("image.tex_paint_2d_select_action", text="Fill Selection with Brush Color", icon='COLOR').action = 'FILL_SEL'
        op_st = layout.operator("image.tex_paint_2d_select_action", text="Stroke Selection Outline", icon='MOD_SKIN')
        op_st.action = 'STROKE_SEL'
        op_st.radius_px = fx_rad
        layout.separator()
        layout.operator("image.tex_paint_2d_select_action", text="Duplicate Selection to New 2D Layer", icon='DUPLICATE').action = 'EXTRACT_LAYER'
        layout.operator("image.tex_paint_2d_select_action", text="Cut Selection to New 2D Layer", icon='COPYDOWN').action = 'CUT_TO_LAYER'
        layout.operator("image.tex_paint_2d_select_action", text="Convert Selection to Layer Mask", icon='MOD_MASK').action = 'SEL_TO_MASK'





class IMAGE_OT_tex_paint_send_to_3d(Operator):
    """Send the current 2D image (Logo, Pattern, or Mask) directly onto the active 3D mesh's Layered Paint Stack"""
    bl_idname = "image.tex_paint_send_to_3d"
    bl_label = "Project 2D Image onto 3D Model"
    bl_options = {'REGISTER', 'UNDO'}

    target_mode: EnumProperty(
        name="3D Target Mode",
        items=(
            ('DECAL', "Project as 3D Decal", "Add this 2D Logo/Image as an interactive 3D Decal layer on the active 3D model"),
            ('TRIPLANAR', "Tile as 3D Box Pattern", "Tile this 2D Pattern seamlessly across the active 3D model using 3D Triplanar Box projection"),
            ('MASK_GRUNGE', "Apply as Grunge / Stencil Mask", "Use this 2D image as a custom mask sub-layer on the active 3D model's layer"),
            ('SYNC', "Refresh 3D Projections", "Re-project this 2D image onto all 3D layers referencing it"),
        ),
        default='DECAL',
    )

    @classmethod
    def poll(cls, context):
        space = getattr(context, "space_data", None)
        if space is None or space.type != 'IMAGE_EDITOR' or getattr(space, "image", None) is None:
            return False
        ob = getattr(context, "active_object", None) or getattr(context, "image_paint_object", None)
        return ob is not None and ob.type == 'MESH'

    def execute(self, context):
        ima = context.space_data.image
        ob = getattr(context, "active_object", None) or getattr(context, "image_paint_object", None)
        if ima is None or ob is None or ob.type != 'MESH':
            return {'CANCELLED'}

        if self.target_mode == 'SYNC':
            _sync_2d_image_to_3d_projections(context, ima)
            return {'FINISHED'}

        node_3d = _find_3d_object_tex_paint_node(context)
        if node_3d is None or node_3d.image is None:
            # Automatically initialize a 3D layered canvas on the active mesh if it doesn't have one yet!
            with context.temp_override(space_data=None):
                bpy.ops.node.tex_paint_canvas_new(name=f"{ob.name}_BaseColor", width=2048, height=2048)
            node_3d = _find_3d_object_tex_paint_node(context)
            if node_3d is None:
                return {'CANCELLED'}

        if self.target_mode in {'DECAL', 'TRIPLANAR'}:
            with context.temp_override(node=node_3d):
                bpy.ops.node.tex_paint_layer_add(type='PAINT')
            layer_3d = node_3d.active_layer
            if layer_3d is not None:
                layer_3d.name = f"{ima.name}_{'Decal' if self.target_mode == 'DECAL' else 'Tile'}"
                c_layer = _C_TexPaintLayer.from_address(layer_3d.as_pointer())
                c_layer.proj_image_name = ima.name.encode('utf-8')[:63]
                dims = ob.dimensions
                avg_ext = max((dims.x + dims.y + dims.z) / 6.0, 0.15)
                if self.target_mode == 'DECAL':
                    layer_3d.proj_scale = (avg_ext * 0.45, avg_ext * 0.45, avg_ext * 0.8)
                    bbox = [Vector(co) for co in ob.bound_box]
                    center = sum(bbox, Vector((0.0, 0.0, 0.0))) / 8.0
                    ok, hit_loc, hit_no, _ = ob.closest_point_on_mesh(center)
                    if ok:
                        layer_3d.proj_location = hit_loc
                        layer_3d.proj_rotation = _rotation_from_normal_and_up(hit_no, Vector((0.0, 0.0, 1.0)))
                    layer_3d.proj_mode = 'DECAL'
                else:
                    layer_3d.proj_scale = (avg_ext, avg_ext, avg_ext)
                    layer_3d.proj_mode = 'TRIPLANAR'
                layer_3d.show_projection_widget = True
                layer_3d.proj_blend_falloff = 0.5
                _reproject_color_layer_multi_object(node_3d, layer_3d, context=context, force=True)

        elif self.target_mode == 'MASK_GRUNGE':
            layer_3d = node_3d.active_layer
            if layer_3d is not None:
                if not layer_3d.use_mask:
                    with context.temp_override(node=node_3d):
                        bpy.ops.node.tex_paint_layer_mask_init(mode='WHITE')
                _update_active_mask_effect(
                    node_3d,
                    layer_3d,
                    {
                        "category": "CUSTOM",
                        "custom_image": ima.name,
                        "name": ima.name[:26],
                    },
                    ensure_type='GRUNGE',
                )
                _evaluate_layer_grunge_mask(node_3d, layer_3d, context=context, force_reproject=True)

        _tag_view3d_gizmo_refresh(context)
        return {'FINISHED'}


class TEXPAINT_MT_2d_canvas_new(Menu):
    """Dropdown menu for creating a new 2D Logo, Seamless Pattern, or Multi-Layer Krita/PS Document."""
    bl_label = "New 2D Canvas"

    def draw(self, context):
        layout = self.layout
        wm = getattr(context, "window_manager", None)
        sel_res = getattr(wm, "tex_paint_2d_new_res", '1024') if wm else '1024'

        op_logo = layout.operator("image.tex_paint_new_2d_canvas", text="New 2D Logo / Decal (Transparent)", icon='IMAGE_ALPHA')
        op_logo.preset = 'LOGO'
        op_logo.resolution = sel_res

        op_pat = layout.operator("image.tex_paint_new_2d_canvas", text="New Seamless Tile Pattern", icon='TEXTURE')
        op_pat.preset = 'PATTERN'
        op_pat.resolution = sel_res

        op_lay = layout.operator("image.tex_paint_new_2d_canvas", text="New 2D Layered Document (Krita / PS)", icon='RENDERLAYERS')
        op_lay.preset = 'LAYERED_2D'
        op_lay.resolution = sel_res

        if wm and hasattr(wm, "tex_paint_2d_new_res"):
            layout.separator()
            layout.props_enum(wm, "tex_paint_2d_new_res")


class TEXPAINT_MT_2d_send_to_3d(Menu):
    """Dropdown menu for projecting or tiling the active 2D image onto the active 3D mesh."""
    bl_label = "Send 2D Image to 3D Model"

    def draw(self, _context):
        layout = self.layout
        op_dec = layout.operator("image.tex_paint_send_to_3d", text="Project as 3D Decal Layer", icon='MOD_UVPROJECT')
        op_dec.target_mode = 'DECAL'
        op_tri = layout.operator("image.tex_paint_send_to_3d", text="Tile as 3D Triplanar Box Layer", icon='MESH_CUBE')
        op_tri.target_mode = 'TRIPLANAR'
        op_msk = layout.operator("image.tex_paint_send_to_3d", text="Use as Custom 3D Mask Fill", icon='MOD_MASK')
        op_msk.target_mode = 'MASK_GRUNGE'
        layout.separator()
        op_syn = layout.operator("image.tex_paint_send_to_3d", text="Sync / Refresh 3D Projections", icon='FILE_REFRESH')
        op_syn.target_mode = 'SYNC'


class TEXPAINT_MT_2d_transform(Menu):
    """Dropdown menu for 2D Image & Layer Transform, Seamless Offset, and Mirror actions."""
    bl_label = "2D Transform & Seamless Actions"

    def draw(self, _context):
        layout = self.layout
        layout.operator("image.tex_paint_2d_edit", text="Flip Horizontal", icon='ARROW_LEFTRIGHT').action = 'FLIP_H'
        layout.operator("image.tex_paint_2d_edit", text="Flip Vertical", icon='SORT_ASC').action = 'FLIP_V'
        layout.operator("image.tex_paint_2d_edit", text="Rotate 90° Clockwise", icon='LOOP_FORWARDS').action = 'ROT_CW'
        layout.operator("image.tex_paint_2d_edit", text="Rotate 90° Counter-Clockwise", icon='LOOP_BACK').action = 'ROT_CCW'
        layout.operator("image.tex_paint_2d_edit", text="Rotate 180°", icon='FILE_REFRESH').action = 'ROT_180'
        layout.separator()
        layout.operator("image.tex_paint_2d_edit", text="Offset +50% X/Y (Inspect Tile Seams)", icon='FULLSCREEN_ENTER').action = 'WRAP_50'
        layout.operator("image.tex_paint_2d_edit", text="Center Opaque Logo / Content", icon='PIVOT_BOUNDBOX').action = 'CENTER_CONTENT'
        layout.operator("image.tex_paint_2d_edit", text="Mirror Left -> Right (Symmetry H)", icon='MOD_MIRROR').action = 'SYMMETRY_H'


class TEXPAINT_MT_2d_shapes(Menu):
    """Dropdown menu for stamping parametric 2D shapes, badges, reticles, and patterns."""
    bl_label = "Stamp 2D Shape / Pattern"

    def draw(self, context):
        layout = self.layout
        wm = getattr(context, "window_manager", None)
        sh_rad = getattr(wm, "tex_paint_2d_shape_radius", 0.38) if wm else 0.38
        sh_thk = getattr(wm, "tex_paint_2d_shape_thickness", 0.0) if wm else 0.0
        sh_sid = getattr(wm, "tex_paint_2d_shape_sides", 6) if wm else 6
        sh_era = getattr(wm, "tex_paint_2d_shape_erase", False) if wm else False

        for sh_id, sh_lbl, sh_icon in (
            ('CIRCLE', "Circle / Ring", 'MESH_CIRCLE'),
            ('BOX', "Rounded Box / Frame", 'MESH_PLANE'),
            ('POLYGON', "Polygon Badge (Hex / Oct)", 'SEQ_CHROMA_SCOPE'),
            ('STAR', "Star Emblem", 'SOLO_ON'),
            ('CROSSHAIR', "Crosshair / Reticle", 'TRACKER'),
            ('STRIPES', "Hazard / Diagonal Stripes", 'MOD_ARRAY'),
            ('GRID', "Tech Grid Pattern", 'MESH_GRID'),
        ):
            op_s = layout.operator("image.tex_paint_2d_shape", text=sh_lbl, icon=sh_icon)
            op_s.shape = sh_id
            op_s.radius = sh_rad
            op_s.thickness = sh_thk
            op_s.sides = sh_sid
            op_s.erase = sh_era


class TEXPAINT_MT_2d_fx(Menu):
    """Dropdown menu for Krita / Photoshop 2D Filters, Outline Stroke, Drop Shadow, and Alpha FX."""
    bl_label = "Apply 2D Filter / Alpha FX"

    def draw(self, context):
        layout = self.layout
        wm = getattr(context, "window_manager", None)
        fx_rad = getattr(wm, "tex_paint_2d_fx_radius", 6) if wm else 6
        fx_str = getattr(wm, "tex_paint_2d_fx_strength", 1.0) if wm else 1.0

        op_out = layout.operator("image.tex_paint_2d_fx", text="Outline Alpha (Stroke)", icon='MOD_SKIN')
        op_out.effect = 'OUTLINE'
        op_out.radius_px = fx_rad
        op_out.strength = fx_str

        op_shd = layout.operator("image.tex_paint_2d_fx", text="Drop Shadow / Outer Glow", icon='MATSHADERBALL')
        op_shd.effect = 'SHADOW'
        op_shd.radius_px = fx_rad
        op_shd.strength = fx_str

        layout.separator()
        op_blr = layout.operator("image.tex_paint_2d_fx", text="Gaussian Blur", icon='SMOOTHCURVE')
        op_blr.effect = 'BLUR'
        op_blr.radius_px = fx_rad

        op_shp = layout.operator("image.tex_paint_2d_fx", text="Sharpen (Unsharp Mask)", icon='SHARPCURVE')
        op_shp.effect = 'SHARPEN'
        op_shp.radius_px = fx_rad
        op_shp.strength = fx_str

        layout.separator()
        op_w2a = layout.operator("image.tex_paint_2d_fx", text="White Background -> Alpha", icon='IMAGE_ALPHA')
        op_w2a.effect = 'COLOR_TO_ALPHA_WHITE'
        op_w2a.strength = fx_str

        op_b2a = layout.operator("image.tex_paint_2d_fx", text="Black Background -> Alpha", icon='IMAGE_ALPHA')
        op_b2a.effect = 'COLOR_TO_ALPHA_BLACK'
        op_b2a.strength = fx_str

        layout.operator("image.tex_paint_2d_fx", text="Invert RGB Colors", icon='ARROW_LEFTRIGHT').effect = 'INVERT_RGB'
        layout.operator("image.tex_paint_2d_fx", text="Auto Levels / Contrast", icon='SEQ_LUMA_WAVEFORM').effect = 'AUTO_CONTRAST'
        op_thr = layout.operator("image.tex_paint_2d_fx", text="Threshold (B&W Stencil)", icon='MOD_MASK')
        op_thr.effect = 'THRESHOLD'
        op_thr.strength = fx_str
        op_pst = layout.operator("image.tex_paint_2d_fx", text="Posterize (Quantize)", icon='IPO_CONSTANT')
        op_pst.effect = 'POSTERIZE'
        op_pst.radius_px = fx_rad
        op_pix = layout.operator("image.tex_paint_2d_fx", text="Pixelate / Mosaic", icon='MESH_GRID')
        op_pix.effect = 'PIXELATE'
        op_pix.radius_px = fx_rad
        layout.separator()
        layout.operator("image.tex_paint_2d_color_adjust", text="HSV / Levels / Color Balance...", icon='COLOR')
        layout.separator()
        layout.operator("image.tex_paint_2d_fx", text="Fill with Brush Color", icon='COLOR').effect = 'FILL_BRUSH'
        layout.operator("image.tex_paint_2d_fx", text="Clear to Transparent", icon='TRASH').effect = 'CLEAR_TRANSPARENT'


class IMAGE_PT_2d_canvas_studio(Panel):
    """Krita / Photoshop 2D Image Editing Studio & 3D Decal/Pattern Workflow Bridge in the Image Editor."""
    bl_space_type = 'IMAGE_EDITOR'
    bl_region_type = 'UI'
    bl_category = "Tool"
    bl_label = "2D Canvas Studio & 3D Bridge"
    bl_order = 1

    @classmethod
    def poll(cls, context):
        if context.space_data is None or context.space_data.type != 'IMAGE_EDITOR':
            return False
        if context.region and context.region.type in {'TOOL_HEADER', 'HEADER'}:
            return False
        return True

    def draw(self, context):
        layout = self.layout
        space = context.space_data
        ima = getattr(space, "image", None)
        wm = getattr(context, "window_manager", None)
        node_2d = tex_paint_node_from_context(context)
        node_3d = _find_3d_object_tex_paint_node(context)
        ob = getattr(context, "active_object", None) or getattr(context, "image_paint_object", None)

        # Row 1: Unified Document, Layer Mode & Send-to-3D Header Bar
        top_row = layout.row(align=True)
        top_row.menu("TEXPAINT_MT_2d_canvas_new", text="New 2D", icon='ADD')
        if wm and hasattr(wm, "tex_paint_2d_new_res"):
            top_row.prop(wm, "tex_paint_2d_new_res", text="")

        if ima is None:
            return

        if node_2d is None:
            op_en = top_row.operator("image.tex_paint_2d_layer_mode", text="2D Layers", icon='RENDERLAYERS')
            op_en.action = 'ENABLE_LAYERS'
        elif _is_standalone_2d_node(node_2d):
            op_fl = top_row.operator("image.tex_paint_2d_layer_mode", text="Flatten", icon='TRIA_DOWN_BAR')
            op_fl.action = 'FLATTEN'

        is_3d_master = (node_3d is not None and node_3d.image == ima)
        if ob is not None and ob.type == 'MESH' and not is_3d_master:
            top_row.menu("TEXPAINT_MT_2d_send_to_3d", text="Send to 3D", icon='EXPORT')

        layout.separator(factor=0.25)

        # Row 2: Krita Selection & Magic Wand Toolbar
        sel_box = layout.box()
        sel_col = sel_box.column(align=True)
        sel_row = sel_col.row(align=True)
        sel_row.operator("image.tex_paint_2d_magic_wand", text="Magic Wand", icon='EYEDROPPER')
        sel_row.operator("image.tex_paint_2d_marquee_select", text="Box", icon='SELECT_SET').shape = 'BOX'
        sel_row.operator("image.tex_paint_2d_marquee_select", text="Ellipse", icon='MESH_CIRCLE').shape = 'ELLIPSE'
        sel_row.operator("image.tex_paint_2d_marquee_select", text="Lasso", icon='GP_SELECT_POINTS').shape = 'LASSO'
        sel_row.menu("TEXPAINT_MT_2d_select_menu", text="", icon='DOWNARROW_HLT')

        if wm and hasattr(wm, "tex_paint_2d_wand_tolerance"):
            wand_row = sel_col.row(align=True)
            wand_row.prop(wm, "tex_paint_2d_wand_tolerance", text="Wand Tol", slider=True)
            wand_row.prop(wm, "tex_paint_2d_wand_contiguous", text="Contig", toggle=True)
            wand_row.prop(wm, "tex_paint_2d_wand_merged", text="Merged", toggle=True)
            wand_row.prop(wm, "tex_paint_2d_sel_feather", text="Feather")

        # Active Selection Quick-Action Bar (appears automatically when a 2D selection is active!)
        sel_st = _2D_SELECTION_STATE.get(ima.name)
        if sel_st and sel_st.get("active") and sel_st.get("mask") is not None:
            act_row = sel_col.row(align=True)
            act_row.alert = False
            act_row.operator("image.tex_paint_2d_select_action", text="Fill", icon='COLOR').action = 'FILL_SEL'
            act_row.operator("image.tex_paint_2d_select_action", text="Erase", icon='TRASH').action = 'DELETE_PIXELS'
            act_row.operator("image.tex_paint_2d_select_action", text="Crop", icon='PIVOT_BOUNDBOX').action = 'CROP_TO_SEL'
            act_row.operator("image.tex_paint_2d_select_action", text="To Layer", icon='DUPLICATE').action = 'EXTRACT_LAYER'
            act_row.operator("image.tex_paint_2d_select_action", text="To Mask", icon='MOD_MASK').action = 'SEL_TO_MASK'
            act_row.operator("image.tex_paint_2d_select_action", text="Invert", icon='ARROW_LEFTRIGHT').action = 'INVERT_SEL'
            act_row.operator("image.tex_paint_2d_select_action", text="", icon='X').action = 'DESELECT'

        # Row 3: Krita Image Editing Tools (Paint Bucket, Gradient, Free Transform, Color Adjust)
        edit_row = layout.row(align=True)
        op_bkt = edit_row.operator("image.tex_paint_2d_bucket_fill", text="Bucket Fill", icon='SNAP_FACE')
        op_bkt.erase = False
        op_era = edit_row.operator("image.tex_paint_2d_bucket_fill", text="Magic Erase", icon='IMAGE_ALPHA')
        op_era.erase = True
        edit_row.operator("image.tex_paint_2d_gradient", text="Gradient", icon='IPO_LINEAR')
        edit_row.operator("image.tex_paint_2d_free_transform", text="Transform", icon='OBJECT_ORIGIN').mode = 'TRANSLATE'
        edit_row.operator("image.tex_paint_2d_color_adjust", text="HSV/Levels", icon='COLOR')

        # Row 4: Seamless Wrap Toggles + Flip/Rotate Menu on one row
        wrap_row = layout.row(align=True)
        ip = getattr(context.tool_settings, "image_paint", None) if context.tool_settings else None
        if ip is not None:
            wrap_row.prop(ip, "tile_x", text="Wrap X", toggle=True, icon='MOD_MIRROR')
            wrap_row.prop(ip, "tile_y", text="Wrap Y", toggle=True, icon='MOD_MIRROR')
        if hasattr(space, "show_repeat"):
            wrap_row.prop(space, "show_repeat", text="Repeat", toggle=True, icon='MESH_GRID')
        wrap_row.menu("TEXPAINT_MT_2d_transform", text="Flip/Rot", icon='ORIENTATION_VIEW')
        wrap_row.operator("image.tex_paint_2d_edit", text="", icon='FULLSCREEN_ENTER').action = 'WRAP_50'

        layout.separator(factor=0.25)

        # Row 5: 2D Shape & Logo Stamp Dropdown + Inline Parametric Controls
        sh_col = layout.column(align=True)
        sh_row = sh_col.row(align=True)
        sh_row.menu("TEXPAINT_MT_2d_shapes", text="Stamp Shape", icon='MESH_CIRCLE')
        if wm and hasattr(wm, "tex_paint_2d_shape_radius"):
            sh_row.prop(wm, "tex_paint_2d_shape_radius", text="Size", slider=True)
            sh_row.prop(wm, "tex_paint_2d_shape_thickness", text="Outline", slider=True)
            sh_row.prop(wm, "tex_paint_2d_shape_sides", text="Sides")
            sh_row.prop(wm, "tex_paint_2d_shape_erase", text="", toggle=True, icon='IMAGE_ALPHA')

        layout.separator(factor=0.2)

        # Row 6: 2D Filters, Outline & Alpha FX Dropdown + Inline Radius/Strength Sliders
        fx_col = layout.column(align=True)
        fx_row = fx_col.row(align=True)
        fx_row.menu("TEXPAINT_MT_2d_fx", text="2D Filters & FX", icon='SHADERFX')
        if wm and hasattr(wm, "tex_paint_2d_fx_radius"):
            fx_row.prop(wm, "tex_paint_2d_fx_radius", text="Radius", slider=True)
            fx_row.prop(wm, "tex_paint_2d_fx_strength", text="Strength", slider=True)
        fx_row.operator("image.tex_paint_2d_fx", text="", icon='TRASH').effect = 'CLEAR_TRANSPARENT'


class IMAGE_PT_2d_canvas_studio_popover(Panel):
    """Popover for 2D Canvas Studio & 3D Bridge in the Image Editor header."""
    bl_space_type = 'IMAGE_EDITOR'
    bl_region_type = 'HEADER'
    bl_label = "2D Canvas Studio & 3D Bridge"
    bl_ui_units_x = 17

    @classmethod
    def poll(cls, context):
        return context.space_data is not None and context.space_data.type == 'IMAGE_EDITOR'

    def draw(self, context):
        IMAGE_PT_2d_canvas_studio.draw(self, context)


# ---------------------------------------------------------------------------
# Node Property Callbacks for Active Mask Sub-Layer Controls
# ---------------------------------------------------------------------------

_GRUNGE_PROJ_ITEMS = (
    ('REPEAT', "UV Projection", "2D UV mapping with full Scale, Offset, Rotation, and Repeat/Clamp tiling", 'UV', 0),
    ('TRIPLANAR', "Triplanar Projection", "Seamless 3D box projection across the entire mesh without UV seams", 'MESH_CUBE', 1),
    ('DECAL', "Planar / Decal Projection", "Directional 3D planar decal projection with 3D manipulator", 'MOD_UVPROJECT', 2),
    ('WARP', "Warp Projection", "Substance-Painter-style 3D surface-hugging warp projection that curves around geometry", 'MOD_WARP', 3),
    ('SPHERICAL', "Spherical Projection", "3D spherical wrap projection around the mesh or 3D manipulator", 'MESH_UVSPHERE', 4),
    ('CYLINDRICAL', "Cylindrical Projection", "3D cylindrical wrap projection around the mesh or 3D manipulator", 'MESH_CYLINDER', 5),
)

_FILL_SOURCE_ITEMS = (
    ('CUSTOM', "Image / Texture", "Fill the mask with any custom grayscale, RGB, or alpha image", 'FILE_IMAGE', 0),
    ('LIBRARY', "Grunge Library (275)", "Fill the mask from the built-in 275 Substance Grunge & Procedural library", 'TEXTURE', 1),
)

_FILL_WRAP_ITEMS = (
    ('REPEAT', "Repeat (Tile)", "Tile the image seamlessly across UV or 3D projection space", 'TEXTURE', 0),
    ('CLAMP', "Clamp (Stamp)", "Stamp a single instance of the image without repeating outside bounds", 'MOD_UVPROJECT', 1),
)

_FILL_CHANNEL_ITEMS = (
    ('AUTO', "Auto (Grayscale × Alpha)", "Automatically extract grayscale luminance weighted by PNG alpha (supports both grayscale maps and transparent decals)", 0),
    ('LUMINANCE', "Grayscale / Luminance", "Extract Rec.709 grayscale brightness ignoring alpha", 1),
    ('ALPHA', "Alpha Channel", "Extract the Alpha transparency channel directly as the mask", 2),
    ('RED', "Red Channel", "Extract the Red channel (for packed channel masks)", 3),
    ('GREEN', "Green Channel", "Extract the Green channel (for packed channel masks)", 4),
    ('BLUE', "Blue Channel", "Extract the Blue channel (for packed channel masks)", 5),
)

_GRUNGE_BLEND_ITEMS = (
    ('REPLACE', "Normal (Replace / Reveal)", "For Procedural/Grunge: replace mask; for Paint: reveal White & hide Black", 0),
    ('MULTIPLY', "Multiply (Break Up)", "Multiply over the mask stack below to break up edges and fills", 1),
    ('SUBTRACT', "Subtract (Chip Away)", "Subtract from the mask stack below to chip away or erase", 2),
    ('ADD', "Add (Linear Dodge)", "Add to the mask stack below to accumulate mask regions", 3),
    ('SCREEN', "Screen (Soft Add)", "Softly accumulate over the mask stack below", 4),
    ('OVERLAY', "Overlay (Contrast)", "Contrast-blend over the mask stack below", 5),
    ('MAX', "Max (Lighten)", "Keep the brighter value between this sub-layer and the stack below", 6),
    ('MIN', "Min (Darken)", "Keep the darker value between this sub-layer and the stack below", 7),
)


def _enum_idx(items, identifier, default=0):
    for i, item in enumerate(items):
        if item[0] == identifier:
            return i
    return default


def _active_eff_val(node, key, default):
    eff = _get_active_mask_effect(node)
    if eff is None:
        return default
    return eff.get(key, default)


def _grunge_texture_items_cb(self, _context):
    cat = _active_eff_val(self, "category", "BITMAP_GRUNGES")
    q = _active_eff_val(self, "search_filter", "")
    return _get_grunge_enum_items_for_category(cat, q)


def _grunge_texture_get(self):
    cat = _active_eff_val(self, "category", "BITMAP_GRUNGES")
    q = _active_eff_val(self, "search_filter", "")
    items = _get_grunge_enum_items_for_category(cat, q)
    return _enum_idx(items, _active_eff_val(self, "texture", ""), 0)


def _grunge_texture_set(self, value):
    cat = _active_eff_val(self, "category", "BITMAP_GRUNGES")
    q = _active_eff_val(self, "search_filter", "")
    items = _get_grunge_enum_items_for_category(cat, q)
    if 0 <= value < len(items) and items[value][0] != 'NONE':
        _update_active_mask_effect(self, None, {"texture": items[value][0]}, ensure_type='GRUNGE')
    return None


def _register_node_grunge_props():
    Node = bpy.types.Node

    def _update_reproject(self, context):
        _evaluate_layer_grunge_mask(self, None, context=context, force_reproject=True, interactive=True)
        return None

    def _update_lut(self, context):
        _evaluate_layer_grunge_mask(self, None, context=context, force_reproject=False, interactive=True)
        return None

    def _set_eff(self, updates, ensure_type=None):
        _update_active_mask_effect(self, None, updates, ensure_type=ensure_type, flush_json=True)
        return None

    def _on_cat_update(self, context):
        eff = _get_active_mask_effect(self)
        if eff is not None:
            cat = eff.get("category", "BITMAP_GRUNGES")
            q = eff.get("search_filter", "")
            if cat != 'CUSTOM':
                items = _get_grunge_enum_items_for_category(cat, q)
                valid_ids = {it[0] for it in items if it[0] != 'NONE'}
                if items and items[0][0] != 'NONE' and eff.get("texture", "") not in valid_ids:
                    _update_active_mask_effect(self, None, {"texture": items[0][0]}, ensure_type='GRUNGE')
        _evaluate_layer_grunge_mask(self, None, context=context, force_reproject=True, interactive=False)
        return None

    def _set_fill_source(self, val):
        eff = _get_active_mask_effect(self)
        cur_cat = eff.get("category", "BITMAP_GRUNGES") if eff else "BITMAP_GRUNGES"
        if val == 0:
            _set_eff(self, {"category": "CUSTOM"}, ensure_type='GRUNGE')
        else:
            new_cat = "BITMAP_GRUNGES" if cur_cat == "CUSTOM" else cur_cat
            _set_eff(self, {"category": new_cat}, ensure_type='GRUNGE')
        return None

    def _set_uniform_scale(self, val):
        fval = float(val)
        updates = {"scale": fval}
        if bool(_active_eff_val(self, "lock_scale", True)):
            updates["scale_x"] = fval
            updates["scale_y"] = fval
        _set_eff(self, updates, ensure_type='GRUNGE')
        return None

    Node.tex_paint_fill_source = EnumProperty(
        name="Grayscale Source",
        description="Choose between any Custom Image / Texture or the built-in 275 Substance Grunge Library",
        items=_FILL_SOURCE_ITEMS,
        get=lambda self: 0 if _active_eff_val(self, "category", "BITMAP_GRUNGES") == "CUSTOM" else 1,
        set=_set_fill_source,
        update=_on_cat_update,
    )

    Node.tex_paint_fill_wrap_mode = EnumProperty(
        name="UV Wrap / Tiling",
        description="Repeat (tile) the fill image across the surface or Clamp (stamp) a single instance",
        items=_FILL_WRAP_ITEMS,
        get=lambda self: _enum_idx(_FILL_WRAP_ITEMS, _active_eff_val(self, "uv_wrap", "REPEAT"), 0),
        set=lambda self, val: _set_eff(self, {"uv_wrap": _FILL_WRAP_ITEMS[val][0]}, ensure_type='GRUNGE'),
        update=_update_reproject,
    )

    Node.tex_paint_fill_channel = EnumProperty(
        name="Mask Channel",
        description="Which channel of the custom image to extract into the grayscale mask",
        items=_FILL_CHANNEL_ITEMS,
        get=lambda self: _enum_idx(_FILL_CHANNEL_ITEMS, _active_eff_val(self, "channel", "AUTO"), 0),
        set=lambda self, val: _set_eff(self, {"channel": _FILL_CHANNEL_ITEMS[val][0]}, ensure_type='GRUNGE'),
        update=_update_reproject,
    )

    Node.tex_paint_grunge_category = EnumProperty(
        name="Grunge Category",
        description="Substance Starter Assets grunge & procedural texture category",
        items=_GRUNGE_CATEGORIES,
        get=lambda self: _enum_idx(_GRUNGE_CATEGORIES, _active_eff_val(self, "category", "BITMAP_GRUNGES"), 1),
        set=lambda self, val: _set_eff(self, {"category": _GRUNGE_CATEGORIES[val][0]}, ensure_type='GRUNGE'),
        update=_on_cat_update,
    )

    Node.tex_paint_grunge_search = StringProperty(
        name="Search Grunge Maps",
        description="Filter the Substance Grunge & Procedural library by name (e.g. rust, dirt, scratch, leak, concrete, weave)",
        default="",
        get=lambda self: str(_active_eff_val(self, "search_filter", "")),
        set=lambda self, val: _set_eff(self, {"search_filter": str(val)}, ensure_type='GRUNGE'),
        update=_on_cat_update,
    )

    Node.tex_paint_grunge_texture = EnumProperty(
        name="Grunge Map",
        description="Select a Substance Grunge texture for the active Grunge Mask sub-layer",
        items=_grunge_texture_items_cb,
        get=_grunge_texture_get,
        set=_grunge_texture_set,
        update=_update_reproject,
    )

    Node.tex_paint_grunge_custom_image = StringProperty(
        name="Custom Mask Image",
        description="Select any image in the blend file (or open from disk) to fill into the mask",
        get=lambda self: str(_active_eff_val(self, "custom_image", "")),
        set=lambda self, val: _set_eff(self, {"custom_image": str(val), "category": "CUSTOM"}, ensure_type='GRUNGE'),
        update=_update_reproject,
    )

    def _set_grunge_proj_mode(self, val):
        mode_str = _GRUNGE_PROJ_ITEMS[val][0]
        _set_eff(self, {"proj_mode": mode_str}, ensure_type='GRUNGE')
        if self.active_layer is not None:
            self.active_layer.show_projection_widget = True

    Node.tex_paint_grunge_proj_mode = EnumProperty(
        name="Projection",
        description="Projection method used to map the grayscale image or grunge across the mesh (UV, Triplanar, Planar/Decal, Warp, Spherical, Cylindrical)",
        items=_GRUNGE_PROJ_ITEMS,
        get=lambda self: _enum_idx(_GRUNGE_PROJ_ITEMS, _active_eff_val(self, "proj_mode", "TRIPLANAR"), 1),
        set=_set_grunge_proj_mode,
        update=_update_reproject,
    )

    def _get_color_proj_mode(self):
        layer = self.active_layer
        if layer is None:
            return 0
        pm = getattr(layer, "proj_mode", "UV_REPEAT")
        if pm in {'FIT', 'UV_REPEAT'}:
            return 0
        return _enum_idx(_GRUNGE_PROJ_ITEMS, pm, 1)

    def _set_color_proj_mode(self, val):
        layer = self.active_layer
        if layer is None:
            return
        c_layer = _C_TexPaintLayer.from_address(layer.as_pointer())
        mode_str = _GRUNGE_PROJ_ITEMS[val][0]
        if mode_str == 'REPEAT':
            layer.proj_mode = 'FIT' if float(c_layer.proj_scale[2]) < 0.0 else 'UV_REPEAT'
        else:
            layer.proj_mode = mode_str
        layer.show_projection_widget = True
        _reproject_color_layer_multi_object(self, layer, context=bpy.context, force=True)

    Node.tex_paint_color_proj_mode = EnumProperty(
        name="Projection",
        description="Projection method for this layer (UV, Triplanar, Planar/Decal, Warp, Spherical, Cylindrical)",
        items=_GRUNGE_PROJ_ITEMS,
        get=_get_color_proj_mode,
        set=_set_color_proj_mode,
        update=lambda _self, context: _tag_view3d_gizmo_refresh(context),
    )

    def _get_color_wrap_mode(self):
        layer = self.active_layer
        if layer is None:
            return 0
        c_layer = _C_TexPaintLayer.from_address(layer.as_pointer())
        if getattr(layer, "proj_mode", "") == 'FIT' or float(c_layer.proj_scale[2]) < 0.0:
            return 1
        return 0

    def _set_color_wrap_mode(self, val):
        layer = self.active_layer
        if layer is None:
            return
        c_layer = _C_TexPaintLayer.from_address(layer.as_pointer())
        wrap_str = _FILL_WRAP_ITEMS[val][0]
        sz = abs(float(c_layer.proj_scale[2])) or 1.0
        if wrap_str == 'CLAMP':
            c_layer.proj_scale[2] = -sz
            if layer.proj_mode == 'UV_REPEAT':
                layer.proj_mode = 'FIT'
            else:
                layer.proj_blend_falloff = layer.proj_blend_falloff
        else:
            c_layer.proj_scale[2] = sz
            if layer.proj_mode == 'FIT':
                layer.proj_mode = 'UV_REPEAT'
            else:
                layer.proj_blend_falloff = layer.proj_blend_falloff
        _reproject_color_layer_multi_object(self, layer, context=bpy.context, force=True)

    Node.tex_paint_color_wrap_mode = EnumProperty(
        name="UV Wrap / Tiling Mode",
        description="Choose whether the projected texture tiles seamlessly across UVs/3D or stamps once with transparent borders",
        items=_FILL_WRAP_ITEMS,
        get=_get_color_wrap_mode,
        set=_set_color_wrap_mode,
        update=lambda _self, context: _tag_view3d_gizmo_refresh(context),
    )

    Node.tex_paint_grunge_blend_mode = EnumProperty(
        name="Combine Mode",
        description="How this mask sub-layer combines with the mask sub-layers below it",
        items=_GRUNGE_BLEND_ITEMS,
        get=lambda self: _enum_idx(_GRUNGE_BLEND_ITEMS, _active_eff_val(self, "blend_mode", "REPLACE"), 0),
        set=lambda self, val: _set_eff(self, {"blend_mode": _GRUNGE_BLEND_ITEMS[val][0]}),
        update=_update_lut,
    )

    Node.tex_paint_mask_eff_opacity = FloatProperty(
        name="Mask Sub-Layer Opacity",
        description="Opacity / strength of the active mask sub-layer in the Mask Stack",
        min=0.0,
        max=1.0,
        default=1.0,
        subtype='FACTOR',
        get=lambda self: float(_active_eff_val(self, "opacity", 1.0)),
        set=lambda self, val: _set_eff(self, {"opacity": float(val)}),
        update=_update_lut,
    )

    Node.tex_paint_grunge_lock_scale = BoolProperty(
        name="Lock Scale Aspect",
        description="Keep Scale X and Scale Y locked to uniform Scale (turn off for non-uniform X/Y stretching)",
        default=True,
        get=lambda self: bool(_active_eff_val(self, "lock_scale", True)),
        set=lambda self, val: _set_eff(self, {"lock_scale": bool(val)}, ensure_type='GRUNGE'),
        update=_update_reproject,
    )

    Node.tex_paint_grunge_scale = FloatProperty(
        name="Scale / Tiling",
        description="Uniform scale / tiling repetition across UV or 3D projection space",
        min=0.05,
        max=64.0,
        soft_min=0.25,
        soft_max=16.0,
        default=2.0,
        get=lambda self: float(_active_eff_val(self, "scale", 2.0)),
        set=_set_uniform_scale,
        update=_update_reproject,
    )

    Node.tex_paint_grunge_scale_x = FloatProperty(
        name="Scale X (U)",
        description="Horizontal (U) scale / tiling factor",
        min=0.05,
        max=64.0,
        soft_min=0.25,
        soft_max=16.0,
        default=2.0,
        get=lambda self: float(_active_eff_val(self, "scale_x", _active_eff_val(self, "scale", 2.0))),
        set=lambda self, val: _set_eff(self, {"scale_x": float(val), "scale": float(val)}, ensure_type='GRUNGE'),
        update=_update_reproject,
    )

    Node.tex_paint_grunge_scale_y = FloatProperty(
        name="Scale Y (V)",
        description="Vertical (V) scale / tiling factor",
        min=0.05,
        max=64.0,
        soft_min=0.25,
        soft_max=16.0,
        default=2.0,
        get=lambda self: float(_active_eff_val(self, "scale_y", _active_eff_val(self, "scale", 2.0))),
        set=lambda self, val: _set_eff(self, {"scale_y": float(val)}, ensure_type='GRUNGE'),
        update=_update_reproject,
    )

    Node.tex_paint_grunge_offset_x = FloatProperty(
        name="Offset X (U)",
        description="Horizontal (U) offset / translation of the projected fill image",
        min=-10.0,
        max=10.0,
        soft_min=-1.0,
        soft_max=1.0,
        default=0.0,
        step=1,
        precision=3,
        get=lambda self: float(_active_eff_val(self, "offset_x", 0.0)),
        set=lambda self, val: _set_eff(self, {"offset_x": float(val)}, ensure_type='GRUNGE'),
        update=_update_reproject,
    )

    Node.tex_paint_grunge_offset_y = FloatProperty(
        name="Offset Y (V)",
        description="Vertical (V) offset / translation of the projected fill image",
        min=-10.0,
        max=10.0,
        soft_min=-1.0,
        soft_max=1.0,
        default=0.0,
        step=1,
        precision=3,
        get=lambda self: float(_active_eff_val(self, "offset_y", 0.0)),
        set=lambda self, val: _set_eff(self, {"offset_y": float(val)}, ensure_type='GRUNGE'),
        update=_update_reproject,
    )

    Node.tex_paint_grunge_balance = FloatProperty(
        name="Balance",
        description="Substance-style Grunge balance (shifts mask coverage from sparse to full)",
        min=0.0,
        max=1.0,
        default=0.5,
        subtype='FACTOR',
        get=lambda self: float(_active_eff_val(self, "balance", 0.5)),
        set=lambda self, val: _set_eff(self, {"balance": float(val)}, ensure_type='GRUNGE'),
        update=_update_lut,
    )

    Node.tex_paint_grunge_contrast = FloatProperty(
        name="Contrast",
        description="Substance-style Grunge contrast (sharpens or softens the mask transitions)",
        min=0.0,
        max=1.0,
        default=0.30,
        subtype='FACTOR',
        get=lambda self: float(_active_eff_val(self, "contrast", 0.30)),
        set=lambda self, val: _set_eff(self, {"contrast": float(val)}, ensure_type='GRUNGE'),
        update=_update_lut,
    )

    Node.tex_paint_grunge_rotation = FloatProperty(
        name="Rotation",
        description="2D / 3D rotation angle of the projected fill image or grunge texture",
        min=-math.pi * 2.0,
        max=math.pi * 2.0,
        soft_min=-math.pi,
        soft_max=math.pi,
        default=0.0,
        subtype='ANGLE',
        get=lambda self: float(_active_eff_val(self, "rotation", 0.0)),
        set=lambda self, val: _set_eff(self, {"rotation": float(val)}, ensure_type='GRUNGE'),
        update=_update_reproject,
    )

    Node.tex_paint_grunge_box_blend = FloatProperty(
        name="Box Blend / Falloff",
        description="Triplanar box blend softness, Planar/Decal normal falloff, or Warp curvature depth",
        min=0.0,
        max=1.0,
        default=0.5,
        subtype='FACTOR',
        get=lambda self: float(_active_eff_val(self, "box_blend", 0.5)),
        set=lambda self, val: _set_eff(self, {"box_blend": float(val)}, ensure_type='GRUNGE'),
        update=_update_reproject,
    )

    Node.tex_paint_grunge_invert = BoolProperty(
        name="Invert Sub-Layer",
        description="Invert this mask sub-layer's values before combining with the stack below",
        default=False,
        get=lambda self: bool(_active_eff_val(self, "invert", False)),
        set=lambda self, val: _set_eff(self, {"invert": bool(val)}),
        update=_update_lut,
    )

    # Smart Mask Generator Sub-Layer Type & Sliders (Balance & Contrast run via 1.5ms LUT; Radius & Noise run via C++ generator)
    _gen_type_items = (
        ('EDGE_WEAR', "Generator: Edge Wear", "Highlight convex sharp edges attenuated by ambient occlusion", 'MOD_BEVEL', 0),
        ('CAVITY', "Generator: Cavity & Dirt", "Highlight concave crevices, seams, and recessed dirt areas", 'SHARPCURVE', 1),
        ('CURVATURE', "Generator: Curvature", "Full curvature map (concave to convex)", 'SMOOTHCURVE', 2),
        ('AO', "Generator: Ambient Occlusion", "Ambient occlusion shading across the mesh", 'SHADING_RENDERED', 3),
        ('NORMAL_UP', "Generator: Top-Down Dust", "Top-facing world/object normal for dust, snow, or moss", 'NORMALS_FACE', 4),
        ('POSITION_Z', "Generator: Vertical Height", "Bottom-to-top vertical height gradient", 'EMPTY_SINGLE_ARROW', 5),
    )

    Node.tex_paint_mask_eff_gen_type = EnumProperty(
        name="Generator Type",
        description="Procedural 3D mesh generator algorithm for the active Generator sub-layer",
        items=_gen_type_items,
        get=lambda self: _enum_idx(_gen_type_items, _active_eff_val(self, "gen_type", "EDGE_WEAR"), 0),
        set=lambda self, val: _set_eff(self, {"gen_type": _gen_type_items[val][0]}, ensure_type='GENERATOR'),
        update=_update_reproject,
    )

    Node.tex_paint_mask_eff_gen_balance = FloatProperty(
        name="Generator Balance",
        description="Threshold / balance of the active Smart Mask Generator sub-layer",
        min=0.0,
        max=1.0,
        default=0.5,
        subtype='FACTOR',
        get=lambda self: float(_active_eff_val(self, "gen_balance", 0.5)),
        set=lambda self, val: _set_eff(self, {"gen_balance": float(val)}, ensure_type='GENERATOR'),
        update=_update_lut,
    )

    Node.tex_paint_mask_eff_gen_contrast = FloatProperty(
        name="Generator Contrast",
        description="Contrast / sharpness of the active Smart Mask Generator sub-layer",
        min=0.0,
        max=1.0,
        default=0.45,
        subtype='FACTOR',
        get=lambda self: float(_active_eff_val(self, "gen_contrast", 0.45)),
        set=lambda self, val: _set_eff(self, {"gen_contrast": float(val)}, ensure_type='GENERATOR'),
        update=_update_lut,
    )

    Node.tex_paint_mask_eff_gen_radius = FloatProperty(
        name="Generator Radius",
        description="Neighborhood spread / bevel radius of the active Smart Mask Generator sub-layer",
        min=0.0,
        max=1.0,
        default=0.35,
        subtype='FACTOR',
        get=lambda self: float(_active_eff_val(self, "gen_radius", 0.35)),
        set=lambda self, val: _set_eff(self, {"gen_radius": float(val)}, ensure_type='GENERATOR'),
        update=_update_reproject,
    )

    Node.tex_paint_mask_eff_gen_noise = FloatProperty(
        name="3D Noise Breakup",
        description="Built-in 3D procedural noise breakup amount on the Smart Mask Generator",
        min=0.0,
        max=1.0,
        default=0.0,
        subtype='FACTOR',
        get=lambda self: float(_active_eff_val(self, "gen_noise", 0.0)),
        set=lambda self, val: _set_eff(self, {"gen_noise": float(val)}, ensure_type='GENERATOR'),
        update=_update_reproject,
    )

    Node.tex_paint_mask_eff_gen_noise_scale = FloatProperty(
        name="Noise Scale",
        description="Scale of the built-in 3D procedural noise on the Smart Mask Generator",
        min=0.2,
        max=64.0,
        soft_min=1.0,
        soft_max=32.0,
        default=8.0,
        get=lambda self: float(_active_eff_val(self, "gen_noise_scale", 8.0)),
        set=lambda self, val: _set_eff(self, {"gen_noise_scale": float(val)}, ensure_type='GENERATOR'),
        update=_update_reproject,
    )

    Node.tex_paint_mask_eff_gen_global_ao = BoolProperty(
        name="Global Scene AO (Surrounding Objects)",
        description="Include Ambient Occlusion and contact shadows from all surrounding objects in the scene (and across shared modular parts)",
        default=True,
        get=lambda self: bool(_active_eff_val(self, "gen_global_ao", True)),
        set=lambda self, val: _set_eff(self, {"gen_global_ao": bool(val)}, ensure_type='GENERATOR'),
        update=_update_reproject,
    )

    # Substance-Painter-style Mesh Maps & High-Poly Baker Hub properties
    _bake_src_items = (
        ('LOW_POLY', "Low-Poly + Scene", "Compute Mesh Maps from the Low-Poly mesh(es) and surrounding scene objects", 'MESH_DATA', 0),
        ('HIGH_POLY_OB', "High-Poly Object -> Low-Poly", "Raycast normal, curvature, AO, and ID details from a High-Poly mesh object onto the Low-Poly mesh(es)", 'MESH_ICOSPHERE', 1),
        ('HIGH_POLY_COL', "High-Poly Collection -> Low-Poly", "Raycast details from all meshes in a High-Poly Collection onto the Low-Poly mesh(es)", 'OUTLINER_COLLECTION', 2),
    )
    _bake_ao_scope_items = (
        ('GLOBAL', "Global Scene (Nearby Objects)", "Ambient Occlusion interacts with all visible objects around each part in the scene", 'WORLD', 0),
        ('MATERIAL', "Shared Material Parts", "Ambient Occlusion interacts only between modular parts sharing this material", 'LINKED', 1),
        ('SELF', "Self Only (Isolated)", "Ambient Occlusion is computed per isolated object ignoring surrounding objects", 'OBJECT_DATA', 2),
    )
    _bake_res_items = (
        ('MATCH', "Match Canvas", "Match the resolution of the material's layered texture canvas"),
        ('512', "512 × 512", "512 × 512"),
        ('1024', "1024 × 1024 (1K)", "1024 × 1024"),
        ('2048', "2048 × 2048 (2K)", "2048 × 2048"),
        ('4096', "4096 × 4096 (4K)", "4096 × 4096"),
    )

    def _on_mesh_map_changed(self, context):
        _EFFECT_RAW_CACHE.clear()
        owner = _canonical_mask_owner_node(self)
        for l_item in owner.layers:
            if getattr(l_item, "use_mask", False):
                _evaluate_layer_grunge_mask(owner, l_item, context=context, force_reproject=True, skip_absorb=True)
        return None

    Node.tex_paint_use_baked_mesh_maps = BoolProperty(
        name="Use Baked Mesh Maps in Smart Masks",
        description="Automatically drive AO, Curvature, Edge Wear, Cavity, Dust, and Height generators from the baked Mesh Maps in this Hub",
        default=True,
        get=lambda self: bool(_get_mesh_maps_state(self).get("use_baked_maps", True)),
        set=lambda self, val: _save_mesh_maps_state(self, updates={"use_baked_maps": bool(val)}),
        update=_on_mesh_map_changed,
    )

    Node.tex_paint_bake_source_mode = EnumProperty(
        name="Baker Mesh Source",
        description="Choose whether to bake Mesh Maps from Low-Poly + Scene or transfer details from a High-Poly Object / Collection",
        items=_bake_src_items,
        get=lambda self: _enum_idx(_bake_src_items, _get_mesh_maps_state(self).get("source_mode", "LOW_POLY"), 0),
        set=lambda self, val: _save_mesh_maps_state(self, updates={"source_mode": _bake_src_items[val][0]}),
    )

    Node.tex_paint_bake_high_poly_ob = StringProperty(
        name="High-Poly Source Object",
        description="High-Poly mesh object to bake Normal, Curvature, AO, and Color ID maps from",
        get=lambda self: str(_get_mesh_maps_state(self).get("high_poly_ob", "")),
        set=lambda self, val: _save_mesh_maps_state(self, updates={"high_poly_ob": str(val)}),
    )

    Node.tex_paint_bake_high_poly_col = StringProperty(
        name="High-Poly Source Collection",
        description="Collection of High-Poly meshes to bake Normal, Curvature, AO, and Color ID maps from",
        get=lambda self: str(_get_mesh_maps_state(self).get("high_poly_col", "")),
        set=lambda self, val: _save_mesh_maps_state(self, updates={"high_poly_col": str(val)}),
    )

    Node.tex_paint_bake_ao_scope = EnumProperty(
        name="AO Interaction Scope",
        description="Whether Ambient Occlusion includes all surrounding objects in the scene (Global), only shared material parts, or only self",
        items=_bake_ao_scope_items,
        get=lambda self: _enum_idx(_bake_ao_scope_items, _get_mesh_maps_state(self).get("ao_scope", "GLOBAL"), 0),
        set=lambda self, val: _save_mesh_maps_state(self, updates={"ao_scope": _bake_ao_scope_items[val][0]}),
    )

    Node.tex_paint_bake_cage_dist = FloatProperty(
        name="Max Ray / Cage Distance",
        description="Ray extrusion distance when projecting High-Poly surface details onto the Low-Poly mesh",
        min=0.0001,
        max=10.0,
        soft_min=0.005,
        soft_max=1.0,
        default=0.08,
        get=lambda self: float(_get_mesh_maps_state(self).get("cage_dist", 0.08)),
        set=lambda self, val: _save_mesh_maps_state(self, updates={"cage_dist": float(val)}),
    )

    Node.tex_paint_bake_ao_dist = FloatProperty(
        name="AO Occlusion Distance",
        description="Relative distance spread for Ambient Occlusion raycasting across self and surrounding objects",
        min=0.05,
        max=2.0,
        default=0.65,
        subtype='FACTOR',
        get=lambda self: float(_get_mesh_maps_state(self).get("ao_dist", 0.65)),
        set=lambda self, val: _save_mesh_maps_state(self, updates={"ao_dist": float(val)}),
    )

    Node.tex_paint_bake_res = EnumProperty(
        name="Bake Resolution",
        description="Resolution for baked Mesh Maps",
        items=_bake_res_items,
        get=lambda self: _enum_idx(_bake_res_items, _get_mesh_maps_state(self).get("bake_res", "MATCH"), 0),
        set=lambda self, val: _save_mesh_maps_state(self, updates={"bake_res": _bake_res_items[val][0]}),
    )

    Node.tex_paint_bake_apply_normal = BoolProperty(
        name="Auto-Connect Baked Normal to Shader",
        description="Automatically connect the baked Tangent-Space Normal Map to the material's Principled BSDF Normal input",
        default=True,
        get=lambda self: bool(_get_mesh_maps_state(self).get("apply_normal_to_shader", True)),
        set=lambda self, val: _save_mesh_maps_state(self, updates={"apply_normal_to_shader": bool(val)}),
    )

    def _make_mesh_map_slot_prop(slot_k, slot_lbl):
        return StringProperty(
            name=f"{slot_lbl} Map",
            description=f"Assigned or baked {slot_lbl} image for this material",
            get=lambda self: str(_get_mesh_maps_state(self).get("maps", {}).get(slot_k, "")),
            set=lambda self, val: _save_mesh_maps_state(self, map_updates={slot_k: str(val)}),
            update=_on_mesh_map_changed,
        )

    for _sk, _slbl, _sic in _MESH_MAP_SLOTS:
        setattr(Node, f"tex_paint_mesh_map_{_sk}", _make_mesh_map_slot_prop(_sk, _slbl))

    # Levels Sub-Layer Sliders
    Node.tex_paint_mask_eff_lvl_balance = FloatProperty(
        name="Levels Balance",
        description="Shift midpoint brightness of the mask stack below",
        min=0.0,
        max=1.0,
        default=0.5,
        subtype='FACTOR',
        get=lambda self: float(_active_eff_val(self, "lvl_balance", 0.5)),
        set=lambda self, val: _set_eff(self, {"lvl_balance": float(val)}, ensure_type='LEVELS'),
        update=_update_lut,
    )

    Node.tex_paint_mask_eff_lvl_contrast = FloatProperty(
        name="Levels Contrast",
        description="Increase or tighten contrast of the mask stack below",
        min=0.0,
        max=1.0,
        default=0.0,
        subtype='FACTOR',
        get=lambda self: float(_active_eff_val(self, "lvl_contrast", 0.0)),
        set=lambda self, val: _set_eff(self, {"lvl_contrast": float(val)}, ensure_type='LEVELS'),
        update=_update_lut,
    )

    # Filter Sub-Layer Type & Sliders (Blur, Blur Slope, Sharpen, Grow/Shrink, Bevel, Threshold, Posterize, Highpass)
    _filter_type_items = (
        ('BLUR', "Filter: Blur", "Gaussian / box softening filter on the mask stack below", 'MOD_SMOOTH', 0),
        ('BLUR_SLOPE', "Filter: Blur Slope", "Substance-style directional slope warp along mask edges", 'MOD_NOISE', 1),
        ('SHARPEN', "Filter: Sharpen", "Unsharp mask edge crispness enhancement on the mask stack below", 'SHARPCURVE', 2),
        ('DILATE_ERODE', "Filter: Grow / Shrink", "Dilate (expand, positive) or Erode (shrink, negative) the mask boundary", 'FULLSCREEN_ENTER', 3),
        ('BEVEL', "Filter: Bevel", "Smooth distance bevel ramp around mask silhouettes", 'MOD_BEVEL', 4),
        ('THRESHOLD', "Filter: Threshold", "Hard binary stencil cutoff", 'IPO_CONSTANT', 5),
        ('POSTERIZE', "Filter: Posterize", "Quantize mask gradients into stepped contour bands", 'LINENUMBERS_ON', 6),
        ('HIGHPASS', "Filter: Highpass", "Isolate high-frequency transitions and edge details", 'SEQ_HISTOGRAM', 7),
    )

    Node.tex_paint_mask_eff_filter_type = EnumProperty(
        name="Filter Type",
        description="2D image-processing filter algorithm applied to the mask stack below",
        items=_filter_type_items,
        get=lambda self: _enum_idx(_filter_type_items, _active_eff_val(self, "filter_type", "BLUR"), 0),
        set=lambda self, val: _set_eff(self, {"filter_type": _filter_type_items[val][0]}, ensure_type='FILTER'),
        update=_update_lut,
    )

    Node.tex_paint_mask_eff_flt_radius = FloatProperty(
        name="Filter Radius",
        description="Pixel radius of the Blur, Blur Slope, Sharpen, Grow/Shrink, Bevel, or Highpass kernel",
        min=0.5,
        max=64.0,
        soft_min=1.0,
        soft_max=32.0,
        default=6.0,
        get=lambda self: float(_active_eff_val(self, "flt_radius", 6.0)),
        set=lambda self, val: _set_eff(self, {"flt_radius": float(val)}, ensure_type='FILTER'),
        update=_update_lut,
    )

    Node.tex_paint_mask_eff_flt_strength = FloatProperty(
        name="Filter Strength",
        description="Intensity of the filter (or positive to Grow / negative to Shrink when using Dilate / Erode)",
        min=-2.0,
        max=2.0,
        soft_min=-1.0,
        soft_max=1.5,
        default=1.0,
        get=lambda self: float(_active_eff_val(self, "flt_strength", 1.0)),
        set=lambda self, val: _set_eff(self, {"flt_strength": float(val)}, ensure_type='FILTER'),
        update=_update_lut,
    )

    Node.tex_paint_mask_eff_flt_scale = FloatProperty(
        name="Slope Noise Scale",
        description="Frequency / scale of the procedural slope field for Blur Slope edge warping",
        min=1.0,
        max=64.0,
        soft_min=2.0,
        soft_max=32.0,
        default=8.0,
        get=lambda self: float(_active_eff_val(self, "flt_scale", 8.0)),
        set=lambda self, val: _set_eff(self, {"flt_scale": float(val)}, ensure_type='FILTER'),
        update=_update_lut,
    )

    # -------------------------------------------------------------------
    # Substance-Painter-Style Multi-Channel Fill Layer Properties (forwarding to native TexPaintLayer RNA)
    # -------------------------------------------------------------------
    def _get_fill_ch_rgb(node, ch_id, default_rgb):
        layer = getattr(node, "active_layer", None)
        if layer is None:
            return default_rgb
        if ch_id == 'BASE_COLOR':
            fc = layer.fill_color
            return (float(fc[0]), float(fc[1]), float(fc[2]))
        if ch_id == 'EMISSION' and hasattr(layer, "fill_emission"):
            fe = layer.fill_emission
            return (float(fe[0]), float(fe[1]), float(fe[2]))
        return default_rgb

    def _set_fill_ch_rgb(node, ch_id, rgb_val):
        layer = getattr(node, "active_layer", None)
        if layer is None:
            return
        if ch_id == 'BASE_COLOR':
            layer.fill_color = (float(rgb_val[0]), float(rgb_val[1]), float(rgb_val[2]), float(layer.fill_color[3]))
        elif ch_id == 'EMISSION' and hasattr(layer, "fill_emission"):
            layer.fill_emission = (float(rgb_val[0]), float(rgb_val[1]), float(rgb_val[2]))

    def _get_fill_ch_scalar(node, ch_id, default_val):
        layer = getattr(node, "active_layer", None)
        if layer is None:
            return default_val
        if ch_id == 'ROUGHNESS' and hasattr(layer, "fill_roughness"):
            return float(layer.fill_roughness)
        if ch_id == 'METALLIC' and hasattr(layer, "fill_metallic"):
            return float(layer.fill_metallic)
        if ch_id == 'SPECULAR' and hasattr(layer, "fill_specular"):
            return float(layer.fill_specular)
        return default_val

    def _set_fill_ch_scalar(node, ch_id, val):
        layer = getattr(node, "active_layer", None)
        if layer is None:
            return
        v = min(max(float(val), 0.0), 1.0)
        if ch_id == 'ROUGHNESS' and hasattr(layer, "fill_roughness"):
            layer.fill_roughness = v
        elif ch_id == 'METALLIC' and hasattr(layer, "fill_metallic"):
            layer.fill_metallic = v
        elif ch_id == 'SPECULAR' and hasattr(layer, "fill_specular"):
            layer.fill_specular = v

    def _get_fill_ch_signed(node, ch_id):
        layer = getattr(node, "active_layer", None)
        if layer is not None and hasattr(layer, "fill_height"):
            return float(layer.fill_height)
        return 0.0

    def _set_fill_ch_signed(node, ch_id, val):
        layer = getattr(node, "active_layer", None)
        if layer is not None and hasattr(layer, "fill_height"):
            layer.fill_height = min(max(float(val), -1.0), 1.0)

    Node.tex_paint_fill_base_color = FloatVectorProperty(
        name="Base Color",
        description="Base Color (Albedo) of this Fill Layer",
        subtype='COLOR',
        size=3,
        min=0.0,
        max=1.0,
        default=(0.8, 0.8, 0.8),
        get=lambda self: _get_fill_ch_rgb(self, 'BASE_COLOR', (0.8, 0.8, 0.8)),
        set=lambda self, val: _set_fill_ch_rgb(self, 'BASE_COLOR', val),
    )

    Node.tex_paint_fill_roughness = FloatProperty(
        name="Roughness",
        description="Roughness value of this Fill Layer (0.0 = Mirror Glossy, 1.0 = Matte Rough)",
        min=0.0,
        max=1.0,
        default=0.35,
        subtype='FACTOR',
        get=lambda self: _get_fill_ch_scalar(self, 'ROUGHNESS', 0.35),
        set=lambda self, val: _set_fill_ch_scalar(self, 'ROUGHNESS', val),
    )

    Node.tex_paint_fill_metallic = FloatProperty(
        name="Metallic",
        description="Metallic value of this Fill Layer (0.0 = Dielectric, 1.0 = Pure Metal)",
        min=0.0,
        max=1.0,
        default=1.0,
        subtype='FACTOR',
        get=lambda self: _get_fill_ch_scalar(self, 'METALLIC', 1.0),
        set=lambda self, val: _set_fill_ch_scalar(self, 'METALLIC', val),
    )

    Node.tex_paint_fill_height = FloatProperty(
        name="Height",
        description="Height / Bump offset of this Fill Layer (-1.0 = Carved Groove, 0.0 = Flat Neutral, +1.0 = Raised Emboss)",
        min=-1.0,
        max=1.0,
        default=0.0,
        subtype='FACTOR',
        get=lambda self: _get_fill_ch_signed(self, 'BUMP'),
        set=lambda self, val: _set_fill_ch_signed(self, 'BUMP', val),
    )

    Node.tex_paint_fill_emission = FloatVectorProperty(
        name="Emission",
        description="Emission glow color of this Fill Layer",
        subtype='COLOR',
        size=3,
        min=0.0,
        max=1.0,
        default=(1.0, 0.45, 0.1),
        get=lambda self: _get_fill_ch_rgb(self, 'EMISSION', (1.0, 0.45, 0.1)),
        set=lambda self, val: _set_fill_ch_rgb(self, 'EMISSION', val),
    )

    Node.tex_paint_fill_normal = FloatVectorProperty(
        name="Normal",
        description="Tangent-space normal color of this Fill Layer",
        subtype='COLOR',
        size=3,
        min=0.0,
        max=1.0,
        default=(0.5, 0.5, 1.0),
        get=lambda self: _get_fill_ch_rgb(self, 'NORMAL', (0.5, 0.5, 1.0)),
        set=lambda self, val: _set_fill_ch_rgb(self, 'NORMAL', val),
    )

    Node.tex_paint_fill_specular = FloatProperty(
        name="Specular",
        description="Specular IOR Level of this Fill Layer",
        min=0.0,
        max=1.0,
        default=0.5,
        subtype='FACTOR',
        get=lambda self: _get_fill_ch_scalar(self, 'SPECULAR', 0.5),
        set=lambda self, val: _set_fill_ch_scalar(self, 'SPECULAR', val),
    )

    Node.tex_paint_fill_alpha = FloatProperty(
        name="Alpha",
        description="Opacity / Alpha cutout value of this Fill Layer",
        min=0.0,
        max=1.0,
        default=1.0,
        subtype='FACTOR',
        get=lambda self: _get_fill_ch_scalar(self, 'ALPHA', 1.0),
        set=lambda self, val: _set_fill_ch_scalar(self, 'ALPHA', val),
    )

    Node.tex_paint_fill_displacement = FloatProperty(
        name="Displacement",
        description="Displacement height offset of this Fill Layer (-1.0 to +1.0)",
        min=-1.0,
        max=1.0,
        default=0.0,
        subtype='FACTOR',
        get=lambda self: _get_fill_ch_signed(self, 'DISPLACEMENT'),
        set=lambda self, val: _set_fill_ch_signed(self, 'DISPLACEMENT', val),
    )


def _unregister_node_grunge_props():
    Node = bpy.types.Node
    for prop_name in (
        "tex_paint_fill_source",
        "tex_paint_fill_wrap_mode",
        "tex_paint_fill_channel",
        "tex_paint_grunge_category",
        "tex_paint_grunge_search",
        "tex_paint_grunge_texture",
        "tex_paint_grunge_custom_image",
        "tex_paint_grunge_proj_mode",
        "tex_paint_color_proj_mode",
        "tex_paint_color_wrap_mode",
        "tex_paint_grunge_blend_mode",
        "tex_paint_mask_eff_opacity",
        "tex_paint_grunge_lock_scale",
        "tex_paint_grunge_scale",
        "tex_paint_grunge_scale_x",
        "tex_paint_grunge_scale_y",
        "tex_paint_grunge_offset_x",
        "tex_paint_grunge_offset_y",
        "tex_paint_grunge_balance",
        "tex_paint_grunge_contrast",
        "tex_paint_grunge_rotation",
        "tex_paint_grunge_box_blend",
        "tex_paint_grunge_invert",
        "tex_paint_mask_eff_gen_type",
        "tex_paint_mask_eff_gen_balance",
        "tex_paint_mask_eff_gen_contrast",
        "tex_paint_mask_eff_gen_radius",
        "tex_paint_mask_eff_gen_noise",
        "tex_paint_mask_eff_gen_noise_scale",
        "tex_paint_mask_eff_gen_global_ao",
        "tex_paint_use_baked_mesh_maps",
        "tex_paint_bake_source_mode",
        "tex_paint_bake_high_poly_ob",
        "tex_paint_bake_high_poly_col",
        "tex_paint_bake_ao_scope",
        "tex_paint_bake_cage_dist",
        "tex_paint_bake_ao_dist",
        "tex_paint_bake_res",
        "tex_paint_bake_apply_normal",
        "tex_paint_mesh_map_normal",
        "tex_paint_mesh_map_normal_ws",
        "tex_paint_mesh_map_id_map",
        "tex_paint_mesh_map_ao",
        "tex_paint_mesh_map_curvature",
        "tex_paint_mesh_map_position",
        "tex_paint_mesh_map_thickness",
        "tex_paint_mask_eff_lvl_balance",
        "tex_paint_mask_eff_lvl_contrast",
        "tex_paint_mask_eff_filter_type",
        "tex_paint_mask_eff_flt_radius",
        "tex_paint_mask_eff_flt_strength",
        "tex_paint_mask_eff_flt_scale",
        "tex_paint_fill_base_color",
        "tex_paint_fill_roughness",
        "tex_paint_fill_metallic",
        "tex_paint_fill_height",
        "tex_paint_fill_emission",
        "tex_paint_fill_normal",
        "tex_paint_fill_specular",
        "tex_paint_fill_alpha",
        "tex_paint_fill_displacement",
    ):
        if hasattr(Node, prop_name):
            delattr(Node, prop_name)


_LAST_SEEN_CONTEXT_MODE = None


def _mask_paint_sync_timer():
    """Lightweight timer to flush debounced JSON state, refine interactive 2K projections on idle, and absorb brush strokes."""
    global _LAST_SEEN_CONTEXT_MODE
    if bpy.app.background or _INTERNAL_REPROJECTING:
        return 0.25
    try:
        import time
        import numpy as np

        if not _PROJECTION_KEYMAPS:
            _register_projection_keymaps()

        context = bpy.context
        cur_mode = getattr(context, "mode", None)
        if _LAST_SEEN_CONTEXT_MODE == 'PAINT_TEXTURE' and cur_mode != 'PAINT_TEXTURE':
            # Automatically turn off any visible 3D projection gizmo when exiting Texture Paint mode!
            node_3d = _find_3d_object_tex_paint_node(context)
            if node_3d is not None:
                for l_item in node_3d.layers:
                    if getattr(l_item, "show_projection_widget", False):
                        l_item.show_projection_widget = False
                _tag_view3d_gizmo_refresh(context)
        _LAST_SEEN_CONTEXT_MODE = cur_mode

        if _DIRTY_JSON_NODES:
            _flush_dirty_node_json()

        if _PENDING_HIRES_REFINE:
            now = time.perf_counter()
            for rkey, (n_ref, l_ref, ts) in list(_PENDING_HIRES_REFINE.items()):
                if now - ts >= 0.18:
                    _PENDING_HIRES_REFINE.pop(rkey, None)
                    try:
                        _evaluate_layer_grunge_mask(n_ref, l_ref, force_reproject=True, interactive=False, skip_absorb=True)
                    except Exception:
                        pass

        node = tex_paint_node_from_context(context)
        layer = node.active_layer if node else None
        if node is not None and layer is not None:
            _reproject_color_layer_multi_object(node, layer, context=context, force=False)
        if node is not None:
            n_ptr = node.as_pointer()
            cur_target = getattr(node, "paint_target", 'COLOR')
            prev_target = _LAST_SEEN_PAINT_TARGET.get(n_ptr, cur_target)
            if cur_target != prev_target:
                _LAST_SEEN_PAINT_TARGET[n_ptr] = cur_target
                if cur_target == 'COLOR':
                    _restore_rgb_brush_color_after_mask(context)
                elif cur_target == 'MASK':
                    _save_rgb_brush_color_before_mask(context)

        if node is not None and layer is not None and getattr(layer, "use_mask", False):
            key = (node.as_pointer(), _layer_uid(layer))
            if key in _LAST_COMPOSITED_MASK:
                c_layer = _C_TexPaintLayer.from_address(layer.as_pointer())
                if c_layer.mask_ibuf and c_layer.runtime_dirty:
                    c_imb = _C_ImBuf.from_address(c_layer.mask_ibuf)
                    w, h = int(c_imb.x), int(c_imb.y)
                    if w > 0 and h > 0 and c_imb.byte_data:
                        buf = (ctypes.c_uint8 * (w * h * 4)).from_address(c_imb.byte_data)
                        cur_u8 = np.frombuffer(buf, dtype=np.uint8).reshape(-1, 4)[:, 0]
                        last_u8 = _LAST_COMPOSITED_MASK.get(key)
                        if last_u8 is None or len(last_u8) != w * h:
                            # Lazily snapshot deferred composited mask once when idle
                            _LAST_COMPOSITED_MASK[key] = cur_u8.copy()
                            step = max(1, (w * h) // 1024)
                            _LAST_MASK_SAMPLE[key] = cur_u8[::step].copy()
                        elif not np.array_equal(cur_u8, last_u8):
                            prev_snap = _PENDING_STROKE_SNAPSHOT.get(key)
                            cur_hash = hash(cur_u8[::64].tobytes())
                            if prev_snap != cur_hash:
                                # Stroke is still in progress; wait one tick for mouse release
                                _PENDING_STROKE_SNAPSHOT[key] = cur_hash
                            else:
                                _PENDING_STROKE_SNAPSHOT.pop(key, None)
                                _absorb_pending_brush_strokes(node, layer)
    except Exception:
        pass
    return 0.16


classes = (
    NODE_OT_tex_paint_gizmo_mode,
    IMAGE_OT_tex_paint_uv_gizmo_transform,
    IMAGE_OT_tex_paint_focus_uv_gizmo,
    NODE_OT_tex_paint_layer_align_view,
    NODE_OT_tex_paint_layer_snap_decal,
    NODE_OT_tex_paint_layer_bake_projection,
    NODE_OT_tex_paint_mask_effect_add,
    NODE_OT_tex_paint_mask_effect_remove,
    NODE_OT_tex_paint_mask_effect_move,
    NODE_OT_tex_paint_mask_effect_duplicate,
    NODE_OT_tex_paint_mask_effect_select,
    NODE_OT_tex_paint_mask_effect_toggle,
    NODE_OT_tex_paint_mask_paint_action,
    NODE_OT_tex_paint_mask_brush_color,
    NODE_OT_tex_paint_grunge_apply,
    NODE_OT_tex_paint_grunge_step,
    NODE_OT_tex_paint_grunge_randomize,
    NODE_OT_tex_paint_grunge_open_image,
    NODE_OT_tex_paint_fill_transform_action,
    NODE_OT_tex_paint_mask_gen_preset,
    NODE_OT_tex_paint_mask_stack_init,
    NODE_OT_tex_paint_layer_target_select,
    NODE_OT_tex_paint_fill_layer_texture,
    NODE_OT_tex_paint_layer_solo,
    NODE_OT_tex_paint_viewport_shading,
    NODE_OT_tex_paint_convert_slot_to_layers,
    NODE_OT_tex_paint_pbr_channel,
    TEXPAINT_MT_add_pbr_channel,
    NODE_OT_tex_paint_fill_channel_toggle,
    TEXPAINT_MT_fill_channels_and_presets,
    NODE_OT_tex_paint_layer_pixel_action,
    NODE_OT_tex_paint_export_texture_set,
    NODE_OT_tex_paint_reconnect_live_layers,
    NODE_OT_tex_paint_make_material_unique,
    NODE_OT_tex_paint_bake_mesh_maps,
    NODE_OT_tex_paint_mesh_map_action,
    NODE_OT_tex_paint_import_mesh_map,
    TEXPAINT_MT_layer_specials,
    NODE_OT_tex_paint_smart_mask_recipe,
    TEXPAINT_MT_smart_mask_recipes,
    TEXPAINT_MT_add_mask_generator,
    TEXPAINT_MT_add_mask_filter,
    TEXPAINT_MT_add_layer,
    TEXPAINT_MT_add_mask_effect,
    NODE_OT_tex_paint_quick_uv,
    IMAGE_OT_tex_paint_new_2d_canvas,
    IMAGE_OT_tex_paint_2d_layer_mode,
    IMAGE_OT_tex_paint_2d_edit,
    IMAGE_OT_tex_paint_2d_shape,
    IMAGE_OT_tex_paint_2d_fx,
    IMAGE_OT_tex_paint_2d_magic_wand,
    IMAGE_OT_tex_paint_2d_marquee_select,
    IMAGE_OT_tex_paint_2d_select_action,
    IMAGE_OT_tex_paint_2d_bucket_fill,
    IMAGE_OT_tex_paint_2d_gradient,
    IMAGE_OT_tex_paint_2d_free_transform,
    IMAGE_OT_tex_paint_2d_color_adjust,
    IMAGE_OT_tex_paint_send_to_3d,
    TEXPAINT_MT_2d_canvas_new,
    TEXPAINT_MT_2d_send_to_3d,
    TEXPAINT_MT_2d_transform,
    TEXPAINT_MT_2d_shapes,
    TEXPAINT_MT_2d_fx,
    TEXPAINT_MT_2d_select_menu,
    TEXPAINT_UL_layers,
    VIEW3D_PT_tex_paint_layers,
    VIEW3D_PT_tex_paint_layers_popover,
    IMAGE_PT_tex_paint_layers,
    IMAGE_PT_2d_canvas_studio,
    IMAGE_PT_2d_canvas_studio_popover,
    NODE_PT_tex_paint_layers,
)


def _register_projection_keymaps():
    _unregister_projection_keymaps()
    wm = getattr(bpy.context, "window_manager", None)
    if wm is None or wm.keyconfigs is None:
        return
    kc = wm.keyconfigs.addon or wm.keyconfigs.default
    if kc is None:
        return

    bindings = (
        ('G', False, 'TRANSLATE'),
        ('R', False, 'ROTATE'),
        ('S', False, 'SCALE'),
        ('G', True, 'RESET_LOC'),
        ('R', True, 'RESET_ROT'),
        ('S', True, 'RESET_SCALE'),
    )
    for km_name, space_type in (("Image Paint", 'EMPTY'), ("3D View", 'VIEW_3D')):
        km = kc.keymaps.get(km_name)
        if km is None:
            km = kc.keymaps.new(name=km_name, space_type=space_type)
        for key_type, use_alt, mode_val in bindings:
            kmi = km.keymap_items.new(
                "node.tex_paint_gizmo_mode",
                type=key_type,
                value='PRESS',
                alt=use_alt,
                head=True,
            )
            kmi.properties.mode = mode_val
            _PROJECTION_KEYMAPS.append((km, kmi))

    # 2D UV Projection Gizmo direct-handle drag (Left Mouse) + G/R/S shortcuts in Image Editor
    for km_name, space_type in (("Image", 'IMAGE_EDITOR'), ("Image Paint", 'EMPTY')):
        km = kc.keymaps.get(km_name)
        if km is None:
            km = kc.keymaps.new(name=km_name, space_type=space_type)
        kmi_lmb = km.keymap_items.new(
            "image.tex_paint_uv_gizmo_transform",
            type='LEFTMOUSE',
            value='PRESS',
            head=True,
        )
        kmi_lmb.properties.mode = 'HIT_TEST'
        _PROJECTION_KEYMAPS.append((km, kmi_lmb))

        for key_type, use_alt, mode_val in bindings:
            kmi_uv = km.keymap_items.new(
                "image.tex_paint_uv_gizmo_transform",
                type=key_type,
                value='PRESS',
                alt=use_alt,
                head=True,
            )
            kmi_uv.properties.mode = mode_val
            _PROJECTION_KEYMAPS.append((km, kmi_uv))


def _unregister_projection_keymaps():
    for km, kmi in _PROJECTION_KEYMAPS:
        try:
            km.keymap_items.remove(kmi)
        except Exception:
            pass
    _PROJECTION_KEYMAPS.clear()


@bpy.app.handlers.persistent
def _on_blend_save_pre(_dummy):
    """Ensure all pending hand-painted mask strokes and debounced Mask Stack JSON states are flushed before saving .blend."""
    try:
        trees = [m.node_tree for m in bpy.data.materials if m.node_tree] + list(bpy.data.node_groups)
        for ntree in trees:
            for node in ntree.nodes:
                if node.bl_idname == "ShaderNodeTexPaint":
                    n_ptr = node.as_pointer()
                    for layer in node.layers:
                        _absorb_pending_brush_strokes(node, layer)
                        l_uid = _layer_uid(layer)
                        st = _RUNTIME_LAYER_STATES.get((n_ptr, l_uid))
                        if st and st.get("effects"):
                            dirty_paint = False
                            for eff in st["effects"]:
                                if eff.get("type") == 'PAINT':
                                    pbuf = _PAINT_EFFECT_BUFFERS.get((n_ptr, l_uid, int(eff["uid"])))
                                    if pbuf is not None:
                                        _sync_paint_effect_z64(eff, pbuf)
                                        dirty_paint = True
                            if dirty_paint:
                                _save_layer_grunge_state(node, layer, st, flush_json=True)
        _flush_dirty_node_json()
    except Exception:
        pass


def _clear_all_runtime_mask_caches(clear_raw_cache=False):
    _RUNTIME_LAYER_STATES.clear()
    _NODE_JSON_CACHE.clear()
    _NODE_LAST_JSON_STR.clear()
    _DIRTY_JSON_NODES.clear()
    _PAINT_EFFECT_BUFFERS.clear()
    _STACK_BELOW_CACHE.clear()
    _PAIR_IDX_CACHE.clear()
    _LAST_COMPOSITED_MASK.clear()
    _LAST_MASK_SAMPLE.clear()
    _PENDING_STROKE_SNAPSHOT.clear()
    _PENDING_HIRES_REFINE.clear()
    if clear_raw_cache:
        _EFFECT_RAW_CACHE.clear()
        _MESH_UV_WIRE_CACHE.clear()
        _GLOBAL_AO_CACHE.clear()
        _UV_TEXEL_GRID_CACHE.clear()


@bpy.app.handlers.persistent
def _on_blend_load_post(_dummy):
    """Clear runtime caches and unpack packed layer buffers when a .blend file is opened."""
    try:
        _clear_all_runtime_mask_caches(clear_raw_cache=True)
        trees = [m.node_tree for m in bpy.data.materials if m.node_tree] + list(bpy.data.node_groups)
        for ntree in trees:
            for node in ntree.nodes:
                if node.bl_idname == "ShaderNodeTexPaint" and len(node.layers) > 0:
                    node.layers[0].opacity = node.layers[0].opacity
    except Exception:
        pass


def _sync_mask_stack_from_restored_mask_ibuf(node, layer):
    """After an ImageUndoStep restores layer.mask_ibuf, sync _LAST_COMPOSITED_MASK and any PAINT sub-layer
    to match the restored mask pixels without overwriting layer.mask_ibuf.
    """
    if node is None or layer is None or not getattr(layer, "use_mask", False):
        return
    c_layer = _C_TexPaintLayer.from_address(layer.as_pointer())
    if not c_layer.mask_ibuf:
        return
    c_imb = _C_ImBuf.from_address(c_layer.mask_ibuf)
    w, h = int(c_imb.x), int(c_imb.y)
    if w <= 0 or h <= 0:
        return

    import numpy as np
    cur_u8 = _read_mask_ibuf_r_u8(c_layer.mask_ibuf, w, h)
    node_ptr = node.as_pointer()
    layer_uid = int(c_layer.uid)
    key = (node_ptr, layer_uid)
    step = max(1, (w * h) // 1024)
    _LAST_COMPOSITED_MASK[key] = cur_u8.copy()
    _LAST_MASK_SAMPLE[key] = cur_u8[::step].copy()

    st = _get_layer_grunge_state(node, layer)
    effects = st.get("effects", [])
    if not effects:
        _sync_layer_mask_to_companion_channels(node, layer)
        return

    target_eff = None
    target_idx = -1
    act_uid = st.get("active_effect_uid", 0)
    for idx, eff in enumerate(effects):
        if eff["uid"] == act_uid and eff["type"] == 'PAINT' and eff.get("enabled", True):
            target_eff = eff
            target_idx = idx
            break
    if target_eff is None:
        for idx in range(len(effects) - 1, -1, -1):
            if effects[idx]["type"] == 'PAINT' and effects[idx].get("enabled", True):
                target_eff = effects[idx]
                target_idx = idx
                break

    if target_eff is not None:
        enabled_indices = [i for i, e in enumerate(effects) if e.get("enabled", True)]
        first_enabled = effects[enabled_indices[0]] if enabled_indices else None
        if first_enabled is None:
            init_val = int(st.get("base_value", 255))
        elif first_enabled["type"] != 'PAINT' and first_enabled.get("blend_mode", "REPLACE") in {'ADD', 'SCREEN', 'MAX'}:
            init_val = 0
        elif first_enabled["type"] != 'PAINT' and first_enabled.get("blend_mode", "REPLACE") in {'MULTIPLY', 'SUBTRACT', 'MIN'}:
            init_val = 255
        else:
            init_val = int(st.get("base_value", 255))

        below_u8 = np.full(w * h, init_val, dtype=np.uint8)
        for i in range(target_idx):
            _evaluate_single_effect_onto_acc(
                node, layer, effects[i], below_u8, w, h,
                context=bpy.context, force_reproject=False, from_gizmo=False, is_first=(i == 0),
            )

        canon_ptr = _canonical_mask_owner_node(node).as_pointer()
        pbuf = _ensure_paint_effect_buffer(canon_ptr, layer_uid, target_eff, w, h)
        val_u8 = pbuf["val_u8"]
        alpha_u8 = pbuf["alpha_u8"]
        val_u8.fill(0)
        alpha_u8.fill(0)

        diff_idx = np.flatnonzero(cur_u8 != below_u8)
        if len(diff_idx) > 0:
            c = cur_u8[diff_idx].astype(np.float32)
            b = below_u8[diff_idx].astype(np.float32)
            brighter = c > b
            s_alpha = np.where(
                brighter,
                (c - b) / np.maximum(255.0 - b, 1.0),
                (b - c) / np.maximum(b, 1.0),
            )
            s_alpha = np.clip(s_alpha, 0.0, 1.0)
            val_u8[diff_idx] = np.where(brighter, 255, 0).astype(np.uint8)
            alpha_u8[diff_idx] = (s_alpha * 255.0 + 0.5).astype(np.uint8)

        pbuf["rev"] = int(pbuf.get("rev", 1)) + 1
        _sync_paint_effect_z64(target_eff, pbuf)
        _save_layer_grunge_state(node, layer, st, flush_json=True)

    _sync_layer_mask_to_companion_channels(node, layer)


@bpy.app.handlers.persistent
def _on_undo_redo_post(_dummy):
    """Invalidate stale Python caches after Undo/Redo and unpack restored layer buffers without clobbering stroke undo."""
    try:
        _clear_all_runtime_mask_caches(clear_raw_cache=False)
        trees = [m.node_tree for m in bpy.data.materials if m.node_tree] + list(bpy.data.node_groups)
        for ntree in trees:
            for node in ntree.nodes:
                if node.bl_idname == "ShaderNodeTexPaint" and len(node.layers) > 0:
                    # Check if C++ ImageUndoStep just restored a stroke buffer (sets runtime_dirty = 1)
                    act_layer = node.active_layer
                    was_stroke_undo = False
                    if act_layer is not None:
                        c_act = _C_TexPaintLayer.from_address(act_layer.as_pointer())
                        was_stroke_undo = bool(c_act.runtime_dirty)

                    node.layers[0].opacity = node.layers[0].opacity

                    if act_layer is not None and getattr(act_layer, "use_mask", False):
                        if was_stroke_undo:
                            _sync_mask_stack_from_restored_mask_ibuf(node, act_layer)
                        else:
                            st = _get_layer_grunge_state(node, act_layer)
                            if st.get("effects"):
                                _evaluate_layer_grunge_mask(
                                    node, act_layer, context=bpy.context, force_reproject=False, skip_absorb=True
                                )
    except Exception:
        pass


def register():
    global _draw_handler_handle, _draw_handler_image_handle
    from bpy.types import WindowManager
    from bpy.utils import register_class

    register_class(VIEW3D_GGT_tex_paint_projection)
    _register_node_grunge_props()
    _register_projection_keymaps()

    if _on_blend_save_pre not in bpy.app.handlers.save_pre:
        bpy.app.handlers.save_pre.append(_on_blend_save_pre)
    if _on_blend_load_post not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_on_blend_load_post)
    if _on_undo_redo_post not in bpy.app.handlers.undo_post:
        bpy.app.handlers.undo_post.append(_on_undo_redo_post)
    if _on_undo_redo_post not in bpy.app.handlers.redo_post:
        bpy.app.handlers.redo_post.append(_on_undo_redo_post)

    WindowManager.tex_paint_gizmo_mode = EnumProperty(
        name="3D Projection Gizmo Mode",
        description="Active 3D gizmo handles for the selected Decal or Triplanar projection layer",
        items=(
            ('TRANSLATE', "Move (G)", "Translate the 3D projection widget (Shortcut: G)", 'ORIENTATION_GLOBAL', 0),
            ('ROTATE', "Rotate (R)", "Rotate the 3D projection widget (Shortcut: R)", 'DRIVER_ROTATIONAL_DIFFERENCE', 1),
            ('SCALE', "Scale (S)", "Scale the 3D projection widget (Shortcut: S)", 'FULLSCREEN_ENTER', 2),
            ('ALL', "All", "Show Move, Rotate, and Scale handles together", 'GIZMO', 3),
        ),
        default='ALL',
        update=lambda _self, context: _tag_view3d_gizmo_refresh(context),
    )

    WindowManager.tex_paint_ui_show_mask_stack = BoolProperty(
        name="Show Mask Stack Inspector",
        description="Expand or collapse the Substance Mask Stack inspector",
        default=True,
    )

    WindowManager.tex_paint_show_mesh_maps_hub = BoolProperty(
        name="Show Mesh Maps & High-Poly Baker Hub",
        description="Expand or collapse the Substance-Painter-style Mesh Maps (Normal, AO, Curvature, ID, Position, Thickness) & High-Poly Baker Hub",
        default=False,
    )

    WindowManager.tex_paint_2d_new_res = EnumProperty(
        name="2D Canvas Resolution",
        description="Default resolution for new 2D Logo, Pattern, or Multi-Layer canvases",
        items=(
            ('512', "512", "512 x 512"),
            ('1024', "1K", "1024 x 1024"),
            ('2048', "2K", "2048 x 2048"),
            ('4096', "4K", "4096 x 4096"),
        ),
        default='1024',
    )

    WindowManager.tex_paint_2d_shape_radius = FloatProperty(
        name="Shape Size",
        description="Radius / size of the stamped 2D shape",
        min=0.02,
        max=1.0,
        default=0.38,
        subtype='FACTOR',
    )
    WindowManager.tex_paint_2d_shape_thickness = FloatProperty(
        name="Shape Outline Thickness",
        description="Outline thickness of the stamped 2D shape (0.0 = filled solid)",
        min=0.0,
        max=0.5,
        default=0.0,
        subtype='FACTOR',
    )
    WindowManager.tex_paint_2d_shape_sides = IntProperty(
        name="Sides / Count",
        description="Polygon sides, star points, or stripe/grid count",
        min=2,
        max=24,
        default=6,
    )
    WindowManager.tex_paint_2d_shape_erase = BoolProperty(
        name="Cutout Alpha",
        description="Erase / cut out the stamped 2D shape from existing alpha instead of drawing with brush color",
        default=False,
    )
    WindowManager.tex_paint_2d_fx_radius = IntProperty(
        name="FX Radius (px)",
        description="Pixel width / blur radius for 2D Outline, Drop Shadow, Blur, and Sharpen",
        min=1,
        max=64,
        default=6,
    )
    WindowManager.tex_paint_2d_fx_strength = FloatProperty(
        name="FX Strength",
        description="Strength / opacity multiplier for 2D Outline, Shadow, Sharpen, and Color-to-Alpha",
        min=0.1,
        max=2.0,
        default=1.0,
    )
    WindowManager.tex_paint_2d_wand_tolerance = FloatProperty(
        name="Magic Wand Tolerance",
        description="Color similarity tolerance for the Krita 2D Magic Wand & Paint Bucket tools (drag left/right in viewport to adjust live)",
        min=0.005,
        max=0.98,
        default=0.14,
        subtype='FACTOR',
    )
    WindowManager.tex_paint_2d_wand_contiguous = BoolProperty(
        name="Contiguous Region Only",
        description="Flood-fill only connected pixels around the clicked point (turn off to select all matching colors across the canvas)",
        default=True,
    )
    WindowManager.tex_paint_2d_wand_merged = BoolProperty(
        name="Sample Merged Canvas",
        description="Sample composite colors across all visible 2D layers instead of only the active layer",
        default=True,
    )
    WindowManager.tex_paint_2d_sel_feather = IntProperty(
        name="Selection Feather (px)",
        description="Soft edge feathering radius in pixels for 2D Magic Wand and Marquee selections",
        min=0,
        max=32,
        default=0,
    )

    if _draw_handler_handle is None:
        _draw_handler_handle = bpy.types.SpaceView3D.draw_handler_add(
            _draw_projection_wireframe_3d, (), 'WINDOW', 'POST_VIEW'
        )
    if _draw_handler_image_handle is None:
        _draw_handler_image_handle = bpy.types.SpaceImageEditor.draw_handler_add(
            _draw_uv_projection_gizmo_2d, (), 'WINDOW', 'POST_PIXEL'
        )

    if not bpy.app.background and not bpy.app.timers.is_registered(_mask_paint_sync_timer):
        bpy.app.timers.register(_mask_paint_sync_timer, first_interval=0.25, persistent=True)


def unregister():
    global _draw_handler_handle, _draw_handler_image_handle, _PREVIEW_COLLECTION
    from bpy.types import WindowManager
    from bpy.utils import unregister_class

    for handler_list, fn in (
        (bpy.app.handlers.save_pre, _on_blend_save_pre),
        (bpy.app.handlers.load_post, _on_blend_load_post),
        (bpy.app.handlers.undo_post, _on_undo_redo_post),
        (bpy.app.handlers.redo_post, _on_undo_redo_post),
    ):
        if fn in handler_list:
            try:
                handler_list.remove(fn)
            except Exception:
                pass

    _unregister_projection_keymaps()

    if bpy.app.timers.is_registered(_mask_paint_sync_timer):
        try:
            bpy.app.timers.unregister(_mask_paint_sync_timer)
        except Exception:
            pass

    if _draw_handler_handle is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_draw_handler_handle, 'WINDOW')
        _draw_handler_handle = None
    if _draw_handler_image_handle is not None:
        bpy.types.SpaceImageEditor.draw_handler_remove(_draw_handler_image_handle, 'WINDOW')
        _draw_handler_image_handle = None

    try:
        unregister_class(VIEW3D_GGT_tex_paint_projection)
    except RuntimeError:
        pass

    _unregister_node_grunge_props()

    if _PREVIEW_COLLECTION is not None:
        try:
            from bpy.utils import previews
            previews.remove(_PREVIEW_COLLECTION)
        except Exception:
            pass
        _PREVIEW_COLLECTION = None
    _GRUNGE_CATALOG.clear()
    _GRUNGE_ENUM_CACHE.clear()
    _GRUNGE_LOOKUP.clear()

    for wm_prop in (
        "tex_paint_gizmo_mode",
        "tex_paint_ui_show_mask_stack",
        "tex_paint_show_mesh_maps_hub",
        "tex_paint_2d_new_res",
        "tex_paint_2d_shape_radius",
        "tex_paint_2d_shape_thickness",
        "tex_paint_2d_shape_sides",
        "tex_paint_2d_shape_erase",
        "tex_paint_2d_fx_radius",
        "tex_paint_2d_fx_strength",
        "tex_paint_2d_wand_tolerance",
        "tex_paint_2d_wand_contiguous",
        "tex_paint_2d_wand_merged",
        "tex_paint_2d_sel_feather",
    ):
        if hasattr(WindowManager, wm_prop):
            delattr(WindowManager, wm_prop)


if __name__ == "__main__":  # only for live edit.
    from bpy.utils import register_class
    for cls in classes:
        register_class(cls)
    register()
