// -----------------------------------------------------------------------------
// 'example' Register Component
// Test fixture for jig.ext.airhdl, written by hand in the layout airhdl
// generates: only the port comments matter to the parser.
// -----------------------------------------------------------------------------

`default_nettype none

module example_regs #(
    parameter AXI_ADDR_WIDTH = 32,
    parameter logic [31:0] BASEADDR = 32'h43C00000
) (
    input wire axi_aclk,
    input wire axi_aresetn,
    // Register 'CONTROL'
    output wire control_strobe, // strobe signal for register 'CONTROL' (pulsed when the register is written from the bus)
    output wire [0:0] control_enable, // value of field 'CONTROL.enable'
    output wire [2:0] control_mode, // value of field 'CONTROL.mode'
    // Register 'STATUS'
    output wire status_strobe, // strobe signal for register 'STATUS' (pulsed when the register is read from the bus)
    input wire [31:0] status_value, // value of field 'STATUS.value'
    // Register 'EVENTS'
    output wire events_strobe, // strobe signal for register 'EVENTS' (pulsed when the register is written from the bus)
    input wire events_done_set, // set signal for field 'EVENTS.done' (pulse to set the field to 1'b1)
    output wire [0:0] events_overflow, // value of field 'EVENTS.overflow'
    // Register 'EVENTS_MASK'
    output wire events_mask_strobe, // strobe signal for register 'EVENTS_MASK' (pulsed when the register is written from the bus)
    output wire [0:0] events_mask_done, // value of field 'EVENTS_MASK.done'
    // Memory 'BUFFER'
    output wire [3:0] buffer_addr, // read/write address for memory 'BUFFER'
    output wire [31:0] buffer_wdata, // write data for memory 'BUFFER'
    output wire buffer_wen, // write enable for memory 'BUFFER'
    // Memory 'TABLE'
    output wire [2:0] table_addr, // read/write address for memory 'TABLE'
    input wire [31:0] table_rdata // read data for memory 'TABLE'
);

endmodule : example_regs

`resetall
