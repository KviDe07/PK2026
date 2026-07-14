"""Веб-приложение приёмной комиссии ФАКТ МФТИ (Flask).

Для сотрудников: загрузка отчёта 1С → предпросмотр изменений → «Применить»
(запись в Битрикс), а также выгрузка сделок воронки в Excel.

Запуск (разработка):   python -m admissions.webapp
Запуск (сервер):       admissions-web   (waitress)  или через gunicorn: admissions.webapp:app

Уровень (бакалавриат/магистратура/аспирантура) выбирается в интерфейсе; активные
уровни задаются в config/settings.yaml -> levels. Сейчас активен бакалавриат.
"""

from __future__ import annotations

import hmac
import html
import logging
import os
import threading
from pathlib import Path

from flask import Flask, Response, abort, redirect, render_template_string, request, send_from_directory, url_for
from werkzeug.utils import secure_filename

from .config import Config
from .export import export_deals
from .report import write_problem_report
from .sync import sync
from .utils import setup_logging, timestamp_slug

log = logging.getLogger("admissions")
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024  # 25 МБ на файл

_CFG = Config.load()  # load_dotenv внутри — .env уже подхвачен


# ── работа под под-путём (например fakt.mipt.ru/pk2026) ───────────────────────
# nginx проксирует /pk2026 → контейнер, передавая полный путь. Middleware
# переносит префикс из PATH_INFO в SCRIPT_NAME, тогда роуты совпадают, а url_for
# генерирует ссылки уже с префиксом. Префикс задаётся переменной APP_URL_PREFIX.

class _PrefixMiddleware:
    def __init__(self, wsgi_app, prefix: str):
        self.wsgi_app = wsgi_app
        self.prefix = "/" + prefix.strip("/")

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO", "")
        if path == self.prefix or path.startswith(self.prefix + "/"):
            environ["SCRIPT_NAME"] = self.prefix + environ.get("SCRIPT_NAME", "")
            environ["PATH_INFO"] = path[len(self.prefix):] or "/"
        return self.wsgi_app(environ, start_response)


_PREFIX = os.environ.get("APP_URL_PREFIX", "").strip("/")
if _PREFIX:
    app.wsgi_app = _PrefixMiddleware(app.wsgi_app, _PREFIX)


# ── авторизация (HTTP Basic) ──────────────────────────────────────────────────
# Если в .env задан APP_PASSWORD — вход обязателен. Если не задан (локальная
# разработка) — приложение открыто. Логин по умолчанию «admin» (APP_USERNAME).

def _auth_ok() -> bool:
    pw = os.environ.get("APP_PASSWORD", "")
    if not pw:
        return True  # пароль не задан — открыто
    user = os.environ.get("APP_USERNAME", "admin")
    a = request.authorization
    return bool(a) and (a.type or "").lower() == "basic" and a.username == user \
        and hmac.compare_digest(a.password or "", pw)


@app.before_request
def _require_auth():
    if not _auth_ok():
        return Response("Требуется вход", 401, {"WWW-Authenticate": 'Basic realm="Priyomka FAKT"'})

CSS = """
:root{--fg:#1a2230;--mut:#6b7280;--line:#e5e7eb;--acc:#2563eb;--ok:#059669;--warn:#b45309;--bg:#f7f8fa}
*{box-sizing:border-box}body{font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;color:var(--fg);background:var(--bg);margin:0}
.wrap{max-width:960px;margin:0 auto;padding:28px 20px 60px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:26px 0 10px}
.sub{color:var(--mut);margin:0 0 22px}
.card{background:#fff;border:1px solid var(--line);border-radius:12px;padding:20px;margin:14px 0}
label{display:block;font-weight:600;margin:0 0 6px}
select,input[type=file]{width:100%;padding:9px;border:1px solid var(--line);border-radius:8px;background:#fff}
.row{display:flex;gap:16px;flex-wrap:wrap}.row>div{flex:1;min-width:220px}
button,.btn{display:inline-block;border:0;border-radius:8px;padding:10px 18px;font:inherit;font-weight:600;cursor:pointer;text-decoration:none}
.btn-primary{background:var(--acc);color:#fff}.btn-ok{background:var(--ok);color:#fff}
.btn-ghost{background:#eef1f5;color:var(--fg)}
.muted{color:var(--mut)}
table{border-collapse:collapse;width:100%;margin:8px 0;font-size:14px}
th,td{border:1px solid var(--line);padding:6px 9px;text-align:left;vertical-align:top}
th{background:#f2f4f7}
.stat{display:flex;gap:22px;flex-wrap:wrap;margin:6px 0 2px}
.stat b{font-size:20px;display:block}.stat span{color:var(--mut);font-size:13px}
.pill{display:inline-block;padding:2px 9px;border-radius:20px;font-size:12px;font-weight:600}
.pill-ok{background:#dcfce7;color:var(--ok)}.pill-warn{background:#fef3c7;color:var(--warn)}
.flash{padding:12px 14px;border-radius:8px;margin:10px 0}
.flash-err{background:#fee2e2;color:#991b1b}.flash-ok{background:#dcfce7;color:#065f46}
a{color:var(--acc)}
"""


