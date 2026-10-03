/* SPDX-FileCopyrightText: 2026 Blender Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

#include <array>
#include <chrono>
#include <condition_variable>
#include <cstddef>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <mutex>
#include <optional>

#include "MEM_guardedalloc.h"

#include "BLI_listbase.hh"
#include "BLI_map.hh"
#include "BLI_math_base.hh"
#include "BLI_set.hh"
#include "BLI_string.hh"
#include "BLI_task.hh"
#include "BLI_time.hh"
#include "BLI_utildefines.hh"

#include "DNA_action_types.h"
#include "DNA_anim_types.h"
#include "DNA_armature_types.h"
#include "DNA_constraint_types.h"
#include "DNA_key_types.h"
#include "DNA_layer_types.h"
#include "DNA_mesh_types.h"
#include "DNA_modifier_types.h"
#include "DNA_object_types.h"
#include "DNA_scene_types.h"
#include "DNA_screen_types.h"
#include "DNA_view3d_types.h"
#include "DNA_windowmanager_types.h"

#include "BKE_anim_data.hh"
#include "BKE_armature.hh"
#include "BKE_collection.hh"
#include "BKE_context.hh"
#include "BKE_fcurve_driver.h"
#include "BKE_global.hh"
#include "BKE_idprop.hh"
#include "BKE_layer.hh"
#include "BKE_lib_id.hh"
#include "BKE_lib_query.hh"
#include "BKE_lib_remap.hh"
#include "BKE_main.hh"
#include "BKE_mesh.hh"
#include "BKE_object.hh"
#include "BKE_object_types.hh"
#include "BKE_scene.hh"

#include "DEG_depsgraph.hh"
#include "DEG_depsgraph_build.hh"
#include "DEG_depsgraph_query.hh"

#include "WM_api.hh"
#include "WM_types.hh"

#include "ED_mesh_onion_skin.hh"
#include "ED_mesh_onion_skin_preview.hh"

#include "RNA_access.hh"
#include "RNA_path.hh"
#include "RNA_prototypes.hh"

#include "ANIM_action_legacy.hh"

#include "anim_mesh_onion_skin_frames.hh"

#ifdef WITH_GTESTS
#  include "anim_mesh_onion_skin_intern.hh"
#endif

namespace blender::ed::mesh_onion_skin {

struct PropertySnapshotDeleter {
  void operator()(IDProperty *property) const
  {
    IDP_FreeProperty_ex(property, false);
  }
};

using PropertySnapshot = std::unique_ptr<IDProperty, PropertySnapshotDeleter>;

static PropertySnapshot property_snapshot(const IDProperty *property)
{
  return PropertySnapshot(
      property ? IDP_CopyProperty_ex(property, LIB_ID_CREATE_NO_USER_REFCOUNT) : nullptr);
}

/* Main-thread-only authored definitions. Keeping the entire DNA prefix makes new rest/deform
 * fields conservative by default; only explicitly identified selection and runtime state is
 * normalized. Pointer values describe source hierarchy identity, never worker-accessible links. */
struct BoneRestDefinition {
  std::array<std::byte, offsetof(Bone, runtime)> data;
  PropertySnapshot properties;
  PropertySnapshot system_properties;
};

struct ArmatureRestDefinition {
  std::array<std::byte, offsetof(bArmature, runtime) - offsetof(bArmature, adt)> data;
  std::optional<std::array<std::byte, sizeof(AnimData)>> animation;
  PropertySnapshot properties;
  PropertySnapshot system_properties;
  Vector<BoneRestDefinition> bones;
};

static auto bone_rest_bytes(const Bone &source)
{
  Bone bone = dna::shallow_copy(source);
  bone.flag &= ~(BONE_SELECTED | BONE_ROOTSEL | BONE_TIPSEL | BONE_TRANSFORM | BONE_DONE |
                 BONE_DRAW_ACTIVE | BONE_TRANSFORM_MIRROR);
  std::array<std::byte, offsetof(Bone, runtime)> data;
  std::memcpy(data.data(), &bone, data.size());
  return data;
}

static auto armature_rest_bytes(const bArmature &source)
{
  bArmature armature = dna::shallow_copy(source);
  armature.bonehash = nullptr;
  armature.act_bone = nullptr;
  armature.act_edbone = nullptr;
  std::array<std::byte, offsetof(bArmature, runtime) - offsetof(bArmature, adt)> data;
  std::memcpy(data.data(),
              reinterpret_cast<const std::byte *>(&armature) + offsetof(bArmature, adt),
              data.size());
  return data;
}

static auto armature_animation_bytes(const bArmature &armature)
{
  std::optional<std::array<std::byte, sizeof(AnimData)>> data;
  if (armature.adt) {
    AnimData animation = *armature.adt;
    animation.driver_array = nullptr;
    data.emplace();
    std::memcpy(data->data(), &animation, data->size());
  }
  return data;
}

static std::shared_ptr<const ArmatureRestDefinition> armature_rest_capture(
    const bArmature &armature)
{
  auto definition = std::make_shared<ArmatureRestDefinition>();
  definition->data = armature_rest_bytes(armature);
  definition->animation = armature_animation_bytes(armature);
  definition->properties = property_snapshot(armature.id.properties);
  definition->system_properties = property_snapshot(armature.id.system_properties);
  auto append = [&](auto &&self, const ListBaseT<Bone> &bones) -> void {
    for (const Bone &bone : bones) {
      definition->bones.append({bone_rest_bytes(bone),
                                property_snapshot(bone.prop),
                                property_snapshot(bone.system_properties)});
      self(self, bone.childbase);
    }
  };
  append(append, armature.bonebase);
  return definition;
}

static bool armature_rest_matches(const bArmature &armature,
                                  const ArmatureRestDefinition &definition)
{
  if (definition.data != armature_rest_bytes(armature) ||
      definition.animation != armature_animation_bytes(armature) ||
      !IDP_EqualsProperties(armature.id.properties, definition.properties.get()) ||
      !IDP_EqualsProperties(armature.id.system_properties, definition.system_properties.get()))
  {
    return false;
  }
  int index = 0;
  auto matches = [&](auto &&self, const ListBaseT<Bone> &bones) -> bool {
    for (const Bone &bone : bones) {
      if (index == definition.bones.size()) {
        return false;
      }
      const BoneRestDefinition &rest = definition.bones[index++];
      if (rest.data != bone_rest_bytes(bone) ||
          !IDP_EqualsProperties(bone.prop, rest.properties.get()) ||
          !IDP_EqualsProperties(bone.system_properties, rest.system_properties.get()) ||
          !self(self, bone.childbase))
      {
        return false;
      }
    }
    return true;
  };
  return matches(matches, armature.bonebase) && index == definition.bones.size();
}

static bool keep_pose_armature_update(const Object &rig,
                                      const ArmatureRestDefinition &definition,
                                      const ID &changed_id,
                                      const uint32_t recalc,
                                      const bool supported_modal_pose)
{
  /* Match deg_recalc_flags_for_legacy_zero(): tagging a pose object for GEOMETRY also tags its
   * Armature data with legacy flags=0. Any explicit SOURCE/ANIMATION update stays invalidating. */
  constexpr uint32_t legacy_recalc = uint32_t(ID_RECALC_ALL) &
                                     ~(ID_RECALC_PSYS_ALL | ID_RECALC_ANIMATION |
                                       ID_RECALC_FRAME_CHANGE | ID_RECALC_SOURCE |
                                       ID_RECALC_EDITORS | ID_RECALC_PROVISION_28 |
                                       ID_RECALC_PROVISION_29 | ID_RECALC_PROVISION_30 |
                                       ID_RECALC_PROVISION_31);
  return supported_modal_pose && rig.type == OB_ARMATURE && rig.mode & OB_MODE_POSE &&
         rig.data == &changed_id && recalc == legacy_recalc &&
         armature_rest_matches(*id_cast<const bArmature *>(&changed_id), definition);
}

/* The capsule is owned by the job, not by an evaluated object or a viewport. Its Main and every
 * evaluable ID are private. A second graph on the user's Main would not provide this isolation. */
struct Capsule {
  Main *main = BKE_main_new();
  Scene *scene = nullptr;
  Depsgraph *graph = nullptr;
  Map<SessionUID, Object *> objects;
  Map<SessionUID, bAction *> actions;
  PreviewApplyState preview_state;
  Vector<std::pair<SessionUID, Object *>> meshes;
  ~Capsule()
  {
    if (graph) {
      DEG_graph_free(graph);
    }
    BKE_main_free(main);
  }
};

struct Work {
  Settings settings;
  Vector<SampleFrame> frames;
  float frame = 0;
  uint64_t generation = 0;
  uint64_t epoch = 0;
  double input_time = 0;
  double staging_seconds = 0;
  std::shared_ptr<const PreparedPreview> preview;
};

