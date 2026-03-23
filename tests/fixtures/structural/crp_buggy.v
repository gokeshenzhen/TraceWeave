module sbox(output [3:0] dout);
endmodule

module crp_buggy(input [15:0] D, output [15:0] S);
  sbox sbox1(.dout(S[1:4]));
  sbox sbox2(.dout(S[6:9]));
  sbox sbox3(.dout(S[9:12]));
endmodule
