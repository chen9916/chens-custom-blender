/* SPDX-FileCopyrightText: 2026 Blender Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

#pragma once

#include <cstdint>

/** \file
 * \ingroup bke
 * Native owned paint-mask effects and incremental evaluation of their ordered stack.
 */

#include "DNA_vec_types.h"

namespace blender {

struct BlendDataReader;
struct BlendWriter;
struct ImBuf;
struct NodeTexPaint;
struct TexPaintLayer;
struct TexPaintMaskEffect;

TexPaintMaskEffect *BKE_tex_paint_mask_effect_find(TexPaintLayer &layer, int uid);
TexPaintMaskEffect &BKE_tex_paint_mask_effect_ensure(TexPaintLayer &layer, int uid, int type);
bool BKE_tex_paint_mask_effect_remove(TexPaintLayer &layer, int uid);
void BKE_tex_paint_mask_effects_clear(TexPaintLayer &layer);
void BKE_tex_paint_mask_effects_copy(TexPaintLayer &dst,
                                     const TexPaintLayer &src,
                                     bool copy_pixels);
void BKE_tex_paint_mask_effects_blend_write(BlendWriter &writer,
                                            const NodeTexPaint &data,
                                            const TexPaintLayer &layer);
void BKE_tex_paint_mask_effects_blend_read(BlendDataReader &reader, TexPaintLayer &layer);
void BKE_tex_paint_mask_effects_resize(TexPaintLayer &layer, int width, int height);

/** Byte RGBA: grayscale straight RGB and independent coverage in alpha. */
ImBuf *BKE_tex_paint_mask_effect_ensure_pixels(const NodeTexPaint &data,
                                               TexPaintMaskEffect &effect);
TexPaintMaskEffect *BKE_tex_paint_mask_effect_find_by_buffer(TexPaintLayer &layer,
                                                             const ImBuf *buffer);
/** Null when the selected native effect is absent, disabled, or not PAINT. */
ImBuf *BKE_tex_paint_mask_paint_target(NodeTexPaint &data, TexPaintLayer &layer);

/** Optional diagnostics owned by the caller, never persisted or retained by the evaluator. */
struct TexPaintMaskEvalStats {
  /** Pixels gathered before applying the first effect. */
  uint64_t input_pixels = 0;
  /** Pixels processed across authoring stages, excluding a filter's internal separable passes. */
  uint64_t effect_pixels = 0;
  uint64_t output_pixels = 0;
  /** A local update required the full canvas because of a warp, wrap boundary, or fresh output. */
  bool full_canvas_fallback = false;
};

/**
 * Evaluate into layer.mask_ibuf. Local spatial filters gather their input halo and update only
 * the affected region. Wrap boundaries and absolute-coordinate warps may require the full canvas.
 * The evaluator owns only temporary stage arrays; source/effect pixels remain untouched.
 * r_region reports the actual output region for canvas/GPU updates.
 */
bool BKE_tex_paint_mask_evaluate(NodeTexPaint &data,
                                 TexPaintLayer &layer,
                                 const rcti *rect,
                                 rcti &r_region,
                                 TexPaintMaskEvalStats *stats = nullptr);

}  // namespace blender
