import unittest
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import heartbeat as hb

TZ = ZoneInfo("Asia/Shanghai")


def at(h, m, s=0, day=1):
    return datetime(2026, 1, day, h, m, s, tzinfo=TZ)


class WindowTest(unittest.TestCase):
    def test_cross_midnight_window(self):
        self.assertTrue(hb.in_window(dtime(8, 30)))
        self.assertTrue(hb.in_window(dtime(23, 0)))
        self.assertTrue(hb.in_window(dtime(1, 59)))
        self.assertTrue(hb.in_window(dtime(2, 0)))
        self.assertFalse(hb.in_window(dtime(2, 1)))
        self.assertFalse(hb.in_window(dtime(8, 29)))

    def test_same_day_window(self):
        cfg = hb.Config(window_start=dtime(9, 0), window_end=dtime(22, 0))
        self.assertTrue(hb.in_window(dtime(12, 0), cfg))
        self.assertFalse(hb.in_window(dtime(23, 0), cfg))
        self.assertFalse(hb.in_window(dtime(8, 0), cfg))

    def test_next_window_start(self):
        self.assertEqual(hb.next_window_start(at(3, 0)), at(8, 30))
        self.assertEqual(hb.next_window_start(at(8, 30)), at(8, 30, day=2))


class DelayATest(unittest.TestCase):
    def test_daytime_full_interval(self):
        self.assertEqual(hb.delay_a(at(12, 0)), 55 * 60)

    def test_night_waits_for_window_start(self):
        self.assertEqual(hb.delay_a(at(3, 0)), int(5.5 * 3600))

    def test_just_before_window_aligns_to_start(self):
        # 08:29 加 55 分钟虽然落在白天，但现在本身在夜里，应该 08:30 准点发
        self.assertEqual(hb.delay_a(at(8, 29)), 60)

    def test_interval_crossing_into_night(self):
        # 01:30 加 55 分钟是 02:25，已经是夜里，等到 08:30
        self.assertEqual(hb.delay_a(at(1, 30)), 7 * 3600)

    def test_sub_second_never_zero(self):
        now = datetime(2026, 1, 1, 8, 29, 59, 700000, tzinfo=TZ)
        self.assertEqual(hb.delay_a(now), 1)


class DelayBTest(unittest.TestCase):
    def test_daytime_uses_interval(self):
        self.assertEqual(hb.delay_b(at(12, 0), 10 * 60), 10 * 60)

    def test_night_cut_at_window_start(self):
        # 07:00 想等 180 分钟，落点越过 08:30，截到 08:30
        self.assertEqual(hb.delay_b(at(7, 0), 180 * 60), 90 * 60)

    def test_night_within_range(self):
        self.assertEqual(hb.delay_b(at(3, 0), 60 * 60), 60 * 60)


class TagTest(unittest.TestCase):
    def test_tag_removed_and_used_in_b(self):
        reply, interval = hb.pop_tag("在干嘛\n[心跳:20]", "B", at(12, 0))
        self.assertEqual(reply, "在干嘛")
        self.assertEqual(interval, 20 * 60)

    def test_tag_removed_but_ignored_in_a(self):
        reply, interval = hb.pop_tag("在干嘛\n[心跳:20]", "A", at(12, 0))
        self.assertEqual(reply, "在干嘛")
        self.assertEqual(interval, 55 * 60)

    def test_variants(self):
        for tag in ("[心跳：20]", "[heartbeat:20]", "[Heartbeat: 20]", "  [心跳:20]  "):
            reply, interval = hb.pop_tag("hi\n" + tag, "B", at(12, 0))
            self.assertEqual((reply, interval), ("hi", 20 * 60), tag)

    def test_no_tag(self):
        self.assertEqual(hb.pop_tag("hi", "B", at(12, 0)), ("hi", 55 * 60))

    def test_tag_not_on_last_line_is_kept(self):
        text = "[心跳:20]\nhi"
        self.assertEqual(hb.pop_tag(text, "B", at(12, 0)), (text, 55 * 60))

    def test_clamped_day_and_night(self):
        self.assertEqual(hb.pop_tag("x\n[心跳:1]", "B", at(12, 0))[1], 5 * 60)
        self.assertEqual(hb.pop_tag("x\n[心跳:999]", "B", at(12, 0))[1], 55 * 60)
        self.assertEqual(hb.pop_tag("x\n[心跳:1]", "B", at(3, 0))[1], 30 * 60)
        self.assertEqual(hb.pop_tag("x\n[心跳:999]", "B", at(3, 0))[1], 180 * 60)

    def test_too_many_digits_not_a_tag(self):
        text = "x\n[心跳:12345]"
        self.assertEqual(hb.pop_tag(text, "B", at(12, 0)), (text, 55 * 60))


class SaneIntervalTest(unittest.TestCase):
    def test_bad_values_fall_back(self):
        for bad in (None, "10", True, -5, 0, float("nan"), float("inf"), 181 * 60):
            self.assertEqual(hb.sane_interval(bad), 55 * 60, bad)

    def test_good_value_kept(self):
        self.assertEqual(hb.sane_interval(600), 600)

    def test_next_delay_uses_fallback(self):
        self.assertEqual(hb.next_delay(at(12, 0), None, "B"), 55 * 60)
        self.assertEqual(hb.next_delay(at(12, 0), -1, "B"), 55 * 60)


class RestartTest(unittest.TestCase):
    def test_never_sent(self):
        self.assertEqual(hb.delay_on_restart(at(12, 0), None), 55 * 60)

    def test_continues_from_last_sent(self):
        self.assertEqual(hb.delay_on_restart(at(12, 0), at(11, 30)), 25 * 60)

    def test_overdue_sends_now(self):
        self.assertEqual(hb.delay_on_restart(at(12, 0), at(10, 0)), 1)

    def test_overdue_at_night_waits_in_a(self):
        self.assertEqual(hb.delay_on_restart(at(3, 0), at(1, 0)), int(5.5 * 3600))

    def test_overdue_at_night_sends_in_b(self):
        self.assertEqual(hb.delay_on_restart(at(3, 0), at(1, 0), mode="B"), 1)

    def test_repeated_restarts_do_not_push_back(self):
        last = at(11, 0)
        first = hb.delay_on_restart(at(11, 30), last)
        second = hb.delay_on_restart(at(11, 40), last)
        self.assertEqual(at(11, 30) + timedelta(seconds=first), at(11, 40) + timedelta(seconds=second))


class HintTest(unittest.TestCase):
    def test_hint_mentions_ranges(self):
        text = hb.b_hint()
        self.assertIn("[心跳:N]", text)
        self.assertIn("08:30–02:00 为 5–55", text)
        self.assertIn("30–180", text)


if __name__ == "__main__":
    unittest.main()
