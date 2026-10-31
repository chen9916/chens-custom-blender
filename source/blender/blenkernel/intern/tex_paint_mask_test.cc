/* SPDX-FileCopyrightText: 2026 Blender Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

#include "testing/testing.h"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstring>

#include "BLI_array.hh"
#include "BLI_listbase.hh"

#include "DNA_node_tex_paint_types.h"

#include "BKE_gtest_base.hh"
#include "BKE_tex_paint_layer.hh"
#include "BKE_tex_paint_mask.hh"

#include "IMB_imbuf.hh"
#include "IMB_imbuf_types.hh"

namespace blender::bke::tests {

class TexPaintMaskTest : public BlenderGTestBase {
 protected:
  NodeTexPaint data_;
  TexPaintLayer layer_;

  void SetUp() override
  {
    BlenderGTestBase::SetUp();
    data_.width = data_.height = 8;
    layer_.mask_effects_active = 1;
    layer_.flag |= TEXPAINT_LAYER_USE_MASK;
  }

  void TearDown() override
  {
    BKE_tex_paint_storage_free_contents(data_);
    BKE_tex_paint_mask_effects_clear(layer_);
    if (layer_.mask_ibuf) {
      IMB_freeImBuf(layer_.mask_ibuf);
    }
    BlenderGTestBase::TearDown();
  }

  TexPaintMaskEffect &source(const uint8_t value = 128, const int uid = 1)
  {
    TexPaintMaskEffect &effect = BKE_tex_paint_mask_effect_ensure(
        layer_, uid, TEXPAINT_MASK_EFFECT_SOURCE);
    ImBuf *pixels = BKE_tex_paint_mask_effect_ensure_pixels(data_, effect);
    uint8_t *bytes = pixels->byte_data_for_write();
    for (int i = 0; i < data_.width * data_.height; i++) {
      bytes[i * 4] = bytes[i * 4 + 1] = bytes[i * 4 + 2] = value;
      bytes[i * 4 + 3] = 255;
    }
    return effect;
  }

  TexPaintMaskEffect &paint(const int uid = 2)
  {
    TexPaintMaskEffect &effect = BKE_tex_paint_mask_effect_ensure(
        layer_, uid, TEXPAINT_MASK_EFFECT_PAINT);
    BKE_tex_paint_mask_effect_ensure_pixels(data_, effect);
    layer_.active_mask_effect_uid = uid;
    return effect;
  }

  uint8_t output(const int x, const int y)
  {
    return layer_.mask_ibuf->byte_data()[(y * data_.width + x) * 4];
  }

  static uint32_t random_next(uint32_t &state)
  {
    state ^= state << 13;
    state ^= state >> 17;
    state ^= state << 5;
    return state;
  }

  void randomize(TexPaintMaskEffect &effect, uint32_t &state, const rcti *rect = nullptr)
  {
    uint8_t *pixels = effect.pixels->byte_data_for_write();
    const rcti region = rect ? *rect : rcti{0, data_.width, 0, data_.height};
    for (int y = region.ymin; y < region.ymax; y++) {
      for (int x = region.xmin; x < region.xmax; x++) {
        uint8_t *pixel = pixels + (int64_t(y) * data_.width + x) * 4;
        pixel[0] = pixel[1] = pixel[2] = uint8_t(random_next(state));
        pixel[3] = effect.type == TEXPAINT_MASK_EFFECT_PAINT ? uint8_t(random_next(state)) : 255;
      }
    }
  }

  Array<uint8_t> output_copy()
  {
    return Array<uint8_t>(
        Span<uint8_t>(layer_.mask_ibuf->byte_data(), int64_t(data_.width) * data_.height * 4));
  }

  void expect_output_near(const Span<uint8_t> expected, const int tolerance = 1)
  {
    const uint8_t *actual = layer_.mask_ibuf->byte_data();
    int maximum = 0;
    for (const int64_t i : expected.index_range()) {
      maximum = std::max(maximum, std::abs(int(actual[i]) - int(expected[i])));
    }
    EXPECT_LE(maximum, tolerance);
  }
};

TEST_F(TexPaintMaskTest, ZeroOpacitySourcesKeepAuthoredBase)
{
  TexPaintMaskEffect &ignored = source();
  for (const int base : {0, 255}) {
    layer_.mask_base_value = base;
    for (int mode = TEXPAINT_MASK_EFFECT_REPLACE; mode <= TEXPAINT_MASK_EFFECT_MIN; mode++) {
      ignored.blend_mode = mode;
      for (const float opacity : {0.0f, 1e-7f, 1e-5f}) {
        SCOPED_TRACE(testing::Message()
                     << "base=" << base << " mode=" << mode << " opacity=" << opacity);
        ignored.opacity = opacity;
        rcti region;
        ASSERT_TRUE(BKE_tex_paint_mask_evaluate(data_, layer_, nullptr, region));
        EXPECT_EQ(output(0, 0), base);
      }
    }
  }
}

TEST_F(TexPaintMaskTest, ZeroOpacitySourcesKeepNextSourceNeutralBase)
{
  TexPaintMaskEffect &ignored = source();
  TexPaintMaskEffect &following = source(64, 2);
  following.opacity = 0.5f;
  /* The following source selects its neutral base, except Replace and Overlay,
   * which retain the authored base. Values include its half opacity. */
  const int expected[2][8] = {{32, 32, 223, 160, 32, 0, 32, 160},
                              {160, 32, 223, 160, 32, 255, 32, 160}};
  for (const int base_index : {0, 1}) {
    layer_.mask_base_value = base_index * 255;
    for (int first_mode = TEXPAINT_MASK_EFFECT_REPLACE; first_mode <= TEXPAINT_MASK_EFFECT_MIN;
         first_mode++)
    {
      ignored.blend_mode = first_mode;
      for (int mode = TEXPAINT_MASK_EFFECT_REPLACE; mode <= TEXPAINT_MASK_EFFECT_MIN; mode++) {
        following.blend_mode = mode;
        for (const float opacity : {0.0f, 1e-7f, 1e-5f}) {
          SCOPED_TRACE(testing::Message()
                       << "base=" << layer_.mask_base_value << " first_mode=" << first_mode
                       << " mode=" << mode << " opacity=" << opacity);
          ignored.opacity = opacity;
          rcti region;
          ASSERT_TRUE(BKE_tex_paint_mask_evaluate(data_, layer_, nullptr, region));
          EXPECT_EQ(output(0, 0), expected[base_index][mode]);
        }
      }
    }
  }
}

