/* SPDX-FileCopyrightText: 2026 Blender Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

#include "anim_mesh_onion_skin_frames.hh"

#include <algorithm>
#include <cmath>
#include <optional>

#include "ANIM_action.hh"

#include "BKE_anim_data.hh"
#include "BKE_nla.hh"

#include "BLI_listbase.hh"
#include "BLI_math_base.hh"

#include "DNA_anim_types.h"
#include "DNA_object_types.h"

namespace blender::ed::mesh_onion_skin {

/* Bound main-thread authored-key expansion independently of the geometry cache budget. */
static constexpr int frame_limit = 1000000;
static constexpr int nesting_limit = 64;
static constexpr int samples_per_side_limit = 4;

static void sort_unique(Vector<float> &frames)
{
  std::sort(frames.begin(), frames.end());
  frames.resize(std::unique(frames.begin(), frames.end()) - frames.begin());
}

static bool append_frame(KeyedFrames &result, const float frame)
{
  if (!std::isfinite(frame)) {
    return true;
  }
  if (result.frames.size() >= frame_limit) {
    result.unsupported_reason = "Keyed onion skin exceeds its authored-key expansion limit";
    return false;
  }
  result.frames.append(frame);
  return true;
}

static KeyedFrames action_frames(const bAction *dna_action, const int slot_handle)
{
  KeyedFrames result;
  if (!dna_action) {
    return result;
  }
  const animrig::Action &action = dna_action->wrap();
  for (const animrig::Layer *layer : action.layers()) {
    if (layer->influence <= 0.0f) {
      continue;
    }
    for (const animrig::Strip *strip : layer->strips()) {
      if (strip->type() != animrig::Strip::Type::Keyframe) {
        continue;
      }
      const animrig::Channelbag *bag =
          strip->data<animrig::StripKeyframeData>(action).channelbag_for_slot(slot_handle);
      if (!bag) {
        continue;
      }
      for (const FCurve *curve : bag->fcurves()) {
        /* Baked samples and modifier-generated repetitions are not authored keyframes. */
        if (!curve->bezt || curve->flag & FCURVE_MUTED) {
          continue;
        }
        for (int key = 0; key < curve->totvert; key++) {
          const float frame = curve->bezt[key].vec[1][0] + strip->frame_offset;
          if (strip->contains_frame(frame) && !append_frame(result, frame)) {
            return result;
          }
        }
      }
    }
  }
  sort_unique(result.frames);
  return result;
}

/* The public native mapper may normalize zero scale/repeat and cache tweak pointers. Keep every
 * writable byte private, including the strip, so sampling never changes authored source data. */
static float strip_map(const NlaStrip &source, const float frame, const eNlaTime_ConvertModes mode)
{
  NlaStrip strip = source;
  NlaTrack track{};
  AnimData mapping{};
  mapping.flag = ADT_NLA_EDIT_ON;
  mapping.act_track = &track;
  mapping.actstrip = &strip;
  return BKE_nla_tweakedit_remap(&mapping, frame, mode);
}

static bool merge_frames(KeyedFrames &result, const KeyedFrames &other)
{
  if (!other.unsupported_reason.empty()) {
    result.unsupported_reason = other.unsupported_reason;
    return false;
  }
  for (const float frame : other.frames) {
    if (!append_frame(result, frame)) {
      return false;
    }
  }
  return true;
}

