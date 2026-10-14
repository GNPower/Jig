// A dual-port block RAM with byte write enables (a Jig co-simulation
// template): WORDS 32-bit words, zero at power-up, both ports on one clock,
// one cycle of read latency on each. A write and a read of one word in the
// same cycle on different ports return the old word; two writes to one word
// in one cycle leave port B's.

`timescale 1ns / 1ps
`default_nettype none

module jig_dp_bram #(
    parameter int WORDS = 1024
) (
    input  wire logic                     clk,

    input  wire logic                     a_en,
    input  wire logic [3:0]               a_we,
    input  wire logic [$clog2(WORDS)-1:0] a_addr,
    input  wire logic [31:0]              a_wdata,
    output logic [31:0]                   a_rdata,

    input  wire logic                     b_en,
    input  wire logic [3:0]               b_we,
    input  wire logic [$clog2(WORDS)-1:0] b_addr,
    input  wire logic [31:0]              b_wdata,
    output logic [31:0]                   b_rdata
);
    logic [31:0] mem [WORDS];

    initial begin
        for (int i = 0; i < WORDS; i++) mem[i] = 32'h0;
    end

    always_ff @(posedge clk) begin
        if (a_en) begin
            a_rdata <= mem[a_addr];
            for (int b = 0; b < 4; b++) begin
                if (a_we[b]) mem[a_addr][b*8 +: 8] <= a_wdata[b*8 +: 8];
            end
        end
        if (b_en) begin
            b_rdata <= mem[b_addr];
            for (int b = 0; b < 4; b++) begin
                if (b_we[b]) mem[b_addr][b*8 +: 8] <= b_wdata[b*8 +: 8];
            end
        end
    end
endmodule : jig_dp_bram

`default_nettype wire
