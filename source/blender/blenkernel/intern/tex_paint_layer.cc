/* SPDX-FileCopyrightText: 2026 Blender Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

/** \file
 * \ingroup bke
 *
 * Layer stack, packing and dirty-rectangle compositing of the layered Texture Paint node.
 *
 * Pixel conventions follow #ImBuf: byte buffers hold straight alpha in the image color space
 * (usually sRGB), float buffers hold premultiplied alpha in scene linear space. Compositing
 * happens in straight alpha with 32-bit floats and is written back in the buffer convention.
 */

#include <cmath>
#include <cstdio>
#include <cstring>
#include <memory>

#include <fmt/format.h>

#include "MEM_guardedalloc.h"

#include "DNA_image_types.h"
#include "DNA_mesh_types.h"
#include "DNA_node_tex_paint_types.h"
#include "DNA_node_types.h"
#include "DNA_packedFile_types.h"
#include "DNA_scene_types.h"

#include "BLI_array.hh"
#include "BLI_implicit_sharing.hh"
#include "BLI_listbase.hh"
#include "BLI_math_base_c.hh"
#include "BLI_math_color_c.hh"
#include "BLI_math_matrix.hh"
#include "BLI_math_vector.hh"
#include "BLI_math_vector_c.hh"
#include "BLI_math_vector_types.hh"
#include "BLI_rect.hh"
#include "BLI_string.hh"
#include "BLI_string_utf8.hh"
#include "BLI_string_utils.hh"
#include "BLI_task.hh"
#include "BLI_vector.hh"

#include "BLT_translation.hh"

#include "BKE_attribute.hh"
#include "BKE_image.hh"
#include "BKE_lib_id.hh"
#include "BKE_main.hh"
#include "BKE_mesh.hh"
#include "BKE_node.hh"
#include "BKE_node_legacy_types.hh"
#include "BKE_node_runtime.hh"
#include "BKE_packedFile.hh"
#include "BKE_tex_paint_layer.hh"
#include "BKE_tex_paint_mask.hh"

#include "IMB_colormanagement.hh"
#include "IMB_imbuf.hh"
#include "IMB_imbuf_types.hh"
#include "IMB_partial_update.hh"

#include "BLO_read_write.hh"

#include "CLG_log.h"

static CLG_LogRef LOG = {"bke.tex_paint"};

namespace blender {

/* -------------------------------------------------------------------- */
/** \name Runtime Data
 * \{ */

namespace bke::tex_paint {

/** Baked UV-space mesh surface sample for real-time 3D projection and Smart Mask generation. */
struct UVTexel {
  uint32_t pixel_idx;
  float3 pos;
  float3 normal;
  float curv_fine;
  float curv_broad;
  float ao_near;
  float ao_far;
};

/** 32x32 UV-space tile with precomputed 3D object-space AABB and covered texels. */
struct UVTile {
  rcti rect = {0, 0, 0, 0};
  float3 bbox_min = float3(FLT_MAX);
  float3 bbox_max = float3(-FLT_MAX);
  Vector<UVTexel> texels;
  bool has_prev_pixels = false;
};

/** Derived pixels of a layer containing spatial effects; never used as a paint target. */
struct LayerFilterCache {
  int layer_uid = 0;
  int width = 0, height = 0;
  const ImBuf *source = nullptr;
  uint64_t settings_hash = 0;
  bool valid = false;
  bool dirty = false;
  rcti dirty_rect = {0, 0, 0, 0};
  Array<float4, 0> pixels;
};

/** Runtime state owned by #NodeTexPaint::runtime. */
struct TexPaintRuntime {
  Vector<int2> selection_contours;
  bool selection_contours_valid = false;
  /**
   * Flattened composite of layers `[0, cache_active_index)` in the canvas buffer format.
   * Only allocated when the active layer is not the bottom layer.
   */
  ImBuf *cache_below = nullptr;
  int cache_active_index = -1;
  bool cache_valid = false;
  Vector<LayerFilterCache> filtered_layers;
  bool effects_dirty = false;
  rcti effects_dirty_rect = {0, 0, 0, 0};

  /**
   * Master image buffer that was last fully composited. Used to detect buffers re-created by the
   * image system (file load, reload, resize), which need a full composite again.
   */
  const ImBuf *composited_ibuf = nullptr;
  const void *composited_data = nullptr;
  const ImBuf *pbr_composited_ibuf = nullptr;

  /**
   * Pre-baked 32x32 UV-space mesh position/normal/curvature/AO tiles for real-time multi-threaded
   * 3D Decal & Triplanar projection and Smart Mask generation. Rebuilt only when the mesh, UVs, or
   * canvas resolution changes.
   */
  const Mesh *uv_bake_mesh = nullptr;
  const float3 *uv_bake_positions_ptr = nullptr;
  const int3 *uv_bake_tris_ptr = nullptr;
  const float2 *uv_bake_uv_ptr = nullptr;
  int uv_bake_verts_num = 0;
  int uv_bake_tris_num = 0;
  int uv_bake_w = 0;
  int uv_bake_h = 0;
  bool uv_bake_has_signals = false;
  float3 uv_bake_bbox_min = float3(-0.5f);
  float3 uv_bake_bbox_max = float3(0.5f);
  float3 uv_bake_bbox_center = float3(0.0f);
  float uv_bake_max_extent = 1.0f;
  Array<UVTile> uv_tiles;
  int last_proj_layer_uid = 0;

