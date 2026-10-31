/* SPDX-FileCopyrightText: 2026 Blender Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

#include "testing/testing.h"

#include <algorithm>
#include <array>
#include <cstring>

#include "MEM_guardedalloc.h"

#include "BLI_listbase.hh"
#include "BLI_math_vector.hh"
#include "BLI_rect.hh"

#include "DNA_image_types.h"
#include "DNA_material_types.h"
#include "DNA_node_tex_paint_types.h"
#include "DNA_node_types.h"

#include "BKE_global.hh"
#include "BKE_gtest_base.hh"
#include "BKE_idtype.hh"
#include "BKE_image.hh"
#include "BKE_lib_id.hh"
#include "BKE_main.hh"
#include "BKE_material.hh"
#include "BKE_node.hh"
#include "BKE_node_legacy_types.hh"
#include "BKE_node_runtime.hh"
#include "BKE_tex_paint_layer.hh"
#include "BKE_tex_paint_mask.hh"

#include "IMB_colormanagement.hh"
#include "IMB_imbuf.hh"
#include "IMB_imbuf_types.hh"

namespace blender::bke::tests {

class TexPaintLayerTest : public BlenderGTestBase {
 protected:
  Main *bmain_ = nullptr;
  bNodeTree *ntree_ = nullptr;
  bNode *node_ = nullptr;

  void SetUp() override
  {
    BlenderGTestBase::SetUp();
    bmain_ = BKE_main_new();
    G_MAIN = bmain_;
    ntree_ = bke::node_tree_add_tree(bmain_, "ShaderTree", "ShaderNodeTree");
    node_ = bke::node_add_static_node(nullptr, *ntree_, SH_NODE_TEX_PAINT);
    ASSERT_NE(node_, nullptr);
    ASSERT_NE(node_->storage, nullptr);
  }

  void TearDown() override
  {
    BKE_main_free(bmain_);
    G_MAIN = nullptr;
    BlenderGTestBase::TearDown();
  }

  NodeTexPaint &storage()
  {
    return *static_cast<NodeTexPaint *>(node_->storage);
  }

  float4 read_master_pixel(int x, int y)
  {
    Image *ima = reinterpret_cast<Image *>(node_->id);
    ImBuf *ibuf = BKE_image_acquire_ibuf(ima, &storage().image.iuser, nullptr);
    EXPECT_NE(ibuf, nullptr);
    const size_t idx = (size_t(y) * ibuf->x + size_t(x)) * 4;
    float4 out(0.0f);
    if (const float *f = ibuf->float_data()) {
      out = float4(f[idx + 0], f[idx + 1], f[idx + 2], f[idx + 3]);
    }
    else if (const uint8_t *b = ibuf->byte_data()) {
      out = float4(b[idx + 0], b[idx + 1], b[idx + 2], b[idx + 3]) * (1.0f / 255.0f);
    }
    BKE_image_release_ibuf(ima, ibuf, nullptr);
    return out;
  }

  std::array<Array<float4>, 3> read_channel_images()
  {
    const std::array<Image *, 3> images = {
        reinterpret_cast<Image *>(node_->id), storage().pbr_image, storage().emission_image};
    std::array<ImageUser *, 3> users = {
        &storage().image.iuser, &storage().pbr_iuser, &storage().emission_iuser};
    std::array<Array<float4>, 3> result;
    for (int i = 0; i < 3; i++) {
      ImBuf *buffer = BKE_image_acquire_ibuf(images[i], users[i], nullptr);
      EXPECT_NE(buffer, nullptr);
      if (buffer) {
        result[i] = BKE_tex_paint_pixels_read(*buffer);
        BKE_image_release_ibuf(images[i], buffer, nullptr);
      }
    }
    return result;
  }

  void expect_channel_images_near(const std::array<Array<float4>, 3> &expected,
                                  const float tolerance)
  {
    const std::array<Array<float4>, 3> actual = read_channel_images();
    for (int image = 0; image < 3; image++) {
      ASSERT_EQ(actual[image].size(), expected[image].size());
      for (const int64_t pixel : actual[image].index_range()) {
        for (int channel = 0; channel < 4; channel++) {
          EXPECT_NEAR(actual[image][pixel][channel], expected[image][pixel][channel], tolerance);
        }
      }
    }
  }
};

TEST_F(TexPaintLayerTest, CanvasInitCreatesSingleMasterImageAndBaseLayer)
{
  const float base_color[4] = {0.2f, 0.4f, 0.8f, 1.0f};
  Image *master = BKE_tex_paint_canvas_init(
      *bmain_, *node_, "LayeredCanvas", 16, 16, true, base_color);
  ASSERT_NE(master, nullptr);
  EXPECT_EQ(node_->id, &master->id);
  EXPECT_TRUE(BKE_tex_paint_is_initialized(*node_));

  /* Color, packed PBR and emission are shared master images; layer pixels remain private. */
  EXPECT_EQ(BLI_listbase_count(&bmain_->images), 3);
  EXPECT_EQ(BKE_tex_paint_layer_count(storage()), 1);

  const float4 px = read_master_pixel(8, 8);
  EXPECT_NEAR(px.x, 0.2f, 1e-4f);
  EXPECT_NEAR(px.y, 0.4f, 1e-4f);
  EXPECT_NEAR(px.z, 0.8f, 1e-4f);
  EXPECT_NEAR(px.w, 1.0f, 1e-4f);
}

TEST_F(TexPaintLayerTest, DirtyRectCompositeAndBelowActiveCache)
{
  const float base_color[4] = {0.0f, 0.0f, 1.0f, 1.0f};
  BKE_tex_paint_canvas_init(*bmain_, *node_, "Canvas", 16, 16, true, base_color);

  /* Add a paint layer on top. */
  TexPaintLayer *top = BKE_tex_paint_layer_add(storage(), "Top", TEXPAINT_LAYER_PAINT);
  ASSERT_NE(top, nullptr);
  EXPECT_EQ(storage().active_layer_index, 1);
  EXPECT_EQ(BLI_listbase_count(&bmain_->images), 3);

  /* Prime the full composite and below-active cache. */
  EXPECT_TRUE(BKE_tex_paint_composite_full(*node_));

  /* Paint red with 50% alpha into pixel (4, 4) of the active top layer via stroke API. */
  Image *master = reinterpret_cast<Image *>(node_->id);
  ImBuf *master_ibuf = BKE_image_acquire_ibuf(master, &storage().image.iuser, nullptr);
  ImBuf *target = BKE_tex_paint_stroke_begin(*node_, master_ibuf);
  BKE_image_release_ibuf(master, master_ibuf, nullptr);
  ASSERT_NE(target, nullptr);
  EXPECT_EQ(target, top->ibuf);

  /* Float ImBuf uses premultiplied alpha: straight (1, 0, 0, 0.5) -> premul (0.5, 0, 0, 0.5). */
  float *dst = target->float_data_for_write() + (size_t(4) * 16 + 4) * 4;
  dst[0] = 0.5f;
  dst[1] = 0.0f;
  dst[2] = 0.0f;
  dst[3] = 0.5f;

  rcti dirty_rect;
  BLI_rcti_init(&dirty_rect, 4, 5, 4, 5);
  BKE_tex_paint_stroke_update(*node_, target, dirty_rect);

  /* Pixel (4, 4) should be 50% red over blue = (0.5, 0.0, 0.5, 1.0). */
  const float4 painted = read_master_pixel(4, 4);
  EXPECT_NEAR(painted.x, 0.5f, 1e-4f);
  EXPECT_NEAR(painted.y, 0.0f, 1e-4f);
  EXPECT_NEAR(painted.z, 0.5f, 1e-4f);
  EXPECT_NEAR(painted.w, 1.0f, 1e-4f);

  /* Neighboring pixel (5, 4) remains pure blue. */
  const float4 untouched = read_master_pixel(5, 4);
  EXPECT_NEAR(untouched.x, 0.0f, 1e-4f);
  EXPECT_NEAR(untouched.y, 0.0f, 1e-4f);
  EXPECT_NEAR(untouched.z, 1.0f, 1e-4f);
  EXPECT_NEAR(untouched.w, 1.0f, 1e-4f);

  /* Undo buffer lookup resolves the synthetic layer filepath back to `top->ibuf`. */
  EXPECT_TRUE(BKE_tex_paint_is_layer_buffer_name(target->filepath.c_str()));
  bNode *found_node = nullptr;
  ImBuf *undo_ibuf = BKE_tex_paint_undo_buffer_find(
      *bmain_, master, target->filepath.c_str(), &found_node);
  EXPECT_EQ(found_node, node_);
  EXPECT_EQ(undo_ibuf, top->ibuf);
}

