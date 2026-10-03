/* SPDX-FileCopyrightText: 2026 Blender Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

#pragma once

#include "ED_mesh_onion_skin.hh"

namespace blender::ed::mesh_onion_skin {

struct KeyedFrames {
  /** Sorted unique authored keys in scene time; never includes provisional preview keys. */
  Vector<float> frames;
  /** Mapping cannot be inverted reliably. Regular frame sampling remains available. */
  std::string unsupported_reason;
};

/** Main-thread read-only collection from the rig's assigned Action slot and enabled NLA stack. */
KeyedFrames keyed_frames_collect(const Object &rig);

struct SampleFrame {
  float frame;
  /** Signed temporal distance from the current frame, including wrapping around the range. */
  float offset;
};

/** Select nearest authored keys, or stepped frames. Results are ordered by signed offset.
 * Looping uses the inclusive scene frame range and does not repeat laps or duplicate samples. */
Vector<SampleFrame> sample_frames_select(Span<float> sorted_keyed_frames,
                                         const Settings &settings,
                                         float current_frame,
                                         int frame_start,
                                         int frame_end);

}  // namespace blender::ed::mesh_onion_skin
