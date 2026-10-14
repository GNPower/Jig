// Top level of the co-simulation test design, as Renode's co-simulated
// peripheral sees it at 0x43C00000:
//
//   0x43C00000  axil_regs       the register block (axil_regs.sv)
//   0x43C01000  jig_axil_regs   a register window that holds what is written
//   0x43C02000  jig_axil_bram   4 KiB of RAM (jig_dp_bram port A)
//
// The jig_* modules are Jig's co-simulation templates; `jig cosim build`
// puts their directory on Verilator's library path.

`timescale 1ns / 1ps
`default_nettype none

module axil_regs_top #(
    parameter int CLK_MHZ = 10
) (
    input  wire logic        clk,
    input  wire logic        rst_n,

    // AXI4-Lite from Renode (the IntegrationLibrary's AxiLite port widths)
    input  wire logic [63:0] awaddr,
    input  wire logic [2:0]  awprot,
    input  wire logic        awvalid,
    output logic             awready,
    input  wire logic [63:0] wdata,
    input  wire logic [7:0]  wstrb,
    input  wire logic        wvalid,
    output logic             wready,
    output logic [1:0]       bresp,
    output logic             bvalid,
    input  wire logic        bready,
    input  wire logic [63:0] araddr,
    input  wire logic [2:0]  arprot,
    input  wire logic        arvalid,
    output logic             arready,
    output logic [63:0]      rdata,
    output logic [1:0]       rresp,
    output logic             rvalid,
    input  wire logic        rready,

    output logic             irq
);
    localparam int N = 3;
    localparam int T_REGS = 0;
    localparam int T_WINDOW = 1;
    localparam int T_RAM = 2;
    localparam logic [31:0] BASE [N] = '{32'h43C0_0000, 32'h43C0_1000, 32'h43C0_2000};
    localparam logic [31:0] SIZE [N] = '{default: 32'h1000};
    localparam int RAM_WORDS = 1024;

    // ------------------------------------------------- Renode port, 32 bits

    logic [31:0] m_awaddr, m_wdata, m_araddr, m_rdata;
    logic [2:0]  m_awprot, m_arprot;
    logic [3:0]  m_wstrb;
    logic [1:0]  m_bresp, m_rresp;
    logic        m_awvalid, m_awready, m_wvalid, m_wready, m_bvalid, m_bready;
    logic        m_arvalid, m_arready, m_rvalid, m_rready;

    jig_axil_byte_lanes u_lanes (
        .clk      (clk),
        .rst_n    (rst_n),
        .s_awaddr (awaddr),
        .s_awprot (awprot),
        .s_awvalid(awvalid),
        .s_awready(awready),
        .s_wdata  (wdata),
        .s_wstrb  (wstrb),
        .s_wvalid (wvalid),
        .s_wready (wready),
        .s_bresp  (bresp),
        .s_bvalid (bvalid),
        .s_bready (bready),
        .s_araddr (araddr),
        .s_arprot (arprot),
        .s_arvalid(arvalid),
        .s_arready(arready),
        .s_rdata  (rdata),
        .s_rresp  (rresp),
        .s_rvalid (rvalid),
        .s_rready (rready),
        .m_awaddr (m_awaddr),
        .m_awprot (m_awprot),
        .m_awvalid(m_awvalid),
        .m_awready(m_awready),
        .m_wdata  (m_wdata),
        .m_wstrb  (m_wstrb),
        .m_wvalid (m_wvalid),
        .m_wready (m_wready),
        .m_bresp  (m_bresp),
        .m_bvalid (m_bvalid),
        .m_bready (m_bready),
        .m_araddr (m_araddr),
        .m_arprot (m_arprot),
        .m_arvalid(m_arvalid),
        .m_arready(m_arready),
        .m_rdata  (m_rdata),
        .m_rresp  (m_rresp),
        .m_rvalid (m_rvalid),
        .m_rready (m_rready)
    );

    // ------------------------------------------------------------- decoder

    logic [N-1:0][31:0] t_awaddr, t_wdata, t_araddr, t_rdata;
    logic [N-1:0][2:0]  t_awprot, t_arprot;
    logic [N-1:0][3:0]  t_wstrb;
    logic [N-1:0][1:0]  t_bresp, t_rresp;
    logic [N-1:0]       t_awvalid, t_awready, t_wvalid, t_wready, t_bvalid, t_bready;
    logic [N-1:0]       t_arvalid, t_arready, t_rvalid, t_rready;

    jig_axil_decoder #(
        .N   (N),
        .BASE(BASE),
        .SIZE(SIZE)
    ) u_decoder (
        .clk      (clk),
        .rst_n    (rst_n),
        .m_awaddr (m_awaddr),
        .m_awprot (m_awprot),
        .m_awvalid(m_awvalid),
        .m_awready(m_awready),
        .m_wdata  (m_wdata),
        .m_wstrb  (m_wstrb),
        .m_wvalid (m_wvalid),
        .m_wready (m_wready),
        .m_bresp  (m_bresp),
        .m_bvalid (m_bvalid),
        .m_bready (m_bready),
        .m_araddr (m_araddr),
        .m_arprot (m_arprot),
        .m_arvalid(m_arvalid),
        .m_arready(m_arready),
        .m_rdata  (m_rdata),
        .m_rresp  (m_rresp),
        .m_rvalid (m_rvalid),
        .m_rready (m_rready),
        .s_awaddr (t_awaddr),
        .s_awprot (t_awprot),
        .s_awvalid(t_awvalid),
        .s_awready(t_awready),
        .s_wdata  (t_wdata),
        .s_wstrb  (t_wstrb),
        .s_wvalid (t_wvalid),
        .s_wready (t_wready),
        .s_bresp  (t_bresp),
        .s_bvalid (t_bvalid),
        .s_bready (t_bready),
        .s_araddr (t_araddr),
        .s_arprot (t_arprot),
        .s_arvalid(t_arvalid),
        .s_arready(t_arready),
        .s_rdata  (t_rdata),
        .s_rresp  (t_rresp),
        .s_rvalid (t_rvalid),
        .s_rready (t_rready)
    );

    // ------------------------------------------------------------- targets

    axil_regs #(
        .BASE   (BASE[T_REGS]),
        .CLK_MHZ(CLK_MHZ)
    ) u_regs (
        .clk    (clk),
        .rst_n  (rst_n),
        .awaddr (t_awaddr [T_REGS]),
        .awvalid(t_awvalid[T_REGS]),
        .awready(t_awready[T_REGS]),
        .wdata  (t_wdata  [T_REGS]),
        .wstrb  (t_wstrb  [T_REGS]),
        .wvalid (t_wvalid [T_REGS]),
        .wready (t_wready [T_REGS]),
        .bresp  (t_bresp  [T_REGS]),
        .bvalid (t_bvalid [T_REGS]),
        .bready (t_bready [T_REGS]),
        .araddr (t_araddr [T_REGS]),
        .arvalid(t_arvalid[T_REGS]),
        .arready(t_arready[T_REGS]),
        .rdata  (t_rdata  [T_REGS]),
        .rresp  (t_rresp  [T_REGS]),
        .rvalid (t_rvalid [T_REGS]),
        .rready (t_rready [T_REGS]),
        .irq    (irq)
    );

    jig_axil_regs #(
        .BASE (BASE[T_WINDOW]),
        .WORDS(1024)
    ) u_window (
        .clk    (clk),
        .rst_n  (rst_n),
        .awaddr (t_awaddr [T_WINDOW]),
        .awvalid(t_awvalid[T_WINDOW]),
        .awready(t_awready[T_WINDOW]),
        .wdata  (t_wdata  [T_WINDOW]),
        .wstrb  (t_wstrb  [T_WINDOW]),
        .wvalid (t_wvalid [T_WINDOW]),
        .wready (t_wready [T_WINDOW]),
        .bresp  (t_bresp  [T_WINDOW]),
        .bvalid (t_bvalid [T_WINDOW]),
        .bready (t_bready [T_WINDOW]),
        .araddr (t_araddr [T_WINDOW]),
        .arvalid(t_arvalid[T_WINDOW]),
        .arready(t_arready[T_WINDOW]),
        .rdata  (t_rdata  [T_WINDOW]),
        .rresp  (t_rresp  [T_WINDOW]),
        .rvalid (t_rvalid [T_WINDOW]),
        .rready (t_rready [T_WINDOW])
    );

    logic                         ram_en;
    logic [3:0]                   ram_we;
    logic [$clog2(RAM_WORDS)-1:0] ram_addr;
    logic [31:0]                  ram_wdata, ram_rdata, unused_b_rdata;

    jig_axil_bram #(
        .BASE (BASE[T_RAM]),
        .WORDS(RAM_WORDS)
    ) u_bram (
        .clk      (clk),
        .rst_n    (rst_n),
        .awaddr   (t_awaddr [T_RAM]),
        .awvalid  (t_awvalid[T_RAM]),
        .awready  (t_awready[T_RAM]),
        .wdata    (t_wdata  [T_RAM]),
        .wstrb    (t_wstrb  [T_RAM]),
        .wvalid   (t_wvalid [T_RAM]),
        .wready   (t_wready [T_RAM]),
        .bresp    (t_bresp  [T_RAM]),
        .bvalid   (t_bvalid [T_RAM]),
        .bready   (t_bready [T_RAM]),
        .araddr   (t_araddr [T_RAM]),
        .arvalid  (t_arvalid[T_RAM]),
        .arready  (t_arready[T_RAM]),
        .rdata    (t_rdata  [T_RAM]),
        .rresp    (t_rresp  [T_RAM]),
        .rvalid   (t_rvalid [T_RAM]),
        .rready   (t_rready [T_RAM]),
        .ram_en   (ram_en),
        .ram_we   (ram_we),
        .ram_addr (ram_addr),
        .ram_wdata(ram_wdata),
        .ram_rdata(ram_rdata)
    );

    jig_dp_bram #(
        .WORDS(RAM_WORDS)
    ) u_ram (
        .clk    (clk),
        .a_en   (ram_en),
        .a_we   (ram_we),
        .a_addr (ram_addr),
        .a_wdata(ram_wdata),
        .a_rdata(ram_rdata),
        .b_en   (1'b0),
        .b_we   (4'b0000),
        .b_addr ('0),
        .b_wdata(32'h0),
        .b_rdata(unused_b_rdata)
    );

    wire unused = ^{t_awprot, t_arprot, unused_b_rdata};
endmodule : axil_regs_top

`default_nettype wire
