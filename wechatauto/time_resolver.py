# -*- coding: utf-8 -*-
"""将 LLM 提取的中文时间语义确定性地解析为北京时间。"""

from __future__ import annotations

from calendar import monthrange
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo("Asia/Shanghai")
SOON_MINUTES = 10


def _iso(value: datetime) -> str:
    return value.astimezone(SHANGHAI).isoformat(timespec="seconds")


class TimeResolver:
    def resolve(self, item: dict, now: datetime | None = None) -> dict:
        return self._resolve_semantics(item, self._now(now))

    def _resolve_semantics(self, item: dict, now: datetime) -> dict:
        """只消费 LLM 的有限枚举语义；原始文本不参与时间计算。"""
        try:
            date_spec = self._semantic_dict(item.get("date_semantic"), "date_semantic")
            time_spec = self._semantic_dict(item.get("time_semantic"), "time_semantic")
            reminder_spec = self._semantic_dict(
                item.get("reminder_semantic"), "reminder_semantic",
            )
            time_kind = str(time_spec.get("kind") or "none")
            if time_kind == "incomplete":
                return self._failed(str(time_spec.get("reason") or "时间信息不完整"))

            if time_kind in {"relative_duration", "soon"}:
                event_at = self._semantic_relative_time(time_spec, now)
                all_day = False
            else:
                date_result = self._semantic_date(date_spec, now.date())
                if date_result[0] == "failed":
                    return self._failed(date_result[1])
                target_date, explicit_date = date_result[1], date_result[2]
                if time_kind == "clock":
                    clock = self._semantic_clock(
                        time_spec, target_date, now, allow_next_day=not explicit_date,
                    )
                    if clock["status"] != "success":
                        return clock
                    event_at, all_day = clock["value"], False
                elif time_kind == "none" and explicit_date:
                    event_at = datetime.combine(target_date, time(), SHANGHAI)
                    all_day = True
                elif time_kind == "none":
                    return self._failed("缺少日期和具体时间")
                else:
                    return self._failed("不支持的时间语义类型：%s" % time_kind)
        except (TypeError, ValueError) as exc:
            return self._failed(str(exc))

        if not all_day and event_at <= now:
            return self._failed("执行时间已经过去，请明确新的日期和时间")
        reminder = self._semantic_reminder(reminder_spec, event_at, all_day, now)
        if reminder["status"] != "success":
            return reminder
        return {
            "status": "success",
            "event_at": _iso(event_at),
            "remind_at": _iso(reminder["value"]) if reminder["value"] else None,
            "event_all_day": all_day,
        }

    @staticmethod
    def _semantic_dict(value, field: str) -> dict:
        if not isinstance(value, dict):
            raise ValueError("%s 缺失或不是对象" % field)
        return value

    @staticmethod
    def _semantic_int(spec: dict, field: str, minimum: int, maximum: int) -> int:
        value = spec.get(field)
        if isinstance(value, bool):
            raise ValueError("%s 不是有效整数" % field)
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("缺少或无法识别 %s" % field) from exc
        if str(value).strip() not in {str(number), "%s.0" % number}:
            raise ValueError("%s 不是有效整数" % field)
        if not minimum <= number <= maximum:
            raise ValueError("%s 超出范围" % field)
        return number

    def _semantic_date(self, spec: dict, today: date):
        kind = str(spec.get("kind") or "none")
        if kind == "incomplete":
            return "failed", str(spec.get("reason") or "日期信息不完整")
        if kind == "none":
            return "success", today, False
        if kind == "today":
            return "success", today, True
        if kind in {
            "relative_days", "relative_weeks", "relative_months", "relative_years",
        }:
            value = self._semantic_int(spec, "value", 0, 10000)
            if kind == "relative_days":
                result = today + timedelta(days=value)
            elif kind == "relative_weeks":
                result = today + timedelta(weeks=value)
            else:
                result = self._add_months(
                    today, value * (12 if kind == "relative_years" else 1),
                )
            return "success", result, True
        if kind == "month_offset_day":
            month_offset = self._semantic_int(spec, "month_offset", 0, 1200)
            shifted = self._add_months(today.replace(day=1), month_offset)
            result = self._calendar_date(
                shifted.year, shifted.month,
                self._semantic_int(spec, "day", 1, 31),
            )
            return (*result, True) if result[0] == "success" else result
        if kind == "weekday":
            week_offset = self._semantic_int(spec, "week_offset", 0, 520)
            weekday = self._semantic_int(spec, "weekday", 1, 7)
            monday = today - timedelta(days=today.weekday())
            return "success", monday + timedelta(
                weeks=week_offset, days=weekday - 1,
            ), True
        if kind == "calendar_date":
            month = self._semantic_int(spec, "month", 1, 12)
            day = self._semantic_int(spec, "day", 1, 31)
            raw_year = spec.get("year")
            year = (
                self._semantic_int(spec, "year", 1, 9999)
                if raw_year is not None else today.year
            )
            result = self._calendar_date(year, month, day)
            if result[0] == "success" and raw_year is None and result[1] < today:
                result = self._calendar_date(year + 1, month, day)
            return (*result, True) if result[0] == "success" else result
        if kind == "day_of_month":
            day = self._semantic_int(spec, "day", 1, 31)
            result = self._calendar_date(today.year, today.month, day)
            if result[0] == "success" and result[1] < today:
                next_month = self._add_months(today.replace(day=1), 1)
                result = self._calendar_date(next_month.year, next_month.month, day)
            return (*result, True) if result[0] == "success" else result
        return "failed", "不支持的日期语义类型：%s" % kind

    def _semantic_relative_time(self, spec: dict, now: datetime) -> datetime:
        kind = str(spec.get("kind") or "none")
        if kind == "soon":
            minutes = SOON_MINUTES
        else:
            value = self._semantic_int(spec, "value", 1, 100000)
            unit = str(spec.get("unit") or "")
            if unit not in {"minutes", "hours"}:
                raise ValueError("相对时间单位只支持 minutes 或 hours")
            minutes = value * (60 if unit == "hours" else 1)
        return now.replace(second=0, microsecond=0) + timedelta(minutes=minutes)

    def _semantic_clock(
        self, spec: dict, target_date: date, now: datetime,
        allow_next_day: bool = False,
    ) -> dict:
        hour = self._semantic_int(spec, "hour", 0, 23)
        minute = self._semantic_int(spec, "minute", 0, 59)
        day_period = spec.get("day_period")
        if day_period is not None:
            if day_period not in {
                "dawn", "morning", "noon", "afternoon", "evening", "night",
            }:
                return self._failed("不支持的时段语义：%s" % day_period)
            hour = self._apply_daypart(hour, day_period)
            value = datetime.combine(target_date, time(hour, minute), SHANGHAI)
            if allow_next_day and value <= now:
                value += timedelta(days=1)
            return {"status": "success", "value": value}

        if spec.get("is_24_hour") is True or hour > 12:
            value = datetime.combine(target_date, time(hour, minute), SHANGHAI)
            if allow_next_day and value <= now:
                value += timedelta(days=1)
            return {"status": "success", "value": value}
        am_hour = 0 if hour == 12 else hour
        pm_hour = 12 if hour == 12 else hour + 12
        candidates = [
            datetime.combine(target_date, time(am_hour, minute), SHANGHAI),
            datetime.combine(target_date, time(pm_hour, minute), SHANGHAI),
        ]
        future = [value for value in candidates if value > now]
        if future:
            return {"status": "success", "value": min(future)}
        if allow_next_day:
            return {"status": "success", "value": candidates[0] + timedelta(days=1)}
        return self._failed("指定日期的执行时间已经过去")

    def _semantic_reminder(
        self, spec: dict, event_at: datetime, all_day: bool, now: datetime,
    ) -> dict:
        kind = str(spec.get("kind") or "none")
        try:
            if kind == "incomplete":
                return self._failed(str(spec.get("reason") or "提醒信息不完整"))
            if kind == "none":
                value = None if all_day else event_at
            elif kind == "at_event":
                value = event_at
            elif kind == "previous_day":
                value = event_at - timedelta(days=1)
            elif kind == "before_event":
                amount = self._semantic_int(spec, "value", 1, 100000)
                unit = str(spec.get("unit") or "")
                factors = {"minutes": 1, "hours": 60, "days": 1440}
                if unit not in factors:
                    return self._failed("提前提醒单位只支持 minutes、hours 或 days")
                value = event_at - timedelta(minutes=amount * factors[unit])
            elif kind == "clock":
                clock = self._semantic_clock(spec, event_at.date(), now)
                if clock["status"] != "success":
                    return clock
                value = clock["value"]
            else:
                return self._failed("不支持的提醒语义类型：%s" % kind)
        except (TypeError, ValueError) as exc:
            return self._failed(str(exc))
        if value is None:
            return {"status": "success", "value": None}
        if value <= now:
            return self._failed("提醒时间已经过去")
        if value > event_at and not all_day:
            return self._failed("提醒时间不能晚于执行时间")
        return {"status": "success", "value": value}

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

    @staticmethod
    def _apply_daypart(hour: int, daypart: str) -> int:
        if daypart in {"afternoon", "evening", "night"}:
            return hour + 12 if 1 <= hour < 12 else hour
        if daypart == "noon":
            return hour + 12 if 1 <= hour <= 10 else hour
        if daypart in {"dawn", "morning"} and hour == 12:
            return 0
        return hour
