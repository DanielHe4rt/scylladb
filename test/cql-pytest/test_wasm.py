# -*- coding: utf-8 -*-
# Copyright 2021-present ScyllaDB
#
# SPDX-License-Identifier: AGPL-3.0-or-later

#############################################################################
# Tests for user defined functions defined with WebAssembly backend
#############################################################################

from cassandra.protocol import InvalidRequest
from cassandra.cluster import NoHostAvailable
from util import new_test_table, unique_name, new_function, new_aggregate

import pytest
import requests
import re
import os.path

# Can be used for marking functions which require
# WASM support to be compiled into Scylla
@pytest.fixture(scope="module")
def scylla_with_wasm_only(scylla_only, cql, test_keyspace):
    try:
        f42 = unique_name()
        f42_body = f'(module(func ${f42} (param $n i64) (result i64)(return i64.const 42))(export "{f42}" (func ${f42})))'
        res = cql.execute(f"CREATE FUNCTION {test_keyspace}.{f42} (input int) RETURNS NULL ON NULL INPUT RETURNS int LANGUAGE xwasm AS '{f42_body}'")
        cql.execute(f"DROP FUNCTION {test_keyspace}.{f42}")
    except NoHostAvailable as err:
        if "not enabled" in str(err):
            pytest.skip("WASM support was not enabled in Scylla, skipping")
    yield

@pytest.fixture(scope="module")
def table1(cql, test_keyspace):
    table = test_keyspace + "." + unique_name()
    cql.execute("CREATE TABLE " + table +
        "(p bigint PRIMARY KEY, p2 bigint, i int, i2 int, s smallint, s2 smallint, t tinyint, t2 tinyint, d double, f float, bl boolean, txt text)")
    yield table
    cql.execute("DROP TABLE " + table)

# Test that calling a wasm-based fibonacci function works
def test_fib(cql, test_keyspace, table1, scylla_with_wasm_only):
    table = table1
    fib_name = unique_name()
    fib_source = f"""
(module
  (func ${fib_name} (param $n i64) (result i64)
    (if
      (i64.lt_s (local.get $n) (i64.const 2))
      (return (local.get $n))
    )
    (i64.add
      (call ${fib_name} (i64.sub (local.get $n) (i64.const 1)))
      (call ${fib_name} (i64.sub (local.get $n) (i64.const 2)))
    )
  )
  (memory (;0;) 17)
  (export "memory" (memory 0))
  (export "{fib_name}" (func ${fib_name}))
)
"""
    src = f"(input bigint) RETURNS NULL ON NULL INPUT RETURNS bigint LANGUAGE xwasm AS '{fib_source}'"
    with new_function(cql, test_keyspace, src, fib_name):
        cql.execute(f"INSERT INTO {table1} (p) VALUES (10)")
        res = [row for row in cql.execute(f"SELECT {test_keyspace}.{fib_name}(p) AS result FROM {table} WHERE p = 10")]
        assert len(res) == 1 and res[0].result == 55

        cql.execute(f"INSERT INTO {table} (p) VALUES (14)")
        res = [row for row in cql.execute(f"SELECT {test_keyspace}.{fib_name}(p) AS result FROM {table} WHERE p = 14")]
        assert len(res) == 1 and res[0].result == 377

        # This function returns null on null values
        res = [row for row in cql.execute(f"SELECT {test_keyspace}.{fib_name}(p2) AS result FROM {table} WHERE p = 14")]
        assert len(res) == 1 and res[0].result is None

        cql.execute(f"INSERT INTO {table} (p) VALUES (997)")
        # The call request takes too much time and resources, and should therefore fail
        with pytest.raises(InvalidRequest, match="wasm"):
            cql.execute(f"SELECT {test_keyspace}.{fib_name}(p) AS result FROM {table} WHERE p = 997")

# Reads WASM UDF from a file. For a function "foo", the file should be named "foo.wat" and
# be located in the "test/resource/wasm" directory. Supports renaming the exported function.
def read_function_from_file(func_name, rename=None):
    wat_path = os.path.realpath(os.path.join(__file__, f"../../resource/wasm/{func_name}.wat"))
    with open(wat_path, "r") as f:
        if rename:
            return f.read().replace(f'export "{func_name}"', f'export "{rename}"')
        else:
            return f.read()

# Test that calling a fibonacci function that claims to accept null input works.
# Note that since the int field is nullable, it's no longer
# passed as a simple param, but instead as a pointer to a structure with a serialized
# integer underneath - which follows the C ABI for WebAssembly.
# Also, note that CQL serializes integers as big endian, which means that
# WebAssembly should convert to host endianness (assumed little endian here)
# before operating on its native types.
# Compiled from:
# const int WASM_PAGE_SIZE = 64 * 1024;
# const int _scylla_abi = 1;
#
# static long long swap_int64(long long val) {
#     val = ((val << 8) & 0xFF00FF00FF00FF00ULL ) | ((val >> 8) & 0x00FF00FF00FF00FFULL );
#     val = ((val << 16) & 0xFFFF0000FFFF0000ULL ) | ((val >> 16) & 0x0000FFFF0000FFFFULL );
#     return (val << 32) | ((val >> 32) & 0xFFFFFFFFULL);
# }
#
# long long fib_aux(long long n) {
#     if (n < 2) {
#         return n;
#     }
#     return fib_aux(n-1) + fib_aux(n-2);
# }
# long long fib(long long p) {
#     int size = p >> 32;
#     long long* p_val = (long long*)(p & 0xffffffff);
#     // Initialize memory for the return value
#     long long* ret_val = (long long*)(__builtin_wasm_memory_size(0) * WASM_PAGE_SIZE);
#     __builtin_wasm_memory_grow(0, 1); // long long fits in one wasm page
#     if (size == -1) {
#         *ret_val = swap_int64(42);
#     } else {
#         *ret_val = swap_int64(fib_aux(swap_int64(*p_val)));
#     }
#     // 8 is the size of a bigint
#     return (long long)(8ll << 32) | (long long)ret_val;
# }
#
# with:
# $ clang -O2  --target=wasm32 --no-standard-libraries -Wl,--export=fib -Wl,--export=_scylla_abi -Wl,--no-entry fib.c -o fib.wasm
# $ wasm2wat fib.wasm > fib.wat
def test_fib_called_on_null(cql, test_keyspace, table1, scylla_with_wasm_only):
    table = table1
    fib_name = unique_name()
    fib_source = read_function_from_file('fib', fib_name)
    src = f"(input bigint) CALLED ON NULL INPUT RETURNS bigint LANGUAGE xwasm AS '{fib_source}'"
    with new_function(cql, test_keyspace, src, fib_name):
        cql.execute(f"INSERT INTO {table1} (p) VALUES (3)")
        res = [row for row in cql.execute(f"SELECT {test_keyspace}.{fib_name}(p) AS result FROM {table} WHERE p = 3")]
        assert len(res) == 1 and res[0].result == 2

        cql.execute(f"INSERT INTO {table} (p) VALUES (7)")
        res = [row for row in cql.execute(f"SELECT {test_keyspace}.{fib_name}(p) AS result FROM {table} WHERE p = 7")]
        assert len(res) == 1 and res[0].result == 13

        # Special semantics defined for null input in our function is to return "42"
        res = [row for row in cql.execute(f"SELECT {test_keyspace}.{fib_name}(p2) AS result FROM {table} WHERE p = 7")]
        assert len(res) == 1 and res[0].result == 42

        cql.execute(f"INSERT INTO {table} (p) VALUES (997)")
        # The call request takes too much time and resources, and should therefore fail
        with pytest.raises(InvalidRequest, match="wasm"):
          cql.execute(f"SELECT {test_keyspace}.{fib_name}(p) AS result FROM {table} WHERE p = 997")

