"""Тесты ядра синхронизации sync() против заглушки Битрикса (без сети)."""

import pytest

from admissions.applicant import Applicant1C
from admissions import sync as sync_mod


def _app(group, score=287.0, prio=1, consent="Да", basis="Бюджетная основа",
         features="Общие места", special=""):
    return {
        "group": group, "no_exams": "", "score": score, "priority": prio,
        "score_id": 0.0, "basis": basis, "targeted": "",
        "consent": consent, "special": special, "app_date": "29.06.2026",
        "features": features, "control": "Да",
    }


def _person(code, fio, *apps, email="x@e.ru", phone="79160001122"):
    return Applicant1C(code=code, full_name=fio, email=email, phone=phone,
                       applications=list(apps))


@pytest.fixture
def patch_people(monkeypatch):
    def _set(people):
        monkeypatch.setattr(sync_mod, "build_applicants", lambda path, colmap=None: people)
    return _set


def test_create_contact_and_deal(cfg, fake_bitrix, patch_people):
    patch_people([_person("111", "Иванов Иван Иванович", _app("Техническая физика"))])
    client = fake_bitrix()  # пустой Битрикс
    stats = sync_mod.sync(cfg, "x", apply=True, client=client)

    assert stats["created_contacts"] == 1 and stats["created_deals"] == 1
    add = client.writes_of("crm.contact.add")[0]["fields"]
    assert add["LAST_NAME"] == "Иванов" and add["NAME"] == "Иван" and add["SECOND_NAME"] == "Иванович"
    assert add["UF_CRM_PK_CODE"] == "111"       # поле кода у контакта
    assert add["TYPE_ID"] == "CLIENT"           # стандартный «Тип контакта» = Абитуриенты
    deal = client.writes_of("crm.deal.add")[0]["fields"]
    assert deal["CATEGORY_ID"] == 8 and deal["STAGE_ID"] == "C8:NEW"
    assert deal["UF_CRM_B_GROUP"] == "Техническая физика"
    assert deal["CONTACT_ID"]                    # привязан к созданному контакту


def _blank_deal(_id, contact_id, stage="C8:UC_K4L2XI"):
    """Пустая сделка оператора в воронке бакалавриата (кат.8), ранняя стадия."""
    return {"ID": _id, "CONTACT_ID": contact_id, "CATEGORY_ID": "8",
            "STAGE_ID": stage, "TITLE": f"Сделка #{_id}"}


def test_adopt_operator_stub_fills_blank(cfg, fake_bitrix, patch_people):
    # оператор завёл контакт + пустую сделку в воронке (без кода)
    stub = {"ID": "501", "LAST_NAME": "Петров", "NAME": "Пётр", "SECOND_NAME": "",
            "PHONE": [], "EMAIL": []}
    patch_people([_person("222", "Петров Пётр Петрович", _app("Техническая физика"))])
    client = fake_bitrix(contacts=[stub], deals=[_blank_deal("9000", "501")])
    stats = sync_mod.sync(cfg, "x", apply=True, client=client)

    assert stats["adopted"] == 1 and stats["created_contacts"] == 0
    upd = client.writes_of("crm.contact.update")[0]
    assert upd["id"] == "501" and upd["fields"]["UF_CRM_PK_CODE"] == "222"
    assert upd["fields"]["TYPE_ID"] == "CLIENT"
    assert "LAST_NAME" not in upd["fields"]          # имя оператора не трогаем
    # пустую сделку ЗАПОЛНИЛИ (реюз), новую не создавали
    assert stats["filled_deals"] == 1 and stats["created_deals"] == 0
    du = client.writes_of("crm.deal.update")
    assert du and du[0]["id"] == "9000"
    assert du[0]["fields"]["UF_CRM_B_GROUP"] == "Техническая физика"
    assert du[0]["fields"]["STAGE_ID"] == "C8:PREPARATION"   # был контакт → «Связались»
    assert client.writes_of("crm.deal.add") == []


def test_new_deal_goes_to_applications_stage(cfg, fake_bitrix, patch_people):
    # заявление без предыдущего контакта -> «Поступившие заявления»
    patch_people([_person("111", "Иванов Иван Иванович", _app("Техническая физика"))])
    client = fake_bitrix()
    sync_mod.sync(cfg, "x", apply=True, client=client)
    deal = client.writes_of("crm.deal.add")[0]["fields"]
    assert deal["STAGE_ID"] == "C8:NEW"   # «Поступившие заявления»


