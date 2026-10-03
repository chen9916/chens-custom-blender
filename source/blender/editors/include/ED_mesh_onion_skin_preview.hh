/* SPDX-FileCopyrightText: 2026 Blender Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

#pragma once

#include <memory>
#include <string>

#include "BLI_map.hh"
#include "BLI_session_uid.hh"
#include "BLI_vector.hh"

#include "RNA_path.hh"

namespace blender {
struct Main;
struct Scene;
struct Object;
struct bAction;
struct bContext;
namespace animrig {
class Channelbag;
}
namespace ed::mesh_onion_skin {

/** Owns authored values, never evaluated pose matrices or source pointers. */
struct PoseBoneInput {
  std::string name;
  float location[3];
  float euler[3];
  float quaternion[4];
  float axis[3];
  float angle;
  float scale[3];
  short rotation_mode;
  Vector<RNAPath> key_paths;
};

/** Cheap capture on each transform event. Native key insertion happens only at refresh. */
struct PosePreviewInput {
  SessionUID object_uid;
  uint64_t transaction_id = 0;
  float frame = 0.0f;
  int key_flags = 0;
  int keyframe_type = 0;
  bool insert_keys = false;
  bool supported = true;
  Vector<PoseBoneInput> bones;
};

/** Immutable hand-off: all curves and RNA paths are owned by the packet. */
struct PreparedPreview {
  SessionUID object_uid;
  uint64_t transaction_id = 0;
  Vector<PoseBoneInput> bones;
  std::unique_ptr<animrig::Channelbag> channelbag;
  SessionUID action_uid;
  int slot_handle = 0;
  bool assign_action = false;
  /** Owned out-of-Main template, used only when keying creates an Action or slot. */
  bAction *action_template = nullptr;

  PreparedPreview();
  ~PreparedPreview();
};

struct PreviewApplyState {
  /** Borrowed from the private Main; never points into the user's database. */
  bAction *created_action = nullptr;
};

bool preview_enabled(const bContext &C);
bool preview_enabled_for_rig(const bContext &C, Scene &scene, Object &rig);

/** Optional correctness diagnostic; disabled unless explicitly requested through the environment.
 */
void preview_debug_action(const Object &rig, const char *label);

/** Main-thread only. Resets to source baseline for every proposal; never edits source IDs. */
std::shared_ptr<const PreparedPreview> preview_prepare(Main &bmain,
                                                       Scene &scene,
                                                       Object &source_rig,
                                                       const PosePreviewInput &input);

/** Worker only. Applies authored values and owned curve copies to the private workspace. */
bool preview_apply(const PreparedPreview &packet,
                   Main &private_main,
                   Map<SessionUID, Object *> &copied_objects,
                   Map<SessionUID, bAction *> &copied_actions,
                   PreviewApplyState &state,
                   bool *r_structure_changed = nullptr);

}  // namespace ed::mesh_onion_skin
}  // namespace blender