# Test that an infinite loop gets broken out of eventually
def test_infinite_loop(cql, test_keyspace, table1, scylla_with_wasm_only):
    table = table1
    inf_loop_name = "inf_loop_" + unique_name()
    inf_loop_source = f"""
(module
  (type (;0;) (func (param i32) (result i32)))
  (func ${inf_loop_name} (type 0) (param i32) (result i32)
    loop (result i32)  ;; label = @1
      br 0 (;@1;)
    end)
  (table (;0;) 1 1 funcref)
  (table (;1;) 32 externref)
  (memory (;0;) 17)
  (export "memory" (memory 0))
  (export "{inf_loop_name}" (func ${inf_loop_name}))
  (elem (;0;) (i32.const 0) func)
  (global (;0;) i32 (i32.const 1024))
  (export "_scylla_abi" (global 0))
  (data $.rodata (i32.const 1024) "\\01"))
"""
    src = f"(input int) RETURNS NULL ON NULL INPUT RETURNS int LANGUAGE xwasm AS '{inf_loop_source}'"
    with new_function(cql, test_keyspace, src, inf_loop_name):
        cql.execute(f"INSERT INTO {table} (p,i) VALUES (10, 10)")
        import time
        start = time.monotonic()
        with pytest.raises(InvalidRequest, match="fuel consumed"):
            cql.execute(f"SELECT {test_keyspace}.{inf_loop_name}(i) AS result FROM {table} WHERE p = 10")
        elapsed_s = time.monotonic() - start
        print(f"Breaking the loop took {elapsed_s*1000:.2f}ms")

# Test a wasm function which decreases given double by 1
def test_f64_param(cql, test_keyspace, table1, scylla_with_wasm_only):
    table = table1
    dec_double_name = "dec_double_" + unique_name()
    dec_double_source = f"""
(module
  (type (;0;) (func (param f64) (result f64)))
  (func ${dec_double_name} (type 0) (param f64) (result f64)
    local.get 0
    f64.const -0x1p+0 (;=-1;)
    f64.add)
  (table (;0;) 1 1 funcref)
  (table (;1;) 32 externref)
  (memory (;0;) 17)
  (export "memory" (memory 0))
  (export "{dec_double_name}" (func ${dec_double_name}))
  (elem (;0;) (i32.const 0) func)
  (global (;0;) i32 (i32.const 1024))
  (export "_scylla_abi" (global 0))
  (data $.rodata (i32.const 1024) "\\01"))
"""
    src = f"(input double) RETURNS NULL ON NULL INPUT RETURNS double LANGUAGE xwasm AS '{dec_double_source}'"
    with new_function(cql, test_keyspace, src, dec_double_name):
        cql.execute(f"INSERT INTO {table} (p,d) VALUES (17,17.015625)")
        res = [row for row in cql.execute(f"SELECT {test_keyspace}.{dec_double_name}(d) AS result FROM {table} WHERE p = 17")]
        assert len(res) == 1 and res[0].result == 16.015625

# Test a wasm function which increases given float by 1
def test_f32_param(cql, test_keyspace, table1, scylla_with_wasm_only):
    table = table1
    inc_float_name = "inc_float_" + unique_name()
    inc_float_source = f"""
(module
  (type (;0;) (func (param f32) (result f32)))
  (func ${inc_float_name} (type 0) (param f32) (result f32)
    local.get 0
    f32.const 0x1p+0 (;=1;)
    f32.add)
  (table (;0;) 1 1 funcref)
  (table (;1;) 32 externref)
  (memory (;0;) 17)
  (export "memory" (memory 0))
  (export "{inc_float_name}" (func ${inc_float_name}))
  (elem (;0;) (i32.const 0) func)
  (global (;0;) i32 (i32.const 1024))
  (export "_scylla_abi" (global 0))
  (data $.rodata (i32.const 1024) "\\01"))
"""
    src = f"(input float) RETURNS NULL ON NULL INPUT RETURNS float LANGUAGE xwasm AS '{inc_float_source}'"
    with new_function(cql, test_keyspace, src, inc_float_name):
        cql.execute(f"INSERT INTO {table} (p, f) VALUES (121, 121.00390625)")
        res = [row for row in cql.execute(f"SELECT {test_keyspace}.{inc_float_name}(f) AS result FROM {table} WHERE p = 121")]
        assert len(res) == 1 and res[0].result == 122.00390625

