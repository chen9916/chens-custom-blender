/* SPDX-FileCopyrightText: 2026 Blender Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

#pragma once

#include "ED_mesh_onion_skin.hh"

namespace blender {
struct ViewLayer;
struct bArmature;
namespace ed::mesh_onion_skin {
struct PreparedPreview;
struct ArmatureRestDefinition;

/** Production membership mailbox: true only when a source layer replacement or new mesh
 * requires rebuilding the private capsule. */
bool source_membership_update_for_test(const Scene &scene,
                                       ViewLayer *&source_view_layer,
                                       Vector<Object *> &source_meshes,
                                       ViewLayer *requested_view_layer,
                                       Span<Object *> meshes);

/** Exercise the production main-thread invalidation predicate with an opaque owned baseline. */
std::shared_ptr<const ArmatureRestDefinition> armature_rest_capture_for_test(
    const bArmature &armature);
bool keep_pose_armature_update_for_test(const Object &rig,
                                        const ArmatureRestDefinition &definition,
                                        const ID &changed_id,
                                        uint32_t recalc,
                                        bool supported_modal_pose);

/** Compiled only with WITH_GTESTS. Exercises the production capsule and snapshot path without WM
 * jobs, editor callbacks or GPU resources. */
std::shared_ptr<const Bundle> evaluate_for_test(Main &bmain,
                                                Scene &scene,
                                                ViewLayer &view_layer,
                                                Object &rig,
                                                Span<Object *> meshes,
                                                const Settings &settings,
                                                float frame,
                                                std::shared_ptr<const PreparedPreview> preview,
                                                std::string &r_error);
}  // namespace ed::mesh_onion_skin
}  // namespace blender
