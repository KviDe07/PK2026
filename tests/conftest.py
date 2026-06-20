"""Общие фикстуры для тестов."""

import pytest

from admissions.config import Config
from admissions.db import Database
from admissions.models import ApplicantRecord


@pytest.fixture
def cfg():
    """Реальная конфигурация проекта (config/*.yaml)."""
    return Config.load()


@pytest.fixture
def db():
    """Чистая БД в памяти со схемой."""
    database = Database(":memory:")
    yield database
    database.close()


@pytest.fixture
def make_record():
    """Фабрика ApplicantRecord для тестов."""
    def _make(source, key, status="documents_submitted", **fields):
        return ApplicantRecord(
            source=source,
            source_key=key,
            fields=fields,
            status_raw=status,
            status=status,
            raw=dict(fields),
        )
    return _make
