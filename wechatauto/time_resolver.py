# -*- coding: utf-8 -*-
"""将 LLM 提取的中文时间语义确定性地解析为北京时间。"""

from __future__ import annotations

import re
from calendar import monthrange
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo("Asia/Shanghai")
SOON_MINUTES = 10
SHORT_TERM_MAX_HOURS = 6

_CN_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3,
    "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_DAYPART_RE = re.compile(r"凌晨|早上|上午|中午|下午|傍晚|晚上|今晚|夜里")
_SHORT_RE = re.compile(r"等会儿?|待会儿?|一会儿?|过会儿?|稍后")
_CLOCK_RE = re.compile(
    r"(?<!\d)(?P<colon_h>\d{1,2})\s*[:：]\s*(?P<colon_m>\d{1,2})"
    r"|(?<!\d)(?P<num_h>\d{1,2})\s*点\s*(?:(?P<num_half>半)|(?P<num_m>\d{1,2})\s*分?)?"
    r"|(?P<cn_h>[零〇一二两三四五六七八九十]{1,3})\s*点\s*"
    r"(?:(?P<cn_half>半)|(?P<cn_m>[零〇一二两三四五六七八九十]{1,3})\s*分?)?"
)


def _cn_int(value: str) -> int:
    if value.isdigit():
        return int(value)
    if value == "十":
        return 10
    if "十" in value:
        left, right = value.split("十", 1)
        return (_CN_DIGITS.get(left, 1) * 10) + (_CN_DIGITS.get(right, 0) if right else 0)
    digits = [_CN_DIGITS[ch] for ch in value]
    return int("".join(str(item) for item in digits))


def _number(value: str) -> float:
    value = value.strip()
    if value == "半":
        return 0.5
    return float(_cn_int(value))


def _iso(value: datetime) -> str:
    return value.astimezone(SHANGHAI).isoformat(timespec="seconds")