struct State {
  /* These source pointers are main-thread-only. The worker accesses only capsule/mailboxes. */
  Main *source_main = nullptr;
  Scene *source_scene = nullptr;
  ViewLayer *source_view_layer = nullptr;
  Object *source_rig = nullptr;
  Vector<Object *> source_meshes;
  Set<unsigned int> source_dependencies;
  std::shared_ptr<const ArmatureRestDefinition> armature_rest;
  wmWindowManager *wm = nullptr;
  wmWindow *window = nullptr;
  SessionUID scene_uid;
  SessionUID rig_uid;
  std::unique_ptr<Capsule> capsule;
  Settings settings;
  KeyedFrames keyed_frames;
  float frame = 0;
  int frame_start = 0;
  int frame_end = 0;
  uint64_t generation = 1;
  uint64_t staged_generation = 0;
  uint64_t epoch = 1;
  double input_time = 0;
  std::shared_ptr<const PosePreviewInput> input;
  bool initialized = false;
  bool job_registered = false;
  bool retired = false;
  bool restart_requested = false;
  std::mutex mutex;
  std::condition_variable ready;
  std::optional<Work> pending;
  std::shared_ptr<const Bundle> completed;
  std::shared_ptr<const Bundle> published;
  std::string message;
  std::string worker_error;
  uint64_t worker_error_epoch = 0;
  uint64_t worker_error_generation = 0;
};

/* Accessed only on the main thread. Worker results are protected by State::mutex. */
static Vector<std::shared_ptr<State>> states;
static Map<std::pair<SessionUID, SessionUID>, std::shared_ptr<const PosePreviewInput>>
    captured_inputs;
static constexpr size_t cache_budget = 256 * 1024 * 1024;
static constexpr int dependency_limit = 512;
static constexpr int sample_limit = 8;

static SessionUID uid(const ID &id)
{
  return SessionUID{uint64_t(id.session_uid)};
}

static State *find_state(const Scene &scene, const Object &rig)
{
  for (const std::shared_ptr<State> &state : states) {
    if (!state->retired && state->scene_uid == uid(scene.id) && state->rig_uid == uid(rig.id)) {
      return state.get();
    }
  }
  return nullptr;
}

/* Retain a scene/rig mesh union across viewport visibility changes. Comparing a removed layer's
 * address is safe; dereferencing it is not. A valid alternate layer does not replace the
 * canonical source layer unless the latter has actually been removed. */
static bool source_membership_update(const Scene &scene,
                                     ViewLayer *&source_view_layer,
                                     Vector<Object *> &source_meshes,
                                     ViewLayer *requested_view_layer,
                                     const Span<Object *> meshes)
{
  if (BLI_findindex(&scene.view_layers, requested_view_layer) == -1) {
    return false;
  }
  bool changed = false;
  if (BLI_findindex(&scene.view_layers, source_view_layer) == -1) {
    source_view_layer = requested_view_layer;
    changed = true;
  }
  for (Object *mesh : meshes) {
    if (!source_meshes.contains(mesh)) {
      source_meshes.append(mesh);
      changed = true;
    }
  }
  return changed;
}

static bool supported_constraint(const bConstraint &constraint)
{
  switch (constraint.type) {
    case CONSTRAINT_TYPE_NULL:
    case CONSTRAINT_TYPE_CHILDOF:
    case CONSTRAINT_TYPE_TRACKTO:
    case CONSTRAINT_TYPE_ROTLIMIT:
    case CONSTRAINT_TYPE_LOCLIMIT:
    case CONSTRAINT_TYPE_SIZELIMIT:
    case CONSTRAINT_TYPE_ROTLIKE:
    case CONSTRAINT_TYPE_LOCLIKE:
    case CONSTRAINT_TYPE_SIZELIKE:
    case CONSTRAINT_TYPE_LOCKTRACK:
    case CONSTRAINT_TYPE_DISTLIMIT:
    case CONSTRAINT_TYPE_STRETCHTO:
    case CONSTRAINT_TYPE_TRANSFORM:
    case CONSTRAINT_TYPE_DAMPTRACK:
    case CONSTRAINT_TYPE_TRANSLIKE:
    case CONSTRAINT_TYPE_SAMEVOL:
    case CONSTRAINT_TYPE_ARMATURE:
      return true;
    case CONSTRAINT_TYPE_KINEMATIC: {
      const bKinematicConstraint &ik = *static_cast<const bKinematicConstraint *>(constraint.data);
      return ik.tar != nullptr && !(ik.flag & (CONSTRAINT_IK_AUTO | CONSTRAINT_IK_TEMP));
    }
    default:
      return false;
  }
}

static bool supported_scene_path(const StringRef path)
{
  /* Only authored fields copied into the evaluation scene. Avoid silently substituting
   * defaults for a driver that reads unrelated render, compositor or collection state. */
  return path.startswith("[") ||
         ELEM(path,
              "frame_current",
              "frame_subframe",
              "frame_current_final",
              "frame_start",
              "frame_end",
              "render.fps",
              "render.fps_base") ||
         path.startswith("unit_settings.");
}

static bool supported_driver_path(PointerRNA pointer,
                                  const char *path,
                                  const Scene &scene,
                                  std::string &message)
{
  Vector<PropertyElemRNA> elements;
  const bool complete = RNA_path_resolve_elements(&pointer, path, &elements);
  const auto supported_owner = [&](const ID *owner) {
    if (owner && !ELEM(owner->id_type(), ID_OB, ID_AR, ID_ME, ID_KE, ID_AC, ID_SCE)) {
      message =
          "Onion skin driver reads a display or shading dependency outside the private "
          "workspace";
      return false;
    }
    if (owner && owner->id_type() == ID_SCE &&
        (owner != &scene.id || pointer.owner_id != &scene.id))
    {
      message = "Onion skin does not support redirected Scene driver inputs";
      return false;
    }
    return true;
  };
  /* An invalid driver can legitimately use its native fallback. Audit the resolved prefix too:
   * a failed trailing lookup must not hide a shading dependency stripped from the capsule. */
  for (const int index : elements.index_range()) {
    PropertyElemRNA &element = elements[index];
    if (!supported_owner(element.ptr.owner_id)) {
      return false;
    }
    if (RNA_property_type(element.prop) == PROP_POINTER &&
        (!complete || index + 1 < elements.size()))
    {
      const PointerRNA next = RNA_property_pointer_get_never_create(&element.ptr, element.prop);
      if (!supported_owner(next.owner_id)) {
        return false;
      }
      /* RenderSettings and other copied Scene substructures retain their Scene owner. Only an
       * actual Scene ID pointer is a redirect, including a self reference or Scene reentry. */
      if (next.owner_id && next.owner_id->id_type() == ID_SCE && RNA_struct_is_ID(next.type)) {
        message = "Onion skin does not support redirected Scene driver inputs";
        return false;
      }
    }
  }
  return true;
}

static bool supported_driver(const ChannelDriver &driver, const Scene &scene, std::string &message)
{
  if (driver.type == DRIVER_TYPE_PYTHON) {
    FCurve disposable{};
    disposable.driver = fcurve_copy_driver(&driver);
    const bool simple = BKE_driver_has_simple_expression(disposable.driver);
    fcurve_free_driver(&disposable);
    if (!simple) {
      message =
          "Onion skin requires native driver expressions; Python namespace drivers are "
          "unsupported";
      return false;
    }
  }
  else if (!ELEM(driver.type,
                 DRIVER_TYPE_AVERAGE,
                 DRIVER_TYPE_SUM,
                 DRIVER_TYPE_MIN,
                 DRIVER_TYPE_MAX))
  {
    message = "Onion skin driver type is unsupported";
    return false;
  }
  for (const DriverVar &variable : driver.variables) {
    for (int index = 0; index < variable.num_targets; index++) {
      const DriverTarget &target = variable.targets[index];
      const StringRef path = target.rna_path ? target.rna_path : "";
      if (path.startswith("active_material") || path.find(".active_material") != -1 ||
          path.startswith("material_slots[") || path.find(".material_slots[") != -1 ||
          path.find(".materials[") != -1 || path.startswith("custom_shape") ||
          path.find(".custom_shape") != -1 || path.startswith("instance_collection") ||
          path.find(".instance_collection") != -1 || path.startswith("users_collection") ||
          path.find(".users_collection") != -1)
      {
        message =
            "Onion skin driver reads a display or shading dependency outside the private "
            "workspace";
        return false;
      }
      if (target.id && variable.type == DVAR_TYPE_SINGLE_PROP) {
        PointerRNA pointer = RNA_id_pointer_create(target.id);
        if (!supported_driver_path(pointer, target.rna_path, scene, message)) {
          return false;
        }
      }
      if (target.id && target.id->id_type() == ID_SCE &&
          (target.id != &scene.id || !supported_scene_path(path)))
      {
        message =
            "Onion skin Scene drivers require current-scene custom properties or frame settings";
        return false;
      }
      if (variable.type == DVAR_TYPE_CONTEXT_PROP) {
        const bool supported = target.context_property == DTAR_CONTEXT_PROPERTY_ACTIVE_SCENE ?
                                   supported_scene_path(path) :
                                   target.context_property ==
                                           DTAR_CONTEXT_PROPERTY_ACTIVE_VIEW_LAYER &&
                                       (path.startswith("[") || path == "name");
        if (!supported) {
          message = "Onion skin context drivers require copied Scene or View Layer properties";
          return false;
        }
        PointerRNA pointer = target.context_property == DTAR_CONTEXT_PROPERTY_ACTIVE_SCENE ?
                                 RNA_id_pointer_create(const_cast<ID *>(&scene.id)) :
                                 RNA_pointer_create_discrete(const_cast<ID *>(&scene.id),
                                                             RNA_ViewLayer,
                                                             scene.view_layers.first());
        if (!supported_driver_path(pointer, target.rna_path, scene, message)) {
          return false;
        }
        if (target.context_property == DTAR_CONTEXT_PROPERTY_ACTIVE_VIEW_LAYER) {
          const ViewLayer *baseline = scene.view_layers.first();
          for (const ViewLayer &layer : scene.view_layers) {
            const bool matching = path == "name" ?
                                      StringRef(layer.name) == baseline->name :
                                      IDP_EqualsProperties(layer.id_properties,
                                                           baseline->id_properties) &&
                                          IDP_EqualsProperties(layer.system_properties,
                                                               baseline->system_properties);
            if (!matching) {
              message = "Onion skin requires matching context driver inputs across View Layers";
              return false;
            }
          }
        }
      }
    }
  }
  return true;
}

