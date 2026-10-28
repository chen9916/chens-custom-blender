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
from bpy.types import GizmoGroup, Operator, Panel, UIList
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
    ('BITMAP_GRUNGES', "Bitmap Grunges (86)", "Scanned & photographic Substance Painter grunge maps (rust, dirt, concrete, leaks, scratches, wipes)", 'TEXTURE', 0),
    ('PROC_GRUNGES', "Procedural Grunges (30)", "Substance procedural grunge maps (spots, leaky paint, damas, galet, dirty)", 'MOD_OCEAN', 1),
    ('NOISES', "Noises & Dirt (64)", "Substance procedural noise, dirt, clouds, cells, moisture, and Voronoi maps", 'FORCE_TURBULENCE', 2),
    ('PATTERNS', "Patterns & Gradients (49)", "Substance geometric patterns, scratches, fibers, tiles, and gradients", 'MESH_GRID', 3),
    ('FABRICS', "Fabrics & Weaves (46)", "Substance fabric weaves, carbon fiber, canvas, leather, and mesh patterns", 'MATCLOTH', 4),
    ('CUSTOM', "Custom Image", "Use any loaded Blender image or import a custom texture file from disk", 'FILE_IMAGE', 5),
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
_GRUNGE_ENUM_CACHE = {}    # category -> list of Blender EnumProperty tuples
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
                _GRUNGE_LOOKUP[item_id] = info
                _GRUNGE_LOOKUP.setdefault(stem, info)
        _GRUNGE_CATALOG[cat_id] = entries


def _get_grunge_enum_items_for_category(cat_id):
    _ensure_grunge_catalog()
    entries = _GRUNGE_CATALOG.get(cat_id, [])
    if not entries:
        return [('NONE', "No Grunge Found", "No grunge textures found in library", 'TEXTURE', 0)]

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
    _GRUNGE_ENUM_CACHE[cat_id] = items
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


def _make_default_mask_effect(effect_type='GRUNGE', uid=1, is_first=True, gen_type='EDGE_WEAR'):
    if effect_type == 'GENERATOR':
        default_blend = 'REPLACE' if is_first else 'ADD'
        name = _MASK_GEN_LABELS.get(gen_type, "Smart Mask")
    elif effect_type == 'PAINT':
        default_blend = 'REPLACE'  # Normal (Paint White = Reveal, Paint Black = Hide)
        name = "Paint Mask"
    elif effect_type == 'LEVELS':
        default_blend = 'REPLACE'
        name = "Levels & Contrast"
    else:
        default_blend = 'REPLACE' if is_first else 'MULTIPLY'
        name = "Grunge Concrete"

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

        # GRUNGE (Substance Grunge Fill) settings
        "category": "BITMAP_GRUNGES",
        "texture": "BITMAP_GRUNGES::grunge_concrete_cracked",
        "custom_image": "",
        "proj_mode": "TRIPLANAR",
        "scale": 2.0,
        "balance": 0.50,
        "contrast": 0.30,
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


def _get_layer_grunge_state(node, layer=None):
    """Read-only lookup of the Mask Stack state for (node, layer), safe to call inside UI draw()."""
    if node is None:
        return dict(_DEFAULT_LAYER_MASK_STATE, effects=[])
    if layer is None:
        layer = getattr(node, "active_layer", None)
    uid = _layer_uid(layer)
    if uid <= 0:
        return dict(_DEFAULT_LAYER_MASK_STATE, effects=[])

    node_ptr = node.as_pointer()
    key = (node_ptr, uid)
    st = _RUNTIME_LAYER_STATES.get(key)
    if st is not None:
        return st

    st = dict(_DEFAULT_LAYER_MASK_STATE, effects=[])
    all_data = _NODE_JSON_CACHE.get(node_ptr)
    if all_data is None:
        raw_json = node.get("_grunge_layers_json", "")
        if raw_json:
            try:
                all_data = json.loads(raw_json)
            except Exception:
                all_data = {}
        if not isinstance(all_data, dict):
            all_data = {}
        _NODE_JSON_CACHE[node_ptr] = all_data

    saved = all_data.get(str(uid))
    if isinstance(saved, dict):
        st.update(saved)

    _migrate_legacy_state_if_needed(st, layer)
    _RUNTIME_LAYER_STATES[key] = st
    return st


