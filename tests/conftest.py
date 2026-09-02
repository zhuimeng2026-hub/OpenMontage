"""Repo-wide pytest shims that apply before any test module imports.

Phase B/C/D of the user-isolation work imported ``lib.workbuddy_session``
(which does ``import fcntl`` at module load) into several production
modules. On POSIX this is a no-op; on Windows it raises
``ModuleNotFoundError`` and blocks even unrelated tests from collecting.

A handful of test modules already carry their own fcntl shim
(``tests/integration/test_bearer_user_id.py``,
``tests/integration/test_principal_registry.py``,
``tests/integration/test_project_workspace.py``). They must stay in
lock-step with this conftest so a future change to the import path is
visible everywhere. The shim here is the single source of truth — the
per-module stubs are belt-and-suspenders for the case where a test
collects in isolation (e.g. ``pytest path/to/single_test.py``) without
this conftest's autouse fixtures firing.

Why a conftest and not a sitecustomize.py or setup.py hack
---------------------------------------------------------

``conftest.py`` is pytest's documented mechanism for collection-time
hooks; a Windows dev running ``pytest tests/test_foo.py`` will pick up
the shim automatically without setting ``PYTHONPATH`` or installing
anything. A ``sitecustomize.py`` would run for every Python invocation
in the project — including production — which is undesirable for a
test-only import shim.
"""
from __future__ import annotations

import sys
import types as _types_module

import pytest


# Run at conftest import time (before any test module's imports are
# evaluated) so the stub is in place by the time ``lib.workbuddy_session``
# (or any transitive import that pulls it in) is loaded. ``sys.modules``
# injection is idempotent — re-importing this conftest does not
# overwrite a real ``fcntl`` if some test happens to install one.
if "fcntl" not in sys.modules:
    sys.modules["fcntl"] = _types_module.ModuleType("fcntl")


@pytest.fixture(autouse=True)
def _reset_principal_secret_between_tests():
    """Keep cached namespace derivation from leaking across test modules.

    ``principal_registry`` intentionally caches the HMAC secret for process
    lifetime in production. Tests exercise key rotation, however, and a
    later namespaced-path test must not inherit that temporary key. Clearing
    the private cache at test boundaries preserves the production contract
    while making combined suites deterministic.
    """
    try:
        import lib.principal_registry as principal_registry
    except Exception:
        yield
        return
    principal_registry._secret_value = None
    principal_registry._secret_warned = False
    yield
    principal_registry._secret_value = None
