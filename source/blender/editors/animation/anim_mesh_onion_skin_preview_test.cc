/* SPDX-FileCopyrightText: 2026 Blender Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

#include "ED_mesh_onion_skin_preview.hh"

#include "ANIM_action.hh"
#include "ANIM_keyframing.hh"
#include "ANIM_nla.hh"

#include "BKE_action.hh"
#include "BKE_anim_data.hh"
#include "BKE_animsys.hh"
#include "BKE_armature.hh"
#include "BKE_constraint.h"
#include "BKE_fcurve.hh"
#include "BKE_gtest_base.hh"
#include "BKE_lib_id.hh"
#include "BKE_main.hh"
#include "BKE_nla.hh"
#include "BKE_object.hh"
#include "BKE_scene.hh"

#include "BLI_listbase.hh"
#include "BLI_string.hh"

#include "DNA_armature_types.h"
#include "DNA_constraint_types.h"
#include "DNA_object_types.h"
#include "DNA_scene_types.h"

#include "MEM_guardedalloc.h"
#include "RNA_access.hh"
#include "RNA_prototypes.hh"
#include "testing/testing.h"

namespace blender::ed::mesh_onion_skin::tests {

class MeshOnionPreviewTest : public bke::BlenderGTestBase {
 protected:
  Main *bmain;
  Scene *scene;
  Object *rig;
  bArmature *armature;
  bPoseChannel *bone;

  void SetUp() override
  {
    bmain = BKE_main_new();
    scene = BKE_scene_add(bmain, "Scene");
    armature = BKE_armature_add(bmain, "Armature");
    Bone *data_bone = MEM_new<Bone>(__func__);
    STRNCPY(data_bone->name, "Bone");
    BLI_addtail(&armature->bonebase, data_bone);
    rig = BKE_object_add_only_object(bmain, OB_ARMATURE, "Rig");
    rig->data = &armature->id;
    BKE_pose_ensure(bmain, rig, armature, false);
    bone = BKE_pose_channel_find_name(rig->pose, "Bone");
    insert_location(1.0f, 0.0f);
    insert_location(10.0f, 10.0f);
  }

  void TearDown() override
  {
    BKE_main_free(bmain);
  }

  void insert_location(const float frame,
                       const float x,
                       const eInsertKeyFlags flags = INSERTKEY_NOFLAGS)
  {
    bone->loc[0] = x;
    PointerRNA ptr = RNA_pointer_create_discrete(&rig->id, RNA_PoseBone, bone);
    const AnimationEvalContext context = BKE_animsys_eval_context_construct(nullptr, frame);
    animrig::insert_keyframes(
        bmain, &ptr, std::nullopt, {{"location"}}, frame, context, BEZT_KEYTYPE_KEYFRAME, flags);
  }

  PosePreviewInput input(const float x, const bool insert_keys = true)
  {
    PosePreviewInput input;
    input.object_uid = SessionUID{rig->id.session_uid};
    input.transaction_id = 1;
    input.frame = 5.0f;
    input.insert_keys = insert_keys;
    input.keyframe_type = BEZT_KEYTYPE_KEYFRAME;
    PoseBoneInput value{};
    value.name = "Bone";
    value.location[0] = x;
    value.quaternion[0] = 1.0f;
    value.scale[0] = value.scale[1] = value.scale[2] = 1.0f;
    value.rotation_mode = bone->rotmode;
    value.key_paths = {{"location"}};
    input.bones.append(std::move(value));
    return input;
  }

  const animrig::Channelbag &source_bag()
  {
    return *animrig::channelbag_for_action_slot(rig->adt->action->wrap(), rig->adt->slot_handle);
  }

  NlaStrip *add_strip(bAction &action, const float start = 1.0f)
  {
    NlaTrack *track = BKE_nlatrack_new_tail(&rig->adt->nla_tracks, false);
    NlaStrip *strip = BKE_nlastrip_new(&action, rig->id);
    EXPECT_TRUE(animrig::nla::assign_action(*strip, action.wrap(), rig->id));
    EXPECT_TRUE(BKE_nlatrack_add_strip(track, strip, false));
    strip->start = start;
    strip->end = start + 9.0f;
    strip->actstart = 1.0f;
    strip->actend = 10.0f;
    strip->scale = 1.0f;
    return strip;
  }

  static void expect_same_curves(const animrig::Channelbag &a, const animrig::Channelbag &b)
  {
    ASSERT_EQ(a.fcurves().size(), b.fcurves().size());
    for (int i = 0; i < a.fcurves().size(); i++) {
      const FCurve &left = *a.fcurve(i);
      const FCurve &right = *b.fcurve(i);
      EXPECT_EQ(left.rna_path(), right.rna_path());
      EXPECT_EQ(left.array_index, right.array_index);
      ASSERT_EQ(left.totvert, right.totvert);
      for (int key = 0; key < left.totvert; key++) {
        for (int handle = 0; handle < 3; handle++) {
          for (int axis = 0; axis < 3; axis++) {
            EXPECT_FLOAT_EQ(left.bezt[key].vec[handle][axis], right.bezt[key].vec[handle][axis]);
          }
        }
        EXPECT_EQ(left.bezt[key].h1, right.bezt[key].h1);
        EXPECT_EQ(left.bezt[key].h2, right.bezt[key].h2);
        EXPECT_EQ(left.bezt[key].ipo, right.bezt[key].ipo);
      }
    }
  }
};

TEST_F(MeshOnionPreviewTest, PreparationAndDiscardPreserveSource)
{
  const animrig::Channelbag baseline(source_bag());
  const int action_users = rig->adt->action->id.us;
  const int armature_users = armature->id.us;
  const float original_x = bone->loc[0];
  auto proposal = preview_prepare(*bmain, *scene, *rig, input(3.0f));
  ASSERT_NE(proposal, nullptr);
  expect_same_curves(baseline, source_bag());
  EXPECT_EQ(action_users, rig->adt->action->id.us);
  EXPECT_EQ(armature_users, armature->id.us);
  EXPECT_FLOAT_EQ(original_x, bone->loc[0]);
  proposal.reset(); /* Cancel owns no source animation to restore. */
  expect_same_curves(baseline, source_bag());
}

