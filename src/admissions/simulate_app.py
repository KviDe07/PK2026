"""Локальный веб-инструмент «Симулятор зачисления» — для запуска на Маке.

Простой сайт только с симуляцией: без Битрикса, без авторизации и без лимитов
внешнего прокси. Поднимает http://localhost:8765 и сам открывает браузер.
Запуск: python -m admissions.simulate_app  (или ярлык «Симулятор зачисления.command»).
"""

from __future__ import annotations

import logging
import threading
import webbrowser

from flask import Flask, render_template_string, request, send_file
from werkzeug.utils import secure_filename

from .config import Config
from .simulation import simulate_admission
from .utils import setup_logging, timestamp_slug

log = logging.getLogger("admissions")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024  # 100 МБ — локально прокси-лимитов нет
_CFG = Config.load()

CSS = """
:root{--fg:#1a2230;--mut:#6b7280;--line:#e5e7eb;--acc:#1B1BF5;--ok:#059669;--warn:#b45309;--bg:#f7f8fa}
*{box-sizing:border-box}body{font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;color:var(--fg);background:var(--bg);margin:0}
.wrap{max-width:720px;margin:0 auto;padding:36px 20px 60px}
h1{font-size:22px;margin:0 0 6px}.sub{color:var(--mut);margin:0 0 24px}
.card{background:#fff;border:1px solid var(--line);border-radius:12px;padding:22px;margin:14px 0}
label{display:block;font-weight:600;margin:0 0 6px}
input[type=file]{width:100%;padding:10px;border:1px solid var(--line);border-radius:8px;background:#fff}
.chk{font-weight:400;display:block;margin:12px 0 0}
button{margin-top:18px;border:0;border-radius:8px;padding:12px 22px;font:inherit;font-weight:600;cursor:pointer;background:var(--acc);color:#fff}
.flash{padding:12px 14px;border-radius:8px;margin:12px 0}
.flash-err{background:#fee2e2;color:#991b1b}
.muted{color:var(--mut);font-size:13px}
"""

PAGE = """
<!doctype html><html lang=ru><head><meta charset=utf-8>
<meta name=viewport content='width=device-width,initial-scale=1'>
<title>Симулятор зачисления — ФАКТ МФТИ</title><style>""" + CSS + """</style></head>
<body><div class=wrap>
<h1>Симулятор зачисления — ФАКТ МФТИ</h1>
<p class=sub>Прогноз проходных по бакалавриату. Локальный инструмент — файлы любого размера, ничего не уходит на сервер.</p>
{% if error %}<div class="flash flash-err">{{ error }}</div>{% endif %}
<div class=card>
  <form method=post action="/run" enctype=multipart/form-data>
    <label>Файл выгрузки (.xls / .xlsx)</label>
    <input type=file name=file accept=".xls,.xlsx" required>
    <p class=muted style="margin:8px 0 0">Нужны колонки: «Уникальный код», баллы по предметам/ИД, приоритет, основание, особенности, «Контроль пройден».</p>
    <label class=chk><input type=checkbox name=only_consent value=1 checked> только с согласием на зачисление</label>
    <label class=chk><input type=checkbox name=require_control value=1 checked> только прошедшие «Контроль пройден»</label>
    <button type=submit>Рассчитать и скачать Excel</button>
  </form>
</div>
<p class=muted>После нажатия расчёт займёт несколько секунд, затем браузер скачает готовый файл
  (сводка + листы по группам с подсветкой проходящих).</p>
</div></body></html>
"""


@app.get("/")
def index():
    return render_template_string(PAGE, error=None)


@app.post("/run")
def run():
    if "file" not in request.files or not request.files["file"].filename:
        return render_template_string(PAGE, error="Не выбран файл выгрузки")
    only_consent = bool(request.form.get("only_consent"))
    require_control = bool(request.form.get("require_control"))

    f = request.files["file"]
    name = secure_filename(f.filename or "vygruzka.xlsx")
    if not name.lower().endswith((".xls", ".xlsx")):
        return render_template_string(PAGE, error="Ожидается файл .xls или .xlsx")

    _CFG.input_dir.mkdir(parents=True, exist_ok=True)
    _CFG.output_dir.mkdir(parents=True, exist_ok=True)
    src = _CFG.input_dir / f"sim_upload_{timestamp_slug()}_{name}"
    f.save(src)
    out = _CFG.output_dir / f"simulation_{timestamp_slug()}.xlsx"
    try:
        simulate_admission(_CFG, str(src), out,
                           only_consent=only_consent, require_control=require_control)
    except Exception as err:  # noqa: BLE001
        log.exception("Симуляция упала")
        return render_template_string(PAGE, error=f"Ошибка: {err}")
    return send_file(out, as_attachment=True, download_name=out.name)


def main():
    setup_logging(False)
    port = 8765
    url = f"http://localhost:{port}/"
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    log.info("Симулятор зачисления: %s  (закрой окно или Ctrl+C, чтобы остановить)", url)
    try:
        from waitress import serve
        serve(app, host="127.0.0.1", port=port)
    except ImportError:
        app.run(host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()
