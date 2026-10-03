/* SPDX-FileCopyrightText: 2026 Blender Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

/** \file
 * \ingroup overlay
 */

#pragma once

#include <algorithm>
#include <bit>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <limits>

#include "BLI_fileops.hh"
#include "BLI_map.hh"
#include "BLI_math_matrix.hh"
#include "BLI_path_utils.hh"
#include "BLI_time.hh"

#include "DNA_screen_types.h"

#include "BKE_context.hh"
#include "BKE_modifier.hh"
#include "BKE_scene.hh"

#include "DEG_depsgraph_query.hh"

#include "ED_mesh_onion_skin.hh"
#include "ED_screen_types.hh"

#include "GPU_batch.hh"
#include "GPU_index_buffer.hh"
#include "GPU_vertex_buffer.hh"

#include "WM_types.hh"

#include "draw_manager_text.hh"

#include "overlay_base.hh"

namespace blender::draw::overlay {

/** Draw immutable, privately evaluated poses without evaluating animation in the draw engine. */
class MeshOnionSkin : Overlay {
 private:
  struct BatchDeleter {
    void operator()(gpu::Batch *batch)
    {
      GPU_BATCH_DISCARD_SAFE(batch);
    }
  };
  using BatchPtr = std::unique_ptr<gpu::Batch, BatchDeleter>;

  struct BatchSlot {
    SessionUID object_uid;
    float frame = 0.0f;
    std::shared_ptr<const ed::mesh_onion_skin::SampleGeometry> geometry;
    BatchPtr batch;
  };

  PassMain ps_ = {"MeshOnionSkin"};
  Vector<Object *> meshes_;
  Vector<SessionUID> visible_meshes_;
  Object *rig_ = nullptr;
  std::shared_ptr<const ed::mesh_onion_skin::Bundle> bundle_;
  Vector<BatchSlot> batches_;
  Vector<float> sample_opacities_;
  Vector<bool> sample_submitted_;
  uint64_t batch_epoch_ = 0;
  uint64_t batch_creates_ = 0;
  uint64_t vbo_uploads_ = 0;
  bool batches_screen_offset_ = false;
  bool diagnostics_pending_ = false;
  int visible_samples_ = 0;
  bool geometry_dump_enabled_ = false;
  bool screen_offset_enabled_ = false;
  bool screen_offset_reversed_ = false;
  float screen_offset_spacing_ = 0.0f;
  float playhead_frame_ = 0.0f;
  float native_frame_ = 0.0f;
  float playhead_rank_ = 0.0f;
  float before_count_ = 0.0f;
  float after_count_ = 0.0f;
  float bundle_playhead_delta_ = 0.0f;
  float loop_period_ = 0.0f;
  const wmTimer *scroll_timer_ = nullptr;
  SessionUID scroll_scene_uid_ = {};
  double scroll_time_ = 0.0;
  double native_unwrapped_frame_ = 0.0;
  double bundle_scroll_origin_ = 0.0;
  float previous_native_frame_ = 0.0f;
  float previous_loop_period_ = 0.0f;
  bool scroll_initialized_ = false;
  Map<SessionUID, std::string> dump_mesh_names_;
  Vector<int> dump_sample_indices_;
  std::string dumped_phase_;
  std::weak_ptr<const ed::mesh_onion_skin::Bundle> dumped_bundle_;