static KeyedFrames strip_frames(const NlaStrip &strip, const int depth)
{
  KeyedFrames result;
  if (strip.flag & NLASTRIP_FLAG_MUTED || strip.type == NLASTRIP_TYPE_SOUND ||
      strip.type == NLASTRIP_TYPE_TRANSITION)
  {
    /* Transitions blend existing clip samples; they do not introduce authored key times. */
    return result;
  }
  if (depth > nesting_limit) {
    result.unsupported_reason = "Keyed onion skin NLA nesting exceeds its supported limit";
    return result;
  }
  if ((strip.flag & NLASTRIP_FLAG_USR_TIME) || !strip.modifiers.is_empty()) {
    result.unsupported_reason =
        "Keyed onion skin cannot invert animated NLA strip time or strip modifiers";
    return result;
  }
  if (!std::isfinite(strip.start) || !std::isfinite(strip.end) || strip.end < strip.start) {
    result.unsupported_reason = "Keyed onion skin requires finite NLA strip bounds";
    return result;
  }
  if (strip.type == NLASTRIP_TYPE_META) {
    if (strip.flag & NLASTRIP_FLAG_NO_TIME_MAP) {
      result.unsupported_reason = "Keyed onion skin cannot invert an unmapped NLA meta strip";
      return result;
    }
    if (strip.end == strip.start) {
      return result;
    }
    for (const NlaStrip &child : strip.strips) {
      KeyedFrames child_frames = strip_frames(child, depth + 1);
      if (!child_frames.unsupported_reason.empty()) {
        return child_frames;
      }
      for (const float frame : child_frames.frames) {
        if (frame < strip.start || frame > strip.end) {
          continue;
        }
        /* Native meta evaluation first maps to normalized time, then reconstructs the child
         * time using the meta's own bounds. Invert precisely that decorated window. */
        const float normalized = (frame - strip.start) / (strip.end - strip.start);
        if (!append_frame(result, strip_map(strip, normalized, NLATIME_CONVERT_MAP))) {
          return result;
        }
      }
    }
    sort_unique(result.frames);
    return result;
  }
  const KeyedFrames keys = action_frames(strip.act, strip.action_slot_handle);
  if (!keys.unsupported_reason.empty()) {
    return keys;
  }
  if (strip.flag & NLASTRIP_FLAG_NO_TIME_MAP) {
    for (const float key : keys.frames) {
      if (key >= strip.start && key <= strip.end && !append_frame(result, key)) {
        return result;
      }
    }
    return result;
  }
  if (!std::isfinite(strip.scale) || !std::isfinite(strip.repeat) ||
      !std::isfinite(strip.actstart) || !std::isfinite(strip.actend) ||
      strip.actend <= strip.actstart)
  {
    result.unsupported_reason = "Keyed onion skin requires a finite nonzero NLA Action range";
    return result;
  }
  const float scale = IS_EQF(strip.scale, 0.0f) ? 1.0f : std::abs(strip.scale);
  const double period = double(strip.actend - strip.actstart) * scale;
  const double cycles = std::ceil(double(strip.end - strip.start) / period) + 1;
  if (!std::isfinite(cycles) || cycles > frame_limit) {
    result.unsupported_reason = "Keyed onion skin NLA repeat count exceeds its supported limit";
    return result;
  }
  /* MAP anchors reverse clips at their end. A private one-cycle window gives the inverse of
   * native EVAL even for fractional repeats; offset each repetition from this first cycle. */
  NlaStrip one_cycle = strip;
  one_cycle.end = float(double(strip.start) + period);
  one_cycle.repeat = 1.0f;
  for (const float key : keys.frames) {
    if (key < strip.actstart || key > strip.actend) {
      continue;
    }
    const double first = strip_map(one_cycle, key, NLATIME_CONVERT_MAP);
    for (int cycle = 0; cycle < int(cycles); cycle++) {
      const float frame = float(first + cycle * period);
      if (frame < strip.start || frame > strip.end) {
        continue;
      }
      /* Repeated boundaries evaluate to the next cycle's first key, except the final whole
       * repeat. Round-trip prevents adding an end key where it is not actually evaluated. */
      const float evaluated_key = strip_map(strip, frame, NLATIME_CONVERT_EVAL);
      const float tolerance = 1.0e-5f * math::max(1.0f, std::abs(key));
      if (std::abs(evaluated_key - key) <= tolerance && !append_frame(result, frame)) {
        return result;
      }
    }
  }
  sort_unique(result.frames);
  return result;
}

