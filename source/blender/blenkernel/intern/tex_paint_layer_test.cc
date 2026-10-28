/* SPDX-FileCopyrightText: 2026 Blender Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

#include "testing/testing.h"

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
};

TEST_F(TexPaintLayerTest, CanvasInitCreatesSingleMasterImageAndBaseLayer)
{
  const float base_color[4] = {0.2f, 0.4f, 0.8f, 1.0f};
  Image *master = BKE_tex_paint_canvas_init(
      *bmain_, *node_, "LayeredCanvas", 16, 16, true, base_color);
  ASSERT_NE(master, nullptr);
  EXPECT_EQ(node_->id, &master->id);
  EXPECT_TRUE(BKE_tex_paint_is_initialized(*node_));

  /* Only a single Image datablock exists in Main; layers are private to NodeTexPaint. */
  EXPECT_EQ(BLI_listbase_count(&bmain_->images), 1);
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
  EXPECT_EQ(BLI_listbase_count(&bmain_->images), 1);

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

}  // namespace blender::bke::tests