def render(page_title, inner, **ctx):
    body = render_template_string(inner, **ctx)
    return (f"<!doctype html><html lang=ru><head><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(page_title)}</title><style>{CSS}</style></head>"
            f"<body><div class=wrap><h1>Приёмная кампания — ФАКТ МФТИ</h1>{body}</div></body></html>")


# ── главная ───────────────────────────────────────────────────────────────────

INDEX = """
<p class=sub>Загрузка отчёта 1С в Битрикс и выгрузка сделок воронки.</p>

<div class=card>
  <h2 style=margin-top:0>Загрузка из 1С</h2>
  <form method=post action="{{ url_for('preview') }}" enctype=multipart/form-data class=row>
    <div>
      <label>Уровень поступления</label>
      <select name=level required>
        {% for key, lv in levels.items() %}
          <option value="{{ key }}" {{ '' if lv.enabled else 'disabled' }}>
            {{ lv.title }}{{ '' if lv.enabled else ' — пока не настроен' }}
          </option>
        {% endfor %}
      </select>
    </div>
    <div>
      <label>Источник заявлений</label>
      <select name=source required>
        <option value="1c">Выгрузка 1С</option>
        <option value="superservice">Суперсервис (ЕПГУ) — без баллов</option>
      </select>
    </div>
    <div>
      <label>Файл выгрузки (.xls / .xlsx)</label>
      <input type=file name=file accept=".xls,.xlsx" required>
    </div>
    <div style="flex:0 0 100%">
      <button class=btn-primary type=submit>Предпросмотр</button>
      <span class=muted>Сначала покажем, что изменится — записи ещё не будет.</span>
    </div>
  </form>
</div>

<div class=card>
  <h2 style=margin-top:0>Выгрузка из Битрикса</h2>
  <p class=muted>Все сделки воронки в Excel: контакт, данные заявления, стадия, комментарии.</p>
  {% for key, lv in levels.items() %}
    {% if lv.enabled %}
      <a class="btn btn-ghost" href="{{ url_for('export') }}?level={{ key }}">Выгрузить: {{ lv.title }}</a>
    {% endif %}
  {% endfor %}
</div>

<div class=card>
  <h2 style=margin-top:0>Симуляция зачисления (бакалавриат)</h2>
  <p class=muted>Прогноз проходных по выгрузке с баллами (лист с «Уникальный код», баллами по
     предметам/ИД и приоритетами). Результат — Excel: сводка + листы по группам с подсветкой проходящих.</p>
  <form method=post action="{{ url_for('simulate') }}" enctype=multipart/form-data class=row>
    <div>
      <label>Файл выгрузки (.xls / .xlsx)</label>
      <input type=file name=file accept=".xls,.xlsx" required>
    </div>
    <div style="flex:0 0 100%">
      <label style="font-weight:400;display:inline-block;margin-right:20px">
        <input type=checkbox name=only_consent value=1 checked> только с согласием на зачисление
      </label>
      <label style="font-weight:400;display:inline-block">
        <input type=checkbox name=require_control value=1 checked> только прошедшие «Контроль пройден»
      </label>
    </div>
    <div style="flex:0 0 100%">
      <button class=btn-primary type=submit>Рассчитать и скачать</button>
      <span class=muted>Расчёт может занять несколько секунд.</span>
    </div>
  </form>
</div>
"""


@app.get("/")
def index():
    return render("Приёмная комиссия", INDEX, levels=_CFG.levels, url_for=url_for)


# ── предпросмотр (dry-run) ────────────────────────────────────────────────────

def _check_level(level: str):
    lv = _CFG.levels.get(level)
    if not lv or not lv.get("enabled"):
        abort(400, f"Уровень «{level}» пока не настроен")
    return lv


def _save_upload(file) -> Path:
    name = secure_filename(file.filename or "")
    if not name.lower().endswith((".xls", ".xlsx")):
        abort(400, "Ожидается файл .xls или .xlsx")
    _CFG.input_dir.mkdir(parents=True, exist_ok=True)
    dest = _CFG.input_dir / f"upload_{timestamp_slug()}_{name}"
    file.save(dest)
    return dest


