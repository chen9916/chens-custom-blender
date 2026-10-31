/* SPDX-FileCopyrightText: 2026 Blender Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

/** \file
 * \ingroup bke
 *
 * Shared canvas selection and pixel editing kernels. No editor context or borrowed evaluated data.
 */

#include <algorithm>
#include <cmath>

#include "BKE_tex_paint_layer.hh"
#include "BLI_hash_mm2a.hh"
#include "BLI_math_color_c.hh"
#include "BLI_math_matrix.hh"
#include "BLI_task.hh"
#include "BLI_vector.hh"
#include "DNA_node_tex_paint_types.h"
#include "DNA_node_types.h"
#include "IMB_imbuf_types.hh"
#include "MEM_guardedalloc.h"

namespace blender {

void BKE_tex_paint_selection_transform_clear(NodeTexPaint &data)
{
  if (data.selection_transform_source) {
    MEM_delete(data.selection_transform_source);
    data.selection_transform_source = nullptr;
  }
  if (data.selection_transform_mask) {
    MEM_delete(data.selection_transform_mask);
    data.selection_transform_mask = nullptr;
  }
  data.selection_transform_size = 0;
  data.selection_transform_layer_uid = 0;
  data.selection_transform_hash = 0;
  const float3x3 identity = float3x3::identity();
  std::copy_n(identity.base_ptr(), 9, &data.selection_transform[0][0]);
}

void BKE_tex_paint_selection_transform_store(NodeTexPaint &data,
                                             const Span<float4> source,
                                             const Span<float> selection)
{
  BKE_tex_paint_selection_transform_clear(data);
  data.selection_transform_size = int(source.size());
  data.selection_transform_source = MEM_new_array_uninitialized<float>(source.size() * 4,
                                                                       __func__);
  std::copy_n(reinterpret_cast<const float *>(source.data()),
              source.size() * 4,
              data.selection_transform_source);
  if (!selection.is_empty()) {
    data.selection_transform_mask = MEM_new_array_uninitialized<float>(selection.size(), __func__);
    std::copy(selection.begin(), selection.end(), data.selection_transform_mask);
  }
}

uint64_t BKE_tex_paint_pixels_hash(const ImBuf &image)
{
  const uchar *bytes = image.float_data() ? reinterpret_cast<const uchar *>(image.float_data()) :
                                            image.byte_data();
  const size_t size = size_t(image.x) * image.y * 4 * (image.float_data() ? sizeof(float) : 1);
  return bytes ? (uint64_t(BLI_hash_mm2(bytes, size, 0)) << 32) |
                     BLI_hash_mm2(bytes, size, 0x9e3779b9) :
                 0;
}

void BKE_tex_paint_selection_clear(NodeTexPaint &data)
{
  BKE_tex_paint_selection_transform_clear(data);
  BKE_tex_paint_selection_tag_changed(data);
  if (data.selection) {
    MEM_delete(data.selection);
    data.selection = nullptr;
  }
  data.selection_size = 0;
  data.selection_active = 0;
}

void BKE_tex_paint_selection_set(NodeTexPaint &data,
                                 const Span<float> coverage,
                                 const bool preserve_transform)
{
  BLI_assert(coverage.size() == int64_t(data.width) * data.height);
  if (coverage.size() != int64_t(data.width) * data.height) {
    return;
  }
  if (!preserve_transform) {
    BKE_tex_paint_selection_transform_clear(data);
  }
  if (data.selection_size != coverage.size() || data.selection == nullptr) {
    BKE_tex_paint_selection_clear(data);
    data.selection = MEM_new_array_uninitialized<float>(coverage.size(), __func__);
    data.selection_size = int(coverage.size());
  }
  std::copy(coverage.begin(), coverage.end(), data.selection);
  data.selection_active = 1;
  BKE_tex_paint_selection_tag_changed(data);
}

Span<float> BKE_tex_paint_selection(const NodeTexPaint &data)
{
  if (!data.selection_active || !data.selection ||
      data.selection_size != int64_t(data.width) * data.height)
  {
    return {};
  }
  return {data.selection, data.selection_size};
}

Array<float4> BKE_tex_paint_pixels_read(const ImBuf &image)
{
  Array<float4> pixels(int64_t(image.x) * image.y);
  threading::parallel_for(pixels.index_range(), 4096, [&](IndexRange range) {
    for (const int64_t i : range) {
      if (const float *src = image.float_data()) {
        pixels[i] = float4(src + i * 4);
      }
      else if (const uchar *src = image.byte_data()) {
        const uchar *c = src + i * 4;
        const float a = c[3] / 255.0f;
        pixels[i] = float4(c[0] / 255.0f * a, c[1] / 255.0f * a, c[2] / 255.0f * a, a);
      }
      else {
        pixels[i] = float4(0);
      }
    }
  });
  return pixels;
}

float BKE_tex_paint_selection_value(const bNode &node, const int x, const int y)
{
  const NodeTexPaint &data = *static_cast<const NodeTexPaint *>(node.storage);
  const Span<float> selection = BKE_tex_paint_selection(data);
  if (selection.is_empty()) {
    return 1.0f;
  }
  if (x < 0 || y < 0 || x >= data.width || y >= data.height) {
    return 0.0f;
  }
  return std::clamp(selection[y * data.width + x], 0.0f, 1.0f);
}

void BKE_tex_paint_pixels_write(ImBuf &image, const Span<float4> pixels, const rcti &rect)
{
  float *floats = image.float_data_for_write();
  uchar *bytes = image.byte_data_for_write();
  threading::parallel_for(IndexRange(rect.ymin, rect.ymax - rect.ymin), 16, [&](IndexRange rows) {
    for (const int64_t y : rows) {
      for (int x = rect.xmin; x < rect.xmax; x++) {
        const int64_t i = y * image.x + x;
        const float4 p = pixels[i];
        if (floats) {
          std::copy_n(&p.x, 4, floats + i * 4);
        }
        else if (bytes) {
          const float inv_alpha = p.w > 0.0f ? 1.0f / p.w : 0.0f;
          const float4 straight(p.x * inv_alpha, p.y * inv_alpha, p.z * inv_alpha, p.w);
          unit_float_to_uchar_clamp_v4(bytes + i * 4, straight);
        }
      }
    }
  });
}

Array<float> BKE_tex_paint_select_wand(const ImBuf &image,
                                       const int2 seed,
                                       const float tolerance,
                                       const bool contiguous)
{
  const int w = image.x, h = image.y;
  Array<float> mask(int64_t(w) * h, 0.0f);
  if (seed.x < 0 || seed.y < 0 || seed.x >= w || seed.y >= h) {
    return mask;
  }
  const Array<float4> pixels = BKE_tex_paint_pixels_read(image);
  auto straight = [](float4 p) {
    if (p.w > 1e-8f) {
      p.x /= p.w;
      p.y /= p.w;
      p.z /= p.w;
    }
    return p;
  };
  const int start = seed.y * w + seed.x;
  const float4 reference = straight(pixels[start]);
  auto matches = [&](int i) {
    const float4 p = straight(pixels[i]);
    float distance = 0;
    for (int c = 0; c < 4; c++) {
      distance = std::max(distance, std::abs(p[c] - reference[c]));
    }
    return distance <= tolerance;
  };
  if (!contiguous) {
    threading::parallel_for(mask.index_range(), 4096, [&](IndexRange range) {
      for (const int64_t i : range) {
        mask[i] = matches(int(i)) ? 1.0f : 0.0f;
      }
    });
    return mask;
  }
  Array<bool> visited(mask.size(), false);
  Vector<int> pending;
  pending.append(start);
  visited[start] = true;
  while (!pending.is_empty()) {
    const int i = pending.pop_last();
    if (!matches(i)) {
      continue;
    }
    mask[i] = 1;
    const int x = i % w, y = i / w;
    const int neighbors[] = {
        x > 0 ? i - 1 : -1, x + 1 < w ? i + 1 : -1, y > 0 ? i - w : -1, y + 1 < h ? i + w : -1};
    for (const int neighbor : neighbors) {
      if (neighbor >= 0 && !visited[neighbor]) {
        visited[neighbor] = true;
        pending.append(neighbor);
      }
    }
  }
  return mask;
}

Array<float> BKE_tex_paint_select_polygon(const int width,
                                          const int height,
                                          const Span<float2> polygon)
{
  Array<float> mask(int64_t(width) * height, 0.0f);
  if (polygon.size() < 3) {
    return mask;
  }
  /* Scan-line even/odd fill; work scales with polygon edges plus covered pixels. */
  threading::parallel_for(IndexRange(height), 32, [&](IndexRange rows) {
    Vector<float> crossings;
    for (const int64_t y : rows) {
      crossings.clear();
      const float py = y + 0.5f;
      for (const int64_t i : polygon.index_range()) {
        const float2 a = polygon[i], b = polygon[(i + 1) % polygon.size()];
        if ((a.y > py) != (b.y > py)) {
          crossings.append(a.x + (py - a.y) * (b.x - a.x) / (b.y - a.y));
        }
      }
      std::sort(crossings.begin(), crossings.end());
      for (int i = 0; i + 1 < crossings.size(); i += 2) {
        const int begin = std::clamp(int(std::ceil(crossings[i] - 0.5f)), 0, width);
        const int end = std::clamp(int(std::ceil(crossings[i + 1] - 0.5f)), 0, width);
        for (int x = begin; x < end; x++) {
          mask[y * width + x] = 1;
        }
      }
    }
  });
  return mask;
}

void BKE_tex_paint_pixels_transform(const Span<float4> source,
                                    const Span<float> selection,
                                    const int width,
                                    const int height,
                                    const float2 pivot,
                                    const float2 translation,
                                    const float angle,
                                    const float2 scale,
                                    MutableSpan<float4> output,
                                    MutableSpan<float> output_selection,
                                    const rcti &rect)
{
  float cosine = std::cos(angle), sine = std::sin(angle);
  /* Avoid introducing interpolation fringes at exact quarter turns. */
  cosine = std::abs(cosine) < 1e-7f ? 0.0f : cosine;
  sine = std::abs(sine) < 1e-7f ? 0.0f : sine;
  float3x3 transform = float3x3::identity();
  transform[0] = float3(cosine * scale.x, sine * scale.x, 0);
  transform[1] = float3(-sine * scale.y, cosine * scale.y, 0);
  transform[2] = float3(pivot.x + translation.x, pivot.y + translation.y, 1) -
                 transform * float3(pivot.x, pivot.y, 0);
  BKE_tex_paint_pixels_transform_affine(
      source, selection, width, height, transform, output, output_selection, rect);
}

void BKE_tex_paint_pixels_transform_affine(const Span<float4> source,
                                           const Span<float> selection,
                                           const int width,
                                           const int height,
                                           const float3x3 &transform,
                                           MutableSpan<float4> output,
                                           MutableSpan<float> output_selection,
                                           const rcti &rect)
{
  const bool identity = transform == float3x3::identity();
  const float3x3 inverse = math::invert(transform);
  threading::parallel_for(IndexRange(rect.ymin, rect.ymax - rect.ymin), 16, [&](IndexRange rows) {
    for (const int64_t y : rows) {
      for (int x = rect.xmin; x < rect.xmax; x++) {
        const int64_t i = y * width + x;
        const float selected = selection.is_empty() ? 1.0f : selection[i];
        if (identity) {
          output[i] = source[i];
          if (!output_selection.is_empty()) {
            output_selection[i] = selected;
          }
          continue;
        }
        const float3 sample = inverse * float3(x + 0.5f, y + 0.5f, 1);
        const float u = sample.x - 0.5f;
        const float v = sample.y - 0.5f;
        const int ix = int(std::floor(u)), iy = int(std::floor(v));
        const float fx = u - ix, fy = v - iy;
        float4 floating(0);
        float coverage = 0;
        for (int dy = 0; dy < 2; dy++) {
          for (int dx = 0; dx < 2; dx++) {
            const int sx = ix + dx, sy = iy + dy;
            if (sx < 0 || sy < 0 || sx >= width || sy >= height) {
              continue;
            }
            const int si = sy * width + sx;
            const float weight = (dx ? fx : 1 - fx) * (dy ? fy : 1 - fy);
            const float s = selection.is_empty() ? 1 : selection[si];
            floating += source[si] * (s * weight);
            coverage += s * weight;
          }
        }
        /* Fill the coverage cut out by the original selection before compositing over the
         * remaining pixels. This preserves feathered edge opacity, including an identity edit. */
        const float remainder = 1 - selected;
        const float backdrop_weight = std::min(remainder, std::max(0.0f, 1 - floating.w));
        output[i] = source[i] * backdrop_weight + floating;
        if (!output_selection.is_empty()) {
          output_selection[i] = coverage;
        }
      }
    }
  });
}

}  // namespace blender
