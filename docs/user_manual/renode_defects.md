# Renode defects Jig works around

Jig is tested on Renode 1.17.0. Some of the peripheral models in that
release behave unlike the silicon they stand for, and firmware fails on
them: an STM32 CAN node that filters on an address in the upper identifier
bits never receives the frames addressed to it, a low-power timer never
matches a compare value of 0, a real-time clock forgets the date at every
reset. For each defect it
has met, Jig ships a model of its own or a hook on Renode's, and the
platforms Jig ships (`soc_platform`) use them.

Every defect in the first table has a test under `tests/models` that drives
Renode's own model and asserts the wrong behaviour. The test passes while
Renode keeps the defect and fails once a Renode release fixes it, and its
comment names the Jig replacement to review. `pytest -m renode` runs them
with the other model tests.

## Models Jig replaces

```{list-table}
:header-rows: 1
:widths: 18 34 20 28

* - Renode model
  - What goes wrong
  - Jig replacement
  - Test that pins it
* - `Timers.ARM_GlobalTimer`
  - CONTROL reads its prescaler back as the written value plus one, so
    every read-modify-write of CONTROL raises the prescaler by one and the
    timer slows down.
  - `Jig.Arm.CortexA9GlobalTimer`
  - `test_cortex_a9_global_timer.py::test_renodes_stock_timer_reads_the_prescaler_back_one_higher`
* - `IRQControllers.ARM_GenericInterruptController` (GICv1)
  - GICD_IPRIORITYR keeps all 8 priority bits whatever the GIC
    implements (5 on the Zynq-7000).
  - `LimitGicPriorityBits` (`Jig.Arm.GicDistributorExtensions`)
  - `test_gic_hooks.py::test_without_the_hook_all_eight_bits_stick`
* - `IRQControllers.ARM_GenericInterruptController` (GICv1)
  - An interrupt whose GICD_ICFGR field has the handling-model bit set
    (0b01 for level, 0b11 for rising edge, as Xilinx's XScuGic driver
    writes them) never becomes pending.
  - `IgnoreGicHandlingModelBits` (`Jig.Arm.GicDistributorExtensions`)
  - `test_gic_hooks.py::test_without_the_hook_such_an_interrupt_never_pends`
* - `CAN.STM32_FDCAN`
  - Extended acceptance filters see a 16-bit cast of the identifier: a
    filter on bits 16 to 28 never matches, and a filter for address 0
    accepts every frame.
  - `Jig.ST.STM32U5_FDCAN`
  - `test_stm32u5_fdcan.py::test_renodes_stock_fdcan_filters_a_16_bit_cast_of_extended_ids`,
    `::test_renodes_stock_fdcan_accepts_frames_for_address_0`
* - `CAN.STM32_FDCAN`
  - FDCAN_XIDAM masks the identifier for range filters (type 00) only;
    ST RM0456 applies it to types 00, 01 and 10.
  - `Jig.ST.STM32U5_FDCAN`
  - `test_stm32u5_fdcan.py::test_renodes_stock_fdcan_masks_only_range_filters_with_xidam`
* - `Timers.STM32L0_LpTimer`
  - A compare value of 0 never matches.
  - `Jig.ST.STM32U5_LPTIM`
  - `test_stm32u5_lptim.py::test_renodes_stock_lptimer_never_matches_a_compare_value_of_0`
* - `Timers.STM32L0_LpTimer`
  - The counter wraps after ARR counts instead of ARR + 1.
  - `Jig.ST.STM32U5_LPTIM`
  - `test_stm32u5_lptim.py::test_renodes_stock_lptimer_wraps_after_arr_counts`
* - `Timers.STM32L0_LpTimer`
  - No update event: ISR.UE, DIER.UEIE and the repetition counter are
    missing.
  - `Jig.ST.STM32U5_LPTIM`
  - `test_stm32u5_lptim.py::test_renodes_stock_lptimer_has_no_update_event`
* - `Timers.STM32F4_RTC`
  - A machine reset returns the calendar to its reset date. On silicon the
    RTC is in the backup domain and keeps counting through a system reset.
  - `Jig.ST.STM32U5_RTC`, with `STM32U5_TAMP` and `STM32U5_RCC`
  - `test_stm32u5_rtc.py::test_renodes_stock_rtc_restarts_its_calendar_on_a_system_reset`
* - `Analog.STM32WBA_ADC`
  - A machine reset returns every channel input to 0 V, while the voltage
    at a pin comes from outside the chip.
  - `Jig.ST.STM32U5_ADC4`
  - `test_stm32u5_adc4.py::test_renodes_stock_adc_returns_its_inputs_to_0_volts_on_reset`
```

