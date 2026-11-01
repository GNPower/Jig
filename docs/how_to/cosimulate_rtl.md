# Co-simulate RTL with Verilator

A Zynq-7000 design puts custom logic in the programmable logic, and a plan
can run against either a C# model of that logic or the RTL itself. This
guide gives a board an option that swaps an AXI4-Lite register block's
model for its Verilated RTL.

## What you need

Co-simulation runs on Linux only: Verilator and Renode's integration
library for co-simulated peripherals build and load there. `jig cosim
build` needs Verilator, `make` and `g++` on the machine or container that
runs the plan, and takes the integration library's sources from the Renode
install (`RENODE_INTEGRATION_LIBRARY` names another copy). Plans that keep
the C# model run anywhere.

## How the RTL reaches Renode

Verilator compiles the RTL and Jig's simulation harness into a shared
library. Renode's `CoSimulatedPeripheral` loads it into the Renode process
and forwards the firmware's bus accesses to it, clocked by the emulation.
The harness binds the RTL's AXI4-Lite slave port to those accesses and
splits them into byte lanes, so the firmware's 8-, 16- and 32-bit accesses
reach the RTL as the processing system would issue them.

## 1. Describe the RTL choice

Board options are choices a system makes per board instance. Give the
board an option, and under `if`, an `ip` line for each choice: the C#
model, or `cosim(...)` with the RTL build.

```jig
board "my-zynq-board" {
    option pl: choice({model: "the register block as a C# model",
                       rtl: "the register block as Verilated RTL"})

    device zynq: zynq7000(part: "XC7Z020-CLG484", xsa: "build/design.xsa",
                          ps_clk: 33.333333 MHz, ps_version: 3) {
        firmware: {app: "build/app.elf"}
        if pl == "model" {
            ip block: "Example.ExampleBlock" (on: 0x43C00000 .. 0x43C00FFF)
        } else {
            ip block: cosim(on: 0x43C00000 .. 0x43C00FFF, build: "cosim/build.sh",
                            sources: "cosim/sources.f", inputs: ["hdl"],
                            library: "libexample_block.so",
                            context: "trace={workdir}/block.vcd",
                            frequency: 20 MHz, limitBuffer: 1000)
        }
    }
}
```

With `pl` set to `rtl`, the description Jig writes puts a co-simulated
peripheral where the model would sit, with the clock the RTL is built for:

```text
block: CoSimulated.CoSimulatedPeripheral @ sysbus <0x43C00000, +0x1000>
    frequency: 20000000
    limitBuffer: 1000
```

The peripheral passes the RTL each access's offset from the start of its
range. RTL that decodes full bus addresses, such as a block behind its own
address decoder, needs `useAbsoluteAddress: true` among the arguments as
well. The model's clock advances in batches of `limitBuffer` cycles. An
`irq: N` argument connects the RTL's interrupt output to `IRQ_F2P[N]`.

```{list-table}
:header-rows: 1
:widths: 20 80

* - Argument
  - Meaning
* - `build`
  - A script Jig runs as `build <output-dir> --clock-mhz <N>`, with `N`
    taken from `frequency`, which must be a whole number of MHz. It leaves
    `library` in the output directory.
* - `sources`
  - A Verilator file list. Options (`+define+`, `-I`) and `$(NAME)`
    entries are skipped when Jig computes the cache key.
* - `inputs`
  - Other files the build reads; a directory counts with everything in
    it.
* - `library`
  - The file name the build produces.
* - `context`
  - Text the model receives before it starts; `{workdir}` in it is the
    machine's work directory.
* - Any other
  - A property of the co-simulated peripheral, such as `frequency`.
```

Paths in `cosim(...)` resolve against the board's root.

## 2. Build the library

`jig cosim build` does a build script's work for a design whose top level
has an AXI4-Lite slave port: it copies Renode's integration library
sources, applies the fix Jig needs to them, compiles Jig's `sim_main.cpp`
harness and the RTL with Verilator, and links the library. A board's
`build` script can call it or do the same steps itself.

Jig keeps the library in its artifact cache. The key covers the build
script, every file the file list names, the `inputs`, the clock and the
tool versions, so editing the RTL rebuilds the library on the next run and
nothing else does.

## 3. Choose the RTL in a plan

A plan picks the choice on the board's line:

```jig
system "register block in RTL" {
    board dut: "my-zynq-board" (pl: "rtl")
}
```

`jig run --option pl=rtl` sets it for every board that has a `pl` option,
over what the plan chose, so one plan runs against both the model and the
RTL:

```bash
jig run plans/registers.jig --option pl=rtl
```

The report's system lines show the value each option took. The run
directory holds the `context` outputs, such as the waveform trace above.
