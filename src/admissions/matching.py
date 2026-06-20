"""Сопоставление входящей записи с уже имеющимися абитуриентами.

Каскад (порядок и пороги — в config/settings.yaml -> matching):
  0. Собственный ID источника (onec_id / bitrix_id) — для обновления той же записи.
  1. exact_id      — точное совпадение по указанным каноническим полям.
  2. exact_contact — точное совпадение по email / телефону.
  3. fuzzy_name_dob — нечёткое ФИО + дата рождения (rapidfuzz).

Принцип безопасности: неоднозначные совпадения НЕ сливаются автоматически,
а помечаются решением 'review' для ручной проверки.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from rapidfuzz import fuzz

from .config import Config
from .db import Database
from .models import SOURCE_1C, ApplicantRecord
from .normalize import full_name_key

DECISION_MATCH = "match"
DECISION_NEW = "new"
DECISION_REVIEW = "review"


@dataclass
class MatchResult:
    decision: str                       # match | new | review
    applicant_id: Optional[int] = None
    method: str = ""
    score: float = 0.0
    candidate_ids: List[int] = field(default_factory=list)
    reason: str = ""


def _own_id_field(source: str) -> str:
    return "onec_id" if source == SOURCE_1C else "bitrix_id"


def match_record(db: Database, cfg: Config, record: ApplicantRecord) -> MatchResult:
    """Определить, к какому абитуриенту относится запись (или что она новая)."""
    mcfg = cfg.settings.get("matching", {})

    # 0. Та же запись из того же источника -> обновляем существующего абитуриента.
    own_field = _own_id_field(record.source)
    if record.source_key:
        rows = db.find_by_field(own_field, record.source_key)
        if rows:
            return MatchResult(
                DECISION_MATCH, rows[0]["id"], "source_id", 100.0,
                [r["id"] for r in rows], "совпадение по собственному ID источника",
            )

    # 1..N. Каскад из конфига.
    for step in mcfg.get("cascade", []):
        method = step.get("method")
        if method == "exact_id":
            result = _match_exact_fields(db, record, step.get("fields", []), method)
            if result:
                return result
        elif method == "exact_contact":
            result = _match_exact_fields(
                db, record, step.get("fields", ["email", "phone"]), "exact_contact"
            )
            if result:
                return result
        elif method == "fuzzy_name_dob":
            result = _match_fuzzy(db, record, step, mcfg)
            if result:
                return result

    return MatchResult(DECISION_NEW, reason="новая запись")


def _match_exact_fields(
    db: Database, record: ApplicantRecord, fields: List[str], method: str
) -> Optional[MatchResult]:
    """Точное совпадение по любому из полей. Несколько разных кандидатов -> review."""
    ids: set = set()
    matched_field = None
    for fld in fields:
        value = record.get(fld)
        if not value:
            continue
        for row in db.find_by_field(fld, value):
            ids.add(row["id"])
            matched_field = fld
    if not ids:
        return None
    sorted_ids = sorted(ids)
    if len(sorted_ids) == 1:
        return MatchResult(
            DECISION_MATCH, sorted_ids[0], f"{method}:{matched_field}", 100.0,
            sorted_ids, f"точное совпадение по «{matched_field}»",
        )
    return MatchResult(
        DECISION_REVIEW, None, method, 100.0, sorted_ids,
        f"совпадение по «{method}» сразу с несколькими карточками: {sorted_ids}",
    )


def _match_fuzzy(db, record, step, mcfg) -> Optional[MatchResult]:
    """Нечёткое сопоставление по ФИО (+ дата рождения)."""
    key = full_name_key(
        record.get("last_name"), record.get("first_name"), record.get("middle_name")
    )
    if not key:
        return None

    threshold = step.get("name_threshold", 90)
    require_dob = step.get("require_dob_match", True)
    review_threshold = mcfg.get("review_threshold", 75)
    margin = mcfg.get("ambiguity_margin", 5)
    dob = record.get("birth_date")

    scored = []
    for a in db.all_applicants():
        if require_dob and dob:
            if a["birth_date"] != dob:   # нет ДР у кандидата или не совпала -> пропуск
                continue
        akey = full_name_key(a["last_name"], a["first_name"], a["middle_name"])
        if not akey:
            continue
        score = fuzz.token_sort_ratio(key, akey)
        scored.append((score, a["id"]))

    if not scored:
        return None
    scored.sort(reverse=True)
    best_score, best_id = scored[0]

    if best_score >= threshold:
        close = [aid for s, aid in scored if s >= best_score - margin]
        if len(close) > 1:
            return MatchResult(
                DECISION_REVIEW, None, "fuzzy_name_dob", best_score, close,
                f"несколько похожих ФИО (score≈{best_score:.0f}): {close}",
            )
        return MatchResult(
            DECISION_MATCH, best_id, "fuzzy_name_dob", best_score, [best_id],
            f"нечёткое совпадение ФИО (score={best_score:.0f})",
        )

    if best_score >= review_threshold:
        return MatchResult(
            DECISION_REVIEW, None, "fuzzy_name_dob", best_score, [best_id],
            f"похоже на существующую карточку, но ниже порога (score={best_score:.0f})",
        )
    return None