 public:
  void begin_sync(Resources &res, const State &state) final
  {
    ps_.init();
    meshes_.clear();
    visible_meshes_.clear();
    visible_samples_ = 0;
    rig_ = nullptr;
    enabled_ = state.is_space_v3d() && !state.hide_overlays && !res.is_selection() &&
               !state.is_depth_only_drawing && !state.is_image_render && state.object_active &&
               state.object_active->type == OB_ARMATURE &&
               (state.object_active->mode & OB_MODE_POSE) &&
               (state.overlay.flag & V3D_OVERLAY_MESH_ONION_SKIN) && DRW_context_get()->evil_C;
    if (!enabled_) {
      bundle_.reset();
      batches_.clear();
      scroll_initialized_ = false;
      return;
    }
    /* Read the opt-in once, before the first enabled draw. Normal and timing runs do not
     * allocate, transform positions, or write diagnostic data. */
    static const bool dump_requested = BLI_getenv(
                                           "BLENDER_MESH_ONION_SKIN_GEOMETRY_DUMP_DIRECTORY") !=
                                       nullptr;
    geometry_dump_enabled_ = dump_requested;
    if (geometry_dump_enabled_) {
      dump_mesh_names_.clear();
      dump_sample_indices_.clear();
    }

    rig_ = const_cast<Object *>(DEG_get_original(state.object_active));
    screen_offset_enabled_ = state.overlay.flag & V3D_OVERLAY_MESH_ONION_SKIN_SCREEN_OFFSET;
    screen_offset_reversed_ = state.overlay.flag &
                              V3D_OVERLAY_MESH_ONION_SKIN_SCREEN_OFFSET_REVERSE;
    screen_offset_spacing_ = state.overlay.mesh_onion_skin_screen_offset_spacing;
    DRWState draw_state = DRW_STATE_WRITE_COLOR | DRW_STATE_BLEND_ALPHA;
    if (!(state.overlay.flag & V3D_OVERLAY_MESH_ONION_SKIN_IN_FRONT)) {
      draw_state |= DRW_STATE_DEPTH_LESS_EQUAL;
    }
    /* Ghosts never write depth and never participate in selection or depth picking. */
    ps_.state_set(draw_state, state.clipping_plane_count);
    ps_.shader_set(screen_offset_enabled_ ? res.shaders->mesh_onion_skin_screen.get() :
                                            res.shaders->uniform_color.get());
    if (screen_offset_enabled_) {
      ps_.bind_ubo(OVERLAY_GLOBALS_SLOT, &res.globals_buf);
      ps_.bind_ubo(DRW_CLIPPING_UBO_SLOT, &res.clip_planes_buf);
    }
  }

  void object_sync(Manager & /*manager*/,
                   const ObjectRef &ob_ref,
                   Resources & /*res*/,
                   const State & /*state*/) final
  {
    if (!enabled_ || ob_ref.is_dupli() || ob_ref.object->type != OB_MESH) {
      return;
    }
    Object *armature = BKE_modifiers_is_deformed_by_armature(ob_ref.object);
    if (!armature || DEG_get_original(armature) != rig_) {
      return;
    }
    Object *mesh = DEG_get_original(ob_ref.object);
    if (!meshes_.contains(mesh)) {
      meshes_.append(mesh);
      visible_meshes_.append(SessionUID{uint64_t(mesh->id.session_uid)});
      if (geometry_dump_enabled_) {
        dump_mesh_names_.add(SessionUID{uint64_t(mesh->id.session_uid)}, mesh->id.name + 2);
      }
    }
  }

