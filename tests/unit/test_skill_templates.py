"""Skills must not drift from the backend: their JSON templates validate against the real models and their
reference links resolve. A fresh session copies these templates verbatim."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

from tests.conftest import REPO
from trader.domain import DecisionReport
from trader.learning import PostTradeReview

SKILLS = REPO / ".claude" / "skills"


def json_block(path: Path) -> dict:
    match = re.search(r"```json\n(.*?)\n```", path.read_text(encoding="utf-8"), re.DOTALL)
    assert match, f"no json block in {path}"
    return json.loads(match.group(1))


def test_decision_report_template_is_valid_and_conservative() -> None:
    report = DecisionReport.model_validate(
        json_block(SKILLS / "analyze-symbol" / "references" / "decision-report-template.md")
    )
    assert report.decision.value in {"NO_TRADE", "WATCH"}
    assert report.expected_net_return is None and report.human_approval_required


def test_tool_description_example_is_valid() -> None:
    """The record_decision_report tool description is what a model reads right before calling it."""
    from trader.mcp_server import record_decision_report

    doc = " ".join((record_decision_report.__doc__ or "").split())
    example = doc.split("Minimal example: ")[1].split(" Full template:")[0]
    DecisionReport.model_validate(json.loads(example))


def test_review_template_is_valid() -> None:
    PostTradeReview.model_validate(
        json_block(SKILLS / "post-trade-review" / "references" / "review-template.md")
    )


@pytest.mark.parametrize("skill", sorted(p.name for p in SKILLS.iterdir() if p.is_dir()))
def test_skill_frontmatter_and_links(skill: str) -> None:
    text = (SKILLS / skill / "SKILL.md").read_text(encoding="utf-8")
    front = yaml.safe_load(text.split("---")[1])
    assert front["name"] == skill and len(front["description"]) > 40
    assert len(text.splitlines()) < 500  # progressive disclosure: details belong in references/
    for target in re.findall(r"\]\(([^)#]+\.md)\)", text):
        assert (SKILLS / skill / target).resolve().exists(), f"{skill}: broken link {target}"


def test_documented_ask_tools_match_settings() -> None:
    settings = json.loads((REPO / ".claude" / "settings.json").read_text(encoding="utf-8"))
    ask = {rule.removeprefix("mcp__trading__") for rule in settings["permissions"]["ask"]}
    doc = (REPO / "docs" / "operating-the-trading-desk.md").read_text(encoding="utf-8")
    section = doc.split("## 4.")[1].split("## 5.")[0]
    assert {name for name in re.findall(r"`([a-z_]+)`", section) if name in ask or "_" in name} >= ask