SUMMARY = """
<div class=stat>
  <div><b>{{ s.applicants }}</b><span>абитуриентов в 1С</span></div>
  <div><b>{{ s.applications }}</b><span>заявлений (код+группа)</span></div>
  <div><b>{{ s.matched_by_code }}</b><span>по коду</span></div>
  <div><b>{{ s.adopted }}</b><span>заготовки приняты</span></div>
  <div><b>{{ s.created_contacts }}</b><span>контактов создать</span></div>
  <div><b>{{ s.created_deals }}</b><span>сделок создать</span></div>
  <div><b>{{ s.filled_deals }}</b><span>пустых заполнить</span></div>
  <div><b>{{ s.updated_deals }}</b><span>сделок обновить</span></div>
</div>
{% macro tbl(title, rows) %}
  {% if rows %}
    <h2>{{ title }} — {{ rows|length }}</h2>
    <table><tr><th>Код</th><th>ФИО</th><th>Причина</th></tr>
    {% for r in rows %}<tr><td>{{ r.code }}</td><td>{{ r.name }}</td><td>{{ r.reason }}</td></tr>{% endfor %}
    </table>
  {% endif %}
{% endmacro %}
{{ tbl('Тёзки (пропущены)', s.ambiguous) }}
{{ tbl('Конфликт ФИО (создан новый)', s.conflicts) }}
{{ tbl('Не создались', s.failed) }}
{% if s.dropped %}
  <h2>Выбывшие (нет в новой выгрузке) — {{ s.dropped|length }}</h2>
  <table><tr><th>Код</th><th>ФИО</th><th>ID контакта</th></tr>
  {% for r in s.dropped %}<tr><td>{{ r.code }}</td><td>{{ r.name }}</td><td>{{ r.id }}</td></tr>{% endfor %}</table>
{% endif %}
{% if s.withdrawn %}
  <h2>Отозванные заявления — {{ s.withdrawn|length }}{% if s.withdrawn_moved %} (перенесено: {{ s.withdrawn_moved }}){% endif %}</h2>
  <table><tr><th>Код</th><th>ID сделки</th><th>Заявление</th><th>Стадия</th></tr>
  {% for r in s.withdrawn %}<tr><td>{{ r.code }}</td><td>{{ r.deal }}</td><td>{{ r.key }}</td><td>{{ r.stage }}</td></tr>{% endfor %}</table>
{% endif %}
{% if s.code_dups or s.deal_dups %}
  <h2>Дубли</h2>
  <table><tr><th>Тип</th><th>Детали</th></tr>
  {% for c in s.code_dups %}<tr><td>Дубль кода</td><td>{{ c }}</td></tr>{% endfor %}
  {% for d in s.deal_dups %}<tr><td>Дубль сделки</td><td>{{ d }}</td></tr>{% endfor %}</table>
{% endif %}
"""

PREVIEW = """
<p><a href="{{ url_for('index') }}">← назад</a></p>
<div class=flash flash-ok>Предпросмотр — записи ещё не было. Файл: <b>{{ fname }}</b> · уровень: <b>{{ title }}</b> · источник: <b>{{ source_label }}</b></div>
""" + SUMMARY + """
<div class=card>
  <form method=post action="{{ url_for('apply') }}">
    <input type=hidden name=file value="{{ fname }}">
    <input type=hidden name=level value="{{ level }}">
    <input type=hidden name=source value="{{ src_val }}">
    <button class=btn-ok type=submit>Применить — записать в Битрикс</button>
    <a class="btn btn-ghost" href="{{ url_for('index') }}">Отмена</a>
  </form>
  {% if report %}<p class=muted>Отчёт-разбор: <a href="{{ url_for('download', name=report) }}">{{ report }}</a></p>{% endif %}
</div>
"""


_SOURCE_LABELS = {"1c": "Выгрузка 1С", "superservice": "Суперсервис (ЕПГУ)"}


@app.post("/preview")
def preview():
    level = request.form.get("level", "bachelor")
    source = request.form.get("source", "1c")
    lv = _check_level(level)
    if "file" not in request.files or not request.files["file"].filename:
        abort(400, "Не выбран файл")
    path = _save_upload(request.files["file"])
    try:
        stats = sync(_CFG, str(path), apply=False, level=level, source=source)
    except Exception as err:  # noqa: BLE001
        return render("Ошибка", ERROR, msg=str(err), url_for=url_for), 500
    report = _write_report(stats)
    return render("Предпросмотр", PREVIEW, s=stats, fname=path.name, level=level,
                  src_val=source, source_label=_SOURCE_LABELS.get(source, source),
                  title=lv["title"], report=report, url_for=url_for)