static bool strip_controls_supported(const ListBaseT<NlaStrip> &strips)
{
  for (const NlaStrip &strip : strips) {
    for (const FCurve &curve : strip.fcurves) {
      if (curve.driver) {
        return false;
      }
    }
    if (!strip_controls_supported(strip.strips)) {
      return false;
    }
  }
  return true;
}

static bool supported_id(ID &id, const Scene &scene, std::string &message)
{
  if (!ELEM(id.id_type(), ID_OB, ID_AR, ID_ME, ID_KE, ID_AC, ID_SCE) ||
      (id.id_type() == ID_SCE && &id != &scene.id))
  {
    message = "Onion skin dependency type is not supported";
    return false;
  }
  const AnimData *adt = BKE_animdata_from_id(&id);
  if (adt) {
    for (const NlaTrack &track : adt->nla_tracks) {
      if (!strip_controls_supported(track.strips)) {
        message = "Onion skin does not support drivers embedded in NLA strip controls";
        return false;
      }
    }
    for (const FCurve &curve : adt->drivers) {
      if (curve.rna_path().endswith(".solidify_mode")) {
        message = "Onion skin requires an unanimated Solidify algorithm";
        return false;
      }
      if (curve.driver && !supported_driver(*curve.driver, scene, message)) {
        return false;
      }
      if (id.id_type() == ID_SCE && !supported_scene_path(curve.rna_path())) {
        message = "Onion skin Scene animation targets an uncopied setting";
        return false;
      }
    }
  }
  if (id.id_type() == ID_AC) {
    for (const FCurve *curve : animrig::legacy::fcurves_all(id_cast<bAction *>(&id))) {
      if (curve->rna_path().endswith(".solidify_mode")) {
        message = "Onion skin requires an unanimated Solidify algorithm";
        return false;
      }
      if (curve->driver) {
        message = "Onion skin does not support drivers embedded in Action curves";
        return false;
      }
    }
  }
  if (id.id_type() != ID_OB) {
    if (id.id_type() == ID_ME && id_cast<Mesh *>(&id)->runtime->edit_mesh) {
      message = "Onion skin does not support meshes being edited";
      return false;
    }
    return true;
  }
  const Object &object = *id_cast<Object *>(&id);
  if (!ELEM(object.type, OB_ARMATURE, OB_MESH, OB_EMPTY) || object.soft ||
      object.rigidbody_object || object.rigidbody_constraint || object.instance_collection)
  {
    message = "Onion skin requires armature, mesh or empty dependencies without simulations";
    return false;
  }
  bool trailing_subdivision = false;
  for (const ModifierData &modifier : object.modifiers) {
    if (modifier.type == eModifierType_Subsurf) {
      trailing_subdivision = true;
      continue;
    }
    if (!ELEM(
            modifier.type, eModifierType_Armature, eModifierType_Mirror, eModifierType_Solidify) ||
        trailing_subdivision)
    {
      message = "Onion skin supports Armature, Mirror and Solidify with terminal Subdivision";
      return false;
    }
    if (modifier.type == eModifierType_Solidify &&
        reinterpret_cast<const SolidifyModifierData &>(modifier).mode != MOD_SOLIDIFY_MODE_EXTRUDE)
    {
      message = "Onion skin supports the simple Solidify mode";
      return false;
    }
  }
  for (const bConstraint &constraint : object.constraints) {
    if (!supported_constraint(constraint)) {
      message = "Onion skin constraint is outside the supported native evaluation envelope";
      return false;
    }
  }
  if (object.pose) {
    if (object.pose->iksolver != IKSOLVER_STANDARD) {
      message = "Onion skin currently supports the standard IK solver; iTaSC is not audited";
      return false;
    }
    for (const bPoseChannel &bone : object.pose->chanbase) {
      for (const bConstraint &constraint : bone.constraints) {
        if (!supported_constraint(constraint)) {
          message = "Onion skin requires authored IK targets and supported transform constraints";
          return false;
        }
      }
    }
  }
  return true;
}

/* Shading/display-only links are removed from copies; they must never pull the user's material
 * node trees, collections or custom bone-shape drawing objects into evaluation. */
static bool discard_link(const LibraryIDLinkCallbackData &data)
{
  if (data.self_id->id_type() == ID_OB) {
    const Object &object = *id_cast<Object *>(data.self_id);
    if (object.pose) {
      for (const bPoseChannel &bone : object.pose->chanbase) {
        if (data.id_pointer == reinterpret_cast<ID *const *>(&bone.custom)) {
          return true;
        }
      }
    }
  }
  return (data.cb_flag &
          (IDWALK_CB_OVERRIDE_LIBRARY_REFERENCE | IDWALK_CB_OVERRIDE_LIBRARY_HIERARCHY_ROOT)) ||
         (*data.id_pointer && ELEM((*data.id_pointer)->id_type(), ID_MA, ID_GR, ID_LI));
}

struct Gather {
  Set<ID *> ids;
  Vector<ID *> queue;
};

/* Copy only the Scene inputs used by geometry and native drivers, keeping its collection,
 * compositor and display data out of the evaluation dependency closure. */
static void foreach_scene_input(Main *bmain,
                                Scene &scene,
                                ViewLayer &view_layer,
                                FunctionRef<LibraryIDLinkCallback> callback,
                                void *user_data,
                                LibraryForeachIDFlag flag)
{
  BKE_library_foreach_subdata_id(
      bmain,
      &scene.id,
      &scene.id,
      [&](LibraryForeachIDData *data) {
        if (scene.adt) {
          BKE_animdata_foreach_id(scene.adt, data);
        }
        for (IDProperty *properties : {scene.id.properties,
                                       scene.id.system_properties,
                                       view_layer.id_properties,
                                       view_layer.system_properties})
        {
          IDP_foreach_property(properties, IDP_TYPE_FILTER_ID, [&](IDProperty *property) {
            BKE_lib_query_idpropertiesForeachIDLink_callback(property, data);
          });
        }
      },
      callback,
      user_data,
      flag);
}

static void clear_strip_source_pointers(ListBaseT<NlaStrip> &strips)
{
  for (NlaStrip &strip : strips) {
    strip.orig_strip = nullptr;
    clear_strip_source_pointers(strip.strips);
  }
}

static int gather_link(LibraryIDLinkCallbackData *data)
{
  Gather &gather = *static_cast<Gather *>(data->user_data);
  ID *id = *data->id_pointer;
  if (id && !discard_link(*data) && !(data->cb_flag & IDWALK_CB_EMBEDDED) && gather.ids.add(id)) {
    gather.queue.append(id);
  }
  return IDWALK_RET_NOP;
}

struct Remap {
  const Map<ID *, ID *> &ids;
  const Set<ID *> &owned;
  bool valid = true;
};

struct OwnershipValidation {
  Set<ID *> owned;
  bool valid = true;
};

static int validate_link(LibraryIDLinkCallbackData *data)
{
  OwnershipValidation &validation = *static_cast<OwnershipValidation *>(data->user_data);
  if (*data->id_pointer && !validation.owned.contains(*data->id_pointer)) {
    validation.valid = false;
  }
  return IDWALK_RET_NOP;
}

static int remap_link(LibraryIDLinkCallbackData *data)
{
  Remap &remap = *static_cast<Remap *>(data->user_data);
  ID *old = *data->id_pointer;
  if (!old || (data->cb_flag & IDWALK_CB_EMBEDDED)) {
    return IDWALK_RET_NOP;
  }
  if (discard_link(*data)) {
    *data->id_pointer = nullptr;
    return IDWALK_RET_NOP;
  }
  /* Native ID copying already remaps some links, including pose-constraint targets that point
   * back to their own object. Accept only members of this capsule's explicit copy set. */
  if (remap.owned.contains(old)) {
    return IDWALK_RET_NOP;
  }
  ID *const *copy = remap.ids.lookup_ptr(old);
  if (!copy) {
    remap.valid = false;
    /* Never leave a source pointer in a copy, even on a rejected capsule. */
    *data->id_pointer = nullptr;
  }
  else {
    *data->id_pointer = *copy;
  }
  return IDWALK_RET_NOP;
}