TEST_F(MeshOnionPreviewTest, PreviewMatchesNativeConfirmation)
{
  const auto proposal = preview_prepare(*bmain, *scene, *rig, input(3.0f));
  ASSERT_NE(proposal, nullptr);
  insert_location(5.0f, 3.0f);
  expect_same_curves(*proposal->channelbag, source_bag());
}

TEST_F(MeshOnionPreviewTest, RepeatedProposalsResetBaselineAndUnkeyedInputDoesNotCreateKeys)
{
  const animrig::Channelbag baseline(source_bag());
  const auto first = preview_prepare(*bmain, *scene, *rig, input(20.0f));
  const auto second = preview_prepare(*bmain, *scene, *rig, input(3.0f));
  const auto unkeyed = preview_prepare(*bmain, *scene, *rig, input(4.0f, false));
  ASSERT_NE(first, nullptr);
  ASSERT_NE(second, nullptr);
  ASSERT_NE(unkeyed, nullptr);
  expect_same_curves(baseline, *unkeyed->channelbag);
  expect_same_curves(baseline, source_bag());
  insert_location(5.0f, 3.0f);
  expect_same_curves(*second->channelbag, source_bag());
}

TEST_F(MeshOnionPreviewTest, UnsupportedPreviewIsRejected)
{
  PosePreviewInput proposal = input(3.0f);
  proposal.supported = false;
  EXPECT_EQ(preview_prepare(*bmain, *scene, *rig, proposal), nullptr);
}

TEST_F(MeshOnionPreviewTest, NativeInsertionPoliciesMatch)
{
  const eInsertKeyFlags flags[] = {
      INSERTKEY_REPLACE, INSERTKEY_AVAILABLE, INSERTKEY_NEEDED, INSERTKEY_CYCLE_AWARE};
  float x = 3.0f;
  for (const eInsertKeyFlags flag : flags) {
    SCOPED_TRACE(int(flag));
    PosePreviewInput proposal = input(x);
    proposal.key_flags = flag;
    const auto prepared = preview_prepare(*bmain, *scene, *rig, proposal);
    ASSERT_NE(prepared, nullptr);
    insert_location(5.0f, x, flag);
    expect_same_curves(*prepared->channelbag, source_bag());
    x += 1.0f;
  }
}

