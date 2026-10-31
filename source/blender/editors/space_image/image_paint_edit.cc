/* SPDX-FileCopyrightText: 2026 Blender Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

/** \file
 * \ingroup spimage
 * Native selections and transactional pixel transforms for layered images and material canvases.
 */

#include <algorithm>
#include <cmath>
#include <fmt/format.h>

#include "BKE_context.hh"
#include "BKE_image.hh"
#include "BKE_lib_id.hh"
#include "BKE_library.hh"
#include "BKE_main.hh"
#include "BKE_node.hh"
#include "BKE_node_legacy_types.hh"
#include "BKE_node_runtime.hh"
#include "BKE_report.hh"
#include "BKE_tex_paint_layer.hh"
#include "BLI_math_matrix.hh"
#include "BLI_math_vector.hh"
#include "BLI_rect.hh"
#include "BLI_vector.hh"
#include "DEG_depsgraph.hh"
#include "DNA_image_types.h"
#include "DNA_node_tex_paint_types.h"
#include "DNA_node_types.h"
#include "DNA_screen_types.h"
#include "DNA_space_types.h"
#include "ED_image.hh"
#include "ED_numinput.hh"
#include "ED_screen.hh"
#include "ED_select_utils.hh"
#include "GPU_immediate.hh"
#include "GPU_state.hh"
#include "IMB_imbuf.hh"
#include "IMB_imbuf_types.hh"
#include "MEM_guardedalloc.h"
#include "RNA_access.hh"
#include "RNA_define.hh"
#include "UI_interface.hh"
#include "UI_resources.hh"
#include "UI_view2d.hh"
#include "WM_api.hh"
#include "WM_types.hh"
#include "image_intern.hh"

