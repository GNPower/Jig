// Top level of the example-zynq co-simulated block (test fixture).
module example_top (
    input  wire        clk,
    input  wire        rst_n,
    output wire [31:0] count
);
    example_block block (.clk(clk), .rst_n(rst_n), .count(count));
endmodule
