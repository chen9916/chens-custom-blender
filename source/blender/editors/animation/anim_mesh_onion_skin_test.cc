/* SPDX-FileCopyrightText: 2026 Blender Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

#include "anim_mesh_onion_skin_intern.hh"

#include <cstring>

#include "ED_mesh_onion_skin_preview.hh"

#include "ANIM_action.hh"
#include "ANIM_keyframing.hh"

#include "BKE_action.hh"
#include "BKE_animsys.hh"
#include "BKE_armature.hh"
#include "BKE_collection.hh"
#include "BKE_constraint.h"
#include "BKE_deform.hh"
#include "BKE_editmesh.hh"
#include "BKE_gtest_base.hh"
#include "BKE_idprop.hh"
#include "BKE_layer.hh"
#include "BKE_lib_id.hh"
#include "BKE_main.hh"
#include "BKE_mesh.hh"
#include "BKE_modifier.hh"
#include "BKE_object.hh"
#include "BKE_scene.hh"

#include "BLI_listbase.hh"
#include "BLI_math_base.hh"
#include "BLI_math_vector.hh"
#include "BLI_string.hh"

#include "DNA_action_types.h"
#include "DNA_armature_types.h"
#include "DNA_constraint_types.h"
#include "DNA_layer_types.h"
#include "DNA_mesh_types.h"
#include "DNA_modifier_types.h"
#include "DNA_object_types.h"
#include "DNA_scene_types.h"

#include "DEG_depsgraph.hh"
#include "DEG_depsgraph_build.hh"
#include "DEG_depsgraph_query.hh"

#include "MEM_guardedalloc.h"
#include "RNA_access.hh"
#include "RNA_prototypes.hh"
#include "testing/testing.h"

namespace blender::ed::mesh_onion_skin::tests {

class MeshOnionEvaluationTest : public bke::BlenderGTestBase {
 protected:
  Main *bmain;
  Scene *scene;
  ViewLayer *view_layer;
  Object *rig;
  Object *object;
  bArmature *armature;
  Mesh *mesh;
  bPoseChannel *control;

  void SetUp() override
  {
    bmain = BKE_main_new();
    scene = BKE_scene_add(bmain, "Scene");
    view_layer = BKE_view_layer_default_view(scene);
    armature = BKE_armature_add(bmain, "Armature");
    Bone *deform = MEM_new<Bone>(__func__);
    STRNCPY(deform->name, "Deform");
    deform->tail[1] = 1.0f;
    BLI_addtail(&armature->bonebase, deform);
    Bone *target = MEM_new<Bone>(__func__);
    STRNCPY(target->name, "Control");
    target->head[1] = 1.0f;
    target->tail[1] = 2.0f;
    target->flag |= BONE_NO_DEFORM;
    BLI_addtail(&armature->bonebase, target);
    BKE_armature_where_is(armature);
    rig = BKE_object_add_only_object(bmain, OB_ARMATURE, "Rig");
    rig->data = &armature->id;
    BKE_pose_ensure(bmain, rig, armature, false);
    control = BKE_pose_channel_find_name(rig->pose, "Control");
    bPoseChannel *deform_pose = BKE_pose_channel_find_name(rig->pose, "Deform");
    bConstraint *constraint = BKE_constraint_add_for_pose(
        rig, deform_pose, "Authored IK", CONSTRAINT_TYPE_KINEMATIC);
    auto &ik = *static_cast<bKinematicConstraint *>(constraint->data);
    ik.tar = rig;
    STRNCPY(ik.subtarget, "Control");
    ik.rootbone = 1;
    ik.iterations = 64;
    ik.flag = CONSTRAINT_IK_TIP | CONSTRAINT_IK_POS;
    ik.weight = 1.0f;
    constraint->enforce = 1.0f;
    BKE_pose_update_constraint_flags(*rig);

    mesh = BKE_mesh_new_nomain(3, 3, 1, 3);
    BKE_libblock_management_main_add(bmain, mesh);
    mesh->vert_positions_for_write()[0] = float3(-0.1f, 0.0f, 0.0f);
    mesh->vert_positions_for_write()[1] = float3(0.1f, 0.0f, 0.0f);
    mesh->vert_positions_for_write()[2] = float3(0.0f, 1.0f, 0.0f);
    mesh->edges_for_write()[0] = int2(0, 1);
    mesh->edges_for_write()[1] = int2(1, 2);
    mesh->edges_for_write()[2] = int2(2, 0);
    mesh->face_offsets_for_write()[0] = 0;
    mesh->face_offsets_for_write()[1] = 3;
    for (int index = 0; index < 3; index++) {
      mesh->corner_verts_for_write()[index] = index;
      mesh->corner_edges_for_write()[index] = index;
    }
    object = BKE_object_add_only_object(bmain, OB_MESH, "Skin");
    object->data = &mesh->id;
    BKE_object_defgroup_new(object, "Deform");
    for (MDeformVert &vertex : mesh->deform_verts_for_write()) {
      BKE_defvert_ensure_index(&vertex, 0)->weight = 1.0f;
    }
    ModifierData *modifier = BKE_modifier_new(eModifierType_Armature);
    BKE_modifiers_persistent_uid_init(*object, *modifier);
    reinterpret_cast<ArmatureModifierData *>(modifier)->object = rig;
    BLI_addtail(&object->modifiers, modifier);
    BKE_collection_object_add(bmain, scene->master_collection, rig);
    BKE_collection_object_add(bmain, scene->master_collection, object);
    insert_control(1.0f, -0.2f);
    insert_control(10.0f, 0.2f);
  }

  void TearDown() override
  {
    BKE_main_free(bmain);
  }

  void insert_control(const float frame, const float x)
  {
    control->loc[0] = x;
    PointerRNA ptr = RNA_pointer_create_discrete(&rig->id, RNA_PoseBone, control);
    const AnimationEvalContext context = BKE_animsys_eval_context_construct(nullptr, frame);
    animrig::insert_keyframes(bmain,
                              &ptr,
                              std::nullopt,
                              {{"location"}},
                              frame,
                              context,
                              BEZT_KEYTYPE_KEYFRAME,
                              INSERTKEY_NOFLAGS);
  }

  std::shared_ptr<const PreparedPreview> proposal(const float x)
  {
    PosePreviewInput input;
    input.object_uid = SessionUID{rig->id.session_uid};
    input.frame = 5.0f;
    input.insert_keys = true;
    input.keyframe_type = BEZT_KEYTYPE_KEYFRAME;
    PoseBoneInput bone{};
    bone.name = "Control";
    bone.location[0] = x;
    bone.quaternion[0] = 1.0f;
    bone.scale[0] = bone.scale[1] = bone.scale[2] = 1.0f;
    bone.rotation_mode = control->rotmode;
    bone.key_paths = {{"location"}};
    input.bones.append(std::move(bone));
    return preview_prepare(*bmain, *scene, *rig, input);
  }

  std::shared_ptr<const Bundle> evaluate_preview(std::shared_ptr<const PreparedPreview> preview,
                                                 std::string &error)
  {
    return evaluate_for_test(
        *bmain, *scene, *view_layer, *rig, {&object, 1}, {1, 1, 1}, 5.0f, preview, error);
  }

  auto active_graph_for_tags()
  {
    std::unique_ptr<Depsgraph, decltype(&DEG_graph_free)> graph(
        DEG_graph_new(bmain, scene, view_layer, DAG_EVAL_VIEWPORT), DEG_graph_free);
    ID *target = &rig->id;
    DEG_graph_build_from_ids(graph.get(), Span<ID *>(&target, 1));
    DEG_make_active(graph.get());
    return graph;
  }
};

TEST_F(MeshOnionEvaluationTest, AuthoredIKSnapshotsMatchCommittedNativeEvaluation)
{
  const animrig::Channelbag baseline(
      *animrig::channelbag_for_action_slot(rig->adt->action->wrap(), rig->adt->slot_handle));
  const int rig_users = rig->id.us;
  const int mesh_users = mesh->id.us;
  const int armature_users = armature->id.us;
  const int action_users = rig->adt->action->id.us;
  const float source_x = control->loc[0];
  const auto prepared = proposal(0.6f);
  ASSERT_NE(prepared, nullptr);
  std::string error;
  const auto bundle = evaluate_preview(prepared, error);
  ASSERT_NE(bundle, nullptr) << error;
  ASSERT_EQ(bundle->samples.size(), 2);
  /* Parity alone could accidentally pass for a disabled modifier or IK fixture. Require both
   * real deformation away from the rest mesh and different neighboring animation samples. */
  for (const Sample &sample : bundle->samples) {
    EXPECT_GT(math::distance(sample.positions[2], mesh->vert_positions()[2]), 0.05f);
  }
  EXPECT_GT(math::distance(bundle->samples[0].positions[2], bundle->samples[1].positions[2]),
            1.0e-4f);
  EXPECT_EQ(rig->id.us, rig_users);
  EXPECT_EQ(mesh->id.us, mesh_users);
  EXPECT_EQ(armature->id.us, armature_users);
  EXPECT_EQ(rig->adt->action->id.us, action_users);
  EXPECT_FLOAT_EQ(control->loc[0], source_x);
  const animrig::Channelbag &unchanged = *animrig::channelbag_for_action_slot(
      rig->adt->action->wrap(), rig->adt->slot_handle);
  ASSERT_EQ(baseline.fcurves().size(), unchanged.fcurves().size());
  for (int index = 0; index < baseline.fcurves().size(); index++) {
    const FCurve &original = *baseline.fcurve(index);
    const FCurve &current = *unchanged.fcurve(index);
    ASSERT_EQ(original.totvert, current.totvert);
    EXPECT_EQ(memcmp(original.bezt, current.bezt, sizeof(BezTriple) * original.totvert), 0);
  }

  /* Commit through the same native inserter to the independent source workspace, then compare
   * the private preview to ordinary sequential source evaluation at each sampled time. */
  insert_control(5.0f, 0.6f);
  Depsgraph *graph = DEG_graph_new(bmain, scene, view_layer, DAG_EVAL_VIEWPORT);
  ID *targets[] = {&rig->id, &object->id};
  DEG_graph_build_from_ids(graph, Span<ID *>(targets, 2));
  for (const Sample &sample : bundle->samples) {
    DEG_evaluate_on_framechange(graph, sample.frame);
    const Object *evaluated_object = DEG_get_evaluated(graph, object);
    const Mesh *evaluated_mesh = BKE_object_get_evaluated_mesh_no_subsurf(evaluated_object);
    ASSERT_NE(evaluated_mesh, nullptr);
    ASSERT_EQ(sample.positions.size(), evaluated_mesh->verts_num);
    for (int vertex = 0; vertex < sample.positions.size(); vertex++) {
      for (int axis = 0; axis < 3; axis++) {
        EXPECT_NEAR(sample.positions[vertex][axis],
                    evaluated_mesh->vert_positions()[vertex][axis],
                    1.0e-5f);
      }
    }
    EXPECT_EQ(sample.triangles.size(), 1);
    EXPECT_EQ(sample.triangles[0], int3(0, 1, 2));
  }
  DEG_graph_free(graph);
}