TEST_F(TexPaintMaskTest, ZeroOpacitySourcesKeepNextPaintAuthoredBase)
{
  TexPaintMaskEffect &ignored = source();
  TexPaintMaskEffect &following = paint();
  uint8_t *pixels = following.pixels->byte_data_for_write();
  pixels[0] = pixels[1] = pixels[2] = 64;
  pixels[3] = 255;
  const int expected[2][8] = {{64, 64, 0, 0, 64, 0, 64, 0}, {64, 255, 191, 64, 255, 255, 255, 64}};
  for (const int base_index : {0, 1}) {
    layer_.mask_base_value = base_index * 255;
    for (int first_mode = TEXPAINT_MASK_EFFECT_REPLACE; first_mode <= TEXPAINT_MASK_EFFECT_MIN;
         first_mode++)
    {
      ignored.blend_mode = first_mode;
      for (int mode = TEXPAINT_MASK_EFFECT_REPLACE; mode <= TEXPAINT_MASK_EFFECT_MIN; mode++) {
        following.blend_mode = mode;
        for (const float opacity : {0.0f, 1e-7f, 1e-5f}) {
          SCOPED_TRACE(testing::Message()
                       << "base=" << layer_.mask_base_value << " first_mode=" << first_mode
                       << " mode=" << mode << " opacity=" << opacity);
          ignored.opacity = opacity;
          rcti region;
          ASSERT_TRUE(BKE_tex_paint_mask_evaluate(data_, layer_, nullptr, region));
          EXPECT_EQ(output(0, 0), expected[base_index][mode]);
          EXPECT_EQ(output(1, 0), layer_.mask_base_value);
        }
      }
    }
  }
}

