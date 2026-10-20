module service_top;
  plain_leaf u_plain();
  tiny_if bus();
  version_a a();
  version_b b();
  p1::Same first;
  p2::Same second;
  int status;
  initial status = uvm_pkg::report();
  initial begin : qualified_only
    begin : inner
      status = uvm_pkg::report();
    end
  end
endmodule
