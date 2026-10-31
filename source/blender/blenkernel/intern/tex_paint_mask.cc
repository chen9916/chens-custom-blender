/* SPDX-FileCopyrightText: 2026 Blender Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

/** \file
 * \ingroup bke
 *
 * Owned painted-mask coverage and native evaluation before viewport image uploads.
 */

#include <algorithm>
#include <cmath>
#include <cstring>

#include "MEM_guardedalloc.h"

#include "DNA_node_tex_paint_types.h"
#include "DNA_packedFile_types.h"

#include "BLI_array.hh"
#include "BLI_implicit_sharing.hh"
#include "BLI_listbase.hh"
#include "BLI_math_base_c.hh"
#include "BLI_rect.hh"
#include "BLI_task.hh"
#include "BLI_vector.hh"

#include "BKE_packedFile.hh"
#include "BKE_tex_paint_layer.hh"
#include "BKE_tex_paint_mask.hh"

#include "IMB_imbuf.hh"
#include "IMB_imbuf_types.hh"

#include "BLO_read_write.hh"

namespace blender {

namespace {

static uint8_t unit_byte(const float value)
{
  return uint8_t(std::clamp(value, 0.0f, 1.0f) * 255.0f + 0.5f);
}

static void effect_free(TexPaintMaskEffect &effect)
{
  if (effect.pixels) {
    IMB_freeImBuf(effect.pixels);
  }
  if (effect.pixels_packed) {
    BKE_packedfile_free(effect.pixels_packed);
  }
  MEM_delete(&effect);
}

static ImBuf *effect_unpack(const PackedFile *packed)
{
  if (!packed || !packed->data || packed->size <= 0) {
    return nullptr;
  }
  /* PNG stores the native buffer's straight RGB and independent coverage exactly. */
  return IMB_load_image_from_memory(static_cast<const uchar *>(packed->data),
                                    size_t(packed->size),
                                    ImBufFlags::ByteData | ImBufFlags::AlphaDetect,
                                    "tex_paint_mask_effect");
}

static void effect_pack(TexPaintMaskEffect &effect)
{
  if (!effect.pixels || (!effect.runtime_dirty && effect.pixels_packed)) {
    return;
  }
  effect.pixels->ftype = IMB_FTYPE_PNG;
  effect.pixels->foptions.flag = 0;
  effect.pixels->foptions.compress = 15;
  Vector<uint8_t> encoded = IMB_save_image_to_buffer(effect.pixels, ImBufFlags::ByteData);
  if (encoded.is_empty()) {
    return;
  }
  auto *shared_data = new ImplicitSharedValue<Vector<uint8_t>>(std::move(encoded));
  PackedFile *packed = BKE_packedfile_new_from_memory(
      shared_data->data.data(), int(shared_data->data.size()), shared_data);
  if (effect.pixels_packed) {
    BKE_packedfile_free(effect.pixels_packed);
  }
  effect.pixels_packed = packed;
  effect.runtime_dirty = 0;
}

static float blend_value(const float base, const float source, const int mode)
{
  switch (mode) {
    case TEXPAINT_MASK_EFFECT_ADD:
      return std::min(base + source, 1.0f);
    case TEXPAINT_MASK_EFFECT_SUBTRACT:
      return std::max(base - source, 0.0f);
    case TEXPAINT_MASK_EFFECT_MULTIPLY:
      return base * source;
    case TEXPAINT_MASK_EFFECT_SCREEN:
      return base + source - base * source;
    case TEXPAINT_MASK_EFFECT_OVERLAY:
      return base <= 0.5f ? 2.0f * base * source : 1.0f - 2.0f * (1.0f - base) * (1.0f - source);
    case TEXPAINT_MASK_EFFECT_MAX:
      return std::max(base, source);
    case TEXPAINT_MASK_EFFECT_MIN:
      return std::min(base, source);
    default:
      return source;
  }
}

static uint8_t blend_source(const uint8_t base,
                            const float source,
                            const int mode,
                            const float opacity)
{
  const float b = float(base) / 255.0f;
  const float value = blend_value(b, std::clamp(source, 0.0f, 1.0f), mode);
  return unit_byte(opacity < 0.9999f ? b + (value - b) * opacity : value);
}

static uint8_t blend_paint(const uint8_t base,
                           const uint8_t *pixel,
                           const TexPaintMaskEffect &effect)
{
  const int value = effect.invert ? 255 - pixel[0] : pixel[0];
  const int opacity = int(std::nearbyint(std::clamp(effect.opacity, 0.0f, 1.0f) * 256.0f));
  const int alpha = (int(pixel[3]) * opacity) >> 8;
  /* Preserve the established integer mask arithmetic, including additive coverage. */
  switch (effect.blend_mode) {
    case TEXPAINT_MASK_EFFECT_ADD:
      return uint8_t(std::min(int(base) + (value * alpha + 127) / 255, 255));
    case TEXPAINT_MASK_EFFECT_SUBTRACT:
      return uint8_t(std::max(int(base) - (value * alpha + 127) / 255, 0));
    case TEXPAINT_MASK_EFFECT_MULTIPLY: {
      const int multiplied = (int(base) * value + 127) / 255;
      return uint8_t((int(base) * (255 - alpha) + multiplied * alpha + 127) / 255);
    }
    case TEXPAINT_MASK_EFFECT_MAX:
      return uint8_t((int(base) * (255 - alpha) + std::max(int(base), value) * alpha + 127) / 255);
    case TEXPAINT_MASK_EFFECT_MIN:
      return uint8_t((int(base) * (255 - alpha) + std::min(int(base), value) * alpha + 127) / 255);
    case TEXPAINT_MASK_EFFECT_SCREEN:
    case TEXPAINT_MASK_EFFECT_OVERLAY:
      return blend_source(base, float(value) / 255.0f, effect.blend_mode, float(alpha) / 255.0f);
    default:
      return uint8_t((int(base) * (255 - alpha) + value * alpha + 127) / 255);
  }
}

struct PreparedEffect {
  const TexPaintMaskEffect *effect;
  const uint8_t *pixels;
};

static bool filter_is_pointwise(const TexPaintMaskEffect &effect)
{
  return ELEM(effect.filter_type, TEXPAINT_MASK_FILTER_THRESHOLD, TEXPAINT_MASK_FILTER_POSTERIZE);
}

static uint8_t apply_pointwise_filter(const uint8_t base, const TexPaintMaskEffect &effect)
{
  const float source = float(base) / 255.0f;
  float value;
  if (effect.filter_type == TEXPAINT_MASK_FILTER_THRESHOLD) {
    const float threshold = std::clamp(1.0f - effect.filter_strength * 0.5f, 0.02f, 0.98f);
    const float softness = std::max(effect.filter_radius / 128.0f, 0.005f);
    value = std::clamp((source - (threshold - softness)) / (2.0f * softness), 0.0f, 1.0f);
  }
  else {
    const float bands = float(std::clamp(int(std::nearbyint(effect.filter_radius)), 2, 16));
    value = std::nearbyint(source * bands) / bands;
  }
  const uint8_t filtered = unit_byte(effect.invert ? 1.0f - value : value);
  return blend_source(
      base, float(filtered) / 255.0f, effect.blend_mode, std::clamp(effect.opacity, 0.0f, 1.0f));
}

static Vector<PreparedEffect> prepare_effects(const NodeTexPaint &data, TexPaintLayer &layer)
{
  Vector<PreparedEffect> result;
  for (TexPaintMaskEffect &effect : layer.mask_effects) {
    if (!effect.enabled || effect.opacity <= 1e-5f) {
      continue;
    }
    ImBuf *buffer = nullptr;
    if (ELEM(effect.type, TEXPAINT_MASK_EFFECT_SOURCE, TEXPAINT_MASK_EFFECT_PAINT)) {
      buffer = BKE_tex_paint_mask_effect_ensure_pixels(data, effect);
      if (!buffer) {
        continue;
      }
    }
    result.append({&effect, buffer ? buffer->byte_data() : nullptr});
  }
  return result;
}

static uint8_t initial_value(const TexPaintLayer &layer)
{
  for (const TexPaintMaskEffect &effect : layer.mask_effects) {
    if (!effect.enabled || effect.opacity <= 1e-5f) {
      continue;
    }
    if (effect.type == TEXPAINT_MASK_EFFECT_PAINT) {
      break;
    }
    switch (effect.blend_mode) {
      case TEXPAINT_MASK_EFFECT_ADD:
      case TEXPAINT_MASK_EFFECT_SCREEN:
      case TEXPAINT_MASK_EFFECT_MAX:
        return 0;
      case TEXPAINT_MASK_EFFECT_MULTIPLY:
      case TEXPAINT_MASK_EFFECT_SUBTRACT:
      case TEXPAINT_MASK_EFFECT_MIN:
        return 255;
      default:
        break;
    }
    break;
  }
  return uint8_t(std::clamp(layer.mask_base_value, 0, 255));
}

static uint8_t apply_pointwise(const uint8_t base,
                               const PreparedEffect &prepared,
                               const int64_t index)
{
  const TexPaintMaskEffect &effect = *prepared.effect;
  if (effect.type == TEXPAINT_MASK_EFFECT_PAINT) {
    return blend_paint(base, prepared.pixels + index * 4, effect);
  }
  if (effect.type == TEXPAINT_MASK_EFFECT_FILTER) {
    BLI_assert(filter_is_pointwise(effect));
    return apply_pointwise_filter(base, effect);
  }
  const float opacity = std::clamp(effect.opacity, 0.0f, 1.0f);
  if (effect.type == TEXPAINT_MASK_EFFECT_LEVELS) {
    return blend_source(base, effect.curve[base], TEXPAINT_MASK_EFFECT_REPLACE, opacity);
  }
  return blend_source(base, effect.curve[prepared.pixels[index * 4]], effect.blend_mode, opacity);
}

/** Match the author's two-pass, even-width box kernel with edge extension. */
static Array<float> box_blur(const Span<float> source,
                             const int width,
                             const int height,
                             int radius)
{
  radius = std::clamp(radius, 1, 64);
  Array<float> a(source), b(source.size());
  const double divisor = 1.0 / double(2 * radius);
  for (int pass = 0; pass < 2; pass++) {
    threading::parallel_for(IndexRange(width), 128, [&](const IndexRange columns) {
      /* Keep neighboring column accumulators together, so the vertical pass reads
       * contiguous row segments rather than striding through the complete image per pixel. */
      Array<double> sums(columns.size(), 0.0);
      for (int y = 1 - radius; y <= radius; y++) {
        const int64_t row = int64_t(std::clamp(y, 0, height - 1)) * width;
        for (const int x : columns) {
          sums[x - columns.start()] += a[row + x];
        }
      }
      for (int y = 0; y < height; y++) {
        const int64_t row = int64_t(y) * width;
        const int64_t added = int64_t(std::clamp(y + radius + 1, 0, height - 1)) * width;
        const int64_t removed = int64_t(std::clamp(y - radius + 1, 0, height - 1)) * width;
        for (const int x : columns) {
          double &sum = sums[x - columns.start()];
          b[row + x] = float(sum * divisor);
          sum += double(a[added + x]) - double(a[removed + x]);
        }
      }
    });
    threading::parallel_for(IndexRange(height), 32, [&](const IndexRange rows) {
      for (const int y : rows) {
        const int64_t row = int64_t(y) * width;
        double sum = 0.0;
        for (int x = 1 - radius; x <= radius; x++) {
          sum += b[row + std::clamp(x, 0, width - 1)];
        }
        for (int x = 0; x < width; x++) {
          a[row + x] = float(sum * divisor);
          sum += double(b[row + std::clamp(x + radius + 1, 0, width - 1)]) -
                 double(b[row + std::clamp(x - radius + 1, 0, width - 1)]);
        }
      }
    });
  }
  return a;
}

static Array<float> dilate_erode(
    const Span<float> source, const int width, const int height, const int steps, const bool grow)
{
  Array<float> current(source), temporary(source.size()), horizontal(source.size());
  /* Match the existing wrap-around grow/shrink operation, including separable square support. */
  for (int step = 1; step <= steps; step++) {
    threading::parallel_for(IndexRange(height), 16, [&](const IndexRange rows) {
      for (const int y : rows) {
        for (int x = 0; x < width; x++) {
          const int64_t index = int64_t(y) * width + x;
          const int y1 = (y + step) % height;
          const int y2 = ((y - step) % height + height) % height;
          const float a = source[int64_t(y1) * width + x];
          const float b = source[int64_t(y2) * width + x];
          temporary[index] = grow ? std::max({current[index], a, b}) :
                                    std::min({current[index], a, b});
        }
      }
    });
    threading::parallel_for(IndexRange(height), 16, [&](const IndexRange rows) {
      for (const int y : rows) {
        for (int x = 0; x < width; x++) {
          const int64_t index = int64_t(y) * width + x;
          const int sx = ((x - step) % width + width) % width;
          const float sample = temporary[int64_t(y) * width + sx];
          horizontal[index] = grow ? std::max(temporary[index], sample) :
                                     std::min(temporary[index], sample);
        }
      }
    });
    threading::parallel_for(IndexRange(height), 16, [&](const IndexRange rows) {
      for (const int y : rows) {
        for (int x = 0; x < width; x++) {
          const int64_t index = int64_t(y) * width + x;
          const float sample = horizontal[int64_t(y) * width + (x + step) % width];
          current[index] = grow ? std::max(horizontal[index], sample) :
                                  std::min(horizontal[index], sample);
        }
      }
    });
  }
  return current;
}

static void apply_filter(MutableSpan<uint8_t> accumulated,
                         const int width,
                         const int height,
                         const TexPaintMaskEffect &effect)
{
  if (filter_is_pointwise(effect)) {
    threading::parallel_for(accumulated.index_range(), 4096, [&](const IndexRange range) {
      for (const int64_t i : range) {
        accumulated[i] = apply_pointwise_filter(accumulated[i], effect);
      }
    });
    return;
  }
  Array<float> source(accumulated.size());
  threading::parallel_for(source.index_range(), 4096, [&](const IndexRange range) {
    for (const int64_t i : range) {
      source[i] = float(accumulated[i]) / 255.0f;
    }
  });
  const float radius = effect.filter_radius;
  const int radius_px = std::clamp(int(std::nearbyint(radius)), 1, 64);
  const float strength = effect.filter_strength;
  Array<float> result;
  switch (effect.filter_type) {
    case TEXPAINT_MASK_FILTER_BLUR:
      result = box_blur(source, width, height, radius_px);
      for (const int64_t i : result.index_range()) {
        result[i] = source[i] + (result[i] - source[i]) * std::clamp(strength, 0.0f, 1.0f);
      }
      break;
    case TEXPAINT_MASK_FILTER_BLUR_SLOPE: {
      result.reinitialize(source.size());
      const float scale = std::max(effect.filter_scale, 0.5f);
      const float amplitude = radius * strength * (float(std::max(width, height)) / 1024.0f);
      threading::parallel_for(IndexRange(height), 16, [&](const IndexRange rows) {
        for (const int y : rows) {
          const float py = height > 1 ? float(y) / float(height - 1) * scale * 2.0f * float(M_PI) :
                                        0.0f;
          for (int x = 0; x < width; x++) {
            const float px = width > 1 ? float(x) / float(width - 1) * scale * 2.0f * float(M_PI) :
                                         0.0f;
            const float dx = std::sin(px + std::cos(py * 1.3f + 0.7f)) * 0.65f +
                             std::cos(px * 2.3f - py * 1.9f + 1.4f) * 0.35f;
            const float dy = std::cos(py + std::sin(px * 1.3f - 0.5f)) * 0.65f +
                             std::sin(py * 2.1f + px * 1.7f - 1.1f) * 0.35f;
            float value = 0.0f;
            for (const float tap : {0.35f, 0.70f, 1.0f}) {
              const int sy = std::clamp(int(float(y) + dy * amplitude * tap), 0, height - 1);
              const int sx = std::clamp(int(float(x) + dx * amplitude * tap), 0, width - 1);
              value += source[int64_t(sy) * width + sx];
            }
            result[int64_t(y) * width + x] = value / 3.0f;
          }
        }
      });
      break;
    }
    case TEXPAINT_MASK_FILTER_SHARPEN:
      result = box_blur(source, width, height, std::max(radius_px / 2, 1));
      for (const int64_t i : result.index_range()) {
        result[i] = std::clamp(
            source[i] + (source[i] - result[i]) * (strength * 2.5f), 0.0f, 1.0f);
      }
      break;
    case TEXPAINT_MASK_FILTER_DILATE_ERODE: {
      const float signed_strength = (strength - 0.5f) * 2.0f;
      const int steps = std::max(1, int(std::nearbyint(std::abs(signed_strength) * radius_px)));
      result = dilate_erode(source, width, height, steps, signed_strength >= 0.0f);
      if (steps > 1) {
        result = box_blur(result, width, height, 1);
      }
      break;
    }
    case TEXPAINT_MASK_FILTER_BEVEL:
      result = dilate_erode(source, width, height, std::max(radius_px / 2, 1), true);
      result = box_blur(result, width, height, radius_px);
      if (std::abs(strength - 1.0f) > 1e-3f) {
        for (float &value : result) {
          value = std::clamp((value - 0.5f) * (0.5f + strength) + 0.5f, 0.0f, 1.0f);
        }
      }
      break;
    case TEXPAINT_MASK_FILTER_HIGHPASS:
    default:
      result = box_blur(source, width, height, radius_px);
      for (const int64_t i : result.index_range()) {
        result[i] = std::clamp(0.5f + (source[i] - result[i]) * (strength * 2.0f), 0.0f, 1.0f);
      }
      break;
  }
  const float opacity = std::clamp(effect.opacity, 0.0f, 1.0f);
  threading::parallel_for(result.index_range(), 4096, [&](const IndexRange range) {
    for (const int64_t i : range) {
      const uint8_t filtered = unit_byte(effect.invert ? 1.0f - result[i] : result[i]);
      accumulated[i] = blend_source(
          accumulated[i], float(filtered) / 255.0f, effect.blend_mode, opacity);
    }
  });
}

static void write_output(uint8_t *bytes, float *floats, const int64_t index, const uint8_t value)
{
  if (bytes) {
    uint8_t *pixel = bytes + index * 4;
    pixel[0] = pixel[1] = pixel[2] = value;
    pixel[3] = 255;
  }
  else if (floats) {
    float *pixel = floats + index * 4;
    pixel[0] = pixel[1] = pixel[2] = float(value) / 255.0f;
    pixel[3] = 1.0f;
  }
}

struct FilterSupport {
  int64_t x = 0;
  int64_t y = 0;
  bool wrap = false;
  bool absolute_coordinates = false;
};

static FilterSupport filter_support(const TexPaintMaskEffect &effect)
{
  if (effect.type != TEXPAINT_MASK_EFFECT_FILTER || filter_is_pointwise(effect)) {
    return {};
  }
  const int radius = std::clamp(int(std::nearbyint(effect.filter_radius)), 1, 64);
  switch (effect.filter_type) {
    case TEXPAINT_MASK_FILTER_BLUR_SLOPE:
      return {0, 0, false, true};
    case TEXPAINT_MASK_FILTER_SHARPEN: {
      const int blur = 2 * std::max(radius / 2, 1);
      return {blur, blur};
    }
    case TEXPAINT_MASK_FILTER_DILATE_ERODE: {
      const float signed_strength = (effect.filter_strength - 0.5f) * 2.0f;
      const int64_t steps = std::max(1, int(std::nearbyint(std::abs(signed_strength) * radius)));
      const int blur = steps > 1 ? 2 : 0;
      /* Horizontal morphology expands at each step; vertical neighbors always sample the
       * original stage. Preserve that established, wrapped behavior conservatively. */
      return {steps * (steps + 1) / 2 + blur, steps + blur, true};
    }
    case TEXPAINT_MASK_FILTER_BEVEL: {
      const int64_t steps = std::max(radius / 2, 1);
      return {steps * (steps + 1) / 2 + 2 * radius, steps + 2 * radius, true};
    }
    default:
      /* Two separable even-width box passes; support is asymmetric by two texels, so
       * a symmetric 2r halo includes both input dependencies and changed-output influence. */
      return {2 * radius, 2 * radius};
  }
}

static rcti expanded_region(const NodeTexPaint &data,
                            const rcti &region,
                            const int64_t support_x,
                            const int64_t support_y)
{
  return {int(std::max<int64_t>(int64_t(region.xmin) - support_x, 0)),
          int(std::min<int64_t>(int64_t(region.xmax) + support_x, data.width)),
          int(std::max<int64_t>(int64_t(region.ymin) - support_y, 0)),
          int(std::min<int64_t>(int64_t(region.ymax) + support_y, data.height))};
}

static uint64_t region_pixels(const rcti &region)
{
  return uint64_t(BLI_rcti_size_x(&region)) * uint64_t(BLI_rcti_size_y(&region));
}

static Array<uint8_t> cropped_stage(const Span<uint8_t> source,
                                    const rcti &source_region,
                                    const rcti &region)
{
  const int source_width = BLI_rcti_size_x(&source_region);
  const int width = BLI_rcti_size_x(&region);
  Array<uint8_t> result(region_pixels(region));
  for (int y = region.ymin; y < region.ymax; y++) {
    const int64_t src = int64_t(y - source_region.ymin) * source_width + region.xmin -
                        source_region.xmin;
    const int64_t dst = int64_t(y - region.ymin) * width;
    std::copy_n(source.data() + src, width, result.data() + dst);
  }
  return result;
}

}  // namespace

TexPaintMaskEffect *BKE_tex_paint_mask_effect_find(TexPaintLayer &layer, const int uid)
{
  for (TexPaintMaskEffect &effect : layer.mask_effects) {
    if (effect.uid == uid) {
      return &effect;
    }
  }
  return nullptr;
}

TexPaintMaskEffect &BKE_tex_paint_mask_effect_ensure(TexPaintLayer &layer,
                                                     const int uid,
                                                     const int type)
{
  if (TexPaintMaskEffect *existing = BKE_tex_paint_mask_effect_find(layer, uid)) {
    existing->type = type;
    return *existing;
  }
  TexPaintMaskEffect *effect = MEM_new<TexPaintMaskEffect>(__func__);
  effect->uid = uid;
  effect->type = type;
  for (int i = 0; i < 256; i++) {
    effect->curve[i] = float(i) / 255.0f;
  }
  BLI_addtail(&layer.mask_effects, effect);
  return *effect;
}

bool BKE_tex_paint_mask_effect_remove(TexPaintLayer &layer, const int uid)
{
  TexPaintMaskEffect *effect = BKE_tex_paint_mask_effect_find(layer, uid);
  if (!effect) {
    return false;
  }
  BLI_remlink(&layer.mask_effects, effect);
  effect_free(*effect);
  if (layer.active_mask_effect_uid == uid) {
    layer.active_mask_effect_uid = 0;
  }
  return true;
}

void BKE_tex_paint_mask_effects_clear(TexPaintLayer &layer)
{
  for (TexPaintMaskEffect &effect : layer.mask_effects.items_mutable()) {
    effect_free(effect);
  }
  layer.mask_effects.clear_no_delete();
  layer.active_mask_effect_uid = 0;
  layer.mask_effects_active = 0;
}

void BKE_tex_paint_mask_effects_copy(TexPaintLayer &dst,
                                     const TexPaintLayer &src,
                                     const bool copy_pixels)
{
  dst.mask_effects.clear_no_delete();
  if (!copy_pixels) {
    dst.active_mask_effect_uid = 0;
    dst.mask_effects_active = 0;
    return;
  }
  for (const TexPaintMaskEffect &effect : src.mask_effects) {
    TexPaintMaskEffect *copy = MEM_new<TexPaintMaskEffect>(__func__, dna::shallow_copy(effect));
    copy->next = copy->prev = nullptr;
    copy->pixels = effect.pixels ? IMB_dupImBuf(effect.pixels) : nullptr;
    copy->pixels_packed = effect.pixels_packed ? BKE_packedfile_duplicate(effect.pixels_packed) :
                                                 nullptr;
    BLI_addtail(&dst.mask_effects, copy);
  }
}

void BKE_tex_paint_mask_effects_blend_write(BlendWriter &writer,
                                            const NodeTexPaint & /*data*/,
                                            const TexPaintLayer &layer)
{
  for (const TexPaintMaskEffect &effect : layer.mask_effects) {
    effect_pack(const_cast<TexPaintMaskEffect &>(effect));
  }
  writer.write_struct_list(&layer.mask_effects);
  for (const TexPaintMaskEffect &effect : layer.mask_effects) {
    if (effect.pixels_packed) {
      BKE_packedfile_blend_write(&writer, effect.pixels_packed);
    }
  }
}

void BKE_tex_paint_mask_effects_blend_read(BlendDataReader &reader, TexPaintLayer &layer)
{
  BLO_read_struct_list(&reader, TexPaintMaskEffect, &layer.mask_effects);
  for (TexPaintMaskEffect &effect : layer.mask_effects) {
    effect.pixels = nullptr;
    effect.runtime_dirty = 0;
    BKE_packedfile_blend_read(&reader, &effect.pixels_packed, "Paint Mask Effect");
  }
}

void BKE_tex_paint_mask_effects_resize(TexPaintLayer &layer, const int width, const int height)
{
  for (TexPaintMaskEffect &effect : layer.mask_effects) {
    if (!effect.pixels) {
      effect.pixels = effect_unpack(effect.pixels_packed);
    }
    if (effect.pixels && (effect.pixels->x != width || effect.pixels->y != height)) {
      IMB_scale(effect.pixels, width, height, IMBScaleFilter::Bilinear);
      effect.runtime_dirty = 1;
      effect.revision++;
    }
  }
}

ImBuf *BKE_tex_paint_mask_effect_ensure_pixels(const NodeTexPaint &data,
                                               TexPaintMaskEffect &effect)
{
  if (data.width <= 0 || data.height <= 0 ||
      !ELEM(effect.type, TEXPAINT_MASK_EFFECT_SOURCE, TEXPAINT_MASK_EFFECT_PAINT))
  {
    return nullptr;
  }
  if (!effect.pixels) {
    effect.pixels = effect_unpack(effect.pixels_packed);
  }
  if (effect.pixels && (effect.pixels->x != data.width || effect.pixels->y != data.height)) {
    IMB_scale(effect.pixels, data.width, data.height, IMBScaleFilter::Bilinear);
    effect.runtime_dirty = 1;
    effect.revision++;
  }
  if (!effect.pixels) {
    effect.pixels = IMB_allocImBuf(data.width, data.height, ImBufFlags::ByteData);
    if (effect.pixels) {
      std::memset(effect.pixels->byte_data_for_write(), 0, size_t(data.width) * data.height * 4);
      effect.runtime_dirty = 1;
    }
  }
  return effect.pixels;
}

TexPaintMaskEffect *BKE_tex_paint_mask_effect_find_by_buffer(TexPaintLayer &layer,
                                                             const ImBuf *buffer)
{
  if (buffer) {
    for (TexPaintMaskEffect &effect : layer.mask_effects) {
      if (effect.pixels == buffer) {
        return &effect;
      }
    }
  }
  return nullptr;
}

ImBuf *BKE_tex_paint_mask_paint_target(NodeTexPaint &data, TexPaintLayer &layer)
{
  TexPaintMaskEffect *effect = BKE_tex_paint_mask_effect_find(layer, layer.active_mask_effect_uid);
  if (!layer.mask_effects_active || !effect || !effect->enabled ||
      effect->type != TEXPAINT_MASK_EFFECT_PAINT)
  {
    return nullptr;
  }
  return BKE_tex_paint_mask_effect_ensure_pixels(data, *effect);
}

bool BKE_tex_paint_mask_evaluate(NodeTexPaint &data,
                                 TexPaintLayer &layer,
                                 const rcti *rect,
                                 rcti &r_region,
                                 TexPaintMaskEvalStats *stats)
{
  if (stats) {
    *stats = {};
  }
  if (!layer.mask_effects_active || data.width <= 0 || data.height <= 0) {
    return false;
  }
  const bool fresh_output = (!layer.mask_ibuf && !layer.mask_pf) ||
                            (layer.mask_ibuf && (layer.mask_ibuf->x != data.width ||
                                                 layer.mask_ibuf->y != data.height));
  ImBuf *output = BKE_tex_paint_layer_ensure_mask_ibuf(data, layer);
  if (!output) {
    return false;
  }
  const Vector<PreparedEffect> effects = prepare_effects(data, layer);
  FilterSupport support;
  bool spatial = false;
  for (const PreparedEffect &prepared : effects) {
    const FilterSupport current = filter_support(*prepared.effect);
    spatial |= prepared.effect->type == TEXPAINT_MASK_EFFECT_FILTER &&
               !filter_is_pointwise(*prepared.effect);
    support.x += current.x;
    support.y += current.y;
    support.wrap |= current.wrap;
    support.absolute_coordinates |= current.absolute_coordinates;
  }
  r_region = rect ? *rect : rcti{0, data.width, 0, data.height};
  r_region.xmin = std::clamp(r_region.xmin, 0, data.width);
  r_region.xmax = std::clamp(r_region.xmax, 0, data.width);
  r_region.ymin = std::clamp(r_region.ymin, 0, data.height);
  r_region.ymax = std::clamp(r_region.ymax, 0, data.height);
  if (BLI_rcti_is_empty(&r_region)) {
    return true;
  }
  /* Wrapped morphology can affect the opposite edge of the canvas. A single output
   * rectangle cannot represent those disjoint regions, so retain a full pass there.
   * Blur Slope likewise retains its absolute canvas coordinates in the full path. */
  const bool wrap_boundary = support.wrap &&
                             (int64_t(r_region.xmin) - 2 * support.x <= 0 ||
                              int64_t(r_region.xmax) + 2 * support.x >= data.width ||
                              int64_t(r_region.ymin) - 2 * support.y <= 0 ||
                              int64_t(r_region.ymax) + 2 * support.y >= data.height);
  const bool full_fallback = fresh_output || support.absolute_coordinates || wrap_boundary;
  if (full_fallback) {
    r_region = {0, data.width, 0, data.height};
  }
  else {
    r_region = expanded_region(data, r_region, support.x, support.y);
  }
  if (stats) {
    stats->output_pixels = region_pixels(r_region);
    stats->full_canvas_fallback = rect && full_fallback;
  }
  /* Detach implicitly shared storage once, before parallel writes. */
  uint8_t *output_bytes = output->byte_data_for_write();
  float *output_floats = output->float_data_for_write();
  const uint8_t base = initial_value(layer);
  if (!spatial) {
    if (stats) {
      stats->input_pixels = region_pixels(r_region);
      stats->effect_pixels = stats->input_pixels * effects.size();
    }
    threading::parallel_for(
        IndexRange(r_region.ymin, BLI_rcti_size_y(&r_region)), 16, [&](const IndexRange rows) {
          for (const int y : rows) {
            for (int x = r_region.xmin; x < r_region.xmax; x++) {
              const int64_t index = int64_t(y) * data.width + x;
              uint8_t value = base;
              for (const PreparedEffect &effect : effects) {
                value = apply_pointwise(value, effect, index);
              }
              write_output(output_bytes, output_floats, index, value);
            }
          }
        });
  }
  else {
    /* Reconstruct only the requested output and the source halo needed by later filters.
     * Each completed spatial stage discards its consumed halo. No persistent cache or
     * evaluated mesh/image ownership is introduced. */
    int64_t remaining_x = support.x, remaining_y = support.y;
    rcti stage_region = expanded_region(data, r_region, remaining_x, remaining_y);
    Array<uint8_t> accumulated(region_pixels(stage_region), base);
    if (stats) {
      stats->input_pixels = accumulated.size();
    }
    for (const PreparedEffect &prepared : effects) {
      if (stats) {
        stats->effect_pixels += accumulated.size();
      }
      const int stage_width = BLI_rcti_size_x(&stage_region);
      if (prepared.effect->type == TEXPAINT_MASK_EFFECT_FILTER) {
        apply_filter(accumulated, stage_width, BLI_rcti_size_y(&stage_region), *prepared.effect);
        const FilterSupport consumed = filter_support(*prepared.effect);
        remaining_x -= consumed.x;
        remaining_y -= consumed.y;
        const rcti next_region = expanded_region(data, r_region, remaining_x, remaining_y);
        if (next_region.xmin != stage_region.xmin || next_region.xmax != stage_region.xmax ||
            next_region.ymin != stage_region.ymin || next_region.ymax != stage_region.ymax)
        {
          accumulated = cropped_stage(accumulated, stage_region, next_region);
          stage_region = next_region;
        }
      }
      else {
        threading::parallel_for(
            IndexRange(BLI_rcti_size_y(&stage_region)), 16, [&](const IndexRange rows) {
              for (const int y : rows) {
                for (int x = 0; x < stage_width; x++) {
                  const int64_t local_index = int64_t(y) * stage_width + x;
                  const int64_t index = int64_t(y + stage_region.ymin) * data.width +
                                        stage_region.xmin + x;
                  accumulated[local_index] = apply_pointwise(
                      accumulated[local_index], prepared, index);
                }
              }
            });
      }
    }
    const int output_width = BLI_rcti_size_x(&r_region);
    threading::parallel_for(
        IndexRange(BLI_rcti_size_y(&r_region)), 16, [&](const IndexRange rows) {
          for (const int y : rows) {
            for (int x = 0; x < output_width; x++) {
              const int64_t local_index = int64_t(y) * output_width + x;
              const int64_t index = int64_t(y + r_region.ymin) * data.width + r_region.xmin + x;
              write_output(output_bytes, output_floats, index, accumulated[local_index]);
            }
          }
        });
  }
  layer.runtime_dirty = 1;
  return true;
}

}  // namespace blender