def test_fill_does_not_regress_advanced_stage(cfg, fake_bitrix, patch_people):
    # пустая сделка уже на поздней стадии -> при заполнении НЕ откатываем назад
    stub = {"ID": "501", "LAST_NAME": "Петров", "NAME": "Пётр", "SECOND_NAME": "",
            "PHONE": [], "EMAIL": []}
    advanced = _blank_deal("9000", "501", stage="C8:EXECUTING")  # позже «Связались»
    patch_people([_person("222", "Петров Пётр Петрович", _app("Техническая физика"))])
    client = fake_bitrix(contacts=[stub], deals=[advanced],
                         stages=[{"STATUS_ID": "C8:NEW", "SORT": "30", "NAME": "Поступившие заявления"},
                                 {"STATUS_ID": "C8:PREPARATION", "SORT": "40", "NAME": "Связались"},
                                 {"STATUS_ID": "C8:EXECUTING", "SORT": "60", "NAME": "Ждем согласие"}])
    sync_mod.sync(cfg, "x", apply=True, client=client)
    fields = client.writes_of("crm.deal.update")[0]["fields"]
    assert "STAGE_ID" not in fields   # стадию не трогаем (она уже дальше)


def test_funnel_scope_ignores_outside_contact(cfg, fake_bitrix, patch_people):
    # тёзка ВНЕ воронки (нет сделки кат.8) не должен матчиться -> создаём новый
    outsider = {"ID": "1422", "LAST_NAME": "Петров", "NAME": "Максим", "SECOND_NAME": "Дмитриевич",
                "PHONE": [{"VALUE": "+79990001122"}], "EMAIL": []}
    patch_people([_person("888", "Петров Максим Александрович", _app("Техническая физика"))])
    client = fake_bitrix(contacts=[outsider])   # у outsider нет сделки в воронке
    stats = sync_mod.sync(cfg, "x", apply=True, client=client)
    assert stats["adopted"] == 0 and stats["created_contacts"] == 1
    assert client.writes_of("crm.contact.update") == []   # чужой контакт не трогаем


def test_swapped_name_fields_match(cfg, fake_bitrix, patch_people):
    # оператор ввёл «Имя Фамилия» -> поля переставлены: LAST=Иван, NAME=Дубцов
    stub = {"ID": "777", "LAST_NAME": "Иван", "NAME": "Дубцов", "SECOND_NAME": "",
            "PHONE": [], "EMAIL": []}
    patch_people([_person("321", "Дубцов Иван Сергеевич", _app("Техническая физика"))])
    client = fake_bitrix(contacts=[stub], deals=[_blank_deal("95", "777")])
    stats = sync_mod.sync(cfg, "x", apply=True, client=client)
    assert stats["adopted"] == 1 and stats["created_contacts"] == 0
    assert client.writes_of("crm.contact.update")[0]["id"] == "777"
    assert stats["filled_deals"] == 1


def test_phone_tiebreak_resolves_homonyms(cfg, fake_bitrix, patch_people):
    # два тёзки в воронке; телефон совпадает только с одним -> берём его
    s1 = {"ID": "1", "LAST_NAME": "Иванов", "NAME": "Иван", "SECOND_NAME": "",
          "PHONE": [{"VALUE": "+79991112233"}], "EMAIL": []}
    s2 = {"ID": "2", "LAST_NAME": "Иванов", "NAME": "Иван", "SECOND_NAME": "",
          "PHONE": [{"VALUE": "+79160001122"}], "EMAIL": []}
    patch_people([_person("333", "Иванов Иван Сергеевич", _app("Техническая физика"),
                          phone="79160001122")])
    client = fake_bitrix(contacts=[s1, s2],
                         deals=[_blank_deal("91", "1"), _blank_deal("92", "2")])
    stats = sync_mod.sync(cfg, "x", apply=True, client=client)
    assert stats["adopted"] == 1 and not stats["ambiguous"]
    assert client.writes_of("crm.contact.update")[0]["id"] == "2"   # с совпавшим телефоном


def test_homonyms_without_phone_are_ambiguous(cfg, fake_bitrix, patch_people):
    s1 = {"ID": "1", "LAST_NAME": "Иванов", "NAME": "Иван", "SECOND_NAME": "", "PHONE": [], "EMAIL": []}
    s2 = {"ID": "2", "LAST_NAME": "Иванов", "NAME": "Иван", "SECOND_NAME": "", "PHONE": [], "EMAIL": []}
    patch_people([_person("333", "Иванов Иван Сергеевич", _app("Техническая физика"))])
    client = fake_bitrix(contacts=[s1, s2],
                         deals=[_blank_deal("91", "1"), _blank_deal("92", "2")])
    stats = sync_mod.sync(cfg, "x", apply=True, client=client)
    assert len(stats["ambiguous"]) == 1
    assert stats["adopted"] == 0 and stats["created_contacts"] == 0


