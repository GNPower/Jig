# How-To Guides

Each guide solves one task and assumes you have run a plan before. If you
haven't, start with {doc}`../getting_started/first_run` and
{doc}`../tutorials/index`.

```{list-table}
:header-rows: 1
:widths: 35 65

* - Guide
  - Problem solved
* - {doc}`add_a_board`
  - How do I describe a new board so plans can use it?
* - {doc}`write_a_model`
  - Renode has no model for a chip on my board. How do I write one that
    plans can drive?
* - {doc}`write_a_plugin`
  - How do I give plans functions, members or decoders for my board?
* - {doc}`github_actions`
  - How do I run plans on every push and keep the reports?
* - {doc}`cosimulate_rtl`
  - How do I run my FPGA logic's RTL in place of a peripheral model?
```

```{toctree}
:hidden:

add_a_board
write_a_model
write_a_plugin
github_actions
cosimulate_rtl
```
