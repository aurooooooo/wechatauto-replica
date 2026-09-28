"""LLM 时间语义到北京时间的确定性测试。"""

from __future__ import annotations

import unittest
from datetime import datetime

from wechatauto.time_resolver import SHANGHAI, TimeResolver


NONE = {"kind": "none"}
AT_EVENT = {"kind": "at_event"}


class TimeResolverTest(unittest.TestCase):
    def setUp(self):
        self.resolver = TimeResolver()
        self.now = datetime(2026, 9, 10, 14, 0, tzinfo=SHANGHAI)

    def resolve(self, date_semantic, time_semantic, reminder_semantic=AT_EVENT, now=None):
        return self.resolver.resolve({
            "date_semantic": date_semantic,
            "time_semantic": time_semantic,
            "reminder_semantic": reminder_semantic,
        }, now or self.now)

    @staticmethod
    def clock(hour, minute=0, period=None, is_24_hour=False):
        return {
            "kind": "clock", "hour": hour, "minute": minute,
            "day_period": period, "is_24_hour": is_24_hour,
        }

    def test_24_hour_colon_mapping(self):
        result = self.resolve({"kind": "today"}, self.clock(15, 30, is_24_hour=True))
        self.assertEqual(result["event_at"], "2026-09-10T15:30:00+08:00")

    def test_afternoon_clock_mapping(self):
        result = self.resolve(NONE, self.clock(3, period="afternoon"))
        self.assertEqual(result["event_at"], "2026-09-10T15:00:00+08:00")

    def test_unspecified_date_rolls_explicit_period_to_next_day(self):
        now = datetime(2026, 9, 10, 18, 0, tzinfo=SHANGHAI)
        result = self.resolve(NONE, self.clock(3, period="afternoon"), now=now)
        self.assertEqual(result["event_at"], "2026-09-11T15:00:00+08:00")

    def test_tomorrow_and_advance_reminder_mapping(self):
        result = self.resolve(
            {"kind": "relative_days", "value": 1},
            self.clock(3, period="afternoon"),
            {"kind": "before_event", "value": 30, "unit": "minutes"},
        )
        self.assertEqual(result["event_at"], "2026-09-11T15:00:00+08:00")
        self.assertEqual(result["remind_at"], "2026-09-11T14:30:00+08:00")

    def test_day_after_tomorrow_mapping(self):
        result = self.resolve(
            {"kind": "relative_days", "value": 2}, self.clock(8, period="evening"),
        )
        self.assertEqual(result["event_at"], "2026-09-12T20:00:00+08:00")

    def test_three_days_later_mapping(self):
        result = self.resolve(
            {"kind": "relative_days", "value": 3}, self.clock(2, period="afternoon"),
        )
        self.assertEqual(result["event_at"], "2026-09-13T14:00:00+08:00")

    def test_two_weeks_later_mapping(self):
        result = self.resolve(
            {"kind": "relative_weeks", "value": 2}, self.clock(2, period="afternoon"),
        )
        self.assertEqual(result["event_at"], "2026-09-24T14:00:00+08:00")

    def test_one_month_later_mapping(self):
        result = self.resolve(
            {"kind": "relative_months", "value": 1}, self.clock(2, period="afternoon"),
        )
        self.assertEqual(result["event_at"], "2026-10-10T14:00:00+08:00")

    def test_next_month_specific_day_mapping(self):
        result = self.resolve(
            {"kind": "month_offset_day", "month_offset": 1, "day": 4},
            self.clock(8, period="morning"),
        )
        self.assertEqual(result["event_at"], "2026-10-04T08:00:00+08:00")

    def test_all_day_event_can_remind_on_same_morning(self):
        result = self.resolve(
            {"kind": "month_offset_day", "month_offset": 1, "day": 4},
            NONE,
            {"kind": "clock", "hour": 8, "minute": 0,
             "day_period": "morning", "is_24_hour": False},
        )
        self.assertTrue(result["event_all_day"])
        self.assertEqual(result["event_at"], "2026-10-04T00:00:00+08:00")
        self.assertEqual(result["remind_at"], "2026-10-04T08:00:00+08:00")

    def test_next_monday_mapping(self):
        result = self.resolve(
            {"kind": "weekday", "week_offset": 1, "weekday": 1},
            self.clock(3, period="afternoon"),
        )
        self.assertEqual(result["event_at"], "2026-09-14T15:00:00+08:00")

    def test_half_hour_later_mapping(self):
        result = self.resolve(
            NONE, {"kind": "relative_duration", "value": 30, "unit": "minutes"},
        )
        self.assertEqual(result["event_at"], "2026-09-10T14:30:00+08:00")

    def test_two_hours_later_mapping(self):
        result = self.resolve(
            NONE, {"kind": "relative_duration", "value": 2, "unit": "hours"},
        )
        self.assertEqual(result["event_at"], "2026-09-10T16:00:00+08:00")

    def test_soon_defaults_to_ten_minutes(self):
        result = self.resolve(NONE, {"kind": "soon"})
        self.assertEqual(result["event_at"], "2026-09-10T14:10:00+08:00")

    def test_bare_eleven_thirty_at_nine_uses_morning(self):
        now = datetime(2026, 9, 10, 9, 0, tzinfo=SHANGHAI)
        result = self.resolve(NONE, self.clock(11, 30), now=now)
        self.assertEqual(result["event_at"], "2026-09-10T11:30:00+08:00")

    def test_bare_afternoon_clock_uses_nearest_future_time(self):
        now = datetime(2026, 9, 10, 13, 0, tzinfo=SHANGHAI)
        result = self.resolve(NONE, self.clock(2, 30, "afternoon"), now=now)
        self.assertEqual(result["event_at"], "2026-09-10T14:30:00+08:00")

    def test_bare_eleven_thirty_at_six_uses_evening(self):
        now = datetime(2026, 9, 10, 18, 0, tzinfo=SHANGHAI)
        result = self.resolve(NONE, self.clock(11, 30), now=now)
        self.assertEqual(result["event_at"], "2026-09-10T23:30:00+08:00")

    def test_bare_eleven_thirty_after_both_candidates_uses_next_day(self):
        now = datetime(2026, 9, 10, 23, 45, tzinfo=SHANGHAI)
        result = self.resolve(NONE, self.clock(11, 30), now=now)
        self.assertEqual(result["event_at"], "2026-09-11T11:30:00+08:00")

    def test_explicit_past_date_does_not_roll_forward(self):
        now = datetime(2026, 9, 10, 23, 45, tzinfo=SHANGHAI)
        result = self.resolve({"kind": "today"}, self.clock(11, 30), now=now)
        self.assertEqual(result["status"], "failed")

    def test_incomplete_date_fails(self):
        result = self.resolve(
            {"kind": "incomplete", "reason": "下周未指定星期"},
            self.clock(3, period="afternoon"),
        )
        self.assertEqual(result, {"status": "failed", "reason": "下周未指定星期"})

    def test_unknown_semantic_kind_fails(self):
        result = self.resolve({"kind": "model_guess"}, NONE, NONE)
        self.assertEqual(result["status"], "failed")

    def test_raw_text_has_no_legacy_fallback(self):
        result = self.resolver.resolve({
            "date_text": "明天", "time_text": "下午三点", "remind_text": None,
        }, self.now)
        self.assertEqual(result["status"], "failed")
        self.assertIn("date_semantic", result["reason"])


if __name__ == "__main__":
    unittest.main()
