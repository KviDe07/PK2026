"""Командная строка системы учёта абитуриентов ФАКТ МФТИ.

Команды:
  setup-contact-fields — создать поля контакта (Уникальный код, Тип поступающего)
  setup-deal-fields    — создать поля сделки (данные заявления из 1С)
  setup-funnel         — скопировать стадии между воронками сделок
  inspect              — осмотреть выгрузку 1С или поля сущности Битрикса
  sync                 — обновить контакты и сделки в Битриксе по выгрузке 1С
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import List, Optional

from .config import Config
from .utils import setup_logging, timestamp_slug

log = logging.getLogger("admissions")


# ── inspect ──────────────────────────────────────────────────────────────────

def _inspect_file(path: Path, cfg: Config, rows: int) -> None:
    from .ingest_applications import COLUMNS, parse_applications

    cols = {**COLUMNS, **(cfg.columns_1c or {})}
    parsed = parse_applications(path, cfg.columns_1c or None)
    total_apps = sum(len(a["applications"]) for a in parsed.values())
    groups = sorted({app["group"] for a in parsed.values() for app in a["applications"] if app["group"]})

    print(f"\nФайл:          {path}")
    print(f"Абитуриентов:  {len(parsed)} (заявлений после дедупа: {total_apps})")
    print(f"Конкурсные группы: {', '.join(groups) or '—'}\n")

    print("── Ожидаемые колонки (атрибут → колонка 1С) ──────────────")
    for key, col in cols.items():
        print(f"  {key:12s} <- {col!r}")

    if rows > 0:
        print(f"\n── Первые {rows} абитуриентов ─────────────────────────────")
        for a in list(parsed.values())[:rows]:
            print(f"  • {a['full_name']} | код {a['code']} | тел {a['phone']} | заявлений {len(a['applications'])}")
            for app in a["applications"]:
                print(f"      - {app['group']} | приоритет {app['priority']} | баллы {app['score']} | согласие {app['consent']}")
    print()


def cmd_inspect(args: argparse.Namespace, cfg: Config) -> int:
    if args.bitrix:
        from .bitrix_client import BitrixClient
        BitrixClient.from_config(cfg).print_inspection(args.entity)
        return 0
    if not args.file:
        log.error("Укажите файл для осмотра или флаг --bitrix")
        return 2
    _inspect_file(Path(args.file), cfg, args.rows)
    return 0


# ── setup-*-fields ────────────────────────────────────────────────────────────

def _print_fields_plan(title: str, plan, codes, apply: bool) -> None:
    header = "СОЗДАНИЕ" if apply else "предпросмотр — изменений нет"
    print(f"\n{title} ({header})\n")
    for action, d, name in plan:
        mark = "[есть]    " if action == "exists" else "[создать] "
        print(f"  {mark} {d['label']:32s} | {d['type']:11s} | {name or d['name']}")
    if apply and codes:
        print("\nФактические коды полей (XML_ID -> код):")
        for xml, code in codes.items():
            print(f"  {xml:12s} -> {code}")
    elif not apply:
        print("\nСоздать поля: повтори команду с флагом --apply")


def cmd_setup_contact_fields(args: argparse.Namespace, cfg: Config) -> int:
    from .bitrix_fields import setup_contact_fields
    plan, codes = setup_contact_fields(cfg, apply=args.apply)
    _print_fields_plan("Поля контакта", plan, codes, args.apply)
    return 0


def cmd_setup_deal_fields(args: argparse.Namespace, cfg: Config) -> int:
    from .bitrix_fields import setup_deal_fields
    plan, codes = setup_deal_fields(cfg, apply=args.apply, level=args.level)
    _print_fields_plan(f"Поля сделки ({args.level})", plan, codes, args.apply)
    return 0


# ── setup-funnel ──────────────────────────────────────────────────────────────

def cmd_setup_funnel(args: argparse.Namespace, cfg: Config) -> int:
    from .bitrix_funnel import copy_funnel_stages

    plan, _ = copy_funnel_stages(cfg, args.from_category, args.to_category, apply=args.apply)
    mark = {"add": "[ДОБАВИТЬ] ", "update": "[ПЕРЕИМЕН.]", "keep": "[ = ]      "}
    sem = {"S": "успех", "F": "провал"}
    header = "ПРИМЕНЕНО" if args.apply else "предпросмотр — изменений нет"
    print(f"\nВоронка [{args.from_category}] → [{args.to_category}]  ({header})\n")
    for p in plan:
        print(f"  {mark.get(p.action, p.action)} {p.status_id:22s} | "
              f"{(sem.get(p.semantics) or 'в работе'):8s} | {p.name}")
    adds = sum(1 for p in plan if p.action == "add")
    upds = sum(1 for p in plan if p.action == "update")
    keeps = sum(1 for p in plan if p.action == "keep")
    print(f"\nИтого: добавить={adds}, переименовать={upds}, без изменений={keeps}")
    if not args.apply:
        print("Применить: повтори команду с флагом --apply")
    return 0


# ── sync ──────────────────────────────────────────────────────────────────────

def _latest_file(directory: Path, suffixes: tuple) -> Optional[Path]:
    files = [p for p in directory.glob("*") if p.suffix.lower() in suffixes]
    return max(files, key=lambda p: p.stat().st_mtime) if files else None


def cmd_sync(args: argparse.Namespace, cfg: Config) -> int:
    from .sync import sync
    from .report import write_problem_report

    apps = Path(args.applications) if args.applications else _latest_file(cfg.input_dir, (".xls", ".xlsx"))
    if not apps or not apps.exists():
        log.error("Не найден файл выгрузки 1С (укажите --applications или положите .xls/.xlsx в %s)",
                  cfg.input_dir)
        return 2
    log.info("Выгрузка 1С: %s", apps.name)

    stats = sync(cfg, str(apps), apply=args.apply, level=args.level)
    header = "ПРИМЕНЕНО" if args.apply else "DRY-RUN — без записи"
    log.info(
        "%s | абитуриентов: %d | заявлений: %d | по коду: %d | приняты заготовки: %d | "
        "создать контактов: %d | сделок: создать %d, заполнить пустых %d, обновить %d, перепривязать %d",
        header, stats["applicants"], stats["applications"], stats["matched_by_code"],
        stats["adopted"], stats["created_contacts"], stats["created_deals"],
        stats["filled_deals"], stats["updated_deals"], stats["relinked_deals"],
    )
    log.info(
        "Разбор: тёзки %d | конфликт ФИО %d | не создались %d | выбывшие %d | "
        "отозвано заявлений %d (перенесено %d) | дубли кода %d | дубли сделок %d",
        len(stats["ambiguous"]), len(stats["conflicts"]), len(stats["failed"]),
        len(stats["dropped"]), len(stats["withdrawn"]), stats["withdrawn_moved"],
        len(stats["code_dups"]), len(stats["deal_dups"]),
    )

    # Excel-отчёт «разбор сопоставления»
    report_path = cfg.output_dir / f"sync_razbor_{timestamp_slug()}.xlsx"
    write_problem_report(report_path, stats)
    log.info("Отчёт-разбор: %s", report_path)

    if not args.apply:
        print("\nПрименить изменения: повтори команду с флагом --apply")
    return 0


# ── export (сделки воронки -> Excel) ──────────────────────────────────────────

def cmd_export(args: argparse.Namespace, cfg: Config) -> int:
    from .export import export_deals

    out = Path(args.out) if args.out else cfg.output_dir / f"deals_export_{args.level}_{timestamp_slug()}.xlsx"
    path = export_deals(cfg, out, level=args.level)
    log.info("Выгрузка сделок сохранена: %s", path)
    return 0


# ── парсер ────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="admissions", description="Учёт абитуриентов ФАКТ МФТИ")
    p.add_argument("-v", "--verbose", action="store_true", help="подробный лог")
    p.add_argument("--config-dir", help="каталог с конфигами (по умолчанию ./config)")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("inspect", help="осмотреть выгрузку 1С или поля сущности Битрикса")
    sp.add_argument("file", nargs="?", help="путь к файлу 1С (.xls/.xlsx)")
    sp.add_argument("--bitrix", action="store_true", help="осмотреть поля сущности Битрикса")
    sp.add_argument("--entity", default="deal", help="сущность для --bitrix (deal/contact)")
    sp.add_argument("--rows", type=int, default=5, help="сколько абитуриентов показать (0 — не показывать)")
    sp.set_defaults(func=cmd_inspect)

    sp = sub.add_parser("setup-contact-fields", help="создать поля контакта (код, тип)")
    sp.add_argument("--apply", action="store_true", help="создать поля (без флага — предпросмотр)")
    sp.set_defaults(func=cmd_setup_contact_fields)

    sp = sub.add_parser("setup-deal-fields", help="создать поля сделки (данные заявления)")
    sp.add_argument("--apply", action="store_true", help="создать поля (без флага — предпросмотр)")
    sp.add_argument("--level", default="bachelor", help="уровень: bachelor | master")
    sp.set_defaults(func=cmd_setup_deal_fields)

    sp = sub.add_parser("setup-funnel", help="скопировать стадии между воронками сделок")
    sp.add_argument("--from", dest="from_category", type=int, required=True, help="ID воронки-источника")
    sp.add_argument("--to", dest="to_category", type=int, required=True, help="ID целевой воронки")
    sp.add_argument("--apply", action="store_true", help="применить (без флага — предпросмотр)")
    sp.set_defaults(func=cmd_setup_funnel)

    sp = sub.add_parser("sync", help="обновить контакты и сделки в Битриксе по выгрузке 1С")
    sp.add_argument("--applications", help="файл выгрузки 1С (по умолчанию свежий .xls/.xlsx из data/input)")
    sp.add_argument("--apply", action="store_true", help="записать изменения (без флага — только показать)")
    sp.add_argument("--level", default="bachelor", help="уровень: bachelor | master")
    sp.set_defaults(func=cmd_sync)

    sp = sub.add_parser("export", help="выгрузить сделки воронки в Excel (с комментариями)")
    sp.add_argument("--out", help="путь к выходному .xlsx (по умолчанию в data/output)")
    sp.add_argument("--level", default="bachelor", help="уровень: bachelor | master")
    sp.set_defaults(func=cmd_export)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(getattr(args, "verbose", False))

    config_dir = Path(args.config_dir) if args.config_dir else None
    try:
        cfg = Config.load(config_dir=config_dir)
    except FileNotFoundError as err:
        log.error("%s", err)
        return 2

    try:
        return args.func(args, cfg)
    except Exception as err:  # noqa: BLE001
        log.error("Ошибка: %s", err)
        if args.verbose:
            raise
        return 1


if __name__ == "__main__":
    sys.exit(main())