TEST_F(TexPaintLayerTest, ClippingMaskLayerMaskAndMergeDown)
{
  const float clear_color[4] = {0.0f, 0.0f, 0.0f, 0.0f};
  BKE_tex_paint_canvas_init(*bmain_, *node_, "ClipCanvas", 8, 8, true, clear_color);

  TexPaintLayer *base = BKE_tex_paint_layer_active(storage());
  ASSERT_NE(base, nullptr);
  ImBuf *base_ibuf = BKE_tex_paint_layer_ensure_ibuf(storage(), *base);
  ASSERT_NE(base_ibuf, nullptr);

  /* Set pixel (2, 2) on base layer to opaque green, leaving (3, 2) transparent. */
  float *base_px = base_ibuf->float_data_for_write() + (size_t(2) * 8 + 2) * 4;
  base_px[0] = 0.0f;
  base_px[1] = 1.0f;
  base_px[2] = 0.0f;
  base_px[3] = 1.0f;
  BKE_tex_paint_layer_pixels_changed(storage(), *base, nullptr);

  /* Add a Fill layer above with clipping enabled. */
  TexPaintLayer *fill = BKE_tex_paint_layer_add(storage(), "Fill", TEXPAINT_LAYER_FILL);
  ASSERT_NE(fill, nullptr);
  fill->fill_color[0] = 1.0f;
  fill->fill_color[1] = 0.0f;
  fill->fill_color[2] = 0.0f;
  fill->fill_color[3] = 1.0f;
  fill->flag |= TEXPAINT_LAYER_CLIPPING;

  EXPECT_TRUE(BKE_tex_paint_composite_full(*node_));

  /* Where base is opaque (2, 2), clipped red fill replaces green. */
  const float4 clipped_on = read_master_pixel(2, 2);
  EXPECT_NEAR(clipped_on.x, 1.0f, 1e-4f);
  EXPECT_NEAR(clipped_on.y, 0.0f, 1e-4f);
  EXPECT_NEAR(clipped_on.w, 1.0f, 1e-4f);

  /* Where base is transparent (3, 2), clipped fill is masked out completely. */
  const float4 clipped_off = read_master_pixel(3, 2);
  EXPECT_NEAR(clipped_off.x, 0.0f, 1e-4f);
  EXPECT_NEAR(clipped_off.w, 0.0f, 1e-4f);

  /* Merge down flattens the fill layer into the base layer. */
  fill->flag &= ~TEXPAINT_LAYER_CLIPPING;
  fill->opacity = 0.5f;
  EXPECT_TRUE(BKE_tex_paint_layer_merge_down(storage()));
  EXPECT_EQ(BKE_tex_paint_layer_count(storage()), 1);
  EXPECT_TRUE(BKE_tex_paint_composite_full(*node_));

  const float4 merged = read_master_pixel(2, 2);
  EXPECT_NEAR(merged.x, 0.5f, 1e-4f);
  EXPECT_NEAR(merged.y, 0.5f, 1e-4f);
  EXPECT_NEAR(merged.w, 1.0f, 1e-4f);
}

TEST_F(TexPaintLayerTest, FillLayerGuardsDirectBrushStrokeUntilMaskSelected)
{
  const float clear_color[4] = {0.2f, 0.2f, 0.2f, 1.0f};
  Image *master = BKE_tex_paint_canvas_init(
      *bmain_, *node_, "GuardCanvas", 8, 8, true, clear_color);
  ASSERT_NE(master, nullptr);

  TexPaintLayer *fill = BKE_tex_paint_layer_add(storage(), "Fill", TEXPAINT_LAYER_FILL);
  ASSERT_NE(fill, nullptr);
  storage().paint_target = TEXPAINT_TARGET_COLOR;

  ImBuf *master_ibuf = BKE_image_acquire_ibuf(master, &storage().image.iuser, nullptr);
  ASSERT_NE(master_ibuf, nullptr);

  /* Direct brush stroke on a Fill layer in Color mode must be blocked and must not create a mask.
   */
  EXPECT_EQ(BKE_tex_paint_paint_target_ibuf(storage()), nullptr);
  EXPECT_EQ(BKE_tex_paint_stroke_begin(*node_, master_ibuf), nullptr);
  EXPECT_NE(BKE_tex_paint_stroke_block_reason(*node_), nullptr);
  EXPECT_EQ(storage().paint_target, TEXPAINT_TARGET_COLOR);
  EXPECT_EQ(fill->flag & TEXPAINT_LAYER_USE_MASK, 0);

  /* Enabling a mask and targeting TEXPAINT_TARGET_MASK allows painting into the Fill layer's mask.
   */
  BKE_tex_paint_layer_mask_fill(storage(), *fill, 1.0f);
  storage().paint_target = TEXPAINT_TARGET_MASK;
  EXPECT_EQ(BKE_tex_paint_stroke_block_reason(*node_), nullptr);
  ImBuf *mask_target = BKE_tex_paint_stroke_begin(*node_, master_ibuf);
  EXPECT_NE(mask_target, nullptr);
  EXPECT_EQ(mask_target, fill->mask_ibuf);

  /* Hidden or locked layers are also guarded against brush strokes. */
  fill->flag &= ~TEXPAINT_LAYER_VISIBLE;
  EXPECT_EQ(BKE_tex_paint_stroke_begin(*node_, master_ibuf), nullptr);
  EXPECT_NE(BKE_tex_paint_stroke_block_reason(*node_), nullptr);

  BKE_image_release_ibuf(master, master_ibuf, nullptr);
}

TEST_F(TexPaintLayerTest, EmptySelectionDoesNotBecomeUnrestricted)
{
  const float color[4] = {1, 1, 1, 1};
  BKE_tex_paint_canvas_init(*bmain_, *node_, "Selected", 4, 3, true, color);
  EXPECT_EQ(BKE_tex_paint_selection_value(*node_, 1, 1), 1.0f);
  Array<float> mask(12, 0.0f);
  BKE_tex_paint_selection_set(storage(), mask);
  EXPECT_TRUE(storage().selection_active);
  EXPECT_EQ(BKE_tex_paint_selection(storage()).size(), 12);
  EXPECT_EQ(BKE_tex_paint_selection_value(*node_, 1, 1), 0.0f);
  BKE_tex_paint_selection_clear(storage());
  EXPECT_EQ(BKE_tex_paint_selection_value(*node_, 1, 1), 1.0f);
}

TEST_F(TexPaintLayerTest, SelectionCopyOwnsIndependentCoverage)
{
  const float color[4] = {1, 1, 1, 1};
  BKE_tex_paint_canvas_init(*bmain_, *node_, "Selected", 4, 3, true, color);
  Array<float> mask(12, 0.5f);
  BKE_tex_paint_selection_set(storage(), mask);
  NodeTexPaint copied = dna::shallow_copy(storage());
  BKE_tex_paint_storage_copy_contents(copied, storage(), true);
  EXPECT_NE(copied.selection, storage().selection);
  copied.selection[0] = 0;
  EXPECT_EQ(storage().selection[0], 0.5f);
  BKE_tex_paint_storage_free_contents(copied);
}

