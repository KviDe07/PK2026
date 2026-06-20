"""Тесты каскадного сопоставления."""

from admissions.matching import (
    DECISION_MATCH,
    DECISION_NEW,
    DECISION_REVIEW,
    match_record,
)
from admissions.models import SOURCE_1C, SOURCE_BITRIX


def _seed_ivanov(db, **overrides):
    data = dict(
        last_name="Иванов", first_name="Иван", middle_name="Иванович",
        birth_date="2007-03-15", email="ivanov@example.com", phone="79161234567",
        snils="11223344595", onec_id="A-1001", current_status="documents_submitted",
    )
    data.update(overrides)
    return db.insert_applicant(data)


def test_match_by_email(db, cfg, make_record):
    aid = _seed_ivanov(db)
    rec = make_record(SOURCE_BITRIX, "501", email="ivanov@example.com")
    res = match_record(db, cfg, rec)
    assert res.decision == DECISION_MATCH
    assert res.applicant_id == aid
    assert "exact_contact" in res.method


def test_match_by_phone(db, cfg, make_record):
    aid = _seed_ivanov(db)
    rec = make_record(SOURCE_BITRIX, "501", phone="79161234567")
    res = match_record(db, cfg, rec)
    assert res.decision == DECISION_MATCH
    assert res.applicant_id == aid


def test_match_by_own_source_id(db, cfg, make_record):
    aid = _seed_ivanov(db)
    # та же запись из 1С (onec_id уже есть в карточке) — обновление
    rec = make_record(SOURCE_1C, "A-1001", email="other@example.com")
    res = match_record(db, cfg, rec)
    assert res.decision == DECISION_MATCH
    assert res.applicant_id == aid
    assert res.method == "source_id"


def test_fuzzy_match_by_name_and_dob(db, cfg, make_record):
    aid = _seed_ivanov(db)
    # без контактов, но совпадает ФИО и дата рождения
    rec = make_record(
        SOURCE_BITRIX, "900",
        last_name="Иванов", first_name="Иван", middle_name="Иванович",
        birth_date="2007-03-15",
    )
    res = match_record(db, cfg, rec)
    assert res.decision == DECISION_MATCH
    assert res.applicant_id == aid
    assert res.method == "fuzzy_name_dob"


def test_new_record(db, cfg, make_record):
    _seed_ivanov(db)
    rec = make_record(
        SOURCE_BITRIX, "777",
        last_name="Сидоров", first_name="Пётр", birth_date="2005-01-01",
        email="sidorov@example.com",
    )
    res = match_record(db, cfg, rec)
    assert res.decision == DECISION_NEW


def test_ambiguous_contact_goes_to_review(db, cfg, make_record):
    # два абитуриента делят один телефон -> совпадение неоднозначно
    a1 = _seed_ivanov(db)
    a2 = db.insert_applicant(dict(
        last_name="Петров", first_name="Олег", birth_date="2006-02-02",
        phone="79161234567", onec_id="A-2002", current_status="lead",
    ))
    rec = make_record(SOURCE_BITRIX, "888", phone="79161234567")
    res = match_record(db, cfg, rec)
    assert res.decision == DECISION_REVIEW
    assert set(res.candidate_ids) == {a1, a2}


def test_fuzzy_below_threshold_goes_to_review(db, cfg, make_record):
    # ФИО похоже (score≈83, между review_threshold=75 и порогом=90),
    # дата рождения совпадает -> ручная проверка, НЕ авто-слияние
    _seed_ivanov(db)
    rec = make_record(
        SOURCE_BITRIX, "901",
        last_name="Иванчук", first_name="Иван", middle_name="Иванович",
        birth_date="2007-03-15",
    )
    res = match_record(db, cfg, rec)
    assert res.decision == DECISION_REVIEW
    assert res.applicant_id is None