def _flush_dirty_node_json(node=None):
    """Write pending Mask Stack state dicts into node['_grunge_layers_json']."""
    if node is not None:
        node_ptr = node.as_pointer()
        all_data = _NODE_JSON_CACHE.get(node_ptr)
        if all_data is not None:
            try:
                node["_grunge_layers_json"] = json.dumps(all_data)
            except Exception:
                pass
        _DIRTY_JSON_NODES.pop(node_ptr, None)
        return

    for node_ptr, n_ref in list(_DIRTY_JSON_NODES.items()):
        all_data = _NODE_JSON_CACHE.get(node_ptr)
        if all_data is not None and n_ref is not None:
            try:
                n_ref["_grunge_layers_json"] = json.dumps(all_data)
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

    node_ptr = node.as_pointer()
    st = _get_layer_grunge_state(node, layer)
    if updates and updates is not st:
        st.update(updates)
    _migrate_legacy_state_if_needed(st, layer)
    _RUNTIME_LAYER_STATES[(node_ptr, uid)] = st

    all_data = _NODE_JSON_CACHE.get(node_ptr)
    if not isinstance(all_data, dict):
        try:
            raw_json = node.get("_grunge_layers_json", "")
            all_data = json.loads(raw_json) if raw_json else {}
            if not isinstance(all_data, dict):
                all_data = {}
        except Exception:
            all_data = {}
        _NODE_JSON_CACHE[node_ptr] = all_data

    all_data[str(uid)] = st
    if flush_json or bpy.app.background:
        _flush_dirty_node_json(node)
    else:
        _DIRTY_JSON_NODES[node_ptr] = node
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
    st = _get_layer_grunge_state(node, layer)
    effects = st.get("effects", [])
    eff = _get_active_mask_effect(node, layer)
    if eff is None or (ensure_type is not None and eff.get("type") != ensure_type):
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

    pbuf = _ensure_paint_effect_buffer(node_ptr, layer_uid, target_eff, w, h)
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
    """Evaluate C++ BKE_tex_paint_layer_reproject into mask_ibuf and cache uint8[w*h] for this grunge effect."""
    global _INTERNAL_REPROJECTING
    import numpy as np

    src_ima = _resolve_grunge_image(eff, interactive=interactive)
    if src_ima is None:
        return np.full(w * h, 255, dtype=np.uint8)

    node_ptr = node.as_pointer()
    c_layer = _C_TexPaintLayer.from_address(layer.as_pointer())
    layer_uid = int(c_layer.uid)
    eff_uid = int(eff["uid"])
    rkey = (node_ptr, layer_uid, eff_uid)

    proj_mode_str = eff.get("proj_mode", "TRIPLANAR")
    proj_mode_map = {'FIT': 1, 'REPEAT': 2, 'TRIPLANAR': 3, 'DECAL': 4}
    proj_mode_int = proj_mode_map.get(proj_mode_str, 3)

    scale = max(float(eff.get("scale", 2.0)), 0.02)
    rot_z = float(eff.get("rotation", 0.0))
    off_x = float(eff.get("offset_x", 0.0))
    off_y = float(eff.get("offset_y", 0.0))
    box_blend = float(np.clip(eff.get("box_blend", 0.5), 0.0, 1.0))

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

    if from_gizmo and getattr(layer, "show_projection_widget", False):
        eff["gizmo_override"] = True
    use_gizmo_xform = (from_gizmo or bool(eff.get("gizmo_override", False))) and getattr(layer, "show_projection_widget", False)

    if use_gizmo_xform and proj_mode_str in {'TRIPLANAR', 'DECAL'}:
        loc = (float(c_layer.proj_location[0]), float(c_layer.proj_location[1]), float(c_layer.proj_location[2]))
        rot = (float(c_layer.proj_rotation[0]), float(c_layer.proj_rotation[1]), float(c_layer.proj_rotation[2]))
        scl = (
            max(float(c_layer.proj_scale[0]), 1e-3),
            max(float(c_layer.proj_scale[1]), 1e-3),
            max(float(c_layer.proj_scale[2]), 1e-3),
        )
    elif proj_mode_str == 'REPEAT':
        loc = (off_x, off_y, 0.0)
        rot = (0.0, 0.0, rot_z)
        scl = (scale, scale, 1.0)
    elif proj_mode_str == 'DECAL':
        half_s = max(max_extent * 0.45 / scale, 1e-3)
        if getattr(layer, "show_projection_widget", False):
            loc = (float(c_layer.proj_location[0]), float(c_layer.proj_location[1]), float(c_layer.proj_location[2]))
            rot = (float(c_layer.proj_rotation[0]), float(c_layer.proj_rotation[1]), rot_z)
        else:
            loc = (
                bbox_center.x + off_x * max_extent * 0.5,
                bbox_center.y + max_extent * 0.5,
                bbox_center.z + off_y * max_extent * 0.5,
            )
            rot = (math.pi * 0.5, 0.0, rot_z)
        scl = (half_s, half_s, max(max_extent * 0.5, 1e-3))
    else:
        half_s = max(max_extent * 0.5 / scale, 1e-3)
        loc = (
            bbox_center.x + off_x * max_extent,
            bbox_center.y + off_y * max_extent,
            bbox_center.z,
        )
        rot = (0.0, 0.0, rot_z)
        scl = (half_s, half_s, half_s)

    sig = (
        "GRUNGE",
        src_ima.name,
        proj_mode_int,
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

    c_imb = _C_ImBuf.from_address(orig_mask_ibuf)
    if c_imb.byte_data:
        buf = (ctypes.c_uint8 * (w * h * 4)).from_address(c_imb.byte_data)
        raw_u8 = np.frombuffer(buf, dtype=np.uint8).reshape(-1, 4)[:, 0].copy()
    elif c_imb.float_data:
        buf = (ctypes.c_float * (w * h * 4)).from_address(c_imb.float_data)
        arr = np.frombuffer(buf, dtype=np.float32).reshape(-1, 4)
        cov = np.clip(arr[:, 3], 0.0, 1.0)
        raw_f = np.where(cov > 1e-5, np.clip(arr[:, 0] / np.maximum(cov, 1e-5), 0.0, 1.0), 0.0)
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
            eff.get("proj_mode", "TRIPLANAR"),
            round(float(eff.get("scale", 2.0)), 4),
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
        node_ptr = node.as_pointer()
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

    # 1. Ensure the layer mask buffer is allocated in C++
    if not layer.use_mask:
        layer.use_mask = True
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
    can_fuse_top = (
        has_byte_dst and
        top_eff is not None and
        top_eff.get("type") in {'GRUNGE', 'GENERATOR', 'LEVELS'} and
        top_opacity > 1e-5
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


def tex_paint_node_from_context(context):
    """Find the layered paint node edited in the current context, or None."""
    if context is None:
        return None
    space = getattr(context, "space_data", None)
    if space and space.type == 'NODE_EDITOR':
        node = getattr(context, "active_node", None)
        if node and node.bl_idname == "ShaderNodeTexPaint":
            return node
        return _find_any_tex_paint_node(getattr(space, "edit_tree", None))

    if space and space.type == 'IMAGE_EDITOR' and getattr(space, "image", None) is not None:
        target_image = space.image
        ob = getattr(context, "active_object", None)
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

    ob = getattr(context, "active_object", None) or getattr(context, "image_paint_object", None)
    mat = ob.active_material if ob else None
    if mat is None or mat.node_tree is None:
        return None
    node = mat.node_tree.nodes.active
    if node and node.bl_idname == "ShaderNodeTexPaint":
        return node
    if mat.texture_paint_slots and mat.paint_active_slot < len(mat.texture_paint_slots):
        image = (
            mat.texture_paint_images[mat.paint_active_slot]
            if mat.texture_paint_images and mat.paint_active_slot < len(mat.texture_paint_images)
            else None
        )
        if image is not None:
            for candidate in mat.node_tree.nodes:
                if candidate.bl_idname == "ShaderNodeTexPaint" and candidate.image == image:
                    return candidate
    return _find_any_tex_paint_node(mat.node_tree)


def _is_layer_grunge_gizmo_eligible(node, layer):
    if node is None or layer is None or not layer.use_mask:
        return False
    eff = _get_active_mask_effect(node, layer)
    if eff is None or eff.get("type") != 'GRUNGE' or not eff.get("enabled", True):
        return False
    return eff.get("proj_mode", "TRIPLANAR") in {'TRIPLANAR', 'DECAL'}


def _active_projection_layer_from_context(context):
    """Return (ob, node, layer) when the active mesh has an active editable Decal/Triplanar layer or Grunge Mask."""
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
    if layer.type == 'PAINT' and getattr(layer, "proj_mode", 'NONE') in {'DECAL', 'TRIPLANAR'}:
        return ob, node, layer
    if _is_layer_grunge_gizmo_eligible(node, layer):
        return ob, node, layer
    return None, None, None


def _effective_proj_mode(node, layer):
    if layer is not None and layer.type == 'PAINT' and getattr(layer, "proj_mode", 'NONE') in {'DECAL', 'TRIPLANAR'}:
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
        return layer is not None

    def execute(self, context):
        ob, node, layer = _active_projection_layer_from_context(context)
        if layer is None:
            return {'PASS_THROUGH'}

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

        # In the 3D Viewport window:
        widget_visible = bool(getattr(layer, "show_projection_widget", False))
        if not widget_visible:
            # In Texture Paint mode, pressing G or R on a 3D projection layer auto-shows the widget and starts moving/rotating
            if getattr(context, "mode", "") == 'PAINT_TEXTURE' and self.mode in {'TRANSLATE', 'ROTATE'}:
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
    if eff_mode == 'DECAL':
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
        gpu.state.line_width_set(2.2)
        shader.bind()
        shader.uniform_float("color", (0.2, 0.92, 1.0, 0.95))
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
        shader.uniform_float("color", (0.2, 0.92, 1.0, 0.32))
        batch_for_shader(shader, 'LINES', {"pos": depth_lines}).draw(shader)

    elif eff_mode == 'TRIPLANAR':
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
# Substance-Style Multi-Layer Mask Stack & Grunge Operators
# ---------------------------------------------------------------------------

def _configure_paint_mask_brush(context, value=1.0):
    val = float(value)
    col = (val, val, val)
    ts = getattr(context, "tool_settings", None)
    if ts is not None:
        ups = getattr(ts, "unified_paint_settings", None)
        if ups is not None:
            ups.color = col
        ip = getattr(ts, "image_paint", None)
        if ip is not None and ip.brush is not None:
            ip.brush.color = col
            ip.brush.blend = 'MIX'


class NODE_OT_tex_paint_mask_effect_add(Operator):
    """Add a new Mask Sub-Layer (Procedural Smart Mask, Hand-Painted Mask, Grunge Fill, or Levels) to the active layer's Mask Stack"""
    bl_idname = "node.tex_paint_mask_effect_add"
    bl_label = "Add Mask Sub-Layer"
    bl_options = {'REGISTER', 'UNDO'}

    effect_type: EnumProperty(
        name="Effect Type",
        items=(
            ('GENERATOR', "Procedural (Smart Mask)", "Add a 3D geometry generator (Edge Wear, Curvature, Cavity, AO, Top Dust, Height)", 'MODIFIER', 0),
            ('PAINT', "Paint Mask", "Add a non-destructive hand-painted mask layer (paint White to Reveal, Black to Hide)", 'BRUSH_DATA', 1),
            ('GRUNGE', "Grunge Fill", "Add a Substance Grunge / Noise / Pattern fill mask layer", 'TEXTURE', 2),
            ('LEVELS', "Levels & Contrast", "Add a non-destructive Balance / Contrast / Invert filter over the mask stack below", 'SEQ_LUMA_WAVEFORM', 3),
        ),
        default='GRUNGE',
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
    """Import an image file from disk and apply it as a custom Grunge Mask sub-layer"""
    bl_idname = "node.tex_paint_grunge_open_image"
    bl_label = "Open Custom Grunge Image"
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
        _tag_view3d_gizmo_refresh(context)
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Layer Stack UIList & Substance-Painter-Style Multi-Layer Mask Stack UI
# ---------------------------------------------------------------------------

class TEXPAINT_UL_layers(UIList):
    """Layer list, drawn from top to bottom with inline color swatches and mask badges."""

    def draw_item(self, _context, layout, data, item, _icon, _active_data, _active_propname, index):
        layer = item
        node = data
        is_active = (getattr(node, "active_layer_index", -1) == index)
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

        if layer.type == 'FILL':
            swatch = row.row(align=True)
            swatch.scale_x = 0.42
            swatch.prop(layer, "fill_color", text="")
            row.prop(layer, "name", text="", emboss=False)
        else:
            row.prop(layer, "name", text="", emboss=False, icon=type_icon)

        if (
            (layer.type == 'PAINT' and getattr(layer, "proj_mode", 'NONE') in {'DECAL', 'TRIPLANAR'}) or
            _is_layer_grunge_gizmo_eligible(node, layer)
        ):
            row.prop(layer, "show_projection_widget", text="", emboss=False, icon='GIZMO')

        if layer.type == 'PAINT' and layer.lock_alpha:
            row.label(text="", icon='IMAGE_ALPHA')

        if layer.use_mask:
            st = _get_layer_grunge_state(node, layer)
            effects = st.get("effects", [])
            if len(effects) > 1:
                mask_icon = 'RENDERLAYERS'
            elif len(effects) == 1:
                et = effects[0].get("type", "GRUNGE")
                mask_icon = {'GENERATOR': 'MODIFIER', 'PAINT': 'BRUSH_DATA', 'GRUNGE': 'TEXTURE', 'LEVELS': 'SEQ_LUMA_WAVEFORM'}.get(et, 'MOD_MASK')
            elif is_active and getattr(node, "paint_target", 'COLOR') == 'MASK':
                mask_icon = 'BRUSH_DATA'
            else:
                mask_icon = 'MOD_MASK'
            row.prop(layer, "use_mask", text="", emboss=False, icon=mask_icon)

        row.prop(layer, "lock", text="", emboss=False)
        row.prop(layer, "hide", text="", emboss=False)

    def filter_items(self, _context, data, propname):
        layers = getattr(data, propname)
        flt_flags = [self.bitflag_filter_item] * len(layers)
        flt_neworder = list(reversed(range(len(layers))))
        return flt_flags, flt_neworder


def _draw_smart_mask_generator_buttons(layout, active_gen_type=""):
    """Draw compact 2x3 Substance-Painter-style Smart Mask preset buttons."""
    grid = layout.grid_flow(row_major=True, columns=3, even_columns=True, even_rows=True, align=True)
    presets = (
        ('EDGE_WEAR', "Edge Wear", 'MOD_BEVEL'),
        ('CURVATURE', "Curvature", 'SMOOTHCURVE'),
        ('CAVITY', "Cavity", 'SHARPCURVE'),
        ('AO', "AO", 'SHADING_RENDERED'),
        ('NORMAL_UP', "Top Dust", 'NORMALS_FACE'),
        ('POSITION_Z', "Height", 'EMPTY_SINGLE_ARROW'),
    )
    for gen_type, label, icon in presets:
        op = grid.operator(
            "node.tex_paint_mask_gen_preset",
            text=label,
            icon=icon,
            depress=(active_gen_type == gen_type),
        )
        op.gen_type = gen_type


def _draw_mask_effect_stack(layout, node, layer):
    """Draw the Substance-Painter-style Multi-Layer Mask Stack (Procedural + Paint + Grunge + Levels)."""
    st = _get_layer_grunge_state(node, layer)
    effects = st.get("effects", [])
    act_uid = st.get("active_effect_uid", 0)

    # 1. Add Mask Sub-Layer Bar: [+ Procedural] [+ Paint] [+ Grunge Fill] [+ Levels]
    add_bar = layout.row(align=True)
    op_gen = add_bar.operator("node.tex_paint_mask_effect_add", text="+ Procedural", icon='MODIFIER')
    op_gen.effect_type = 'GENERATOR'
    op_gen.gen_type = 'EDGE_WEAR'

    op_pnt = add_bar.operator("node.tex_paint_mask_effect_add", text="+ Paint", icon='BRUSH_DATA')
    op_pnt.effect_type = 'PAINT'

    op_grn = add_bar.operator("node.tex_paint_mask_effect_add", text="+ Grunge", icon='TEXTURE')
    op_grn.effect_type = 'GRUNGE'

    op_lvl = add_bar.operator("node.tex_paint_mask_effect_add", text="+ Levels", icon='SEQ_LUMA_WAVEFORM')
    op_lvl.effect_type = 'LEVELS'

    if not effects:
        info_row = layout.row(align=True)
        info_row.label(text="Stack Procedural, Paint & Grunge mask layers above", icon='INFO')
        return

    layout.separator(factor=0.35)

    # 2. Mask Effect Stack List (Drawn top-to-bottom like Substance Painter)
    stack_row = layout.row(align=False)
    list_box = stack_row.box()
    list_col = list_box.column(align=True)

    type_icons = {
        'GENERATOR': 'MODIFIER',
        'PAINT': 'BRUSH_DATA',
        'GRUNGE': 'TEXTURE',
        'LEVELS': 'SEQ_LUMA_WAVEFORM',
    }

    for eff in reversed(effects):
        e_uid = int(eff["uid"])
        is_sel = (e_uid == act_uid)
        enabled = bool(eff.get("enabled", True))
        etype = eff.get("type", "GRUNGE")
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

    # Side Column: Up / Down / Duplicate
    side_col = stack_row.column(align=True)
    op_up = side_col.operator("node.tex_paint_mask_effect_move", text="", icon='TRIA_UP')
    op_up.direction = 'UP'
    op_dn = side_col.operator("node.tex_paint_mask_effect_move", text="", icon='TRIA_DOWN')
    op_dn.direction = 'DOWN'
    side_col.separator()
    side_col.operator("node.tex_paint_mask_effect_duplicate", text="", icon='DUPLICATE')

    # 3. Contextual Inspector for the Active Mask Sub-Layer
    act_eff = _get_active_mask_effect(node, layer)
    if act_eff is None:
        return

    layout.separator(factor=0.5)
    etype = act_eff.get("type", "GRUNGE")

    # Shared Sub-Layer Blend Mode & Opacity Header
    common_col = layout.column(align=True)
    common_col.use_property_split = True
    common_col.use_property_decorate = False
    common_col.prop(node, "tex_paint_grunge_blend_mode", text="Combine")
    common_col.prop(node, "tex_paint_mask_eff_opacity", text="Opacity", slider=True)

    layout.separator(factor=0.4)

    if etype == 'GRUNGE':
        cat = act_eff.get("category", "BITMAP_GRUNGES")
        cat_row = layout.row(align=True)
        cat_row.prop(node, "tex_paint_grunge_category", text="")
        cat_row.operator("node.tex_paint_grunge_open_image", text="", icon='FILE_FOLDER')

        if cat != 'CUSTOM':
            layout.template_icon_view(
                node,
                "tex_paint_grunge_texture",
                show_labels=True,
                scale=3.6,
                scale_popup=4.8,
            )
            nav_row = layout.row(align=True)
            op_prev = nav_row.operator("node.tex_paint_grunge_step", text="", icon='TRIA_LEFT')
            op_prev.step = -1
            nav_row.prop(node, "tex_paint_grunge_texture", text="")
            op_next = nav_row.operator("node.tex_paint_grunge_step", text="", icon='TRIA_RIGHT')
            op_next.step = 1
            op_rnd = nav_row.operator("node.tex_paint_grunge_randomize", text="", icon='FILE_REFRESH')
            op_rnd.random_map = True
        else:
            cust_row = layout.row(align=True)
            cust_row.prop_search(node, "tex_paint_grunge_custom_image", bpy.data, "images", text="Image")

        layout.separator(factor=0.4)
        g_col = layout.column(align=True)
        g_col.use_property_split = True
        g_col.use_property_decorate = False
        g_col.prop(node, "tex_paint_grunge_proj_mode", text="Projection")
        g_col.prop(node, "tex_paint_grunge_scale", text="Scale / Tile", slider=True)
        g_col.prop(node, "tex_paint_grunge_balance", text="Balance", slider=True)
        g_col.prop(node, "tex_paint_grunge_contrast", text="Contrast", slider=True)
        g_col.prop(node, "tex_paint_grunge_rotation", text="Rotation", slider=True)
        if act_eff.get("proj_mode", "TRIPLANAR") in {'TRIPLANAR', 'DECAL'}:
            lbl = "Box Blend" if act_eff.get("proj_mode", "TRIPLANAR") == 'TRIPLANAR' else "Normal Falloff"
            g_col.prop(node, "tex_paint_grunge_box_blend", text=lbl, slider=True)

        layout.separator(factor=0.35)
        bot_row = layout.row(align=True)
        bot_row.prop(node, "tex_paint_grunge_invert", text="Invert Grunge", toggle=True, icon='ARROW_LEFTRIGHT')
        op_seed = bot_row.operator("node.tex_paint_grunge_randomize", text="Random Seed", icon='RNDCURVE')
        op_seed.random_map = False
        if act_eff.get("proj_mode", "TRIPLANAR") in {'TRIPLANAR', 'DECAL'}:
            bot_row.prop(layer, "show_projection_widget", text="3D Gizmo", toggle=True, icon='GIZMO')

    elif etype == 'GENERATOR':
        _draw_smart_mask_generator_buttons(layout, active_gen_type=act_eff.get("gen_type", "EDGE_WEAR"))
        layout.separator(factor=0.4)
        gen_col = layout.column(align=True)
        gen_col.use_property_split = True
        gen_col.use_property_decorate = False
        gen_col.prop(node, "tex_paint_mask_eff_gen_balance", text="Balance", slider=True)
        gen_col.prop(node, "tex_paint_mask_eff_gen_contrast", text="Contrast", slider=True)
        gen_col.prop(node, "tex_paint_mask_eff_gen_radius", text="Radius", slider=True)
        gen_col.prop(node, "tex_paint_mask_eff_gen_noise", text="3D Noise", slider=True)
        if float(act_eff.get("gen_noise", 0.0)) > 0.001:
            gen_col.prop(node, "tex_paint_mask_eff_gen_noise_scale", text="Noise Scale", slider=True)

        layout.separator(factor=0.35)
        bot_row = layout.row(align=True)
        bot_row.prop(node, "tex_paint_grunge_invert", text="Invert Generator", toggle=True, icon='ARROW_LEFTRIGHT')

    elif etype == 'PAINT':
        brush_row = layout.row(align=True)
        op_rev = brush_row.operator(
            "node.tex_paint_mask_brush_color",
            text="Paint White (Reveal)",
            icon='LIGHT_SUN',
        )
        op_rev.value = 1.0
        op_hid = brush_row.operator(
            "node.tex_paint_mask_brush_color",
            text="Paint Black (Hide)",
            icon='SHADING_SOLID',
        )
        op_hid.value = 0.0

        layout.separator(factor=0.3)
        act_row = layout.row(align=True)
        op_clr = act_row.operator("node.tex_paint_mask_paint_action", text="Clear Strokes", icon='LOOP_BACK')
        op_clr.action = 'CLEAR'
        op_fw = act_row.operator("node.tex_paint_mask_paint_action", text="White", icon='LIGHT_SUN')
        op_fw.action = 'FILL_WHITE'
        op_fb = act_row.operator("node.tex_paint_mask_paint_action", text="Black", icon='SHADING_SOLID')
        op_fb.action = 'FILL_BLACK'
        op_inv = act_row.operator("node.tex_paint_mask_paint_action", text="Invert", icon='ARROW_LEFTRIGHT')
        op_inv.action = 'INVERT_STROKES'

    elif etype == 'LEVELS':
        lvl_col = layout.column(align=True)
        lvl_col.use_property_split = True
        lvl_col.use_property_decorate = False
        lvl_col.prop(node, "tex_paint_mask_eff_lvl_balance", text="Balance", slider=True)
        lvl_col.prop(node, "tex_paint_mask_eff_lvl_contrast", text="Contrast", slider=True)
        layout.separator(factor=0.3)
        layout.prop(node, "tex_paint_grunge_invert", text="Invert Levels", toggle=True, icon='ARROW_LEFTRIGHT')


def draw_tex_paint_layers(layout, context, node, compact=False):
    ob = getattr(context, "active_object", None)
    mat = ob.active_material if ob else None

    if not compact and context.space_data and context.space_data.type == 'VIEW_3D':
        if ob and len(ob.material_slots) > 1:
            layout.template_list(
                "MATERIAL_UL_matslots", "tex_paint_mats",
                ob, "material_slots",
                ob, "active_material_index", rows=2,
            )
        if mat and len(mat.texture_paint_slots) > 1:
            layout.template_list(
                "TEXTURE_UL_texpaintslots", "tex_paint_slots",
                mat, "texture_paint_slots",
                mat, "paint_active_slot", rows=2,
            )

    if node is not None:
        layout.context_pointer_set("node", node)

    if node is None or node.image is None:
        col = layout.column(align=True)
        col.operator("node.tex_paint_canvas_new", text="New Layered Canvas", icon='ADD')
        return

    # 1. Layer & Projection Creation Toolbar
    top_col = layout.column(align=True)
    add_row = top_col.row(align=True)
    op_paint = add_row.operator("node.tex_paint_layer_add", text="Paint", icon='BRUSH_DATA')
    op_paint.type = 'PAINT'
    op_fill = add_row.operator("node.tex_paint_layer_add", text="Fill", icon='COLOR')
    op_fill.type = 'FILL'
    op_adj = add_row.operator("node.tex_paint_layer_add", text="Adjust", icon='MODIFIER')
    op_adj.type = 'ADJUSTMENT'

    imp_row = top_col.row(align=True)
    imp_row.operator("node.tex_paint_layer_import_image", text="Import", icon='FILE_IMAGE')
    op_decal = imp_row.operator("node.tex_paint_layer_project_image", text="3D Decal", icon='MOD_UVPROJECT')
    op_decal.projection_mode = 'DECAL'
    op_decal.new_layer = True
    op_decal.clear_layer = True
    op_repeat = imp_row.operator("node.tex_paint_layer_project_image", text="Tile 3D", icon='MESH_CUBE')
    op_repeat.projection_mode = 'TRIPLANAR'
    op_repeat.new_layer = True
    op_repeat.clear_layer = True

    # 2. Layer Stack List
    row = layout.row()
    row.template_list(
        "TEXPAINT_UL_layers", "",
        node, "layers",
        node, "active_layer_index",
        rows=3 if compact else 5,
        sort_lock=True,
    )

    col = row.column(align=True)
    col.operator_menu_enum("node.tex_paint_layer_add", "type", icon='ADD', text="")
    col.operator("node.tex_paint_layer_import_image", icon='FILE_IMAGE', text="")
    col.operator("node.tex_paint_layer_remove", icon='REMOVE', text="")
    col.separator()
    col.operator("node.tex_paint_layer_duplicate", icon='DUPLICATE', text="")
    col.operator("node.tex_paint_layer_merge_down", icon='TRIA_DOWN_BAR', text="")
    col.separator()
    col.operator("node.tex_paint_layer_move", icon='TRIA_UP', text="").direction = 'UP'
    col.operator("node.tex_paint_layer_move", icon='TRIA_DOWN', text="").direction = 'DOWN'

    layer = node.active_layer
    if layer is None:
        return

    # 3. Active Layer Properties & Compact Flags
    layer_box = layout.box()
    layer_col = layer_box.column(align=True)

    flag_row = layer_col.row(align=True)
    if layer.type == 'PAINT':
        flag_row.prop(layer, "lock_alpha", text="Alpha", toggle=True, icon='IMAGE_ALPHA')
    flag_row.prop(layer, "use_clipping", text="Clip", toggle=True, icon='TRIA_DOWN_BAR')
    flag_row.prop(layer, "lock", text="Lock", toggle=True, icon='LOCKED')
    flag_row.prop(layer, "use_mask", text="Mask", toggle=True, icon='MOD_MASK')

    layer_col.separator(factor=0.4)
    prop_col = layer_col.column(align=True)
    prop_col.use_property_split = True
    prop_col.use_property_decorate = False
    if layer.type == 'FILL':
        prop_col.prop(layer, "fill_color", text="Fill Color")
    prop_col.prop(layer, "blend_mode", text="Blend")
    prop_col.prop(layer, "opacity", text="Opacity", slider=True)

    if layer.type == 'ADJUSTMENT':
        prop_col.separator(factor=0.4)
        prop_col.prop(layer, "adjust_hsv", index=0, text="Hue", slider=True)
        prop_col.prop(layer, "adjust_hsv", index=1, text="Saturation", slider=True)
        prop_col.prop(layer, "adjust_hsv", index=2, text="Value", slider=True)
        prop_col.separator(factor=0.3)
        prop_col.prop(layer, "adjust_bright_contrast", index=0, text="Brightness", slider=True)
        prop_col.prop(layer, "adjust_bright_contrast", index=1, text="Contrast", slider=True)

    # 4. Substance-Style Multi-Layer Mask Studio
    mask_box = layout.box()
    mask_col = mask_box.column(align=True)

    if not layer.use_mask:
        hdr = mask_col.row(align=True)
        hdr.label(text="Layer Mask Stack", icon='MOD_MASK')

        init_row = mask_col.row(align=True)
        op_w = init_row.operator("node.tex_paint_mask_stack_init", text="White (Reveal)", icon='LIGHT_SUN')
        op_w.mode = 'WHITE'
        op_b = init_row.operator("node.tex_paint_mask_stack_init", text="Black (Hide)", icon='SHADING_SOLID')
        op_b.mode = 'BLACK'

        proc_row = mask_col.row(align=True)
        op_sm = proc_row.operator("node.tex_paint_mask_effect_add", text="+ Procedural", icon='MODIFIER')
        op_sm.effect_type = 'GENERATOR'
        op_sm.gen_type = 'EDGE_WEAR'
        op_pn = proc_row.operator("node.tex_paint_mask_effect_add", text="+ Paint", icon='BRUSH_DATA')
        op_pn.effect_type = 'PAINT'
        op_gr = proc_row.operator("node.tex_paint_mask_effect_add", text="+ Grunge", icon='TEXTURE')
        op_gr.effect_type = 'GRUNGE'
    else:
        hdr_row = mask_col.row(align=True)
        if layer.type == 'PAINT':
            tgt_sub = hdr_row.row(align=True)
            tgt_sub.prop(node, "paint_target", expand=True)
        else:
            hdr_row.label(text="Layer Mask Stack", icon='MOD_MASK')

        act_sub = hdr_row.row(align=True)
        act_sub.prop(layer, "invert_mask", text="Invert", toggle=True, icon='ARROW_LEFTRIGHT')
        op_fw = act_sub.operator("node.tex_paint_mask_stack_init", text="", icon='LIGHT_SUN')
        op_fw.mode = 'WHITE'
        op_fb = act_sub.operator("node.tex_paint_mask_stack_init", text="", icon='SHADING_SOLID')
        op_fb.mode = 'BLACK'
        op_rem = act_sub.operator("node.tex_paint_mask_stack_init", text="", icon='X')
        op_rem.mode = 'REMOVE'

        mask_col.separator(factor=0.45)
        _draw_mask_effect_stack(mask_col, node, layer)

    # 5. 3D Projection Widget controls for Decal / Triplanar Paint or Grunge Mask layers
    show_proj_panel = (
        (layer.type == 'PAINT' and hasattr(layer, "proj_mode") and layer.proj_mode in {'DECAL', 'TRIPLANAR'}) or
        (_is_layer_grunge_gizmo_eligible(node, layer) and getattr(layer, "show_projection_widget", False))
    )
    if show_proj_panel:
        proj_box = layout.box()
        proj_col = proj_box.column(align=True)

        hdr = proj_col.row(align=True)
        if layer.type == 'PAINT' and getattr(layer, "proj_mode", 'NONE') in {'DECAL', 'TRIPLANAR'}:
            hdr.prop(layer, "proj_mode", text="")
        else:
            hdr.label(text="3D Grunge Projection Gizmo", icon='GIZMO')
        hdr.prop(layer, "show_projection_widget", text="3D Widget", toggle=True, icon='GIZMO')

        if context.window_manager and hasattr(context.window_manager, "tex_paint_gizmo_mode"):
            mode_row = proj_col.row(align=True)
            mode_row.prop(context.window_manager, "tex_paint_gizmo_mode", expand=True)

        act_row = proj_col.row(align=True)
        act_row.operator("node.tex_paint_layer_snap_decal", text="Place on Surface", icon='SNAP_FACE_NEAREST')
        act_row.operator("node.tex_paint_layer_align_view", text="Align View", icon='RESTRICT_VIEW_OFF')
        act_row.operator("node.tex_paint_layer_bake_projection", text="Hide Gizmo", icon='CHECKMARK')

        if layer.type == 'PAINT' and getattr(layer, "proj_mode", 'NONE') in {'DECAL', 'TRIPLANAR'}:
            p_col = proj_box.column(align=True)
            p_col.use_property_split = True
            p_col.use_property_decorate = False
            p_col.prop(
                layer,
                "proj_blend_falloff",
                text="Normal Falloff" if layer.proj_mode == 'DECAL' else "Box Blend",
                slider=True,
            )
            p_col.prop(layer, "proj_location", text="Location")
            p_col.prop(layer, "proj_rotation", text="Rotation")
            p_col.prop(layer, "proj_scale", text="Scale")

    if not compact:
        layout.label(
            text=iface_("Canvas: {:d} x {:d} {:s}").format(
                node.canvas_width, node.canvas_height, iface_("Float") if node.use_float else iface_("Byte"),
            ),
            icon='IMAGE_RGB_ALPHA',
        )


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


class IMAGE_PT_tex_paint_layers(Panel):
    """Paint Layers panel in the 2D Image Editor N-toolbar (Tool tab) when using a Paint brush."""
    bl_space_type = 'IMAGE_EDITOR'
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
        return UnifiedPaintPanel.get_brush_mode(context) == 'PAINT_2D'

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
# Node Property Callbacks for Active Mask Sub-Layer Controls
# ---------------------------------------------------------------------------

_GRUNGE_PROJ_ITEMS = (
    ('TRIPLANAR', "3D Triplanar Box", "Seamless 3D box projection across the entire mesh without UV seams", 'MESH_CUBE', 0),
    ('REPEAT', "2D UV Repeat", "Tile the grunge map across the 2D UV map", 'TEXTURE', 1),
    ('DECAL', "3D Surface Decal", "Project the grunge map as a 3D directional decal box", 'MOD_UVPROJECT', 2),
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
    return _get_grunge_enum_items_for_category(cat)


def _grunge_texture_get(self):
    cat = _active_eff_val(self, "category", "BITMAP_GRUNGES")
    items = _get_grunge_enum_items_for_category(cat)
    return _enum_idx(items, _active_eff_val(self, "texture", ""), 0)


def _grunge_texture_set(self, value):
    cat = _active_eff_val(self, "category", "BITMAP_GRUNGES")
    items = _get_grunge_enum_items_for_category(cat)
    if 0 <= value < len(items):
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
        _update_active_mask_effect(self, None, updates, ensure_type=ensure_type, flush_json=False)
        return None

    def _on_cat_update(self, context):
        eff = _get_active_mask_effect(self)
        if eff is not None:
            cat = eff.get("category", "BITMAP_GRUNGES")
            if cat != 'CUSTOM':
                items = _get_grunge_enum_items_for_category(cat)
                if items and not eff.get("texture", "").startswith(cat + "::"):
                    _update_active_mask_effect(self, None, {"texture": items[0][0]}, ensure_type='GRUNGE')
        _evaluate_layer_grunge_mask(self, None, context=context, force_reproject=True, interactive=False)
        return None

    Node.tex_paint_grunge_category = EnumProperty(
        name="Grunge Category",
        description="Substance Starter Assets grunge & procedural texture category",
        items=_GRUNGE_CATEGORIES,
        get=lambda self: _enum_idx(_GRUNGE_CATEGORIES, _active_eff_val(self, "category", "BITMAP_GRUNGES"), 0),
        set=lambda self, val: _set_eff(self, {"category": _GRUNGE_CATEGORIES[val][0]}, ensure_type='GRUNGE'),
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
        name="Custom Grunge Image",
        description="Name of the loaded Blender image to use as a custom Grunge Mask",
        get=lambda self: str(_active_eff_val(self, "custom_image", "")),
        set=lambda self, val: _set_eff(self, {"custom_image": str(val)}, ensure_type='GRUNGE'),
        update=_update_reproject,
    )

    Node.tex_paint_grunge_proj_mode = EnumProperty(
        name="Projection",
        description="How the Substance Grunge texture is mapped across the mesh into the mask sub-layer",
        items=_GRUNGE_PROJ_ITEMS,
        get=lambda self: _enum_idx(_GRUNGE_PROJ_ITEMS, _active_eff_val(self, "proj_mode", "TRIPLANAR"), 0),
        set=lambda self, val: _set_eff(self, {"proj_mode": _GRUNGE_PROJ_ITEMS[val][0]}, ensure_type='GRUNGE'),
        update=_update_reproject,
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

    Node.tex_paint_grunge_scale = FloatProperty(
        name="Scale / Tiling",
        description="Number of times the Grunge texture repeats across the model or UV space",
        min=0.05,
        max=64.0,
        soft_min=0.25,
        soft_max=16.0,
        default=2.0,
        get=lambda self: float(_active_eff_val(self, "scale", 2.0)),
        set=lambda self, val: _set_eff(self, {"scale": float(val), "gizmo_override": False}, ensure_type='GRUNGE'),
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
        description="2D / 3D rotation angle of the projected Grunge texture",
        min=-math.pi * 2.0,
        max=math.pi * 2.0,
        soft_min=-math.pi,
        soft_max=math.pi,
        default=0.0,
        subtype='ANGLE',
        get=lambda self: float(_active_eff_val(self, "rotation", 0.0)),
        set=lambda self, val: _set_eff(self, {"rotation": float(val), "gizmo_override": False}, ensure_type='GRUNGE'),
        update=_update_reproject,
    )

    Node.tex_paint_grunge_box_blend = FloatProperty(
        name="Box Blend",
        description="Triplanar box projection blend softness or Decal surface normal falloff",
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

    # Smart Mask Generator Sub-Layer Sliders (Balance & Contrast run via 1.5ms LUT; Radius & Noise run via C++ generator)
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


def _unregister_node_grunge_props():
    Node = bpy.types.Node
    for prop_name in (
        "tex_paint_grunge_category",
        "tex_paint_grunge_texture",
        "tex_paint_grunge_custom_image",
        "tex_paint_grunge_proj_mode",
        "tex_paint_grunge_blend_mode",
        "tex_paint_mask_eff_opacity",
        "tex_paint_grunge_scale",
        "tex_paint_grunge_balance",
        "tex_paint_grunge_contrast",
        "tex_paint_grunge_rotation",
        "tex_paint_grunge_box_blend",
        "tex_paint_grunge_invert",
        "tex_paint_mask_eff_gen_balance",
        "tex_paint_mask_eff_gen_contrast",
        "tex_paint_mask_eff_gen_radius",
        "tex_paint_mask_eff_gen_noise",
        "tex_paint_mask_eff_gen_noise_scale",
        "tex_paint_mask_eff_lvl_balance",
        "tex_paint_mask_eff_lvl_contrast",
    ):
        if hasattr(Node, prop_name):
            delattr(Node, prop_name)


def _mask_paint_sync_timer():
    """Lightweight timer to flush debounced JSON state, refine interactive 2K projections on idle, and absorb brush strokes."""
    if bpy.app.background or _INTERNAL_REPROJECTING:
        return 0.25
    try:
        import time
        import numpy as np

        if not _PROJECTION_KEYMAPS:
            _register_projection_keymaps()

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

        context = bpy.context
        node = tex_paint_node_from_context(context)
        layer = node.active_layer if node else None
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
    NODE_OT_tex_paint_mask_gen_preset,
    NODE_OT_tex_paint_mask_stack_init,
    TEXPAINT_UL_layers,
    VIEW3D_PT_tex_paint_layers,
    IMAGE_PT_tex_paint_layers,
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


def _unregister_projection_keymaps():
    for km, kmi in _PROJECTION_KEYMAPS:
        try:
            km.keymap_items.remove(kmi)
        except Exception:
            pass
    _PROJECTION_KEYMAPS.clear()


def register():
    global _draw_handler_handle
    from bpy.types import WindowManager
    from bpy.utils import register_class

    register_class(VIEW3D_GGT_tex_paint_projection)
    _register_node_grunge_props()
    _register_projection_keymaps()

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

    if _draw_handler_handle is None:
        _draw_handler_handle = bpy.types.SpaceView3D.draw_handler_add(
            _draw_projection_wireframe_3d, (), 'WINDOW', 'POST_VIEW'
        )

    if not bpy.app.background and not bpy.app.timers.is_registered(_mask_paint_sync_timer):
        bpy.app.timers.register(_mask_paint_sync_timer, first_interval=0.25, persistent=True)


def unregister():
    global _draw_handler_handle, _PREVIEW_COLLECTION
    from bpy.types import WindowManager
    from bpy.utils import unregister_class

    _unregister_projection_keymaps()

    if bpy.app.timers.is_registered(_mask_paint_sync_timer):
        try:
            bpy.app.timers.unregister(_mask_paint_sync_timer)
        except Exception:
            pass

    if _draw_handler_handle is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_draw_handler_handle, 'WINDOW')
        _draw_handler_handle = None

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

    if hasattr(WindowManager, "tex_paint_gizmo_mode"):
        del WindowManager.tex_paint_gizmo_mode


if __name__ == "__main__":  # only for live edit.
    from bpy.utils import register_class
    for cls in classes:
        register_class(cls)
    register()
