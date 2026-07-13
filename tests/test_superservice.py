"""Тесты источника «суперсервис»: парсер выгрузки ЕПГУ и sync(source=superservice)."""

import pandas as pd
import pytest

from admissions.applicant import Applicant1C
from admissions.ingest_superservice import parse_superservice
from admissions import sync as sync_mod


# ── парсер ────────────────────────────────────────────────────────────────────

SS_COLS = ["Уникальный код поступающего", "ФИО", "Почта", "Телефон", "Обр.программа",
           "Вид заявления", "Вид мест", "Приоритет", "Дата регистрации",
           "Согласие подано очно", "Согласие подано онлайн", "Актуальность"]


def _row(code, fio, program, basis="Бюджетные места", places="Основные места в рамках КЦП",
         prio=1, offline=False, online=False, actual="Действующее",
         email="a@b.ru", phone="79990001122"):
    return [code, fio, email, phone, program, basis, places, prio,
            "2026-06-20 12:00:00", offline, online, actual]


def _write_xlsx(tmp_path, rows):
    path = tmp_path / "superservice.xlsx"
    pd.DataFrame(rows, columns=SS_COLS).to_excel(path, index=False)
    return path


def test_parser_maps_values_and_filters_non_fakt(cfg, tmp_path):
    rows = [
        _row("101", "Иванов Иван Иванович", "Системный анализ и управление в больших системах"),
        _row("102", "Петров Пётр Петрович", "Техническая физика космических летательных аппаратов",
             basis="Платные мест", places="Платные места", online=True),
        _row("103", "Чужой Абитуриент Чужович", "Прикладная математика и информатика"),  # не ФАКТ
    ]
    parsed = parse_superservice(_write_xlsx(tmp_path, rows), cfg.mapping_superservice)

    assert set(parsed) == {"101", "102"}                         # чужая программа отфильтрована
    a1 = parsed["101"]["applications"][0]
    assert a1["group"] == "Системный анализ и управление"        # обр.программа → КГ 1С
    assert a1["basis"] == "Бюджетная основа"
    assert a1["features"] == "Общие места"
    assert a1["score"] is None and a1["score_id"] is None        # баллов в суперсервисе нет
    assert a1["consent"] == ""                                   # согласие не подано
    a2 = parsed["102"]["applications"][0]
    assert a2["group"] == "Техническая физика"
    assert a2["basis"] == "Полное возмещение затрат"             # «Платные мест» → платное
    assert a2["consent"] == "Да"                                 # подано онлайн
    assert a2["withdrawn"] is False


def test_parser_flags_withdrawn(cfg, tmp_path):
    rows = [_row("201", "Отзыв Отзыв Отзывович",
                 "Техническая физика космических летательных аппаратов", actual="Отозвано")]
    parsed = parse_superservice(_write_xlsx(tmp_path, rows), cfg.mapping_superservice)
    assert parsed["201"]["applications"][0]["withdrawn"] is True


def test_parser_active_wins_over_withdrawn_same_key(cfg, tmp_path):
    # одна и та же заявка (тот же ключ) есть и отозванная, и действующая → берём действующую
    rows = [
        _row("301", "Дубль Дубль Дублевич", "Техническая физика космических летательных аппаратов",
             actual="Отозвано"),
        _row("301", "Дубль Дубль Дублевич", "Техническая физика космических летательных аппаратов",
             actual="Действующее"),
    ]
    parsed = parse_superservice(_write_xlsx(tmp_path, rows), cfg.mapping_superservice)
    apps = parsed["301"]["applications"]
    assert len(apps) == 1 and apps[0]["withdrawn"] is False


# ── sync(source=superservice) ────────────────────────────────────────────────

_MAG_STAGES = [
    {"STATUS_ID": "C10:NEW", "SORT": "10", "NAME": "Поступившие заявления"},
    {"STATUS_ID": "C10:PREP", "SORT": "20", "NAME": "Связались"},
    {"STATUS_ID": "C10:WD", "SORT": "90", "NAME": "Отозвал заявление", "SEMANTICS": "F"},
]


def _ss_app(group, prio=1, consent="Да", basis="Бюджетная основа",
            features="Общие места", withdrawn=False):
    return {"group": group, "no_exams": "", "score": None, "priority": prio,
            "score_id": None, "basis": basis, "targeted": "", "consent": consent,
            "special": "", "app_date": "2026-06-20", "features": features,
            "control": "", "withdrawn": withdrawn}


def _person(code, fio, *apps, email="a@b.ru", phone="79990001122"):
    return Applicant1C(code=code, full_name=fio, email=email, phone=phone,
                       applications=list(apps))


@pytest.fixture
def patch_ss(monkeypatch):
    def _set(people):
        monkeypatch.setattr(sync_mod, "build_applicants_superservice",
                            lambda path, mapping=None: people)
    return _set