static std::unique_ptr<Capsule> capsule_create(State &state)
{
  if (BLI_findindex(&state.source_scene->view_layers, state.source_view_layer) == -1) {
    state.message = "Onion skin source view layer no longer exists";
    return nullptr;
  }
  /* Cache the view-layer's complete direct skin set. Individual viewports filter drawing, so
   * visibility differences between two viewports never churn the shared geometry capsule. */
  BKE_view_layer_synced_ensure(*state.source_main, state.source_scene, state.source_view_layer);
  for (Base &base : *BKE_view_layer_object_bases_get(state.source_view_layer)) {
    Object *object = base.object;
    if (object->type != OB_MESH || state.source_meshes.contains(object)) {
      continue;
    }
    for (const ModifierData &modifier : object->modifiers) {
      if (modifier.type == eModifierType_Armature &&
          reinterpret_cast<const ArmatureModifierData &>(modifier).object == state.source_rig)
      {
        state.source_meshes.append(object);
        break;
      }
    }
  }
  Gather gather;
  auto add = [&](ID *id) {
    if (gather.ids.add(id)) {
      gather.queue.append(id);
    }
  };
  add(&state.source_rig->id);
  add(&state.source_scene->id);
  for (Object *mesh : state.source_meshes) {
    add(&mesh->id);
  }
  /* Native reference traversal covers every dependency in the audited envelope, including
   * inactive Actions, drivers and constraint targets. Construct relations only on private IDs:
   * building a source graph would compile driver caches in the user's database. */
  for (int index = 0; index < gather.queue.size(); index++) {
    state.source_dependencies.add(gather.queue[index]->session_uid);
    if (gather.queue.size() > dependency_limit ||
        !supported_id(*gather.queue[index], *state.source_scene, state.message))
    {
      if (gather.queue.size() > dependency_limit) {
        state.message = "Onion skin dependency capsule exceeds the supported size limit";
      }
      return nullptr;
    }
    if (gather.queue[index] == &state.source_scene->id) {
      foreach_scene_input(state.source_main,
                          *state.source_scene,
                          *state.source_view_layer,
                          gather_link,
                          &gather,
                          IDWALK_READONLY);
      bool valid_scene_animation = true;
      BKE_library_foreach_subdata_id(
          state.source_main,
          &state.source_scene->id,
          &state.source_scene->id,
          [&](LibraryForeachIDData *data) {
            if (state.source_scene->adt) {
              BKE_animdata_foreach_id(state.source_scene->adt, data);
            }
          },
          [&](LibraryIDLinkCallbackData *data) {
            ID *id = *data->id_pointer;
            if (id && id->id_type() == ID_AC) {
              for (const FCurve *curve : animrig::legacy::fcurves_all(id_cast<bAction *>(id))) {
                valid_scene_animation &= supported_scene_path(curve->rna_path());
              }
            }
            return IDWALK_RET_NOP;
          },
          nullptr,
          IDWALK_READONLY);
      if (!valid_scene_animation) {
        state.message = "Onion skin Scene animation targets an uncopied setting";
        return nullptr;
      }
    }
    else {
      BKE_library_foreach_ID_link(
          state.source_main, gather.queue[index], gather_link, &gather, IDWALK_READONLY);
    }
  }

  size_t estimated_bytes = 0;
  for (ID *id : gather.queue) {
    if (id->id_type() == ID_ME) {
      const Mesh &mesh = *id_cast<Mesh *>(id);
      const size_t triangle_count = size_t(math::max(mesh.corners_num - 2 * mesh.faces_num, 0));
      estimated_bytes += size_t(mesh.verts_num) * sizeof(float3) * 3;
      estimated_bytes += triangle_count * sizeof(int3) * 3;
    }
    if (id->id_type() == ID_KE) {
      const Key &key = *id_cast<Key *>(id);
      for (const KeyBlock &block : key.block) {
        estimated_bytes += size_t(block.totelem) * size_t(key.elemsize) * 3;
      }
    }
    if (id->id_type() == ID_AC) {
      /* Include private Action/COW data and the bounded staging/pending/current packets. */
      for (const FCurve *curve : animrig::legacy::fcurves_all(id_cast<bAction *>(id))) {
        estimated_bytes += sizeof(FCurve) * 6;
        estimated_bytes += size_t(curve->totvert) *
                           (curve->bezt ? sizeof(BezTriple) : sizeof(FPoint)) * 6;
      }
    }
  }
  /* Bound constructive modifier output before invoking native mesh evaluation. Account for
   * each sampled Object separately, even when several objects share one source Mesh. */
  for (Object *object : state.source_meshes) {
    const Mesh &mesh = *id_cast<const Mesh *>(object->data);
    size_t vertices = size_t(mesh.verts_num);
    size_t edges = size_t(mesh.edges_num);
    size_t triangles = size_t(math::max(mesh.corners_num - 2 * mesh.faces_num, 0));
    for (const ModifierData &modifier : object->modifiers) {
      if (modifier.type == eModifierType_Mirror) {
        const auto flag = reinterpret_cast<const MirrorModifierData &>(modifier).flag;
        const bool animated = object->adt &&
                              (object->adt->action || !object->adt->nla_tracks.is_empty() ||
                               !object->adt->drivers.is_empty());
        const bool bisect = animated || flag & (MOD_MIR_BISECT_AXIS_X | MOD_MIR_BISECT_AXIS_Y |
                                                MOD_MIR_BISECT_AXIS_Z);
        if (bisect) {
          /* Each plane can split edges/faces before mirroring. Conservatively bound all three
           * cuts, including flags driven or animated at another sampled time. */
          for (int axis = 0; axis < 3; axis++) {
            vertices += edges;
            edges *= 3;
            triangles *= 3;
          }
        }
        const int axes = animated ?
                             3 :
                             int(bool(flag & MOD_MIR_AXIS_X)) + int(bool(flag & MOD_MIR_AXIS_Y)) +
                                 int(bool(flag & MOD_MIR_AXIS_Z));
        const size_t factor = size_t(1) << axes;
        vertices *= factor;
        edges *= factor;
        triangles *= factor;
      }
      else if (modifier.type == eModifierType_Solidify) {
        triangles = 2 * triangles + 2 * edges;
        edges = 2 * edges + vertices;
        vertices *= 2;
      }
      if (vertices > cache_budget / sizeof(float3) || edges > cache_budget / sizeof(int2) ||
          triangles > cache_budget / sizeof(int3))
      {
        state.message = "Onion skin constructive modifier output exceeds the cache budget";
        return nullptr;
      }
    }
    estimated_bytes += (vertices * sizeof(float3) + triangles * sizeof(int3)) *
                       (3 * sample_limit + 3);
    if (estimated_bytes > cache_budget) {
      break;
    }
  }
  if (estimated_bytes > cache_budget) {
    state.message = "Onion skin workspace exceeds the 256 MiB cache budget";
    return nullptr;
  }

  auto capsule = std::make_unique<Capsule>();
  capsule->scene = BKE_scene_add(capsule->main, "Mesh Onion Skin Evaluation");
  Map<ID *, ID *> copies;
  Map<ID *, int> source_users;
  /* No user reference changes, caches, override hierarchy or source ID.newid writes. Remap all
   * copies before adding any to the private Main, which then establishes private user counts. */
  const int copy_flags = LIB_ID_CREATE_NO_MAIN | LIB_ID_CREATE_NO_USER_REFCOUNT |
                         LIB_ID_CREATE_NO_DEG_TAG | LIB_ID_COPY_NO_PREVIEW |
                         LIB_ID_COPY_NO_LIB_OVERRIDE;
  for (ID *source : gather.queue) {
    source_users.add(source, source->us);
    ID *copy = source == &state.source_scene->id ?
                   &capsule->scene->id :
                   BKE_id_copy_ex(nullptr, source, nullptr, copy_flags);
    if (!copy) {
      state.message = "Onion skin could not copy an evaluation dependency";
      for (ID *owned : copies.values()) {
        if (owned != &capsule->scene->id) {
          BKE_id_free(nullptr, owned);
        }
      }
      return nullptr;
    }
    copy->lib = nullptr;
    copy->newid = nullptr;
    copy->orig_id = nullptr;
    if (copy->id_type() == ID_ME) {
      Mesh &mesh = *id_cast<Mesh *>(copy);
      /* Ordinary mesh duplication shares derived runtime caches. The private workspace must
       * start with fresh caches, including subdivision/edit-mesh runtime, before worker access. */
      const ImplicitSharingInfo *face_offsets_owner = mesh.runtime->face_offsets_sharing_info;
      mesh.runtime->face_offsets_sharing_info = nullptr;
      delete mesh.runtime;
      mesh.runtime = new bke::MeshRuntime();
      /* This runtime field owns actual geometry storage, not a derived cache. Transfer its
       * existing sharing reference so source edits cannot free the capsule's face offsets. */
      mesh.runtime->face_offsets_sharing_info = face_offsets_owner;
    }
    if (copy->id_type() == ID_OB) {
      Object &object = *id_cast<Object *>(copy);
      /* BKE_object_runtime_reset_on_copy deliberately retains some source-derived fields,
       * including data_orig. This evaluator needs a completely fresh private runtime instead. */
      MEM_delete(object.runtime);
      object.runtime = MEM_new<bke::ObjectRuntime>(__func__);
    }
    copies.add(source, copy);
  }
  Scene &private_scene = *capsule->scene;
  const Scene &source_scene = *state.source_scene;
  private_scene.r.frs_sec = source_scene.r.frs_sec;
  private_scene.r.frs_sec_base = source_scene.r.frs_sec_base;
  private_scene.r.framelen = source_scene.r.framelen;
  private_scene.r.sfra = source_scene.r.sfra;
  private_scene.r.efra = source_scene.r.efra;
  private_scene.unit = source_scene.unit;
  private_scene.adt = BKE_animdata_copy(nullptr, source_scene.adt, copy_flags);
  private_scene.id.properties = property_snapshot(source_scene.id.properties).release();
  private_scene.id.system_properties =
      property_snapshot(source_scene.id.system_properties).release();
  ViewLayer *view_layer = BKE_view_layer_default_view(capsule->scene);
  STRNCPY(view_layer->name, state.source_view_layer->name);
  view_layer->id_properties = property_snapshot(state.source_view_layer->id_properties).release();
  view_layer->system_properties =
      property_snapshot(state.source_view_layer->system_properties).release();
  Set<ID *> owned_copies;
  for (ID *copy : copies.values()) {
    if (AnimData *adt = BKE_animdata_from_id(copy)) {
      for (NlaTrack &track : adt->nla_tracks) {
        clear_strip_source_pointers(track.strips);
      }
    }
    owned_copies.add(copy);
  }
  Remap remap{copies, owned_copies};
  for (ID *copy : copies.values()) {
    if (copy == &capsule->scene->id) {
      foreach_scene_input(nullptr, private_scene, *view_layer, remap_link, &remap, IDWALK_NOP);
    }
    else {
      BKE_library_foreach_ID_link(nullptr, copy, remap_link, &remap, IDWALK_NOP);
    }
  }
  if (!remap.valid) {
    state.message = "Onion skin could not isolate every evaluation dependency";
    for (ID *copy : copies.values()) {
      if (copy != &capsule->scene->id) {
        BKE_id_free(nullptr, copy);
      }
    }
    return nullptr;
  }
  for (const auto item : copies.items()) {
    if (item.value != &capsule->scene->id) {
      BKE_libblock_management_main_add(capsule->main, item.value);
    }
    if (item.value->id_type() == ID_OB) {
      capsule->objects.add(uid(*item.key), id_cast<Object *>(item.value));
    }
    if (item.value->id_type() == ID_AC) {
      capsule->actions.add(uid(*item.key), id_cast<bAction *>(item.value));
    }
  }
  for (const auto item : source_users.items()) {
    if (item.key->us != item.value) {
      BLI_assert_unreachable();
      state.message = "Onion skin source ownership validation failed";
      return nullptr;
    }
  }

  for (Object *copy : capsule->objects.values()) {
    for (ModifierData &modifier : copy->modifiers) {
      if (modifier.type == eModifierType_Subsurf) {
        /* The declared interaction quality is the deformation cage. Disabling only the copied
         * terminal subdivision stack avoids realization and leaves source viewport quality alone.
         */
        modifier.mode &= ~(eModifierMode_Realtime | eModifierMode_Render);
      }
    }
    if (copy->pose && copy->type == OB_ARMATURE) {
      BKE_pose_rebuild(capsule->main, copy, id_cast<bArmature *>(copy->data), false);
      for (bPoseChannel &bone : copy->pose->chanbase) {
        bone.orig_pchan = nullptr;
      }
    }
    BKE_collection_object_add(capsule->main, capsule->scene->master_collection, copy);
  }
  for (Object *mesh : state.source_meshes) {
    capsule->meshes.append({uid(mesh->id), capsule->objects.lookup(uid(mesh->id))});
  }
  BKE_view_layer_synced_ensure(*capsule->main, capsule->scene, view_layer);
  OwnershipValidation ownership;
  ownership.owned.add(&capsule->scene->id);
  ownership.owned.add(&capsule->scene->master_collection->id);
  for (ID *copy : copies.values()) {
    ownership.owned.add(copy);
  }
  for (ID *copy : ownership.owned) {
    BKE_library_foreach_ID_link(capsule->main,
                                copy,
                                validate_link,
                                &ownership,
                                IDWALK_READONLY | IDWALK_DO_INTERNAL_RUNTIME_POINTERS);
  }
  if (!ownership.valid) {
    state.message = "Onion skin workspace retains a non-owned evaluation reference";
    return nullptr;
  }
  capsule->graph = DEG_graph_new(capsule->main, capsule->scene, view_layer, DAG_EVAL_VIEWPORT);
  Vector<ID *> targets;
  targets.append(&capsule->objects.lookup(state.rig_uid)->id);
  for (const auto &mesh : capsule->meshes) {
    targets.append(&mesh.second->id);
  }
  DEG_graph_build_from_ids(capsule->graph, targets);
  state.armature_rest = armature_rest_capture(*id_cast<bArmature *>(state.source_rig->data));
  state.keyed_frames = keyed_frames_collect(*state.source_rig);
  return capsule;
}