KeyedFrames keyed_frames_collect(const Object &rig)
{
  KeyedFrames result;
  const AnimData *adt = BKE_animdata_from_id(&rig.id);
  if (!adt) {
    return result;
  }
  const bool use_nla = !adt->nla_tracks.is_empty() && !(adt->flag & ADT_NLA_EVAL_OFF);
  if (!use_nla) {
    return action_frames(adt->action, adt->slot_handle);
  }
  const bool tweaking = adt->flag & ADT_NLA_EDIT_ON;
  const NlaTrack *tweaked_track = adt->act_track;
  if (tweaking && !tweaked_track) {
    for (const NlaTrack &track : adt->nla_tracks) {
      if (track.flag & NLATRACK_DISABLED) {
        tweaked_track = &track;
        break;
      }
    }
  }
  if (tweaking && !adt->actstrip) {
    result.unsupported_reason = "Keyed onion skin requires a valid NLA tweak strip";
    return result;
  }
  bool any_enabled_strips = false;
  for (const NlaTrack &track : adt->nla_tracks) {
    const bool is_tweaked = tweaking && &track == tweaked_track;
    if (!BKE_nlatrack_is_enabled(*adt, track) || (track.flag & NLATRACK_DISABLED && !is_tweaked)) {
      continue;
    }
    any_enabled_strips |= !track.strips.is_empty();
    if (is_tweaked) {
      if ((adt->flag & ADT_NLA_EDIT_NOMAP) || adt->actstrip->flag & NLASTRIP_FLAG_USR_TIME) {
        /* Native outside-in-place tweak evaluates the active Action at scene time. */
        if (!merge_frames(result, action_frames(adt->action, adt->slot_handle))) {
          return result;
        }
      }
      else {
        NlaStrip tweak = *adt->actstrip;
        tweak.next = tweak.prev = nullptr;
        if (tweak.act && tweak.flag & NLASTRIP_FLAG_SYNC_LENGTH) {
          BKE_nlastrip_recalculate_bounds_sync_action(&tweak);
        }
        if (!merge_frames(result, strip_frames(tweak, 0))) {
          return result;
        }
      }
      continue;
    }
    for (const NlaStrip &strip : track.strips) {
      if (!merge_frames(result, strip_frames(strip, 0))) {
        return result;
      }
    }
  }
  const bool soloing = adt->flag & ADT_NLA_SOLO_TRACK;
  const bool upper = !tweaking || (adt->flag & ADT_NLA_EVAL_UPPER_TRACKS);
  if ((!any_enabled_strips && !tweaking && !soloing) || (!soloing && upper)) {
    const bAction *action = tweaking ? adt->tmpact : adt->action;
    const int slot = tweaking ? adt->tmp_slot_handle : adt->slot_handle;
    if (!merge_frames(result, action_frames(action, slot))) {
      return result;
    }
  }
  sort_unique(result.frames);
  return result;
}

static float wrap_frame(const float frame, const int start, const double period)
{
  double offset = std::fmod(double(frame) - start, period);
  if (offset < 0) {
    offset += period;
  }
  return float(start + offset);
}

