@kphp_should_fail
KPHP_REQUIRE_CLASS_TYPING=1
/TYPE INFERENCE ERROR/
<?php

// check that default value fixes the type

class A {
  static $cache = false;
}

A::$cache = [new A];