TEST_F(MeshOnionPreviewTest, PrivateApplyDoesNotRetainPreviousProvisionalKeys)
{
  const animrig::Channelbag baseline(source_bag());
  const auto keyed = preview_prepare(*bmain, *scene, *rig, input(3.0f));
  const auto unkeyed = preview_prepare(*bmain, *scene, *rig, input(4.0f, false));
  ASSERT_NE(keyed, nullptr);
  ASSERT_NE(unkeyed, nullptr);
  constexpr int flags = LIB_ID_CREATE_LOCALIZE | LIB_ID_COPY_NO_PREVIEW |
                        LIB_ID_COPY_NO_LIB_OVERRIDE;
  Object *private_rig = reinterpret_cast<Object *>(
      BKE_id_copy_ex(nullptr, &rig->id, nullptr, flags));
  bAction *private_action = reinterpret_cast<bAction *>(
      BKE_id_copy_ex(nullptr, &rig->adt->action->id, nullptr, flags));
  private_rig->adt->action = private_action;
  Map<SessionUID, Object *> objects;
  objects.add(SessionUID{rig->id.session_uid}, private_rig);
  Map<SessionUID, bAction *> actions;
  actions.add(SessionUID{rig->adt->action->id.session_uid}, private_action);
  PreviewApplyState state;
  bool structure_changed = true;
  EXPECT_TRUE(preview_apply(*keyed, *bmain, objects, actions, state, &structure_changed));
  EXPECT_FALSE(structure_changed); /* Existing channels only acquired new values/keys. */
  PosePreviewInput rotation = input(3.0f);
  rotation.bones[0].key_paths = {{"rotation_quaternion"}};
  const auto new_channels = preview_prepare(*bmain, *scene, *rig, rotation);
  ASSERT_NE(new_channels, nullptr);
  EXPECT_TRUE(preview_apply(*new_channels, *bmain, objects, actions, state, &structure_changed));
  EXPECT_TRUE(structure_changed);
  EXPECT_TRUE(preview_apply(*new_channels, *bmain, objects, actions, state, &structure_changed));
  EXPECT_FALSE(structure_changed);
  EXPECT_TRUE(preview_apply(*unkeyed, *bmain, objects, actions, state, &structure_changed));
  EXPECT_TRUE(structure_changed); /* Returning to baseline removes provisional channels. */
  expect_same_curves(
      baseline,
      *animrig::channelbag_for_action_slot(private_action->wrap(), private_rig->adt->slot_handle));
  EXPECT_FLOAT_EQ(BKE_pose_channel_find_name(private_rig->pose, "Bone")->loc[0], 4.0f);
  expect_same_curves(baseline, source_bag());
  BKE_id_free(nullptr, private_rig);
  BKE_id_free(nullptr, private_action);
}

TEST_F(MeshOnionPreviewTest, NlaTweakUsesNativeTimeMappingAndPreservesSharedAction)
{
  bAction *action = rig->adt->action;
  const int slot_handle = rig->adt->slot_handle;
  NlaStrip *strip = add_strip(*action, 11.0f);
  strip->scale = 2.0f;
  strip->end = 29.0f;
  NlaStrip *other_strip = add_strip(*action, 40.0f);
  ASSERT_TRUE(animrig::unassign_action(rig->id));
  BKE_nlatrack_set_active(&rig->adt->nla_tracks, rig->adt->nla_tracks.first());
  strip->flag |= NLASTRIP_FLAG_ACTIVE;
  BKE_nla_tweakmode_enter({rig->id, *rig->adt});
  ASSERT_EQ(rig->adt->actstrip, strip);
  const animrig::Channelbag baseline(
      *animrig::channelbag_for_action_slot(action->wrap(), slot_handle));
  const int users = action->id.us;
  PosePreviewInput proposal_input = input(3.0f);
  proposal_input.frame = 19.0f; /* Scene 19 maps to Action 5. */
  const auto proposal = preview_prepare(*bmain, *scene, *rig, proposal_input);
  ASSERT_NE(proposal, nullptr);
  ASSERT_NE(proposal->channelbag, nullptr);
  EXPECT_EQ(proposal->action_uid, SessionUID{action->id.session_uid});
  EXPECT_EQ(action->id.us, users);
  EXPECT_EQ(rig->adt->actstrip, strip);
  EXPECT_EQ(strip->act, action);
  EXPECT_EQ(other_strip->act, action);
  expect_same_curves(baseline, source_bag());
  insert_location(19.0f, 3.0f);
  expect_same_curves(*proposal->channelbag, source_bag());
  const FCurve *curve = proposal->channelbag->fcurve_find({"pose.bones[\"Bone\"].location", 0});
  ASSERT_NE(curve, nullptr);
  ASSERT_EQ(curve->totvert, 3);
  EXPECT_FLOAT_EQ(curve->bezt[1].vec[1][0], 5.0f);
  BKE_nla_tweakmode_exit({rig->id, *rig->adt});
}

