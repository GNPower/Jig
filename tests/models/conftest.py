"""Fixtures for the register-level model tests (``pytest -m renode``).

``renode`` is one Renode session per test module with Jig's models and the
fixture models in ``tests/fixtures/models`` loaded; ``bench`` is a fresh
machine in it for each test. Without Renode (or, before the runtime compile,
without the .NET SDK) the tests skip.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from jig.errors import ToolchainError
from jig.renode.session import RenodeSession

from .bench import Bench, fixture_models, include, open_session, remove_quietly


@pytest.fixture(scope="module")
def renode(
    tmp_path_factory: pytest.TempPathFactory, request: pytest.FixtureRequest
) -> Iterator[RenodeSession]:
    label = request.module.__name__.rpartition(".")[2]
    workdir = tmp_path_factory.mktemp(f"renode-{label}")
    try:
        session = open_session(workdir / "renode")
        session.start()
    except ToolchainError as exc:
        pytest.skip(f"no Renode session: {exc}")
    try:
        include(session, fixture_models())
        yield session
    finally:
        session.stop()


@pytest.fixture
def bench(renode: RenodeSession, tmp_path: Path) -> Iterator[Bench]:
    machine = Bench(renode, tmp_path / "bench")
    try:
        yield machine
    finally:
        remove_quietly(machine)
