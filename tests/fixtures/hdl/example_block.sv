// A free-running counter: the RTL of the example-zynq fixture's block.
module example_block (
    input  wire        clk,
    input  wire        rst_n,
    output reg  [31:0] count
);
    always @(posedge clk) begin
        if (!rst_n) count <= 32'd0;
        else        count <= count + 32'd1;
    end
endmodule