  void end_sync(Resources & /*res*/, const State &state) final
  {
    if (!enabled_) {
      return;
    }
    const DRWContext *ctx = DRW_context_get();
    const Scene &scene = *DEG_get_input_scene(state.depsgraph);
    native_frame_ = BKE_scene_frame_get(&scene);
    playhead_frame_ = native_frame_;
    const double now = BLI_time_now_seconds();
    const wmTimer *animation_timer = nullptr;
    bool reverse_playback = false;
    if (screen_offset_enabled_) {
      /* The animation operator owns scene time. Only interpolate the presentation between
       * its native timer ticks; no additional dependency graph evaluation is needed. */
      const bScreen *screen = CTX_wm_screen(ctx->evil_C);
      const wmTimer *timer = screen ? screen->animtimer : nullptr;
      const ScreenAnimData *playback = timer ? static_cast<ScreenAnimData *>(timer->customdata) :
                                               nullptr;
      if (playback && playback->scene == &scene && !timer->sleep &&
          !(timer->flags & WM_TIMER_TAGGED_FOR_REMOVAL))
      {
        animation_timer = timer;
        reverse_playback = playback->flag & ANIMPLAY_FLAG_REVERSE;
        const double elapsed = std::max(0.0, now - timer->time_last);
        const float fraction = std::clamp(float(elapsed * scene.frames_per_second()), 0.0f, 1.0f);
        playhead_frame_ += reverse_playback ? -fraction : fraction;
      }
    }
    loop_period_ = (state.overlay.flag & V3D_OVERLAY_MESH_ONION_SKIN_LOOP_FRAME_RANGE) ?
                       std::max(1.0f, float(scene.r.efra) - float(scene.r.sfra) + 1.0f) :
                       0.0f;
    if (loop_period_ == 1.0f) {
      /* A one-frame loop contains only the current pose, with no neighboring identities. */
      playhead_frame_ = native_frame_;
    }
    bool reset_scroll = !scroll_initialized_ || scroll_timer_ != animation_timer ||
                        scroll_scene_uid_ != SessionUID{uint64_t(scene.id.session_uid)} ||
                        previous_loop_period_ != loop_period_;
    float native_delta = native_frame_ - previous_native_frame_;
    if (animation_timer && loop_period_ > 0.0f) {
      native_delta = std::remainder(native_delta, loop_period_);
    }
    if (!reset_scroll && native_delta != 0.0f) {
      const double expected_steps = std::max(0.0, now - scroll_time_) * scene.frames_per_second();
      /* Explicit seeks or playback direction changes establish a new presentation origin.
       * Regular wraps accumulate monotonically, including bundles held for multiple laps. */
      reset_scroll = !animation_timer ||
                     (reverse_playback ? native_delta > 0.0f : native_delta < 0.0f) ||
                     std::abs(native_delta) > std::ceil(expected_steps) + 2.0;
    }
    native_unwrapped_frame_ = reset_scroll ? double(native_frame_) :
                                             native_unwrapped_frame_ + native_delta;
    previous_native_frame_ = native_frame_;
    previous_loop_period_ = loop_period_;
    scroll_timer_ = animation_timer;
    scroll_scene_uid_ = SessionUID{uint64_t(scene.id.session_uid)};
    scroll_time_ = now;
    scroll_initialized_ = screen_offset_enabled_;
    const ed::mesh_onion_skin::Settings settings = {
        state.overlay.mesh_onion_skin_before,
        state.overlay.mesh_onion_skin_after,
        state.overlay.mesh_onion_skin_step,
        bool(state.overlay.flag & V3D_OVERLAY_MESH_ONION_SKIN_KEYED_FRAMES_ONLY),
        bool(state.overlay.flag & V3D_OVERLAY_MESH_ONION_SKIN_LOOP_FRAME_RANGE)};
    before_count_ = float(settings.before);
    after_count_ = float(settings.after);
    /* Only queues a request. Workspace preparation and evaluation run outside drawing. */
    ed::mesh_onion_skin::request(*const_cast<bContext *>(ctx->evil_C),
                                 *state.depsgraph,
                                 *rig_,
                                 meshes_,
                                 settings,
                                 screen_offset_enabled_ ?
                                     ed::mesh_onion_skin::SamplingMode::Timeline :
                                     ed::mesh_onion_skin::SamplingMode::Relative);
    std::shared_ptr<const ed::mesh_onion_skin::Bundle> bundle = ed::mesh_onion_skin::get(
        *DEG_get_input_scene(state.depsgraph), *rig_);
    const bool bundle_changed = bundle != bundle_;
    if (bundle != bundle_) {
      bundle_ = std::move(bundle);
      diagnostics_pending_ = bool(bundle_);
      if (!bundle_) {
        batches_.clear();
      }
    }
    if (!bundle_) {
      if (state.show_text) {
        const std::string message = ed::mesh_onion_skin::status(
            *DEG_get_input_scene(state.depsgraph), *rig_);
        if (!message.empty()) {
          const uchar color[4] = {255, 180, 64, 255};
          DRW_text_cache_add(state.dt,
                             state.object_active->object_to_world().location(),
                             message.c_str(),
                             message.size(),
                             10,
                             10,
                             DRW_TEXT_CACHE_GLOBALSPACE,
                             color,
                             true);
        }
      }
      meshes_.clear();
      rig_ = nullptr;
      return;
    }

    if (bundle_changed || batches_screen_offset_ != screen_offset_enabled_) {
      sync_batches();
    }
    if (bundle_changed || reset_scroll) {
      float center_delta = native_frame_ - bundle_->frame;
      if (loop_period_ > 0.0f) {
        center_delta = std::remainder(center_delta, loop_period_);
      }
      bundle_scroll_origin_ = native_unwrapped_frame_ - center_delta;
    }
    bundle_playhead_delta_ = float(native_unwrapped_frame_ - bundle_scroll_origin_) +
                             (playhead_frame_ - native_frame_);
    playhead_rank_ = interpolated_playhead_rank(bundle_playhead_delta_, settings.step);
    sample_opacities_.resize(samples().size());
    sample_submitted_.resize(samples().size());
    sample_opacities_.fill(0.0f);
    sample_submitted_.fill(false);

    /* Temporal distances are shared across meshes and both directions, including unevenly
     * spaced authored keys. Equal-distance samples keep the full configured opacity. */
    float nearest_distance = 0.0f;
    float farthest_distance = 0.0f;
    const bool distance_fade = state.overlay.flag & V3D_OVERLAY_MESH_ONION_SKIN_DISTANCE_FADE;
    if (distance_fade && !screen_offset_enabled_ && !samples().is_empty()) {
      nearest_distance = std::numeric_limits<float>::max();
      for (const ed::mesh_onion_skin::Sample &sample : samples()) {
        const float distance = std::abs(displayed_temporal_offset(sample));
        nearest_distance = std::min(nearest_distance, distance);
        farthest_distance = std::max(farthest_distance, distance);
      }
    }
    Manager &manager = *DRW_manager_get();
    for (const int i : samples().index_range()) {
      const ed::mesh_onion_skin::Sample &sample = samples()[i];
      if (!batches_[i].batch || !visible_meshes_.contains(sample.object_uid) ||
          state.overlay.mesh_onion_skin_opacity <= 0.0f)
      {
        continue;
      }
      const float temporal_offset = displayed_temporal_offset(sample);
      const bool before = temporal_offset < 0.0f;
      float opacity = state.overlay.mesh_onion_skin_opacity;
      if (screen_offset_enabled_) {
        const float rank = sample.rank - playhead_rank_;
        opacity *= timeline_opacity(rank);
        const bool both_sides = before_count_ > 0.0f && after_count_ > 0.0f;
        const float center_width = loop_period_ > 0.0f ? std::min(1.0f, loop_period_) : 1.0f;
        if (!both_sides) {
          /* The hidden side cannot retain a center ghost. Fade its visible neighbor in
           * from zero as it crosses the current pose, just like the outer hand-off. */
          opacity *= smoothstep(std::clamp(std::abs(temporal_offset) / center_width, 0.0f, 1.0f));
        }
        if (loop_period_ > 0.0f) {
          /* Short loops can choose a different occurrence of the same cached frame. Make
           * that alias hand-off invisible, instead of moving an opaque ghost across the view.
           * A both-sided strip uses the nearest lap; a one-sided strip uses its full lap. */
          const float reach = both_sides ? loop_period_ * 0.5f : loop_period_;
          const float edge_width = std::min(1.0f, reach);
          opacity *= smoothstep(
              std::clamp((reach - std::abs(temporal_offset)) / edge_width, 0.0f, 1.0f));
        }
        if (distance_fade) {
          const float boundary = std::max(1.0f, rank < 0.0f ? before_count_ : after_count_);
          const float t = std::clamp(std::abs(rank) / boundary, 0.0f, 1.0f);
          opacity *= 1.0f -
                     0.85f * std::clamp(state.overlay.mesh_onion_skin_fade_strength, 0.0f, 1.0f) *
                         smoothstep(t);
        }
      }
      else if (distance_fade && farthest_distance > nearest_distance) {
        const float distance = std::abs(temporal_offset);
        const float t = std::clamp(
            (distance - nearest_distance) / (farthest_distance - nearest_distance), 0.0f, 1.0f);
        const float smooth_distance = t * t * (3.0f - 2.0f * t);
        /* Preserve faint visibility even at maximum fade strength. */
        opacity *= 1.0f - 0.85f *
                              std::clamp(state.overlay.mesh_onion_skin_fade_strength, 0.0f, 1.0f) *
                              smooth_distance;
      }
      sample_opacities_[i] = opacity;
      if (opacity <= 0.0f) {
        continue;
      }
      const float4 past_color(0.16f, 0.55f, 1.0f, opacity);
      const float4 future_color(1.0f, 0.25f, 0.14f, opacity);
      const float transition = screen_offset_enabled_ ?
                                   smoothstep(std::clamp(
                                       sample.rank - playhead_rank_ + 0.5f, 0.0f, 1.0f)) :
                                   float(!before);
      const float4 color = past_color * (1.0f - transition) + future_color * transition;
      PassMain::Sub &sub = ps_.sub(before ? "Before" : "After");
      sub.push_constant("ucolor", color);
      if (screen_offset_enabled_) {
        sub.push_constant("screen_offset", screen_pixel_offset(sample));
      }
      const ResourceID handle = manager.resource_handle(sample.geometry->object_to_world);
      sub.draw(batches_[i].batch.get(), ResourceIDRange(handle));
      sample_submitted_[i] = true;
      visible_samples_++;
      if (geometry_dump_enabled_) {
        dump_sample_indices_.append(i);
      }
    }
    /* Do not retain original object pointers after synchronization. */
    meshes_.clear();
    rig_ = nullptr;
  }