TEST_F(TexPaintMaskTest, MultiplyPaintCoverageErasesToUnmodifiedGenerator)
{
  TexPaintMaskEffect &generated = source();
  TexPaintMaskEffect &painted = paint();
  painted.blend_mode = TEXPAINT_MASK_EFFECT_MULTIPLY;
  const int index = (3 * data_.width + 4) * 4;
  uint8_t *pixels = painted.pixels->byte_data_for_write();
  pixels[index + 3] = 128;
  rcti region;
  ASSERT_TRUE(BKE_tex_paint_mask_evaluate(data_, layer_, nullptr, region));
  EXPECT_EQ(output(4, 3), 64);
  EXPECT_EQ(output(0, 0), 128);
  EXPECT_EQ(generated.pixels->byte_data()[index], 128);

  pixels[index + 3] = 0;
  const rcti changed = {4, 5, 3, 4};
  ASSERT_TRUE(BKE_tex_paint_mask_evaluate(data_, layer_, &changed, region));
  EXPECT_EQ(output(4, 3), 128);
  EXPECT_EQ(region.xmin, 4);
  EXPECT_EQ(region.xmax, 5);
  EXPECT_EQ(region.ymin, 3);
  EXPECT_EQ(region.ymax, 4);
  EXPECT_EQ(generated.pixels->byte_data()[index], 128);
}

TEST_F(TexPaintMaskTest, PaintTargetOwnsCoverageAndBlocksNonpaintSelections)
{
  source();
  TexPaintMaskEffect &first = paint();
  TexPaintMaskEffect &second = paint(3);
  EXPECT_EQ(BKE_tex_paint_mask_paint_target(data_, layer_), second.pixels);
  EXPECT_NE(first.pixels, second.pixels);
  EXPECT_EQ(BKE_tex_paint_mask_effect_find_by_buffer(layer_, first.pixels), &first);
  second.enabled = 0;
  EXPECT_EQ(BKE_tex_paint_mask_paint_target(data_, layer_), nullptr);
  layer_.active_mask_effect_uid = 1;
  EXPECT_EQ(BKE_tex_paint_mask_paint_target(data_, layer_), nullptr);
  layer_.active_mask_effect_uid = 2;
  EXPECT_EQ(BKE_tex_paint_mask_paint_target(data_, layer_), first.pixels);
}

TEST_F(TexPaintMaskTest, OwnedCopiesKeepIndependentPaintAndEvaluatedCopiesOmitIt)
{
  source();
  TexPaintMaskEffect &painted = paint();
  painted.pixels->byte_data_for_write()[3] = 129;
  TexPaintLayer owned;
  BKE_tex_paint_mask_effects_copy(owned, layer_, true);
  ASSERT_EQ(owned.mask_effects.count(), 2);
  TexPaintMaskEffect *copy = BKE_tex_paint_mask_effect_find(owned, painted.uid);
  ASSERT_NE(copy, nullptr);
  EXPECT_NE(copy->pixels, painted.pixels);
  EXPECT_EQ(copy->pixels->byte_data()[3], 129);
  copy->pixels->byte_data_for_write()[3] = 0;
  EXPECT_EQ(painted.pixels->byte_data()[3], 129);

  TexPaintLayer evaluated;
  BKE_tex_paint_mask_effects_copy(evaluated, layer_, false);
  EXPECT_TRUE(evaluated.mask_effects.is_empty());
  EXPECT_EQ(evaluated.mask_effects_active, 0);
  BKE_tex_paint_mask_effects_clear(owned);
}

TEST_F(TexPaintMaskTest, LevelsAfterPaintUsesNativeCoverageWithoutModifyingSource)
{
  source(200);
  TexPaintMaskEffect &painted = paint();
  uint8_t *pixels = painted.pixels->byte_data_for_write();
  pixels[3] = 255;
  TexPaintMaskEffect &levels = BKE_tex_paint_mask_effect_ensure(
      layer_, 3, TEXPAINT_MASK_EFFECT_LEVELS);
  for (int i = 0; i < 256; i++) {
    levels.curve[i] = 1.0f - float(i) / 255.0f;
  }
  rcti region;
  ASSERT_TRUE(BKE_tex_paint_mask_evaluate(data_, layer_, nullptr, region));
  EXPECT_EQ(output(0, 0), 255);
  EXPECT_EQ(output(1, 0), 55);
  EXPECT_EQ(pixels[0], 0);
  EXPECT_EQ(pixels[3], 255);
  EXPECT_TRUE(BKE_tex_paint_mask_effect_remove(layer_, levels.uid));
  EXPECT_FALSE(BKE_tex_paint_mask_effect_remove(layer_, 999));
}

