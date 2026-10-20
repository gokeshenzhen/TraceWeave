package alpha;
  class Same;
  endclass
  class ArgA;
  endclass
  class ArgB;
  endclass
  typedef ArgA ArgAlias;
  class Outer;
    class Nested extends Same;
    endclass
  endclass
  class Generic #(int N = 1, type T = int) extends Same;
    T payload;
    class Inner;
      T item;
    endclass
  endclass
  class TypeOnly #(type T = int);
  endclass
endpackage
package beta;
  class Same extends alpha::Same;
  endclass
  class Cross extends alpha::Outer::Nested;
  endclass
  class BoundA extends alpha::Generic#(3, alpha::ArgA);
  endclass
  class BoundB extends alpha::Generic#(3, alpha::ArgB);
  endclass
  class BoundAlias extends alpha::Generic#(3, alpha::ArgAlias);
  endclass
  class ValueOther extends alpha::Generic#(4, alpha::ArgA);
  endclass
  class TypeBound extends alpha::TypeOnly#(alpha::ArgA);
  endclass
  class External extends external_pkg::Base;
  endclass
endpackage
module class_top;
  alpha::Generic#(3, alpha::ArgA) ga;
  alpha::Generic#(3, alpha::ArgB) gb;
  alpha::Generic#(3, alpha::ArgAlias) g_alias;
  alpha::Generic#(4, alpha::ArgA) gv;
  alpha::Generic#(3, alpha::ArgA)::Inner ia;
  alpha::Generic#(3, alpha::ArgB)::Inner ib;
  beta::BoundA a;
  beta::BoundB b;
  beta::BoundAlias alias_bound;
  beta::ValueOther other;
  beta::TypeBound typed;
  beta::External external_object;
endmodule