# Test a wasm function which operates on booleans
def test_bool_negate(cql, test_keyspace, table1, scylla_with_wasm_only):
    table = table1
    negate_name = "negate_" + unique_name()
    negate_source = f"""
(module
  (type (;0;) (func (param i32) (result i32)))
  (func ${negate_name} (type 0) (param i32) (result i32)
    local.get 0
    i32.eqz)
  (table (;0;) 1 1 funcref)
  (table (;1;) 32 externref)
  (memory (;0;) 17)
  (export "memory" (memory 0))
  (export "{negate_name}" (func ${negate_name}))
  (elem (;0;) (i32.const 0) func)
  (global (;0;) i32 (i32.const 1024))
  (export "_scylla_abi" (global 0))
  (data $.rodata (i32.const 1024) "\\01"))
"""
    src = f"(input boolean) RETURNS NULL ON NULL INPUT RETURNS boolean LANGUAGE xwasm AS '{negate_source}'"
    with new_function(cql, test_keyspace, src, negate_name):
        cql.execute(f"INSERT INTO {table} (p, bl) VALUES (19, true)")
        cql.execute(f"INSERT INTO {table} (p, bl) VALUES (21, false)")
        res = [row for row in cql.execute(f"SELECT {test_keyspace}.{negate_name}(bl) AS result FROM {table} WHERE p = 19")]
        assert len(res) == 1 and res[0].result == False
        res = [row for row in cql.execute(f"SELECT {test_keyspace}.{negate_name}(bl) AS result FROM {table} WHERE p = 21")]
        assert len(res) == 1 and res[0].result == True

# Test wasm functions which operate on 8bit and 16bit integers,
# which are simulated by 32bit integers by wasm anyway
def test_short_ints(cql, test_keyspace, table1, scylla_with_wasm_only):
    table = table1
    plus_name = "plus_" + unique_name()
    plus_source = f"""
(module
  (type (;0;) (func (param i32 i32) (result i32)))
  (func ${plus_name} (type 0) (param i32 i32) (result i32)
    local.get 1
    local.get 0
    i32.add)
  (table (;0;) 1 1 funcref)
  (table (;1;) 32 externref)
  (memory (;0;) 17)
  (export "memory" (memory 0))
  (export "{plus_name}" (func ${plus_name}))
  (elem (;0;) (i32.const 0) func)
  (global (;0;) i32 (i32.const 1024))
  (export "_scylla_abi" (global 0))
  (data $.rodata (i32.const 1024) "\\01"))
"""
    src = f"(input tinyint, input2 tinyint) RETURNS NULL ON NULL INPUT RETURNS tinyint LANGUAGE xwasm AS '{plus_source}'"
    with new_function(cql, test_keyspace, src, plus_name):
        cql.execute(f"INSERT INTO {table} (p, t, t2, s, s2) VALUES (42, 42, 24, 33, 55)")
        cql.execute(f"INSERT INTO {table} (p, t, t2, s, s2) VALUES (43, 120, 112, 32000, 24001)")
        res = [row for row in cql.execute(f"SELECT {test_keyspace}.{plus_name}(t, t2) AS result FROM {table} WHERE p = 42")]
        assert len(res) == 1 and res[0].result == 66
        # Overflow is fine
        res = [row for row in cql.execute(f"SELECT {test_keyspace}.{plus_name}(t, t2) AS result FROM {table} WHERE p = 43")]
        assert len(res) == 1 and res[0].result == -24
    # A similar run for 16bit ints - note that the exact same source code is used
    src = f"(input smallint, input2 smallint) RETURNS NULL ON NULL INPUT RETURNS smallint LANGUAGE xwasm AS '{plus_source}'"
    with new_function(cql, test_keyspace, src, plus_name):
        res = [row for row in cql.execute(f"SELECT {test_keyspace}.{plus_name}(s, s2) AS result FROM {table} WHERE p = 42")]
        assert len(res) == 1 and res[0].result == 88
        # Overflow is fine
        res = [row for row in cql.execute(f"SELECT {test_keyspace}.{plus_name}(s, s2) AS result FROM {table} WHERE p = 43")]
        assert len(res) == 1 and res[0].result == -9535
    # Check whether we can use a different function under the same name
    plus42_source = read_function_from_file('plus42', plus_name)
    plus42_src = f"(input smallint, input2 smallint) RETURNS NULL ON NULL INPUT RETURNS smallint LANGUAGE xwasm AS '{plus42_source}'"
    # Repeat a number of times so the wasm instances get cached on all shards
    with new_function(cql, test_keyspace, src, plus_name):
        for _ in range(100):
            res = [row for row in cql.execute(f"SELECT {test_keyspace}.{plus_name}(s, s2) AS result FROM {table} WHERE p = 42")]
            assert len(res) == 1 and res[0].result == 88
    with new_function(cql, test_keyspace, plus42_src, plus_name):
        for _ in range(100):
            res = [row for row in cql.execute(f"SELECT {test_keyspace}.{plus_name}(s, s2) AS result FROM {table} WHERE p = 42")]
            assert len(res) == 1 and res[0].result == 88 + 42

    # Check whether we can use another function with the same name but different signature
    plusplus_source = f"""
(module
  (type (;0;) (func (param i32 i32 i32) (result i32)))
  (func ${plus_name} (type 0) (param i32 i32 i32) (result i32)
    local.get 2
    local.get 1
    i32.add
    local.get 0
    i32.add)
  (memory (;0;) 2)
  (global (;0;) i32 (i32.const 1024))
  (export "memory" (memory 0))
  (export "{plus_name}" (func ${plus_name}))
  (global (;0;) i32 (i32.const 1024))
  (export "_scylla_abi" (global 0))
  (data (;0;) (i32.const 1024) "\\01"))
"""
    plusplus_src = f"(input smallint, input2 smallint, input3 smallint) RETURNS NULL ON NULL INPUT RETURNS smallint LANGUAGE xwasm AS '{plusplus_source}'"
    # Repeat a number of times so the wasm instances get cached on all shards
    with new_function(cql, test_keyspace, src, plus_name):
        for _ in range(100):
            res = [row for row in cql.execute(f"SELECT {test_keyspace}.{plus_name}(s, s2) AS result FROM {table} WHERE p = 42")]
            assert len(res) == 1 and res[0].result == 88
    with new_function(cql, test_keyspace, plusplus_src, plus_name):
        for _ in range(100):
            res = [row for row in cql.execute(f"SELECT {test_keyspace}.{plus_name}(s, s, s2) AS result FROM {table} WHERE p = 42")]
            assert len(res) == 1 and res[0].result == 121

