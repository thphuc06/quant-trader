"""Knowledge base: markdown files with YAML front matter, keyword retrieval, lesson lifecycle.

Statuses: unverified < candidate < reviewed < approved; deprecated is excluded from search by default.
Only ``reviewed``/``approved`` documents may be cited as rules (enforced by skills; surfaced here).
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from trader.domain import new_id
from trader.timeutil import utc_now

STATUSES = ("unverified", "candidate", "reviewed", "approved", "deprecated")
CITABLE = ("reviewed", "approved")
_TOKEN = re.compile(r"[a-z0-9_]{2,}")
_FRONT = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.DOTALL)


@dataclass
class KnowledgeDoc:
    doc_id: str
    title: str
    type: str
    status: str
    tags: list[str]
    path: str
    body: str
    meta: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.doc_id,
            "title": self.title,
            "type": self.type,
            "status": self.status,
            "citable_as_rule": self.status in CITABLE,
            "tags": self.tags,
            "confidence": self.meta.get("confidence", "unknown"),
            "updated": str(self.meta.get("updated", self.meta.get("created", "unknown"))),
            "path": self.path,
        }


def parse_document(path: Path, root: Path) -> KnowledgeDoc | None:
    match = _FRONT.match(path.read_text(encoding="utf-8"))
    if not match:
        return None
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        return None
    if not isinstance(meta, dict) or "id" not in meta:
        return None
    status = str(meta.get("status", "unverified"))
    return KnowledgeDoc(
        doc_id=str(meta["id"]),
        title=str(meta.get("title", path.stem)),
        type=str(meta.get("type", "note")),
        status=status if status in STATUSES else "unverified",
        tags=[str(t).lower() for t in (meta.get("tags") or [])],
        path=path.relative_to(root).as_posix(),
        body=match.group(2).strip(),
        meta=meta,
    )


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


class KnowledgeStore:
    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)

    def documents(self) -> list[KnowledgeDoc]:
        docs: list[KnowledgeDoc] = []
        if not self.directory.exists():
            return docs
        for path in sorted(self.directory.rglob("*.md")):
            if path.name.lower() == "readme.md":
                continue
            doc = parse_document(path, self.directory)
            if doc is not None:
                docs.append(doc)
        return docs

    def get(self, doc_id: str) -> KnowledgeDoc | None:
        return next((d for d in self.documents() if d.doc_id == doc_id), None)

    def index(self) -> dict[str, Any]:
        docs = self.documents()
        return {
            "documents": len(docs),
            "by_status": dict(Counter(d.status for d in docs)),
            "by_type": dict(Counter(d.type for d in docs)),
            "items": [d.summary() for d in docs],
        }

    def search(
        self,
        query: str,
        *,
        doc_type: str | None = None,
        min_status: str | None = None,
        tags: list[str] | None = None,
        include_deprecated: bool = False,
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        """Keyword search: title and tag matches weigh more than body term frequency."""
        terms = _tokens(query)
        floor = STATUSES.index(min_status) if min_status in STATUSES[:4] else 0
        wanted_tags = {t.lower() for t in (tags or [])}
        results = []
        for doc in self.documents():
            if doc.status == "deprecated" and not include_deprecated:
                continue
            if doc.status != "deprecated" and STATUSES.index(doc.status) < floor:
                continue
            if doc_type and doc.type != doc_type:
                continue
            if wanted_tags and not wanted_tags & set(doc.tags):
                continue
            title_tokens, body_counts = set(_tokens(doc.title)), Counter(_tokens(doc.body))
            score = 0.0
            for term in terms:
                score += (
                    3.0 * (term in title_tokens)
                    + 2.0 * (term in doc.tags)
                    + math.log1p(body_counts.get(term, 0))
                )
            if score > 0 or not terms:
                excerpt = _excerpt(doc.body, terms)
                results.append({**doc.summary(), "score": round(score, 3), "excerpt": excerpt})
        results.sort(key=lambda r: (-r["score"], r["id"]))
        return results[:limit]

    # ------------------------------------------------------------------ lessons

    def create_candidate_lesson(
        self,
        *,
        title: str,
        claim: str,
        scope: str,
        supporting_cases: list[str],
        contradicting_cases: list[str],
        author: str,
    ) -> KnowledgeDoc:
        lesson_id = new_id("lesson")
        folder = self.directory / "lessons_pending_review"
        folder.mkdir(parents=True, exist_ok=True)
        today = utc_now().date().isoformat()
        meta = {
            "id": lesson_id,
            "title": title[:120],
            "type": "lesson",
            "status": "candidate",
            "tags": ["lesson", "pending-review"],
            "created": today,
            "updated": today,
            "author": author,
            "confidence": "low",
            "sample_size": len(supporting_cases),
            "supporting_cases": supporting_cases,
            "contradicting_cases": contradicting_cases,
            "applicability": scope[:300],
            "validation": {
                "experiment_id": None,
                "out_of_sample": "not_tested",
                "paper_validation": "not_tested",
            },
        }
        body = (
            f"## Claim (hypothesis, not a rule)\n\n{claim.strip()}\n\n"
            "## Why this is only a candidate\n\n"
            f"Drafted from {len(supporting_cases)} trade case(s). A handful of trades cannot establish a rule. "
            "It must be tested as a research hypothesis (experiment registry), then reviewed by the operator and "
            "promoted with `python -m trader lesson promote`.\n"
        )
        path = folder / f"{lesson_id}.md"
        path.write_text(
            "---\n" + yaml.safe_dump(meta, sort_keys=False, allow_unicode=True) + "---\n\n" + body,
            encoding="utf-8",
        )
        doc = parse_document(path, self.directory)
        assert doc is not None
        return doc

    def promote_lesson(self, lesson_id: str, experiment_id: str, reviewer: str) -> KnowledgeDoc:
        """Human-only (called from the TTY-gated CLI). Requires an experiment as evidence."""
        doc = self.get(lesson_id)
        if doc is None or doc.type != "lesson":
            raise ValueError(f"no lesson with id {lesson_id}")
        if doc.status != "candidate":
            raise ValueError(f"lesson is {doc.status}, only candidates can be promoted")
        if not experiment_id:
            raise ValueError("an experiment id is required as validation evidence")
        meta = dict(doc.meta)
        meta["status"] = "approved"
        meta["updated"] = utc_now().date().isoformat()
        meta["tags"] = [t for t in doc.tags if t != "pending-review"]
        meta["validation"] = {
            **(meta.get("validation") or {}),
            "experiment_id": experiment_id,
            "reviewed_by": reviewer,
        }
        source = self.directory / doc.path
        target_dir = self.directory / "approved_lessons"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / source.name
        target.write_text(
            "---\n" + yaml.safe_dump(meta, sort_keys=False, allow_unicode=True) + "---\n\n" + doc.body + "\n",
            encoding="utf-8",
        )
        source.unlink()
        promoted = parse_document(target, self.directory)
        assert promoted is not None
        return promoted


def _excerpt(body: str, terms: list[str], width: int = 240) -> str:
    lowered = body.lower()
    position = min((lowered.find(t) for t in terms if t in lowered), default=0)
    start = max(0, position - 60)
    return re.sub(r"\s+", " ", body[start : start + width]).strip()
