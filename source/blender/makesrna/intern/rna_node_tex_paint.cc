/* SPDX-FileCopyrightText: 2026 Blender Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

/** \file
 * \ingroup RNA
 *
 * RNA of the layered Texture Paint shader node and its #TexPaintLayer items.
 *
 * Layer edits composite directly into the master image and only request redraws. They never tag
 * the node tree, so materials are not re-evaluated or recompiled.
 */

#include "DNA_node_tex_paint_types.h"
#include "DNA_node_types.h"

#include "BLT_translation.hh"

#include "RNA_define.hh"
#include "RNA_enum_types.hh"
#include "RNA_types.hh"

#include "rna_internal.hh"

#include "WM_types.hh"

#ifdef RNA_RUNTIME

#  include <fmt/format.h>

#  include "DNA_material_types.h"
#  include "DNA_mesh_types.h"
#  include "DNA_object_types.h"

#  include "BLI_listbase.hh"
#  include "BLI_string.hh"
#  include "BLI_string_utils.hh"

#  include "BKE_main.hh"
#  include "BKE_material.hh"
#  include "BKE_node_legacy_types.hh"
#  include "BKE_tex_paint_layer.hh"

#  include "DEG_depsgraph.hh"

#  include "RNA_access.hh"
#  include "RNA_prototypes.hh"

#  include "WM_api.hh"