# Test that passing a large number of params works fine
def test_9_params(cql, test_keyspace, table1, scylla_with_wasm_only):
    table = table1
    sum9_name = "sum9_" + unique_name()
    sum9_source = f"""
(module
  (type (;0;) (func (param i32 i32 i32 i32 i32 i32 i32 i32 i32) (result i32)))
  (func ${sum9_name} (type 0) (param i32 i32 i32 i32 i32 i32 i32 i32 i32) (result i32)
    local.get 1
    local.get 0
    i32.add
    local.get 2
    i32.add
    local.get 3
    i32.add
    local.get 4
    i32.add
    local.get 5
    i32.add
    local.get 6
    i32.add
    local.get 7
    i32.add
    local.get 8
    i32.add)
  (table (;0;) 1 1 funcref)
  (table (;1;) 32 externref)
  (memory (;0;) 17)
  (export "memory" (memory 0))
  (export "{sum9_name}" (func ${sum9_name}))
  (elem (;0;) (i32.const 0) func)
  (global (;0;) i32 (i32.const 1024))
  (export "_scylla_abi" (global 0))
  (data $.rodata (i32.const 1024) "\\01"))

"""
    src = f"(a int, b int, c int, d int, e int, f int, g int, h int, i int) RETURNS NULL ON NULL INPUT RETURNS int LANGUAGE xwasm AS '{sum9_source}'"
    with new_function(cql, test_keyspace, src, sum9_name):
        cql.execute(f"INSERT INTO {table} (p, i, i2) VALUES (777, 1,2)")
        res = [row for row in cql.execute(f"SELECT {test_keyspace}.{sum9_name}(i,i2,i2,i,i2,i,i2,i,i2) AS result FROM {table} WHERE p = 777")]
        assert len(res) == 1 and res[0].result == 14

# Test a wasm function which takes 2 arguments - a base and a power - and returns base**power
def test_pow(cql, test_keyspace, table1, scylla_with_wasm_only):
    table = table1
    pow_name = "pow_" + unique_name()
    pow_source = f"""
(module
  (type (;0;) (func (param i32 i32) (result i32)))
  (func ${pow_name} (type 0) (param i32 i32) (result i32)
    (local i32 i32)
    i32.const 1
    local.set 2
    block  ;; label = @1
      block  ;; label = @2
        block  ;; label = @3
          local.get 1
          br_table 2 (;@1;) 1 (;@2;) 0 (;@3;)
        end
        local.get 1
        local.set 2
        i32.const 1
        local.set 1
        loop  ;; label = @3
          local.get 0
          i32.const 1
          local.get 2
          i32.const 1
          i32.and
          select
          local.get 1
          i32.mul
          local.set 1
          local.get 2
          i32.const 3
          i32.gt_u
          local.set 3
          local.get 0
          local.get 0
          i32.mul
          local.set 0
          local.get 2
          i32.const 1
          i32.shr_u
          local.set 2
          local.get 3
          br_if 0 (;@3;)
        end
      end
      local.get 0
      local.get 1
      i32.mul
      local.set 2
    end
    local.get 2)
  (table (;0;) 1 1 funcref)
  (table (;1;) 32 externref)
  (memory (;0;) 17)
  (export "memory" (memory 0))
  (global (;0;) i32 (i32.const 1024))
  (export "{pow_name}" (func ${pow_name}))
  (elem (;0;) (i32.const 0) func)
  (export "_scylla_abi" (global 0))
  (data $.rodata (i32.const 1024) "\\01"))
"""
    src = f"(base int, pow int) RETURNS NULL ON NULL INPUT RETURNS int LANGUAGE xwasm AS '{pow_source}'"
    with new_function(cql, test_keyspace, src, pow_name):
        cql.execute(f"INSERT INTO {table} (p, i, i2) VALUES (311, 3, 11)")
        res = [row for row in cql.execute(f"SELECT {test_keyspace}.{pow_name}(i, i2) AS result FROM {table} WHERE p = 311")]
        assert len(res) == 1 and res[0].result == 177147

# Test that only compilable input is accepted
def test_compilable(cql, test_keyspace, table1, scylla_with_wasm_only):
    table = table1
    wrong_source = f"""
Dear wasmtime compiler, please return a function which returns its float argument increased by 1
"""
    with pytest.raises(InvalidRequest, match="Compilation failed"):
      cql.execute(f"CREATE FUNCTION {test_keyspace}.i_was_not_exported (input float) RETURNS NULL ON NULL INPUT RETURNS float LANGUAGE xwasm "
                f"AS '{wrong_source}'")

# Test that not exporting a function with matching name
# results in an error
def test_not_exported(cql, test_keyspace, table1, scylla_with_wasm_only):
    table = table1
    wrong_source = f"""
(module
  (type (;0;) (func (param f32) (result f32)))
  (func $i_was_not_exported (type 0) (param f32) (result f32)
    local.get 0
    f32.const 0x1p+0 (;=1;)
    f32.add)
  (table (;0;) 1 1 funcref)
  (table (;1;) 32 externref)
  (memory (;0;) 17)
  (elem (;0;) (i32.const 0) func))
"""
    with pytest.raises(InvalidRequest, match="not found"):
        cql.execute(f"CREATE FUNCTION {test_keyspace}.i_was_not_exported (input float) RETURNS NULL ON NULL INPUT RETURNS float LANGUAGE xwasm "
                f"AS '{wrong_source}'")