  /** Snapshot of hand-painted mask values before non-replace Smart Mask generator blending. */
  Array<float> mask_base_values;
  int mask_base_layer_uid = 0;
};

static TexPaintRuntime &runtime_ensure(NodeTexPaint &data)
{
  if (data.runtime == nullptr) {
    data.runtime = MEM_new<TexPaintRuntime>(__func__);
  }
  return *static_cast<TexPaintRuntime *>(data.runtime);
}

static void runtime_free(NodeTexPaint &data)
{
  TexPaintRuntime *runtime = static_cast<TexPaintRuntime *>(data.runtime);
  if (runtime == nullptr) {
    return;
  }
  if (runtime->cache_below) {
    IMB_freeImBuf(runtime->cache_below);
  }
  MEM_delete(runtime);
  data.runtime = nullptr;
}

}  // namespace bke::tex_paint

using namespace bke::tex_paint;

void BKE_tex_paint_selection_tag_changed(NodeTexPaint &data)
{
  if (data.runtime) {
    static_cast<TexPaintRuntime *>(data.runtime)->selection_contours_valid = false;
  }
}

Span<int2> BKE_tex_paint_selection_contours(NodeTexPaint &data)
{
  const Span<float> selection = BKE_tex_paint_selection(data);
  if (selection.is_empty()) {
    return {};
  }
  TexPaintRuntime &runtime = runtime_ensure(data);
  if (runtime.selection_contours_valid) {
    return runtime.selection_contours;
  }
  Vector<int2> &lines = runtime.selection_contours;
  lines.clear();
  auto selected = [&](int x, int y) {
    return x >= 0 && y >= 0 && x < data.width && y < data.height &&
           selection[y * data.width + x] >= 0.5f;
  };
  /* Merge adjacent edges into runs. Zooming/panning only projects these cached endpoints. */
  for (int y = 0; y <= data.height; y++) {
    int start = -1;
    for (int x = 0; x <= data.width; x++) {
      const bool edge = x < data.width && selected(x, y - 1) != selected(x, y);
      if (edge && start < 0) {
        start = x;
      }
      else if (!edge && start >= 0) {
        lines.append(int2(start, y));
        lines.append(int2(x, y));
        start = -1;
      }
    }
  }
  for (int x = 0; x <= data.width; x++) {
    int start = -1;
    for (int y = 0; y <= data.height; y++) {
      const bool edge = y < data.height && selected(x - 1, y) != selected(x, y);
      if (edge && start < 0) {
        start = y;
      }
      else if (!edge && start >= 0) {
        lines.append(int2(x, start));
        lines.append(int2(x, y));
        start = -1;
      }
    }
  }
  runtime.selection_contours_valid = true;
  return lines;
}

void BKE_tex_paint_cache_invalidate(NodeTexPaint &data)
{
  if (data.runtime == nullptr) {
    return;
  }
  TexPaintRuntime &runtime = *static_cast<TexPaintRuntime *>(data.runtime);
  runtime.cache_valid = false;
  runtime.cache_active_index = -1;
  for (LayerFilterCache &cache : runtime.filtered_layers) {
    cache.valid = false;
  }
}

/** \} */

/* -------------------------------------------------------------------- */
/** \name Buffer Helpers
 * \{ */

static void layer_free_buffers(TexPaintLayer &layer)
{
  if (layer.ibuf) {
    IMB_freeImBuf(layer.ibuf);
    layer.ibuf = nullptr;
  }
  if (layer.mask_ibuf) {
    IMB_freeImBuf(layer.mask_ibuf);
    layer.mask_ibuf = nullptr;
  }
  if (layer.pbr_ibuf) {
    IMB_freeImBuf(layer.pbr_ibuf);
    layer.pbr_ibuf = nullptr;
  }
}

static void layer_free_packed(TexPaintLayer &layer)
{
  if (layer.pf) {
    BKE_packedfile_free(layer.pf);
    layer.pf = nullptr;
  }
  if (layer.mask_pf) {
    BKE_packedfile_free(layer.mask_pf);
    layer.mask_pf = nullptr;
  }
  if (layer.pbr_pf) {
    BKE_packedfile_free(layer.pbr_pf);
    layer.pbr_pf = nullptr;
  }
}

static void layer_filters_clear(TexPaintLayer &layer)
{
  for (TexPaintColorFilter &filter : layer.color_filters.items_mutable()) {
    MEM_delete(&filter);
  }
  layer.color_filters.clear_no_delete();
  layer.active_color_filter_index = 0;
}

static void layer_filters_copy(TexPaintLayer &dst, const TexPaintLayer &src)
{
  dst.color_filters.clear_no_delete();
  for (const TexPaintColorFilter &filter : src.color_filters) {
    TexPaintColorFilter *copy = MEM_new<TexPaintColorFilter>(__func__, dna::shallow_copy(filter));
    copy->next = copy->prev = nullptr;
    BLI_addtail(&dst.color_filters, copy);
  }
}

static void layer_free(TexPaintLayer *layer)
{
  BKE_tex_paint_mask_effects_clear(*layer);
  layer_free_buffers(*layer);
  layer_free_packed(*layer);
  layer_filters_clear(*layer);
  MEM_delete(layer);
}

static ImBuf *canvas_buffer_alloc(const NodeTexPaint &data)
{
  const ImBufFlags flags = data.is_float ? ImBufFlags::FloatData : ImBufFlags::ByteData;
  ImBuf *ibuf = IMB_allocImBuf(data.width, data.height, flags);
  if (ibuf == nullptr) {
    return nullptr;
  }
  const size_t pixels = IMB_get_pixel_count(ibuf);
  if (data.is_float) {
    memset(ibuf->float_data_for_write(), 0, pixels * 4 * sizeof(float));
  }
  else {
    memset(ibuf->byte_data_for_write(), 0, pixels * 4);
  }
  return ibuf;
}

/** Convert a scene linear straight-alpha color to the canvas storage convention. */
static float4 canvas_color_from_scene_linear(const NodeTexPaint &data, const float color[4])
{
  float4 result(color);
  if (!data.is_float) {
    IMB_colormanagement_scene_linear_to_srgb_v3(result, color);
  }
  return result;
}

/** Fill a canvas buffer with a straight-alpha color expressed in canvas space. */
static void canvas_buffer_fill(ImBuf &ibuf, const float4 &straight)
{
  const size_t pixels = IMB_get_pixel_count(&ibuf);
  if (float *dst = ibuf.float_data_for_write()) {
    float4 premul;
    straight_to_premul_v4_v4(premul, straight);
    threading::parallel_for(IndexRange(pixels), 4096, [&](const IndexRange range) {
      for (const int64_t i : range) {
        copy_v4_v4(dst + i * 4, premul);
      }
    });
  }
  else if (uint8_t *dst = ibuf.byte_data_for_write()) {
    uchar col[4];
    unit_float_to_uchar_clamp_v4(col, straight);
    threading::parallel_for(IndexRange(pixels), 4096, [&](const IndexRange range) {
      for (const int64_t i : range) {
        memcpy(dst + i * 4, col, 4);
      }
    });
  }
}

static bool buffer_matches_canvas(const NodeTexPaint &data, const ImBuf *ibuf)
{
  if (ibuf == nullptr || ibuf->x != data.width || ibuf->y != data.height) {
    return false;
  }
  return data.is_float ? (ibuf->float_data() != nullptr && ELEM(ibuf->channels, 0, 4)) :
                         (ibuf->byte_data() != nullptr);
}

/** \} */

/* -------------------------------------------------------------------- */
/** \name Packing
 * \{ */

static PackedFile *pack_buffer(ImBuf &ibuf, const bool is_float)
{
  ibuf.ftype = is_float ? IMB_FTYPE_OPENEXR : IMB_FTYPE_PNG;
  if (is_float) {
    /* Lossless full float with ZIP compression. */
    ibuf.foptions.flag = R_IMF_EXR_CODEC_ZIP;
  }
  else {
    /* Fast PNG compression level, layers are re-encoded on every save. */
    ibuf.foptions.flag = 0;
    ibuf.foptions.compress = 15;
  }

  Vector<uint8_t> encoded = IMB_save_image_to_buffer(
      &ibuf, is_float ? ImBufFlags::FloatData : ImBufFlags::ByteData);
  if (encoded.is_empty()) {
    CLOG_ERROR(&LOG, "Failed to encode texture paint layer");
    return nullptr;
  }
  auto *shared_data = new ImplicitSharedValue<Vector<uint8_t>>(std::move(encoded));
  return BKE_packedfile_new_from_memory(
      shared_data->data.data(), int(shared_data->data.size()), shared_data);
}

static ImBuf *unpack_buffer(const NodeTexPaint &data, const PackedFile *pf)
{
  if (pf == nullptr || pf->data == nullptr || pf->size <= 0) {
    return nullptr;
  }
  /* EXR stores our premultiplied floats, whereas PNG stores straight bytes.
   * Detect the format's alpha convention so a reload does not multiply floats twice. */
  const ImBufFlags flags = (data.is_float ? ImBufFlags::FloatData : ImBufFlags::ByteData) |
                          ImBufFlags::AlphaDetect;
  ImBuf *ibuf = IMB_load_image_from_memory(
      static_cast<const uchar *>(pf->data), size_t(pf->size), flags, "tex_paint_layer");
  if (ibuf == nullptr) {
    CLOG_ERROR(&LOG, "Failed to decode texture paint layer");
    return nullptr;
  }
  if (data.is_float && ibuf->float_data() == nullptr) {
    IMB_float_from_byte(ibuf);
  }
  if (!data.is_float && ibuf->byte_data() == nullptr) {
    IMB_byte_from_float(ibuf);
  }
  if (ibuf->x != data.width || ibuf->y != data.height) {
    IMB_scale(ibuf, data.width, data.height, IMBScaleFilter::Bilinear);
  }
  return ibuf;
}

/** \} */

/* -------------------------------------------------------------------- */
/** \name Storage Lifecycle
 * \{ */

void BKE_tex_paint_storage_free_contents(NodeTexPaint &data)
{
  BKE_tex_paint_selection_clear(data);
  for (TexPaintLayer &layer : data.layers.items_mutable()) {
    layer_free(&layer);
  }
  data.layers.clear_no_delete();
  runtime_free(data);
}

void BKE_tex_paint_storage_copy_contents(NodeTexPaint &dst,
                                         const NodeTexPaint &src,
                                         const bool copy_pixels)
{
  dst.layers.clear_no_delete();
  dst.runtime = nullptr;
  dst.selection = nullptr;
  dst.selection_size = 0;
  dst.selection_active = 0;
  dst.selection_transform_source = nullptr;
  dst.selection_transform_mask = nullptr;
  dst.selection_transform_size = 0;
  dst.selection_transform_layer_uid = 0;
  if (copy_pixels && !BKE_tex_paint_selection(src).is_empty()) {
    BKE_tex_paint_selection_set(dst, BKE_tex_paint_selection(src));
  }
  if (copy_pixels && src.selection_transform_source && src.selection_transform_size > 0) {
    BKE_tex_paint_selection_transform_store(
        dst,
        {reinterpret_cast<const float4 *>(src.selection_transform_source),
         src.selection_transform_size},
        src.selection_transform_mask ?
            Span<float>(src.selection_transform_mask, src.selection_transform_size) :
            Span<float>());
    std::copy_n(&src.selection_transform[0][0], 9, &dst.selection_transform[0][0]);
    dst.selection_transform_layer_uid = src.selection_transform_layer_uid;
    dst.selection_transform_target = src.selection_transform_target;
    dst.selection_transform_hash = src.selection_transform_hash;
  }
  for (const TexPaintLayer &src_layer : src.layers) {
    TexPaintLayer *layer = MEM_new<TexPaintLayer>(__func__, dna::shallow_copy(src_layer));
    layer->next = layer->prev = nullptr;
    layer->pf = nullptr;
    layer->mask_pf = nullptr;
    layer->pbr_pf = nullptr;
    layer->ibuf = nullptr;
    layer->mask_ibuf = nullptr;
    layer->pbr_ibuf = nullptr;
    layer->runtime_dirty = 0;
    layer_filters_copy(*layer, src_layer);
    BKE_tex_paint_mask_effects_copy(*layer, src_layer, copy_pixels);
    if (copy_pixels) {
      /* Packed data is implicitly shared, so this is cheap. */
      layer->pf = src_layer.pf ? BKE_packedfile_duplicate(src_layer.pf) : nullptr;
      layer->mask_pf = src_layer.mask_pf ? BKE_packedfile_duplicate(src_layer.mask_pf) : nullptr;
      layer->pbr_pf = src_layer.pbr_pf ? BKE_packedfile_duplicate(src_layer.pbr_pf) : nullptr;
      layer->ibuf = src_layer.ibuf ? IMB_dupImBuf(src_layer.ibuf) : nullptr;
      layer->mask_ibuf = src_layer.mask_ibuf ? IMB_dupImBuf(src_layer.mask_ibuf) : nullptr;
      layer->pbr_ibuf = src_layer.pbr_ibuf ? IMB_dupImBuf(src_layer.pbr_ibuf) : nullptr;
      layer->runtime_dirty = src_layer.runtime_dirty;
    }
    BLI_addtail(&dst.layers, layer);
  }
}

/** Re-encode layers whose pixels changed since they were last packed. */
static void layer_update_packed(const NodeTexPaint &data, TexPaintLayer &layer)
{
  if (!layer.runtime_dirty) {
    return;
  }
  if (layer.ibuf) {
    if (PackedFile *pf = pack_buffer(*layer.ibuf, data.is_float)) {
      if (layer.pf) {
        BKE_packedfile_free(layer.pf);
      }
      layer.pf = pf;
    }
  }
  if (layer.mask_ibuf) {
    if (PackedFile *pf = pack_buffer(*layer.mask_ibuf, data.is_float)) {
      if (layer.mask_pf) {
        BKE_packedfile_free(layer.mask_pf);
      }
      layer.mask_pf = pf;
    }
  }
  if (layer.pbr_ibuf) {
    if (PackedFile *pf = pack_buffer(*layer.pbr_ibuf, data.is_float)) {
      if (layer.pbr_pf) {
        BKE_packedfile_free(layer.pbr_pf);
      }
      layer.pbr_pf = pf;
    }
  }
  layer.runtime_dirty = 0;
}

void BKE_tex_paint_blend_write(BlendWriter &writer, const NodeTexPaint &data)
{
  if (data.selection_transform_source) {
    writer.write_float_array(int64_t(data.selection_transform_size) * 4,
                             data.selection_transform_source);
    if (data.selection_transform_mask) {
      writer.write_float_array(data.selection_transform_size, data.selection_transform_mask);
    }
  }
  if (data.selection) {
    writer.write_float_array(data.selection_size, data.selection);
  }
  for (const TexPaintLayer &layer_const : data.layers) {
    /* Encoding only updates the persistent cache of the runtime pixels. */
    layer_update_packed(data, const_cast<TexPaintLayer &>(layer_const));
  }
  writer.write_struct_list(&data.layers);
  for (const TexPaintLayer &layer : data.layers) {
    writer.write_struct_list(&layer.color_filters);
    BKE_tex_paint_mask_effects_blend_write(writer, data, layer);
    if (layer.pf) {
      BKE_packedfile_blend_write(&writer, layer.pf);
    }
    if (layer.mask_pf) {
      BKE_packedfile_blend_write(&writer, layer.mask_pf);
    }
    if (layer.pbr_pf) {
      BKE_packedfile_blend_write(&writer, layer.pbr_pf);
    }
  }
}

void BKE_tex_paint_blend_read_data(BlendDataReader &reader, NodeTexPaint &data)
{
  data.runtime = nullptr;
  /* Remap owned arrays before any cleanup; an unrestricted selection may still have a
   * whole-layer transform source. */
  const bool transform_source_read = BLO_read_array(
      &reader, &data.selection_transform_source, int64_t(data.selection_transform_size) * 4);
  const bool transform_mask_read = data.selection_transform_mask == nullptr ||
                                   BLO_read_array(&reader,
                                                  &data.selection_transform_mask,
                                                  data.selection_transform_size);
  if (!transform_source_read || !transform_mask_read || !data.selection_transform_source ||
      int64_t(data.selection_transform_size) != int64_t(data.width) * data.height)
  {
    BKE_tex_paint_selection_transform_clear(data);
  }
  const bool selection_read = BLO_read_array(&reader, &data.selection, data.selection_size);
  if (!selection_read || int64_t(data.selection_size) != int64_t(data.width) * data.height ||
      !data.selection)
  {
    if (data.selection) {
      MEM_delete(data.selection);
      data.selection = nullptr;
    }
    data.selection_size = 0;
    data.selection_active = 0;
  }
  data.image.iuser.scene = nullptr;
  data.pbr_iuser.scene = nullptr;
  data.emission_iuser.scene = nullptr;
  if (data.bump_strength <= 0.0f && data.base_roughness <= 0.0f && data.base_specular <= 0.0f) {
    data.base_roughness = 0.5f;
    data.base_specular = 0.5f;
    data.bump_strength = 1.0f;
  }
  BLO_read_struct_list(&reader, TexPaintLayer, &data.layers);
  for (TexPaintLayer &layer : data.layers) {
    BLO_read_struct_list(&reader, TexPaintColorFilter, &layer.color_filters);
    BKE_tex_paint_mask_effects_blend_read(reader, layer);
    layer.active_color_filter_index = std::clamp(
        layer.active_color_filter_index, 0, std::max(0, layer.color_filters.count() - 1));
    layer.ibuf = nullptr;
    layer.mask_ibuf = nullptr;
    layer.pbr_ibuf = nullptr;
    layer.runtime_dirty = 0;
    if ((layer.flag & TEXPAINT_LAYER_CHANNELS_INIT) == 0) {
      layer.flag |= TEXPAINT_LAYER_CHANNELS_INIT;
      layer.channel_flags = TEXPAINT_CHAN_COLOR;
      layer.fill_roughness = 0.45f;
      layer.fill_metallic = 0.0f;
      layer.fill_height = 0.0f;
      layer.fill_specular = 0.5f;
      layer.fill_emission[0] = 1.0f;
      layer.fill_emission[1] = 0.45f;
      layer.fill_emission[2] = 0.1f;
    }
    BKE_packedfile_blend_read(&reader, &layer.pf, layer.name);
    BKE_packedfile_blend_read(&reader, &layer.mask_pf, layer.name);
    BKE_packedfile_blend_read(&reader, &layer.pbr_pf, layer.name);
  }
  const int count = data.layers.count();
  data.active_layer_index = count ? std::clamp(data.active_layer_index, 0, count - 1) : 0;
}

/** \} */

/* -------------------------------------------------------------------- */
/** \name Canvas and Layers
 * \{ */

bool BKE_tex_paint_tree_shares_channel_layers(const bNodeTree *ntree)
{
  return ntree != nullptr && !STREQ(ntree->id.name + 2, ".TexPaint_2D_Layers");
}

bool BKE_tex_paint_is_initialized(const bNode &node)
{
  const NodeTexPaint *data = static_cast<const NodeTexPaint *>(node.storage);
  return node.id != nullptr && data != nullptr && data->width > 0 && data->height > 0;
}

TexPaintLayer *BKE_tex_paint_layer_active(NodeTexPaint &data)
{
  return static_cast<TexPaintLayer *>(BLI_findlink(&data.layers, data.active_layer_index));
}

int BKE_tex_paint_layer_count(const NodeTexPaint &data)
{
  return data.layers.count();
}

TexPaintLayer *BKE_tex_paint_layer_find_uid(NodeTexPaint &data, const int uid)
{
  for (TexPaintLayer &layer : data.layers) {
    if (layer.uid == uid) {
      return &layer;
    }
  }
  return nullptr;
}

bNode *BKE_tex_paint_node_find_by_layer(bNodeTree &ntree, const TexPaintLayer *layer)
{
  for (bNode &node : ntree.nodes) {
    if (node.type_legacy != SH_NODE_TEX_PAINT || node.storage == nullptr) {
      continue;
    }
    NodeTexPaint *data = static_cast<NodeTexPaint *>(node.storage);
    if (BLI_findindex(&data->layers, layer) != -1) {
      return &node;
    }
  }
  return nullptr;
}

bNode *BKE_tex_paint_node_find_by_image(bNodeTree &ntree, const Image *image)
{
  if (image == nullptr) {
    return nullptr;
  }
  for (bNode &node : ntree.nodes) {
    if (node.type_legacy == SH_NODE_TEX_PAINT && node.id == &image->id && node.storage) {
      return &node;
    }
  }
  for (bNode &node : ntree.nodes) {
    if (node.is_group() && node.id) {
      if (bNode *found = BKE_tex_paint_node_find_by_image(*reinterpret_cast<bNodeTree *>(node.id),
                                                          image))
      {
        return found;
      }
    }
  }
  return nullptr;
}

void BKE_tex_paint_layer_unique_name(NodeTexPaint &data, TexPaintLayer &layer)
{
  BLI_uniquename(&data.layers,
                 &layer,
                 DATA_("Layer"),
                 '.',
                 offsetof(TexPaintLayer, name),
                 sizeof(layer.name));
}

static TexPaintLayer *layer_new(NodeTexPaint &data, const char *name, const int type)
{
  TexPaintLayer *layer = MEM_new<TexPaintLayer>(__func__);
  layer->uid = data.next_layer_uid++;
  layer->type = type;
  layer->flag |= TEXPAINT_LAYER_CHANNELS_INIT;
  layer->channel_flags = (type == TEXPAINT_LAYER_FILL) ?
                             (TEXPAINT_CHAN_COLOR | TEXPAINT_CHAN_ROUGHNESS |
                              TEXPAINT_CHAN_METALLIC | TEXPAINT_CHAN_HEIGHT) :
                             TEXPAINT_CHAN_COLOR;
  STRNCPY_UTF8(layer->name, name ? name : DATA_("Layer"));
  return layer;
}

/** Insert `layer` above the active layer and make it active. */
static void layer_insert_above_active(NodeTexPaint &data, TexPaintLayer *layer)
{
  TexPaintLayer *active = BKE_tex_paint_layer_active(data);
  if (active) {
    BLI_insertlinkafter(&data.layers, active, layer);
  }
  else {
    BLI_addtail(&data.layers, layer);
  }
  BKE_tex_paint_layer_unique_name(data, *layer);
  data.active_layer_index = BLI_findindex(&data.layers, layer);
  BKE_tex_paint_cache_invalidate(data);
}

TexPaintLayer *BKE_tex_paint_layer_add(NodeTexPaint &data, const char *name, const int type)
{
  TexPaintLayer *layer = layer_new(data, name, type);
  layer_insert_above_active(data, layer);
  return layer;
}

TexPaintLayer *BKE_tex_paint_layer_duplicate(NodeTexPaint &data, const TexPaintLayer &src)
{
  TexPaintLayer *layer = MEM_new<TexPaintLayer>(__func__, dna::shallow_copy(src));
  layer_filters_copy(*layer, src);
  BKE_tex_paint_mask_effects_copy(*layer, src, true);
  layer->next = layer->prev = nullptr;
  layer->uid = data.next_layer_uid++;
  layer->pf = src.pf ? BKE_packedfile_duplicate(src.pf) : nullptr;
  layer->mask_pf = src.mask_pf ? BKE_packedfile_duplicate(src.mask_pf) : nullptr;
  layer->pbr_pf = src.pbr_pf ? BKE_packedfile_duplicate(src.pbr_pf) : nullptr;
  layer->ibuf = src.ibuf ? IMB_dupImBuf(src.ibuf) : nullptr;
  layer->mask_ibuf = src.mask_ibuf ? IMB_dupImBuf(src.mask_ibuf) : nullptr;
  layer->pbr_ibuf = src.pbr_ibuf ? IMB_dupImBuf(src.pbr_ibuf) : nullptr;
  layer->runtime_dirty = src.runtime_dirty;
  layer_insert_above_active(data, layer);
  return layer;
}

void BKE_tex_paint_layer_remove(NodeTexPaint &data, TexPaintLayer &layer)
{
  const int index = BLI_findindex(&data.layers, &layer);
  if (index == -1) {
    return;
  }
  if (data.runtime) {
    TexPaintRuntime &runtime = *static_cast<TexPaintRuntime *>(data.runtime);
    for (int i = runtime.filtered_layers.size() - 1; i >= 0; i--) {
      if (runtime.filtered_layers[i].layer_uid == layer.uid) {
        runtime.filtered_layers.remove_and_reorder(i);
      }
    }
  }
  BLI_remlink(&data.layers, &layer);
  layer_free(&layer);
  if (data.active_layer_index >= index) {
    data.active_layer_index = std::max(data.active_layer_index - 1, 0);
  }
  BKE_tex_paint_cache_invalidate(data);
}

bool BKE_tex_paint_layer_move(NodeTexPaint &data, TexPaintLayer &layer, const int direction)
{
  if (direction == 0) {
    return false;
  }
  TexPaintLayer *active = BKE_tex_paint_layer_active(data);
  if (!BLI_listbase_link_move(&data.layers, &layer, direction > 0 ? 1 : -1)) {
    return false;
  }
  if (active) {
    data.active_layer_index = BLI_findindex(&data.layers, active);
  }
  BKE_tex_paint_cache_invalidate(data);
  return true;
}

void BKE_tex_paint_layer_active_set(NodeTexPaint &data, const int index)
{
  const int count = data.layers.count();
  const int new_index = count ? std::clamp(index, 0, count - 1) : 0;
  if (new_index != data.active_layer_index) {
    data.active_layer_index = new_index;
    if (const TexPaintLayer *active = BKE_tex_paint_layer_active(data)) {
      if (!(active->flag & TEXPAINT_LAYER_USE_MASK)) {
        data.paint_target = TEXPAINT_TARGET_COLOR;
      }
    }
    BKE_tex_paint_cache_invalidate(data);
  }
}

void BKE_tex_paint_ensure_pbr_images(Main &bmain, bNode &node)
{
  NodeTexPaint *data = static_cast<NodeTexPaint *>(node.storage);
  if (data == nullptr || data->width <= 0 || data->height <= 0) {
    return;
  }
  const char *base_name = node.id ? (node.id->name + 2) : "Paint";
  if (data->pbr_image == nullptr) {
    const float default_pbr[4] = {
        data->base_roughness, data->base_metallic, 0.5f, data->base_specular};
    const std::string pbr_name = fmt::format(".{}_PBR", base_name);
    Image *pbr_img = BKE_image_add_generated(&bmain,
                                             data->width,
                                             data->height,
                                             pbr_name.c_str(),
                                             data->is_float ? 128 : 32,
                                             data->is_float,
                                             IMA_GENTYPE_BLANK,
                                             default_pbr,
                                             false,
                                             true,
                                             false);
    if (pbr_img != nullptr) {
      pbr_img->alpha_mode = IMA_ALPHA_CHANNEL_PACKED;
      data->pbr_image = pbr_img;
    }
  }
  if (data->emission_image == nullptr) {
    const float default_em[4] = {0.0f, 0.0f, 0.0f, 1.0f};
    const std::string em_name = fmt::format(".{}_Emission", base_name);
    Image *em_img = BKE_image_add_generated(&bmain,
                                            data->width,
                                            data->height,
                                            em_name.c_str(),
                                            data->is_float ? 128 : 32,
                                            data->is_float,
                                            IMA_GENTYPE_BLANK,
                                            default_em,
                                            false,
                                            false,
                                            false);
    if (em_img != nullptr) {
      data->emission_image = em_img;
    }
  }
}

Image *BKE_tex_paint_canvas_init(Main &bmain,
                                 bNode &node,
                                 const char *name,
                                 const int width,
                                 const int height,
                                 const bool is_float,
                                 const float color[4])
{
  NodeTexPaint *data = static_cast<NodeTexPaint *>(node.storage);
  if (data == nullptr || width <= 0 || height <= 0) {
    return nullptr;
  }

  const float transparent[4] = {0.0f, 0.0f, 0.0f, 0.0f};
  Image *image = BKE_image_add_generated(&bmain,
                                         width,
                                         height,
                                         name,
                                         is_float ? 128 : 32,
                                         is_float,
                                         IMA_GENTYPE_BLANK,
                                         transparent,
                                         false,
                                         false,
                                         false);
  if (image == nullptr) {
    return nullptr;
  }

  /* The new image's initial user is transferred to the node. */
  if (node.id) {
    id_us_min(node.id);
  }
  node.id = &image->id;

  BKE_tex_paint_storage_free_contents(*data);
  data->width = width;
  data->height = height;
  data->is_float = is_float;
  data->active_layer_index = 0;
  data->paint_target = TEXPAINT_TARGET_COLOR;

  if (data->pbr_image) {
    id_us_min(&data->pbr_image->id);
    data->pbr_image = nullptr;
  }
  if (data->emission_image) {
    id_us_min(&data->emission_image->id);
    data->emission_image = nullptr;
  }
  BKE_tex_paint_ensure_pbr_images(bmain, node);

  TexPaintLayer *base = BKE_tex_paint_layer_add(*data, DATA_("Base"), TEXPAINT_LAYER_PAINT);
  if (ImBuf *ibuf = BKE_tex_paint_layer_ensure_ibuf(*data, *base)) {
    canvas_buffer_fill(*ibuf, canvas_color_from_scene_linear(*data, color));
    base->runtime_dirty = 1;
  }

  BKE_tex_paint_composite_full(node);
  return image;
}

bool BKE_tex_paint_canvas_resize(NodeTexPaint &data, const int width, const int height)
{
  if (width <= 0 || height <= 0) {
    return false;
  }
  BKE_tex_paint_selection_clear(data);
  BKE_tex_paint_ensure_all_ibufs(data);
  for (TexPaintLayer &layer : data.layers) {
    if (layer.ibuf) {
      IMB_scale(layer.ibuf, width, height, IMBScaleFilter::Bilinear);
    }
    if (layer.mask_ibuf) {
      IMB_scale(layer.mask_ibuf, width, height, IMBScaleFilter::Bilinear);
    }
    BKE_tex_paint_mask_effects_resize(layer, width, height);
    if (layer.pbr_ibuf) {
      IMB_scale(layer.pbr_ibuf, width, height, IMBScaleFilter::Bilinear);
    }
    layer.runtime_dirty = 1;
  }
  data.width = width;
  data.height = height;
  runtime_free(data);
  return true;
}

/** \} */

/* -------------------------------------------------------------------- */
/** \name Pixel Buffers
 * \{ */

/** Match the color space of layer buffers with the master image buffers. */
static void layer_buffer_assign_colorspace(const ImBuf *master, ImBuf &ibuf)
{
  if (master == nullptr) {
    return;
  }
  if (master->byte_buffer.colorspace) {
    ibuf.byte_buffer.colorspace = master->byte_buffer.colorspace;
  }
  if (master->float_buffer.colorspace) {
    ibuf.float_buffer.colorspace = master->float_buffer.colorspace;
  }
}

ImBuf *BKE_tex_paint_layer_ensure_ibuf(NodeTexPaint &data, TexPaintLayer &layer)
{
  if (!ELEM(layer.type, TEXPAINT_LAYER_PAINT, TEXPAINT_LAYER_FILL)) {
    return nullptr;
  }
  if (layer.ibuf && !buffer_matches_canvas(data, layer.ibuf)) {
    /* Canvas settings changed underneath the buffer, e.g. through Python. */
    IMB_scale(layer.ibuf, data.width, data.height, IMBScaleFilter::Bilinear);
  }
  if (layer.ibuf == nullptr) {
    layer.ibuf = unpack_buffer(data, layer.pf);
  }
  if (layer.ibuf == nullptr) {
    layer.ibuf = canvas_buffer_alloc(data);
  }
  return layer.ibuf;
}

ImBuf *BKE_tex_paint_layer_ensure_pbr_ibuf(NodeTexPaint &data, TexPaintLayer &layer)
{
  if (!ELEM(layer.type, TEXPAINT_LAYER_PAINT, TEXPAINT_LAYER_FILL)) {
    return nullptr;
  }
  if (layer.pbr_ibuf && !buffer_matches_canvas(data, layer.pbr_ibuf)) {
    IMB_scale(layer.pbr_ibuf, data.width, data.height, IMBScaleFilter::Bilinear);
  }
  if (layer.pbr_ibuf == nullptr) {
    layer.pbr_ibuf = unpack_buffer(data, layer.pbr_pf);
  }
  if (layer.pbr_ibuf == nullptr) {
    layer.pbr_ibuf = canvas_buffer_alloc(data);
    if (layer.pbr_ibuf) {
      canvas_buffer_fill(*layer.pbr_ibuf, float4(0.5f, 0.0f, 0.5f, 0.0f));
    }
  }
  return layer.pbr_ibuf;
}

ImBuf *BKE_tex_paint_layer_ensure_mask_ibuf(NodeTexPaint &data, TexPaintLayer &layer)
{
  if (layer.mask_ibuf && !buffer_matches_canvas(data, layer.mask_ibuf)) {
    IMB_scale(layer.mask_ibuf, data.width, data.height, IMBScaleFilter::Bilinear);
  }
  if (layer.mask_ibuf == nullptr) {
    layer.mask_ibuf = unpack_buffer(data, layer.mask_pf);
  }
  if (layer.mask_ibuf == nullptr) {
    layer.mask_ibuf = canvas_buffer_alloc(data);
    if (layer.mask_ibuf) {
      canvas_buffer_fill(*layer.mask_ibuf, float4(1.0f));
    }
  }
  return layer.mask_ibuf;
}

void BKE_tex_paint_ensure_all_ibufs(NodeTexPaint &data)
{
  for (TexPaintLayer &layer : data.layers) {
    if (layer.ibuf == nullptr && layer.pf) {
      BKE_tex_paint_layer_ensure_ibuf(data, layer);
    }
    if (layer.pbr_ibuf == nullptr && layer.pbr_pf) {
      BKE_tex_paint_layer_ensure_pbr_ibuf(data, layer);
    }
    if (layer.mask_ibuf == nullptr && (layer.mask_pf || (layer.flag & TEXPAINT_LAYER_USE_MASK))) {
      BKE_tex_paint_layer_ensure_mask_ibuf(data, layer);
    }
  }
}

ImBuf *BKE_tex_paint_paint_target_ibuf(NodeTexPaint &data)
{
  TexPaintLayer *layer = BKE_tex_paint_layer_active(data);
  if (layer == nullptr || (layer->flag & TEXPAINT_LAYER_LOCKED) != 0 ||
      (layer->flag & TEXPAINT_LAYER_VISIBLE) == 0)
  {
    return nullptr;
  }
  if (data.paint_target == TEXPAINT_TARGET_MASK) {
    if ((layer->flag & TEXPAINT_LAYER_USE_MASK) == 0) {
      return nullptr;
    }
    return layer->mask_effects_active ? BKE_tex_paint_mask_paint_target(data, *layer) :
                                        BKE_tex_paint_layer_ensure_mask_ibuf(data, *layer);
  }
  if (layer->type != TEXPAINT_LAYER_PAINT) {
    /* Fill/Adjustment layers cannot be directly brush-painted in Color mode. */
    return nullptr;
  }
  return BKE_tex_paint_layer_ensure_ibuf(data, *layer);
}

const char *BKE_tex_paint_stroke_block_reason(const bNode &node)
{
  if (!BKE_tex_paint_is_initialized(node)) {
    return nullptr;
  }
  const NodeTexPaint &data = *static_cast<const NodeTexPaint *>(node.storage);
  const TexPaintLayer *layer = BKE_tex_paint_layer_active(const_cast<NodeTexPaint &>(data));
  if (layer == nullptr) {
    return "No active paint layer";
  }
  if ((layer->flag & TEXPAINT_LAYER_VISIBLE) == 0) {
    return "Cannot paint on a hidden layer";
  }
  if (layer->flag & TEXPAINT_LAYER_LOCKED) {
    return "Cannot paint on a locked layer";
  }
  if (data.paint_target == TEXPAINT_TARGET_MASK) {
    if ((layer->flag & TEXPAINT_LAYER_USE_MASK) == 0) {
      return "Layer has no mask — initialize a Black or White Mask first";
    }
    if (layer->mask_effects_active) {
      const TexPaintMaskEffect *effect = BKE_tex_paint_mask_effect_find(
          *const_cast<TexPaintLayer *>(layer), layer->active_mask_effect_uid);
      if (!effect || effect->type != TEXPAINT_MASK_EFFECT_PAINT || !effect->enabled) {
        return "Select an enabled Paint effect in the mask stack before painting";
      }
    }
    return nullptr;
  }
  if (layer->type == TEXPAINT_LAYER_FILL) {
    return "Cannot paint directly on a Fill Layer — add a Paint Layer, or select its Mask to "
           "paint";
  }
  if (layer->type == TEXPAINT_LAYER_ADJUSTMENT) {
    return "Cannot paint directly on an Adjustment Layer — select its Mask to paint";
  }
  return nullptr;
}

/** \} */

/* -------------------------------------------------------------------- */
/** \name Blending
 * \{ */

static float lum(const float3 &c)
{
  return 0.3f * c.x + 0.59f * c.y + 0.11f * c.z;
}

static float3 clip_color(float3 c)
{
  const float l = lum(c);
  const float n = std::min({c.x, c.y, c.z});
  const float x = std::max({c.x, c.y, c.z});
  if (n < 0.0f && l - n > 1e-6f) {
    c = l + (c - l) * l / (l - n);
  }
  if (x > 1.0f && x - l > 1e-6f) {
    c = l + (c - l) * (1.0f - l) / (x - l);
  }
  return c;
}

static float3 set_lum(const float3 &c, const float l)
{
  return clip_color(c + (l - lum(c)));
}

static float sat(const float3 &c)
{
  return std::max({c.x, c.y, c.z}) - std::min({c.x, c.y, c.z});
}

static float3 set_sat(const float3 &c, const float s)
{
  const float cmax = std::max({c.x, c.y, c.z});
  const float cmin = std::min({c.x, c.y, c.z});
  const float range = cmax - cmin;
  if (range <= 1e-6f) {
    return float3(0.0f);
  }
  return (c - cmin) * (s / range);
}

static float soft_light_channel(const float cb, const float cs)
{
  if (cs <= 0.5f) {
    return cb - (1.0f - 2.0f * cs) * cb * (1.0f - cb);
  }
  const float d = (cb <= 0.25f) ? ((16.0f * cb - 12.0f) * cb + 4.0f) * cb : std::sqrt(cb);
  return cb + (2.0f * cs - 1.0f) * (d - cb);
}

/** Separable and non-separable blend functions on straight colors (W3C compositing). */
static float3 blend_colors(const float3 &cb, const float3 &cs, const int mode)
{
  switch (mode) {
    case IMB_BLEND_ADD:
      return cb + cs;
    case IMB_BLEND_SUB:
      return math::max(cb - cs, float3(0.0f));
    case IMB_BLEND_MUL:
      return cb * cs;
    case IMB_BLEND_LIGHTEN:
      return math::max(cb, cs);
    case IMB_BLEND_DARKEN:
      return math::min(cb, cs);
    case IMB_BLEND_SCREEN:
      return cb + cs - cb * cs;
    case IMB_BLEND_OVERLAY: {
      float3 r;
      for (int i = 0; i < 3; i++) {
        r[i] = (cb[i] <= 0.5f) ? 2.0f * cb[i] * cs[i] :
                                 1.0f - 2.0f * (1.0f - cb[i]) * (1.0f - cs[i]);
      }
      return r;
    }
    case IMB_BLEND_SOFTLIGHT:
      return float3(soft_light_channel(cb.x, cs.x),
                    soft_light_channel(cb.y, cs.y),
                    soft_light_channel(cb.z, cs.z));
    case IMB_BLEND_DIFFERENCE:
      return math::abs(cb - cs);
    case IMB_BLEND_HUE:
      return set_lum(set_sat(cs, sat(cb)), lum(cb));
    case IMB_BLEND_SATURATION:
      return set_lum(set_sat(cb, sat(cs)), lum(cb));
    case IMB_BLEND_COLOR:
      return set_lum(cs, lum(cb));
    case IMB_BLEND_LUMINOSITY:
      return set_lum(cb, lum(cs));
    case IMB_BLEND_MIX:
    default:
      return cs;
  }
}

/** Composite a straight source color with coverage `as` over a straight backdrop. */
static void composite_over(float4 &dst, const float3 &cs, const float as, const int mode)
{
  if (as <= 0.0f) {
    return;
  }
  const float ab = dst.w;
  const float3 cb = dst.xyz();
  const float3 mixed = (mode == IMB_BLEND_MIX) ? cs :
                                                 cs * (1.0f - ab) + blend_colors(cb, cs, mode) * ab;
  const float ao = as + ab * (1.0f - as);
  const float3 premul = mixed * as + cb * (ab * (1.0f - as));
  if (ao <= 0.0f) {
    dst = float4(0.0f);
    return;
  }
  dst = float4(premul / ao, ao);
}

static float3 adjust_color(const float3 &c, const float hsv_offset[3], const float bc[2])
{
  float3 result = c;
  if (hsv_offset[0] != 0.0f || hsv_offset[1] != 0.0f || hsv_offset[2] != 0.0f) {
    float hsv[3];
    rgb_to_hsv_v(result, hsv);
    hsv[0] = hsv[0] + hsv_offset[0];
    hsv[0] -= std::floor(hsv[0]);
    hsv[1] = std::clamp(hsv[1] + hsv_offset[1], 0.0f, 1.0f);
    hsv[2] = std::max(hsv[2] + hsv_offset[2], 0.0f);
    hsv_to_rgb_v(hsv, result);
  }
  if (bc[0] != 0.0f || bc[1] != 0.0f) {
    /* Same formula as the Brightness/Contrast shader node. */
    const float a = 1.0f + bc[1];
    const float b = bc[0] - bc[1] * 0.5f;
    result = math::max(result * a + b, float3(0.0f));
  }
  return result;
}

/** \} */

/* -------------------------------------------------------------------- */
/** \name Compositor
 * \{ */

namespace {

/** Read-only view of a canvas-format buffer. */
struct PixelSource {
  const uint8_t *bytes = nullptr;
  const float *floats = nullptr;