Vector<SampleFrame> sample_frames_select(const Span<float> sorted_keyed_frames,
                                         const Settings &settings,
                                         const float current_frame,
                                         const int frame_start,
                                         const int frame_end)
{
  Vector<SampleFrame> candidates;
  Vector<SampleFrame> result;
  if (!std::isfinite(current_frame) || (settings.loop_frame_range && frame_end < frame_start)) {
    return result;
  }
  const int before = math::clamp(settings.before, 0, samples_per_side_limit);
  const int after = math::clamp(settings.after, 0, samples_per_side_limit);
  if (before + after == 0) {
    return result;
  }
  const double period = double(frame_end) - frame_start + 1;
  const float reference = settings.loop_frame_range ?
                              wrap_frame(current_frame, frame_start, period) :
                              current_frame;
  if (settings.keyed_frames_only) {
    /* The capsule caches sorted unique authored keys. Find the current-frame split in logarithmic
     * time and inspect only enough neighbors to fill both quotas after cross-loop duplicates.
     * At most before+after keys from each direction can be needed: the opposite side can claim
     * at most its own quota. Never scan/sort an entire animation for each mouse event. */
    int64_t first = 0;
    int64_t last = sorted_keyed_frames.size();
    if (settings.loop_frame_range) {
      first = std::lower_bound(
                  sorted_keyed_frames.begin(), sorted_keyed_frames.end(), float(frame_start)) -
              sorted_keyed_frames.begin();
      last = std::upper_bound(
                 sorted_keyed_frames.begin(), sorted_keyed_frames.end(), float(frame_end)) -
             sorted_keyed_frames.begin();
    }
    if (first != last) {
      const Span<float> keys = sorted_keyed_frames.slice(first, last - first);
      for (const int direction : {-1, 1}) {
        if ((direction < 0 ? before : after) == 0) {
          continue;
        }
        const int64_t split = direction < 0 ?
                                  std::lower_bound(keys.begin(), keys.end(), reference) -
                                      keys.begin() :
                                  std::upper_bound(keys.begin(), keys.end(), reference) -
                                      keys.begin();
        int64_t index = direction < 0 ? split - 1 : split;
        int gathered = 0;
        std::optional<float> previous;
        for (int64_t visited = 0; visited < keys.size() && gathered < before + after; visited++) {
          if (index < 0 || index >= keys.size()) {
            if (!settings.loop_frame_range) {
              break;
            }
            index = direction < 0 ? keys.size() - 1 : 0;
          }
          const float frame = keys[index];
          index += direction;
          if (!std::isfinite(frame) || frame == reference || (previous && *previous == frame)) {
            continue;
          }
          previous = frame;
          float offset = frame - reference;
          if (settings.loop_frame_range) {
            if (direction < 0 && offset > 0) {
              offset = float(offset - period);
            }
            else if (direction > 0 && offset < 0) {
              offset = float(offset + period);
            }
          }
          candidates.append({frame, offset});
          gathered++;
        }
      }
    }
  }
  else {
    const int step = math::max(settings.step, 1);
    for (const int direction : {-1, 1}) {
      for (int distance = 1; distance <= (direction < 0 ? before : after); distance++) {
        const float offset = float(double(direction) * distance * step);
        if (settings.loop_frame_range && std::abs(offset) >= period) {
          continue;
        }
        const float frame = settings.loop_frame_range ?
                                wrap_frame(current_frame + offset, frame_start, period) :
                                current_frame + offset;
        if (std::isfinite(frame) && frame != reference) {
          candidates.append({frame, offset});
        }
      }
    }
  }
  /* If past and future reach the same wrapped key, prefer its nearest side; ties go to past.
   * Enforce each side's quota after choosing unique samples, avoiding duplicate overdraw. */
  std::sort(candidates.begin(), candidates.end(), [](const SampleFrame &a, const SampleFrame &b) {
    const float a_distance = std::abs(a.offset);
    const float b_distance = std::abs(b.offset);
    return a_distance == b_distance ? a.offset < b.offset : a_distance < b_distance;
  });
  int before_count = 0;
  int after_count = 0;
  for (const SampleFrame &candidate : candidates) {
    int &count = candidate.offset < 0 ? before_count : after_count;
    if (count == (candidate.offset < 0 ? before : after)) {
      continue;
    }
    if (std::any_of(result.begin(), result.end(), [&](const SampleFrame &sample) {
          return sample.frame == candidate.frame;
        }))
    {
      continue;
    }
    result.append(candidate);
    count++;
  }
  std::sort(result.begin(), result.end(), [](const SampleFrame &a, const SampleFrame &b) {
    return a.offset < b.offset;
  });
  return result;
}

}  // namespace blender::ed::mesh_onion_skin