TEST_F(TexPaintMaskTest, SpatialFiltersEvaluateFullOutputAndLeavePaintSourceIntact)
{
  source(0);
  TexPaintMaskEffect &painted = paint();
  const int index = (4 * data_.width + 4) * 4;
  uint8_t *pixels = painted.pixels->byte_data_for_write();
  pixels[index] = pixels[index + 1] = pixels[index + 2] = pixels[index + 3] = 255;
  TexPaintMaskEffect &filter = BKE_tex_paint_mask_effect_ensure(
      layer_, 3, TEXPAINT_MASK_EFFECT_FILTER);
  filter.filter_radius = 2;
  const rcti changed = {4, 5, 4, 5};
  rcti region;
  ASSERT_TRUE(BKE_tex_paint_mask_evaluate(data_, layer_, &changed, region));
  EXPECT_EQ(region.xmin, 0);
  EXPECT_EQ(region.ymin, 0);
  EXPECT_EQ(region.xmax, data_.width);
  EXPECT_EQ(region.ymax, data_.height);
  EXPECT_GT(output(3, 4), 0);
  EXPECT_LT(output(4, 4), 255);
  EXPECT_EQ(pixels[index], 255);
  EXPECT_EQ(pixels[index + 3], 255);
  for (int type = TEXPAINT_MASK_FILTER_BLUR; type <= TEXPAINT_MASK_FILTER_HIGHPASS; type++) {
    filter.filter_type = type;
    ASSERT_TRUE(BKE_tex_paint_mask_evaluate(data_, layer_, &changed, region));
    for (int i = 0; i < data_.width * data_.height; i++) {
      EXPECT_EQ(layer_.mask_ibuf->byte_data()[i * 4 + 3], 255);
    }
  }
}

TEST_F(TexPaintMaskTest, FloatCanvasReceivesOpaqueMaskAndBytePaintCoverage)
{
  data_.is_float = 1;
  source(128);
  TexPaintMaskEffect &painted = paint();
  ASSERT_NE(painted.pixels->byte_data(), nullptr);
  rcti region;
  ASSERT_TRUE(BKE_tex_paint_mask_evaluate(data_, layer_, nullptr, region));
  ASSERT_NE(layer_.mask_ibuf->float_data(), nullptr);
  EXPECT_NEAR(layer_.mask_ibuf->float_data()[0], 128.0f / 255.0f, 1e-6f);
  EXPECT_EQ(layer_.mask_ibuf->float_data()[3], 1.0f);
}

TEST_F(TexPaintMaskTest, ThresholdAndPosterizeKeepIncrementalOutputBounds)
{
  source(128);
  TexPaintMaskEffect &painted = paint();
  painted.pixels->byte_data_for_write()[(4 * data_.width + 4) * 4 + 3] = 255;
  TexPaintMaskEffect &filter = BKE_tex_paint_mask_effect_ensure(
      layer_, 3, TEXPAINT_MASK_EFFECT_FILTER);
  const rcti changed = {4, 5, 4, 5};
  rcti region;
  for (const int type : {TEXPAINT_MASK_FILTER_THRESHOLD, TEXPAINT_MASK_FILTER_POSTERIZE}) {
    filter.filter_type = type;
    ASSERT_TRUE(BKE_tex_paint_mask_evaluate(data_, layer_, nullptr, region));
    layer_.mask_ibuf->byte_data_for_write()[0] = 17;
    ASSERT_TRUE(BKE_tex_paint_mask_evaluate(data_, layer_, &changed, region));
    EXPECT_EQ(region.xmin, changed.xmin);
    EXPECT_EQ(region.xmax, changed.xmax);
    EXPECT_EQ(region.ymin, changed.ymin);
    EXPECT_EQ(region.ymax, changed.ymax);
    EXPECT_EQ(output(0, 0), 17);
    EXPECT_EQ(output(4, 4), 0);
  }
}

