/* SPDX-FileCopyrightText: 2026 Blender Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

/** \file
 * \ingroup DNA
 *
 * Storage of the layered "Texture Paint" shader node (#SH_NODE_TEX_PAINT).
 *
 * The node's `bNode::id` points to a single regular #Image datablock that receives the composited
 * result of the layer stack, so render engines and editors treat the node like an Image Texture
 * node. Individual layers never create #Image datablocks: their pixels live in private runtime
 * #ImBuf buffers and are serialized as packed PNG/EXR data.
 */

#pragma once

#include "DNA_defs.h"
#include "DNA_listBase.h"
#include "DNA_node_types.h" /* For #NodeTexImage. */

namespace blender {

struct Image;
struct ImBuf;
struct PackedFile;

/** #TexPaintLayer::type */
enum eTexPaintLayerType {
  /** Layer with its own painted RGBA pixels. */
  TEXPAINT_LAYER_PAINT = 0,
  /** Layer filled with a constant color, shaped by the layer mask. */
  TEXPAINT_LAYER_FILL = 1,
  /** Layer adjusting the composite of the layers below it. */
  TEXPAINT_LAYER_ADJUSTMENT = 2,
};

/** #TexPaintLayer::flag */
enum eTexPaintLayerFlag {
  TEXPAINT_LAYER_VISIBLE = (1 << 0),
  TEXPAINT_LAYER_LOCKED = (1 << 1),
  TEXPAINT_LAYER_ALPHA_LOCK = (1 << 2),
  /** Clip the layer to the alpha of the nearest non-clipping layer below it. */
  TEXPAINT_LAYER_CLIPPING = (1 << 3),
  TEXPAINT_LAYER_USE_MASK = (1 << 4),
  TEXPAINT_LAYER_MASK_INVERT = (1 << 5),
  /** Show the interactive 3D projection widget in the 3D Viewport when this layer is active. */
  TEXPAINT_LAYER_SHOW_WIDGET = (1 << 6),
  /** Temporarily preview this layer's grayscale mask directly on the composited master image. */
  TEXPAINT_LAYER_PREVIEW_MASK = (1 << 7),
  /** Set once `channel_flags` has been explicitly initialized on this layer. */
  TEXPAINT_LAYER_CHANNELS_INIT = (1 << 8),
};

/** #TexPaintLayer::channel_flags (Substance Painter per-layer PBR channel toggles). */
enum eTexPaintChannelFlag {
  TEXPAINT_CHAN_COLOR = (1 << 0),
  TEXPAINT_CHAN_ROUGHNESS = (1 << 1),
  TEXPAINT_CHAN_METALLIC = (1 << 2),
  TEXPAINT_CHAN_HEIGHT = (1 << 3),
  TEXPAINT_CHAN_EMISSION = (1 << 4),
  TEXPAINT_CHAN_SPECULAR = (1 << 5),
  TEXPAINT_CHAN_NORMAL = (1 << 6),
  TEXPAINT_CHAN_ALPHA = (1 << 7),
};

/** #NodeTexPaint::paint_target */
enum eTexPaintTarget {
  TEXPAINT_TARGET_COLOR = 0,
  TEXPAINT_TARGET_MASK = 1,
};

/** #TexPaintLayer::mask_gen_type */
enum eTexPaintMaskGenType {
  TEXPAINT_MASK_GEN_NONE = 0,
  TEXPAINT_MASK_GEN_AO = 1,
  TEXPAINT_MASK_GEN_CURVATURE = 2,
  TEXPAINT_MASK_GEN_CAVITY = 3,
  TEXPAINT_MASK_GEN_EDGE_WEAR = 4,
  TEXPAINT_MASK_GEN_NORMAL_UP = 5,
  TEXPAINT_MASK_GEN_POSITION_Z = 6,
};

/** #TexPaintLayer::mask_gen_blend */
enum eTexPaintMaskGenBlend {
  TEXPAINT_MASK_BLEND_REPLACE = 0,
  TEXPAINT_MASK_BLEND_ADD = 1,
  TEXPAINT_MASK_BLEND_MULTIPLY = 2,
  TEXPAINT_MASK_BLEND_SUBTRACT = 3,
};

enum eTexPaintColorFilterType {
  TEXPAINT_COLOR_FILTER_HSV = 0,
  TEXPAINT_COLOR_FILTER_BRIGHT_CONTRAST = 1,
  TEXPAINT_COLOR_FILTER_LEVELS = 2,
  TEXPAINT_COLOR_FILTER_INVERT = 3,
  TEXPAINT_COLOR_FILTER_GRAYSCALE = 4,
  TEXPAINT_COLOR_FILTER_BLUR = 5,
};

/** Ordered, non-destructive color operations evaluated before a layer is blended. */
struct TexPaintColorFilter {
  DNA_DEFINE_CXX_METHODS(TexPaintColorFilter)

