"""中文待办时间解析器的确定性测试。"""

from __future__ import annotations

import unittest
from datetime import datetime

from wechatauto.time_resolver import SHANGHAI, TimeResolver


class TimeResolverTest(unittest.TestCase):
    def setUp(self):
        self.resolver = TimeResolver()
        self.now = datetime(2026, 9, 10, 14, 0, tzinfo=SHANGHAI)

    def resolve(self, date_text=None, time_text=None, remind_text=None):
        return self.resolver.resolve({
            "date_text": date_text, "time_text": time_text,
            "remind_text": remind_text,
        }, self.now)

    def test_24_hour_colon(self):
        result = self.resolve("今天", "15:30")
        self.assertEqual(result["event_at"], "2026-09-10T15:30:00+08:00")

    def test_24_hour_chinese_clock(self):
        result = self.resolve("今天", "15点30")
        self.assertEqual(result["event_at"], "2026-09-10T15:30:00+08:00")

    def test_afternoon_clock(self):
        result = self.resolve(None, "下午三点")
        self.assertEqual(result["event_at"], "2026-09-10T15:00:00+08:00")

    def test_evening_clock(self):
        result = self.resolve("今天", "晚上八点")
        self.assertEqual(result["event_at"], "2026-09-10T20:00:00+08:00")

    def test_tomorrow_and_advance_reminder(self):
        result = self.resolve("明天", "下午三点", "提前30分钟")
        self.assertEqual(result["event_at"], "2026-09-11T15:00:00+08:00")
        self.assertEqual(result["remind_at"], "2026-09-11T14:30:00+08:00")

    def test_day_after_tomorrow(self):
        result = self.resolve("后天", "晚上八点")
        self.assertEqual(result["event_at"], "2026-09-12T20:00:00+08:00")

    def test_three_days_later(self):
        result = self.resolve("三天后", "下午两点")
        self.assertEqual(result["event_at"], "2026-09-13T14:00:00+08:00")

    def test_ten_days_later(self):
        result = self.resolve("10天后", "下午两点")
        self.assertEqual(result["event_at"], "2026-09-20T14:00:00+08:00")

    def test_two_weeks_later(self):
        result = self.resolve("两周后", "下午两点")
        self.assertEqual(result["event_at"], "2026-09-24T14:00:00+08:00")

    def test_one_month_later(self):
        result = self.resolve("一个月后", "下午两点")
        self.assertEqual(result["event_at"], "2026-10-10T14:00:00+08:00")

    def test_next_month_specific_day(self):
        result = self.resolve("下个月4号", "早上八点")
        self.assertEqual(result["event_at"], "2026-10-04T08:00:00+08:00")

    def test_all_day_event_can_remind_on_same_morning(self):
        result = self.resolve("下个月4号", None, "当天早上八点")
        self.assertTrue(result["event_all_day"])
        self.assertEqual(result["event_at"], "2026-10-04T00:00:00+08:00")
        self.assertEqual(result["remind_at"], "2026-10-04T08:00:00+08:00")

    def test_next_month_without_day_fails(self):
        result = self.resolve("下个月", "下午两点")
        self.assertEqual(result["status"], "failed")

    def test_next_monday(self):
        result = self.resolve("下周一", "下午三点")
        self.assertEqual(result["event_at"], "2026-09-14T15:00:00+08:00")

    def test_this_friday(self):
        result = self.resolve("本周五", "下午三点")
        self.assertEqual(result["event_at"], "2026-09-11T15:00:00+08:00")

    def test_monday_after_next(self):
        result = self.resolve("下下周一", "下午三点")
        self.assertEqual(result["event_at"], "2026-09-21T15:00:00+08:00")

    def test_half_hour_later(self):
        result = self.resolve(None, "半小时后")
        self.assertEqual(result["event_at"], "2026-09-10T14:30:00+08:00")

    def test_two_hours_later(self):
        result = self.resolve(None, "两小时后")
        self.assertEqual(result["event_at"], "2026-09-10T16:00:00+08:00")

    def test_soon_defaults_to_ten_minutes(self):
        result = self.resolve(None, "等会儿")
        self.assertEqual(result["event_at"], "2026-09-10T14:10:00+08:00")

    def test_short_term_clock_uses_nearest_future_candidate(self):
        result = self.resolve(None, "等会儿三点半")
        self.assertEqual(result["event_at"], "2026-09-10T15:30:00+08:00")

    def test_bare_eleven_thirty_is_ambiguous(self):
        result = self.resolve("今天", "十一点半")
        self.assertEqual(result["status"], "ambiguous")
        self.assertEqual(len(result["candidates"]), 2)

    def test_missing_weekday_fails(self):
        result = self.resolve("下周", "下午三点")
        self.assertEqual(result["status"], "failed")

    def test_daypart_without_clock_fails(self):
        result = self.resolve("下周一", "下午")
        self.assertEqual(result["status"], "failed")

    def test_multiple_items_are_resolved_independently(self):
        items = [
            {"date_text": "今天", "time_text": "下午三点", "remind_text": None},
            {"date_text": "今天", "time_text": "晚上八点", "remind_text": None},
        ]
        results = [self.resolver.resolve(item, self.now) for item in items]
        self.assertEqual(
            [result["event_at"] for result in results],
            ["2026-09-10T15:00:00+08:00", "2026-09-10T20:00:00+08:00"],
        )


if __name__ == "__main__":
    unittest.main()
