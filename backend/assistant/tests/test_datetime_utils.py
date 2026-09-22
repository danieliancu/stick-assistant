from datetime import date, datetime, time, timedelta, timezone

from django.test import SimpleTestCase

from assistant.services import datetime_utils as dtu
from assistant.tests.helpers import FIXED_NOW, fixed_clock

TUESDAY = date(2026, 9, 22)


class RelativeDayTests(SimpleTestCase):
    def test_today_and_tomorrow(self):
        for expr in ("azi", "astăzi", "today"):
            self.assertEqual(dtu.resolve_relative_day(expr, TUESDAY).start, TUESDAY)
        for expr in ("mâine", "maine", "Tomorrow"):
            r = dtu.resolve_relative_day(expr, TUESDAY)
            self.assertEqual((r.start, r.end), (date(2026, 9, 23), date(2026, 9, 23)))
        self.assertEqual(dtu.resolve_relative_day("poimâine", TUESDAY).start, date(2026, 9, 24))

    def test_bare_weekday_is_next_occurrence(self):
        self.assertEqual(dtu.resolve_relative_day("vineri", TUESDAY).start, date(2026, 9, 25))
        self.assertEqual(dtu.resolve_relative_day("Friday", TUESDAY).start, date(2026, 9, 25))
        # Same weekday as today -> one week later, never today.
        self.assertEqual(dtu.resolve_relative_day("marți", TUESDAY).start, date(2026, 9, 29))
        self.assertEqual(dtu.resolve_relative_day("luni", TUESDAY).start, date(2026, 9, 28))

    def test_next_weekday_means_next_week(self):
        self.assertEqual(dtu.resolve_relative_day("next Friday", TUESDAY).start, date(2026, 10, 2))
        self.assertEqual(dtu.resolve_relative_day("vinerea viitoare", TUESDAY).start,
                         date(2026, 10, 2))
        self.assertEqual(dtu.resolve_relative_day("lunea viitoare", TUESDAY).start,
                         date(2026, 9, 28))
        self.assertEqual(dtu.resolve_relative_day("sâmbăta viitoare", TUESDAY).start,
                         date(2026, 10, 3))

    def test_next_week_range(self):
        for expr in ("săptămâna viitoare", "saptamana viitoare", "next week"):
            r = dtu.resolve_relative_day(expr, TUESDAY)
            self.assertEqual((r.start, r.end), (date(2026, 9, 28), date(2026, 10, 4)))

    def test_from_sunday(self):
        sunday = date(2026, 9, 27)
        self.assertEqual(dtu.resolve_relative_day("luni", sunday).start, date(2026, 9, 28))
        self.assertEqual(dtu.resolve_relative_day("lunea viitoare", sunday).start,
                         date(2026, 9, 28))

    def test_unknown_expression(self):
        with self.assertRaises(dtu.DateTimeValidationError):
            dtu.resolve_relative_day("cândva", TUESDAY)


class ParsingTests(SimpleTestCase):
    def test_parse_valid(self):
        self.assertEqual(dtu.parse_local_date("2026-09-25"), date(2026, 9, 25))
        self.assertEqual(dtu.parse_local_time("14:30"), time(14, 30))
        self.assertEqual(dtu.parse_local_time("9:05"), time(9, 5))

    def test_parse_invalid(self):
        for value in ("25/09/2026", "2026-02-30", "2026-13-01", "", "tomorrow", None, 20260925):
            with self.assertRaises(dtu.DateTimeValidationError):
                dtu.parse_local_date(value)
        for value in ("24:00", "10:60", "10", "10am", "", None):
            with self.assertRaises(dtu.DateTimeValidationError):
                dtu.parse_local_time(value)


class TimezoneTests(SimpleTestCase):
    def test_now_local_uses_clock(self):
        now = dtu.now_local(fixed_clock())
        self.assertEqual((now.date(), now.hour), (TUESDAY, 10))  # BST = UTC+1

    def test_now_local_rejects_naive_clock(self):
        with self.assertRaises(dtu.DateTimeValidationError):
            dtu.now_local(lambda: datetime(2026, 9, 22, 9, 0))

    def test_bst_and_gmt_offsets(self):
        summer = dtu.make_local_aware(date(2026, 7, 1), time(10, 0))
        winter = dtu.make_local_aware(date(2026, 12, 1), time(10, 0))
        self.assertEqual(summer.utcoffset(), timedelta(hours=1))
        self.assertEqual(winter.utcoffset(), timedelta(hours=0))
        self.assertEqual(summer.astimezone(timezone.utc).hour, 9)
        self.assertEqual(winter.astimezone(timezone.utc).hour, 10)

    def test_spring_forward_gap_rejected(self):
        # Clocks go forward at 01:00 GMT on 29 March 2026; 01:30 does not exist.
        with self.assertRaises(dtu.DateTimeValidationError):
            dtu.make_local_aware(date(2026, 3, 29), time(1, 30))
        self.assertEqual(dtu.make_local_aware(date(2026, 3, 29), time(2, 30)).utcoffset(),
                         timedelta(hours=1))

    def test_autumn_fold_uses_first_occurrence(self):
        # Clocks go back at 02:00 BST on 25 October 2026; 01:30 happens twice.
        value = dtu.make_local_aware(date(2026, 10, 25), time(1, 30))
        self.assertEqual(value.utcoffset(), timedelta(hours=1))
        self.assertEqual(value.astimezone(timezone.utc), datetime(2026, 10, 25, 0, 30,
                                                                  tzinfo=timezone.utc))

    def test_dst_day_bounds(self):
        # Compare in UTC: subtracting datetimes that share a tzinfo gives wall-clock time.
        def elapsed(bounds):
            start, end = bounds
            return end.astimezone(timezone.utc) - start.astimezone(timezone.utc)

        self.assertEqual(elapsed(dtu.local_day_bounds(date(2026, 3, 29))), timedelta(hours=23))
        self.assertEqual(elapsed(dtu.local_day_bounds(date(2026, 10, 25))), timedelta(hours=25))

    def test_format_local(self):
        value = datetime(2026, 12, 25, 14, 30, tzinfo=timezone.utc)
        self.assertEqual(dtu.format_local(value), "Friday 25/12/2026 14:30")
        self.assertEqual(dtu.format_local(value, has_time=False), "Friday 25/12/2026")
        self.assertIsNone(dtu.format_local(None))


class CalendarContextTests(SimpleTestCase):
    def test_context_contains_real_date_and_timezone(self):
        text = dtu.build_calendar_context(FIXED_NOW)
        self.assertIn("2026-09-22 (Tuesday / marți)", text)
        self.assertIn("Current local time: 10:00", text)
        self.assertIn("Europe/London, currently BST (UTC+1)", text)
        self.assertIn("2026-09-23 Wednesday / miercuri (tomorrow / mâine)", text)
        self.assertIn("Next week (Monday-Sunday): 2026-09-28 to 2026-10-04", text)

    def test_context_in_winter_is_gmt(self):
        text = dtu.build_calendar_context(datetime(2026, 12, 1, 9, 0, tzinfo=timezone.utc))
        self.assertIn("currently GMT (UTC+0)", text)
        self.assertIn("Current local time: 09:00", text)
