"""Синхронизация: выгрузка 1С → контакты и сделки Битрикс24.

Поток (1С-управляемый, идемпотентный):
  1. читаем заявления 1С (по уникальному коду, дедуп по конкурсной группе);
  2. находим контакт:
       • по уникальному коду;
       • иначе по Фамилии+Имени, но ТОЛЬКО среди контактов, у которых есть сделка
         в воронке бакалавриата (заготовки оператора) — прошлогодние тёзки без
         сделки в воронке так отсекаются;
       • телефон используем как различитель тёзок, но лишь когда он есть с обеих
         сторон (оператор телефон обычно не вводит);
       • иначе создаём новый контакт из 1С;
  3. на каждое заявление (конкурсную группу) обеспечиваем сделку: пустую сделку
     оператора заполняем (реюз), для остальных групп — отдельные сделки;
  4. пишем только изменившиеся поля; стадию и заголовок (оператор) не трогаем.

Проблемные случаи (тёзки, конфликт по телефону, несозданные, выбывшие, дубли)
НЕ угадываются, а собираются в stats для «разбора» (Excel-отчёт пишет CLI).

По умолчанию dry-run: показывает план, без записи. apply=True — пишет пачками.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .applicant import Applicant1C, build_applicants, build_applicants_superservice
from .bitrix_client import BitrixClient, extract_value
from .bitrix_fields import (
    DEAL_FIELDS_BACHELOR,
    DEAL_FIELDS_BY_LEVEL,
    DESIRED_CONTACT_FIELDS,
    enum_value_map,
    index_by_xml,
)
from .normalize import clean_str, name_key, normalize_phone
from .utils import now_iso

log = logging.getLogger("admissions")

# Префикс XML_ID полей сделки по уровню (бакалавриат «B», магистратура «M»).
LEVEL_PREFIX = {"bachelor": "B", "master": "M"}

# Атрибут заявления (1С) -> XML_ID поля сделки, по уровню. У магистратуры нет
# БВИ/Целевик/особое право/контроль (эти колонки убраны из выгрузки), «Кафедра» —
# операторское поле (в маппинге нет: sync его не заполняет).
APP_TO_XML_BY_LEVEL = {
    "bachelor": {
        "group": "B_GROUP", "no_exams": "B_BVI", "score": "B_SCORE", "priority": "B_PRIORITY",
        "score_id": "B_SCORE_ID", "basis": "B_BASIS", "targeted": "B_TARGETED",
        "consent": "B_CONSENT", "special": "B_SPECIAL", "app_date": "B_APP_DATE",
        "features": "B_FEATURES", "control": "B_CONTROL",
    },
    "master": {
        "group": "M_GROUP", "score": "M_SCORE", "priority": "M_PRIORITY",
        "score_id": "M_SCORE_ID", "basis": "M_BASIS", "consent": "M_CONSENT",
        "features": "M_FEATURES",
    },
}


def _phone_fmt(phone: Optional[str]) -> Optional[str]:
    if phone and phone.isdigit() and len(phone) == 11 and phone[0] == "7":
        return "+" + phone
    return phone


def _contact_name(c: Dict[str, Any]) -> str:
    return " ".join(x for x in [c.get("LAST_NAME"), c.get("NAME"), c.get("SECOND_NAME")] if x)


def _cphone(c: Dict[str, Any]) -> Optional[str]:
    return normalize_phone(extract_value(c.get("PHONE")))


def _tokens(text: Optional[str]) -> set:
    """Множество слов имени (нормализованных): регистр, ё→е, без пунктуации."""
    k = name_key(text)
    return set(k.split()) if k else set()


def _contact_tokens(c: Dict[str, Any]) -> set:
    """Все слова ФИО контакта из полей LAST_NAME/NAME/SECOND_NAME (любой порядок)."""
    return _tokens(" ".join(x for x in [c.get("LAST_NAME"), c.get("NAME"), c.get("SECOND_NAME")] if x))


def _match_by_name(a: Applicant1C, cands: List[Dict[str, Any]]):
    """Решение по кандидатам с тем же Фамилия+Имя (только контакты из воронки).

    Возвращает (action, contact|None, reason): action ∈
    'adopt' | 'create' | 'conflict' | 'ambiguous'. Телефон сравниваем только
    когда он есть и у абитуриента, и у контакта.
    """
    if not cands:
        return ("create", None, None)

    if a.phone:
        matching = [c for c in cands if _cphone(c) == a.phone]
        if len(matching) == 1:
            return ("adopt", matching[0], None)
        if len(matching) >= 2:
            return ("ambiguous", None, f"несколько контактов с тем же телефоном: {len(matching)}")
        # ни один телефон кандидатов не совпал с 1С
        no_phone = [c for c in cands if not _cphone(c)]
        if not no_phone:
            # у всех кандидатов телефон есть и он другой → вероятно другой человек
            ids = ", ".join("#" + str(c["ID"]) for c in cands)
            return ("conflict", None, f"ФИО совпало ({ids}), но телефон(ы) другие — возможно тёзка")
        if len(no_phone) == 1:
            return ("adopt", no_phone[0], None)
        return ("ambiguous", None, f"тёзки без телефона: {len(no_phone)} контакт(ов)")

    # телефона у абитуриента нет — различить нечем
    if len(cands) == 1:
        return ("adopt", cands[0], None)
    return ("ambiguous", None, f"тёзки: {len(cands)} контакт(ов)")


def _same(cur: Any, des: Any) -> bool:
    """Сравнение значений поля (с числовой устойчивостью к 291 / 291.00)."""
    sa = "" if cur is None else str(cur).strip()
    sb = "" if des is None else str(des).strip()
    if sa == sb:
        return True
    try:
        return float(sa.replace(",", ".")) == float(sb.replace(",", "."))
    except (ValueError, TypeError):
        return False


def _stage_rows(client: BitrixClient, category_id: int) -> List[Dict[str, Any]]:
    entity = "DEAL_STAGE" if category_id == 0 else f"DEAL_STAGE_{category_id}"
    r = client._call("crm.status.list",
                     {"filter": {"ENTITY_ID": entity}, "order": {"SORT": "ASC"}})
    return r.get("result", []) or []


def _require_fields(idx: Dict[str, Any], desired, entity: str) -> None:
    missing = [d["xml"] for d in desired if d["xml"] not in idx]
    if missing:
        raise RuntimeError(
            f"В Битриксе нет полей {entity} ({', '.join(missing)}). "
            f"Запустите `admissions setup-{entity}-fields --apply`."
        )


def _desired_deal(app: Dict[str, Any], code: str, dcode: Dict[str, str], now: str,
                  enum_maps: Dict[str, Dict[str, str]], app_to_xml: Dict[str, str],
                  code_xml: str, updated_xml: str) -> Dict[str, Any]:
    desired = {dcode[xml]: app.get(key) for key, xml in app_to_xml.items()}
    desired[dcode[code_xml]] = code
    desired[dcode[updated_xml]] = now
    for field_code, vmap in enum_maps.items():
        if field_code not in desired:
            continue
        val = desired[field_code]
        if val in vmap:
            desired[field_code] = vmap[val]
        elif not val and "Нет" in vmap:
            desired[field_code] = vmap["Нет"]
    return desired


def _deal_changes(deal: Dict[str, Any], desired: Dict[str, Any],
                  updated_field: str) -> Dict[str, Any]:
    changed = {}
    for code, value in desired.items():
        if code == updated_field:
            continue
        if not _same(deal.get(code), value):
            changed[code] = value
    if changed:
        changed[updated_field] = desired[updated_field]
    return changed


def _contact_fields(a: Applicant1C, code_field: str, type_id: str,
                    existing: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Поля для создания/обновления контакта. existing=None -> создание."""
    fields: Dict[str, Any] = {code_field: a.code}
    if type_id:
        fields["TYPE_ID"] = type_id       # стандартный «Тип контакта» (Абитуриенты)
    if existing is None:
        last, first, middle = a.name_parts
        fields.update({"NAME": first or "", "LAST_NAME": last or "", "SECOND_NAME": middle or ""})
    # телефон/почту проставляем при создании или если у контакта пусто
    if (existing is None or not extract_value(existing.get("PHONE"))) and a.phone:
        fields["PHONE"] = [{"VALUE": _phone_fmt(a.phone), "VALUE_TYPE": "MOBILE"}]
    if (existing is None or not extract_value(existing.get("EMAIL"))) and a.email:
        fields["EMAIL"] = [{"VALUE": a.email, "VALUE_TYPE": "WORK"}]
    return fields


