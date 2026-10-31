/* SPDX-FileCopyrightText: 2026 Blender Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

#include "testing/testing.h"

#include <cstring>
#include <iterator>

#include "MEM_guardedalloc.h"

#include "BLI_listbase.hh"

#include "DNA_image_types.h"
#include "DNA_node_tex_paint_types.h"
#include "DNA_node_types.h"

#include "BKE_global.hh"
#include "BKE_gtest_base.hh"
#include "BKE_image.hh"
#include "BKE_main.hh"
#include "BKE_node.hh"
#include "BKE_node_legacy_types.hh"
#include "BKE_tex_paint_layer.hh"

#include "IMB_imbuf.hh"
#include "IMB_imbuf_types.hh"

namespace blender::bke::tests {

class TexPaintFlattenTest : public BlenderGTestBase {
 protected:
  Main *bmain_ = nullptr;
  bNode *node_ = nullptr;

  void SetUp() override
  {
    BlenderGTestBase::SetUp();
    bmain_ = BKE_main_new();
    G_MAIN = bmain_;
    bNodeTree *tree = bke::node_tree_add_tree(bmain_, "FlattenTree", "ShaderNodeTree");
    node_ = bke::node_add_static_node(nullptr, *tree, SH_NODE_TEX_PAINT);
    const float base[4] = {2.0f, 0.25f, 0.125f, 0.5f};
    ASSERT_NE(BKE_tex_paint_canvas_init(*bmain_, *node_, "Flatten", 16, 8, true, base), nullptr);
  }

  void TearDown() override
  {
    BKE_main_free(bmain_);
    G_MAIN = nullptr;
    BlenderGTestBase::TearDown();
  }

  NodeTexPaint &data()
  {
    return *static_cast<NodeTexPaint *>(node_->storage);
  }

  Array<float4> pixels(Image *image, ImageUser *user)
  {
    ImBuf *buffer = BKE_image_acquire_ibuf(image, user, nullptr);
    EXPECT_NE(buffer, nullptr);
    Array<float4> result = BKE_tex_paint_pixels_read(*buffer);
    BKE_image_release_ibuf(image, buffer, nullptr);
    return result;
  }

  Array<float4> color_pixels()
  {
    return pixels(reinterpret_cast<Image *>(node_->id), &data().image.iuser);
  }

  void expect_pixels_near(const Span<float4> before, const Span<float4> after)
  {
    ASSERT_EQ(before.size(), after.size());
    for (const int64_t i : before.index_range()) {
      for (int channel = 0; channel < 4; channel++) {
        EXPECT_NEAR(before[i][channel], after[i][channel], 2e-6f);
      }
    }
  }

  TexPaintLayer &fill()
  {
    TexPaintLayer &layer = *BKE_tex_paint_layer_add(data(), "Top", TEXPAINT_LAYER_FILL);
    layer.channel_flags = TEXPAINT_CHAN_COLOR;
    layer.fill_color[0] = 0.8f;
    layer.fill_color[1] = 0.2f;
    layer.fill_color[2] = 0.3f;
    layer.fill_color[3] = 0.7f;
    return layer;
  }
};

TEST_F(TexPaintFlattenTest, DiscardPreparedResultLeavesSourceOwnershipAndPixelsUntouched)
{
  TexPaintLayer &top = fill();
  const int uids[] = {data().layers.first()->uid, top.uid};
  BKE_tex_paint_composite_full(*node_);
  ImBuf *source = data().layers.first()->ibuf;
  const uint64_t hash = BKE_tex_paint_pixels_hash(*source);
  const char *error = nullptr;
  {
    std::unique_ptr<TexPaintFlattenResult> prepared = BKE_tex_paint_layers_flatten_prepare(
        data(), Span<int>(uids, std::size(uids)), &error);
    ASSERT_NE(prepared, nullptr) << (error ? error : "");
    ASSERT_NE(prepared->layer, nullptr);
    EXPECT_NE(prepared->layer->ibuf, source);
    EXPECT_EQ(data().layers.count(), 2);
    EXPECT_EQ(data().layers.first()->ibuf, source);
  }
  EXPECT_EQ(data().layers.count(), 2);
  EXPECT_EQ(BKE_tex_paint_pixels_hash(*source), hash);
}

TEST_F(TexPaintFlattenTest, FloatHdrFlattenBakesFiltersAndMasksOnce)
{
  TexPaintLayer &top = fill();
  BKE_tex_paint_layer_mask_fill(data(), top, 0.5f);
  TexPaintColorFilter *filter = MEM_new<TexPaintColorFilter>(__func__);
  filter->type = TEXPAINT_COLOR_FILTER_INVERT;
  filter->strength = 0.6f;
  BLI_addtail(&top.color_filters, filter);
  const int uids[] = {data().layers.first()->uid, top.uid};
  BKE_tex_paint_composite_full(*node_);
  const Array<float4> before = color_pixels();
  const char *error = nullptr;
  std::unique_ptr<TexPaintFlattenResult> prepared = BKE_tex_paint_layers_flatten_prepare(
      data(), Span<int>(uids, std::size(uids)), &error);
  ASSERT_NE(prepared, nullptr) << (error ? error : "");
  EXPECT_TRUE(prepared->layer->color_filters.is_empty());
  EXPECT_TRUE(prepared->layer->mask_effects.is_empty());
  EXPECT_EQ(prepared->layer->flag & TEXPAINT_LAYER_USE_MASK, 0);
  EXPECT_EQ(prepared->layer->proj_mode, 0);
  TexPaintLayer *flattened = BKE_tex_paint_layers_flatten_commit(
      data(), *prepared, 3, "Flattened");
  EXPECT_EQ(prepared->layer, nullptr);
  ASSERT_EQ(data().layers.count(), 1);
  EXPECT_EQ(data().layers.first(), flattened);
  EXPECT_EQ(flattened->uid, 3);
  BKE_tex_paint_composite_full(*node_);
  expect_pixels_near(before, color_pixels());
  const Array<float4> raw = BKE_tex_paint_pixels_read(*flattened->ibuf);
  EXPECT_GT(raw[0].x, raw[0].w);
}

TEST_F(TexPaintFlattenTest, VisibleMergePreservesHiddenLayersOrderAndPixels)
{
  TexPaintLayer *base = data().layers.first();
  TexPaintLayer &hidden_a = fill();
  hidden_a.flag &= ~TEXPAINT_LAYER_VISIBLE;
  hidden_a.flag |= TEXPAINT_LAYER_LOCKED;
  TexPaintLayer &middle = fill();
  TexPaintLayer &hidden_b = fill();
  hidden_b.flag &= ~TEXPAINT_LAYER_VISIBLE;
  TexPaintLayer &top = fill();
  const int uids[] = {base->uid, middle.uid, top.uid};
  BKE_tex_paint_composite_full(*node_);
  const Array<float4> before = color_pixels();
  const float hidden_color = hidden_a.fill_color[0];
  const char *error = nullptr;
  std::unique_ptr<TexPaintFlattenResult> prepared = BKE_tex_paint_layers_flatten_prepare(
      data(), Span<int>(uids, std::size(uids)), &error);
  ASSERT_NE(prepared, nullptr) << (error ? error : "");
  BKE_tex_paint_layers_flatten_commit(data(), *prepared, 6, "Visible");
  ASSERT_EQ(data().layers.count(), 3);
  EXPECT_EQ(data().layers.first()->uid, 6);
  EXPECT_EQ(data().layers.first()->next, &hidden_a);
  EXPECT_EQ(hidden_a.next, &hidden_b);
  EXPECT_EQ(hidden_a.flag & TEXPAINT_LAYER_VISIBLE, 0);
  EXPECT_NE(hidden_a.flag & TEXPAINT_LAYER_LOCKED, 0);
  EXPECT_EQ(hidden_a.fill_color[0], hidden_color);
  BKE_tex_paint_composite_full(*node_);
  expect_pixels_near(before, color_pixels());
}

TEST_F(TexPaintFlattenTest, MaterialChannelsAndUniformSpecularSurviveFlatten)
{
  TexPaintLayer &top = fill();
  top.channel_flags |= TEXPAINT_CHAN_ROUGHNESS | TEXPAINT_CHAN_METALLIC | TEXPAINT_CHAN_HEIGHT |
                       TEXPAINT_CHAN_SPECULAR;
  top.fill_roughness = 0.1f;
  top.fill_metallic = 0.7f;
  top.fill_height = 0.4f;
  top.fill_specular = 0.8f;
  top.fill_color[3] = 0.5f;
  const int uids[] = {data().layers.first()->uid, top.uid};
  BKE_tex_paint_composite_full(*node_);
  const Array<float4> before_color = color_pixels();
  const Array<float4> before_pbr = pixels(data().pbr_image, &data().pbr_iuser);
  const char *error = nullptr;
  std::unique_ptr<TexPaintFlattenResult> prepared = BKE_tex_paint_layers_flatten_prepare(
      data(), Span<int>(uids, std::size(uids)), &error);
  ASSERT_NE(prepared, nullptr) << (error ? error : "");
  EXPECT_NE(prepared->layer->pbr_ibuf, nullptr);
  EXPECT_NEAR(prepared->layer->fill_specular, 0.65f, 1e-6f);
  BKE_tex_paint_layers_flatten_commit(data(), *prepared, 3, "Material");
  BKE_tex_paint_composite_full(*node_);
  expect_pixels_near(before_color, color_pixels());
  expect_pixels_near(before_pbr, pixels(data().pbr_image, &data().pbr_iuser));
}

TEST_F(TexPaintFlattenTest, UnsupportedMaterialChannelsRejectWithoutRemovingLayers)
{
  TexPaintLayer &top = *BKE_tex_paint_layer_add(data(), "Spatial", TEXPAINT_LAYER_PAINT);
  top.channel_flags = TEXPAINT_CHAN_COLOR | TEXPAINT_CHAN_SPECULAR;
  top.fill_specular = 1.0f;
  ImBuf *buffer = BKE_tex_paint_layer_ensure_ibuf(data(), top);
  float *rgba = buffer->float_data_for_write();
  for (int y = 0; y < data().height; y++) {
    for (int x = 0; x < data().width; x++) {
      const int index = (y * data().width + x) * 4;
      rgba[index] = rgba[index + 1] = rgba[index + 2] = rgba[index + 3] = x < 8 ? 1.0f : 0.0f;
    }
  }
  const int uids[] = {data().layers.first()->uid, top.uid};
  const uint64_t hash = BKE_tex_paint_pixels_hash(*buffer);
  const char *error = nullptr;
  EXPECT_EQ(BKE_tex_paint_layers_flatten_prepare(data(), Span<int>(uids, std::size(uids)), &error),
            nullptr);
  ASSERT_NE(error, nullptr);
  EXPECT_NE(std::strstr(error, "Specular"), nullptr);
  EXPECT_EQ(data().layers.count(), 2);
  EXPECT_EQ(BKE_tex_paint_pixels_hash(*buffer), hash);
  top.channel_flags = TEXPAINT_CHAN_COLOR | TEXPAINT_CHAN_EMISSION;
  EXPECT_EQ(BKE_tex_paint_layers_flatten_prepare(data(), Span<int>(uids, std::size(uids)), &error),
            nullptr);
  ASSERT_NE(error, nullptr);
  EXPECT_NE(std::strstr(error, "Emission"), nullptr);
  EXPECT_EQ(data().layers.count(), 2);
  EXPECT_EQ(BKE_tex_paint_pixels_hash(*buffer), hash);
}

TEST_F(TexPaintFlattenTest, LockedAndPartialVisibleSelectionsRejectBeforeSnapshots)
{
  TexPaintLayer &top = fill();
  const int uids[] = {data().layers.first()->uid, top.uid};
  const char *error = nullptr;
  top.flag |= TEXPAINT_LAYER_LOCKED;
  EXPECT_EQ(BKE_tex_paint_layers_flatten_prepare(data(), Span<int>(uids, std::size(uids)), &error),
            nullptr);
  ASSERT_NE(error, nullptr);
  EXPECT_NE(std::strstr(error, "Unlock"), nullptr);
  top.flag &= ~TEXPAINT_LAYER_LOCKED;
  EXPECT_EQ(BKE_tex_paint_layers_flatten_prepare(data(), Span<int>(uids, 1), &error), nullptr);
  ASSERT_NE(error, nullptr);
  EXPECT_NE(std::strstr(error, "Every visible"), nullptr);
  EXPECT_EQ(data().layers.count(), 2);
}

TEST_F(TexPaintFlattenTest, MergeDownPreservesLowerIdentityAndIgnoresMaskPreview)
{
  TexPaintLayer *base = data().layers.first();
  const int base_uid = base->uid;
  TexPaintLayer &top = fill();
  top.channel_flags |= TEXPAINT_CHAN_ROUGHNESS | TEXPAINT_CHAN_METALLIC | TEXPAINT_CHAN_HEIGHT |
                       TEXPAINT_CHAN_SPECULAR;
  top.fill_roughness = 0.15f;
  top.fill_metallic = 0.8f;
  top.fill_specular = 0.75f;
  BKE_tex_paint_layer_mask_fill(data(), top, 0.3f);
  BKE_tex_paint_composite_full(*node_);
  const Array<float4> before_color = color_pixels();
  const Array<float4> before_pbr = pixels(data().pbr_image, &data().pbr_iuser);
  top.flag |= TEXPAINT_LAYER_PREVIEW_MASK;
  BKE_tex_paint_composite_full(*node_);
  ASSERT_TRUE(BKE_tex_paint_layer_merge_down(data()));
  ASSERT_EQ(data().layers.count(), 1);
  EXPECT_EQ(data().layers.first(), base);
  EXPECT_EQ(base->uid, base_uid);
  EXPECT_EQ(base->flag & TEXPAINT_LAYER_PREVIEW_MASK, 0);
  BKE_tex_paint_composite_full(*node_);
  expect_pixels_near(before_color, color_pixels());
  expect_pixels_near(before_pbr, pixels(data().pbr_image, &data().pbr_iuser));
}

TEST_F(TexPaintFlattenTest, MergeDownRejectsLockedHiddenAndBackdropDependentPairs)
{
  TexPaintLayer *base = data().layers.first();
  TexPaintLayer &middle = *BKE_tex_paint_layer_add(data(), "Middle", TEXPAINT_LAYER_PAINT);
  TexPaintLayer &top = fill();
  const char *error = nullptr;
  top.flag |= TEXPAINT_LAYER_LOCKED;
  EXPECT_EQ(BKE_tex_paint_layer_merge_down_prepare(data(), &error), nullptr);
  EXPECT_NE(std::strstr(error, "Unlock"), nullptr);
  top.flag &= ~TEXPAINT_LAYER_LOCKED;
  top.flag &= ~TEXPAINT_LAYER_VISIBLE;
  EXPECT_EQ(BKE_tex_paint_layer_merge_down_prepare(data(), &error), nullptr);
  EXPECT_NE(std::strstr(error, "Show"), nullptr);
  top.flag |= TEXPAINT_LAYER_VISIBLE;
  top.channel_flags |= TEXPAINT_CHAN_ROUGHNESS;
  EXPECT_EQ(BKE_tex_paint_layer_merge_down_prepare(data(), &error), nullptr);
  EXPECT_NE(std::strstr(error, "material channel"), nullptr);
  top.channel_flags = TEXPAINT_CHAN_COLOR;
  middle.blend_mode = IMB_BLEND_MUL;
  EXPECT_EQ(BKE_tex_paint_layer_merge_down_prepare(data(), &error), nullptr);
  EXPECT_NE(std::strstr(error, "blend interactions"), nullptr);
  EXPECT_EQ(data().layers.count(), 3);
  EXPECT_EQ(data().layers.first(), base);
}

TEST_F(TexPaintFlattenTest, MergeDownRejectsClippedFollowersWhoseCoverageWouldChange)
{
  TexPaintLayer &middle = *BKE_tex_paint_layer_add(data(), "Middle", TEXPAINT_LAYER_PAINT);
  TexPaintLayer &follower = fill();
  follower.flag |= TEXPAINT_LAYER_CLIPPING;
  BKE_tex_paint_layer_active_set(data(), 1);
  const char *error = nullptr;
  EXPECT_EQ(BKE_tex_paint_layer_merge_down_prepare(data(), &error), nullptr);
  ASSERT_NE(error, nullptr);
  EXPECT_NE(std::strstr(error, "clipping coverage"), nullptr);
  EXPECT_EQ(data().layers.count(), 3);
  EXPECT_EQ(data().layers.first()->next, &middle);
}

}  // namespace blender::bke::tests
