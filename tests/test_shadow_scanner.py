"""
Unit tests for the pure logic in shadow_scanner.

Covers stage detection, conflict detection, and coach sort priority.
No Google Calendar / Slack / filesystem side effects.

Run:
    python3 -m unittest discover tests -v
"""

import sys
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import shadow_scanner as ss


# ---------------------------------------------------------------------------
# Stage matcher
# ---------------------------------------------------------------------------

class StageMatcher(unittest.TestCase):
    def setUp(self):
        self.cfg = {
            "stages": [
                {"id": "intake", "label": "S1: Intake", "keywords": ["intake"]},
                {"id": "lab_review", "label": "S2: Lab Review", "keywords": ["lab review", "lab"]},
                {"id": "provider_exam", "label": "S3: Provider Exam", "keywords": ["provider exam", "patient exam"]},
            ],
            "default_stage": "intake",
        }
        self.detect, self.labels = ss.build_stage_matcher(self.cfg)

    def test_matches_by_title(self):
        self.assertEqual(self.detect("Intake: John Doe"), "intake")
        self.assertEqual(self.detect("Lab Review — Jane"), "lab_review")
        self.assertEqual(self.detect("Provider Exam"), "provider_exam")

    def test_case_insensitive(self):
        self.assertEqual(self.detect("INTAKE CALL"), "intake")
        self.assertEqual(self.detect("lab review meeting"), "lab_review")

    def test_specificity_longer_keyword_wins(self):
        # 'provider exam' should match before 'lab' (both are substrings of
        # 'lab exam' / 'provider exam' respectively), validating that longer
        # keywords outrank shorter ones.
        self.assertEqual(self.detect("Provider Exam follow-up"), "provider_exam")
        self.assertEqual(self.detect("Patient Exam notes"), "provider_exam")

    def test_falls_back_to_event_name_line(self):
        # Title has nothing useful; description carries 'Event Name: Lab Review'
        title = "Zoom meeting with Jane"
        desc = "Some preamble\nEvent Name: Lab Review for Jane\nLink: ..."
        self.assertEqual(self.detect(title, desc), "lab_review")

    def test_default_stage_when_no_match(self):
        self.assertEqual(self.detect("Random meeting"), "intake")

    def test_no_default_returns_none(self):
        cfg = {**self.cfg}
        cfg.pop("default_stage")
        detect, _ = ss.build_stage_matcher(cfg)
        self.assertIsNone(detect("Random meeting"))

    def test_empty_stages_returns_none(self):
        detect, _ = ss.build_stage_matcher({"stages": []})
        self.assertIsNone(detect("Intake"))

    def test_labels_returned(self):
        self.assertEqual(self.labels["intake"], "S1: Intake")
        self.assertEqual(self.labels["provider_exam"], "S3: Provider Exam")


# ---------------------------------------------------------------------------
# Conflict detection
# ---------------------------------------------------------------------------

class ConflictDetection(unittest.TestCase):
    def setUp(self):
        self.shadows = [
            (datetime(2026, 4, 21, 12, 45, tzinfo=ZoneInfo("America/Chicago")), "Carey"),
            (datetime(2026, 4, 23, 16, 0, tzinfo=ZoneInfo("America/Chicago")), "Sawyer"),
        ]

    def test_conflict_within_window(self):
        # 30 min after an existing shadow — inside 45-min window.
        t, coach = ss.find_shadow_conflict("2026-04-21T13:15:00-05:00", self.shadows)
        self.assertIsNotNone(t)
        self.assertEqual(coach, "Carey")

    def test_no_conflict_outside_window(self):
        # 90 min after — outside 45-min window.
        t, coach = ss.find_shadow_conflict("2026-04-21T14:15:00-05:00", self.shadows)
        self.assertIsNone(t)
        self.assertIsNone(coach)

    def test_conflict_across_timezones(self):
        # 14:15 EDT = 13:15 CDT; existing shadow 12:45 CDT → 30 min apart.
        # Validates we compare absolute UTC timestamps, not naive clock times.
        t, coach = ss.find_shadow_conflict("2026-04-21T14:15:00-04:00", self.shadows)
        self.assertEqual(coach, "Carey")

    def test_empty_start_str(self):
        t, coach = ss.find_shadow_conflict("", self.shadows)
        self.assertIsNone(t)
        self.assertIsNone(coach)

    def test_empty_shadows(self):
        t, coach = ss.find_shadow_conflict("2026-04-21T13:15:00-05:00", [])
        self.assertIsNone(t)


# ---------------------------------------------------------------------------
# Coach sort priority
# ---------------------------------------------------------------------------

class CoachSortKey(unittest.TestCase):
    def test_never_shadowed_comes_first(self):
        state = {
            "shadowed_events": {
                "a@x.com::evt1": {"created_at": "2026-04-10T10:00:00"},
            }
        }
        never = {"coach_email": "b@x.com", "coach_name": "Bob"}
        once = {"coach_email": "a@x.com", "coach_name": "Alice"}
        entries = sorted([once, never], key=lambda e: ss.coach_sort_key(e, state))
        self.assertEqual(entries[0]["coach_name"], "Bob")

    def test_oldest_shadow_comes_before_newer(self):
        state = {
            "shadowed_events": {
                "a@x.com::e1": {"created_at": "2026-04-15T10:00:00"},
                "b@x.com::e1": {"created_at": "2026-04-01T10:00:00"},
            }
        }
        alice = {"coach_email": "a@x.com", "coach_name": "Alice"}
        bob = {"coach_email": "b@x.com", "coach_name": "Bob"}
        entries = sorted([alice, bob], key=lambda e: ss.coach_sort_key(e, state))
        self.assertEqual(entries[0]["coach_name"], "Bob")

    def test_alphabetical_tiebreak(self):
        state = {"shadowed_events": {}}
        entries = [
            {"coach_email": "c@x.com", "coach_name": "Carol"},
            {"coach_email": "a@x.com", "coach_name": "Alice"},
            {"coach_email": "b@x.com", "coach_name": "Bob"},
        ]
        entries.sort(key=lambda e: ss.coach_sort_key(e, state))
        self.assertEqual([e["coach_name"] for e in entries], ["Alice", "Bob", "Carol"])


# ---------------------------------------------------------------------------
# State (idempotent marking)
# ---------------------------------------------------------------------------

class StateMarking(unittest.TestCase):
    def test_mark_and_check(self):
        state = {"shadowed_events": {}}
        self.assertFalse(ss.event_already_shadowed(state, "a@x.com", "evt1"))
        ss.mark_event_shadowed(state, "a@x.com", "evt1", "shadow-1", "intake", "John")
        self.assertTrue(ss.event_already_shadowed(state, "a@x.com", "evt1"))

    def test_different_coach_same_event_id_not_a_match(self):
        state = {"shadowed_events": {}}
        ss.mark_event_shadowed(state, "a@x.com", "evt1", "s1", "intake", "John")
        self.assertFalse(ss.event_already_shadowed(state, "b@x.com", "evt1"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