namespace blender {

static bNode *paint_edit_node(const bContext *C)
{
  SpaceImage *sima = CTX_wm_space_image(C);
  return sima && sima->image ? BKE_tex_paint_node_find_in_main(*CTX_data_main(C), sima->image) :
                               nullptr;
}

static bool paint_edit_poll(bContext *C)
{
  SpaceImage *sima = CTX_wm_space_image(C);
  if (!sima || !ELEM(sima->mode, SI_MODE_PAINT, SI_MODE_VIEW) || !sima->image ||
      ID_IS_LINKED(sima->image))
  {
    return false;
  }
  if (bNode *node = paint_edit_node(C)) {
    return ID_IS_EDITABLE(&node->owner_tree().id) &&
           !ID_IS_OVERRIDE_LIBRARY(&node->owner_tree().id) && BKE_tex_paint_is_initialized(*node);
  }
  return false;
}

static NodeTexPaint &storage(bNode &node)
{
  return *static_cast<NodeTexPaint *>(node.storage);
}

static void paint_edit_tag_undo(bContext *C, bNode &node)
{
  CTX_data_main(C)->is_memfile_undo_written = false;
  bNodeTree &tree = node.owner_tree();
  DEG_id_tag_update(&tree.id, ID_RECALC_TAG_FOR_UNDO);
  if (tree.owner_id) {
    DEG_id_tag_update(tree.owner_id, ID_RECALC_TAG_FOR_UNDO);
  }
}

static void selection_notify(bContext *C, bNode &node)
{
  paint_edit_tag_undo(C, node);
  WM_event_add_notifier(C, NC_IMAGE | ND_DRAW, node.id);
}

static float2 mouse_pixel(const bContext *C, const int x, const int y, const NodeTexPaint &data)
{
  float2 uv;
  ui::view2d_region_to_view(&CTX_wm_region(C)->v2d, x, y, &uv.x, &uv.y);
  return uv * float2(data.width, data.height);
}

static void combine_selection(NodeTexPaint &data, MutableSpan<float> incoming, const int mode)
{
  const Span<float> old = BKE_tex_paint_selection(data);
  for (const int64_t i : incoming.index_range()) {
    const float value = old.is_empty() ? 0 : old[i];
    switch (mode) {
      case SEL_OP_ADD:
        incoming[i] = std::max(value, incoming[i]);
        break;
      case SEL_OP_SUB:
        incoming[i] = std::max(value - incoming[i], 0.0f);
        break;
      case SEL_OP_AND:
        incoming[i] = std::min(value, incoming[i]);
        break;
      default:
        break;
    }
  }
  BKE_tex_paint_selection_set(data, incoming);
}

static wmOperatorStatus select_wand_exec(bContext *C, wmOperator *op)
{
  bNode &node = *paint_edit_node(C);
  NodeTexPaint &data = storage(node);
  ImBuf *image = nullptr;
  Image *master = reinterpret_cast<Image *>(node.id);
  void *lock = nullptr;
  if (RNA_boolean_get(op->ptr, "sample_merged")) {
    image = BKE_image_acquire_ibuf(master, &data.image.iuser, &lock);
  }
  else {
    image = BKE_tex_paint_paint_target_ibuf(data);
  }
  if (!image) {
    BKE_report(op->reports, RPT_WARNING, "Select a visible, unlocked pixel layer or layer mask");
    return OPERATOR_CANCELLED;
  }
  int seed[2];
  RNA_int_get_array(op->ptr, "location", seed);
  Array<float> mask = BKE_tex_paint_select_wand(*image,
                                                int2(seed),
                                                RNA_float_get(op->ptr, "tolerance"),
                                                RNA_boolean_get(op->ptr, "contiguous"));
  if (RNA_boolean_get(op->ptr, "sample_merged")) {
    BKE_image_release_ibuf(master, image, lock);
  }
  combine_selection(data, mask, RNA_enum_get(op->ptr, "mode"));
  selection_notify(C, node);
  return OPERATOR_FINISHED;
}

static wmOperatorStatus select_wand_invoke(bContext *C, wmOperator *op, const wmEvent *event)
{
  const float2 pixel = mouse_pixel(
      C, event->mval[0], event->mval[1], storage(*paint_edit_node(C)));
  const int location[] = {int(std::floor(pixel.x)), int(std::floor(pixel.y))};
  RNA_int_set_array(op->ptr, "location", location);
  return select_wand_exec(C, op);
}

void IMAGE_OT_paint_select_wand(wmOperatorType *ot)
{
  ot->name = "Select Image by Color";
  ot->idname = "IMAGE_OT_paint_select_wand";
  ot->description = "Select canvas pixels by color on the active layer or mask";
  ot->poll = paint_edit_poll;
  ot->exec = select_wand_exec;
  ot->invoke = select_wand_invoke;
  ot->flag = OPTYPE_UNDO | OPTYPE_REGISTER;
  RNA_def_int_vector(
      ot->srna, "location", 2, nullptr, INT_MIN, INT_MAX, "Pixel", "", INT_MIN, INT_MAX);
  RNA_def_float(ot->srna, "tolerance", 0.1f, 0, 1, "Tolerance", "Maximum RGBA difference", 0, 1);
  RNA_def_boolean(ot->srna, "contiguous", true, "Contiguous", "Select connected pixels only");
  RNA_def_boolean(
      ot->srna, "sample_merged", false, "Sample Merged", "Sample the composited canvas");
  WM_operator_properties_select_operation_simple(ot);
}

static wmOperatorStatus select_lasso_exec(bContext *C, wmOperator *op)
{
  bNode &node = *paint_edit_node(C);
  NodeTexPaint &data = storage(node);
  const Array<int2> path = WM_gesture_lasso_path_to_array(C, op);
  if (path.size() < 3) {
    return OPERATOR_CANCELLED;
  }
  Vector<float2> polygon;
  for (const int2 p : path) {
    polygon.append(mouse_pixel(C, p.x, p.y, data));
  }
  Array<float> mask = BKE_tex_paint_select_polygon(data.width, data.height, polygon);
  combine_selection(data, mask, RNA_enum_get(op->ptr, "mode"));
  selection_notify(C, node);
  return OPERATOR_FINISHED;
}

void IMAGE_OT_paint_select_lasso(wmOperatorType *ot)
{
  ot->name = "Lasso Select Image";
  ot->idname = "IMAGE_OT_paint_select_lasso";
  ot->description = "Select canvas pixels with a freehand outline";
  ot->poll = paint_edit_poll;
  ot->invoke = WM_gesture_lasso_invoke;
  ot->modal = WM_gesture_lasso_modal;
  ot->cancel = WM_gesture_lasso_cancel;
  ot->exec = select_lasso_exec;
  ot->flag = OPTYPE_UNDO | OPTYPE_DEPENDS_ON_CURSOR;
  WM_operator_properties_gesture_lasso(ot);
  WM_operator_properties_select_operation_simple(ot);
}

static wmOperatorStatus select_box_exec(bContext *C, wmOperator *op)
{
  bNode &node = *paint_edit_node(C);
  NodeTexPaint &data = storage(node);
  const float2 a = mouse_pixel(
      C, RNA_int_get(op->ptr, "xmin"), RNA_int_get(op->ptr, "ymin"), data);
  const float2 b = mouse_pixel(
      C, RNA_int_get(op->ptr, "xmax"), RNA_int_get(op->ptr, "ymax"), data);
  const float2 points[] = {a, float2(b.x, a.y), b, float2(a.x, b.y)};
  Array<float> mask = BKE_tex_paint_select_polygon(data.width, data.height, Span(points, 4));
  combine_selection(data, mask, RNA_enum_get(op->ptr, "mode"));
  selection_notify(C, node);
  return OPERATOR_FINISHED;
}

void IMAGE_OT_paint_select_box(wmOperatorType *ot)
{
  ot->name = "Box Select Image";
  ot->idname = "IMAGE_OT_paint_select_box";
  ot->description = "Select canvas pixels within a rectangle";
  ot->poll = paint_edit_poll;
  ot->invoke = WM_gesture_box_invoke;
  ot->modal = WM_gesture_box_modal;
  ot->cancel = WM_gesture_box_cancel;
  ot->exec = select_box_exec;
  ot->flag = OPTYPE_UNDO;
  WM_operator_properties_border(ot);
  WM_operator_properties_select_operation_simple(ot);
}

static wmOperatorStatus select_all_exec(bContext *C, wmOperator *op)
{
  bNode &node = *paint_edit_node(C);
  NodeTexPaint &data = storage(node);
  const int action = RNA_enum_get(op->ptr, "action");
  if (action == 0) {
    BKE_tex_paint_selection_clear(data);
  }
  else {
    const Span<float> old = BKE_tex_paint_selection(data);
    Array<float> mask(int64_t(data.width) * data.height, 1.0f);
    if (action == 2 && !old.is_empty()) {
      for (const int64_t i : mask.index_range()) {
        mask[i] = 1 - old[i];
      }
    }
    BKE_tex_paint_selection_set(data, mask);
  }
  selection_notify(C, node);
  return OPERATOR_FINISHED;
}

void IMAGE_OT_paint_select_all(wmOperatorType *ot)
{
  static const EnumPropertyItem actions[] = {
      {0, "DESELECT", 0, "Deselect", "Remove the selection restriction"},
      {1, "SELECT", 0, "Select All", "Select the entire canvas"},
      {2, "INVERT", 0, "Invert", "Invert selected and unselected pixels"},
      {0, nullptr, 0, nullptr, nullptr}};
  ot->name = "Image Selection";
  ot->idname = "IMAGE_OT_paint_select_all";
  ot->description = "Select all, invert, or clear the canvas selection";
  ot->poll = paint_edit_poll;
  ot->exec = select_all_exec;
  ot->flag = OPTYPE_UNDO | OPTYPE_REGISTER;
  RNA_def_enum(ot->srna, "action", actions, 1, "Action", "");
}

struct PaintTransform {
  bNode *node = nullptr;
  ImBuf *target = nullptr;
  Array<float4> original, preview, source;
  Array<float> selection, preview_selection, source_selection;
  float3x3 transform_before = float3x3::identity(), transform = float3x3::identity();
  float2 pivot, mouse_start;
  rcti source_bounds, previous_bounds;
  NumInput numeric;
  int mode = 0, axis = -1;
  bool mask = false, dirty = false, confirm_on_release = false;
  bool reuse_source = false;
};

static rcti transformed_bounds(const rcti &source,
                               const float3x3 &transform,
                               float2 *r_pivot = nullptr)
{
  rctf bounds;
  BLI_rctf_init_minmax(&bounds);
  for (int cy = 0; cy < 2; cy++) {
    for (int cx = 0; cx < 2; cx++) {
      /* Bilinear sampling extends half a source pixel past the selection's pixel edges.
       * Expand before transforming so large scales also include the interpolation fringe. */
      const float3 p = transform * float3(cx ? source.xmax + 0.5f : source.xmin - 0.5f,
                                          cy ? source.ymax + 0.5f : source.ymin - 0.5f,
                                          1);
      BLI_rctf_do_minmax_v(&bounds, float2(p.x, p.y));
    }
  }
  if (r_pivot) {
    *r_pivot = float2((bounds.xmin + bounds.xmax) * 0.5f, (bounds.ymin + bounds.ymax) * 0.5f);
  }
  return {int(std::floor(bounds.xmin)) - 1,
          int(std::ceil(bounds.xmax)) + 1,
          int(std::floor(bounds.ymin)) - 1,
          int(std::ceil(bounds.ymax)) + 1};
}

static bool transform_poll(bContext *C)
{
  if (!paint_edit_poll(C)) {
    return false;
  }
  bNode *node = paint_edit_node(C);
  if (const char *reason = BKE_tex_paint_stroke_block_reason(*node)) {
    CTX_wm_operator_poll_msg_set(C, reason);
    return false;
  }
  if (BKE_tex_paint_stroke_locks_alpha(*node)) {
    CTX_wm_operator_poll_msg_set(C, "Unlock the layer alpha before transforming pixels");
    return false;
  }
  return true;
}

static PaintTransform *transform_begin(bContext *C, wmOperator *op)
{
  auto *state = MEM_new<PaintTransform>(__func__);
  state->node = paint_edit_node(C);
  NodeTexPaint &data = storage(*state->node);
  state->target = BKE_tex_paint_paint_target_ibuf(data);
  if (!state->target) {
    BKE_report(op->reports, RPT_WARNING, "Select a visible, unlocked pixel layer or layer mask");
    MEM_delete(state);
    return nullptr;
  }
  /* Paint effects own coverage just like color layers. Only a plain scalar mask
   * fills the vacated area with opaque black when its pixels are moved. */
  state->mask = data.paint_target == TEXPAINT_TARGET_MASK &&
                !BKE_tex_paint_layer_active(data)->mask_effects_active;
  state->original = BKE_tex_paint_pixels_read(*state->target);
  state->preview = state->original;
  state->selection = Array<float>(BKE_tex_paint_selection(data));
  state->preview_selection = state->selection;
  const TexPaintLayer &layer = *BKE_tex_paint_layer_active(data);
  state->reuse_source = data.selection_transform_source &&
                        data.selection_transform_size == int64_t(data.width) * data.height &&
                        data.selection_transform_layer_uid == layer.uid &&
                        data.selection_transform_target == data.paint_target &&
                        data.selection_transform_hash == BKE_tex_paint_pixels_hash(*state->target);
  if (state->reuse_source) {
    state->source = Array<float4>(
        Span<float4>(reinterpret_cast<const float4 *>(data.selection_transform_source),
                     data.selection_transform_size));
    if (data.selection_transform_mask) {
      state->source_selection = Array<float>(
          Span<float>(data.selection_transform_mask, data.selection_transform_size));
    }
    state->transform_before = float3x3(data.selection_transform);
  }
  else {
    state->source = state->original;
    state->source_selection = state->selection;
  }
  state->transform = state->transform_before;
  BLI_rcti_init_minmax(&state->source_bounds);
  for (int y = 0; y < data.height; y++) {
    for (int x = 0; x < data.width; x++) {
      if (state->source_selection.is_empty() || state->source_selection[y * data.width + x] > 0) {
        BLI_rcti_do_minmax_v(&state->source_bounds, int2(x, y));
      }
    }
  }
  if (state->source_bounds.xmin > state->source_bounds.xmax) {
    BKE_report(op->reports, RPT_INFO, "The selection is empty");
    MEM_delete(state);
    return nullptr;
  }
  state->source_bounds.xmax++;
  state->source_bounds.ymax++;
  state->previous_bounds = transformed_bounds(
      state->source_bounds, state->transform_before, &state->pivot);
  initNumInput(&state->numeric);
  state->mode = RNA_enum_get(op->ptr, "mode");
  op->customdata = state;
  return state;
}

static void transform_notify(bContext *C, PaintTransform &state, const rcti &bounds)
{
  BKE_tex_paint_stroke_update(*state.node, state.target, bounds);
  DEG_id_tag_update(state.node->id, ID_RECALC_EDITORS);
  WM_event_add_notifier(C, NC_IMAGE | NA_EDITED, state.node->id);
}

static void transform_apply(bContext *C, wmOperator *op)
{
  auto &s = *static_cast<PaintTransform *>(op->customdata);
  NodeTexPaint &data = storage(*s.node);
  float2 translation, scale;
  RNA_float_get_array(op->ptr, "translation", translation);
  RNA_float_get_array(op->ptr, "scale", scale);
  const float angle = RNA_float_get(op->ptr, "angle");
  for (int axis = 0; axis < 2; axis++) {
    if (std::abs(scale[axis]) < 0.001f) {
      scale[axis] = std::copysign(0.001f, scale[axis]);
    }
  }
  float cosine = std::cos(angle), sine = std::sin(angle);
  /* Keep pixel-aligned quarter turns exact when composing affine matrices. */
  cosine = std::abs(cosine) < 1e-7f ? 0.0f : cosine;
  sine = std::abs(sine) < 1e-7f ? 0.0f : sine;
  float3x3 delta = float3x3::identity();
  delta[0] = float3(cosine * scale.x, sine * scale.x, 0);
  delta[1] = float3(-sine * scale.y, cosine * scale.y, 0);
  delta[2] = float3(s.pivot.x + translation.x, s.pivot.y + translation.y, 1) -
             delta * float3(s.pivot.x, s.pivot.y, 0);
  s.transform = delta * s.transform_before;
  const rcti bounds = transformed_bounds(s.source_bounds, s.transform);
  rcti update = bounds;
  BLI_rcti_union(&update, &s.previous_bounds);
  update.xmin = std::clamp(update.xmin, 0, data.width);
  update.xmax = std::clamp(update.xmax, 0, data.width);
  update.ymin = std::clamp(update.ymin, 0, data.height);
  update.ymax = std::clamp(update.ymax, 0, data.height);
  BKE_tex_paint_pixels_transform_affine(s.source,
                                        s.source_selection,
                                        data.width,
                                        data.height,
                                        s.transform,
                                        s.preview,
                                        s.preview_selection,
                                        update);
  if (s.mask) {
    for (int y = update.ymin; y < update.ymax; y++) {
      for (int x = update.xmin; x < update.xmax; x++) {
        s.preview[y * data.width + x].w = 1;
      }
    }
  }
  BKE_tex_paint_pixels_write(*s.target, s.preview, update);
  if (!s.selection.is_empty()) {
    BKE_tex_paint_selection_set(data, s.preview_selection, true);
  }
  s.previous_bounds = bounds;
  s.dirty = true;
  transform_notify(C, s, update);
}

static void transform_finish(bContext *C, wmOperator *op, const bool cancel)
{
  auto *s = static_cast<PaintTransform *>(op->customdata);
  if (!s) {
    return;
  }
  NodeTexPaint &data = storage(*s->node);
  if (cancel && s->dirty) {
    const rcti full = {0, data.width, 0, data.height};
    BKE_tex_paint_pixels_write(*s->target, s->original, full);
    if (!s->selection.is_empty()) {
      BKE_tex_paint_selection_set(data, s->selection, true);
    }
    transform_notify(C, *s, full);
  }
  if (!cancel) {
    if (!s->reuse_source) {
      BKE_tex_paint_selection_transform_store(data, s->source, s->source_selection);
    }
    std::copy_n(s->transform.base_ptr(), 9, &data.selection_transform[0][0]);
    data.selection_transform_layer_uid = BKE_tex_paint_layer_active(data)->uid;
    data.selection_transform_target = data.paint_target;
    data.selection_transform_hash = BKE_tex_paint_pixels_hash(*s->target);
    paint_edit_tag_undo(C, *s->node);
  }
  if (ScrArea *area = CTX_wm_area(C)) {
    ED_area_status_text(area, nullptr);
  }
  MEM_delete(s);
  op->customdata = nullptr;
}

static wmOperatorStatus transform_exec(bContext *C, wmOperator *op)
{
  if (!transform_begin(C, op)) {
    return OPERATOR_CANCELLED;
  }
  transform_apply(C, op);
  transform_finish(C, op, false);
  return OPERATOR_FINISHED;
}

static wmOperatorStatus transform_invoke(bContext *C, wmOperator *op, const wmEvent *event)
{
  /* A new gesture starts from the current placement, not the previous operation's values.
   * Keep those values available to exec for repeat/redo. */
  RNA_float_set_array(op->ptr, "translation", float2(0));
  RNA_float_set_array(op->ptr, "scale", float2(1));
  RNA_float_set(op->ptr, "angle", 0);
  PaintTransform *s = transform_begin(C, op);
  if (!s) {
    return OPERATOR_CANCELLED;
  }
  s->mouse_start = mouse_pixel(C, event->mval[0], event->mval[1], storage(*s->node));
  s->confirm_on_release = event->type == LEFTMOUSE;
  ED_area_status_text(
      CTX_wm_area(C),
      "G Move | R Rotate | S Scale | X/Y Axis | Type a value | Enter Confirm | Esc Cancel");
  WM_event_add_modal_handler(C, op);
  return OPERATOR_RUNNING_MODAL;
}

static wmOperatorStatus transform_modal(bContext *C, wmOperator *op, const wmEvent *event)
{
  auto &s = *static_cast<PaintTransform *>(op->customdata);
  if (ELEM(event->type, EVT_ESCKEY, RIGHTMOUSE) && event->val == KM_PRESS) {
    transform_finish(C, op, true);
    return OPERATOR_CANCELLED;
  }
  if ((ELEM(event->type, EVT_RETKEY, EVT_PADENTER, LEFTMOUSE) && event->val == KM_PRESS) ||
      (s.confirm_on_release && event->type == LEFTMOUSE && event->val == KM_RELEASE))
  {
    const bool changed = s.dirty;
    transform_finish(C, op, !changed);
    return changed ? OPERATOR_FINISHED : OPERATOR_CANCELLED;
  }
  if (event->val == KM_PRESS && ELEM(event->type, EVT_GKEY, EVT_RKEY, EVT_SKEY)) {
    const int mode = event->type == EVT_GKEY ? 0 : event->type == EVT_RKEY ? 1 : 2;
    if (mode != s.mode) {
      s.mode = mode;
      s.axis = -1;
      RNA_enum_set(op->ptr, "mode", s.mode);
      RNA_float_set_array(op->ptr, "translation", float2(0));
      RNA_float_set_array(op->ptr, "scale", float2(1));
      RNA_float_set(op->ptr, "angle", 0);
      initNumInput(&s.numeric);
      transform_apply(C, op);
    }
    return OPERATOR_RUNNING_MODAL;
  }
  if (event->val == KM_PRESS && ELEM(event->type, EVT_XKEY, EVT_YKEY)) {
    const int axis = event->type == EVT_XKEY ? 0 : 1;
    s.axis = s.axis == axis ? -1 : axis;
  }
  const bool numeric = handleNumInput(C, &s.numeric, event);
  if (event->type != MOUSEMOVE && !numeric && !ELEM(event->type, EVT_XKEY, EVT_YKEY)) {
    return OPERATOR_RUNNING_MODAL;
  }
  const float2 mouse = mouse_pixel(C, event->mval[0], event->mval[1], storage(*s.node));
  float values[3] = {0, 0, 0};
  if (s.mode == 0) {
    const float2 delta = mouse - s.mouse_start;
    values[0] = s.axis == 1 ? delta.y : delta.x;
    applyNumInput(&s.numeric, values);
    const float2 translation = s.axis == 0             ? float2(values[0], 0) :
                               s.axis == 1             ? float2(0, values[0]) :
                               hasNumInput(&s.numeric) ? float2(values[0], 0) :
                                                         delta;
    RNA_float_set_array(op->ptr, "translation", translation);
  }
  else if (s.mode == 1) {
    const float2 a = s.mouse_start - s.pivot, b = mouse - s.pivot;
    values[0] = (std::atan2(b.y, b.x) - std::atan2(a.y, a.x)) * 180.0f / float(M_PI);
    applyNumInput(&s.numeric, values);
    RNA_float_set(op->ptr, "angle", values[0] * float(M_PI) / 180.0f);
  }
  else {
    values[0] = math::length(mouse - s.pivot) /
                std::max(1.0f, math::length(s.mouse_start - s.pivot));
    applyNumInput(&s.numeric, values);
    const float2 scale = s.axis == 0 ? float2(values[0], 1) :
                         s.axis == 1 ? float2(1, values[0]) :
                                       float2(values[0]);
    RNA_float_set_array(op->ptr, "scale", scale);
  }
  transform_apply(C, op);
  float2 status_trans = float2(0);
  RNA_float_get_array(op->ptr, "translation", status_trans);
  const std::string value_str = (s.mode == 0 && s.axis == -1 && !hasNumInput(&s.numeric)) ?
                                    fmt::format("({:.1f}, {:.1f})", status_trans.x, status_trans.y) :
                                    fmt::format("{:.3f}{}",
                                                values[0],
                                                s.axis == 0 ? " X" :
                                                s.axis == 1 ? " Y" :
                                                              "");
  const char *mode_name = s.mode == 0 ? "Move (px)" : s.mode == 1 ? "Rotate (degrees)" : "Scale";
  const std::string status = fmt::format(
      "{}: {} | G Move | R Rotate | S Scale | X/Y Axis | Enter Confirm | Esc Cancel",
      mode_name,
      value_str);
  ED_area_status_text(CTX_wm_area(C), status.c_str());
  return OPERATOR_RUNNING_MODAL;
}

static void transform_cancel(bContext *C, wmOperator *op)
{
  transform_finish(C, op, true);
}

void IMAGE_OT_paint_transform(wmOperatorType *ot)
{
  static const EnumPropertyItem modes[] = {{0, "TRANSLATE", 0, "Move", "Move selected pixels"},
                                           {1, "ROTATE", 0, "Rotate", "Rotate selected pixels"},
                                           {2, "SCALE", 0, "Scale", "Scale selected pixels"},
                                           {0, nullptr, 0, nullptr, nullptr}};
  static const float unit_scale[2] = {1, 1};
  ot->name = "Transform Image Pixels";
  ot->idname = "IMAGE_OT_paint_transform";
  ot->description = "Transform the active layer or mask through the shared native paint engine";
  ot->poll = transform_poll;
  ot->exec = transform_exec;
  ot->invoke = transform_invoke;
  ot->modal = transform_modal;
  ot->cancel = transform_cancel;
  ot->flag = OPTYPE_UNDO | OPTYPE_REGISTER | OPTYPE_BLOCKING;
  RNA_def_enum(ot->srna, "mode", modes, 0, "Mode", "");
  RNA_def_float_vector(
      ot->srna, "translation", 2, nullptr, -1e6f, 1e6f, "Translation", "Pixels", -10000, 10000);
  RNA_def_float(ot->srna, "angle", 0, -1000, 1000, "Angle", "Radians", -float(M_PI), float(M_PI));
  RNA_def_float_vector(ot->srna, "scale", 2, unit_scale, -1000, 1000, "Scale", "", -10, 10);
}

void image_paint_edit_draw(const bContext *C, ARegion *region)
{
  SpaceImage *sima = CTX_wm_space_image(C);
  bNode *node = paint_edit_node(C);
  if (!node || !sima || !ELEM(sima->mode, SI_MODE_PAINT, SI_MODE_VIEW)) {
    return;
  }
  NodeTexPaint &data = storage(*node);
  const Span<int2> contours = BKE_tex_paint_selection_contours(data);
  if (contours.is_empty()) {
    return;
  }
  const uint pos = GPU_vertformat_attr_add(
      immVertexFormat(), "pos", gpu::VertAttrType::SFLOAT_32_32);
  immBindBuiltinProgram(GPU_SHADER_3D_LINE_DASHED_UNIFORM_COLOR);
  float viewport[4];
  GPU_viewport_size_get_f(viewport);
  immUniform2f("viewport_size", viewport[2] / UI_SCALE_FAC, viewport[3] / UI_SCALE_FAC);
  immUniform1i("colors_len", 2);
  immUniform4f("color", 0, 0, 0, 1);
  immUniform4f("color2", 1, 1, 1, 1);
  immUniform1f("dash_width", 6);
  immUniform1f("udash_factor", 0.5f);
  immBegin(GPU_PRIM_LINES, contours.size());
  for (const int2 point : contours) {
    int x, y;
    ui::view2d_view_to_region(
        &region->v2d, float(point.x) / data.width, float(point.y) / data.height, &x, &y);
    immVertex2f(pos, x, y);
  }
  immEnd();
  immUnbindProgram();
}

}  // namespace blender