namespace blender {

/** Request redraws of the master image users without touching the node tree. */
static void tex_paint_tag_redraw(bNode &node)
{
  if (node.id) {
    DEG_id_tag_update(node.id, ID_RECALC_EDITORS);
    WM_main_add_notifier(NC_IMAGE | NA_EDITED, node.id);
  }
  WM_main_add_notifier(NC_NODE | NA_EDITED, nullptr);
  WM_main_add_notifier(NC_SCENE | ND_OB_ACTIVE, nullptr);
}

static bool rna_TexPaintLayer_has_mask_get(PointerRNA *ptr)
{
  const TexPaintLayer *layer = ptr->data_as<TexPaintLayer>();
  return layer->mask_ibuf != nullptr || layer->mask_pf != nullptr;
}

void rna_ShaderNodeTexPaint_active_layer_index_range(
    PointerRNA *ptr, int *min, int *max, int * /*softmin*/, int * /*softmax*/)
{
  const bNode *node = ptr->data_as<bNode>();
  const NodeTexPaint *data = static_cast<const NodeTexPaint *>(node->storage);
  *min = 0;
  *max = std::max(BKE_tex_paint_layer_count(*data) - 1, 0);
}

void rna_ShaderNodeTexPaint_active_layer_index_set(PointerRNA *ptr, int value)
{
  bNode *node = ptr->data_as<bNode>();
  BKE_tex_paint_layer_active_set(*static_cast<NodeTexPaint *>(node->storage), value);
}

PointerRNA rna_ShaderNodeTexPaint_active_layer_get(PointerRNA *ptr)
{
  bNode *node = ptr->data_as<bNode>();
  TexPaintLayer *layer = BKE_tex_paint_layer_active(*static_cast<NodeTexPaint *>(node->storage));
  return RNA_pointer_create_with_parent(*ptr, RNA_TexPaintLayer, layer);
}

void rna_ShaderNodeTexPaint_active_layer_set(PointerRNA *ptr,
                                             PointerRNA value,
                                             ReportList * /*reports*/)
{
  bNode *node = ptr->data_as<bNode>();
  NodeTexPaint *data = static_cast<NodeTexPaint *>(node->storage);
  const int index = BLI_findindex(&data->layers, value.data);
  if (index != -1) {
    BKE_tex_paint_layer_active_set(*data, index);
  }
}

static bNode *rna_TexPaintLayer_node(const PointerRNA *ptr)
{
  bNodeTree *ntree = reinterpret_cast<bNodeTree *>(ptr->owner_id);
  if (ntree == nullptr) {
    return nullptr;
  }
  return BKE_tex_paint_node_find_by_layer(*ntree, ptr->data_as<TexPaintLayer>());
}

static std::optional<std::string> rna_TexPaintLayer_path(const PointerRNA *ptr)
{
  const bNode *node = rna_TexPaintLayer_node(ptr);
  if (node == nullptr) {
    return std::nullopt;
  }
  const TexPaintLayer *layer = ptr->data_as<TexPaintLayer>();
  char node_name_esc[sizeof(node->name) * 2];
  char layer_name_esc[sizeof(layer->name) * 2];
  BLI_str_escape(node_name_esc, node->name, sizeof(node_name_esc));
  BLI_str_escape(layer_name_esc, layer->name, sizeof(layer_name_esc));
  return fmt::format("nodes[\"{}\"].layers[\"{}\"]", node_name_esc, layer_name_esc);
}

static void rna_TexPaintLayer_name_set(PointerRNA *ptr, const char *value)
{
  TexPaintLayer *layer = ptr->data_as<TexPaintLayer>();
  STRNCPY_UTF8(layer->name, value);
  if (bNode *node = rna_TexPaintLayer_node(ptr)) {
    BKE_tex_paint_layer_unique_name(*static_cast<NodeTexPaint *>(node->storage), *layer);
  }
}

/** Composite after a layer property change, reusing the below-active cache when possible. */
static void rna_TexPaintLayer_update(Main * /*bmain*/, Scene * /*scene*/, PointerRNA *ptr)
{
  bNode *node = rna_TexPaintLayer_node(ptr);
  if (node == nullptr) {
    return;
  }
  NodeTexPaint *data = static_cast<NodeTexPaint *>(node->storage);
  const int index = BLI_findindex(&data->layers, ptr->data);
  if (index < data->active_layer_index) {
    BKE_tex_paint_cache_invalidate(*data);
  }
  BKE_tex_paint_composite_rect(*node, nullptr);
  tex_paint_tag_redraw(*node);
}

static void rna_TexPaintLayer_redraw_update(Main * /*bmain*/, Scene * /*scene*/, PointerRNA *ptr)
{
  if (bNode *node = rna_TexPaintLayer_node(ptr)) {
    tex_paint_tag_redraw(*node);
  }
}

static const Mesh *find_mesh_for_ntree(Main *bmain, const bNodeTree *ntree)
{
  if (bmain == nullptr) {
    return nullptr;
  }
  const Mesh *fallback = nullptr;
  for (Object &ob : bmain->objects) {
    if (ob.type != OB_MESH || ob.data == nullptr) {
      continue;
    }
    const Mesh *mesh = id_cast<const Mesh *>(ob.data);
    if (fallback == nullptr) {
      fallback = mesh;
    }
    for (int i = 1; i <= ob.totcol; i++) {
      if (Material *ma = BKE_object_material_get(&ob, i)) {
        if (ma->nodetree == ntree) {
          return mesh;
        }
      }
    }
  }
  return fallback;
}

static void rna_TexPaintLayer_projection_update(Main *bmain, Scene * /*scene*/, PointerRNA *ptr)
{
  bNode *node = rna_TexPaintLayer_node(ptr);
  if (node == nullptr) {
    return;
  }
  NodeTexPaint *data = static_cast<NodeTexPaint *>(node->storage);
  TexPaintLayer *layer = ptr->data_as<TexPaintLayer>();
  const bNodeTree *ntree = reinterpret_cast<const bNodeTree *>(ptr->owner_id);
  rcti dirty_rect = {0, 0, 0, 0};
  const rcti *dirty_rect_ptr = nullptr;
  if (bmain != nullptr && layer != nullptr) {
    const Mesh *mesh = find_mesh_for_ntree(bmain, ntree);
    if (BKE_tex_paint_layer_reproject(*bmain, mesh, *data, *layer, &dirty_rect)) {
      dirty_rect_ptr = &dirty_rect;
    }
  }
  const int index = BLI_findindex(&data->layers, ptr->data);
  if (index < data->active_layer_index) {
    BKE_tex_paint_cache_invalidate(*data);
  }
  BKE_tex_paint_composite_rect(*node, dirty_rect_ptr);
  tex_paint_tag_redraw(*node);
}

static void rna_TexPaintLayer_use_mask_update(Main * /*bmain*/, Scene * /*scene*/, PointerRNA *ptr)
{
  bNode *node = rna_TexPaintLayer_node(ptr);
  if (node == nullptr) {
    return;
  }
  NodeTexPaint *data = static_cast<NodeTexPaint *>(node->storage);
  TexPaintLayer *layer = ptr->data_as<TexPaintLayer>();
  if (layer != nullptr) {
    if (layer->flag & TEXPAINT_LAYER_USE_MASK) {
      BKE_tex_paint_layer_ensure_mask_ibuf(*data, *layer);
      if (BKE_tex_paint_layer_active(*data) == layer) {
        data->paint_target = TEXPAINT_TARGET_MASK;
      }
    }
    else if (BKE_tex_paint_layer_active(*data) == layer) {
      data->paint_target = TEXPAINT_TARGET_COLOR;
    }
  }
  const int index = BLI_findindex(&data->layers, ptr->data);
  if (index < data->active_layer_index) {
    BKE_tex_paint_cache_invalidate(*data);
  }
  BKE_tex_paint_composite_rect(*node, nullptr);
  tex_paint_tag_redraw(*node);
}

static void rna_TexPaintLayer_mask_gen_update(Main *bmain, Scene * /*scene*/, PointerRNA *ptr)
{
  bNode *node = rna_TexPaintLayer_node(ptr);
  if (node == nullptr) {
    return;
  }
  NodeTexPaint *data = static_cast<NodeTexPaint *>(node->storage);
  TexPaintLayer *layer = ptr->data_as<TexPaintLayer>();
  const bNodeTree *ntree = reinterpret_cast<const bNodeTree *>(ptr->owner_id);
  if (bmain != nullptr && layer != nullptr && layer->mask_gen_type != TEXPAINT_MASK_GEN_NONE) {
    const Mesh *mesh = find_mesh_for_ntree(bmain, ntree);
    BKE_tex_paint_layer_generate_mask(mesh, *data, *layer);
    if (BKE_tex_paint_layer_active(*data) == layer) {
      data->paint_target = TEXPAINT_TARGET_MASK;
    }
  }
  const int index = BLI_findindex(&data->layers, ptr->data);
  if (index < data->active_layer_index) {
    BKE_tex_paint_cache_invalidate(*data);
  }
  BKE_tex_paint_composite_rect(*node, nullptr);
  tex_paint_tag_redraw(*node);
}

}  // namespace blender

