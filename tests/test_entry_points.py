"""Cover what actually happens when the app starts.

`app/wsgi.py` is the module gunicorn loads in production and nothing exercised
it, so a mistake there would have shipped: the workers silently not starting, or
the WSGI callable disappearing under a tidy-up. It runs its work at import time,
so these tests stub the pieces first and then import it fresh.
"""

import importlib
import sys

import pytest


ENTRY_MODULES = ("app.wsgi", "app.main")


@pytest.fixture
def fresh_import(monkeypatch):
    """Import an entry point with its side effects recorded rather than run."""
    called = []

    def _import(name):
        # Stub on the modules the entry point imports *from*, before it binds
        # them into its own namespace.
        import app.impact_backfill
        import app.jobs
        import app.poster_maintenance
        import app.test_lab

        monkeypatch.setattr(app.jobs, "start_worker", lambda: called.append("gif_worker"))
        monkeypatch.setattr(app.test_lab, "start_test_lab_worker", lambda: called.append("test_lab_worker"))
        monkeypatch.setattr(
            app.poster_maintenance,
            "start_landscape_poster_worker",
            lambda: called.append("poster_worker"),
        )
        monkeypatch.setattr(app.impact_backfill, "ensure_backfilled", lambda: called.append("backfill"))

        for module in ENTRY_MODULES:
            sys.modules.pop(module, None)
        return importlib.import_module(name), called

    yield _import

    for module in ENTRY_MODULES:
        sys.modules.pop(module, None)


def test_wsgi_starts_every_worker_and_recovers_history(fresh_import):
    module, called = fresh_import("app.wsgi")

    # All three workers, or the queue accepts jobs nothing ever runs.
    assert "gif_worker" in called
    assert "test_lab_worker" in called
    assert "poster_worker" in called

    # Before the workers, so the first dashboard request already has the
    # recovered lifetime total rather than an understated one.
    assert called.index("backfill") < called.index("gif_worker")


def test_wsgi_exposes_the_callable_gunicorn_loads(fresh_import):
    """The Dockerfile runs `app.wsgi:app`. If this breaks, production does."""
    module, _called = fresh_import("app.wsgi")

    assert hasattr(module, "app"), "gunicorn would fail to find the application"
    assert "app" in getattr(module, "__all__", ()), "the re-export guard is gone"
    assert callable(module.app.wsgi_app)
    # A real Flask app with the routes attached, not a placeholder.
    assert len(list(module.app.url_map.iter_rules())) > 100


def test_importing_main_does_not_start_anything(fresh_import):
    """`python -m app.main` starts workers; importing the module must not.

    The tests import app modules constantly. If main.py did its work at import
    time the way wsgi.py does, every test run would spawn worker threads.
    """
    _module, called = fresh_import("app.main")

    assert called == []


def test_dockerfile_and_entry_point_agree_on_the_module_path():
    from pathlib import Path

    dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text(encoding="utf-8")
    assert '"app.wsgi:app"' in dockerfile
