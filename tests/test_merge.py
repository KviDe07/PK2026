"""Тесты слияния, истории статусов и идемпотентности."""

from admissions.merge import process_records
from admissions.models import SOURCE_1C, SOURCE_BITRIX


def _ivanov_1c(make_record, status="documents_submitted", **extra):
    return make_record(
        SOURCE_1C, "A-1001", status=status,
        last_name="Иванов", first_name="Иван", middle_name="Иванович",
        birth_date="2007-03-15", email="ivanov@example.com", phone="79161234567",
        snils="11223344595", program="Прикладная математика", **extra,
    )


def test_create_new(db, cfg, make_record):
    stats = process_records(db, cfg, [_ivanov_1c(make_record)], source=SOURCE_1C)
    assert stats["new"] == 1
    assert len(db.all_applicants()) == 1
    # начальная запись истории
    assert len(db.query("SELECT 1 FROM status_history")) == 1


def test_idempotent_reingest(db, cfg, make_record):
    process_records(db, cfg, [_ivanov_1c(make_record)], source=SOURCE_1C)
    # повторная загрузка той же записи -> без новых карточек и без смен статуса
    stats = process_records(db, cfg, [_ivanov_1c(make_record)], source=SOURCE_1C)
    assert stats["new"] == 0
    assert stats["updated"] == 1
    assert stats["status_changes"] == 0
    assert len(db.all_applicants()) == 1
    assert len(db.query("SELECT 1 FROM status_history")) == 1


def test_merge_two_sources_and_status_advance(db, cfg, make_record):
    process_records(db, cfg, [_ivanov_1c(make_record)], source=SOURCE_1C)
    # из Битрикса — тот же человек (по email/phone), статус продвинулся
    bx = make_record(
        SOURCE_BITRIX, "501", status="consent_given",
        email="ivanov@example.com", phone="79161234567",
    )
    stats = process_records(db, cfg, [bx], source=SOURCE_BITRIX)
    assert len(db.all_applicants()) == 1          # слилось в одну карточку
    assert stats["status_changes"] == 1
    a = db.get_applicant(1)
    assert a["current_status"] == "consent_given"
    assert a["onec_id"] == "A-1001" and a["bitrix_id"] == "501"


def test_status_not_downgraded(db, cfg, make_record):
    process_records(db, cfg, [_ivanov_1c(make_record, status="under_review")], source=SOURCE_1C)
    bx = make_record(
        SOURCE_BITRIX, "501", status="documents_submitted",  # этап раньше
        email="ivanov@example.com",
    )
    process_records(db, cfg, [bx], source=SOURCE_BITRIX)
    assert db.get_applicant(1)["current_status"] == "under_review"


def test_terminal_status_overrides(db, cfg, make_record):
    process_records(db, cfg, [_ivanov_1c(make_record, status="under_review")], source=SOURCE_1C)
    bx = make_record(SOURCE_BITRIX, "501", status="withdrawn", email="ivanov@example.com")
    process_records(db, cfg, [bx], source=SOURCE_BITRIX)
    assert db.get_applicant(1)["current_status"] == "withdrawn"


def test_secondary_source_does_not_overwrite_and_logs_conflict(db, cfg, make_record):
    process_records(db, cfg, [_ivanov_1c(make_record)], source=SOURCE_1C)
    # Битрикс (вторичный) присылает другое направление -> не перезаписывает, конфликт в очереди
    bx = make_record(
        SOURCE_BITRIX, "501", email="ivanov@example.com", program="Физика",
    )
    process_records(db, cfg, [bx], source=SOURCE_BITRIX)
    assert db.get_applicant(1)["program"] == "Прикладная математика"
    conflicts = [r for r in db.get_review_items() if r["kind"] == "conflict"]
    assert conflicts and "program" in conflicts[0]["reason"]


def test_dry_run_writes_nothing(db, cfg, make_record):
    process_records(db, cfg, [_ivanov_1c(make_record)], source=SOURCE_1C)
    hist_before = len(db.query("SELECT 1 FROM status_history"))
    bx = make_record(SOURCE_BITRIX, "501", status="enrolled", email="ivanov@example.com")
    process_records(db, cfg, [bx], source=SOURCE_BITRIX, dry_run=True)
    # ничего не записалось
    assert len(db.query("SELECT 1 FROM status_history")) == hist_before
    assert db.get_applicant(1)["current_status"] == "documents_submitted"
