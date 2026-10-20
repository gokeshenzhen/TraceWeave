package diagnostic_pkg;
  class Base;
  endclass
  class Child extends Base;
  endclass
endpackage
module diagnostic_class_top;
  unavailable_diagnostic_module missing_module();
  diagnostic_pkg::Child item;
endmodule