TEST_F(MeshOnionEvaluationTest, TerminalSubdivisionUsesCageWithoutSourceChanges)
{
  ModifierData *subdivision = BKE_modifier_new(eModifierType_Subsurf);
  BKE_modifiers_persistent_uid_init(*object, *subdivision);
  BLI_addtail(&object->modifiers, subdivision);
  const auto source_mode = subdivision->mode;
  std::string error;
  const auto bundle = evaluate_preview(proposal(0.6f), error);
  ASSERT_NE(bundle, nullptr) << error;
  EXPECT_EQ(subdivision->mode, source_mode);
  for (const Sample &sample : bundle->samples) {
    EXPECT_EQ(sample.positions.size(), 3);
    EXPECT_EQ(sample.triangles.size(), 1);
  }
}

TEST_F(MeshOnionEvaluationTest, FractionalFramesAndHistoricalMatricesPreserveSourcePlayhead)
{
  BKE_scene_frame_set(scene, 5.5f);
  PointerRNA object_ptr = RNA_pointer_create_discrete(&object->id, RNA_Object, object);
  for (const float frame : {1.0f, 10.0f}) {
    object->loc[0] = frame == 1.0f ? 1.0f : 4.0f;
    const AnimationEvalContext context = BKE_animsys_eval_context_construct(nullptr, frame);
    animrig::insert_keyframes(bmain,
                              &object_ptr,
                              std::nullopt,
                              {{"location"}},
                              frame,
                              context,
                              BEZT_KEYTYPE_KEYFRAME,
                              INSERTKEY_NOFLAGS);
  }
  object->loc[0] = 2.0f;
  const int source_frame = scene->r.cfra;
  const float source_subframe = scene->r.subframe;
  const auto prepared = proposal(0.6f);
  ASSERT_NE(prepared, nullptr);
  std::string error;
  const auto bundle = evaluate_for_test(
      *bmain, *scene, *view_layer, *rig, {&object, 1}, {1, 1, 1}, 5.5f, prepared, error);
  ASSERT_NE(bundle, nullptr) << error;
  ASSERT_EQ(bundle->samples.size(), 2);
  EXPECT_EQ(scene->r.cfra, source_frame);
  EXPECT_FLOAT_EQ(scene->r.subframe, source_subframe);
  EXPECT_FLOAT_EQ(bundle->samples[0].frame, 4.5f);
  EXPECT_FLOAT_EQ(bundle->samples[1].frame, 6.5f);
  EXPECT_FLOAT_EQ(object->loc[0], 2.0f);
  EXPECT_GT(math::abs(bundle->samples[0].object_to_world[3][0] -
                      bundle->samples[1].object_to_world[3][0]),
            0.1f);

  insert_control(5.0f, 0.6f);
  std::unique_ptr<Depsgraph, decltype(&DEG_graph_free)> graph(
      DEG_graph_new(bmain, scene, view_layer, DAG_EVAL_VIEWPORT), DEG_graph_free);
  ID *targets[] = {&rig->id, &object->id};
  DEG_graph_build_from_ids(graph.get(), Span<ID *>(targets, 2));
  for (const Sample &sample : bundle->samples) {
    BKE_scene_frame_set(scene, sample.frame);
    DEG_evaluate_on_framechange(graph.get(), sample.frame);
    const Object *evaluated_object = DEG_get_evaluated(graph.get(), object);
    const Mesh *evaluated_mesh = BKE_object_get_evaluated_mesh_no_subsurf(evaluated_object);
    ASSERT_NE(evaluated_mesh, nullptr);
    for (int vertex = 0; vertex < sample.positions.size(); vertex++) {
      EXPECT_LT(math::distance(sample.positions[vertex], evaluated_mesh->vert_positions()[vertex]),
                1.0e-5f);
    }
    for (int column = 0; column < 4; column++) {
      for (int row = 0; row < 4; row++) {
        EXPECT_NEAR(sample.object_to_world[column][row],
                    evaluated_object->object_to_world()[column][row],
                    1.0e-6f);
      }
    }
  }
}

