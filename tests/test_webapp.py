"""Тесты веб-слоя (Flask) — без обращения к Битриксу (sync/export замоканы)."""

from io import BytesIO
from pathlib import Path

import pytest

from admissions import webapp


@pytest.fixture(autouse=True)
def _clear_auth_env(monkeypatch):
    # тесты не должны зависеть от локального .env (APP_PASSWORD и т.п.)
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    monkeypatch.delenv("APP_USERNAME", raising=False)


@pytest.fixture
def client():
    webapp.app.config.update(TESTING=True)
    return webapp.app.test_client()


def _stats(**over):
    base = {"applicants": 3, "applications": 4, "matched_by_code": 1, "adopted": 1, "created_contacts": 1,
            "created_deals": 2, "filled_deals": 1, "updated_deals": 0,
            "ambiguous": [], "conflicts": [{"code": "5", "name": "Тёзка Т.", "reason": "телефон другой"}],
            "failed": [], "dropped": [], "withdrawn": [], "withdrawn_moved": 0,
            "code_dups": [], "deal_dups": []}
    base.update(over)
    return base


def test_prefix_middleware_strips_prefix():
    from admissions.webapp import _PrefixMiddleware
    seen = {}

    def dummy(environ, start_response):
        seen["script"] = environ.get("SCRIPT_NAME")
        seen["path"] = environ.get("PATH_INFO")
        return []

    mw = _PrefixMiddleware(dummy, "pk2026")
    mw({"PATH_INFO": "/pk2026/preview", "SCRIPT_NAME": ""}, lambda *a: None)
    assert seen == {"script": "/pk2026", "path": "/preview"}
    # ровно префикс без хвоста -> корень
    mw({"PATH_INFO": "/pk2026", "SCRIPT_NAME": ""}, lambda *a: None)
    assert seen == {"script": "/pk2026", "path": "/"}
    # чужой путь не трогаем
    mw({"PATH_INFO": "/other", "SCRIPT_NAME": ""}, lambda *a: None)
    assert seen == {"script": "", "path": "/other"}


def test_auth_open_when_no_password(client, monkeypatch):
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    assert client.get("/").status_code == 200


def test_auth_required_when_password_set(client, monkeypatch):
    import base64
    monkeypatch.setenv("APP_PASSWORD", "secret")
    monkeypatch.setenv("APP_USERNAME", "admin")
    assert client.get("/").status_code == 401                      # без пароля — 401
    token = base64.b64encode(b"admin:secret").decode()
    r = client.get("/", headers={"Authorization": f"Basic {token}"})
    assert r.status_code == 200                                    # с паролем — ок
    bad = base64.b64encode(b"admin:wrong").decode()
    assert client.get("/", headers={"Authorization": f"Basic {bad}"}).status_code == 401


def test_index_shows_upload_and_levels(client):
    r = client.get("/")
    body = r.get_data(as_text=True)
    assert r.status_code == 200
    assert "Загрузка из 1С" in body
    assert "Базовое высшее" in body            # активный уровень
    assert "пока не настроен" in body          # маг/асп заглушки


def test_preview_renders_summary(client, monkeypatch):
    monkeypatch.setattr(webapp, "sync",
                        lambda cfg, path, apply=False, level="bachelor", source="1c": _stats())
    monkeypatch.setattr(webapp, "_save_upload", lambda f: Path("upload_test.xlsx"))
    monkeypatch.setattr(webapp, "_write_report", lambda s: "razbor.xlsx")
    data = {"level": "bachelor", "source": "1c", "file": (BytesIO(b"x"), "otchet.xlsx")}
    r = client.post("/preview", data=data, content_type="multipart/form-data")
    body = r.get_data(as_text=True)
    assert r.status_code == 200
    assert "Предпросмотр" in body and "Применить" in body
    assert "Конфликт ФИО" in body               # раздел разбора отрендерился
    assert "upload_test.xlsx" in body           # имя файла проброшено в форму «Применить»


def test_preview_rejects_disabled_level(client, monkeypatch):
    # аспирантура ещё выключена (enabled: false) -> отклоняем
    monkeypatch.setattr(webapp, "_save_upload", lambda f: Path("x.xlsx"))
    data = {"level": "postgrad", "file": (BytesIO(b"x"), "o.xlsx")}
    r = client.post("/preview", data=data, content_type="multipart/form-data")
    assert r.status_code == 400
    assert "не настроен" in r.get_data(as_text=True)


def test_apply_refuses_concurrent_run(client):
    # если синк уже идёт -> второй «Применить» отклоняется (защита от гонки/дублей при 504)
    webapp._JOB.clear(); webapp._JOB.update(state="running", title="Бакалавриат")
    try:
        r = client.post("/apply", data={"level": "bachelor", "file": "x.xlsx"},
                        content_type="multipart/form-data")
        assert r.status_code == 409
        assert "заблокирован" in r.get_data(as_text=True)
    finally:
        webapp._JOB.clear(); webapp._JOB.update(state="idle")


def test_export_redirects_to_download(client, monkeypatch):
    def fake_export(cfg, out, client=None, level="bachelor"):
        Path(out).write_text("x")
        return Path(out)
    monkeypatch.setattr(webapp, "export_deals", fake_export)
    r = client.get("/export")
    assert r.status_code == 302
    assert "/download/deals_export_" in r.headers["Location"]
