/* SPDX-FileCopyrightText: 2026 Blender Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

#pragma once

/** \file
 * \ingroup bke
 *
 * Layer stack of the layered "Texture Paint" shader node (#SH_NODE_TEX_PAINT).
 *
 * Ownership:
 * - `bNode::id` is the master #Image. Its image buffer receives the composite and is what render
 *   engines and editors sample. It is derived data and is never tagged as modified.
 * - `bNode::storage` is a #NodeTexPaint that owns the #TexPaintLayer list. Layers own private
 *   runtime #ImBuf buffers and packed PNG/EXR copies used for `.blend` storage.
 * - Compositing runs on the CPU over dirty rectangles only, and never requires a shader
 *   recompilation: changes are pushed to the GPU through image partial updates.
 */

#include <memory>

#include "BLI_array.hh"
#include "BLI_math_matrix_types.hh"
#include "BLI_math_vector_types.hh"
#include "BLI_span.hh"
#include "DNA_vec_types.h"

namespace blender {

struct BlendDataReader;
struct BlendWriter;
struct ImBuf;
struct Main;
struct Mesh;
struct bNode;
struct bNodeTree;
struct Image;
struct NodeTexPaint;
struct TexPaintLayer;

enum eTexPaintProjectionMode {
  TEXPAINT_PROJ_NONE = 0,
  TEXPAINT_PROJ_UV_FIT = 1,
  TEXPAINT_PROJ_UV_REPEAT = 2,
  TEXPAINT_PROJ_TRIPLANAR = 3,
  TEXPAINT_PROJ_VIEW_DECAL = 4,
  TEXPAINT_PROJ_WARP = 5,
  TEXPAINT_PROJ_SPHERICAL = 6,
  TEXPAINT_PROJ_CYLINDRICAL = 7,
};

struct TexPaintProjectionParams {
  int mode = TEXPAINT_PROJ_UV_FIT;
  float scale[2] = {1.0f, 1.0f};
  float offset[2] = {0.0f, 0.0f};
  float rotation = 0.0f;
  float blend_falloff = 0.5f;
  bool clear_layer = true;
  bool use_layer_widget = false;
  bool repeat_uv = true;