  explicit PixelSource(const ImBuf *ibuf = nullptr)
  {
    if (ibuf) {
      floats = ibuf->float_data();
      bytes = floats ? nullptr : ibuf->byte_data();
    }
  }

  bool is_valid() const
  {
    return bytes || floats;
  }

  /** Straight alpha color. */
  float4 read(const size_t i) const
  {
    if (floats) {
      float4 straight;
      premul_to_straight_v4_v4(straight, floats + i * 4);
      return straight;
    }
    const uint8_t *p = bytes + i * 4;
    return float4(p[0], p[1], p[2], p[3]) * (1.0f / 255.0f);
  }

  /** Grayscale value used by masks (supports both White/Black painting and Erase Alpha). */
  float read_value(const size_t i) const
  {
    if (floats) {
      const float *p = floats + i * 4;
      const float a = std::clamp(p[3], 0.0f, 1.0f);
      if (a <= 1e-6f) {
        return 0.0f;
      }
      return std::clamp(std::max({p[0], p[1], p[2]}), 0.0f, 1.0f);
    }
    const uint8_t *p = bytes + i * 4;
    const float v = std::max({p[0], p[1], p[2]}) * (1.0f / 255.0f);
    const float a = p[3] * (1.0f / 255.0f);
    return v * a;
  }
};

/** Writable view of a canvas-format buffer. */
struct PixelTarget {
  uint8_t *bytes = nullptr;
  float *floats = nullptr;

  explicit PixelTarget(ImBuf *ibuf = nullptr)
  {
    if (ibuf) {
      if (ibuf->float_data()) {
        floats = ibuf->float_data_for_write();
      }
      else if (ibuf->byte_data()) {
        bytes = ibuf->byte_data_for_write();
      }
    }
  }

  bool is_valid() const
  {
    return bytes || floats;
  }

  void write(const size_t i, const float4 &straight) const
  {
    if (floats) {
      straight_to_premul_v4_v4(floats + i * 4, straight);
      return;
    }
    unit_float_to_uchar_clamp_v4(bytes + i * 4, straight);
  }
};

static float3 color_filter_apply(const TexPaintColorFilter &filter, const float3 color)
{
  float3 adjusted = color;
  const float zero[3] = {0.0f, 0.0f, 0.0f};
  switch (filter.type) {
    case TEXPAINT_COLOR_FILTER_HSV:
      adjusted = adjust_color(color, filter.hsv, zero);
      break;
    case TEXPAINT_COLOR_FILTER_BRIGHT_CONTRAST:
      adjusted = adjust_color(color, zero, filter.bright_contrast);
      break;
    case TEXPAINT_COLOR_FILTER_LEVELS: {
      const float range = std::max(filter.input_white - filter.input_black, 1e-6f);
      const float exponent = 1.0f / std::max(filter.gamma, 0.01f);
      for (int channel = 0; channel < 3; channel++) {
        const float value = std::max((color[channel] - filter.input_black) / range, 0.0f);
        adjusted[channel] = filter.output_black +
                            std::pow(value, exponent) *
                                (filter.output_white - filter.output_black);
      }
      break;
    }
    case TEXPAINT_COLOR_FILTER_INVERT:
      adjusted = math::max(float3(1.0f) - color, float3(0.0f));
      break;
    case TEXPAINT_COLOR_FILTER_GRAYSCALE:
      adjusted = float3(lum(color));
      break;
    case TEXPAINT_COLOR_FILTER_BLUR:
      /* Spatial evaluation is prepared once before pixel composition. */
      break;
  }
  return math::interpolate(color, adjusted, std::clamp(filter.strength, 0.0f, 1.0f));
}

static int color_filter_blur_radius(const TexPaintColorFilter &filter)
{
  return filter.enabled && filter.type == TEXPAINT_COLOR_FILTER_BLUR && filter.strength > 0.0f &&
                 std::isfinite(filter.blur_radius) ?
             std::clamp(int(std::ceil(filter.blur_radius)), 0, 128) :
             0;
}

static int layer_filter_support(const TexPaintLayer &layer)
{
  int support = 0;
  for (const TexPaintColorFilter &filter : layer.color_filters) {
    support += color_filter_blur_radius(filter);
  }
  return support;
}

/** Pre-resolved evaluation data of one layer. */
struct LayerEval {
  int type = TEXPAINT_LAYER_PAINT;
  int blend_mode = IMB_BLEND_MIX;
  int channel_flags = TEXPAINT_CHAN_COLOR;
  bool visible = true;
  bool clipping = false;
  float opacity = 1.0f;
  PixelSource color;
  PixelSource pbr;
  PixelSource mask;
  bool use_mask = false;
  bool mask_invert = false;
  float4 fill = float4(0.0f);
  float fill_roughness = 0.45f;
  float fill_metallic = 0.0f;
  float fill_height = 0.0f;
  float fill_specular = 0.5f;
  float3 fill_emission = float3(1.0f, 0.45f, 0.1f);
  float hsv[3] = {0.0f, 0.0f, 0.0f};
  float bright_contrast[2] = {0.0f, 0.0f};
  const ListBaseT<TexPaintColorFilter> *color_filters = nullptr;

  float3 filtered_color(float3 color) const
  {
    if (color_filters == nullptr) {
      return color;
    }
    for (const TexPaintColorFilter &filter : *color_filters) {
      if (!filter.enabled || filter.strength <= 0.0f) {
        continue;
      }
      color = color_filter_apply(filter, color);
    }
    return color;
  }

  float mask_value(const size_t i) const
  {
    if (!use_mask) {
      return 1.0f;
    }
    const float value = std::clamp(mask.read_value(i), 0.0f, 1.0f);
    return mask_invert ? 1.0f - value : value;
  }

  /** Alpha of this layer used as clipping base, ignoring opacity. */
  float base_alpha(const size_t i) const
  {
    if (!visible) {
      return 0.0f;
    }
    switch (type) {
      case TEXPAINT_LAYER_PAINT:
        return color.is_valid() ? color.read(i).w * mask_value(i) : 0.0f;
      case TEXPAINT_LAYER_FILL:
        return (color.is_valid() ? color.read(i).w : fill.w) * mask_value(i);
      default:
        return mask_value(i);
    }
  }

  void apply(float4 &dst, const size_t i, const float clip) const
  {
    if (type != TEXPAINT_LAYER_ADJUSTMENT && (channel_flags & TEXPAINT_CHAN_COLOR) == 0) {
      return;
    }
    switch (type) {
      case TEXPAINT_LAYER_PAINT: {
        if (!color.is_valid()) {
          return;
        }
        const float4 src = color.read(i);
        composite_over(
            dst, filtered_color(src.xyz()), src.w * opacity * mask_value(i) * clip, blend_mode);
        break;
      }
      case TEXPAINT_LAYER_FILL: {
        const float4 src = color.is_valid() ? (color.read(i) * fill) : fill;
        composite_over(
            dst, filtered_color(src.xyz()), src.w * opacity * mask_value(i) * clip, blend_mode);
        break;
      }
      case TEXPAINT_LAYER_ADJUSTMENT: {
        if (dst.w <= 0.0f) {
          return;
        }
        const float t = opacity * mask_value(i) * clip;
        if (t <= 0.0f) {
          return;
        }
        const float3 adjusted = adjust_color(dst.xyz(), hsv, bright_contrast);
        dst = float4(math::interpolate(dst.xyz(), adjusted, t), dst.w);
        break;
      }
    }
  }