TEST_F(TexPaintLayerTest, SelectionOutlinesInvalidateAndMergeRuns)
{
  const float color[4] = {1, 1, 1, 1};
  BKE_tex_paint_canvas_init(*bmain_, *node_, "Outline", 4, 3, true, color);
  Array<float> mask(12, 1.0f);
  BKE_tex_paint_selection_set(storage(), mask);
  /* A full rectangular selection needs four lines, regardless of its pixel dimensions. */
  EXPECT_EQ(BKE_tex_paint_selection_contours(storage()).size(), 8);
  mask.fill(0);
  BKE_tex_paint_selection_set(storage(), mask);
  EXPECT_TRUE(BKE_tex_paint_selection_contours(storage()).is_empty());
  mask[5] = 1;
  BKE_tex_paint_selection_set(storage(), mask);
  EXPECT_EQ(BKE_tex_paint_selection_contours(storage()).size(), 8);
  BKE_tex_paint_selection_clear(storage());
  EXPECT_TRUE(BKE_tex_paint_selection_contours(storage()).is_empty());
}

TEST_F(TexPaintLayerTest, TransformSourceCopiesOwnPixelsAndSelectionEditsReleaseIt)
{
  const float color[4] = {1, 0, 0, 0.5f};
  BKE_tex_paint_canvas_init(*bmain_, *node_, "Floating", 4, 3, true, color);
  Array<float> mask(12, 0.5f);
  BKE_tex_paint_selection_set(storage(), mask);
  const Array<float4> source = BKE_tex_paint_pixels_read(
      *BKE_tex_paint_paint_target_ibuf(storage()));
  BKE_tex_paint_selection_transform_store(storage(), source, mask);
  storage().selection_transform[2][0] = 2;
  storage().selection_transform_layer_uid = BKE_tex_paint_layer_active(storage())->uid;
  storage().selection_transform_hash = 123;

  NodeTexPaint copy = dna::shallow_copy(storage());
  BKE_tex_paint_storage_copy_contents(copy, storage(), true);
  ASSERT_NE(copy.selection_transform_source, nullptr);
  ASSERT_NE(copy.selection_transform_mask, nullptr);
  EXPECT_NE(copy.selection_transform_source, storage().selection_transform_source);
  EXPECT_NE(copy.selection_transform_mask, storage().selection_transform_mask);
  EXPECT_EQ(copy.selection_transform[2][0], 2);
  EXPECT_EQ(copy.selection_transform_hash, 123);
  copy.selection_transform_source[0] = 0;
  copy.selection_transform_mask[0] = 0;
  EXPECT_EQ(storage().selection_transform_source[0], source[0].x);
  EXPECT_EQ(storage().selection_transform_mask[0], 0.5f);
  BKE_tex_paint_storage_free_contents(copy);

  NodeTexPaint evaluated = dna::shallow_copy(storage());
  BKE_tex_paint_storage_copy_contents(evaluated, storage(), false);
  EXPECT_EQ(evaluated.selection_transform_source, nullptr);
  EXPECT_EQ(evaluated.selection_transform_mask, nullptr);
  BKE_tex_paint_storage_free_contents(evaluated);
  BKE_tex_paint_selection_set(storage(), mask, true);
  EXPECT_NE(storage().selection_transform_source, nullptr);
  BKE_tex_paint_selection_set(storage(), mask);
  EXPECT_EQ(storage().selection_transform_source, nullptr);
  EXPECT_EQ(storage().selection_transform_mask, nullptr);
}

TEST_F(TexPaintLayerTest, AccumulatedTransformDoesNotCollectDestinationPixels)
{
  Array<float4> source(4, float4(0)), result(4);
  source[0] = float4(0.5f, 0, 0, 0.5f);
  source[1] = float4(0, 0, 1, 1);
  Array<float> mask(4, 0.0f), coverage(4);
  mask[0] = 1;
  const rcti full = {0, 4, 0, 1};
  float3x3 transform = float3x3::identity();
  transform[2][0] = 1;
  BKE_tex_paint_pixels_transform_affine(source, mask, 4, 1, transform, result, coverage, full);
  EXPECT_EQ(result[1], float4(0.5f, 0, 0.5f, 1));
  transform[2][0] = 2;
  BKE_tex_paint_pixels_transform_affine(source, mask, 4, 1, transform, result, coverage, full);
  EXPECT_EQ(result[1], source[1]);
  EXPECT_EQ(result[2], source[0]);
  EXPECT_EQ(result[0], float4(0));
  EXPECT_EQ(coverage[1], 0);
  EXPECT_EQ(coverage[2], 1);
}

TEST_F(TexPaintLayerTest, WandContiguousAndDisconnectedIslands)
{
  const float color[4] = {1, 0, 0, 1};
  BKE_tex_paint_canvas_init(*bmain_, *node_, "Wand", 3, 2, true, color);
  ImBuf *buffer = BKE_tex_paint_paint_target_ibuf(storage());
  Array<float4> pixels = BKE_tex_paint_pixels_read(*buffer);
  pixels[1] = pixels[4] = float4(0, 0, 1, 1);
  const rcti full = {0, 3, 0, 2};
  BKE_tex_paint_pixels_write(*buffer, pixels, full);
  Array<float> connected = BKE_tex_paint_select_wand(*buffer, int2(0, 0), 0.01f, true);
  EXPECT_EQ(connected[0], 1);
  EXPECT_EQ(connected[3], 1);
  EXPECT_EQ(connected[2], 0);
  Array<float> all = BKE_tex_paint_select_wand(*buffer, int2(0, 0), 0.01f, false);
  EXPECT_EQ(all[2], 1);
  EXPECT_EQ(all[1], 0);
  Array<float> outside = BKE_tex_paint_select_wand(*buffer, int2(-1, 0), 0.01f, true);
  for (float value : outside) {
    EXPECT_EQ(value, 0);
  }
}

TEST_F(TexPaintLayerTest, LassoPixelCentersAndWinding)
{
  const float2 polygon[] = {float2(1, 1), float2(3, 1), float2(3, 3), float2(1, 3)};
  Array<float> mask = BKE_tex_paint_select_polygon(4, 4, Span(polygon, 4));
  for (int y = 0; y < 4; y++) {
    for (int x = 0; x < 4; x++) {
      EXPECT_EQ(mask[y * 4 + x], x >= 1 && x < 3 && y >= 1 && y < 3 ? 1.0f : 0.0f);
    }
  }
  const float2 reverse[] = {polygon[3], polygon[2], polygon[1], polygon[0]};
  Array<float> reversed = BKE_tex_paint_select_polygon(4, 4, Span(reverse, 4));
  EXPECT_EQ(mask.as_span(), reversed.as_span());
}

TEST_F(TexPaintLayerTest, FeatheredIdentityPreservesOpacityAndHdr)
{
  Array<float4> source(4, float4(2.0f, 0.5f, -0.1f, 0.75f));
  Array<float> selection(4, 0.5f), out_selection(4, 0.0f);
  Array<float4> result(4);
  const rcti full = {0, 2, 0, 2};
  BKE_tex_paint_pixels_transform(
      source, selection, 2, 2, float2(1), float2(0), 0, float2(1), result, out_selection, full);
  for (int i = 0; i < 4; i++) {
    EXPECT_EQ(result[i], source[i]);
    EXPECT_EQ(out_selection[i], 0.5f);
  }
}

