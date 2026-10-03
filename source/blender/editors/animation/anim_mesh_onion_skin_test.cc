/* SPDX-FileCopyrightText: 2026 Blender Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

#include "anim_mesh_onion_skin_intern.hh"

#include <cstring>

#include "ED_mesh_onion_skin_preview.hh"

#include "ANIM_action.hh"
#include "ANIM_keyframing.hh"

#include "BKE_action.hh"
#include "BKE_anim_data.hh"
#include "BKE_animsys.hh"
#include "BKE_armature.hh"
#include "BKE_collection.hh"
#include "BKE_constraint.h"
#include "BKE_deform.hh"
#include "BKE_editmesh.hh"
#include "BKE_fcurve.hh"
#include "BKE_fcurve_driver.h"
#include "BKE_gtest_base.hh"
#include "BKE_idprop.hh"
#include "BKE_layer.hh"
#include "BKE_lib_id.hh"
#include "BKE_main.hh"
#include "BKE_material.hh"
#include "BKE_mesh.hh"
#include "BKE_modifier.hh"
#include "BKE_nla.hh"
#include "BKE_object.hh"
#include "BKE_scene.hh"

#include "BLI_listbase.hh"
#include "BLI_math_base.hh"
#include "BLI_math_vector.hh"
#include "BLI_string.hh"

#include "DNA_action_types.h"
#include "DNA_anim_types.h"
#include "DNA_armature_types.h"
#include "DNA_constraint_types.h"
#include "DNA_layer_types.h"
#include "DNA_material_types.h"
#include "DNA_mesh_types.h"
#include "DNA_modifier_types.h"
#include "DNA_object_types.h"
#include "DNA_scene_types.h"

#include "DEG_depsgraph.hh"
#include "DEG_depsgraph_build.hh"
#include "DEG_depsgraph_query.hh"

#include "MEM_guardedalloc.h"
#include "RNA_access.hh"
#include "RNA_path.hh"
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

  ChannelDriver *add_control_driver(
      ID *target,
      const char *path,
      const eDriverVar_Types type = DVAR_TYPE_SINGLE_PROP,
      const eDriverTarget_ContextProperty context = DTAR_CONTEXT_PROPERTY_ACTIVE_SCENE)
  {
    FCurve *curve = BKE_fcurve_create();
    curve->rna_path_set("pose.bones[\"Control\"].location");
    curve->array_index = 0;
    curve->driver = MEM_new<ChannelDriver>(__func__);
    curve->driver->type = DRIVER_TYPE_PYTHON;
    STRNCPY(curve->driver->expression, "var + frame * 0.02");
    DriverVar *variable = driver_add_new_variable(curve->driver);
    STRNCPY(variable->name, "var");
    variable->type = type;
    variable->num_targets = 1;
    variable->targets[0].id = target;
    variable->targets[0].rna_path = BLI_strdup(path);
    variable->targets[0].context_property = context;
    BLI_addtail(&rig->adt->drivers, curve);
    return curve->driver;
  }

  void expect_native_geometry(const Bundle &bundle, const bool timeline = false)
  {
    std::unique_ptr<Depsgraph, decltype(&DEG_graph_free)> graph(
        DEG_graph_new(bmain, scene, view_layer, DAG_EVAL_VIEWPORT), DEG_graph_free);
    ID *targets[] = {&rig->id, &object->id};
    DEG_graph_build_from_ids(graph.get(), Span<ID *>(targets, 2));
    for (const Sample &sample : timeline ? bundle.timeline_samples : bundle.samples) {
      BKE_scene_frame_set(scene, sample.frame);
      DEG_evaluate_on_framechange(graph.get(), sample.frame);
      const Mesh *evaluated = BKE_object_get_evaluated_mesh_no_subsurf(
          DEG_get_evaluated(graph.get(), object));
      ASSERT_NE(evaluated, nullptr);
      ASSERT_EQ(evaluated->verts_num, sample.geometry->positions.size());
      for (const int vertex : sample.geometry->positions.index_range()) {
        EXPECT_LT(math::distance(sample.geometry->positions[vertex],
                                 evaluated->vert_positions()[vertex]),
                  1.0e-5f);
      }
      EXPECT_EQ(sample.geometry->triangles.size(), evaluated->corner_tris().size());
    }
  }
};

TEST_F(MeshOnionEvaluationTest, TimelineCenterAndOverlappingWindowsShareExactNativeGeometry)
{
  const EvaluationRequestForTest requests[] = {{5.0f}, {7.0f}};
  std::string error;
  const auto bundles = evaluate_sequence_for_test(
      *bmain, *scene, *view_layer, *rig, {&object, 1}, {2, 2, 2}, requests, error);
  ASSERT_EQ(bundles.size(), 2) << error;
  EXPECT_FLOAT_EQ(bundles[0]->frame, 5.0f);
  ASSERT_EQ(bundles[0]->samples.size(), 4);
  ASSERT_EQ(bundles[0]->timeline_samples.size(), 7);
  EXPECT_EQ(bundles[0]->bytes, size_t(7 * (3 * sizeof(float3) + sizeof(int3))));
  bool found_center = false;
  for (const Sample &first : bundles[0]->timeline_samples) {
    found_center |= first.frame == 5.0f && first.temporal_offset == 0.0f;
    for (const Sample &second : bundles[1]->timeline_samples) {
      if (first.frame == second.frame) {
        EXPECT_EQ(first.geometry, second.geometry);
        EXPECT_FLOAT_EQ(first.rank, second.rank);
      }
    }
    for (const Sample &relative : bundles[0]->samples) {
      if (first.frame == relative.frame) {
        EXPECT_EQ(first.geometry, relative.geometry);
      }
    }
  }
  EXPECT_TRUE(found_center);
  for (const Sample &relative : bundles[0]->samples) {
    EXPECT_NE(relative.frame, 5.0f);
  }
  expect_native_geometry(*bundles[0], true);
  expect_native_geometry(*bundles[1], true);
}

TEST_F(MeshOnionEvaluationTest, PreviewGeometryRevisionInvalidatesEveryCachedPose)
{
  const EvaluationRequestForTest requests[] = {
      {5.0f, 1, nullptr}, {7.0f, 1, nullptr}, {5.0f, 2, proposal(0.8f)}};
  const int users = rig->id.us;
  std::string error;
  const auto bundles = evaluate_sequence_for_test(
      *bmain, *scene, *view_layer, *rig, {&object, 1}, {2, 2, 2}, requests, error);
  ASSERT_EQ(bundles.size(), 3) << error;
  EXPECT_EQ(bundles[2]->geometry_revision, 2);
  bool compared_center = false;
  for (const Sample &first : bundles[0]->timeline_samples) {
    for (const Sample &edited : bundles[2]->timeline_samples) {
      if (first.frame != edited.frame) {
        continue;
      }
      EXPECT_NE(first.geometry, edited.geometry);
      if (first.frame == 5.0f) {
        compared_center = true;
        EXPECT_GT(math::distance(first.geometry->positions[2], edited.geometry->positions[2]),
                  0.05f);
      }
    }
  }
  EXPECT_TRUE(compared_center);
  EXPECT_EQ(rig->id.us, users);
}

TEST_F(MeshOnionEvaluationTest, SelfPropertyNativeDriverMatchesNativeGeometryAndPreservesSource)
{
  IDProperty *properties = IDP_EnsureProperties(&rig->id);
  IDP_AddToGroup(properties, bke::idprop::create("strength", 0.6f).release());
  ChannelDriver *driver = add_control_driver(&rig->id, "[\"strength\"]");
  const int users = rig->id.us;
  std::string error;
  const auto bundle = evaluate_preview(nullptr, error);
  ASSERT_NE(bundle, nullptr) << error;
  ASSERT_EQ(bundle->samples.size(), 2);
  EXPECT_EQ(driver->expr_simple, nullptr);
  EXPECT_EQ(driver->expr_comp, nullptr);
  EXPECT_FLOAT_EQ(driver->curval, 0.0f);
  EXPECT_EQ(rig->id.us, users);
  EXPECT_GT(math::distance(bundle->samples[0].geometry->positions[2], mesh->vert_positions()[2]),
            0.05f);
  expect_native_geometry(*bundle);
}

TEST_F(MeshOnionEvaluationTest, ExternalAnimatedObjectDriverUsesCopiedAction)
{
  Object *target = BKE_object_add_only_object(bmain, OB_EMPTY, "Driver Target");
  BKE_collection_object_add(bmain, scene->master_collection, target);
  PointerRNA ptr = RNA_id_pointer_create(&target->id);
  for (const float frame : {1.0f, 10.0f}) {
    target->loc[0] = frame * 0.08f;
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
  add_control_driver(&target->id, "location[0]");
  const int users = target->id.us;
  const float source_x = target->loc[0];
  std::string error;
  const auto bundle = evaluate_preview(nullptr, error);
  ASSERT_NE(bundle, nullptr) << error;
  EXPECT_EQ(target->id.us, users);
  EXPECT_FLOAT_EQ(target->loc[0], source_x);
  EXPECT_GT(math::distance(bundle->samples[0].geometry->positions[2],
                           bundle->samples[1].geometry->positions[2]),
            0.01f);
  expect_native_geometry(*bundle);
}

TEST_F(MeshOnionEvaluationTest, SceneAndContextPropertyDriversUsePrivateSceneInputs)
{
  IDP_AddToGroup(IDP_EnsureProperties(&scene->id),
                 bke::idprop::create("strength", 0.6f).release());
  ChannelDriver *driver = add_control_driver(&scene->id, "[\"strength\"]");
  std::string error;
  auto bundle = evaluate_preview(nullptr, error);
  ASSERT_NE(bundle, nullptr) << error;
  expect_native_geometry(*bundle);
  DriverVar *variable = driver->variables.first();
  variable->type = DVAR_TYPE_CONTEXT_PROP;
  variable->targets[0].id = nullptr;
  bundle = evaluate_preview(nullptr, error);
  ASSERT_NE(bundle, nullptr) << error;
  expect_native_geometry(*bundle);
  view_layer->id_properties = IDP_New(IDP_GROUP, nullptr, "Properties");
  IDP_AddToGroup(view_layer->id_properties, bke::idprop::create("strength", 0.4f).release());
  variable->targets[0].context_property = DTAR_CONTEXT_PROPERTY_ACTIVE_VIEW_LAYER;
  bundle = evaluate_preview(nullptr, error);
  ASSERT_NE(bundle, nullptr) << error;
  expect_native_geometry(*bundle);
}

TEST_F(MeshOnionEvaluationTest, PythonNamespaceDriverIsRejectedWithoutCompilingSource)
{
  ChannelDriver *driver = add_control_driver(&rig->id, "location[0]");
  STRNCPY(driver->expression, "bpy.context.scene.frame_current");
  std::string error;
  EXPECT_EQ(evaluate_preview(nullptr, error), nullptr);
  EXPECT_NE(error.find("Python namespace"), std::string::npos);
  EXPECT_EQ(driver->expr_simple, nullptr);
  EXPECT_EQ(driver->expr_comp, nullptr);
}

TEST_F(MeshOnionEvaluationTest, MirrorAndSolidifyRetainNativeGeometry)
{
  ModifierData *mirror = BKE_modifier_new(eModifierType_Mirror);
  BKE_modifiers_persistent_uid_init(*object, *mirror);
  BLI_addhead(&object->modifiers, mirror);
  ModifierData *solidify = BKE_modifier_new(eModifierType_Solidify);
  BKE_modifiers_persistent_uid_init(*object, *solidify);
  BLI_addtail(&object->modifiers, solidify);
  std::string error;
  const auto bundle = evaluate_preview(nullptr, error);
  ASSERT_NE(bundle, nullptr) << error;
  EXPECT_GT(bundle->samples[0].geometry->positions.size(), mesh->verts_num);
  EXPECT_EQ(BLI_listbase_count(&object->modifiers), 3);
  expect_native_geometry(*bundle);
}

TEST_F(MeshOnionEvaluationTest, LoopSnapshotsEvaluateWrappedFramesWithSignedDistances)
{
  scene->r.sfra = 1;
  scene->r.efra = 10;
  const int source_frame = scene->r.cfra;
  std::string error;
  const auto bundle = evaluate_for_test(*bmain,
                                        *scene,
                                        *view_layer,
                                        *rig,
                                        {&object, 1},
                                        {2, 2, 1, false, true},
                                        1.0f,
                                        nullptr,
                                        error);
  ASSERT_NE(bundle, nullptr) << error;
  ASSERT_EQ(bundle->samples.size(), 4);
  const float frames[] = {9.0f, 10.0f, 2.0f, 3.0f};
  const float offsets[] = {-2.0f, -1.0f, 1.0f, 2.0f};
  for (int index = 0; index < 4; index++) {
    EXPECT_FLOAT_EQ(bundle->samples[index].frame, frames[index]);
    EXPECT_FLOAT_EQ(bundle->samples[index].temporal_offset, offsets[index]);
  }
  EXPECT_EQ(scene->r.cfra, source_frame);
  expect_native_geometry(*bundle);
}

TEST_F(MeshOnionEvaluationTest, KeyedSnapshotsIgnoreStepAndExcludeProvisionalKeys)
{
  const auto prepared = proposal(0.6f);
  ASSERT_NE(prepared, nullptr);
  std::string error;
  const auto bundle = evaluate_for_test(*bmain,
                                        *scene,
                                        *view_layer,
                                        *rig,
                                        {&object, 1},
                                        {2, 2, 37, true, false},
                                        5.0f,
                                        prepared,
                                        error);
  ASSERT_NE(bundle, nullptr) << error;
  ASSERT_EQ(bundle->samples.size(), 2);
  EXPECT_FLOAT_EQ(bundle->samples[0].frame, 1.0f);
  EXPECT_FLOAT_EQ(bundle->samples[0].temporal_offset, -4.0f);
  EXPECT_FLOAT_EQ(bundle->samples[1].frame, 10.0f);
  EXPECT_FLOAT_EQ(bundle->samples[1].temporal_offset, 5.0f);
  insert_control(5.0f, 0.6f);
  expect_native_geometry(*bundle);
}

TEST_F(MeshOnionEvaluationTest, IndexedDriverCannotReadStrippedMaterialProperty)
{
  Material *material = BKE_material_add(bmain, "Driver Material");
  IDP_AddToGroup(IDP_EnsureProperties(&scene->id),
                 bke::idprop::create("material", &material->id).release());
  ChannelDriver *driver = add_control_driver(
      nullptr, "[\"material\"].diffuse_color[0]", DVAR_TYPE_CONTEXT_PROP);
  const int users = material->id.us;
  std::string error;
  EXPECT_EQ(evaluate_preview(nullptr, error), nullptr);
  EXPECT_NE(error.find("shading dependency"), std::string::npos);
  EXPECT_EQ(material->id.us, users);
  DriverVar &variable = *driver->variables.first();
  variable.type = DVAR_TYPE_SINGLE_PROP;
  variable.targets[0].id = &scene->id;
  error.clear();
  EXPECT_EQ(evaluate_preview(nullptr, error), nullptr);
  EXPECT_NE(error.find("shading dependency"), std::string::npos);
  EXPECT_EQ(material->id.us, users);
}

TEST_F(MeshOnionEvaluationTest, DriverCannotTraverseStrippedMaterialBackToObject)
{
  Material *material = BKE_material_add(bmain, "Driver Material");
  IDP_AddToGroup(IDP_EnsureProperties(&scene->id),
                 bke::idprop::create("material", &material->id).release());
  IDP_AddToGroup(IDP_EnsureProperties(&material->id),
                 bke::idprop::create("rig", &rig->id).release());
  const char *path = "[\"material\"][\"rig\"].location[0]";
  PointerRNA pointer = RNA_id_pointer_create(&scene->id);
  PointerRNA resolved;
  PropertyRNA *property;
  int array_index;
  ASSERT_TRUE(RNA_path_resolve_property_full(&pointer, path, &resolved, &property, &array_index));
  ASSERT_EQ(resolved.owner_id, &rig->id);
  ChannelDriver *driver = add_control_driver(nullptr, path, DVAR_TYPE_CONTEXT_PROP);
  const int material_users = material->id.us;
  const int rig_users = rig->id.us;
  for (const eDriverVar_Types type : {DVAR_TYPE_CONTEXT_PROP, DVAR_TYPE_SINGLE_PROP}) {
    DriverVar &variable = *driver->variables.first();
    variable.type = type;
    variable.targets[0].id = type == DVAR_TYPE_SINGLE_PROP ? &scene->id : nullptr;
    std::string error;
    EXPECT_EQ(evaluate_preview(nullptr, error), nullptr);
    EXPECT_NE(error.find("shading dependency"), std::string::npos);
    EXPECT_EQ(material->id.us, material_users);
    EXPECT_EQ(rig->id.us, rig_users);
  }
}

TEST_F(MeshOnionEvaluationTest, InvalidDriverPrefixCannotHideStrippedMaterial)
{
  Material *material = BKE_material_add(bmain, "Driver Material");
  IDP_AddToGroup(IDP_EnsureProperties(&scene->id),
                 bke::idprop::create("material", &material->id).release());
  add_control_driver(nullptr, "[\"material\"].missing_property", DVAR_TYPE_CONTEXT_PROP);
  const int users = material->id.us;
  std::string error;
  EXPECT_EQ(evaluate_preview(nullptr, error), nullptr);
  EXPECT_NE(error.find("shading dependency"), std::string::npos);
  EXPECT_EQ(material->id.us, users);
}

TEST_F(MeshOnionEvaluationTest, DriverCannotReadSceneSettingsThroughObjectProperty)
{
  scene->r.xsch = 3840;
  IDP_AddToGroup(IDP_EnsureProperties(&rig->id),
                 bke::idprop::create("scene", &scene->id).release());
  add_control_driver(&rig->id, "[\"scene\"].render.resolution_x");
  const int users = scene->id.us;
  std::string error;
  EXPECT_EQ(evaluate_preview(nullptr, error), nullptr);
  EXPECT_NE(error.find("redirected Scene"), std::string::npos);
  EXPECT_EQ(scene->id.us, users);
  EXPECT_EQ(scene->r.xsch, 3840);
}

TEST_F(MeshOnionEvaluationTest, DriverCannotReenterSceneThroughSelfReference)
{
  IDP_AddToGroup(IDP_EnsureProperties(&scene->id),
                 bke::idprop::create("self", &scene->id).release());
  ChannelDriver *driver = add_control_driver(
      nullptr, "[\"self\"].render.resolution_x", DVAR_TYPE_CONTEXT_PROP);
  const int users = scene->id.us;
  for (const eDriverVar_Types type : {DVAR_TYPE_CONTEXT_PROP, DVAR_TYPE_SINGLE_PROP}) {
    DriverVar &variable = *driver->variables.first();
    variable.type = type;
    variable.targets[0].id = type == DVAR_TYPE_SINGLE_PROP ? &scene->id : nullptr;
    std::string error;
    EXPECT_EQ(evaluate_preview(nullptr, error), nullptr);
    EXPECT_NE(error.find("redirected Scene"), std::string::npos);
    EXPECT_EQ(scene->id.us, users);
  }
}

TEST_F(MeshOnionEvaluationTest, DriverCannotReenterSceneThroughObjectReference)
{
  IDP_AddToGroup(IDP_EnsureProperties(&scene->id), bke::idprop::create("rig", &rig->id).release());
  IDP_AddToGroup(IDP_EnsureProperties(&rig->id),
                 bke::idprop::create("scene", &scene->id).release());
  add_control_driver(nullptr, "[\"rig\"][\"scene\"].render.resolution_x", DVAR_TYPE_CONTEXT_PROP);
  const int scene_users = scene->id.us;
  const int rig_users = rig->id.us;
  std::string error;
  EXPECT_EQ(evaluate_preview(nullptr, error), nullptr);
  EXPECT_NE(error.find("redirected Scene"), std::string::npos);
  EXPECT_EQ(scene->id.us, scene_users);
  EXPECT_EQ(rig->id.us, rig_users);
}

TEST_F(MeshOnionEvaluationTest, InvalidNativePropertyDriverRetainsFallback)
{
  ChannelDriver *driver = add_control_driver(&rig->id, "[\"missing_property\"]");
  DriverTarget &target = driver->variables.first()->targets[0];
  target.options = DTAR_OPTION_USE_FALLBACK;
  target.fallback_value = 0.6f;
  const int users = rig->id.us;
  std::string error;
  const auto bundle = evaluate_preview(nullptr, error);
  ASSERT_NE(bundle, nullptr) << error;
  EXPECT_EQ(rig->id.us, users);
  EXPECT_EQ(driver->expr_simple, nullptr);
  EXPECT_EQ(driver->expr_comp, nullptr);
  expect_native_geometry(*bundle);
}

TEST_F(MeshOnionEvaluationTest, NestedNlaControlDriverCannotExecutePython)
{
  NlaTrack *track = BKE_nlatrack_new_tail(&rig->adt->nla_tracks, false);
  NlaStrip *meta = MEM_new<NlaStrip>(__func__);
  meta->type = NLASTRIP_TYPE_META;
  BLI_addtail(&track->strips, meta);
  NlaStrip *strip = BKE_nlastrip_new_for_slot(rig->adt->action, rig->adt->slot_handle, rig->id);
  BLI_addtail(&meta->strips, strip);
  FCurve *curve = BKE_fcurve_create();
  curve->rna_path_set("influence");
  curve->driver = MEM_new<ChannelDriver>(__func__);
  curve->driver->type = DRIVER_TYPE_PYTHON;
  STRNCPY(curve->driver->expression, "bpy.context.scene.frame_current");
  BLI_addtail(&strip->fcurves, curve);
  std::string error;
  EXPECT_EQ(evaluate_preview(nullptr, error), nullptr);
  EXPECT_NE(error.find("NLA strip controls"), std::string::npos);
  EXPECT_EQ(curve->driver->expr_simple, nullptr);
  EXPECT_EQ(curve->driver->expr_comp, nullptr);
}

TEST_F(MeshOnionEvaluationTest, DisabledConstructiveStackIsBoundedBeforeEvaluation)
{
  for (int index = 0; index < 10; index++) {
    ModifierData *modifier = BKE_modifier_new(eModifierType_Mirror);
    reinterpret_cast<MirrorModifierData *>(modifier)->flag = MOD_MIR_AXIS_X | MOD_MIR_AXIS_Y |
                                                             MOD_MIR_AXIS_Z;
    modifier->mode &= ~eModifierMode_Realtime;
    BKE_modifiers_persistent_uid_init(*object, *modifier);
    BLI_addhead(&object->modifiers, modifier);
  }
  std::string error;
  EXPECT_EQ(evaluate_preview(nullptr, error), nullptr);
  EXPECT_NE(error.find("cache budget"), std::string::npos);
  EXPECT_EQ(mesh->verts_num, 3);
  EXPECT_EQ(BLI_listbase_count(&object->modifiers), 11);
}

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
    EXPECT_GT(math::distance(sample.geometry->positions[2], mesh->vert_positions()[2]), 0.05f);
  }
  EXPECT_GT(math::distance(bundle->samples[0].geometry->positions[2],
                           bundle->samples[1].geometry->positions[2]),
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
    ASSERT_EQ(sample.geometry->positions.size(), evaluated_mesh->verts_num);
    for (int vertex = 0; vertex < sample.geometry->positions.size(); vertex++) {
      for (int axis = 0; axis < 3; axis++) {
        EXPECT_NEAR(sample.geometry->positions[vertex][axis],
                    evaluated_mesh->vert_positions()[vertex][axis],
                    1.0e-5f);
      }
    }
    EXPECT_EQ(sample.geometry->triangles.size(), 1);
    EXPECT_EQ(sample.geometry->triangles[0], int3(0, 1, 2));
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
    EXPECT_EQ(sample.geometry->positions.size(), 3);
    EXPECT_EQ(sample.geometry->triangles.size(), 1);
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
  EXPECT_GT(math::abs(bundle->samples[0].geometry->object_to_world[3][0] -
                      bundle->samples[1].geometry->object_to_world[3][0]),
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
    for (int vertex = 0; vertex < sample.geometry->positions.size(); vertex++) {
      EXPECT_LT(math::distance(sample.geometry->positions[vertex],
                               evaluated_mesh->vert_positions()[vertex]),
                1.0e-5f);
    }
    for (int column = 0; column < 4; column++) {
      for (int row = 0; row < 4; row++) {
        EXPECT_NEAR(sample.geometry->object_to_world[column][row],
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
