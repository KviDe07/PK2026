"""Командная строка системы учёта абитуриентов.

Команды:
  initdb        — создать/проверить схему БД
  inspect       — осмотреть выгрузку 1С или поля Битрикса (этап разведки)
  ingest        — загрузить файл 1С (с сопоставлением и слиянием)
  sync-bitrix   — выгрузить и загрузить данные из Битрикс24
  report        — сформировать Excel-отчёты
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import List, Optional

from .config import Config
from .utils import setup_logging

log = logging.getLogger("admissions")


# ── inspect ────────────────────────────────────────────────────────────────────

def _inspect_file(path: Path, cfg: Config, rows: int) -> None:
    from .ingest_1c import read_table

    sheet = cfg.mapping_1c.get("sheet")
    df = read_table(path, sheet=sheet)
    cols = list(df.columns)

    print(f"\nФайл:    {path}")
    print(f"Строк:   {len(df)}")
    print(f"Колонок: {len(cols)}\n")

    print("── Колонки и пример значения ─────────────────────────────")
    for c in cols:
        sample = df[c].dropna()
        example = str(sample.iloc[0])[:50] if len(sample) else "—"
        print(f"  • {c!r}: {example}")

    # Сверка с текущим маппингом
    fields_map = cfg.mapping_1c.get("fields", {})
    id_field = cfg.mapping_1c.get("id_field")
    status_field = cfg.mapping_1c.get("status_field")
    col_set = set(cols)

    print("\n── Сверка с config/mapping_1c.yaml ───────────────────────")
    print(f"  id_field     {id_field!r}: {'НАЙДЕНО' if id_field in col_set else 'НЕ найдено'}")
    print(f"  status_field {status_field!r}: {'НАЙДЕНО' if status_field in col_set else 'НЕ найдено'}")
    mapped_columns = set()
    for canonical, column in fields_map.items():
        ok = column in col_set
        if ok:
            mapped_columns.add(column)
        mark = "✓" if ok else "✗"
        print(f"  {mark} {canonical:14s} <- {column!r}")

    unmapped = [c for c in cols if c not in mapped_columns and c not in {id_field, status_field}]
    if unmapped:
        print("\n── Колонки файла без маппинга ────────────────────────────")
        for c in unmapped:
            print(f"  ? {c!r}")

    if rows > 0:
        print(f"\n── Первые {rows} строк ────────────────────────────────────")
        with_opt = df.head(rows).to_string(max_colwidth=24)
        print(with_opt)
    print()


def _inspect_bitrix(cfg: Config) -> None:
    from .ingest_bitrix import BitrixClient

    client = BitrixClient.from_config(cfg)
    client.print_inspection()


def cmd_inspect(args: argparse.Namespace, cfg: Config) -> int:
    if args.bitrix:
        _inspect_bitrix(cfg)
        return 0
    if not args.file:
        log.error("Укажите файл для осмотра или флаг --bitrix")
        return 2
    _inspect_file(Path(args.file), cfg, args.rows)
    return 0


# ── initdb ─────────────────────────────────────────────────────────────────────

def cmd_initdb(args: argparse.Namespace, cfg: Config) -> int:
    from .db import Database

    db = Database(cfg.db_path)
    db.close()
    log.info("Схема БД готова: %s", cfg.db_path)
    return 0


# ── ingest (1С) ────────────────────────────────────────────────────────────────

def cmd_ingest(args: argparse.Namespace, cfg: Config) -> int:
    from .db import Database
    from .ingest_1c import ingest_file
    from .merge import process_records

    path = Path(args.file)
    if not path.exists():
        log.error("Файл не найден: %s", path)
        return 2

    records, file_hash = ingest_file(path, cfg, sheet=args.sheet)
    log.info("Прочитано записей из 1С: %d", len(records))

    db = Database(cfg.db_path)
    try:
        if not args.force and db.hash_already_ingested("1c", file_hash):
            log.warning("Этот файл уже загружался (hash совпал). Используйте --force для повтора.")
            return 0
        stats = process_records(
            db, cfg, records, source="1c",
            file_name=path.name, file_hash=file_hash, dry_run=args.dry_run,
        )
        _print_stats(stats, args.dry_run)
    finally:
        db.close()
    return 0


# ── sync-bitrix ────────────────────────────────────────────────────────────────

def cmd_sync_bitrix(args: argparse.Namespace, cfg: Config) -> int:
    from .db import Database
    from .ingest_bitrix import fetch_records
    from .merge import process_records

    records = fetch_records(cfg, from_file=args.from_file)
    log.info("Получено записей из Битрикс24: %d", len(records))

    db = Database(cfg.db_path)
    try:
        stats = process_records(
            db, cfg, records, source="bitrix",
            file_name=None, file_hash=None, dry_run=args.dry_run,
        )
        _print_stats(stats, args.dry_run)
    finally:
        db.close()
    return 0


# ── report ─────────────────────────────────────────────────────────────────────

def cmd_report(args: argparse.Namespace, cfg: Config) -> int:
    from .db import Database
    from .reports import build_reports

    db = Database(cfg.db_path)
    try:
        out_path = build_reports(db, cfg, kinds=args.type, out_file=args.out)
    finally:
        db.close()
    log.info("Отчёт сохранён: %s", out_path)
    return 0


# ── вспомогательное ──────────────────────────────────────────────────────────────

def _print_stats(stats: dict, dry_run: bool) -> None:
    prefix = "[DRY-RUN] " if dry_run else ""
    log.info(
        "%sИтог: всего=%d, новых=%d, обновлено=%d, смен статуса=%d, в review=%d",
        prefix,
        stats.get("total", 0),
        stats.get("new", 0),
        stats.get("updated", 0),
        stats.get("status_changes", 0),
        stats.get("review", 0),
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="admissions", description="Учёт абитуриентов ФАКТ МФТИ")
    p.add_argument("-v", "--verbose", action="store_true", help="подробный лог")
    p.add_argument("--config-dir", help="каталог с конфигами (по умолчанию ./config)")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("initdb", help="создать/проверить схему БД")
    sp.set_defaults(func=cmd_initdb)

    sp = sub.add_parser("inspect", help="осмотреть выгрузку 1С или поля Битрикса")
    sp.add_argument("file", nargs="?", help="путь к файлу 1С (.xlsx/.csv)")
    sp.add_argument("--bitrix", action="store_true", help="осмотреть поля сущности Битрикса")
    sp.add_argument("--rows", type=int, default=5, help="сколько строк показать (0 — не показывать)")
    sp.set_defaults(func=cmd_inspect)

    sp = sub.add_parser("ingest", help="загрузить файл 1С")
    sp.add_argument("--file", required=True, help="путь к файлу 1С (.xlsx/.csv)")
    sp.add_argument("--sheet", help="имя листа Excel (по умолчанию из конфига)")
    sp.add_argument("--dry-run", action="store_true", help="показать изменения, не записывая")
    sp.add_argument("--force", action="store_true", help="игнорировать проверку повторной загрузки")
    sp.set_defaults(func=cmd_ingest)

    sp = sub.add_parser("sync-bitrix", help="выгрузить данные из Битрикс24")
    sp.add_argument("--dry-run", action="store_true", help="показать изменения, не записывая")
    sp.add_argument("--from-file", help="взять записи из локального JSON вместо API (офлайн)")
    sp.set_defaults(func=cmd_sync_bitrix)

    sp = sub.add_parser("report", help="сформировать Excel-отчёты")
    sp.add_argument(
        "--type", nargs="+",
        choices=["master", "changes", "analytics", "review", "all"],
        default=["all"], help="какие листы включить",
    )
    sp.add_argument("--out", help="путь к выходному .xlsx (по умолчанию в data/output)")
    sp.set_defaults(func=cmd_report)

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
