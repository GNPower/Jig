// -----------------------------------------------------------------------------
// 'example' Register Definitions
// Test fixture for jig.ext.airhdl, written by hand in the layout airhdl
// generates.
// -----------------------------------------------------------------------------

package example_regs_pkg;

    // Revision number of the 'example' register map
    localparam EXAMPLE_REVISION = 3;

    // Default base address of the 'example' register map
    localparam logic [31:0] EXAMPLE_DEFAULT_BASEADDR = 32'h43C00000;

    // Size of the 'example' register map, in bytes
    localparam EXAMPLE_RANGE_BYTES = 160;

    // Register 'CONTROL'
    localparam logic [31:0] CONTROL_OFFSET = 32'h00000000; // address offset of the 'CONTROL' register
    // Field 'CONTROL.enable'
    localparam CONTROL_ENABLE_BIT_OFFSET = 0; // bit offset of the 'enable' field
    localparam CONTROL_ENABLE_BIT_WIDTH = 1; // bit width of the 'enable' field
    localparam logic [0:0] CONTROL_ENABLE_RESET = 1'b0; // reset value of the 'enable' field
    // Field 'CONTROL.mode'
    localparam CONTROL_MODE_BIT_OFFSET = 4; // bit offset of the 'mode' field
    localparam CONTROL_MODE_BIT_WIDTH = 3; // bit width of the 'mode' field
    localparam logic [6:4] CONTROL_MODE_RESET = 3'b101; // reset value of the 'mode' field

    // Register 'STATUS'
    localparam logic [31:0] STATUS_OFFSET = 32'h00000004; // address offset of the 'STATUS' register
    // Field 'STATUS.value'
    localparam STATUS_VALUE_BIT_OFFSET = 0; // bit offset of the 'value' field
    localparam STATUS_VALUE_BIT_WIDTH = 32; // bit width of the 'value' field
    localparam logic [31:0] STATUS_VALUE_RESET = 32'h00000000; // reset value of the 'value' field

    // Register 'EVENTS'
    localparam logic [31:0] EVENTS_OFFSET = 32'h00000008; // address offset of the 'EVENTS' register
    // Field 'EVENTS.done'
    localparam EVENTS_DONE_BIT_OFFSET = 0; // bit offset of the 'done' field
    localparam EVENTS_DONE_BIT_WIDTH = 1; // bit width of the 'done' field
    localparam logic [0:0] EVENTS_DONE_RESET = 1'b0; // reset value of the 'done' field
    // Field 'EVENTS.overflow'
    localparam EVENTS_OVERFLOW_BIT_OFFSET = 1; // bit offset of the 'overflow' field
    localparam EVENTS_OVERFLOW_BIT_WIDTH = 1; // bit width of the 'overflow' field
    localparam logic [1:1] EVENTS_OVERFLOW_RESET = 1'b0; // reset value of the 'overflow' field

    // Register 'EVENTS_MASK'
    localparam logic [31:0] EVENTS_MASK_OFFSET = 32'h0000000C; // address offset of the 'EVENTS_MASK' register
    // Field 'EVENTS_MASK.done'
    localparam EVENTS_MASK_DONE_BIT_OFFSET = 0; // bit offset of the 'done' field
    localparam EVENTS_MASK_DONE_BIT_WIDTH = 1; // bit width of the 'done' field
    localparam logic [0:0] EVENTS_MASK_DONE_RESET = 1'b1; // reset value of the 'done' field

    // Register 'BUFFER'
    localparam logic [31:0] BUFFER_OFFSET = 32'h00000040; // address offset of the 'BUFFER' register
    localparam BUFFER_DEPTH = 16; // depth of the 'BUFFER' memory, in elements
    localparam BUFFER_READ_LATENCY = 1; // read latency of the 'BUFFER' memory, in clock cycles
    // Field 'BUFFER.value'
    localparam BUFFER_VALUE_BIT_OFFSET = 0; // bit offset of the 'value' field
    localparam BUFFER_VALUE_BIT_WIDTH = 32; // bit width of the 'value' field
    localparam logic [31:0] BUFFER_VALUE_RESET = 32'b00000000000000000000000000000000; // reset value of the 'value' field

    // Register 'TABLE'
    localparam logic [31:0] TABLE_OFFSET = 32'h00000080; // address offset of the 'TABLE' register
    localparam TABLE_DEPTH = 8; // depth of the 'TABLE' memory, in elements
    localparam TABLE_READ_LATENCY = 1; // read latency of the 'TABLE' memory, in clock cycles
    // Field 'TABLE.value'
    localparam TABLE_VALUE_BIT_OFFSET = 0; // bit offset of the 'value' field
    localparam TABLE_VALUE_BIT_WIDTH = 32; // bit width of the 'value' field
    localparam logic [31:0] TABLE_VALUE_RESET = 32'h00000000; // reset value of the 'value' field

endpackage: example_regs_pkg
