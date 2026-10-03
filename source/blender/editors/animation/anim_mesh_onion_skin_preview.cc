/* SPDX-FileCopyrightText: 2026 Blender Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

/** \file
 * \ingroup edanimation
 */

#include "ED_mesh_onion_skin.hh"
#include "ED_mesh_onion_skin_preview.hh"

#include "ANIM_action.hh"
#include "ANIM_keyframing.hh"

#include "BKE_action.hh"
#include "BKE_anim_data.hh"
#include "BKE_animsys.hh"
#include "BKE_context.hh"
#include "BKE_fcurve.hh"
#include "BKE_layer.hh"
#include "BKE_lib_id.hh"
#include "BKE_main.hh"
#include "BKE_pose.hh"

#include "BLI_listbase.hh"
#include "BLI_math_vector_c.hh"
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
PreparedPreview::~PreparedPreview() = default;

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

std::shared_ptr<const PreparedPreview> preview_prepare(Main & /*bmain*/,
                                                       Scene & /*scene*/,
                                                       Object &source_rig,
                                                       const PosePreviewInput &input)
{
  BLI_assert(BLI_thread_is_main());
  const AnimData *source_adt = BKE_animdata_from_id(&source_rig.id);
  if (input.object_uid != SessionUID{source_rig.id.session_uid} || !input.supported ||
      !source_rig.pose || !source_adt || !source_adt->action ||
      source_adt->action->wrap().layers().size() != 1 ||
      source_adt->action->wrap().layer(0)->strips().size() != 1 ||
      !source_adt->action->wrap().slot_for_handle(source_adt->slot_handle) ||
      !BLI_listbase_is_empty(&source_adt->nla_tracks) || (source_adt->flag & ADT_NLA_EDIT_ON) ||
      source_adt->act_blendmode != NLASTRIP_MODE_REPLACE || source_adt->act_influence != 1.0f ||
      (input.key_flags & INSERTKEY_MATRIX))
  {
    return nullptr;
  }

  /* Out-of-Main copies don't modify source ID user counts. They are used exclusively on the
   * main thread to read authored RNA properties, never to evaluate constraints or geometry. */
  constexpr int copy_flags = LIB_ID_CREATE_LOCALIZE | LIB_ID_COPY_NO_PREVIEW |
                             LIB_ID_COPY_NO_LIB_OVERRIDE;
  Object *rig = reinterpret_cast<Object *>(
      BKE_id_copy_ex(nullptr, &source_rig.id, nullptr, copy_flags));
  bAction *action = reinterpret_cast<bAction *>(
      BKE_id_copy_ex(nullptr, &source_adt->action->id, nullptr, copy_flags));
  if (!rig || !action) {
    if (rig) {
      BKE_id_free(nullptr, rig);
    }
    if (action) {
      BKE_id_free(nullptr, action);
    }
    return nullptr;
  }
  rig->adt->action = action;

  auto packet = std::make_shared<PreparedPreview>();
  packet->object_uid = input.object_uid;
  packet->transaction_id = input.transaction_id;
  packet->bones = input.bones;
  bool success = apply_bone_inputs(*rig, input.bones);
  Main *staging_main = BKE_main_new();
  if (success && input.insert_keys) {
    /* The native inserter reads user preferences. Keep this call on the main thread; exporting
     * completed curves also preserves native handles, needed/available/replace and cycle rules. */
    const AnimationEvalContext eval_context = BKE_animsys_eval_context_construct(nullptr,
                                                                                 input.frame);
    for (const PoseBoneInput &bone : input.bones) {
      bPoseChannel *pchan = BKE_pose_channel_find_name(rig->pose, bone.name.c_str());
      PointerRNA ptr = RNA_pointer_create_discrete(&rig->id, RNA_PoseBone, pchan);
      animrig::insert_keyframes(staging_main,
                                &ptr,
                                std::nullopt,
                                bone.key_paths,
                                input.frame,
                                eval_context,
                                eBezTriple_KeyframeType(input.keyframe_type),
                                eInsertKeyFlags(input.key_flags));
    }
  }
  if (success) {
    const animrig::Channelbag *bag = animrig::channelbag_for_action_slot(action->wrap(),
                                                                         rig->adt->slot_handle);
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
    else {
      success = false;
    }
  }
  BKE_main_free(staging_main);
  BKE_id_free(nullptr, rig);
  BKE_id_free(nullptr, action);
  return success ? packet : nullptr;
}

bool preview_apply(const PreparedPreview &packet,
                   Map<SessionUID, Object *> &copied_objects,
                   bool *r_structure_changed)
{
  if (r_structure_changed) {
    *r_structure_changed = false;
  }
  Object *rig = copied_objects.lookup_default(packet.object_uid, nullptr);
  if (!rig || !rig->pose || !rig->adt || !rig->adt->action || !packet.channelbag) {
    return false;
  }
  animrig::Channelbag *bag = animrig::channelbag_for_action_slot(rig->adt->action->wrap(),
                                                                 rig->adt->slot_handle);
  if (!bag || !apply_bone_inputs(*rig, packet.bones)) {
    return false;
  }
  if (r_structure_changed) {
    *r_structure_changed = bag->fcurves().size() != packet.channelbag->fcurves().size();
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