# ── применить (асинхронно, с блокировкой от параллельных запусков) ─────────────

RESULT = """
<p><a href="{{ url_for('index') }}">← на главную</a></p>
<div class=flash flash-ok><b>Применено.</b> Данные записаны в Битрикс. Уровень: <b>{{ title }}</b></div>
""" + SUMMARY + """
<div class=card>
  {% if report %}<p>Отчёт-разбор: <a href="{{ url_for('download', name=report) }}">{{ report }}</a></p>{% endif %}
  <a class="btn btn-ghost" href="{{ url_for('index') }}">Готово</a>
</div>
"""

RUNNING = """
<div class=card>
  <h2 style=margin-top:0>Идёт запись в Битрикс…</h2>
  <p>Уровень: <b>{{ title }}</b>. Большая выгрузка может занять несколько минут —
     страница обновляется сама.</p>
  <p class=muted>Не закрывай вкладку и <b>не запускай повторно</b>: синхронизация уже идёт,
     повторный запуск заблокирован (чтобы не создавать дубли).</p>
</div>
<meta http-equiv="refresh" content="5; url={{ url_for('status') }}">
"""

BUSY = """
<div class=card>
  <p><b>⏳ Синхронизация уже выполняется.</b> Второй запуск заблокирован —
     иначе создались бы дубли. Дождись завершения.</p>
  <a class="btn btn-ghost" href="{{ url_for('status') }}">Показать статус</a>
</div>
"""

# Состояние текущей задачи синка (одна на процесс). Блокировка гарантирует, что
# одновременно идёт максимум ОДИН sync --apply — защита от гонки (504 → повторные нажатия).
_JOB = {"state": "idle"}          # idle | running | done | error
_JOB_LOCK = threading.Lock()


def _run_sync_job(path, level, title, source="1c"):
    """Фоновая задача: выполнить sync --apply и сохранить результат в _JOB."""
    try:
        stats = sync(_CFG, str(path), apply=True, level=level, source=source)
        report = _write_report(stats)
        with _JOB_LOCK:
            _JOB.update(state="done", stats=stats, report=report, title=title, error=None)
    except Exception as err:  # noqa: BLE001
        log.exception("Фоновая sync-задача упала")
        with _JOB_LOCK:
            _JOB.update(state="error", error=str(err))


@app.post("/apply")
def apply():
    level = request.form.get("level", "bachelor")
    source = request.form.get("source", "1c")
    lv = _check_level(level)
    with _JOB_LOCK:
        if _JOB.get("state") == "running":
            # синк уже идёт — второй НЕ запускаем (защита от дублей при 504-повторах)
            return render("Уже выполняется", BUSY, url_for=url_for), 409
        fname = secure_filename(request.form.get("file", ""))
        path = _CFG.input_dir / fname
        if not fname or not path.exists():
            abort(400, "Файл не найден — загрузите заново")
        _JOB.clear()
        _JOB.update(state="running", level=level, title=lv["title"])
    threading.Thread(target=_run_sync_job, args=(path, level, lv["title"], source), daemon=True).start()
    return render("Запущено", RUNNING, title=lv["title"], url_for=url_for)


@app.get("/status")
def status():
    with _JOB_LOCK:
        st = dict(_JOB)
    state = st.get("state")
    if state == "running":
        return render("Идёт синхронизация", RUNNING, title=st.get("title", ""), url_for=url_for)
    if state == "error":
        return render("Ошибка", ERROR, msg=st.get("error", "неизвестно"), url_for=url_for), 500
    if state == "done":
        return render("Применено", RESULT, s=st["stats"], title=st.get("title", ""),
                      report=st.get("report"), url_for=url_for)
    return redirect(url_for("index"))


# ── выгрузка из Битрикса ──────────────────────────────────────────────────────

@app.get("/export")
def export():
    level = request.args.get("level", "bachelor")
    _check_level(level)
    try:
        out = _CFG.output_dir / f"deals_export_{level}_{timestamp_slug()}.xlsx"
        export_deals(_CFG, out, level=level)
    except Exception as err:  # noqa: BLE001
        return render("Ошибка", ERROR, msg=str(err), url_for=url_for), 500
    return redirect(url_for("download", name=out.name))


# ── симуляция (асинхронно: долгий расчёт не должен вешать запрос/ловить 504) ────

