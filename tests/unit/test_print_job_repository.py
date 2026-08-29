"""Print-job queue repositories are removed with the office Print feature."""

import importlib
import pytest


@pytest.mark.parametrize(
    "module",
    [
        "app.repositories.print_job_repository",
        "app.repositories.postgres_print_job_repository",
        "app.services.print_jobs",
        "app.api.print_jobs",
    ],
)
def test_print_job_modules_are_gone(module):
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module)