  TexPaintColorFilter *next = nullptr, *prev = nullptr;
  char name[64] = "Filter";
  int type = TEXPAINT_COLOR_FILTER_HSV;
  int enabled = 1;
  float strength = 1.0f;
  float hsv[3] = {0.0f, 0.0f, 0.0f};
  float bright_contrast[2] = {0.0f, 0.0f};
  float input_black = 0.0f;
  float input_white = 1.0f;
  float gamma = 1.0f;
  float output_black = 0.0f;
  float output_white = 1.0f;
  /** Spatial blur support in canvas pixels; occupies previously unused padding. */
  float blur_radius = 4.0f;
};

enum eTexPaintMaskEffectType {
  TEXPAINT_MASK_EFFECT_SOURCE = 0,
  TEXPAINT_MASK_EFFECT_PAINT = 1,
  TEXPAINT_MASK_EFFECT_LEVELS = 2,
  TEXPAINT_MASK_EFFECT_FILTER = 3,
};

enum eTexPaintMaskEffectBlend {
  TEXPAINT_MASK_EFFECT_REPLACE = 0,
  TEXPAINT_MASK_EFFECT_ADD = 1,
  TEXPAINT_MASK_EFFECT_SUBTRACT = 2,
  TEXPAINT_MASK_EFFECT_MULTIPLY = 3,
  TEXPAINT_MASK_EFFECT_SCREEN = 4,
  TEXPAINT_MASK_EFFECT_OVERLAY = 5,
  TEXPAINT_MASK_EFFECT_MAX = 6,
  TEXPAINT_MASK_EFFECT_MIN = 7,
};

enum eTexPaintMaskFilterType {
  TEXPAINT_MASK_FILTER_BLUR = 0,
  TEXPAINT_MASK_FILTER_BLUR_SLOPE = 1,
  TEXPAINT_MASK_FILTER_SHARPEN = 2,
  TEXPAINT_MASK_FILTER_DILATE_ERODE = 3,
  TEXPAINT_MASK_FILTER_BEVEL = 4,
  TEXPAINT_MASK_FILTER_THRESHOLD = 5,
  TEXPAINT_MASK_FILTER_POSTERIZE = 6,
  TEXPAINT_MASK_FILTER_HIGHPASS = 7,
};

/** Prepared native mask operation. Paint owns its independent RGBA coverage buffer. */
struct TexPaintMaskEffect {
  DNA_DEFINE_CXX_METHODS(TexPaintMaskEffect)

  TexPaintMaskEffect *next = nullptr, *prev = nullptr;
  int uid = 0;
  int type = TEXPAINT_MASK_EFFECT_SOURCE;
  int enabled = 1;
  int blend_mode = TEXPAINT_MASK_EFFECT_REPLACE;
  float opacity = 1.0f;
  int invert = 0;
  /** Prepared source/levels tone curve. Initialized to identity by effect creation. */
  float curve[256] = {};
  int filter_type = TEXPAINT_MASK_FILTER_BLUR;
  float filter_radius = 6.0f;
  float filter_strength = 1.0f;
  float filter_scale = 8.0f;
  PackedFile *pixels_packed = nullptr;
  ImBuf *pixels = nullptr;
  int runtime_dirty = 0;
  int revision = 1;
};

struct TexPaintLayer {
  DNA_DEFINE_CXX_METHODS(TexPaintLayer)

  TexPaintLayer *next = nullptr, *prev = nullptr;
  char name[64] = "";
  /** Persistent identifier, unique within the owning node. Used by undo to find the layer. */
  int uid = 0;
  /** #eTexPaintLayerType. */
  int type = TEXPAINT_LAYER_PAINT;
  /** Subset of #IMB_BlendMode values. */
  int blend_mode = 0;
  /** #eTexPaintLayerFlag. */
  int flag = TEXPAINT_LAYER_VISIBLE;
  float opacity = 1.0f;
  char _pad0[4] = {};

  /* Fill and adjustment parameters. */
  float fill_color[4] = {1.0f, 1.0f, 1.0f, 1.0f};
  /** Hue shift, saturation and value offsets. */
  float adjust_hsv[3] = {0.0f, 0.0f, 0.0f};
  /** Brightness and contrast offsets. */
  float adjust_bright_contrast[2] = {0.0f, 0.0f};
  char _pad1[12] = {};

  /* Linked source image & 3D projection widget parameters (Decal / Triplanar / Repeat). */
  char proj_image_name[64] = "";
  /** #eTexPaintProjectionMode (0 = None / Hand-painted). */
  int proj_mode = 0;
  float proj_blend_falloff = 0.5f;
  /** 3D projection widget center in Object space. */
  float proj_location[3] = {0.0f, 0.0f, 0.0f};
  /** 3D projection widget Euler XYZ rotation in Object space (radians). */
  float proj_rotation[3] = {0.0f, 0.0f, 0.0f};
  /** 3D projection widget half-extents / scale in Object space. */
  float proj_scale[3] = {0.5f, 0.5f, 0.5f};
  char _pad_proj[4] = {};

