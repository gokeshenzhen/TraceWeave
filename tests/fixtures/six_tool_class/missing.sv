package broken_pkg;
  class MissingChild extends MissingDefinition;
  endclass
endpackage
module missing_class_top;
  broken_pkg::MissingChild item;
endmodule