TEST_F(MeshOnionEvaluationTest, CoveredMeshesInAnotherViewLayerKeepCanonicalMembership)
{
  ViewLayer *alternate = BKE_view_layer_add(bmain, scene, "Alternate", nullptr, VIEWLAYER_ADD_NEW);
  ViewLayer *source_layer = view_layer;
  Vector<Object *> meshes = {object};
  EXPECT_FALSE(
      source_membership_update_for_test(*scene, source_layer, meshes, alternate, {&object, 1}));
  EXPECT_EQ(source_layer, view_layer);
  EXPECT_EQ(meshes.size(), 1);
}

TEST_F(MeshOnionEvaluationTest, NewlyRequestedMeshEntersRetainedUnionOnlyOnce)
{
  ViewLayer *alternate = BKE_view_layer_add(bmain, scene, "Alternate", nullptr, VIEWLAYER_ADD_NEW);
  Object *new_object = BKE_object_add_only_object(bmain, OB_MESH, "Additional Skin");
  ViewLayer *source_layer = view_layer;
  Vector<Object *> meshes = {object};
  EXPECT_TRUE(source_membership_update_for_test(
      *scene, source_layer, meshes, alternate, {&new_object, 1}));
  EXPECT_EQ(source_layer, view_layer);
  ASSERT_EQ(meshes.size(), 2);
  EXPECT_EQ(meshes[0], object);
  EXPECT_EQ(meshes[1], new_object);
  EXPECT_FALSE(
      source_membership_update_for_test(*scene, source_layer, meshes, view_layer, {&object, 1}));
  EXPECT_FALSE(source_membership_update_for_test(
      *scene, source_layer, meshes, alternate, {&new_object, 1}));
}