SIM_RUNNING = """
<div class=card>
  <h2 style=margin-top:0>Идёт расчёт симуляции…</h2>
  <p>Большая выгрузка может считаться до минуты — страница обновится сама.</p>
  <p class=muted>Не закрывай вкладку и <b>не запускай повторно</b> — расчёт уже идёт.</p>
</div>
<meta http-equiv="refresh" content="4; url={{ url_for('simulate_status') }}">
"""

SIM_BUSY = """
<div class=card>
  <p><b>⏳ Расчёт уже идёт.</b> Повторный запуск заблокирован — дождись завершения.</p>
  <a class="btn btn-ghost" href="{{ url_for('simulate_status') }}">Показать статус</a>
</div>
"""

SIM_DONE = """
<p><a href="{{ url_for('index') }}">← на главную</a></p>
<div class=flash flash-ok><b>Готово.</b> Зачислено (проходят сейчас): <b>{{ res.enrolled }}</b>
  из {{ res.applicants }} абитуриентов ({{ 'только с согласием' if res.only_consent else 'все' }}),
  конкурсных групп: {{ res.groups }}.</div>
<div class=card>
  <a class="btn btn-primary" href="{{ url_for('download', name=name) }}">Скачать Excel</a>
  <a class="btn btn-ghost" href="{{ url_for('index') }}">На главную</a>
</div>
"""

_SIM_JOB = {"state": "idle"}          # idle | running | done | error
_SIM_LOCK = threading.Lock()


def _run_sim_job(path, only_consent, require_control):
    """Фоновая задача: посчитать симуляцию и сохранить результат в _SIM_JOB."""
    from .simulation import simulate_admission
    try:
        out = _CFG.output_dir / f"simulation_{timestamp_slug()}.xlsx"
        res = simulate_admission(_CFG, str(path), out,
                                 only_consent=only_consent, require_control=require_control)
        with _SIM_LOCK:
            _SIM_JOB.update(state="done", result=res, name=out.name, error=None)
    except Exception as err:  # noqa: BLE001
        log.exception("Симуляция упала")
        with _SIM_LOCK:
            _SIM_JOB.update(state="error", error=str(err))


@app.post("/simulate")
def simulate():
    if "file" not in request.files or not request.files["file"].filename:
        abort(400, "Не выбран файл выгрузки")
    only_consent = bool(request.form.get("only_consent"))
    require_control = bool(request.form.get("require_control"))
    with _SIM_LOCK:
        if _SIM_JOB.get("state") == "running":
            return render("Уже считается", SIM_BUSY, url_for=url_for), 409
        path = _save_upload(request.files["file"])
        _SIM_JOB.clear()
        _SIM_JOB.update(state="running")
    threading.Thread(target=_run_sim_job, args=(path, only_consent, require_control),
                     daemon=True).start()
    return render("Расчёт запущен", SIM_RUNNING, url_for=url_for)


@app.get("/simulate-status")
def simulate_status():
    with _SIM_LOCK:
        st = dict(_SIM_JOB)
    state = st.get("state")
    if state == "running":
        return render("Идёт расчёт", SIM_RUNNING, url_for=url_for)
    if state == "error":
        return render("Ошибка", ERROR, msg=st.get("error", "неизвестно"), url_for=url_for), 500
    if state == "done":
        return render("Готово", SIM_DONE, res=st["result"], name=st["name"], url_for=url_for)
    return redirect(url_for("index"))


@app.get("/download/<path:name>")
def download(name):
    safe = secure_filename(name)
    if not (_CFG.output_dir / safe).exists():
        abort(404)
    return send_from_directory(_CFG.output_dir, safe, as_attachment=True)


ERROR = """
<p><a href="{{ url_for('index') }}">← на главную</a></p>
<div class=flash flash-err><b>Ошибка.</b> {{ msg }}</div>
"""


@app.errorhandler(400)
@app.errorhandler(404)
def _err(e):
    return render("Ошибка", ERROR, msg=getattr(e, "description", str(e)), url_for=url_for), e.code


def _write_report(stats):
    try:
        path = _CFG.output_dir / f"sync_razbor_{timestamp_slug()}.xlsx"
        write_problem_report(path, stats)
        return path.name
    except Exception:  # noqa: BLE001
        return None


def main():
    setup_logging(False)
    host, port = "0.0.0.0", 8000
    try:
        from waitress import serve
        log.info("Веб-приложение: http://localhost:%d (waitress)", port)
        serve(app, host=host, port=port)
    except ImportError:
        log.info("Веб-приложение: http://localhost:%d (flask dev)", port)
        app.run(host=host, port=port)


if __name__ == "__main__":
    main()
