# Summary

<!-- What this change does and why. Link the issue it closes: Closes #123. -->

## Checks

- [ ] `ruff check src tests tools` and `ruff format --check src tests tools`
- [ ] `mypy --strict src`
- [ ] `pytest`, and `pytest -m renode` when the change touches Renode,
      models or boards
- [ ] `python -m sphinx -W --keep-going -E docs docs/_build/html` when the
      change touches the docs or docstrings
- [ ] A line under `[Unreleased]` in `CHANGELOG.md`
- [ ] Every commit signed
