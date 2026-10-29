/* SPDX-FileCopyrightText: 2026 Blender Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

/** \file
 * \ingroup spnode
 *
 * Operators managing the layer stack of the layered Texture Paint node. They are available in
 * the node editor and in the 3D viewport, where the node is found through the active material.
 */

#include "DNA_image_types.h"
#include "DNA_material_types.h"
#include "DNA_mesh_types.h"
#include "DNA_node_tex_paint_types.h"
#include "DNA_node_types.h"
#include "DNA_object_types.h"
#include "DNA_screen_types.h"
#include "DNA_space_types.h"
#include "DNA_view3d_types.h"

#include "BLI_listbase.hh"
#include "BLI_math_matrix.hh"
#include "BLI_math_matrix_c.hh"
#include "BLI_math_vector.hh"
#include "BLI_math_vector_c.hh"
#include "BLI_path_utils.hh"
#include "BLI_string_utf8.hh"

#include "BLT_translation.hh"

#include "BKE_callbacks.hh"
#include "BKE_context.hh"
#include "BKE_image.hh"
#include "BKE_library.hh"
#include "BKE_main.hh"
#include "BKE_main_invariants.hh"
#include "BKE_material.hh"
#include "BKE_node.hh"
#include "BKE_node_legacy_types.hh"
#include "BKE_node_runtime.hh"
#include "BKE_node_tree_update.hh"
#include "BKE_object_types.hh"
#include "BKE_report.hh"
#include "BKE_tex_paint_layer.hh"

#include "IMB_imbuf.hh"
#include "IMB_imbuf_types.hh"

#include "DEG_depsgraph.hh"
#include "DEG_depsgraph_build.hh"

#include "ED_image.hh"

#include "NOD_defaults.hh"

#include "RNA_access.hh"
#include "RNA_define.hh"
#include "RNA_enum_types.hh"
#include "RNA_prototypes.hh"

#include "UI_interface_layout.hh"
#include "UI_resources.hh"

#include "WM_api.hh"
#include "WM_types.hh"

#include "node_intern.hh"