static std::shared_ptr<Bundle> evaluate(Capsule &capsule,
                                        const Work &work,
                                        wmJobWorkerStatus &worker_status)
{
  const double start = BLI_time_now_seconds();
  const bool diagnostics = std::getenv("BLENDER_MESH_ONION_SKIN_DIAGNOSTICS") != nullptr;
  double graph_seconds = 0.0;
  double snapshot_seconds = 0.0;
  double triangulation_seconds = 0.0;
  bool structure_changed = false;
  Object *edited_rig = work.preview ?
                           capsule.objects.lookup_default(work.preview->object_uid, nullptr) :
                           nullptr;
  bAction *previous_action = nullptr;
  if (edited_rig) {
    const AnimData *adt = BKE_animdata_from_id(&edited_rig->id);
    previous_action = adt ? adt->action : nullptr;
  }
  if (work.preview && !preview_apply(*work.preview,
                                     *capsule.main,
                                     capsule.objects,
                                     capsule.actions,
                                     capsule.preview_state,
                                     &structure_changed))
  {
    return nullptr;
  }
  if (structure_changed) {
    /* A provisional key can create a channel that did not exist in the baseline. */
    DEG_graph_tag_relations_update(capsule.graph);
    DEG_graph_relations_update(capsule.graph);
  }
  if (edited_rig) {
    /* Only the proposal changes private originals. Native frame-change evaluation tags the
     * time source and propagates updates to the remaining animated dependencies. Retain the
     * pose geometry tag here: unkeyed channels are authored inputs even without an Action. */
    DEG_id_tag_update_ex(capsule.main,
                         &edited_rig->id,
                         ID_RECALC_ANIMATION | ID_RECALC_TRANSFORM | ID_RECALC_GEOMETRY);
    /* A rollback can restore a related data Action's slots while unassigning it from the Rig.
     * Select the packet's actual target independently of the resulting AnimData assignment. */
    bAction *modified_action = work.preview->action_uid.is_generated() ?
                                   capsule.actions.lookup_default(work.preview->action_uid,
                                                                  nullptr) :
                                   capsule.preview_state.created_action;
    const bool action_modified = modified_action &&
                                 (work.preview->channelbag || work.preview->action_template);
    if (action_modified) {
      DEG_id_tag_update_ex(capsule.main, &modified_action->id, ID_RECALC_ANIMATION);
    }
    const AnimData *adt = BKE_animdata_from_id(&edited_rig->id);
    if (previous_action && previous_action != (adt ? adt->action : nullptr) &&
        (!action_modified || previous_action != modified_action))
    {
      /* Removing a provisional assignment must also flush its previous evaluated use. */
      DEG_id_tag_update_ex(capsule.main, &previous_action->id, ID_RECALC_ANIMATION);
    }
  }
  const double preparation_seconds = diagnostics ? BLI_time_now_seconds() - start : 0.0;
  auto bundle = std::make_shared<Bundle>();
  bundle->generation = work.generation;
  bundle->epoch = work.epoch;
  bundle->input_time = work.input_time;
  bundle->staging_seconds = work.staging_seconds;
  /* Publish only a complete coherent set. The packet owns scene-time samples selected on the
   * main thread; the worker never accesses source Actions or scene range settings. */
  for (const SampleFrame &requested_frame : work.frames) {
    if (worker_status.stop) {
      return nullptr;
    }
    const float frame = requested_frame.frame;
    BKE_scene_frame_set(capsule.scene, frame);
    const double graph_start = diagnostics ? BLI_time_now_seconds() : 0.0;
    DEG_evaluate_on_framechange(capsule.graph, frame, DEG_EVALUATE_SYNC_WRITEBACK_NO);
    const double snapshot_start = diagnostics ? BLI_time_now_seconds() : 0.0;
    graph_seconds += snapshot_start - graph_start;
    for (const auto &entry : capsule.meshes) {
      Object *object = DEG_get_evaluated(capsule.graph, entry.second);
      const Mesh *mesh = BKE_object_get_evaluated_mesh_no_subsurf(object);
      if (!mesh) {
        return nullptr;
      }
      Sample sample;
      const size_t sample_bytes = size_t(mesh->verts_num) * sizeof(float3) +
                                  size_t(mesh->corners_num - 2 * mesh->faces_num) * sizeof(int3);
      if (sample_bytes > cache_budget - bundle->bytes) {
        return nullptr;
      }
      sample.object_uid = entry.first;
      sample.frame = frame;
      sample.temporal_offset = requested_frame.offset;
      sample.object_to_world = object->object_to_world();
      sample.positions = Array<float3>(mesh->vert_positions());
      const double triangulation_start = diagnostics ? BLI_time_now_seconds() : 0.0;
      const Span<int3> corner_triangles = mesh->corner_tris();
      if (diagnostics) {
        triangulation_seconds += BLI_time_now_seconds() - triangulation_start;
      }
      const Span<int> corner_vertices = mesh->corner_verts();
      sample.triangles.reinitialize(corner_triangles.size());
      threading::parallel_for(corner_triangles.index_range(), 4096, [&](IndexRange range) {
        for (const int index : range) {
          const int3 corners = corner_triangles[index];
          sample.triangles[index] = int3(
              corner_vertices[corners.x], corner_vertices[corners.y], corner_vertices[corners.z]);
        }
      });
      bundle->bytes += sample.positions.size() * sizeof(float3) +
                       sample.triangles.size() * sizeof(int3);
      if (bundle->bytes > cache_budget) {
        return nullptr;
      }
      bundle->samples.append(std::move(sample));
    }
    if (diagnostics) {
      snapshot_seconds += BLI_time_now_seconds() - snapshot_start;
    }
  }
  bundle->evaluation_seconds = BLI_time_now_seconds() - start;
  if (diagnostics) {
    printf(
        "mesh_onion_skin_worker generation=%llu preparation_ms=%.3f graph_ms=%.3f "
        "snapshot_ms=%.3f triangulation_ms=%.3f\n",
        static_cast<unsigned long long>(work.generation),
        preparation_seconds * 1000.0,
        graph_seconds * 1000.0,
        snapshot_seconds * 1000.0,
        triangulation_seconds * 1000.0);
  }
  return bundle;
}