TEST_F(MeshOnionEvaluationTest, RemovedSourceViewLayerUsesValidRequestLayer)
{
  ViewLayer *alternate = BKE_view_layer_add(bmain, scene, "Alternate", nullptr, VIEWLAYER_ADD_NEW);
  ViewLayer *source_layer = view_layer;
  Vector<Object *> meshes = {object};
  /* Keep the removed layer allocation alive only to restore fixture ownership afterward. The
   * production helper must treat scene membership, not pointer non-nullness, as validity. */
  BLI_remlink(&scene->view_layers, view_layer);
  EXPECT_FALSE(
      source_membership_update_for_test(*scene, source_layer, meshes, view_layer, {&object, 1}));
  EXPECT_TRUE(
      source_membership_update_for_test(*scene, source_layer, meshes, alternate, {&object, 1}));
  EXPECT_EQ(source_layer, alternate);
  EXPECT_EQ(meshes.size(), 1);
  EXPECT_FALSE(
      source_membership_update_for_test(*scene, source_layer, meshes, alternate, {&object, 1}));
  BLI_addtail(&scene->view_layers, view_layer);
}

TEST_F(MeshOnionEvaluationTest, NonStandardIKAndEditMeshAreRejected)
{
  rig->pose->iksolver = IKSOLVER_ITASC;
  std::string error;
  EXPECT_EQ(evaluate_preview(nullptr, error), nullptr);
  EXPECT_NE(error.find("iTaSC"), std::string::npos);
  rig->pose->iksolver = IKSOLVER_STANDARD;
  const BMeshCreateParams params{};
  BMesh *bmesh = BM_mesh_create(&bm_mesh_allocsize_default, &params);
  mesh->runtime->edit_mesh = std::shared_ptr<BMEditMesh>(BKE_editmesh_create(bmesh),
                                                         [](BMEditMesh *edit_mesh) {
                                                           BKE_editmesh_free_data(edit_mesh);
                                                           MEM_delete(edit_mesh);
                                                         });
  error.clear();
  EXPECT_EQ(evaluate_preview(nullptr, error), nullptr);
  EXPECT_NE(error.find("edited"), std::string::npos);
  mesh->runtime->edit_mesh.reset();
}