  void pre_draw(Manager &manager, View &view) final
  {
    if (enabled_) {
      manager.generate_commands(ps_, view);
    }
  }

  void draw(Framebuffer &framebuffer, Manager &manager, View &view) final
  {
    if (!enabled_) {
      return;
    }
    GPU_framebuffer_bind(framebuffer);
    manager.submit_only(ps_, view);
    if (diagnostics_pending_ && bundle_ && visible_samples_ > 0 &&
        std::getenv("BLENDER_MESH_ONION_SKIN_DIAGNOSTICS"))
    {
      printf(
          "mesh_onion_skin_draw generation=%llu input_to_draw_ms=%.3f samples=%d "
          "visible_samples=%d\n",
          static_cast<unsigned long long>(bundle_->generation),
          (BLI_time_now_seconds() - bundle_->input_time) * 1000.0,
          int(samples().size()),
          visible_samples_);
      fflush(stdout);
    }
    /* Geometry validation is separate from latency measurements: hashing deliberately adds
     * work and is enabled only for disposable correctness runs. */
    if (diagnostics_pending_ && bundle_ && visible_samples_ > 0 &&
        std::getenv("BLENDER_MESH_ONION_SKIN_GEOMETRY_DIAGNOSTICS"))
    {
      printf("mesh_onion_skin_geometry generation=%llu geometry_hash=%016llx visible_samples=%d\n",
             static_cast<unsigned long long>(bundle_->generation),
             static_cast<unsigned long long>(geometry_fingerprint()),
             visible_samples_);
      printf("mesh_onion_skin_frames generation=%llu frames=",
             static_cast<unsigned long long>(bundle_->generation));
      bool first = true;
      for (const int i : samples().index_range()) {
        const ed::mesh_onion_skin::Sample &sample = samples()[i];
        if (batches_[i].batch && visible_meshes_.contains(sample.object_uid)) {
          printf("%s%.9g", first ? "" : ",", double(sample.frame));
          first = false;
        }
      }
      printf(" offsets=");
      first = true;
      for (const int i : samples().index_range()) {
        const ed::mesh_onion_skin::Sample &sample = samples()[i];
        if (batches_[i].batch && visible_meshes_.contains(sample.object_uid)) {
          printf("%s%.9g", first ? "" : ",", double(sample.temporal_offset));
          first = false;
        }
      }
      printf("\n");
      fflush(stdout);
    }
    diagnostics_pending_ = false;
    if (bundle_ && !samples().is_empty() &&
        std::getenv("BLENDER_MESH_ONION_SKIN_GEOMETRY_DIAGNOSTICS"))
    {
      /* Includes redraws of held bundles: validation can observe scrolling without adding
       * diagnostics or allocations to normal playback. Values match the submitted constants. */
      printf(
          "mesh_onion_skin_screen generation=%llu epoch=%llu playhead=%.9g native_frame=%.9g "
          "center=%.9g enabled=%d reverse=%d spacing=%.9g vbo_uploads=%llu batch_creates=%llu "
          "frames=",
          static_cast<unsigned long long>(bundle_->generation),
          static_cast<unsigned long long>(bundle_->epoch),
          double(playhead_frame_),
          double(native_frame_),
          double(bundle_->frame),
          int(screen_offset_enabled_),
          int(screen_offset_reversed_),
          double(screen_offset_spacing_),
          static_cast<unsigned long long>(vbo_uploads_),
          static_cast<unsigned long long>(batch_creates_));
      bool first = true;
      for (const int i : samples().index_range()) {
        printf("%s%.9g", first ? "" : ",", double(samples()[i].frame));
        first = false;
      }
      printf(" offsets=");
      first = true;
      for (const int i : samples().index_range()) {
        printf("%s%.9g", first ? "" : ",", double(displayed_temporal_offset(samples()[i])));
        first = false;
      }
      printf(" pixels=");
      first = true;
      for (const int i : samples().index_range()) {
        printf("%s%.9g", first ? "" : ",", double(screen_pixel_offset(samples()[i])));
        first = false;
      }
      printf(" alpha=");
      first = true;
      for (const int i : samples().index_range()) {
        printf("%s%.9g", first ? "" : ",", double(sample_opacities_[i]));
        first = false;
      }
      printf(" ranks=");
      first = true;
      for (const ed::mesh_onion_skin::Sample &sample : samples()) {
        printf("%s%.9g", first ? "" : ",", double(sample.rank - playhead_rank_));
        first = false;
      }
      printf(" payloads=");
      first = true;
      for (const ed::mesh_onion_skin::Sample &sample : samples()) {
        printf(
            "%s%llu",
            first ? "" : ",",
            static_cast<unsigned long long>(reinterpret_cast<uintptr_t>(sample.geometry.get())));
        first = false;
      }
      printf(" submitted=");
      first = true;
      for (const int i : samples().index_range()) {
        printf("%s%d", first ? "" : ",", int(screen_sample_submitted(i)));
        first = false;
      }
      printf("\n");
      fflush(stdout);
    }
    if (geometry_dump_enabled_ && bundle_ && visible_samples_ > 0) {
      dump_geometry();
    }
  }