TEST_F(TexPaintLayerTest, TranslateSelectionClearsSourceAndMovesCoverage)
{
  Array<float4> source(6, float4(0)), result(6, float4(0));
  source[0] = float4(1, 0, 0, 1);
  source[5] = float4(0, 1, 0, 1);
  Array<float> selection(6, 0.0f), out_selection(6, 0.0f);
  selection[0] = 1;
  const rcti full = {0, 3, 0, 2};
  BKE_tex_paint_pixels_transform(source,
                                 selection,
                                 3,
                                 2,
                                 float2(0.5f),
                                 float2(1, 0),
                                 0,
                                 float2(1),
                                 result,
                                 out_selection,
                                 full);
  EXPECT_EQ(result[0], float4(0));
  EXPECT_EQ(result[1], source[0]);
  EXPECT_EQ(result[5], source[5]);
  EXPECT_EQ(out_selection[0], 0);
  EXPECT_EQ(out_selection[1], 1);
}

TEST_F(TexPaintLayerTest, EmptySelectionTransformPreservesEveryPixel)
{
  Array<float4> source(4, float4(0.4f, 0.2f, 0.1f, 0.8f)), result(4);
  Array<float> selection(4, 0.0f), output_selection(4);
  const rcti full = {0, 2, 0, 2};
  BKE_tex_paint_pixels_transform(source,
                                 selection,
                                 2,
                                 2,
                                 float2(1),
                                 float2(2),
                                 1.2f,
                                 float2(0.5f),
                                 result,
                                 output_selection,
                                 full);
  EXPECT_EQ(source.as_span(), result.as_span());
}

TEST_F(TexPaintLayerTest, PartialTransformLeavesOutsideRegionUntouched)
{
  Array<float4> source(16, float4(1)), result(16, float4(-1));
  Array<float> selection(16, 0.5f), output_selection(16, -1.0f);
  const rcti rect = {1, 3, 1, 3};
  BKE_tex_paint_pixels_transform(
      source, selection, 4, 4, float2(2), float2(0), 0, float2(1), result, output_selection, rect);
  EXPECT_EQ(result[0], float4(-1));
  EXPECT_EQ(result[5], float4(1));
  EXPECT_EQ(output_selection[0], -1.0f);
}

TEST_F(TexPaintLayerTest, NativePixelServicePreservesByteAndFloatAlpha)
{
  for (const bool floating : {false, true}) {
    const float color[4] = {0.2f, 0.4f, 0.6f, 0.5f};
    BKE_tex_paint_canvas_init(*bmain_, *node_, "Pixels", 2, 2, floating, color);
    ImBuf *buffer = BKE_tex_paint_paint_target_ibuf(storage());
    Array<float4> before = BKE_tex_paint_pixels_read(*buffer);
    const rcti full = {0, 2, 0, 2};
    BKE_tex_paint_pixels_write(*buffer, before, full);
    Array<float4> after = BKE_tex_paint_pixels_read(*buffer);
    EXPECT_EQ(before.as_span(), after.as_span());
  }
}

TEST_F(TexPaintLayerTest, ColorFiltersPreserveSourceMaskAndAlpha)
{
  const float color[4] = {0.2f, 0.4f, 0.8f, 0.5f};
  BKE_tex_paint_canvas_init(*bmain_, *node_, "Filtered", 4, 4, true, color);
  TexPaintLayer &layer = *BKE_tex_paint_layer_active(storage());
  ImBuf *mask = BKE_tex_paint_layer_ensure_mask_ibuf(storage(), layer);
  const Array<float4> mask_before = BKE_tex_paint_pixels_read(*mask);
  const Array<float4> source = BKE_tex_paint_pixels_read(*layer.ibuf);
  TexPaintColorFilter *filter = MEM_new<TexPaintColorFilter>(__func__);
  filter->type = TEXPAINT_COLOR_FILTER_INVERT;
  BLI_addtail(&layer.color_filters, filter);
  BKE_tex_paint_composite_full(*node_);
  const float4 result = read_master_pixel(1, 1);
  EXPECT_NEAR(result.x, 0.4f, 1e-6f);
  EXPECT_NEAR(result.y, 0.3f, 1e-6f);
  EXPECT_NEAR(result.z, 0.1f, 1e-6f);
  EXPECT_EQ(result.w, 0.5f);
  EXPECT_EQ(source.as_span(), BKE_tex_paint_pixels_read(*layer.ibuf).as_span());
  EXPECT_EQ(mask_before.as_span(), BKE_tex_paint_pixels_read(*mask).as_span());
  filter->enabled = false;
  BKE_tex_paint_composite_full(*node_);
  EXPECT_EQ(read_master_pixel(1, 1), source[5]);
}

TEST_F(TexPaintLayerTest, ColorFilterOrderAndStrength)
{
  const float color[4] = {0.2f, 0.4f, 0.6f, 1};
  BKE_tex_paint_canvas_init(*bmain_, *node_, "Filtered", 2, 2, true, color);
  TexPaintLayer &layer = *BKE_tex_paint_layer_active(storage());
  TexPaintColorFilter *bright = MEM_new<TexPaintColorFilter>(__func__);
  bright->type = TEXPAINT_COLOR_FILTER_BRIGHT_CONTRAST;
  bright->bright_contrast[0] = 0.2f;
  bright->strength = 0.5f;
  BLI_addtail(&layer.color_filters, bright);
  TexPaintColorFilter *invert = MEM_new<TexPaintColorFilter>(__func__);
  invert->type = TEXPAINT_COLOR_FILTER_INVERT;
  BLI_addtail(&layer.color_filters, invert);
  BKE_tex_paint_composite_full(*node_);
  EXPECT_NEAR(read_master_pixel(0, 0).x, 0.7f, 1e-6f);
  BLI_listbase_link_move(&layer.color_filters, invert, -1);
  BKE_tex_paint_composite_full(*node_);
  EXPECT_NEAR(read_master_pixel(0, 0).x, 0.9f, 1e-6f);
}

TEST_F(TexPaintLayerTest, IdentityColorFiltersPreserveHdrAndCopiesOwnTheirFilters)
{
  const float color[4] = {2.0f, 0.4f, 0.6f, 0.5f};
  BKE_tex_paint_canvas_init(*bmain_, *node_, "Filtered", 2, 2, true, color);
  TexPaintLayer &layer = *BKE_tex_paint_layer_active(storage());
  for (int type : {TEXPAINT_COLOR_FILTER_HSV,
                   TEXPAINT_COLOR_FILTER_BRIGHT_CONTRAST,
                   TEXPAINT_COLOR_FILTER_LEVELS})
  {
    TexPaintColorFilter *filter = MEM_new<TexPaintColorFilter>(__func__);
    filter->type = type;
    BLI_addtail(&layer.color_filters, filter);
  }
  BKE_tex_paint_composite_full(*node_);
  EXPECT_EQ(read_master_pixel(0, 0), float4(1, 0.2f, 0.3f, 0.5f));
  TexPaintLayer *duplicate = BKE_tex_paint_layer_duplicate(storage(), layer);
  ASSERT_EQ(duplicate->color_filters.count(), 3);
  EXPECT_NE(duplicate->color_filters.first(), layer.color_filters.first());
  duplicate->color_filters.first()->enabled = 0;
  EXPECT_EQ(layer.color_filters.first()->enabled, 1);
  NodeTexPaint copy = dna::shallow_copy(storage());
  BKE_tex_paint_storage_copy_contents(copy, storage(), true);
  ASSERT_EQ(copy.layers.first()->color_filters.count(), 3);
  EXPECT_NE(copy.layers.first()->color_filters.first(), layer.color_filters.first());
  BKE_tex_paint_storage_free_contents(copy);
}

