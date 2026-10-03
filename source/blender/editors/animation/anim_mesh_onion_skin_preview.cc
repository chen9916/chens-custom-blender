/* SPDX-FileCopyrightText: 2026 Blender Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

/** \file
 * \ingroup edanimation
 */

#include "ED_mesh_onion_skin.hh"
#include "ED_mesh_onion_skin_preview.hh"

#include <cstdio>
#include <cstdlib>

#include "ANIM_action.hh"
#include "ANIM_action_legacy.hh"
#include "ANIM_animdata.hh"
#include "ANIM_keyframing.hh"

#include "BKE_action.hh"
#include "BKE_anim_data.hh"
#include "BKE_animsys.hh"
#include "BKE_context.hh"
#include "BKE_fcurve.hh"
#include "BKE_layer.hh"
#include "BKE_lib_id.hh"
#include "BKE_lib_query.hh"
#include "BKE_main.hh"
#include "BKE_pose.hh"

#include "BLI_listbase.hh"
#include "BLI_math_vector_c.hh"
#include "BLI_set.hh"
#include "BLI_threads.hh"

#include "DNA_anim_types.h"
#include "DNA_object_types.h"
#include "DNA_scene_types.h"
#include "DNA_screen_types.h"
#include "DNA_view3d_types.h"

#include "RNA_access.hh"
#include "RNA_prototypes.hh"

#include "WM_api.hh"
#include "WM_types.hh"