namespace blender {

namespace ed::space_node {

/* -------------------------------------------------------------------- */
/** \name Context
 * \{ */

static bool is_tex_paint_node(const bNode *node)
{
  return node != nullptr && node->type_legacy == SH_NODE_TEX_PAINT && node->storage != nullptr;
}

static bNode *tex_paint_node_find_any(bNodeTree &ntree)
{
  bNode *fallback = nullptr;
  for (bNode *node : ntree.all_nodes()) {
    if (is_tex_paint_node(node)) {
      if (BKE_tex_paint_is_initialized(*node)) {
        return node;
      }
      if (fallback == nullptr) {
        fallback = node;
      }
    }
  }
  return fallback;
}

/**
 * Find the layered paint node to operate on: the node of the UI context, the active node of the
 * node editor, the image editor's active image, or the node used by the active material in the 3D
 * viewport.
 */
static bNode *tex_paint_node_from_context(bContext *C, bNodeTree **r_ntree)
{
  PointerRNA node_ptr = CTX_data_pointer_get_type(C, "node", RNA_Node);
  if (bNode *node = static_cast<bNode *>(node_ptr.data); is_tex_paint_node(node)) {
    *r_ntree = reinterpret_cast<bNodeTree *>(node_ptr.owner_id);
    return node;
  }

  if (SpaceNode *snode = CTX_wm_space_node(C); snode && snode->edittree) {
    bNode *node = bke::node_get_active(*snode->edittree);
    if (!is_tex_paint_node(node)) {
      node = tex_paint_node_find_any(*snode->edittree);
    }
    if (is_tex_paint_node(node)) {
      *r_ntree = snode->edittree;
      return node;
    }
  }

  if (SpaceImage *sima = CTX_wm_space_image(C)) {
    if (sima->image == nullptr) {
      return nullptr;
    }
    if (Object *ob = CTX_data_active_object(C)) {
      if (Material *ma = BKE_object_material_get(ob, ob->actcol); ma && ma->nodetree) {
        if (bNode *node = BKE_tex_paint_node_find_by_image(*ma->nodetree, sima->image)) {
          *r_ntree = ma->nodetree;
          return node;
        }
      }
    }
    if (Main *bmain = CTX_data_main(C)) {
      for (Material &ma : bmain->materials) {
        if (ma.nodetree) {
          if (bNode *node = BKE_tex_paint_node_find_by_image(*ma.nodetree, sima->image)) {
            *r_ntree = ma.nodetree;
            return node;
          }
        }
      }
      for (bNodeTree &ntree : bmain->nodetrees) {
        if (bNode *node = BKE_tex_paint_node_find_by_image(ntree, sima->image)) {
          *r_ntree = &ntree;
          return node;
        }
      }
    }
    return nullptr;
  }

  Object *ob = CTX_data_active_object(C);
  Material *ma = ob ? BKE_object_material_get(ob, ob->actcol) : nullptr;
  if (ma == nullptr || ma->nodetree == nullptr) {
    return nullptr;
  }
  bNode *node = bke::node_get_active(*ma->nodetree);
  if (!(is_tex_paint_node(node) && node->id == nullptr)) {
    node = nullptr;
    if (ma->texpaintslot && ma->paint_active_slot >= 0 && ma->paint_active_slot < ma->tot_slots) {
      node = BKE_tex_paint_node_find_by_image(*ma->nodetree,
                                              ma->texpaintslot[ma->paint_active_slot].ima);
    }
  }
  if (!is_tex_paint_node(node)) {
    node = bke::node_get_active_paint_canvas(*ma->nodetree);
  }
  if (!is_tex_paint_node(node)) {
    node = bke::node_get_active_texture(*ma->nodetree);
  }
  if (!is_tex_paint_node(node)) {
    node = tex_paint_node_find_any(*ma->nodetree);
  }
  if (!is_tex_paint_node(node)) {
    return nullptr;
  }
  *r_ntree = ma->nodetree;
  return node;
}

static bool tex_paint_node_poll(bContext *C)
{
  bNodeTree *ntree;
  return tex_paint_node_from_context(C, &ntree) != nullptr;
}

static bool tex_paint_canvas_new_poll(bContext *C)
{
  if (tex_paint_node_poll(C)) {
    return true;
  }
  Object *ob = CTX_data_active_object(C);
  return ob != nullptr && ob->type == OB_MESH && !ID_IS_LINKED(ob);
}

static bool tex_paint_canvas_poll(bContext *C)
{
  bNodeTree *ntree;
  const bNode *node = tex_paint_node_from_context(C, &ntree);
  return node && BKE_tex_paint_is_initialized(*node);
}

static NodeTexPaint &storage(bNode &node)
{
  return *static_cast<NodeTexPaint *>(node.storage);
}

/** Composite after a layer stack change and redraw, without re-evaluating the node tree. */
static void tex_paint_layers_changed(bContext *C, bNode &node)
{
  BKE_tex_paint_cache_invalidate(storage(node));
  BKE_tex_paint_composite_full(node);
  if (node.id) {
    DEG_id_tag_update(node.id, ID_RECALC_EDITORS);
    WM_event_add_notifier(C, NC_IMAGE | NA_EDITED, node.id);
  }
  WM_event_add_notifier(C, NC_NODE | NA_EDITED, nullptr);
  WM_event_add_notifier(C, NC_SCENE | ND_OB_ACTIVE, nullptr);
}

/** \} */

/* -------------------------------------------------------------------- */
/** \name New Layered Canvas
 * \{ */

static wmOperatorStatus tex_paint_canvas_new_exec(bContext *C, wmOperator *op)
{
  Main *bmain = CTX_data_main(C);
  bNodeTree *ntree = nullptr;
  bNode *node = tex_paint_node_from_context(C, &ntree);
  Object *ob = CTX_data_active_object(C);

  if (node == nullptr && ob != nullptr && ob->type == OB_MESH && !ID_IS_LINKED(ob)) {
    Material *ma = BKE_object_material_get(ob, ob->actcol);
    if (ma == nullptr) {
      ma = BKE_material_add(bmain, DATA_("Material"));
      BKE_object_material_assign(bmain, ob, ma, ob->actcol, BKE_MAT_ASSIGN_USERPREF);
      nodes::node_tree_shader_default(C, bmain, &ma->id);
    }
    else if (ma->nodetree == nullptr) {
      nodes::node_tree_shader_default(C, bmain, &ma->id);
    }
    ntree = ma->nodetree;
    if (ntree != nullptr) {
      node = bke::node_add_static_node(C, *ntree, SH_NODE_TEX_PAINT);
    }
  }

  if (node == nullptr || ntree == nullptr) {
    return OPERATOR_CANCELLED;
  }

  char name[MAX_ID_NAME - 2];
  RNA_string_get(op->ptr, "name", name);
  if (STREQ(name, "Painted Texture") && ob != nullptr) {
    if (Material *ma = BKE_object_material_get(ob, ob->actcol)) {
      SNPRINTF_UTF8(name, "%s_Base", ma->id.name + 2);
      if (node->label[0] == '\0') {
        STRNCPY_UTF8(node->label, "Base Color");
      }
    }
  }
  const int width = RNA_int_get(op->ptr, "width");
  const int height = RNA_int_get(op->ptr, "height");
  const bool is_float = RNA_boolean_get(op->ptr, "float");
  float color[4];
  RNA_float_get_array(op->ptr, "color", color);

  if (!BKE_tex_paint_canvas_init(*bmain, *node, name, width, height, is_float, color)) {
    BKE_report(op->reports, RPT_ERROR, "Could not create the layered canvas");
    return OPERATOR_CANCELLED;
  }

  /* Auto-link Color -> Principled BSDF Base Color when Base Color is unconnected and this node is for Base Color. */
  if (node->label[0] == '\0' || STREQ(node->label, "Base Color")) {
    ntree->ensure_topology_cache();
    const Span<bNode *> bsdf_nodes = ntree->nodes_by_type("ShaderNodeBsdfPrincipled"_ustr);
    if (!bsdf_nodes.is_empty()) {
      bNode *bsdf = bsdf_nodes.first();
      bNodeSocket *out_sock = bke::node_find_socket(*node, SOCK_OUT, "Color"_ustr);
      bNodeSocket *in_sock = bke::node_find_socket(*bsdf, SOCK_IN, "Base Color"_ustr);
      if (out_sock != nullptr && in_sock != nullptr && in_sock->link == nullptr) {
        if (node->label[0] == '\0') {
          STRNCPY_UTF8(node->label, "Base Color");
        }
        bke::node_add_link(*ntree, *node, *out_sock, *bsdf, *in_sock);
        bke::node_position_relative(*node, *bsdf, out_sock, *in_sock);
        bke::node_position_propagate(*node);
      }
    }
  }

  bke::node_set_active(*ntree, *node);

  /* The node now references an image: this changes the shader once. */
  BKE_ntree_update_tag_node_property(ntree, node);
  BKE_main_ensure_invariants(*bmain, ntree->id);
  DEG_id_tag_update(&ntree->id, 0);
  DEG_relations_tag_update(bmain);

  if (ob != nullptr) {
    BKE_texpaint_slots_refresh_object(CTX_data_scene(C), ob);
    if (Material *ma = BKE_object_material_get(ob, ob->actcol)) {
      DEG_id_tag_update(&ma->id, ID_RECALC_SHADING);
    }
  }
  if (Image *ima = reinterpret_cast<Image *>(node->id)) {
    ED_space_image_sync(bmain, ima, false);
  }
  WM_event_add_notifier(C, NC_IMAGE | NA_ADDED, node->id);
  WM_event_add_notifier(C, NC_NODE | NA_EDITED, ntree);
  WM_event_add_notifier(C, NC_MATERIAL | ND_SHADING, nullptr);
  return OPERATOR_FINISHED;
}

static wmOperatorStatus tex_paint_canvas_new_invoke(bContext *C,
                                                    wmOperator *op,
                                                    const wmEvent * /*event*/)
{
  return WM_operator_props_dialog_popup(
      C, op, 300, IFACE_("Create a New Layered Canvas"), IFACE_("Create"));
}

static void NODE_OT_tex_paint_canvas_new(wmOperatorType *ot)
{
  static const float default_color[4] = {1.0f, 1.0f, 1.0f, 1.0f};
  PropertyRNA *prop;

  ot->name = "New Layered Canvas";
  ot->description = "Create the composited image and a base layer for a Texture Paint node";
  ot->idname = "NODE_OT_tex_paint_canvas_new";

  ot->exec = tex_paint_canvas_new_exec;
  ot->invoke = tex_paint_canvas_new_invoke;
  ot->poll = tex_paint_canvas_new_poll;

  ot->flag = OPTYPE_REGISTER | OPTYPE_UNDO;

  RNA_def_string(ot->srna,
                 "name",
                 DATA_("Painted Texture"),
                 MAX_ID_NAME - 2,
                 "Name",
                 "Name of the composited image");
  prop = RNA_def_int(ot->srna, "width", 2048, 1, 16384, "Width", "Canvas width", 1, 16384);
  RNA_def_property_subtype(prop, PROP_PIXEL);
  prop = RNA_def_int(ot->srna, "height", 2048, 1, 16384, "Height", "Canvas height", 1, 16384);
  RNA_def_property_subtype(prop, PROP_PIXEL);
  prop = RNA_def_float_color(ot->srna,
                             "color",
                             4,
                             nullptr,
                             0.0f,
                             FLT_MAX,
                             "Base Color",
                             "Fill color of the base layer, in scene linear space",
                             0.0f,
                             1.0f);
  RNA_def_property_float_array_default(prop, default_color);
  RNA_def_boolean(ot->srna,
                  "float",
                  false,
                  "32-bit Float",
                  "Use 32-bit floating-point layers instead of 8-bit layers");
}

/** \} */

/* -------------------------------------------------------------------- */
/** \name Layer Operators
 * \{ */

static const EnumPropertyItem layer_type_items[] = {
    {TEXPAINT_LAYER_PAINT, "PAINT", ICON_BRUSH_DATA, "Paint Layer", ""},
    {TEXPAINT_LAYER_FILL, "FILL", ICON_IMAGE_RGB, "Fill Layer", ""},
    {TEXPAINT_LAYER_ADJUSTMENT, "ADJUSTMENT", ICON_MODIFIER, "Adjustment Layer", ""},
    {0, nullptr, 0, nullptr, nullptr},
};

static wmOperatorStatus tex_paint_layer_add_exec(bContext *C, wmOperator *op)
{
  bNodeTree *ntree;
  bNode *node = tex_paint_node_from_context(C, &ntree);
  if (node == nullptr) {
    return OPERATOR_CANCELLED;
  }
  const int type = RNA_enum_get(op->ptr, "type");
  const char *name = type == TEXPAINT_LAYER_FILL       ? DATA_("Fill") :
                     type == TEXPAINT_LAYER_ADJUSTMENT ? DATA_("Adjustment") :
                                                         DATA_("Layer");
  NodeTexPaint &data = storage(*node);
  const TexPaintLayer *prev_active = BKE_tex_paint_layer_active(data);
  const int prev_uid = prev_active ? prev_active->uid : 0;
  TexPaintLayer *new_layer = BKE_tex_paint_layer_add(data, name, type);
  data.paint_target = TEXPAINT_TARGET_COLOR;
  tex_paint_layers_changed(C, *node);

  /* Keep companion PBR channel nodes in the same material tree synchronized with the layer stack. */
  if (ntree != nullptr && new_layer != nullptr) {
    const int new_count = BKE_tex_paint_layer_count(data);
    for (bNode *other_node : ntree->all_nodes()) {
      if (other_node == node || other_node->type_legacy != SH_NODE_TEX_PAINT ||
          other_node->storage == nullptr || !BKE_tex_paint_is_initialized(*other_node))
      {
        continue;
      }
      NodeTexPaint &other_data = storage(*other_node);
      if (BKE_tex_paint_layer_find_uid(other_data, new_layer->uid) != nullptr ||
          BKE_tex_paint_layer_count(other_data) != new_count - 1)
      {
        continue;
      }
      if (prev_uid > 0) {
        if (TexPaintLayer *other_prev = BKE_tex_paint_layer_find_uid(other_data, prev_uid)) {
          BKE_tex_paint_layer_active_set(other_data,
                                         BLI_findindex(&other_data.layers, other_prev));
        }
      }
      TexPaintLayer *other_layer = BKE_tex_paint_layer_add(other_data, new_layer->name, type);
      if (other_layer != nullptr) {
        STRNCPY_UTF8(other_layer->name, new_layer->name);
        other_layer->uid = new_layer->uid;
        other_data.next_layer_uid = std::max(other_data.next_layer_uid, new_layer->uid + 1);
        if (type == TEXPAINT_LAYER_FILL) {
          /* Companion channels on a new Fill Layer start disabled (alpha = 0) until toggled ON. */
          other_layer->fill_color[0] = 0.5f;
          other_layer->fill_color[1] = 0.5f;
          other_layer->fill_color[2] = 0.5f;
          other_layer->fill_color[3] = 0.0f;
        }
        tex_paint_layers_changed(C, *other_node);
      }
    }
  }
  return OPERATOR_FINISHED;
}

static void NODE_OT_tex_paint_layer_add(wmOperatorType *ot)
{
  ot->name = "Add Layer";
  ot->description = "Add a layer above the active layer";
  ot->idname = "NODE_OT_tex_paint_layer_add";

  ot->exec = tex_paint_layer_add_exec;
  ot->poll = tex_paint_canvas_poll;

  ot->flag = OPTYPE_REGISTER | OPTYPE_UNDO;

  ot->prop = RNA_def_enum(
      ot->srna, "type", layer_type_items, TEXPAINT_LAYER_PAINT, "Type", "Kind of layer");
}

static wmOperatorStatus tex_paint_layer_remove_exec(bContext *C, wmOperator *op)
{
  bNodeTree *ntree;
  bNode *node = tex_paint_node_from_context(C, &ntree);
  if (node == nullptr) {
    return OPERATOR_CANCELLED;
  }
  NodeTexPaint &data = storage(*node);
  TexPaintLayer *layer = BKE_tex_paint_layer_active(data);
  if (layer == nullptr) {
    return OPERATOR_CANCELLED;
  }
  if (BKE_tex_paint_layer_count(data) <= 1) {
    BKE_report(op->reports, RPT_WARNING, "Cannot remove the last layer");
    return OPERATOR_CANCELLED;
  }
  const int removed_uid = layer->uid;
  BKE_tex_paint_layer_remove(data, *layer);
  tex_paint_layers_changed(C, *node);

  if (ntree != nullptr && removed_uid > 0) {
    for (bNode *other_node : ntree->all_nodes()) {
      if (other_node == node || other_node->type_legacy != SH_NODE_TEX_PAINT ||
          other_node->storage == nullptr || !BKE_tex_paint_is_initialized(*other_node))
      {
        continue;
      }
      NodeTexPaint &other_data = storage(*other_node);
      if (BKE_tex_paint_layer_count(other_data) <= 1) {
        continue;
      }
      if (TexPaintLayer *other_layer = BKE_tex_paint_layer_find_uid(other_data, removed_uid)) {
        BKE_tex_paint_layer_remove(other_data, *other_layer);
        tex_paint_layers_changed(C, *other_node);
      }
    }
  }
  return OPERATOR_FINISHED;
}

static void NODE_OT_tex_paint_layer_remove(wmOperatorType *ot)
{
  ot->name = "Remove Layer";
  ot->description = "Remove the active layer";
  ot->idname = "NODE_OT_tex_paint_layer_remove";

  ot->exec = tex_paint_layer_remove_exec;
  ot->poll = tex_paint_canvas_poll;

  ot->flag = OPTYPE_REGISTER | OPTYPE_UNDO;
}

static wmOperatorStatus tex_paint_layer_duplicate_exec(bContext *C, wmOperator * /*op*/)
{
  bNodeTree *ntree;
  bNode *node = tex_paint_node_from_context(C, &ntree);
  if (node == nullptr) {
    return OPERATOR_CANCELLED;
  }
  NodeTexPaint &data = storage(*node);
  TexPaintLayer *layer = BKE_tex_paint_layer_active(data);
  if (layer == nullptr) {
    return OPERATOR_CANCELLED;
  }
  const int src_uid = layer->uid;
  TexPaintLayer *dup_layer = BKE_tex_paint_layer_duplicate(data, *layer);
  tex_paint_layers_changed(C, *node);

  if (ntree != nullptr && dup_layer != nullptr && src_uid > 0) {
    for (bNode *other_node : ntree->all_nodes()) {
      if (other_node == node || other_node->type_legacy != SH_NODE_TEX_PAINT ||
          other_node->storage == nullptr || !BKE_tex_paint_is_initialized(*other_node))
      {
        continue;
      }
      NodeTexPaint &other_data = storage(*other_node);
      if (TexPaintLayer *other_src = BKE_tex_paint_layer_find_uid(other_data, src_uid)) {
        if (TexPaintLayer *other_dup = BKE_tex_paint_layer_duplicate(other_data, *other_src)) {
          STRNCPY_UTF8(other_dup->name, dup_layer->name);
          other_dup->uid = dup_layer->uid;
          other_data.next_layer_uid = std::max(other_data.next_layer_uid, dup_layer->uid + 1);
          tex_paint_layers_changed(C, *other_node);
        }
      }
    }
  }
  return OPERATOR_FINISHED;
}

static void NODE_OT_tex_paint_layer_duplicate(wmOperatorType *ot)
{
  ot->name = "Duplicate Layer";
  ot->description = "Duplicate the active layer";
  ot->idname = "NODE_OT_tex_paint_layer_duplicate";

  ot->exec = tex_paint_layer_duplicate_exec;
  ot->poll = tex_paint_canvas_poll;

  ot->flag = OPTYPE_REGISTER | OPTYPE_UNDO;
}

static wmOperatorStatus tex_paint_layer_move_exec(bContext *C, wmOperator *op)
{
  bNodeTree *ntree;
  bNode *node = tex_paint_node_from_context(C, &ntree);
  if (node == nullptr) {
    return OPERATOR_CANCELLED;
  }
  NodeTexPaint &data = storage(*node);
  TexPaintLayer *layer = BKE_tex_paint_layer_active(data);
  const int direction = RNA_enum_get(op->ptr, "direction");
  if (layer == nullptr || !BKE_tex_paint_layer_move(data, *layer, direction)) {
    return OPERATOR_CANCELLED;
  }
  const int moved_uid = layer->uid;
  tex_paint_layers_changed(C, *node);

  if (ntree != nullptr && moved_uid > 0) {
    for (bNode *other_node : ntree->all_nodes()) {
      if (other_node == node || other_node->type_legacy != SH_NODE_TEX_PAINT ||
          other_node->storage == nullptr || !BKE_tex_paint_is_initialized(*other_node))
      {
        continue;
      }
      NodeTexPaint &other_data = storage(*other_node);
      if (TexPaintLayer *other_layer = BKE_tex_paint_layer_find_uid(other_data, moved_uid)) {
        if (BKE_tex_paint_layer_move(other_data, *other_layer, direction)) {
          tex_paint_layers_changed(C, *other_node);
        }
      }
    }
  }
  return OPERATOR_FINISHED;
}

static void NODE_OT_tex_paint_layer_move(wmOperatorType *ot)
{
  static const EnumPropertyItem direction_items[] = {
      {1, "UP", 0, "Up", ""},
      {-1, "DOWN", 0, "Down", ""},
      {0, nullptr, 0, nullptr, nullptr},
  };

  ot->name = "Move Layer";
  ot->description = "Move the active layer up or down the stack";
  ot->idname = "NODE_OT_tex_paint_layer_move";

  ot->exec = tex_paint_layer_move_exec;
  ot->poll = tex_paint_canvas_poll;

  ot->flag = OPTYPE_REGISTER | OPTYPE_UNDO;

  ot->prop = RNA_def_enum(ot->srna, "direction", direction_items, 1, "Direction", "");
}

static wmOperatorStatus tex_paint_layer_merge_down_exec(bContext *C, wmOperator *op)
{
  bNodeTree *ntree;
  bNode *node = tex_paint_node_from_context(C, &ntree);
  if (node == nullptr) {
    return OPERATOR_CANCELLED;
  }
  if (!BKE_tex_paint_layer_merge_down(storage(*node))) {
    BKE_report(op->reports, RPT_WARNING, "The layer below must be an unlocked paint layer");
    return OPERATOR_CANCELLED;
  }
  tex_paint_layers_changed(C, *node);
  return OPERATOR_FINISHED;
}

static void NODE_OT_tex_paint_layer_merge_down(wmOperatorType *ot)
{
  ot->name = "Merge Down";
  ot->description = "Merge the active layer into the layer below it";
  ot->idname = "NODE_OT_tex_paint_layer_merge_down";

  ot->exec = tex_paint_layer_merge_down_exec;
  ot->poll = tex_paint_canvas_poll;

  ot->flag = OPTYPE_REGISTER | OPTYPE_UNDO;
}

/* -------------------------------------------------------------------- */
/** \name Import Image as Layer & Decal / Repeat Projection
 * \{ */

static const EnumPropertyItem projection_mode_items[] = {
    {TEXPAINT_PROJ_UV_FIT,
     "FIT",
     ICON_IMAGE_DATA,
     "UV Projection (Clamp)",
     "Fit or position the image in 2D UV space"},
    {TEXPAINT_PROJ_UV_REPEAT,
     "UV_REPEAT",
     ICON_TEXTURE,
     "UV Projection (Tile)",
     "Tile and repeat the image across the UV map"},
    {TEXPAINT_PROJ_TRIPLANAR,
     "TRIPLANAR",
     ICON_MESH_CUBE,
     "Triplanar Projection (3D Box)",
     "Repeat the image seamlessly across the entire 3D model using triplanar box projection"},
    {TEXPAINT_PROJ_VIEW_DECAL,
     "DECAL",
     ICON_MOD_UVPROJECT,
     "Planar / Decal Projection (3D)",
     "Project the image as a surface decal from the current 3D Viewport angle"},
    {TEXPAINT_PROJ_WARP,
     "WARP",
     ICON_MOD_WARP,
     "Warp Projection (3D Surface)",
     "Substance-Painter-style 3D projection that warps and curves around mesh geometry"},
    {TEXPAINT_PROJ_SPHERICAL,
     "SPHERICAL",
     ICON_MESH_UVSPHERE,
     "Spherical Projection (3D)",
     "Wrap the image spherically around the 3D model"},
    {TEXPAINT_PROJ_CYLINDRICAL,
     "CYLINDRICAL",
     ICON_MESH_CYLINDER,
     "Cylindrical Projection (3D)",
     "Wrap the image cylindrically around the 3D model"},
    {0, nullptr, 0, nullptr, nullptr},
};

static RegionView3D *find_active_rv3d(bContext *C, ARegion **r_win_region)
{
  if (r_win_region) {
    *r_win_region = nullptr;
  }
  RegionView3D *rv3d = CTX_wm_region_view3d(C);
  if (rv3d != nullptr) {
    if (r_win_region) {
      *r_win_region = CTX_wm_region(C);
    }
    return rv3d;
  }
  if (ScrArea *area = CTX_wm_area(C); area && area->spacetype == SPACE_VIEW3D) {
    for (ARegion &reg : area->regionbase) {
      if (reg.regiontype == RGN_TYPE_WINDOW && reg.regiondata != nullptr) {
        if (r_win_region) {
          *r_win_region = &reg;
        }
        return static_cast<RegionView3D *>(reg.regiondata);
      }
    }
  }
  if (bScreen *screen = CTX_wm_screen(C)) {
    for (ScrArea &area : screen->areabase) {
      if (area.spacetype == SPACE_VIEW3D) {
        for (ARegion &reg : area.regionbase) {
          if (reg.regiontype == RGN_TYPE_WINDOW && reg.regiondata != nullptr) {
            if (r_win_region) {
              *r_win_region = &reg;
            }
            return static_cast<RegionView3D *>(reg.regiondata);
          }
        }
      }
    }
  }
  return nullptr;
}

static bool find_view3d_projection(bContext *C,
                                   float r_persmat[4][4],
                                   float r_view_forward[3],
                                   float *r_aspect)
{
  ARegion *win_region = nullptr;
  RegionView3D *rv3d = find_active_rv3d(C, &win_region);
  if (rv3d == nullptr) {
    return false;
  }
  copy_m4_m4(r_persmat, rv3d->persmat);
  /* `viewinv[2]` points out of the screen toward the viewer. */
  copy_v3_v3(r_view_forward, rv3d->viewinv[2]);
  normalize_v3(r_view_forward);
  if (win_region != nullptr && win_region->winy > 0) {
    *r_aspect = float(win_region->winx) / float(win_region->winy);
  }
  else {
    *r_aspect = 1.0f;
  }
  return true;
}

static void init_layer_projection_widget(bContext *C,
                                         TexPaintLayer &layer,
                                         const Image &src_ima,
                                         const ImBuf &src_ibuf,
                                         TexPaintProjectionParams &params)
{
  STRNCPY_UTF8(layer.proj_image_name, src_ima.id.name + 2);
  layer.proj_mode = params.mode;
  layer.proj_blend_falloff = params.blend_falloff;
  layer.flag |= TEXPAINT_LAYER_SHOW_WIDGET;

  float3 bbox_min(-0.5f), bbox_max(0.5f);
  if (params.mesh != nullptr && !params.mesh->vert_positions().is_empty()) {
    bbox_min = float3(FLT_MAX);
    bbox_max = float3(-FLT_MAX);
    for (const float3 &p : params.mesh->vert_positions()) {
      bbox_min = math::min(bbox_min, p);
      bbox_max = math::max(bbox_max, p);
    }
  }
  const float3 bbox_center = (bbox_min + bbox_max) * 0.5f;
  const float3 bbox_size = bbox_max - bbox_min;
  const float max_extent = std::max({bbox_size.x, bbox_size.y, bbox_size.z, 1e-3f});
  const float img_aspect = float(src_ibuf.x) / float(std::max(src_ibuf.y, 1));

  if (ELEM(params.mode,
           TEXPAINT_PROJ_TRIPLANAR,
           TEXPAINT_PROJ_SPHERICAL,
           TEXPAINT_PROJ_CYLINDRICAL))
  {
    const float sx = max_extent * 0.5f / std::max(params.scale[0], 0.01f);
    const float sy = max_extent * 0.5f / std::max(params.scale[1], 0.01f);
    layer.proj_location[0] = bbox_center.x + params.offset[0] * max_extent;
    layer.proj_location[1] = bbox_center.y + params.offset[1] * max_extent;
    layer.proj_location[2] = bbox_center.z;
    layer.proj_rotation[0] = 0.0f;
    layer.proj_rotation[1] = 0.0f;
    layer.proj_rotation[2] = params.rotation;
    layer.proj_scale[0] = sx;
    layer.proj_scale[1] = sy;
    layer.proj_scale[2] = (params.mode == TEXPAINT_PROJ_TRIPLANAR) ? sx : -sx;
    params.use_layer_widget = true;
    params.repeat_uv = true;
  }
  else if (ELEM(params.mode, TEXPAINT_PROJ_VIEW_DECAL, TEXPAINT_PROJ_WARP)) {
    float3 col0(1.0f, 0.0f, 0.0f);
    float3 col1(0.0f, 1.0f, 0.0f);
    float3 col2(0.0f, 0.0f, 1.0f);
    Object *ob = CTX_data_active_object(C);
    if (RegionView3D *rv3d = find_active_rv3d(C, nullptr)) {
      float3 v_right(rv3d->viewinv[0]);
      float3 v_up(rv3d->viewinv[1]);
      float3 v_fwd(rv3d->viewinv[2]);
      if (ob != nullptr) {
        const float4x4 world_to_ob = ob->world_to_object();
        v_right = math::transform_direction(world_to_ob, v_right);
        v_up = math::transform_direction(world_to_ob, v_up);
        v_fwd = math::transform_direction(world_to_ob, v_fwd);
      }
      col2 = math::normalize(v_fwd);
      col0 = math::normalize(v_right - col2 * math::dot(v_right, col2));
      col1 = math::normalize(math::cross(col2, col0));
    }
    if (std::abs(params.rotation) > 1e-6f) {
      const float c = std::cos(params.rotation);
      const float s = std::sin(params.rotation);
      const float3 r0 = col0 * c + col1 * s;
      const float3 r1 = -col0 * s + col1 * c;
      col0 = r0;
      col1 = r1;
    }

    /* Extract Euler XYZ from orthonormal basis (col0, col1, col2). */
    layer.proj_rotation[1] = std::asin(std::clamp(-col0.z, -1.0f, 1.0f));
    if (std::abs(col0.z) < 0.9999f) {
      layer.proj_rotation[0] = std::atan2(col1.z, col2.z);
      layer.proj_rotation[2] = std::atan2(col0.y, col0.x);
    }
    else {
      layer.proj_rotation[0] = std::atan2(-col2.y, col1.y);
      layer.proj_rotation[2] = 0.0f;
    }

    /* Place the 3D decal widget on the front-most surface along `col2`. */
    float max_proj = 0.0f;
    if (params.mesh != nullptr && !params.mesh->vert_positions().is_empty()) {
      max_proj = -FLT_MAX;
      for (const float3 &p : params.mesh->vert_positions()) {
        max_proj = std::max(max_proj, math::dot(p - bbox_center, col2));
      }
    }
    const float3 surf_pt = bbox_center + col2 * max_proj +
                           col0 * (params.offset[0] * max_extent * 0.5f) +
                           col1 * (params.offset[1] * max_extent * 0.5f);
    layer.proj_location[0] = surf_pt.x;
    layer.proj_location[1] = surf_pt.y;
    layer.proj_location[2] = surf_pt.z;

    layer.proj_scale[0] = std::max(max_extent * 0.3f * params.scale[0] * img_aspect, 1e-3f);
    layer.proj_scale[1] = std::max(max_extent * 0.3f * params.scale[1], 1e-3f);
    layer.proj_scale[2] = std::max(max_extent * 0.45f, 1e-3f);
    params.use_layer_widget = true;
  }
  else {
    /* 2D UV Fit / Repeat */
    layer.proj_location[0] = params.offset[0];
    layer.proj_location[1] = params.offset[1];
    layer.proj_location[2] = 0.0f;
    layer.proj_rotation[0] = 0.0f;
    layer.proj_rotation[1] = 0.0f;
    layer.proj_rotation[2] = params.rotation;
    layer.proj_scale[0] = params.scale[0];
    layer.proj_scale[1] = params.scale[1];
    layer.proj_scale[2] = 1.0f;
  }
}

static void fill_projection_params_from_op(bContext *C,
                                           wmOperator *op,
                                           TexPaintProjectionParams &params)
{
  params.mode = RNA_enum_get(op->ptr, "projection_mode");
  RNA_float_get_array(op->ptr, "scale", params.scale);
  RNA_float_get_array(op->ptr, "offset", params.offset);
  params.rotation = RNA_float_get(op->ptr, "rotation");
  params.blend_falloff = RNA_float_get(op->ptr, "blend_falloff");
  params.clear_layer = RNA_boolean_get(op->ptr, "clear_layer");

  if (Object *ob = CTX_data_active_object(C); ob && ob->type == OB_MESH) {
    params.mesh = id_cast<const Mesh *>(ob->data);
    copy_m4_m4(params.ob_to_world, ob->object_to_world().ptr());
  }
  params.has_view = find_view3d_projection(
      C, params.view_proj_mat, params.view_forward, &params.viewport_aspect);
}

static void def_projection_operator_props(wmOperatorType *ot,
                                          const int default_mode,
                                          const bool default_new_layer,
                                          const bool default_clear_layer)
{
  static const float default_scale[2] = {1.0f, 1.0f};
  static const float default_offset[2] = {0.0f, 0.0f};
  PropertyRNA *prop;

  RNA_def_enum(ot->srna,
               "projection_mode",
               projection_mode_items,
               default_mode,
               "Projection",
               "How the image is mapped or projected onto the layer");

  prop = RNA_def_float_vector(ot->srna,
                              "scale",
                              2,
                              default_scale,
                              0.01f,
                              100.0f,
                              "Scale / Repeat",
                              "Decal size (in Fit/Decal mode) or tile repeat count (in Repeat mode)",
                              0.05f,
                              32.0f);
  RNA_def_property_float_array_default(prop, default_scale);

  prop = RNA_def_float_vector(ot->srna,
                              "offset",
                              2,
                              default_offset,
                              -10.0f,
                              10.0f,
                              "Offset",
                              "2D translation offset of the projected image",
                              -2.0f,
                              2.0f);
  RNA_def_property_float_array_default(prop, default_offset);

  prop = RNA_def_float_rotation(ot->srna,
                                "rotation",
                                0,
                                nullptr,
                                -FLT_MAX,
                                FLT_MAX,
                                "Rotation",
                                "Rotation angle of the projected image",
                                -float(M_PI) * 2.0f,
                                float(M_PI) * 2.0f);

  RNA_def_float_factor(ot->srna,
                       "blend_falloff",
                       0.5f,
                       0.0f,
                       1.0f,
                       "Falloff / Box Blend",
                       "Normal angle falloff for 3D decals or box blend sharpness for triplanar",
                       0.0f,
                       1.0f);

  RNA_def_boolean(ot->srna,
                  "new_layer",
                  default_new_layer,
                  "Create New Layer",
                  "Create a new paint layer for the projected image instead of projecting onto "
                  "the active layer");

  RNA_def_boolean(ot->srna,
                  "clear_layer",
                  default_clear_layer,
                  "Replace Layer Pixels",
                  "Clear the target layer before projecting (turn off to stamp multiple decals "
                  "onto the same layer)");
}

static wmOperatorStatus tex_paint_layer_import_image_exec(bContext *C, wmOperator *op)
{
  char filepath[FILE_MAX];
  RNA_string_get(op->ptr, "filepath", filepath);
  if (filepath[0] == '\0') {
    BKE_report(op->reports, RPT_ERROR, "No image file selected");
    return OPERATOR_CANCELLED;
  }

  bNodeTree *ntree = nullptr;
  bNode *node = tex_paint_node_from_context(C, &ntree);
  if (node == nullptr || !BKE_tex_paint_is_initialized(*node)) {
    BKE_report(op->reports, RPT_ERROR, "Initialize a Layered Canvas first");
    return OPERATOR_CANCELLED;
  }

  Main *bmain = CTX_data_main(C);
  Image *src_ima = BKE_image_load_exists(bmain, filepath);
  if (src_ima == nullptr) {
    BKE_reportf(op->reports, RPT_ERROR, "Could not load image \"%s\"", filepath);
    return OPERATOR_CANCELLED;
  }

  ImageUser iuser = {};
  BKE_imageuser_default(&iuser);
  void *lock = nullptr;
  ImBuf *src_ibuf = BKE_image_acquire_ibuf(src_ima, &iuser, &lock);
  if (src_ibuf == nullptr) {
    BKE_image_release_ibuf(src_ima, src_ibuf, lock);
    BKE_reportf(op->reports, RPT_ERROR, "Could not read pixels from \"%s\"", filepath);
    return OPERATOR_CANCELLED;
  }

  char file_name[FILE_MAXFILE];
  BLI_path_split_file_part(filepath, file_name, sizeof(file_name));
  if (file_name[0] == '\0') {
    STRNCPY_UTF8(file_name, DATA_("Imported Image"));
  }

  NodeTexPaint &data = storage(*node);
  const bool new_layer = RNA_boolean_get(op->ptr, "new_layer");
  TexPaintLayer *layer = new_layer ?
                             BKE_tex_paint_layer_add(data, file_name, TEXPAINT_LAYER_PAINT) :
                             BKE_tex_paint_layer_active(data);
  if (layer == nullptr || layer->type != TEXPAINT_LAYER_PAINT) {
    layer = BKE_tex_paint_layer_add(data, file_name, TEXPAINT_LAYER_PAINT);
  }

  TexPaintProjectionParams params;
  fill_projection_params_from_op(C, op, params);
  if (new_layer) {
    params.clear_layer = true;
  }
  init_layer_projection_widget(C, *layer, *src_ima, *src_ibuf, params);

  const bool ok = BKE_tex_paint_layer_project_imbuf(data, *layer, *src_ibuf, params);
  BKE_image_release_ibuf(src_ima, src_ibuf, lock);
  if (!ok) {
    BKE_report(op->reports, RPT_ERROR, "Failed to project image into layer");
    return OPERATOR_CANCELLED;
  }

  tex_paint_layers_changed(C, *node);
  return OPERATOR_FINISHED;
}

static void NODE_OT_tex_paint_layer_import_image(wmOperatorType *ot)
{
  ot->name = "Import Image as Layer";
  ot->description =
      "Import an image file into a paint layer (fit to canvas, repeat across model, or stamp as a "
      "3D surface decal)";
  ot->idname = "NODE_OT_tex_paint_layer_import_image";

  ot->exec = tex_paint_layer_import_image_exec;
  ot->invoke = WM_operator_filesel;
  ot->poll = tex_paint_canvas_poll;

  ot->flag = OPTYPE_REGISTER | OPTYPE_UNDO;

  WM_operator_properties_filesel(ot,
                                 FILE_TYPE_FOLDER | FILE_TYPE_IMAGE,
                                 FILE_SPECIAL,
                                 FILE_OPENFILE,
                                 WM_FILESEL_FILEPATH | WM_FILESEL_SHOW_PROPS,
                                 FILE_DEFAULTDISPLAY,
                                 FILE_SORT_DEFAULT);
  def_projection_operator_props(ot, TEXPAINT_PROJ_UV_FIT, true, true);
}

static const EnumPropertyItem *tex_paint_source_image_itemf(bContext *C,
                                                            PointerRNA * /*ptr*/,
                                                            PropertyRNA * /*prop*/,
                                                            bool *r_free)
{
  EnumPropertyItem *items = nullptr;
  int totitem = 0;

  if (C != nullptr) {
    Main *bmain = CTX_data_main(C);
    bNodeTree *ntree = nullptr;
    const bNode *node = tex_paint_node_from_context(C, &ntree);
    const ID *master_id = node ? node->id : nullptr;

    int idx = 0;
    for (Image &ima : bmain->images) {
      if (&ima.id == master_id || ELEM(ima.type, IMA_TYPE_R_RESULT, IMA_TYPE_COMPOSITE)) {
        continue;
      }
      EnumPropertyItem item = {};
      item.value = idx++;
      item.identifier = ima.id.name + 2;
      item.icon = ICON_IMAGE_DATA;
      item.name = ima.id.name + 2;
      RNA_enum_item_add(&items, &totitem, &item);
    }
  }

  if (totitem == 0) {
    EnumPropertyItem item = {0, "NONE", ICON_NONE, "No Image Loaded", ""};
    RNA_enum_item_add(&items, &totitem, &item);
  }
  RNA_enum_item_end(&items, &totitem);
  *r_free = true;
  return items;
}

static Image *find_source_image_by_enum(Main &bmain, const bNode *node, const int enum_val)
{
  const ID *master_id = node ? node->id : nullptr;
  int idx = 0;
  for (Image &ima : bmain.images) {
    if (&ima.id == master_id || ELEM(ima.type, IMA_TYPE_R_RESULT, IMA_TYPE_COMPOSITE)) {
      continue;
    }
    if (idx == enum_val) {
      return &ima;
    }
    idx++;
  }
  return nullptr;
}

static wmOperatorStatus tex_paint_layer_project_image_exec(bContext *C, wmOperator *op)
{
  Main *bmain = CTX_data_main(C);
  bNodeTree *ntree = nullptr;
  bNode *node = tex_paint_node_from_context(C, &ntree);
  if (node == nullptr || !BKE_tex_paint_is_initialized(*node)) {
    return OPERATOR_CANCELLED;
  }

  const int image_idx = RNA_enum_get(op->ptr, "image");
  Image *src_ima = find_source_image_by_enum(*bmain, node, image_idx);
  if (src_ima == nullptr) {
    BKE_report(
        op->reports, RPT_WARNING, "Load an image first (use Import Image Layer) to project it");
    return OPERATOR_CANCELLED;
  }

  ImageUser iuser = {};
  BKE_imageuser_default(&iuser);
  void *lock = nullptr;
  ImBuf *src_ibuf = BKE_image_acquire_ibuf(src_ima, &iuser, &lock);
  if (src_ibuf == nullptr) {
    BKE_image_release_ibuf(src_ima, src_ibuf, lock);
    BKE_report(op->reports, RPT_ERROR, "Selected image has no pixel buffer");
    return OPERATOR_CANCELLED;
  }

  NodeTexPaint &data = storage(*node);
  const bool new_layer = RNA_boolean_get(op->ptr, "new_layer");
  TexPaintLayer *layer = new_layer ? BKE_tex_paint_layer_add(
                                         data, src_ima->id.name + 2, TEXPAINT_LAYER_PAINT) :
                                     BKE_tex_paint_layer_active(data);
  if (layer == nullptr || layer->type != TEXPAINT_LAYER_PAINT) {
    layer = BKE_tex_paint_layer_add(data, src_ima->id.name + 2, TEXPAINT_LAYER_PAINT);
  }

  TexPaintProjectionParams params;
  fill_projection_params_from_op(C, op, params);
  init_layer_projection_widget(C, *layer, *src_ima, *src_ibuf, params);

  const bool ok = BKE_tex_paint_layer_project_imbuf(data, *layer, *src_ibuf, params);
  BKE_image_release_ibuf(src_ima, src_ibuf, lock);
  if (!ok) {
    return OPERATOR_CANCELLED;
  }

  tex_paint_layers_changed(C, *node);
  return OPERATOR_FINISHED;
}

static wmOperatorStatus tex_paint_layer_project_image_invoke(bContext *C,
                                                             wmOperator *op,
                                                             const wmEvent * /*event*/)
{
  return WM_operator_props_dialog_popup(
      C, op, 320, IFACE_("Project Decal / Repeat Image on Layer"), IFACE_("Project"));
}

static void NODE_OT_tex_paint_layer_project_image(wmOperatorType *ot)
{
  PropertyRNA *prop;

  ot->name = "Project Decal / Repeat Image";
  ot->description =
      "Project a loaded image onto the active or new layer as a 3D surface decal, 3D triplanar "
      "whole-model repeat, or 2D UV tile";
  ot->idname = "NODE_OT_tex_paint_layer_project_image";

  ot->exec = tex_paint_layer_project_image_exec;
  ot->invoke = tex_paint_layer_project_image_invoke;
  ot->poll = tex_paint_canvas_poll;

  ot->flag = OPTYPE_REGISTER | OPTYPE_UNDO;

  prop = RNA_def_enum(
      ot->srna, "image", rna_enum_dummy_NULL_items, 0, "Image", "Source image to project");
  RNA_def_enum_funcs(prop, tex_paint_source_image_itemf);

  def_projection_operator_props(ot, TEXPAINT_PROJ_VIEW_DECAL, false, false);
}

/** \} */

/* -------------------------------------------------------------------- */
/** \name Layer Mask & Substance-Style Smart Mask Generator Operators
 * \{ */

enum eTexPaintMaskInitMode {
  TEXPAINT_MASK_INIT_WHITE = 0,
  TEXPAINT_MASK_INIT_BLACK = 1,
  TEXPAINT_MASK_INIT_INVERT = 2,
  TEXPAINT_MASK_INIT_REMOVE = 3,
};

static const EnumPropertyItem mask_init_mode_items[] = {
    {TEXPAINT_MASK_INIT_WHITE,
     "WHITE",
     ICON_MOD_MASK,
     "White Mask (Reveal All)",
     "Add or clear a white mask (layer is visible everywhere; paint black to hide)"},
    {TEXPAINT_MASK_INIT_BLACK,
     "BLACK",
     ICON_SHADING_SOLID,
     "Black Mask (Hide All)",
     "Add or clear a black mask (layer is hidden everywhere; paint white to reveal)"},
    {TEXPAINT_MASK_INIT_INVERT,
     "INVERT",
     ICON_ARROW_LEFTRIGHT,
     "Invert Mask Pixels",
     "Invert the grayscale pixels of the active layer mask"},
    {TEXPAINT_MASK_INIT_REMOVE,
     "REMOVE",
     ICON_X,
     "Remove Mask",
     "Delete the mask from the active layer"},
    {0, nullptr, 0, nullptr, nullptr},
};

static wmOperatorStatus tex_paint_layer_mask_init_exec(bContext *C, wmOperator *op)
{
  bNodeTree *ntree = nullptr;
  bNode *node = tex_paint_node_from_context(C, &ntree);
  if (node == nullptr || !BKE_tex_paint_is_initialized(*node)) {
    return OPERATOR_CANCELLED;
  }
  NodeTexPaint &data = storage(*node);
  TexPaintLayer *layer = BKE_tex_paint_layer_active(data);
  if (layer == nullptr) {
    return OPERATOR_CANCELLED;
  }

  const int mode = RNA_enum_get(op->ptr, "mode");
  const int target_uid = layer->uid;
  switch (mode) {
    case TEXPAINT_MASK_INIT_WHITE:
      BKE_tex_paint_layer_mask_fill(data, *layer, 1.0f);
      data.paint_target = TEXPAINT_TARGET_MASK;
      break;
    case TEXPAINT_MASK_INIT_BLACK:
      BKE_tex_paint_layer_mask_fill(data, *layer, 0.0f);
      data.paint_target = TEXPAINT_TARGET_MASK;
      break;
    case TEXPAINT_MASK_INIT_INVERT:
      layer->flag |= TEXPAINT_LAYER_USE_MASK;
      BKE_tex_paint_layer_mask_invert_pixels(data, *layer);
      data.paint_target = TEXPAINT_TARGET_MASK;
      break;
    case TEXPAINT_MASK_INIT_REMOVE:
      BKE_tex_paint_layer_mask_remove(data, *layer);
      break;
    default:
      return OPERATOR_CANCELLED;
  }

  tex_paint_layers_changed(C, *node);

  if (ntree != nullptr && target_uid > 0) {
    for (bNode *other_node : ntree->all_nodes()) {
      if (other_node == node || other_node->type_legacy != SH_NODE_TEX_PAINT ||
          other_node->storage == nullptr || !BKE_tex_paint_is_initialized(*other_node))
      {
        continue;
      }
      NodeTexPaint &other_data = storage(*other_node);
      TexPaintLayer *other_layer = BKE_tex_paint_layer_find_uid(other_data, target_uid);
      if (other_layer == nullptr) {
        continue;
      }
      switch (mode) {
        case TEXPAINT_MASK_INIT_WHITE:
          BKE_tex_paint_layer_mask_fill(other_data, *other_layer, 1.0f);
          break;
        case TEXPAINT_MASK_INIT_BLACK:
          BKE_tex_paint_layer_mask_fill(other_data, *other_layer, 0.0f);
          break;
        case TEXPAINT_MASK_INIT_INVERT:
          other_layer->flag |= TEXPAINT_LAYER_USE_MASK;
          BKE_tex_paint_layer_mask_invert_pixels(other_data, *other_layer);
          break;
        case TEXPAINT_MASK_INIT_REMOVE:
          BKE_tex_paint_layer_mask_remove(other_data, *other_layer);
          break;
        default:
          break;
      }
      tex_paint_layers_changed(C, *other_node);
    }
  }
  return OPERATOR_FINISHED;
}

static void NODE_OT_tex_paint_layer_mask_init(wmOperatorType *ot)
{
  ot->name = "Layer Mask Action";
  ot->description =
      "Add a White Mask (reveal all), Black Mask (hide all), Invert Mask, or Remove Mask on the "
      "active layer";
  ot->idname = "NODE_OT_tex_paint_layer_mask_init";

  ot->exec = tex_paint_layer_mask_init_exec;
  ot->poll = tex_paint_canvas_poll;

  ot->flag = OPTYPE_REGISTER | OPTYPE_UNDO;

  ot->prop = RNA_def_enum(
      ot->srna, "mode", mask_init_mode_items, TEXPAINT_MASK_INIT_WHITE, "Action", "");
}

static const EnumPropertyItem mask_gen_op_type_items[] = {
    {TEXPAINT_MASK_GEN_EDGE_WEAR,
     "EDGE_WEAR",
     ICON_MOD_BEVEL,
     "Edge Wear (Convex)",
     "Generate a Substance-style convex edge wear & grunge mask on the active layer"},
    {TEXPAINT_MASK_GEN_CURVATURE,
     "CURVATURE",
     ICON_SMOOTHCURVE,
     "Curvature",
     "Generate a 3D mesh surface curvature mask on the active layer"},
    {TEXPAINT_MASK_GEN_CAVITY,
     "CAVITY",
     ICON_LINCURVE,
     "Cavity / Creases",
     "Generate a concave cavity & seam dirt mask on the active layer"},
    {TEXPAINT_MASK_GEN_AO,
     "AO",
     ICON_SHADING_RENDERED,
     "Ambient Occlusion (AO)",
     "Generate a 3D hemisphere ambient occlusion mask on the active layer"},
    {TEXPAINT_MASK_GEN_NORMAL_UP,
     "NORMAL_UP",
     ICON_NORMALS_FACE,
     "Top Dust / Upward Normal",
     "Generate an upward-facing surface normal mask (dust, snow, moss) on the active layer"},
    {TEXPAINT_MASK_GEN_POSITION_Z,
     "POSITION_Z",
     ICON_AXIS_TOP,
     "Height Gradient (Z)",
     "Generate a vertical bottom-to-top position gradient mask on the active layer"},
    {0, nullptr, 0, nullptr, nullptr},
};

static const Mesh *find_active_or_ntree_mesh(bContext *C, const bNodeTree *ntree)
{
  if (Object *ob = CTX_data_active_object(C); ob && ob->type == OB_MESH && ob->data) {
    return id_cast<const Mesh *>(ob->data);
  }
  if (Main *bmain = CTX_data_main(C)) {
    const Mesh *fallback = nullptr;
    for (Object &ob : bmain->objects) {
      if (ob.type != OB_MESH || ob.data == nullptr) {
        continue;
      }
      const Mesh *mesh = id_cast<const Mesh *>(ob.data);
      if (fallback == nullptr) {
        fallback = mesh;
      }
      for (int i = 1; i <= ob.totcol; i++) {
        if (Material *ma = BKE_object_material_get(&ob, i); ma && ma->nodetree == ntree) {
          return mesh;
        }
      }
    }
    return fallback;
  }
  return nullptr;
}

static wmOperatorStatus tex_paint_layer_generate_mask_exec(bContext *C, wmOperator *op)
{
  bNodeTree *ntree = nullptr;
  bNode *node = tex_paint_node_from_context(C, &ntree);
  if (node == nullptr || !BKE_tex_paint_is_initialized(*node)) {
    return OPERATOR_CANCELLED;
  }
  NodeTexPaint &data = storage(*node);
  TexPaintLayer *layer = BKE_tex_paint_layer_active(data);
  if (layer == nullptr) {
    return OPERATOR_CANCELLED;
  }
  const Mesh *mesh = find_active_or_ntree_mesh(C, ntree);
  if (mesh == nullptr) {
    BKE_report(op->reports, RPT_WARNING, "No active mesh found to bake geometry mask");
    return OPERATOR_CANCELLED;
  }

  const int gen_type = RNA_enum_get(op->ptr, "type");
  layer->mask_gen_type = gen_type;
  layer->flag |= TEXPAINT_LAYER_USE_MASK;
  layer->flag &= ~TEXPAINT_LAYER_MASK_INVERT;

  switch (gen_type) {
    case TEXPAINT_MASK_GEN_EDGE_WEAR:
      layer->mask_gen_balance = 0.58f;
      layer->mask_gen_contrast = 0.65f;
      layer->mask_gen_radius = 0.35f;
      layer->mask_gen_noise = 0.35f;
      layer->mask_gen_noise_scale = 8.0f;
      layer->mask_gen_invert = 0;
      break;
    case TEXPAINT_MASK_GEN_CURVATURE:
      layer->mask_gen_balance = 0.50f;
      layer->mask_gen_contrast = 0.55f;
      layer->mask_gen_radius = 0.35f;
      layer->mask_gen_noise = 0.0f;
      layer->mask_gen_invert = 0;
      break;
    case TEXPAINT_MASK_GEN_CAVITY:
      layer->mask_gen_balance = 0.58f;
      layer->mask_gen_contrast = 0.65f;
      layer->mask_gen_radius = 0.35f;
      layer->mask_gen_noise = 0.20f;
      layer->mask_gen_noise_scale = 10.0f;
      layer->mask_gen_invert = 0;
      break;
    case TEXPAINT_MASK_GEN_AO:
      layer->mask_gen_balance = 0.50f;
      layer->mask_gen_contrast = 0.50f;
      layer->mask_gen_radius = 0.45f;
      layer->mask_gen_noise = 0.0f;
      layer->mask_gen_invert = 0;
      break;
    case TEXPAINT_MASK_GEN_NORMAL_UP:
      layer->mask_gen_balance = 0.45f;
      layer->mask_gen_contrast = 0.55f;
      layer->mask_gen_radius = 0.35f;
      layer->mask_gen_noise = 0.15f;
      layer->mask_gen_noise_scale = 6.0f;
      layer->mask_gen_invert = 0;
      break;
    case TEXPAINT_MASK_GEN_POSITION_Z:
      layer->mask_gen_balance = 0.50f;
      layer->mask_gen_contrast = 0.30f;
      layer->mask_gen_radius = 0.35f;
      layer->mask_gen_noise = 0.10f;
      layer->mask_gen_noise_scale = 5.0f;
      layer->mask_gen_invert = 0;
      break;
    default:
      break;
  }

  if (!BKE_tex_paint_layer_generate_mask(mesh, data, *layer)) {
    BKE_report(op->reports, RPT_WARNING, "Mesh needs a UV map to generate a geometry mask");
    return OPERATOR_CANCELLED;
  }

  data.paint_target = TEXPAINT_TARGET_MASK;
  tex_paint_layers_changed(C, *node);
  return OPERATOR_FINISHED;
}

static void NODE_OT_tex_paint_layer_generate_mask(wmOperatorType *ot)
{
  ot->name = "Generate Smart Mask";
  ot->description =
      "Generate a Substance-style procedural geometry mask (Edge Wear, Curvature, Cavity, Ambient "
      "Occlusion, Top Normal, or Height Gradient) on the active layer";
  ot->idname = "NODE_OT_tex_paint_layer_generate_mask";

  ot->exec = tex_paint_layer_generate_mask_exec;
  ot->poll = tex_paint_canvas_poll;

  ot->flag = OPTYPE_REGISTER | OPTYPE_UNDO;

  ot->prop = RNA_def_enum(ot->srna,
                          "type",
                          mask_gen_op_type_items,
                          TEXPAINT_MASK_GEN_EDGE_WEAR,
                          "Generator",
                          "Type of procedural geometry mask to generate");
}

/** \} */

/* -------------------------------------------------------------------- */
/** \name Registration
 * \{ */

/**
 * Loading a file and memfile undo re-read node storage without runtime data, and image buffers
 * of the master images may be new. Restore their composites.
 */
static void tex_paint_composite_all_cb(Main *bmain,
                                       PointerRNA ** /*pointers*/,
                                       const int /*pointers_num*/,
                                       void * /*arg*/)
{
  BKE_tex_paint_composite_all(*bmain);
}

static bCallbackFuncStore tex_paint_load_post_cb = {
    nullptr, nullptr, tex_paint_composite_all_cb, nullptr, 0};
static bCallbackFuncStore tex_paint_undo_post_cb = {
    nullptr, nullptr, tex_paint_composite_all_cb, nullptr, 0};
static bCallbackFuncStore tex_paint_redo_post_cb = {
    nullptr, nullptr, tex_paint_composite_all_cb, nullptr, 0};

void node_tex_paint_operatortypes()
{
  WM_operatortype_append(NODE_OT_tex_paint_canvas_new);
  WM_operatortype_append(NODE_OT_tex_paint_layer_add);
  WM_operatortype_append(NODE_OT_tex_paint_layer_remove);
  WM_operatortype_append(NODE_OT_tex_paint_layer_duplicate);
  WM_operatortype_append(NODE_OT_tex_paint_layer_move);
  WM_operatortype_append(NODE_OT_tex_paint_layer_merge_down);
  WM_operatortype_append(NODE_OT_tex_paint_layer_import_image);
  WM_operatortype_append(NODE_OT_tex_paint_layer_project_image);
  WM_operatortype_append(NODE_OT_tex_paint_layer_mask_init);
  WM_operatortype_append(NODE_OT_tex_paint_layer_generate_mask);

  BKE_callback_add(&tex_paint_load_post_cb, BKE_CB_EVT_LOAD_POST);
  BKE_callback_add(&tex_paint_undo_post_cb, BKE_CB_EVT_UNDO_POST);
  BKE_callback_add(&tex_paint_redo_post_cb, BKE_CB_EVT_REDO_POST);
}

/** \} */

}  // namespace ed::space_node

}  // namespace blender