# Test that trying to use something that is exported, but is not a function, won't work
def test_not_a_function(cql, test_keyspace, table1, scylla_with_wasm_only):
    table = table1
    wrong_source = f"""
(module
  (type (;0;) (func (param f32) (result f32)))
  (func $i_was_not_exported (type 0) (param f32) (result f32)
    local.get 0
    f32.const 0x1p+0 (;=1;)
    f32.add)
  (table (;0;) 1 1 funcref)
  (table (;1;) 32 externref)
  (memory (;0;) 17)
  (global (;0;) (mut i32) (i32.const 1048576))
  (global (;1;) i32 (i32.const 1024))
  (export "memory" (memory 0))
  (elem (;0;) (i32.const 0) func)
  (export "_scylla_abi" (global 1))
  (data $.rodata (i32.const 1024) "\\01"))
"""
    with pytest.raises(InvalidRequest, match="not a function"):
        cql.execute(f"CREATE FUNCTION {test_keyspace}.memory (input float) RETURNS NULL ON NULL INPUT RETURNS float LANGUAGE xwasm "
                f"AS '{wrong_source}'")

# Test that the function should accept only the correct number and types of params
def test_validate_params(cql, test_keyspace, table1, scylla_with_wasm_only):
    table = table1
    inc_float_name = "inc_float_" + unique_name()
    inc_float_source = f"""
(module
  (type (;0;) (func (param f32) (result f32)))
  (func ${inc_float_name} (type 0) (param f32) (result f32)
    local.get 0
    f32.const 0x1p+0 (;=1;)
    f32.add)
  (table (;0;) 1 1 funcref)
  (table (;1;) 32 externref)
  (memory (;0;) 17)
  (global (;0;) i32 (i32.const 1024))
  (export "memory" (memory 0))
  (export "{inc_float_name}" (func ${inc_float_name}))
  (elem (;0;) (i32.const 0) func)
  (export "_scylla_abi" (global 0))
  (data $.rodata (i32.const 1024) "\\01"))
"""
    src = f"(input int) RETURNS NULL ON NULL INPUT RETURNS float LANGUAGE xwasm AS '{inc_float_source}'"
    with new_function(cql, test_keyspace, src, inc_float_name):
        cql.execute(f"INSERT INTO {table} (p, i, f, txt) VALUES (700, 7, 7., 'oi')")
        with pytest.raises(InvalidRequest, match="type mismatch"):
            cql.execute(f"SELECT {test_keyspace}.{inc_float_name}(i) AS result FROM {table} WHERE p = 700")
    src = f"(input text) RETURNS NULL ON NULL INPUT RETURNS int LANGUAGE xwasm AS '{inc_float_source}'"
    with new_function(cql, test_keyspace, src, inc_float_name):
        with pytest.raises(InvalidRequest, match="failed"):
            cql.execute(f"SELECT {test_keyspace}.{inc_float_name}(txt) AS result FROM {table} WHERE p = 700")
    src = f"(input float) RETURNS NULL ON NULL INPUT RETURNS int LANGUAGE xwasm AS '{inc_float_source}'"
    with new_function(cql, test_keyspace, src, inc_float_name):
        with pytest.raises(InvalidRequest, match="Expected i32, got f32"):
            cql.execute(f"SELECT {test_keyspace}.{inc_float_name}(f) AS result FROM {table} WHERE p = 700")
        with pytest.raises(InvalidRequest, match="number.*arguments"):
            cql.execute(f"SELECT {test_keyspace}.{inc_float_name}(i, f) AS result FROM {table} WHERE p = 700")

# Test that calling a wasm-based function on a string works.
# The function doubles the string: dog -> dogdog.
# Created with:
# const int WASM_PAGE_SIZE = 64 * 1024;
# const int _scylla_abi = 1;

# long long dbl(long long par) {
#    int size = par >> 32;
#    int position = par & 0xffffffff;
#    int orig_size = __builtin_wasm_memory_size(0) * WASM_PAGE_SIZE;
#    __builtin_wasm_memory_grow(0, 1 + (2 * size - 1) / WASM_PAGE_SIZE);
#    char* p = (char*)0;
#    for (int i = 0; i < size; ++i) {
#        p[orig_size + i] = p[position + i];
#        p[orig_size + size + i] = p[position + i];
#    }
#    long long ret = ((long long)2 * size << 32) | (long long)orig_size;
#    return ret;
# }
# ... and compiled with
# clang --target=wasm32 --no-standard-libraries -Wl,--export=dbl -Wl,--export=_scylla_abi -Wl,--no-entry demo.c -o demo.wasm
# wasm2wat demo.wasm > demo.wat

def test_word_double(cql, test_keyspace, table1, scylla_with_wasm_only):
    table = table1
    dbl_name = unique_name()
    dbl_source = read_function_from_file('dbl', dbl_name)
    src = f"(input text) RETURNS NULL ON NULL INPUT RETURNS text LANGUAGE xwasm AS '{dbl_source}'"
    with new_function(cql, test_keyspace, src, dbl_name):
        cql.execute(f"INSERT INTO {table1} (p, txt) VALUES (1000, 'doggo')")
        res = [row for row in cql.execute(f"SELECT {test_keyspace}.{dbl_name}(txt) AS result FROM {table} WHERE p = 1000")]
        assert len(res) == 1 and res[0].result == 'doggodoggo'

        cql.execute(f"INSERT INTO {table} (p, txt) VALUES (1001, 'cat42')")
        res = [row for row in cql.execute(f"SELECT {test_keyspace}.{dbl_name}(txt) AS result FROM {table} WHERE p = 1001")]
        assert len(res) == 1 and res[0].result == 'cat42cat42'
        res = [row for row in cql.execute(f"SELECT {test_keyspace}.{dbl_name}(txt) AS result FROM {table} WHERE p IN (1000, 1001)")]
        assert len(res) == 2 and (res[0].result == 'cat42cat42' and res[1].result == 'doggodoggo' or res[0].result == 'doggodoggo' and res[1].result == 'cat42cat42')

# Test that calling a wasm-based function works with ABI version 2.
# The function returns the input. It's compatible with all data types represented by size + pointer.
# Created with:
# # extern "C" {
#     fn malloc(size: usize) -> usize;
#     fn free(ptr: *mut usize);
# }

# #[no_mangle]
# pub unsafe extern "C" fn _scylla_malloc(size: usize) -> u32 {
#     malloc(size) as u32
# }

# #[no_mangle]
# pub unsafe extern "C" fn _scylla_free(ptr: *mut usize) {
#     free(ptr)
# }

# #[no_mangle]
# pub static _scylla_abi: u32 = 2;

# #[no_mangle]
# pub extern "C" fn return_input(sizeptr: u64) -> u64 {
#     sizeptr
# }

