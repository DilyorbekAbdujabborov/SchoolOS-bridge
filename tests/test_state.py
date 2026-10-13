import tempfile
import unittest
from pathlib import Path

from schoolos_bridge.state import State


class StateTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "sub" / "state.json"

    def test_fresh_state_is_empty(self):
        state = State(self.path)
        self.assertIsNone(state.cursor("S1"))
        self.assertEqual(state.outbox, [])

    def test_round_trip_survives_reload(self):
        state = State(self.path)
        state.set_cursor("S1", 128303, "2026-10-10T08:57:36+05:00")
        state.outbox.append({"osid": "1"})
        state.save()
        again = State(self.path)
        self.assertEqual(again.cursor("S1"), 128303)
        self.assertEqual(again.since("S1"), "2026-10-10T08:57:36+05:00")
        self.assertEqual(again.outbox, [{"osid": "1"}])


class FaceVersionStateTests(unittest.TestCase):
    def test_face_versions_survive_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            state = State(path)
            state.set_face("S1", "1000000001", "abc")
            state.set_face("S1", "1000000002", "def")
            state.drop_face("S1", "1000000002")
            state.save()
            again = State(path)
            self.assertEqual(again.face("S1", "1000000001"), "abc")
            self.assertIsNone(again.face("S1", "1000000002"))
            self.assertIsNone(again.face("S2", "1000000001"))