  const Mesh *mesh = nullptr;
  float ob_to_world[4][4] = {};
  bool has_view = false;
  float view_proj_mat[4][4] = {};
  float view_forward[3] = {0.0f, 0.0f, 1.0f};
  float viewport_aspect = 1.0f;
};

/* -------------------------------------------------------------------- */
/** \name Storage Lifecycle
 * \{ */

/** Free all layers, their pixel buffers and the runtime cache. Does not free `data` itself. */
void BKE_tex_paint_storage_free_contents(NodeTexPaint &data);

/**
 * Copy the layer list of `src` into `dst`, which must be a shallow copy of `src`.
 * \param copy_pixels: When false (evaluated and localized copies), layers are copied without
 * pixels or packed data. Such copies only need the master image, which is shared.
 */
void BKE_tex_paint_storage_copy_contents(NodeTexPaint &dst,
                                         const NodeTexPaint &src,
                                         bool copy_pixels);

/** Write the layer list; encodes layers whose pixels changed since the last encode. */
void BKE_tex_paint_blend_write(BlendWriter &writer, const NodeTexPaint &data);
/** Read the layer list and reset runtime data. Pixels are decoded lazily. */
void BKE_tex_paint_blend_read_data(BlendDataReader &reader, NodeTexPaint &data);

/** \} */

/* -------------------------------------------------------------------- */
/** \name Canvas and Layers
 * \{ */

/** True when the node has a master image and a valid canvas size. */
bool BKE_tex_paint_is_initialized(const bNode &node);

/**
 * True when `ntree` is a material node tree whose `SH_NODE_TEX_PAINT` nodes represent
 * companion PBR channels sharing a layer stack (false for the `.TexPaint_2D_Layers`
 * container tree that holds independent 2D Image Editor documents).
 */
bool BKE_tex_paint_tree_shares_channel_layers(const bNodeTree *ntree);

/**
 * Create the master image and a single "Base" paint layer filled with `color`
 * (scene linear, straight alpha). Replaces existing layers.
 */
Image *BKE_tex_paint_canvas_init(Main &bmain,
                                 bNode &node,
                                 const char *name,
                                 int width,
                                 int height,
                                 bool is_float,
                                 const float color[4]);

TexPaintLayer *BKE_tex_paint_layer_active(NodeTexPaint &data);
int BKE_tex_paint_layer_count(const NodeTexPaint &data);
TexPaintLayer *BKE_tex_paint_layer_find_uid(NodeTexPaint &data, int uid);
/** Find the node owning `layer` in `ntree`. */
bNode *BKE_tex_paint_node_find_by_layer(bNodeTree &ntree, const TexPaintLayer *layer);
/** Find a layered paint node in `ntree` (recursing into groups) using `image` as master. */
bNode *BKE_tex_paint_node_find_by_image(bNodeTree &ntree, const Image *image);

void BKE_tex_paint_layer_unique_name(NodeTexPaint &data, TexPaintLayer &layer);

/**
 * Add a layer above the active layer and make it active.
 * \param type: #eTexPaintLayerType.
 */
TexPaintLayer *BKE_tex_paint_layer_add(NodeTexPaint &data, const char *name, int type);
TexPaintLayer *BKE_tex_paint_layer_duplicate(NodeTexPaint &data, const TexPaintLayer &src);
/**
 * Rasterize an owned Fill layer in-place, preserving its UID, masks, effects and channel settings.
 * The color texture and fill multiplier are baked into canvas-format source pixels; filters remain
 * editable. Texture-driven height is baked into PBR pixels when needed. Byte canvases retain their
 * normal 8-bit quantization. Returns false without replacing source data for unsupported clipping
 * coverage, a projection with no evaluated texture, or unreadable/incompatible buffers.
 * With check_only, validate and prepare allocations without replacing any source data.
 * The caller composites the owning node and records undo.
 */
bool BKE_tex_paint_layer_rasterize(NodeTexPaint &data,
                                   TexPaintLayer &layer,
                                   bool check_only = false);
void BKE_tex_paint_layer_remove(NodeTexPaint &data, TexPaintLayer &layer);
/** Move a layer up (positive) or down (negative) the stack. The active layer follows. */
bool BKE_tex_paint_layer_move(NodeTexPaint &data, TexPaintLayer &layer, int direction);
/** Merge the active layer into the layer below it. */
bool BKE_tex_paint_layer_merge_down(NodeTexPaint &data);

/**
 * Owned, unlinked flattened layer prepared before any source layer is removed. Destroying an
 * uncommitted result releases its private pixels. A committed result transfers that ownership.
 */
struct TexPaintFlattenResult {
  TexPaintLayer *layer = nullptr;
  Array<int> source_uids;

