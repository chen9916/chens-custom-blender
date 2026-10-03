/* SPDX-FileCopyrightText: 2026 Blender Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

#include "anim_mesh_onion_skin_frames.hh"

#include <array>

#include "ANIM_action.hh"
#include "ANIM_keyframing.hh"

#include "BKE_animsys.hh"
#include "BKE_gtest_base.hh"
#include "BKE_main.hh"
#include "BKE_nla.hh"
#include "BKE_object.hh"

#include "BLI_listbase.hh"

#include "DNA_anim_types.h"
#include "DNA_object_types.h"

#include "MEM_guardedalloc.h"
#include "RNA_access.hh"
#include "RNA_prototypes.hh"
#include "testing/testing.h"

namespace blender::ed::mesh_onion_skin::tests {

class MeshOnionFramesTest : public bke::BlenderGTestBase {
 protected:
  Main *bmain;
  Object *rig;

  void SetUp() override
  {
    bmain = BKE_main_new();
    rig = BKE_object_add_only_object(bmain, OB_ARMATURE, "Rig");
    for (const float frame : {1.0f, 5.0f, 10.0f}) {
      insert_key(*rig, frame);
    }
  }

  void TearDown() override
  {
    BKE_main_free(bmain);
  }

  void insert_key(Object &object, const float frame)
  {
    object.loc[0] = frame;
    PointerRNA ptr = RNA_pointer_create_discrete(&object.id, RNA_Object, &object);
    const AnimationEvalContext context = BKE_animsys_eval_context_construct(nullptr, frame);
    animrig::insert_keyframes(bmain,
                              &ptr,
                              std::nullopt,
                              {{"location"}},
                              frame,
                              context,
                              BEZT_KEYTYPE_KEYFRAME,
                              INSERTKEY_NOFLAGS);
  }

  NlaStrip *add_clip(const float start = 20.0f,
                     const float scale = 2.0f,
                     const float repeat = 2.0f)
  {
    NlaTrack *track = BKE_nlatrack_new_tail(&rig->adt->nla_tracks, false);
    NlaStrip *strip = BKE_nlastrip_new_for_slot(rig->adt->action, rig->adt->slot_handle, rig->id);
    BLI_addtail(&track->strips, strip);
    strip->start = start;
    strip->actstart = 1.0f;
    strip->actend = 10.0f;
    strip->scale = scale;
    strip->repeat = repeat;
    strip->end = start + 9.0f * scale * repeat;
    strip->flag &= ~NLASTRIP_FLAG_SYNC_LENGTH;
    return strip;
  }

  void expect_frames(const KeyedFrames &result, const Span<float> expected)
  {
    ASSERT_TRUE(result.unsupported_reason.empty()) << result.unsupported_reason;
    ASSERT_EQ(result.frames.size(), expected.size());
    for (const int i : expected.index_range()) {
      EXPECT_FLOAT_EQ(result.frames[i], expected[i]);
    }
  }

  void expect_samples(const Vector<SampleFrame> &samples,
                      const Span<float> frames,
                      const Span<float> offsets)
  {
    ASSERT_EQ(samples.size(), frames.size());
    ASSERT_EQ(samples.size(), offsets.size());
    for (const int i : frames.index_range()) {
      EXPECT_FLOAT_EQ(samples[i].frame, frames[i]);
      EXPECT_FLOAT_EQ(samples[i].offset, offsets[i]);
    }
  }
};

TEST_F(MeshOnionFramesTest, AssignedSlotOnlyAndDuplicateChannels)
{
  const std::array expected = {1.0f, 5.0f, 10.0f};
  expect_frames(keyed_frames_collect(*rig), expected);
  Object *other = BKE_object_add_only_object(bmain, OB_ARMATURE, "Other Rig");
  ASSERT_TRUE(animrig::assign_action(rig->adt->action, other->id));
  animrig::Slot &other_slot = rig->adt->action->wrap().slot_add_for_id(other->id);
  ASSERT_EQ(animrig::assign_action_slot(&other_slot, other->id),
            animrig::ActionSlotAssignmentResult::OK);
  insert_key(*other, 7.5f);
  expect_frames(keyed_frames_collect(*rig), expected);
  const std::array other_expected = {7.5f};
  expect_frames(keyed_frames_collect(*other), other_expected);
}

TEST_F(MeshOnionFramesTest, NativeNlaScaleRepeatsAndReverse)
{
  NlaStrip *strip = add_clip();
  ASSERT_TRUE(animrig::unassign_action(rig->id));
  const std::array forward = {20.0f, 28.0f, 38.0f, 46.0f, 56.0f};
  expect_frames(keyed_frames_collect(*rig), forward);
  strip->flag |= NLASTRIP_FLAG_REVERSE;
  const std::array reverse = {20.0f, 30.0f, 38.0f, 48.0f, 56.0f};
  expect_frames(keyed_frames_collect(*rig), reverse);
  strip->repeat = 1.5f;
  strip->end = 47.0f;
  const std::array fractional_reverse = {20.0f, 30.0f, 38.0f};
  expect_frames(keyed_frames_collect(*rig), fractional_reverse);
}

TEST_F(MeshOnionFramesTest, EnabledNlaTracksHonorMuteSoloAndDisabled)
{
  NlaStrip *first = add_clip(20.0f, 1.0f, 1.0f);
  NlaStrip *second = add_clip(40.0f, 1.0f, 1.0f);
  ASSERT_TRUE(animrig::unassign_action(rig->id));
  NlaTrack *first_track = rig->adt->nla_tracks.first();
  NlaTrack *second_track = rig->adt->nla_tracks.last();
  first_track->flag |= NLATRACK_MUTED;
  const std::array second_keys = {40.0f, 44.0f, 49.0f};
  expect_frames(keyed_frames_collect(*rig), second_keys);
  first_track->flag &= ~NLATRACK_MUTED;
  first_track->flag |= NLATRACK_SOLO;
  rig->adt->flag |= ADT_NLA_SOLO_TRACK;
  const std::array first_keys = {20.0f, 24.0f, 29.0f};
  expect_frames(keyed_frames_collect(*rig), first_keys);
  rig->adt->flag &= ~ADT_NLA_SOLO_TRACK;
  second_track->flag |= NLATRACK_DISABLED;
  expect_frames(keyed_frames_collect(*rig), first_keys);
  first->flag |= NLASTRIP_FLAG_MUTED;
  EXPECT_TRUE(keyed_frames_collect(*rig).frames.is_empty());
  second->flag |= NLASTRIP_FLAG_MUTED;
}

TEST_F(MeshOnionFramesTest, TweakMappingDoesNotMutateSourceAndNoMapUsesSceneKeys)
{
  NlaStrip *strip = add_clip(20.0f, 2.0f, 1.0f);
  NlaTrack *track = rig->adt->nla_tracks.first();
  track->flag |= NLATRACK_DISABLED;
  rig->adt->flag |= ADT_NLA_EDIT_ON;
  rig->adt->act_track = track;
  rig->adt->actstrip = strip;
  const NlaStrip original = *strip;
  const std::array mapped = {20.0f, 28.0f, 38.0f};
  expect_frames(keyed_frames_collect(*rig), mapped);
  EXPECT_FLOAT_EQ(strip->start, original.start);
  EXPECT_FLOAT_EQ(strip->end, original.end);
  EXPECT_FLOAT_EQ(strip->scale, original.scale);
  EXPECT_FLOAT_EQ(strip->repeat, original.repeat);
  EXPECT_EQ(rig->adt->act_track, track);
  rig->adt->flag |= ADT_NLA_EDIT_NOMAP;
  const std::array unmapped = {1.0f, 5.0f, 10.0f};
  expect_frames(keyed_frames_collect(*rig), unmapped);
  rig->adt->flag &= ~(ADT_NLA_EDIT_ON | ADT_NLA_EDIT_NOMAP);
  rig->adt->actstrip = nullptr;
  rig->adt->act_track = nullptr;
}

TEST_F(MeshOnionFramesTest, NestedMetaWindowsUseNativeReverseMapping)
{
  NlaStrip *clip = add_clip(20.0f, 1.0f, 1.0f);
  NlaTrack *track = rig->adt->nla_tracks.first();
  BLI_remlink(&track->strips, clip);
  NlaStrip *meta = MEM_new<NlaStrip>(__func__);
  meta->type = NLASTRIP_TYPE_META;
  meta->start = 20.0f;
  meta->end = 29.0f;
  meta->flag |= NLASTRIP_FLAG_REVERSE;
  BLI_addtail(&meta->strips, clip);
  NlaStrip *outer = MEM_new<NlaStrip>(__func__);
  outer->type = NLASTRIP_TYPE_META;
  outer->start = 20.0f;
  outer->end = 29.0f;
  BLI_addtail(&outer->strips, meta);
  BLI_addtail(&track->strips, outer);
  ASSERT_TRUE(animrig::unassign_action(rig->id));
  const std::array expected = {20.0f, 25.0f, 29.0f};
  expect_frames(keyed_frames_collect(*rig), expected);
}

TEST_F(MeshOnionFramesTest, AnimatedNlaTimeRejectsKeyOnlyCollection)
{
  NlaStrip *strip = add_clip();
  strip->flag |= NLASTRIP_FLAG_USR_TIME;
  EXPECT_FALSE(keyed_frames_collect(*rig).unsupported_reason.empty());
  Settings settings;
  const std::array expected_frames = {3.0f, 4.0f, 6.0f, 7.0f};
  const std::array expected_offsets = {-2.0f, -1.0f, 1.0f, 2.0f};
  expect_samples(
      sample_frames_select({}, settings, 5.0f, 1, 21), expected_frames, expected_offsets);
}

TEST_F(MeshOnionFramesTest, KeyOnlySelectsNearestFractionalKeysAndBypassesStep)
{
  Settings settings;
  settings.keyed_frames_only = true;
  settings.step = 100;
  const std::array keys = {1.0f, 4.25f, 5.0f, 6.75f, 10.0f};
  const std::array expected_frames = {1.0f, 4.25f, 6.75f, 10.0f};
  const std::array expected_offsets = {-4.0f, -0.75f, 1.75f, 5.0f};
  expect_samples(
      sample_frames_select(keys, settings, 5.0f, 1, 21), expected_frames, expected_offsets);
}

TEST_F(MeshOnionFramesTest, LoopWrapsInclusiveSceneRangeAndPreservesFractionalFrame)
{
  Settings settings;
  settings.loop_frame_range = true;
  const std::array frames = {20.0f, 21.0f, 2.0f, 3.0f};
  const std::array offsets = {-2.0f, -1.0f, 1.0f, 2.0f};
  expect_samples(sample_frames_select({}, settings, 1.0f, 1, 21), frames, offsets);
  const std::array fractional = {19.5f, 20.5f, 1.5f, 2.5f};
  expect_samples(sample_frames_select({}, settings, 21.5f, 1, 21), fractional, offsets);
  EXPECT_TRUE(sample_frames_select({}, settings, 1.0f, 21, 1).is_empty());
}

TEST_F(MeshOnionFramesTest, KeyedLoopSkipsOutsideRangeCurrentKeysAndDuplicateLaps)
{
  Settings settings;
  settings.keyed_frames_only = true;
  settings.loop_frame_range = true;
  const std::array keys = {-1.0f, 1.0f, 2.0f, 2.0f, 3.0f, 22.0f};
  const std::array frames = {3.0f, 2.0f};
  const std::array offsets = {-1.0f, 1.0f};
  expect_samples(sample_frames_select(keys, settings, 1.0f, 1, 3), frames, offsets);
  const std::array only_current = {1.0f};
  EXPECT_TRUE(sample_frames_select(only_current, settings, 1.0f, 1, 1).is_empty());
  settings.keyed_frames_only = false;
  expect_samples(sample_frames_select({}, settings, 1.0f, 1, 3), frames, offsets);
  settings.step = 3;
  EXPECT_TRUE(sample_frames_select({}, settings, 1.0f, 1, 3).is_empty());
}

TEST_F(MeshOnionFramesTest, LongKeyedAnimationKeepsNearestQuotasAcrossLoop)
{
  Vector<float> keys;
  for (int frame = 1; frame <= 100000; frame++) {
    keys.append(float(frame));
  }
  Settings settings;
  settings.keyed_frames_only = true;
  settings.loop_frame_range = true;
  const std::array frames = {99999.0f, 100000.0f, 2.0f, 3.0f};
  const std::array offsets = {-2.0f, -1.0f, 1.0f, 2.0f};
  expect_samples(sample_frames_select(keys, settings, 1.0f, 1, 100000), frames, offsets);
  settings.before = 0;
  settings.after = 4;
  const std::array future = {1.0f, 2.0f, 3.0f, 4.0f};
  const std::array future_offsets = {1.0f, 2.0f, 3.0f, 4.0f};
  expect_samples(
      sample_frames_select(keys, settings, 100000.0f, 1, 100000), future, future_offsets);
}

TEST_F(MeshOnionFramesTest, KeyedLoopFillsUnevenQuotasAfterSharedNearestKeys)
{
  Settings settings;
  settings.keyed_frames_only = true;
  settings.loop_frame_range = true;
  settings.before = 1;
  settings.after = 4;
  const std::array keys = {2.0f, 3.0f, 4.0f, 5.0f, 10.0f};
  const std::array frames = {10.0f, 2.0f, 3.0f, 4.0f, 5.0f};
  const std::array offsets = {-1.0f, 1.0f, 2.0f, 3.0f, 4.0f};
  expect_samples(sample_frames_select(keys, settings, 1.0f, 1, 10), frames, offsets);
}

}  // namespace blender::ed::mesh_onion_skin::tests