TEST_F(MeshOnionPreviewTest, NlaAdditiveInfluenceMatchesNativeConfirmation)
{
  bAction *lower_action = id_cast<bAction *>(BKE_id_copy(bmain, &rig->adt->action->id));
  add_strip(*lower_action);
  rig->adt->act_blendmode = NLASTRIP_MODE_ADD;
  rig->adt->act_influence = 0.4f;
  const animrig::Channelbag baseline(source_bag());
  const int users = lower_action->id.us;
  const auto proposal = preview_prepare(*bmain, *scene, *rig, input(3.0f));
  ASSERT_NE(proposal, nullptr);
  ASSERT_NE(proposal->channelbag, nullptr);
  expect_same_curves(baseline, source_bag());
  EXPECT_EQ(lower_action->id.us, users);
  insert_location(5.0f, 3.0f);
  expect_same_curves(*proposal->channelbag, source_bag());
}

TEST_F(MeshOnionPreviewTest, FractionallyScaledTweakPreservesAlignedHandles)
{
  ASSERT_TRUE(animrig::unassign_action(rig->id));
  const float frames[] = {1.0f, 8.0f, 15.0f, 23.0f, 30.0f};
  const float values[] = {-0.23933783f, -0.15113649f, -0.05122352f, -0.17220214f, -0.23933783f};
  for (int i = 0; i < 5; i++) {
    insert_location(frames[i], values[i]);
  }
  bAction *action = rig->adt->action;
  for (FCurve *curve :
       animrig::channelbag_for_action_slot(action->wrap(), rig->adt->slot_handle)->fcurves())
  {
    for (int i : {1, 3}) {
      curve->bezt[i].h1 = curve->bezt[i].h2 = HD_ALIGN;
    }
    BKE_fcurve_deselect_all_keys(*curve);
    BKE_fcurve_handles_recalc(*curve);
  }
  NlaStrip *strip = add_strip(*action, 5.0f);
  strip->actend = 30.0f;
  strip->scale = 1.5f;
  strip->end = 48.5f;
  ASSERT_TRUE(animrig::unassign_action(rig->id));
  BKE_nlatrack_set_active(&rig->adt->nla_tracks, rig->adt->nla_tracks.first());
  strip->flag |= NLASTRIP_FLAG_ACTIVE | NLASTRIP_FLAG_SYNC_LENGTH;
  ASSERT_TRUE(BKE_nla_tweakmode_enter({rig->id, *rig->adt}));
  PosePreviewInput requested = input(-0.1216193214f);
  requested.frame = 17.0f;
  requested.key_flags = INSERTKEY_AVAILABLE;
  const auto prepared = preview_prepare(*bmain, *scene, *rig, requested);
  ASSERT_NE(prepared, nullptr);
  insert_location(requested.frame, requested.bones[0].location[0], INSERTKEY_AVAILABLE);
  expect_same_curves(*prepared->channelbag, source_bag());
  for (int i : source_bag().fcurves().index_range()) {
    const FCurve &actual = *source_bag().fcurve(i);
    const FCurve &expected = *prepared->channelbag->fcurve(i);
    for (int key = 0; key < actual.totvert; key++) {
      for (int handle = 0; handle < 3; handle++) {
        for (int axis = 0; axis < 3; axis++) {
          EXPECT_EQ(actual.bezt[key].vec[handle][axis], expected.bezt[key].vec[handle][axis]);
        }
      }
    }
  }
  BKE_nla_tweakmode_exit({rig->id, *rig->adt});
}

