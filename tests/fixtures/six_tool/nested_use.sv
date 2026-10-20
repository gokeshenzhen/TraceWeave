package nested_use;
  function int nested();
    int value;
    begin : only
      begin : inner
        value = uvm_pkg::report();
      end
    end
    return value;
  endfunction
endpackage