TEST_F(TexPaintLayerTest, MergeBakesColorFiltersExactlyOnce)
{
  const float color[4] = {0.2f, 0.4f, 0.6f, 1};
  BKE_tex_paint_canvas_init(*bmain_, *node_, "Filtered", 2, 2, true, color);
  TexPaintLayer &layer = *BKE_tex_paint_layer_active(storage());
  TexPaintColorFilter *filter = MEM_new<TexPaintColorFilter>(__func__);
  filter->type = TEXPAINT_COLOR_FILTER_INVERT;
  BLI_addtail(&layer.color_filters, filter);
  BKE_tex_paint_layer_add(storage(), "Transparent", TEXPAINT_LAYER_PAINT);
  BKE_tex_paint_composite_full(*node_);
  const float4 before = read_master_pixel(0, 0);
  EXPECT_TRUE(BKE_tex_paint_layer_merge_down(storage()));
  BKE_tex_paint_composite_full(*node_);
  EXPECT_EQ(layer.color_filters.count(), 0);
  EXPECT_EQ(read_master_pixel(0, 0), before);
}

TEST_F(TexPaintLayerTest, BlurFiltersPreserveSourceAndPremultipliedCoverage)
{
  const float clear[4] = {0, 0, 0, 0};
  BKE_tex_paint_canvas_init(*bmain_, *node_, "Blur", 9, 9, true, clear);
  TexPaintLayer &layer = *BKE_tex_paint_layer_active(storage());
  Array<float4> source(81, float4(0, 0, 100, 0));
  /* The shared pixel service takes premultiplied values: straight HDR red 2 at half coverage. */
  source[4 * 9 + 4] = float4(1, 0, 0, 0.5f);
  const rcti full = {0, 9, 0, 9};
  BKE_tex_paint_pixels_write(*layer.ibuf, source, full);
  const Array<float4> before = BKE_tex_paint_pixels_read(*layer.ibuf);
  ImBuf *mask = BKE_tex_paint_layer_ensure_mask_ibuf(storage(), layer);
  const Array<float4> mask_before = BKE_tex_paint_pixels_read(*mask);
  TexPaintColorFilter *blur = MEM_new<TexPaintColorFilter>(__func__);
  blur->type = TEXPAINT_COLOR_FILTER_BLUR;
  blur->blur_radius = 1;
  BLI_addtail(&layer.color_filters, blur);
  BKE_tex_paint_composite_full(*node_);
  const float4 center = read_master_pixel(4, 4);
  EXPECT_NEAR(center.x, 1.0f / 9, 1e-6f);
  EXPECT_EQ(center.z, 0);
  EXPECT_NEAR(center.w, 0.5f / 9, 1e-6f);
  EXPECT_EQ(read_master_pixel(3, 3), center);
  EXPECT_EQ(read_master_pixel(2, 4), float4(0));
  EXPECT_EQ(BKE_tex_paint_pixels_read(*layer.ibuf).as_span(), before.as_span());
  EXPECT_EQ(BKE_tex_paint_pixels_read(*mask).as_span(), mask_before.as_span());
  blur->enabled = false;
  BKE_tex_paint_composite_full(*node_);
  EXPECT_EQ(read_master_pixel(4, 4), float4(1, 0, 0, 0.5f));
}

TEST_F(TexPaintLayerTest, BlurDirtyHaloMatchesFullCompositionAcrossLayerCache)
{
  const float clear[4] = {0, 0, 0, 0};
  BKE_tex_paint_canvas_init(*bmain_, *node_, "Blur", 32, 24, true, clear);
  TexPaintLayer &layer = *BKE_tex_paint_layer_active(storage());
  TexPaintColorFilter *blur = MEM_new<TexPaintColorFilter>(__func__);
  blur->type = TEXPAINT_COLOR_FILTER_BLUR;
  blur->blur_radius = 2;
  BLI_addtail(&layer.color_filters, blur);
  TexPaintColorFilter *levels = MEM_new<TexPaintColorFilter>(__func__);
  levels->type = TEXPAINT_COLOR_FILTER_LEVELS;
  levels->gamma = 0.5f;
  BLI_addtail(&layer.color_filters, levels);
  TexPaintColorFilter *second = MEM_new<TexPaintColorFilter>(__func__);
  second->type = TEXPAINT_COLOR_FILTER_BLUR;
  second->blur_radius = 1;
  second->strength = 0.5f;
  BLI_addtail(&layer.color_filters, second);
  BKE_tex_paint_layer_add(storage(), "Top", TEXPAINT_LAYER_PAINT);
  BKE_tex_paint_composite_full(*node_);
  const rcti changed = {15, 16, 11, 12};
  Array<float4> source = BKE_tex_paint_pixels_read(*layer.ibuf);
  source[11 * 32 + 15] = float4(0.8f, 0.2f, 0.1f, 0.6f);
  BKE_tex_paint_pixels_write(*layer.ibuf, source, changed);
  BKE_tex_paint_layer_pixels_changed(storage(), layer, &changed);
  BKE_tex_paint_composite_rect(*node_, &changed);
  EXPECT_GT(read_master_pixel(12, 11).w, 0);
  Array<float4> partial(32 * 24);
  for (int y = 0; y < 24; y++) {
    for (int x = 0; x < 32; x++) {
      partial[y * 32 + x] = read_master_pixel(x, y);
    }
  }
  BKE_tex_paint_composite_full(*node_);
  for (int y = 0; y < 24; y++) {
    for (int x = 0; x < 32; x++) {
      const float4 full = read_master_pixel(x, y);
      for (int c = 0; c < 4; c++) {
        EXPECT_NEAR(partial[y * 32 + x][c], full[c], 1e-6f);
      }
    }
  }
  rcti halo = changed;
  BKE_tex_paint_layer_effects_expand_rect(storage(), layer, halo);
  EXPECT_EQ(halo.xmin, 12);
  EXPECT_EQ(halo.xmax, 19);
  EXPECT_EQ(halo.ymin, 8);
  EXPECT_EQ(halo.ymax, 15);
}

TEST_F(TexPaintLayerTest, BlurFilterOrderAndStrengthAreNonDestructive)
{
  const float clear[4] = {0, 0, 0, 0};
  BKE_tex_paint_canvas_init(*bmain_, *node_, "Blur", 5, 1, true, clear);
  TexPaintLayer &layer = *BKE_tex_paint_layer_active(storage());
  Array<float4> source(5, float4(0, 0, 0, 1));
  source[2] = float4(1, 1, 1, 1);
  const rcti full = {0, 5, 0, 1};
  BKE_tex_paint_pixels_write(*layer.ibuf, source, full);
  TexPaintColorFilter *blur = MEM_new<TexPaintColorFilter>(__func__);
  blur->type = TEXPAINT_COLOR_FILTER_BLUR;
  blur->blur_radius = 1;
  BLI_addtail(&layer.color_filters, blur);
  TexPaintColorFilter *levels = MEM_new<TexPaintColorFilter>(__func__);
  levels->type = TEXPAINT_COLOR_FILTER_LEVELS;
  levels->gamma = 0.5f;
  BLI_addtail(&layer.color_filters, levels);
  BKE_tex_paint_composite_full(*node_);
  EXPECT_NEAR(read_master_pixel(2, 0).x, 1.0f / 9, 1e-6f);
  BLI_listbase_link_move(&layer.color_filters, levels, -1);
  BKE_tex_paint_composite_full(*node_);
  EXPECT_NEAR(read_master_pixel(2, 0).x, 1.0f / 3, 1e-6f);
  blur->strength = 0.5f;
  BKE_tex_paint_composite_full(*node_);
  EXPECT_NEAR(read_master_pixel(2, 0).x, 2.0f / 3, 1e-6f);
  EXPECT_EQ(BKE_tex_paint_pixels_read(*layer.ibuf).as_span(), source.as_span());
  TexPaintLayer *copy = BKE_tex_paint_layer_duplicate(storage(), layer);
  copy->color_filters.last()->blur_radius = 8;
  EXPECT_EQ(layer.color_filters.last()->blur_radius, 1);
}

