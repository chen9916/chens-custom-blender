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

#include "IMB_colormanagement.hh"

#include "BLO_read_write.hh"

#include "RNA_access.hh"

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
  b.add_output<decl::Float>("Roughness"_ustr).no_muted_links();
  b.add_output<decl::Float>("Metallic"_ustr).no_muted_links();
  b.add_output<decl::Float>("Height"_ustr).no_muted_links();
  b.add_output<decl::Vector>("Normal"_ustr).no_muted_links();
  b.add_output<decl::Color>("Emission"_ustr).no_muted_links();
}

static void node_init(bNodeTree * /*ntree*/, bNode *node)
{
  NodeTexPaint *data = MEM_new<NodeTexPaint>(__func__);
  BKE_texture_mapping_default(&data->image.base.tex_mapping, TEXMAP_TYPE_POINT);
  BKE_texture_colormapping_default(&data->image.base.color_mapping);
  BKE_imageuser_default(&data->image.iuser);
  BKE_imageuser_default(&data->pbr_iuser);
  BKE_imageuser_default(&data->emission_iuser);
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
    if (active->flag & TEXPAINT_LAYER_USE_MASK) {
      ui::Layout &tgt_row = col.row(true);
      tgt_row.prop(ptr, "paint_target", ui::ITEM_R_EXPAND, "", ICON_NONE);
    }
  }
  ui::Layout &act_row = col.row(true);
  PointerRNA op_paint = act_row.op("NODE_OT_tex_paint_layer_add", IFACE_("+ Paint"), ICON_BRUSH_DATA);
  RNA_enum_set(&op_paint, "type", TEXPAINT_LAYER_PAINT);
  PointerRNA op_fill = act_row.op("NODE_OT_tex_paint_layer_add", IFACE_("+ Fill"), ICON_COLOR);
  RNA_enum_set(&op_fill, "type", TEXPAINT_LAYER_FILL);

  layout.prop(ptr, "interpolation", UI_ITEM_NONE, "", ICON_NONE);
  layout.prop(ptr, "projection", UI_ITEM_NONE, "", ICON_NONE);
  if (data.image.projection == SHD_PROJ_BOX) {
    layout.prop(ptr, "projection_blend", UI_ITEM_NONE, IFACE_("Blend"), ICON_NONE);
  }
  layout.prop(ptr, "extension", UI_ITEM_NONE, "", ICON_NONE);
}

