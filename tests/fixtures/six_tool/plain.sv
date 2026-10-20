// uvm_pkg::uvm_component in a comment is not a semantic use.
module plain_leaf;
  string text = "uvm_pkg::uvm_component";
endmodule
interface tiny_if;
  logic ready;
endinterface
class Plain;
  function int local_value();
    typedef int local_t;
    local_t value = 3;
    return value;
  endfunction
endclass