  void apply_pbr_em(float4 &pbr_dst,
                    float4 &em_dst,
                    const size_t i,
                    const float clip,
                    const bool do_pbr,
                    const bool do_em) const
  {
    if (!visible || opacity <= 0.0f || type == TEXPAINT_LAYER_ADJUSTMENT) {
      return;
    }
    const int pbr_mask = TEXPAINT_CHAN_ROUGHNESS | TEXPAINT_CHAN_METALLIC | TEXPAINT_CHAN_HEIGHT |
                         TEXPAINT_CHAN_NORMAL | TEXPAINT_CHAN_SPECULAR;
    if ((!do_pbr || (channel_flags & pbr_mask) == 0) &&
        (!do_em || (channel_flags & TEXPAINT_CHAN_EMISSION) == 0))
    {
      return;
    }
    const float m = opacity * mask_value(i) * clip;
    if (m <= 0.0f) {
      return;
    }

    float stroke_a = 1.0f;
    float tex_mod = 1.0f;
    bool has_fill_tex = false;
    if (type == TEXPAINT_LAYER_PAINT) {
      if (!color.is_valid()) {
        return;
      }
      stroke_a = color.read(i).w;
    }
    else if (type == TEXPAINT_LAYER_FILL) {
      if (color.is_valid()) {
        const float4 tc = color.read(i);
        stroke_a = tc.w * fill.w;
        tex_mod = lum(tc.xyz());
        has_fill_tex = true;
      }
      else {
        stroke_a = fill.w;
      }
    }
    if (stroke_a <= 0.0f && !pbr.is_valid()) {
      return;
    }

    if (do_pbr && (channel_flags & pbr_mask) != 0) {
      float r_src = fill_roughness;
      float m_src = fill_metallic;
      const float eff_h = has_fill_tex ? (fill_height * (tex_mod * 2.0f - 1.0f)) : fill_height;
      float h_src = std::clamp(0.5f + 0.5f * eff_h, 0.0f, 1.0f);
      float s_src = fill_specular;
      float cov = stroke_a * m;
      if (pbr.is_valid()) {
        const float4 ps = pbr.read(i);
        r_src = ps.x;
        m_src = ps.y;
        h_src = ps.z;
        cov = ps.w * m;
      }
      if (cov > 0.0f) {
        if (channel_flags & TEXPAINT_CHAN_ROUGHNESS) {
          pbr_dst.x = math::interpolate(pbr_dst.x, r_src, cov);
        }
        if (channel_flags & TEXPAINT_CHAN_METALLIC) {
          pbr_dst.y = math::interpolate(pbr_dst.y, m_src, cov);
        }
        if (channel_flags & TEXPAINT_CHAN_NORMAL) {
          const float target_h = (channel_flags & TEXPAINT_CHAN_HEIGHT) ? h_src : 0.5f;
          pbr_dst.z = math::interpolate(pbr_dst.z, target_h, cov);
        }
        else if (channel_flags & TEXPAINT_CHAN_HEIGHT) {
          pbr_dst.z = std::clamp(pbr_dst.z + (h_src - 0.5f) * cov, 0.0f, 1.0f);
        }
        if (channel_flags & TEXPAINT_CHAN_SPECULAR) {
          pbr_dst.w = math::interpolate(pbr_dst.w, s_src, cov);
        }
      }
    }

    if (do_em && (channel_flags & TEXPAINT_CHAN_EMISSION) != 0) {
      const float cov = stroke_a * m;
      if (cov > 0.0f) {
        composite_over(em_dst, fill_emission, cov, IMB_BLEND_ADD);
      }
    }
  }
};

}  // namespace

static bool is_active_layer_previewing_mask(NodeTexPaint &data)
{
  const TexPaintLayer *active = BKE_tex_paint_layer_active(data);
  return active != nullptr && (active->flag & TEXPAINT_LAYER_PREVIEW_MASK) != 0 &&
         (active->flag & TEXPAINT_LAYER_USE_MASK) != 0 &&
         buffer_matches_canvas(data, active->mask_ibuf);
}

static rcti filter_rect_expanded(const NodeTexPaint &data, rcti rect, const int support)
{
  if (BLI_rcti_is_empty(&rect)) {
    return rect;
  }
  rect.xmin = std::max(0, rect.xmin - support);
  rect.ymin = std::max(0, rect.ymin - support);
  rect.xmax = std::min(data.width, rect.xmax + support);
  rect.ymax = std::min(data.height, rect.ymax + support);
  return rect;
}

void BKE_tex_paint_layer_effects_expand_rect(const NodeTexPaint &data,
                                             const TexPaintLayer &layer,
                                             rcti &rect)
{
  rect = filter_rect_expanded(data, rect, layer_filter_support(layer));
}

static uint64_t filter_settings_hash(const TexPaintLayer &layer, const float4 &fill)
{
  uint64_t hash = 1469598103934665603ull;
  const auto add = [&](const void *data, const size_t size) {
    const uint8_t *bytes = static_cast<const uint8_t *>(data);
    for (const size_t i : IndexRange(size)) {
      hash = (hash ^ bytes[i]) * 1099511628211ull;
    }
  };
  add(&layer.type, sizeof(layer.type));
  add(&fill, sizeof(fill));
  for (const TexPaintColorFilter &filter : layer.color_filters) {
    /* Parameters contain no pointers, names or unused padding. */
    add(&filter.type, sizeof(filter) - offsetof(TexPaintColorFilter, type));
  }
  return hash;
}

/** Box blur in premultiplied RGBA, retaining all contributions needed by `output_rect`. */
static Array<float4> filter_blur_region(const Span<float4> input,
                                        const rcti &input_rect,
                                        const rcti &output_rect,
                                        const int canvas_width,
                                        const int canvas_height,
                                        const int radius,
                                        const float strength)
{
  const int in_width = BLI_rcti_size_x(&input_rect);
  const int in_height = BLI_rcti_size_y(&input_rect);
  const int out_width = BLI_rcti_size_x(&output_rect);
  const int out_height = BLI_rcti_size_y(&output_rect);
  const double normalization = 1.0 / double(2 * radius + 1);
  Array<float4> horizontal(size_t(out_width) * in_height);
  threading::parallel_for(IndexRange(in_height), 16, [&](const IndexRange rows) {
    for (const int y : rows) {
      const auto value = [&](const int x) {
        return input[size_t(y) * in_width + std::clamp(x, 0, canvas_width - 1) - input_rect.xmin];
      };
      /* Keep zero coverage exact. Float running-sum cancellation otherwise
       * leaves tile-dependent RGB noise when tiny alpha is unpremultiplied. */
      double4 sum(0.0);
      for (int dx = -radius; dx <= radius; dx++) {
        sum += double4(value(output_rect.xmin + dx));
      }
      for (int x = 0; x < out_width; x++) {
        horizontal[size_t(y) * out_width + x] = float4(sum * normalization);
        if (x + 1 < out_width) {
          sum += double4(value(output_rect.xmin + x + radius + 1));
          sum -= double4(value(output_rect.xmin + x - radius));
        }
      }
    }
  });
  Array<float4> output(size_t(out_width) * out_height);
  threading::parallel_for(IndexRange(out_width), 16, [&](const IndexRange columns) {
    for (const int x : columns) {
      const auto value = [&](const int y) {
        return horizontal[size_t(std::clamp(y, 0, canvas_height - 1) - input_rect.ymin) *
                              out_width +
                          x];
      };
      double4 sum(0.0);
      for (int dy = -radius; dy <= radius; dy++) {
        sum += double4(value(output_rect.ymin + dy));
      }
      for (int y = 0; y < out_height; y++) {
        const size_t old_index = size_t(output_rect.ymin + y - input_rect.ymin) * in_width +
                                 output_rect.xmin + x - input_rect.xmin;
        output[size_t(y) * out_width + x] = math::interpolate(
            input[old_index], float4(sum * normalization), strength);
        if (y + 1 < out_height) {
          sum += double4(value(output_rect.ymin + y + radius + 1));
          sum -= double4(value(output_rect.ymin + y - radius));
        }
      }
    }
  });
  return output;
}

static PixelSource filtered_layer_source(NodeTexPaint &data,
                                         TexPaintLayer &layer,
                                         const LayerEval &eval)
{
  TexPaintRuntime &runtime = runtime_ensure(data);
  LayerFilterCache *cache = nullptr;
  for (LayerFilterCache &candidate : runtime.filtered_layers) {
    if (candidate.layer_uid == layer.uid) {
      cache = &candidate;
      break;
    }
  }
  if (cache == nullptr) {
    runtime.filtered_layers.append({});
    cache = &runtime.filtered_layers.last();
    cache->layer_uid = layer.uid;
  }
  const uint64_t settings_hash = filter_settings_hash(layer, eval.fill);
  if (cache->width != data.width || cache->height != data.height ||
      cache->source != layer.ibuf || cache->settings_hash != settings_hash)
  {
    cache->valid = false;
    cache->width = data.width;
    cache->height = data.height;
    cache->source = layer.ibuf;
    cache->settings_hash = settings_hash;
  }
  if (!cache->valid || cache->dirty) {
    const int support = layer_filter_support(layer);
    const rcti full = {0, data.width, 0, data.height};
    const rcti output_rect = cache->valid ?
                                  filter_rect_expanded(data, cache->dirty_rect, support) :
                                  full;
    rcti stage_rect = filter_rect_expanded(data, output_rect, support);
    int stage_width = BLI_rcti_size_x(&stage_rect);
    Array<float4> stage(size_t(stage_width) * BLI_rcti_size_y(&stage_rect));
    threading::parallel_for(IndexRange(BLI_rcti_size_y(&stage_rect)), 16, [&](const IndexRange rows) {
      for (const int y : rows) {
        for (int x = 0; x < stage_width; x++) {
          const size_t index = size_t(y + stage_rect.ymin) * data.width + stage_rect.xmin + x;
          const float4 source = eval.color.read(index) *
                                (layer.type == TEXPAINT_LAYER_FILL ? eval.fill : float4(1.0f));
          straight_to_premul_v4_v4(stage[size_t(y) * stage_width + x], source);
        }
      }
    });
    int remaining_support = support;
    for (const TexPaintColorFilter &filter : layer.color_filters) {
      if (!filter.enabled || filter.strength <= 0.0f) {
        continue;
      }
      const int radius = color_filter_blur_radius(filter);
      if (radius > 0) {
        remaining_support -= radius;
        const rcti next_rect = filter_rect_expanded(data, output_rect, remaining_support);
        stage = filter_blur_region(stage,
                                   stage_rect,
                                   next_rect,
                                   data.width,
                                   data.height,
                                   radius,
                                   std::clamp(filter.strength, 0.0f, 1.0f));
        stage_rect = next_rect;
        stage_width = BLI_rcti_size_x(&stage_rect);
      }
      else if (filter.type != TEXPAINT_COLOR_FILTER_BLUR) {
        threading::parallel_for(stage.index_range(), 4096, [&](const IndexRange range) {
          for (const int64_t i : range) {
            float4 straight;
            premul_to_straight_v4_v4(straight, stage[i]);
            straight = float4(color_filter_apply(filter, straight.xyz()), straight.w);
            straight_to_premul_v4_v4(stage[i], straight);
          }
        });
      }
    }
    if (cache->pixels.size() != size_t(data.width) * data.height) {
      cache->pixels.reinitialize(size_t(data.width) * data.height);
    }
    const int output_width = BLI_rcti_size_x(&output_rect);
    for (int y = output_rect.ymin; y < output_rect.ymax; y++) {
      const size_t source_index = size_t(y - stage_rect.ymin) * stage_width +
                                  output_rect.xmin - stage_rect.xmin;
      const size_t target_index = size_t(y) * data.width + output_rect.xmin;
      cache->pixels.as_mutable_span().slice(target_index, output_width).copy_from(
          stage.as_span().slice(source_index, output_width));
    }
    cache->valid = true;
    cache->dirty = false;
  }
  PixelSource result;
  result.floats = reinterpret_cast<const float *>(cache->pixels.data());
  return result;
}

bool BKE_tex_paint_layer_rasterize(NodeTexPaint &data, TexPaintLayer &layer, const bool check_only)
{
  if (layer.type != TEXPAINT_LAYER_FILL || data.width <= 0 || data.height <= 0 ||
      BLI_findindex(&data.layers, &layer) == -1)
  {
    return false;
  }
  using BufferOwner = std::unique_ptr<ImBuf, decltype(&IMB_freeImBuf)>;
  BufferOwner decoded_color(nullptr, IMB_freeImBuf);
  BufferOwner decoded_pbr(nullptr, IMB_freeImBuf);
  const ImBuf *source = layer.ibuf;
  const ImBuf *pbr_source = layer.pbr_ibuf;
  if (!source && layer.pf) {
    decoded_color.reset(unpack_buffer(data, layer.pf));
    source = decoded_color.get();
    if (!source) {
      return false;
    }
  }
  if (!pbr_source && layer.pbr_pf) {
    decoded_pbr.reset(unpack_buffer(data, layer.pbr_pf));
    pbr_source = decoded_pbr.get();
    if (!pbr_source) {
      return false;
    }
  }
  if ((source && !buffer_matches_canvas(data, source)) ||
      (pbr_source && !buffer_matches_canvas(data, pbr_source)) ||
      (layer.proj_mode != TEXPAINT_PROJ_NONE && !source))
  {
    return false;
  }
  const float4 fill = canvas_color_from_scene_linear(data, layer.fill_color);
  const bool spatial_filters = layer_filter_support(layer) > 0;
  /* A projected Fill historically exposes texture alpha, without fill alpha, as its clipping
   * base. Paint has a single RGBA coverage for both color and clipping. Do not silently change
   * clipped layers when those two coverages cannot be represented by one paint buffer. */
  if (source && !spatial_filters && fill.w != 1.0f &&
      (layer.flag & TEXPAINT_LAYER_CLIPPING) == 0 && layer.next &&
      (layer.next->flag & TEXPAINT_LAYER_CLIPPING))
  {
    return false;
  }
  const int channels = (layer.flag & TEXPAINT_LAYER_CHANNELS_INIT) ?
                           layer.channel_flags :
                           TEXPAINT_CHAN_COLOR | TEXPAINT_CHAN_ROUGHNESS | TEXPAINT_CHAN_METALLIC |
                               TEXPAINT_CHAN_HEIGHT;
  const bool bake_height = source && !pbr_source && (channels & TEXPAINT_CHAN_HEIGHT) &&
                           layer.fill_height != 0.0f;
  BufferOwner raster(canvas_buffer_alloc(data), IMB_freeImBuf);
  BufferOwner raster_pbr(bake_height ? canvas_buffer_alloc(data) : nullptr, IMB_freeImBuf);
  if (!raster || (bake_height && !raster_pbr)) {
    return false;
  }
  const PixelSource raw(source);
  const PixelTarget destination(raster.get());
  const int64_t count = int64_t(data.width) * data.height;
  threading::parallel_for(IndexRange(count), 4096, [&](const IndexRange range) {
    for (const int64_t i : range) {
      destination.write(i, source ? raw.read(i) * fill : fill);
    }
  });
  if (source) {
    layer_buffer_assign_colorspace(source, *raster);
  }
  if (bake_height) {
    PixelSource height_source = raw;
    if (spatial_filters) {
      /* The normal Fill evaluator folds the entire color effect stack into its spatial cache.
       * Preserve that currently evaluated height modulation without baking the editable effects
       * a second time into the color source. Discard derived caches before/after this one-off
       * read. */
      runtime_free(data);
      LayerEval old_eval;
      old_eval.type = TEXPAINT_LAYER_FILL;
      old_eval.color = raw;
      old_eval.fill = fill;
      height_source = filtered_layer_source(data, layer, old_eval);
    }
    const PixelTarget pbr_destination(raster_pbr.get());
    const float roughness = std::clamp(layer.fill_roughness, 0.0f, 1.0f);
    const float metallic = std::clamp(layer.fill_metallic, 0.0f, 1.0f);
    const float height = std::clamp(layer.fill_height, -1.0f, 1.0f);
    threading::parallel_for(IndexRange(count), 4096, [&](const IndexRange range) {
      for (const int64_t i : range) {
        const float4 texture = height_source.read(i);
        const float modulation = lum(texture.xyz());
        const float value = std::clamp(
            0.5f + 0.5f * height * (modulation * 2.0f - 1.0f), 0.0f, 1.0f);
        const float coverage = texture.w * (spatial_filters ? 1.0f : fill.w);
        pbr_destination.write(i, float4(roughness, metallic, value, coverage));
      }
    });
  }
  if (check_only) {
    runtime_free(data);
    return true;
  }
  /* Commit after every source has been read and every replacement buffer allocated. Masks,
   * native Paint coverage, filters, opacity, clipping and all channel parameters keep ownership.
   */
  if (layer.ibuf) {
    IMB_freeImBuf(layer.ibuf);
  }
  layer.ibuf = raster.release();
  if (layer.pf) {
    BKE_packedfile_free(layer.pf);
    layer.pf = nullptr;
  }
  if (raster_pbr) {
    layer.pbr_ibuf = raster_pbr.release();
  }
  layer.type = TEXPAINT_LAYER_PAINT;
  layer.flag |= TEXPAINT_LAYER_CHANNELS_INIT;
  layer.flag &= ~TEXPAINT_LAYER_SHOW_WIDGET;
  layer.channel_flags = channels;
  layer.proj_mode = TEXPAINT_PROJ_NONE;
  layer.proj_image_name[0] = '\0';
  runtime_free(data);
  BKE_tex_paint_layer_pixels_changed(data, layer, nullptr);
  return true;
}

static Vector<LayerEval> build_layer_evals(NodeTexPaint &data, const bool use_mask_preview = true)
{
  BKE_tex_paint_ensure_all_ibufs(data);

  if (use_mask_preview && is_active_layer_previewing_mask(data)) {
    const TexPaintLayer *active = BKE_tex_paint_layer_active(data);
    Vector<LayerEval> mask_evals;
    LayerEval black_base;
    black_base.type = TEXPAINT_LAYER_FILL;
    black_base.blend_mode = 0;
    black_base.visible = true;
    black_base.opacity = 1.0f;
    black_base.fill = float4(0.0f, 0.0f, 0.0f, 1.0f);
    mask_evals.append(black_base);

    LayerEval white_masked;
    white_masked.type = TEXPAINT_LAYER_FILL;
    white_masked.blend_mode = 0;
    white_masked.visible = true;
    white_masked.opacity = 1.0f;
    white_masked.fill = float4(1.0f, 1.0f, 1.0f, 1.0f);
    white_masked.use_mask = true;
    white_masked.mask = PixelSource(active->mask_ibuf);
    white_masked.mask_invert = (active->flag & TEXPAINT_LAYER_MASK_INVERT) != 0;
    mask_evals.append(white_masked);
    return mask_evals;
  }

  Vector<LayerEval> evals;
  for (TexPaintLayer &layer : data.layers) {
    if ((layer.flag & TEXPAINT_LAYER_CHANNELS_INIT) == 0) {
      layer.flag |= TEXPAINT_LAYER_CHANNELS_INIT;
      layer.channel_flags = (layer.type == TEXPAINT_LAYER_FILL) ?
                                (TEXPAINT_CHAN_COLOR | TEXPAINT_CHAN_ROUGHNESS |
                                 TEXPAINT_CHAN_METALLIC | TEXPAINT_CHAN_HEIGHT) :
                                TEXPAINT_CHAN_COLOR;
    }
    LayerEval eval;
    eval.type = layer.type;
    eval.blend_mode = layer.blend_mode;
    eval.channel_flags = layer.channel_flags;
    eval.visible = (layer.flag & TEXPAINT_LAYER_VISIBLE) != 0;
    eval.clipping = (layer.flag & TEXPAINT_LAYER_CLIPPING) != 0;
    eval.opacity = std::clamp(layer.opacity, 0.0f, 1.0f);
    if (ELEM(layer.type, TEXPAINT_LAYER_PAINT, TEXPAINT_LAYER_FILL) &&
        buffer_matches_canvas(data, layer.ibuf))
    {
      eval.color = PixelSource(layer.ibuf);
    }
    if (ELEM(layer.type, TEXPAINT_LAYER_PAINT, TEXPAINT_LAYER_FILL) &&
        buffer_matches_canvas(data, layer.pbr_ibuf))
    {
      eval.pbr = PixelSource(layer.pbr_ibuf);
    }
    eval.use_mask = (layer.flag & TEXPAINT_LAYER_USE_MASK) &&
                    buffer_matches_canvas(data, layer.mask_ibuf);
    if (eval.use_mask) {
      eval.mask = PixelSource(layer.mask_ibuf);
      eval.mask_invert = (layer.flag & TEXPAINT_LAYER_MASK_INVERT) != 0;
    }
    eval.fill = canvas_color_from_scene_linear(data, layer.fill_color);
    eval.fill_roughness = std::clamp(layer.fill_roughness, 0.0f, 1.0f);
    eval.fill_metallic = std::clamp(layer.fill_metallic, 0.0f, 1.0f);
    eval.fill_height = std::clamp(layer.fill_height, -1.0f, 1.0f);
    eval.fill_specular = std::clamp(layer.fill_specular, 0.0f, 1.0f);
    float em_rgba[4] = {
        layer.fill_emission[0], layer.fill_emission[1], layer.fill_emission[2], 1.0f};
    eval.fill_emission = canvas_color_from_scene_linear(data, em_rgba).xyz();
    copy_v3_v3(eval.hsv, layer.adjust_hsv);
    copy_v2_v2(eval.bright_contrast, layer.adjust_bright_contrast);
    eval.color_filters = &layer.color_filters;
    if (eval.visible && eval.color.is_valid() && layer_filter_support(layer) > 0) {
      eval.color = filtered_layer_source(data, layer, eval);
      eval.fill = float4(1.0f);
      eval.color_filters = nullptr;
    }
    evals.append(eval);
  }
  return evals;
}

/**
 * Composite rows of `rect`.
 * \param backdrop: When valid, holds the composite of layers `[0, start)`.
 * \param cache_target: When valid, receives the composite of layers `[0, cache_split)`.
 * \param target: When valid, receives the composite of all layers.
 */
static void composite_region(const Span<LayerEval> layers,
                             const rcti &rect,
                             const int canvas_width,
                             const PixelSource &backdrop,
                             const int start,
                             const PixelTarget &cache_target,
                             const int cache_split,
                             const PixelTarget &target)
{
  const int width = BLI_rcti_size_x(&rect);
  const int height = BLI_rcti_size_y(&rect);
  if (width <= 0 || height <= 0) {
    return;
  }

  /* Index of the clipping base below `start`, used when the first composited layers clip. */
  int clip_base_before_start = -1;
  for (int i = start - 1; i >= 0; i--) {
    if (!layers[i].clipping) {
      clip_base_before_start = i;
      break;
    }
  }

  const int64_t grain = std::max<int64_t>(8, 16384 / std::max(width, 1));
  threading::parallel_for(IndexRange(height), grain, [&](const IndexRange rows) {
    Array<float4> row(width);
    Array<float> base_alpha(width);
    for (const int64_t row_i : rows) {
      const int y = rect.ymin + int(row_i);
      const size_t row_offset = size_t(y) * size_t(canvas_width) + size_t(rect.xmin);

      for (int x = 0; x < width; x++) {
        row[x] = backdrop.is_valid() ? backdrop.read(row_offset + x) : float4(0.0f);
        base_alpha[x] = (clip_base_before_start == -1) ?
                            1.0f :
                            layers[clip_base_before_start].base_alpha(row_offset + x);
      }

      for (const int layer_i : IndexRange(start, layers.size() - start)) {
        if (cache_target.is_valid() && layer_i == cache_split) {
          for (int x = 0; x < width; x++) {
            cache_target.write(row_offset + x, row[x]);
          }
        }
        const LayerEval &layer = layers[layer_i];
        if (!layer.clipping) {
          for (int x = 0; x < width; x++) {
            base_alpha[x] = layer.base_alpha(row_offset + x);
          }
        }
        if (!layer.visible || layer.opacity <= 0.0f) {
          continue;
        }
        for (int x = 0; x < width; x++) {
          layer.apply(row[x], row_offset + x, layer.clipping ? base_alpha[x] : 1.0f);
        }
      }

      if (target.is_valid()) {
        for (int x = 0; x < width; x++) {
          target.write(row_offset + x, row[x]);
        }
      }
    }
  });
}

static void canvas_rect_clamp(const NodeTexPaint &data, const rcti *rect, rcti &r_rect)
{
  BLI_rcti_init(&r_rect, 0, data.width, 0, data.height);
  if (rect) {
    rcti clipped;
    if (!BLI_rcti_isect(&r_rect, rect, &clipped)) {
      BLI_rcti_init(&r_rect, 0, 0, 0, 0);
      return;
    }
    r_rect = clipped;
  }
}

/**
 * Rebuild the below-active cache over the whole canvas while compositing everything into the
 * master buffer.
 */
static void composite_full_with_cache(NodeTexPaint &data, ImBuf &master)
{
  TexPaintRuntime &runtime = runtime_ensure(data);
  const Vector<LayerEval> layers = build_layer_evals(data);
  const int active = is_active_layer_previewing_mask(data) ? 1 : data.active_layer_index;

  PixelTarget cache_target;
  if (active > 0 && active < layers.size()) {
    if (runtime.cache_below && !buffer_matches_canvas(data, runtime.cache_below)) {
      IMB_freeImBuf(runtime.cache_below);
      runtime.cache_below = nullptr;
    }
    if (runtime.cache_below == nullptr) {
      runtime.cache_below = canvas_buffer_alloc(data);
    }
    cache_target = PixelTarget(runtime.cache_below);
  }

  rcti rect;
  canvas_rect_clamp(data, nullptr, rect);
  composite_region(
      layers, rect, data.width, PixelSource(), 0, cache_target, active, PixelTarget(&master));

  runtime.cache_valid = cache_target.is_valid();
  runtime.cache_active_index = runtime.cache_valid ? active : -1;
  runtime.effects_dirty = false;
}

static void master_mark_updated(ImBuf &master, const rcti *rect)
{
  if (master.color_mode == ImColorMode::BW) {
    master.color_mode = ImColorMode::RGBA;
    rect = nullptr;
  }
  if (rect) {
    IMB_partial_update_mark_region(&master, *rect);
  }
  else {
    IMB_partial_update_mark_full(&master);
  }
}

static ImBuf *master_acquire(bNode &node, NodeTexPaint &data, void **r_lock)
{
  Image *image = reinterpret_cast<Image *>(node.id);
  ImBuf *master = BKE_image_acquire_ibuf(image, &data.image.iuser, r_lock);
  if (!buffer_matches_canvas(data, master)) {
    if (master) {
      CLOG_WARN(&LOG,
                "Master image \"%s\" does not match the %dx%d canvas",
                image->id.name + 2,
                data.width,
                data.height);
    }
    BKE_image_release_ibuf(image, master, *r_lock);
    return nullptr;
  }
  return master;
}

static void runtime_mark_composited(NodeTexPaint &data, const ImBuf &master)
{
  TexPaintRuntime &runtime = runtime_ensure(data);
  runtime.composited_ibuf = &master;
  runtime.composited_data = data.is_float ? static_cast<const void *>(master.float_data()) :
                                            static_cast<const void *>(master.byte_data());
}

static bool runtime_is_composited(const NodeTexPaint &data, const ImBuf &master)
{
  const TexPaintRuntime *runtime = static_cast<const TexPaintRuntime *>(data.runtime);
  if (runtime == nullptr || runtime->composited_ibuf != &master) {
    return false;
  }
  const void *pixels = data.is_float ? static_cast<const void *>(master.float_data()) :
                                       static_cast<const void *>(master.byte_data());
  return runtime->composited_data == pixels;
}

static ImBuf *aux_image_acquire(Image *image,
                                ImageUser *iuser,
                                const NodeTexPaint &data,
                                void **r_lock)
{
  *r_lock = nullptr;
  if (image == nullptr) {
    return nullptr;
  }
  ImBuf *ibuf = BKE_image_acquire_ibuf(image, iuser, r_lock);
  if (ibuf && (ibuf->x != data.width || ibuf->y != data.height)) {
    IMB_scale(ibuf, data.width, data.height, IMBScaleFilter::Bilinear);
  }
  if (!buffer_matches_canvas(data, ibuf)) {
    BKE_image_release_ibuf(image, ibuf, *r_lock);
    *r_lock = nullptr;
    return nullptr;
  }
  return ibuf;
}

static void composite_pbr_region(const NodeTexPaint &data,
                                 const Span<LayerEval> layers,
                                 const rcti &rect,
                                 const PixelTarget &pbr_target,
                                 const PixelTarget &em_target)
{
  const bool do_pbr = pbr_target.is_valid();
  const bool do_em = em_target.is_valid();
  if (!do_pbr && !do_em) {
    return;
  }
  const int width = BLI_rcti_size_x(&rect);
  const int height = BLI_rcti_size_y(&rect);
  if (width <= 0 || height <= 0) {
    return;
  }
  const int canvas_width = data.width;
  const float4 base_pbr(std::clamp(data.base_roughness, 0.0f, 1.0f),
                        std::clamp(data.base_metallic, 0.0f, 1.0f),
                        0.5f,
                        std::clamp(data.base_specular, 0.0f, 1.0f));
  const float4 base_em(0.0f, 0.0f, 0.0f, 1.0f);

  const int64_t grain = std::max<int64_t>(8, 16384 / std::max(width, 1));
  threading::parallel_for(IndexRange(height), grain, [&](const IndexRange rows) {
    Array<float4> pbr_row(do_pbr ? width : 0);
    Array<float4> em_row(do_em ? width : 0);
    Array<float> base_alpha(width);
    for (const int64_t row_i : rows) {
      const int y = rect.ymin + int(row_i);
      const size_t row_offset = size_t(y) * size_t(canvas_width) + size_t(rect.xmin);

      if (do_pbr) {
        pbr_row.fill(base_pbr);
      }
      if (do_em) {
        em_row.fill(base_em);
      }
      base_alpha.fill(1.0f);

      for (const LayerEval &layer : layers) {
        if (!layer.clipping) {
          for (int x = 0; x < width; x++) {
            base_alpha[x] = layer.base_alpha(row_offset + x);
          }
        }
        if (!layer.visible || layer.opacity <= 0.0f) {
          continue;
        }
        for (int x = 0; x < width; x++) {
          float4 dummy_pbr(0.0f), dummy_em(0.0f);
          layer.apply_pbr_em(do_pbr ? pbr_row[x] : dummy_pbr,
                             do_em ? em_row[x] : dummy_em,
                             row_offset + x,
                             layer.clipping ? base_alpha[x] : 1.0f,
                             do_pbr,
                             do_em);
        }
      }

      if (do_pbr) {
        for (int x = 0; x < width; x++) {
          pbr_target.write(row_offset + x, pbr_row[x]);
        }
      }
      if (do_em) {
        for (int x = 0; x < width; x++) {
          em_target.write(row_offset + x, em_row[x]);
        }
      }
    }
  });
}

static void composite_aux_targets(NodeTexPaint &data,
                                  const Span<LayerEval> layers,
                                  const rcti *rect)
{
  if (is_active_layer_previewing_mask(data)) {
    return;
  }
  if (data.pbr_image == nullptr && data.emission_image == nullptr) {
    return;
  }
  void *pbr_lock = nullptr;
  void *em_lock = nullptr;
  ImBuf *pbr_ibuf = aux_image_acquire(data.pbr_image, &data.pbr_iuser, data, &pbr_lock);
  ImBuf *em_ibuf = aux_image_acquire(data.emission_image, &data.emission_iuser, data, &em_lock);
  if (pbr_ibuf != nullptr || em_ibuf != nullptr) {
    TexPaintRuntime &runtime = runtime_ensure(data);
    const bool pbr_was_comp = (pbr_ibuf == nullptr) || (runtime.pbr_composited_ibuf == pbr_ibuf);
    const rcti *eff_rect = pbr_was_comp ? rect : nullptr;
    rcti region;
    canvas_rect_clamp(data, eff_rect, region);
    if (!BLI_rcti_is_empty(&region)) {
      composite_pbr_region(
          data, layers, region, PixelTarget(pbr_ibuf), PixelTarget(em_ibuf));
      if (pbr_ibuf) {
        runtime.pbr_composited_ibuf = pbr_ibuf;
        master_mark_updated(*pbr_ibuf, eff_rect ? &region : nullptr);
      }
      if (em_ibuf) {
        master_mark_updated(*em_ibuf, eff_rect ? &region : nullptr);
      }
    }
  }
  if (pbr_ibuf) {
    BKE_image_release_ibuf(data.pbr_image, pbr_ibuf, pbr_lock);
  }
  if (em_ibuf) {
    BKE_image_release_ibuf(data.emission_image, em_ibuf, em_lock);
  }
}

bool BKE_tex_paint_composite_full(bNode &node)
{
  if (!BKE_tex_paint_is_initialized(node)) {
    return false;
  }
  NodeTexPaint &data = *static_cast<NodeTexPaint *>(node.storage);
  BKE_tex_paint_cache_invalidate(data);
  void *lock;
  ImBuf *master = master_acquire(node, data, &lock);
  if (master == nullptr) {
    return false;
  }
  composite_full_with_cache(data, *master);
  runtime_mark_composited(data, *master);
  master_mark_updated(*master, nullptr);
  BKE_image_release_ibuf(reinterpret_cast<Image *>(node.id), master, lock);
  composite_aux_targets(data, build_layer_evals(data), nullptr);
  return true;
}

bool BKE_tex_paint_composite_rect(bNode &node, const rcti *rect)
{
  if (!BKE_tex_paint_is_initialized(node)) {
    return false;
  }
  NodeTexPaint &data = *static_cast<NodeTexPaint *>(node.storage);
  rcti region;
  canvas_rect_clamp(data, rect, region);
  TexPaintRuntime &runtime = runtime_ensure(data);
  if (runtime.effects_dirty) {
    BLI_rcti_union(&region, &runtime.effects_dirty_rect);
  }
  if (BLI_rcti_is_empty(&region)) {
    return true;
  }

  void *lock;
  ImBuf *master = master_acquire(node, data, &lock);
  if (master == nullptr) {
    return false;
  }

  const int eff_active_idx = is_active_layer_previewing_mask(data) ? 1 : data.active_layer_index;
  const bool needs_cache = eff_active_idx > 0;
  const bool cache_usable = !needs_cache ||
                            (runtime.cache_valid &&
                             runtime.cache_active_index == eff_active_idx &&
                             buffer_matches_canvas(data, runtime.cache_below));

  if (!cache_usable || !runtime_is_composited(data, *master)) {
    /* One full pass establishes the cache, later dirty rectangles reuse it. */
    composite_full_with_cache(data, *master);
    runtime_mark_composited(data, *master);
    master_mark_updated(*master, nullptr);
    BKE_image_release_ibuf(reinterpret_cast<Image *>(node.id), master, lock);
    composite_aux_targets(data, build_layer_evals(data), nullptr);
  }
  else {
    const Vector<LayerEval> layers = build_layer_evals(data);
    const int start = needs_cache ? eff_active_idx : 0;
    const PixelSource backdrop = needs_cache ? PixelSource(runtime.cache_below) : PixelSource();
    composite_region(
        layers, region, data.width, backdrop, start, PixelTarget(), -1, PixelTarget(master));
    master_mark_updated(*master, &region);
    BKE_image_release_ibuf(reinterpret_cast<Image *>(node.id), master, lock);
    composite_aux_targets(data, layers, &region);
    runtime.effects_dirty = false;
  }

  return true;
}

bool BKE_tex_paint_ensure_composited(bNode &node)
{
  if (!BKE_tex_paint_is_initialized(node)) {
    return false;
  }
  NodeTexPaint &data = *static_cast<NodeTexPaint *>(node.storage);
  void *lock;
  ImBuf *master = master_acquire(node, data, &lock);
  if (master == nullptr) {
    return false;
  }
  const bool is_composited = runtime_is_composited(data, *master);
  BKE_image_release_ibuf(reinterpret_cast<Image *>(node.id), master, lock);
  if (is_composited) {
    return true;
  }
  return BKE_tex_paint_composite_full(node);
}

void BKE_tex_paint_layer_pixels_changed(NodeTexPaint &data,
                                        const TexPaintLayer &layer,
                                        const rcti *rect)
{
  const_cast<TexPaintLayer &>(layer).runtime_dirty = 1;

  const int index = BLI_findindex(&data.layers, &layer);
  TexPaintRuntime *runtime = static_cast<TexPaintRuntime *>(data.runtime);
  if (runtime) {
    rcti changed;
    canvas_rect_clamp(data, rect, changed);
    for (LayerFilterCache &cache : runtime->filtered_layers) {
      if (cache.layer_uid == layer.uid && cache.valid) {
        if (cache.dirty) {
          BLI_rcti_union(&cache.dirty_rect, &changed);
        }
        else {
          cache.dirty_rect = changed;
          cache.dirty = true;
        }
      }
    }
    if (layer_filter_support(layer) > 0) {
      BKE_tex_paint_layer_effects_expand_rect(data, layer, changed);
      if (runtime->effects_dirty) {
        BLI_rcti_union(&runtime->effects_dirty_rect, &changed);
      }
      else {
        runtime->effects_dirty_rect = changed;
        runtime->effects_dirty = true;
      }
    }
  }
  if (index == -1 || runtime == nullptr || !runtime->cache_valid ||
      index >= runtime->cache_active_index)
  {
    /* Layers at or above the active layer are not part of the cache. */
    return;
  }
  if (!buffer_matches_canvas(data, runtime->cache_below)) {
    BKE_tex_paint_cache_invalidate(data);
    return;
  }

  /* Recomposite the cache inside the changed region. */
  rcti region;
  canvas_rect_clamp(data, rect, region);
  BKE_tex_paint_layer_effects_expand_rect(data, layer, region);
  const Vector<LayerEval> layers = build_layer_evals(data);
  composite_region(layers.as_span().take_front(runtime->cache_active_index),
                   region,
                   data.width,
                   PixelSource(),
                   0,
                   PixelTarget(),
                   -1,
                   PixelTarget(runtime->cache_below));
}

bool BKE_tex_paint_layer_merge_down(NodeTexPaint &data)
{
  const char *error = nullptr;
  std::unique_ptr<TexPaintFlattenResult> prepared = BKE_tex_paint_layer_merge_down_prepare(data,
                                                                                           &error);
  if (!prepared) {
    return false;
  }
  const TexPaintLayer *below = BKE_tex_paint_layer_active(data)->prev;
  const int result_uid = below->uid;
  char name[sizeof(below->name)];
  STRNCPY_UTF8(name, below->name);
  BKE_tex_paint_layers_flatten_commit(data, *prepared, result_uid, name);
  return true;
}

TexPaintFlattenResult::~TexPaintFlattenResult()
{
  if (layer) {
    layer_free(layer);
  }
}

std::unique_ptr<TexPaintFlattenResult> BKE_tex_paint_layers_flatten_prepare(
    NodeTexPaint &data, const Span<int> source_uids, const char **r_error)
{
  if (r_error) {
    *r_error = nullptr;
  }
  const auto fail = [&](const char *message) -> std::unique_ptr<TexPaintFlattenResult> {
    if (r_error) {
      *r_error = message;
    }
    return nullptr;
  };
  if (source_uids.is_empty() || data.width <= 0 || data.height <= 0) {
    return fail("There are no layers to flatten");
  }
  int selected_count = 0;
  for (const TexPaintLayer &source : data.layers) {
    const bool selected = source_uids.contains(source.uid);
    selected_count += selected;
    if (selected && (source.flag & TEXPAINT_LAYER_LOCKED)) {
      return fail("Unlock the layers before flattening them");
    }
    if (!selected && (source.flag & TEXPAINT_LAYER_VISIBLE)) {
      return fail("Every visible layer must be included in the flattened result");
    }
  }
  if (selected_count != source_uids.size()) {
    return fail("The source layer list changed before flattening");
  }

  const Vector<LayerEval> evaluations = build_layer_evals(data, false);
  auto result = std::make_unique<TexPaintFlattenResult>();
  result->source_uids = Array<int>(source_uids);
  result->layer = MEM_new<TexPaintLayer>(__func__);
  result->layer->flag |= TEXPAINT_LAYER_CHANNELS_INIT;
  result->layer->channel_flags = TEXPAINT_CHAN_COLOR;
  result->layer->ibuf = canvas_buffer_alloc(data);
  if (!result->layer->ibuf) {
    return fail("Unable to allocate the flattened color pixels");
  }
  rcti region;
  canvas_rect_clamp(data, nullptr, region);
  composite_region(evaluations,
                   region,
                   data.width,
                   PixelSource(),
                   0,
                   PixelTarget(),
                   -1,
                   PixelTarget(result->layer->ibuf));

  const int pbr_channels = TEXPAINT_CHAN_ROUGHNESS | TEXPAINT_CHAN_METALLIC |
                           TEXPAINT_CHAN_HEIGHT | TEXPAINT_CHAN_NORMAL | TEXPAINT_CHAN_SPECULAR;
  bool has_pbr = false;
  bool has_emission = false;
  for (const LayerEval &evaluation : evaluations) {
    if (evaluation.visible && evaluation.opacity > 0.0f) {
      has_pbr |= (evaluation.channel_flags & pbr_channels) != 0;
      has_emission |= (evaluation.channel_flags & TEXPAINT_CHAN_EMISSION) != 0;
    }
  }
  using BufferPtr = std::unique_ptr<ImBuf, decltype(&IMB_freeImBuf)>;
  BufferPtr pbr(has_pbr ? canvas_buffer_alloc(data) : nullptr, IMB_freeImBuf);
  BufferPtr emission(has_emission ? canvas_buffer_alloc(data) : nullptr, IMB_freeImBuf);
  if ((has_pbr && !pbr) || (has_emission && !emission)) {
    return fail("Unable to allocate the flattened material channels");
  }
  if (has_pbr || has_emission) {
    composite_pbr_region(
        data, evaluations, region, PixelTarget(pbr.get()), PixelTarget(emission.get()));
  }
  const int64_t pixels = int64_t(data.width) * data.height;
  const PixelSource color_source(result->layer->ibuf);
  const PixelSource pbr_source(pbr.get());
  const PixelSource emission_source(emission.get());
  const float specular = has_pbr ? pbr_source.read(0).w : data.base_specular;
  for (const int64_t i : IndexRange(pixels)) {
    const float4 color = color_source.read(i);
    if (!std::isfinite(color.x) || !std::isfinite(color.y) || !std::isfinite(color.z) ||
        !std::isfinite(color.w))
    {
      return fail("Flattening requires finite color values");
    }
    if (has_pbr) {
      const float4 material = pbr_source.read(i);
      if (!std::isfinite(material.x) || !std::isfinite(material.y) || !std::isfinite(material.z) ||
          !std::isfinite(material.w))
      {
        return fail("Flattening requires finite material channel values");
      }
      if (material.w != specular) {
        return fail(
            "Flattening spatially varying Specular is not supported; export the texture set "
            "instead");
      }
    }
    if (has_emission) {
      const float3 emitted = emission_source.read(i).xyz();
      if (emitted != float3(0.0f)) {
        return fail(
            "Flattening nonzero Emission is not supported; export the texture set instead");
      }
    }
  }
  if (has_pbr) {
    const PixelTarget target(pbr.get());
    /* pbr_ibuf alpha means independent coverage, while the composited auxiliary alpha is
     * Specular. Store full coverage and retain the verified uniform Specular parameter. */
    for (const int64_t i : IndexRange(pixels)) {
      const float4 material = pbr_source.read(i);
      target.write(i, float4(material.xyz(), 1.0f));
    }
    result->layer->pbr_ibuf = pbr.release();
    result->layer->channel_flags |= pbr_channels;
    result->layer->fill_specular = specular;
  }
  result->layer->runtime_dirty = 1;
  return result;
}

std::unique_ptr<TexPaintFlattenResult> BKE_tex_paint_layer_merge_down_prepare(NodeTexPaint &data,
                                                                              const char **r_error)
{
  if (r_error) {
    *r_error = nullptr;
  }
  const auto fail = [&](const char *message) -> std::unique_ptr<TexPaintFlattenResult> {
    if (r_error) {
      *r_error = message;
    }
    return nullptr;
  };
  const TexPaintLayer *top = BKE_tex_paint_layer_active(data);
  const TexPaintLayer *below = top ? top->prev : nullptr;
  if (!below || below->type != TEXPAINT_LAYER_PAINT) {
    return fail("The layer below must be a Paint layer");
  }
  if ((top->flag | below->flag) & TEXPAINT_LAYER_LOCKED) {
    return fail("Unlock both layers before merging them");
  }
  if (!(top->flag & TEXPAINT_LAYER_VISIBLE) || !(below->flag & TEXPAINT_LAYER_VISIBLE)) {
    return fail("Show both layers before merging them");
  }
  const int source_uids[] = {below->uid, top->uid};
  const bool other_visible = std::any_of(
      data.layers.begin(), data.layers.end(), [&](const TexPaintLayer &layer) {
        return &layer != top && &layer != below && (layer.flag & TEXPAINT_LAYER_VISIBLE);
      });
  if (!other_visible) {
    return BKE_tex_paint_layers_flatten_prepare(data, Span<int>(source_uids, 2), r_error);
  }
  for (const TexPaintLayer *follower = top->next;
       follower && (follower->flag & TEXPAINT_LAYER_CLIPPING);
       follower = follower->next)
  {
    if ((follower->flag & TEXPAINT_LAYER_VISIBLE) && follower->opacity > 0.0f) {
      return fail("Use Merge Visible to preserve the clipping coverage of layers above the merge");
    }
  }
  const Vector<LayerEval> evaluations = build_layer_evals(data, false);
  if ((top->flag | below->flag) & TEXPAINT_LAYER_CLIPPING || top->blend_mode != IMB_BLEND_MIX ||
      below->blend_mode != IMB_BLEND_MIX || top->type == TEXPAINT_LAYER_ADJUSTMENT)
  {
    return fail(
        "Use Merge Visible to preserve clipping, adjustment and blend interactions with other "
        "layers");
  }
  if ((top->channel_flags | below->channel_flags) & ~TEXPAINT_CHAN_COLOR) {
    return fail("Use Merge Visible to preserve material channel interactions with other layers");
  }
  const int below_index = data.active_layer_index - 1;
  const Span<LayerEval> pair = evaluations.as_span().slice(below_index, 2);
  auto result = std::make_unique<TexPaintFlattenResult>();
  result->source_uids = Array<int>(Span<int>(source_uids, 2));
  result->layer = MEM_new<TexPaintLayer>(__func__);
  result->layer->flag |= TEXPAINT_LAYER_CHANNELS_INIT;
  result->layer->ibuf = canvas_buffer_alloc(data);
  if (!result->layer->ibuf) {
    return fail("Unable to allocate the merged color pixels");
  }
  rcti region;
  canvas_rect_clamp(data, nullptr, region);
  composite_region(pair,
                   region,
                   data.width,
                   PixelSource(),
                   0,
                   PixelTarget(),
                   -1,
                   PixelTarget(result->layer->ibuf));
  result->layer->runtime_dirty = 1;
  return result;
}

TexPaintLayer *BKE_tex_paint_layers_flatten_commit(NodeTexPaint &data,
                                                   TexPaintFlattenResult &prepared,
                                                   const int uid,
                                                   const char *name)
{
  BLI_assert(prepared.layer && !prepared.source_uids.is_empty());
  TexPaintLayer *first = nullptr;
  for (TexPaintLayer &source : data.layers) {
    if (prepared.source_uids.as_span().contains(source.uid)) {
      first = &source;
      break;
    }
  }
  BLI_assert(first);
  TexPaintLayer *result = BKE_tex_paint_layer_find_uid(data, uid);
  if (result) {
    /* Merge Down retains the lower layer's address as well as its UID. Existing native callers
     * and RNA references can continue referring to that layer after its pixels are replaced. */
    BLI_assert(result == first && prepared.source_uids.as_span().contains(uid));
    TexPaintLayer *previous = result->prev;
    TexPaintLayer *next = result->next;
    layer_free_buffers(*result);
    layer_free_packed(*result);
    layer_filters_clear(*result);
    BKE_tex_paint_mask_effects_clear(*result);
    *result = dna::shallow_copy(*prepared.layer);
    result->prev = previous;
    result->next = next;
    MEM_delete(prepared.layer);
    prepared.layer = nullptr;
    result->uid = uid;
    BKE_tex_paint_selection_transform_clear(data);
  }
  else {
    result = prepared.layer;
    prepared.layer = nullptr;
    /* New flattened layers remain outside the source UID namespace until removals finish. */
    result->uid = 0;
    BLI_insertlinkbefore(&data.layers, first, result);
  }
  STRNCPY_UTF8(result->name, name ? name : DATA_("Flattened"));
  for (const int source_uid : prepared.source_uids) {
    TexPaintLayer *source = BKE_tex_paint_layer_find_uid(data, source_uid);
    BLI_assert(source);
    if (source != result) {
      BKE_tex_paint_layer_remove(data, *source);
    }
  }
  result->uid = uid;
  data.next_layer_uid = std::max(data.next_layer_uid, uid + 1);
  data.active_layer_index = BLI_findindex(&data.layers, result);
  data.paint_target = TEXPAINT_TARGET_COLOR;
  BKE_tex_paint_layer_unique_name(data, *result);
  BKE_tex_paint_cache_invalidate(data);
  return result;
}

void BKE_tex_paint_composite_all(Main &bmain)
{
  FOREACH_NODETREE_BEGIN (&bmain, ntree, id) {
    for (bNode &node : ntree->nodes) {
      if (node.type_legacy == SH_NODE_TEX_PAINT && BKE_tex_paint_is_initialized(node)) {
        BKE_tex_paint_ensure_composited(node);
      }
    }
  }
  FOREACH_NODETREE_END;
}

/** \} */

/* -------------------------------------------------------------------- */
/** \name Paint Strokes and Undo
 * \{ */

/** Prefix of the synthetic file path of layer buffers, never a valid file path. */
static constexpr StringRefNull LAYER_BUFFER_PREFIX = "<tex_paint_layer>";

static void layer_buffers_tag(TexPaintLayer &layer)
{
  if (layer.ibuf) {
    layer.ibuf->filepath = fmt::format("{}{}:color", LAYER_BUFFER_PREFIX, layer.uid);
  }
  if (layer.mask_ibuf) {
    layer.mask_ibuf->filepath = fmt::format("{}{}:mask", LAYER_BUFFER_PREFIX, layer.uid);
  }
  for (TexPaintMaskEffect &effect : layer.mask_effects) {
    if (effect.pixels) {
      effect.pixels->filepath = fmt::format(
          "{}{}:paint:{}", LAYER_BUFFER_PREFIX, layer.uid, effect.uid);
    }
  }
}

bNode *BKE_tex_paint_node_find_in_main(Main &bmain, const Image *image)
{
  if (image == nullptr) {
    return nullptr;
  }
  bNode *result = nullptr;
  FOREACH_NODETREE_BEGIN (&bmain, ntree, id) {
    for (bNode &node : ntree->nodes) {
      if (node.type_legacy == SH_NODE_TEX_PAINT && node.id == &image->id && node.storage) {
        result = &node;
        break;
      }
    }
    if (result) {
      break;
    }
  }
  FOREACH_NODETREE_END;
  return result;
}

static TexPaintLayer *layer_find_by_buffer(NodeTexPaint &data, const ImBuf *ibuf)
{
  for (TexPaintLayer &layer : data.layers) {
    if (ibuf && (layer.ibuf == ibuf || layer.mask_ibuf == ibuf ||
                 BKE_tex_paint_mask_effect_find_by_buffer(layer, ibuf)))
    {
      return &layer;
    }
  }
  return nullptr;
}

ImBuf *BKE_tex_paint_stroke_begin(bNode &node, const ImBuf *master)
{
  if (!BKE_tex_paint_is_initialized(node)) {
    return nullptr;
  }
  NodeTexPaint &data = *static_cast<NodeTexPaint *>(node.storage);
  if (!buffer_matches_canvas(data, master)) {
    return nullptr;
  }
  BKE_tex_paint_ensure_composited(node);

  ImBuf *target = BKE_tex_paint_paint_target_ibuf(data);
  TexPaintLayer *layer = BKE_tex_paint_layer_active(data);
  if (target == nullptr || layer == nullptr) {
    return nullptr;
  }
  if (target == layer->ibuf) {
    /* Brushes compute their color in the color space of the painted buffer. Masks keep the
     * default space so their values are painted as given. */
    layer_buffer_assign_colorspace(master, *target);
    runtime_ensure(data).last_proj_layer_uid = 0;
  }
  else if (target == layer->mask_ibuf) {
    runtime_ensure(data).mask_base_layer_uid = 0;
  }
  layer_buffers_tag(*layer);
  return target;
}

bool BKE_tex_paint_stroke_locks_alpha(const bNode &node)
{
  const NodeTexPaint *data = static_cast<const NodeTexPaint *>(node.storage);
  if (data == nullptr || data->paint_target != TEXPAINT_TARGET_COLOR) {
    return false;
  }
  const TexPaintLayer *layer = BKE_tex_paint_layer_active(const_cast<NodeTexPaint &>(*data));
  return layer && (layer->flag & TEXPAINT_LAYER_ALPHA_LOCK) != 0;
}

void BKE_tex_paint_stroke_update(bNode &node, const ImBuf *target, const rcti &rect)
{
  NodeTexPaint &data = *static_cast<NodeTexPaint *>(node.storage);
  TexPaintLayer *layer = layer_find_by_buffer(data, target);
  if (layer == nullptr) {
    return;
  }
  if (TexPaintMaskEffect *effect = BKE_tex_paint_mask_effect_find_by_buffer(*layer, target)) {
    effect->runtime_dirty = 1;
    effect->revision++;
    rcti evaluated_rect;
    if (BKE_tex_paint_mask_evaluate(data, *layer, &rect, evaluated_rect)) {
      /* Publish only the fully evaluated mask. Erasing edits this effect's coverage,
       * never the generated or painted effects beneath it. */
      if (node.runtime && BKE_tex_paint_tree_shares_channel_layers(node.runtime->owner_tree)) {
        for (bNode &other_node : node.runtime->owner_tree->nodes) {
          if (&other_node == &node || other_node.type_legacy != SH_NODE_TEX_PAINT ||
              !other_node.storage || !BKE_tex_paint_is_initialized(other_node))
          {
            continue;
          }
          NodeTexPaint &other_data = *static_cast<NodeTexPaint *>(other_node.storage);
          TexPaintLayer *other_layer = BKE_tex_paint_layer_find_uid(other_data, layer->uid);
          if (!other_layer || other_data.width != data.width || other_data.height != data.height) {
            continue;
          }
          TexPaintMaskEffect *other_effect = BKE_tex_paint_mask_effect_find(*other_layer,
                                                                            effect->uid);
          if (other_effect && other_effect->type == TEXPAINT_MASK_EFFECT_PAINT) {
            ImBuf *other_pixels = BKE_tex_paint_mask_effect_ensure_pixels(other_data,
                                                                          *other_effect);
            rcti clipped;
            canvas_rect_clamp(data, &rect, clipped);
            const uint8_t *src = target->byte_data();
            uint8_t *dst = other_pixels ? other_pixels->byte_data_for_write() : nullptr;
            if (src && dst) {
              for (int y = clipped.ymin; y < clipped.ymax; y++) {
                const size_t offset = (size_t(y) * data.width + clipped.xmin) * 4;
                memcpy(dst + offset, src + offset, size_t(clipped.xmax - clipped.xmin) * 4);
              }
              other_effect->runtime_dirty = 1;
              other_effect->revision++;
              rcti other_rect;
              if (BKE_tex_paint_mask_evaluate(other_data, *other_layer, &rect, other_rect)) {
                BKE_tex_paint_layer_pixels_changed(other_data, *other_layer, &other_rect);
                BKE_tex_paint_composite_rect(other_node, &other_rect);
              }
            }
          }
        }
      }
      BKE_tex_paint_layer_pixels_changed(data, *layer, &evaluated_rect);
      BKE_tex_paint_composite_rect(node, &evaluated_rect);
    }
    return;
  }
  if (target == layer->mask_ibuf && layer->mask_ibuf != nullptr) {
    rcti clamped;
    canvas_rect_clamp(data, &rect, clamped);
    if (!BLI_rcti_is_empty(&clamped)) {
      ImBuf *mask = layer->mask_ibuf;
      if (uint8_t *bytes = mask->byte_data_for_write()) {
        for (int y = clamped.ymin; y < clamped.ymax; y++) {
          uint8_t *row = bytes + (size_t(y) * mask->x + clamped.xmin) * 4;
          for (int x = clamped.xmin; x < clamped.xmax; x++, row += 4) {
            if (row[3] < 255) {
              const uint8_t v = std::max({row[0], row[1], row[2]});
              const uint8_t out_v = uint8_t((uint32_t(v) * uint32_t(row[3]) + 127u) / 255u);
              row[0] = out_v;
              row[1] = out_v;
              row[2] = out_v;
              row[3] = 255;
            }
          }
        }
      }
      else if (float *floats = mask->float_data_for_write()) {
        for (int y = clamped.ymin; y < clamped.ymax; y++) {
          float *row = floats + (size_t(y) * mask->x + clamped.xmin) * 4;
          for (int x = clamped.xmin; x < clamped.xmax; x++, row += 4) {
            if (row[3] < 1.0f) {
              const float a = std::clamp(row[3], 0.0f, 1.0f);
              const float v = (a <= 1e-6f) ?
                                  0.0f :
                                  std::clamp(std::max({row[0], row[1], row[2]}), 0.0f, 1.0f);
              row[0] = v;
              row[1] = v;
              row[2] = v;
              row[3] = 1.0f;
            }
          }
        }
      }

      /* Propagate shared mask stroke dirty rectangles across companion PBR channel nodes in the
       * same material node tree that share this layer's UID. */
      if (node.runtime != nullptr && BKE_tex_paint_tree_shares_channel_layers(node.runtime->owner_tree)) {
        bNodeTree *ntree = node.runtime->owner_tree;
        for (bNode &other_node : ntree->nodes) {
          if (&other_node == &node || other_node.type_legacy != SH_NODE_TEX_PAINT ||
              other_node.storage == nullptr || !BKE_tex_paint_is_initialized(other_node))
          {
            continue;
          }
          NodeTexPaint &other_data = *static_cast<NodeTexPaint *>(other_node.storage);
          TexPaintLayer *other_layer = BKE_tex_paint_layer_find_uid(other_data, layer->uid);
          if (other_layer == nullptr) {
            continue;
          }
          other_layer->flag = (other_layer->flag &
                               ~(TEXPAINT_LAYER_USE_MASK | TEXPAINT_LAYER_MASK_INVERT)) |
                              (layer->flag &
                               (TEXPAINT_LAYER_USE_MASK | TEXPAINT_LAYER_MASK_INVERT));
          ImBuf *other_mask = BKE_tex_paint_layer_ensure_mask_ibuf(other_data, *other_layer);
          if (other_mask == nullptr || other_mask->x != mask->x || other_mask->y != mask->y) {
            continue;
          }
          if (const uint8_t *src_bytes = mask->byte_data()) {
            if (uint8_t *dst_bytes = other_mask->byte_data_for_write()) {
              const size_t row_bytes = size_t(clamped.xmax - clamped.xmin) * 4;
              for (int y = clamped.ymin; y < clamped.ymax; y++) {
                const size_t offset = (size_t(y) * size_t(mask->x) + size_t(clamped.xmin)) * 4;
                memcpy(dst_bytes + offset, src_bytes + offset, row_bytes);
              }
            }
          }
          else if (const float *src_floats = mask->float_data()) {
            if (float *dst_floats = other_mask->float_data_for_write()) {
              const size_t row_bytes = size_t(clamped.xmax - clamped.xmin) * 4 * sizeof(float);
              for (int y = clamped.ymin; y < clamped.ymax; y++) {
                const size_t offset = (size_t(y) * size_t(mask->x) + size_t(clamped.xmin)) * 4;
                memcpy(dst_floats + offset, src_floats + offset, row_bytes);
              }
            }
          }
          BKE_tex_paint_layer_pixels_changed(other_data, *other_layer, &rect);
          BKE_tex_paint_composite_rect(other_node, &rect);
        }
      }
    }
  }
  BKE_tex_paint_layer_pixels_changed(data, *layer, &rect);
  BKE_tex_paint_composite_rect(node, &rect);
}

bool BKE_tex_paint_is_layer_buffer_name(const char *filepath)
{
  return filepath && StringRef(filepath).startswith(LAYER_BUFFER_PREFIX);
}

ImBuf *BKE_tex_paint_undo_buffer_find(Main &bmain,
                                      const Image *image,
                                      const char *filepath,
                                      bNode **r_node)
{
  *r_node = nullptr;
  if (!BKE_tex_paint_is_layer_buffer_name(filepath)) {
    return nullptr;
  }
  int uid = 0;
  char kind[8] = "";
  if (sscanf(filepath + LAYER_BUFFER_PREFIX.size(), "%d:%7s", &uid, kind) != 2) {
    return nullptr;
  }
  bNode *node = BKE_tex_paint_node_find_in_main(bmain, image);
  if (node == nullptr) {
    return nullptr;
  }
  NodeTexPaint &data = *static_cast<NodeTexPaint *>(node->storage);
  TexPaintLayer *layer = BKE_tex_paint_layer_find_uid(data, uid);
  if (layer == nullptr) {
    return nullptr;
  }
  ImBuf *ibuf = nullptr;
  int effect_uid = 0;
  if (sscanf(filepath + LAYER_BUFFER_PREFIX.size(), "%d:paint:%d", &uid, &effect_uid) == 2) {
    TexPaintMaskEffect *effect = BKE_tex_paint_mask_effect_find(*layer, effect_uid);
    ibuf = effect ? BKE_tex_paint_mask_effect_ensure_pixels(data, *effect) : nullptr;
  }
  else if (STREQ(kind, "mask")) {
    ibuf = BKE_tex_paint_layer_ensure_mask_ibuf(data, *layer);
  }
  else if (STREQ(kind, "color")) {
    ibuf = BKE_tex_paint_layer_ensure_ibuf(data, *layer);
  }
  layer_buffers_tag(*layer);
  *r_node = node;
  return ibuf;
}

void BKE_tex_paint_undo_buffer_restored(bNode &node, const ImBuf *target, const rcti &rect)
{
  BKE_tex_paint_stroke_update(node, target, rect);
}

/** \} */

/* -------------------------------------------------------------------- */
/** \name Image Import, Decal & Tiled Projection
 * \{ */

static float4 sample_source_pixel_raw(
    const PixelSource &src, const int src_w, const int src_h, int x, int y, const bool repeat)
{
  if (repeat) {
    x = ((x % src_w) + src_w) % src_w;
    y = ((y % src_h) + src_h) % src_h;
  }
  else {
    x = std::clamp(x, 0, src_w - 1);
    y = std::clamp(y, 0, src_h - 1);
  }
  return src.read(size_t(y) * size_t(src_w) + size_t(x));
}

static float4 sample_source_bilinear(const PixelSource &src,
                                     const bool src_is_float,
                                     const bool dst_is_float,
                                     const int src_w,
                                     const int src_h,
                                     float2 uv,
                                     const bool repeat)
{
  if (!repeat) {
    if (uv.x < 0.0f || uv.x > 1.0f || uv.y < 0.0f || uv.y > 1.0f) {
      return float4(0.0f);
    }
  }
  else {
    uv.x = uv.x - std::floor(uv.x);
    uv.y = uv.y - std::floor(uv.y);
  }

  const float fx = uv.x * float(src_w) - 0.5f;
  const float fy = uv.y * float(src_h) - 0.5f;
  const int x0 = int(std::floor(fx));
  const int y0 = int(std::floor(fy));
  const float tx = fx - float(x0);
  const float ty = fy - float(y0);

  const float4 c00 = sample_source_pixel_raw(src, src_w, src_h, x0, y0, repeat);
  const float4 c10 = sample_source_pixel_raw(src, src_w, src_h, x0 + 1, y0, repeat);
  const float4 c01 = sample_source_pixel_raw(src, src_w, src_h, x0, y0 + 1, repeat);
  const float4 c11 = sample_source_pixel_raw(src, src_w, src_h, x0 + 1, y0 + 1, repeat);

  const float4 c0 = c00 * (1.0f - tx) + c10 * tx;
  const float4 c1 = c01 * (1.0f - tx) + c11 * tx;
  float4 c = c0 * (1.0f - ty) + c1 * ty;
  if (src_is_float != dst_is_float && c.w > 0.0f) {
    if (src_is_float) {
      linearrgb_to_srgb_v3_v3(c, c);
    }
    else {
      srgb_to_linearrgb_v3_v3(c, c);
    }
  }
  return c;
}

static float2 transform_uv_2d(const float2 &uv,
                              const float offset[2],
                              const float scale[2],
                              const float rotation,
                              const bool is_repeat)
{
  const float cos_a = std::cos(-rotation);
  const float sin_a = std::sin(-rotation);
  const float2 p = uv - float2(0.5f, 0.5f) - float2(offset[0], offset[1]);
  const float2 rot(p.x * cos_a - p.y * sin_a, p.x * sin_a + p.y * cos_a);
  if (is_repeat) {
    return float2(rot.x * scale[0] + 0.5f, rot.y * scale[1] + 0.5f);
  }
  const float sx = (std::abs(scale[0]) > 1e-4f) ? scale[0] : 1e-4f;
  const float sy = (std::abs(scale[1]) > 1e-4f) ? scale[1] : 1e-4f;
  return float2(rot.x / sx + 0.5f, rot.y / sy + 0.5f);
}

static inline float fast_pow_pos(const float x, const float p)
{
  if (x <= 1e-5f) {
    return 0.0f;
  }
  if (x >= 1.0f) {
    return 1.0f;
  }
  return std::exp2(p * std::log2(x));
}

/**
 * Ensure the runtime 32x32 UV-space position/normal tile cache is built for `mesh` at `(w, h)`.
 * Built in parallel across horizontal Y-bands and reused across all interactive 3D widget updates.
 */
static void ensure_uv_bake_tiles(TexPaintRuntime &runtime,
                                 const Mesh &mesh,
                                 const VArraySpan<float2> &uv_map,
                                 const int w,
                                 const int h,
                                 const bool need_signals)
{
  const Span<float3> positions = mesh.vert_positions();
  const Span<float3> vert_normals = mesh.vert_normals();
  const Span<int> corner_verts = mesh.corner_verts();
  const Span<int3> corner_tris = mesh.corner_tris();

  if (runtime.uv_bake_mesh == &mesh && runtime.uv_bake_positions_ptr == positions.data() &&
      runtime.uv_bake_tris_ptr == corner_tris.data() && runtime.uv_bake_uv_ptr == uv_map.data() &&
      runtime.uv_bake_verts_num == int(positions.size()) &&
      runtime.uv_bake_tris_num == int(corner_tris.size()) && runtime.uv_bake_w == w &&
      runtime.uv_bake_h == h && !runtime.uv_tiles.is_empty() &&
      (!need_signals || runtime.uv_bake_has_signals))
  {
    return;
  }

  float3 bbox_min(FLT_MAX);
  float3 bbox_max(-FLT_MAX);
  for (const float3 &p : positions) {
    bbox_min = math::min(bbox_min, p);
    bbox_max = math::max(bbox_max, p);
  }
  const float3 bbox_size = bbox_max - bbox_min;
  runtime.uv_bake_bbox_min = bbox_min;
  runtime.uv_bake_bbox_max = bbox_max;
  runtime.uv_bake_bbox_center = (bbox_min + bbox_max) * 0.5f;
  const float max_extent = std::max({bbox_size.x, bbox_size.y, bbox_size.z, 1e-4f});
  runtime.uv_bake_max_extent = max_extent;

  const int num_verts = int(positions.size());
  const int num_tris = int(corner_tris.size());
  Array<float> v_curv_fine;
  Array<float> v_curv_broad;
  Array<float> v_ao_near;
  Array<float> v_ao_far;

  struct TriFaceInfo {
    int v[3];
    float3 normal;
    float edge_curv[3] = {0.0f, 0.0f, 0.0f};
    float edge_alt[3] = {1.0f, 1.0f, 1.0f};
  };
  Array<TriFaceInfo> tri_infos;

  if (need_signals) {
    /* 1. Precompute per-vertex Curvature (fine & broad) and Ambient Occlusion (near & far). */
    v_curv_fine.reinitialize(num_verts);
    v_curv_fine.fill(0.0f);
    v_curv_broad.reinitialize(num_verts);
    v_curv_broad.fill(0.0f);
    v_ao_near.reinitialize(num_verts);
    v_ao_near.fill(1.0f);
    v_ao_far.reinitialize(num_verts);
    v_ao_far.fill(1.0f);

    Array<Vector<int, 8>> v_adj(num_verts);
    for (const int2 &edge : mesh.edges()) {
      const int a = edge[0], b = edge[1];
      if (a >= 0 && a < num_verts && b >= 0 && b < num_verts && a != b) {
        v_adj[a].append_non_duplicates(b);
        v_adj[b].append_non_duplicates(a);
      }
    }

    threading::parallel_for(IndexRange(num_verts), 256, [&](const IndexRange v_range) {
      for (const int64_t vi : v_range) {
        const int v = int(vi);
        const Span<int> nbrs = v_adj[v];
        if (nbrs.is_empty()) {
          continue;
        }
        const float3 &P = positions[v];
        const float3 &N = vert_normals[v];
        float sum_c = 0.0f;
        int valid_n = 0;
        for (const int nb : nbrs) {
          const float3 d = positions[nb] - P;
          const float len = math::length(d);
          if (len > 1e-7f) {
            const float3 dir = d / len;
            sum_c += 0.5f * math::dot(vert_normals[nb] - N, dir);
            valid_n++;
          }
        }
        if (valid_n > 0) {
          v_curv_fine[v] = std::clamp(sum_c / float(valid_n), -1.0f, 1.0f);
        }
      }
    });

    v_curv_broad = v_curv_fine;
    Array<float> v_curv_tmp(num_verts, 0.0f);
    for (int pass = 0; pass < 3; pass++) {
      threading::parallel_for(IndexRange(num_verts), 256, [&](const IndexRange v_range) {
        for (const int64_t vi : v_range) {
          const int v = int(vi);
          float sum = v_curv_broad[v];
          for (const int nb : v_adj[v]) {
            sum += v_curv_broad[nb];
          }
          v_curv_tmp[v] = sum / float(1 + v_adj[v].size());
        }
      });
      v_curv_broad = v_curv_tmp;
    }

    /* Representative surface elements for multi-threaded 3D Hemisphere Ambient Occlusion. */
    struct AOSurfel {
      float3 pos;
      float3 normal;
      float area;
    };
    constexpr int MAX_AO_SURFELS = 768;
    const int tri_step = std::max(1, num_tris / MAX_AO_SURFELS);
    Vector<AOSurfel> surfels;
    surfels.reserve(std::min(num_tris, MAX_AO_SURFELS));
    for (int ti = 0; ti < num_tris; ti += tri_step) {
      const int3 &tri = corner_tris[ti];
      const float3 &p0 = positions[corner_verts[tri[0]]];
      const float3 &p1 = positions[corner_verts[tri[1]]];
      const float3 &p2 = positions[corner_verts[tri[2]]];
      const float3 cr = math::cross(p1 - p0, p2 - p0);
      const float area2 = math::length(cr);
      if (area2 > 1e-8f) {
        surfels.append({(p0 + p1 + p2) / 3.0f, cr / area2, 0.5f * area2 * float(tri_step)});
      }
    }

    const float near_dist2 = math::square(0.18f * max_extent);
    const float far_dist2 = math::square(0.65f * max_extent);
    const float eps_dist2 = math::square(0.002f * max_extent);
    threading::parallel_for(IndexRange(num_verts), 128, [&](const IndexRange v_range) {
      for (const int64_t vi : v_range) {
        const int v = int(vi);
        const float3 &P = positions[v];
        const float3 &N = vert_normals[v];
        float occ_near = 0.0f;
        float occ_far = 0.0f;
        for (const AOSurfel &s : surfels) {
          const float3 d = s.pos - P;
          const float d2 = math::length_squared(d);
          if (d2 <= eps_dist2 || d2 > far_dist2) {
            continue;
          }
          const float inv_d = 1.0f / std::sqrt(d2);
          const float3 dir = d * inv_d;
          const float cos_v = math::dot(N, dir);
          if (cos_v <= 0.08f) {
            continue;
          }
          const float cos_s = std::max(-math::dot(s.normal, dir), 0.15f);
          const float solid = std::min(
              (s.area * cos_v * cos_s) / (float(M_PI) * d2 + s.area), 0.25f);
          const float falloff_far = 1.0f - (d2 / far_dist2);
          occ_far += solid * falloff_far;
          if (d2 < near_dist2) {
            occ_near += solid * (1.0f - (d2 / near_dist2));
          }
        }
        const float crease_occ = std::max(-v_curv_fine[v], 0.0f) * 0.55f +
                                 std::max(-v_curv_broad[v], 0.0f) * 0.35f;
        v_ao_near[v] = std::clamp(1.0f - (occ_near * 1.6f + crease_occ), 0.0f, 1.0f);
        v_ao_far[v] = std::clamp(1.0f - (occ_far * 1.25f + crease_occ * 0.7f), 0.0f, 1.0f);
      }
    });

    /* 2. Precompute per-triangle dihedral edge curvature so hard-surface / low-poly meshes (like a
     * Cube) get crisp Edge Wear and Cavity along sharp 3D edges while ignoring flat quad diagonals.
     */
    tri_infos.reinitialize(num_tris);
    struct UndirEdgeRef {
      uint64_t key;
      int tri_idx;
      int edge_slot;
      int opp_vert;
    };
    Array<UndirEdgeRef> edge_refs(size_t(num_tris) * 3);

    threading::parallel_for(IndexRange(num_tris), 512, [&](const IndexRange t_range) {
      for (const int64_t ti_i : t_range) {
        const int ti = int(ti_i);
        const int3 &tri = corner_tris[ti];
        const int v0 = corner_verts[tri[0]];
        const int v1 = corner_verts[tri[1]];
        const int v2 = corner_verts[tri[2]];
        TriFaceInfo &info = tri_infos[ti];
        info.v[0] = v0;
        info.v[1] = v1;
        info.v[2] = v2;
        const float3 &p0 = positions[v0];
        const float3 &p1 = positions[v1];
        const float3 &p2 = positions[v2];
        const float3 cr = math::cross(p1 - p0, p2 - p0);
        const float area2 = math::length(cr);
        info.normal = (area2 > 1e-8f) ? (cr / area2) : float3(0.0f, 0.0f, 1.0f);
        info.edge_alt[0] = std::max(area2 / std::max(math::length(p2 - p1), 1e-6f), 1e-5f);
        info.edge_alt[1] = std::max(area2 / std::max(math::length(p0 - p2), 1e-6f), 1e-5f);
        info.edge_alt[2] = std::max(area2 / std::max(math::length(p1 - p0), 1e-6f), 1e-5f);

        const int pairs[3][3] = {{v1, v2, v0}, {v2, v0, v1}, {v0, v1, v2}};
        for (int es = 0; es < 3; es++) {
          const uint32_t a = uint32_t(std::min(pairs[es][0], pairs[es][1]));
          const uint32_t b = uint32_t(std::max(pairs[es][0], pairs[es][1]));
          edge_refs[size_t(ti) * 3 + size_t(es)] = {
              (uint64_t(a) << 32) | uint64_t(b), ti, es, pairs[es][2]};
        }
      }
    });

    std::sort(edge_refs.begin(),
              edge_refs.end(),
              [](const UndirEdgeRef &a, const UndirEdgeRef &b) { return a.key < b.key; });

    for (int i = 0; i + 1 < int(edge_refs.size()); i++) {
      if (edge_refs[i].key != edge_refs[i + 1].key) {
        continue;
      }
      const UndirEdgeRef &rA = edge_refs[i];
      const UndirEdgeRef &rB = edge_refs[i + 1];
      TriFaceInfo &tA = tri_infos[rA.tri_idx];
      TriFaceInfo &tB = tri_infos[rB.tri_idx];
      const float cos_n = std::clamp(math::dot(tA.normal, tB.normal), -1.0f, 1.0f);
      if (cos_n >= 0.998f) {
        continue;
      }
      const uint32_t va = uint32_t(rA.key >> 32);
      const uint32_t vb = uint32_t(rA.key & 0xFFFFFFFFu);
      const float3 edge_mid = 0.5f * (positions[va] + positions[vb]);
      const float3 to_opp_B = positions[rB.opp_vert] - edge_mid;
      const float sign_dihedral = (math::dot(tA.normal, to_opp_B) < 0.0f) ? 1.0f : -1.0f;
      const float strength = sign_dihedral * std::sqrt(std::max(0.5f * (1.0f - cos_n), 0.0f));
      tA.edge_curv[rA.edge_slot] = strength;
      tB.edge_curv[rB.edge_slot] = strength;
    }
  }

  constexpr int TILE_SIZE = 32;
  const int tiles_x = (w + TILE_SIZE - 1) / TILE_SIZE;
  const int tiles_y = (h + TILE_SIZE - 1) / TILE_SIZE;
  runtime.uv_tiles.reinitialize(tiles_x * tiles_y);

  for (int ty = 0; ty < tiles_y; ty++) {
    for (int tx = 0; tx < tiles_x; tx++) {
      UVTile &tile = runtime.uv_tiles[ty * tiles_x + tx];
      BLI_rcti_init(&tile.rect,
                    tx * TILE_SIZE,
                    std::min((tx + 1) * TILE_SIZE, w),
                    ty * TILE_SIZE,
                    std::min((ty + 1) * TILE_SIZE, h));
      tile.bbox_min = float3(FLT_MAX);
      tile.bbox_max = float3(-FLT_MAX);
      tile.texels.clear();
      tile.has_prev_pixels = false;
    }
  }

  struct BakeTri {
    int tri_idx;
    int v0, v1, v2;
    float2 uv0, e0, e1;
    float inv_denom, bleed_tol;
    int min_x, max_x, min_y, max_y;
  };

  Vector<BakeTri> bake_tris;
  bake_tris.reserve(corner_tris.size());
  Array<Vector<int>> band_tris(tiles_y);

  for (int ti = 0; ti < num_tris; ti++) {
    const int3 &tri = corner_tris[ti];
    const int c0 = tri[0], c1 = tri[1], c2 = tri[2];
    const float2 uv0 = uv_map[c0] * float2(float(w), float(h));
    const float2 uv1 = uv_map[c1] * float2(float(w), float(h));
    const float2 uv2 = uv_map[c2] * float2(float(w), float(h));
    const float2 e0 = uv1 - uv0;
    const float2 e1 = uv2 - uv0;
    const float denom = e0.x * e1.y - e0.y * e1.x;
    if (std::abs(denom) < 1e-6f) {
      continue;
    }

    const float2 tri_min = math::min(uv0, math::min(uv1, uv2)) - float2(2.0f);
    const float2 tri_max = math::max(uv0, math::max(uv1, uv2)) + float2(2.0f);
    const int min_x = std::clamp(int(std::floor(tri_min.x)), 0, w - 1);
    const int max_x = std::clamp(int(std::ceil(tri_max.x)), 0, w - 1);
    const int min_y = std::clamp(int(std::floor(tri_min.y)), 0, h - 1);
    const int max_y = std::clamp(int(std::ceil(tri_max.y)), 0, h - 1);
    if (min_x > max_x || min_y > max_y) {
      continue;
    }

    const float max_edge = std::max(
        {math::length(e0), math::length(e1), math::length(uv2 - uv1), 1.0f});
    const int bt_idx = int(bake_tris.size());
    bake_tris.append({ti,
                      corner_verts[c0],
                      corner_verts[c1],
                      corner_verts[c2],
                      uv0,
                      e0,
                      e1,
                      1.0f / denom,
                      -2.2f / max_edge,
                      min_x,
                      max_x,
                      min_y,
                      max_y});

    const int b_min = min_y / TILE_SIZE;
    const int b_max = max_y / TILE_SIZE;
    for (int by = b_min; by <= b_max; by++) {
      band_tris[by].append(bt_idx);
    }
  }

  const float edge_r_fine = 0.045f * max_extent;
  const float edge_r_broad = 0.18f * max_extent;

  threading::parallel_for(IndexRange(tiles_y), 1, [&](const IndexRange band_range) {
    for (const int64_t by_i : band_range) {
      const int by = int(by_i);
      if (band_tris[by].is_empty()) {
        continue;
      }
      const int band_ymin = by * TILE_SIZE;
      const int band_ymax = std::min(band_ymin + TILE_SIZE, h);
      const int band_h = band_ymax - band_ymin;

      Array<uint8_t> band_cov(size_t(w) * size_t(band_h), 0);
      Array<UVTexel> band_tex(size_t(w) * size_t(band_h));

      for (const int bt_idx : band_tris[by]) {
        const BakeTri &bt = bake_tris[bt_idx];
        const TriFaceInfo *tinfo = need_signals ? &tri_infos[bt.tri_idx] : nullptr;
        const int y0 = std::max(bt.min_y, band_ymin);
        const int y1 = std::min(bt.max_y, band_ymax - 1);
        const float dw1_dx = bt.e1.y * bt.inv_denom;
        const float dw2_dx = -bt.e0.y * bt.inv_denom;

        for (int py = y0; py <= y1; py++) {
          const size_t local_row = size_t(py - band_ymin) * size_t(w);
          const size_t global_row = size_t(py) * size_t(w);
          const float2 d0(float(bt.min_x) + 0.5f - bt.uv0.x, float(py) + 0.5f - bt.uv0.y);
          float w1_cur = (d0.x * bt.e1.y - d0.y * bt.e1.x) * bt.inv_denom;
          float w2_cur = (bt.e0.x * d0.y - bt.e0.y * d0.x) * bt.inv_denom;

          for (int px = bt.min_x; px <= bt.max_x; px++, w1_cur += dw1_dx, w2_cur += dw2_dx) {
            const size_t l_idx = local_row + size_t(px);
            if (band_cov[l_idx] == 2) {
              continue;
            }
            const float w0_raw = 1.0f - w1_cur - w2_cur;
            if (w0_raw < bt.bleed_tol || w1_cur < bt.bleed_tol || w2_cur < bt.bleed_tol) {
              continue;
            }
            const uint8_t prio = (w0_raw >= -1e-4f && w1_cur >= -1e-4f && w2_cur >= -1e-4f) ? 2 :
                                                                                              1;
            if (prio < band_cov[l_idx]) {
              continue;
            }

            float w0 = std::max(w0_raw, 0.0f);
            float w1 = std::max(w1_cur, 0.0f);
            float w2 = std::max(w2_cur, 0.0f);
            const float inv_sum = 1.0f / std::max(w0 + w1 + w2, 1e-6f);
            w0 *= inv_sum;
            w1 *= inv_sum;
            w2 *= inv_sum;

            const float3 P = positions[bt.v0] * w0 + positions[bt.v1] * w1 + positions[bt.v2] * w2;
            const float3 N = math::normalize(vert_normals[bt.v0] * w0 + vert_normals[bt.v1] * w1 +
                                             vert_normals[bt.v2] * w2);

            float c_fine = 0.0f;
            float c_broad = 0.0f;
            float ao_n = 1.0f;
            float ao_f = 1.0f;
            if (need_signals) {
              c_fine = v_curv_fine[bt.v0] * w0 + v_curv_fine[bt.v1] * w1 + v_curv_fine[bt.v2] * w2;
              c_broad = v_curv_broad[bt.v0] * w0 + v_curv_broad[bt.v1] * w1 +
                        v_curv_broad[bt.v2] * w2;

              /* Dihedral edge distance contribution for sharp hard-surface edges. */
              const float weights[3] = {w0, w1, w2};
              float edge_c_fine = 0.0f;
              float edge_c_broad = 0.0f;
              for (int es = 0; es < 3; es++) {
                const float sc = tinfo->edge_curv[es];
                if (std::abs(sc) > 1e-4f) {
                  const float dist_edge = weights[es] * tinfo->edge_alt[es];
                  const float f_fine = std::max(1.0f - dist_edge / edge_r_fine, 0.0f);
                  const float f_broad = std::max(1.0f - dist_edge / edge_r_broad, 0.0f);
                  edge_c_fine += sc * f_fine * f_fine;
                  edge_c_broad += sc * f_broad * f_broad;
                }
              }
              if (std::abs(edge_c_fine) > std::abs(c_fine)) {
                c_fine = std::clamp(edge_c_fine, -1.0f, 1.0f);
              }
              if (std::abs(edge_c_broad) > std::abs(c_broad)) {
                c_broad = std::clamp(edge_c_broad, -1.0f, 1.0f);
              }

              const float crease_ao = std::max(-edge_c_broad, 0.0f) * 0.55f;
              ao_n = std::clamp(
                  (v_ao_near[bt.v0] * w0 + v_ao_near[bt.v1] * w1 + v_ao_near[bt.v2] * w2) -
                      crease_ao,
                  0.0f,
                  1.0f);
              ao_f = std::clamp(
                  (v_ao_far[bt.v0] * w0 + v_ao_far[bt.v1] * w1 + v_ao_far[bt.v2] * w2) -
                      crease_ao * 0.7f,
                  0.0f,
                  1.0f);
            }

            band_cov[l_idx] = prio;
            band_tex[l_idx] = {
                uint32_t(global_row + size_t(px)), P, N, c_fine, c_broad, ao_n, ao_f};
          }
        }
      }

      for (int tx = 0; tx < tiles_x; tx++) {
        UVTile &tile = runtime.uv_tiles[by * tiles_x + tx];
        int count = 0;
        for (int py = tile.rect.ymin; py < tile.rect.ymax; py++) {
          const size_t local_row = size_t(py - band_ymin) * size_t(w);
          for (int px = tile.rect.xmin; px < tile.rect.xmax; px++) {
            if (band_cov[local_row + size_t(px)] > 0) {
              count++;
            }
          }
        }
        if (count == 0) {
          continue;
        }
        tile.texels.reserve(count);
        float3 bmin(FLT_MAX), bmax(-FLT_MAX);
        for (int py = tile.rect.ymin; py < tile.rect.ymax; py++) {
          const size_t local_row = size_t(py - band_ymin) * size_t(w);
          for (int px = tile.rect.xmin; px < tile.rect.xmax; px++) {
            const size_t l_idx = local_row + size_t(px);
            if (band_cov[l_idx] > 0) {
              const UVTexel &tex = band_tex[l_idx];
              bmin = math::min(bmin, tex.pos);
              bmax = math::max(bmax, tex.pos);
              tile.texels.append(tex);
            }
          }
        }
        tile.bbox_min = bmin;
        tile.bbox_max = bmax;
      }
    }
  });

  runtime.uv_bake_mesh = &mesh;
  runtime.uv_bake_positions_ptr = positions.data();
  runtime.uv_bake_tris_ptr = corner_tris.data();
  runtime.uv_bake_uv_ptr = uv_map.data();
  runtime.uv_bake_verts_num = int(positions.size());
  runtime.uv_bake_tris_num = int(corner_tris.size());
  runtime.uv_bake_w = w;
  runtime.uv_bake_h = h;
  runtime.uv_bake_has_signals = need_signals;
  runtime.last_proj_layer_uid = 0;
}

bool BKE_tex_paint_layer_project_imbuf(NodeTexPaint &data,
                                       TexPaintLayer &layer,
                                       const ImBuf &src_ibuf,
                                       const TexPaintProjectionParams &params,
                                       rcti *r_dirty_rect)
{
  if (src_ibuf.x <= 0 || src_ibuf.y <= 0) {
    return false;
  }
  ImBuf *dst_ibuf = BKE_tex_paint_layer_ensure_ibuf(data, layer);
  if (dst_ibuf == nullptr) {
    return false;
  }

  const PixelSource src(&src_ibuf);
  const PixelSource dst_read(dst_ibuf);
  const PixelTarget dst(dst_ibuf);
  if (!src.is_valid() || !dst.is_valid()) {
    return false;
  }

  const int w = data.width;
  const int h = data.height;
  const int src_w = src_ibuf.x;
  const int src_h = src_ibuf.y;
  const bool src_is_float = src_ibuf.float_data() != nullptr;
  const bool dst_is_float = data.is_float != 0;
  TexPaintRuntime &runtime = runtime_ensure(data);

  int mode = params.mode;
  VArraySpan<float2> uv_map;
  if (ELEM(mode,
           TEXPAINT_PROJ_TRIPLANAR,
           TEXPAINT_PROJ_VIEW_DECAL,
           TEXPAINT_PROJ_WARP,
           TEXPAINT_PROJ_SPHERICAL,
           TEXPAINT_PROJ_CYLINDRICAL))
  {
    if (params.mesh != nullptr) {
      const bke::AttributeAccessor attributes = params.mesh->attributes();
      const StringRef uv_name = params.mesh->active_uv_map_name();
      uv_map = *attributes.lookup<float2>(uv_name, bke::AttrDomain::Corner);
    }
    if (uv_map.is_empty()) {
      mode = (mode == TEXPAINT_PROJ_TRIPLANAR || params.repeat_uv) ? TEXPAINT_PROJ_UV_REPEAT :
                                                                     TEXPAINT_PROJ_UV_FIT;
    }
    else if (mode == TEXPAINT_PROJ_VIEW_DECAL && !params.has_view && !params.use_layer_widget) {
      mode = TEXPAINT_PROJ_UV_FIT;
    }
  }

  if (ELEM(mode, TEXPAINT_PROJ_UV_FIT, TEXPAINT_PROJ_UV_REPEAT)) {
    const bool is_repeat = (mode == TEXPAINT_PROJ_UV_REPEAT);
    threading::parallel_for(IndexRange(h), 16, [&](const IndexRange y_range) {
      for (const int64_t y : y_range) {
        const float v = (float(y) + 0.5f) / float(h);
        const size_t row = size_t(y) * size_t(w);
        for (int x = 0; x < w; x++) {
          const float u = (float(x) + 0.5f) / float(w);
          const float2 src_uv = transform_uv_2d(
              float2(u, v), params.offset, params.scale, params.rotation, is_repeat);
          const float4 sampled = sample_source_bilinear(
              src, src_is_float, dst_is_float, src_w, src_h, src_uv, is_repeat);
          const size_t idx = row + size_t(x);
          if (params.clear_layer) {
            dst.write(idx, sampled);
          }
          else {
            float4 cur = dst_read.read(idx);
            composite_over(cur, sampled.xyz(), sampled.w, IMB_BLEND_MIX);
            dst.write(idx, cur);
          }
        }
      }
    });
    runtime.last_proj_layer_uid = 0;
    if (r_dirty_rect) {
      BLI_rcti_init(r_dirty_rect, 0, w, 0, h);
    }
    BKE_tex_paint_layer_pixels_changed(data, layer, nullptr);
    return true;
  }

  /* 3D Mesh Projection (Triplanar Whole-Model Repeat or 3D Viewport Decal). */
  const Mesh &mesh = *params.mesh;
  if (mesh.vert_positions().is_empty() || mesh.corner_tris().is_empty()) {
    return false;
  }

  ensure_uv_bake_tiles(runtime, mesh, uv_map, w, h, false);
  const float3 bbox_center = runtime.uv_bake_bbox_center;
  const float max_extent = runtime.uv_bake_max_extent;

  const float4x4 ob_mat(params.ob_to_world);
  const float4x4 view_proj(params.view_proj_mat);
  const float3 view_fwd = math::normalize(float3(params.view_forward));
  const float img_aspect = float(src_w) / float(std::max(src_h, 1));
  const float vp_aspect = (params.viewport_aspect > 1e-4f) ? params.viewport_aspect : 1.0f;

  /* 3D Widget orthonormal basis (Euler XYZ in Object space) and half-extents. */
  const float rx = layer.proj_rotation[0];
  const float ry = layer.proj_rotation[1];
  const float rz = layer.proj_rotation[2];
  const float cx = std::cos(rx), sx = std::sin(rx);
  const float cy = std::cos(ry), sy = std::sin(ry);
  const float cz = std::cos(rz), sz = std::sin(rz);
  const float3 w_col0(cy * cz, cy * sz, -sy);
  const float3 w_col1(sy * sx * cz - cx * sz, sy * sx * sz + cx * cz, cy * sx);
  const float3 w_col2(sy * cx * cz + sx * sz, sy * cx * sz - sx * cz, cy * cx);
  const float3 w_loc(layer.proj_location[0], layer.proj_location[1], layer.proj_location[2]);
  const float3 w_scale(std::max(std::abs(layer.proj_scale[0]), 1e-4f),
                       std::max(std::abs(layer.proj_scale[1]), 1e-4f),
                       std::max(std::abs(layer.proj_scale[2]), 1e-4f));
  const float3 inv_w_scale = 1.0f / w_scale;

  auto to_widget_dir = [&](const float3 &v) -> float3 {
    return float3(math::dot(v, w_col0), math::dot(v, w_col1), math::dot(v, w_col2));
  };
  auto to_widget_pt = [&](const float3 &p) -> float3 {
    return to_widget_dir(p - w_loc) * inv_w_scale;
  };

  const bool same_layer = (runtime.last_proj_layer_uid == layer.uid);
  Array<float4> base_pixels;
  if (!params.clear_layer) {
    base_pixels.reinitialize(size_t(w) * size_t(h));
    for (size_t i = 0; i < base_pixels.size(); i++) {
      base_pixels[i] = dst_read.read(i);
    }
  }
  else if (!same_layer) {
    threading::parallel_for(IndexRange(h), 32, [&](const IndexRange y_range) {
      for (const int64_t y : y_range) {
        const size_t row = size_t(y) * size_t(w);
        for (int x = 0; x < w; x++) {
          dst.write(row + size_t(x), float4(0.0f));
        }
      }
    });
    for (UVTile &tile : runtime.uv_tiles) {
      tile.has_prev_pixels = false;
    }
    runtime.last_proj_layer_uid = layer.uid;
  }

  const float sharpness = std::clamp(params.blend_falloff * 12.0f + 1.0f, 1.0f, 16.0f);
  const float falloff_end = std::max(params.blend_falloff * 0.7f, 0.06f);
  const float inv_falloff_range = 1.0f / std::max(falloff_end - 0.05f, 1e-4f);

  const int num_tiles = int(runtime.uv_tiles.size());
  Array<uint8_t> tile_dirty(num_tiles, 0);

  threading::parallel_for(IndexRange(num_tiles), 4, [&](const IndexRange tile_range) {
    for (const int64_t tile_i : tile_range) {
      UVTile &tile = runtime.uv_tiles[tile_i];
      if (tile.texels.is_empty()) {
        continue;
      }

      /* Fast 3D Tile AABB culling for non-repeating Decal mode with 3D widget. */
      if (mode == TEXPAINT_PROJ_VIEW_DECAL && params.use_layer_widget && !params.repeat_uv) {
        const float3 tc = (tile.bbox_min + tile.bbox_max) * 0.5f - w_loc;
        const float3 th = (tile.bbox_max - tile.bbox_min) * 0.5f;
        const float r0 = th.x * std::abs(w_col0.x) + th.y * std::abs(w_col0.y) +
                         th.z * std::abs(w_col0.z);
        const float r1 = th.x * std::abs(w_col1.x) + th.y * std::abs(w_col1.y) +
                         th.z * std::abs(w_col1.z);
        const float r2 = th.x * std::abs(w_col2.x) + th.y * std::abs(w_col2.y) +
                         th.z * std::abs(w_col2.z);
        if (std::abs(math::dot(tc, w_col0)) - r0 > 1.02f * w_scale.x ||
            std::abs(math::dot(tc, w_col1)) - r1 > 1.02f * w_scale.y ||
            std::abs(math::dot(tc, w_col2)) - r2 > 1.02f * w_scale.z)
        {
          if (params.clear_layer && tile.has_prev_pixels) {
            for (const UVTexel &texel : tile.texels) {
              dst.write(texel.pixel_idx, float4(0.0f));
            }
            tile.has_prev_pixels = false;
            tile_dirty[tile_i] = 1;
          }
          continue;
        }
      }

      const bool had_prev = tile.has_prev_pixels;
      bool any_written = false;

      for (const UVTexel &texel : tile.texels) {
        const float3 &P = texel.pos;
        const float3 &N = texel.normal;
        float4 sampled(0.0f);

        if (mode == TEXPAINT_PROJ_TRIPLANAR) {
          float2 uv_yz, uv_xz, uv_xy;
          float3 n_tri;
          if (params.use_layer_widget) {
            const float3 p_loc = to_widget_pt(P);
            n_tri = to_widget_dir(N);
            uv_yz = float2(p_loc.y, p_loc.z) * 0.5f + 0.5f;
            uv_xz = float2(p_loc.x, p_loc.z) * 0.5f + 0.5f;
            uv_xy = float2(p_loc.x, p_loc.y) * 0.5f + 0.5f;
          }
          else {
            const float3 p_norm = (P - bbox_center) / max_extent + float3(0.5f);
            n_tri = N;
            uv_yz = transform_uv_2d(
                float2(p_norm.y, p_norm.z), params.offset, params.scale, params.rotation, true);
            uv_xz = transform_uv_2d(
                float2(p_norm.x, p_norm.z), params.offset, params.scale, params.rotation, true);
            uv_xy = transform_uv_2d(
                float2(p_norm.x, p_norm.y), params.offset, params.scale, params.rotation, true);
          }

          float3 weights(fast_pow_pos(std::abs(n_tri.x), sharpness),
                         fast_pow_pos(std::abs(n_tri.y), sharpness),
                         fast_pow_pos(std::abs(n_tri.z), sharpness));
          const float weight_sum = std::max(weights.x + weights.y + weights.z, 1e-6f);
          weights /= weight_sum;

          if (weights.x > 1e-3f) {
            sampled += sample_source_bilinear(src,
                                              src_is_float,
                                              dst_is_float,
                                              src_w,
                                              src_h,
                                              uv_yz,
                                              params.repeat_uv) *
                       weights.x;
          }
          if (weights.y > 1e-3f) {
            sampled += sample_source_bilinear(src,
                                              src_is_float,
                                              dst_is_float,
                                              src_w,
                                              src_h,
                                              uv_xz,
                                              params.repeat_uv) *
                       weights.y;
          }
          if (weights.z > 1e-3f) {
            sampled += sample_source_bilinear(src,
                                              src_is_float,
                                              dst_is_float,
                                              src_w,
                                              src_h,
                                              uv_xy,
                                              params.repeat_uv) *
                       weights.z;
          }
        }
        else if (mode == TEXPAINT_PROJ_WARP) {
          /* Substance-Painter-style 3D Surface Warp Projection: unfolds radial arc-length across
           * surface depth and bends projection coordinates along local surface normal curvature. */
          const float3 n_loc = to_widget_dir(N);
          const float facing = n_loc.z;
          if (facing <= -0.45f) {
            if (params.clear_layer && had_prev) {
              dst.write(texel.pixel_idx, float4(0.0f));
            }
            continue;
          }
          const float3 p_rel = to_widget_dir(P - w_loc);
          const float z_norm = p_rel.z * inv_w_scale.z;
          if (std::abs(z_norm) > 1.5f) {
            if (params.clear_layer && had_prev) {
              dst.write(texel.pixel_idx, float4(0.0f));
            }
            continue;
          }
          const float warp_fac = std::clamp(params.blend_falloff * 1.6f, 0.0f, 1.6f);
          const float r_xy = std::hypot(p_rel.x, p_rel.y);
          const float z_warp = p_rel.z * warp_fac;
          const float arc_mul = (r_xy > 1e-5f) ?
                                    (std::hypot(r_xy, z_warp) / r_xy) :
                                    1.0f;
          const float curv_bend = warp_fac * 0.42f *
                                  (1.0f - std::clamp(facing, -0.25f, 1.0f));
          const float2 warp_xy(
              p_rel.x * arc_mul * inv_w_scale.x + n_loc.x * curv_bend,
              p_rel.y * arc_mul * inv_w_scale.y + n_loc.y * curv_bend);
          if (!params.repeat_uv &&
              (std::abs(warp_xy.x) > 1.0f || std::abs(warp_xy.y) > 1.0f))
          {
            if (params.clear_layer && had_prev) {
              dst.write(texel.pixel_idx, float4(0.0f));
            }
            continue;
          }
          const float depth_fade = std::clamp((1.5f - std::abs(z_norm)) * 2.5f, 0.0f, 1.0f);
          const float angle_fade = std::clamp((facing + 0.45f) * (1.0f / 0.65f), 0.0f, 1.0f);
          const float2 warp_uv = warp_xy * 0.5f + 0.5f;
          sampled = sample_source_bilinear(
              src, src_is_float, dst_is_float, src_w, src_h, warp_uv, params.repeat_uv);
          sampled.w *= depth_fade * angle_fade;
          if (sampled.w <= 0.0f) {
            if (params.clear_layer && had_prev) {
              dst.write(texel.pixel_idx, float4(0.0f));
            }
            continue;
          }
        }
        else if (ELEM(mode, TEXPAINT_PROJ_SPHERICAL, TEXPAINT_PROJ_CYLINDRICAL)) {
          const float3 p_rel = to_widget_dir(P - w_loc);
          const float u_ang = std::atan2(p_rel.y, p_rel.x) * (1.0f / (2.0f * float(M_PI)));
          float v_coord = 0.0f;
          if (mode == TEXPAINT_PROJ_SPHERICAL) {
            const float r_len = std::max(math::length(p_rel), 1e-6f);
            v_coord = 0.5f - std::acos(std::clamp(p_rel.z / r_len, -1.0f, 1.0f)) *
                                 (1.0f / float(M_PI));
          }
          else {
            v_coord = p_rel.z * inv_w_scale.y * 0.5f;
          }
          const float ref_ext = std::max(max_extent * 0.5f, 1e-3f);
          const float su = ref_ext * inv_w_scale.x;
          const float sv = (mode == TEXPAINT_PROJ_SPHERICAL) ? (ref_ext * inv_w_scale.y) : 1.0f;
          const float2 raw_uv(u_ang * su + 0.5f, v_coord * sv + 0.5f);
          if (!params.repeat_uv &&
              (raw_uv.x < 0.0f || raw_uv.x > 1.0f || raw_uv.y < 0.0f || raw_uv.y > 1.0f))
          {
            if (params.clear_layer && had_prev) {
              dst.write(texel.pixel_idx, float4(0.0f));
            }
            continue;
          }
          sampled = sample_source_bilinear(
              src, src_is_float, dst_is_float, src_w, src_h, raw_uv, params.repeat_uv);
        }
        else {
          /* TEXPAINT_PROJ_VIEW_DECAL (Planar 3D Projection) */
          if (params.use_layer_widget) {
            const float facing = math::dot(N, w_col2);
            if (facing <= 0.05f) {
              if (params.clear_layer && had_prev) {
                dst.write(texel.pixel_idx, float4(0.0f));
              }
              continue;
            }
            const float3 p_loc = to_widget_pt(P);
            if (std::abs(p_loc.z) > 1.0f ||
                (!params.repeat_uv && (std::abs(p_loc.x) > 1.0f || std::abs(p_loc.y) > 1.0f)))
            {
              if (params.clear_layer && had_prev) {
                dst.write(texel.pixel_idx, float4(0.0f));
              }
              continue;
            }
            const float depth_fade = std::clamp((1.0f - std::abs(p_loc.z)) * 5.0f, 0.0f, 1.0f);
            const float facing_alpha =
                std::clamp((facing - 0.05f) * inv_falloff_range, 0.0f, 1.0f) * depth_fade;

            const float2 decal_uv = float2(p_loc.x, p_loc.y) * 0.5f + 0.5f;
            sampled = sample_source_bilinear(
                src, src_is_float, dst_is_float, src_w, src_h, decal_uv, params.repeat_uv);
            sampled.w *= facing_alpha;
            if (sampled.w <= 0.0f) {
              if (params.clear_layer && had_prev) {
                dst.write(texel.pixel_idx, float4(0.0f));
              }
              continue;
            }
          }
          else {
            const float3 P_world = math::transform_point(ob_mat, P);
            const float3 N_world = math::normalize(math::transform_direction(ob_mat, N));
            const float facing = math::dot(N_world, view_fwd);
            if (facing <= 0.05f) {
              if (params.clear_layer && had_prev) {
                dst.write(texel.pixel_idx, float4(0.0f));
              }
              continue;
            }
            const float facing_alpha = std::clamp(
                (facing - 0.05f) * inv_falloff_range, 0.0f, 1.0f);

            const float4 clip = view_proj * float4(P_world, 1.0f);
            if (clip.w <= 1e-5f) {
              if (params.clear_layer && had_prev) {
                dst.write(texel.pixel_idx, float4(0.0f));
              }
              continue;
            }
            const float2 ndc = clip.xy() / clip.w;
            const float2 raw_uv = float2(ndc.x * (vp_aspect / img_aspect), ndc.y) * 0.5f + 0.5f;
            const float2 decal_uv = transform_uv_2d(
                raw_uv, params.offset, params.scale, params.rotation, false);
            sampled = sample_source_bilinear(
                src, src_is_float, dst_is_float, src_w, src_h, decal_uv, false);
            sampled.w *= facing_alpha;
            if (sampled.w <= 0.0f) {
              if (params.clear_layer && had_prev) {
                dst.write(texel.pixel_idx, float4(0.0f));
              }
              continue;
            }
          }
        }

        any_written = true;
        if (params.clear_layer) {
          dst.write(texel.pixel_idx, sampled);
        }
        else {
          float4 cur = base_pixels[texel.pixel_idx];
          composite_over(cur, sampled.xyz(), sampled.w, IMB_BLEND_MIX);
          dst.write(texel.pixel_idx, cur);
        }
      }

      tile.has_prev_pixels = any_written;
      if (any_written || (params.clear_layer && had_prev)) {
        tile_dirty[tile_i] = 1;
      }
    }
  });

  rcti dirty_rect = {w, 0, h, 0};
  if (!same_layer) {
    BLI_rcti_init(&dirty_rect, 0, w, 0, h);
  }
  else {
    for (int i = 0; i < num_tiles; i++) {
      if (tile_dirty[i]) {
        const rcti &tr = runtime.uv_tiles[i].rect;
        dirty_rect.xmin = std::min(dirty_rect.xmin, tr.xmin);
        dirty_rect.xmax = std::max(dirty_rect.xmax, tr.xmax);
        dirty_rect.ymin = std::min(dirty_rect.ymin, tr.ymin);
        dirty_rect.ymax = std::max(dirty_rect.ymax, tr.ymax);
      }
    }
    if (dirty_rect.xmin >= dirty_rect.xmax || dirty_rect.ymin >= dirty_rect.ymax) {
      BLI_rcti_init(&dirty_rect, 0, 0, 0, 0);
    }
  }

  if (r_dirty_rect) {
    *r_dirty_rect = dirty_rect;
  }
  BKE_tex_paint_layer_pixels_changed(data, layer, &dirty_rect);
  return true;
}

bool BKE_tex_paint_layer_reproject(Main &bmain,
                                   const Mesh *mesh,
                                   NodeTexPaint &data,
                                   TexPaintLayer &layer,
                                   rcti *r_dirty_rect)
{
  if (!ELEM(layer.type, TEXPAINT_LAYER_PAINT, TEXPAINT_LAYER_FILL)) {
    return false;
  }
  if (layer.proj_mode == TEXPAINT_PROJ_NONE || layer.proj_image_name[0] == '\0') {
    if (layer.type == TEXPAINT_LAYER_FILL && (layer.ibuf != nullptr || layer.pf != nullptr)) {
      if (layer.ibuf) {
        IMB_freeImBuf(layer.ibuf);
        layer.ibuf = nullptr;
      }
      if (layer.pf) {
        BKE_packedfile_free(layer.pf);
        layer.pf = nullptr;
      }
      BKE_tex_paint_layer_pixels_changed(data, layer, nullptr);
      return true;
    }
    return false;
  }

  Image *src_ima = nullptr;
  for (Image &ima : bmain.images) {
    if (STREQ(ima.id.name + 2, layer.proj_image_name)) {
      src_ima = &ima;
      break;
    }
  }
  if (src_ima == nullptr) {
    return false;
  }

  ImageUser iuser = {};
  BKE_imageuser_default(&iuser);
  void *lock = nullptr;
  ImBuf *src_ibuf = BKE_image_acquire_ibuf(src_ima, &iuser, &lock);
  if (src_ibuf == nullptr) {
    BKE_image_release_ibuf(src_ima, src_ibuf, lock);
    return false;
  }

  TexPaintProjectionParams params;
  params.mode = layer.proj_mode;
  params.use_layer_widget = true;
  params.clear_layer = true;
  params.blend_falloff = layer.proj_blend_falloff;
  params.scale[0] = std::max(std::abs(layer.proj_scale[0]), 1e-3f);
  params.scale[1] = std::max(std::abs(layer.proj_scale[1]), 1e-3f);
  params.offset[0] = layer.proj_location[0];
  params.offset[1] = layer.proj_location[1];
  params.rotation = layer.proj_rotation[2];
  params.repeat_uv = (layer.proj_mode == TEXPAINT_PROJ_TRIPLANAR) ? (layer.proj_scale[2] >= 0.0f) :
                                                                     (layer.proj_scale[2] < 0.0f);
  params.mesh = mesh;

  const bool ok = BKE_tex_paint_layer_project_imbuf(data, layer, *src_ibuf, params, r_dirty_rect);
  BKE_image_release_ibuf(src_ima, src_ibuf, lock);
  return ok;
}

/* -------------------------------------------------------------------- */
/** \name Layer Mask Operations & Substance-Style Smart Mask Generators
 * \{ */

void BKE_tex_paint_layer_mask_fill(NodeTexPaint &data, TexPaintLayer &layer, const float value)
{
  BKE_tex_paint_mask_effects_clear(layer);
  const float v = std::clamp(value, 0.0f, 1.0f);
  layer.mask_base_value = int(v * 255.0f + 0.5f);
  layer.flag |= TEXPAINT_LAYER_USE_MASK;
  layer.flag &= ~TEXPAINT_LAYER_MASK_INVERT;
  layer.mask_gen_type = TEXPAINT_MASK_GEN_NONE;
  if (ImBuf *mask_ibuf = BKE_tex_paint_layer_ensure_mask_ibuf(data, layer)) {
    canvas_buffer_fill(*mask_ibuf, float4(v, v, v, 1.0f));
    runtime_ensure(data).mask_base_layer_uid = 0;
    BKE_tex_paint_layer_pixels_changed(data, layer, nullptr);
  }
}

void BKE_tex_paint_layer_mask_invert_pixels(NodeTexPaint &data, TexPaintLayer &layer)
{
  if (layer.mask_effects_active) {
    layer.flag ^= TEXPAINT_LAYER_MASK_INVERT;
    BKE_tex_paint_cache_invalidate(data);
    return;
  }
  ImBuf *mask_ibuf = BKE_tex_paint_layer_ensure_mask_ibuf(data, layer);
  if (mask_ibuf == nullptr) {
    return;
  }
  const PixelSource src(mask_ibuf);
  const PixelTarget dst(mask_ibuf);
  if (!src.is_valid() || !dst.is_valid()) {
    return;
  }
  const size_t total = size_t(mask_ibuf->x) * size_t(mask_ibuf->y);
  threading::parallel_for(IndexRange(total), 4096, [&](const IndexRange r) {
    for (const int64_t i : r) {
      const float inv_v = std::clamp(1.0f - src.read_value(size_t(i)), 0.0f, 1.0f);
      dst.write(size_t(i), float4(inv_v, inv_v, inv_v, 1.0f));
    }
  });
  runtime_ensure(data).mask_base_layer_uid = 0;
  BKE_tex_paint_layer_pixels_changed(data, layer, nullptr);
}

void BKE_tex_paint_layer_mask_remove(NodeTexPaint &data, TexPaintLayer &layer)
{
  BKE_tex_paint_mask_effects_clear(layer);
  layer.flag &= ~(TEXPAINT_LAYER_USE_MASK | TEXPAINT_LAYER_MASK_INVERT);
  layer.mask_gen_type = TEXPAINT_MASK_GEN_NONE;
  if (layer.mask_ibuf) {
    IMB_freeImBuf(layer.mask_ibuf);
    layer.mask_ibuf = nullptr;
  }
  if (layer.mask_pf) {
    BKE_packedfile_free(layer.mask_pf);
    layer.mask_pf = nullptr;
  }
  if (data.paint_target == TEXPAINT_TARGET_MASK) {
    data.paint_target = TEXPAINT_TARGET_COLOR;
  }
  runtime_ensure(data).mask_base_layer_uid = 0;
  BKE_tex_paint_cache_invalidate(data);
}

static inline float hash_3d_unit(const int x, const int y, const int z)
{
  uint32_t h = uint32_t(x) * 374761393u + uint32_t(y) * 668265263u + uint32_t(z) * 2147483647u;
  h = (h ^ (h >> 13)) * 1274126177u;
  h ^= (h >> 16);
  return float(h & 0x00FFFFFFu) * (1.0f / float(0x00FFFFFFu));
}

static float value_noise_3d(const float3 &p)
{
  const int ix = int(std::floor(p.x));
  const int iy = int(std::floor(p.y));
  const int iz = int(std::floor(p.z));
  const float fx = p.x - float(ix);
  const float fy = p.y - float(iy);
  const float fz = p.z - float(iz);
  const float ux = fx * fx * (3.0f - 2.0f * fx);
  const float uy = fy * fy * (3.0f - 2.0f * fy);
  const float uz = fz * fz * (3.0f - 2.0f * fz);

  const float c000 = hash_3d_unit(ix, iy, iz);
  const float c100 = hash_3d_unit(ix + 1, iy, iz);
  const float c010 = hash_3d_unit(ix, iy + 1, iz);
  const float c110 = hash_3d_unit(ix + 1, iy + 1, iz);
  const float c001 = hash_3d_unit(ix, iy, iz + 1);
  const float c101 = hash_3d_unit(ix + 1, iy, iz + 1);
  const float c011 = hash_3d_unit(ix, iy + 1, iz + 1);
  const float c111 = hash_3d_unit(ix + 1, iy + 1, iz + 1);

  const float x00 = c000 + (c100 - c000) * ux;
  const float x10 = c010 + (c110 - c010) * ux;
  const float x01 = c001 + (c101 - c001) * ux;
  const float x11 = c011 + (c111 - c011) * ux;
  const float y0 = x00 + (x10 - x00) * uy;
  const float y1 = x01 + (x11 - x01) * uy;
  return y0 + (y1 - y0) * uz;
}

static float fbm_grunge_3d(const float3 &p)
{
  const float n0 = value_noise_3d(p);
  const float n1 = value_noise_3d(p * 2.07f + float3(13.7f, 29.3f, 7.1f));
  const float n2 = value_noise_3d(p * 4.19f + float3(41.2f, 11.9f, 53.5f));
  return (n0 * 0.55f + n1 * 0.30f + n2 * 0.15f) * 2.0f - 1.0f;
}

bool BKE_tex_paint_layer_generate_mask(const Mesh *mesh, NodeTexPaint &data, TexPaintLayer &layer)
{
  if (mesh == nullptr || layer.mask_gen_type == TEXPAINT_MASK_GEN_NONE ||
      mesh->vert_positions().is_empty() || mesh->corner_tris().is_empty())
  {
    return false;
  }

  const bke::AttributeAccessor attributes = mesh->attributes();
  const StringRef uv_name = mesh->active_uv_map_name();
  const VArraySpan<float2> uv_map = *attributes.lookup<float2>(uv_name, bke::AttrDomain::Corner);
  if (uv_map.is_empty()) {
    return false;
  }

  layer.flag |= TEXPAINT_LAYER_USE_MASK;
  ImBuf *mask_ibuf = BKE_tex_paint_layer_ensure_mask_ibuf(data, layer);
  if (mask_ibuf == nullptr) {
    return false;
  }

  const int w = data.width;
  const int h = data.height;
  TexPaintRuntime &runtime = runtime_ensure(data);
  const bool need_signals = !ELEM(
      layer.mask_gen_type, TEXPAINT_MASK_GEN_NORMAL_UP, TEXPAINT_MASK_GEN_POSITION_Z);
  ensure_uv_bake_tiles(runtime, *mesh, uv_map, w, h, need_signals);

  const PixelSource mask_src(mask_ibuf);
  const PixelTarget mask_dst(mask_ibuf);
  if (!mask_src.is_valid() || !mask_dst.is_valid()) {
    return false;
  }

  const int gen_type = layer.mask_gen_type;
  const int blend_mode = layer.mask_gen_blend;
  const float balance = std::clamp(layer.mask_gen_balance, 0.0f, 1.0f);
  const float contrast = std::clamp(layer.mask_gen_contrast, 0.0f, 1.0f);
  const float radius = std::clamp(layer.mask_gen_radius, 0.0f, 1.0f);
  const float noise_amt = std::clamp(layer.mask_gen_noise, 0.0f, 1.0f);
  const float noise_scale = std::max(layer.mask_gen_noise_scale, 0.1f);
  const bool invert = (layer.mask_gen_invert != 0);

  const size_t total_px = size_t(w) * size_t(h);
  if (blend_mode != TEXPAINT_MASK_BLEND_REPLACE) {
    if (runtime.mask_base_layer_uid != layer.uid || runtime.mask_base_values.size() != total_px) {
      runtime.mask_base_values.reinitialize(total_px);
      for (size_t i = 0; i < total_px; i++) {
        runtime.mask_base_values[i] = mask_src.read_value(i);
      }
      runtime.mask_base_layer_uid = layer.uid;
    }
  }
  else {
    /* Clear background UV gutter pixels to 0 (or 1 when inverted) in Replace mode. */
    const float bg_val = invert ? 1.0f : 0.0f;
    threading::parallel_for(IndexRange(h), 32, [&](const IndexRange y_range) {
      for (const int64_t y : y_range) {
        const size_t row = size_t(y) * size_t(w);
        for (int x = 0; x < w; x++) {
          mask_dst.write(row + size_t(x), float4(bg_val, bg_val, bg_val, 1.0f));
        }
      }
    });
  }

  const float3 bbox_min = runtime.uv_bake_bbox_min;
  const float3 bbox_max = runtime.uv_bake_bbox_max;
  const float max_extent = runtime.uv_bake_max_extent;
  const float z_span = std::max(bbox_max.z - bbox_min.z, 1e-5f);
  const float pivot = 1.0f - balance;
  const float slope = 1.0f / std::max(1.0f - contrast * 0.96f, 0.025f);

  const int num_tiles = int(runtime.uv_tiles.size());
  threading::parallel_for(IndexRange(num_tiles), 4, [&](const IndexRange tile_range) {
    for (const int64_t tile_i : tile_range) {
      const UVTile &tile = runtime.uv_tiles[tile_i];
      for (const UVTexel &texel : tile.texels) {
        const float curv = math::interpolate(texel.curv_fine, texel.curv_broad, radius);
        const float ao = math::interpolate(texel.ao_near, texel.ao_far, radius);
        float raw = 0.0f;

        switch (gen_type) {
          case TEXPAINT_MASK_GEN_AO:
            /* Ambient Occlusion: 1.0 = exposed, 0.0 = occluded crevice. */
            raw = ao;
            break;
          case TEXPAINT_MASK_GEN_CURVATURE:
            /* Full Curvature map: 0.0 = concave, 0.5 = flat, 1.0 = convex edge. */
            raw = std::clamp(0.5f + curv * 0.9f, 0.0f, 1.0f);
            break;
          case TEXPAINT_MASK_GEN_CAVITY:
            /* Concave Cavity / Crease Dirt: 1.0 in concave crevices & seams, 0.0 on flat/convex. */
            raw = std::clamp(std::max(-curv, 0.0f) * 1.6f + (1.0f - ao) * 0.55f, 0.0f, 1.0f);
            break;
          case TEXPAINT_MASK_GEN_EDGE_WEAR:
            /* Convex Edge Wear: highlights convex ridges attenuated by AO crevices. */
            raw = std::clamp(std::max(curv, 0.0f) * 1.85f * (0.35f + 0.65f * ao), 0.0f, 1.0f);
            break;
          case TEXPAINT_MASK_GEN_NORMAL_UP:
            /* Top-Facing Normal (Dust / Snow / Moss). */
            raw = std::clamp(texel.normal.z * 0.5f + 0.5f, 0.0f, 1.0f);
            break;
          case TEXPAINT_MASK_GEN_POSITION_Z:
            /* Bottom-to-Top Height Gradient. */
            raw = std::clamp((texel.pos.z - bbox_min.z) / z_span, 0.0f, 1.0f);
            break;
          default:
            raw = 1.0f;
            break;
        }

        if (noise_amt > 1e-4f) {
          const float3 p_noise = ((texel.pos - bbox_min) / max_extent) * noise_scale;
          const float g = fbm_grunge_3d(p_noise);
          if (ELEM(gen_type, TEXPAINT_MASK_GEN_EDGE_WEAR, TEXPAINT_MASK_GEN_CAVITY)) {
            /* Modulate primarily near active edges/cavities so flat interiors stay clean. */
            const float edge_mask = std::clamp(raw * 2.5f, 0.0f, 1.0f);
            raw = std::clamp(raw + edge_mask * noise_amt * 0.65f * g, 0.0f, 1.0f);
          }
          else {
            raw = std::clamp(raw + noise_amt * 0.45f * g, 0.0f, 1.0f);
          }
        }

        float val = std::clamp((raw - pivot) * slope + 0.5f, 0.0f, 1.0f);
        const float smooth_val = val * val * (3.0f - 2.0f * val);
        val = math::interpolate(val, smooth_val, contrast);
        if (invert) {
          val = 1.0f - val;
        }

        if (blend_mode != TEXPAINT_MASK_BLEND_REPLACE) {
          const float base_v = runtime.mask_base_values[texel.pixel_idx];
          switch (blend_mode) {
            case TEXPAINT_MASK_BLEND_ADD:
              val = std::clamp(base_v + val, 0.0f, 1.0f);
              break;
            case TEXPAINT_MASK_BLEND_MULTIPLY:
              val = base_v * val;
              break;
            case TEXPAINT_MASK_BLEND_SUBTRACT:
              val = std::clamp(base_v - val, 0.0f, 1.0f);
              break;
            default:
              break;
          }
        }

        mask_dst.write(texel.pixel_idx, float4(val, val, val, 1.0f));
      }
    }
  });

  BKE_tex_paint_layer_pixels_changed(data, layer, nullptr);
  return true;
}

/** \} */

}  // namespace blender