# ... and compiled with
# cargo build --target=wasm32-wasi --release
# wasm2wat return_input.wasm > return_input.wat

def test_abi_v2(cql, test_keyspace, table1, scylla_with_wasm_only):
    table = table1
    ri_name = unique_name()
    ri_source = read_function_from_file('return_input', ri_name)
    text_src = f"(input text) RETURNS NULL ON NULL INPUT RETURNS text LANGUAGE xwasm AS '{ri_source}'"
    with new_function(cql, test_keyspace, text_src, ri_name):
        cql.execute(f"INSERT INTO {table1} (p, txt) VALUES (2000, 'doggo')")
        res = [row for row in cql.execute(f"SELECT {test_keyspace}.{ri_name}(txt) AS result FROM {table} WHERE p = 2000")]
        assert len(res) == 1 and res[0].result == 'doggo'

@pytest.fixture(scope="module")
def metrics(request, scylla_with_wasm_only):
    url = request.config.getoption('host')
    # The Prometheus API is on port 9180, and always http
    url = 'http://' + url + ':9180/metrics'
    resp = requests.get(url)
    if resp.status_code != 200:
        pytest.skip('Metrics port 9180 is not available')
    yield url

def get_metrics(metrics):
    response = requests.get(metrics)
    assert response.status_code == 200
    return response.text

def get_metric(metrics, name, requested_labels=None, the_metrics=None):
    if not the_metrics:
        the_metrics = get_metrics(metrics)
    total = 0.0
    lines = re.compile('^'+name+'{.*$', re.MULTILINE)
    for match in re.findall(lines, the_metrics):
        a = match.split()
        metric = a[0]
        val = float(a[1])
        # Check if match also matches the requested labels
        if requested_labels:
            # we know metric begins with name{ and ends with } - the labels
            # are what we have between those
            got_labels = metric[len(name)+1:-1].split(',')
            # Check that every one of the requested labels is in got_labels:
            for k, v in requested_labels.items():
                if not f'{k}="{v}"' in got_labels:
                    # No match for requested label, skip this metric (python
                    # doesn't have "continue 2" so let's just set val to 0...
                    val = 0
                    break
        total += float(val)
    return total

# Test that calling a wasm-based aggregate works.
# The aggregate calculates the average of integers.
# Created with scalar function:
# const int _scylla_abi = 1;

# static int swap_int32(int val) {
#     val = ((val << 8) & 0xFF00FF00 ) | ((val >> 8) & 0x00FF00FF );
#     return (val << 16) | ((val >> 16) & 0xFFFF);
# }

# long long sum(long long acc, long long p) {
#     int size = p >> 32;
#     int accsize = acc >> 32;
#     if (size != 4 || accsize != 16) {
#         return acc;
#     }
#     int p_val = swap_int32(*(int*)(p & 0xffffffff));
#     int* acc_val_cnt = (int*)((acc + 4) & 0xffffffff);
#     int* acc_val_sum = (int*)((acc + 12) & 0xffffffff);
#     *acc_val_cnt = swap_int32(1 + swap_int32(*acc_val_cnt));
#     *acc_val_sum = swap_int32(p_val + swap_int32(*acc_val_sum));
#     return acc;
# }

# ... and compiled with
# clang --target=wasm32 --no-standard-libraries -Wl,--export=sum -Wl,--export=_scylla_abi -Wl,--no-entry scalar.c -o scalar.wasm
# wasm2wat scalar.wasm > scalar.wat

# And final function:
# const int _scylla_abi = 1;

# static int swap_int32(int val) {
#     val = ((val << 8) & 0xFF00FF00 ) | ((val >> 8) & 0x00FF00FF );
#     return (val << 16) | ((val >> 16) & 0xFFFF);
# }

# long long div(long long acc) {
#     int accsize = acc >> 32;
#     if (accsize != 16) {
#         long long ret = -1;
#         return ret << 32;
#     }
#     int* acc_val_cnt = (int*)((acc + 4) & 0xffffffff);
#     int* acc_val_sum = (int*)((acc + 12) & 0xffffffff);
#     int cnt = swap_int32(*acc_val_cnt);
#     int sum = swap_int32(*acc_val_sum);
#     float ret_val = (float)sum / cnt;
#     *acc_val_cnt = swap_int32(*((unsigned int*)&ret_val));
#     acc = 4ll << 32 | (int)acc_val_cnt;
#     return acc;
# }


# ... and compiled with
# clang --target=wasm32 --no-standard-libraries -Wl,--export=div -Wl,--export=_scylla_abi -Wl,--no-entry final.c -o final.wasm
# wasm2wat final.wasm > final.wat


