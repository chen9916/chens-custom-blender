/* SPDX-FileCopyrightText: 2026 Blender Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

/** \file
 * \ingroup shdnodes
 *
 * Layered "Texture Paint" node. The layer stack is composited on the CPU into the master image
 * referenced by `bNode::id`. Shading samples that image exactly like the Image Texture node, so
 * painting and layer edits never require a shader recompilation.
 */

#include <fmt/format.h>

#include "node_shader_util.hh"
#include "node_util.hh"

#include "DNA_node_tex_paint_types.h"

#include "BKE_image.hh"
#include "BKE_node_runtime.hh"
#include "BKE_tex_paint_layer.hh"
#include "BKE_texture.h"

#include "BLO_read_write.hh"

#include "UI_interface_layout.hh"
#include "UI_resources.hh"

namespace blender {

namespace nodes::node_shader_tex_paint_cc {

NODE_STORAGE_FUNCS(NodeTexPaint)

static void node_declare(NodeDeclarationBuilder &b)
{
  b.is_function_node();
  b.add_input<decl::Vector>("Vector"_ustr).default_input_type(NODE_DEFAULT_INPUT_POSITION_FIELD);
  b.add_output<decl::Color>("Color"_ustr).no_muted_links();
  b.add_output<decl::Float>("Alpha"_ustr).no_muted_links();
}

static void node_init(bNodeTree * /*ntree*/, bNode *node)
{
  NodeTexPaint *data = MEM_new<NodeTexPaint>(__func__);
  BKE_texture_mapping_default(&data->image.base.tex_mapping, TEXMAP_TYPE_POINT);
  BKE_texture_colormapping_default(&data->image.base.color_mapping);
  BKE_imageuser_default(&data->image.iuser);
  node->storage = data;
}

static void node_free_storage(bNode *node)
{
  NodeTexPaint *data = static_cast<NodeTexPaint *>(node->storage);
  if (data == nullptr) {
    return;
  }
  BKE_tex_paint_storage_free_contents(*data);
  MEM_delete(data);
  node->storage = nullptr;
}

static void node_copy_storage(bNodeTree *dest_ntree, bNode *dest_node, const bNode *src_node)
{
  const NodeTexPaint &src = node_storage(*src_node);
  NodeTexPaint *dst = MEM_new<NodeTexPaint>(__func__, dna::shallow_copy(src));
  /* Evaluated and localized trees only sample the shared master image, avoid copying layer
   * pixels for them. Copies without a tree (clipboard) keep the pixels. */
  const bool is_runtime_copy = dest_ntree != nullptr &&
                               (dest_ntree->id.tag & (ID_TAG_COPIED_ON_EVAL | ID_TAG_LOCALIZED));
  BKE_tex_paint_storage_copy_contents(*dst, src, !is_runtime_copy);
  dest_node->storage = dst;
}

static void node_blend_write(const bNodeTree & /*tree*/, const bNode &node, BlendWriter &writer)
{
  BKE_tex_paint_blend_write(writer, node_storage(node));
}

static void node_blend_read(bNodeTree & /*tree*/, bNode &node, BlendDataReader &reader)
{
  BKE_tex_paint_blend_read_data(reader, node_storage(node));
}

static void node_draw_buttons(ui::Layout &layout, bContext * /*C*/, PointerRNA *ptr)
{
  bNode &node = *static_cast<bNode *>(ptr->data);
  NodeTexPaint &data = node_storage(node);

  if (!BKE_tex_paint_is_initialized(node)) {
    layout.op("NODE_OT_tex_paint_canvas_new", IFACE_("New Layered Canvas"), ICON_ADD);
    return;
  }

  /* Recover the composite if the image system re-created the master buffer. */
  BKE_tex_paint_ensure_composited(node);

  ui::Layout &col = layout.column(true);
  const int layers_num = BKE_tex_paint_layer_count(data);
  col.label(fmt::format(fmt::runtime(RPT_("{} x {} {}, {} layers")),
                        data.width,
                        data.height,
                        data.is_float ? RPT_("Float") : RPT_("Byte"),
                        layers_num),
            ICON_IMAGE_RGB_ALPHA);
  if (const TexPaintLayer *active = BKE_tex_paint_layer_active(data)) {
    const bool paint_mask = data.paint_target == TEXPAINT_TARGET_MASK;
    col.label(active->name, paint_mask ? ICON_MOD_MASK : ICON_BRUSH_DATA);
  }

  layout.prop(ptr, "interpolation", UI_ITEM_NONE, "", ICON_NONE);
  layout.prop(ptr, "projection", UI_ITEM_NONE, "", ICON_NONE);
  if (data.image.projection == SHD_PROJ_BOX) {
    layout.prop(ptr, "projection_blend", UI_ITEM_NONE, IFACE_("Blend"), ICON_NONE);
  }
  layout.prop(ptr, "extension", UI_ITEM_NONE, "", ICON_NONE);
}

static int node_gpu(GPUMaterial *mat,
                    bNode *node,
                    bNodeExecData *execdata,
                    GPUNodeStack *in,
                    GPUNodeStack *out)
{
  /* #NodeTexPaint starts with a #NodeTexImage, reuse the Image Texture node evaluation. */
  static const bke::bNodeType *image_type = bke::node_type_find("ShaderNodeTexImage"_ustr);
  if (image_type == nullptr || image_type->gpu_fn == nullptr) {
    return GPU_stack_link(mat, node, "node_tex_image_empty", in, out);
  }
  return image_type->gpu_fn(mat, node, execdata, in, out);
}

}  // namespace nodes::node_shader_tex_paint_cc

void register_node_type_sh_tex_paint()
{
  namespace file_ns = nodes::node_shader_tex_paint_cc;

  static bke::bNodeType ntype;

  sh_node_type_base(&ntype, "ShaderNodeTexPaint"_ustr, SH_NODE_TEX_PAINT);
  ntype.ui_name = "Texture Paint";
  ntype.ui_description =
      "Paint a stack of layers that is composited into an image and sampled as a texture";
  ntype.enum_name_legacy = "TEX_PAINT";
  ntype.nclass = NODE_CLASS_TEXTURE;
  ntype.declare = file_ns::node_declare;
  ntype.initfunc = file_ns::node_init;
  bke::node_type_storage(
      ntype, "NodeTexPaint", file_ns::node_free_storage, file_ns::node_copy_storage);
  ntype.blend_write_storage_content = file_ns::node_blend_write;
  ntype.blend_data_read_storage_content = file_ns::node_blend_read;
  ntype.gpu_fn = file_ns::node_gpu;
  ntype.labelfunc = node_image_label;
  ntype.draw_buttons = file_ns::node_draw_buttons;
  ntype.default_width = bke::NodeWidth::_240;

  bke::node_register_type(ntype);
}

}  // namespace blender