def test_superservice_create_leaves_scores_empty(cfg, fake_bitrix, patch_ss):
    patch_ss([_person("501", "Иванов Иван Иванович", _ss_app("Системный анализ и управление"))])
    client = fake_bitrix(stages=_MAG_STAGES)
    stats = sync_mod.sync(cfg, "x", apply=True, client=client, level="master", source="superservice")

    assert stats["created_deals"] == 1
    deal = client.writes_of("crm.deal.add")[0]["fields"]
    assert deal["UF_CRM_M_GROUP"] == "Системный анализ и управление"
    assert deal["CATEGORY_ID"] == 10
    assert "UF_CRM_M_SCORE" not in deal and "UF_CRM_M_SCORE_ID" not in deal   # баллы пусты
    assert client.writes_of("crm.contact.add")[0]["fields"]["TYPE_ID"] == "SUPPLIER"


def test_superservice_does_not_overwrite_existing_scores(cfg, fake_bitrix, patch_ss):
    # существующая маг-сделка с баллом (проставлен из 1С) — суперсервис его НЕ затирает
    contact = {"ID": "501", "LAST_NAME": "Иванов", "NAME": "Иван", "SECOND_NAME": "Иванович",
               "UF_CRM_PK_CODE": "501", "PHONE": [{"VALUE": "+79990001122"}]}
    deal = {"ID": "9100", "CONTACT_ID": "501", "CATEGORY_ID": "10", "STAGE_ID": "C10:NEW",
            "UF_CRM_M_CODE": "501", "UF_CRM_M_GROUP": "Аэрокосмические технологии",
            "UF_CRM_M_BASIS": "Бюджетная основа", "UF_CRM_M_FEATURES": "Общие места",
            "UF_CRM_M_SCORE": "88.5"}
    patch_ss([_person("501", "Иванов Иван Иванович", _ss_app("Аэрокосмические технологии"))])
    client = fake_bitrix(contacts=[contact], deals=[deal], stages=_MAG_STAGES)
    sync_mod.sync(cfg, "x", apply=True, client=client, level="master", source="superservice")

    for upd in client.writes_of("crm.deal.update"):
        assert "UF_CRM_M_SCORE" not in upd["fields"]     # балл не среди изменений
    assert deal["UF_CRM_M_SCORE"] == "88.5"              # и остался на месте


def test_superservice_withdrawn_moves_stage(cfg, fake_bitrix, patch_ss):
    cfg.settings.setdefault("stages", {})["on_withdrawn"] = "Отозвал заявление"
    contact = {"ID": "501", "LAST_NAME": "Иванов", "NAME": "Иван", "SECOND_NAME": "Иванович",
               "UF_CRM_PK_CODE": "501", "PHONE": [{"VALUE": "+79990001122"}]}
    deal = {"ID": "9100", "CONTACT_ID": "501", "CATEGORY_ID": "10", "STAGE_ID": "C10:NEW",
            "UF_CRM_M_CODE": "501", "UF_CRM_M_GROUP": "Аэрокосмические технологии",
            "UF_CRM_M_BASIS": "Бюджетная основа", "UF_CRM_M_FEATURES": "Общие места"}
    patch_ss([_person("501", "Иванов Иван Иванович",
                      _ss_app("Аэрокосмические технологии", withdrawn=True))])
    client = fake_bitrix(contacts=[contact], deals=[deal], stages=_MAG_STAGES)
    stats = sync_mod.sync(cfg, "x", apply=True, client=client, level="master", source="superservice")

    assert stats["withdrawn_moved"] == 1
    upd = [p for p in client.writes_of("crm.deal.update") if p["id"] == "9100"]
    assert upd and upd[0]["fields"]["STAGE_ID"] == "C10:WD"


def test_superservice_does_not_withdraw_by_absence(cfg, fake_bitrix, patch_ss):
    # 1С-сделка (с баллом), которой нет в суперсервис-выгрузке, НЕ должна быть отозвана
    cfg.settings.setdefault("stages", {})["on_withdrawn"] = "Отозвал заявление"
    contact = {"ID": "501", "LAST_NAME": "Иванов", "NAME": "Иван", "SECOND_NAME": "Иванович",
               "UF_CRM_PK_CODE": "501", "PHONE": [{"VALUE": "+79990001122"}]}
    # контакт из 1С, которого нет в суперсервис-выгрузке — НЕ должен считаться «выбывшим»
    absent = {"ID": "600", "LAST_NAME": "Нету", "NAME": "Вфайле", "SECOND_NAME": "",
              "UF_CRM_PK_CODE": "888", "PHONE": [], "EMAIL": []}
    other = {"ID": "9100", "CONTACT_ID": "501", "CATEGORY_ID": "10", "STAGE_ID": "C10:NEW",
             "UF_CRM_M_CODE": "999", "UF_CRM_M_GROUP": "Техническая физика", "UF_CRM_M_SCORE": "70"}
    patch_ss([_person("501", "Иванов Иван Иванович", _ss_app("Аэрокосмические технологии"))])
    client = fake_bitrix(contacts=[contact, absent], deals=[other], stages=_MAG_STAGES)
    stats = sync_mod.sync(cfg, "x", apply=True, client=client, level="master", source="superservice")

    assert stats["withdrawn"] == []            # чужая 1С-сделка не тронута отзывом
    assert stats["withdrawn_moved"] == 0
    assert stats["dropped"] == []              # состав суперсервиса неполон → «выбывших» не считаем
