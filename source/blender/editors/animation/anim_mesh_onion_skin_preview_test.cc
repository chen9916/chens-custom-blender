/* SPDX-FileCopyrightText: 2026 Blender Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

#include "ED_mesh_onion_skin_preview.hh"

#include "ANIM_action.hh"
#include "ANIM_keyframing.hh"

#include "BKE_action.hh"
#include "BKE_animsys.hh"
#include "BKE_armature.hh"
#include "BKE_gtest_base.hh"
#include "BKE_lib_id.hh"
#include "BKE_main.hh"
#include "BKE_object.hh"
#include "BKE_scene.hh"

#include "BLI_listbase.hh"
#include "BLI_string.hh"

#include "DNA_armature_types.h"
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
  bool structure_changed = true;
  EXPECT_TRUE(preview_apply(*keyed, objects, &structure_changed));
  EXPECT_FALSE(structure_changed); /* Existing channels only acquired new values/keys. */
  PosePreviewInput rotation = input(3.0f);
  rotation.bones[0].key_paths = {{"rotation_quaternion"}};
  const auto new_channels = preview_prepare(*bmain, *scene, *rig, rotation);
  ASSERT_NE(new_channels, nullptr);
  EXPECT_TRUE(preview_apply(*new_channels, objects, &structure_changed));
  EXPECT_TRUE(structure_changed);
  EXPECT_TRUE(preview_apply(*new_channels, objects, &structure_changed));
  EXPECT_FALSE(structure_changed);
  EXPECT_TRUE(preview_apply(*unkeyed, objects, &structure_changed));
  EXPECT_TRUE(structure_changed); /* Returning to baseline removes provisional channels. */
  expect_same_curves(
      baseline,
      *animrig::channelbag_for_action_slot(private_action->wrap(), private_rig->adt->slot_handle));
  EXPECT_FLOAT_EQ(BKE_pose_channel_find_name(private_rig->pose, "Bone")->loc[0], 4.0f);
  expect_same_curves(baseline, source_bag());
  BKE_id_free(nullptr, private_rig);
  BKE_id_free(nullptr, private_action);
}

}  // namespace blender::ed::mesh_onion_skin::tests