class TimeResolver:
    def resolve(self, item: dict, now: datetime | None = None) -> dict:
        now = self._now(now)
        date_text = str(item.get("date_text") or "").strip()
        time_text = str(item.get("time_text") or "").strip()
        remind_text = str(item.get("remind_text") or "").strip()
        combined = (date_text + " " + time_text).strip()

        relative = self._relative_datetime(combined, now)
        if relative is not None:
            event_at = relative
            all_day = False
        else:
            date_source = date_text
            if not date_source and re.search(
                r"今天|明天|后天|大后天|本周|这周|下周|下下周|本月|下个月|"
                r"天后|周后|个月后|年后|\d{1,4}\s*[-年月号]", combined,
            ):
                date_source = combined
            date_result = self._resolve_date(date_source, now.date())
            if date_result[0] == "failed":
                return self._failed(date_result[1])
            target_date = date_result[1]
            if not time_text and not _CLOCK_RE.search(combined):
                if date_text and not _DAYPART_RE.search(date_text):
                    event_at = datetime.combine(target_date, time(), SHANGHAI)
                    all_day = True
                else:
                    return self._failed("缺少具体时间")
            else:
                clock = self._resolve_clock(combined, target_date, now)
                if clock["status"] != "success":
                    return clock
                event_at = clock["value"]
                all_day = False

        if not all_day and event_at <= now:
            return self._failed("执行时间已经过去，请明确新的日期和时间")

        reminder = self._resolve_reminder(remind_text, event_at, all_day, now)
        if reminder["status"] != "success":
            return reminder
        return {
            "status": "success",
            "event_at": _iso(event_at),
            "remind_at": _iso(reminder["value"]) if reminder["value"] else None,
            "event_all_day": all_day,
        }

    @staticmethod
    def _now(value: datetime | None) -> datetime:
        value = value or datetime.now(SHANGHAI)
        if value.tzinfo is None:
            value = value.replace(tzinfo=SHANGHAI)
        return value.astimezone(SHANGHAI)

    @staticmethod
    def _failed(reason: str) -> dict:
        return {"status": "failed", "reason": reason}

    @staticmethod
    def _ambiguous(reason: str, candidates: list[datetime]) -> dict:
        return {
            "status": "ambiguous", "reason": reason,
            "candidates": [_iso(value) for value in candidates],
        }

    def _resolve_date(self, text: str, today: date):
        if "大后天" in text:
            return "success", today + timedelta(days=3)
        if "后天" in text:
            return "success", today + timedelta(days=2)
        if "明天" in text:
            return "success", today + timedelta(days=1)
        if "今天" in text or not text:
            return "success", today

        relative_days = re.search(
            r"([0-9零〇一二两三四五六七八九十]{1,4})\s*(?:个)?\s*天后", text,
        )
        if relative_days:
            return "success", today + timedelta(days=int(_number(relative_days.group(1))))

        relative_weeks = re.search(
            r"([0-9零〇一二两三四五六七八九十]{1,4})\s*(?:个)?\s*周后", text,
        )
        if relative_weeks:
            return "success", today + timedelta(weeks=int(_number(relative_weeks.group(1))))

        relative_months = re.search(
            r"([0-9零〇一二两三四五六七八九十]{1,4})\s*(?:个)?\s*月后", text,
        )
        if relative_months:
            return "success", self._add_months(today, int(_number(relative_months.group(1))))

        relative_years = re.search(
            r"([0-9零〇一二两三四五六七八九十]{1,4})\s*(?:个)?\s*年后", text,
        )
        if relative_years:
            return "success", self._add_months(today, int(_number(relative_years.group(1))) * 12)

        weekday = re.search(r"(下下周|下周|本周|这周)\s*([一二三四五六日天])", text)
        if weekday:
            indexes = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
            week_offsets = {"本周": 0, "这周": 0, "下周": 1, "下下周": 2}
            monday = today - timedelta(days=today.weekday())
            return "success", monday + timedelta(
                weeks=week_offsets[weekday.group(1)],
                days=indexes[weekday.group(2)],
            )
        if re.search(r"本周|这周|下周|下下周", text):
            return "failed", "未指定具体星期"

        next_month = re.search(r"下个月\s*([0-9一二三四五六七八九十]{1,3})\s*[日号]", text)
        if next_month:
            month = 1 if today.month == 12 else today.month + 1
            year = today.year + (1 if today.month == 12 else 0)
            return self._calendar_date(year, month, _cn_int(next_month.group(1)))
        if "下个月" in text:
            return "failed", "下个月未指定具体日期"

        current_month = re.search(
            r"本月\s*([0-9一二三四五六七八九十]{1,3})\s*[日号]", text,
        )
        if current_month:
            return self._calendar_date(
                today.year, today.month, _cn_int(current_month.group(1)),
            )

        iso_date = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
        if iso_date:
            return self._calendar_date(*map(int, iso_date.groups()))

        month_day = re.search(
            r"(?:(\d{4})\s*年)?\s*(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?", text,
        )
        if month_day:
            raw_year, raw_month, raw_day = month_day.groups()
            year = int(raw_year) if raw_year else today.year
            result = self._calendar_date(year, int(raw_month), int(raw_day))
            if result[0] == "success" and not raw_year and result[1] < today:
                result = self._calendar_date(year + 1, int(raw_month), int(raw_day))
            return result

        day_only = re.search(r"([0-9一二三四五六七八九十]{1,3})\s*[日号]", text)
        if day_only:
            day = _cn_int(day_only.group(1))
            result = self._calendar_date(today.year, today.month, day)
            if result[0] == "success" and result[1] < today:
                next_date = self._add_months(today.replace(day=1), 1)
                result = self._calendar_date(next_date.year, next_date.month, day)
            return result
        return "failed", "无法识别日期表达：%s" % text

    @staticmethod
    def _add_months(source: date, count: int) -> date:
        index = source.year * 12 + source.month - 1 + count
        year, month_index = divmod(index, 12)
        month = month_index + 1
        return date(year, month, min(source.day, monthrange(year, month)[1]))

    @staticmethod
    def _calendar_date(year: int, month: int, day: int):
        try:
            return "success", date(year, month, day)
        except ValueError:
            return "failed", "日期不存在：%s-%s-%s" % (year, month, day)

    def _relative_datetime(self, text: str, now: datetime) -> datetime | None:
        duration = re.search(
            r"([0-9零〇一二两三四五六七八九十半]{1,4})\s*(?:个)?\s*(分钟|小时)后", text,
        )
        if duration:
            amount = _number(duration.group(1))
            minutes = amount if duration.group(2) == "分钟" else amount * 60
            return now.replace(second=0, microsecond=0) + timedelta(minutes=minutes)
        if _SHORT_RE.search(text) and not _CLOCK_RE.search(text):
            return now.replace(second=0, microsecond=0) + timedelta(minutes=SOON_MINUTES)
        return None

    def _resolve_clock(self, text: str, target_date: date, now: datetime) -> dict:
        match = _CLOCK_RE.search(text)
        if not match:
            return self._failed("无法识别具体时间：%s" % text)
        if match.group("colon_h") is not None:
            hour, minute = int(match.group("colon_h")), int(match.group("colon_m"))
            exact_24_hour = True
        else:
            raw_hour = match.group("num_h") or match.group("cn_h")
            hour = _cn_int(raw_hour)
            raw_minute = match.group("num_m") or match.group("cn_m")
            minute = 30 if match.group("num_half") or match.group("cn_half") else (
                _cn_int(raw_minute) if raw_minute else 0
            )
            exact_24_hour = hour > 12
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            return self._failed("时间数值无效")

        daypart = _DAYPART_RE.search(text)
        if daypart:
            hour = self._apply_daypart(hour, daypart.group())
            return {"status": "success", "value": datetime.combine(
                target_date, time(hour, minute), SHANGHAI,
            )}
        if exact_24_hour:
            return {"status": "success", "value": datetime.combine(
                target_date, time(hour, minute), SHANGHAI,
            )}

        am_hour = 0 if hour == 12 else hour
        pm_hour = 12 if hour == 12 else hour + 12
        candidates = [
            datetime.combine(target_date, time(am_hour, minute), SHANGHAI),
            datetime.combine(target_date, time(pm_hour, minute), SHANGHAI),
        ]
        if _SHORT_RE.search(text) and target_date == now.date():
            near = [value for value in candidates if now < value <= now + timedelta(hours=SHORT_TERM_MAX_HOURS)]
            if len(near) == 1:
                return {"status": "success", "value": near[0]}
        return self._ambiguous(
            "无法确定上午%s或下午/晚上%s" % (
                candidates[0].strftime("%H:%M"), candidates[1].strftime("%H:%M"),
            ),
            candidates,
        )

    @staticmethod
    def _apply_daypart(hour: int, daypart: str) -> int:
        if daypart in {"下午", "傍晚", "晚上", "今晚", "夜里"}:
            return hour + 12 if 1 <= hour < 12 else hour
        if daypart == "中午":
            return hour + 12 if 1 <= hour <= 10 else hour
        if daypart in {"凌晨", "早上", "上午"} and hour == 12:
            return 0
        return hour

    def _resolve_reminder(
        self, text: str, event_at: datetime, all_day: bool, now: datetime,
    ) -> dict:
        if all_day and not text:
            return {"status": "success", "value": None}
        if not text or re.search(r"到时候|准时|届时", text):
            return {"status": "success", "value": event_at}

        advance = re.search(
            r"提前\s*([0-9零〇一二两三四五六七八九十半]{1,4})\s*(?:个)?\s*(分钟|小时|天)", text,
        )
        if advance:
            amount = _number(advance.group(1))
            unit = {"分钟": 1, "小时": 60, "天": 1440}[advance.group(2)]
            value = event_at - timedelta(minutes=amount * unit)
        elif "前一天" in text:
            value = event_at - timedelta(days=1)
        else:
            clock = self._resolve_clock(text, event_at.date(), now)
            if clock["status"] != "success":
                return self._failed("无法识别提醒时间：%s" % text)
            value = clock["value"]
        if value <= now:
            return self._failed("提醒时间已经过去")
        if value > event_at and not all_day:
            return self._failed("提醒时间不能晚于执行时间")
        return {"status": "success", "value": value}
