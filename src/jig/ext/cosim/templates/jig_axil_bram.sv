// AXI4-Lite port of a block RAM (a Jig co-simulation template). It stands in
// for an AXI BRAM controller: WORDS 32-bit words from BASE on the RAM port
// below (port A of jig_dp_bram, for example), written with byte strobes and
// read with one cycle of RAM latency. With READ_ONLY set, writes leave the
// RAM alone and answer SLVERR (a RAM only the design writes).
//
// A write and a read can reach the RAM port in one cycle; the write goes
// first and the read waits a cycle.

`timescale 1ns / 1ps
`default_nettype none

module jig_axil_bram #(
    parameter logic [31:0] BASE      = 32'h0,
    parameter int          WORDS     = 1024,
    parameter bit          READ_ONLY = 1'b0
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

    // RAM port
    output logic                     ram_en,
    output logic [3:0]               ram_we,
    output logic [$clog2(WORDS)-1:0] ram_addr,
    output logic [31:0]              ram_wdata,
    input  wire logic [31:0]         ram_rdata
);
    localparam int AW = $clog2(WORDS);
    localparam logic [1:0] SLVERR = 2'b10;

    typedef enum logic [1:0] {W_ADDR, W_DATA, W_RESP} write_state_e;
    write_state_e  w_state;
    logic [AW-1:0] w_index;
    logic [31:0]   w_offset, r_offset;

    wire write_now = (w_state == W_DATA) && wvalid;

    assign awready  = (w_state == W_ADDR);
    assign wready   = (w_state == W_DATA);
    assign bvalid   = (w_state == W_RESP);
    assign bresp    = READ_ONLY ? SLVERR : 2'b00;
    assign w_offset = awaddr - BASE;

    always_ff @(posedge clk) begin
        if (!rst_n) begin
            w_state <= W_ADDR;
            w_index <= '0;
        end else begin
            unique case (w_state)
                W_ADDR: if (awvalid) begin
                    w_index <= w_offset[AW+1:2];
                    w_state <= W_DATA;
                end
                W_DATA: if (wvalid) w_state <= W_RESP;
                W_RESP: if (bready) w_state <= W_ADDR;
                default: w_state <= W_ADDR;
            endcase
        end
    end

    // Reads: the address is taken when the RAM port is free, then one cycle
    // of RAM latency, then the data.
    typedef enum logic [1:0] {R_ADDR, R_WAIT, R_DATA} read_state_e;
    read_state_e r_state;

    wire read_now = (r_state == R_ADDR) && arvalid && !write_now;

    assign r_offset  = araddr - BASE;
    assign arready   = (r_state == R_ADDR) && !write_now;
    assign rvalid    = (r_state == R_DATA);
    assign rresp     = 2'b00;
    assign ram_en    = write_now || read_now;
    assign ram_we    = (write_now && !READ_ONLY) ? wstrb : 4'b0000;
    assign ram_addr  = write_now ? w_index : r_offset[AW+1:2];
    assign ram_wdata = wdata;

    always_ff @(posedge clk) begin
        if (!rst_n) begin
            r_state <= R_ADDR;
            rdata   <= 32'h0;
        end else begin
            unique case (r_state)
                R_ADDR: if (read_now) r_state <= R_WAIT;
                R_WAIT: begin
                    rdata   <= ram_rdata;
                    r_state <= R_DATA;
                end
                R_DATA: if (rready) r_state <= R_ADDR;
                default: r_state <= R_ADDR;
            endcase
        end
    end

    wire unused = ^{w_offset[31:AW+2], w_offset[1:0], r_offset[31:AW+2], r_offset[1:0]};
endmodule : jig_axil_bram

`default_nettype wire