def sync(cfg, apps_path: str | Path, apply: bool = False, client=None,
         level: str = "bachelor", source: str = "1c") -> Dict[str, Any]:
    client = client or BitrixClient.from_config(cfg)
    prefix = LEVEL_PREFIX.get(level, "B")
    app_to_xml = APP_TO_XML_BY_LEVEL.get(level, APP_TO_XML_BY_LEVEL["bachelor"])
    # Суперсервис не содержит баллов: исключаем их из записи, чтобы поля остались
    # пустыми (при создании) и не затёрлись у уже проставленных из 1С (при обновлении) —
    # баллы дозаполняет позже sync из 1С.
    if source == "superservice":
        app_to_xml = {k: v for k, v in app_to_xml.items() if k not in ("score", "score_id")}
    desired_fields = DEAL_FIELDS_BY_LEVEL.get(level, DEAL_FIELDS_BACHELOR)
    category_id = cfg.category_id_for(level)
    now = now_iso()

    # ── резолв полей по XML_ID ───────────────────────────────────────────────
    deal_idx = index_by_xml(client, "deal")
    contact_idx = index_by_xml(client, "contact")
    _require_fields(deal_idx, desired_fields, "deal")
    _require_fields(contact_idx, DESIRED_CONTACT_FIELDS, "contact")

    dcode = {xml: row["FIELD_NAME"] for xml, row in deal_idx.items()}
    deal_enum_maps = {
        row["FIELD_NAME"]: enum_value_map(row)
        for row in deal_idx.values()
        if row.get("USER_TYPE_ID") == "enumeration"
    }
    code_field = contact_idx["PK_CODE"]["FIELD_NAME"]
    contact_type_id = cfg.contact_type_id_for(level)  # «Тип контакта»: CLIENT=Абитуриенты, SUPPLIER=Магистры
    code_xml = f"{prefix}_CODE"
    updated_xml = f"{prefix}_UPDATED"
    group_field = dcode[f"{prefix}_GROUP"]
    basis_field = dcode[f"{prefix}_BASIS"]
    features_field = dcode[f"{prefix}_FEATURES"]
    special_field = dcode.get(f"{prefix}_SPECIAL")   # None у магистратуры (особого права нет)

    def dkey(group, basis, features, special):
        """Ключ заявления/сделки: группа + основание + особенности + особое право."""
        return (clean_str(group) or "", clean_str(basis) or "",
                clean_str(features) or "", clean_str(special) or "")

    if source == "superservice":
        applicants: List[Applicant1C] = build_applicants_superservice(
            apps_path, cfg.mapping_superservice or None)
    else:
        applicants = build_applicants(apps_path, cfg.columns_1c or None)
    export_codes = {a.code for a in applicants}

    # ── сделки воронки: по группам + пустые (реюз), множество контактов воронки ─
    deals = client.list_all(
        "crm.deal.list",
        filter={"CATEGORY_ID": category_id},
        select=["ID", "TITLE", "CONTACT_ID", "STAGE_ID"] + list(dcode.values()),
    )
    deals_group: Dict[str, Dict[tuple, Dict[str, Any]]] = defaultdict(dict)
    deals_blank: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    deal_dups: List[str] = []
    funnel_contact_ids: set = set()
    for d in deals:
        cid = str(d.get("CONTACT_ID") or "")
        if not cid or cid == "0":
            continue
        funnel_contact_ids.add(cid)
        g = clean_str(d.get(group_field)) or ""
        if g:
            key = dkey(d.get(group_field), d.get(basis_field), d.get(features_field), d.get(special_field))
            if key in deals_group[cid]:
                deal_dups.append(f"контакт {cid}: {' / '.join(p for p in key if p)}")
            deals_group[cid][key] = d
        else:
            deals_blank[cid].append(d)

    # глобальный индекс сделок по (код+ключ) по ВСЕМ контактам: чтобы «осиротевшую»
    # сделку (заявление уже в воронке, но на другом контакте) перепривязать к нужному
    # контакту, а не создать дубль (защита от раздвоения контактов).
    deals_by_codekey: Dict[Tuple[str, tuple], List[Dict[str, Any]]] = defaultdict(list)
    for d in deals:
        dc = clean_str(d.get(dcode[code_xml]))
        g = clean_str(d.get(group_field))
        if dc and g:
            deals_by_codekey[(dc, dkey(d.get(group_field), d.get(basis_field),
                                       d.get(features_field), d.get(special_field)))].append(d)

    # ── контакты: индекс по коду (все) и по ФИО (только контакты воронки) ──────
    contacts = client.list_all(
        "crm.contact.list",
        select=["ID", "NAME", "LAST_NAME", "SECOND_NAME", "PHONE", "EMAIL", "TYPE_ID", code_field],
    )
    by_code: Dict[str, Dict[str, Any]] = {}
    # заготовки оператора в воронке: (множество слов ФИО, контакт) — порядок неважен
    funnel_stubs: List[Tuple[set, Dict[str, Any]]] = []
    code_dups: set = set()
    for c in contacts:
        code = clean_str(c.get(code_field))
        ctype = str(c.get("TYPE_ID") or "")
        if code:
            # только контакты СВОЕГО уровня (или ещё не типизированные): чтобы sync
            # магистратуры не трогал контакты бакалавриата (и наоборот), а «выбывшие»
            # считались в пределах уровня.
            if ctype not in ("", contact_type_id):
                continue
            if code in by_code:
                code_dups.add(code)
            by_code[code] = c
        elif str(c["ID"]) in funnel_contact_ids:  # заготовка оператора в воронке
            ts = _contact_tokens(c)
            if ts:
                funnel_stubs.append((ts, c))

    stage_rows = _stage_rows(client, category_id)
    stage_by_name = {s["NAME"]: {"code": s["STATUS_ID"], "sort": int(s["SORT"])} for s in stage_rows}
    stage_sort = {s["STATUS_ID"]: int(s["SORT"]) for s in stage_rows}
    first_stage = stage_rows[0]["STATUS_ID"] if stage_rows else None
    stage_application = (stage_by_name.get(cfg.stage_on_application, {}).get("code")
                         or cfg.default_stage_id or first_stage)
    stage_contacted = stage_by_name.get(cfg.stage_on_contacted)  # {code, sort} | None
    stage_name = {s["STATUS_ID"]: s["NAME"] for s in stage_rows}
    stage_sem = {s["STATUS_ID"]: s.get("SEMANTICS") for s in stage_rows}
    stage_withdrawn = stage_by_name.get(cfg.stage_on_withdrawn) if cfg.stage_on_withdrawn else None

    stats: Dict[str, Any] = {
        "applicants": len(applicants),
        "applications": sum(len(a.applications) for a in applicants),
        "matched_by_code": 0, "adopted": 0, "created_contacts": 0,
        "ambiguous": [], "conflicts": [], "failed": [], "dropped": [], "withdrawn": [],
        "created_deals": 0, "filled_deals": 0, "updated_deals": 0, "withdrawn_moved": 0,
        "relinked_deals": 0,
        "code_dups": sorted(code_dups), "deal_dups": deal_dups, "examples": [],
    }

    # выбывшие: код есть на контакте, но его нет в новой выгрузке.
    # Для суперсервиса состав неполон относительно 1С → «выбывших» по нему не считаем.
    if source != "superservice":
        for code, c in by_code.items():
            if code not in export_codes:
                stats["dropped"].append({"code": code, "id": str(c["ID"]), "name": _contact_name(c)})

    code_field_deal = dcode[code_xml]
    score_field = dcode.get(f"{prefix}_SCORE")
    withdrawn_ops: List[Tuple[str, Dict[str, Any]]] = []

    def _mark_withdrawn(d: Dict[str, Any], k: tuple) -> None:
        """В отчёт + авто-перенос сделки в стадию «отозвано» (кроме зачисленных/там же)."""
        sid = d.get("STAGE_ID")
        stats["withdrawn"].append({
            "code": clean_str(d.get(code_field_deal)), "deal": d["ID"],
            "key": " / ".join(p for p in k if p), "stage": stage_name.get(sid, sid)})
        if stage_withdrawn and sid != stage_withdrawn["code"] and stage_sem.get(sid) != "S":
            stats["withdrawn_moved"] += 1
            withdrawn_ops.append(("crm.deal.update",
                                  {"id": d["ID"], "fields": {"STAGE_ID": stage_withdrawn["code"]}}))

    if source != "superservice":
        # 1С: отозвано = сделка с кодом в воронке, ключа которой НЕТ в новой выгрузке.
        # Но только сделки, реально ведомые 1С (с проставленным баллом): свежие сделки
        # из суперсервиса (баллы ещё не дозаполнены) ложно отзывать нельзя.
        export_keys = {(a.code, dkey(app.get("group"), app.get("basis"), app.get("features"),
                                     app.get("special") if special_field else ""))
                       for a in applicants for app in a.applications}
        for d in deals:
            dc = clean_str(d.get(code_field_deal))
            if not dc:
                continue  # пустая операторская сделка — не отозвана
            k = dkey(d.get(group_field), d.get(basis_field), d.get(features_field), d.get(special_field))
            if (dc, k) in export_keys:
                continue
            if score_field and not clean_str(d.get(score_field)):
                continue  # без балла — не 1С-сделка (напр. из суперсервиса) → не отзываем
            _mark_withdrawn(d, k)
    # Для суперсервиса «отсутствие в выгрузке» отзывом НЕ считаем (файл частичный
    # относительно 1С); отозванные заявки помечены флагом и обрабатываются в фазе B.

    # ── фаза A: классификация контактов ──────────────────────────────────────
    contact_creates: List[Applicant1C] = []
    contact_updates: List[Tuple[str, Dict[str, Any]]] = []
    targets_pre: List[Tuple[Applicant1C, Optional[str]]] = []

    for a in applicants:
        if a.code in by_code:
            stats["matched_by_code"] += 1
            targets_pre.append((a, str(by_code[a.code]["ID"])))
            continue
        last, first, _mid = a.name_parts
        app_tokens = _tokens(f"{last or ''} {first or ''}")
        # матч по набору слов Фамилия+Имя (требуем оба слова, порядок/поле неважны)
        cands = ([c for ts, c in funnel_stubs if app_tokens <= ts]
                 if len(app_tokens) >= 2 else [])
        action, c, reason = _match_by_name(a, cands)
        if action == "adopt":
            cid = str(c["ID"])
            contact_updates.append((cid, _contact_fields(a, code_field, contact_type_id, c)))
            stats["adopted"] += 1
            targets_pre.append((a, cid))
        elif action == "ambiguous":
            stats["ambiguous"].append({"code": a.code, "name": a.full_name, "reason": reason})
        else:  # create | conflict — оба создают новый контакт
            contact_creates.append(a)
            stats["created_contacts"] += 1
            targets_pre.append((a, None))
            if action == "conflict":
                stats["conflicts"].append({"code": a.code, "name": a.full_name, "reason": reason})

    # ── применение контактов + проверка, что реально записалось ──────────────
    if apply:
        if contact_creates:
            client.batch_call([("crm.contact.add",
                                {"fields": _contact_fields(a, code_field, contact_type_id)})
                               for a in contact_creates])
        if contact_updates:
            client.batch_call([("crm.contact.update", {"id": cid, "fields": f})
                               for cid, f in contact_updates])

        # перечитать контакты по коду — не доверяем ID из batch (контроль дублей
        # Битрикса может вернуть «фантомный» ID без реальной записи)
        refreshed = client.list_all("crm.contact.list",
                                    filter={"!=" + code_field: ""}, select=["ID", code_field])
        by_code_now = {clean_str(c.get(code_field)): str(c["ID"])
                       for c in refreshed if clean_str(c.get(code_field))}
        targets: List[Tuple[Applicant1C, str]] = []
        for a, _pre in targets_pre:
            cid = by_code_now.get(a.code)
            if cid:
                targets.append((a, cid))
            else:
                stats["failed"].append({
                    "code": a.code, "name": a.full_name,
                    "reason": "контакт не создан (возможно дубль по телефону/почте) — проверьте вручную",
                })
        stats["created_contacts"] = sum(1 for a in contact_creates if a.code in by_code_now)
    else:
        targets = targets_pre  # dry-run: создаваемые контакты с cid=None

    # ── фаза B: сделки по группам (реюз пустых сделок оператора) ──────────────
    deal_ops: List[Tuple[str, Dict[str, Any]]] = []
    relinked_ids: set = set()  # сделки, уже перепривязанные в этом прогоне (не трогать повторно)

    def _wd_stage(sid: Optional[str]) -> Optional[str]:
        """Стадия «отозвано» для отозванной суперсервис-заявки (кроме зачисленных/там же)."""
        if stage_withdrawn and stage_sem.get(sid) != "S" and sid != stage_withdrawn["code"]:
            return stage_withdrawn["code"]
        return None

    def _wd_report(d: Dict[str, Any], k: tuple) -> None:
        """Существующую сделку отозванной заявки — в отчёт «Отозванные»."""
        sid = d.get("STAGE_ID")
        stats["withdrawn"].append({
            "code": clean_str(d.get(code_field_deal)), "deal": d["ID"],
            "key": " / ".join(p for p in k if p), "stage": stage_name.get(sid, sid)})

    for a, cid in targets:
        grp = deals_group.get(cid, {}) if cid else {}
        blanks = list(deals_blank.get(cid, [])) if cid else []
        for app in a.applications:
            group = app.get("group") or ""
            # суперсервис: заявка помечена «Отозвано» → её сделку ведём в стадию «отозвано»
            is_wd = bool(source == "superservice" and app.get("withdrawn"))
            key = dkey(app.get("group"), app.get("basis"), app.get("features"),
                       app.get("special") if special_field else "")
            desired = _desired_deal(app, a.code, dcode, now, deal_enum_maps,
                                    app_to_xml, code_xml, updated_xml)
            found = grp.get(key)
            if found:  # сделка для этого заявления уже есть → обновить изменения
                changed = _deal_changes(found, desired, dcode[updated_xml])
                wd = _wd_stage(found.get("STAGE_ID")) if is_wd else None
                if wd:
                    changed["STAGE_ID"] = wd
                    stats["withdrawn_moved"] += 1
                if is_wd:
                    _wd_report(found, key)
                if changed:
                    stats["updated_deals"] += 1
                    if apply:
                        deal_ops.append(("crm.deal.update", {"id": found["ID"], "fields": changed}))
                    _example(stats, "update", a, group, len(changed) - 1)
            elif blanks:  # заполнить пустую сделку оператора (реюз) → «Связались»
                d = blanks.pop(0)
                changed = _deal_changes(d, desired, dcode[updated_xml])
                wd = _wd_stage(d.get("STAGE_ID")) if is_wd else None
                if wd:  # отозванная заявка → сразу «отозвано» (приоритетнее «Связались»)
                    changed["STAGE_ID"] = wd
                    stats["withdrawn_moved"] += 1
                elif stage_contacted and stage_sort.get(d.get("STAGE_ID"), 0) < stage_contacted["sort"]:
                    # перенос вперёд: контакт был → «Связались», если стадия раньше
                    changed["STAGE_ID"] = stage_contacted["code"]
                if is_wd:
                    _wd_report(d, key)
                stats["filled_deals"] += 1
                if apply and changed:
                    deal_ops.append(("crm.deal.update", {"id": d["ID"], "fields": changed}))
                _example(stats, "fill", a, group, len(changed))
            elif cid and [d for d in deals_by_codekey.get((a.code, key), [])
                          if str(d.get("CONTACT_ID") or "") != cid and d["ID"] not in relinked_ids]:
                # сделка (код+ключ) уже есть, но на ДРУГОМ контакте (раздвоение) →
                # перепривязать к правильному контакту, а не плодить дубль.
                strays = [d for d in deals_by_codekey.get((a.code, key), [])
                          if str(d.get("CONTACT_ID") or "") != cid and d["ID"] not in relinked_ids]
                stray = min(strays, key=lambda d: int(d["ID"]))
                relinked_ids.add(stray["ID"])
                changed = _deal_changes(stray, desired, dcode[updated_xml])
                changed["CONTACT_ID"] = cid
                wd = _wd_stage(stray.get("STAGE_ID")) if is_wd else None
                if wd:
                    changed["STAGE_ID"] = wd
                    stats["withdrawn_moved"] += 1
                if is_wd:
                    _wd_report(stray, key)
                stats["relinked_deals"] += 1
                if apply:
                    deal_ops.append(("crm.deal.update", {"id": stray["ID"], "fields": changed}))
                _example(stats, "relink", a, group, len(changed))
            else:  # новая сделка → «Поступившие заявления» (или сразу «отозвано»)
                stats["created_deals"] += 1
                create = {k: v for k, v in desired.items() if v not in (None, "")}
                create["TITLE"] = _deal_title(a, app)
                create["CATEGORY_ID"] = category_id
                if cid:
                    create["CONTACT_ID"] = cid
                target_stage = (stage_withdrawn["code"] if (is_wd and stage_withdrawn)
                                else stage_application)
                if target_stage:
                    create["STAGE_ID"] = target_stage
                if is_wd:
                    stats["withdrawn"].append({
                        "code": a.code, "deal": "(новая)",
                        "key": " / ".join(p for p in key if p),
                        "stage": stage_name.get(target_stage, target_stage)})
                if apply and cid:
                    deal_ops.append(("crm.deal.add", {"fields": create}))
                _example(stats, "create", a, group, len(create))

    if apply:
        ops = deal_ops + withdrawn_ops
        if ops:
            for r in client.batch_call(ops):
                if r.get("error"):
                    log.error("сделка: %s", r["error"])

    stats["problems"] = (len(stats["ambiguous"]) + len(stats["conflicts"]) + len(stats["failed"])
                         + len(stats["dropped"]) + len(stats["withdrawn"])
                         + len(stats["code_dups"]) + len(stats["deal_dups"]))
    return stats


def _deal_title(a: Applicant1C, app: Dict[str, Any]) -> str:
    """Заголовок сделки: ФИО — группа / основание [ / особенности, если не общие]."""
    parts = [app.get("group") or ""]
    if app.get("basis"):
        parts.append(app["basis"])
    feat = app.get("features")
    if feat and feat != "Общие места":
        parts.append(feat)
    if app.get("special"):
        parts.append("особое право")
    tail = " / ".join(p for p in parts if p)
    name = a.full_name or a.code
    return f"{name} — {tail}".strip(" —")


def _example(stats: Dict[str, Any], action: str, a: Applicant1C, group: str, fields: int) -> None:
    if len(stats["examples"]) < 10:
        stats["examples"].append({"action": action, "code": a.code, "group": group, "fields": fields})
