# Installation

Jig is a Python package. It needs Python 3.11 or newer and Renode 1.17,
which {doc}`renode` installs or finds.

## Install from PyPI

Create a virtual environment and install the `jig-dsl` distribution into
it. On Linux and macOS:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install jig-dsl
```

On Windows, in PowerShell:

```powershell
py -3 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install jig-dsl
```

The package installs the `jig` command and the `jig` import package.
Check the install:

```console
$ jig --version
jig-dsl 0.1.0
```

**Use the full name.** The PyPI project called `jig` is an unrelated tool
from 2015 for Python 2. It also installs a top-level `jig` package and a
`jig` command, so it would replace this one if both were installed into one
environment. `jig --version` prints the distribution name, `jig-dsl`, so
you can tell which one you have.

## What else a task needs

Running plans needs only Jig and Renode. Some tasks need more:

```{list-table}
:header-rows: 1
:widths: 35 65

* - Task
  - Needs
* - Building the ZedBoard example's firmware, or an STM32 board whose
    firmware Jig builds from source
  - `arm-none-eabi-gcc` (the Arm GNU Toolchain) on `PATH`, or its path in
    `ARM_NONE_EABI_GCC`
* - `jig models check --sdk` and `jig models ide`, which build C# models
    with the .NET SDK for strict checks and editor support
  - The .NET 8 SDK
* - Co-simulating RTL with Verilator ({doc}`../how_to/cosimulate_rtl`)
  - Linux, Verilator 5.020 (Ubuntu 24.04's package) or newer, `make` and
    `g++`
```

## Install from a checkout

To work on Jig itself, or to run the ZedBoard example from the repository,
clone it and install it in editable mode with the development tools:

```bash
git clone https://github.com/GNPower/Jig.git
cd Jig
python -m pip install -e ".[dev]"
```

{doc}`../project/contributing` lists the checks a change has to pass.
