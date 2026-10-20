package use_types;
  typedef uvm_pkg::uvm_component component_queue[$];
  component_queue pending;
  typedef struct { uvm_pkg::uvm_component entry; } holder_t;
  holder_t holder;
  int initialization = uvm_pkg::report();
endpackage