TEST_F(MeshOnionPreviewTest, NlaOnlyCreatesPrivateActionOnlyWhenNativeKeyingDoes)
{
  bAction *strip_action = rig->adt->action;
  NlaStrip *strip = add_strip(*strip_action);
  ASSERT_TRUE(animrig::unassign_action(rig->id));
  const int users = strip_action->id.us;
  const int action_count = bmain->actions.count();
  const auto unkeyed = preview_prepare(*bmain, *scene, *rig, input(4.0f, false));
  ASSERT_NE(unkeyed, nullptr);
  EXPECT_FALSE(unkeyed->assign_action);
  EXPECT_EQ(unkeyed->channelbag, nullptr);
  EXPECT_EQ(unkeyed->action_template, nullptr);
  PosePreviewInput available = input(3.0f);
  available.key_flags = INSERTKEY_AVAILABLE;
  const auto available_only = preview_prepare(*bmain, *scene, *rig, available);
  ASSERT_NE(available_only, nullptr);
  EXPECT_FALSE(available_only->assign_action);
  const auto keyed = preview_prepare(*bmain, *scene, *rig, input(3.0f));
  ASSERT_NE(keyed, nullptr);
  EXPECT_TRUE(keyed->assign_action);
  EXPECT_FALSE(keyed->action_uid.is_generated());
  ASSERT_NE(keyed->action_template, nullptr);
  EXPECT_EQ(rig->adt->action, nullptr);
  EXPECT_EQ(strip->act, strip_action);
  EXPECT_EQ(strip_action->id.us, users);
  EXPECT_EQ(bmain->actions.count(), action_count);

  Main *private_main = BKE_main_new();
  Object *private_rig = id_cast<Object *>(
      BKE_id_copy_ex(nullptr, &rig->id, nullptr, LIB_ID_CREATE_LOCALIZE | LIB_ID_COPY_NO_PREVIEW));
  bAction *private_strip_action = id_cast<bAction *>(BKE_id_copy_ex(
      nullptr, &strip_action->id, nullptr, LIB_ID_CREATE_LOCALIZE | LIB_ID_COPY_NO_PREVIEW));
  private_rig->data = nullptr;
  private_rig->adt->nla_tracks.first()->strips.first()->act = private_strip_action;
  BKE_libblock_management_main_add(private_main, &private_strip_action->id);
  BKE_libblock_management_main_add(private_main, &private_rig->id);
  Map<SessionUID, Object *> objects;
  objects.add(SessionUID{rig->id.session_uid}, private_rig);
  Map<SessionUID, bAction *> actions;
  PreviewApplyState state;
  bool changed = false;
  ASSERT_TRUE(preview_apply(*keyed, *private_main, objects, actions, state, &changed));
  EXPECT_TRUE(changed);
  ASSERT_NE(private_rig->adt->action, nullptr);
  EXPECT_NE(private_rig->adt->action, strip_action);
  bAction *created_action = private_rig->adt->action;
  ASSERT_TRUE(preview_apply(*keyed, *private_main, objects, actions, state, &changed));
  EXPECT_FALSE(changed); /* Repeated drag proposals reuse Action topology and graph relations. */
  EXPECT_EQ(private_rig->adt->action, created_action);
  ASSERT_TRUE(preview_apply(*unkeyed, *private_main, objects, actions, state, &changed));
  EXPECT_TRUE(changed);
  EXPECT_EQ(private_rig->adt->action, nullptr);
  EXPECT_EQ(strip_action->id.us, users);
  BKE_main_free(private_main);

  insert_location(5.0f, 3.0f);
  ASSERT_NE(rig->adt->action, nullptr);
  ASSERT_NE(keyed->channelbag, nullptr);
  expect_same_curves(*keyed->channelbag, source_bag());
}