TEST_F(MeshOnionEvaluationTest, LegacyPoseGeometryTagKeepsUnchangedArmature)
{
  rig->mode = OB_MODE_POSE;
  const auto definition = armature_rest_capture_for_test(*armature);
  const int source_users = rig->id.us;
  const auto graph = active_graph_for_tags();
  /* Obtain the actual compatibility flags from Blender's native tagger and an active graph,
   * rather than repeating the predicate's mask in this regression test. */
  armature->id.recalc = 0;
  DEG_id_tag_update_ex(bmain, &rig->id, ID_RECALC_GEOMETRY);
  const uint32_t compatibility_flags = armature->id.recalc;
  ASSERT_NE(compatibility_flags & ID_RECALC_PARAMETERS, 0);
  EXPECT_TRUE(keep_pose_armature_update_for_test(
      *rig, *definition, armature->id, compatibility_flags, true));
  Bone *bone = armature->bonebase.first();
  bone->flag |= BONE_SELECTED | BONE_TRANSFORM | BONE_TRANSFORM_MIRROR;
  armature->act_bone = bone;
  BKE_armature_bone_hash_make(armature);
  EXPECT_TRUE(keep_pose_armature_update_for_test(
      *rig, *definition, armature->id, compatibility_flags, true));
  EXPECT_EQ(rig->id.us, source_users);
  EXPECT_FALSE(keep_pose_armature_update_for_test(
      *rig, *definition, armature->id, compatibility_flags, false));
  EXPECT_FALSE(keep_pose_armature_update_for_test(
      *rig, *definition, armature->id, compatibility_flags | ID_RECALC_ANIMATION, true));
  EXPECT_FALSE(keep_pose_armature_update_for_test(
      *rig, *definition, armature->id, compatibility_flags | ID_RECALC_SOURCE, true));
  EXPECT_FALSE(keep_pose_armature_update_for_test(
      *rig, *definition, armature->id, ID_RECALC_PARAMETERS, true));
  EXPECT_FALSE(keep_pose_armature_update_for_test(
      *rig, *definition, rig->adt->action->id, compatibility_flags, true));
  EXPECT_FALSE(
      keep_pose_armature_update_for_test(*rig, *definition, mesh->id, compatibility_flags, true));
}

