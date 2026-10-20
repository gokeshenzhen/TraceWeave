package p1;
  import uvm_pkg::*;
  `include "included.svh"
  class Base extends uvm_component;
  endclass
  class Same extends Base;
    class Nested;
    endclass
  endclass
endpackage
