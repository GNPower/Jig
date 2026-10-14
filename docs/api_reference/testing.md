# jig.testing

Test doubles for code built on Jig, for tests that run without Renode or
firmware: a clock the scheduler advances, a CSP board behind an in-process
KISS link, and a runner for plans that declare no system. Board
repositories use them to test plugins and `.jig` procedure libraries in
their own CI.

```python
from pathlib import Path

from jig.testing import run_source


def test_procedure(tmp_path: Path) -> None:
    report = run_source(
        tmp_path,
        'test "sum" {\n    check 1 + 1 == 2\n}\n',
    )
    assert [case.status for case in report.cases] == ["PASS"]
```

```{eval-rst}
.. automodule:: jig.testing
```
