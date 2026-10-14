// AXI4-Lite address decoder, one master to N targets (a Jig co-simulation
// template). Stands in for an AXI interconnect that has no simulation model.
//
// Renode's co-simulated peripheral is one master issuing one transaction at a
// time (address, then data, then the response) with absolute addresses.
// Target i answers [BASE[i], BASE[i] + SIZE[i]); an address outside every
// window gets SLVERR, as an interconnect's decode error does.

`timescale 1ns / 1ps
`default_nettype none

module jig_axil_decoder #(
    parameter int          N = 1,
    parameter logic [31:0] BASE [N] = '{default: 32'h0},
    parameter logic [31:0] SIZE [N] = '{default: 32'h1000}
) (
    input  wire logic               clk,
    input  wire logic               rst_n,

    // master
    input  wire logic [31:0]        m_awaddr,
    input  wire logic [2:0]         m_awprot,
    input  wire logic               m_awvalid,
    output logic                    m_awready,
    input  wire logic [31:0]        m_wdata,
    input  wire logic [3:0]         m_wstrb,
    input  wire logic               m_wvalid,
    output logic                    m_wready,
    output logic [1:0]              m_bresp,
    output logic                    m_bvalid,
    input  wire logic               m_bready,
    input  wire logic [31:0]        m_araddr,
    input  wire logic [2:0]         m_arprot,
    input  wire logic               m_arvalid,
    output logic                    m_arready,
    output logic [31:0]             m_rdata,
    output logic [1:0]              m_rresp,
    output logic                    m_rvalid,
    input  wire logic               m_rready,

    // targets
    output logic [N-1:0][31:0]      s_awaddr,
    output logic [N-1:0][2:0]       s_awprot,
    output logic [N-1:0]            s_awvalid,
    input  wire logic [N-1:0]       s_awready,
    output logic [N-1:0][31:0]      s_wdata,
    output logic [N-1:0][3:0]       s_wstrb,
    output logic [N-1:0]            s_wvalid,
    input  wire logic [N-1:0]       s_wready,
    input  wire logic [N-1:0][1:0]  s_bresp,
    input  wire logic [N-1:0]       s_bvalid,
    output logic [N-1:0]            s_bready,
    output logic [N-1:0][31:0]      s_araddr,
    output logic [N-1:0][2:0]       s_arprot,
    output logic [N-1:0]            s_arvalid,
    input  wire logic [N-1:0]       s_arready,
    input  wire logic [N-1:0][31:0] s_rdata,
    input  wire logic [N-1:0][1:0]  s_rresp,
    input  wire logic [N-1:0]       s_rvalid,
    output logic [N-1:0]            s_rready
);
    localparam int SEL_W = $clog2(N + 1);
    localparam logic [SEL_W-1:0] NONE = SEL_W'(N);  // decode error
    localparam logic [1:0] SLVERR = 2'b10;

    function automatic logic [SEL_W-1:0] decode(input logic [31:0] address);
        for (int i = 0; i < N; i++) begin
            if (address >= BASE[i] && (address - BASE[i]) < SIZE[i]) begin
                return SEL_W'(i);
            end
        end
        return NONE;
    endfunction

    // ---------------------------------------------------------------- write

    typedef enum logic [1:0] {W_ADDR, W_DATA, W_RESP} write_state_e;
    write_state_e     w_state;
    logic [SEL_W-1:0] w_sel;
    wire  [SEL_W-1:0] w_decoded = decode(m_awaddr);

    always_ff @(posedge clk) begin
        if (!rst_n) begin
            w_state <= W_ADDR;
            w_sel   <= NONE;
        end else begin
            unique case (w_state)
                W_ADDR: if (m_awvalid && m_awready) begin
                    w_sel   <= w_decoded;
                    w_state <= W_DATA;
                end
                W_DATA: if (m_wvalid && m_wready) w_state <= W_RESP;
                W_RESP: if (m_bvalid && m_bready) w_state <= W_ADDR;
                default: w_state <= W_ADDR;
            endcase
        end
    end

    always_comb begin
        s_awvalid = '0;
        s_wvalid  = '0;
        s_bready  = '0;
        m_awready = 1'b0;
        m_wready  = 1'b0;
        m_bvalid  = 1'b0;
        m_bresp   = 2'b00;
        for (int i = 0; i < N; i++) begin
            s_awaddr[i] = m_awaddr;
            s_awprot[i] = m_awprot;
            s_wdata[i]  = m_wdata;
            s_wstrb[i]  = m_wstrb;
        end
        unique case (w_state)
            W_ADDR: begin
                if (w_decoded == NONE) begin
                    m_awready = m_awvalid;
                end else begin
                    s_awvalid[w_decoded] = m_awvalid;
                    m_awready            = s_awready[w_decoded];
                end
            end
            W_DATA: begin
                if (w_sel == NONE) begin
                    m_wready = m_wvalid;
                end else begin
                    s_wvalid[w_sel] = m_wvalid;
                    m_wready        = s_wready[w_sel];
                end
            end
            W_RESP: begin
                if (w_sel == NONE) begin
                    m_bvalid = 1'b1;
                    m_bresp  = SLVERR;
                end else begin
                    m_bvalid        = s_bvalid[w_sel];
                    m_bresp         = s_bresp[w_sel];
                    s_bready[w_sel] = m_bready;
                end
            end
            default: ;
        endcase
    end

    // ----------------------------------------------------------------- read

    typedef enum logic {R_ADDR, R_DATA} read_state_e;
    read_state_e      r_state;
    logic [SEL_W-1:0] r_sel;
    wire  [SEL_W-1:0] r_decoded = decode(m_araddr);

    always_ff @(posedge clk) begin
        if (!rst_n) begin
            r_state <= R_ADDR;
            r_sel   <= NONE;
        end else begin
            unique case (r_state)
                R_ADDR: if (m_arvalid && m_arready) begin
                    r_sel   <= r_decoded;
                    r_state <= R_DATA;
                end
                R_DATA: if (m_rvalid && m_rready) r_state <= R_ADDR;
                default: r_state <= R_ADDR;
            endcase
        end
    end

    always_comb begin
        s_arvalid = '0;
        s_rready  = '0;
        m_arready = 1'b0;
        m_rvalid  = 1'b0;
        m_rresp   = 2'b00;
        m_rdata   = 32'h0;
        for (int i = 0; i < N; i++) begin
            s_araddr[i] = m_araddr;
            s_arprot[i] = m_arprot;
        end
        unique case (r_state)
            R_ADDR: begin
                if (r_decoded == NONE) begin
                    m_arready = m_arvalid;
                end else begin
                    s_arvalid[r_decoded] = m_arvalid;
                    m_arready            = s_arready[r_decoded];
                end
            end
            R_DATA: begin
                if (r_sel == NONE) begin
                    m_rvalid = 1'b1;
                    m_rresp  = SLVERR;
                end else begin
                    m_rvalid        = s_rvalid[r_sel];
                    m_rresp         = s_rresp[r_sel];
                    m_rdata         = s_rdata[r_sel];
                    s_rready[r_sel] = m_rready;
                end
            end
            default: ;
        endcase
    end
endmodule : jig_axil_decoder

`default_nettype wire
