"""Тесты нормализации значений."""

import pytest

from admissions import normalize as nz


@pytest.mark.parametrize("raw,expected", [
    ("8 (916) 123-45-67", "79161234567"),
    ("+7 916 765 43 21", "79167654321"),
    ("9261112233", "79261112233"),
    ("8-915-000-11-22", "79150001122"),
    ("", None),
    (None, None),
])
def test_phone(raw, expected):
    assert nz.normalize_phone(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("15.03.2007", "2007-03-15"),
    ("2007-07-22", "2007-07-22"),
    ("01.12.2006", "2006-12-01"),
    ("ерунда", None),
    (None, None),
])
def test_date(raw, expected):
    assert nz.normalize_date(raw) == expected


def test_email():
    assert nz.normalize_email("  PETROVA@Example.com ") == "petrova@example.com"
    assert nz.normalize_email("a@b.ru, c@d.ru") == "a@b.ru"
    assert nz.normalize_email("") is None


@pytest.mark.parametrize("raw,expected", [
    ("112-233-445 95", "11223344595"),
    ("11223344595", "11223344595"),
    ("123", None),            # неверная длина
    ("", None),
])
def test_snils(raw, expected):
    assert nz.normalize_snils(raw) == expected


def test_name_and_key():
    assert nz.normalize_name("  иванов   ИВАН ") == "Иванов Иван"
    assert nz.normalize_name("половцев-заварзин") == "Половцев-Заварзин"
    # «ё» нормализуется к «е», регистр снимается
    assert nz.name_key("Пётр") == nz.name_key("петр")


@pytest.mark.parametrize("raw,expected", [
    ("Да", True), ("нет", False), ("Y", True), ("0", False),
    (1, True), (True, True), ("непонятно", None),
])
def test_bool(raw, expected):
    assert nz.normalize_bool(raw) == expected


def test_score():
    assert nz.normalize_score("287") == 287.0
    assert nz.normalize_score("99,5") == 99.5
    assert nz.normalize_score("") is None
    assert nz.normalize_score("abc") is None


def test_field_dispatch():
    assert nz.normalize_field("phone", "8 916 123 45 67") == "79161234567"
    assert nz.normalize_field("birth_date", "15.03.2007") == "2007-03-15"
    assert nz.normalize_field("program", "  Физика ") == "Физика"
