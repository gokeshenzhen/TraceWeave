package v3_types;
  class Base;
  endclass
  class Child extends Base;
  endclass
endpackage
interface v3_bus;
  logic flag;
endinterface
interface v3_bus_unused;
  logic unused_flag;
endinterface
