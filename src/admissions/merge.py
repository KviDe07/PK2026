"""Слияние записей в единые карточки и отслеживание статусов.

Один проход process_records() используется и для 1С, и для Битрикса:
сопоставляет каждую запись (matching.match_record), затем создаёт/обновляет
карточку, ведёт историю статусов и складывает спорные случаи в review_queue.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from .config import Config
from .db import Database
from .matching import (
    DECISION_MATCH,
    DECISION_NEW,
    DECISION_REVIEW,
    _own_id_field,
    match_record,
)
from .models import CANONICAL_FIELDS, ApplicantRecord

log = logging.getLogger("admissions")

# Поля, расхождение которых между источниками стоит вынести в review.
CONFLICT_FIELDS = ("email", "phone", "snils", "birth_date", "program")


def process_records(
    db: Database,
    cfg: Config,
    records: List[ApplicantRecord],
    source: str,
    file_name: Optional[str] = None,
    file_hash: Optional[str] = None,
    dry_run: bool = False,
) -> Dict[str, int]:
    """Обработать пачку записей одного источника. Возвращает статистику."""
    stats = {"total": 0, "new": 0, "updated": 0, "status_changes": 0,
             "review": 0, "conflicts": 0, "unchanged": 0}

    primary_source = (cfg.settings.get("source_priority") or [source])[0]
    run_id: Optional[int] = None
    if not dry_run:
        run_id = db.create_ingest_run(source, file_name, file_hash)

    for record in records:
        stats["total"] += 1
        result = match_record(db, cfg, record)

        if result.decision == DECISION_REVIEW:
            stats["review"] += 1
            if not dry_run:
                db.add_review_item(
                    "match", record.source, record.source_key, record.display_name,
                    result.reason, result.candidate_ids, result.score, ingest_run_id=run_id,
                )
                db.add_source_record(
                    None, record.source, record.source_key, record.status_raw,
                    record.status, record.fields, record.raw, run_id,
                )
            continue

        if result.decision == DECISION_NEW:
            stats["new"] += 1
            if not dry_run:
                _create_applicant(db, record, run_id)
            continue

        # DECISION_MATCH — обновляем существующую карточку
        changed_status = _update_applicant(
            db, cfg, record, result.applicant_id, primary_source, run_id, dry_run, stats
        )
        stats["updated"] += 1
        if changed_status:
            stats["status_changes"] += 1

    if not dry_run and run_id is not None:
        db.finish_ingest_run(
            run_id,
            records_total=stats["total"], records_new=stats["new"],
            records_updated=stats["updated"], status_changes=stats["status_changes"],
        )
    return stats


def _create_applicant(db: Database, record: ApplicantRecord, run_id: Optional[int]) -> int:
    data: Dict[str, Any] = dict(record.fields)
    data[_own_id_field(record.source)] = record.source_key
    data["current_status"] = record.status
    applicant_id = db.insert_applicant(data)
    db.add_status_history(applicant_id, record.status, None, record.source, "создана карточка")
    db.add_source_record(
        applicant_id, record.source, record.source_key, record.status_raw,
        record.status, record.fields, record.raw, run_id,
    )
    return applicant_id


def _update_applicant(
    db: Database, cfg: Config, record: ApplicantRecord, applicant_id: int,
    primary_source: str, run_id: Optional[int], dry_run: bool, stats: Dict[str, int],
) -> bool:
    existing = db.get_applicant(applicant_id)
    if existing is None:
        return False

    update, conflicts = _merge_fields(existing, record, primary_source)

    own = _own_id_field(record.source)
    if not existing[own] and record.source_key:
        update[own] = record.source_key

    new_status, status_changed = _resolve_status(cfg, existing["current_status"], record.status)
    if status_changed:
        update["current_status"] = new_status

    stats["conflicts"] += len(conflicts)
    if not update and not status_changed:
        stats["unchanged"] += 1

    if dry_run:
        return status_changed

    if update:
        db.update_applicant(applicant_id, update)
    if status_changed:
        db.add_status_history(
            applicant_id, new_status, existing["current_status"], record.source,
            f"обновление из {record.source}",
        )
    for reason in conflicts:
        db.add_review_item(
            "conflict", record.source, record.source_key, record.display_name,
            reason, [applicant_id], applicant_id=applicant_id, ingest_run_id=run_id,
        )
    db.add_source_record(
        applicant_id, record.source, record.source_key, record.status_raw,
        record.status, record.fields, record.raw, run_id,
    )
    return status_changed


def _merge_fields(
    existing: Any, record: ApplicantRecord, primary_source: str
) -> Tuple[Dict[str, Any], List[str]]:
    """Сформировать словарь обновлений и список конфликтов.

    Правило: первичный источник (по source_priority, обычно 1С) перезаписывает
    значения; вторичный — только заполняет пустые поля. Расхождения по ключевым
    полям попадают в конфликты.
    """
    update: Dict[str, Any] = {}
    conflicts: List[str] = []
    is_primary = record.source == primary_source

    for fld in CANONICAL_FIELDS:
        value = record.get(fld)
        if value is None or value == "":
            continue
        current = existing[fld]
        if _is_empty(current):
            update[fld] = value
            continue
        if _values_equal(fld, current, value):
            continue
        # значения есть и различаются
        if is_primary:
            update[fld] = value
            if fld in CONFLICT_FIELDS:
                conflicts.append(f"{fld}: «{value}» (1С) заменило «{current}»")
        elif fld in CONFLICT_FIELDS:
            conflicts.append(
                f"{fld}: источник={record.source} «{value}» ≠ сохранённое «{current}» (оставлено сохранённое)"
            )
    return update, conflicts


def _resolve_status(cfg: Config, current: Optional[str], incoming: Optional[str]) -> Tuple[str, bool]:
    """Решить, менять ли статус. Не «откатываем» назад; терминальные — приоритетны."""
    terminal = set(cfg.settings.get("statuses", {}).get("terminal", ["rejected", "withdrawn"]))
    if not incoming:
        return current or cfg.default_status, False
    if not current:
        return incoming, True
    if incoming == current:
        return current, False
    if incoming in terminal:
        return incoming, True
    if _rank(cfg, incoming) > _rank(cfg, current):
        return incoming, True
    return current, False


def _rank(cfg: Config, status: Optional[str]) -> int:
    order = cfg.status_order
    return order.index(status) if status in order else -1


def _is_empty(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


def _values_equal(field: str, a: Any, b: Any) -> bool:
    if field == "consent":
        return bool(a) == bool(b)
    if field == "total_score":
        try:
            return float(a) == float(b)
        except (TypeError, ValueError):
            return str(a) == str(b)
    return str(a).strip() == str(b).strip()
