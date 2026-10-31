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

#  include "DNA_image_types.h"
#  include "DNA_material_types.h"
#  include "DNA_mesh_types.h"
#  include "DNA_object_types.h"

#  include "BLI_listbase.hh"
#  include "BLI_string.hh"
#  include "BLI_string_utils.hh"

#  include "BKE_lib_id.hh"
#  include "BKE_library.hh"
#  include "BKE_main.hh"
#  include "BKE_main_invariants.hh"
#  include "BKE_material.hh"
#  include "BKE_node_legacy_types.hh"
#  include "BKE_node_tree_update.hh"
#  include "BKE_tex_paint_layer.hh"
#  include "BKE_tex_paint_mask.hh"
#  include "IMB_imbuf_types.hh"

#  include "DEG_depsgraph.hh"
#  include "DEG_depsgraph_build.hh"

#  include "RNA_access.hh"
#  include "RNA_prototypes.hh"

#  include "WM_api.hh"

namespace blender {

static NodeTexPaint &rna_paint_storage(const PointerRNA *ptr)
{
  return *static_cast<NodeTexPaint *>(ptr->data_as<bNode>()->storage);
}

int rna_paint_selection_length(const PointerRNA *ptr, int length[RNA_MAX_ARRAY_DIMENSION])
{
  const NodeTexPaint &data = rna_paint_storage(ptr);
  length[0] = data.width * data.height;
  return length[0];
}

void rna_paint_selection_get(PointerRNA *ptr, float *values)
{
  const NodeTexPaint &data = rna_paint_storage(ptr);
  const Span<float> selection = BKE_tex_paint_selection(data);
  if (selection.is_empty()) {
    std::fill_n(values, data.width * data.height, 0.0f);
  }
  else {
    std::copy(selection.begin(), selection.end(), values);
  }
}

void rna_paint_selection_set(PointerRNA *ptr, const float *values)
{
  NodeTexPaint &data = rna_paint_storage(ptr);
  BKE_tex_paint_selection_set(data, {values, int64_t(data.width) * data.height});
}

void rna_paint_pixels_update(Main *bmain, Scene *, PointerRNA *ptr)
{
  bmain->is_memfile_undo_written = false;
  DEG_id_tag_update(ptr->owner_id, ID_RECALC_TAG_FOR_UNDO);
  bNodeTree &tree = *reinterpret_cast<bNodeTree *>(ptr->owner_id);
  if (tree.owner_id) {
    DEG_id_tag_update(tree.owner_id, ID_RECALC_TAG_FOR_UNDO);
  }
}

void rna_paint_selection_update(Main *bmain, Scene *scene, PointerRNA *ptr)
{
  BKE_tex_paint_selection_transform_clear(rna_paint_storage(ptr));
  rna_paint_pixels_update(bmain, scene, ptr);
}

int rna_paint_pixels_length(const PointerRNA *ptr, int length[RNA_MAX_ARRAY_DIMENSION])
{
  ImBuf *buffer = BKE_tex_paint_paint_target_ibuf(rna_paint_storage(ptr));
  length[0] = buffer ? buffer->x * buffer->y * 4 : 0;
  return length[0];
}

void rna_paint_pixels_get(PointerRNA *ptr, float *values)
{
  ImBuf *buffer = BKE_tex_paint_paint_target_ibuf(rna_paint_storage(ptr));
  if (!buffer) {
    return;
  }
  Array<float4> pixels = BKE_tex_paint_pixels_read(*buffer);
  for (const int64_t i : pixels.index_range()) {
    const float4 p = pixels[i];
    const float a = p.w > 0 ? 1.0f / p.w : 0.0f;
    values[i * 4] = p.x * a;
    values[i * 4 + 1] = p.y * a;
    values[i * 4 + 2] = p.z * a;
    values[i * 4 + 3] = p.w;
  }
}

void rna_paint_pixels_set(PointerRNA *ptr, const float *values)
{
  bNode &node = *ptr->data_as<bNode>();
  ImBuf *buffer = BKE_tex_paint_paint_target_ibuf(rna_paint_storage(ptr));
  if (!buffer) {
    return;
  }
  Array<float4> pixels(int64_t(buffer->x) * buffer->y);
  for (const int64_t i : pixels.index_range()) {
    const float *p = values + i * 4;
    pixels[i] = float4(p[0] * p[3], p[1] * p[3], p[2] * p[3], p[3]);
  }
  const rcti rect = {0, buffer->x, 0, buffer->y};
  BKE_tex_paint_pixels_write(*buffer, pixels, rect);
  BKE_tex_paint_stroke_update(node, buffer, rect);
}

/** Request redraws of the master image users without touching the node tree. */
static void tex_paint_tag_redraw(bNode &node)
{
  if (node.id) {
    DEG_id_tag_update(node.id, ID_RECALC_EDITORS);
    WM_main_add_notifier(NC_IMAGE | NA_EDITED, node.id);
  }
  if (NodeTexPaint *data = static_cast<NodeTexPaint *>(node.storage)) {
    if (data->pbr_image) {
      DEG_id_tag_update(&data->pbr_image->id, ID_RECALC_EDITORS);
      WM_main_add_notifier(NC_IMAGE | NA_EDITED, data->pbr_image);
    }
    if (data->emission_image) {
      DEG_id_tag_update(&data->emission_image->id, ID_RECALC_EDITORS);
      WM_main_add_notifier(NC_IMAGE | NA_EDITED, data->emission_image);
    }
  }
  WM_main_add_notifier(NC_NODE | NA_EDITED, nullptr);
  WM_main_add_notifier(NC_SCENE | ND_OB_ACTIVE, nullptr);
}

static bool rna_TexPaintLayer_has_mask_get(PointerRNA *ptr)
{
  const TexPaintLayer *layer = ptr->data_as<TexPaintLayer>();
  return layer->mask_ibuf != nullptr || layer->mask_pf != nullptr;
}

static void rna_TexPaintColorFilter_update(Main *bmain, Scene *scene, PointerRNA *ptr)
{
  bNodeTree *tree = reinterpret_cast<bNodeTree *>(ptr->owner_id);
  if (tree == nullptr) {
    return;
  }
  for (bNode &node : tree->nodes) {
    if (node.type_legacy != SH_NODE_TEX_PAINT || node.storage == nullptr) {
      continue;
    }
    NodeTexPaint &data = *static_cast<NodeTexPaint *>(node.storage);
    for (TexPaintLayer &layer : data.layers) {
      if (BLI_findindex(&layer.color_filters, ptr->data) != -1) {
        rna_paint_pixels_update(bmain, scene, ptr);
        BKE_tex_paint_cache_invalidate(data);
        BKE_tex_paint_composite_full(node);
        tex_paint_tag_redraw(node);
        return;
      }
    }
  }
}

static void rna_TexPaintLayer_filter_index_range(
    PointerRNA *ptr, int *min, int *max, int * /*softmin*/, int * /*softmax*/)
{
  *min = 0;
  *max = std::max(0, ptr->data_as<TexPaintLayer>()->color_filters.count() - 1);
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
  NodeTexPaint *data = static_cast<NodeTexPaint *>(node->storage);
  bool had_preview_mask = false;
  if (TexPaintLayer *prev = BKE_tex_paint_layer_active(*data)) {
    if (prev->flag & TEXPAINT_LAYER_PREVIEW_MASK) {
      prev->flag &= ~TEXPAINT_LAYER_PREVIEW_MASK;
      had_preview_mask = true;
    }
  }
  BKE_tex_paint_layer_active_set(*data, value);
  if (had_preview_mask) {
    BKE_tex_paint_cache_invalidate(*data);
    BKE_tex_paint_composite_full(*node);
  }
  /* Keep companion PBR channel nodes in the same tree positioned on the matching active layer
   * so their below-active composite caches stay warm. */
  bNodeTree *ntree = reinterpret_cast<bNodeTree *>(ptr->owner_id);
  const TexPaintLayer *active = BKE_tex_paint_layer_active(*data);
  if (BKE_tex_paint_tree_shares_channel_layers(ntree) && active != nullptr) {
    for (bNode &other_node : ntree->nodes) {
      if (&other_node == node || other_node.type_legacy != SH_NODE_TEX_PAINT ||
          other_node.storage == nullptr)
      {
        continue;
      }
      NodeTexPaint *other_data = static_cast<NodeTexPaint *>(other_node.storage);
      if (TexPaintLayer *other_layer = BKE_tex_paint_layer_find_uid(*other_data, active->uid)) {
        const int other_idx = BLI_findindex(&other_data->layers, other_layer);
        if (other_idx != -1 && other_data->active_layer_index != other_idx) {
          BKE_tex_paint_layer_active_set(*other_data, other_idx);
        }
      }
    }
  }
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

void rna_ShaderNodeTexPaint_pbr_update(Main *bmain, Scene * /*scene*/, PointerRNA *ptr)
{
  bNode *node = ptr->data_as<bNode>();
  if (node == nullptr || node->storage == nullptr) {
    return;
  }
  NodeTexPaint *data = static_cast<NodeTexPaint *>(node->storage);
  if (bmain != nullptr && (data->pbr_image == nullptr || data->emission_image == nullptr)) {
    BKE_tex_paint_ensure_pbr_images(*bmain, *node);
  }
  BKE_tex_paint_cache_invalidate(*data);
  BKE_tex_paint_composite_full(*node);
  tex_paint_tag_redraw(*node);
}

static bNode *rna_TexPaintLayer_node(const PointerRNA *ptr)
{
  bNodeTree *ntree = reinterpret_cast<bNodeTree *>(ptr->owner_id);
  if (ntree == nullptr) {
    return nullptr;
  }
  return BKE_tex_paint_node_find_by_layer(*ntree, ptr->data_as<TexPaintLayer>());
}

static bool rna_TexPaintMaskEffect_owner(const PointerRNA *ptr,
                                         bNode *&r_node,
                                         TexPaintLayer *&r_layer)
{
  r_node = nullptr;
  r_layer = nullptr;
  bNodeTree *tree = reinterpret_cast<bNodeTree *>(ptr->owner_id);
  if (!tree || !ptr->data) {
    return false;
  }
  for (bNode &node : tree->nodes) {
    if (node.type_legacy != SH_NODE_TEX_PAINT || !node.storage) {
      continue;
    }
    NodeTexPaint &data = *static_cast<NodeTexPaint *>(node.storage);
    for (TexPaintLayer &layer : data.layers) {
      if (BLI_findindex(&layer.mask_effects, ptr->data) != -1) {
        r_node = &node;
        r_layer = &layer;
        return true;
      }
    }
  }
  return false;
}

static ImBuf *rna_TexPaintMaskEffect_pixels_ensure(const PointerRNA *ptr)
{
  bNode *node;
  TexPaintLayer *layer;
  if (!rna_TexPaintMaskEffect_owner(ptr, node, layer)) {
    return nullptr;
  }
  return BKE_tex_paint_mask_effect_ensure_pixels(*static_cast<NodeTexPaint *>(node->storage),
                                                 *ptr->data_as<TexPaintMaskEffect>());
}

static int rna_TexPaintMaskEffect_pixels_length(const PointerRNA *ptr,
                                                int length[RNA_MAX_ARRAY_DIMENSION])
{
  ImBuf *buffer = rna_TexPaintMaskEffect_pixels_ensure(ptr);
  length[0] = buffer ? buffer->x * buffer->y * 4 : 0;
  return length[0];
}

static void rna_tex_paint_rgba_get(const ImBuf *buffer, float *values)
{
  if (!buffer) {
    return;
  }
  const Array<float4> pixels = BKE_tex_paint_pixels_read(*buffer);
  for (const int64_t i : pixels.index_range()) {
    const float4 pixel = pixels[i];
    const float inverse_alpha = pixel.w > 0.0f ? 1.0f / pixel.w : 0.0f;
    values[i * 4] = pixel.x * inverse_alpha;
    values[i * 4 + 1] = pixel.y * inverse_alpha;
    values[i * 4 + 2] = pixel.z * inverse_alpha;
    values[i * 4 + 3] = pixel.w;
  }
}

static void rna_TexPaintMaskEffect_pixels_get(PointerRNA *ptr, float *values)
{
  rna_tex_paint_rgba_get(rna_TexPaintMaskEffect_pixels_ensure(ptr), values);
}

static void rna_TexPaintMaskEffect_pixels_set(PointerRNA *ptr, const float *values)
{
  ImBuf *buffer = rna_TexPaintMaskEffect_pixels_ensure(ptr);
  if (!buffer) {
    return;
  }
  Array<float4> pixels(int64_t(buffer->x) * buffer->y);
  for (const int64_t i : pixels.index_range()) {
    const float *pixel = values + i * 4;
    pixels[i] = float4(pixel[0] * pixel[3], pixel[1] * pixel[3], pixel[2] * pixel[3], pixel[3]);
  }
  const rcti rect = {0, buffer->x, 0, buffer->y};
  BKE_tex_paint_pixels_write(*buffer, pixels, rect);
  TexPaintMaskEffect &effect = *ptr->data_as<TexPaintMaskEffect>();
  effect.runtime_dirty = 1;
  effect.revision++;
}

static int rna_TexPaintMaskEffect_curve_length(const PointerRNA * /*ptr*/,
                                              int length[RNA_MAX_ARRAY_DIMENSION])
{
  length[0] = 256;
  return length[0];
}

static void rna_TexPaintMaskEffect_curve_get(PointerRNA *ptr, float *values)
{
  std::copy_n(ptr->data_as<TexPaintMaskEffect>()->curve, 256, values);
}

static void rna_TexPaintMaskEffect_curve_set(PointerRNA *ptr, const float *values)
{
  std::copy_n(values, 256, ptr->data_as<TexPaintMaskEffect>()->curve);
}

static ImBuf *rna_TexPaintLayer_mask_pixels_ensure(const PointerRNA *ptr)
{
  bNode *node = rna_TexPaintLayer_node(ptr);
  if (!node) {
    return nullptr;
  }
  TexPaintLayer &layer = *ptr->data_as<TexPaintLayer>();
  if (!layer.mask_ibuf && !layer.mask_pf && !(layer.flag & TEXPAINT_LAYER_USE_MASK)) {
    return nullptr;
  }
  return BKE_tex_paint_layer_ensure_mask_ibuf(*static_cast<NodeTexPaint *>(node->storage), layer);
}

static int rna_TexPaintLayer_mask_pixels_length(const PointerRNA *ptr,
                                                int length[RNA_MAX_ARRAY_DIMENSION])
{
  ImBuf *buffer = rna_TexPaintLayer_mask_pixels_ensure(ptr);
  length[0] = buffer ? buffer->x * buffer->y * 4 : 0;
  return length[0];
}

static void rna_TexPaintLayer_mask_pixels_get(PointerRNA *ptr, float *values)
{
  rna_tex_paint_rgba_get(rna_TexPaintLayer_mask_pixels_ensure(ptr), values);
}

static PointerRNA rna_TexPaintLayer_mask_effect_ensure(PointerRNA ptr,
                                                       const int uid,
                                                       const int type)
{
  if (!rna_TexPaintLayer_node(&ptr) || uid <= 0 || type < TEXPAINT_MASK_EFFECT_SOURCE ||
      type > TEXPAINT_MASK_EFFECT_FILTER)
  {
    return {};
  }
  TexPaintLayer &layer = *ptr.data_as<TexPaintLayer>();
  TexPaintMaskEffect &effect = BKE_tex_paint_mask_effect_ensure(layer, uid, type);
  /* Publishing metadata in authoring order also restores native evaluation order. */
  BLI_remlink(&layer.mask_effects, &effect);
  BLI_addtail(&layer.mask_effects, &effect);
  return RNA_pointer_create_with_parent(ptr, RNA_TexPaintMaskEffect, &effect);
}

static bool rna_TexPaintLayer_mask_effect_remove(PointerRNA ptr, const int uid)
{
  return rna_TexPaintLayer_node(&ptr) &&
         BKE_tex_paint_mask_effect_remove(*ptr.data_as<TexPaintLayer>(), uid);
}

static void rna_TexPaintLayer_mask_effects_clear(PointerRNA ptr)
{
  if (rna_TexPaintLayer_node(&ptr)) {
    BKE_tex_paint_mask_effects_clear(*ptr.data_as<TexPaintLayer>());
  }
}

static bool rna_TexPaintLayer_mask_stack_evaluate(PointerRNA ptr, Main *bmain)
{
  bNode *node = rna_TexPaintLayer_node(&ptr);
  if (!node) {
    return false;
  }
  NodeTexPaint &data = *static_cast<NodeTexPaint *>(node->storage);
  TexPaintLayer &layer = *ptr.data_as<TexPaintLayer>();
  rcti rect;
  if (!BKE_tex_paint_mask_evaluate(data, layer, nullptr, rect)) {
    return false;
  }
  rna_paint_pixels_update(bmain, nullptr, &ptr);
  BKE_tex_paint_layer_pixels_changed(data, layer, &rect);
  BKE_tex_paint_composite_rect(*node, &rect);
  tex_paint_tag_redraw(*node);
  return true;
}

static bool rna_TexPaintLayer_mask_stack_copy_from(PointerRNA ptr, Main *bmain, PointerRNA *source)
{
  bNode *node = rna_TexPaintLayer_node(&ptr);
  bNode *source_node = source ? rna_TexPaintLayer_node(source) : nullptr;
  if (!node || !source_node || !ID_IS_EDITABLE(ptr.owner_id)) {
    return false;
  }
  NodeTexPaint &data = *static_cast<NodeTexPaint *>(node->storage);
  NodeTexPaint &source_data = *static_cast<NodeTexPaint *>(source_node->storage);
  if (data.width != source_data.width || data.height != source_data.height) {
    return false;
  }
  TexPaintLayer &dst = *ptr.data_as<TexPaintLayer>();
  TexPaintLayer &src = *source->data_as<TexPaintLayer>();
  if (&dst != &src) {
    Array<float4> plain_mask;
    if (!src.mask_effects_active && (src.flag & TEXPAINT_LAYER_USE_MASK)) {
      ImBuf *source_mask = BKE_tex_paint_layer_ensure_mask_ibuf(source_data, src);
      if (!source_mask) {
        return false;
      }
      plain_mask = BKE_tex_paint_pixels_read(*source_mask);
    }
    BKE_tex_paint_mask_effects_clear(dst);
    BKE_tex_paint_mask_effects_copy(dst, src, true);
    dst.active_mask_effect_uid = src.active_mask_effect_uid;
    dst.mask_base_value = src.mask_base_value;
    dst.mask_effects_active = src.mask_effects_active;
    constexpr int mask_flags = TEXPAINT_LAYER_USE_MASK | TEXPAINT_LAYER_MASK_INVERT;
    dst.flag = (dst.flag & ~mask_flags) | (src.flag & mask_flags);
    if (!plain_mask.is_empty()) {
      ImBuf *mask = BKE_tex_paint_layer_ensure_mask_ibuf(data, dst);
      if (!mask) {
        return false;
      }
      const rcti region = {0, data.width, 0, data.height};
      BKE_tex_paint_pixels_write(*mask, plain_mask, region);
    }
  }
  if (dst.mask_effects_active) {
    return rna_TexPaintLayer_mask_stack_evaluate(ptr, bmain);
  }
  rna_paint_pixels_update(bmain, nullptr, &ptr);
  BKE_tex_paint_layer_pixels_changed(data, dst, nullptr);
  BKE_tex_paint_composite_rect(*node, nullptr);
  tex_paint_tag_redraw(*node);
  return true;
}

static bool rna_TexPaintLayer_rasterize(PointerRNA ptr, Main *bmain, bool check_only)
{
  bNode *node = rna_TexPaintLayer_node(&ptr);
  if (!node || !ID_IS_EDITABLE(ptr.owner_id)) {
    return false;
  }
  NodeTexPaint &data = *static_cast<NodeTexPaint *>(node->storage);
  TexPaintLayer &layer = *ptr.data_as<TexPaintLayer>();
  if ((layer.flag & TEXPAINT_LAYER_LOCKED) ||
      !BKE_tex_paint_layer_rasterize(data, layer, check_only))
  {
    return false;
  }
  if (!check_only) {
    rna_paint_pixels_update(bmain, nullptr, &ptr);
    BKE_tex_paint_composite_full(*node);
    tex_paint_tag_redraw(*node);
  }
  return true;
}

bool rna_ShaderNodeTexPaint_make_images_single_user(PointerRNA ptr, Main *bmain)
{
  bNode &node = *ptr.data_as<bNode>();
  if (!ID_IS_EDITABLE(ptr.owner_id) || !BKE_tex_paint_is_initialized(node)) {
    return false;
  }
  NodeTexPaint &data = *static_cast<NodeTexPaint *>(node.storage);
  /* A material copy owns its layers but still references the original derived
   * images. Replace those IDs through Blender ownership, never raw pointer writes. */
  const auto copy_image = [&](Image *image) -> Image * {
    if (!image) {
      return nullptr;
    }
    Image *copy = reinterpret_cast<Image *>(BKE_id_copy_for_use_in_bmain(bmain, &image->id));
    id_us_min(&image->id);
    BKE_main_ensure_invariants(*bmain, copy->id);
    return copy;
  };
  node.id = reinterpret_cast<ID *>(copy_image(reinterpret_cast<Image *>(node.id)));
  data.pbr_image = copy_image(data.pbr_image);
  data.emission_image = copy_image(data.emission_image);
  BKE_tex_paint_ensure_pbr_images(*bmain, node);
  BKE_tex_paint_composite_full(node);
  rna_paint_pixels_update(bmain, nullptr, &ptr);
  DEG_relations_tag_update(bmain);
  bNodeTree &tree = *reinterpret_cast<bNodeTree *>(ptr.owner_id);
  BKE_ntree_update_tag_node_property(&tree, &node);
  BKE_main_ensure_invariants(*bmain, tree.id);
  tex_paint_tag_redraw(node);
  return true;
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
    if (bNodeTree *ntree = reinterpret_cast<bNodeTree *>(ptr->owner_id);
        BKE_tex_paint_tree_shares_channel_layers(ntree))
    {
      for (bNode &other_node : ntree->nodes) {
        if (&other_node == node || other_node.type_legacy != SH_NODE_TEX_PAINT ||
            other_node.storage == nullptr)
        {
          continue;
        }
        NodeTexPaint *other_data = static_cast<NodeTexPaint *>(other_node.storage);
        if (TexPaintLayer *other_layer = BKE_tex_paint_layer_find_uid(*other_data, layer->uid)) {
          STRNCPY_UTF8(other_layer->name, layer->name);
        }
      }
    }
  }
}

/** Composite after a layer property change, reusing the below-active cache when possible. */
static void rna_TexPaintLayer_update(Main *bmain, Scene * /*scene*/, PointerRNA *ptr)
{
  bNode *node = rna_TexPaintLayer_node(ptr);
  if (node == nullptr) {
    return;
  }
  NodeTexPaint *data = static_cast<NodeTexPaint *>(node->storage);
  if (bmain != nullptr && (data->pbr_image == nullptr || data->emission_image == nullptr)) {
    BKE_tex_paint_ensure_pbr_images(*bmain, *node);
  }
  TexPaintLayer *layer = ptr->data_as<TexPaintLayer>();
  const int index = BLI_findindex(&data->layers, ptr->data);
  if (index < data->active_layer_index) {
    BKE_tex_paint_cache_invalidate(*data);
  }
  BKE_tex_paint_composite_rect(*node, nullptr);
  tex_paint_tag_redraw(*node);

  /* Propagate shared layer attributes (visibility, clipping, mask invert, opacity)
   * across companion PBR channel nodes in the same material tree when any of them changed. */
  bNodeTree *ntree = reinterpret_cast<bNodeTree *>(ptr->owner_id);
  if (BKE_tex_paint_tree_shares_channel_layers(ntree) && layer != nullptr) {
    constexpr int shared_flags = TEXPAINT_LAYER_VISIBLE | TEXPAINT_LAYER_CLIPPING |
                                 TEXPAINT_LAYER_USE_MASK | TEXPAINT_LAYER_MASK_INVERT;
    for (bNode &other_node : ntree->nodes) {
      if (&other_node == node || other_node.type_legacy != SH_NODE_TEX_PAINT ||
          other_node.storage == nullptr || !BKE_tex_paint_is_initialized(other_node))
      {
        continue;
      }
      NodeTexPaint *other_data = static_cast<NodeTexPaint *>(other_node.storage);
      TexPaintLayer *other_layer = BKE_tex_paint_layer_find_uid(*other_data, layer->uid);
      if (other_layer == nullptr) {
        continue;
      }
      if (other_layer->opacity != layer->opacity ||
          (other_layer->flag & shared_flags) != (layer->flag & shared_flags))
      {
        other_layer->opacity = layer->opacity;
        other_layer->flag = (other_layer->flag & ~shared_flags) | (layer->flag & shared_flags);
        const int other_idx = BLI_findindex(&other_data->layers, other_layer);
        if (other_idx < other_data->active_layer_index) {
          BKE_tex_paint_cache_invalidate(*other_data);
        }
        BKE_tex_paint_composite_rect(other_node, nullptr);
        tex_paint_tag_redraw(other_node);
      }
    }
  }
}

static void rna_TexPaintLayer_redraw_update(Main * /*bmain*/, Scene * /*scene*/, PointerRNA *ptr)
{
  if (bNode *node = rna_TexPaintLayer_node(ptr)) {
    tex_paint_tag_redraw(*node);
  }
}

static void rna_TexPaintLayer_lock_update(Main *bmain, Scene * /*scene*/, PointerRNA *ptr)
{
  bNode *node = rna_TexPaintLayer_node(ptr);
  if (!node) {
    return;
  }
  const TexPaintLayer &layer = *ptr->data_as<TexPaintLayer>();
  bNodeTree &tree = *reinterpret_cast<bNodeTree *>(ptr->owner_id);
  rna_paint_pixels_update(bmain, nullptr, ptr);
  tex_paint_tag_redraw(*node);
  /* Independent Image Editor canvases share a container tree, not a channel stack. */
  if (!BKE_tex_paint_tree_shares_channel_layers(&tree)) {
    return;
  }
  for (bNode &other_node : tree.nodes) {
    if (&other_node == node || other_node.type_legacy != SH_NODE_TEX_PAINT ||
        !BKE_tex_paint_is_initialized(other_node))
    {
      continue;
    }
    NodeTexPaint &other_data = *static_cast<NodeTexPaint *>(other_node.storage);
    if (TexPaintLayer *other = BKE_tex_paint_layer_find_uid(other_data, layer.uid)) {
      other->flag = (other->flag & ~TEXPAINT_LAYER_LOCKED) | (layer.flag & TEXPAINT_LAYER_LOCKED);
      tex_paint_tag_redraw(other_node);
    }
  }
}

static void rna_TexPaintLayer_preview_mask_update(Main * /*bmain*/,
                                                  Scene * /*scene*/,
                                                  PointerRNA *ptr)
{
  bNode *node = rna_TexPaintLayer_node(ptr);
  if (node == nullptr) {
    return;
  }
  NodeTexPaint *data = static_cast<NodeTexPaint *>(node->storage);
  BKE_tex_paint_cache_invalidate(*data);
  BKE_tex_paint_composite_full(*node);
  tex_paint_tag_redraw(*node);
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

  bNodeTree *ntree = reinterpret_cast<bNodeTree *>(ptr->owner_id);
  if (BKE_tex_paint_tree_shares_channel_layers(ntree) && layer != nullptr) {
    for (bNode &other_node : ntree->nodes) {
      if (&other_node == node || other_node.type_legacy != SH_NODE_TEX_PAINT ||
          other_node.storage == nullptr || !BKE_tex_paint_is_initialized(other_node))
      {
        continue;
      }
      NodeTexPaint *other_data = static_cast<NodeTexPaint *>(other_node.storage);
      TexPaintLayer *other_layer = BKE_tex_paint_layer_find_uid(*other_data, layer->uid);
      if (other_layer == nullptr) {
        continue;
      }
      if ((other_layer->flag & TEXPAINT_LAYER_USE_MASK) != (layer->flag & TEXPAINT_LAYER_USE_MASK)) {
        other_layer->flag = (other_layer->flag & ~TEXPAINT_LAYER_USE_MASK) |
                            (layer->flag & TEXPAINT_LAYER_USE_MASK);
        if (other_layer->flag & TEXPAINT_LAYER_USE_MASK) {
          BKE_tex_paint_layer_ensure_mask_ibuf(*other_data, *other_layer);
        }
        const int other_idx = BLI_findindex(&other_data->layers, other_layer);
        if (other_idx < other_data->active_layer_index) {
          BKE_tex_paint_cache_invalidate(*other_data);
        }
        BKE_tex_paint_composite_rect(other_node, nullptr);
        tex_paint_tag_redraw(other_node);
      }
    }
  }
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
    {0, "NONE", ICON_BRUSH_DATA, "None (Baked)", "Standard baked paint layer without live projection"},
    {1, "FIT", ICON_IMAGE_PLANE, "UV Projection (Clamp)", "2D UV single stamp / fit"},
    {2, "UV_REPEAT", ICON_TEXTURE, "UV Projection (Tile)", "2D UV tile repeat"},
    {3,
     "TRIPLANAR",
     ICON_MESH_CUBE,
     "Triplanar Projection",
     "3D triplanar box projection with interactive 3D viewport widget"},
    {4,
     "DECAL",
     ICON_MOD_UVPROJECT,
     "Planar / Decal Projection",
     "3D directional planar decal projection with interactive 3D viewport widget"},
    {5,
     "WARP",
     ICON_MOD_WARP,
     "Warp Projection",
     "Substance-Painter-style 3D surface-curving warp projection"},
    {6,
     "SPHERICAL",
     ICON_MESH_UVSPHERE,
     "Spherical Projection",
     "3D spherical projection around the widget center"},
    {7,
     "CYLINDRICAL",
     ICON_MESH_CYLINDER,
     "Cylindrical Projection",
     "3D cylindrical projection around the widget Z axis"},
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

static void rna_def_tex_paint_color_filter(BlenderRNA *brna)
{
  static const EnumPropertyItem types[] = {
      {TEXPAINT_COLOR_FILTER_HSV, "HSV", 0, "Hue / Saturation / Value", "Adjust layer colors"},
      {TEXPAINT_COLOR_FILTER_BRIGHT_CONTRAST,
       "BRIGHT_CONTRAST",
       0,
       "Brightness / Contrast",
       "Adjust layer brightness and contrast"},
      {TEXPAINT_COLOR_FILTER_LEVELS,
       "LEVELS",
       0,
       "Levels / Gamma",
       "Remap input and output levels"},
      {TEXPAINT_COLOR_FILTER_INVERT, "INVERT", 0, "Invert", "Invert RGB without changing alpha"},
      {TEXPAINT_COLOR_FILTER_GRAYSCALE, "GRAYSCALE", 0, "Grayscale", "Convert RGB to luminance"},
      {TEXPAINT_COLOR_FILTER_BLUR,
       "BLUR",
       0,
       "Blur",
       "Soften layer colors and coverage without changing painted pixels"},
      {0, nullptr, 0, nullptr, nullptr},
  };
  StructRNA *srna = RNA_def_struct(brna, "TexPaintColorFilter", nullptr);
  RNA_def_struct_sdna(srna, "TexPaintColorFilter");
  RNA_def_struct_ui_text(
      srna, "Paint Layer Filter", "Non-destructive color filter on a paint or fill layer");
  RNA_def_struct_ui_icon(srna, ICON_MODIFIER);
  auto update = [](PropertyRNA *prop) {
    RNA_def_property_clear_flag(prop, PROP_ANIMATABLE);
    RNA_def_property_flag(prop, PROP_NO_DEG_UPDATE);
    RNA_def_property_update(prop, 0, "rna_TexPaintColorFilter_update");
  };
  PropertyRNA *prop = RNA_def_property(srna, "name", PROP_STRING, PROP_NONE);
  RNA_def_property_ui_text(prop, "Name", "Filter name");
  RNA_def_struct_name_property(srna, prop);
  update(prop);
  prop = RNA_def_property(srna, "type", PROP_ENUM, PROP_NONE);
  RNA_def_property_enum_items(prop, types);
  RNA_def_property_clear_flag(prop, PROP_EDITABLE | PROP_ANIMATABLE);
  RNA_def_property_ui_text(prop, "Type", "Filter operation");
  prop = RNA_def_property(srna, "enabled", PROP_BOOLEAN, PROP_NONE);
  RNA_def_property_boolean_sdna(prop, nullptr, "enabled", 0);
  RNA_def_property_boolean_default(prop, true);
  RNA_def_property_ui_text(prop, "Enabled", "Include this filter in the layer result");
  update(prop);
  prop = RNA_def_property(srna, "strength", PROP_FLOAT, PROP_FACTOR);
  RNA_def_property_range(prop, 0.0f, 1.0f);
  RNA_def_property_ui_text(prop, "Strength", "Blend the filter result with its input");
  update(prop);
  prop = RNA_def_property(srna, "hsv", PROP_FLOAT, PROP_NONE);
  RNA_def_property_array(prop, 3);
  RNA_def_property_range(prop, -1.0f, 1.0f);
  RNA_def_property_ui_text(prop, "HSV", "Hue, saturation and value offsets");
  update(prop);
  prop = RNA_def_property(srna, "bright_contrast", PROP_FLOAT, PROP_NONE);
  RNA_def_property_array(prop, 2);
  RNA_def_property_range(prop, -1.0f, 1.0f);
  RNA_def_property_ui_text(prop, "Brightness / Contrast", "Brightness and contrast offsets");
  update(prop);
  const char *names[] = {"input_black", "input_white", "gamma", "output_black", "output_white"};
  const char *labels[] = {"Input Black", "Input White", "Gamma", "Output Black", "Output White"};
  for (int i = 0; i < 5; i++) {
    prop = RNA_def_property(srna, names[i], PROP_FLOAT, PROP_NONE);
    const bool gamma = i == 2;
    RNA_def_property_range(prop, gamma ? 0.01f : 0.0f, gamma ? 10.0f : 1.0f);
    RNA_def_property_ui_text(prop, labels[i], "Levels parameter");
    update(prop);
  }
  prop = RNA_def_property(srna, "blur_radius", PROP_FLOAT, PROP_NONE);
  RNA_def_property_range(prop, 0.0f, 128.0f);
  RNA_def_property_ui_range(prop, 0.0f, 64.0f, 1.0f, 0);
  RNA_def_property_ui_text(prop, "Radius", "Blur radius in canvas pixels");
  update(prop);
}

static void rna_def_tex_paint_mask_effect(BlenderRNA *brna)
{
  StructRNA *srna = RNA_def_struct(brna, "TexPaintMaskEffect", nullptr);
  RNA_def_struct_sdna(srna, "TexPaintMaskEffect");
  RNA_def_struct_ui_text(srna, "Paint Mask Effect", "Owned operation in a layer's mask stack");
  const auto plain = [](PropertyRNA *prop) {
    RNA_def_property_clear_flag(prop, PROP_ANIMATABLE);
    RNA_def_property_flag(prop, PROP_NO_DEG_UPDATE);
  };
  PropertyRNA *prop = RNA_def_property(srna, "uid", PROP_INT, PROP_NONE);
  RNA_def_property_clear_flag(prop, PROP_EDITABLE | PROP_ANIMATABLE);
  RNA_def_property_ui_text(prop, "Identifier", "Stable identifier within the mask stack");
  prop = RNA_def_property(srna, "type", PROP_INT, PROP_NONE);
  RNA_def_property_range(prop, TEXPAINT_MASK_EFFECT_SOURCE, TEXPAINT_MASK_EFFECT_FILTER);
  RNA_def_property_ui_text(prop, "Type", "Prepared source, paint, levels, or spatial filter");
  plain(prop);
  prop = RNA_def_property(srna, "enabled", PROP_BOOLEAN, PROP_NONE);
  RNA_def_property_boolean_sdna(prop, nullptr, "enabled", 0);
  RNA_def_property_ui_text(prop, "Enabled", "Include the effect when evaluating the mask stack");
  plain(prop);
  prop = RNA_def_property(srna, "blend_mode", PROP_INT, PROP_NONE);
  RNA_def_property_range(prop, TEXPAINT_MASK_EFFECT_REPLACE, TEXPAINT_MASK_EFFECT_MIN);
  RNA_def_property_ui_text(prop, "Blend Mode", "Native mask blend operation");
  plain(prop);
  prop = RNA_def_property(srna, "opacity", PROP_FLOAT, PROP_FACTOR);
  RNA_def_property_range(prop, 0.0f, 1.0f);
  RNA_def_property_ui_text(prop, "Opacity", "Coverage of the effect result");
  plain(prop);
  prop = RNA_def_property(srna, "invert", PROP_BOOLEAN, PROP_NONE);
  RNA_def_property_boolean_sdna(prop, nullptr, "invert", 0);
  RNA_def_property_ui_text(prop, "Invert", "Invert paint or spatial filter values");
  plain(prop);
  prop = RNA_def_property(srna, "curve", PROP_FLOAT, PROP_NONE);
  RNA_def_property_array(prop, 1);
  RNA_def_property_flag(prop, PROP_DYNAMIC);
  RNA_def_property_dynamic_array_funcs(prop, "rna_TexPaintMaskEffect_curve_length");
  RNA_def_property_float_funcs(prop,
                               "rna_TexPaintMaskEffect_curve_get",
                               "rna_TexPaintMaskEffect_curve_set",
                               nullptr);
  RNA_def_property_range(prop, 0.0f, 1.0f);
  RNA_def_property_ui_text(prop, "Tone Curve", "Prepared normalized value for each byte input");
  plain(prop);
  prop = RNA_def_property(srna, "filter_type", PROP_INT, PROP_NONE);
  RNA_def_property_range(prop, TEXPAINT_MASK_FILTER_BLUR, TEXPAINT_MASK_FILTER_HIGHPASS);
  RNA_def_property_ui_text(prop, "Filter Type", "Native spatial filter operation");
  plain(prop);
  prop = RNA_def_property(srna, "filter_radius", PROP_FLOAT, PROP_NONE);
  RNA_def_property_range(prop, 0.0f, 128.0f);
  RNA_def_property_ui_text(prop, "Radius", "Spatial filter radius in pixels");
  plain(prop);
  prop = RNA_def_property(srna, "filter_strength", PROP_FLOAT, PROP_NONE);
  RNA_def_property_range(prop, -2.0f, 2.0f);
  RNA_def_property_ui_text(
      prop, "Strength", "Filter intensity, or signed growth for dilate/erode");
  plain(prop);
  prop = RNA_def_property(srna, "filter_scale", PROP_FLOAT, PROP_NONE);
  RNA_def_property_range(prop, 1.0f, 64.0f);
  RNA_def_property_ui_text(prop, "Scale", "Blur-slope noise frequency");
  plain(prop);
  prop = RNA_def_property(srna, "revision", PROP_INT, PROP_NONE);
  RNA_def_property_clear_flag(prop, PROP_EDITABLE | PROP_ANIMATABLE);
  RNA_def_property_ui_text(prop, "Revision", "Revision of the owned source or paint pixels");
  prop = RNA_def_property(srna, "pixels", PROP_FLOAT, PROP_NONE);
  RNA_def_property_array(prop, 1);
  RNA_def_property_flag(prop, PROP_DYNAMIC);
  RNA_def_property_dynamic_array_funcs(prop, "rna_TexPaintMaskEffect_pixels_length");
  RNA_def_property_float_funcs(
      prop, "rna_TexPaintMaskEffect_pixels_get", "rna_TexPaintMaskEffect_pixels_set", nullptr);
  RNA_def_property_ui_text(prop, "Pixels", "Owned source or paint pixels as straight RGBA");
  plain(prop);
}

static void rna_def_tex_paint_layer(BlenderRNA *brna)
{
  StructRNA *srna;
  PropertyRNA *prop;

  srna = RNA_def_struct(brna, "TexPaintLayer", nullptr);
  RNA_def_struct_ui_text(srna, "Texture Paint Layer", "Layer of a layered Texture Paint node");
  RNA_def_struct_sdna(srna, "TexPaintLayer");
  RNA_def_struct_path_func(srna, "rna_TexPaintLayer_path");
  RNA_def_struct_ui_icon(srna, ICON_RENDERLAYERS);

  /* Publishing a complete mask stack batches changes and composites only on explicit evaluation.
   */
  prop = RNA_def_property(srna, "mask_effects", PROP_COLLECTION, PROP_NONE);
  RNA_def_property_collection_sdna(prop, nullptr, "mask_effects", nullptr);
  RNA_def_property_struct_type(prop, "TexPaintMaskEffect");
  RNA_def_property_ui_text(prop, "Mask Effects", "Native owned effects in evaluation order");
  prop = RNA_def_property(srna, "active_mask_effect_uid", PROP_INT, PROP_NONE);
  RNA_def_property_range(prop, 0, INT_MAX);
  RNA_def_property_clear_flag(prop, PROP_ANIMATABLE);
  RNA_def_property_flag(prop, PROP_NO_DEG_UPDATE);
  RNA_def_property_ui_text(prop, "Active Mask Effect", "Selected mask effect for native painting");
  prop = RNA_def_property(srna, "mask_base_value", PROP_INT, PROP_NONE);
  RNA_def_property_range(prop, 0, 255);
  RNA_def_property_clear_flag(prop, PROP_ANIMATABLE);
  RNA_def_property_flag(prop, PROP_NO_DEG_UPDATE);
  RNA_def_property_ui_text(prop, "Mask Base Value", "Mask value before the first enabled effect");
  prop = RNA_def_property(srna, "mask_effects_active", PROP_BOOLEAN, PROP_NONE);
  RNA_def_property_boolean_sdna(prop, nullptr, "mask_effects_active", 0);
  RNA_def_property_clear_flag(prop, PROP_ANIMATABLE);
  RNA_def_property_flag(prop, PROP_NO_DEG_UPDATE);
  RNA_def_property_ui_text(
      prop, "Native Mask Stack", "Use native effect buffers as brush targets");
  prop = RNA_def_property(srna, "mask_pixels", PROP_FLOAT, PROP_NONE);
  RNA_def_property_array(prop, 1);
  RNA_def_property_flag(prop, PROP_DYNAMIC);
  RNA_def_property_clear_flag(prop, PROP_EDITABLE | PROP_ANIMATABLE);
  RNA_def_property_dynamic_array_funcs(prop, "rna_TexPaintLayer_mask_pixels_length");
  RNA_def_property_float_funcs(prop, "rna_TexPaintLayer_mask_pixels_get", nullptr, nullptr);
  RNA_def_property_ui_text(prop, "Mask Pixels", "Final evaluated layer mask as straight RGBA");

  FunctionRNA *func = RNA_def_function(
      srna, "mask_effect_ensure", "rna_TexPaintLayer_mask_effect_ensure");
  RNA_def_function_flag(func, FUNC_SELF_AS_RNA);
  RNA_def_function_ui_description(
      func, "Ensure a native mask effect and place it last in evaluation order");
  PropertyRNA *parm = RNA_def_int(
      func, "uid", 1, 1, INT_MAX, "Identifier", "Effect identifier", 1, INT_MAX);
  RNA_def_parameter_flags(parm, PropertyFlag(0), PARM_REQUIRED);
  parm = RNA_def_int(func,
                     "type",
                     0,
                     TEXPAINT_MASK_EFFECT_SOURCE,
                     TEXPAINT_MASK_EFFECT_FILTER,
                     "Type",
                     "Native effect type",
                     TEXPAINT_MASK_EFFECT_SOURCE,
                     TEXPAINT_MASK_EFFECT_FILTER);
  RNA_def_parameter_flags(parm, PropertyFlag(0), PARM_REQUIRED);
  parm = RNA_def_pointer(func, "effect", "TexPaintMaskEffect", "Effect", "Ensured native effect");
  RNA_def_parameter_flags(parm, PropertyFlag(0), PARM_RNAPTR);
  RNA_def_function_return(func, parm);
  func = RNA_def_function(srna, "mask_effect_remove", "rna_TexPaintLayer_mask_effect_remove");
  RNA_def_function_flag(func, FUNC_SELF_AS_RNA);
  parm = RNA_def_int(func, "uid", 1, 1, INT_MAX, "Identifier", "Effect to remove", 1, INT_MAX);
  RNA_def_parameter_flags(parm, PropertyFlag(0), PARM_REQUIRED);
  parm = RNA_def_boolean(func, "removed", false, "Removed", "The effect existed and was removed");
  RNA_def_function_return(func, parm);
  func = RNA_def_function(srna, "mask_effects_clear", "rna_TexPaintLayer_mask_effects_clear");
  RNA_def_function_flag(func, FUNC_SELF_AS_RNA);
  RNA_def_function_ui_description(func,
                                  "Release all owned native mask effects without compositing");
  func = RNA_def_function(srna, "mask_stack_evaluate", "rna_TexPaintLayer_mask_stack_evaluate");
  RNA_def_function_flag(func, FUNC_SELF_AS_RNA | FUNC_USE_MAIN);
  RNA_def_function_ui_description(
      func, "Evaluate the native mask stack and refresh its composited image");
  parm = RNA_def_boolean(
      func, "evaluated", false, "Evaluated", "The native mask stack was evaluated");
  RNA_def_function_return(func, parm);
  func = RNA_def_function(srna, "mask_stack_copy_from", "rna_TexPaintLayer_mask_stack_copy_from");
  RNA_def_function_flag(func, FUNC_SELF_AS_RNA | FUNC_USE_MAIN);
  RNA_def_function_ui_description(
      func, "Copy owned effects from another layer and evaluate the new mask stack");
  parm = RNA_def_pointer(
      func, "source", "TexPaintLayer", "Source", "Layer whose mask effects are copied");
  RNA_def_parameter_flags(parm, PROP_NEVER_NULL, PARM_REQUIRED | PARM_RNAPTR);
  parm = RNA_def_boolean(
      func, "evaluated", false, "Evaluated", "The copied native mask stack was evaluated");
  RNA_def_function_return(func, parm);

  func = RNA_def_function(srna, "rasterize", "rna_TexPaintLayer_rasterize");
  RNA_def_function_flag(func, FUNC_SELF_AS_RNA | FUNC_USE_MAIN);
  RNA_def_function_ui_description(
      func, "Convert a Fill to owned Paint pixels while preserving its mask and effects");
  RNA_def_boolean(
      func, "check_only", false, "Check Only", "Validate without replacing source data");
  parm = RNA_def_boolean(func, "result", false, "Result", "The conversion is supported");
  RNA_def_function_return(func, parm);

  prop = RNA_def_property(srna, "color_filters", PROP_COLLECTION, PROP_NONE);
  RNA_def_property_collection_sdna(prop, nullptr, "color_filters", nullptr);
  RNA_def_property_struct_type(prop, "TexPaintColorFilter");
  RNA_def_property_ui_text(
      prop, "Color Filters", "Non-destructive filters evaluated from top to bottom");
  prop = RNA_def_property(srna, "active_color_filter_index", PROP_INT, PROP_NONE);
  RNA_def_property_int_funcs(prop, nullptr, nullptr, "rna_TexPaintLayer_filter_index_range");
  RNA_def_property_clear_flag(prop, PROP_ANIMATABLE);
  RNA_def_property_flag(prop, PROP_NO_DEG_UPDATE);
  RNA_def_property_ui_text(prop, "Active Filter", "Selected color filter");
  RNA_def_property_update(prop, 0, "rna_TexPaintLayer_redraw_update");

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
  RNA_def_property_update(prop, 0, "rna_TexPaintLayer_lock_update");

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

  prop = RNA_def_property(srna, "preview_mask", PROP_BOOLEAN, PROP_NONE);
  RNA_def_property_boolean_sdna(prop, nullptr, "flag", TEXPAINT_LAYER_PREVIEW_MASK);
  RNA_def_property_ui_icon(prop, ICON_HIDE_OFF, 0);
  RNA_def_property_ui_text(
      prop,
      "Inspect Mask on Surface",
      "Temporarily display this layer's grayscale mask directly on the 3D surface and 2D canvas");
  RNA_def_property_flag(prop, PROP_NO_DEG_UPDATE);
  RNA_def_property_update(prop, 0, "rna_TexPaintLayer_preview_mask_update");

  prop = RNA_def_property(srna, "fill_color", PROP_FLOAT, PROP_COLOR);
  RNA_def_property_array(prop, 4);
  RNA_def_property_range(prop, 0.0f, FLT_MAX);
  RNA_def_property_ui_range(prop, 0.0f, 1.0f, 1, 3);
  RNA_def_property_ui_text(prop, "Fill Color", "Color of a fill layer, in scene linear space");
  RNA_def_property_flag(prop, PROP_NO_DEG_UPDATE);
  RNA_def_property_update(prop, 0, "rna_TexPaintLayer_update");

  /* Substance Painter-style multi-channel flags and values on each layer. */
  prop = RNA_def_property(srna, "use_channel_color", PROP_BOOLEAN, PROP_NONE);
  RNA_def_property_boolean_sdna(prop, nullptr, "channel_flags", TEXPAINT_CHAN_COLOR);
  RNA_def_property_ui_text(prop, "Color Channel", "Affect the Base Color channel on this layer");
  RNA_def_property_flag(prop, PROP_NO_DEG_UPDATE);
  RNA_def_property_update(prop, 0, "rna_TexPaintLayer_update");

  prop = RNA_def_property(srna, "use_channel_roughness", PROP_BOOLEAN, PROP_NONE);
  RNA_def_property_boolean_sdna(prop, nullptr, "channel_flags", TEXPAINT_CHAN_ROUGHNESS);
  RNA_def_property_ui_text(prop, "Roughness Channel", "Affect the Roughness channel on this layer");
  RNA_def_property_flag(prop, PROP_NO_DEG_UPDATE);
  RNA_def_property_update(prop, 0, "rna_TexPaintLayer_update");

  prop = RNA_def_property(srna, "use_channel_metallic", PROP_BOOLEAN, PROP_NONE);
  RNA_def_property_boolean_sdna(prop, nullptr, "channel_flags", TEXPAINT_CHAN_METALLIC);
  RNA_def_property_ui_text(prop, "Metallic Channel", "Affect the Metallic channel on this layer");
  RNA_def_property_flag(prop, PROP_NO_DEG_UPDATE);
  RNA_def_property_update(prop, 0, "rna_TexPaintLayer_update");

  prop = RNA_def_property(srna, "use_channel_height", PROP_BOOLEAN, PROP_NONE);
  RNA_def_property_boolean_sdna(prop, nullptr, "channel_flags", TEXPAINT_CHAN_HEIGHT);
  RNA_def_property_ui_text(
      prop, "Height Channel", "Affect the Height / Normal Bump channel on this layer");
  RNA_def_property_flag(prop, PROP_NO_DEG_UPDATE);
  RNA_def_property_update(prop, 0, "rna_TexPaintLayer_update");

  prop = RNA_def_property(srna, "use_channel_normal", PROP_BOOLEAN, PROP_NONE);
  RNA_def_property_boolean_sdna(prop, nullptr, "channel_flags", TEXPAINT_CHAN_NORMAL);
  RNA_def_property_ui_text(
      prop,
      "Normal Channel",
      "Affect the Normal channel on this layer (flattens or replaces underlying height bump)");
  RNA_def_property_flag(prop, PROP_NO_DEG_UPDATE);
  RNA_def_property_update(prop, 0, "rna_TexPaintLayer_update");

  prop = RNA_def_property(srna, "use_channel_emission", PROP_BOOLEAN, PROP_NONE);
  RNA_def_property_boolean_sdna(prop, nullptr, "channel_flags", TEXPAINT_CHAN_EMISSION);
  RNA_def_property_ui_text(prop, "Emission Channel", "Affect the Emission channel on this layer");
  RNA_def_property_flag(prop, PROP_NO_DEG_UPDATE);
  RNA_def_property_update(prop, 0, "rna_TexPaintLayer_update");

  prop = RNA_def_property(srna, "use_channel_specular", PROP_BOOLEAN, PROP_NONE);
  RNA_def_property_boolean_sdna(prop, nullptr, "channel_flags", TEXPAINT_CHAN_SPECULAR);
  RNA_def_property_ui_text(prop, "Specular Channel", "Affect the Specular channel on this layer");
  RNA_def_property_flag(prop, PROP_NO_DEG_UPDATE);
  RNA_def_property_update(prop, 0, "rna_TexPaintLayer_update");

  prop = RNA_def_property(srna, "fill_roughness", PROP_FLOAT, PROP_FACTOR);
  RNA_def_property_float_sdna(prop, nullptr, "fill_roughness");
  RNA_def_property_range(prop, 0.0f, 1.0f);
  RNA_def_property_ui_text(prop, "Roughness", "Roughness value for this layer");
  RNA_def_property_flag(prop, PROP_NO_DEG_UPDATE);
  RNA_def_property_update(prop, 0, "rna_TexPaintLayer_update");

  prop = RNA_def_property(srna, "fill_metallic", PROP_FLOAT, PROP_FACTOR);
  RNA_def_property_float_sdna(prop, nullptr, "fill_metallic");
  RNA_def_property_range(prop, 0.0f, 1.0f);
  RNA_def_property_ui_text(prop, "Metallic", "Metallic value for this layer");
  RNA_def_property_flag(prop, PROP_NO_DEG_UPDATE);
  RNA_def_property_update(prop, 0, "rna_TexPaintLayer_update");

  prop = RNA_def_property(srna, "fill_height", PROP_FLOAT, PROP_FACTOR);
  RNA_def_property_float_sdna(prop, nullptr, "fill_height");
  RNA_def_property_range(prop, -1.0f, 1.0f);
  RNA_def_property_ui_text(
      prop, "Height", "Signed height / bump offset (-1 recessed to +1 raised) for this layer");
  RNA_def_property_flag(prop, PROP_NO_DEG_UPDATE);
  RNA_def_property_update(prop, 0, "rna_TexPaintLayer_update");

  prop = RNA_def_property(srna, "fill_specular", PROP_FLOAT, PROP_FACTOR);
  RNA_def_property_float_sdna(prop, nullptr, "fill_specular");
  RNA_def_property_range(prop, 0.0f, 1.0f);
  RNA_def_property_ui_text(prop, "Specular", "Specular IOR level for this layer");
  RNA_def_property_flag(prop, PROP_NO_DEG_UPDATE);
  RNA_def_property_update(prop, 0, "rna_TexPaintLayer_update");

  prop = RNA_def_property(srna, "fill_emission", PROP_FLOAT, PROP_COLOR);
  RNA_def_property_float_sdna(prop, nullptr, "fill_emission");
  RNA_def_property_array(prop, 3);
  RNA_def_property_range(prop, 0.0f, FLT_MAX);
  RNA_def_property_ui_range(prop, 0.0f, 1.0f, 1, 3);
  RNA_def_property_ui_text(prop, "Emission", "Emission color for this layer");
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

  FunctionRNA *func = RNA_def_function(
      srna, "make_images_single_user", "rna_ShaderNodeTexPaint_make_images_single_user");
  RNA_def_function_flag(func, FUNC_SELF_AS_RNA | FUNC_USE_MAIN);
  RNA_def_function_ui_description(func,
                                  "Copy this node's composited images with independent ownership");
  PropertyRNA *result = RNA_def_boolean(func, "result", false, "Result", "Images were copied");
  RNA_def_function_return(func, result);

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

  prop = RNA_def_property(srna, "selection_active", PROP_BOOLEAN, PROP_NONE);
  RNA_def_property_boolean_sdna(prop, nullptr, "selection_active", 1);
  RNA_def_property_ui_text(
      prop, "Selection Active", "Restrict editing even when the selected area is empty");
  RNA_def_property_update(prop, NC_IMAGE | ND_DRAW, "rna_paint_selection_update");

  prop = RNA_def_property(srna, "selection_mask", PROP_FLOAT, PROP_NONE);
  RNA_def_property_flag(prop, PROP_DYNAMIC | PROP_NO_DEG_UPDATE);
  RNA_def_property_multi_array(prop, 1, nullptr);
  RNA_def_property_dynamic_array_funcs(prop, "rna_paint_selection_length");
  RNA_def_property_float_funcs(
      prop, "rna_paint_selection_get", "rna_paint_selection_set", nullptr);
  RNA_def_property_ui_text(
      prop, "Selection Coverage", "Canvas selection coverage, including empty selections");
  RNA_def_property_update(prop, NC_IMAGE | ND_DRAW, "rna_paint_pixels_update");

  prop = RNA_def_property(srna, "paint_pixels", PROP_FLOAT, PROP_NONE);
  RNA_def_property_flag(prop, PROP_DYNAMIC | PROP_NO_DEG_UPDATE);
  RNA_def_property_multi_array(prop, 1, nullptr);
  RNA_def_property_dynamic_array_funcs(prop, "rna_paint_pixels_length");
  RNA_def_property_float_funcs(prop, "rna_paint_pixels_get", "rna_paint_pixels_set", nullptr);
  RNA_def_property_ui_text(
      prop,
      "Paint Pixels",
      "Straight RGBA of the editable active layer or mask in canvas color space");
  RNA_def_property_update(prop, NC_IMAGE | NA_EDITED, "rna_paint_pixels_update");

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

  prop = RNA_def_property(srna, "pbr_image", PROP_POINTER, PROP_NONE);
  RNA_def_property_pointer_sdna(prop, nullptr, "pbr_image");
  RNA_def_property_struct_type(prop, "Image");
  RNA_def_property_clear_flag(prop, PROP_EDITABLE);
  RNA_def_property_ui_text(
      prop,
      "PBR Packed Image",
      "Packed Non-Color RGBA image holding Roughness (R), Metallic (G), Height (B), Specular (A)");

  prop = RNA_def_property(srna, "emission_image", PROP_POINTER, PROP_NONE);
  RNA_def_property_pointer_sdna(prop, nullptr, "emission_image");
  RNA_def_property_struct_type(prop, "Image");
  RNA_def_property_clear_flag(prop, PROP_EDITABLE);
  RNA_def_property_ui_text(prop, "Emission Image", "Composited Emission image");

  prop = RNA_def_property(srna, "base_roughness", PROP_FLOAT, PROP_FACTOR);
  RNA_def_property_float_sdna(prop, nullptr, "base_roughness");
  RNA_def_property_range(prop, 0.0f, 1.0f);
  RNA_def_property_ui_text(
      prop, "Base Roughness", "Default roughness where no layer overrides Roughness");
  RNA_def_property_flag(prop, PROP_NO_DEG_UPDATE);
  RNA_def_property_update(prop, 0, "rna_ShaderNodeTexPaint_pbr_update");

  prop = RNA_def_property(srna, "base_metallic", PROP_FLOAT, PROP_FACTOR);
  RNA_def_property_float_sdna(prop, nullptr, "base_metallic");
  RNA_def_property_range(prop, 0.0f, 1.0f);
  RNA_def_property_ui_text(
      prop, "Base Metallic", "Default metallic value where no layer overrides Metallic");
  RNA_def_property_flag(prop, PROP_NO_DEG_UPDATE);
  RNA_def_property_update(prop, 0, "rna_ShaderNodeTexPaint_pbr_update");

  prop = RNA_def_property(srna, "base_specular", PROP_FLOAT, PROP_FACTOR);
  RNA_def_property_float_sdna(prop, nullptr, "base_specular");
  RNA_def_property_range(prop, 0.0f, 1.0f);
  RNA_def_property_ui_text(
      prop, "Base Specular", "Default specular value where no layer overrides Specular");
  RNA_def_property_flag(prop, PROP_NO_DEG_UPDATE);
  RNA_def_property_update(prop, 0, "rna_ShaderNodeTexPaint_pbr_update");

  prop = RNA_def_property(srna, "bump_strength", PROP_FLOAT, PROP_FACTOR);
  RNA_def_property_float_sdna(prop, nullptr, "bump_strength");
  RNA_def_property_range(prop, 0.0f, 2.0f);
  RNA_def_property_ui_text(
      prop, "Height Bump Strength", "Strength of the 3D normal bump generated from the Height channel");
  RNA_def_property_update(prop, 0, "rna_Node_update");
}

void RNA_def_node_tex_paint(BlenderRNA *brna)
{
  rna_def_tex_paint_color_filter(brna);
  rna_def_tex_paint_mask_effect(brna);
  rna_def_tex_paint_layer(brna);
}

}  // namespace blender

#endif /* RNA_RUNTIME */