static void stage(State &state)
{
  if (state.retired || state.restart_requested || !state.capsule ||
      state.staged_generation == state.generation)
  {
    return;
  }
  Work work;
  work.settings = state.settings;
  work.frame = state.frame;
  work.generation = state.generation;
  work.epoch = state.epoch;
  work.input_time = state.input_time;
  const double start = BLI_time_now_seconds();
  if (state.settings.keyed_frames_only && !state.keyed_frames.unsupported_reason.empty()) {
    state.message = state.keyed_frames.unsupported_reason;
    state.epoch++;
    state.published.reset();
    std::lock_guard lock(state.mutex);
    state.pending.reset();
    state.completed.reset();
    state.staged_generation = state.generation;
    return;
  }
  work.frames = sample_frames_select(state.keyed_frames.frames,
                                     state.settings,
                                     state.frame,
                                     state.source_scene->r.sfra,
                                     state.source_scene->r.efra);
  if (state.input) {
    work.preview = preview_prepare(
        *state.source_main, *state.source_scene, *state.source_rig, *state.input);
    if (!work.preview) {
      state.message = "Current pose edit is outside the supported onion skin preview envelope";
      state.epoch++;
      state.published.reset();
      std::lock_guard lock(state.mutex);
      state.pending.reset();
      state.completed.reset();
      state.staged_generation = state.generation;
      return;
    }
  }
  work.staging_seconds = BLI_time_now_seconds() - start;
  {
    std::lock_guard lock(state.mutex);
    state.pending = std::move(work);
  }
  state.staged_generation = state.generation;
  state.ready.notify_one();
}

static void job_init(void *custom_data)
{
  State &state = **static_cast<std::shared_ptr<State> *>(custom_data);
  /* Loaded files can contain different sampling settings in their viewports. The first request
   * chooses the shared CPU policy; reconcile subscribers in the main-thread event loop. */
  for (wmWindow &window : state.wm->windows) {
    if (WM_window_get_active_scene(&window) != state.source_scene) {
      continue;
    }
    ViewLayer *view_layer = WM_window_get_active_view_layer(&window);
    BKE_view_layer_synced_ensure(*state.source_main, state.source_scene, view_layer);
    if (BKE_view_layer_active_object_get(view_layer) != state.source_rig) {
      continue;
    }
    bScreen *screen = WM_window_get_active_screen(&window);
    if (!screen) {
      continue;
    }
    for (ScrArea &area : screen->areabase) {
      if (area.spacetype != SPACE_VIEW3D || !area.spacedata.first()) {
        continue;
      }
      View3D &view = *reinterpret_cast<View3D *>(area.spacedata.first());
      if (!(view.flag2 & V3D_HIDE_OVERLAYS) && view.overlay.flag & V3D_OVERLAY_MESH_ONION_SKIN) {
        view.overlay.mesh_onion_skin_before = state.settings.before;
        view.overlay.mesh_onion_skin_after = state.settings.after;
        view.overlay.mesh_onion_skin_step = state.settings.step;
        SET_FLAG_FROM_TEST(view.overlay.flag,
                           state.settings.keyed_frames_only,
                           V3D_OVERLAY_MESH_ONION_SKIN_KEYED_FRAMES_ONLY);
        SET_FLAG_FROM_TEST(view.overlay.flag,
                           state.settings.loop_frame_range,
                           V3D_OVERLAY_MESH_ONION_SKIN_LOOP_FRAME_RANGE);
      }
    }
  }
  state.capsule = capsule_create(state);
  state.initialized = true;
  state.restart_requested = false;
  stage(state);
}

static void job_start(void *custom_data, wmJobWorkerStatus *worker_status)
{
  State &state = **static_cast<std::shared_ptr<State> *>(custom_data);
  if (!state.capsule) {
    return;
  }
  while (!worker_status->stop) {
    std::optional<Work> work;
    {
      std::unique_lock lock(state.mutex);
      state.ready.wait_for(lock, std::chrono::milliseconds(10), [&] {
        return state.pending.has_value() || worker_status->stop;
      });
      if (worker_status->stop) {
        break;
      }
      work.swap(state.pending);
    }
    if (!work) {
      continue;
    }
    std::shared_ptr<Bundle> result = evaluate(*state.capsule, *work, *worker_status);
    if (result) {
      std::lock_guard lock(state.mutex);
      state.completed = std::move(result);
      worker_status->do_update = true;
    }
    else if (!worker_status->stop) {
      std::lock_guard lock(state.mutex);
      state.worker_error = "Onion skin evaluation failed or exceeded its cache budget";
      state.worker_error_epoch = work->epoch;
      state.worker_error_generation = work->generation;
      worker_status->do_update = true;
    }
  }
}

static void job_timer(void *custom_data)
{
  State &state = **static_cast<std::shared_ptr<State> *>(custom_data);
  bool subscribed = false;
  if (state.source_rig->mode & OB_MODE_POSE) {
    for (wmWindow &window : state.wm->windows) {
      if (WM_window_get_active_scene(&window) != state.source_scene) {
        continue;
      }
      ViewLayer *view_layer = WM_window_get_active_view_layer(&window);
      BKE_view_layer_synced_ensure(*state.source_main, state.source_scene, view_layer);
      if (BKE_view_layer_active_object_get(view_layer) != state.source_rig) {
        continue;
      }
      const bScreen *screen = WM_window_get_active_screen(&window);
      if (!screen) {
        continue;
      }
      for (const ScrArea &area : screen->areabase) {
        if (area.spacetype != SPACE_VIEW3D || !area.spacedata.first()) {
          continue;
        }
        const View3D &view = *reinterpret_cast<const View3D *>(area.spacedata.first());
        if (!(view.flag2 & V3D_HIDE_OVERLAYS) && view.overlay.flag & V3D_OVERLAY_MESH_ONION_SKIN) {
          subscribed = true;
          break;
        }
      }
    }
  }
  if (!subscribed) {
    state.retired = true;
    state.published.reset();
    /* A timer must not destroy its own WM job. Cooperatively stop, then free in the WM loop. */
    WM_jobs_stop_type(state.wm, &state, WM_JOB_TYPE_MESH_ONION_SKIN);
    state.ready.notify_one();
    return;
  }
  if (state.restart_requested) {
    /* Requests only change the main-thread membership mailbox. Let WM reap the worker before
     * releasing its capsule, then a redraw can register the replacement delayed job. */
    WM_jobs_stop_type(state.wm, &state, WM_JOB_TYPE_MESH_ONION_SKIN);
    state.ready.notify_one();
    return;
  }
  stage(state);
}

