// A register window that holds what is written (a Jig co-simulation
// template). It stands in for the register block of an IP that has no
// simulation model when the design only needs its registers to exist:
// WORDS 32-bit registers from BASE, zero after reset, written with byte
// strobes and read back as written. Every access answers OKAY.

`timescale 1ns / 1ps
`default_nettype none

module jig_axil_regs #(
    parameter logic [31:0] BASE  = 32'h0,
    parameter int          WORDS = 1024
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
    input  wire logic        rready
);
    localparam int AW = $clog2(WORDS);

    logic [31:0]   regs [WORDS];
    logic [AW-1:0] w_index;
    logic [31:0]   w_offset, r_offset;

    typedef enum logic [1:0] {W_ADDR, W_DATA, W_RESP} write_state_e;
    write_state_e w_state;

    assign awready  = (w_state == W_ADDR);
    assign wready   = (w_state == W_DATA);
    assign bvalid   = (w_state == W_RESP);
    assign bresp    = 2'b00;
    assign w_offset = awaddr - BASE;

    always_ff @(posedge clk) begin
        if (!rst_n) begin
            w_state <= W_ADDR;
            w_index <= '0;
            regs    <= '{default: 32'h0};
        end else begin
            unique case (w_state)
                W_ADDR: if (awvalid) begin
                    w_index <= w_offset[AW+1:2];
                    w_state <= W_DATA;
                end
                W_DATA: if (wvalid) begin
                    for (int b = 0; b < 4; b++) begin
                        if (wstrb[b]) regs[w_index][b*8 +: 8] <= wdata[b*8 +: 8];
                    end
                    w_state <= W_RESP;
                end
                W_RESP: if (bready) w_state <= W_ADDR;
                default: w_state <= W_ADDR;
            endcase
        end
    end

    typedef enum logic {R_ADDR, R_DATA} read_state_e;
    read_state_e r_state;

    assign arready  = (r_state == R_ADDR);
    assign rvalid   = (r_state == R_DATA);
    assign rresp    = 2'b00;
    assign r_offset = araddr - BASE;

    always_ff @(posedge clk) begin
        if (!rst_n) begin
            r_state <= R_ADDR;
            rdata   <= 32'h0;
        end else begin
            unique case (r_state)
                R_ADDR: if (arvalid) begin
                    rdata   <= regs[r_offset[AW+1:2]];
                    r_state <= R_DATA;
                end
                R_DATA: if (rready) r_state <= R_ADDR;
                default: r_state <= R_ADDR;
            endcase
        end
    end

    wire unused = ^{w_offset[31:AW+2], w_offset[1:0], r_offset[31:AW+2], r_offset[1:0]};
endmodule : jig_axil_regs

`default_nettype wire
