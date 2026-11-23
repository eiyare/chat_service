"""Use cases shown on the public site (/use-cases/). Each entry is one page.

To add a use case: add an entry here and a template at portal/use_cases/<slug>.html that
extends portal/use_cases/_page.html (see moodle.html). The index page, the URLs and the
landing page's "Use cases" section all come from this list, in this order.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class UseCase:
    slug: str
    name: str         # short label, e.g. for the index card
    audience: str     # who it's for
    headline: str     # one line for the index card
    summary: str      # two lines for the index card
    icon: str         # SVG path data (24x24 viewBox, stroked)

    @property
    def template(self) -> str:
        return f"portal/use_cases/{self.slug}.html"


USE_CASES = [
    UseCase(
        slug="moodle",
        name="Schools on Moodle",
        audience="Principals, heads of e-learning and Moodle administrators",
        headline="A course assistant inside every Moodle course",
        summary="Answers students from their own course, knows their grades, completion and deadlines, "
                "and stays out of quizzes.",
        icon="M3 9l9-5 9 5-9 5z M7 11.5V16c0 1.5 2.5 3 5 3s5-1.5 5-3v-4.5 M21 9v5",
    ),
]

BY_SLUG = {u.slug: u for u in USE_CASES}