static void job_update(void *custom_data)
{
  State &state = **static_cast<std::shared_ptr<State> *>(custom_data);
  std::lock_guard lock(state.mutex);
  if (state.retired) {
    state.completed.reset();
    return;
  }
  /* A newer pending edit does not invalidate a completed same-epoch result. This is essential
   * during continuous input, otherwise results can be perpetually starved. */
  if (state.completed && state.completed->epoch == state.epoch &&
      (!state.published || state.completed->generation > state.published->generation))
  {
    state.published = std::move(state.completed);
    state.message.clear();
    if (std::getenv("BLENDER_MESH_ONION_SKIN_DIAGNOSTICS")) {
      const Bundle &bundle = *state.published;
      printf(
          "mesh_onion_skin generation=%llu staging_ms=%.3f worker_ms=%.3f "
          "input_to_publish_ms=%.3f bytes=%zu\n",
          static_cast<unsigned long long>(bundle.generation),
          bundle.staging_seconds * 1000.0,
          bundle.evaluation_seconds * 1000.0,
          (BLI_time_now_seconds() - bundle.input_time) * 1000.0,
          bundle.bytes);
    }
  }
  if (!state.worker_error.empty()) {
    if (state.worker_error_epoch == state.epoch &&
        (!state.published || state.worker_error_generation > state.published->generation))
    {
      state.message = std::move(state.worker_error);
      state.published.reset();
    }
    state.worker_error.clear();
  }
}

static void job_free(void *custom_data)
{
  auto *owner = static_cast<std::shared_ptr<State> *>(custom_data);
  const bool restart = (*owner)->restart_requested && !(*owner)->retired &&
                       states.contains(*owner);
  if (!restart && ((*owner)->capsule || !(*owner)->initialized)) {
    (*owner)->retired = true;
  }
  (*owner)->capsule.reset();
  (*owner)->job_registered = false;
  (*owner)->window = nullptr;
  if (restart) {
    (*owner)->initialized = false;
    (*owner)->staged_generation = 0;
    std::lock_guard lock((*owner)->mutex);
    (*owner)->pending.reset();
    (*owner)->completed.reset();
    (*owner)->worker_error.clear();
  }
  if ((*owner)->retired) {
    states.remove_if([&](const std::shared_ptr<State> &state) { return state == *owner; });
  }
  MEM_delete(owner);
}

static void job_register(const std::shared_ptr<State> &owned_state)
{
  State &state = *owned_state;
  /* WM jobs of the same type are serialized. Do not leave a second persistent worker queued
   * forever behind the first one: this initial envelope explicitly supports one active rig. */
  for (const std::shared_ptr<State> &other : states) {
    if (other != owned_state && other->job_registered) {
      state.message = "Mesh onion skin currently supports one active armature at a time";
      return;
    }
  }
  state.job_registered = true;
  state.message.clear();
  wmJob *job = WM_jobs_get(state.wm,
                           state.window,
                           &state,
                           "Mesh Onion Skin",
                           eWM_JobFlag(0),
                           WM_JOB_TYPE_MESH_ONION_SKIN);
  WM_jobs_customdata_set(job, MEM_new<std::shared_ptr<State>>(__func__, owned_state), job_free);
  WM_jobs_timer(job, 0.01, NC_SPACE | ND_SPACE_VIEW3D, NC_SPACE | ND_SPACE_VIEW3D, job_timer);
  WM_jobs_callbacks(job, job_start, job_init, job_update, nullptr);
  /* Draw callers only post requests. Capsule preparation runs later in the WM event loop. */
  WM_jobs_delay_start(job, 0.01);
  WM_jobs_start(state.wm, job);
}

void request(bContext &C,
             Depsgraph &depsgraph,
             Object &rig,
             const Span<Object *> meshes,
             const Settings &settings)
{
  Scene *scene = CTX_data_scene(&C);
  wmWindowManager *wm = CTX_wm_manager(&C);
  wmWindow *window = CTX_wm_window(&C);
  if (!scene || !wm || !window || meshes.is_empty()) {
    return;
  }
  ViewLayer *requested_view_layer = DEG_get_input_view_layer(&depsgraph);
  if (BLI_findindex(&scene->view_layers, requested_view_layer) == -1) {
    return;
  }
  State *state = find_state(*scene, rig);
  const float frame = scene->r.cfra + scene->r.subframe;
  Settings bounded = settings;
  bounded.before = math::clamp(bounded.before, 0, sample_limit / 2);
  bounded.after = math::clamp(bounded.after, 0, sample_limit / 2);
  bounded.step = math::max(bounded.step, 1);
  if (bounded.before + bounded.after == 0) {
    cancel(*wm, *scene, rig);
    return;
  }
  if (state) {
    if (source_membership_update(
            *scene, state->source_view_layer, state->source_meshes, requested_view_layer, meshes))
    {
      /* Register new source IDs immediately so remapping/removal remains safe while the old
       * worker is winding down. It never reads this main-thread union. */
      for (Object *mesh : meshes) {
        state->source_dependencies.add(mesh->id.session_uid);
      }
      state->generation++;
      state->epoch++;
      state->input_time = BLI_time_now_seconds();
      state->published.reset();
      state->restart_requested = state->initialized && state->job_registered;
      if (!state->job_registered) {
        /* A previously rejected capsule has no timer to process a membership retry. */
        state->initialized = false;
      }
      std::lock_guard lock(state->mutex);
      state->pending.reset();
      state->completed.reset();
    }
    if (state->frame != frame) {
      state->frame = frame;
      state->generation++;
      state->input_time = BLI_time_now_seconds();
    }
    if (state->frame_start != scene->r.sfra || state->frame_end != scene->r.efra) {
      state->frame_start = scene->r.sfra;
      state->frame_end = scene->r.efra;
      if (state->settings.loop_frame_range) {
        state->generation++;
        state->input_time = BLI_time_now_seconds();
      }
    }
    if (!state->initialized && !state->job_registered) {
      /* A rejected secondary request has no WM job to receive a window-close callback. Refresh
       * its main-thread window/view-layer handles before retrying from a different live window. */
      state->wm = wm;
      state->window = window;
      if (BLI_findindex(&scene->view_layers, state->source_view_layer) == -1) {
        state->source_view_layer = requested_view_layer;
      }
      for (const std::shared_ptr<State> &owned_state : states) {
        if (owned_state.get() == state) {
          job_register(owned_state);
          break;
        }
      }
    }
    return;
  }
  auto owned_state = std::make_shared<State>();
  state = owned_state.get();
  state->source_main = CTX_data_main(&C);
  state->source_scene = scene;
  state->frame_start = scene->r.sfra;
  state->frame_end = scene->r.efra;
  state->source_view_layer = requested_view_layer;
  state->source_rig = &rig;
  state->source_meshes.extend(meshes);
  for (Object *mesh : meshes) {
    state->source_dependencies.add(mesh->id.session_uid);
  }
  state->wm = wm;
  state->window = window;
  state->scene_uid = uid(scene->id);
  state->rig_uid = uid(rig.id);
  state->settings = bounded;
  state->frame = frame;
  state->input_time = BLI_time_now_seconds();
  state->input = captured_inputs.lookup_default({state->scene_uid, state->rig_uid}, nullptr);
  states.append(owned_state);
  job_register(owned_state);
}

std::shared_ptr<const Bundle> get(const Scene &scene, const Object &rig)
{
  State *state = find_state(scene, rig);
  return state ? state->published : nullptr;
}

std::string status(const Scene &scene, const Object &rig)
{
  State *state = find_state(scene, rig);
  return state ? state->message : std::string();
}

bool is_requested(const Scene &scene, const Object &rig)
{
  State *state = find_state(scene, rig);
  return state && state->job_registered;
}

bool settings_get(const Scene &scene, const Object &rig, Settings &r_settings)
{
  State *state = find_state(scene, rig);
  if (!state) {
    return false;
  }
  r_settings = state->settings;
  return true;
}

void settings_update(const Scene &scene, const Object &rig, const Settings &settings)
{
  State *state = find_state(scene, rig);
  if (!state || state->settings == settings) {
    return;
  }
  state->settings = settings;
  state->settings.before = math::clamp(settings.before, 0, sample_limit / 2);
  state->settings.after = math::clamp(settings.after, 0, sample_limit / 2);
  state->settings.step = math::max(settings.step, 1);
  state->generation++;
  state->input_time = BLI_time_now_seconds();
}

void preview_input_update(bContext &C, std::shared_ptr<const PosePreviewInput> input)
{
  Scene *scene = CTX_data_scene(&C);
  if (!scene) {
    return;
  }
  /* Capture can precede the first overlay draw. Retain that proposal so a single drag event
   * followed by a pause still previews the proposed keys once the delayed job starts. */
  captured_inputs.add_overwrite({uid(scene->id), input->object_uid}, input);
  for (const std::shared_ptr<State> &state : states) {
    if (state->scene_uid == uid(scene->id) && state->rig_uid == input->object_uid) {
      state->input = std::move(input);
      if (!state->input->supported) {
        state->epoch++;
        state->published.reset();
        state->message = "Current pose edit is outside the supported onion skin preview envelope";
        std::lock_guard lock(state->mutex);
        state->pending.reset();
        state->completed.reset();
      }
      state->frame = state->input->frame;
      state->generation++;
      state->input_time = BLI_time_now_seconds();
      if (state->input->supported) {
        /* Stage while the authored pose is available instead of waiting for the producer timer.
         * Only private IDs are keyed; the worker receives an immutable latest-input packet. */
        stage(*state);
      }
      break;
    }
  }
}