TEST_F(MeshOnionEvaluationTest, LegacyPoseTagStillInvalidatesChangedRestAndProperties)
{
  rig->mode = OB_MODE_POSE;
  Bone *bone = armature->bonebase.first();
  IDPropertyTemplate value{};
  value.i = 1;
  armature->id.properties = IDP_New(IDP_GROUP, &value, "properties");
  IDP_AddToGroup(armature->id.properties, IDP_New(IDP_INT, &value, "control"));
  bone->prop = IDP_New(IDP_GROUP, &value, "bone properties");
  IDP_AddToGroup(bone->prop, IDP_New(IDP_INT, &value, "bone control"));
  value.id = &object->id;
  IDP_AddToGroup(armature->id.properties, IDP_New(IDP_ID, &value, "target"));
  const int source_target_users = object->id.us;
  auto definition = armature_rest_capture_for_test(*armature);
  EXPECT_EQ(object->id.us, source_target_users);
  const auto graph = active_graph_for_tags();
  armature->id.recalc = 0;
  DEG_id_tag_update_ex(bmain, &rig->id, ID_RECALC_GEOMETRY);
  const uint32_t compatibility_flags = armature->id.recalc;
  auto retained = [&]() {
    return keep_pose_armature_update_for_test(
        *rig, *definition, armature->id, compatibility_flags, true);
  };
  ASSERT_TRUE(retained());
  bone->arm_mat[3][0] += 0.1f;
  EXPECT_FALSE(retained());
  bone->arm_mat[3][0] -= 0.1f;
  ASSERT_TRUE(retained());
  bone->segments += 1;
  EXPECT_FALSE(retained());
  bone->segments -= 1;
  bone->bbone_next = armature->bonebase.last();
  EXPECT_FALSE(retained());
  bone->bbone_next = nullptr;
  bone->parent = armature->bonebase.last();
  EXPECT_FALSE(retained());
  bone->parent = nullptr;
  armature->flag |= ARM_RESTPOS;
  EXPECT_FALSE(retained());
  armature->flag &= ~ARM_RESTPOS;
  IDProperty *property = IDP_GetPropertyFromGroup(armature->id.properties, "control");
  IDP_int_set(property, 2);
  EXPECT_FALSE(retained());
  IDP_int_set(property, 1);
  property = IDP_GetPropertyFromGroup(bone->prop, "bone control");
  IDP_int_set(property, 2);
  EXPECT_FALSE(retained());
  IDP_int_set(property, 1);
  EXPECT_TRUE(retained());
  definition.reset();
  EXPECT_EQ(object->id.us, source_target_users);
}

}  // namespace blender::ed::mesh_onion_skin::tests