TEST_F(TexPaintMaskTest, MaskResetAndRemovalReleaseNativePaintTargets)
{
  source();
  paint();
  BKE_tex_paint_layer_mask_fill(data_, layer_, 0.0f);
  EXPECT_TRUE(layer_.mask_effects.is_empty());
  EXPECT_EQ(layer_.mask_effects_active, 0);
  EXPECT_EQ(layer_.active_mask_effect_uid, 0);
  EXPECT_EQ(layer_.mask_base_value, 0);
  ASSERT_NE(layer_.mask_ibuf, nullptr);
  EXPECT_EQ(output(0, 0), 0);

  source();
  paint();
  layer_.mask_effects_active = 1;
  BKE_tex_paint_layer_mask_remove(data_, layer_);
  EXPECT_TRUE(layer_.mask_effects.is_empty());
  EXPECT_EQ(layer_.mask_effects_active, 0);
  EXPECT_EQ(layer_.active_mask_effect_uid, 0);
  EXPECT_EQ(layer_.mask_ibuf, nullptr);
  EXPECT_EQ(BKE_tex_paint_mask_paint_target(data_, layer_), nullptr);
}

TEST_F(TexPaintMaskTest, LocalSpatialFiltersMatchFullEvaluationAtRandomTilesAndEdges)
{
  data_.width = 257;
  data_.height = 193;
  uint32_t random = 0x892ac547u;
  TexPaintMaskEffect &generated = source();
  TexPaintMaskEffect &painted = paint();
  randomize(generated, random);
  randomize(painted, random);
  const Array<uint8_t> source_before(
      Span<uint8_t>(generated.pixels->byte_data(), int64_t(data_.width) * data_.height * 4));
  TexPaintMaskEffect &filter = BKE_tex_paint_mask_effect_ensure(
      layer_, 3, TEXPAINT_MASK_EFFECT_FILTER);
  filter.filter_radius = 3;
  const int types[] = {TEXPAINT_MASK_FILTER_BLUR,
                       TEXPAINT_MASK_FILTER_SHARPEN,
                       TEXPAINT_MASK_FILTER_HIGHPASS,
                       TEXPAINT_MASK_FILTER_DILATE_ERODE,
                       TEXPAINT_MASK_FILTER_BEVEL};
  for (const int type : types) {
    filter.filter_type = type;
    for (const float strength : {0.0f, 0.7f, 1.0f}) {
      filter.filter_strength = strength;
      rcti region;
      ASSERT_TRUE(BKE_tex_paint_mask_evaluate(data_, layer_, nullptr, region));
      for (int tile = 0; tile < 9; tile++) {
        const int x = tile == 0 ? 0 :
                      tile == 1 ? data_.width - 9 :
                                  int(random_next(random) % (data_.width - 9));
        const int y = tile == 0 ? 0 :
                      tile == 1 ? data_.height - 7 :
                                  int(random_next(random) % (data_.height - 7));
        const rcti changed = {x, x + 9, y, y + 7};
        randomize(painted, random, &changed);
        TexPaintMaskEvalStats local, full;
        ASSERT_TRUE(BKE_tex_paint_mask_evaluate(data_, layer_, &changed, region, &local));
        const Array<uint8_t> partial = output_copy();
        ASSERT_TRUE(BKE_tex_paint_mask_evaluate(data_, layer_, nullptr, region, &full));
        expect_output_near(partial);
        EXPECT_LE(local.input_pixels, full.input_pixels);
        EXPECT_LE(local.effect_pixels, full.effect_pixels);
        EXPECT_LE(local.output_pixels, full.output_pixels);
        if (tile == 0 && ELEM(type, TEXPAINT_MASK_FILTER_DILATE_ERODE, TEXPAINT_MASK_FILTER_BEVEL))
        {
          EXPECT_TRUE(local.full_canvas_fallback);
        }
        if (!ELEM(type, TEXPAINT_MASK_FILTER_DILATE_ERODE, TEXPAINT_MASK_FILTER_BEVEL)) {
          EXPECT_FALSE(local.full_canvas_fallback);
          EXPECT_LT(local.input_pixels, full.input_pixels / 4);
        }
      }
    }
  }
  EXPECT_EQ(std::memcmp(source_before.data(), generated.pixels->byte_data(), source_before.size()),
            0);
}