  TexPaintFlattenResult() = default;
  ~TexPaintFlattenResult();
  TexPaintFlattenResult(const TexPaintFlattenResult &) = delete;
  TexPaintFlattenResult &operator=(const TexPaintFlattenResult &) = delete;
};

/** Prepare a safe merge of the active layer and its immediate lower Paint layer. */
std::unique_ptr<TexPaintFlattenResult> BKE_tex_paint_layer_merge_down_prepare(
    NodeTexPaint &data, const char **r_error);

/**
 * Composite the visible stack into independent pixels with mask preview disabled. Every visible
 * layer must be included in `source_uids`; selected hidden layers are discarded without becoming
 * visible. Locked selected layers, spatial specular and nonzero emission are rejected.
 */
std::unique_ptr<TexPaintFlattenResult> BKE_tex_paint_layers_flatten_prepare(NodeTexPaint &data,
                                                                            Span<int> source_uids,
                                                                            const char **r_error);
/**
 * Insert the prepared layer at the bottommost removed layer, preserving remaining layer order.
 * Source layer membership must not change between prepare and commit. `uid` is shared by matching
 * companion channels. This stage only transfers ownership and removes the preflighted layers.
 */
TexPaintLayer *BKE_tex_paint_layers_flatten_commit(NodeTexPaint &data,
                                                   TexPaintFlattenResult &prepared,
                                                   int uid,
                                                   const char *name);
void BKE_tex_paint_layer_active_set(NodeTexPaint &data, int index);

/** Resample every layer to a new canvas size. The caller resizes the master image. */
bool BKE_tex_paint_canvas_resize(NodeTexPaint &data, int width, int height);

/**
 * Project or tile `src_ibuf` into `layer`'s color buffer using UV fit, UV repeat, 3D triplanar
 * box repeat, or 3D viewport decal projection.
 */
bool BKE_tex_paint_layer_project_imbuf(NodeTexPaint &data,
                                       TexPaintLayer &layer,
                                       const ImBuf &src_ibuf,
                                       const TexPaintProjectionParams &params,
                                       rcti *r_dirty_rect = nullptr);

/**
 * Re-project `layer`'s linked source image (`proj_image_name`) into `layer` using the layer's
 * stored 3D projection widget transform (`proj_location`, `proj_rotation`, `proj_scale`,
 * `proj_blend_falloff`, `proj_mode`).
 */
bool BKE_tex_paint_layer_reproject(Main &bmain,
                                   const Mesh *mesh,
                                   NodeTexPaint &data,
                                   TexPaintLayer &layer,
                                   rcti *r_dirty_rect = nullptr);

/** Fill `layer`'s mask buffer with a constant grayscale `value` in `[0, 1]` and enable masking. */
void BKE_tex_paint_layer_mask_fill(NodeTexPaint &data, TexPaintLayer &layer, float value);
/** Invert all pixel values in `layer`'s mask buffer in-place. */
void BKE_tex_paint_layer_mask_invert_pixels(NodeTexPaint &data, TexPaintLayer &layer);
/** Remove `layer`'s mask buffer and disable masking on `layer`. */
void BKE_tex_paint_layer_mask_remove(NodeTexPaint &data, TexPaintLayer &layer);
/**
 * Generate a procedural Substance-style Smart Mask (AO, Curvature, Cavity, Edge Wear, Normal Up,
 * or Height Gradient) into `layer`'s mask buffer using `layer.mask_gen_*` settings.
 */
bool BKE_tex_paint_layer_generate_mask(const Mesh *mesh,
                                       NodeTexPaint &data,
                                       TexPaintLayer &layer);

/** \} */

/* -------------------------------------------------------------------- */
/** \name Pixel Buffers
 * \{ */

/** Get the layer color buffer, decoding it or allocating a transparent buffer as needed. */
ImBuf *BKE_tex_paint_layer_ensure_ibuf(NodeTexPaint &data, TexPaintLayer &layer);
/** Get the layer mask buffer, decoding it or allocating a white (opaque) buffer as needed. */
ImBuf *BKE_tex_paint_layer_ensure_mask_ibuf(NodeTexPaint &data, TexPaintLayer &layer);
/** Get the layer packed PBR buffer (R=Roughness, G=Metallic, B=Height, A=Coverage), allocating as needed. */
ImBuf *BKE_tex_paint_layer_ensure_pbr_ibuf(NodeTexPaint &data, TexPaintLayer &layer);
/** Ensure `node`'s derived `pbr_image` and `emission_image` exist and match the canvas size. */
void BKE_tex_paint_ensure_pbr_images(Main &bmain, bNode &node);
/** Decode all layers from packed data. */
void BKE_tex_paint_ensure_all_ibufs(NodeTexPaint &data);

/**
 * Buffer that brushes should paint into: the active layer color or mask, according to
 * #NodeTexPaint::paint_target. Null when the active layer cannot be painted (hidden,
 * locked or not a pixel layer).
 */
ImBuf *BKE_tex_paint_paint_target_ibuf(NodeTexPaint &data);

/**
 * Notify that layer pixels changed inside `rect` (null for the whole layer). Tags the layer for
 * re-encoding and keeps the below-active cache consistent. Does not composite.
 */
void BKE_tex_paint_layer_pixels_changed(NodeTexPaint &data,
                                        const TexPaintLayer &layer,
                                        const rcti *rect);
/** Expand/clamp a source dirty rectangle to include the layer's spatial effect halo. */
void BKE_tex_paint_layer_effects_expand_rect(const NodeTexPaint &data,
                                             const TexPaintLayer &layer,
                                             rcti &rect);

/** \} */

/* -------------------------------------------------------------------- */
/** \name Compositing
 * \{ */

/** Invalidate the cached composite of the layers below the active layer. */
void BKE_tex_paint_cache_invalidate(NodeTexPaint &data);

/**
 * Composite the layer stack into the master image inside `rect` (null for the whole canvas) and
 * mark that region for partial GPU update. Uses the below-active cache when valid.
 * \return False when the node has no master image or the image buffer does not match the canvas.
 */
bool BKE_tex_paint_composite_rect(bNode &node, const rcti *rect);

/** Full composite, rebuilding the below-active cache. */
bool BKE_tex_paint_composite_full(bNode &node);

/**
 * Full composite only if the master image buffer was not produced by this node's compositor
 * (after file load, image reload or buffer re-creation).
 */
bool BKE_tex_paint_ensure_composited(bNode &node);

/**
 * Ensure every layered paint node in `bmain` has a valid composite. Used after loading a file
 * and after memfile undo, which re-read node storage without runtime data.
 */
void BKE_tex_paint_composite_all(Main &bmain);

/** \} */

/* -------------------------------------------------------------------- */
/** \name Paint Strokes and Undo
 *
 * Brushes paint into the layer buffer returned by #BKE_tex_paint_stroke_begin instead of the
 * master image buffer. Layer buffers carry a synthetic `ImBuf::filepath` so the image undo system
 * stores them as separate buffers of the master image and can find them again on restore.
 * \{ */

/** Find the layered paint node using `image` as master in any node tree of `bmain`. */
bNode *BKE_tex_paint_node_find_in_main(Main &bmain, const Image *image);

/**
 * Prepare a stroke on the master image `master` of `node`.
 * \return The buffer to paint into, or null when the active layer cannot be painted.
 */
ImBuf *BKE_tex_paint_stroke_begin(bNode &node, const ImBuf *master);

/**
 * Human-readable warning message explaining why a brush stroke is blocked on `node`
 * (e.g. Fill or Adjustment layer in Color mode, locked layer, or hidden layer),
 * or null when painting is permitted.
 */
const char *BKE_tex_paint_stroke_block_reason(const bNode &node);

/** True when the active paint target of `node` locks the alpha channel. */
bool BKE_tex_paint_stroke_locks_alpha(const bNode &node);

/** Painting changed `target` inside `rect`: composite that region into the master image. */
void BKE_tex_paint_stroke_update(bNode &node, const ImBuf *target, const rcti &rect);

/** True when `ibuf` is a layer buffer, identified by its synthetic file path. */
bool BKE_tex_paint_is_layer_buffer_name(const char *filepath);

/**
 * Find the layer buffer stored by image undo under `filepath` for the master `image`.
 * Allocates the buffer when the layer exists but has no pixels yet.
 */
ImBuf *BKE_tex_paint_undo_buffer_find(Main &bmain,
                                      const Image *image,
                                      const char *filepath,
                                      bNode **r_node);

/** Undo restored layer pixels inside `rect`: re-pack and composite. */
void BKE_tex_paint_undo_buffer_restored(bNode &node, const ImBuf *target, const rcti &rect);

/** Native, document-owned pixel selections. Empty coverage still restricts editing. */
void BKE_tex_paint_selection_set(NodeTexPaint &data,
                                Span<float> coverage,
                                bool preserve_transform = false);
void BKE_tex_paint_selection_clear(NodeTexPaint &data);
void BKE_tex_paint_selection_transform_clear(NodeTexPaint &data);
void BKE_tex_paint_selection_transform_store(NodeTexPaint &data,
                                            Span<float4> source,
                                            Span<float> selection);
/** Invalidate derived selection outlines without touching compositing caches. */
void BKE_tex_paint_selection_tag_changed(NodeTexPaint &data);
/** Pairs of pixel-space endpoints, cached until coverage changes. */
Span<int2> BKE_tex_paint_selection_contours(NodeTexPaint &data);
Span<float> BKE_tex_paint_selection(const NodeTexPaint &data);
float BKE_tex_paint_selection_value(const bNode &node, int x, int y);
Array<float> BKE_tex_paint_select_wand(const ImBuf &image,
                                       int2 seed,
                                       float tolerance,
                                       bool contiguous);
Array<float> BKE_tex_paint_select_polygon(int width, int height, Span<float2> polygon);
/** Pixels are premultiplied in the buffer's color space; float HDR values are preserved. */
Array<float4> BKE_tex_paint_pixels_read(const ImBuf &image);
uint64_t BKE_tex_paint_pixels_hash(const ImBuf &image);
void BKE_tex_paint_pixels_write(ImBuf &image, Span<float4> pixels, const rcti &rect);
/** Transform selected coverage and pixels from an immutable source. Pixel-space coordinates. */
void BKE_tex_paint_pixels_transform(Span<float4> source,
                                    Span<float> selection,
                                    int width,
                                    int height,
                                    float2 pivot,
                                    float2 translation,
                                    float angle,
                                    float2 scale,
                                    MutableSpan<float4> output,
                                    MutableSpan<float> output_selection,
                                    const rcti &rect);
/** General affine transform, including accumulated non-uniform scales and rotations. */
void BKE_tex_paint_pixels_transform_affine(Span<float4> source,
                                         Span<float> selection,
                                         int width,
                                         int height,
                                         const float3x3 &transform,
                                         MutableSpan<float4> output,
                                         MutableSpan<float> output_selection,
                                         const rcti &rect);

/** \} */

}  // namespace blender