def test_UDA(cql, test_keyspace, table1, scylla_with_wasm_only, metrics):
    table = table1
    sum_name = unique_name()
    sum_source = f"""
(module
  (type (;0;) (func (param i64 i64) (result i64)))
  (func (;0;) (type 0) (param i64 i64) (result i64)
    (local i32 i32 i32)
    block  ;; label = @1
      local.get 1
      i64.const -4294967296
      i64.and
      i64.const 17179869184
      i64.ne
      br_if 0 (;@1;)
      local.get 0
      i64.const -4294967296
      i64.and
      i64.const 68719476736
      i64.ne
      br_if 0 (;@1;)
      local.get 1
      i32.wrap_i64
      i32.load
      local.set 3
      local.get 0
      i32.wrap_i64
      local.tee 4
      i32.const 4
      i32.add
      local.tee 2
      local.get 2
      i32.load
      local.tee 2
      i32.const 24
      i32.shl
      local.get 2
      i32.const 8
      i32.shl
      i32.const 16711680
      i32.and
      i32.or
      local.get 2
      i32.const 8
      i32.shr_u
      i32.const 65280
      i32.and
      local.get 2
      i32.const 24
      i32.shr_u
      i32.or
      i32.or
      i32.const 1
      i32.add
      local.tee 2
      i32.const 24
      i32.shl
      local.get 2
      i32.const 8
      i32.shl
      i32.const 16711680
      i32.and
      i32.or
      local.get 2
      i32.const 8
      i32.shr_u
      i32.const 65280
      i32.and
      local.get 2
      i32.const 24
      i32.shr_u
      i32.or
      i32.or
      i32.store
      local.get 4
      i32.const 12
      i32.add
      local.tee 2
      local.get 2
      i32.load
      local.tee 2
      i32.const 24
      i32.shl
      local.get 2
      i32.const 8
      i32.shl
      i32.const 16711680
      i32.and
      i32.or
      local.get 2
      i32.const 8
      i32.shr_u
      i32.const 65280
      i32.and
      local.get 2
      i32.const 24
      i32.shr_u
      i32.or
      i32.or
      local.get 3
      i32.const 8
      i32.shl
      i32.const 16711680
      i32.and
      local.get 3
      i32.const 24
      i32.shl
      i32.or
      local.get 3
      i32.const 8
      i32.shr_u
      i32.const 65280
      i32.and
      local.get 3
      i32.const 24
      i32.shr_u
      i32.or
      i32.or
      i32.add
      local.tee 3
      i32.const 24
      i32.shl
      local.get 3
      i32.const 8
      i32.shl
      i32.const 16711680
      i32.and
      i32.or
      local.get 3
      i32.const 8
      i32.shr_u
      i32.const 65280
      i32.and
      local.get 3
      i32.const 24
      i32.shr_u
      i32.or
      i32.or
      i32.store
    end
    local.get 0)
  (memory (;0;) 2)
  (global (;0;) i32 (i32.const 1024))
  (export "memory" (memory 0))
  (export "{sum_name}" (func 0))
  (export "_scylla_abi" (global 0))
  (data (;0;) (i32.const 1024) "\\01"))
"""
    sum_src = f"(acc tuple<int, int>, input int) CALLED ON NULL INPUT RETURNS tuple<int,int> LANGUAGE xwasm AS '{sum_source}'"

    div_name = unique_name()
    div_source = f"""
(module
  (type (;0;) (func (param i64) (result i64)))
  (func (;0;) (type 0) (param i64) (result i64)
    (local i32 i32 i64)
    i64.const -4294967296
    local.set 3
    local.get 0
    i64.const -4294967296
    i64.and
    i64.const 68719476736
    i64.eq
    if (result i64)  ;; label = @1
      local.get 0
      i32.wrap_i64
      local.tee 1
      i32.const 4
      i32.add
      local.tee 2
      local.get 1
      i32.const 12
      i32.add
      i32.load
      local.tee 1
      i32.const 24
      i32.shl
      local.get 1
      i32.const 8
      i32.shl
      i32.const 16711680
      i32.and
      i32.or
      local.get 1
      i32.const 8
      i32.shr_u
      i32.const 65280
      i32.and
      local.get 1
      i32.const 24
      i32.shr_u
      i32.or
      i32.or
      f32.convert_i32_s
      local.get 2
      i32.load
      local.tee 1
      i32.const 24
      i32.shl
      local.get 1
      i32.const 8
      i32.shl
      i32.const 16711680
      i32.and
      i32.or
      local.get 1
      i32.const 8
      i32.shr_u
      i32.const 65280
      i32.and
      local.get 1
      i32.const 24
      i32.shr_u
      i32.or
      i32.or
      f32.convert_i32_s
      f32.div
      i32.reinterpret_f32
      local.tee 1
      i32.const 24
      i32.shl
      local.get 1
      i32.const 8
      i32.shl
      i32.const 16711680
      i32.and
      i32.or
      local.get 1
      i32.const 8
      i32.shr_u
      i32.const 65280
      i32.and
      local.get 1
      i32.const 24
      i32.shr_u
      i32.or
      i32.or
      i32.store
      local.get 2
      i64.extend_i32_s
      i64.const 17179869184
      i64.or
    else
      local.get 3
    end)
  (memory (;0;) 2)
  (global (;0;) i32 (i32.const 1024))
  (export "memory" (memory 0))
  (export "{div_name}" (func 0))
  (export "_scylla_abi" (global 0))
  (data (;0;) (i32.const 1024) "\\01"))
"""
    div_src = f"(acc tuple<int, int>) CALLED ON NULL INPUT RETURNS float LANGUAGE xwasm AS '{div_source}'"
    for i in range(20):
      cql.execute(f"INSERT INTO {table} (p, i, i2) VALUES ({i}, {i}, {i})")
    with new_function(cql, test_keyspace, sum_src, sum_name), new_function(cql, test_keyspace, div_src, div_name):
        agg_body = f"(int) SFUNC {sum_name} STYPE tuple<int,int> FINALFUNC {div_name} INITCOND (0,0)"
        hits_before = get_metric(metrics, 'scylla_user_functions_cache_hits')
        with new_aggregate(cql, test_keyspace, agg_body) as custom_avg:
          custom_res = [row for row in cql.execute(f"SELECT {test_keyspace}.{custom_avg}(i) AS result FROM {table}")]
          avg_res = [row for row in cql.execute(f"SELECT avg(cast(i as float)) AS result FROM {table}")]
          assert custom_res == avg_res
          hits_after = get_metric(metrics, 'scylla_user_functions_cache_hits')
          assert hits_after - hits_before >= 1
          misses_before_reuse = get_metric(metrics, 'scylla_user_functions_cache_misses')
          for i in range(100):
            custom_res = [row for row in cql.execute(f"SELECT {test_keyspace}.{custom_avg}(i2) AS result FROM {table}")]
            avg_res = [row for row in cql.execute(f"SELECT avg(cast(i2 as float)) AS result FROM {table}")]
            assert custom_res == avg_res

          res = [row for row in cql.execute(f"SELECT i2 AS result FROM {table}")]

          misses_after_reuse = get_metric(metrics, 'scylla_user_functions_cache_misses')
          # Sum of hits and misses should equal the total number of UDF calls, which is one row function call for
          # each of the table elements and one additional call for the final function, both multiplied by the number of repetitions.
          assert misses_after_reuse - misses_before_reuse + get_metric(metrics, 'scylla_user_functions_cache_hits') - hits_after == 100 * (1 + len(res))
          # Each shard has its own cache, so check if at least one shard reuses the cache without excessive missing.
          # Misses caused by replacing an instance that can no longer be used can be justified, we estimate that this happens
          # once every 7 calls for row function calls (memory has 2 initial pages, 2 pages are added for 2 arguments, max instance
          # size = 16 pages) and once every 14 calls for final function calls (1 page is added for each call in this case).
          # Additionally, 2 misses are expected for each of the 2 shards, for the first calls of row and final functions.
          assert misses_after_reuse - misses_before_reuse <= 4 + 100 * len(res) / 7 + 100 / 14

