"""Тесты парсера выгрузки 1С (формат ФАКТ 2026)."""

import pandas as pd

from admissions.applicant import build_applicants
from admissions.ingest_applications import parse_applications

HEADER = [
    "№", "ФИО", "Конкурсная группа", "Без вступительных испытаний", "Сумма баллов",
    "Приоритет", "Сумма баллов по ИД (все)", "Основание поступления", "Целевик",
    "Согласие на зачисление", "Email", "Телефон", "Уникальный код",
    "Лицо, имеющее особое право", "Дата подачи заявления", "Особенности приема",
    "Контроль пройден",
]


def _row(num, fio, group, score, prio, code, **kw):
    return [
        num, fio, group, kw.get("bvi", ""), score, prio, kw.get("score_id", " 0 "),
        kw.get("basis", "Бюджетная основа"), kw.get("targeted", ""), kw.get("consent", ""),
        kw.get("email", "x@e.ru"), kw.get("phone", "+7(900) 000-00-00"), code,
        kw.get("special", ""), "29.06.2026 13:03:52", "Общие места", kw.get("control", "✓"),
    ]


def _make_file(path, data_rows):
    # шапка отчёта (3 строки) + пустая + шапка таблицы + данные — как в реальном .xls
    top = [["ФАКТ 2026 (н)"], [], ["Дата формирования - 30.06.2026."], []]
    rows = top + [HEADER] + data_rows
    width = max(len(r) for r in rows)
    norm = [r + [None] * (width - len(r)) for r in rows]
    pd.DataFrame(norm).to_excel(path, header=False, index=False)


def test_header_detection_and_basic_parse(tmp_path):
    f = tmp_path / "f.xlsx"
    _make_file(f, [
        _row(1, "Иванов Иван Иванович", "Техническая физика", "287", "1", "111",
             consent="✓", phone="8 (916) 123-45-67"),
    ])
    apps = parse_applications(f)
    assert set(apps) == {"111"}
    a = apps["111"]
    assert a["full_name"] == "Иванов Иван Иванович"
    assert a["phone"] == "79161234567"          # нормализован
    app = a["applications"][0]
    assert app["group"] == "Техническая физика"
    assert app["score"] == 287.0
    assert app["priority"] == 1
    assert app["consent"] == "Да"               # «✓» -> «Да»
    assert app["control"] == "Да"


def test_dedup_by_code_and_group(tmp_path):
    f = tmp_path / "f.xlsx"
    _make_file(f, [
        # дублирующиеся строки (как в реальной выгрузке) + 2 разные группы
        _row(1, "Кудрявцев Иван Александрович", "Геокосмические науки и технологии", "260", "2", "1208443"),
        _row(2, "Кудрявцев Иван Александрович", "Геокосмические науки и технологии", "260", "2", "1208443"),
        _row(3, "Кудрявцев Иван Александрович", "Техническая физика", "256", "5", "1208443"),
        _row(4, "Кудрявцев Иван Александрович", "Техническая физика", "256", "5", "1208443"),
    ])
    apps = parse_applications(f)
    a = apps["1208443"]
    assert len(a["applications"]) == 2          # 4 строки -> 2 заявления
    groups = {x["group"] for x in a["applications"]}
    assert groups == {"Геокосмические науки и технологии", "Техническая физика"}
    # отсортировано по приоритету (2 раньше 5)
    assert a["applications"][0]["priority"] == 2


def test_same_group_different_basis_kept(tmp_path):
    # одна группа, но разные основания -> ДВА заявления (не склеивать)
    f = tmp_path / "f.xlsx"
    _make_file(f, [
        _row(1, "Орлов Олег", "Техническая физика", "270", "1", "700", basis="Бюджетная основа"),
        _row(2, "Орлов Олег", "Техническая физика", "270", "1", "700", basis="Полное возмещение затрат"),
        # а вот это — полный дубль по (группа, основание, особенности) -> схлопнуть
        _row(3, "Орлов Олег", "Техническая физика", "270", "1", "700", basis="Бюджетная основа"),
    ])
    apps = parse_applications(f)
    bases = sorted(a["basis"] for a in apps["700"]["applications"])
    assert bases == ["Бюджетная основа", "Полное возмещение затрат"]  # 2, дубль убран


def test_special_right_distinct_and_resubmission_keeps_best(tmp_path):
    f = tmp_path / "f.xlsx"
    _make_file(f, [
        _row(1, "Львов Лев", "Техническая физика", "270", "1", "800", special=""),
        _row(2, "Львов Лев", "Техническая физика", "270", "1", "800", special="✓"),   # особое право
        # повторная подача: сначала балл 0, потом 304 -> берём 304
        _row(3, "Мамонтов Мир", "Техническая физика", "0", "1", "801", special=""),
        _row(4, "Мамонтов Мир", "Техническая физика", "304", "1", "801", special="", consent="✓"),
    ])
    apps = parse_applications(f)
    lvov = apps["800"]["applications"]
    assert len(lvov) == 2                                    # общий + особое право
    assert sorted(a["special"] for a in lvov) == ["", "Да"]
    mam = apps["801"]["applications"]
    assert len(mam) == 1 and mam[0]["score"] == 304.0        # актуальная версия


def test_build_applicants(tmp_path):
    f = tmp_path / "f.xlsx"
    _make_file(f, [
        _row(1, "Петров Пётр Петрович", "Техническая физика", "300", "1", "222"),
    ])
    people = build_applicants(f)
    assert len(people) == 1
    p = people[0]
    assert p.code == "222"
    assert p.name_parts == ("Петров", "Пётр", "Петрович")
    assert p.lastfirst_key == "петров петр"     # ё->е, регистр, без отчества
    assert p.groups == ["Техническая физика"]
