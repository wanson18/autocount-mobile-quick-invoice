"""Postgres print-job repository is gone; see test_print_job_repository.py."""

import importlib

import pytest


def test_postgres_print_job_repository_module_is_gone():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("app.repositories.postgres_print_job_repository")