TEST_F(TexPaintLayerTest, BlurDirtyTilesPreserveExactZeroCoverageAndByteRgb)
{
  const float clear[4] = {0, 0, 0, 0};
  BKE_tex_paint_canvas_init(*bmain_, *node_, "Blur", 256, 256, false, clear);
  TexPaintLayer &layer = *BKE_tex_paint_layer_active(storage());
  Array<float4> source(256 * 256, float4(0));
  for (int y = 80; y < 176; y++) {
    for (int x = 80; x < 176; x++) {
      source[y * 256 + x] = x < 128 ? float4(0.8f, 0.2f, 0.1f, 1.0f) :
                                      float4(0.1f, 0.4f, 0.2f, 0.5f);
    }
  }
  const rcti full = {0, 256, 0, 256};
  BKE_tex_paint_pixels_write(*layer.ibuf, source, full);
  TexPaintColorFilter *levels = MEM_new<TexPaintColorFilter>(__func__);
  levels->type = TEXPAINT_COLOR_FILTER_LEVELS;
  levels->gamma = 0.5f;
  BLI_addtail(&layer.color_filters, levels);
  TexPaintColorFilter *blur = MEM_new<TexPaintColorFilter>(__func__);
  blur->type = TEXPAINT_COLOR_FILTER_BLUR;
  blur->blur_radius = 4;
  BLI_addtail(&layer.color_filters, blur);
  BKE_tex_paint_composite_full(*node_);
  const rcti stroke = {43, 50, 126, 130};
  for (int y = stroke.ymin; y < stroke.ymax; y++) {
    for (int x = stroke.xmin; x < stroke.xmax; x++) {
      const float alpha = float(13 + (x * 17 + y * 11) % 230) / 255.0f;
      source[y * 256 + x] = float4(0.1f * alpha, 0.2f * alpha, 0.9f * alpha, alpha);
    }
  }
  BKE_tex_paint_pixels_write(*layer.ibuf, source, stroke);
  BKE_tex_paint_layer_pixels_changed(storage(), layer, &stroke);
  BKE_tex_paint_composite_rect(*node_, &stroke);
  Image *image = reinterpret_cast<Image *>(node_->id);
  ImBuf *master = BKE_image_acquire_ibuf(image, &storage().image.iuser, nullptr);
  ASSERT_NE(master, nullptr);
  Array<uint8_t> partial(256 * 256 * 4);
  std::copy_n(master->byte_data(), partial.size(), partial.data());
  BKE_image_release_ibuf(image, master, nullptr);
  BKE_tex_paint_composite_full(*node_);
  EXPECT_EQ(read_master_pixel(44, 200), float4(0));
  EXPECT_EQ(read_master_pixel(100, 240), float4(0));
  master = BKE_image_acquire_ibuf(image, &storage().image.iuser, nullptr);
  ASSERT_NE(master, nullptr);
  int mismatched_channels = 0;
  for (const int64_t i : partial.index_range()) {
    mismatched_channels += partial[i] != master->byte_data()[i];
  }
  EXPECT_EQ(mismatched_channels, 0);
  BKE_image_release_ibuf(image, master, nullptr);
}

TEST_F(TexPaintLayerTest, PlainFillRasterizePreservesNativeMaskEffectsAndChannels)
{
  const float base[4] = {.03f, .2f, .4f, 1};
  BKE_tex_paint_canvas_init(*bmain_, *node_, "Raster Byte", 8, 8, false, base);
  TexPaintLayer &layer = *BKE_tex_paint_layer_add(storage(), "Raster Fill", TEXPAINT_LAYER_FILL);
  const float fill[4] = {.28f, .49f, .71f, .65f};
  std::copy_n(fill, 4, layer.fill_color);
  layer.opacity = .6f;
  layer.channel_flags = TEXPAINT_CHAN_COLOR | TEXPAINT_CHAN_ROUGHNESS | TEXPAINT_CHAN_METALLIC |
                        TEXPAINT_CHAN_HEIGHT | TEXPAINT_CHAN_SPECULAR | TEXPAINT_CHAN_EMISSION;
  layer.fill_roughness = .73f;
  layer.fill_metallic = .23f;
  layer.fill_height = .37f;
  layer.fill_specular = .61f;
  layer.flag |= TEXPAINT_LAYER_USE_MASK;
  TexPaintMaskEffect &painted = BKE_tex_paint_mask_effect_ensure(
      layer, 12, TEXPAINT_MASK_EFFECT_PAINT);
  ImBuf *paint_pixels = BKE_tex_paint_mask_effect_ensure_pixels(storage(), painted);
  uint8_t *paint_bytes = paint_pixels->byte_data_for_write();
  for (int i = 0; i < 64; i++) {
    paint_bytes[i * 4] = paint_bytes[i * 4 + 1] = paint_bytes[i * 4 + 2] = 0;
    paint_bytes[i * 4 + 3] = uint8_t(i * 4);
  }
  layer.mask_effects_active = 1;
  layer.active_mask_effect_uid = painted.uid;
  rcti rect;
  ASSERT_TRUE(BKE_tex_paint_mask_evaluate(storage(), layer, nullptr, rect));
  TexPaintColorFilter *filter = MEM_new<TexPaintColorFilter>(__func__);
  filter->type = TEXPAINT_COLOR_FILTER_INVERT;
  filter->strength = .25f;
  BLI_addtail(&layer.color_filters, filter);
  ASSERT_TRUE(BKE_tex_paint_composite_full(*node_));
  const auto before = read_channel_images();
  const Array<float4> mask_before = BKE_tex_paint_pixels_read(*layer.mask_ibuf);
  const Array<float4> coverage_before = BKE_tex_paint_pixels_read(*paint_pixels);
  const int uid = layer.uid;
  const int channels = layer.channel_flags;
  ASSERT_TRUE(BKE_tex_paint_layer_rasterize(storage(), layer, true));
  EXPECT_EQ(layer.type, TEXPAINT_LAYER_FILL);
  EXPECT_EQ(layer.ibuf, nullptr);
  ASSERT_TRUE(BKE_tex_paint_layer_rasterize(storage(), layer));
  EXPECT_EQ(layer.type, TEXPAINT_LAYER_PAINT);
  EXPECT_EQ(layer.uid, uid);
  EXPECT_EQ(layer.channel_flags, channels);
  EXPECT_EQ(layer.color_filters.first(), filter);
  EXPECT_EQ(layer.mask_effects.first(), &painted);
  EXPECT_EQ(layer.active_mask_effect_uid, painted.uid);
  EXPECT_EQ(layer.mask_effects_active, 1);
  EXPECT_EQ(layer.fill_roughness, .73f);
  EXPECT_EQ(layer.fill_metallic, .23f);
  EXPECT_EQ(layer.fill_height, .37f);
  EXPECT_EQ(layer.fill_specular, .61f);
  EXPECT_EQ(layer.opacity, .6f);
  EXPECT_EQ(BKE_tex_paint_pixels_read(*layer.mask_ibuf).as_span(), mask_before.as_span());
  EXPECT_EQ(BKE_tex_paint_pixels_read(*paint_pixels).as_span(), coverage_before.as_span());
  ASSERT_TRUE(BKE_tex_paint_composite_full(*node_));
  expect_channel_images_near(before, 1.0f / 255.0f + 1e-6f);
}