 private:
  Span<ed::mesh_onion_skin::Sample> samples() const
  {
    return screen_offset_enabled_ ? bundle_->timeline_samples.as_span() :
                                    bundle_->samples.as_span();
  }

  void sync_batches()
  {
    const bool reuse_relative_slots = !screen_offset_enabled_ && !batches_screen_offset_;
    batches_screen_offset_ = screen_offset_enabled_;
    if (batch_epoch_ != bundle_->epoch) {
      batches_.clear();
      batch_epoch_ = bundle_->epoch;
    }
    Vector<BatchSlot> previous = std::move(batches_);
    batches_.reserve(samples().size());
    for (const int sample_index : samples().index_range()) {
      const ed::mesh_onion_skin::Sample &sample = samples()[sample_index];
      BatchSlot slot;
      if (reuse_relative_slots && sample_index < previous.size()) {
        /* Ordinary centered ghosts update their existing slots as before. Only the scrolling
         * timeline needs GPU allocations to follow persistent authored-frame identities. */
        slot = std::move(previous[sample_index]);
      }
      else {
        for (BatchSlot &candidate : previous) {
          if (candidate.object_uid == sample.object_uid && candidate.frame == sample.frame &&
              candidate.geometry)
          {
            slot = std::move(candidate);
            break;
          }
        }
      }
      slot.object_uid = sample.object_uid;
      slot.frame = sample.frame;
      if (!sample.geometry || sample.geometry->positions.is_empty() ||
          sample.geometry->triangles.is_empty())
      {
        slot.batch.reset();
      }
      else if (!slot.batch ||
               slot.batch->verts[0]->vertex_len != sample.geometry->positions.size())
      {
        slot.batch = create_batch(sample);
        batch_creates_++;
        vbo_uploads_++;
      }
      else if (slot.geometry != sample.geometry) {
        /* Stable timeline identities retain GPU buffers. Cached frames with the same immutable
         * payload need no vertex upload, even when their slot in the moving window changes. */
        gpu::VertBuf *vbo = slot.batch->verts[0];
        vbo->data<float3>().copy_from(sample.geometry->positions);
        GPU_vertbuf_tag_dirty(vbo);
        vbo_uploads_++;
        if (!slot.geometry ||
            slot.geometry->triangles.as_span() != sample.geometry->triangles.as_span())
        {
          GPU_batch_elembuf_set(slot.batch.get(), create_index_buffer(sample), true);
        }
      }
      slot.geometry = sample.geometry;
      batches_.append(std::move(slot));
    }
    /* Only active identities and their guard frames stay resident. No original object or
     * evaluated mesh pointer is retained by this cache. */
  }

