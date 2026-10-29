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


def _make_default_mask_effect(effect_type='GRUNGE', uid=1, is_first=True, gen_type='EDGE_WEAR', fill_source='GRUNGE'):
    if effect_type == 'GENERATOR':
        default_blend = 'REPLACE' if is_first else 'ADD'
        name = _MASK_GEN_LABELS.get(gen_type, "Smart Mask")
    elif effect_type == 'PAINT':
        default_blend = 'REPLACE'  # Normal (Paint White = Reveal, Paint Black = Hide)
        name = "Paint Mask"
    elif effect_type == 'LEVELS':
        default_blend = 'REPLACE'
        name = "Levels & Contrast"
    elif fill_source == 'CUSTOM_IMAGE':
        default_blend = 'REPLACE' if is_first else 'ADD'
        name = "Image Fill"
    else:
        default_blend = 'REPLACE' if is_first else 'MULTIPLY'
        name = "Grunge Fill"

    is_custom_img = (effect_type == 'GRUNGE' and fill_source == 'CUSTOM_IMAGE')
    def_scale = 1.0 if is_custom_img else 2.0
    def_contrast = 0.0 if is_custom_img else 0.30

    return {
        "uid": int(uid),
        "name": name,
        "type": effect_type,         # 'GENERATOR' | 'PAINT' | 'GRUNGE' | 'LEVELS'
        "enabled": True,
        "blend_mode": default_blend, # 'REPLACE' | 'MULTIPLY' | 'SUBTRACT' | 'ADD' | 'SCREEN' | 'OVERLAY' | 'MAX' | 'MIN'
        "opacity": 1.0,
        "invert": False,

        # GENERATOR (Smart Mask) settings
        "gen_type": gen_type,
        "gen_balance": 0.50,
        "gen_contrast": 0.45,
        "gen_radius": 0.35,
        "gen_noise": 0.0,
        "gen_noise_scale": 8.0,

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
        eff["name"] = _MASK_GEN_LABELS.get(eff["gen_type"], "Smart Mask")

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


def _find_mesh_object_for_node(node, context=None):
    if context is not None:
        ob = getattr(context, "active_object", None) or getattr(context, "image_paint_object", None)
        if ob is not None and ob.type == 'MESH' and ob.data is not None:
            return ob
    if bpy.context is not None:
        ob = getattr(bpy.context, "active_object", None)
        if ob is not None and ob.type == 'MESH' and ob.data is not None:
            return ob
    for ob in bpy.data.objects:
        if ob.type == 'MESH' and ob.data is not None:
            return ob
    return None


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


def _ensure_generator_raw_u8(node, layer, eff, w, h, force=False):
    """Evaluate C++ BKE_tex_paint_layer_generate_mask in raw mode (balance=0.5, contrast=0.0) and cache uint8[w*h]."""
    global _INTERNAL_REPROJECTING
    import numpy as np

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

    sig = ("GEN", gen_type_int, round(radius, 4), round(noise, 4), round(noise_scale, 4), w, h)
    cached = _EFFECT_RAW_CACHE.get(rkey)
    if not force and cached is not None and cached.get("sig") == sig:
        return cached["raw_u8"]

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

    _INTERNAL_REPROJECTING = True
    try:
        c_node_id.value = 0
        c_layer.mask_gen_type = gen_type_int
        c_layer.mask_gen_blend = 0  # REPLACE
        c_layer.mask_gen_balance = 0.5
        c_layer.mask_gen_contrast = 0.0
        c_layer.mask_gen_radius = radius
        c_layer.mask_gen_noise = noise
        c_layer.mask_gen_invert = 0
        # Trigger rna_TexPaintLayer_mask_gen_update -> BKE_tex_paint_layer_generate_mask
        layer.mask_gen_noise_scale = noise_scale
    finally:
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

    c_imb = _C_ImBuf.from_address(c_layer.mask_ibuf)
    if c_imb.byte_data:
        buf = (ctypes.c_uint8 * (w * h * 4)).from_address(c_imb.byte_data)
        raw_u8 = np.frombuffer(buf, dtype=np.uint8).reshape(-1, 4)[:, 0].copy()
    elif c_imb.float_data:
        buf = (ctypes.c_float * (w * h * 4)).from_address(c_imb.float_data)
        arr_f = np.frombuffer(buf, dtype=np.float32).reshape(-1, 4)[:, 0]
        raw_u8 = (np.clip(arr_f, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)
    else:
        raw_u8 = np.zeros(w * h, dtype=np.uint8)

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

    ob = _find_mesh_object_for_node(node, context)
    bbox_center = Vector((0.0, 0.0, 0.0))
    max_extent = 2.0
    if ob is not None and getattr(ob, "bound_box", None):
        pts = [Vector(co) for co in ob.bound_box]
        bmin = Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
        bmax = Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
        bbox_center = (bmin + bmax) * 0.5
        bsize = bmax - bmin
        max_extent = max(bsize.x, bsize.y, bsize.z, 1e-3)

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

    if proj_mode_int in {1, 2}:
        loc = (off_x, off_y, 0.0)
        rot = (0.0, 0.0, rot_z)
        if proj_mode_int == 2:
            scl = (sx, sy, 1.0)
        else:
            scl = (1.0 / sx, 1.0 / sy, 1.0)
    else:
        if eff.get("gizmo_override") and eff.get("gizmo_loc") and eff.get("gizmo_rot"):
            base_loc = Vector(eff["gizmo_loc"])
            base_rot_mat = Euler(eff["gizmo_rot"], 'XYZ').to_matrix()
        elif proj_mode_str in {'DECAL', 'WARP'}:
            base_loc = Vector((bbox_center.x, bbox_center.y + max_extent * 0.5, bbox_center.z))
            base_rot_mat = Euler((math.pi * 0.5, 0.0, 0.0), 'XYZ').to_matrix()
        else:
            base_loc = bbox_center.copy()
            base_rot_mat = Matrix.Identity(3)

        ax_x = base_rot_mat.col[0].normalized()
        ax_y = base_rot_mat.col[1].normalized()
        final_loc = base_loc + ax_x * (off_x * max_extent * 0.5) + ax_y * (off_y * max_extent * 0.5)
        final_rot_mat = base_rot_mat @ Matrix.Rotation(rot_z, 3, 'Z')
        final_rot = final_rot_mat.to_euler('XYZ')

        half_x = max(max_extent * 0.5 / sx, 1e-3)
        half_y = max(max_extent * 0.5 / sy, 1e-3)
        half_z = (
            max(max_extent * 0.5 / ((sx + sy) * 0.5), 1e-3)
            if proj_mode_str == 'TRIPLANAR'
            else max(max_extent * 0.6, 1e-3)
        )
        # Sign of scl[2] communicates repeat_uv to C++ BKE_tex_paint_layer_reproject
        if proj_mode_str == 'TRIPLANAR':
            z_signed = half_z if is_repeat else -half_z
        else:
            z_signed = -half_z if is_repeat else half_z

        loc = (float(final_loc.x), float(final_loc.y), float(final_loc.z))
        rot = (float(final_rot.x), float(final_rot.y), float(final_rot.z))
        scl = (half_x, half_y, z_signed)

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

    _INTERNAL_REPROJECTING = True
    try:
        c_node_id.value = 0
        c_layer.type = 0  # TEXPAINT_LAYER_PAINT
        c_layer.ibuf = orig_mask_ibuf
        c_layer.proj_mode = proj_mode_int
        c_layer.proj_image_name = src_ima.name.encode('utf-8')[:63]
        for i in range(3):
            c_layer.proj_location[i] = loc[i]
            c_layer.proj_rotation[i] = rot[i]
            c_layer.proj_scale[i] = scl[i]
        layer.proj_blend_falloff = box_blend
    finally:
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

    c_imb = _C_ImBuf.from_address(orig_mask_ibuf)
    if c_imb.byte_data:
        buf = (ctypes.c_uint8 * (w * h * 4)).from_address(c_imb.byte_data)
        rgba = np.frombuffer(buf, dtype=np.uint8).reshape(-1, 4)
        if (
            channel_mode == 'AUTO' and
            eff.get("category", "BITMAP_GRUNGES") != 'CUSTOM' and
            proj_mode_int in {2, 3} and
            is_repeat
        ):
            raw_u8 = rgba[:, 0].copy()
        elif channel_mode == 'ALPHA':
            raw_u8 = rgba[:, 3].copy()
        elif channel_mode in {'RED', 'GREEN', 'BLUE'}:
            ch_i = {'RED': 0, 'GREEN': 1, 'BLUE': 2}[channel_mode]
            a_u16 = rgba[:, 3].astype(np.uint16)
            raw_u8 = ((rgba[:, ch_i].astype(np.uint16) * a_u16 + 127) >> 8).astype(np.uint8)
        elif channel_mode == 'LUMINANCE':
            lum_u16 = (
                rgba[:, 0].astype(np.uint16) * 77 +
                rgba[:, 1].astype(np.uint16) * 150 +
                rgba[:, 2].astype(np.uint16) * 29
            ) >> 8
            raw_u8 = lum_u16.astype(np.uint8)
        else:
            # 'AUTO': Luminance * Alpha, with automatic detection for black-on-transparent PNG decals
            a_u16 = rgba[:, 3].astype(np.uint16)
            lum_u16 = (
                rgba[:, 0].astype(np.uint16) * 77 +
                rgba[:, 1].astype(np.uint16) * 150 +
                rgba[:, 2].astype(np.uint16) * 29
            ) >> 8
            if int(lum_u16.max()) == 0 and int(a_u16.max()) > 0:
                raw_u8 = rgba[:, 3].copy()
            else:
                raw_u8 = ((lum_u16 * a_u16 + 127) >> 8).astype(np.uint8)
    elif c_imb.float_data:
        buf = (ctypes.c_float * (w * h * 4)).from_address(c_imb.float_data)
        arr = np.frombuffer(buf, dtype=np.float32).reshape(-1, 4)
        cov = np.clip(arr[:, 3], 0.0, 1.0)
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
        raw_u8 = (raw_f * 255.0 + 0.5).astype(np.uint8)
    else:
        raw_u8 = np.zeros(w * h, dtype=np.uint8)

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


def _notify_gizmo_projection_changed(node, layer, context=None):
    if _is_layer_grunge_gizmo_eligible(node, layer):
        _evaluate_layer_grunge_mask(node, layer, context=context, force_reproject=True, from_gizmo=True, interactive=True)


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
        ray_dir_world = view3d_utils.region_2d_to_vector_3d(region, rv3d, coord)

        ob_inv = ob.matrix_world.inverted_safe()
        ray_origin_obj = ob_inv @ ray_origin_world
        ray_dir_obj = (ob_inv.to_3x3() @ ray_dir_world).normalized()

        hit, hit_loc, hit_no, _face_idx = ob.ray_cast(ray_origin_obj, ray_dir_obj)
        if not hit:
            return False

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


def _draw_uv_projection_gizmo_2d():
    """Draw the 3D Mesh UV layout overlay and interactive 2D UV Projection Gizmo in the Image Editor."""
    if bpy.app.background:
        return
    context = bpy.context
    if context is None or context.space_data is None or context.space_data.type != 'IMAGE_EDITOR':
        return
    region = context.region
    if region is None or region.type != 'WINDOW':
        return

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
            ('GENERATOR', "Procedural (Smart Mask)", "Add a 3D geometry generator (Edge Wear, Curvature, Cavity, AO, Top Dust, Height)", 'MODIFIER', 0),
            ('PAINT', "Paint Mask", "Add a non-destructive hand-painted mask layer (paint White to Reveal, Black to Hide)", 'BRUSH_DATA', 1),
            ('GRUNGE', "Mask Fill (Image / Grunge)", "Fill the mask with any custom grayscale/alpha image or Substance Grunge texture with 2D/3D projection", 'TEXTURE', 2),
            ('LEVELS', "Levels & Contrast", "Add a non-destructive Balance / Contrast / Invert filter over the mask stack below", 'SEQ_LUMA_WAVEFORM', 3),
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


class TEXPAINT_MT_layer_specials(Menu):
    """Special layer operations (Fill, Clear, Invert, Flip, Rasterize, Copy Mask, Merge Visible, Flatten)"""
    bl_idname = "TEXPAINT_MT_layer_specials"
    bl_label = "Layer Specials"

    def draw(self, _context):
        layout = self.layout
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
    """Apply a 1-click Substance Painter multi-layer Smart Weathering Mask Recipe (Generator + Grunge + Levels/Paint)"""
    bl_idname = "node.tex_paint_smart_mask_recipe"
    bl_label = "Apply Smart Mask Recipe"
    bl_options = {'REGISTER', 'UNDO'}

    recipe: EnumProperty(
        name="Smart Mask Recipe",
        items=(
            ('EDGE_SCRATCHES', "Edge Wear + Scratches", "Sharp edge wear broken up by concrete/scratch grunge + Paint touchup layer", 'MOD_BEVEL', 0),
            ('CAVITY_DIRT', "Cavity Dirt & Grime", "Concave crevices & seams multiplied with organic dirt grunge", 'SHARPCURVE', 1),
            ('TOP_DUST', "Top-Down Dust / Moss", "Upward-facing surface normals broken up with soft procedural noise", 'NORMALS_FACE', 2),
            ('RUST_LEAKS', "Rust & Vertical Streaks", "Occluded crevices combined with vertical leak grunge and contrast boost", 'MOD_FLUIDSIM', 3),
            ('HEAVY_CHIPPING', "Heavy Paint Chipping", "Broad edge wear chipped away with high-contrast procedural spots", 'MOD_EXPLODE', 4),
            ('PAINT_PEEL', "Weathered Surface Peel", "Curvature base + Overlay cracked concrete grunge + hand-paintable mask layer", 'BRUSH_DATA', 5),
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
    """Substance-Painter-style 1-click multi-layer Smart Weathering Mask Recipes"""
    bl_idname = "TEXPAINT_MT_smart_mask_recipes"
    bl_label = "Smart Weathering Mask Recipes"

    def draw(self, _context):
        layout = self.layout
        for rec_id, rec_lbl, _desc, rec_icon, _idx in NODE_OT_tex_paint_smart_mask_recipe.bl_rna.properties["recipe"].enum_items:
            op = layout.operator("node.tex_paint_smart_mask_recipe", text=rec_lbl, icon=rec_icon)
            op.recipe = rec_id


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
            layout.menu("TEXPAINT_MT_smart_mask_recipes", text="Smart Weathering Masks", icon='PRESET')


class TEXPAINT_MT_add_mask_effect(Menu):
    """Substance-Painter-style + Add Effect dropdown menu between the Up/Down arrows on the Mask Stack"""
    bl_idname = "TEXPAINT_MT_add_mask_effect"
    bl_label = "Add Mask Sub-Layer"

    def draw(self, context):
        layout = self.layout
        space = getattr(context, "space_data", None)
        node = tex_paint_node_from_context(context)
        is_2d_only = bool((space and space.type == 'IMAGE_EDITOR') or _is_standalone_2d_node(node))

        op_fill = layout.operator("node.tex_paint_mask_effect_add", text="Add Fill (Image / Stencil)", icon='FILE_IMAGE')
        op_fill.effect_type = 'GRUNGE'
        op_fill.fill_source = 'CUSTOM_IMAGE'

        layout.operator("node.tex_paint_grunge_open_image", text="Open Image File into Mask...", icon='FILE_FOLDER')

        op_grn = layout.operator("node.tex_paint_mask_effect_add", text="Add Grunge Fill (275 Library)", icon='TEXTURE')
        op_grn.effect_type = 'GRUNGE'
        op_grn.fill_source = 'GRUNGE'

        op_pnt = layout.operator("node.tex_paint_mask_effect_add", text="Add Paint (Hand Brush)", icon='BRUSH_DATA')
        op_pnt.effect_type = 'PAINT'

        op_lvl = layout.operator("node.tex_paint_mask_effect_add", text="Add Levels (Balance / Contrast)", icon='SEQ_LUMA_WAVEFORM')
        op_lvl.effect_type = 'LEVELS'

        if not is_2d_only:
            layout.separator()
            gen_items = (
                ('EDGE_WEAR', "Add Generator: Edge Wear", 'MOD_BEVEL'),
                ('CAVITY', "Add Generator: Cavity & Dirt", 'SHARPCURVE'),
                ('CURVATURE', "Add Generator: Curvature", 'SMOOTHCURVE'),
                ('AO', "Add Generator: Ambient Occlusion", 'SHADING_RENDERED'),
                ('NORMAL_UP', "Add Generator: Top-Down Dust", 'NORMALS_FACE'),
                ('POSITION_Z', "Add Generator: Vertical Height", 'EMPTY_SINGLE_ARROW'),
            )
            for g_id, g_lbl, g_icon in gen_items:
                op_g = layout.operator("node.tex_paint_mask_effect_add", text=g_lbl, icon=g_icon)
                op_g.effect_type = 'GENERATOR'
                op_g.gen_type = g_id

            layout.separator()
            layout.menu("TEXPAINT_MT_smart_mask_recipes", text="Smart Weathering Recipes", icon='PRESET')

        layout.separator()
        layout.operator("node.tex_paint_mask_effect_duplicate", text="Duplicate Active Sub-Layer", icon='DUPLICATE')
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

    elif etype == 'PAINT':
        _draw_mask_brush_bar(layout, context, node, layer, is_paint_sublayer=True)

    elif etype == 'LEVELS':
        lvl_row = layout.row(align=True)
        lvl_row.prop(node, "tex_paint_mask_eff_lvl_balance", text="Balance", slider=True)
        lvl_row.prop(node, "tex_paint_mask_eff_lvl_contrast", text="Contrast", slider=True)
        lvl_row.prop(node, "tex_paint_grunge_invert", text="", toggle=True, icon='ARROW_LEFTRIGHT')


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
        foot_row = layout.row(align=True)
        foot_row.label(
            text=iface_("Canvas: {:d} x {:d} {:s}").format(
                node.canvas_width, node.canvas_height, iface_("Float") if node.use_float else iface_("Byte"),
            ),
            icon='IMAGE_RGB_ALPHA',
        )
        foot_row.operator("node.tex_paint_canvas_resize", text="Resize", icon='FULLSCREEN_ENTER')


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
            ('BLUR', "Gaussian Blur", "Smoothly blur the 2D image or active layer"),
            ('SHARPEN', "Sharpen (Unsharp Mask)", "Enhance crispness and edge contrast"),
            ('COLOR_TO_ALPHA_WHITE', "White BG -> Transparent", "Convert white background into clean alpha transparency (Krita Color-to-Alpha)"),
            ('COLOR_TO_ALPHA_BLACK', "Black BG -> Transparent", "Convert black background into clean alpha transparency"),
            ('INVERT_RGB', "Invert Colors", "Invert RGB channels"),
            ('CLEAR_TRANSPARENT', "Clear to Transparent", "Clear the active 2D layer or image to 100% transparent"),
            ('FILL_BRUSH', "Fill with Brush Color", "Fill the active 2D layer or image with the current brush color"),
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

        _write_active_2d_rgba_f32(context, ima, node, layer, rgba)
        return {'FINISHED'}


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

        # Row 2: Seamless Wrap Toggles + Transform Dropdown Menu on one row
        wrap_row = layout.row(align=True)
        ip = getattr(context.tool_settings, "image_paint", None) if context.tool_settings else None
        if ip is not None:
            wrap_row.prop(ip, "tile_x", text="Wrap X", toggle=True, icon='MOD_MIRROR')
            wrap_row.prop(ip, "tile_y", text="Wrap Y", toggle=True, icon='MOD_MIRROR')
        if hasattr(space, "show_repeat"):
            wrap_row.prop(space, "show_repeat", text="Repeat", toggle=True, icon='MESH_GRID')
        wrap_row.menu("TEXPAINT_MT_2d_transform", text="Transform", icon='ORIENTATION_VIEW')
        wrap_row.operator("image.tex_paint_2d_edit", text="", icon='FULLSCREEN_ENTER').action = 'WRAP_50'

        layout.separator(factor=0.3)

        # Row 3: 2D Shape & Logo Stamp Dropdown + Inline Parametric Controls
        sh_col = layout.column(align=True)
        sh_row = sh_col.row(align=True)
        sh_row.menu("TEXPAINT_MT_2d_shapes", text="Stamp Shape", icon='MESH_CIRCLE')
        if wm and hasattr(wm, "tex_paint_2d_shape_radius"):
            sh_row.prop(wm, "tex_paint_2d_shape_radius", text="Size", slider=True)
            sh_row.prop(wm, "tex_paint_2d_shape_thickness", text="Outline", slider=True)
            sh_row.prop(wm, "tex_paint_2d_shape_sides", text="Sides")
            sh_row.prop(wm, "tex_paint_2d_shape_erase", text="", toggle=True, icon='IMAGE_ALPHA')

        layout.separator(factor=0.25)

        # Row 4: 2D Filters, Outline & Alpha FX Dropdown + Inline Radius/Strength Sliders
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
        "tex_paint_mask_eff_lvl_balance",
        "tex_paint_mask_eff_lvl_contrast",
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
    TEXPAINT_MT_layer_specials,
    NODE_OT_tex_paint_smart_mask_recipe,
    TEXPAINT_MT_smart_mask_recipes,
    TEXPAINT_MT_add_layer,
    TEXPAINT_MT_add_mask_effect,
    NODE_OT_tex_paint_quick_uv,
    IMAGE_OT_tex_paint_new_2d_canvas,
    IMAGE_OT_tex_paint_2d_layer_mode,
    IMAGE_OT_tex_paint_2d_edit,
    IMAGE_OT_tex_paint_2d_shape,
    IMAGE_OT_tex_paint_2d_fx,
    IMAGE_OT_tex_paint_send_to_3d,
    TEXPAINT_MT_2d_canvas_new,
    TEXPAINT_MT_2d_send_to_3d,
    TEXPAINT_MT_2d_transform,
    TEXPAINT_MT_2d_shapes,
    TEXPAINT_MT_2d_fx,
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


@bpy.app.handlers.persistent
def _on_undo_redo_post(_dummy):
    """Invalidate stale Python caches after Undo/Redo and unpack restored layer buffers."""
    try:
        _clear_all_runtime_mask_caches(clear_raw_cache=False)
        trees = [m.node_tree for m in bpy.data.materials if m.node_tree] + list(bpy.data.node_groups)
        for ntree in trees:
            for node in ntree.nodes:
                if node.bl_idname == "ShaderNodeTexPaint" and len(node.layers) > 0:
                    node.layers[0].opacity = node.layers[0].opacity
                    act_layer = node.active_layer
                    if act_layer is not None and getattr(act_layer, "use_mask", False):
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
        "tex_paint_2d_new_res",
        "tex_paint_2d_shape_radius",
        "tex_paint_2d_shape_thickness",
        "tex_paint_2d_shape_sides",
        "tex_paint_2d_shape_erase",
        "tex_paint_2d_fx_radius",
        "tex_paint_2d_fx_strength",
    ):
        if hasattr(WindowManager, wm_prop):
            delattr(WindowManager, wm_prop)


if __name__ == "__main__":  # only for live edit.
    from bpy.utils import register_class
    for cls in classes:
        register_class(cls)
    register()
