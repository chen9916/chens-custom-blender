/* SPDX-FileCopyrightText: 2026 Blender Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

#pragma once

#include <memory>
#include <string>

#include "BLI_array.hh"
#include "BLI_math_matrix_types.hh"
#include "BLI_math_vector_types.hh"
#include "BLI_session_uid.hh"
#include "BLI_span.hh"
#include "BLI_vector.hh"

namespace blender {
struct bContext;
struct Depsgraph;
struct ID;
struct Main;
struct Object;
struct Scene;
struct wmWindowManager;
namespace bke::id {
class IDRemapper;
}

namespace ed::mesh_onion_skin {
struct PosePreviewInput;

struct Settings {
  int before = 2;
  int after = 2;
  int step = 1;
  bool keyed_frames_only = false;
  bool loop_frame_range = false;
  bool operator==(const Settings &) const = default;
};

enum class SamplingMode : uint8_t { Relative, Timeline };

/** Owned immutable geometry, shared by view policies and overlapping timeline windows. */
struct SampleGeometry {
  float4x4 object_to_world;
  Array<float3> positions;
  Array<int3> triangles;
};

struct Sample {
  SessionUID object_uid;
  float frame;
  /** Signed distance from the playhead, including travel across a loop boundary. */
  float temporal_offset;
  /** Stable grid/key index, including the selected lap when looping. */
  float rank = 0.0f;
  std::shared_ptr<const SampleGeometry> geometry;
};

/** Immutable, coherent result. All samples belong to the same edit revision. */
struct Bundle {
  uint64_t generation = 0;
  uint64_t epoch = 0;
  uint64_t geometry_revision = 0;
  float frame = 0.0f;
  double input_time = 0.0;
  double staging_seconds = 0.0;
  double evaluation_seconds = 0.0;
  size_t bytes = 0;
  Vector<Sample> samples;
  Vector<Sample> timeline_samples;
};

/** Main-thread only. Queues work; never evaluates geometry in the draw caller. */
void request(bContext &C,
             Depsgraph &depsgraph,
             Object &rig,
             Span<Object *> meshes,
             const Settings &settings,
             SamplingMode mode = SamplingMode::Relative);
std::shared_ptr<const Bundle> get(const Scene &scene, const Object &rig);
std::string status(const Scene &scene, const Object &rig);
bool is_requested(const Scene &scene, const Object &rig);
bool settings_get(const Scene &scene, const Object &rig, Settings &r_settings);
void settings_update(const Scene &scene, const Object &rig, const Settings &settings);

/** Cheap main-thread input mailbox, populated by the transform adapter. */
void preview_input_update(bContext &C, std::shared_ptr<const PosePreviewInput> input);
void preview_finish(bContext &C, uint64_t transaction_id, bool cancelled);

/** Stop and join private evaluation before source lifetime or topology changes. */
void invalidate_all(wmWindowManager *wm);
void tag_id_changed(Main &bmain, ID &id);
void tag_ids_remapped(const bke::id::IDRemapper &mappings);
void cancel(wmWindowManager &wm, const Scene &scene, const Object &rig);
}  // namespace ed::mesh_onion_skin
}  // namespace blender