  static float smoothstep(const float t)
  {
    return t * t * (3.0f - 2.0f * t);
  }

  float timeline_opacity(const float rank) const
  {
    if ((rank < 0.0f && before_count_ == 0.0f) || (rank > 0.0f && after_count_ == 0.0f)) {
      return 0.0f;
    }
    if (rank < -before_count_) {
      return smoothstep(std::clamp(rank + before_count_ + 1.0f, 0.0f, 1.0f));
    }
    if (rank > after_count_) {
      return smoothstep(std::clamp(after_count_ + 1.0f - rank, 0.0f, 1.0f));
    }
    return 1.0f;
  }

  float interpolated_playhead_rank(const float delta, const int step) const
  {
    const ed::mesh_onion_skin::Sample *left = nullptr;
    const ed::mesh_onion_skin::Sample *right = nullptr;
    for (const ed::mesh_onion_skin::Sample &sample : samples()) {
      if (sample.temporal_offset <= delta &&
          (!left || sample.temporal_offset > left->temporal_offset))
      {
        left = &sample;
      }
      if (sample.temporal_offset >= delta &&
          (!right || sample.temporal_offset < right->temporal_offset))
      {
        right = &sample;
      }
    }
    if (!left && !right) {
      return 0.0f;
    }
    if (!left || !right) {
      /* A held bundle may outlive its guard interval. Extrapolate the boundary cell until
       * every old frame has smoothly faded out, instead of wrapping it back into view. */
      const ed::mesh_onion_skin::Sample *edge = left ? left : right;
      const ed::mesh_onion_skin::Sample *neighbor = nullptr;
      for (const ed::mesh_onion_skin::Sample &sample : samples()) {
        const float offset = sample.temporal_offset;
        if ((left && offset < edge->temporal_offset &&
             (!neighbor || offset > neighbor->temporal_offset)) ||
            (right && offset > edge->temporal_offset &&
             (!neighbor || offset < neighbor->temporal_offset)))
        {
          neighbor = &sample;
        }
      }
      if (!neighbor) {
        return edge->rank + (delta - edge->temporal_offset) / std::max(1, step);
      }
      left = left ? neighbor : edge;
      right = right ? neighbor : edge;
    }
    const float span = right->temporal_offset - left->temporal_offset;
    if (span == 0.0f) {
      return left->rank;
    }
    return left->rank + (delta - left->temporal_offset) / span * (right->rank - left->rank);
  }