TEST_F(TexPaintMaskTest, StackedSpatialEffectsKeepGlobalSamplingAndBoundedWork)
{
  data_.width = 311;
  data_.height = 239;
  uint32_t random = 0x5b73684fu;
  TexPaintMaskEffect &generated = source();
  TexPaintMaskEffect &painted = paint();
  randomize(generated, random);
  randomize(painted, random);
  TexPaintMaskEffect &blur = BKE_tex_paint_mask_effect_ensure(
      layer_, 3, TEXPAINT_MASK_EFFECT_FILTER);
  blur.filter_radius = 2;
  TexPaintMaskEffect &levels = BKE_tex_paint_mask_effect_ensure(
      layer_, 4, TEXPAINT_MASK_EFFECT_LEVELS);
  for (int i = 0; i < 256; i++) {
    /* A steep operation exposes even tiny differences in intermediate filtered values. */
    levels.curve[i] = i < 128 ? 0.0f : 1.0f;
  }
  TexPaintMaskEffect &upper_paint = paint(5);
  randomize(upper_paint, random);
  upper_paint.blend_mode = TEXPAINT_MASK_EFFECT_ADD;
  upper_paint.opacity = 0.3f;
  TexPaintMaskEffect &highpass = BKE_tex_paint_mask_effect_ensure(
      layer_, 6, TEXPAINT_MASK_EFFECT_FILTER);
  highpass.filter_type = TEXPAINT_MASK_FILTER_HIGHPASS;
  highpass.filter_radius = 3;
  highpass.opacity = 0.65f;
  TexPaintMaskEffect &bevel = BKE_tex_paint_mask_effect_ensure(
      layer_, 7, TEXPAINT_MASK_EFFECT_FILTER);
  bevel.filter_type = TEXPAINT_MASK_FILTER_BEVEL;
  bevel.filter_radius = 2;
  rcti region;
  ASSERT_TRUE(BKE_tex_paint_mask_evaluate(data_, layer_, nullptr, region));
  for (int tile = 0; tile < 12; tile++) {
    const int x = 70 + int(random_next(random) % 160);
    const int y = 55 + int(random_next(random) % 120);
    const rcti changed = {x, x + 11, y, y + 9};
    randomize(painted, random, &changed);
    TexPaintMaskEvalStats local, full;
    ASSERT_TRUE(BKE_tex_paint_mask_evaluate(data_, layer_, &changed, region, &local));
    const Array<uint8_t> partial = output_copy();
    ASSERT_TRUE(BKE_tex_paint_mask_evaluate(data_, layer_, nullptr, region, &full));
    expect_output_near(partial, 0);
    EXPECT_FALSE(local.full_canvas_fallback);
    EXPECT_LT(local.input_pixels, full.input_pixels / 8);
    EXPECT_LT(local.effect_pixels, full.effect_pixels / 8);
    EXPECT_LT(local.output_pixels, full.output_pixels / 8);
  }
}

TEST_F(TexPaintMaskTest, BlurSlopeRetainsCanvasCoordinatesInConservativeFallback)
{
  data_.width = 67;
  data_.height = 53;
  uint32_t random = 0x2acbd723u;
  TexPaintMaskEffect &generated = source();
  TexPaintMaskEffect &painted = paint();
  randomize(generated, random);
  randomize(painted, random);
  TexPaintMaskEffect &slope = BKE_tex_paint_mask_effect_ensure(
      layer_, 3, TEXPAINT_MASK_EFFECT_FILTER);
  slope.filter_type = TEXPAINT_MASK_FILTER_BLUR_SLOPE;
  TexPaintMaskEffect &blur = BKE_tex_paint_mask_effect_ensure(
      layer_, 4, TEXPAINT_MASK_EFFECT_FILTER);
  blur.filter_radius = 3;
  rcti region;
  ASSERT_TRUE(BKE_tex_paint_mask_evaluate(data_, layer_, nullptr, region));
  const rcti changed = {23, 27, 21, 25};
  randomize(painted, random, &changed);
  TexPaintMaskEvalStats stats;
  ASSERT_TRUE(BKE_tex_paint_mask_evaluate(data_, layer_, &changed, region, &stats));
  const Array<uint8_t> partial = output_copy();
  EXPECT_TRUE(stats.full_canvas_fallback);
  EXPECT_EQ(stats.output_pixels, uint64_t(data_.width) * data_.height);
  ASSERT_TRUE(BKE_tex_paint_mask_evaluate(data_, layer_, nullptr, region));
  expect_output_near(partial, 0);
}

