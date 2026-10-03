/* SPDX-FileCopyrightText: 2026 Blender Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

/** \file
 * \ingroup overlay
 */

#pragma once

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstdlib>

#include "BLI_math_matrix.hh"
#include "BLI_time.hh"

#include "BKE_modifier.hh"

#include "DEG_depsgraph_query.hh"

#include "ED_mesh_onion_skin.hh"

#include "GPU_batch.hh"
#include "GPU_index_buffer.hh"
#include "GPU_vertex_buffer.hh"

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

  PassMain ps_ = {"MeshOnionSkin"};
  Vector<Object *> meshes_;
  Vector<SessionUID> visible_meshes_;
  Object *rig_ = nullptr;
  std::shared_ptr<const ed::mesh_onion_skin::Bundle> bundle_;
  Vector<BatchPtr> batches_;
  bool diagnostics_pending_ = false;
  int visible_samples_ = 0;

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
      return;
    }

    rig_ = const_cast<Object *>(DEG_get_original(state.object_active));
    DRWState draw_state = DRW_STATE_WRITE_COLOR | DRW_STATE_BLEND_ALPHA;
    if (!(state.overlay.flag & V3D_OVERLAY_MESH_ONION_SKIN_IN_FRONT)) {
      draw_state |= DRW_STATE_DEPTH_LESS_EQUAL;
    }
    /* Ghosts never write depth and never participate in selection or depth picking. */
    ps_.state_set(draw_state, state.clipping_plane_count);
    ps_.shader_set(res.shaders->uniform_color.get());
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
    }
  }

  void end_sync(Resources & /*res*/, const State &state) final
  {
    if (!enabled_) {
      return;
    }
    const DRWContext *ctx = DRW_context_get();
    const ed::mesh_onion_skin::Settings settings = {state.overlay.mesh_onion_skin_before,
                                                    state.overlay.mesh_onion_skin_after,
                                                    state.overlay.mesh_onion_skin_step};
    /* Only queues a request. Workspace preparation and evaluation run outside drawing. */
    ed::mesh_onion_skin::request(
        *const_cast<bContext *>(ctx->evil_C), *state.depsgraph, *rig_, meshes_, settings);
    std::shared_ptr<const ed::mesh_onion_skin::Bundle> bundle = ed::mesh_onion_skin::get(
        *DEG_get_input_scene(state.depsgraph), *rig_);
    if (bundle != bundle_) {
      const std::shared_ptr<const ed::mesh_onion_skin::Bundle> previous = std::move(bundle_);
      bundle_ = std::move(bundle);
      diagnostics_pending_ = bool(bundle_);
      if (!bundle_) {
        batches_.clear();
      }
      else {
        batches_.resize(bundle_->samples.size());
        for (const int i : bundle_->samples.index_range()) {
          const ed::mesh_onion_skin::Sample &sample = bundle_->samples[i];
          if (sample.positions.is_empty() || sample.triangles.is_empty()) {
            batches_[i].reset();
          }
          else if (!batches_[i] || batches_[i]->verts[0]->vertex_len != sample.positions.size()) {
            batches_[i] = create_batch(sample);
          }
          else {
            /* Keep dynamic VBO allocations across pose revisions. */
            gpu::VertBuf *vbo = batches_[i]->verts[0];
            vbo->data<float3>().copy_from(sample.positions);
            GPU_vertbuf_tag_dirty(vbo);
            if (!previous || i >= previous->samples.size() ||
                previous->samples[i].triangles.as_span() != sample.triangles.as_span())
            {
              GPU_batch_elembuf_set(batches_[i].get(), create_index_buffer(sample), true);
            }
          }
        }
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

    Manager &manager = *DRW_manager_get();
    for (const int i : bundle_->samples.index_range()) {
      const ed::mesh_onion_skin::Sample &sample = bundle_->samples[i];
      if (!batches_[i] || !visible_meshes_.contains(sample.object_uid) ||
          state.overlay.mesh_onion_skin_opacity <= 0.0f)
      {
        continue;
      }
      const bool before = sample.frame < state.cfra;
      const float4 color = before ?
                               float4(0.16f, 0.55f, 1.0f, state.overlay.mesh_onion_skin_opacity) :
                               float4(1.0f, 0.25f, 0.14f, state.overlay.mesh_onion_skin_opacity);
      PassMain::Sub &sub = ps_.sub(before ? "Before" : "After");
      sub.push_constant("ucolor", color);
      const ResourceID handle = manager.resource_handle(sample.object_to_world);
      sub.draw(batches_[i].get(), ResourceIDRange(handle));
      visible_samples_++;
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
          int(bundle_->samples.size()),
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
      fflush(stdout);
    }
    diagnostics_pending_ = false;
  }

 private:
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
    for (const int i : bundle_->samples.index_range()) {
      const ed::mesh_onion_skin::Sample &sample = bundle_->samples[i];
      if (!batches_[i] || !visible_meshes_.contains(sample.object_uid)) {
        continue;
      }
      uint64_t hash = seed;
      append(hash, std::llround(double(sample.frame) * 100000.0));
      append(hash, sample.positions.size());
      for (const float3 &position : sample.positions) {
        const float3 world = math::transform_point(sample.object_to_world, position);
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
    if (sample.positions.is_empty() || sample.triangles.is_empty()) {
      return nullptr;
    }
    GPUVertFormat format = {};
    GPU_vertformat_attr_add(&format, "pos", blender::gpu::VertAttrType::SFLOAT_32_32_32);
    gpu::VertBuf *vbo = GPU_vertbuf_create_with_format_ex(format, GPU_USAGE_DYNAMIC);
    GPU_vertbuf_data_alloc(*vbo, sample.positions.size());
    vbo->data<float3>().copy_from(sample.positions);
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
        reinterpret_cast<const uint32_t *>(sample.triangles.data()),
        /* This API takes primitive count and multiplies by three for triangles. */
        sample.triangles.size(),
        0,
        sample.positions.size() - 1,
        false);
  }
};

}  // namespace blender::draw::overlay