  float displayed_temporal_offset(const ed::mesh_onion_skin::Sample &sample) const
  {
    if (!screen_offset_enabled_) {
      return sample.temporal_offset;
    }
    return sample.temporal_offset - bundle_playhead_delta_;
  }

  float screen_pixel_offset(const ed::mesh_onion_skin::Sample &sample) const
  {
    if (!screen_offset_enabled_) {
      return 0.0f;
    }
    return displayed_temporal_offset(sample) * screen_offset_spacing_ *
           (screen_offset_reversed_ ? -1.0f : 1.0f);
  }

  bool screen_sample_submitted(const int index) const
  {
    return sample_submitted_[index];
  }

  void dump_geometry()
  {
    /* BLI_getenv observes Python's environment updates on Windows too, even across CRTs. */
    const char *phase_value = BLI_getenv("BLENDER_MESH_ONION_SKIN_GEOMETRY_DUMP_PHASE");
    if (!phase_value) {
      return;
    }
    const std::string phase(phase_value);
    constexpr const char *phases[] = {
        "baseline", "hold_cancel", "after_cancel", "hold_confirm", "after_confirm", "after_undo"};
    int phase_index = -1;
    for (int i = 0; i < 6; i++) {
      if (phase == phases[i]) {
        phase_index = i;
        break;
      }
    }
    if (phase_index < 0 || (phase == dumped_phase_ && dumped_bundle_.lock() == bundle_)) {
      return;
    }
    const char *directory = BLI_getenv("BLENDER_MESH_ONION_SKIN_GEOMETRY_DUMP_DIRECTORY");
    if (!directory || bundle_->bytes > size_t(256) * 1024 * 1024) {
      return;
    }
    size_t dump_bytes = 32;
    for (const int index : dump_sample_indices_) {
      const ed::mesh_onion_skin::Sample &sample = samples()[index];
      dump_bytes += 36 + dump_mesh_names_.lookup(sample.object_uid).size() +
                    size_t(sample.geometry->positions.size() + sample.geometry->triangles.size()) *
                        12;
    }
    if (dump_bytes > size_t(256) * 1024 * 1024) {
      return;
    }
    char path[FILE_MAX], temporary_path[FILE_MAX];
    const std::string filename = phase + ".bin";
    const std::string temporary_filename = filename + ".tmp";
    BLI_path_join(path, sizeof(path), directory, filename.c_str());
    BLI_path_join(temporary_path, sizeof(temporary_path), directory, temporary_filename.c_str());
    FILE *file = BLI_fopen(temporary_path, "wb");
    if (!file) {
      return;
    }
    bool ok = true;
    auto bytes = [&](const void *data, const size_t size) {
      if (ok && std::fwrite(data, 1, size, file) != size) {
        ok = false;
      }
    };
    auto integer = [&](uint64_t value, const int size) {
      unsigned char encoded[8];
      for (int i = 0; i < size; i++, value >>= 8) {
        encoded[i] = value & 0xff;
      }
      bytes(encoded, size);
    };
    auto scalar = [&](const float value) { integer(std::bit_cast<uint32_t>(value), 4); };
    /* Versioned little-endian format, independent of native struct padding. Six filenames
     * bound storage; each phase is replaced only when a newer bundle is actually submitted. */
    bytes("ONIONSK1", 8);
    integer(phase_index, 4);
    integer(dump_sample_indices_.size(), 4);
    integer(bundle_->generation, 8);
    integer(bundle_->epoch, 8);
    for (const int index : dump_sample_indices_) {
      const ed::mesh_onion_skin::Sample &sample = samples()[index];
      const std::string &name = dump_mesh_names_.lookup(sample.object_uid);
      integer(sample.object_uid.hash(), 8);
      scalar(sample.frame);
      scalar(sample.temporal_offset);
      integer(name.size(), 4);
      integer(sample.geometry->positions.size(), 8);
      integer(sample.geometry->triangles.size(), 8);
      bytes(name.data(), name.size());
      for (const float3 &position : sample.geometry->positions) {
        const float3 world = math::transform_point(sample.geometry->object_to_world, position);
        for (int axis = 0; axis < 3; axis++) {
          scalar(world[axis]);
        }
      }
      for (const int3 &triangle : sample.geometry->triangles) {
        for (int corner = 0; corner < 3; corner++) {
          integer(uint32_t(triangle[corner]), 4);
        }
      }
    }
    ok = std::fclose(file) == 0 && ok;
    if (ok && BLI_rename_overwrite(temporary_path, path) == 0) {
      dumped_phase_ = phase;
      dumped_bundle_ = bundle_;
      printf("mesh_onion_skin_dump phase=%s generation=%llu samples=%d\n",
             phase.c_str(),
             static_cast<unsigned long long>(bundle_->generation),
             visible_samples_);
      fflush(stdout);
    }
    else {
      BLI_delete(temporary_path, false, false);
    }
  }