def test_phone_conflict_creates_new_and_flags(cfg, fake_bitrix, patch_people):
    # один кандидат в воронке, но телефон ПРОТИВОРЕЧИТ 1С -> другой человек
    stub = {"ID": "1", "LAST_NAME": "Петров", "NAME": "Пётр", "SECOND_NAME": "",
            "PHONE": [{"VALUE": "+79990000000"}], "EMAIL": []}
    patch_people([_person("555", "Петров Пётр Петрович", _app("Техническая физика"),
                          phone="79160001122")])
    client = fake_bitrix(contacts=[stub], deals=[_blank_deal("91", "1")])
    stats = sync_mod.sync(cfg, "x", apply=True, client=client)
    assert stats["adopted"] == 0 and stats["created_contacts"] == 1
    assert len(stats["conflicts"]) == 1
    assert client.writes_of("crm.contact.update") == []   # чужой контакт не трогаем
    assert len(client.writes_of("crm.contact.add")) == 1


def test_two_groups_two_deals(cfg, fake_bitrix, patch_people):
    patch_people([_person("444", "Сидоров Семён",
                          _app("Геокосмические науки и технологии", score=260, prio=2),
                          _app("Техническая физика", score=256, prio=5))])
    client = fake_bitrix()
    stats = sync_mod.sync(cfg, "x", apply=True, client=client)
    assert stats["created_deals"] == 2
    groups = {p["fields"]["UF_CRM_B_GROUP"] for p in client.writes_of("crm.deal.add")}
    assert groups == {"Геокосмические науки и технологии", "Техническая физика"}
    # один общий контакт
    assert len(client.writes_of("crm.contact.add")) == 1


def test_same_group_different_basis_two_deals(cfg, fake_bitrix, patch_people):
    # одна группа, но два заявления: бюджет и платное -> ДВЕ сделки
    patch_people([_person("777", "Сидоров Семён",
                          _app("Техническая физика", basis="Бюджетная основа"),
                          _app("Техническая физика", basis="Полное возмещение затрат"))])
    client = fake_bitrix()
    stats = sync_mod.sync(cfg, "x", apply=True, client=client)
    assert stats["created_deals"] == 2
    bases = {p["fields"]["UF_CRM_B_BASIS"] for p in client.writes_of("crm.deal.add")}
    assert bases == {"Бюджетная основа", "Полное возмещение затрат"}


def test_special_right_is_separate_deal(cfg, fake_bitrix, patch_people):
    # общий конкурс + особое право в одной группе/основании -> ДВЕ сделки
    patch_people([_person("800", "Львов Лев",
                          _app("Техническая физика", special=""),
                          _app("Техническая физика", special="Да"))])
    client = fake_bitrix()
    stats = sync_mod.sync(cfg, "x", apply=True, client=client)
    assert stats["created_deals"] == 2
    specials = {p["fields"].get("UF_CRM_B_SPECIAL", "") for p in client.writes_of("crm.deal.add")}
    assert specials == {"", "Да"}


def test_update_only_diff_protects_operator_fields(cfg, fake_bitrix, patch_people):
    contact = {"ID": "501", "LAST_NAME": "Иванов", "NAME": "Иван", "SECOND_NAME": "Иванович",
               "UF_CRM_PK_CODE": "111", "PHONE": [{"VALUE": "+79160001122"}]}
    deal = {"ID": "9001", "CONTACT_ID": "501", "CATEGORY_ID": "8",
            "STAGE_ID": "C8:WON", "TITLE": "ручной заголовок",
            "UF_CRM_B_CODE": "111", "UF_CRM_B_GROUP": "Техническая физика",
            "UF_CRM_B_BASIS": "Бюджетная основа", "UF_CRM_B_FEATURES": "Общие места",
            "UF_CRM_B_SCORE": "280"}
    patch_people([_person("111", "Иванов Иван Иванович", _app("Техническая физика", score=287))])
    client = fake_bitrix(contacts=[contact], deals=[deal])
    stats = sync_mod.sync(cfg, "x", apply=True, client=client)

    assert stats["matched_by_code"] == 1 and stats["updated_deals"] == 1 and stats["created_deals"] == 0
    fields = client.writes_of("crm.deal.update")[0]["fields"]
    assert fields["UF_CRM_B_SCORE"] == 287.0
    assert "STAGE_ID" not in fields and "TITLE" not in fields   # операторские не трогаем
    assert "UF_CRM_B_UPDATED" in fields