static int node_gpu(GPUMaterial *mat,
                    bNode *node,
                    bNodeExecData * /*execdata*/,
                    GPUNodeStack *in,
                    GPUNodeStack *out)
{
  Image *ima = id_cast<Image *>(node->id);
  NodeTexPaint &data = node_storage(*node);
  NodeTexImage *tex = &data.image;

  bNode *node_original = node->runtime->original ? node->runtime->original : node;
  NodeTexPaint &data_orig = node_storage(*node_original);
  ImageUser *iuser = &data_orig.image.iuser;

  GPUNodeLink **texco = &in[0].link;
  if (!*texco) {
    *texco = GPU_attribute(mat, CD_AUTO_FROM_NAME, "");
    node_shader_gpu_bump_tex_coord(mat, node, texco);
  }

  node_shader_gpu_tex_mapping(mat, node, in, out);

  GPUSamplerState sampler_state = GPUSamplerState::default_sampler();
  switch (tex->extension) {
    case SHD_IMAGE_EXTENSION_EXTEND:
      sampler_state.extend_x = GPU_SAMPLER_EXTEND_MODE_EXTEND;
      sampler_state.extend_yz = GPU_SAMPLER_EXTEND_MODE_EXTEND;
      break;
    case SHD_IMAGE_EXTENSION_REPEAT:
      sampler_state.extend_x = GPU_SAMPLER_EXTEND_MODE_REPEAT;
      sampler_state.extend_yz = GPU_SAMPLER_EXTEND_MODE_REPEAT;
      break;
    case SHD_IMAGE_EXTENSION_CLIP:
      sampler_state.extend_x = GPU_SAMPLER_EXTEND_MODE_CLAMP_TO_BORDER;
      sampler_state.extend_yz = GPU_SAMPLER_EXTEND_MODE_CLAMP_TO_BORDER;
      break;
    case SHD_IMAGE_EXTENSION_MIRROR:
      sampler_state.extend_x = GPU_SAMPLER_EXTEND_MODE_MIRRORED_REPEAT;
      sampler_state.extend_yz = GPU_SAMPLER_EXTEND_MODE_MIRRORED_REPEAT;
      break;
    default:
      break;
  }
  if (tex->interpolation != SHD_INTERP_CLOSEST) {
    sampler_state.filtering = GPU_SAMPLER_FILTERING_ANISOTROPIC_ENABLE |
                              GPU_SAMPLER_FILTERING_LINEAR | GPU_SAMPLER_FILTERING_MIPMAP;
  }
  const bool use_cubic = ELEM(tex->interpolation, SHD_INTERP_CUBIC, SHD_INTERP_SMART);

  auto sample_image = [&](Image *img,
                          ImageUser *img_user,
                          GPUNodeLink **r_col,
                          GPUNodeLink **r_alpha) {
    if (img == nullptr) {
      static const float zero4[4] = {0.0f, 0.0f, 0.0f, 0.0f};
      static const float zero1 = 0.0f;
      GPU_link(mat, "set_rgba", GPU_constant(zero4), r_col);
      GPU_link(mat, "set_value", GPU_constant(&zero1), r_alpha);
      return;
    }
    const char *gpu_node_name = use_cubic ? "node_tex_image_cubic" : "node_tex_image_linear";
    if (tex->projection == SHD_PROJ_BOX) {
      gpu_node_name = use_cubic ? "tex_box_sample_cubic" : "tex_box_sample_linear";
      GPUNodeLink *vnor, *wnor, *col1, *col2, *col3;
      GPUNodeLink *blend = GPU_uniform(&tex->projection_blend);
      GPUNodeLink *gpu_image = GPU_image(mat, img, img_user, sampler_state);
      GPU_link(mat, "world_normals_get", GPU_shading_data(), &vnor);
      GPU_link(mat,
               "normal_transform_world_to_object",
               vnor,
               GPU_kernel_globals(),
               GPU_shading_data(),
               &wnor);
      GPU_link(mat,
               gpu_node_name,
               in[0].link,
               wnor,
               gpu_image,
               GPU_kernel_globals(),
               &col1,
               &col2,
               &col3);
      GPU_link(mat, "tex_box_blend", wnor, col1, col2, col3, blend, r_col, r_alpha);
    }
    else {
      GPUNodeLink *gpu_image = GPU_image(mat, img, img_user, sampler_state);
      GPU_link(mat, gpu_node_name, in[0].link, gpu_image, GPU_kernel_globals(), r_col, r_alpha);
    }
  };

  /* 1. Base Color & Alpha (outputs 0 & 1) */
  sample_image(ima, iuser, &out[0].link, &out[1].link);
  if (ima && out[0].hasoutput && out[0].link) {
    if (ELEM(ima->alpha_mode, IMA_ALPHA_IGNORE, IMA_ALPHA_CHANNEL_PACKED) ||
        IMB_colormanagement_space_name_is_data(ima->colorspace_settings.name))
    {
      GPU_link(mat, "color_alpha_clear", out[0].link, &out[0].link);
    }
    else if (ima->alpha_mode == IMA_ALPHA_PREMUL) {
      GPU_link(mat,
               out[1].hasoutput ? "color_alpha_unpremultiply" : "color_alpha_clear",
               out[0].link,
               &out[0].link);
    }
    else {
      GPU_link(mat,
               out[1].hasoutput ? "color_alpha_clear" : "color_alpha_premultiply",
               out[0].link,
               &out[0].link);
    }
  }

  /* 2. Packed PBR Image: R=Roughness (2), G=Metallic (3), B=Height (4) */
  if (data_orig.pbr_image != nullptr) {
    GPUNodeLink *pbr_rgba = nullptr;
    GPUNodeLink *pbr_a = nullptr;
    sample_image(data_orig.pbr_image, &data_orig.pbr_iuser, &pbr_rgba, &pbr_a);
    GPU_link(mat, "separate_color_rgb", pbr_rgba, &out[2].link, &out[3].link, &out[4].link);
  }
  else {
    GPU_link(mat, "set_value", GPU_uniform(&data.base_roughness), &out[2].link);
    GPU_link(mat, "set_value", GPU_uniform(&data.base_metallic), &out[3].link);
    static const float half_val = 0.5f;
    GPU_link(mat, "set_value", GPU_constant(&half_val), &out[4].link);
  }

  /* 3. Normal (output 5) via GPU height differentiation when connected */
  GPUNodeLink *wnor = nullptr;
  GPU_link(mat, "world_normals_get", GPU_shading_data(), &wnor);
  if (data_orig.pbr_image != nullptr && out[5].hasoutput) {
    GPUNodeLink *height_for_bump = out[4].link;
    const char *height_fn = GPU_material_split_sub_function(mat, GPU_FLOAT, &height_for_bump);
    GPUNodeLink *dheight = GPU_differentiate_float_function(height_fn, 0.1f);
    static const float bump_dist = 0.02f;
    static const float filter_w = 0.1f;
    static const float invert = 1.0f;
    GPU_link(mat,
             "node_bump",
             GPU_uniform(&data.bump_strength),
             GPU_constant(&bump_dist),
             GPU_constant(&filter_w),
             height_for_bump,
             wnor,
             dheight,
             GPU_constant(&invert),
             GPU_kernel_globals(),
             GPU_shading_data(),
             &out[5].link);
  }
  else {
    out[5].link = wnor;
  }

  /* 4. Emission (output 6) */
  if (data_orig.emission_image != nullptr) {
    GPUNodeLink *em_a = nullptr;
    sample_image(data_orig.emission_image, &data_orig.emission_iuser, &out[6].link, &em_a);
    GPU_link(mat, "color_alpha_clear", out[6].link, &out[6].link);
  }
  else {
    static const float zero_rgba[4] = {0.0f, 0.0f, 0.0f, 1.0f};
    GPU_link(mat, "set_rgba", GPU_constant(zero_rgba), &out[6].link);
  }

  return true;
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