namespace blender::ed::mesh_onion_skin {

PreparedPreview::PreparedPreview() = default;
PreparedPreview::~PreparedPreview()
{
  if (action_template) {
    BKE_id_free(nullptr, action_template);
  }
}

static void preview_debug_channelbag(const animrig::Channelbag &bag, const char *label)
{
  if (!std::getenv("BLENDER_MESH_ONION_SKIN_PREVIEW_DIAGNOSTICS")) {
    return;
  }
  uint64_t hash = 1469598103934665603ULL;
  auto mix = [&](const void *data, const size_t size) {
    const auto *bytes = static_cast<const unsigned char *>(data);
    for (size_t i = 0; i < size; i++) {
      hash = (hash ^ bytes[i]) * 1099511628211ULL;
    }
  };
  int keys = 0;
  for (const FCurve *curve : bag.fcurves()) {
    const StringRef path = curve->rna_path();
    mix(path.data(), path.size());
    mix(&curve->array_index, sizeof(curve->array_index));
    mix(&curve->totvert, sizeof(curve->totvert));
    for (int i = 0; curve->bezt && i < curve->totvert; i++) {
      mix(curve->bezt[i].vec, sizeof(curve->bezt[i].vec));
      keys++;
    }
  }
  std::fprintf(stderr,
               "mesh_onion_preview_action label=%s hash=%016llx curves=%lld keys=%d\n",
               label,
               static_cast<unsigned long long>(hash),
               static_cast<long long>(bag.fcurves().size()),
               keys);
}

void preview_debug_action(const Object &rig, const char *label)
{
  if (!std::getenv("BLENDER_MESH_ONION_SKIN_PREVIEW_DIAGNOSTICS") || !rig.adt || !rig.adt->action)
  {
    return;
  }
  if (const animrig::Channelbag *bag = animrig::channelbag_for_action_slot(rig.adt->action->wrap(),
                                                                           rig.adt->slot_handle))
  {
    preview_debug_channelbag(*bag, label);
  }
}

bool preview_enabled(const bContext &C)
{
  const View3D *v3d = CTX_wm_view3d(&C);
  return v3d && !(v3d->flag2 & V3D_HIDE_OVERLAYS) &&
         (v3d->overlay.flag & V3D_OVERLAY_MESH_ONION_SKIN);
}

bool preview_enabled_for_rig(const bContext &C, Scene &scene, Object &rig)
{
  if (preview_enabled(C) || is_requested(scene, rig)) {
    return true;
  }
  wmWindowManager *wm = CTX_wm_manager(&C);
  Main *bmain = CTX_data_main(&C);
  if (!wm || !bmain || !(rig.mode & OB_MODE_POSE)) {
    return false;
  }
  /* Cold activation can precede the first draw/request in another viewport. Normally the cheap
   * registered-job check above handles this, including dragging from an overlay-disabled view. */
  for (wmWindow &window : wm->windows) {
    if (WM_window_get_active_scene(&window) != &scene) {
      continue;
    }
    ViewLayer *view_layer = WM_window_get_active_view_layer(&window);
    BKE_view_layer_synced_ensure(*bmain, &scene, view_layer);
    if (BKE_view_layer_active_object_get(view_layer) != &rig) {
      continue;
    }
    const bScreen *screen = WM_window_get_active_screen(&window);
    if (!screen) {
      continue;
    }
    for (const ScrArea &area : screen->areabase) {
      if (area.spacetype != SPACE_VIEW3D) {
        continue;
      }
      const View3D &view = *reinterpret_cast<const View3D *>(area.spacedata.first());
      if (!(view.flag2 & V3D_HIDE_OVERLAYS) && (view.overlay.flag & V3D_OVERLAY_MESH_ONION_SKIN)) {
        return true;
      }
    }
  }
  return false;
}

static bool apply_bone_inputs(Object &rig, const Span<PoseBoneInput> inputs)
{
  for (const PoseBoneInput &input : inputs) {
    bPoseChannel *bone = BKE_pose_channel_find_name(rig.pose, input.name.c_str());
    if (!bone) {
      return false;
    }
    copy_v3_v3(bone->loc, input.location);
    copy_v3_v3(bone->eul, input.euler);
    copy_v4_v4(bone->quat, input.quaternion);
    copy_v3_v3(bone->rotAxis, input.axis);
    bone->rotAngle = input.angle;
    copy_v3_v3(bone->scale, input.scale);
    bone->rotmode = eRotationModes(input.rotation_mode);
  }
  return true;
}

static constexpr int staging_copy_flags = LIB_ID_CREATE_LOCALIZE | LIB_ID_COPY_NO_PREVIEW |
                                          LIB_ID_COPY_NO_LIB_OVERRIDE;

static bool strips_have_drivers(const ListBaseT<NlaStrip> &strips)
{
  for (const NlaStrip &strip : strips) {
    for (const FCurve &curve : strip.fcurves) {
      if (curve.driver) {
        return true;
      }
    }
    if (strips_have_drivers(strip.strips)) {
      return true;
    }
  }
  return false;
}

/** Native NLA key remapping evaluates these curves with no dependency graph. */
static bool staging_animation_supported(ID &id)
{
  if (id.id_type() == ID_AC) {
    for (const FCurve *curve : animrig::legacy::fcurves_all(id_cast<bAction *>(&id))) {
      if (curve->driver) {
        return false;
      }
    }
  }
  if (const AnimData *adt = BKE_animdata_from_id(&id)) {
    for (const NlaTrack &track : adt->nla_tracks) {
      if (strips_have_drivers(track.strips)) {
        return false;
      }
    }
  }
  return true;
}

struct StagingWorkspace {
  Main *main = BKE_main_new();
  Map<ID *, ID *> copies;
  Set<ID *> actions_with_id_links;
  bool added_to_main = false;

  ~StagingWorkspace()
  {
    if (!added_to_main) {
      for (ID *copy : copies.values()) {
        BKE_id_free(nullptr, copy);
      }
    }
    BKE_main_free(main);
  }

