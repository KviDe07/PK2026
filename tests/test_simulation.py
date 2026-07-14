"""Тесты симулятора зачисления: инварианты движка и e2e (парсинг + расчёт)."""

import pandas as pd
import pytest

from admissions.simulation import COL, build_apps, simulate, simulate_admission


# ── движок simulate() на синтетических заявлениях ─────────────────────────────

def _e(kg, ss, prio=1, bvi=False, sp=None, si=0.0,
       otd=False, cel=False, osob=False, in_general=True):
    return {'kg': kg, 'prio': prio, 'sp': float(sp if sp is not None else ss),
            'si': float(si), 'ss': float(ss), 'bvi': bvi, 'in_general': in_general,
            'otd': otd, 'cel': cel, 'osob': osob}


def test_capacity_and_scores():
    # 1 общее место, двое подали → проходит только с высшим баллом
    kcp = {"A": (1, 0, 0, 0)}
    apps = {"hi": [_e("A", ss=300)], "lo": [_e("A", ss=250)]}
    res, _, _ = simulate(apps, {"hi": True, "lo": True}, kcp)
    assert res["hi"]["kg"] == "A" and res["hi"]["type"] == "general"
    assert "lo" not in res


def test_bvi_outranks_higher_score():
    # БВИ вне конкурса по баллам: проходит даже с низким баллом против высокобалльника
    kcp = {"A": (1, 0, 0, 0)}
    apps = {"bvi": [_e("A", ss=100, bvi=True)], "high": [_e("A", ss=300)]}
    res, _, _ = simulate(apps, {"bvi": True, "high": True}, kcp)
    assert res["bvi"]["type"] == "bvi"
    assert "high" not in res


def test_unused_quota_flows_to_general():
    # 0 общих мест, 1 отдельная квота, но отдельников нет → место перетекает в общий
    kcp = {"A": (0, 0, 1, 0)}
    apps = {"c": [_e("A", ss=200, otd=False, in_general=True)]}
    res, gen_cap, _ = simulate(apps, {"c": True}, kcp)
    assert gen_cap["A"] == 1
    assert res["c"]["kg"] == "A" and res["c"]["type"] == "general"


def test_priority_deferred_acceptance():
    # x проходит на высший приоритет A и освобождает B для y
    kcp = {"A": (1, 0, 0, 0), "B": (1, 0, 0, 0)}
    apps = {
        "x": [_e("A", ss=300, prio=1), _e("B", ss=300, prio=2)],
        "y": [_e("B", ss=250, prio=1)],
    }
    res, _, _ = simulate(apps, {"x": True, "y": True}, kcp)
    assert res["x"]["kg"] == "A"
    assert res["y"]["kg"] == "B"


def test_only_consent_filter():
    kcp = {"A": (1, 0, 0, 0)}
    apps = {"c": [_e("A", ss=300)]}
    assert simulate(apps, {"c": False}, kcp, only_consent=True)[0] == {}
    assert simulate(apps, {"c": False}, kcp, only_consent=False)[0]["c"]["kg"] == "A"


def test_separate_quota_used_first():
    # отдельная квота занимается раньше общего конкурса
    kcp = {"A": (1, 0, 1, 0)}
    apps = {
        "q": [_e("A", ss=100, otd=True)],     # квотник со слабым баллом
        "g": [_e("A", ss=300)],               # общий, сильный балл
    }
    res, _, used = simulate(apps, {"q": True, "g": True}, kcp)
    assert res["q"]["type"] == "quota_otd"    # занял квотное место
    assert res["g"]["type"] == "general"      # и общий тоже прошёл
    assert used["otd"]["A"] == 1


# ── build_apps() парсинг заявлений ────────────────────────────────────────────

def _row(code, kg, ss, sp, si, prio=1, special="Общие места", basis="Бюджетная основа",
         bvi="", consent="✓", control="✓"):
    return {COL['group']: kg, COL['bvi']: bvi, COL['consent']: consent, COL['priority']: prio,
            COL['sp']: sp, COL['si']: si, COL['ss']: ss, COL['code']: code,
            COL['fio']: "Тест Тест Тест", COL['special']: special, COL['basis']: basis,
            COL['control']: control}


def _prep(records):
    """Сэмулировать колонки, которые готовит load()."""
    df = pd.DataFrame(records)
    df['КГ'] = df[COL['group']]
    df['_bvi'] = df[COL['bvi']] == '✓'
    df['_consent'] = df[COL['consent']] == '✓'
    df['_control'] = df[COL['control']] == '✓'
    df['_prio'] = pd.to_numeric(df[COL['priority']], errors='coerce')
    df['_sp'] = pd.to_numeric(df[COL['sp']], errors='coerce').fillna(0)
    df['_si'] = pd.to_numeric(df[COL['si']], errors='coerce').fillna(0)
    df['_ss'] = pd.to_numeric(df[COL['ss']], errors='coerce').fillna(0)
    df['_code'] = df[COL['code']]
    return df


def test_build_apps_control_filter():
    kcp = {"A": (10, 0, 0, 0)}
    df = _prep([
        _row("1", "A", 300, 290, 10, control="✓"),
        _row("2", "A", 250, 240, 10, control=""),   # без контроля — выбывает
    ])
    apps, consent, _ = build_apps(df, kcp, require_control=True)
    assert "1" in apps and "2" not in apps
    # без фильтра контроля — оба
    apps2, _, _ = build_apps(df, kcp, require_control=False)
    assert "1" in apps2 and "2" in apps2


def test_build_apps_targeted_is_quota():
    kcp = {"A": (10, 0, 0, 5)}
    df = _prep([_row("1", "A", 300, 290, 10, basis="Целевой прием")])
    apps, _, _ = build_apps(df, kcp, require_control=True)
    assert apps["1"][0]["cel"] is True


# ── e2e: реальный формат (шапка не в первой строке) ───────────────────────────

def test_end_to_end_autodetect_header(cfg, tmp_path):
    # реальная группа из КЦП, шапка со сдвигом (как в выгрузке 1С) — load её найдёт
    group = "Техническая физика"
    assert group in cfg.kcp, "тестовая группа должна быть в config/kcp_bachelor.yaml"
    recs = [_row("1", group, 300, 290, 10), _row("2", group, 250, 240, 10)]
    path = tmp_path / "vygruzka.xlsx"
    with pd.ExcelWriter(path) as w:
        pd.DataFrame(recs).to_excel(w, sheet_name="Лист_1", startrow=5, index=False)

    out = tmp_path / "result.xlsx"
    res = simulate_admission(cfg, path, out, only_consent=True, require_control=True)
    assert res["enrolled"] == 2           # мест в группе много — оба проходят
    assert res["with_consent"] == 2
    assert out.exists()
