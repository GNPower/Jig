// Byte-lane adapter between Renode's AXI4-Lite master and a 32-bit AXI4-Lite
// design (a Jig co-simulation template).
//
// Renode's master (the IntegrationLibrary's AxiLite bus) drives 64-bit
// addresses and data and does not place sub-word data on its byte lanes: a
// byte write carries the value in bits [7:0] with wstrb = 1, and a byte read
// takes bits [7:0] of the reply. A 32-bit AXI4-Lite interconnect, such as a
// Zynq-7000 general-purpose port, puts a sub-word access on the lanes its
// address selects. This module moves data and strobes to and from those lanes
// and narrows the port to 32 bits; the low address bits pass unchanged.
//
// The library clears the address once the address handshake completes, so
// the lane offset is kept for the data phase.

`timescale 1ns / 1ps
`default_nettype none

module jig_axil_byte_lanes (
    input  wire logic        clk,
    input  wire logic        rst_n,

    // Renode side (the IntegrationLibrary's AxiLite port widths)
    input  wire logic [63:0] s_awaddr,
    input  wire logic [2:0]  s_awprot,
    input  wire logic        s_awvalid,
    output logic             s_awready,
    input  wire logic [63:0] s_wdata,
    input  wire logic [7:0]  s_wstrb,
    input  wire logic        s_wvalid,
    output logic             s_wready,
    output logic [1:0]       s_bresp,
    output logic             s_bvalid,
    input  wire logic        s_bready,
    input  wire logic [63:0] s_araddr,
    input  wire logic [2:0]  s_arprot,
    input  wire logic        s_arvalid,
    output logic             s_arready,
    output logic [63:0]      s_rdata,
    output logic [1:0]       s_rresp,
    output logic             s_rvalid,
    input  wire logic        s_rready,

    // design side: 32-bit AXI4-Lite
    output logic [31:0]      m_awaddr,
    output logic [2:0]       m_awprot,
    output logic             m_awvalid,
    input  wire logic        m_awready,
    output logic [31:0]      m_wdata,
    output logic [3:0]       m_wstrb,
    output logic             m_wvalid,
    input  wire logic        m_wready,
    input  wire logic [1:0]  m_bresp,
    input  wire logic        m_bvalid,
    output logic             m_bready,
    output logic [31:0]      m_araddr,
    output logic [2:0]       m_arprot,
    output logic             m_arvalid,
    input  wire logic        m_arready,
    input  wire logic [31:0] m_rdata,
    input  wire logic [1:0]  m_rresp,
    input  wire logic        m_rvalid,
    output logic             m_rready
);
    logic [1:0] w_lane;
    logic [1:0] r_lane;

    always_ff @(posedge clk) begin
        if (!rst_n) begin
            w_lane <= 2'd0;
            r_lane <= 2'd0;
        end else begin
            if (s_awvalid && s_awready) w_lane <= s_awaddr[1:0];
            if (s_arvalid && s_arready) r_lane <= s_araddr[1:0];
        end
    end

    assign m_awaddr  = s_awaddr[31:0];
    assign m_awprot  = s_awprot;
    assign m_awvalid = s_awvalid;
    assign s_awready = m_awready;
    assign m_wdata   = s_wdata[31:0] << (8 * w_lane);
    assign m_wstrb   = s_wstrb[3:0] << w_lane;
    assign m_wvalid  = s_wvalid;
    assign s_wready  = m_wready;
    assign s_bresp   = m_bresp;
    assign s_bvalid  = m_bvalid;
    assign m_bready  = s_bready;
    assign m_araddr  = s_araddr[31:0];
    assign m_arprot  = s_arprot;
    assign m_arvalid = s_arvalid;
    assign s_arready = m_arready;
    assign s_rdata   = {32'h0, m_rdata >> (8 * r_lane)};
    assign s_rresp   = m_rresp;
    assign s_rvalid  = m_rvalid;
    assign m_rready  = s_rready;

    wire unused = ^{s_awaddr[63:32], s_araddr[63:32], s_wdata[63:32], s_wstrb[7:4]};
endmodule : jig_axil_byte_lanes

`default_nettype wire
