/* SPDX-FileCopyrightText: 2026 Blender Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

#include "infos/overlay_edit_mode_infos.hh"

VERTEX_SHADER_CREATE_INFO(overlay_mesh_onion_skin_screen)

#include "draw_model_lib.glsl"
#include "draw_view_clipping_lib.glsl"
#include "draw_view_lib.glsl"

void main()
{
  float3 world_pos = drw_point_object_to_world(pos);
  /* Scene-distance spacing follows the viewer's horizontal axis. Project the displaced point
   * normally so camera distance, zoom, and perspective determine its apparent separation. */
  world_pos += normalize(drw_view().viewinv[0].xyz) * world_offset;
  gl_Position = drw_point_world_to_homogenous(world_pos);
  /* Projection-only translation keeps spacing constant in orthographic and perspective views.
   * The inactive spacing mode contributes zero; scene-distance clipping follows its displacement.
   */
  gl_Position.x += 2.0f * screen_offset * uniform_buf.size_viewport_inv.x * gl_Position.w;
  view_clipping_distances(world_pos);
}