## What the replacements change

The FDCAN fix decides whether CAN works for protocols that put a node
address in the upper identifier bits. The CAN Fragmentation Protocol
of libcsp carries the destination in bits 19 to 23 (the
`STM32U5_FDCAN` tests use its layout: filter 0x00780000 under mask
0x00F80000, frame 0x00781234). With Renode's model, the node that filters
on address 15 receives nothing and the node with address 0 receives every
frame on the bus. `Jig.ST.STM32U5_FDCAN` is Renode's `STM32_FDCAN` from
renode-infrastructure commit 066a7f13 with the full identifier passed to
the filters; its header lists every change against the original.

`Jig.ST.STM32U5_LPTIM` is written from RM0456 rather than forked, because
the compare logic, the period and the update event all differ from
`STM32L0_LpTimer`. It counts from 0 to ARR, raises CC1IF when the counter
moves into CCR1 (for CCR1 = 0, at the wrap), and raises UE at the wrap
where the repetition counter is 0.

The STM32U5 backup domain needed three models, since what survives a reset
depends on which reset it is:

- `STM32U5_RCC` keeps RCC_BDCR and the reset flags of RCC_CSR through a
  system reset. A power-on reset, or RCC_BDCR.BDRST, clears RCC_BDCR and
  drives its `BackupDomainReset` output, which the platform connects to
  the RTC and TAMP.
- `STM32U5_RTC` follows the STM32U5 register layout (Renode's
  `STM32F4_RTC`, placed at the U5 address, reads RTC_SSR from its own
  control register) and keeps its calendar, prescalers, alarms and wakeup
  timer through a system reset.
- `STM32U5_TAMP` keeps the 32 backup registers through a system reset.

Firmware that keeps a marker in a backup register to remember setting the
clock then behaves as on silicon in both directions: it finds the marker
and the time after a watchdog reset, and finds neither after a power
cycle.

`STM32U5_ADC4` is Renode's `STM32WBA_ADC` with the inputs set through
`SetChannelVoltage` put back after every machine reset: one the harness
commands, a watchdog's, or the core's AIRCR.SYSRESETREQ. An input a plan
sets once holds until the plan changes it.

## Other Renode behaviours Jig handles

These are not peripheral defects. Only the first has a test that pins
it.

`LimitTimer.Value`
: Renode's `LimitTimer` refuses a value above the limit it was
  constructed with, even after its limit was raised. Jig's models that set
  a timer's value build the timer with the largest limit they use:
  `CortexA9PrivateTimerWatchdog`, `CadenceWDT`, `STM32U5_LPTIM` and
  `STM32U5_RTC`. `test_stm32u5_lptim.py::test_renodes_limit_timer_checks_a_value_against_its_first_limit`
  pins it.

`mach create`
: Every machine made with `mach create` runs on the emulation's master
  time source, which advances only as far as the slowest core, so one
  board's timers fire late while another board catches up. Jig gives each
  board of a multi-board system its own time source
  (`RenodeSession.create_machine(own_time=True)`).

IntegrationLibrary
: The co-simulation library shipped with Renode 1.17.0 calls a
  `connect()` overload its own header does not declare. `jig cosim build`
  compiles a patched copy (`jig.ext.cosim.patch_integration_library`).

Remote control
: `--disable-gui` alone opens a telnet monitor on port 1234 of every
  interface, and the Robot Framework server listens beyond loopback.
  Jig starts Renode with `--console` and controls it through its own
  bridge ({doc}`bridge`).

## STM32U5 defects without a workaround

The STM32U5 platforms keep these Renode models as they are. The platform
files, and the `STM32U5_ADC4` header for the converter, note each one.

- `MTD.STM32WBA_FlashController` has no FLASH_NSCR.BKER bit and a 7-bit
  page number, so a page erase in bank 2, or in pages 128 to 255 of a
  bank, erases bank 1 page PNB mod 128.
- `Analog.STM32WBA_ADC` reads ADC4_CHSELR as a channel bitmap even with
  CFGR1.CHSELRMOD = 1, so a sequence converts the channels whose bits its
  SQx fields set rather than the channels they name.
- `SPI.STM32WBA_SPI` has no simplex receive (SPI_CFG2.COMM), so a
  receive-only transfer never sees RXP or EOT and the HAL call times out.
- `IRQControllers.NVIC` has one SysTick frequency whatever SysTick
  CTRL.CLKSOURCE selects. The platform sets 32000 Hz, the LSI reference;
  firmware that clocks SysTick from the core sets `systickFrequency` to
  HCLK in its board file.