void preview_finish(bContext &C, const uint64_t transaction_id, const bool cancelled)
{
  wmWindowManager *wm = CTX_wm_manager(&C);
  Vector<std::pair<SessionUID, SessionUID>> finished_inputs;
  for (const auto item : captured_inputs.items()) {
    if (item.value->transaction_id == transaction_id) {
      finished_inputs.append(item.key);
    }
  }
  for (const auto &key : finished_inputs) {
    captured_inputs.remove(key);
  }
  /* Stop the private graph before dropping the provisional Action. The next draw rebuilds from
   * committed/restored source data. No provisional channel can survive cancel or confirmation. */
  for (int index = states.size() - 1; index >= 0; index--) {
    const std::shared_ptr<State> state = states[index];
    if (state->input && state->input->transaction_id == transaction_id) {
      preview_debug_action(*state->source_rig, cancelled ? "cancel" : "confirm");
      state->epoch++;
      states.remove_if([&](const std::shared_ptr<State> &other) { return other == state; });
      WM_jobs_kill_type(wm, state.get(), WM_JOB_TYPE_MESH_ONION_SKIN);
    }
  }
}

void invalidate_all(wmWindowManager *wm)
{
  /* Jobs retain shared ownership until joined. Detach the registry first so their synchronous
   * free callbacks cannot invalidate a registry iteration or remove the same entry twice. */
  states.clear();
  captured_inputs.clear();
  if (wm) {
    WM_jobs_kill_type(wm, nullptr, WM_JOB_TYPE_MESH_ONION_SKIN);
  }
}

void tag_id_changed(Main &bmain, ID &id)
{
  for (int index = states.size() - 1; index >= 0; index--) {
    const std::shared_ptr<State> state = states[index];
    if (state->source_main != &bmain) {
      continue;
    }
    const bool affected = state->source_dependencies.contains(id.session_uid) ||
                          id.session_uid == state->source_scene->id.session_uid ||
                          id.session_uid == state->source_rig->id.session_uid;
    if (!affected) {
      continue;
    }
    if (std::getenv("BLENDER_MESH_ONION_SKIN_INVALIDATION_DIAGNOSTICS")) {
      std::fprintf(
          stderr,
          "mesh_onion_skin id_update name=%s recalc=0x%08x moving=%d "
          "pose=%d input=%d supported=%d transaction=%llu generation=%llu\n",
          id.name,
          unsigned(id.recalc),
          int(G.moving),
          bool(state->source_rig->mode & OB_MODE_POSE),
          bool(state->input),
          state->input ? state->input->supported : false,
          static_cast<unsigned long long>(state->input ? state->input->transaction_id : 0),
          static_cast<unsigned long long>(state->generation));
    }
    if (!(id.recalc & (ID_RECALC_ANIMATION | ID_RECALC_TRANSFORM | ID_RECALC_GEOMETRY |
                       ID_RECALC_PARAMETERS | ID_RECALC_SOURCE)))
    {
      continue;
    }
    /* Pose transforms are explicitly captured after the authored channel/mirror update. Their
     * flush must not tear down the capsule on every mouse event. */
    if (id.session_uid == state->source_rig->id.session_uid && G.moving &&
        state->source_rig->mode & OB_MODE_POSE &&
        !(id.recalc & (ID_RECALC_PARAMETERS | ID_RECALC_SOURCE | ID_RECALC_ANIMATION)))
    {
      continue;
    }
    if (state->armature_rest &&
        keep_pose_armature_update(*state->source_rig,
                                  *state->armature_rest,
                                  id,
                                  unsigned(id.recalc),
                                  G.moving && state->input && state->input->supported &&
                                      state->input->transaction_id != 0))
    {
      if (std::getenv("BLENDER_MESH_ONION_SKIN_INVALIDATION_DIAGNOSTICS")) {
        std::fprintf(stderr, "mesh_onion_skin keep_legacy_pose_data name=%s\n", id.name);
      }
      continue;
    }
    states.remove_if([&](const std::shared_ptr<State> &other) { return other == state; });
    WM_jobs_kill_type(state->wm, state.get(), WM_JOB_TYPE_MESH_ONION_SKIN);
  }
}

void tag_ids_remapped(const bke::id::IDRemapper &mappings)
{
  Set<unsigned int> remapped_ids;
  mappings.iter([&](ID *old_id, ID * /*new_id*/) {
    if (old_id) {
      remapped_ids.add(old_id->session_uid);
    }
  });
  Vector<std::pair<SessionUID, SessionUID>> stale_inputs;
  for (const auto item : captured_inputs.items()) {
    if (remapped_ids.contains(unsigned(item.key.first.hash())) ||
        remapped_ids.contains(unsigned(item.key.second.hash())))
    {
      stale_inputs.append(item.key);
    }
  }
  for (const auto &key : stale_inputs) {
    captured_inputs.remove(key);
  }
  for (int index = states.size() - 1; index >= 0; index--) {
    const std::shared_ptr<State> state = states[index];
    bool affected = remapped_ids.contains(unsigned(state->scene_uid.hash())) ||
                    remapped_ids.contains(unsigned(state->rig_uid.hash()));
    for (const unsigned int dependency : state->source_dependencies) {
      affected |= remapped_ids.contains(dependency);
    }
    if (affected) {
      captured_inputs.remove({state->scene_uid, state->rig_uid});
      states.remove_if([&](const std::shared_ptr<State> &other) { return other == state; });
      WM_jobs_kill_type(state->wm, state.get(), WM_JOB_TYPE_MESH_ONION_SKIN);
    }
  }
}

void cancel(wmWindowManager &wm, const Scene &scene, const Object &rig)
{
  for (int index = states.size() - 1; index >= 0; index--) {
    const std::shared_ptr<State> state = states[index];
    if (state->scene_uid == uid(scene.id) && state->rig_uid == uid(rig.id)) {
      states.remove_if([&](const std::shared_ptr<State> &other) { return other == state; });
      WM_jobs_kill_type(&wm, state.get(), WM_JOB_TYPE_MESH_ONION_SKIN);
    }
  }
}

#ifdef WITH_GTESTS
bool source_membership_update_for_test(const Scene &scene,
                                       ViewLayer *&source_view_layer,
                                       Vector<Object *> &source_meshes,
                                       ViewLayer *requested_view_layer,
                                       const Span<Object *> meshes)
{
  return source_membership_update(
      scene, source_view_layer, source_meshes, requested_view_layer, meshes);
}

std::shared_ptr<const ArmatureRestDefinition> armature_rest_capture_for_test(
    const bArmature &armature)
{
  return armature_rest_capture(armature);
}

bool keep_pose_armature_update_for_test(const Object &rig,
                                        const ArmatureRestDefinition &definition,
                                        const ID &changed_id,
                                        const uint32_t recalc,
                                        const bool supported_modal_pose)
{
  return keep_pose_armature_update(rig, definition, changed_id, recalc, supported_modal_pose);
}

std::shared_ptr<const Bundle> evaluate_for_test(Main &bmain,
                                                Scene &scene,
                                                ViewLayer &view_layer,
                                                Object &rig,
                                                const Span<Object *> meshes,
                                                const Settings &settings,
                                                const float frame,
                                                std::shared_ptr<const PreparedPreview> preview,
                                                std::string &r_error)
{
  State state;
  state.source_main = &bmain;
  state.source_scene = &scene;
  state.source_view_layer = &view_layer;
  state.source_rig = &rig;
  state.rig_uid = uid(rig.id);
  state.source_meshes.extend(meshes);
  std::unique_ptr<Capsule> capsule = capsule_create(state);
  if (!capsule) {
    r_error = std::move(state.message);
    return nullptr;
  }
  Work work;
  work.settings = settings;
  work.settings.before = math::clamp(settings.before, 0, sample_limit / 2);
  work.settings.after = math::clamp(settings.after, 0, sample_limit / 2);
  work.settings.step = math::max(settings.step, 1);
  work.frame = frame;
  work.generation = 1;
  work.epoch = 1;
  work.preview = std::move(preview);
  if (settings.keyed_frames_only && !state.keyed_frames.unsupported_reason.empty()) {
    r_error = state.keyed_frames.unsupported_reason;
    return nullptr;
  }
  work.frames = sample_frames_select(
      state.keyed_frames.frames, work.settings, frame, scene.r.sfra, scene.r.efra);
  wmJobWorkerStatus worker_status{};
  auto result = evaluate(*capsule, work, worker_status);
  if (!result) {
    r_error = "Private geometry evaluation failed";
  }
  return result;
}
#endif

}  // namespace blender::ed::mesh_onion_skin