TEST_F(TexPaintMaskTest, FloatMaskSpatialRegionMatchesFullEvaluation)
{
  data_.width = 97;
  data_.height = 81;
  data_.is_float = 1;
  uint32_t random = 0x5abe3287u;
  source();
  TexPaintMaskEffect &painted = paint();
  randomize(painted, random);
  TexPaintMaskEffect &blur = BKE_tex_paint_mask_effect_ensure(
      layer_, 3, TEXPAINT_MASK_EFFECT_FILTER);
  blur.filter_radius = 3;
  rcti region;
  ASSERT_TRUE(BKE_tex_paint_mask_evaluate(data_, layer_, nullptr, region));
  const rcti changed = {43, 47, 35, 39};
  randomize(painted, random, &changed);
  const int64_t elements = int64_t(data_.width) * data_.height * 4;
  const Array<uint8_t> source_before(Span<uint8_t>(painted.pixels->byte_data(), elements));
  TexPaintMaskEvalStats stats;
  ASSERT_TRUE(BKE_tex_paint_mask_evaluate(data_, layer_, &changed, region, &stats));
  const Array<float> partial(Span<float>(layer_.mask_ibuf->float_data(), elements));
  EXPECT_FALSE(stats.full_canvas_fallback);
  EXPECT_LT(stats.input_pixels, uint64_t(data_.width) * data_.height / 4);
  ASSERT_TRUE(BKE_tex_paint_mask_evaluate(data_, layer_, nullptr, region));
  const float *full = layer_.mask_ibuf->float_data();
  for (const int64_t i : partial.index_range()) {
    EXPECT_EQ(partial[i], full[i]);
  }
  EXPECT_EQ(std::memcmp(source_before.data(), painted.pixels->byte_data(), source_before.size()),
            0);
}

TEST_F(TexPaintMaskTest, DISABLED_BenchmarkSpatialFilterRegions)
{
  using Clock = std::chrono::steady_clock;
  for (const int size : {2048, 4096}) {
    BKE_tex_paint_mask_effects_clear(layer_);
    if (layer_.mask_ibuf) {
      IMB_freeImBuf(layer_.mask_ibuf);
      layer_.mask_ibuf = nullptr;
    }
    data_.width = data_.height = size;
    layer_.mask_effects_active = 1;
    source();
    TexPaintMaskEffect &painted = paint();
    const int center = size / 2;
    const rcti changed = {center - 8, center + 8, center - 8, center + 8};
    uint32_t random = 0x4ace2937u;
    randomize(painted, random, &changed);
    TexPaintMaskEffect &blur = BKE_tex_paint_mask_effect_ensure(
        layer_, 3, TEXPAINT_MASK_EFFECT_FILTER);
    blur.filter_radius = 6;
    rcti region;
    ASSERT_TRUE(BKE_tex_paint_mask_evaluate(data_, layer_, nullptr, region));
    TexPaintMaskEvalStats local, full;
    const auto local_start = Clock::now();
    for (int i = 0; i < 8; i++) {
      ASSERT_TRUE(BKE_tex_paint_mask_evaluate(data_, layer_, &changed, region, &local));
    }
    const double local_ms =
        std::chrono::duration<double, std::milli>(Clock::now() - local_start).count() / 8;
    const auto full_start = Clock::now();
    for (int i = 0; i < 3; i++) {
      ASSERT_TRUE(BKE_tex_paint_mask_evaluate(data_, layer_, nullptr, region, &full));
    }
    const double full_ms =
        std::chrono::duration<double, std::milli>(Clock::now() - full_start).count() / 3;
    std::printf(
        "MASK_REGION_BENCH size=%d local_ms=%.6f full_ms=%.6f input=%llu/%llu effect=%llu/%llu "
        "output=%llu/%llu\n",
        size,
        local_ms,
        full_ms,
        static_cast<unsigned long long>(local.input_pixels),
        static_cast<unsigned long long>(full.input_pixels),
        static_cast<unsigned long long>(local.effect_pixels),
        static_cast<unsigned long long>(full.effect_pixels),
        static_cast<unsigned long long>(local.output_pixels),
        static_cast<unsigned long long>(full.output_pixels));
  }
}

}  // namespace blender::bke::tests