  uint64_t geometry_fingerprint() const
  {
    constexpr uint64_t seed = 14695981039346656037ULL;
    const auto append = [](uint64_t &hash, const int64_t value) {
      uint64_t bits = uint64_t(value);
      for (int byte = 0; byte < 8; byte++, bits >>= 8) {
        hash = (hash ^ (bits & 0xff)) * 1099511628211ULL;
      }
    };
    Vector<uint64_t> sample_hashes;
    for (const int i : samples().index_range()) {
      const ed::mesh_onion_skin::Sample &sample = samples()[i];
      if (!batches_[i].batch || !visible_meshes_.contains(sample.object_uid)) {
        continue;
      }
      uint64_t hash = seed;
      append(hash, std::llround(double(sample.frame) * 100000.0));
      append(hash, std::llround(double(sample.temporal_offset) * 100000.0));
      append(hash, sample.geometry->positions.size());
      for (const float3 &position : sample.geometry->positions) {
        const float3 world = math::transform_point(sample.geometry->object_to_world, position);
        for (int axis = 0; axis < 3; axis++) {
          append(hash, std::llround(double(world[axis]) * 100000.0));
        }
      }
      sample_hashes.append(hash);
    }
    /* IDs can be remapped by undo. Neither their identity nor bundle ordering is geometry. */
    std::sort(sample_hashes.begin(), sample_hashes.end());
    uint64_t result = seed;
    for (const uint64_t hash : sample_hashes) {
      append(result, int64_t(hash));
    }
    return result;
  }

  static BatchPtr create_batch(const ed::mesh_onion_skin::Sample &sample)
  {
    if (sample.geometry->positions.is_empty() || sample.geometry->triangles.is_empty()) {
      return nullptr;
    }
    GPUVertFormat format = {};
    GPU_vertformat_attr_add(&format, "pos", blender::gpu::VertAttrType::SFLOAT_32_32_32);
    gpu::VertBuf *vbo = GPU_vertbuf_create_with_format_ex(format, GPU_USAGE_DYNAMIC);
    GPU_vertbuf_data_alloc(*vbo, sample.geometry->positions.size());
    vbo->data<float3>().copy_from(sample.geometry->positions);
    return BatchPtr(GPU_batch_create_ex(GPU_PRIM_TRIS,
                                        vbo,
                                        create_index_buffer(sample),
                                        GPU_BATCH_OWNS_VBO | GPU_BATCH_OWNS_INDEX));
  }

  static gpu::IndexBuf *create_index_buffer(const ed::mesh_onion_skin::Sample &sample)
  {
    /* Tessellation is supplied per sample; stable polygon connectivity alone is insufficient. */
    return GPU_indexbuf_build_from_memory(
        GPU_PRIM_TRIS,
        reinterpret_cast<const uint32_t *>(sample.geometry->triangles.data()),
        /* This API takes primitive count and multiplies by three for triangles. */
        sample.geometry->triangles.size(),
        0,
        sample.geometry->positions.size() - 1,
        false);
  }
};

}  // namespace blender::draw::overlay