TEST_F(TexPaintLayerTest, PlainFloatFillRasterizeRetainsHdrAndAlpha)
{
  const float base[4] = {0, 0, 0, 0};
  BKE_tex_paint_canvas_init(*bmain_, *node_, "Raster Float", 4, 4, true, base);
  TexPaintLayer &layer = *BKE_tex_paint_layer_add(storage(), "HDR Fill", TEXPAINT_LAYER_FILL);
  const float fill[4] = {3.5f, .25f, 1.75f, .4f};
  std::copy_n(fill, 4, layer.fill_color);
  ASSERT_TRUE(BKE_tex_paint_composite_full(*node_));
  const auto before = read_channel_images();
  ASSERT_TRUE(BKE_tex_paint_layer_rasterize(storage(), layer));
  ASSERT_NE(layer.ibuf->float_data(), nullptr);
  EXPECT_EQ(layer.ibuf->byte_data(), nullptr);
  const float4 pixel = BKE_tex_paint_pixels_read(*layer.ibuf)[0];
  EXPECT_NEAR(pixel.x, 1.4f, 1e-6f);
  EXPECT_NEAR(pixel.y, .1f, 1e-6f);
  EXPECT_NEAR(pixel.z, .7f, 1e-6f);
  EXPECT_EQ(pixel.w, .4f);
  ASSERT_TRUE(BKE_tex_paint_composite_full(*node_));
  expect_channel_images_near(before, 1e-6f);
}

TEST_F(TexPaintLayerTest, ProjectedFillRasterizeBakesTexturedHeightAndKeepsLiveFilters)
{
  const float base[4] = {.1f, .2f, .3f, 1};
  BKE_tex_paint_canvas_init(*bmain_, *node_, "Raster Projected", 8, 8, true, base);
  TexPaintLayer &layer = *BKE_tex_paint_layer_add(
      storage(), "Projected Fill", TEXPAINT_LAYER_FILL);
  const float fill[4] = {2.0f, .5f, 1.5f, .6f};
  std::copy_n(fill, 4, layer.fill_color);
  layer.channel_flags = TEXPAINT_CHAN_COLOR | TEXPAINT_CHAN_ROUGHNESS | TEXPAINT_CHAN_METALLIC |
                        TEXPAINT_CHAN_HEIGHT | TEXPAINT_CHAN_SPECULAR | TEXPAINT_CHAN_EMISSION;
  layer.fill_height = .55f;
  layer.fill_roughness = .72f;
  layer.fill_metallic = .35f;
  layer.fill_specular = .41f;
  layer.fill_emission[0] = 2.0f;
  layer.fill_emission[1] = .5f;
  layer.fill_emission[2] = 1.0f;
  layer.proj_mode = TEXPAINT_PROJ_UV_REPEAT;
  std::strcpy(layer.proj_image_name, "Evaluated Projection");
  layer.flag |= TEXPAINT_LAYER_SHOW_WIDGET;
  ImBuf *texture = BKE_tex_paint_layer_ensure_ibuf(storage(), layer);
  Array<float4> pixels(64);
  for (int i = 0; i < 64; i++) {
    const float alpha = .2f + .7f * float(i) / 63.0f;
    pixels[i] = float4((.2f + float(i) * .015f) * alpha, .7f * alpha, .4f * alpha, alpha);
  }
  BKE_tex_paint_pixels_write(*texture, pixels, {0, texture->x, 0, texture->y});
  TexPaintColorFilter *levels = MEM_new<TexPaintColorFilter>(__func__);
  levels->type = TEXPAINT_COLOR_FILTER_LEVELS;
  levels->gamma = .8f;
  BLI_addtail(&layer.color_filters, levels);
  TexPaintColorFilter *blur = MEM_new<TexPaintColorFilter>(__func__);
  blur->type = TEXPAINT_COLOR_FILTER_BLUR;
  blur->blur_radius = 1;
  blur->strength = .5f;
  BLI_addtail(&layer.color_filters, blur);
  ASSERT_TRUE(BKE_tex_paint_composite_full(*node_));
  const auto before = read_channel_images();
  const int uid = layer.uid;
  ASSERT_TRUE(BKE_tex_paint_layer_rasterize(storage(), layer, true));
  EXPECT_EQ(layer.type, TEXPAINT_LAYER_FILL);
  EXPECT_EQ(layer.ibuf, texture);
  EXPECT_EQ(layer.pbr_ibuf, nullptr);
  EXPECT_EQ(layer.proj_mode, TEXPAINT_PROJ_UV_REPEAT);
  ASSERT_TRUE(BKE_tex_paint_layer_rasterize(storage(), layer));
  EXPECT_EQ(layer.type, TEXPAINT_LAYER_PAINT);
  EXPECT_EQ(layer.uid, uid);
  EXPECT_EQ(layer.proj_mode, TEXPAINT_PROJ_NONE);
  EXPECT_EQ(layer.proj_image_name[0], '\0');
  EXPECT_EQ(layer.flag & TEXPAINT_LAYER_SHOW_WIDGET, 0);
  EXPECT_EQ(layer.color_filters.first(), levels);
  EXPECT_EQ(layer.color_filters.last(), blur);
  ASSERT_NE(layer.pbr_ibuf, nullptr);
  ASSERT_TRUE(BKE_tex_paint_composite_full(*node_));
  expect_channel_images_near(before, 2e-5f);
  levels->enabled = false;
  BKE_tex_paint_cache_invalidate(storage());
  ASSERT_TRUE(BKE_tex_paint_composite_full(*node_));
  EXPECT_NE(read_master_pixel(4, 4), before[0][4 * 8 + 4]);
}

TEST_F(TexPaintLayerTest, ProjectedFillRasterizePreservesExistingPbrSource)
{
  const float base[4] = {.1f, .2f, .3f, 1};
  BKE_tex_paint_canvas_init(*bmain_, *node_, "Raster PBR", 4, 4, true, base);
  TexPaintLayer &layer = *BKE_tex_paint_layer_add(storage(), "PBR Fill", TEXPAINT_LAYER_FILL);
  const float fill[4] = {.4f, 1.2f, .7f, .7f};
  std::copy_n(fill, 4, layer.fill_color);
  layer.channel_flags = TEXPAINT_CHAN_COLOR | TEXPAINT_CHAN_ROUGHNESS | TEXPAINT_CHAN_METALLIC |
                        TEXPAINT_CHAN_HEIGHT | TEXPAINT_CHAN_SPECULAR | TEXPAINT_CHAN_EMISSION;
  layer.proj_mode = TEXPAINT_PROJ_UV_FIT;
  ImBuf *texture = BKE_tex_paint_layer_ensure_ibuf(storage(), layer);
  ImBuf *pbr = BKE_tex_paint_layer_ensure_pbr_ibuf(storage(), layer);
  Array<float4> pixels(16), pbr_pixels(16);
  for (int i = 0; i < 16; i++) {
    const float alpha = .2f + .7f * float(i) / 15.0f;
    pixels[i] = float4(.7f * alpha, .3f * alpha, .2f * alpha, alpha);
    pbr_pixels[i] = float4(.8f * alpha, .6f * alpha, .4f * alpha, alpha);
  }
  BKE_tex_paint_pixels_write(*texture, pixels, {0, texture->x, 0, texture->y});
  BKE_tex_paint_pixels_write(*pbr, pbr_pixels, {0, pbr->x, 0, pbr->y});
  TexPaintColorFilter *invert = MEM_new<TexPaintColorFilter>(__func__);
  invert->type = TEXPAINT_COLOR_FILTER_INVERT;
  invert->strength = .3f;
  BLI_addtail(&layer.color_filters, invert);
  ASSERT_TRUE(BKE_tex_paint_composite_full(*node_));
  const auto before = read_channel_images();
  const Array<float4> pbr_before = BKE_tex_paint_pixels_read(*pbr);
  ASSERT_TRUE(BKE_tex_paint_layer_rasterize(storage(), layer));
  EXPECT_EQ(layer.pbr_ibuf, pbr);
  EXPECT_EQ(BKE_tex_paint_pixels_read(*pbr).as_span(), pbr_before.as_span());
  ASSERT_TRUE(BKE_tex_paint_composite_full(*node_));
  expect_channel_images_near(before, 1e-6f);
}