def test_withdrawn_deal_detected(cfg, fake_bitrix, patch_people):
    # у контакта есть сделка (код), но её заявления НЕТ в новой выгрузке -> отозвано
    contact = {"ID": "501", "LAST_NAME": "Иванов", "NAME": "Иван", "SECOND_NAME": "Иванович",
               "UF_CRM_PK_CODE": "111", "PHONE": [{"VALUE": "+79160001122"}]}
    live = {"ID": "9001", "CONTACT_ID": "501", "CATEGORY_ID": "8", "STAGE_ID": "C8:NEW",
            "UF_CRM_B_CODE": "111", "UF_CRM_B_GROUP": "Техническая физика",
            "UF_CRM_B_BASIS": "Бюджетная основа", "UF_CRM_B_FEATURES": "Общие места"}
    gone = {"ID": "9002", "CONTACT_ID": "501", "CATEGORY_ID": "8", "STAGE_ID": "C8:NEW",
            "UF_CRM_B_CODE": "111", "UF_CRM_B_GROUP": "Геокосмические науки и технологии"}
    patch_people([_person("111", "Иванов Иван Иванович", _app("Техническая физика"))])
    client = fake_bitrix(contacts=[contact], deals=[live, gone])
    stats = sync_mod.sync(cfg, "x", apply=True, client=client)
    assert [w["deal"] for w in stats["withdrawn"]] == ["9002"]     # только «Геокосмические»
    assert stats["withdrawn_moved"] == 0                            # стадия отозвано не задана


def test_withdrawn_deal_moved_when_stage_set(cfg, fake_bitrix, patch_people):
    cfg.settings.setdefault("stages", {})["on_withdrawn"] = "Связались"  # для теста
    contact = {"ID": "501", "LAST_NAME": "Иванов", "NAME": "Иван", "SECOND_NAME": "Иванович",
               "UF_CRM_PK_CODE": "111", "PHONE": [{"VALUE": "+79160001122"}]}
    gone = {"ID": "9002", "CONTACT_ID": "501", "CATEGORY_ID": "8", "STAGE_ID": "C8:NEW",
            "UF_CRM_B_CODE": "111", "UF_CRM_B_GROUP": "Геокосмические науки и технологии"}
    patch_people([_person("111", "Иванов Иван Иванович", _app("Техническая физика"))])
    client = fake_bitrix(contacts=[contact], deals=[gone])
    stats = sync_mod.sync(cfg, "x", apply=True, client=client)
    assert stats["withdrawn_moved"] == 1
    upd = [p for p in client.writes_of("crm.deal.update") if p["id"] == "9002"]
    assert upd and upd[0]["fields"]["STAGE_ID"] == "C8:PREPARATION"


def test_dry_run_writes_nothing(cfg, fake_bitrix, patch_people):
    patch_people([_person("111", "Иванов Иван Иванович", _app("Техническая физика"))])
    client = fake_bitrix()
    stats = sync_mod.sync(cfg, "x", apply=False, client=client)
    assert stats["created_contacts"] == 1 and stats["created_deals"] == 1
    assert client.writes == []                  # dry-run: ничего не записано


def test_dropped_reported(cfg, fake_bitrix, patch_people):
    # контакт с кодом, которого нет в новой выгрузке -> выбывший
    gone = {"ID": "700", "LAST_NAME": "Ушёл", "NAME": "Пётр", "SECOND_NAME": "",
            "UF_CRM_PK_CODE": "999", "PHONE": [], "EMAIL": []}
    patch_people([_person("111", "Иванов Иван Иванович", _app("Техническая физика"))])
    client = fake_bitrix(contacts=[gone])
    stats = sync_mod.sync(cfg, "x", apply=False, client=client)
    assert [d["code"] for d in stats["dropped"]] == ["999"]


def test_failed_create_reported(cfg, fake_bitrix, patch_people):
    # Битрикс «отклоняет» создание (фантом) -> попадает в «не создались», сделок 0
    patch_people([_person("111", "Иванов Иван Иванович", _app("Техническая физика"))])
    client = fake_bitrix(reject_codes={"111"})
    stats = sync_mod.sync(cfg, "x", apply=True, client=client)
    assert [f["code"] for f in stats["failed"]] == ["111"]
    assert client.writes_of("crm.deal.add") == []   # сделку-сироту не создаём


def test_idempotent_second_run(cfg, fake_bitrix, patch_people):
    patch_people([_person("111", "Иванов Иван Иванович", _app("Техническая физика"))])
    client = fake_bitrix()
    sync_mod.sync(cfg, "x", apply=True, client=client)   # создаёт контакт+сделку
    client.writes.clear()
    stats = sync_mod.sync(cfg, "x", apply=True, client=client)  # повтор
    assert stats["created_deals"] == 0 and stats["updated_deals"] == 0
    assert client.writes == []