#else

namespace blender {

static const EnumPropertyItem tex_paint_layer_type_items[] = {
    {TEXPAINT_LAYER_PAINT, "PAINT", ICON_BRUSH_DATA, "Paint", "Layer with painted pixels"},
    {TEXPAINT_LAYER_FILL, "FILL", ICON_IMAGE_RGB, "Fill", "Layer filled with a constant color"},
    {TEXPAINT_LAYER_ADJUSTMENT,
     "ADJUSTMENT",
     ICON_MODIFIER,
     "Adjustment",
     "Layer adjusting the colors of the layers below it"},
    {0, nullptr, 0, nullptr, nullptr},
};

static const EnumPropertyItem tex_paint_layer_proj_mode_items[] = {
    {0, "NONE", ICON_BRUSH_DATA, "Hand Painted", "Standard hand-painted layer"},
    {1, "FIT", ICON_IMAGE_PLANE, "UV Fit / Stamp", "2D UV fit or stamp"},
    {2, "UV_REPEAT", ICON_TEXTURE, "UV Tile Repeat", "2D UV tile repeat"},
    {3,
     "TRIPLANAR",
     ICON_MESH_CUBE,
     "3D Triplanar Box",
     "3D triplanar box projection with interactive 3D viewport widget"},
    {4,
     "DECAL",
     ICON_MOD_UVPROJECT,
     "3D Planar Decal",
     "3D surface decal projection with interactive 3D viewport widget"},
    {0, nullptr, 0, nullptr, nullptr},
};

static const EnumPropertyItem tex_paint_mask_gen_type_items[] = {
    {TEXPAINT_MASK_GEN_NONE,
     "NONE",
     ICON_BRUSH_DATA,
     "Hand Painted",
     "Manual brush-painted layer mask"},
    {TEXPAINT_MASK_GEN_EDGE_WEAR,
     "EDGE_WEAR",
     ICON_MOD_BEVEL,
     "Edge Wear (Convex)",
     "Substance-style convex edge wear & grunge mask"},
    {TEXPAINT_MASK_GEN_CURVATURE,
     "CURVATURE",
     ICON_SMOOTHCURVE,
     "Curvature (Relief)",
     "Mesh surface curvature map (concave to convex)"},
    {TEXPAINT_MASK_GEN_CAVITY,
     "CAVITY",
     ICON_LINCURVE,
     "Cavity / Creases",
     "Concave crevices, seams, and recessed details"},
    {TEXPAINT_MASK_GEN_AO,
     "AO",
     ICON_SHADING_RENDERED,
     "Ambient Occlusion (AO)",
     "3D hemisphere ambient occlusion across the mesh"},
    {TEXPAINT_MASK_GEN_NORMAL_UP,
     "NORMAL_UP",
     ICON_NORMALS_FACE,
     "Top Facing (Dust / Snow)",
     "Upward-facing surface normal mask"},
    {TEXPAINT_MASK_GEN_POSITION_Z,
     "POSITION_Z",
     ICON_AXIS_TOP,
     "Height Gradient (Z)",
     "Bottom-to-top vertical position gradient"},
    {0, nullptr, 0, nullptr, nullptr},
};

static const EnumPropertyItem tex_paint_mask_gen_blend_items[] = {
    {TEXPAINT_MASK_BLEND_REPLACE, "REPLACE", 0, "Replace", "Replace the mask with the generator"},
    {TEXPAINT_MASK_BLEND_ADD, "ADD", 0, "Add", "Add the generator onto the painted mask"},
    {TEXPAINT_MASK_BLEND_MULTIPLY,
     "MULTIPLY",
     0,
     "Multiply",
     "Multiply the painted mask by the generator"},
    {TEXPAINT_MASK_BLEND_SUBTRACT,
     "SUBTRACT",
     0,
     "Subtract",
     "Subtract the generator from the painted mask"},
    {0, nullptr, 0, nullptr, nullptr},
};

/** Supported subset of #IMB_BlendMode, values match the enum. */
static const EnumPropertyItem tex_paint_blend_mode_items[] = {
    {0, "MIX", 0, "Mix", ""},
    RNA_ENUM_ITEM_SEPR,
    {5, "DARKEN", 0, "Darken", ""},
    {3, "MUL", 0, "Multiply", ""},
    RNA_ENUM_ITEM_SEPR,
    {4, "LIGHTEN", 0, "Lighten", ""},
    {13, "SCREEN", 0, "Screen", ""},
    {1, "ADD", 0, "Add", ""},
    RNA_ENUM_ITEM_SEPR,
    {8, "OVERLAY", 0, "Overlay", ""},
    {14, "SOFTLIGHT", 0, "Soft Light", ""},
    RNA_ENUM_ITEM_SEPR,
    {18, "DIFFERENCE", 0, "Difference", ""},
    {2, "SUB", 0, "Subtract", ""},
    RNA_ENUM_ITEM_SEPR,
    {20, "HUE", 0, "Hue", ""},
    {21, "SATURATION", 0, "Saturation", ""},
    {23, "COLOR", 0, "Color", ""},
    {22, "LUMINOSITY", 0, "Luminosity", ""},
    {0, nullptr, 0, nullptr, nullptr},
};

static const EnumPropertyItem tex_paint_target_items[] = {
    {TEXPAINT_TARGET_COLOR, "COLOR", ICON_BRUSH_DATA, "Color", "Paint the layer colors"},
    {TEXPAINT_TARGET_MASK, "MASK", ICON_MOD_MASK, "Mask", "Paint the layer mask"},
    {0, nullptr, 0, nullptr, nullptr},
};

static const EnumPropertyItem tex_paint_projection_items[] = {
    {SHD_PROJ_FLAT,
     "FLAT",
     0,
     "Flat",
     "Image is projected flat using the X and Y coordinates of the texture vector"},
    {SHD_PROJ_BOX,
     "BOX",
     0,
     "Box",
     "Image is projected using different components for each side of the object space bounding "
     "box"},
    {SHD_PROJ_SPHERE,
     "SPHERE",
     0,
     "Sphere",
     "Image is projected spherically using the Z axis as central"},
    {SHD_PROJ_TUBE,
     "TUBE",
     0,
     "Tube",
     "Image is projected from the tube using the Z axis as central"},
    {0, nullptr, 0, nullptr, nullptr},
};

static const EnumPropertyItem tex_paint_interpolation_items[] = {
    {SHD_INTERP_LINEAR, "Linear", 0, "Linear", "Linear interpolation"},
    {SHD_INTERP_CLOSEST, "Closest", 0, "Closest", "No interpolation (sample closest texel)"},
    {SHD_INTERP_CUBIC, "Cubic", 0, "Cubic", "Cubic interpolation"},
    {SHD_INTERP_SMART, "Smart", 0, "Smart", "Bicubic when magnifying, else bilinear (OSL only)"},
    {0, nullptr, 0, nullptr, nullptr},
};

static const EnumPropertyItem tex_paint_extension_items[] = {
    {SHD_IMAGE_EXTENSION_REPEAT,
     "REPEAT",
     0,
     "Repeat",
     "Cause the image to repeat horizontally and vertically"},
    {SHD_IMAGE_EXTENSION_EXTEND,
     "EXTEND",
     0,
     "Extend",
     "Extend by repeating edge pixels of the image"},
    {SHD_IMAGE_EXTENSION_CLIP,
     "CLIP",
     0,
     "Clip",
     "Clip to image size and set exterior pixels as transparent"},
    {SHD_IMAGE_EXTENSION_MIRROR,
     "MIRROR",
     0,
     "Mirror",
     "Repeatedly flip the image horizontally and vertically"},
    {0, nullptr, 0, nullptr, nullptr},
};

static void rna_def_tex_paint_layer(BlenderRNA *brna)
{
  StructRNA *srna;
  PropertyRNA *prop;

  srna = RNA_def_struct(brna, "TexPaintLayer", nullptr);
  RNA_def_struct_ui_text(srna, "Texture Paint Layer", "Layer of a layered Texture Paint node");
  RNA_def_struct_sdna(srna, "TexPaintLayer");
  RNA_def_struct_path_func(srna, "rna_TexPaintLayer_path");
  RNA_def_struct_ui_icon(srna, ICON_RENDERLAYERS);

  prop = RNA_def_property(srna, "name", PROP_STRING, PROP_NONE);
  RNA_def_property_string_funcs(prop, nullptr, nullptr, "rna_TexPaintLayer_name_set");
  RNA_def_property_ui_text(prop, "Name", "Layer name");
  RNA_def_property_flag(prop, PROP_NO_DEG_UPDATE);
  RNA_def_property_update(prop, 0, "rna_TexPaintLayer_redraw_update");
  RNA_def_struct_name_property(srna, prop);

  prop = RNA_def_property(srna, "uid", PROP_INT, PROP_NONE);
  RNA_def_property_clear_flag(prop, PROP_EDITABLE);
  RNA_def_property_ui_text(
      prop, "Unique Identifier", "Persistent identifier of the layer within its node");

  prop = RNA_def_property(srna, "type", PROP_ENUM, PROP_NONE);
  RNA_def_property_enum_items(prop, tex_paint_layer_type_items);
  RNA_def_property_clear_flag(prop, PROP_EDITABLE);
  RNA_def_property_ui_text(prop, "Type", "Kind of layer");

  prop = RNA_def_property(srna, "blend_mode", PROP_ENUM, PROP_NONE);
  RNA_def_property_enum_items(prop, tex_paint_blend_mode_items);
  RNA_def_property_ui_text(prop, "Blend Mode", "How the layer is blended with the layers below");
  RNA_def_property_flag(prop, PROP_NO_DEG_UPDATE);
  RNA_def_property_update(prop, 0, "rna_TexPaintLayer_update");

  prop = RNA_def_property(srna, "opacity", PROP_FLOAT, PROP_FACTOR);
  RNA_def_property_range(prop, 0.0f, 1.0f);
  RNA_def_property_ui_text(prop, "Opacity", "Layer opacity");
  RNA_def_property_flag(prop, PROP_NO_DEG_UPDATE);
  RNA_def_property_update(prop, 0, "rna_TexPaintLayer_update");

  prop = RNA_def_property(srna, "hide", PROP_BOOLEAN, PROP_NONE);
  RNA_def_property_boolean_negative_sdna(prop, nullptr, "flag", TEXPAINT_LAYER_VISIBLE);
  RNA_def_property_ui_icon(prop, ICON_HIDE_OFF, -1);
  RNA_def_property_ui_text(prop, "Hide", "Exclude the layer from the composite");
  RNA_def_property_flag(prop, PROP_NO_DEG_UPDATE);
  RNA_def_property_update(prop, 0, "rna_TexPaintLayer_update");

  prop = RNA_def_property(srna, "lock", PROP_BOOLEAN, PROP_NONE);
  RNA_def_property_boolean_sdna(prop, nullptr, "flag", TEXPAINT_LAYER_LOCKED);
  RNA_def_property_ui_icon(prop, ICON_UNLOCKED, 1);
  RNA_def_property_ui_text(prop, "Lock", "Prevent painting on the layer");
  RNA_def_property_flag(prop, PROP_NO_DEG_UPDATE);
  RNA_def_property_update(prop, 0, "rna_TexPaintLayer_redraw_update");

  prop = RNA_def_property(srna, "lock_alpha", PROP_BOOLEAN, PROP_NONE);
  RNA_def_property_boolean_sdna(prop, nullptr, "flag", TEXPAINT_LAYER_ALPHA_LOCK);
  RNA_def_property_ui_icon(prop, ICON_TEXTURE, 0);
  RNA_def_property_ui_text(
      prop, "Lock Alpha", "Keep the transparency of the layer pixels while painting");
  RNA_def_property_flag(prop, PROP_NO_DEG_UPDATE);
  RNA_def_property_update(prop, 0, "rna_TexPaintLayer_redraw_update");

  prop = RNA_def_property(srna, "use_clipping", PROP_BOOLEAN, PROP_NONE);
  RNA_def_property_boolean_sdna(prop, nullptr, "flag", TEXPAINT_LAYER_CLIPPING);
  RNA_def_property_ui_text(
      prop, "Clipping Mask", "Clip the layer to the transparency of the layer below it");
  RNA_def_property_flag(prop, PROP_NO_DEG_UPDATE);
  RNA_def_property_update(prop, 0, "rna_TexPaintLayer_update");

  prop = RNA_def_property(srna, "use_mask", PROP_BOOLEAN, PROP_NONE);
  RNA_def_property_boolean_sdna(prop, nullptr, "flag", TEXPAINT_LAYER_USE_MASK);
  RNA_def_property_ui_text(prop, "Use Mask", "Use the layer mask");
  RNA_def_property_flag(prop, PROP_NO_DEG_UPDATE);
  RNA_def_property_update(prop, 0, "rna_TexPaintLayer_use_mask_update");

  prop = RNA_def_property(srna, "invert_mask", PROP_BOOLEAN, PROP_NONE);
  RNA_def_property_boolean_sdna(prop, nullptr, "flag", TEXPAINT_LAYER_MASK_INVERT);
  RNA_def_property_ui_text(prop, "Invert Mask", "Invert the layer mask");
  RNA_def_property_flag(prop, PROP_NO_DEG_UPDATE);
  RNA_def_property_update(prop, 0, "rna_TexPaintLayer_update");

  prop = RNA_def_property(srna, "fill_color", PROP_FLOAT, PROP_COLOR);
  RNA_def_property_array(prop, 4);
  RNA_def_property_range(prop, 0.0f, FLT_MAX);
  RNA_def_property_ui_range(prop, 0.0f, 1.0f, 1, 3);
  RNA_def_property_ui_text(prop, "Fill Color", "Color of a fill layer, in scene linear space");
  RNA_def_property_flag(prop, PROP_NO_DEG_UPDATE);
  RNA_def_property_update(prop, 0, "rna_TexPaintLayer_update");

  prop = RNA_def_property(srna, "adjust_hsv", PROP_FLOAT, PROP_NONE);
  RNA_def_property_array(prop, 3);
  RNA_def_property_ui_range(prop, -1.0f, 1.0f, 1, 3);
  RNA_def_property_ui_text(
      prop, "Hue/Saturation/Value", "Hue shift, saturation and value offsets of an adjustment");
  RNA_def_property_flag(prop, PROP_NO_DEG_UPDATE);
  RNA_def_property_update(prop, 0, "rna_TexPaintLayer_update");

  prop = RNA_def_property(srna, "adjust_bright_contrast", PROP_FLOAT, PROP_NONE);
  RNA_def_property_array(prop, 2);
  RNA_def_property_ui_range(prop, -1.0f, 1.0f, 1, 3);
  RNA_def_property_ui_text(
      prop, "Brightness/Contrast", "Brightness and contrast offsets of an adjustment");
  RNA_def_property_flag(prop, PROP_NO_DEG_UPDATE);
  RNA_def_property_update(prop, 0, "rna_TexPaintLayer_update");

  prop = RNA_def_property(srna, "has_mask", PROP_BOOLEAN, PROP_NONE);
  RNA_def_property_boolean_funcs(prop, "rna_TexPaintLayer_has_mask_get", nullptr);
  RNA_def_property_clear_flag(prop, PROP_EDITABLE);
  RNA_def_property_ui_text(prop, "Has Mask", "The layer has mask pixels");

  /* Linked image & 3D Projection Widget properties. */
  prop = RNA_def_property(srna, "proj_image_name", PROP_STRING, PROP_NONE);
  RNA_def_property_string_sdna(prop, nullptr, "proj_image_name");
  RNA_def_property_ui_text(
      prop, "Projected Image", "Name of the source Image projected onto this layer");
  RNA_def_property_flag(prop, PROP_NO_DEG_UPDATE);
  RNA_def_property_update(prop, 0, "rna_TexPaintLayer_projection_update");

  prop = RNA_def_property(srna, "proj_mode", PROP_ENUM, PROP_NONE);
  RNA_def_property_enum_sdna(prop, nullptr, "proj_mode");
  RNA_def_property_enum_items(prop, tex_paint_layer_proj_mode_items);
  RNA_def_property_ui_text(
      prop, "Projection Mode", "How the linked image is projected onto this layer");
  RNA_def_property_flag(prop, PROP_NO_DEG_UPDATE);
  RNA_def_property_update(prop, 0, "rna_TexPaintLayer_projection_update");

  prop = RNA_def_property(srna, "proj_blend_falloff", PROP_FLOAT, PROP_FACTOR);
  RNA_def_property_float_sdna(prop, nullptr, "proj_blend_falloff");
  RNA_def_property_range(prop, 0.0f, 1.0f);
  RNA_def_property_ui_text(
      prop, "Falloff / Blend", "Normal angle falloff for decals or box blend for triplanar");
  RNA_def_property_flag(prop, PROP_NO_DEG_UPDATE);
  RNA_def_property_update(prop, 0, "rna_TexPaintLayer_projection_update");

  prop = RNA_def_property(srna, "proj_location", PROP_FLOAT, PROP_TRANSLATION);
  RNA_def_property_float_sdna(prop, nullptr, "proj_location");
  RNA_def_property_array(prop, 3);
  RNA_def_property_ui_range(prop, -100.0f, 100.0f, 1, 3);
  RNA_def_property_ui_text(
      prop, "Projection Location", "3D projection widget position in Object space");
  RNA_def_property_flag(prop, PROP_NO_DEG_UPDATE);
  RNA_def_property_update(prop, 0, "rna_TexPaintLayer_projection_update");

  prop = RNA_def_property(srna, "proj_rotation", PROP_FLOAT, PROP_EULER);
  RNA_def_property_float_sdna(prop, nullptr, "proj_rotation");
  RNA_def_property_array(prop, 3);
  RNA_def_property_ui_text(
      prop, "Projection Rotation", "3D projection widget Euler XYZ rotation in Object space");
  RNA_def_property_flag(prop, PROP_NO_DEG_UPDATE);
  RNA_def_property_update(prop, 0, "rna_TexPaintLayer_projection_update");

  prop = RNA_def_property(srna, "proj_scale", PROP_FLOAT, PROP_XYZ);
  RNA_def_property_float_sdna(prop, nullptr, "proj_scale");
  RNA_def_property_array(prop, 3);
  RNA_def_property_range(prop, 0.001f, 1000.0f);
  RNA_def_property_ui_range(prop, 0.01f, 50.0f, 1, 3);
  RNA_def_property_ui_text(
      prop, "Projection Scale", "3D projection widget half-extents / scale in Object space");
  RNA_def_property_flag(prop, PROP_NO_DEG_UPDATE);
  RNA_def_property_update(prop, 0, "rna_TexPaintLayer_projection_update");

  prop = RNA_def_property(srna, "show_projection_widget", PROP_BOOLEAN, PROP_NONE);
  RNA_def_property_boolean_sdna(prop, nullptr, "flag", TEXPAINT_LAYER_SHOW_WIDGET);
  RNA_def_property_ui_icon(prop, ICON_GIZMO, 0);
  RNA_def_property_ui_text(
      prop,
      "Show 3D Widget",
      "Display the interactive 3D projection gizmo in the 3D Viewport when this layer is selected");
  RNA_def_property_flag(prop, PROP_NO_DEG_UPDATE);
  RNA_def_property_update(prop, 0, "rna_TexPaintLayer_redraw_update");

  /* Substance-style Smart Mask Generator properties. */
  prop = RNA_def_property(srna, "mask_gen_type", PROP_ENUM, PROP_NONE);
  RNA_def_property_enum_sdna(prop, nullptr, "mask_gen_type");
  RNA_def_property_enum_items(prop, tex_paint_mask_gen_type_items);
  RNA_def_property_ui_text(
      prop, "Mask Generator", "Procedural geometry mask generator (AO, Curvature, Edge Wear)");
  RNA_def_property_flag(prop, PROP_NO_DEG_UPDATE);
  RNA_def_property_update(prop, 0, "rna_TexPaintLayer_mask_gen_update");

  prop = RNA_def_property(srna, "mask_gen_blend", PROP_ENUM, PROP_NONE);
  RNA_def_property_enum_sdna(prop, nullptr, "mask_gen_blend");
  RNA_def_property_enum_items(prop, tex_paint_mask_gen_blend_items);
  RNA_def_property_ui_text(
      prop, "Generator Blend", "How the procedural generator blends with hand-painted mask strokes");
  RNA_def_property_flag(prop, PROP_NO_DEG_UPDATE);
  RNA_def_property_update(prop, 0, "rna_TexPaintLayer_mask_gen_update");

  prop = RNA_def_property(srna, "mask_gen_balance", PROP_FLOAT, PROP_FACTOR);
  RNA_def_property_float_sdna(prop, nullptr, "mask_gen_balance");
  RNA_def_property_range(prop, 0.0f, 1.0f);
  RNA_def_property_ui_text(
      prop, "Balance", "Threshold / coverage balance of the generated mask");
  RNA_def_property_flag(prop, PROP_NO_DEG_UPDATE);
  RNA_def_property_update(prop, 0, "rna_TexPaintLayer_mask_gen_update");

  prop = RNA_def_property(srna, "mask_gen_contrast", PROP_FLOAT, PROP_FACTOR);
  RNA_def_property_float_sdna(prop, nullptr, "mask_gen_contrast");
  RNA_def_property_range(prop, 0.0f, 1.0f);
  RNA_def_property_ui_text(prop, "Contrast", "Sharpness / contrast of the generated mask");
  RNA_def_property_flag(prop, PROP_NO_DEG_UPDATE);
  RNA_def_property_update(prop, 0, "rna_TexPaintLayer_mask_gen_update");

  prop = RNA_def_property(srna, "mask_gen_radius", PROP_FLOAT, PROP_FACTOR);
  RNA_def_property_float_sdna(prop, nullptr, "mask_gen_radius");
  RNA_def_property_range(prop, 0.0f, 1.0f);
  RNA_def_property_ui_text(
      prop, "Spread / Radius", "Spatial spread of curvature or ambient occlusion");
  RNA_def_property_flag(prop, PROP_NO_DEG_UPDATE);
  RNA_def_property_update(prop, 0, "rna_TexPaintLayer_mask_gen_update");

  prop = RNA_def_property(srna, "mask_gen_noise", PROP_FLOAT, PROP_FACTOR);
  RNA_def_property_float_sdna(prop, nullptr, "mask_gen_noise");
  RNA_def_property_range(prop, 0.0f, 1.0f);
  RNA_def_property_ui_text(
      prop, "Grunge Breakup", "Amount of 3D procedural grunge noise breakup on edges/mask");
  RNA_def_property_flag(prop, PROP_NO_DEG_UPDATE);
  RNA_def_property_update(prop, 0, "rna_TexPaintLayer_mask_gen_update");

  prop = RNA_def_property(srna, "mask_gen_noise_scale", PROP_FLOAT, PROP_NONE);
  RNA_def_property_float_sdna(prop, nullptr, "mask_gen_noise_scale");
  RNA_def_property_range(prop, 0.5f, 64.0f);
  RNA_def_property_ui_range(prop, 1.0f, 32.0f, 1, 2);
  RNA_def_property_ui_text(prop, "Grunge Scale", "3D frequency scale of the grunge breakup noise");
  RNA_def_property_flag(prop, PROP_NO_DEG_UPDATE);
  RNA_def_property_update(prop, 0, "rna_TexPaintLayer_mask_gen_update");

  prop = RNA_def_property(srna, "mask_gen_invert", PROP_BOOLEAN, PROP_NONE);
  RNA_def_property_boolean_sdna(prop, nullptr, "mask_gen_invert", 1);
  RNA_def_property_ui_text(prop, "Invert Generator", "Invert the generated mask values");
  RNA_def_property_flag(prop, PROP_NO_DEG_UPDATE);
  RNA_def_property_update(prop, 0, "rna_TexPaintLayer_mask_gen_update");
}

void rna_def_sh_tex_paint(BlenderRNA * /*brna*/, StructRNA *srna)
{
  PropertyRNA *prop;

  prop = RNA_def_property(srna, "image", PROP_POINTER, PROP_NONE);
  RNA_def_property_pointer_sdna(prop, nullptr, "id");
  RNA_def_property_struct_type(prop, "Image");
  RNA_def_property_clear_flag(prop, PROP_EDITABLE);
  RNA_def_property_ui_text(
      prop, "Image", "Image receiving the composite of the layers, sampled for shading");

  /* #NodeTexPaint starts with a #NodeTexImage. */
  RNA_def_struct_sdna_from(srna, "NodeTexImage", "storage");

  prop = RNA_def_property(srna, "projection", PROP_ENUM, PROP_NONE);
  RNA_def_property_enum_items(prop, tex_paint_projection_items);
  RNA_def_property_ui_text(
      prop, "Projection", "Method to project 2D image on object with a 3D texture vector");
  RNA_def_property_translation_context(prop, BLT_I18NCONTEXT_ID_IMAGE);
  RNA_def_property_update(prop, 0, "rna_Node_update");

  prop = RNA_def_property(srna, "interpolation", PROP_ENUM, PROP_NONE);
  RNA_def_property_enum_items(prop, tex_paint_interpolation_items);
  RNA_def_property_ui_text(prop, "Interpolation", "Texture interpolation");
  RNA_def_property_update(prop, 0, "rna_Node_update");

  prop = RNA_def_property(srna, "projection_blend", PROP_FLOAT, PROP_FACTOR);
  RNA_def_property_ui_text(
      prop, "Projection Blend", "For box projection, amount of blend to use between sides");
  RNA_def_property_update(prop, 0, "rna_Node_update");

  prop = RNA_def_property(srna, "extension", PROP_ENUM, PROP_NONE);
  RNA_def_property_enum_items(prop, tex_paint_extension_items);
  RNA_def_property_ui_text(
      prop, "Extension", "How the image is extrapolated past its original bounds");
  RNA_def_property_translation_context(prop, BLT_I18NCONTEXT_ID_IMAGE);
  RNA_def_property_update(prop, 0, "rna_Node_update");

  prop = RNA_def_property(srna, "image_user", PROP_POINTER, PROP_NONE);
  RNA_def_property_flag(prop, PROP_NEVER_NULL);
  RNA_def_property_pointer_sdna(prop, nullptr, "iuser");
  RNA_def_property_ui_text(prop, "Image User", "Parameters defining how the image is sampled");
  RNA_def_property_update(prop, 0, "rna_Node_update");

  RNA_def_struct_sdna_from(srna, "NodeTexPaint", "storage");

  prop = RNA_def_property(srna, "canvas_width", PROP_INT, PROP_PIXEL);
  RNA_def_property_int_sdna(prop, nullptr, "width");
  RNA_def_property_clear_flag(prop, PROP_EDITABLE);
  RNA_def_property_ui_text(prop, "Canvas Width", "Width of the layer canvas");

  prop = RNA_def_property(srna, "canvas_height", PROP_INT, PROP_PIXEL);
  RNA_def_property_int_sdna(prop, nullptr, "height");
  RNA_def_property_clear_flag(prop, PROP_EDITABLE);
  RNA_def_property_ui_text(prop, "Canvas Height", "Height of the layer canvas");

  prop = RNA_def_property(srna, "use_float", PROP_BOOLEAN, PROP_NONE);
  RNA_def_property_boolean_sdna(prop, nullptr, "is_float", 1);
  RNA_def_property_clear_flag(prop, PROP_EDITABLE);
  RNA_def_property_ui_text(prop, "Float Buffer", "Layers use 32-bit float pixels");

  prop = RNA_def_property(srna, "layers", PROP_COLLECTION, PROP_NONE);
  RNA_def_property_collection_sdna(prop, nullptr, "layers", nullptr);
  RNA_def_property_struct_type(prop, "TexPaintLayer");
  RNA_def_property_ui_text(prop, "Layers", "Layers from bottom to top");

  prop = RNA_def_property(srna, "active_layer_index", PROP_INT, PROP_UNSIGNED);
  RNA_def_property_int_funcs(prop,
                             nullptr,
                             "rna_ShaderNodeTexPaint_active_layer_index_set",
                             "rna_ShaderNodeTexPaint_active_layer_index_range");
  RNA_def_property_ui_text(prop, "Active Layer Index", "Index of the layer being painted");
  RNA_def_property_flag(prop, PROP_NO_DEG_UPDATE);
  RNA_def_property_update(prop, NC_SCENE | ND_OB_ACTIVE, nullptr);

  prop = RNA_def_property(srna, "active_layer", PROP_POINTER, PROP_NONE);
  RNA_def_property_struct_type(prop, "TexPaintLayer");
  RNA_def_property_pointer_funcs(prop,
                                 "rna_ShaderNodeTexPaint_active_layer_get",
                                 "rna_ShaderNodeTexPaint_active_layer_set",
                                 nullptr,
                                 nullptr);
  RNA_def_property_flag(prop, PROP_EDITABLE | PROP_NO_DEG_UPDATE);
  RNA_def_property_ui_text(prop, "Active Layer", "Layer being painted");
  RNA_def_property_update(prop, NC_SCENE | ND_OB_ACTIVE, nullptr);

  prop = RNA_def_property(srna, "paint_target", PROP_ENUM, PROP_NONE);
  RNA_def_property_enum_items(prop, tex_paint_target_items);
  RNA_def_property_ui_text(prop, "Paint Target", "Part of the active layer that brushes paint");
  RNA_def_property_flag(prop, PROP_NO_DEG_UPDATE);
  RNA_def_property_update(prop, NC_NODE | NA_EDITED, nullptr);
}

void RNA_def_node_tex_paint(BlenderRNA *brna)
{
  rna_def_tex_paint_layer(brna);
}

}  // namespace blender

#endif /* RNA_RUNTIME */