  bool add(ID &source)
  {
    if (copies.contains(&source)) {
      return true;
    }
    if (!staging_animation_supported(source)) {
      return false;
    }
    ID *copy = BKE_id_copy_ex(nullptr, &source, nullptr, staging_copy_flags);
    if (!copy) {
      return false;
    }
    copy->lib = nullptr;
    copy->newid = nullptr;
    copy->orig_id = nullptr;
    /* Related-Action discovery uses real user counts to decide whether object data is shared. */
    copy->us = source.us;
    copies.add(&source, copy);
    return true;
  }
};

std::shared_ptr<const PreparedPreview> preview_prepare(Main &bmain,
                                                       Scene & /*scene*/,
                                                       Object &source_rig,
                                                       const PosePreviewInput &input)
{
  BLI_assert(BLI_thread_is_main());
  const AnimData *source_adt = BKE_animdata_from_id(&source_rig.id);
  if (input.object_uid != SessionUID{source_rig.id.session_uid} || !input.supported ||
      !source_rig.pose || (input.key_flags & INSERTKEY_MATRIX))
  {
    return nullptr;
  }

  /* Native keying may reuse an Action on related object data. Copy that data as well, so
   * id_action_ensure() can never select a source Action through rig->data. */
  StagingWorkspace workspace;
  const Vector<ID *> related = animrig::find_related_ids(bmain, source_rig.id);
  bAction *source_target = source_adt ? source_adt->action : nullptr;
  for (ID *id : related) {
    if (!workspace.add(*id)) {
      return nullptr;
    }
    if (!source_target) {
      const AnimData *adt = BKE_animdata_from_id(id);
      if (adt && adt->action && BKE_id_is_editable(&bmain, &adt->action->id)) {
        source_target = adt->action;
      }
    }
  }
  if (source_rig.data && !workspace.add(*source_rig.data)) {
    return nullptr;
  }
  if (input.insert_keys && source_target &&
      (ID_IS_LINKED(source_target) || ID_IS_OVERRIDE_LIBRARY(source_target)))
  {
    return nullptr;
  }

  /* Include every Action reached through AnimData, including tmpact and repeated strip
   * references. One map preserves shared identity in tweak-mode inverse blending. */
  Vector<ID *> actions;
  for (ID *id : workspace.copies.keys()) {
    BKE_library_foreach_ID_link(
        &bmain,
        id,
        [&](LibraryIDLinkCallbackData *data) {
          ID *target = *data->id_pointer;
          if (target && target->id_type() == ID_AC) {
            actions.append_non_duplicates(target);
          }
          return IDWALK_RET_NOP;
        },
        nullptr,
        IDWALK_READONLY);
  }
  for (ID *action : actions) {
    if (!workspace.add(*action)) {
      return nullptr;
    }
  }
  Set<ID *> owned;
  for (ID *copy : workspace.copies.values()) {
    owned.add(copy);
  }
  for (ID *copy : workspace.copies.values()) {
    BKE_library_foreach_ID_link(
        workspace.main,
        copy,
        [&](LibraryIDLinkCallbackData *data) {
          ID *target = *data->id_pointer;
          if (target && copy->id_type() == ID_AC) {
            workspace.actions_with_id_links.add(copy);
          }
          if (ID **mapped = workspace.copies.lookup_ptr(*data->id_pointer)) {
            *data->id_pointer = *mapped;
          }
          else if (target && !owned.contains(target)) {
            /* Staging evaluates authored RNA/NLA curves only. External driver, constraint,
             * material and custom-shape links are unnecessary and must not become counted
             * references into the source Main when these copies are registered below. */
            *data->id_pointer = nullptr;
          }
          return IDWALK_RET_NOP;
        },
        nullptr,
        IDWALK_NOP);
  }
  /* Register only after every retained link is private. main_add establishes user counts. */
  for (ID *copy : workspace.copies.values()) {
    BKE_libblock_management_main_add(workspace.main, copy);
  }
  workspace.added_to_main = true;
  for (const auto item : workspace.copies.items()) {
    /* Native related-Action discovery depends on source sharing, not capsule membership. */
    item.value->us = item.key->us;
  }
  Object *rig = id_cast<Object *>(workspace.copies.lookup(&source_rig.id));
  if ((!source_adt || !source_adt->action) && !source_target) {
    /* Localization must not make a linked related Action newly eligible for reuse. */
    for (ID *source : related) {
      if (source == &source_rig.id) {
        continue;
      }
      const AnimData *source_data = BKE_animdata_from_id(source);
      if (source_data && source_data->action &&
          !BKE_id_is_editable(&bmain, &source_data->action->id))
      {
        BKE_animdata_from_id(workspace.copies.lookup(source))->action = nullptr;
      }
    }
  }

  auto packet = std::make_shared<PreparedPreview>();
  packet->object_uid = input.object_uid;
  packet->transaction_id = input.transaction_id;
  packet->bones = input.bones;
  bool success = apply_bone_inputs(*rig, input.bones);
  if (success && input.insert_keys) {
    /* The native inserter reads user preferences. Keep this call on the main thread; exporting
     * completed curves also preserves native handles, needed/available/replace and cycle rules. */
    const AnimationEvalContext eval_context = BKE_animsys_eval_context_construct(nullptr,
                                                                                 input.frame);
    for (const PoseBoneInput &bone : input.bones) {
      bPoseChannel *pchan = BKE_pose_channel_find_name(rig->pose, bone.name.c_str());
      PointerRNA ptr = RNA_pointer_create_discrete(&rig->id, RNA_PoseBone, pchan);
      animrig::insert_keyframes(workspace.main,
                                &ptr,
                                std::nullopt,
                                bone.key_paths,
                                input.frame,
                                eval_context,
                                eBezTriple_KeyframeType(input.keyframe_type),
                                eInsertKeyFlags(input.key_flags));
    }
  }
  bAction *action = rig->adt ? rig->adt->action : nullptr;
  packet->assign_action = action != nullptr;
  packet->slot_handle = rig->adt ? rig->adt->slot_handle : 0;
  /* When keying did not assign an Action, also restore any related Action that a preceding
   * proposal could have modified. This includes removing a provisional new slot. */
  if (!action && source_target) {
    action = id_cast<bAction *>(workspace.copies.lookup(&source_target->id));
  }
  ID *original_action = nullptr;
  if (action) {
    for (const auto item : workspace.copies.items()) {
      if (item.value == &action->id) {
        original_action = item.key;
        packet->action_uid = SessionUID{item.key->session_uid};
        break;
      }
    }
  }
  if (success && action) {
    const animrig::Channelbag *bag = animrig::channelbag_for_action_slot(action->wrap(),
                                                                         packet->slot_handle);
    if (bag) {
      /* Action curves ordinarily have no drivers. Reject any unexpected source ID references
       * instead of carrying them into the worker through a copied Channelbag. */
      for (const FCurve *curve : bag->fcurves()) {
        if (curve->driver) {
          success = false;
          break;
        }
      }
      if (success) {
        packet->channelbag = std::make_unique<animrig::Channelbag>(*bag);
        for (bActionGroup *group : packet->channelbag->channel_groups()) {
          /* These legacy list links are irrelevant to a layered slot. Make ownership explicit
           * even if an unusual source action contains stale legacy bookkeeping. */
          group->next = group->prev = nullptr;
          group->channels.clear_no_delete();
        }
      }
    }
    /* Existing assigned slots use the small curve packet. New/related Actions and new slots
     * need an owned structural template; keep ID pointers out of that cross-thread packet. */
    const bool needs_template =
        !source_adt || !source_adt->action || !original_action ||
        !id_cast<bAction *>(original_action)->wrap().slot_for_handle(packet->slot_handle) ||
        !animrig::channelbag_for_action_slot(id_cast<bAction *>(original_action)->wrap(),
                                             packet->slot_handle);
    if (needs_template) {
      bool has_id_links = workspace.actions_with_id_links.contains(&action->id);
      BKE_library_foreach_ID_link(
          workspace.main,
          &action->id,
          [&](LibraryIDLinkCallbackData *data) {
            has_id_links |= *data->id_pointer != nullptr;
            return IDWALK_RET_NOP;
          },
          nullptr,
          IDWALK_READONLY);
      if (has_id_links) {
        success = false;
      }
      else {
        packet->action_template = id_cast<bAction *>(
            BKE_id_copy_ex(nullptr, &action->id, nullptr, staging_copy_flags));
        success &= packet->action_template != nullptr;
      }
    }
  }
  if (success && packet->channelbag) {
    preview_debug_channelbag(*packet->channelbag, "prepared");
  }
  return success ? packet : nullptr;
}

bool preview_apply(const PreparedPreview &packet,
                   Main &private_main,
                   Map<SessionUID, Object *> &copied_objects,
                   Map<SessionUID, bAction *> &copied_actions,
                   PreviewApplyState &state,
                   bool *r_structure_changed)
{
  if (r_structure_changed) {
    *r_structure_changed = false;
  }
  Object *rig = copied_objects.lookup_default(packet.object_uid, nullptr);
  if (!rig || !rig->pose || !apply_bone_inputs(*rig, packet.bones)) {
    return false;
  }
  AnimData *adt = BKE_animdata_ensure_id(&rig->id);
  bAction *action = packet.action_uid.is_generated() ?
                        copied_actions.lookup_default(packet.action_uid, nullptr) :
                        state.created_action;
  if (packet.action_uid.is_generated() && !action) {
    return false;
  }
  bool applied_template = false;
  /* Once a provisional Action/slot has been created, further mouse events normally change
   * only its curves. Avoid rebuilding graph relations for each proposal in NLA-only rigs. */
  bool template_matches = packet.action_template && action && packet.channelbag &&
                          action->wrap().slots().size() ==
                              packet.action_template->wrap().slots().size() &&
                          action->wrap().layers().size() == 1 &&
                          packet.action_template->wrap().layers().size() == 1 &&
                          action->wrap().layer(0)->strips().size() == 1 &&
                          packet.action_template->wrap().layer(0)->strips().size() == 1 &&
                          animrig::channelbag_for_action_slot(action->wrap(), packet.slot_handle);
  if (template_matches) {
    for (const animrig::Slot *slot : packet.action_template->wrap().slots()) {
      const animrig::Slot *current = action->wrap().slot_for_handle(slot->handle);
      if (!current || current->idtype != slot->idtype ||
          StringRef(current->identifier) != StringRef(slot->identifier))
      {
        template_matches = false;
        break;
      }
    }
  }
  if (packet.action_template && !template_matches) {
    bAction *replacement = id_cast<bAction *>(
        BKE_id_copy_ex(nullptr, &packet.action_template->id, nullptr, staging_copy_flags));
    if (!replacement) {
      return false;
    }
    if (action) {
      BKE_lib_id_swap(&private_main, &action->id, &replacement->id, false, 0);
      BKE_id_free(nullptr, replacement);
    }
    else {
      action = replacement;
      BKE_libblock_management_main_add(&private_main, &action->id);
      state.created_action = action;
    }
    if (r_structure_changed) {
      *r_structure_changed = true;
    }
    applied_template = true;
  }
  bAction *assigned_action = packet.assign_action ? action : nullptr;
  if (packet.assign_action && !assigned_action) {
    return false;
  }
  if (adt->action != assigned_action || adt->slot_handle != packet.slot_handle) {
    /* The private Main owns all IDs independently; these assignments deliberately do not
     * update user caches or exit tweak mode. Shared NLA strip references retain their identity. */
    adt->action = assigned_action;
    adt->slot_handle = packet.slot_handle;
    if (r_structure_changed) {
      *r_structure_changed = true;
    }
  }
  if (!packet.channelbag || applied_template) {
    return true;
  }
  animrig::Channelbag *bag = animrig::channelbag_for_action_slot(action->wrap(),
                                                                 packet.slot_handle);
  if (!bag) {
    return false;
  }
  if (r_structure_changed) {
    *r_structure_changed |= bag->fcurves().size() != packet.channelbag->fcurves().size();
    if (!*r_structure_changed) {
      for (const int index : bag->fcurves().index_range()) {
        const FCurve &previous = *bag->fcurve(index);
        const FCurve &next = *packet.channelbag->fcurve(index);
        if (previous.array_index != next.array_index || previous.rna_path() != next.rna_path()) {
          *r_structure_changed = true;
          break;
        }
      }
    }
  }
  /* Each packet contains the baseline plus this proposal, including when auto-key is off.
   * Reset the whole private bag so rejected/needed keys never retain an earlier proposal. */
  /* Bulk replacement is linear; individual remove/append operations reallocate the curve array
   * and restore group invariants repeatedly. The private graph is idle during this replacement
   * and its relations are rebuilt by the evaluator if the animated channel structure changed. */
  std::destroy_at(bag);
  std::construct_at(bag, *packet.channelbag);
  return true;
}

}  // namespace blender::ed::mesh_onion_skin