  /* Smart Mask Generator parameters (Substance-style AO, Curvature, Cavity, Edge Wear, etc.). */
  /** #eTexPaintMaskGenType (0 = Hand-painted / None). */
  int mask_gen_type = 0;
  /** #eTexPaintMaskGenBlend. */
  int mask_gen_blend = 0;
  /** Threshold / Balance of the generated mask (0..1). */
  float mask_gen_balance = 0.5f;
  /** Contrast / sharpness of the generated mask (0..1). */
  float mask_gen_contrast = 0.5f;
  /** Neighborhood spread / radius (0..1). */
  float mask_gen_radius = 0.35f;
  /** Procedural 3D grunge/noise breakup amount (0..1). */
  float mask_gen_noise = 0.0f;
  /** Procedural 3D grunge/noise scale. */
  float mask_gen_noise_scale = 8.0f;
  /** Invert the generated mask values before blending. */
  int mask_gen_invert = 0;

  /** Persistent compressed pixels, written to and read from `.blend` files. */
  PackedFile *pf = nullptr;
  PackedFile *mask_pf = nullptr;

  /** Runtime pixel buffers. Cleared on file read and decoded lazily from the packed data. */
  ImBuf *ibuf = nullptr;
  ImBuf *mask_ibuf = nullptr;
  /** Runtime: the buffers changed since the packed data was last encoded. */
  int runtime_dirty = 0;
  char _pad2[4] = {};

  /* Substance-Painter-style Multi-Channel PBR parameters & optional textured PBR buffer. */
  /** Bitmask of #eTexPaintChannelFlag enabled on this layer. */
  int channel_flags = TEXPAINT_CHAN_COLOR;
  float fill_roughness = 0.45f;
  float fill_metallic = 0.0f;
  /** Signed height offset in [-1.0, 1.0] (0.0 = flat neutral 0.5). */
  float fill_height = 0.0f;
  float fill_specular = 0.5f;
  float fill_emission[3] = {1.0f, 0.45f, 0.1f};
  /** Optional packed PBR pixel buffer (R=Roughness, G=Metallic, B=Height, A=Coverage). */
  PackedFile *pbr_pf = nullptr;
  ImBuf *pbr_ibuf = nullptr;

  /** Applied first to last; never written into the source color or mask buffers. */
  ListBaseT<TexPaintColorFilter> color_filters = {nullptr, nullptr};
  int active_color_filter_index = 0;
  char _pad_filters[4] = {};

  /** Owned native mask stack, prepared from the mask authoring UI. */
  ListBaseT<TexPaintMaskEffect> mask_effects = {nullptr, nullptr};
  int active_mask_effect_uid = 0;
  int mask_base_value = 255;
  int mask_effects_active = 0;
  char _pad_mask_effects[4] = {};
};

struct NodeTexPaint {
  DNA_DEFINE_CXX_METHODS(NodeTexPaint)

  /**
   * Sampling settings of the composited image. Kept as the first member so code handling the
   * Image Texture node storage (texture mapping, GPU sampling, paint slots) can read it directly.
   */
  NodeTexImage image;

  int width = 0;
  int height = 0;
  /** 0: 8-bit RGBA byte buffers, 1: 32-bit float RGBA buffers. */
  int is_float = 0;
  int active_layer_index = 0;
  /** #eTexPaintTarget. */
  int paint_target = TEXPAINT_TARGET_COLOR;
  int next_layer_uid = 1;

  /** #TexPaintLayer, from bottom to top. */
  ListBaseT<TexPaintLayer> layers = {nullptr, nullptr};

  /** Runtime composite cache, `blender::bke::tex_paint::TexPaintRuntime`. */
  void *runtime = nullptr;

  /** Derived master image for packed PBR channels (R=Roughness, G=Metallic, B=Height, A=Specular). */
  Image *pbr_image = nullptr;
  /** Derived master image for Emission RGB. */
  Image *emission_image = nullptr;
  ImageUser pbr_iuser;
  ImageUser emission_iuser;
  float base_roughness = 0.5f;
  float base_metallic = 0.0f;
  float base_specular = 0.5f;
  float bump_strength = 1.0f;

  /** Canvas-space selection coverage, shared by 2D editing and material paint layers.
   * An active, all-zero selection is distinct from an unrestricted canvas. Stored in undo and
   * blend files; evaluated node copies do not need the selection. */
  float *selection = nullptr;
  int selection_size = 0;
  int selection_active = 0;

  /** Immutable source of consecutive pixel transforms. The layer buffer contains the current
   * composite; these owned arrays keep destination pixels out of the lifted selection. */
  float *selection_transform_source = nullptr;
  float *selection_transform_mask = nullptr;
  int selection_transform_size = 0;
  int selection_transform_layer_uid = 0;
  int selection_transform_target = TEXPAINT_TARGET_COLOR;
  char _pad_selection_transform[4] = {};
  float selection_transform[3][3] = {{1, 0, 0}, {0, 1, 0}, {0, 0, 1}};
  char _pad_selection_matrix[4] = {};
  uint64_t selection_transform_hash = 0;
};

}  // namespace blender