TEST_F(TexPaintLayerTest, RasterizeRejectsUnsupportedProjectionAndClippingWithoutMutation)
{
  const float base[4] = {0, 0, 0, 0};
  BKE_tex_paint_canvas_init(*bmain_, *node_, "Raster Reject", 4, 4, true, base);
  TexPaintLayer &layer = *BKE_tex_paint_layer_add(storage(), "Pending Fill", TEXPAINT_LAYER_FILL);
  layer.proj_mode = TEXPAINT_PROJ_UV_REPEAT;
  EXPECT_FALSE(BKE_tex_paint_layer_rasterize(storage(), layer, true));
  EXPECT_FALSE(BKE_tex_paint_layer_rasterize(storage(), layer));
  EXPECT_EQ(layer.type, TEXPAINT_LAYER_FILL);
  EXPECT_EQ(layer.ibuf, nullptr);
  ImBuf *source = BKE_tex_paint_layer_ensure_ibuf(storage(), layer);
  Array<float4> pixels(16, float4(.2f, .3f, .4f, 1));
  BKE_tex_paint_pixels_write(*source, pixels, {0, source->x, 0, source->y});
  layer.fill_color[3] = .5f;
  TexPaintLayer *clipped = BKE_tex_paint_layer_add(storage(), "Clipped", TEXPAINT_LAYER_PAINT);
  clipped->flag |= TEXPAINT_LAYER_CLIPPING;
  const int uid = layer.uid;
  EXPECT_FALSE(BKE_tex_paint_layer_rasterize(storage(), layer, true));
  EXPECT_FALSE(BKE_tex_paint_layer_rasterize(storage(), layer));
  EXPECT_EQ(layer.type, TEXPAINT_LAYER_FILL);
  EXPECT_EQ(layer.ibuf, source);
  EXPECT_EQ(layer.uid, uid);
  EXPECT_EQ(layer.proj_mode, TEXPAINT_PROJ_UV_REPEAT);
  EXPECT_EQ(BKE_tex_paint_pixels_read(*source).as_span(), pixels.as_span());
}

TEST_F(TexPaintLayerTest, Standalone2DCanvasesIsolateMaskStrokeUpdates)
{
  bNodeTree *tree_2d = bke::node_tree_add_tree(bmain_, ".TexPaint_2D_Layers", "ShaderNodeTree");
  ASSERT_NE(tree_2d, nullptr);
  EXPECT_FALSE(BKE_tex_paint_tree_shares_channel_layers(tree_2d));
  EXPECT_TRUE(BKE_tex_paint_tree_shares_channel_layers(ntree_));

  bNode *canvas_a = bke::node_add_static_node(nullptr, *tree_2d, SH_NODE_TEX_PAINT);
  bNode *canvas_b = bke::node_add_static_node(nullptr, *tree_2d, SH_NODE_TEX_PAINT);
  ASSERT_NE(canvas_a, nullptr);
  ASSERT_NE(canvas_b, nullptr);
  NodeTexPaint &data_a = *static_cast<NodeTexPaint *>(canvas_a->storage);
  NodeTexPaint &data_b = *static_cast<NodeTexPaint *>(canvas_b->storage);

  const float base_color[4] = {0.1f, 0.2f, 0.3f, 1.0f};
  ASSERT_NE(BKE_tex_paint_canvas_init(*bmain_, *canvas_a, "Canvas2DA", 8, 8, false, base_color),
            nullptr);
  ASSERT_NE(BKE_tex_paint_canvas_init(*bmain_, *canvas_b, "Canvas2DB", 8, 8, false, base_color),
            nullptr);

  TexPaintLayer *layer_a = BKE_tex_paint_layer_add(data_a, "Top", TEXPAINT_LAYER_FILL);
  TexPaintLayer *layer_b = BKE_tex_paint_layer_add(data_b, "Top", TEXPAINT_LAYER_FILL);
  ASSERT_NE(layer_a, nullptr);
  ASSERT_NE(layer_b, nullptr);
  EXPECT_EQ(layer_a->uid, layer_b->uid);

  for (TexPaintLayer *layer : {layer_a, layer_b}) {
    layer->fill_color[0] = 1.0f;
    layer->fill_color[1] = 0.0f;
    layer->fill_color[2] = 0.0f;
    layer->fill_color[3] = 1.0f;
    layer->flag |= TEXPAINT_LAYER_USE_MASK;
    layer->mask_base_value = 0;
    layer->mask_effects_active = 1;
    layer->active_mask_effect_uid = 10;
  }
  TexPaintMaskEffect &paint_a = BKE_tex_paint_mask_effect_ensure(
      *layer_a, 10, TEXPAINT_MASK_EFFECT_PAINT);
  TexPaintMaskEffect &paint_b = BKE_tex_paint_mask_effect_ensure(
      *layer_b, 10, TEXPAINT_MASK_EFFECT_PAINT);
  ImBuf *paint_ibuf_a = BKE_tex_paint_mask_effect_ensure_pixels(data_a, paint_a);
  ImBuf *paint_ibuf_b = BKE_tex_paint_mask_effect_ensure_pixels(data_b, paint_b);
  ASSERT_NE(paint_ibuf_a, nullptr);
  ASSERT_NE(paint_ibuf_b, nullptr);

  rcti eval_rect;
  ASSERT_TRUE(BKE_tex_paint_mask_evaluate(data_a, *layer_a, nullptr, eval_rect));
  ASSERT_TRUE(BKE_tex_paint_mask_evaluate(data_b, *layer_b, nullptr, eval_rect));
  ASSERT_TRUE(BKE_tex_paint_composite_full(*canvas_a));
  ASSERT_TRUE(BKE_tex_paint_composite_full(*canvas_b));

  const Array<float4> mask_b_before = BKE_tex_paint_pixels_read(*layer_b->mask_ibuf);
  const Array<float4> paint_b_before = BKE_tex_paint_pixels_read(*paint_ibuf_b);

  data_a.paint_target = TEXPAINT_TARGET_MASK;
  uint8_t *px_a = paint_ibuf_a->byte_data_for_write() + (2 * 8 + 2) * 4;
  px_a[0] = px_a[1] = px_a[2] = px_a[3] = 255;
  const rcti stroke_rect = {2, 3, 2, 3};
  BKE_tex_paint_stroke_update(*canvas_a, paint_ibuf_a, stroke_rect);

  EXPECT_EQ(layer_a->mask_ibuf->byte_data()[(2 * 8 + 2) * 4], 255);
  EXPECT_EQ(BKE_tex_paint_pixels_read(*layer_b->mask_ibuf).as_span(), mask_b_before.as_span());
  EXPECT_EQ(BKE_tex_paint_pixels_read(*paint_ibuf_b).as_span(), paint_b_before.as_span());

  BKE_tex_paint_layer_mask_fill(data_a, *layer_a, 1.0f);
  BKE_tex_paint_layer_mask_fill(data_b, *layer_b, 1.0f);
  const Array<float4> plain_b_before = BKE_tex_paint_pixels_read(*layer_b->mask_ibuf);

  uint8_t *plain_a = layer_a->mask_ibuf->byte_data_for_write() + (3 * 8 + 3) * 4;
  plain_a[0] = plain_a[1] = plain_a[2] = 0;
  plain_a[3] = 255;
  const rcti plain_rect = {3, 4, 3, 4};
  BKE_tex_paint_stroke_update(*canvas_a, layer_a->mask_ibuf, plain_rect);

  EXPECT_EQ(layer_a->mask_ibuf->byte_data()[(3 * 8 + 3) * 4], 0);
  EXPECT_EQ(BKE_tex_paint_pixels_read(*layer_b->mask_ibuf).as_span(), plain_b_before.as_span());

}

}  // namespace blender::bke::tests
