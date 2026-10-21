"""Gia's stage: the moment chosen for each chat state, and the HTML it renders."""
import re
import unittest
from types import SimpleNamespace as E

from goodfoods import mascot


def tool(name, result=None, error=False):
    return E(kind="tool", name=name, detail={"result": result or {}, "error": error})


class ChooseMoment(unittest.TestCase):
    def test_journey(self):
        c = mascot.choose_moment
        self.assertEqual(c("start"), "greeting")
        self.assertEqual(c("options", [tool("recommend_restaurants", {"recommendations": [{}]})]), "options")
        self.assertEqual(c("options", []), "suggestions", "talking about live cards = advising")
        self.assertEqual(c("details"), "picked")
        self.assertEqual(c("review", pending_tool="make_reservation"), "confirm")
        self.assertEqual(c("done", done_status="confirmed"), "booked")
        self.assertEqual(c("done", done_status="modified"), "updated")
        self.assertEqual(c("done", done_status="cancelled"), "cancelled")
        self.assertEqual(c("manage"), "manage")
        self.assertEqual(c("manage", [tool("lookup_reservation", {"reservations": []})]), "found")
        self.assertEqual(c("modify"), "modify")

    def test_problems(self):
        c = mascot.choose_moment
        self.assertEqual(c("chat", [E(kind="llm", name="error", detail={})]), "sorry")
        full = tool("check_availability", {"error": "NOT_AVAILABLE"}, error=True)
        self.assertEqual(c("chat", [full]), "full")
        self.assertEqual(c("chat", [], reply="Which day would you like?"), "thinking")
        self.assertEqual(c("chat", [], reply="Done."), "idle")

    def test_pending_wins_over_everything_but_start(self):
        self.assertEqual(mascot.choose_moment("chat", [E(kind="llm", name="error", detail={})],
                                              pending_tool="cancel_reservation"), "confirm")


class StageHtml(unittest.TestCase):
    def test_every_moment_renders(self):
        for name, m in mascot.MOMENTS.items():
            h = mascot.stage_html(name, base="app/static/gia")
            self.assertTrue(h.startswith(f'<div class="gia-stage" data-moment="{name}"'), name)
            self.assertIn(f'src="app/static/gia/{m.pose}.png"', h)
            self.assertIn(f'class="gia-pose p-{m.pose}', h)

    def test_no_css_variables_inline(self):
        # Streamlit's markdown may drop custom properties in style="", so they must stay in the stylesheet.
        for name in mascot.MOMENTS:
            self.assertIsNone(re.search(r'style="[^"]*--', mascot.stage_html(name)), name)

    def test_booked_settles_into_relaxed_pose(self):
        h = mascot.stage_html("booked")
        self.assertIn("happy.png", h)
        self.assertIn("travel.png", h)
        confetti = h.split('class="gia-confetti"', 1)[1].split("</div>", 1)[0]
        self.assertEqual(confetti.count("<i></i>"), mascot.CONFETTI_PIECES)

    def test_line_is_escaped(self):
        h = mascot.stage_html("thinking", line='<script>alert(1)</script> "hi"')
        self.assertNotIn("<script>", h)

    def test_every_pose_has_a_size_and_image(self):
        import pathlib
        for pose in mascot.POSE_HEIGHT:
            self.assertIn(f".gia-pose.p-{pose} ", mascot.STAGE_CSS)
            self.assertTrue((pathlib.Path(__file__).parent.parent / "static" / "gia" / f"{pose}.png").exists(), pose)


if __name__ == "__main__":
    unittest.main()
