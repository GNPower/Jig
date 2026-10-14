// An AXI4-Lite register block for Jig's co-simulation tests.
//
//   offset  name       access          contents
//   0x00    ID         read            0x4A494701
//   0x04    SCRATCH    read/write      any value, written with byte strobes
//   0x08    COUNTER    read            clock cycles since reset
//   0x0C    CONTROL    read/write      bit 0: interrupt enable
//   0x10    STATUS     write 1: clear  bit 0: interrupt pending
//   0x14    TRIGGER    write           bit 0 set: STATUS bit 0 set
//   0x18    CLOCK_MHZ  read            the CLK_MHZ parameter
//
// irq is CONTROL bit 0 and STATUS bit 0. Other offsets read 0 and ignore
// writes; every access answers OKAY.

`timescale 1ns / 1ps
`default_nettype none

module axil_regs #(
    parameter logic [31:0] BASE    = 32'h0,
    parameter int          CLK_MHZ = 10
) (
    input  wire logic        clk,
    input  wire logic        rst_n,
    input  wire logic [31:0] awaddr,
    input  wire logic        awvalid,
    output logic             awready,
    input  wire logic [31:0] wdata,
    input  wire logic [3:0]  wstrb,
    input  wire logic        wvalid,
    output logic             wready,
    output logic [1:0]       bresp,
    output logic             bvalid,
    input  wire logic        bready,
    input  wire logic [31:0] araddr,
    input  wire logic        arvalid,
    output logic             arready,
    output logic [31:0]      rdata,
    output logic [1:0]       rresp,
    output logic             rvalid,
    input  wire logic        rready,
    output logic             irq
);
    localparam logic [31:0] ID = 32'h4A49_4701;

    localparam logic [7:0] ID_OFFSET        = 8'h00;
    localparam logic [7:0] SCRATCH_OFFSET   = 8'h04;
    localparam logic [7:0] COUNTER_OFFSET   = 8'h08;
    localparam logic [7:0] CONTROL_OFFSET   = 8'h0C;
    localparam logic [7:0] STATUS_OFFSET    = 8'h10;
    localparam logic [7:0] TRIGGER_OFFSET   = 8'h14;
    localparam logic [7:0] CLOCK_MHZ_OFFSET = 8'h18;

    logic [31:0] scratch;
    logic [31:0] counter;
    logic        enable;
    logic        pending;

    assign irq = enable && pending;

    always_ff @(posedge clk) begin
        if (!rst_n) begin
            counter <= 32'h0;
        end else begin
            counter <= counter + 32'h1;
        end
    end

    // ---------------------------------------------------------------- write

    typedef enum logic [1:0] {W_ADDR, W_DATA, W_RESP} write_state_e;
    write_state_e w_state;
    logic [7:0]   w_offset;
    wire  [31:0]  w_relative = awaddr - BASE;

    assign awready = (w_state == W_ADDR);
    assign wready  = (w_state == W_DATA);
    assign bvalid  = (w_state == W_RESP);
    assign bresp   = 2'b00;

    always_ff @(posedge clk) begin
        if (!rst_n) begin
            w_state  <= W_ADDR;
            w_offset <= 8'h0;
            scratch  <= 32'h0;
            enable   <= 1'b0;
            pending  <= 1'b0;
        end else begin
            unique case (w_state)
                W_ADDR: if (awvalid) begin
                    w_offset <= {w_relative[7:2], 2'b00};
                    w_state  <= W_DATA;
                end
                W_DATA: if (wvalid) begin
                    unique case (w_offset)
                        SCRATCH_OFFSET: begin
                            for (int b = 0; b < 4; b++) begin
                                if (wstrb[b]) scratch[b*8 +: 8] <= wdata[b*8 +: 8];
                            end
                        end
                        CONTROL_OFFSET: if (wstrb[0]) enable <= wdata[0];
                        STATUS_OFFSET:  if (wstrb[0] && wdata[0]) pending <= 1'b0;
                        TRIGGER_OFFSET: if (wstrb[0] && wdata[0]) pending <= 1'b1;
                        default: ;
                    endcase
                    w_state <= W_RESP;
                end
                W_RESP: if (bready) w_state <= W_ADDR;
                default: w_state <= W_ADDR;
            endcase
        end
    end

    // ----------------------------------------------------------------- read

    typedef enum logic {R_ADDR, R_DATA} read_state_e;
    read_state_e r_state;
    wire [31:0]  r_relative = araddr - BASE;

    assign arready = (r_state == R_ADDR);
    assign rvalid  = (r_state == R_DATA);
    assign rresp   = 2'b00;

    always_ff @(posedge clk) begin
        if (!rst_n) begin
            r_state <= R_ADDR;
            rdata   <= 32'h0;
        end else begin
            unique case (r_state)
                R_ADDR: if (arvalid) begin
                    unique case ({r_relative[7:2], 2'b00})
                        ID_OFFSET:        rdata <= ID;
                        SCRATCH_OFFSET:   rdata <= scratch;
                        COUNTER_OFFSET:   rdata <= counter;
                        CONTROL_OFFSET:   rdata <= {31'h0, enable};
                        STATUS_OFFSET:    rdata <= {31'h0, pending};
                        CLOCK_MHZ_OFFSET: rdata <= 32'(CLK_MHZ);
                        default:          rdata <= 32'h0;
                    endcase
                    r_state <= R_DATA;
                end
                R_DATA: if (rready) r_state <= R_ADDR;
                default: r_state <= R_ADDR;
            endcase
        end
    end

    wire unused = ^{w_relative[31:8], w_relative[1:0], r_relative[31:8], r_relative[1:0]};
endmodule : axil_regs

`default_nettype wire