# Test that wasm instances are removed from the cache when:
# - a single instance is too big
# - the instances in cache consume too much memory in total
# - the instance hasn't been used for a long time
# FIXME: shorten the wait time when such configuration becomes possible

# The function grows the memory by n pages and returns n.
# Compiled from:
# const int _scylla_abi = 1;

# int grow_mem(int val) {
#     __builtin_wasm_memory_grow(0, val);
#     return val;
# }
#
# with:
# $ clang -O2  --target=wasm32 --no-standard-libraries -Wl,--export=grow_mem -Wl,--export=_scylla_abi -Wl,--no-entry grow_mem.c -o grow_mem.wasm
# $ wasm2wat grow_mem.wasm > grow_mem.wat
@pytest.mark.skip(reason="slow test, remove skip to try it anyway")
def test_mem_grow(cql, test_keyspace, table1, scylla_with_wasm_only, metrics):
    table = table1
    mem_grow_name = "mem_grow_" + unique_name()
    mem_grow_source = f"""
(module
  (type (;0;) (func (param i32) (result i32)))
  (func ${mem_grow_name} (type 0) (param i32) (result i32)
    local.get 0
    memory.grow)
  (memory (;0;) 2)
  (global (;0;) i32 (i32.const 1024))
  (export "memory" (memory 0))
  (export "{mem_grow_name}" (func ${mem_grow_name}))
  (export "_scylla_abi" (global 0))
  (data (;0;) (i32.const 1024) "\\01"))
"""
    src = f"(pages int) RETURNS NULL ON NULL INPUT RETURNS int LANGUAGE xwasm AS '{mem_grow_source}'"
    with new_function(cql, test_keyspace, src, mem_grow_name):
        cql.execute(f"INSERT INTO {table} (p, i) VALUES (8, 8)")
        for i in range(512):
            cql.execute(f"SELECT {test_keyspace}.{mem_grow_name}(i) AS result FROM {table} WHERE p = 8")
            # We grow the memory by 8 pages, each page is 64KiB, so in total we'll grow 512*8*64*1024=256MiB
            # The default memory limit is 128MiB, so assert we're staying under that anyway
            assert(get_metric(metrics, 'scylla_user_functions_cache_total_size') <= 128*1024*1024)

        # Wait for all instances to time out
        import time
        time.sleep(10)
        assert(get_metric(metrics, 'scylla_user_functions_cache_total_size') == 0)

        cql.execute(f"INSERT INTO {table} (p, i) VALUES (30, 30)")
        cql.execute(f"SELECT {test_keyspace}.{mem_grow_name}(i) AS result FROM {table} WHERE p = 30")
        # A memory of 30+ pages is too big for the cache, so assert that it's not cached
        assert(get_metric(metrics, 'scylla_user_functions_cache_total_size') == 0)

        cql.execute(f"INSERT INTO {table} (p, i) VALUES (100, 100)")
        # A memory of 100+ pages is too big for an instance in the cache, it is rejected
        map_res = [row for row in cql.execute(f"SELECT {test_keyspace}.{mem_grow_name}(i) AS result FROM {table} WHERE p = 100")]
        assert len(map_res) == 1 and map_res[0].result == -1

# Test that all wasm instance entries are removed from the cache when the correlating UDF is dropped,
# to avoid using excessive memory for unused UDFs.
# The first UDF used returns the input integer.
def test_drop(cql, test_keyspace, table1, scylla_with_wasm_only, metrics):
    table = table1
    ret_name = "ret_" + unique_name()
    ret_source = f"""
(module
  (type (;0;) (func (param i64) (result i64)))
  (func $ret_name (type 0) (param i64) (result i64)
    local.get 0)
  (memory (;0;) 2)
  (global (;0;) i32 (i32.const 1024))
  (export "memory" (memory 0))
  (export "ret_name" (func $ret_name))
  (export "_scylla_abi" (global 0))
  (data (;0;) (i32.const 1024) "\\01"))
"""
    src = f"(input bigint) RETURNS NULL ON NULL INPUT RETURNS bigint LANGUAGE xwasm AS '{ret_source}'"
    cql.execute(f"INSERT INTO {table} (p) VALUES (42)")
    for _ in range(10):
        ret_name = "ret_" + unique_name()
        with new_function(cql, test_keyspace, src.replace('ret_name', ret_name), ret_name):
            cql.execute(f"SELECT {test_keyspace}.{ret_name}(p) AS result FROM {table} WHERE p = 42")
            assert(get_metric(metrics, 'scylla_user_functions_cache_instace_count_any') > 0)
        assert(get_metric(metrics, 'scylla_user_functions_cache_instace_count_any') == 0)

# Test that we can use counters as the return type of a WASM UDF.
def test_counter(cql, test_keyspace, scylla_only):
    schema = "p int, c counter, PRIMARY KEY (p)"
    ri_counter_name = unique_name()
    ri_counter_source = read_function_from_file('return_input', ri_counter_name)
    src = f"(input counter) RETURNS NULL ON NULL INPUT RETURNS counter LANGUAGE xwasm AS '{ri_counter_source}'"
    with new_test_table(cql, test_keyspace, schema) as table:
        cql.execute(f"UPDATE {table} SET c = c + 2  WHERE p = 42;")
        with new_function(cql, test_keyspace, src, ri_counter_name):
            assert cql.execute(f"SELECT {ri_counter_name}(c) AS result FROM {table} WHERE p = 42").one().result == 2
            cql.execute(f"UPDATE {table} SET c = c + 1  WHERE p = 42;")
            cql.execute(f"UPDATE {table} SET c = c - 4  WHERE p = 42;")
            assert cql.execute(f"SELECT {ri_counter_name}(c) AS result FROM {table} WHERE p = 42").one().result == -1