TEST_F(MeshOnionPreviewTest, RelatedDataActionIsCopiedBeforeNativeSlotCreation)
{
  bAction *action = BKE_action_add(bmain, "DataAction");
  AnimData *data_adt = BKE_animdata_ensure_id(&armature->id);
  ASSERT_TRUE(animrig::assign_action(action, armature->id));
  ASSERT_TRUE(animrig::unassign_action(rig->id));
  armature->id.us = 1;
  const int slots = action->wrap().slots().size();
  const int users = action->id.us;
  const auto proposal = preview_prepare(*bmain, *scene, *rig, input(3.0f));
  ASSERT_NE(proposal, nullptr);
  EXPECT_EQ(proposal->action_uid, SessionUID{action->id.session_uid});
  ASSERT_NE(proposal->action_template, nullptr);
  EXPECT_EQ(action->wrap().slots().size(), slots);
  EXPECT_EQ(action->id.us, users);
  EXPECT_EQ(rig->adt->action, nullptr);
  EXPECT_EQ(data_adt->action, action);
  const auto unkeyed = preview_prepare(*bmain, *scene, *rig, input(3.0f, false));
  ASSERT_NE(unkeyed, nullptr);
  ASSERT_NE(unkeyed->action_template, nullptr);
  Main *private_main = BKE_main_new();
  constexpr int flags = LIB_ID_CREATE_LOCALIZE | LIB_ID_COPY_NO_PREVIEW;
  Object *private_rig = id_cast<Object *>(BKE_id_copy_ex(nullptr, &rig->id, nullptr, flags));
  bAction *private_action = id_cast<bAction *>(
      BKE_id_copy_ex(nullptr, &action->id, nullptr, flags));
  private_rig->data = nullptr;
  BKE_libblock_management_main_add(private_main, &private_rig->id);
  BKE_libblock_management_main_add(private_main, &private_action->id);
  Map<SessionUID, Object *> objects;
  objects.add(SessionUID{rig->id.session_uid}, private_rig);
  Map<SessionUID, bAction *> actions;
  actions.add(SessionUID{action->id.session_uid}, private_action);
  PreviewApplyState state;
  bool changed = false;
  ASSERT_TRUE(preview_apply(*proposal, *private_main, objects, actions, state, &changed));
  EXPECT_TRUE(changed);
  EXPECT_EQ(private_rig->adt->action, private_action);
  EXPECT_EQ(actions.lookup(SessionUID{action->id.session_uid}), private_action);
  EXPECT_GT(private_action->wrap().slots().size(), slots);
  ASSERT_TRUE(preview_apply(*unkeyed, *private_main, objects, actions, state, &changed));
  EXPECT_TRUE(changed);
  EXPECT_EQ(private_rig->adt->action, nullptr);
  EXPECT_EQ(private_action->wrap().slots().size(), slots);
  EXPECT_EQ(action->id.us, users);
  BKE_main_free(private_main);
  insert_location(5.0f, 3.0f);
  expect_same_curves(*proposal->channelbag, source_bag());
}

TEST_F(MeshOnionPreviewTest, RepeatedStagingNeverCountsExternalSourceReferences)
{
  Object *target = BKE_object_add_only_object(bmain, OB_EMPTY, "ConstraintTarget");
  Object *shape = BKE_object_add_only_object(bmain, OB_EMPTY, "CustomShape");
  bConstraint *constraint = BKE_constraint_add_for_pose(
      rig, bone, "Copy Location", CONSTRAINT_TYPE_LOCLIKE);
  static_cast<bLocateLikeConstraint *>(constraint->data)->tar = target;
  bone->custom = shape;
  bAction *action = rig->adt->action;
  add_strip(*action);
  const animrig::Channelbag baseline(source_bag());
  const int action_users = action->id.us;
  const int data_users = armature->id.us;
  const int target_users = target->id.us;
  const int shape_users = shape->id.us;
  const int action_count = bmain->actions.count();
  for (int iteration = 0; iteration < 24; iteration++) {
    SCOPED_TRACE(iteration);
    const auto packet = preview_prepare(
        *bmain, *scene, *rig, input(float(iteration), iteration % 2));
    ASSERT_NE(packet, nullptr);
    EXPECT_EQ(action->id.us, action_users);
    EXPECT_EQ(armature->id.us, data_users);
    EXPECT_EQ(target->id.us, target_users);
    EXPECT_EQ(shape->id.us, shape_users);
    EXPECT_EQ(bmain->actions.count(), action_count);
    EXPECT_EQ(static_cast<bLocateLikeConstraint *>(constraint->data)->tar, target);
    EXPECT_EQ(bone->custom, shape);
    expect_same_curves(baseline, source_bag());
  }
}

}  // namespace blender::ed::mesh_onion_skin::tests
